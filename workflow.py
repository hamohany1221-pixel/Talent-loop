"""
Workflow engine (Section 51) — real triggers/conditions/actions stored as
structured JSON, evaluated against real events, with every execution
logged. Actions that would touch the outside world (send a message,
publish something) always land in `approvals` instead of firing directly
— Section 47's approval-gating applies to automations, not just the
copilot.
"""
import json
import re
import secrets
import datetime

OPS = {
    "gte": lambda a, b: a >= b, "lte": lambda a, b: a <= b, "eq": lambda a, b: a == b,
    "gt": lambda a, b: a > b, "lt": lambda a, b: a < b, "contains": lambda a, b: b in (a or ""),
}


def is_killed(conn):
    row = conn.execute("SELECT active FROM kill_switch WHERE id=1").fetchone()
    return bool(row and row["active"])


def set_kill_switch(conn, active, actor):
    from app.db import log_audit
    conn.execute("UPDATE kill_switch SET active=?, updated_at=?, updated_by=? WHERE id=1",
                 (1 if active else 0, datetime.datetime.now().isoformat(timespec="seconds"), actor))
    conn.commit()
    log_audit(conn, actor, "Kill switch " + ("activated — all automations halted" if active else "deactivated"), "")


def create_workflow(conn, organization_id, name, trigger_type, conditions, actions, created_by, status="draft"):
    wid = "WF" + secrets.token_hex(4)
    conn.execute("""INSERT INTO workflows (id,organization_id,name,trigger_type,conditions,actions,status,
        created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                 (wid, organization_id, name, trigger_type, json.dumps(conditions), json.dumps(actions),
                  status, created_by, datetime.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return wid


def evaluate_conditions(conditions, context):
    for c in conditions:
        field, op, value = c.get("field"), c.get("op", "eq"), c.get("value")
        actual = context.get(field)
        fn = OPS.get(op)
        if not fn:
            return False
        try:
            if not fn(actual, value):
                return False
        except TypeError:
            return False
    return True


def execute_actions(conn, workflow, actions, context, organization_id):
    from app.db import log_audit, log_candidate_event
    results = []
    for action in actions:
        atype = action.get("type")
        if atype == "tag":
            results.append(f"tag:{action.get('value')}")
        elif atype == "add_to_pool":
            results.append(f"add_to_pool:{action.get('pool')}")
        elif atype == "notify":
            results.append(f"notify:{action.get('message', 'workflow triggered')}")
        elif atype in ("send_message", "publish"):
            # anything that reaches outside the system requires human approval
            channel = action.get("channel", "email")
            title = f"Workflow action pending approval — {workflow['name']}"
            detail = f"Action '{atype}' via {channel} for candidate {context.get('candidate_id','?')}: {action.get('params', {})}"
            conn.execute("""INSERT INTO approvals (organization_id,title,detail,action_type,action_payload,status,created_at)
                VALUES (?,?,?,?,?,'pending',?)""",
                (organization_id, title, detail, atype, json.dumps({**action, "candidate_id": context.get("candidate_id")}),
                 datetime.datetime.now().isoformat(timespec="seconds")))
            results.append(f"{atype}:queued_for_approval")
        else:
            results.append(f"unknown_action:{atype}")
        if context.get("candidate_id"):
            log_candidate_event(conn, context["candidate_id"], "workflow_action",
                                 f"{workflow['name']}: {atype}")
    return results


def evaluate_workflows(conn, organization_id, trigger_type, context, actor="system"):
    """Called by route handlers whenever a real event happens (stage
    change, screening completed, match computed). Returns executions run."""
    from app.db import log_audit
    if is_killed(conn):
        return {"skipped": "kill_switch_active"}
    rows = conn.execute("""SELECT * FROM workflows WHERE organization_id=? AND trigger_type=? AND status='active'""",
                         (organization_id, trigger_type)).fetchall()
    executions = []
    for r in rows:
        wf = dict(r)
        conditions = json.loads(wf["conditions"])
        if not evaluate_conditions(conditions, context):
            continue
        actions = json.loads(wf["actions"])
        results = execute_actions(conn, wf, actions, context, organization_id)
        conn.execute("""INSERT INTO workflow_executions (workflow_id,candidate_id,ts,status,detail)
            VALUES (?,?,?,?,?)""", (wf["id"], context.get("candidate_id"),
             datetime.datetime.now().isoformat(timespec="seconds"), "executed", "; ".join(results)))
        conn.commit()
        log_audit(conn, actor, f"Workflow executed: {wf['name']}", "; ".join(results))
        executions.append({"workflow_id": wf["id"], "workflow_name": wf["name"], "results": results})
    return {"executions": executions}


# ============================================================
# Natural-language workflow builder (Section 52) — rule-based pattern
# matching over a handful of templates. Honest about its limits: anything
# outside the recognized patterns returns a clear "couldn't parse" result
# rather than guessing.
# ============================================================
def parse_nl_workflow(text):
    t = text.strip()

    # "When a German B2 candidate scores above 85, notify me and add them to interview workflow."
    m = re.search(
        r"when a (\w+)\s*([ABC][12])?\s*candidate scores? (above|over|below|under)\s*(\d+)"
        r"(?:.*?notify me)?(?:.*?add (?:them|him|her) to (?:the )?([\w\s]+))?",
        t, re.I)
    if m:
        lang, level, direction, threshold, pool = m.groups()
        op = "gte" if direction and direction.lower() in ("above", "over") else "lte"
        conditions = [{"field": "language", "op": "eq", "value": lang.capitalize()}]
        if level:
            conditions.append({"field": "level", "op": "eq", "value": level.upper()})
        conditions.append({"field": "score", "op": op, "value": int(threshold)})
        actions = []
        if "notify me" in t.lower():
            actions.append({"type": "notify", "message": f"{lang.capitalize()} candidate crossed the score threshold"})
        if pool:
            actions.append({"type": "add_to_pool", "pool": pool.strip()})
        if not actions:
            actions.append({"type": "notify", "message": "Condition met"})
        return {"ok": True, "trigger_type": "match_computed", "conditions": conditions, "actions": actions,
                "name": f"Auto: {lang.capitalize()} score {direction} {threshold}"}

    # "When a candidate applies for <job>, send them a screening link."
    m = re.search(r"when a candidate applies(?: for ([\w\s]+))?.*?send (?:them|him|her) (.+)", t, re.I)
    if m:
        job_hint, message = m.groups()
        conditions = [{"field": "job_hint", "op": "contains", "value": (job_hint or "").strip()}] if job_hint else []
        return {"ok": True, "trigger_type": "application_received", "conditions": conditions,
                "actions": [{"type": "send_message", "params": {"message": message.strip()}}],
                "name": "Auto: application follow-up"}

    # "When interview attendance is below X%, notify me"
    m = re.search(r"when interview attendance is below (\d+)%?.*?notify", t, re.I)
    if m:
        return {"ok": True, "trigger_type": "daily_check", "conditions": [
            {"field": "attendance_rate_pct", "op": "lte", "value": int(m.group(1))}],
            "actions": [{"type": "notify", "message": "Interview attendance below threshold"}],
            "name": f"Auto: attendance below {m.group(1)}%"}

    return {"ok": False, "reason": "Couldn't match this to a supported workflow pattern. Supported patterns: "
                                    "'When a <language> [<level>] candidate scores above/below <N>, notify me "
                                    "[and add them to <pool>]', 'When a candidate applies [for <job>], send them "
                                    "<message>', 'When interview attendance is below <N>%, notify me'.",
            "supported_examples": [
                "When a German B2 candidate scores above 85, notify me and add them to interview pool.",
                "When a candidate applies for Sales, send them a screening link.",
                "When interview attendance is below 50%, notify me.",
            ]}
