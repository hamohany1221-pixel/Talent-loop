"""
AI Recruiter Copilot — tool-calling agent (Sections 45-47).

Architecture: natural-language command → parse_command() picks a
registered tool + typed args → dispatch() validates the args against the
tool's schema, checks the caller's role against TOOL_PERMISSIONS
(app/security.py) independent of anything the parser decided, executes
the tool's real function against the database, logs the call to
agent_tool_calls + audit_log, and returns a structured result.

The NLU step (parse_command) is regex/keyword-based because there's no
live LLM connected in this environment — that limitation is isolated to
this one function. Everything downstream of it (validation, permissions,
execution, logging) is the same real pipeline a live model's tool calls
would go through, so swapping in a model later only touches
parse_command().
"""
import re
import json
import datetime
from app import domain
from app.security import tool_allowed
from app.db import log_audit


class ToolError(Exception):
    pass


def _job_by_ref(conn, text):
    m = re.search(r"\bj\d+\b", text, re.I)
    if m:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (m.group(0).upper(),)).fetchone()
        if row:
            return domain.row_to_job(row)
    for row in conn.execute("SELECT * FROM jobs"):
        job = domain.row_to_job(row)
        if job["language"].lower() in text.lower():
            return job
    return None


# ============================================================
# Tool implementations — real functions, typed args, DB-backed.
# ============================================================
def tool_search_candidates(conn, organization_id, job_id=None, location=None, min_score=50, limit=5):
    if not job_id:
        raise ToolError("job_id is required")
    job, results = domain.match_candidates_for_job(conn, job_id, min_score=min_score)
    if job is None:
        raise ToolError(f"job {job_id} not found")
    if location:
        results = [r for r in results if location.lower() in (r["candidate"]["location"] or "").lower()]
    return {"job": job["title"], "count": len(results), "results": results[:limit]}


def tool_match_candidates(conn, organization_id, job_id):
    job, results = domain.match_candidates_for_job(conn, job_id, min_score=0)
    if job is None:
        raise ToolError(f"job {job_id} not found")
    return {"job": job["title"], "results": results}


def tool_find_never_hired(conn, organization_id):
    return {"candidates": domain.never_hired_but_contacted(conn)}


def tool_get_recruitment_stats(conn, organization_id):
    stale = conn.execute("""SELECT * FROM candidates WHERE merged_into IS NULL AND last_contact_days>3
        AND stage IN ('Contacted','Screening','New')""").fetchall()
    return {"overdue_follow_ups": [dict(r) for r in stale], "count": len(stale)}


def tool_find_bottlenecks(conn, organization_id, campaign_id=None):
    rows = conn.execute("SELECT * FROM campaigns" + (" WHERE id=?" if campaign_id else ""),
                         (campaign_id,) if campaign_id else ()).fetchall()
    out = []
    for r in rows:
        camp = dict(r)
        stages, worst = domain.find_bottleneck(camp)
        out.append({"campaign": camp["name"], "worst_stage": worst})
    return {"campaigns": out}


def tool_rediscover_candidates(conn, organization_id, job_id):
    job, results = domain.match_candidates_for_job(conn, job_id, min_score=50)
    if job is None:
        raise ToolError(f"job {job_id} not found")
    return {"job": job["title"], "count": len(results), "results": results}


def tool_generate_daily_briefing(conn, organization_id):
    return domain.daily_briefing(conn)


def tool_create_screening_questions(conn, organization_id, job_id):
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise ToolError(f"job {job_id} not found")
    job = domain.row_to_job(row)
    return {"job": job["title"], "questions": domain.generate_screening_questions(job)}


