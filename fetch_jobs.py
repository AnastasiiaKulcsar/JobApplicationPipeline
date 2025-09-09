# fetch_jobs.py
# Pull jobs from Greenhouse + Lever and store/update them in jobs.db

import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

import httpx


# ----------------------------
# Config: put your companies here
# ----------------------------
GH_ORGS: list[str] = [
    "stripe",
    "notion",
    # add more greenhouse slugs...
    # e.g. "datadog", "cloudflare", "figma", "dropbox"
]

LEVER_ORGS: list[str] = [
    # add lever slugs if you have any; example:
    # "asana", "robinhood"
]


# ----------------------------
# Helpers
# ----------------------------
def gh_boards(org: str) -> str:
    """Greenhouse jobs endpoint for an org slug."""
    return f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs"


def lever_posting_urls(org: str) -> Iterable[str]:
    """Lever postings endpoints to try (US + EU)."""
    yield f"https://api.lever.co/v0/postings/{org}?mode=json"
    yield f"https://api.eu.lever.co/v0/postings/{org}?mode=json"


def to_iso_utc(val: Optional[object]) -> Optional[str]:
    """Return ISO 8601 in UTC ('...Z') from various inputs (epoch s/ms or ISO string)."""
    if val is None:
        return None

    # Epoch (int/float)
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(float(val), tz=timezone.utc).isoformat().replace("+00:00", "Z")

    # String: try ISO; if numeric, treat as epoch (ms or s)
    if isinstance(val, str):
        s = val.strip()
        # try ISO quickly
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            pass
        # try numeric epoch
        try:
            num = float(s)
            # ms vs s heuristic
            if num > 10_000_000_000:
                num = num / 1000.0
            return datetime.fromtimestamp(num, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            return s

    # Fallback
    return str(val)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create jobs table if it doesn't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            source TEXT,
            company TEXT,
            title TEXT,
            location TEXT,
            url TEXT,
            posted_at TEXT,
            raw_json TEXT
        )
        """
    )
    conn.commit()


def upsert(conn: sqlite3.Connection, row: dict) -> None:
    """Insert or update a job row by id."""
    conn.execute(
        """
        INSERT INTO jobs (id, source, company, title, location, url, posted_at, raw_json)
        VALUES (:id, :source, :company, :title, :location, :url, :posted_at, :raw_json)
        ON CONFLICT(id) DO UPDATE SET
            source=excluded.source,
            company=excluded.company,
            title=excluded.title,
            location=excluded.location,
            url=excluded.url,
            posted_at=excluded.posted_at,
            raw_json=excluded.raw_json
        """,
        row,
    )


# ----------------------------
# Normalizers
# ----------------------------
def normalize_gh(job: dict, org: str) -> dict:
    return {
        "id": f"greenhouse:{org}:{job['id']}",
        "source": "greenhouse",
        "company": org,
        "title": job.get("title"),
        "location": (job.get("location") or {}).get("name"),
        "url": job.get("absolute_url"),
        # GH commonly returns created_at/updated_at as ISO strings
        "posted_at": to_iso_utc(job.get("updated_at") or job.get("created_at")),
        "raw_json": json.dumps(job, ensure_ascii=False),
    }


def normalize_lever(job: dict, org: str) -> dict:
    cats = job.get("categories") or {}
    location = cats.get("location") or ", ".join(v for v in cats.values() if v) or None

    created = job.get("createdAt")
    # If createdAt is ms since epoch, convert
    if isinstance(created, (int, float)) and created > 10_000_000_000:
        created = created / 1000.0

    return {
        "id": f"lever:{org}:{job['id']}",
        "source": "lever",
        "company": org,
        "title": job.get("text"),
        "location": location,
        "url": job.get("hostedUrl"),
        "posted_at": to_iso_utc(created),
        "raw_json": json.dumps(job, ensure_ascii=False),
    }


# ----------------------------
# Fetchers
# ----------------------------
def fetch_greenhouse(client: httpx.Client, org: str, conn: sqlite3.Connection) -> None:
    try:
        r = client.get(gh_boards(org))
        r.raise_for_status()
        data = r.json()
        for j in data.get("jobs", []):
            upsert(conn, normalize_gh(j, org))
    except Exception as e:
        print(f"[greenhouse] {org}: {e}")


def fetch_lever(client: httpx.Client, org: str, conn: sqlite3.Connection) -> None:
    last_err = None
    for url in lever_posting_urls(org):
        try:
            r = client.get(url)
            r.raise_for_status()
            data = r.json() or []
            for j in data:
                upsert(conn, normalize_lever(j, org))
            return
        except Exception as e:
            last_err = e
            continue
    if last_err:
        print(f"[lever] {org}: {last_err}")


def fetch_and_store(orgs_gh: Iterable[str], orgs_lever: Iterable[str], db: str = "jobs.db") -> None:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    ensure_schema(conn)

    headers = {"User-Agent": "job-automation/1.0 (+local)"}
    timeout = httpx.Timeout(30.0)
    limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)

    with httpx.Client(
        headers=headers,
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
    ) as client:
        for org in orgs_gh:
            fetch_greenhouse(client, org, conn)
        for org in orgs_lever:
            fetch_lever(client, org, conn)

    conn.commit()
    conn.close()


# ----------------------------
# Entry point
# ----------------------------
if __name__ == "__main__":
    fetch_and_store(GH_ORGS, LEVER_ORGS, db="jobs.db")
    print("Done. Wrote/updated jobs in jobs.db")
