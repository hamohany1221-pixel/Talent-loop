"""
Call-center / team-lead layer. Real logic over real tables — every
number below is computed from `calls`, `candidates.assigned_recruiter`,
`interviews`, and `escalations`, not templated.
"""
import datetime
import statistics
import math
from app import domain

OUTCOMES = ["answered", "no_answer", "voicemail", "interested", "not_interested", "callback_requested"]
OBJECTION_REASONS = ["salary", "shift", "location", "language_level", "timing", "already_placed", "other"]
DEFAULT_WEEKLY_TARGET = 150


# ============================================================
# Ownership / assignment
# ============================================================
def active_recruiters(conn):
    rows = conn.execute("SELECT username FROM users WHERE role='recruiter' ORDER BY username").fetchall()
    return [r["username"] for r in rows]


def assign_candidate(conn, candidate_id, recruiter, actor):
    from app.db import log_candidate_event, log_audit
    conn.execute("UPDATE candidates SET assigned_recruiter=? WHERE id=?", (recruiter, candidate_id))
    log_candidate_event(conn, candidate_id, "assigned", f"Assigned to {recruiter} by {actor}")
    log_audit(conn, actor, "Assigned candidate", f"{candidate_id} -> {recruiter}")
    conn.commit()


def round_robin_assign(conn, candidate_ids, recruiters, actor):
    if not recruiters:
        return {}
    mapping = {}
    for i, cid in enumerate(candidate_ids):
        r = recruiters[i % len(recruiters)]
        assign_candidate(conn, cid, r, actor)
        mapping[cid] = r
    return mapping


