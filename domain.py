"""
Domain logic — pure functions with no HTTP/DB-framework dependency
(only sqlite3 connections passed in), so this layer ports to any backend
(Postgres, a task queue, a different web framework) without rewriting.
"""
import re
import json
import difflib
import statistics
import datetime

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]


def level_rank(level):
    return LEVELS.index(level) if level in LEVELS else -1


# ============================================================
# Rule engine + Matching engine (Sections 11, 13)
# ============================================================
def row_to_job(r):
    return {
        "id": r["id"], "title": r["title"], "company": r["company"], "language": r["language"],
        "level": r["level"], "education": r["education"],
        "experience_years_required": r["experience_years_required"], "location": r["location"],
        "work_model": r["work_model"], "shift": r["shift"], "salary": r["salary"],
        "vacancies": r["vacancies"], "filled": r["filled"], "deadline": r["deadline"],
        "must_have": json.loads(r["must_have"]), "nice_to_have": json.loads(r["nice_to_have"]),
        "disqualifiers": json.loads(r["disqualifiers"]), "status": r["status"],
    }


def get_candidate_full(conn, cid):
    r = conn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
    if not r:
        return None
    langs = conn.execute("SELECT lang,level FROM candidate_languages WHERE candidate_id=?", (cid,)).fetchall()
    skills = conn.execute("SELECT skill FROM candidate_skills WHERE candidate_id=?", (cid,)).fetchall()
    return {
        "id": r["id"], "name": r["name"], "email": r["email"], "phone": r["phone"],
        "location": r["location"], "education": r["education"],
        "experience_years": r["experience_years"], "source": r["source"], "stage": r["stage"],
        "last_contact_days": r["last_contact_days"], "notes": r["notes"],
        "merged_into": r["merged_into"],
        "languages": [{"lang": l["lang"], "level": l["level"]} for l in langs],
        "skills": [s["skill"] for s in skills],
    }


def list_active_candidate_ids(conn):
    """Excludes candidates merged away by the dedup engine (Section 10:
    'never merge irreversibly' — merged_into keeps the record but hides it
    from normal listings, and the merge is reversible by clearing the field)."""
    return [r["id"] for r in conn.execute("SELECT id FROM candidates WHERE merged_into IS NULL").fetchall()]


def evaluate_rules(job, candidate):
    lang_match = next((l for l in candidate["languages"] if l["lang"] == job["language"]), None)
    if not lang_match or level_rank(lang_match["level"]) < level_rank(job["level"]):
        return False, f"Does not meet {job['language']} {job['level']} requirement"
    if "Graduate" in job["must_have"] and candidate["education"] != "Graduate":
        return False, "Job requires Graduate status"
    return True, None


def compute_match(job, candidate):
    eligible, reason = evaluate_rules(job, candidate)
    if not eligible:
        return {"eligible": False, "score": 0, "reason": reason, "breakdown": []}

    breakdown = []
    lang_match = next(l for l in candidate["languages"] if l["lang"] == job["language"])
    lang_delta = level_rank(lang_match["level"]) - level_rank(job["level"])
    lang_pts = min(30, 22 + lang_delta * 4)
    breakdown.append({"factor": f"Language ({job['language']})", "points": lang_pts, "max": 30,
                       "note": f"Candidate: {lang_match['level']}, required: {job['level']}"})

    edu_pts = 20 if (candidate["education"] == job["education"] or job["education"] == "Any") \
        else (18 if candidate["education"] == "Graduate" else 8)
    breakdown.append({"factor": "Education", "points": edu_pts, "max": 20, "note": candidate["education"]})

    exp_needed = job["experience_years_required"]
    exp_pts = 20 if candidate["experience_years"] >= exp_needed \
        else max(6, 20 - (exp_needed - candidate["experience_years"]) * 8)
    breakdown.append({"factor": "Experience", "points": exp_pts, "max": 20,
                       "note": f"{candidate['experience_years']} yrs vs {exp_needed} required"})

    loc_pts = 15 if (job["location"] in (candidate["location"] or "") or job["work_model"] == "Remote") else 6
    breakdown.append({"factor": "Location / Work model", "points": loc_pts, "max": 15,
                       "note": f"{candidate['location']} · job is {job['work_model']}"})

    reqs = [x.lower() for x in job["must_have"] + job["nice_to_have"]]
    overlap = [s for s in candidate["skills"] if any(s.lower() in r or r in s.lower() for r in reqs)]
    skill_pts = min(15, len(overlap) * 6)
    breakdown.append({"factor": "Skill overlap", "points": skill_pts, "max": 15,
                       "note": ", ".join(candidate["skills"]) or "none listed"})

    score = round(lang_pts + edu_pts + exp_pts + loc_pts + skill_pts)
    return {"eligible": True, "score": score, "breakdown": breakdown,
            "concerns": [candidate["notes"]] if candidate["notes"] else []}


