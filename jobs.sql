-- jobs.sql
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,                 -- stable job id (e.g., greenhouse:<board>:<id>)
  source TEXT NOT NULL,                -- greenhouse | lever | rss | custom
  company TEXT, title TEXT, location TEXT,
  url TEXT UNIQUE, posted_at TEXT, 
  raw_json TEXT,                       -- store the source payload
  score REAL DEFAULT 0,
  status TEXT DEFAULT 'new'            -- new | shortlisted | applied | interview | rejected | offer
);

CREATE TABLE IF NOT EXISTS applications (
  job_id TEXT, applied_at TEXT, resume_path TEXT, cover_path TEXT,
  notes TEXT, PRIMARY KEY(job_id)
);
