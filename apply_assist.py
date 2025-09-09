# apply_assist.py
import asyncio, os, re, sqlite3
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from playwright._impl._errors import TargetClosedError

# =========================
# EDIT THESE VALUES LATER
# =========================
PROFILE = {
    "first": "Jordan",
    "last": "Miller",
    "email": "jordan.miller@example.com",
    "phone": "+41 79 555 12 34",
    "linkedin": "https://www.linkedin.com/in/jordanmiller123",
    "website": "https://jordanmiller.dev",
    # extras (non-legal)
    "city": "Zurich, Switzerland",
    "employer": "Acme Analytics AG",
    "job_title": "Data Analyst",
    "school": "University of Zurich",
    "degree": "B.Sc. Computer Science",
}

REVIEW_SECONDS = 180
ALLOWED_UPLOAD_EXTS = {".pdf", ".doc", ".docx", ".rtf"}

# Known selectors (Greenhouse/Lever variants)
FILE_SELECTORS = {
    "resume": [
        "input[type='file'][name*='resume']",
        "input[type='file'][name*='cv']",
        "input#resume",
        "input#resume-upload-input",
        "input[name='application[resume]']",
        "[data-qa='resume-uploader'] input[type='file']",
    ],
    "cover": [
        "input[type='file'][name*='cover']",
        "input[name='application[cover_letter]']",
        "input#cover",
        "[data-qa='coverLetter-uploader'] input[type='file']",
    ],
}

# Labels we try to match
LABEL_PATTERNS = {
    "first": [r"first\s*name"],
    "last":  [r"last\s*name|surname"],
    "email": [r"email"],
    "phone": [r"phone|mobile"],
    "linkedin": [r"linkedin"],
    "website":  [r"website|portfolio|personal\s*site"],
    "city":     [r"where.*based|current.*location|city|location"],
    "employer": [r"current.*employer|previous.*employer|company"],
    "job_title":[r"current.*job.*title|previous.*job.*title|job\s*title|title"],
    "school":   [r"most.*recent.*school|school|university|college"],
    "degree":   [r"most.*recent.*degree|degree|qualification"],
}

# Fallback CSS for common fields
CSS_FALLBACKS = {
    "first":  ["input[name*='firstName']", "input[name*='first_name']"],
    "last":   ["input[name*='lastName']", "input[name*='last_name']"],
    "email":  ["input[type='email']", "input[name*='email']"],
    "phone":  ["input[type='tel']", "input[name*='phone']"],
    "linkedin": ["input[name*='linkedin']", "input[placeholder*='LinkedIn' i]"],
    "website":  ["input[name*='website']", "input[name*='portfolio']"],
    "city":     ["input[name*='city']", "input[name*='location']", "input[placeholder*='location' i]"],
    "employer": ["input[name*='employer']", "input[name*='company']"],
    "job_title":["input[name*='title']"],
    "school":   ["input[name*='school']", "input[name*='university']", "input[name*='college']"],
    "degree":   ["input[name*='degree']"],
}

# Buttons that reveal hidden file inputs
BUTTON_CUES = [r"attach", r"upload", r"resume", r"choose file", r"browse"]