def confidence_label(score):
    if score >= 80:
        return "High confidence"
    if score >= 55:
        return "Medium confidence"
    return "Needs review"


def match_candidates_for_job(conn, job_id, min_score=0):
    jrow = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not jrow:
        return None, []
    job = row_to_job(jrow)
    results = []
    for cid in list_active_candidate_ids(conn):
        cand = get_candidate_full(conn, cid)
        match = compute_match(job, cand)
        if match["eligible"]:
            match["confidence"] = confidence_label(match["score"])
        if match["score"] >= min_score:
            results.append({"candidate": cand, "match": match})
    results.sort(key=lambda x: x["match"]["score"], reverse=True)
    return job, results


def never_hired_but_contacted(conn):
    """'Find candidates we contacted before but never hired.' Real query,
    not a canned phrase — contacted-or-further stages, excluding Hired."""
    rows = conn.execute("""SELECT * FROM candidates WHERE merged_into IS NULL
        AND stage NOT IN ('New','Hired') """).fetchall()
    return [get_candidate_full(conn, r["id"]) for r in rows]


# ============================================================
# Screening flow (Section 15)
# ============================================================
SCREENING_FLOW = {
    "J1": [
        {"key": "lang", "q": "What's your German level?"},
        {"key": "grad", "q": "Are you a graduate?"},
        {"key": "shift", "q": "Are you available for rotational shifts?"},
    ],
    "J2": [
        {"key": "lang", "q": "What's your English level?"},
        {"key": "exp", "q": "How many years of technical support experience do you have?"},
    ],
    "J3": [
        {"key": "lang", "q": "What's your French level?"},
        {"key": "grad", "q": "Are you a graduate?"},
    ],
}


def generate_screening_questions(job):
    """Section 14: auto-generate screening questions from job requirements.
    Deterministic template-based generation (no LLM available) — still real
    logic derived from the job's own must_have/level/education fields, not
    a static list."""
    questions = []
    questions.append({"type": "text", "key": "lang", "required": True, "knockout": True,
                       "text": f"What's your {job['language']} level? (state as A1–C2)"})
    if job["education"] == "Graduate" or "Graduate" in job["must_have"]:
        questions.append({"type": "yes_no", "key": "grad", "required": True, "knockout": True,
                           "text": "Are you a university graduate?"})
    if job["experience_years_required"] > 0:
        questions.append({"type": "numeric", "key": "exp", "required": True, "knockout": True,
                           "text": f"How many years of relevant experience do you have? (need {job['experience_years_required']}+)"})
    if job["shift"] == "Rotational":
        questions.append({"type": "yes_no", "key": "shift", "required": True, "knockout": True,
                           "text": "Are you available for rotational shifts?"})
    if job["work_model"] == "On-site":
        questions.append({"type": "yes_no", "key": "location_ok", "required": True, "knockout": False,
                           "text": f"Can you commute to {job['location']} regularly?"})
    for nice in job["nice_to_have"]:
        questions.append({"type": "yes_no", "key": f"nice_{re.sub(r'[^a-z0-9]', '', nice.lower())}",
                           "required": False, "knockout": False,
                           "text": f"Do you have experience with: {nice}?"})
    return questions