def bulk_import_candidates(conn, organization_id, rows, actor, auto_assign=True):
    import secrets
    created = []
    recruiters = active_recruiters(conn) if auto_assign else []
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for i, row in enumerate(rows):
        cid = "C" + secrets.token_hex(4).upper()
        conn.execute("""INSERT INTO candidates (id,organization_id,name,email,phone,location,education,
            experience_years,source,stage,last_contact_days,notes,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, organization_id, row.get("name", "Unnamed"), row.get("email"), row.get("phone"),
             row.get("location", ""), row.get("education", ""), int(row.get("experience_years") or 0),
             row.get("source", "Bulk import"), "New", 0, row.get("notes", ""), now))
        lang = row.get("language")
        if lang and row.get("level"):
            conn.execute("INSERT INTO candidate_languages (candidate_id,lang,level) VALUES (?,?,?)",
                         (cid, lang, row["level"]))
        from app.db import log_candidate_event
        log_candidate_event(conn, cid, "created", f"Bulk imported by {actor}")
        created.append(cid)
    conn.commit()
    assignment = round_robin_assign(conn, created, recruiters, actor) if auto_assign else {}
    new_dupes = domain.refresh_duplicate_suggestions(conn)
    return {"created": created, "assignment": assignment, "duplicate_suggestions_found": new_dupes}


# ============================================================
# Call logging
# ============================================================
def log_call(conn, candidate_id, recruiter, outcome, objection_reason=None, notes="", callback_at=None):
    from app.db import log_candidate_event
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome}")
    now = datetime.datetime.now()
    conn.execute("""INSERT INTO calls (candidate_id,recruiter,ts,hour_of_day,weekday,outcome,objection_reason,notes,callback_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (candidate_id, recruiter, now.isoformat(timespec="seconds"), now.hour, now.weekday(),
         outcome, objection_reason, notes, callback_at))
    conn.execute("UPDATE candidates SET last_contact_days=0 WHERE id=?", (candidate_id,))
    detail = f"{recruiter}: {outcome}" + (f" (reason: {objection_reason})" if objection_reason else "")
    log_candidate_event(conn, candidate_id, "call_logged", detail)
    conn.commit()


def call_history(conn, candidate_id):
    rows = conn.execute("SELECT * FROM calls WHERE candidate_id=? ORDER BY ts DESC", (candidate_id,)).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# Best call time (Section: "when do candidates like this answer?")
# ============================================================
def best_call_hours(conn, candidate_id=None, language=None, top_n=3):
    params = []
    base = "SELECT hour_of_day, COUNT(*) c FROM calls WHERE outcome IN ('answered','interested')"
    if candidate_id:
        base += " AND candidate_id=?"
        params.append(candidate_id)
        rows = conn.execute(base + " GROUP BY hour_of_day ORDER BY c DESC", params).fetchall()
        if rows and sum(r["c"] for r in rows) >= 3:
            return {"basis": "this candidate", "hours": [{"hour": r["hour_of_day"], "successful_calls": r["c"]} for r in rows[:top_n]]}
        # fall through to segment/global if too little candidate-specific data
        cand = domain.get_candidate_full(conn, candidate_id)
        language = cand["languages"][0]["lang"] if cand and cand["languages"] else None

    if language:
        rows = conn.execute("""SELECT c.hour_of_day, COUNT(*) cnt FROM calls c
            JOIN candidates cd ON cd.id = c.candidate_id
            JOIN candidate_languages cl ON cl.candidate_id = cd.id
            WHERE c.outcome IN ('answered','interested') AND cl.lang=?
            GROUP BY c.hour_of_day ORDER BY cnt DESC""", (language,)).fetchall()
        if rows and sum(r["cnt"] for r in rows) >= 5:
            return {"basis": f"{language}-speaking candidates", "hours": [{"hour": r["hour_of_day"], "successful_calls": r["cnt"]} for r in rows[:top_n]]}

    rows = conn.execute("""SELECT hour_of_day, COUNT(*) c FROM calls WHERE outcome IN ('answered','interested')
        GROUP BY hour_of_day ORDER BY c DESC""").fetchall()
    return {"basis": "all candidates (insufficient specific data)", "hours": [{"hour": r["hour_of_day"], "successful_calls": r["c"]} for r in rows[:top_n]]}


# ============================================================
# Smart callback queue
# ============================================================
def smart_queue(conn, recruiter=None, limit=20):
    now = datetime.datetime.now()
    query = "SELECT * FROM candidates WHERE merged_into IS NULL AND stage NOT IN ('Hired','Rejected')"
    params = []
    if recruiter:
        query += " AND assigned_recruiter=?"
        params.append(recruiter)
    rows = conn.execute(query, params).fetchall()

    jobs = [domain.row_to_job(r) for r in conn.execute("SELECT * FROM jobs WHERE status='open'")]
    global_best = best_call_hours(conn)
    global_hours = {h["hour"] for h in global_best["hours"]}

    queue = []
    for r in rows:
        cand = domain.get_candidate_full(conn, r["id"])
        last_call = conn.execute("SELECT * FROM calls WHERE candidate_id=? ORDER BY ts DESC LIMIT 1", (r["id"],)).fetchone()

        # forced priority: an overdue/due callback
        if last_call and last_call["callback_at"]:
            try:
                cb_time = datetime.datetime.fromisoformat(last_call["callback_at"])
                if cb_time <= now:
                    queue.append({"candidate": cand, "priority": "callback_due", "score": 1000,
                                  "reason": f"Callback was requested for {last_call['callback_at']} — overdue." if cb_time < now
                                            else f"Callback due now ({last_call['callback_at']})."})
                    continue
            except ValueError:
                pass

        # urgency from recency
        urgency = min(1.0, cand["last_contact_days"] / 10)

        # deadline proximity from matching open jobs
        deadline_score, nearest_job = 0.0, None
        for job in jobs:
            if any(l["lang"] == job["language"] for l in cand["languages"]):
                try:
                    days_left = (datetime.date.fromisoformat(job["deadline"]) - datetime.date.today()).days
                except (ValueError, TypeError):
                    continue
                s = max(0.0, min(1.0, 1 - days_left / 30))
                if s > deadline_score:
                    deadline_score, nearest_job = s, job["title"]

        # answer likelihood: is now a historically good hour for this candidate/segment?
        cand_best = best_call_hours(conn, candidate_id=r["id"])
        cand_hours = {h["hour"] for h in cand_best["hours"]}
        answer_likelihood = 1.0 if now.hour in cand_hours else (0.6 if now.hour in global_hours else 0.3)

        attempts = conn.execute("SELECT COUNT(*) c FROM calls WHERE candidate_id=?", (r["id"],)).fetchone()["c"]
        attempts_factor = 1.0 if attempts == 0 else max(0.3, 1 - attempts * 0.15)

        score = round((urgency * 0.35 + deadline_score * 0.30 + answer_likelihood * 0.25 + attempts_factor * 0.10) * 100)
        reasons = [f"{cand['last_contact_days']}d since last contact"]
        if nearest_job:
            reasons.append(f"deadline pressure from {nearest_job}")
        reasons.append(f"{'good' if answer_likelihood>=0.6 else 'uncertain'} time to call ({cand_best['basis']})")
        if attempts:
            reasons.append(f"{attempts} prior attempt(s)")
        queue.append({"candidate": cand, "priority": "scored", "score": score, "reason": "; ".join(reasons)})

    queue.sort(key=lambda q: q["score"], reverse=True)
    return queue[:limit]


# ============================================================
# Objection library / trends
# ============================================================
def objection_trends(conn, window_days=7):
    now = datetime.datetime.now()
    cur_start = (now - datetime.timedelta(days=window_days)).isoformat(timespec="seconds")
    prev_start = (now - datetime.timedelta(days=window_days * 2)).isoformat(timespec="seconds")

    cur_rows = conn.execute("""SELECT objection_reason, COUNT(*) c FROM calls
        WHERE outcome='not_interested' AND objection_reason IS NOT NULL AND ts>=?
        GROUP BY objection_reason""", (cur_start,)).fetchall()
    prev_rows = conn.execute("""SELECT objection_reason, COUNT(*) c FROM calls
        WHERE outcome='not_interested' AND objection_reason IS NOT NULL AND ts>=? AND ts<?
        GROUP BY objection_reason""", (prev_start, cur_start)).fetchall()

    cur_map = {r["objection_reason"]: r["c"] for r in cur_rows}
    prev_map = {r["objection_reason"]: r["c"] for r in prev_rows}
    reasons = set(cur_map) | set(prev_map)
    out = []
    for reason in reasons:
        cur, prev = cur_map.get(reason, 0), prev_map.get(reason, 0)
        out.append({"reason": reason, "count_this_window": cur, "count_previous_window": prev,
                     "change": cur - prev, "trend": "up" if cur > prev else ("down" if cur < prev else "flat")})
    out.sort(key=lambda x: x["count_this_window"], reverse=True)
    return {"window_days": window_days, "objections": out}


# ============================================================
# Fair (difficulty-adjusted) leaderboard
# ============================================================
def candidate_difficulty(conn, candidate):
    score, reasons = 1.0, []
    if candidate["notes"] and "reject" in candidate["notes"].lower():
        score += 0.5
        reasons.append("previously rejected (recycled)")
    if candidate["experience_years"] == 0:
        score += 0.2
        reasons.append("no experience")
    if candidate["languages"]:
        lang = candidate["languages"][0]["lang"]
        count = conn.execute("""SELECT COUNT(DISTINCT cl.candidate_id) c FROM candidate_languages cl
            JOIN candidates c ON c.id=cl.candidate_id WHERE cl.lang=? AND c.merged_into IS NULL""",
            (lang,)).fetchone()["c"]
        if count <= 3:
            score += 0.3
            reasons.append(f"rare language pool ({lang}, only {count} candidates)")
    return round(score, 2), reasons


def fair_leaderboard(conn):
    recruiters = active_recruiters(conn)
    week_start = (datetime.date.today() - datetime.timedelta(days=datetime.date.today().weekday())).isoformat()
    out = []
    for r in recruiters:
        rows = conn.execute("SELECT * FROM candidates WHERE assigned_recruiter=? AND merged_into IS NULL", (r,)).fetchall()
        assigned = [domain.get_candidate_full(conn, row["id"]) for row in rows]
        hires = sum(1 for c in assigned if c["stage"] == "Hired")
        difficulties = [candidate_difficulty(conn, c)[0] for c in assigned] if assigned else [1.0]
        avg_difficulty = round(statistics.mean(difficulties), 2)
        raw_rate = round(hires / len(assigned), 3) if assigned else 0.0
        adjusted_score = round(raw_rate * avg_difficulty, 3)
        calls_this_week = conn.execute("SELECT COUNT(*) c FROM calls WHERE recruiter=? AND ts>=?",
                                        (r, week_start)).fetchone()["c"]
        out.append({"recruiter": r, "assigned_count": len(assigned), "hires": hires,
                     "raw_hire_rate": raw_rate, "avg_caseload_difficulty": avg_difficulty,
                     "adjusted_score": adjusted_score, "calls_this_week": calls_this_week})
    out.sort(key=lambda x: x["adjusted_score"], reverse=True)
    return out


# ============================================================
# Recycling alerts
# ============================================================
def recycle_alerts(conn):
    rejected = conn.execute("""SELECT * FROM candidates WHERE merged_into IS NULL AND stage='Rejected'
        AND notes LIKE '%shift%'""").fetchall()
    open_jobs = [domain.row_to_job(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE status='open' AND work_model IN ('Remote','Hybrid')")]
    alerts = []
    for row in rejected:
        cand = domain.get_candidate_full(conn, row["id"])
        matches = [j for j in open_jobs if any(l["lang"] == j["language"] for l in cand["languages"])]
        if matches:
            alerts.append({"candidate": cand, "matching_jobs": [j["title"] for j in matches],
                            "reason": "Previously rejected for a shift conflict — matching job(s) now offer remote/hybrid work."})
    return alerts


# ============================================================
# No-show risk
# ============================================================
def no_show_risk(conn, candidate_id):
    score, reasons = 0, []
    past_no_shows = conn.execute("SELECT COUNT(*) c FROM interviews WHERE candidate_id=? AND status='No-show'",
                                  (candidate_id,)).fetchone()["c"]
    if past_no_shows:
        score += 40
        reasons.append(f"{past_no_shows} previous no-show(s)")
    cand = domain.get_candidate_full(conn, candidate_id)
    if cand and cand["last_contact_days"] > 7:
        score += 20
        reasons.append("long gap since last contact")
    abandoned = conn.execute("""SELECT COUNT(*) c FROM screening_sessions WHERE candidate_id=? AND status!='done'""",
                              (candidate_id,)).fetchone()["c"]
    if abandoned:
        score += 20
        reasons.append("has an incomplete screening session")
    unanswered = conn.execute("""SELECT COUNT(*) c FROM calls WHERE candidate_id=? AND outcome IN ('no_answer','voicemail')""",
                               (candidate_id,)).fetchone()["c"]
    if unanswered >= 2:
        score += 20
        reasons.append(f"{unanswered} unanswered contact attempts")
    score = min(score, 100)
    level = "high" if score >= 60 else "medium" if score >= 30 else "low"
    return {"score": score, "level": level, "reasons": reasons}


def upcoming_interviews_with_risk(conn):
    rows = conn.execute("SELECT * FROM interviews WHERE status='Scheduled'").fetchall()
    out = []
    for r in rows:
        risk = no_show_risk(conn, r["candidate_id"])
        out.append({"interview": dict(r), "risk": risk})
    out.sort(key=lambda x: x["risk"]["score"], reverse=True)
    return out


# ============================================================
# Weekly team report
# ============================================================
def weekly_team_report(conn):
    today = datetime.date.today()
    week_start = (today - datetime.timedelta(days=7)).isoformat()
    prev_week_start = (today - datetime.timedelta(days=14)).isoformat()

    calls_this_week = conn.execute("SELECT COUNT(*) c FROM calls WHERE ts>=?", (week_start,)).fetchone()["c"]
    calls_last_week = conn.execute("SELECT COUNT(*) c FROM calls WHERE ts>=? AND ts<?",
                                    (prev_week_start, week_start)).fetchone()["c"]

    leaderboard = fair_leaderboard(conn)
    top_recruiter = leaderboard[0] if leaderboard else None
    objections = objection_trends(conn, 7)
    top_objection = objections["objections"][0] if objections["objections"] else None
    best_hours = best_call_hours(conn)

    attention = []
    for entry in leaderboard:
        overdue = conn.execute("""SELECT COUNT(*) c FROM candidates WHERE assigned_recruiter=? AND merged_into IS NULL
            AND last_contact_days>3 AND stage NOT IN ('Hired','Rejected')""", (entry["recruiter"],)).fetchone()["c"]
        if entry["calls_this_week"] < 5 or overdue >= 3:
            attention.append({"recruiter": entry["recruiter"], "calls_this_week": entry["calls_this_week"],
                               "overdue_follow_ups": overdue})

    return {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "calls_this_week": calls_this_week, "calls_last_week": calls_last_week,
        "calls_trend": calls_this_week - calls_last_week,
        "top_recruiter": top_recruiter, "top_objection": top_objection,
        "best_call_hours": best_hours, "leaderboard": leaderboard,
        "team_needs_attention": attention,
    }


# ============================================================
# Dynamic daily goal
# ============================================================
def dynamic_daily_goal(conn, recruiter):
    today = datetime.date.today()
    week_start = today - datetime.timedelta(days=today.weekday())
    row = conn.execute("SELECT weekly_call_target FROM recruiter_targets WHERE recruiter=? AND week_start=?",
                        (recruiter, week_start.isoformat())).fetchone()
    target = row["weekly_call_target"] if row else DEFAULT_WEEKLY_TARGET
    made = conn.execute("SELECT COUNT(*) c FROM calls WHERE recruiter=? AND ts>=?",
                         (recruiter, week_start.isoformat())).fetchone()["c"]
    remaining = max(0, target - made)
    week_end = week_start + datetime.timedelta(days=4)  # Mon-Fri working week
    remaining_days = max(1, (week_end - today).days + 1) if today <= week_end else 1
    daily_goal = math.ceil(remaining / remaining_days)
    return {"recruiter": recruiter, "weekly_target": target, "calls_made_this_week": made,
            "remaining": remaining, "remaining_working_days": remaining_days, "daily_goal": daily_goal,
            "target_source": "configured" if row else "default"}


def set_weekly_target(conn, recruiter, weekly_call_target):
    today = datetime.date.today()
    week_start = (today - datetime.timedelta(days=today.weekday())).isoformat()
    conn.execute("""INSERT INTO recruiter_targets (recruiter, week_start, weekly_call_target) VALUES (?,?,?)
        ON CONFLICT(recruiter, week_start) DO UPDATE SET weekly_call_target=excluded.weekly_call_target""",
        (recruiter, week_start, weekly_call_target))
    conn.commit()


# ============================================================
# Team workload (dashboard)
# ============================================================
def team_workload(conn):
    recruiters = active_recruiters(conn)
    today = datetime.date.today()
    week_start = (today - datetime.timedelta(days=today.weekday())).isoformat()
    out = []
    for r in recruiters:
        rows = conn.execute("SELECT * FROM candidates WHERE assigned_recruiter=? AND merged_into IS NULL", (r,)).fetchall()
        by_stage = {}
        for row in rows:
            by_stage[row["stage"]] = by_stage.get(row["stage"], 0) + 1
        overdue = conn.execute("""SELECT COUNT(*) c FROM candidates WHERE assigned_recruiter=? AND merged_into IS NULL
            AND last_contact_days>3 AND stage NOT IN ('Hired','Rejected')""", (r,)).fetchone()["c"]
        calls_week = conn.execute("SELECT COUNT(*) c FROM calls WHERE recruiter=? AND ts>=?", (r, week_start)).fetchone()["c"]
        goal = dynamic_daily_goal(conn, r)
        out.append({"recruiter": r, "total_assigned": len(rows), "by_stage": by_stage, "overdue": overdue,
                     "calls_this_week": calls_week, "daily_goal": goal["daily_goal"]})
    unassigned = conn.execute("SELECT COUNT(*) c FROM candidates WHERE assigned_recruiter IS NULL AND merged_into IS NULL").fetchone()["c"]
    return {"recruiters": out, "unassigned_candidates": unassigned}


# ============================================================
# Escalations
# ============================================================
def raise_escalation(conn, candidate_id, raised_by, reason):
    from app.db import log_candidate_event, log_audit
    now = datetime.datetime.now().isoformat(timespec="seconds")
    cur = conn.execute("INSERT INTO escalations (candidate_id,raised_by,ts,reason,status) VALUES (?,?,?,?,'open')",
                        (candidate_id, raised_by, now, reason))
    log_candidate_event(conn, candidate_id, "escalated", f"{raised_by}: {reason}")
    log_audit(conn, raised_by, "Raised escalation", f"{candidate_id}: {reason}")
    conn.commit()
    return cur.lastrowid


def resolve_escalation(conn, escalation_id, resolved_by, note):
    from app.db import log_audit
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute("""UPDATE escalations SET status='resolved', resolved_by=?, resolved_at=?, resolution_note=?
        WHERE id=?""", (resolved_by, now, note, escalation_id))
    log_audit(conn, resolved_by, "Resolved escalation", f"#{escalation_id}: {note}")
    conn.commit()


def list_escalations(conn, status="open"):
    rows = conn.execute("SELECT * FROM escalations WHERE status=? ORDER BY ts DESC", (status,)).fetchall()
    return [dict(r) for r in rows]
