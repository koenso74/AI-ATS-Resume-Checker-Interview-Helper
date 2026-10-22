import json
import os
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from openai import OpenAI
from docx import Document
from pypdf import PdfReader

import prompts

load_dotenv()

app = Flask(__name__)


def get_client():
    api_key = os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY or OPENAI_API_KEY is not configured")
    base_url = os.getenv("OPENAI_BASE_URL")
    return OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)


MODEL = os.getenv(
    "OPENAI_MODEL",
    "llama-3.3-70b-versatile" if os.getenv("GROQ_API_KEY") else "gpt-4o-mini",
)


def llm(prompt, system=None, json_mode=False):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    kwargs = {"model": MODEL, "messages": messages, "temperature": 0.3}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = get_client().chat.completions.create(**kwargs)
    return response.choices[0].message.content


@app.route("/")
def index():
    return render_template("index.html")


def extract_document_text(upload):
    """Extract text from a PDF, DOCX, or plain-text upload in memory."""
    extension = Path(upload.filename or "").suffix.lower()
    content = upload.read()
    if not content:
        raise ValueError("The uploaded file is empty.")
    if len(content) > 8 * 1024 * 1024:
        raise ValueError("Please upload a file smaller than 8 MB.")

    if extension == ".pdf":
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)
    elif extension == ".docx":
        document = Document(BytesIO(content))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    elif extension in {".txt", ".md"}:
        text = content.decode("utf-8", errors="replace")
    else:
        raise ValueError("Supported file types are PDF, DOCX, TXT, and MD. Legacy .doc files are not supported.")

    text = text.strip()
    if not text:
        raise ValueError("No readable text was found in the uploaded file.")
    return text


@app.route("/api/extract", methods=["POST"])
def extract():
    upload = request.files.get("document")
    if not upload:
        return jsonify({"error": "Choose a PDF, DOCX, TXT, or MD file."}), 400
    try:
        return jsonify({"text": extract_document_text(upload)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    resume = data.get("resume", "").strip()
    if not resume:
        return jsonify({"error": "A resume is required."}), 400

    try:
        result = json.loads(llm(prompts.ATS_FORMAT_PROMPT.format(resume=resume), json_mode=True))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify(result)


@app.route("/api/interview/start", methods=["POST"])
def interview_start():
    data = request.get_json(silent=True) or {}
    resume = data.get("resume", "").strip()
    if not resume:
        return jsonify({"error": "Upload or paste a resume before starting the interview."}), 400
    system = prompts.INTERVIEWER_RESUME_SYSTEM_PROMPT.format(resume=resume)
    try:
        first_q = llm("Ask the first interview question based on this candidate's resume.", system=system)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"question": first_q, "system": system})


@app.route("/api/interview/reply", methods=["POST"])
def interview_reply():
    data = request.get_json(silent=True) or {}
    system = data.get("system", "")
    history = data.get("history", [])
    convo = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in history)
    try:
        reply = llm(
            f"Conversation so far:\n{convo}\n\nCandidate's last answer is above. "
            "Give brief feedback, then ask the next question.",
            system=system,
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"reply": reply})


if __name__ == "__main__":
    app.run(debug=True)
