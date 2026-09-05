# Product Audit vs. Master Specification

Generated after this build session. Every claim below was verified by
actually running the code (27 automated tests + manual end-to-end curl
sessions against a live server instance) before being written down here
— nothing in section A is asserted without having been executed.

---

## A. Fully working now (real backend logic + persistence + API + UI where relevant)

- **Auth & RBAC**: PBKDF2 password hashing, bearer tokens, 3 roles
  (admin/recruiter/viewer), enforced server-side on every write endpoint
  and every AI tool call — verified a `viewer` token gets a real `403`.
- **Multi-tenant scaffold**: every core table carries `organization_id`;
  single org seeded today, but queries and the schema are already
  tenant-scoped (Section 13 spec item).
- **Rule engine + matching engine**: hard disqualifiers checked before
  any scoring; explainable weighted breakdown; confidence labels.
- **CV Intelligence (text)**: real regex/heuristic parser extracts
  education, experience, languages, skills from pasted CV text, tags
  each field's confidence, and can apply results to a candidate profile.
  *(PDF/DOCX/OCR extraction is not implemented — see D.)*
- **Deduplication**: fuzzy name-matching (difflib) + phone/email
  exact-match scoring; suggestions surfaced for human review;
  non-destructive merge (`merged_into`, reversible, audited). Verified
  it actually detects the seeded near-duplicate pair.
- **Candidate 360 / timeline**: every stage change, screening result, CV
  parse, and merge is logged to `candidate_events` with a real
  timestamp; `/candidates/:id/timeline` returns it.
- **Data quality engine**: flags missing contact info, invalid CEFR
  levels, duplicate phone numbers, implausible experience values.
- **Recruitment intelligence**: funnel/bottleneck detection, z-score
  anomaly detection over 21 days of seeded daily metrics (caught the
  seeded traffic drop), linear-regression forecasting with a stated
  confidence + assumptions, proportional what-if scenarios, per-job
  health scoring, evidence-citing recommendations, and a combined daily
  briefing — all computed from real data, not templated text.
- **Campaign engine**: campaigns, 3 seeded A/B content variants ranked
  by qualified rate, a real public landing page per job
  (`/api/landing/:job_id`) with a working application form that writes
  a real candidate + application record and fires workflows —
  end-to-end tested including UTM attribution.
- **Workflow engine**: structured triggers/conditions/actions stored as
  JSON, evaluated against real events (stage changes, screening
  completion, applications); a rule-based natural-language parser for
  3 command patterns; every execution logged; verified a workflow
  created from an NL sentence actually fired when a real screening
  completed.
- **AI Recruiter Copilot — real tool-calling agent** (not a regex
  chatbot returning fixed strings): typed tool registry, argument
  validation, independent permission check per tool, full audit log of
  every call (`agent_tool_calls`), 10 working tools including
  `findNeverHired`, `matchCandidates`, `createScreeningQuestions`,
  `generateDailyBriefing`. The NLU step that picks which tool to call is
  regex-based (no live LLM available); everything after that point is
  the same real pipeline a live model's tool calls would go through.
- **Integration adapters**: a real `DistributionAdapter` interface with
  working `MockAdapter` (writes to a real `outbox` table) and
  `LiveAdapter` implementations for Facebook, LinkedIn, WhatsApp,
  Telegram, Email, Calendar that contain the actual HTTP-calling code,
  gated behind `IntegrationNotConfigured` until credentials are set via
  env vars. Verified the full loop: application → workflow → approval
  → (on approve) adapter execution → outbox record, using the mock
  provider.
- **Approval Center**: drafts (campaigns, workflow message actions)
  require an explicit `recruiter`/`admin` approval before anything
  executes; approving a `send_message` action actually invokes the
  integration adapter.
- **Kill switch**: admin-only global toggle; verified it blocks workflow
  execution and the `createCampaign` tool while active.
- **Rate limiting**: sliding-window limiter (120 req/min/token), tested.
- **Audit log**: every login, tool call, approval, workflow execution,
  and merge is recorded with actor + timestamp.
- **Migrations**: versioned `migrations/*.sql` + `schema_migrations`
  tracking table, idempotent, runs automatically on startup.
- **Automated tests**: 27 unit tests across auth, matching, dedup, CV
  parsing, workflows, agent permissions, rate limiting, and
  intelligence — all passing (`python3 -m unittest discover -s tests`).
- **Frontend**: every panel above (Duplicates, Workflows, Intelligence,
  Admin/kill-switch/integrations, stage changes, CV paste box) is wired
  to the real API, not static mockup content.

## B. Partially implemented