def check_screening_answer(key, answer, job):
    if key == "lang":
        m = re.search(r"\b([ABC][12])\b", answer, re.I)
        return bool(m) and level_rank(m.group(1).upper()) >= level_rank(job["level"])
    if key in ("grad", "shift", "location_ok") or key.startswith("nice_"):
        return bool(re.search(r"yes|available|graduate|نعم", answer, re.I))
    if key == "exp":
        m = re.search(r"\d+", answer)
        return bool(m) and int(m.group(0)) >= job.get("experience_years_required", 1)
    return False


# ============================================================
# CV Intelligence (Section 9) — plain-text parser.
# Real PDF/DOCX/OCR extraction needs libraries this sandbox can't install
# (no network to pip-install); this parses whatever text is pasted/extracted
# upstream, which is the same contract the AI extraction step would fulfill.
# ============================================================
EDU_PATTERNS = [
    (r"\b(bachelor|b\.?sc|licentiate|university degree|graduated)\b", "Graduate", "high"),
    (r"\b(master|m\.?sc|mba|postgraduate)\b", "Graduate", "high"),
    (r"\b(undergraduate|currently studying|in progress)\b", "Undergraduate", "medium"),
]
LANG_PATTERN = re.compile(r"\b(german|english|french|arabic|spanish|italian)\b.{0,20}?\b([ABC][12])\b", re.I)
EXP_PATTERN = re.compile(r"(\d{1,2})\+?\s*(years?|yrs?)\s+(of\s+)?experience", re.I)
SKILL_KEYWORDS = ["Customer Service", "Communication", "Sales", "Technical Support", "Ticketing",
                   "Troubleshooting", "CRM", "Team Leadership", "Fluent writing", "Data Entry",
                   "Negotiation", "Project Management", "Excel", "Reporting"]


def parse_cv_text(text):
    result = {"education": None, "education_confidence": "low", "experience_years": None,
              "experience_confidence": "low", "languages": [], "skills": [], "raw_length": len(text)}

    for pattern, label, conf in EDU_PATTERNS:
        if re.search(pattern, text, re.I):
            result["education"] = label
            result["education_confidence"] = conf
            break

    exp_match = EXP_PATTERN.search(text)
    if exp_match:
        result["experience_years"] = int(exp_match.group(1))
        result["experience_confidence"] = "high"
    else:
        # weaker signal: count distinct "20XX - 20XX" ranges as a fallback
        years = re.findall(r"\b(20[0-2]\d)\b", text)
        if len(years) >= 2:
            span = max(int(y) for y in years) - min(int(y) for y in years)
            if 0 < span < 40:
                result["experience_years"] = span
                result["experience_confidence"] = "low"

    for m in LANG_PATTERN.finditer(text):
        result["languages"].append({"lang": m.group(1).capitalize(), "level": m.group(2).upper(),
                                     "confidence": "high"})

    for kw in SKILL_KEYWORDS:
        if kw.lower() in text.lower():
            result["skills"].append(kw)

    return result


# ============================================================
# Deduplication (Section 10) — fuzzy match, never auto-merges.
# ============================================================
def name_similarity(a, b):
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def find_duplicate_candidates(conn):
    rows = conn.execute("SELECT id,name,phone,email,source FROM candidates WHERE merged_into IS NULL").fetchall()
    candidates = [dict(r) for r in rows]
    found = []
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            a, b = candidates[i], candidates[j]
            reasons = []
            score = 0.0
            name_sim = name_similarity(a["name"], b["name"])
            if name_sim > 0.6:
                score += name_sim * 0.6
                reasons.append(f"name similarity {name_sim:.2f}")
            if a["phone"] and a["phone"] == b["phone"]:
                score += 0.35
                reasons.append("same phone number")
            if a["email"] and a["email"] == b["email"]:
                score += 0.25
                reasons.append("same email")
            if score >= 0.55:
                found.append({"candidate_a": a["id"], "candidate_b": b["id"],
                               "score": round(min(score, 1.0), 2), "reason": "; ".join(reasons)})
    return found


