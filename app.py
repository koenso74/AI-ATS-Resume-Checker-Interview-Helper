import json
import os
import re
import tempfile
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from openai import OpenAI
from docx import Document
from docx.shared import Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
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
ALLOWED_EDIT_ACTIONS = {"create", "clarify", "rewrite", "reorder", "normalize", "remove"}
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


SECTION_HEADINGS = {
    "summary": "summary",
    "professional summary": "summary",
    "objective": "summary",
    "experience": "experience",
    "work experience": "experience",
    "professional experience": "experience",
    "education": "education",
    "skills": "skills",
    "technical skills": "skills",
    "relevant coursework and technical skills": "skills",
    "projects": "projects",
    "certifications": "certifications",
}
SECTION_LABELS = {
    "summary": "Professional Summary",
    "experience": "Experience",
    "education": "Education",
    "skills": "Technical Skills",
    "projects": "Projects",
    "certifications": "Certifications",
}


def parse_resume_sections(resume):
    sections = {"contact": []}
    order = ["contact"]
    current = "contact"
    for line in resume.splitlines():
        heading = re.sub(r"[^a-z ]", "", line.strip().lower())
        if heading in SECTION_HEADINGS and len(line.strip()) < 60:
            current = SECTION_HEADINGS[heading]
            if current not in sections:
                sections[current] = []
                order.append(current)
        else:
            sections.setdefault(current, []).append(line.rstrip())
    return sections, order


def render_resume_sections(sections, order):
    rendered = []
    for section in order:
        content = [line.strip() for line in sections.get(section, [])]
        while content and not content[-1]:
            content.pop()
        if not any(content):
            continue
        if section != "contact":
            rendered.append(SECTION_LABELS.get(section, section.title()))
        rendered.extend(content)
        rendered.append("")
    return "\n".join(rendered).strip()


