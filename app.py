import json
import os
import re
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from openai import OpenAI
from docx import Document
from docx.shared import Inches
import pdfplumber

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
    "openai/gpt-oss-120b" if os.getenv("GROQ_API_KEY") else "gpt-4o-mini",
)
MAX_ITERATIONS = 3
ALLOWED_EDIT_SECTIONS = {
    "contact", "summary", "experience", "education", "skills",
    "projects", "certifications", "formatting",
}
ALLOWED_EDIT_ACTIONS = {"clarify", "rewrite", "reorder", "normalize", "remove"}
FORBIDDEN_EDIT_LANGUAGE = re.compile(
    r"\b(invent|fabricat|make up|create a new|add (?:a|an|new)|assume)\b",
    re.IGNORECASE,
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


def score_resume(resume):
    return json.loads(llm(prompts.ATS_FORMAT_PROMPT.format(resume=resume), json_mode=True))


def score_gaps(score_result):
    gaps = []
    for issue in score_result.get("issues", []):
        gaps.append(f"{issue.get('severity', 'medium')}: {issue.get('title', '')} - {issue.get('detail', '')}")
    gaps.extend(str(item) for item in score_result.get("suggestions", []))
    return gaps


def validate_edit_decisions(raw_decisions):
    """Keep only safe, bounded edit decisions and return rejection details."""
    try:
        payload = json.loads(raw_decisions)
    except json.JSONDecodeError as exc:
        return [], [f"invalid JSON: {exc.msg}"]

    decisions = payload.get("edits")
    if not isinstance(decisions, list):
        return [], ["edits must be a JSON array"]

    accepted, rejected = [], []
    for index, decision in enumerate(decisions[:8]):
        if not isinstance(decision, dict):
            rejected.append(f"edit {index + 1}: must be an object")
            continue
        section = str(decision.get("section", "")).strip().lower()
        action = str(decision.get("action", "")).strip().lower()
        instructions = str(decision.get("instructions", "")).strip()
        if section not in ALLOWED_EDIT_SECTIONS:
            rejected.append(f"edit {index + 1}: disallowed section")
        elif action not in ALLOWED_EDIT_ACTIONS:
            rejected.append(f"edit {index + 1}: disallowed action")
        elif not instructions:
            rejected.append(f"edit {index + 1}: empty instructions")
        elif len(instructions) > 600:
            rejected.append(f"edit {index + 1}: instructions too long")
        elif FORBIDDEN_EDIT_LANGUAGE.search(instructions):
            rejected.append(f"edit {index + 1}: asks for unsupported invented content")
        else:
            accepted.append({
                "section": section,
                "action": action,
                "instructions": instructions,
            })
    if len(decisions) > 8:
        rejected.append("extra edits beyond the maximum of 8 were rejected")
    return accepted, rejected


def build_resume_docx(resume_text):
    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = Inches(0.65)
    section.left_margin = section.right_margin = Inches(0.7)
    for index, line in enumerate(resume_text.splitlines()):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_after = 0
        run = paragraph.add_run(line.strip())
        if index == 0 or line.strip().upper() == line.strip() and len(line.strip()) < 45:
            run.bold = True
    output = BytesIO()
    document.save(output)
    output.seek(0)
    return output


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
        # Natural text flow avoids the very large padding runs that layout=True
        # inserts to reproduce a PDF's visual columns.
        with pdfplumber.open(BytesIO(content)) as pdf:
            pages = [
                page.extract_text(
                    layout=False,
                    x_tolerance=2,
                    y_tolerance=3,
                    use_text_flow=True,
                ) or ""
                for page in pdf.pages
            ]
        text = "\n\n".join(pages)
    elif extension == ".docx":
        document = Document(BytesIO(content))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    elif extension in {".txt", ".md"}:
        text = content.decode("utf-8", errors="replace")
    else:
        raise ValueError("Supported file types are PDF, DOCX, TXT, and MD. Legacy .doc files are not supported.")

    # Normalize extraction artifacts while retaining useful section breaks.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?<=\w)-\n(?=\w)", "", text)
    cleaned_lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
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
        result = score_resume(resume)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify(result)


@app.route("/api/improve", methods=["POST"])
def improve_resume():
    data = request.get_json(silent=True) or {}
    original = data.get("resume", "").strip()
    if not original:
        return jsonify({"error": "A resume is required."}), 400

    try:
        score_before_result = score_resume(original)
        score_before = int(score_before_result.get("overall_score", 0))
        initial_score = score_before
        current_resume = original
        best_resume = original
        best_result = score_before_result
        iteration_log = []
        decision_log = []

        for iteration in range(1, MAX_ITERATIONS + 1):
            gaps = score_gaps(score_before_result)
            raw_decisions = llm(
                prompts.IMPROVEMENT_DECISIONS_PROMPT.format(
                    resume=current_resume,
                    gaps=json.dumps(gaps),
                ),
                json_mode=True,
            )
            edits, rejected = validate_edit_decisions(raw_decisions)
            decision_log.append({"iteration": iteration, "rejected": rejected})

            if not edits:
                iteration_log.append({
                    "iteration": iteration,
                    "edits": [],
                    "rejected": rejected,
                    "score_before": score_before,
                    "score_after": score_before,
                    "stopped": "no valid edits",
                })
                break

            revised_resume = llm(
                prompts.APPLY_IMPROVEMENT_EDITS_PROMPT.format(
                    resume=current_resume,
                    edits=json.dumps(edits),
                )
            ).strip()
            if not revised_resume or len(revised_resume) < 80:
                rejected.append("editor returned empty or unusably short resume")
                iteration_log.append({
                    "iteration": iteration,
                    "edits": edits,
                    "rejected": rejected,
                    "score_before": score_before,
                    "score_after": score_before,
                    "stopped": "invalid revised resume",
                })
                break

            score_after_result = score_resume(revised_resume)
            score_after = int(score_after_result.get("overall_score", 0))
            iteration_log.append({
                "iteration": iteration,
                "edits": edits,
                "rejected": rejected,
                "score_before": score_before,
                "score_after": score_after,
            })

            if score_after > int(best_result.get("overall_score", 0)):
                best_resume = revised_resume
                best_result = score_after_result
            current_resume = revised_resume
            score_before_result = score_after_result
            score_before = score_after

            if score_after - iteration_log[-1]["score_before"] < 2:
                iteration_log[-1]["stopped"] = "plateau or improvement below 2 points"
                break

        return jsonify({
            "original_resume": original,
            "best_resume": best_resume,
            "initial_score": initial_score,
            "best_score": int(best_result.get("overall_score", 0)),
            "best_result": best_result,
            "iteration_log": iteration_log,
            "decision_log": decision_log,
            "max_iterations": MAX_ITERATIONS,
            "disclaimer": "A higher ATS score does not guarantee a better resume.",
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/improve/download", methods=["POST"])
def improve_download():
    data = request.get_json(silent=True) or {}
    resume = data.get("resume", "").strip()
    if not resume:
        return jsonify({"error": "An improved resume is required."}), 400
    try:
        return send_file(
            build_resume_docx(resume),
            as_attachment=True,
            download_name="improved-resume.docx",
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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
        result = json.loads(llm(prompts.INTERVIEW_REPLY_PROMPT.format(conversation=convo), system=system, json_mode=True))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    result["reply"] = f"{result.get('feedback', '')}\n\nNext question: {result.get('next_question', '')}"
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True)