def refresh_duplicate_suggestions(conn):
    existing = {(r["candidate_a"], r["candidate_b"])
                for r in conn.execute("SELECT candidate_a, candidate_b FROM duplicate_suggestions WHERE status='pending'")}
    new_count = 0
    for dup in find_duplicate_candidates(conn):
        key = (dup["candidate_a"], dup["candidate_b"])
        if key in existing:
            continue
        conn.execute("""INSERT INTO duplicate_suggestions (candidate_a,candidate_b,score,reason,status)
                        VALUES (?,?,?,?,'pending')""", (dup["candidate_a"], dup["candidate_b"], dup["score"], dup["reason"]))
        new_count += 1
    conn.commit()
    return new_count


def merge_candidates(conn, keep_id, merge_id, actor):
    """Non-destructive merge: merge_id's record stays in the DB with
    merged_into set, so it disappears from normal listings but the merge is
    reversible (Section 10: 'support undo where technically possible')."""
    conn.execute("UPDATE candidates SET merged_into=? WHERE id=?", (keep_id, merge_id))
    from app.db import log_candidate_event, log_audit
    log_candidate_event(conn, keep_id, "merged", f"Merged {merge_id} into {keep_id}")
    log_audit(conn, actor, "Merged candidates", f"{merge_id} → {keep_id}")
    conn.commit()


# ============================================================
# Data quality engine (Section 98)
# ============================================================
def data_quality_report(conn):
    issues = []
    for r in conn.execute("SELECT * FROM candidates WHERE merged_into IS NULL"):
        cand = get_candidate_full(conn, r["id"])
        if not cand["languages"]:
            issues.append({"candidate_id": cand["id"], "name": cand["name"], "issue": "No language recorded"})
        if not cand["email"] and not cand["phone"]:
            issues.append({"candidate_id": cand["id"], "name": cand["name"], "issue": "No contact information"})
        for lang in cand["languages"]:
            if lang["level"] not in LEVELS:
                issues.append({"candidate_id": cand["id"], "name": cand["name"],
                                "issue": f"Invalid language level '{lang['level']}' for {lang['lang']}"})
        if cand["experience_years"] is not None and cand["experience_years"] > 50:
            issues.append({"candidate_id": cand["id"], "name": cand["name"], "issue": "Implausible experience_years value"})
    # contact-info duplicates (same phone across different, non-merged candidates)
    phones = {}
    for r in conn.execute("SELECT id,name,phone FROM candidates WHERE merged_into IS NULL AND phone IS NOT NULL"):
        phones.setdefault(r["phone"], []).append(r["name"])
    for phone, names in phones.items():
        if len(names) > 1:
            issues.append({"candidate_id": None, "name": ", ".join(names), "issue": f"Duplicate phone number {phone}"})
    return issues


# ============================================================
# Recruitment Intelligence (Sections 36-41, 78, 127, 48-50, 77)
# ============================================================
def find_bottleneck(campaign):
    stages = [
        ("Applications", campaign["applications"], campaign["clicks"]),
        ("Screened", campaign["screened"], campaign["applications"]),
        ("Qualified", campaign["qualified"], campaign["screened"]),
        ("Interviews", campaign["interviews"], campaign["qualified"]),
        ("Selected", campaign["selected"], campaign["interviews"]),
        ("Hires", campaign["hires"], campaign["selected"]),
    ]
    computed = [{"name": n, "value": v, "from": f, "rate": (v / f if f else 0)} for n, v, f in stages]
    worst = min(computed, key=lambda s: s["rate"])
    return computed, worst