def clean_section_edit_text(section, text):
    """Remove headings returned inside section content; the renderer owns headings."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    section_heading = SECTION_LABELS.get(section, section.title()).lower()
    cleaned = []
    for line in lines:
        normalized = re.sub(r"[^a-z ]", "", line.strip().lower())
        if normalized in SECTION_HEADINGS and (
            normalized == section_heading.lower()
            or SECTION_HEADINGS.get(normalized) == section
        ):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def validate_edit_decisions(raw_decisions, current_resume):
    """Validate structured decisions locally and against the source resume."""
    try:
        payload = json.loads(raw_decisions)
    except json.JSONDecodeError as exc:
        return [], [f"invalid JSON: {exc.msg}"]

    decisions = payload.get("edits")
    if not isinstance(decisions, list):
        return [], ["edits must be a JSON array"]

    accepted, rejected = [], []
    seen_sections = set()
    for index, decision in enumerate(decisions[:6]):
        if not isinstance(decision, dict):
            rejected.append(f"edit {index + 1}: must be an object")
            continue
        section = str(decision.get("section", "")).strip().lower()
        action = str(decision.get("action", "")).strip().lower()
        new_text = str(decision.get("new_text", "")).strip()
        if section not in ALLOWED_EDIT_SECTIONS:
            rejected.append(f"edit {index + 1}: disallowed section")
        elif action not in ALLOWED_EDIT_ACTIONS:
            rejected.append(f"edit {index + 1}: disallowed action")
        elif not new_text:
            rejected.append(f"edit {index + 1}: empty new_text")
        elif len(new_text) > 4000:
            rejected.append(f"edit {index + 1}: new_text too long")
        elif section in seen_sections:
            rejected.append(f"edit {index + 1}: contradictory duplicate section decision")
        else:
            seen_sections.add(section)
            accepted.append({
                "section": section,
                "action": action,
                "new_text": new_text,
            })
    if len(decisions) > 6:
        rejected.append("extra edits beyond the maximum of 6 were rejected")

    if accepted:
        try:
            fact_check = json.loads(llm(
                prompts.VALIDATE_EDIT_DECISIONS_PROMPT.format(
                    resume=current_resume,
                    edits=json.dumps(accepted),
                ),
                json_mode=True,
            ))
            for item in fact_check.get("rejected", []):
                index = int(item.get("index", -1))
                reason = str(item.get("reason", "failed fact validation"))
                if 0 <= index < len(accepted):
                    rejected.append(f"edit {index + 1}: {reason}")
                    accepted[index]["_invalid"] = True
            accepted = [item for item in accepted if not item.get("_invalid")]
        except Exception as exc:
            rejected.append(f"fact validation failed: {exc}")
            accepted = []
    return accepted, rejected


def apply_edit_decisions(current_resume, decisions):
    sections, order = parse_resume_sections(current_resume)
    applied, rejected = [], []
    formatting_applied = False
    for decision in decisions:
        section = decision["section"]
        new_text = decision["new_text"].strip()
        if section == "formatting":
            format_sections, format_order = parse_resume_sections(new_text)
            candidate = render_resume_sections(format_sections, format_order)
            if candidate != current_resume.strip():
                current_resume = candidate
                formatting_applied = True
                applied.append(decision)
            else:
                rejected.append("formatting decision produced no text change")
            continue
        if section not in sections and decision["action"] != "create":
            rejected.append(f"{section}: cannot edit a missing section without create")
            continue
        if section not in sections:
            sections[section] = []
            if section == "summary":
                order.insert(1, section)
            elif section == "skills":
                order.insert(order.index("experience") + 1 if "experience" in order else len(order), section)
            else:
                order.append(section)
        cleaned_text = clean_section_edit_text(section, new_text)
        if not cleaned_text:
            rejected.append(f"{section}: edit contained only a section heading")
            continue
        sections[section] = cleaned_text.splitlines()
        applied.append(decision)
    if formatting_applied:
        updated = current_resume.strip()
    elif sections:
        updated = render_resume_sections(sections, order)
    else:
        updated = current_resume.strip()
    return updated, applied, rejected


def build_resume_docx(resume_text):
    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = Inches(0.4)
    section.left_margin = section.right_margin = Inches(0.55)

    normal = document.styles["Normal"]
    normal.font.name = "Arial"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
    normal.font.size = Pt(9.2)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = 0.92

    def add_bar(paragraph):
        paragraph.paragraph_format.space_before = Pt(4)
        paragraph.paragraph_format.space_after = Pt(1)
        paragraph.paragraph_format.keep_with_next = True
        p_pr = paragraph._p.get_or_add_pPr()
        borders = p_pr.find(qn("w:pBdr"))
        if borders is None:
            borders = OxmlElement("w:pBdr")
            p_pr.append(borders)
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "8")
        bottom.set(qn("w:space"), "2")
        bottom.set(qn("w:color"), "0E9F6E")
        borders.append(bottom)

    def is_heading(value):
        clean = value.strip()
        if not clean or len(clean) > 48:
            return False
        lowered = clean.lower().rstrip(":")
        return lowered in SECTION_HEADINGS or (
            clean.upper() == clean and any(char.isalpha() for char in clean)
        )

    first_content = True
    for line in (line.strip() for line in resume_text.splitlines()):
        if not line:
            if document.paragraphs:
                document.paragraphs[-1].paragraph_format.space_after = Pt(2.5)
            continue
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.keep_together = True
        paragraph.paragraph_format.widow_control = True
        run = paragraph.add_run(line)
        if first_content:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run.bold = True
            run.font.size = Pt(14)
            first_content = False
        elif is_heading(line):
            run.bold = True
            run.font.size = Pt(10.2)
            add_bar(paragraph)
        else:
            run.font.size = Pt(9.2)
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
    requested_priority_fixes = data.get("priority_fixes", {})
    if not original:
        return jsonify({"error": "A resume is required."}), 400

    try:
        work_dir = Path(tempfile.mkdtemp(prefix="ats-improve-"))
        original_path = work_dir / "original-resume.txt"
        original_path.write_text(original, encoding="utf-8")
        score_before_result = score_resume(original_path.read_text(encoding="utf-8"))
        score_before = int(score_before_result.get("overall_score", 0))
        initial_score = score_before
        current_resume = original
        best_resume = original
        best_result = score_before_result
        best_iteration = 0
        best_path = work_dir / "best-resume.txt"
        iteration_log = []
        decision_log = []

        for iteration in range(1, MAX_ITERATIONS + 1):
            if iteration == 1 and isinstance(requested_priority_fixes, dict) and (
                requested_priority_fixes.get("issues") or requested_priority_fixes.get("suggestions")
            ):
                priority_issues = requested_priority_fixes.get("issues", [])
                priority_suggestions = requested_priority_fixes.get("suggestions", [])
                gaps = [
                    f"{issue.get('severity', 'medium')}: {issue.get('title', '')} - {issue.get('detail', '')}"
                    for issue in priority_issues if isinstance(issue, dict)
                ] + [str(item).strip() for item in priority_suggestions if str(item).strip()]
                gaps = gaps[:12]
            else:
                gaps = score_gaps(score_before_result)

            raw_decisions = llm(
                prompts.IMPROVEMENT_DECISIONS_PROMPT.format(
                    resume=current_resume,
                    gaps=json.dumps(gaps),
                ),
                json_mode=True,
            )
            edits, rejected = validate_edit_decisions(raw_decisions, current_resume)
            decision_log.append({"iteration": iteration, "raw": raw_decisions, "rejected": rejected})

            if not edits:
                iteration_log.append({
                    "iteration": iteration,
                    "edits": [],
                    "raw_decisions": raw_decisions,
                    "validation": {"accepted": [], "rejected": rejected},
                    "output_file": str(original_path),
                    "score_before": score_before,
                    "score_after": score_before,
                    "best_score": int(best_result.get("overall_score", 0)),
                    "stopped": "no valid edits",
                    "applied": False,
                    "priority_gaps_used": gaps if iteration == 1 else [],
                    "gaps_targeted": gaps,
                    "gaps_remaining": score_gaps(score_before_result),
                })
                break

            revised_resume, applied, apply_rejected = apply_edit_decisions(current_resume, edits)
            rejected.extend(apply_rejected)
            iteration_path = work_dir / f"iteration-{iteration}-resume.txt"
            iteration_path.write_text(revised_resume, encoding="utf-8")
            output_changed = revised_resume.strip() != current_resume.strip()
            if not output_changed or not applied:
                rejected.append("no approved edit produced a concrete text change")
                iteration_log.append({
                    "iteration": iteration,
                    "edits": edits,
                    "raw_decisions": raw_decisions,
                    "validation": {"accepted": edits, "rejected": rejected},
                    "output_file": str(iteration_path),
                    "score_before": score_before,
                    "score_after": score_before,
                    "best_score": int(best_result.get("overall_score", 0)),
                    "stopped": "no concrete applied change",
                    "applied": False,
                    "priority_gaps_used": gaps if iteration == 1 else [],
                    "gaps_targeted": gaps,
                    "gaps_remaining": score_gaps(score_before_result),
                })
                break

            # Read and score the newly written iteration file, never the old text.
            scored_resume = iteration_path.read_text(encoding="utf-8")
            score_after_result = score_resume(scored_resume)
            score_after = int(score_after_result.get("overall_score", 0))
            best_score = int(best_result.get("overall_score", 0))
            if score_after >= best_score:
                best_resume = scored_resume
                best_result = score_after_result
                best_iteration = iteration
                best_path.write_text(best_resume, encoding="utf-8")

            iteration_log.append({
                "iteration": iteration,
                "edits": edits,
                "raw_decisions": raw_decisions,
                "validation": {"accepted": edits, "rejected": rejected},
                "output_file": str(iteration_path),
                "score_before": score_before,
                "score_after": score_after,
                "best_score": int(best_result.get("overall_score", 0)),
                "applied": True,
                "priority_gaps_used": gaps if iteration == 1 else [],
                "gaps_targeted": gaps,
                "gaps_remaining": score_gaps(score_after_result),
            })

            current_resume = scored_resume
            score_before_result = score_after_result
            score_before = score_after
            if score_after - iteration_log[-1]["score_before"] < 2:
                iteration_log[-1]["stopped"] = "plateau or improvement below 2 points"
                break

        if not best_path.exists():
            best_path.write_text(best_resume, encoding="utf-8")
        if best_path == original_path:
            raise RuntimeError("best output path must differ from original input path")
        for item in iteration_log:
            item["selected"] = item.get("iteration") == best_iteration
        changelog = {
            "format_version": "1.0",
            "report": "ATS resume improvement changelog",
            "initial_score": initial_score,
            "best_score": int(best_result.get("overall_score", 0)),
            "score_change": int(best_result.get("overall_score", 0)) - initial_score,
            "best_iteration": best_iteration,
            "max_iterations": MAX_ITERATIONS,
            "gaps_addressed": [
                {
                    "iteration": item["iteration"],
                    "targeted": item.get("gaps_targeted", []),
                    "remaining_after_iteration": item.get("gaps_remaining", []),
                }
                for item in iteration_log
            ],
            "iterations": [
                {
                    "iteration": item["iteration"],
                    "score_before": item["score_before"],
                    "score_after": item["score_after"],
                    "best_score": item["best_score"],
                    "applied": item["applied"],
                    "selected": item.get("selected", False),
                    "changes": [
                        {
                            "section": edit.get("section"),
                            "action": edit.get("action"),
                            "new_text": edit.get("new_text"),
                        }
                        for edit in item.get("edits", [])
                    ],
                    "rejected_changes": item.get("validation", {}).get("rejected", []),
                    "gaps_targeted": item.get("gaps_targeted", []),
                    "gaps_remaining": item.get("gaps_remaining", []),
                    "stopped": item.get("stopped"),
                }
                for item in iteration_log
            ],
            "disclaimer": "A higher ATS score does not guarantee a better resume.",
        }
        return jsonify({
            "original_resume": original,
            "best_resume": best_path.read_text(encoding="utf-8"),
            "original_file_path": str(original_path),
            "best_file_path": str(best_path),
            "best_iteration": best_iteration,
            "initial_score": initial_score,
            "best_score": int(best_result.get("overall_score", 0)),
            "best_result": best_result,
            "iteration_log": iteration_log,
            "decision_log": decision_log,
            "initial_gaps_used": iteration_log[0].get("priority_gaps_used", []) if iteration_log else [],
            "max_iterations": MAX_ITERATIONS,
            "best_version_json": {
                "resume": best_path.read_text(encoding="utf-8"),
                "ats_score": int(best_result.get("overall_score", 0)),
                "ats_result": best_result,
                "best_iteration": best_iteration,
                "disclaimer": "A higher ATS score does not guarantee a better resume.",
            },
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
