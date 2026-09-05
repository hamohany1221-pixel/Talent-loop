-- 001_init.sql — core entities. Written in portable SQL (works on SQLite now;
-- moving to Postgres later needs only the driver swap in app/db.py, not a
-- schema rewrite — see README "Postgres migration path").

CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    ai_budget_daily REAL NOT NULL DEFAULT 50.0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','recruiter','viewer'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    title TEXT NOT NULL,
    company TEXT,
    language TEXT NOT NULL,
    level TEXT NOT NULL,
    education TEXT NOT NULL,
    experience_years_required INTEGER NOT NULL DEFAULT 0,
    location TEXT NOT NULL,
    work_model TEXT NOT NULL,
    shift TEXT,
    salary TEXT,
    vacancies INTEGER NOT NULL DEFAULT 0,
    filled INTEGER NOT NULL DEFAULT 0,
    deadline TEXT,
    must_have TEXT NOT NULL DEFAULT '[]',
    nice_to_have TEXT NOT NULL DEFAULT '[]',
    disqualifiers TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    location TEXT,
    education TEXT,
    experience_years INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    stage TEXT NOT NULL DEFAULT 'New',
    last_contact_days INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    merged_into TEXT REFERENCES candidates(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_languages (
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    lang TEXT NOT NULL,
    level TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_skills (
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    skill TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interviews (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    interviewer TEXT,
    date TEXT,
    status TEXT NOT NULL DEFAULT 'Scheduled',
    feedback TEXT
);

CREATE TABLE IF NOT EXISTS offers (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL DEFAULT 'Draft',
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    applications INTEGER NOT NULL DEFAULT 0,
    qualified INTEGER NOT NULL DEFAULT 0,
    hires INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    job_id TEXT REFERENCES jobs(id),
    target INTEGER, reach INTEGER, clicks INTEGER, applications INTEGER,
    screened INTEGER, qualified INTEGER, interviews INTEGER, selected INTEGER,
    hires INTEGER, attendance_rate REAL, status TEXT DEFAULT 'Active'
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    action_type TEXT NOT NULL DEFAULT 'generic',
    action_payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id TEXT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screening_sessions (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    step INTEGER NOT NULL DEFAULT 0,
    answers TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'in_progress',
    log TEXT NOT NULL DEFAULT '[]'
);
