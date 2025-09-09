# score_jobs.py
# Score jobs in jobs.db by matching descriptions to your skills profile.

import sqlite3
import json
import re
from typing import Dict, List, Iterable, Tuple
from rapidfuzz import fuzz

# ----------------------------
# 1) Your skills profile
#    Edit freely to reflect you.
# ----------------------------
MY_SKILLS: Dict[str, Iterable] = {
    "languages": ["python", "typescript", "go"],
    "ml": ["nlp", "transformers", "langchain", "retrieval", "llmops"],
    "tools": ["postgres", "docker", "kubernetes", "playwright", "aws", "gcp"],
    "years_exp": {"python": 5, "typescript": 3},
}

# Optional bucket weights (tweak as you like)
BUCKET_WEIGHTS: Dict[str, float] = {
    "languages": 1.0,
    "ml": 1.2,
    "tools": 0.9,
    "years_exp": 1.1,
}

# Minimum fuzzy ratio to count as a "soft hit"
FUZZY_THRESHOLD = 85


# ----------------------------
# 2) Utilities
# ----------------------------
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

def strip_html(s: str) -> str:
    if not s:
        return ""
    s = TAG_RE.sub(" ", s)
    s = WS_RE.sub(" ", s)
    return s.strip()

def normalize_text(s: str) -> str:
    return (s or "").lower()

def safe_get(payload: dict, *keys, default=""):
    for k in keys:
        if isinstance(payload, dict) and k in payload:
            payload = payload[k]
        else:
            return default
    return payload if isinstance(payload, str) else default

def ensure_score_column(conn: sqlite3.Connection) -> None:
    # Add score column if it doesn't exist yet
    cur = conn.execute("PRAGMA table_info(jobs)")
    cols = {row[1] for row in cur.fetchall()}
    if "score" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN score REAL")
        conn.commit()


# ----------------------------
# 3) Scoring
# ----------------------------
def exact_or_fuzzy_hit(skill: str, text: str) -> bool:
    if not skill:
        return False
    if skill in text:
        return True
    # Fuzzy partial match for slight variations / punctuation
    return fuzz.partial_ratio(skill, text) >= FUZZY_THRESHOLD

def skill_score(text: str) -> float:
    text = normalize_text(text)

    weighted_hits = 0.0
    weighted_total = 0.0

    for bucket, skills in MY_SKILLS.items():
        weight = BUCKET_WEIGHTS.get(bucket, 1.0)

        # Dict buckets (e.g., years_exp -> {"python": 5, ...})
        if isinstance(skills, dict):
            for skill_name in skills.keys():
                weighted_total += weight
                if exact_or_fuzzy_hit(skill_name.lower(), text):
                    weighted_hits += weight

        # List buckets
        elif isinstance(skills, (list, tuple, set)):
            for skill_name in skills:
                weighted_total += weight
                if exact_or_fuzzy_hit(str(skill_name).lower(), text):
                    weighted_hits += weight

    if weighted_total == 0:
        return 0.0
    return round(100.0 * (weighted_hits / weighted_total), 1)

def job_text_from_payload(payload: dict, source: str) -> str:
    """
    Build a searchable text blob from the raw API payload.
    - Greenhouse: content (HTML) + title
    - Lever: descriptionPlain or description + title
    """
    if source == "greenhouse":
        content_html = payload.get("content", "") or ""
        title = payload.get("title", "") or ""
        text = strip_html(content_html) + " " + title
        return text
    elif source == "lever":
        text = (
            payload.get("descriptionPlain")
            or payload.get("description")
            or ""
        )
        # Sometimes description is HTML
        text = strip_html(text)
        title = payload.get("text", "") or payload.get("title", "") or ""
        return f"{text} {title}"
    else:
        # Fallback: join everything that looks like text
        return strip_html(json.dumps(payload, ensure_ascii=False))


# ----------------------------
# 4) Main scoring routine
# ----------------------------
def score_all(db: str = "jobs.db") -> None:
    conn = sqlite3.connect(db)
    ensure_score_column(conn)

    cur = conn.execute("SELECT id, raw_json, source FROM jobs")
    rows: List[Tuple[str, str, str]] = cur.fetchall()

    for job_id, raw_json, source in rows:
        try:
            payload = json.loads(raw_json) if raw_json else {}
        except Exception:
            payload = {}
        text = job_text_from_payload(payload, source or "")
        s = skill_score(text)
        conn.execute("UPDATE jobs SET score=? WHERE id=?", (s, job_id))

    conn.commit()
    conn.close()


if __name__ == "__main__":
    score_all()
    print("Scored jobs and updated 'score' column in jobs.db")
