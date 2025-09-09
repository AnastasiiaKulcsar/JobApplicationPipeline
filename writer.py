# writer.py
# Generate role-specific resume bullets + a cover letter from jobs.db

import os
import re
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Tuple

# OpenAI SDK v1 style
from openai import OpenAI  # pip install openai>=1.0.0
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Optional CLI convenience
try:
    import typer
    app = typer.Typer(add_completion=False)
except Exception:
    app = None  # CLI will be unavailable, but functions still work

BASE_RESUME = "resume_base.md"
OUTDIR = Path("docs")

PROMPT_BULLETS = """You are a concise résumé bullet writer.
Given MY_SKILLS and JOB_DESC, produce 4 bullet points using action verbs with measurable impact, tailored to the role.
Avoid exaggeration or fabrications; only use content consistent with MY_SKILLS.

MY_SKILLS:
{skills}

JOB_DESC:
{job}

Return bullets as a markdown list.
"""

PROMPT_COVER = """Write a one-page cover letter for the role below.
Tone: warm, confident, specific. Reference 2–3 concrete role requirements and match them with my experience.
Do not invent experience. End with a short call to action.

MY_SKILLS:
{skills}

ROLE:
{job}
"""

# ---- your skills profile (edit me) ----
MY_SKILLS = {
    "summary": "Software engineer focusing on Python, ML/NLP, data tooling, and automation.",
    "languages": ["python", "typescript", "go"],
    "ml": ["nlp", "transformers", "retrieval", "llmops"],
    "tools": ["postgres", "docker", "kubernetes", "playwright", "aws", "gcp"],
    "years_exp": {"python": 5, "typescript": 3}
}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = TAG_RE.sub(" ", s)
    return WS_RE.sub(" ", s).strip()


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            job_id TEXT PRIMARY KEY,
            applied_at TEXT,
            resume_path TEXT,
            cover_path TEXT,
            notes TEXT
        )
    """)
    conn.commit()


def get_job(conn: sqlite3.Connection, job_id: str):
    row = conn.execute("""
        SELECT source, raw_json, company, title
        FROM jobs WHERE id=?
    """, (job_id,)).fetchone()
    if not row:
        raise ValueError(f"Job not found: {job_id}")
    return row


def extract_description(payload: dict, source: str) -> str:
    if source == "greenhouse":
        return strip_html(payload.get("content", "")) + " " + (payload.get("title") or "")
    if source == "lever":
        desc = payload.get("descriptionPlain") or payload.get("description") or ""
        desc = strip_html(desc)
        title = payload.get("text") or payload.get("title") or ""
        return f"{desc} {title}"
    # Fallback to any text
    return strip_html(json.dumps(payload, ensure_ascii=False))


def complete(prompt: str, model: str = "gpt-4o-mini") -> str:
    # Chat Completions (current SDK) — messages in, text out
    resp = client.chat.completions.create(  # API: https://platform.openai.com/docs/api-reference/chat/create
        model=model,
        temperature=0.4,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content or ""


def write_files(job_id: str, company: str, title: str, bullets_md: str, cover_md: str) -> Tuple[Path, Path]:
    OUTDIR.mkdir(parents=True, exist_ok=True)

    cover_path = OUTDIR / f"{job_id.replace(':','_')}_cover.md"
    cover_path.write_text(f"# Cover Letter — {company} — {title}\n\n{cover_md}\n", encoding="utf-8")

    base = Path(BASE_RESUME).read_text(encoding="utf-8") if Path(BASE_RESUME).exists() else "# Resume\n\n"
    tailored = base + f"\n\n## Role-specific Highlights ({company} — {title})\n\n{bullets_md}\n"
    resume_path = OUTDIR / f"{job_id.replace(':','_')}_resume.md"
    resume_path.write_text(tailored, encoding="utf-8")

    return resume_path, cover_path


def generate_for(job_id: str, db: str = "jobs.db") -> Tuple[str, str]:
    conn = sqlite3.connect(db)
    ensure_tables(conn)

    source, raw_json, company, title = get_job(conn, job_id)
    payload = json.loads(raw_json or "{}")
    desc = extract_description(payload, source or "")

    skills_json = json.dumps(MY_SKILLS, indent=2, ensure_ascii=False)
    bullets = complete(PROMPT_BULLETS.format(skills=skills_json, job=desc))
    cover = complete(PROMPT_COVER.format(skills=skills_json, job=desc))

    resume_path, cover_path = write_files(job_id, company or "Company", title or "Role", bullets, cover)

    conn.execute("""
        INSERT OR REPLACE INTO applications(job_id, applied_at, resume_path, cover_path, notes)
        VALUES (?, date('now'), ?, ?, ?)
    """, (job_id, str(resume_path), str(cover_path), "generated"))
    conn.commit()
    conn.close()

    return str(resume_path), str(cover_path)


# --- CLI ---
if app:
    @app.command("write")
    def cli_write(job_id: str, db: str = "jobs.db"):
        resume, cover = generate_for(job_id, db=db)
        typer.echo(f"Created:\n  {resume}\n  {cover}")

    @app.callback(invoke_without_command=True)
    def main(
        ctx: typer.Context,
        job_id: str = typer.Argument(None, help="Full job ID, e.g. greenhouse:stripe:5922987"),
        db: str = typer.Option("jobs.db", help="Path to database"),
    ):
        # Allow root style: python writer.py "<ID>"
        if job_id and ctx.invoked_subcommand is None:
            resume, cover = generate_for(job_id, db=db)
            typer.echo(f"Created:\n  {resume}\n  {cover}")
            raise typer.Exit()

        if ctx.invoked_subcommand is None:
            typer.echo("Usage:\n  python writer.py write <JOB_ID>\n  python writer.py <JOB_ID>")
            raise typer.Exit(code=1)

    if __name__ == "__main__":
        app()
else:
    if __name__ == "__main__":
        # Fallback: generate for an env var JOB_ID if CLI isn't available
        jid = os.getenv("JOB_ID")
        if not jid:
            raise SystemExit("Usage: python writer.py write <JOB_ID>  (or)  python writer.py <JOB_ID>")
        r, c = generate_for(jid)
        print("Created:", r, c)

