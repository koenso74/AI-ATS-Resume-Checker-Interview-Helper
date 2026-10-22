import json
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from openai import OpenAI

import prompts

load_dotenv()

app = Flask(__name__)


def get_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=api_key)


MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


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


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    resume = data.get("resume", "").strip()
    jd = data.get("job_description", "").strip()
    if not resume or not jd:
        return jsonify({"error": "Resume and job description are required."}), 400

    try:
        kw = json.loads(llm(prompts.KEYWORD_EXTRACTION_PROMPT.format(job_description=jd), json_mode=True))
        must_haves = kw.get("must_have_skills", [])
        score = json.loads(llm(prompts.ATS_SCORING_PROMPT.format(
            must_haves=", ".join(must_haves), resume=resume
        ), json_mode=True))
        suggestions = llm(prompts.SUGGESTIONS_PROMPT.format(
            score=score.get("match_score", 0),
            missing=", ".join(score.get("missing", [])) or "none",
            reasoning=score.get("reasoning", ""),
        ))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify({"keywords": kw, "score": score, "suggestions": suggestions})


@app.route("/api/interview/start", methods=["POST"])
def interview_start():
    data = request.get_json(silent=True) or {}
    jd = data.get("job_description", "").strip()
    if not jd:
        return jsonify({"error": "Job description is required."}), 400
    system = prompts.INTERVIEWER_SYSTEM_PROMPT.format(job_description=jd)
    try:
        first_q = llm("Ask the first interview question for this role.", system=system)
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