def tool_create_campaign(conn, organization_id, job_id, actor):
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise ToolError(f"job {job_id} not found")
    job = domain.row_to_job(row)
    title = f"Draft campaign — {job['title']}"
    detail = (f"Target: fill remaining {job['vacancies']-job['filled']} vacancies for {job['title']}. "
              f"Channels: Facebook, LinkedIn, Telegram (adapters are integration-ready, not connected). "
              f"3 content variants. Awaiting approval.")
    conn.execute("""INSERT INTO approvals (organization_id,title,detail,action_type,action_payload,status,created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (organization_id, title, detail, "create_campaign", json.dumps({"job_id": job_id}), "pending",
         datetime.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return {"drafted": title, "status": "pending_approval"}


def tool_compare_jobs(conn, organization_id, job_ids):
    out = []
    for jid in job_ids:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if row:
            out.append(domain.row_to_job(row))
    return {"jobs": out}


TOOLS = {
    "searchCandidates": {"fn": tool_search_candidates,
                          "schema": {"job_id": str, "location": (str, type(None)), "min_score": int, "limit": int},
                          "description": "Search/rank eligible candidates for a job, optionally filtered by location."},
    "matchCandidates": {"fn": tool_match_candidates, "schema": {"job_id": str},
                         "description": "Match all candidates against a specific job."},
    "findNeverHired": {"fn": tool_find_never_hired, "schema": {},
                        "description": "Find candidates contacted before but never hired."},
    "getRecruitmentStats": {"fn": tool_get_recruitment_stats, "schema": {},
                             "description": "Get overdue follow-ups and pipeline stats."},
    "findBottlenecks": {"fn": tool_find_bottlenecks, "schema": {"campaign_id": (str, type(None))},
                         "description": "Analyze funnel bottlenecks for one or all campaigns."},
    "rediscoverCandidates": {"fn": tool_rediscover_candidates, "schema": {"job_id": str},
                              "description": "Rediscover existing database candidates against a new/updated job."},
    "generateDailyBriefing": {"fn": tool_generate_daily_briefing, "schema": {},
                               "description": "Produce the daily AI briefing (priorities, health, anomalies, recs)."},
    "createScreeningQuestions": {"fn": tool_create_screening_questions, "schema": {"job_id": str},
                                  "description": "Auto-generate screening questions from a job's requirements."},
    "createCampaign": {"fn": tool_create_campaign, "schema": {"job_id": str},
                        "description": "Draft a campaign for a job — lands in the Approval Center."},
    "compareJobs": {"fn": tool_compare_jobs, "schema": {"job_ids": list},
                     "description": "Compare two or more jobs side by side."},
}


def validate_args(schema, args):
    clean = {}
    for key, expected_type in schema.items():
        if key not in args:
            if isinstance(expected_type, tuple) and type(None) in expected_type:
                clean[key] = None
                continue
            raise ToolError(f"missing required argument: {key}")
        val = args[key]
        types = expected_type if isinstance(expected_type, tuple) else (expected_type,)
        if not isinstance(val, types):
            # light coercion for the common int-as-string case from a text command
            if int in types:
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    raise ToolError(f"argument '{key}' must be {expected_type}")
            else:
                raise ToolError(f"argument '{key}' must be {expected_type}")
        clean[key] = val
    return clean


def dispatch(conn, session, tool_name, args):
    """The real enforcement point: permission → validation → execution →
    audit. Every one of these steps runs regardless of how tool_name/args
    were produced (parsed from text here, or a live LLM's tool call later)."""
    from app.workflow import is_killed
    allowed, why = tool_allowed(session, tool_name)
    permission_str = "allowed" if allowed else f"denied: {why}"

    conn.execute("""INSERT INTO agent_tool_calls (ts,actor,tool_name,args,permission_check,result_summary)
        VALUES (?,?,?,?,?,?)""", (datetime.datetime.now().isoformat(timespec="seconds"), session["username"],
         tool_name, json.dumps(args), permission_str, ""))
    conn.commit()

    if not allowed:
        raise ToolError(f"Permission denied: tool '{tool_name}' {why}.")
    if is_killed(conn) and tool_name in ("createCampaign",):
        raise ToolError("Kill switch is active — automations that create or publish are paused.")

    tool = TOOLS.get(tool_name)
    if not tool:
        raise ToolError(f"Unknown tool: {tool_name}")
    clean_args = validate_args(tool["schema"], args)
    if tool_name == "createCampaign":
        clean_args["actor"] = session["username"]
    result = tool["fn"](conn, session["organization_id"], **clean_args)
    log_audit(conn, session["username"], f"AI tool call: {tool_name}", json.dumps(args)[:200],
              organization_id=session["organization_id"])
    return result


# ============================================================
# Command parser (the regex-based NLU stand-in — see module docstring)
# ============================================================
def parse_command(conn, text):
    t = text.lower()

    if re.search(r"never.?hired|contacted.*never hired|but never hired", t):
        return "findNeverHired", {}

    if re.search(r"\bmatch\b.*\ball\b.*candidat|match all candidates against", t):
        job = _job_by_ref(conn, text)
        if job:
            return "matchCandidates", {"job_id": job["id"]}

    if re.search(r"find|search|show|best candidates?", t) and "candidat" in t:
        job = _job_by_ref(conn, text)
        loc_match = re.search(r"in ([a-z\s]+?)(?:\.|$)", t)
        location = loc_match.group(1).strip() if loc_match and _job_by_ref(conn, text) else None
        return "searchCandidates", {"job_id": job["id"] if job else "J1", "location": location, "min_score": 50, "limit": 8}

    if "rediscover" in t:
        job = _job_by_ref(conn, text)
        return "rediscoverCandidates", {"job_id": job["id"] if job else "J1"}

    if re.search(r"follow.?up", t):
        return "getRecruitmentStats", {}

    if re.search(r"why|underperform|bottleneck|drop", t):
        return "findBottlenecks", {"campaign_id": None}

    if re.search(r"screening questions?", t):
        job = _job_by_ref(conn, text)
        return "createScreeningQuestions", {"job_id": job["id"] if job else "J1"}

    if "campaign" in t and ("create" in t or "for" in t):
        job = _job_by_ref(conn, text)
        return "createCampaign", {"job_id": job["id"] if job else "J3"}

    if re.search(r"what should i (do|focus on)|today\'?s? priorit|daily briefing", t):
        return "generateDailyBriefing", {}

    if "compare" in t:
        ids = re.findall(r"\bj\d+\b", t, re.I)
        if len(ids) >= 2:
            return "compareJobs", {"job_ids": [i.upper() for i in ids]}

    return None, None