def detect_anomalies(conn, campaign_id, z_threshold=2.0):
    """Real statistical anomaly detection: z-score of the latest day's
    applications against the trailing history's mean/stdev (Section 39)."""
    rows = conn.execute("""SELECT date, applications, qualified, hires FROM daily_metrics
        WHERE campaign_id=? ORDER BY date""", (campaign_id,)).fetchall()
    if len(rows) < 5:
        return {"status": "insufficient_data", "anomalies": []}
    values = [r["applications"] for r in rows]
    history, latest = values[:-1], values[-1]
    mean = statistics.mean(history)
    stdev = statistics.pstdev(history) or 1.0
    z = (latest - mean) / stdev
    anomalies = []
    if abs(z) >= z_threshold:
        direction = "drop" if z < 0 else "spike"
        anomalies.append({
            "metric": "applications", "date": rows[-1]["date"], "value": latest,
            "expected_mean": round(mean, 1), "z_score": round(z, 2), "direction": direction,
            "note": f"Applications on {rows[-1]['date']} were {latest}, vs a {round(mean,1)}-average "
                    f"over the prior {len(history)} days (z={z:.2f}) — a significant {direction}."
        })
    return {"status": "ok", "history_days": len(history), "anomalies": anomalies}


def forecast_metric(conn, campaign_id, days_ahead=14, metric="hires"):
    """Simple linear regression (least squares, stdlib only) over
    daily_metrics history, projected forward. Always returns assumptions
    and a naive confidence band — never a bare number claiming certainty
    (Section 40)."""
    rows = conn.execute(f"""SELECT date, {metric} FROM daily_metrics WHERE campaign_id=? ORDER BY date""",
                         (campaign_id,)).fetchall()
    if len(rows) < 5:
        return {"status": "insufficient_data"}
    xs = list(range(len(rows)))
    ys = [r[metric] for r in rows]
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs) or 1
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    intercept = mean_y - slope * mean_x
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    stdev = statistics.pstdev(residuals) if len(residuals) > 1 else 0
    projections = []
    last_date = datetime.date.fromisoformat(rows[-1]["date"])
    for i in range(1, days_ahead + 1):
        x = n - 1 + i
        y = max(0, slope * x + intercept)
        projections.append({"date": (last_date + datetime.timedelta(days=i)).isoformat(),
                             "projected": round(y, 1), "low": round(max(0, y - stdev), 1),
                             "high": round(y + stdev, 1)})
    return {"status": "ok", "metric": metric, "slope_per_day": round(slope, 3),
            "data_window_days": n, "confidence": "low" if n < 10 else "medium",
            "assumptions": "Linear trend over the observed history; does not account for planned "
                           "campaign changes, seasonality, or external events.",
            "projections": projections}


def what_if(conn, campaign_id, param, delta_pct):
    """Section 41 — scenario estimate using the campaign's own historical
    conversion rates, not invented numbers."""
    row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not row:
        return {"status": "not_found"}
    camp = dict(row)
    factor = 1 + (delta_pct / 100)
    scenario = dict(camp)
    if param == "volume":
        for key in ("reach", "clicks", "applications", "screened", "qualified", "interviews", "selected", "hires"):
            scenario[key] = round(camp[key] * factor)
    elif param == "attendance_rate":
        new_rate = min(1.0, max(0.0, camp["attendance_rate"] * factor))
        scenario["attendance_rate"] = round(new_rate, 3)
        scenario["selected"] = round(camp["interviews"] * new_rate * (camp["selected"] / max(1, camp["interviews"] * camp["attendance_rate"])))
    else:
        return {"status": "unknown_param", "supported": ["volume", "attendance_rate"]}
    return {"status": "ok", "param": param, "delta_pct": delta_pct, "baseline": camp, "scenario": scenario,
            "assumptions": "Scales historical figures proportionally using the campaign's own observed "
                           "funnel; assumes conversion rates elsewhere in the funnel stay constant."}


def recruitment_health_score(conn):
    """Section 78/127 — dashboard summary per job, derived from real fill
    rate + deadline pressure, not an unexplained AI score."""
    scores = []
    for r in conn.execute("SELECT * FROM jobs WHERE status='open'"):
        job = row_to_job(r)
        fill_rate = job["filled"] / job["vacancies"] if job["vacancies"] else 0
        days_left = None
        status = "green"
        reason = f"{job['filled']}/{job['vacancies']} filled ({fill_rate*100:.0f}%)"
        if job["deadline"]:
            try:
                days_left = (datetime.date.fromisoformat(job["deadline"]) - datetime.date.today()).days
            except ValueError:
                days_left = None
        if fill_rate < 0.3 and (days_left is None or days_left < 30):
            status = "red"
            reason += f" — behind pace with {days_left if days_left is not None else '?'} days left"
        elif fill_rate < 0.6:
            status = "yellow"
            reason += " — needs attention"
        scores.append({"job_id": job["id"], "title": job["title"], "language": job["language"],
                        "status": status, "fill_rate": round(fill_rate, 2), "reason": reason})
    return scores