def _abspath_or_none(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    ap = os.path.abspath(p)
    return ap if os.path.exists(ap) else None

def _pick_valid_upload(db_path: Optional[str], env_name: str) -> Optional[str]:
    """Prefer DB path if valid; else ENV var (RESUME_FILE/COVER_FILE)."""
    def ok(path: Optional[str]) -> bool:
        return bool(path) and Path(path).exists() and Path(path).suffix.lower() in ALLOWED_UPLOAD_EXTS
    if ok(db_path):
        return os.path.abspath(db_path)
    envp = os.getenv(env_name)
    if ok(envp):
        return os.path.abspath(envp)
    return None

async def _maybe_get_form_scope(page):
    # Greenhouse form often lives in an iframe named grnhse_iframe
    frame = page.frame(name="grnhse_iframe")
    if frame:
        return frame
    for fr in page.frames:
        try:
            if fr.url and ("boards.greenhouse.io" in fr.url or "greenhouse.io" in fr.url or "grnhse" in fr.url):
                return fr
        except Exception:
            pass
    return page

async def _try_fill_by_label(scope, patterns, value) -> bool:
    for pat in patterns:
        try:
            await scope.get_by_label(re.compile(pat, re.I)).first.fill(value, timeout=1500)
            return True
        except Exception:
            continue
    return False

async def _try_fill_by_css(scope, selectors, value) -> bool:
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if await loc.count() > 0:
                await loc.fill(value, timeout=1500)
                return True
        except Exception:
            continue
    return False

async def _click_possible_attach_buttons(scope):
    for cue in BUTTON_CUES:
        # Try "role=button" then text
        try:
            await scope.get_by_role("button", name=re.compile(cue, re.I)).first.click(timeout=800)
        except Exception:
            pass
        try:
            await scope.get_by_text(re.compile(cue, re.I)).first.click(timeout=800)
        except Exception:
            pass

async def _scan_file_inputs(scope):
    loc = scope.locator("input[type='file']")
    count = await loc.count()
    inputs = []
    for i in range(count):
        ith = loc.nth(i)
        name = (await ith.get_attribute("name")) or ""
        fid  = (await ith.get_attribute("id")) or ""
        accept = (await ith.get_attribute("accept")) or ""
        inputs.append({"idx": i, "loc": ith, "name": name, "id": fid, "accept": accept})
    return inputs

async def _try_upload_known(scope, kind: str, path: Optional[str]) -> bool:
    if not path:
        return False
    for sel in FILE_SELECTORS.get(kind, []):
        try:
            loc = scope.locator(sel).first
            if await loc.count() > 0:
                await loc.set_input_files(path, timeout=2500)
                return True
        except Exception:
            continue
    return False

async def _upload_best_effort(scope, resume_path: Optional[str], cover_path: Optional[str]):
    uploaded_resume = await _try_upload_known(scope, "resume", resume_path)
    uploaded_cover  = await _try_upload_known(scope, "cover",  cover_path)

    if (resume_path and not uploaded_resume) or (cover_path and not uploaded_cover):
        await _click_possible_attach_buttons(scope)

    inputs = await _scan_file_inputs(scope)
    print(f"   Found {len(inputs)} file input(s) in the form.")

    def pick(kind_regex):
        rgx = re.compile(kind_regex, re.I)
        for info in inputs:
            if rgx.search(info["name"]) or rgx.search(info["id"]):
                return info
        return None

    if resume_path and not uploaded_resume:
        tgt = pick("resume|cv")
        if tgt:
            try:
                await tgt["loc"].set_input_files(resume_path, timeout=2500)
                uploaded_resume = True
                print(f"   ✔ Resume uploaded via input idx={tgt['idx']} (name='{tgt['name']}', id='{tgt['id']}').")
            except Exception:
                pass

    if cover_path and not uploaded_cover:
        tgt = pick("cover")
        if tgt:
            try:
                await tgt["loc"].set_input_files(cover_path, timeout=2500)
                uploaded_cover = True
                print(f"   ✔ Cover uploaded via input idx={tgt['idx']} (name='{tgt['name']}', id='{tgt['id']}').")
            except Exception:
                pass

    # Generic fallbacks
    if resume_path and not uploaded_resume and inputs:
        try:
            await inputs[0]["loc"].set_input_files(resume_path, timeout=2500)
            uploaded_resume = True
            print(f"   ✔ Resume uploaded via generic input idx=0 (fallback).")
        except Exception:
            pass
    if cover_path and not uploaded_cover and len(inputs) >= 2:
        try:
            await inputs[1]["loc"].set_input_files(cover_path, timeout=2500)
            uploaded_cover = True
            print(f"   ✔ Cover uploaded via generic input idx=1 (fallback).")
        except Exception:
            pass

    return uploaded_resume, uploaded_cover

def _candidate_apply_urls(job_id: str, url_from_db: str) -> list[str]:
    urls = []
    if job_id.startswith("greenhouse:"):
        try:
            _, org, token = job_id.split(":", 2)
            urls.append(f"https://boards.greenhouse.io/embed/job_app?for={org}&token={token}")
            urls.append(f"https://boards.greenhouse.io/{org}/jobs/{token}")
        except ValueError:
            pass
    urls.append(url_from_db)
    seen = set(); out = []
    for u in urls:
        if u and u not in seen:
            out.append(u); seen.add(u)
    return out

async def _fill_field(scope, key, value):
    if not value:
        return False
    # Try label regex
    if await _try_fill_by_label(scope, LABEL_PATTERNS.get(key, []), value):
        return True
    # Try CSS fallbacks
    if key in CSS_FALLBACKS:
        return await _try_fill_by_css(scope, CSS_FALLBACKS[key], value)
    return False

async def _fill_all_textish(scope, profile: dict):
    # basics first
    for k in ["first","last","email","phone","linkedin","website"]:
        await _fill_field(scope, k, profile.get(k,""))
    # extras (non-legal)
    for k in ["city","employer","job_title","school","degree"]:
        await _fill_field(scope, k, profile.get(k,""))

    # opportunistic placeholder match for any remaining text inputs
    cues = {
        r"city|location": profile["city"],
        r"employer|company": profile["employer"],
        r"job\s*title|title": profile["job_title"],
        r"school|university|college": profile["school"],
        r"degree|qualification": profile["degree"],
        r"linkedin": profile["linkedin"],
        r"website|portfolio": profile["website"],
    }
    inputs = scope.locator("input[type='text'], input:not([type])")
    for i in range(await inputs.count()):
        el = inputs.nth(i)
        try:
            if await el.input_value():
                continue
            blob = " ".join([
                (await el.get_attribute("placeholder")) or "",
                (await el.get_attribute("name")) or "",
                (await el.get_attribute("aria-label")) or ""
            ]).lower()
            for pat, v in cues.items():
                if v and re.search(pat, blob):
                    await el.fill(v, timeout=1000)
                    break
        except Exception:
            pass

async def apply_to(job_id, db="jobs.db"):
    # ---- DB lookups
    conn = sqlite3.connect(db)
    job = conn.execute("SELECT url, company, title FROM jobs WHERE id=?", (job_id,)).fetchone()
    app = conn.execute("SELECT resume_path, cover_path FROM applications WHERE job_id=?", (job_id,)).fetchone()
    conn.close()
    if not job:
        raise RuntimeError(f"Job {job_id} not found in DB.")

    url_from_db, company, title = job
    resume_from_db = app[0] if app else None
    cover_from_db  = app[1] if app else None

    # Pick upload files (prefer DB paths, else ENV)
    resume_path = _pick_valid_upload(resume_from_db, "RESUME_FILE")
    cover_path  = _pick_valid_upload(cover_from_db,  "COVER_FILE")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # Navigate to a real apply form
        found_form = False
        for target in _candidate_apply_urls(job_id, url_from_db):
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=60000)
                scope = await _maybe_get_form_scope(page)
                if "boards.greenhouse.io" in page.url or await scope.locator("input[type='file']").count() > 0:
                    found_form = True
                    break
            except Exception:
                continue

        scope = await _maybe_get_form_scope(page)

        # Uploads
        uploaded_resume, uploaded_cover = await _upload_best_effort(scope, resume_path, cover_path)

        # Fill fields
        await _fill_all_textish(scope, PROFILE)

        # (Legal/EEO dropdowns intentionally left for you)
        print(f"\n→ Opened: {company} — {title}")
        print(f"   URL now: {page.url}")
        print(f"   Resume uploaded: {bool(uploaded_resume)} ({resume_path or 'none'})")
        print(f"   Cover uploaded:  {bool(uploaded_cover)} ({cover_path or 'none'})")
        if resume_from_db and not resume_path:
            print("   ⚠ apps table points to a non-uploadable file. Set RESUME_FILE to a PDF/DOC/DOCX/RTF.")
        if cover_from_db and not cover_path:
            print("   ⚠ apps table points to a non-uploadable file. Set COVER_FILE to a PDF/DOC/DOCX/RTF.")
        if not found_form:
            print("   ⚠ If you see an 'Apply' button, click it; the script will still fill once the form appears.")

        print("\n⏳ Pausing so you can review/submit…\n")
        try:
            await page.wait_for_timeout(REVIEW_SECONDS * 1000)
        except (TargetClosedError, PWTimeout):
            pass
        finally:
            try:
                await browser.close()
            except Exception:
                pass

# ---- CLI ----
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--db", default="jobs.db")
    args = parser.parse_args()
    asyncio.run(apply_to(args.job_id, db=args.db))
