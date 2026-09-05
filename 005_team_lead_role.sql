-- 005_team_lead_role.sql — adds a fourth tier (team_lead) between
-- recruiter and admin, and an `active` flag so admins can deactivate
-- accounts without deleting history. SQLite can't ALTER a CHECK
-- constraint in place, so the table is recreated and data copied over.

CREATE TABLE users_new (
    id INTEGER PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','team_lead','recruiter','viewer')),
    active INTEGER NOT NULL DEFAULT 1
);

INSERT INTO users_new (id, organization_id, username, password_hash, salt, role, active)
    SELECT id, organization_id, username, password_hash, salt, role, 1 FROM users;

DROP TABLE users;
ALTER TABLE users_new RENAME TO users;