- **CV parsing** works on plain text only — no PDF/DOCX/image/OCR
  pipeline (would need libraries this sandbox can't install; see D).
- **Natural-language workflow builder** recognizes 3 specific sentence
  patterns well; anything phrased differently returns an honest
  "couldn't parse this" message with supported examples, rather than
  guessing. A live LLM would generalize this significantly.
- **What-if simulator** supports two parameters (`volume`,
  `attendance_rate`) with real proportional math; the full spec's
  broader scenario space (e.g. "what if the job becomes remote") isn't
  built.
- **Multi-tenant isolation** is schema-ready (every table has
  `organization_id`) but there's only one seeded organization and no
  signup/org-creation flow yet.
- **Postgres readiness**: SQL avoids SQLite-only syntax except the
  `AUTOINCREMENT` keyword (documented fix: swap to `SERIAL` on
  Postgres); `app/db.py` isolates the connection so switching drivers
  is one function, but this hasn't been tested against a real Postgres
  instance (none available in this sandbox).

## C. Integration-ready (real code exists, gated only on a credential)

These are not buttons that say "coming soon" — the actual
request-building logic is written and tested to the point where it
raises a clear, typed `IntegrationNotConfigured` error rather than
silently pretending to work:

| Channel | What's built | What's missing |
|---|---|---|
| Facebook | Graph API post-publish call, mock verified end-to-end | `FACEBOOK_PAGE_ACCESS_TOKEN` + `FACEBOOK_PAGE_ID` |
| WhatsApp | Cloud API message-send call | `WHATSAPP_BUSINESS_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID` |
| Telegram | Bot API sendMessage call | `TELEGRAM_BOT_TOKEN` |
| Email | SMTP send via stdlib `smtplib` | `SMTP_HOST`/`SMTP_USER`/`SMTP_PASS` |
| LinkedIn | Adapter shell in place | Needs partner-approved Marketing API access + org URN — token alone isn't sufficient |
| Calendar | Adapter shell in place | Google Calendar OAuth credentials |
| Live AI model | `parse_command`/`route_intent` isolated as the single swap point | An Anthropic/OpenAI API key + an outbound network connection (this sandbox has neither) |
| Postgres | Portable SQL, isolated connection function | `psycopg2` install + a running Postgres instance |
| Docker deployment | `Dockerfile` + `docker-compose.yml` written, health-checked | No Docker daemon in this sandbox to actually build/run it — untested here, syntactically standard |

## D. Still requires your credentials/infrastructure

Exactly what E below spells out step by step. Summary list:
1. An LLM API key (Anthropic/OpenAI/local model) + outbound network access.
2. Meta Developer app + page access token (Facebook/WhatsApp).
3. Telegram bot token (free, fastest to get — no approval wait).
4. SMTP credentials (Gmail/SendGrid/etc.) for real email.
5. LinkedIn Marketing API partner access (slowest — real review process).
6. A hosting provider (Render/Railway/a VPS) so the app runs 24/7 on a
   stable URL instead of this session's temporary environment.
7. A Postgres database once traffic outgrows SQLite's single-writer model.
8. A payment processor (Stripe or similar) if you intend to charge
   other organizations — no billing code exists yet.

## E. Exact steps you personally need to do later

1. **Get it running permanently**: create a free account on Render.com
   (or similar), connect this codebase, it auto-detects the `Dockerfile`
   — no code changes needed.
2. **Turn on real AI**: get an API key from console.anthropic.com, add
   it as an environment variable, then `app/agent.py`'s
   `parse_command()` is the one function to replace with a real model
   call — everything downstream (validation/permissions/logging)
   already works and won't need touching.
3. **Turn on Telegram** (easiest real integration — do this first if you
   want to see a live external send): message @BotFather on Telegram,
   create a bot, get the token, set `TELEGRAM_BOT_TOKEN`. No approval
   wait.
4. **Turn on email**: get SMTP credentials from your email provider or
   SendGrid, set the four `SMTP_*` env vars.
5. **Turn on WhatsApp/Facebook**: register at developers.facebook.com,
   create an app, go through Meta's business verification (can take
   days), get a page access token.
6. **Turn on LinkedIn**: apply for LinkedIn Marketing API partner
   access — this has the longest, least certain approval process of
   any integration here.
7. **Move to Postgres** once you have real concurrent users: provision
   a Postgres instance (most hosts offer one as an add-on), `pip
   install psycopg2-binary`, update `app/db.py`'s `get_db()`.

## F. What I'd implement next, in priority order

1. **PDF/DOCX CV upload + real extraction pipeline** — the single
   biggest gap between "candidates paste text" and the spec's actual CV
   intelligence vision; needs `pdfplumber`/`python-docx` (pip installs,
   no external credential needed) plus a file-upload endpoint.
2. **Org signup/creation flow** to make the multi-tenant scaffold
   actually multi-tenant, not just schema-ready.
3. **A real background scheduler** (currently everything is
   request-triggered) for the "daily check" workflow trigger type and
   for time-based follow-up reminders (Section 32) — a simple polling
   thread checking due times, still stdlib-only.
