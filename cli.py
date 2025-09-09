# cli.py
import os
import sqlite3
from pathlib import Path
from typing import List, Tuple, Optional
import typer

# Local modules
from fetch_jobs import fetch_and_store
from score_jobs import score_all
from writer import generate_for
from apply_assist import apply_to

# Import your converter helpers
from convert_and_export import (
    ensure_pdf_from_md,
    write_env_ps1,
    update_db as update_app_pdfs,
)

app = typer.Typer(help="Job automation CLI: fetch → score → generate → convert → apply")

DEFAULT_DB = "jobs.db"
DEFAULT_GH = ["stripe", "notion"]          # Greenhouse org slugs
DEFAULT_LEVER = ["datadog", "cloudflare"]  # Lever org slugs

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  company TEXT, title TEXT, location TEXT,
  url TEXT UNIQUE, posted_at TEXT,
  raw_json TEXT,
  score REAL DEFAULT 0,
  status TEXT DEFAULT 'new'
);
CREATE TABLE IF NOT EXISTS applications (
  job_id TEXT, applied_at TEXT, resume_path TEXT, cover_path TEXT,
  notes TEXT, PRIMARY KEY(job_id)
);
"""

def ensure_db(db_path: str = DEFAULT_DB) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()

def _default_md_paths(job_id: str, docs_dir: str = "docs") -> Tuple[Path, Path]:
    base = job_id.replace(":", "_")
    return (
        Path(docs_dir) / f"{base}_resume.md",
        Path(docs_dir) / f"{base}_cover.md",
    )

def _get_md_paths_from_db(job_id: str, db: str) -> Tuple[Optional[Path], Optional[Path]]:
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT resume_path, cover_path FROM applications WHERE job_id=?",
        (job_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None, None
    resume_md, cover_md = row
    return (Path(resume_md) if resume_md else None,
            Path(cover_md) if cover_md else None)

def _resolve_md_paths(job_id: str, db: str, docs_dir: str = "docs") -> Tuple[Path, Path]:
    # Prefer paths saved by writer.py; otherwise fall back to default locations
    res_md, cov_md = _get_md_paths_from_db(job_id, db)
    if not res_md or not res_md.exists() or res_md.suffix.lower() != ".md":
        res_md_default, cov_md_default = _default_md_paths(job_id, docs_dir)
        res_md = res_md if (res_md and res_md.exists() and res_md.suffix.lower()==".md") else res_md_default
        cov_md = cov_md if (cov_md and cov_md.exists() and cov_md.suffix.lower()==".md") else cov_md_default
    # Final existence check
    if not res_md.exists():
        raise FileNotFoundError(f"Resume MD not found: {res_md}")
    if not cov_md.exists():
        raise FileNotFoundError(f"Cover MD not found: {cov_md}")
    return res_md, cov_md

@app.command()
def initdb(db: str = DEFAULT_DB):
    """Create the SQLite tables if they don't exist."""
    ensure_db(db)
    typer.secho(f"✅ DB initialized -> {db}", fg=typer.colors.GREEN)

@app.command()
def refresh(
    gh: List[str] = typer.Option(DEFAULT_GH, "--gh", help="Greenhouse org slugs (repeatable)"),
    lever: List[str] = typer.Option(DEFAULT_LEVER, "--lever", help="Lever org slugs (repeatable)"),
    db: str = DEFAULT_DB,
):
    """Fetch jobs from boards and score them."""
    ensure_db(db)
    if not gh and not lever:
        typer.echo("No boards provided. Example:\n  python cli.py refresh --gh stripe --lever datadog")
        raise typer.Exit(code=1)
    fetch_and_store(gh, lever, db=db)
    score_all(db=db)
    typer.secho("✅ Refreshed and scored.", fg=typer.colors.GREEN)

