# convert_and_export.py
import argparse, os, shutil, sqlite3
from pathlib import Path

# Try to import pdfkit and markdown; always have reportlab fallback
import markdown
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch

# pdfkit (optional, nicer PDFs if wkhtmltopdf is installed)
try:
    import pdfkit
except Exception:
    pdfkit = None

ALLOWED_UPLOAD_EXTS = {".pdf"}

def to_abs(p: str) -> str:
    return str(Path(p).expanduser().resolve())

def md_to_pdf_simple(md_path: str, pdf_path: str) -> None:
    """
    Fallback: simple text-only PDF using reportlab (no fancy styling, but ATS-safe).
    """
    text = Path(md_path).read_text(encoding="utf-8", errors="ignore")
    pdfp = to_abs(pdf_path)
    c = canvas.Canvas(pdfp, pagesize=LETTER)
    width, height = LETTER
    left = 1.0 * inch
    top = height - 1.0 * inch
    line_height = 12
    max_width = width - 2 * inch

    def wrap_line(line, max_chars=95):
        # crude wrap so text doesn't run off page
        chunks = []
        while len(line) > max_chars:
            cut = line.rfind(" ", 0, max_chars)
            if cut == -1:
                cut = max_chars
            chunks.append(line[:cut])
            line = line[cut:].lstrip()
        chunks.append(line)
        return chunks

    y = top
    for raw_line in text.splitlines():
        for line in wrap_line(raw_line):
            if y < 1.0 * inch:
                c.showPage()
                y = top
            c.drawString(left, y, line)
            y -= line_height
    c.save()

def md_to_pdf_pretty(md_path: str, pdf_path: str) -> bool:
    """
    Preferred: convert MD->HTML->PDF via wkhtmltopdf (if available).
    Returns True on success, False to trigger fallback.
    """
    if not pdfkit:
        return False
    wkhtml = shutil.which("wkhtmltopdf")
    if not wkhtml:
        return False
    html = markdown.markdown(Path(md_path).read_text(encoding="utf-8", errors="ignore"))
    cfg = pdfkit.configuration(wkhtmltopdf=wkhtml)
    options = {
        "quiet": "",
        "enable-local-file-access": None,
        "margin-top": "12mm",
        "margin-bottom": "12mm",
        "margin-left": "12mm",
        "margin-right": "12mm",
        "encoding": "UTF-8",
    }
    pdfkit.from_string(html, to_abs(pdf_path), options=options, configuration=cfg)
    return True

def ensure_pdf_from_md(md_path: str) -> str:
    mdp = Path(md_path)
    if not mdp.exists():
        raise FileNotFoundError(mdp)
    pdf_path = mdp.with_suffix(".pdf")
    # try pretty, else simple
    ok = False
    try:
        ok = md_to_pdf_pretty(str(mdp), str(pdf_path))
    except Exception:
        ok = False
    if not ok:
        md_to_pdf_simple(str(mdp), str(pdf_path))
    if not Path(pdf_path).exists():
        raise RuntimeError(f"Failed to create PDF for {mdp}")
    return to_abs(pdf_path)

def write_env_ps1(resume_pdf: str, cover_pdf: str, out_ps1="set_app_files.ps1", out_env=".env"):
    ps = Path(out_ps1)
    ps.write_text(
        f'$env:RESUME_FILE = "{resume_pdf}"\n'
        f'$env:COVER_FILE  = "{cover_pdf}"\n'
        f'Write-Host "RESUME_FILE=$env:RESUME_FILE"\n'
        f'Write-Host "COVER_FILE=$env:COVER_FILE"\n',
        encoding="utf-8",
    )
    Path(out_env).write_text(
        f"RESUME_FILE={resume_pdf}\nCOVER_FILE={cover_pdf}\n", encoding="utf-8"
    )
    return str(ps), str(Path(out_env).resolve())

def update_db(job_id: str, resume_pdf: str, cover_pdf: str, db="jobs.db"):
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO applications(job_id, applied_at, resume_path, cover_path, notes) "
            "VALUES(?, date('now'), ?, ?, COALESCE((SELECT notes FROM applications WHERE job_id=?),'converted'))",
            (job_id, resume_pdf, cover_pdf, job_id),
        )
        conn.commit()
    finally:
        conn.close()

def main():
    ap = argparse.ArgumentParser(description="Convert MD resume/cover to PDF and export env vars.")
    ap.add_argument("--resume-md", required=True, help="Path to resume .md")
    ap.add_argument("--cover-md",  required=True, help="Path to cover letter .md")
    ap.add_argument("--job-id",    help="Optional job_id to update in applications table")
    ap.add_argument("--db", default="jobs.db", help="SQLite DB path")
    args = ap.parse_args()

    resume_pdf = ensure_pdf_from_md(args.resume_md)
    cover_pdf  = ensure_pdf_from_md(args.cover_md)

    ps1_path, env_path = write_env_ps1(resume_pdf, cover_pdf)

    print("✅ PDFs created:")
    print("  RESUME:", resume_pdf)
    print("  COVER: ", cover_pdf)
    print("")
    print("🔐 Env files written:")
    print(" ", ps1_path, "(dot-source this in PowerShell to set variables for this terminal)")
    print(" ", env_path, "(for pipelines / dotenv loaders)")
    print("")
    print("👉 To load in current PowerShell session:")
    print(f"  . .\\{Path(ps1_path).name}")
    print("")
    if args.job_id:
        update_db(args.job_id, resume_pdf, cover_pdf, db=args.db)
        print(f"🗃️  DB updated for job_id={args.job_id} -> applications.resume_path/cover_path now point to the PDFs")

if __name__ == "__main__":
    main()
