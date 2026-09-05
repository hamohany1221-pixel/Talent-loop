-- 002_intelligence_and_automation.sql

CREATE TABLE IF NOT EXISTS candidate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS duplicate_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_a TEXT NOT NULL REFERENCES candidates(id),
    candidate_b TEXT NOT NULL REFERENCES candidates(id),
    score REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    candidate_id TEXT REFERENCES candidates(id),
    source TEXT,
    utm TEXT,
    submitted_at TEXT NOT NULL,
    form_data TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS campaign_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    name TEXT NOT NULL,
    variant_type TEXT NOT NULL DEFAULT 'message',
    reach INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0,
    applications INTEGER NOT NULL DEFAULT 0,
    qualified INTEGER NOT NULL DEFAULT 0,
    hires INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    date TEXT NOT NULL,
    applications INTEGER NOT NULL DEFAULT 0,
    qualified INTEGER NOT NULL DEFAULT 0,
    interviews INTEGER NOT NULL DEFAULT 0,
    hires INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    conditions TEXT NOT NULL DEFAULT '[]',
    actions TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft',
    created_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    candidate_id TEXT,
    ts TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kill_switch (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS agent_tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args TEXT NOT NULL,
    permission_check TEXT NOT NULL,
    result_summary TEXT NOT NULL DEFAULT ''
);
