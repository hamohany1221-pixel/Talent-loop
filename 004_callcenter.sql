-- 004_callcenter.sql — recruiter ownership, call logging, escalations,
-- and weekly call targets for the team-lead / call-center layer.

ALTER TABLE candidates ADD COLUMN assigned_recruiter TEXT;

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    recruiter TEXT NOT NULL,
    ts TEXT NOT NULL,
    hour_of_day INTEGER NOT NULL,
    weekday INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    objection_reason TEXT,
    notes TEXT DEFAULT '',
    callback_at TEXT
);

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    raised_by TEXT NOT NULL,
    ts TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolved_by TEXT,
    resolved_at TEXT,
    resolution_note TEXT
);

CREATE TABLE IF NOT EXISTS recruiter_targets (
    recruiter TEXT NOT NULL,
    week_start TEXT NOT NULL,
    weekly_call_target INTEGER NOT NULL,
    PRIMARY KEY (recruiter, week_start)
);
