# Talent Loop API Reference

Base URL: `http://localhost:8000/api`

All endpoints except `POST /auth/login`, `POST /jobs/extract`,
`GET /landing/:job_id`, and `POST /apply/:job_id` require:
`Authorization: Bearer <token>`

Rate limit: 120 requests/minute per token (or per IP for unauthenticated
calls) — returns `429` when exceeded.

## Auth
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/auth/login` | none | `{username, password}` → `{token, username, role}` |
| GET | `/me` | any | current session info |

## Jobs
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/jobs` | any | list all jobs |
| POST | `/jobs/extract` | none | `{text}` → rule-based field extraction (labeled simulated) |

## Candidates
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/candidates` | any | list active (non-merged) candidates |
| POST | `/candidates` | recruiter+ | create; triggers dedup check |
| GET | `/candidates/:id/timeline` | any | chronological event log |
| POST | `/candidates/:id/cv-parse` | any | `{text, apply?}` → parsed CV fields; `apply:true` writes them to the profile |
| POST | `/candidates/:id/stage` | any | `{stage}` → updates pipeline stage, logs event, fires `candidate_stage_changed` workflows |

## Matching
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/match/:job_id` | any | ranked, explainable match results for every active candidate |

## Deduplication
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/duplicates` | any | pending fuzzy-match suggestions |
| POST | `/duplicates/refresh` | any | recompute suggestions |
| POST | `/duplicates/:id/merge` | any | `{keep}` → non-destructive merge |
| POST | `/duplicates/:id/reject` | any | dismiss suggestion |

## Data quality
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/data-quality` | any | missing/invalid field report |

## Interviews / Offers / Sources / Campaigns
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/interviews` | any | |
| GET | `/offers` | any | |
| GET | `/sources` | any | |
| GET | `/campaigns` | any | includes computed `funnel` + `bottleneck` |
| GET | `/campaigns/:id/variants` | any | A/B variants ranked by qualified rate |
| POST | `/campaigns/:id/variants` | any | add a variant with metrics |
| GET | `/pools` | any | candidates grouped by language |

## Screening
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/screening/start` | any | `{candidate_id}` → starts a multi-turn session |
| POST | `/screening/:session_id/answer` | any | `{text}` → next question or verdict; on pass, fires `match_computed` workflows |

## Recruitment Intelligence
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/intelligence/anomalies?campaign_id=` | any | z-score based anomaly detection over `daily_metrics` |
| GET | `/intelligence/forecast/:campaign_id?days=&metric=` | any | linear-regression projection with confidence band |
| POST | `/intelligence/whatif` *(via agent, see below)* | recruiter+ | scenario recompute |
| GET | `/intelligence/health-score` | any | per-job 🟢🟡🔴 |
| GET | `/intelligence/recommendations` | any | evidence-backed recommendations |
| GET | `/intelligence/daily-briefing` | any | combined priorities/health/anomalies/recs |

## Workflows
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/workflows` | any | |
| POST | `/workflows` | recruiter+ | structured create `{name, trigger_type, conditions, actions}` |
| POST | `/workflows/nl` | recruiter+ | `{text}` → rule-based NL parse → draft workflow |
| POST | `/workflows/:id/activate` | recruiter+ | |
| POST | `/workflows/:id/pause` | recruiter+ | |
| GET | `/workflows/executions` | any | execution log |

## Approvals & Audit
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/approvals` | any | |
| POST | `/approvals/:id/approve` | recruiter+ | executes the underlying action (e.g. sends via integration adapter) |
| POST | `/approvals/:id/reject` | recruiter+ | |
| GET | `/audit` | any | last 150 entries |
| GET | `/agent/tool-calls` | any | last 50 agent tool invocations with permission-check result |

## AI Agent / Copilot
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/agent/command` (alias: `/copilot`) | any | `{text}` → parses → validates → permission-checks → executes a registered tool → logs → returns result |
| GET | `/tools` | any | registered tool names, descriptions, typed schemas |

## Integrations
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/integrations` | any | configured/not-configured status per channel |
| GET | `/outbox` | any | every message/post attempt (mock or live), last 50 |

## Kill switch
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/killswitch` | any | |
| POST | `/killswitch/activate` | admin | halts workflow execution and campaign-creation tool calls |
| POST | `/killswitch/deactivate` | admin | |

## Public (no auth)
| Method | Path | Notes |
|---|---|---|
| GET | `/landing/:job_id` | rendered application page for a job |
| POST | `/apply/:job_id` | `{name, phone, email?, language_level?, experience_years?, utm_source?}` → creates candidate + application record, fires `application_received` workflows |

## Call Center / Team Lead
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/team/members` | any | list of recruiter usernames |
| POST | `/candidates/:id/assign` | recruiter+ | `{recruiter}` → reassign ownership |
| POST | `/candidates/bulk-import` | recruiter+ | `{rows:[{name,phone,email?,language?,level?,...}], auto_assign?}` → creates + round-robin assigns, runs dedup |
| GET | `/calls/:candidate_id` | any | call history for one candidate |
| POST | `/calls` | any | `{candidate_id, outcome, objection_reason?, notes?, callback_at?}` → logs a call, resets last-contact, fires `call_logged` workflows |
| GET | `/queue?recruiter=` | any | smart callback queue, ranked by urgency/deadline/best-time-to-call/attempt count |
| GET | `/objections?window_days=7` | any | objection-reason trend vs. the prior window |
| GET | `/leaderboard` | any | difficulty-adjusted recruiter performance |
| GET | `/recycle-alerts` | any | previously shift-rejected candidates matching newly-open remote/hybrid roles |
| GET | `/no-show-risk` | any | upcoming interviews ranked by a real risk score |
| GET | `/team/workload` | any | per-recruiter caseload, overdue count, calls this week, daily goal |
| GET | `/team/weekly-report` | any | combined weekly summary for the team lead |
| GET | `/team/daily-goal?recruiter=` | any | today's call target for a recruiter (defaults to self) |
| POST | `/team/targets` | admin | `{recruiter, weekly_call_target}` → sets this week's target |
| POST | `/escalations` | any | `{candidate_id, reason}` → raises a case to the team lead |
| GET | `/escalations?status=open|resolved` | any | |
| POST | `/escalations/:id/resolve` | recruiter+ | `{note}` |

## Ops
| Method | Path | Notes |
|---|---|---|
| GET | `/health` | liveness check for Docker/load balancers |