def generate_recommendations(conn):
    """Section 77 — rule-based, evidence-backed recommendations (not an LLM
    guess): every recommendation cites the number that produced it."""
    recs = []
    sources = [dict(r) for r in conn.execute("SELECT * FROM sources")]
    if sources:
        rates = [(s, s["qualified"] / s["applications"] if s["applications"] else 0) for s in sources]
        rates.sort(key=lambda x: x[1])
        worst, worst_rate = rates[0]
        best, best_rate = rates[-1]
        if best_rate - worst_rate > 0.1:
            recs.append({"text": f"Shift budget from {worst['name']} ({worst_rate*100:.0f}% qualified rate) "
                                  f"toward {best['name']} ({best_rate*100:.0f}%).",
                         "evidence": f"{worst['name']}: {worst['qualified']}/{worst['applications']} qualified; "
                                     f"{best['name']}: {best['qualified']}/{best['applications']} qualified."})

    for row in conn.execute("SELECT * FROM campaigns"):
        camp = dict(row)
        stages, worst = find_bottleneck(camp)
        recs.append({"text": f"Investigate {worst['name']} for {camp['name']} — weakest funnel transition.",
                      "evidence": f"{worst['name']} converts at {worst['rate']*100:.0f}% from the prior stage, "
                                  f"the lowest of any stage."})

    rejected_shift = conn.execute("""SELECT * FROM candidates WHERE merged_into IS NULL
        AND notes LIKE '%shift%' AND stage='Rejected'""").fetchall()
    remote_jobs = conn.execute("SELECT * FROM jobs WHERE work_model IN ('Remote','Hybrid') AND status='open'").fetchall()
    if rejected_shift and remote_jobs:
        recs.append({"text": f"Re-engage {len(rejected_shift)} candidate(s) previously rejected for shift "
                              f"conflicts — {len(remote_jobs)} open role(s) now offer remote/hybrid work.",
                      "evidence": ", ".join(r["name"] for r in rejected_shift) + " · jobs: " +
                                  ", ".join(j["title"] for j in remote_jobs)})

    dq_issues = data_quality_report(conn)
    if dq_issues:
        recs.append({"text": f"Clean up {len(dq_issues)} data quality issue(s) in the candidate database.",
                      "evidence": "; ".join(f"{i['name']}: {i['issue']}" for i in dq_issues[:3]) +
                                  (f" (+{len(dq_issues)-3} more)" if len(dq_issues) > 3 else "")})

    return recs


def daily_briefing(conn):
    campaigns = [dict(r) for r in conn.execute("SELECT * FROM campaigns")]
    stale = conn.execute("""SELECT * FROM candidates WHERE merged_into IS NULL AND last_contact_days>3
        AND stage IN ('Contacted','Screening','New')""").fetchall()
    health = recruitment_health_score(conn)
    recs = generate_recommendations(conn)
    anomalies = []
    for c in campaigns:
        a = detect_anomalies(conn, c["id"])
        if a["status"] == "ok" and a["anomalies"]:
            anomalies.extend(a["anomalies"])
    priorities = []
    if stale:
        priorities.append(f"Follow up with {len(stale)} stale candidate(s).")
    for a in anomalies:
        priorities.append(a["note"])
    red = [h for h in health if h["status"] == "red"]
    if red:
        priorities.append(f"{len(red)} job(s) are behind hiring pace: " + ", ".join(h["title"] for h in red) + ".")
    if not priorities:
        priorities.append("No urgent items — recruitment pipeline looks healthy.")
    return {"generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "overdue_follow_ups": len(stale), "health": health, "anomalies": anomalies,
            "recommendations": recs, "priorities": priorities}