@app.command()
def top(
    n: int = typer.Option(10, help="How many jobs to show"),
    min_score: float = typer.Option(60, help="Minimum score"),
    company: str = typer.Option("", help="Filter by company (substring)"),
    title: str = typer.Option("", help="Filter by title (substring)"),
    db: str = DEFAULT_DB,
):
    """List top-scoring jobs (with optional filters)."""
    ensure_db(db)
    conn = sqlite3.connect(db)
    sql = "SELECT id, company, title, score, url FROM jobs WHERE score >= ?"
    params = [min_score]
    if company:
        sql += " AND company LIKE ?"; params.append(f"%{company}%")
    if title:
        sql += " AND title LIKE ?"; params.append(f"%{title}%")
    sql += " ORDER BY score DESC LIMIT ?"; params.append(n)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    if not rows:
        typer.echo("No jobs matched. Try lowering --min-score or changing filters.")
        raise typer.Exit(code=0)
    for jid, comp, ttl, score, url in rows:
        print(f"{jid} | {ttl} @ {comp} | {score:.1f} | {url}")

@app.command()
def generate(
    job_id: str = typer.Argument(..., help="Job ID from 'top'"),
    db: str = DEFAULT_DB,
    outdir: str = typer.Option("docs", help="Output folder for resume/cover"),
):
    """Create tailored résumé + cover letter for a job ID (writes .md files)."""
    ensure_db(db)
    if not os.getenv("OPENAI_API_KEY"):
        typer.secho("⚠ OPENAI_API_KEY not set. 'writer.py' will fail. Set it and retry.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    # Support both writer.py versions (with/without outdir)
    try:
        resume_path, cover_path = generate_for(job_id, db=db, outdir=outdir)
    except TypeError:
        resume_path, cover_path = generate_for(job_id, db=db)
    typer.secho("✅ Created:", fg=typer.colors.GREEN)
    print("  ", os.path.abspath(resume_path))
    print("  ", os.path.abspath(cover_path))

@app.command()
def convert(
    job_id: str = typer.Argument(..., help="Job ID from 'top'"),
    db: str = DEFAULT_DB,
    docs_dir: str = typer.Option("docs", help="Where the .md files live"),
    export_env: bool = typer.Option(True, help="Also write set_app_files.ps1 and .env"),
):
    """
    Convert the generated .md resume/cover to PDF and update DB to use the PDFs.
    """
    ensure_db(db)
    res_md, cov_md = _resolve_md_paths(job_id, db=db, docs_dir=docs_dir)

    resume_pdf = ensure_pdf_from_md(str(res_md))
    cover_pdf  = ensure_pdf_from_md(str(cov_md))

    # Update DB applications table to point at the PDFs
    update_app_pdfs(job_id, resume_pdf, cover_pdf, db=db)

    typer.secho("✅ PDFs ready:", fg=typer.colors.GREEN)
    print("  RESUME:", resume_pdf)
    print("  COVER: ", cover_pdf)

    if export_env:
        ps1_path, env_path = write_env_ps1(resume_pdf, cover_pdf)
        typer.secho("🔐 Env files written:", fg=typer.colors.GREEN)
        print(" ", ps1_path)
        print(" ", env_path)
        print("\nTo load in current PowerShell session:")
        print(f"  . .\\{Path(ps1_path).name}")

@app.command(name="apply")
def apply_cmd(
    job_id: str = typer.Argument(..., help="Job ID from 'top'"),
    db: str = DEFAULT_DB,
):
    """Open the application and prefill (uses whatever the DB has for files)."""
    ensure_db(db)
    import asyncio
    asyncio.run(apply_to(job_id, db=db))

@app.command(name="apply-pdf")
def apply_pdf_cmd(
    job_id: str = typer.Argument(..., help="Job ID from 'top'"),
    db: str = DEFAULT_DB,
    docs_dir: str = typer.Option("docs", help="Where the .md files live"),
):
    """
    One-shot: convert MD -> PDF, update DB to PDFs, then open assisted apply.
    """
    ensure_db(db)
    # 1) Convert + update DB (no need to export env; we read from DB)
    res_md, cov_md = _resolve_md_paths(job_id, db=db, docs_dir=docs_dir)
    resume_pdf = ensure_pdf_from_md(str(res_md))
    cover_pdf  = ensure_pdf_from_md(str(cov_md))
    update_app_pdfs(job_id, resume_pdf, cover_pdf, db=db)

    typer.secho("✅ Using PDFs for apply:", fg=typer.colors.GREEN)
    print("  RESUME:", resume_pdf)
    print("  COVER: ", cover_pdf)

    # 2) Apply (assisted; with your review pause)
    import asyncio
    asyncio.run(apply_to(job_id, db=db))

if __name__ == "__main__":
    app()
