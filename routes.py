import http.server
import json
import re
import secrets
import datetime
import urllib.parse

from app import domain, workflow, agent, integrations, callcenter
from app.db import get_db, log_audit, log_candidate_event, hash_password, DEFAULT_ORG
from app.security import require_auth, SESSIONS, check_rate_limit, has_role


def _candidate_full_list(conn):
    return [domain.get_candidate_full(conn, cid) for cid in domain.list_active_candidate_ids(conn)]


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TalentLoop/0.2"

    def log_message(self, fmt, *args):
        pass

    # ---------- helpers ----------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _serve_static(self, path):
        import os
        static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
        if path == "/":
            path = "/index.html"
        fpath = os.path.join(static_dir, path.lstrip("/"))
        if not os.path.abspath(fpath).startswith(os.path.abspath(static_dir)):
            self.send_error(403); return
        if not os.path.isfile(fpath):
            self.send_error(404); return
        text_types = {".html": "text/html", ".js": "application/javascript", ".css": "text/css",
                      ".json": "application/json"}
        binary_types = {".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml"}
        ext = os.path.splitext(fpath)[1]
        if ext in text_types:
            ctype = text_types[ext] + "; charset=utf-8"
        elif ext in binary_types:
            ctype = binary_types[ext]
        else:
            ctype = "application/octet-stream"
        with open(fpath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _rate_limit_key(self):
        auth = self.headers.get("Authorization", "")
        return auth if auth else self.client_address[0]

    # ---------------- GET ----------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path.startswith("/api/landing/"):
            return self._handle_landing_page(path)

        if not path.startswith("/api/"):
            return self._serve_static(path)

        if not check_rate_limit(self._rate_limit_key()):
            return self._send_json({"error": "rate limit exceeded — try again shortly"}, 429)

        conn = get_db()
        try:
            if path == "/api/health":
                return self._send_json({"status": "ok", "time": datetime.datetime.now().isoformat()})

            if path == "/api/me":
                session = require_auth(self.headers)
                if not session: return self._send_json({"error": "unauthorized"}, 401)
                return self._send_json(session)

            if path == "/api/jobs":
                rows = conn.execute("SELECT * FROM jobs").fetchall()
                return self._send_json([domain.row_to_job(r) for r in rows])

            if path == "/api/candidates":
                return self._send_json(_candidate_full_list(conn))

            m = re.match(r"^/api/candidates/([\w-]+)/timeline$", path)
            if m:
                rows = conn.execute("SELECT * FROM candidate_events WHERE candidate_id=? ORDER BY id",
                                     (m.group(1),)).fetchall()
                return self._send_json([dict(r) for r in rows])

            m = re.match(r"^/api/match/([\w-]+)$", path)
            if m:
                job, results = domain.match_candidates_for_job(conn, m.group(1))
                if job is None: return self._send_json({"error": "job not found"}, 404)
                return self._send_json({"job": job, "results": results})

            if path == "/api/interviews":
                return self._send_json([dict(r) for r in conn.execute("SELECT * FROM interviews")])

            if path == "/api/offers":
                return self._send_json([dict(r) for r in conn.execute("SELECT * FROM offers")])

            if path == "/api/sources":
                return self._send_json([dict(r) for r in conn.execute("SELECT * FROM sources")])

            if path == "/api/campaigns":
                out = []
                for r in conn.execute("SELECT * FROM campaigns"):
                    d = dict(r)
                    stages, worst = domain.find_bottleneck(d)
                    d["funnel"] = stages; d["bottleneck"] = worst
                    out.append(d)
                return self._send_json(out)

            m = re.match(r"^/api/campaigns/([\w-]+)/variants$", path)
            if m:
                rows = conn.execute("SELECT * FROM campaign_variants WHERE campaign_id=?", (m.group(1),)).fetchall()
                variants = []
                for r in rows:
                    d = dict(r)
                    d["qualified_rate"] = round(d["qualified"] / d["applications"], 3) if d["applications"] else 0
                    d["hire_rate"] = round(d["hires"] / d["applications"], 3) if d["applications"] else 0
                    variants.append(d)
                variants.sort(key=lambda v: v["qualified_rate"], reverse=True)
                return self._send_json({"variants": variants,
                                         "leading_variant": variants[0]["name"] if variants else None})

            if path == "/api/pools":
                rows = conn.execute("""SELECT cl.lang, c.id, c.name, cl.level FROM candidate_languages cl
                    JOIN candidates c ON c.id=cl.candidate_id WHERE c.merged_into IS NULL ORDER BY cl.lang""").fetchall()
                pools = {}
                for r in rows:
                    pools.setdefault(r["lang"], []).append({"id": r["id"], "name": r["name"], "level": r["level"]})
                return self._send_json(pools)

            if path == "/api/approvals":
                return self._send_json([dict(r) for r in conn.execute("SELECT * FROM approvals ORDER BY id DESC")])

            if path == "/api/audit":
                return self._send_json([dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 150")])

            if path == "/api/agent/tool-calls":
                return self._send_json([dict(r) for r in conn.execute("SELECT * FROM agent_tool_calls ORDER BY id DESC LIMIT 50")])

            if path == "/api/duplicates":
                rows = conn.execute("SELECT * FROM duplicate_suggestions WHERE status='pending' ORDER BY score DESC").fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    d["candidate_a_name"] = (domain.get_candidate_full(conn, d["candidate_a"]) or {}).get("name")
                    d["candidate_b_name"] = (domain.get_candidate_full(conn, d["candidate_b"]) or {}).get("name")
                    out.append(d)
                return self._send_json(out)

            if path == "/api/data-quality":
                return self._send_json(domain.data_quality_report(conn))

            if path == "/api/workflows":
                rows = conn.execute("SELECT * FROM workflows ORDER BY id DESC").fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    d["conditions"] = json.loads(d["conditions"]); d["actions"] = json.loads(d["actions"])
                    out.append(d)
                return self._send_json(out)

            if path == "/api/workflows/executions":
                return self._send_json([dict(r) for r in conn.execute(
                    "SELECT * FROM workflow_executions ORDER BY id DESC LIMIT 50")])

            if path == "/api/intelligence/anomalies":
                camp_id = qs.get("campaign_id", [None])[0]
                camps = [camp_id] if camp_id else [r["id"] for r in conn.execute("SELECT id FROM campaigns")]
                out = {c: domain.detect_anomalies(conn, c) for c in camps}
                return self._send_json(out)

            m = re.match(r"^/api/intelligence/forecast/([\w-]+)$", path)
            if m:
                days = int(qs.get("days", [14])[0]); metric = qs.get("metric", ["hires"])[0]
                return self._send_json(domain.forecast_metric(conn, m.group(1), days, metric))

            if path == "/api/intelligence/health-score":
                return self._send_json(domain.recruitment_health_score(conn))

            if path == "/api/intelligence/recommendations":
                return self._send_json(domain.generate_recommendations(conn))

            if path == "/api/intelligence/daily-briefing":
                return self._send_json(domain.daily_briefing(conn))

            if path == "/api/killswitch":
                row = conn.execute("SELECT * FROM kill_switch WHERE id=1").fetchone()
                return self._send_json(dict(row) if row else {"active": False})

            if path == "/api/tools":
                return self._send_json({name: {"description": t["description"], "schema": {k: str(v) for k, v in t["schema"].items()}}
                                         for name, t in agent.TOOLS.items()})

            if path == "/api/integrations":
                return self._send_json(integrations.integration_status())

            if path == "/api/outbox":
                return self._send_json([dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 50")])

            m = re.match(r"^/api/calls/([\w-]+)$", path)
            if m:
                return self._send_json(callcenter.call_history(conn, m.group(1)))

            if path == "/api/queue":
                recruiter = qs.get("recruiter", [None])[0]
                return self._send_json(callcenter.smart_queue(conn, recruiter=recruiter))

            if path == "/api/objections":
                window = int(qs.get("window_days", [7])[0])
                return self._send_json(callcenter.objection_trends(conn, window))

            if path == "/api/leaderboard":
                return self._send_json(callcenter.fair_leaderboard(conn))

            if path == "/api/recycle-alerts":
                return self._send_json(callcenter.recycle_alerts(conn))

            if path == "/api/no-show-risk":
                return self._send_json(callcenter.upcoming_interviews_with_risk(conn))

            if path == "/api/team/workload":
                return self._send_json(callcenter.team_workload(conn))

            if path == "/api/team/weekly-report":
                return self._send_json(callcenter.weekly_team_report(conn))

            if path == "/api/team/daily-goal":
                session = require_auth(self.headers)
                if not session: return self._send_json({"error": "unauthorized"}, 401)
                recruiter = qs.get("recruiter", [session["username"]])[0]
                return self._send_json(callcenter.dynamic_daily_goal(conn, recruiter))

            if path == "/api/escalations":
                status = qs.get("status", ["open"])[0]
                return self._send_json(callcenter.list_escalations(conn, status))

            if path == "/api/team/members":
                return self._send_json(callcenter.active_recruiters(conn))

            if path == "/api/admin/users":
                session = require_auth(self.headers)
                if not session: return self._send_json({"error": "unauthorized"}, 401)
                if not has_role(session, "admin"):
                    return self._send_json({"error": "forbidden — admin role required"}, 403)
                rows = conn.execute("SELECT id, username, role, active FROM users ORDER BY role, username").fetchall()
                return self._send_json([dict(r) for r in rows])

            return self._send_json({"error": "not found"}, 404)
        finally:
            conn.close()

    def _handle_landing_page(self, path):
        m = re.match(r"^/api/landing/([\w-]+)$", path)
        if not m:
            return self._send_html("<h1>Not found</h1>", 404)
        conn = get_db()
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (m.group(1),)).fetchone()
        conn.close()
        if not row:
            return self._send_html("<h1>Job not found</h1>", 404)
        job = domain.row_to_job(row)
        must = "".join(f"<li>{x}</li>" for x in job["must_have"])
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{job['title']}</title>
        <style>body{{font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 20px;}}
        label{{display:block;margin-top:12px;font-size:13px;color:#555;}}
        input,select{{width:100%;padding:8px;margin-top:4px;box-sizing:border-box;}}
        button{{margin-top:16px;padding:10px 18px;background:#22213B;color:#fff;border:none;border-radius:4px;}}</style>
        </head><body>
        <h1>{job['title']}</h1>
        <p>{job['company']} · {job['location']} · {job['work_model']} · {job['salary'] or ''}</p>
        <h3>Requirements</h3><ul>{must}</ul>
        <form id="f">
          <label>Full name</label><input name="name" required>
          <label>Email</label><input name="email" type="email">
          <label>Phone</label><input name="phone" required>
          <label>{job['language']} level</label>
          <select name="language_level"><option>A1</option><option>A2</option><option>B1</option>
            <option selected>B2</option><option>C1</option><option>C2</option></select>
          <label>Years of experience</label><input name="experience_years" type="number" value="0">
          <button type="submit">Apply</button>
        </form>
        <p id="result"></p>
        <script>
        document.getElementById('f').onsubmit = async (e) => {{
          e.preventDefault();
          const data = Object.fromEntries(new FormData(e.target));
          const params = new URLSearchParams(window.location.search);
          data.utm_source = params.get('utm_source') || 'direct';
          const res = await fetch('/api/apply/{job['id']}', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(data)}});
          const json = await res.json();
          document.getElementById('result').textContent = json.ok ? 'Application received — thank you!' : (json.error || 'Something went wrong.');
        }};
        </script>
        </body></html>"""
        return self._send_html(html)

    # ---------------- POST ----------------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._read_json()

        if path.startswith("/api/apply/"):
            return self._handle_application(path, body)
        if path == "/api/jobs/extract":
            return self._handle_extract(body)
        if path == "/api/auth/login":
            return self._handle_login(body)

        if not check_rate_limit(self._rate_limit_key()):
            return self._send_json({"error": "rate limit exceeded — try again shortly"}, 429)

        session = require_auth(self.headers)
        if not session:
            return self._send_json({"error": "unauthorized"}, 401)

        conn = get_db()
        try:
            handlers = [
                (r"^/api/candidates$", self._h_create_candidate),
                (r"^/api/candidates/([\w-]+)/cv-parse$", self._h_cv_parse),
                (r"^/api/candidates/([\w-]+)/stage$", self._h_set_stage),
                (r"^/api/duplicates/refresh$", self._h_refresh_duplicates),
                (r"^/api/duplicates/(\d+)/(merge|reject)$", self._h_duplicate_action),
                (r"^/api/screening/start$", self._h_screening_start),
                (r"^/api/screening/([\w]+)/answer$", self._h_screening_answer),
                (r"^/api/approvals/(\d+)/(approve|reject)$", self._h_approval_action),
                (r"^/api/agent/command$", self._h_agent_command),
                (r"^/api/copilot$", self._h_agent_command),
                (r"^/api/workflows$", self._h_create_workflow),
                (r"^/api/workflows/nl$", self._h_workflow_nl),
                (r"^/api/workflows/([\w]+)/(activate|pause)$", self._h_workflow_toggle),
                (r"^/api/killswitch/(activate|deactivate)$", self._h_killswitch),
                (r"^/api/campaigns/([\w-]+)/variants$", self._h_create_variant),
                (r"^/api/candidates/([\w-]+)/assign$", self._h_assign_candidate),
                (r"^/api/candidates/bulk-import$", self._h_bulk_import),
                (r"^/api/calls$", self._h_log_call),
                (r"^/api/escalations$", self._h_raise_escalation),
                (r"^/api/escalations/(\d+)/resolve$", self._h_resolve_escalation),
                (r"^/api/team/targets$", self._h_set_target),
                (r"^/api/jobs$", self._h_create_job),
                (r"^/api/offers$", self._h_create_offer),
                (r"^/api/sources$", self._h_create_source),
                (r"^/api/admin/users$", self._h_create_user),
            ]
            for pattern, fn in handlers:
                m = re.match(pattern, path)
                if m:
                    return fn(conn, session, body, *m.groups())
            return self._send_json({"error": "not found"}, 404)
        finally:
            conn.close()

    # ---------------- PATCH (updates) ----------------
    def do_PATCH(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._read_json()

        if not check_rate_limit(self._rate_limit_key()):
            return self._send_json({"error": "rate limit exceeded — try again shortly"}, 429)

        session = require_auth(self.headers)
        if not session:
            return self._send_json({"error": "unauthorized"}, 401)

        conn = get_db()
        try:
            handlers = [
                (r"^/api/jobs/([\w-]+)$", self._h_update_job),
                (r"^/api/offers/([\w-]+)$", self._h_update_offer),
                (r"^/api/sources/(\d+)$", self._h_update_source),
                (r"^/api/admin/users/([\w.@-]+)$", self._h_update_user),
            ]
            for pattern, fn in handlers:
                m = re.match(pattern, path)
                if m:
                    return fn(conn, session, body, *m.groups())
            return self._send_json({"error": "not found"}, 404)
        finally:
            conn.close()

    # ---------- individual handlers ----------
    def _handle_login(self, body):
        conn = get_db()
        try:
            username, password = body.get("username", ""), body.get("password", "")
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                return self._send_json({"error": "invalid credentials"}, 401)
            if not row["active"]:
                return self._send_json({"error": "this account has been deactivated"}, 403)
            digest, _ = hash_password(password, row["salt"])
            if digest != row["password_hash"]:
                return self._send_json({"error": "invalid credentials"}, 401)
            token = secrets.token_hex(24)
            SESSIONS[token] = {"username": username, "role": row["role"], "organization_id": row["organization_id"]}
            log_audit(conn, username, "Logged in", "", organization_id=row["organization_id"])
            return self._send_json({"token": token, "username": username, "role": row["role"]})
        finally:
            conn.close()

    def _handle_extract(self, body):
        raw = body.get("text", "")
        lang = re.search(r"german|english|french|arabic|spanish", raw, re.I)
        level = re.search(r"\b([ABC][12])\b", raw, re.I)
        grad = bool(re.search(r"graduate", raw, re.I)) and not re.search(r"undergraduate", raw, re.I)
        work_model = ("Remote" if re.search(r"remote", raw, re.I) else
                      "Hybrid" if re.search(r"hybrid", raw, re.I) else
                      "On-site" if re.search(r"on-?site", raw, re.I) else None)
        salary = re.search(r"[\d,]{4,}\s?(egp|usd|\$)", raw, re.I)
        return self._send_json({
            "simulated": True, "note": "No live model provider connected — rule-based approximation only.",
            "language": lang.group(0) if lang else None, "level": level.group(0) if level else None,
            "graduate": grad, "work_model": work_model, "salary": salary.group(0) if salary else None,
        })

    def _handle_application(self, path, body):
        m = re.match(r"^/api/apply/([\w-]+)$", path)
        if not m: return self._send_json({"error": "bad request"}, 400)
        job_id = m.group(1)
        conn = get_db()
        try:
            jrow = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not jrow: return self._send_json({"ok": False, "error": "job not found"}, 404)
            name, email, phone = body.get("name"), body.get("email"), body.get("phone")
            if not name or not phone:
                return self._send_json({"ok": False, "error": "name and phone are required"}, 400)
            cid = "C" + secrets.token_hex(4).upper()
            now = datetime.datetime.now().isoformat(timespec="seconds")
            conn.execute("""INSERT INTO candidates (id,organization_id,name,email,phone,location,education,
                experience_years,source,stage,last_contact_days,notes,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, DEFAULT_ORG, name, email, phone, body.get("location", ""), body.get("education", ""),
                 int(body.get("experience_years") or 0), "Landing page", "New", 0, "", now))
            job = domain.row_to_job(jrow)
            level = body.get("language_level")
            if level:
                conn.execute("INSERT INTO candidate_languages (candidate_id,lang,level) VALUES (?,?,?)",
                             (cid, job["language"], level))
            conn.execute("""INSERT INTO applications (job_id,candidate_id,source,utm,submitted_at,form_data)
                VALUES (?,?,?,?,?,?)""", (job_id, cid, "landing_page", body.get("utm_source", "direct"), now, json.dumps(body)))
            log_candidate_event(conn, cid, "created", f"Applied via landing page for {job['title']} (utm={body.get('utm_source','direct')})")
            conn.commit()
            workflow.evaluate_workflows(conn, DEFAULT_ORG, "application_received",
                                         {"candidate_id": cid, "job_hint": job["title"]}, actor="landing_page")
            return self._send_json({"ok": True, "candidate_id": cid})
        finally:
            conn.close()

    def _h_create_candidate(self, conn, session, body, *_):
        if not has_role(session, "recruiter"):
            return self._send_json({"error": "forbidden — viewers are read-only"}, 403)
        cid = "C" + secrets.token_hex(4).upper()
        now = datetime.datetime.now().isoformat(timespec="seconds")
        conn.execute("""INSERT INTO candidates (id,organization_id,name,email,phone,location,education,
            experience_years,source,stage,last_contact_days,notes,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, session["organization_id"], body.get("name", "Unnamed"), body.get("email"), body.get("phone"),
             body.get("location", ""), body.get("education", ""), int(body.get("experience_years") or 0),
             body.get("source", "Manual"), "New", 0, body.get("notes", ""), now))
        for lang in body.get("languages", []):
            conn.execute("INSERT INTO candidate_languages (candidate_id,lang,level) VALUES (?,?,?)",
                         (cid, lang.get("lang"), lang.get("level")))
        for skill in body.get("skills", []):
            conn.execute("INSERT INTO candidate_skills (candidate_id,skill) VALUES (?,?)", (cid, skill))
        log_candidate_event(conn, cid, "created", f"Created by {session['username']}")
        conn.commit()
        new_dupes = domain.refresh_duplicate_suggestions(conn)
        return self._send_json({"id": cid, "duplicate_suggestions_found": new_dupes})

    def _h_cv_parse(self, conn, session, body, candidate_id):
        if not has_role(session, "recruiter"):
            return self._send_json({"error": "forbidden — viewers are read-only"}, 403)
        parsed = domain.parse_cv_text(body.get("text", ""))
        if body.get("apply") and candidate_id:
            cand = domain.get_candidate_full(conn, candidate_id)
            if cand:
                if parsed["education"]:
                    conn.execute("UPDATE candidates SET education=? WHERE id=?", (parsed["education"], candidate_id))
                for lang in parsed["languages"]:
                    exists = conn.execute("SELECT 1 FROM candidate_languages WHERE candidate_id=? AND lang=?",
                                           (candidate_id, lang["lang"])).fetchone()
                    if not exists:
                        conn.execute("INSERT INTO candidate_languages (candidate_id,lang,level) VALUES (?,?,?)",
                                     (candidate_id, lang["lang"], lang["level"]))
                for skill in parsed["skills"]:
                    exists = conn.execute("SELECT 1 FROM candidate_skills WHERE candidate_id=? AND skill=?",
                                           (candidate_id, skill)).fetchone()
                    if not exists:
                        conn.execute("INSERT INTO candidate_skills (candidate_id,skill) VALUES (?,?)", (candidate_id, skill))
                log_candidate_event(conn, candidate_id, "cv_parsed", "Applied CV extraction to profile")
                conn.commit()
        return self._send_json(parsed)

    def _h_set_stage(self, conn, session, body, candidate_id):
        if not has_role(session, "recruiter"):
            return self._send_json({"error": "forbidden — viewers are read-only"}, 403)
        new_stage = body.get("stage")
        if not new_stage:
            return self._send_json({"error": "stage is required"}, 400)
        old = conn.execute("SELECT stage FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not old:
            return self._send_json({"error": "candidate not found"}, 404)
        conn.execute("UPDATE candidates SET stage=?, last_contact_days=0 WHERE id=?", (new_stage, candidate_id))
        log_candidate_event(conn, candidate_id, "stage_changed", f"{old['stage']} → {new_stage} by {session['username']}")
        conn.commit()
        wf_result = workflow.evaluate_workflows(conn, session["organization_id"], "candidate_stage_changed",
                                                  {"candidate_id": candidate_id, "stage": new_stage}, actor=session["username"])
        return self._send_json({"ok": True, "stage": new_stage, "workflows": wf_result})

    def _h_refresh_duplicates(self, conn, session, body, *_):
        if not has_role(session, "recruiter"):
            return self._send_json({"error": "forbidden — viewers are read-only"}, 403)
        count = domain.refresh_duplicate_suggestions(conn)
        return self._send_json({"new_suggestions": count})

    def _h_duplicate_action(self, conn, session, body, dup_id, action):
        if not has_role(session, "recruiter"):
            return self._send_json({"error": "forbidden — viewers are read-only"}, 403)
        row = conn.execute("SELECT * FROM duplicate_suggestions WHERE id=?", (dup_id,)).fetchone()
        if not row:
            return self._send_json({"error": "not found"}, 404)
        if action == "merge":
            keep = body.get("keep", row["candidate_a"])
            merge_id = row["candidate_b"] if keep == row["candidate_a"] else row["candidate_a"]
            domain.merge_candidates(conn, keep, merge_id, session["username"])
        conn.execute("UPDATE duplicate_suggestions SET status=? WHERE id=?",
                     ("merged" if action == "merge" else "rejected", dup_id))
        conn.commit()
        return self._send_json({"ok": True})

    def _h_screening_start(self, conn, session, body, *_):
        candidate_id = body.get("candidate_id")
        cand = domain.get_candidate_full(conn, candidate_id)
        if not cand: return self._send_json({"error": "candidate not found"}, 404)
        jobs = [domain.row_to_job(r) for r in conn.execute("SELECT * FROM jobs")]
        job = next((j for j in jobs if any(l["lang"] == j["language"] for l in cand["languages"])), jobs[0])
        sid = secrets.token_hex(8)
        greeting = (f"Hi {cand['name'].split(' ')[0]}, you're chatting with Talent Loop's screening "
                    f"assistant — an AI, not a recruiter — for the {job['title']} role.")
        flow = domain.SCREENING_FLOW[job["id"]]
        log = [{"role": "ai", "text": greeting}, {"role": "ai", "text": flow[0]["q"]}]
        conn.execute("""INSERT INTO screening_sessions (id,candidate_id,job_id,step,answers,status,log)
            VALUES (?,?,?,?,?,?,?)""", (sid, candidate_id, job["id"], 0, "{}", "in_progress", json.dumps(log)))
        conn.commit()
        return self._send_json({"session_id": sid, "job": job["title"], "log": log, "done": False})

    def _h_screening_answer(self, conn, session, body, sid):
        srow = conn.execute("SELECT * FROM screening_sessions WHERE id=?", (sid,)).fetchone()
        if not srow: return self._send_json({"error": "session not found"}, 404)
        job = domain.row_to_job(conn.execute("SELECT * FROM jobs WHERE id=?", (srow["job_id"],)).fetchone())
        flow = domain.SCREENING_FLOW[job["id"]]
        answers, log, step = json.loads(srow["answers"]), json.loads(srow["log"]), srow["step"]
        answer_text = body.get("text", "")
        log.append({"role": "user", "text": answer_text})
        if step < len(flow):
            key = flow[step]["key"]
            answers[key] = domain.check_screening_answer(key, answer_text, job)
            step += 1
        done = step >= len(flow)
        if not done:
            log.append({"role": "ai", "text": flow[step]["q"]})
        else:
            passed = all(answers.values())
            verdict = (f"Thanks — based on your answers you meet the core requirements for {job['title']}. "
                       f"A recruiter will follow up to schedule next steps.") if passed else \
                      ("Thanks for your answers. This role may not be the best fit right now, but you'll "
                       "stay in our talent pool for future roles that match better.")
            log.append({"role": "ai", "text": verdict})
            cand = domain.get_candidate_full(conn, srow["candidate_id"])
            log_audit(conn, "AI Screening Agent", "Completed screening",
                      f"{cand['name']} → {job['title']}: {'passed' if passed else 'did not pass'}",
                      organization_id=session["organization_id"])
            log_candidate_event(conn, srow["candidate_id"], "screening_completed",
                                 f"{job['title']}: {'passed' if passed else 'did not pass'}")
            if passed:
                conn.execute("UPDATE candidates SET stage='Screening' WHERE id=? AND stage='New'", (srow["candidate_id"],))
        conn.execute("UPDATE screening_sessions SET step=?, answers=?, log=?, status=? WHERE id=?",
                     (step, json.dumps(answers), json.dumps(log), "done" if done else "in_progress", sid))
        conn.commit()
        if done:
            job_score_ctx = {"candidate_id": srow["candidate_id"], "language": job["language"], "level": job["level"]}
            match = domain.compute_match(job, domain.get_candidate_full(conn, srow["candidate_id"]))
            if match["eligible"]:
                job_score_ctx["score"] = match["score"]
                workflow.evaluate_workflows(conn, session["organization_id"], "match_computed", job_score_ctx,
                                             actor="AI Screening Agent")
        return self._send_json({"log": log, "done": done})

    def _h_approval_action(self, conn, session, body, approval_id, action):
        if not has_role(session, "team_lead"):
            return self._send_json({"error": "forbidden — team lead or admin role required"}, 403)
        new_status = "approved" if action == "approve" else "rejected"
        conn.execute("UPDATE approvals SET status=? WHERE id=?", (new_status, approval_id))
        row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        conn.commit()
        log_audit(conn, session["username"], f"{new_status.capitalize()} action", row["title"] if row else "",
                  organization_id=session["organization_id"])
        integration_result = None
        if new_status == "approved" and row and row["action_type"] in ("send_message", "publish"):
            payload = json.loads(row["action_payload"] or "{}")
            channel = payload.get("channel", "email")
            adapter = integrations.get_adapter(channel)
            params = payload.get("params", {})
            candidate_id = payload.get("candidate_id")
            to = params.get("to")
            if not to and candidate_id:
                cand = domain.get_candidate_full(conn, candidate_id)
                to = (cand or {}).get("email") if channel == "email" else (cand or {}).get("phone")
            try:
                if row["action_type"] == "publish":
                    integration_result = adapter.publish_post(conn, params.get("message", ""), params.get("target"))
                else:
                    integration_result = adapter.send_message(conn, to, params.get("message", ""), params)
                log_audit(conn, session["username"], f"Executed approved {row['action_type']} via {channel}",
                          json.dumps(integration_result)[:200], organization_id=session["organization_id"])
            except integrations.IntegrationNotConfigured as e:
                integration_result = {"ok": False, "error": str(e)}
        return self._send_json({"ok": True, "status": new_status, "integration_result": integration_result})

    def _h_agent_command(self, conn, session, body, *_):
        text = body.get("text", "")
        tool_name, args = agent.parse_command(conn, text)
        if not tool_name:
            return self._send_json({"reply": "I can search candidates, check bottlenecks, draft a campaign, "
                                              "rediscover candidates, generate screening questions, or give you "
                                              "the daily briefing. Try referencing a job by ID (J1, J2, J3).",
                                     "tool": None})
        try:
            result = agent.dispatch(conn, session, tool_name, args)
        except agent.ToolError as e:
            return self._send_json({"reply": f"I couldn't safely complete this: {e}", "tool": tool_name, "error": str(e)})
        reply = self._summarize_tool_result(tool_name, result)
        return self._send_json({"reply": reply, "tool": tool_name, "result": result})

    def _summarize_tool_result(self, tool_name, result):
        if tool_name in ("searchCandidates", "rediscoverCandidates", "matchCandidates"):
            results = result.get("results", [])
            eligible = [r for r in results if r["match"]["eligible"]]
            if not eligible:
                return f"No eligible candidates found for {result.get('job','this job')}."
            top = ", ".join(f"{r['candidate']['name']} ({r['match']['score']}/100)" for r in eligible[:5])
            return f"For {result['job']}, {len(eligible)} eligible candidates found. Top: {top}."
        if tool_name == "findNeverHired":
            names = ", ".join(c["name"] for c in result["candidates"][:8])
            return f"{len(result['candidates'])} candidate(s) contacted before but never hired: {names or 'none'}."
        if tool_name == "getRecruitmentStats":
            return f"{result['count']} candidate(s) are overdue for follow-up."
        if tool_name == "findBottlenecks":
            parts = [f"{c['campaign']}: weakest stage is {c['worst_stage']['name']} at {c['worst_stage']['rate']*100:.0f}%"
                     for c in result["campaigns"]]
            return "; ".join(parts) or "No campaigns to analyze."
        if tool_name == "generateDailyBriefing":
            return " ".join(result["priorities"])
        if tool_name == "createScreeningQuestions":
            return f"Generated {len(result['questions'])} screening questions for {result['job']}."
        if tool_name == "createCampaign":
            return f"Drafted '{result['drafted']}' — sent to the Approval Center."
        if tool_name == "compareJobs":
            return "; ".join(f"{j['title']}: {j['filled']}/{j['vacancies']} filled" for j in result["jobs"])
        return json.dumps(result)[:300]

    def _h_create_workflow(self, conn, session, body, *_):
        if not has_role(session, "team_lead"):
            return self._send_json({"error": "forbidden — team lead or admin role required"}, 403)
        wid = workflow.create_workflow(conn, session["organization_id"], body.get("name", "Untitled workflow"),
                                        body.get("trigger_type"), body.get("conditions", []),
                                        body.get("actions", []), session["username"], status="draft")
        log_audit(conn, session["username"], "Created workflow", wid, organization_id=session["organization_id"])
        return self._send_json({"id": wid, "status": "draft"})

    def _h_workflow_nl(self, conn, session, body, *_):
        if not has_role(session, "team_lead"):
            return self._send_json({"error": "forbidden — team lead or admin role required"}, 403)
        parsed = workflow.parse_nl_workflow(body.get("text", ""))
        if not parsed["ok"]:
            return self._send_json(parsed)
        wid = workflow.create_workflow(conn, session["organization_id"], parsed["name"], parsed["trigger_type"],
                                        parsed["conditions"], parsed["actions"], session["username"], status="draft")
        log_audit(conn, session["username"], "AI drafted workflow from natural language", parsed["name"],
                  organization_id=session["organization_id"])
        return self._send_json({"ok": True, "id": wid, "preview": parsed, "status": "draft — review then activate"})

    def _h_workflow_toggle(self, conn, session, body, wid, action):
        if not has_role(session, "team_lead"):
            return self._send_json({"error": "forbidden — team lead or admin role required"}, 403)
        new_status = "active" if action == "activate" else "paused"
        conn.execute("UPDATE workflows SET status=? WHERE id=?", (new_status, wid))
        conn.commit()
        log_audit(conn, session["username"], f"Workflow {new_status}", wid, organization_id=session["organization_id"])
        return self._send_json({"ok": True, "status": new_status})

    def _h_killswitch(self, conn, session, body, action):
        if not has_role(session, "admin"):
            return self._send_json({"error": "forbidden — admin role required"}, 403)
        workflow.set_kill_switch(conn, action == "activate", session["username"])
        return self._send_json({"active": action == "activate"})

    def _h_create_variant(self, conn, session, body, campaign_id):
        if not has_role(session, "recruiter"):
            return self._send_json({"error": "forbidden — viewers are read-only"}, 403)
        conn.execute("""INSERT INTO campaign_variants (campaign_id,name,variant_type,reach,clicks,applications,qualified,hires)
            VALUES (?,?,?,?,?,?,?,?)""", (campaign_id, body.get("name", "Untitled variant"), body.get("variant_type", "message"),
             int(body.get("reach", 0)), int(body.get("clicks", 0)), int(body.get("applications", 0)),
             int(body.get("qualified", 0)), int(body.get("hires", 0))))
        conn.commit()
        return self._send_json({"ok": True})

    def _h_assign_candidate(self, conn, session, body, candidate_id):
        if not has_role(session, "team_lead"):
            return self._send_json({"error": "forbidden — team lead or admin role required"}, 403)
        recruiter = body.get("recruiter")
        if not recruiter:
            return self._send_json({"error": "recruiter is required"}, 400)
        callcenter.assign_candidate(conn, candidate_id, recruiter, session["username"])
        return self._send_json({"ok": True, "assigned_to": recruiter})

    def _h_bulk_import(self, conn, session, body, *_):
        if not has_role(session, "team_lead"):
            return self._send_json({"error": "forbidden — team lead or admin role required"}, 403)
        rows = body.get("rows", [])
        if not rows:
            return self._send_json({"error": "rows is required and must be non-empty"}, 400)
        result = callcenter.bulk_import_candidates(conn, session["organization_id"], rows, session["username"],
                                                     auto_assign=body.get("auto_assign", True))
        log_audit(conn, session["username"], "Bulk imported candidates", f"{len(result['created'])} created",
                  organization_id=session["organization_id"])
        return self._send_json(result)

    def _h_log_call(self, conn, session, body, *_):
        if not has_role(session, "recruiter"):
            return self._send_json({"error": "forbidden — viewers are read-only"}, 403)
        candidate_id, outcome = body.get("candidate_id"), body.get("outcome")
        if not candidate_id or not outcome:
            return self._send_json({"error": "candidate_id and outcome are required"}, 400)
        try:
            callcenter.log_call(conn, candidate_id, session["username"], outcome,
                                 objection_reason=body.get("objection_reason"), notes=body.get("notes", ""),
                                 callback_at=body.get("callback_at"))
        except ValueError as e:
            return self._send_json({"error": str(e)}, 400)
        wf_ctx = {"candidate_id": candidate_id, "outcome": outcome}
        workflow.evaluate_workflows(conn, session["organization_id"], "call_logged", wf_ctx, actor=session["username"])
        return self._send_json({"ok": True})

    def _h_raise_escalation(self, conn, session, body, *_):
        if not has_role(session, "recruiter"):
            return self._send_json({"error": "forbidden — viewers are read-only"}, 403)
        candidate_id, reason = body.get("candidate_id"), body.get("reason")
        if not candidate_id or not reason:
            return self._send_json({"error": "candidate_id and reason are required"}, 400)
        eid = callcenter.raise_escalation(conn, candidate_id, session["username"], reason)
        return self._send_json({"ok": True, "id": eid})

    def _h_resolve_escalation(self, conn, session, body, escalation_id):
        if not has_role(session, "team_lead"):
            return self._send_json({"error": "forbidden — team lead or admin role required"}, 403)
        callcenter.resolve_escalation(conn, escalation_id, session["username"], body.get("note", ""))
        return self._send_json({"ok": True})

    # ---------- Admin Console: Jobs CRUD ----------
    def _h_create_job(self, conn, session, body, *_):
        if not has_role(session, "team_lead"):
            return self._send_json({"error": "forbidden — team lead or admin role required"}, 403)
        jid = body.get("id") or ("J" + secrets.token_hex(3).upper())
        exists = conn.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone()
        if exists:
            return self._send_json({"error": f"job id {jid} already exists"}, 400)
        conn.execute("""INSERT INTO jobs (id,organization_id,title,company,language,level,education,
            experience_years_required,location,work_model,shift,salary,vacancies,filled,deadline,
            must_have,nice_to_have,disqualifiers,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (jid, session["organization_id"], body.get("title", "Untitled role"), body.get("company", ""),
             body.get("language", "English"), body.get("level", "B1"), body.get("education", "Any"),
             int(body.get("experience_years_required", 0)), body.get("location", ""),
             body.get("work_model", "On-site"), body.get("shift", ""), body.get("salary", ""),
             int(body.get("vacancies", 1)), int(body.get("filled", 0)), body.get("deadline"),
             json.dumps(body.get("must_have", [])), json.dumps(body.get("nice_to_have", [])),
             json.dumps(body.get("disqualifiers", [])), body.get("status", "open"),
             datetime.datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        log_audit(conn, session["username"], "Created job", jid, organization_id=session["organization_id"])
        return self._send_json({"ok": True, "id": jid})

    _JOB_FIELDS = {"title", "company", "language", "level", "education", "experience_years_required",
                    "location", "work_model", "shift", "salary", "vacancies", "filled", "deadline", "status"}
    _JOB_JSON_FIELDS = {"must_have", "nice_to_have", "disqualifiers"}

    def _h_update_job(self, conn, session, body, job_id):
        if not has_role(session, "team_lead"):
            return self._send_json({"error": "forbidden — team lead or admin role required"}, 403)
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return self._send_json({"error": "job not found"}, 404)
        sets, params = [], []
        for k, v in body.items():
            if k in self._JOB_FIELDS:
                sets.append(f"{k}=?"); params.append(v)
            elif k in self._JOB_JSON_FIELDS:
                sets.append(f"{k}=?"); params.append(json.dumps(v))
        if not sets:
            return self._send_json({"error": "no valid fields to update"}, 400)
        params.append(job_id)
        conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", params)
        conn.commit()
        log_audit(conn, session["username"], "Updated job", f"{job_id}: {list(body.keys())}",
                  organization_id=session["organization_id"])
        return self._send_json({"ok": True, "job": domain.row_to_job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())})

    # ---------- Admin Console: Offers CRUD ----------
    def _h_create_offer(self, conn, session, body, *_):
        if not has_role(session, "recruiter"):
            return self._send_json({"error": "forbidden — viewers are read-only"}, 403)
        candidate_id, job_id = body.get("candidate_id"), body.get("job_id")
        if not candidate_id or not job_id:
            return self._send_json({"error": "candidate_id and job_id are required"}, 400)
        oid = "O" + secrets.token_hex(3).upper()
        conn.execute("INSERT INTO offers (id,candidate_id,job_id,status,sent_at) VALUES (?,?,?,?,?)",
                     (oid, candidate_id, job_id, body.get("status", "Draft"), body.get("sent_at")))
        log_candidate_event(conn, candidate_id, "offer_created", f"Offer {oid} for {job_id} by {session['username']}")
        conn.commit()
        return self._send_json({"ok": True, "id": oid})

    def _h_update_offer(self, conn, session, body, offer_id):
        if not has_role(session, "recruiter"):
            return self._send_json({"error": "forbidden — viewers are read-only"}, 403)
        row = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not row:
            return self._send_json({"error": "offer not found"}, 404)
        new_status = body.get("status", row["status"])
        sent_at = body.get("sent_at", row["sent_at"])
        if new_status == "Sent" and not sent_at:
            sent_at = datetime.date.today().isoformat()
        conn.execute("UPDATE offers SET status=?, sent_at=? WHERE id=?", (new_status, sent_at, offer_id))
        log_candidate_event(conn, row["candidate_id"], "offer_updated", f"Offer {offer_id}: {row['status']} → {new_status}")
        conn.commit()
        return self._send_json({"ok": True, "status": new_status, "sent_at": sent_at})

    # ---------- Admin Console: Sources CRUD ----------
    def _h_create_source(self, conn, session, body, *_):
        if not has_role(session, "team_lead"):
            return self._send_json({"error": "forbidden — team lead or admin role required"}, 403)
        conn.execute("""INSERT INTO sources (organization_id,name,applications,qualified,hires)
            VALUES (?,?,?,?,?)""", (session["organization_id"], body.get("name", "Untitled source"),
             int(body.get("applications", 0)), int(body.get("qualified", 0)), int(body.get("hires", 0))))
        conn.commit()
        return self._send_json({"ok": True})

    def _h_update_source(self, conn, session, body, source_id):
        if not has_role(session, "team_lead"):
            return self._send_json({"error": "forbidden — team lead or admin role required"}, 403)
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            return self._send_json({"error": "source not found"}, 404)
        fields = {k: body[k] for k in ("name", "applications", "qualified", "hires") if k in body}
        if not fields:
            return self._send_json({"error": "no valid fields to update"}, 400)
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE sources SET {sets} WHERE id=?", list(fields.values()) + [source_id])
        conn.commit()
        return self._send_json({"ok": True})

    # ---------- Admin Console: Users CRUD (admin only — top of the hierarchy) ----------
    def _h_create_user(self, conn, session, body, *_):
        if not has_role(session, "admin"):
            return self._send_json({"error": "forbidden — admin role required"}, 403)
        username, password, role = body.get("username"), body.get("password"), body.get("role")
        if not username or not password or role not in ("admin", "team_lead", "recruiter", "viewer"):
            return self._send_json({"error": "username, password, and a valid role are required"}, 400)
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return self._send_json({"error": "username already exists"}, 400)
        digest, salt = hash_password(password)
        conn.execute("INSERT INTO users (organization_id,username,password_hash,salt,role,active) VALUES (?,?,?,?,?,1)",
                     (session["organization_id"], username, digest, salt, role))
        conn.commit()
        log_audit(conn, session["username"], "Created user", f"{username} ({role})", organization_id=session["organization_id"])
        return self._send_json({"ok": True, "username": username, "role": role})

    def _h_update_user(self, conn, session, body, username):
        if not has_role(session, "admin"):
            return self._send_json({"error": "forbidden — admin role required"}, 403)
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return self._send_json({"error": "user not found"}, 404)
        if username == session["username"] and (body.get("active") is False or (body.get("role") and body["role"] != row["role"])):
            return self._send_json({"error": "cannot change your own role or deactivate your own account"}, 400)
        sets, params = [], []
        if "role" in body:
            if body["role"] not in ("admin", "team_lead", "recruiter", "viewer"):
                return self._send_json({"error": "invalid role"}, 400)
            sets.append("role=?"); params.append(body["role"])
        if "active" in body:
            sets.append("active=?"); params.append(1 if body["active"] else 0)
        if "password" in body and body["password"]:
            digest, salt = hash_password(body["password"])
            sets.append("password_hash=?"); params.append(digest)
            sets.append("salt=?"); params.append(salt)
        if not sets:
            return self._send_json({"error": "no valid fields to update"}, 400)
        params.append(username)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE username=?", params)
        conn.commit()
        log_audit(conn, session["username"], "Updated user", f"{username}: {list(body.keys())}",
                  organization_id=session["organization_id"])
        return self._send_json({"ok": True})

    def _h_set_target(self, conn, session, body, *_):
        if not has_role(session, "team_lead"):
            return self._send_json({"error": "forbidden — team lead or admin role required"}, 403)
        recruiter, target = body.get("recruiter"), body.get("weekly_call_target")
        if not recruiter or not target:
            return self._send_json({"error": "recruiter and weekly_call_target are required"}, 400)
        callcenter.set_weekly_target(conn, recruiter, int(target))
        return self._send_json({"ok": True})
