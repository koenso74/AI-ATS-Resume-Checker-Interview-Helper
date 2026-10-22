# AI Resume ATS Checker & Interview Helper

An MVP Flask app that scores a resume against a job description and provides a tailored mock interview. It demonstrates iterative refinement (keyword extraction → ATS scoring → suggestions), persona prompting, few-shot examples, and structured JSON output.

## Local setup

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
copy .env.example .env
```

Add an OpenAI API key to `.env`, then run:

```bash
python app.py
```

Open http://127.0.0.1:5000.

## Deploy to Vercel

Import this GitHub repository into Vercel, set the `OPENAI_API_KEY` environment variable, and deploy. The `api/index.py` entry point and `vercel.json` configure Flask as a Vercel Python function.

## Privacy and limitations

Resumes and job descriptions are processed in memory per request and are not persisted by this app. Do not treat the score as a guarantee of any real ATS system's behavior; real ATS implementations vary widely.