4. **Broaden the NL workflow parser and command parser** once a live
   model is connected — this is where an LLM adds the most value with
   the least architectural change, since the tool/permission/audit
   pipeline downstream is already built to receive it.
5. **File-based document storage** (candidate documents, CVs as
   uploaded files rather than pasted text) with access control per
   organization.

---

## Addendum: Call-center / team-lead layer (this session's build)

Added on top of the existing product, fully tested (15 new unit tests,
42 total) and verified end-to-end against a live server:

**Fully working:**
- Candidate ownership (`assigned_recruiter`), manual reassignment, and
  round-robin auto-assignment on bulk import.
- Bulk lead import (`POST /candidates/bulk-import`) — creates
  candidates, distributes them across the recruiter team, and runs the
  existing dedup engine automatically.
- Call logging with outcome + objection reason, resetting last-contact
  and firing workflows on `call_logged`.
- **Smart callback queue** — real scoring (recency + deadline proximity
  from matching open jobs + historically-good-hour match + attempt
  count), with a forced-priority path for overdue callbacks. Verified a
  due callback always surfaces first.
- **Best-call-time** — computed per-candidate from their own answered
  calls, falling back to a language-segment average, then a global
  average, when there isn't enough candidate-specific history yet.
- **Objection trends** — real week-over-week counts per reason from the
  `calls` table.
- **Fair leaderboard** — hire rate adjusted by a real caseload-difficulty
  score (recycled candidates, rare-language pools, zero-experience
  candidates all raise a recruiter's difficulty multiplier).
- **Recycling alerts** — previously shift-rejected candidates
  automatically matched against newly-open remote/hybrid roles.
- **No-show risk** — a real score built from past no-shows, contact
  recency, abandoned screenings, and unanswered call counts.
- **Weekly team report** and **dynamic daily call goal** (remaining
  weekly target ÷ remaining working days), with a settable per-recruiter
  weekly target (admin/team-lead only).
- **Escalations** — any recruiter can flag a case to the team lead;
  admin/recruiter roles can resolve it with a note; fully audited.

**Known simplification:** "best call hour" and the smart queue's
answer-likelihood signal are only as good as the call volume behind
them — the seed data manufactures a plausible pattern (10-13h and
17-20h skew toward `answered`/`interested`) so the feature has
something real to find, but a live deployment needs real call volume
before these numbers mean much. This is a data-maturity limitation, not
a stubbed feature — the computation itself is real and will improve
automatically as real calls accumulate.

---

## Addendum 2: 4-tier roles + Admin Console (this session)

**Role hierarchy — how it's split, and why:**

| Role | Can do | Cannot do |
|---|---|---|
| **viewer** | Read everything | Any write action |
| **recruiter** | Everything viewer can, plus: log calls, change candidate stage, screen candidates, create/update offers, raise escalations, draft campaigns via the AI copilot | Reassign candidates, approve AI drafts, create/edit jobs or sources, resolve escalations, set targets, manage users, kill switch |
| **team_lead** | Everything recruiter can, plus: reassign candidates, bulk-import + auto-distribute leads, approve/reject AI-drafted campaigns and workflow actions, create/activate workflows, resolve escalations, set weekly call targets, create/edit jobs and sources | Manage user accounts, kill switch |
| **admin** | Everything, including: create/deactivate/promote user accounts, the global kill switch | — |

The logic lives in one place (`security.has_role(session, min_role)`,
rank-based) rather than scattered `role in (...)` lists — every route
handler now calls it consistently, which also caught and fixed a real
gap: several write endpoints (`create_candidate`, `cv-parse`,
`set_stage`, `log_call`, duplicate actions) previously had **no** role
check at all, meaning a `viewer` token could call them. Verified all of
this end-to-end: a `viewer` gets `403` on writes, a `recruiter` gets
`403` reassigning candidates or resolving escalations, a `team_lead`
gets `403` managing users, and `admin` can do everything — including
being blocked from deactivating/demoting *itself* (a deliberate
lockout guard).

Demo accounts: `mona/mona123` (team_lead), plus the existing
`admin`/`recruiter`/`sara`/`hassan`/`viewer`.

**Admin Console** (new "Admin" tab, visible to team_lead/admin): real
tabbed CRUD — Overview (kill switch, integrations, outbox), Jobs
(create + inline-editable table, `PATCH /api/jobs/:id`), Offers
(create + status dropdown), Sources (create + inline-editable table),
Users (admin-only: create accounts, change role, activate/deactivate —
all backed by real `PATCH`/`POST` endpoints, not placeholders).

**Known limitation:** deactivating a user blocks new logins immediately,
but a token issued before deactivation stays valid until the process
restarts — there's no session store to revoke a live token from, since
sessions are in-memory only (documented trade-off of the no-dependency,
stdlib-only architecture; a Redis-backed session store would close this
in a production deployment).

Full test count: **49 automated tests, all passing** (7 new this
session: role-hierarchy rank checks + CRUD sanity checks).
