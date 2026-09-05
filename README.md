# Talent Loop — AI Recruitment OS (runnable slice)

A real, working, tested backend + frontend for the AI Recruitment OS
specification. Runs entirely on the Python standard library — no
`pip install`, no internet connection, no Docker required to try it.

## Run it

```
python3 server.py
```

Open **http://localhost:8000**. First run creates `talent_loop.db`
(SQLite) and seeds demo data automatically.

Demo logins: `admin/admin123` (admin) · `mona/mona123` (team lead) · `recruiter/recruiter123`, `sara/sara123`, `hassan/hassan123` (recruiters) · `viewer/viewer123` (read-only)

## Run the tests

```
python3 -m unittest discover -s tests -v
```

27 tests covering auth, the matching/rule engine, deduplication, CV
parsing, the workflow engine (including kill-switch and approval
gating), agent tool permissions, rate limiting, and recruitment
intelligence. All passing.

## What this is

See **AUDIT.md** for the full point-by-point audit against the master
specification (what's fully working, partial, integration-ready, and
what needs your credentials — with exact next steps). See **API.md**
for the complete endpoint reference.

## Project layout

```
talent-loop/
  server.py              — entrypoint: runs migrations, seeds data, starts the server
  app/
    db.py                 — connection, versioned migrations, seed data
    domain.py              — rule/matching engine, CV parser, dedup, recruitment intelligence
    workflow.py             — triggers/conditions/actions engine + NL workflow parser
    agent.py                — typed tool registry + AI copilot dispatcher
    security.py              — auth, RBAC, rate limiting, tool permissions
    integrations.py           — Facebook/LinkedIn/WhatsApp/Telegram/Email/Calendar adapters
    routes.py                  — HTTP handler wiring everything together
  migrations/*.sql        — versioned schema
  static/index.html       — frontend (vanilla JS, calls the API via fetch)
  tests/test_core.py      — automated tests
  Dockerfile, docker-compose.yml — deployment config (see note below)
  API.md, AUDIT.md        — documentation
```

## Connecting real integrations

Every external channel (Facebook, LinkedIn, WhatsApp, Telegram, Email,
Calendar) is built as a real adapter behind one interface
(`app/integrations.py`). Today they all resolve to a `MockAdapter` that
writes every "send" to a real `outbox` table — inspect it at
`/api/outbox` or the Admin panel. Setting the matching environment
variable (see `docker-compose.yml` or `API.md`) switches that channel
to its live implementation with **no code changes** — the request-
building logic already exists, it's just gated on a credential it
doesn't have in this environment.

Same pattern for AI: `app/agent.py`'s `parse_command()` is the single
function doing rule-based NLU today; it's the one place to plug in a
real Anthropic/OpenAI call later.

## Postgres migration path

The app runs on SQLite by default. Every table already has an
`organization_id` column and the SQL avoids SQLite-only syntax except
`INTEGER PRIMARY KEY AUTOINCREMENT` (→ `SERIAL PRIMARY KEY` on
Postgres — a one-line find/replace per migration file). To switch:
`pip install psycopg2-binary`, set `DATABASE_URL`, update `get_db()` in
`app/db.py` to return a psycopg2 connection with dict-like rows
(`psycopg2.extras.RealDictCursor`). Not tested against a real Postgres
instance in this sandbox (none available here).

## Docker

`Dockerfile` and `docker-compose.yml` are written and syntactically
standard (health check included), but there's no Docker daemon in this
sandbox to actually build/run them — they're untested here. They
should work as-is on any machine with Docker installed:
`docker compose up --build`.

## Honesty note

Nothing in this codebase claims to be "done" just because a UI button
exists for it. Where a feature needs a credential this environment
doesn't have, the code raises a clear, typed error explaining exactly
what's missing rather than silently pretending to work — see
`IntegrationNotConfigured` in `app/integrations.py` for the pattern.
See **AUDIT.md** for the full accounting.
