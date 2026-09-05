"""
Database layer: connection + versioned migrations + seed data.

Runs on SQLite today (stdlib, zero install). To move to Postgres later:
  1. `pip install psycopg2-binary`
  2. set DATABASE_URL to a postgres:// URL
  3. swap get_db() below for a psycopg2 connection with the same
     row-as-dict behaviour (psycopg2.extras.RealDictCursor)
The SQL in migrations/*.sql avoids SQLite-only syntax (no `AUTOINCREMENT`
quirks beyond what Postgres's SERIAL-equivalent tolerates via
`INTEGER PRIMARY KEY AUTOINCREMENT` → note: this one line does need a
find/replace to `SERIAL PRIMARY KEY` on Postgres; documented in README).
"""
import sqlite3
import os
import glob
import hashlib
import secrets
import datetime
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "talent_loop.db"))
MIGRATIONS_DIR = os.path.join(BASE_DIR, "migrations")
DEFAULT_ORG = "org_northwind"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def run_migrations():
    """Applies any migrations/*.sql not yet recorded in schema_migrations,
    in filename order. Idempotent — safe to call on every startup."""
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)""")
    applied = {r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations")}
    files = sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))
    newly_applied = []
    for path in files:
        fname = os.path.basename(path)
        if fname in applied:
            continue
        with open(path) as f:
            sql = f.read()
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_migrations (filename, applied_at) VALUES (?,?)",
                     (fname, datetime.datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        newly_applied.append(fname)
    conn.close()
    return newly_applied


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return digest, salt


def seed_if_empty():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM organizations")
    if cur.fetchone()[0] > 0:
        conn.close()
        return False

    now = datetime.datetime.now().isoformat(timespec="seconds")
    cur.execute("INSERT INTO organizations (id,name,plan,ai_budget_daily,created_at) VALUES (?,?,?,?,?)",
                (DEFAULT_ORG, "Northwind Staffing", "agency", 50.0, now))

    cur.execute("INSERT INTO kill_switch (id, active, updated_at, updated_by) VALUES (1, 0, ?, 'system')", (now,))

    users = [("admin", "admin123", "admin"), ("mona", "mona123", "team_lead"),
             ("recruiter", "recruiter123", "recruiter"), ("sara", "sara123", "recruiter"),
             ("hassan", "hassan123", "recruiter"), ("viewer", "viewer123", "viewer")]
    for username, pw, role in users:
        digest, salt = hash_password(pw)
        cur.execute("INSERT INTO users (organization_id,username,password_hash,salt,role,active) VALUES (?,?,?,?,?,1)",
                    (DEFAULT_ORG, username, digest, salt, role))

    jobs = [
        ("J1", "German B2 Customer Service", "Northwind BPO", "German", "B2", "Graduate", 0,
         "Cairo", "On-site", "Rotational", "18,000 EGP", 40, 14, "2026-09-20",
         '["German B2+","Graduate","Rotational shift OK"]', '["Prior call-center experience"]', '["Below B1 German"]', "open"),
        ("J2", "English B2 Technical Support", "Northwind BPO", "English", "B2", "Any", 1,
         "Remote", "Remote", "Fixed", "22,000 EGP", 20, 6, "2026-09-15",
         '["English B2+","1+ yrs support exp"]', '["Ticketing tool experience"]', '[]', "open"),
        ("J3", "French B1 Sales Advisor", "Northwind BPO", "French", "B1", "Graduate", 0,
         "Cairo", "On-site", "Rotational", "17,000 EGP", 15, 3, "2026-09-25",
         '["French B1+","Graduate"]', '["Sales experience"]', '["Below A2 French"]', "open"),
    ]
    cur.executemany(f"""INSERT INTO jobs (id,title,company,language,level,education,
        experience_years_required,location,work_model,shift,salary,vacancies,filled,deadline,
        must_have,nice_to_have,disqualifiers,status,organization_id,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'{DEFAULT_ORG}','{now}')""", jobs)

    candidates = [
        ("C1", "Mariam Sabry", "mariam.sabry@example.com", "+201001110001", "Cairo", "Graduate", 2, "Facebook", "Hired", 1, "Available immediately."),
        ("C2", "Omar Farouk", "omar.farouk@example.com", "+201001110002", "Cairo", "Undergraduate", 0, "Referral", "New", 5, ""),
        ("C3", "Laila Nasr", "laila.nasr@example.com", "+201001110003", "Giza", "Graduate", 1, "LinkedIn", "Contacted", 4, "Rejected previously for a German role — reason: shift conflict."),
        ("C4", "Youssef Adel", "youssef.adel@example.com", "+201001110004", "Remote / Alexandria", "Graduate", 3, "Website", "Interview", 2, ""),
        ("C5", "Nour El-Din", "nour.eldin@example.com", "+201001110005", "Cairo", "Graduate", 0, "Telegram", "Screening", 6, ""),
        ("C6", "Sara Kamel", "sara.kamel@example.com", "+201001110006", "Cairo", "Graduate", 1, "Facebook", "New", 0, ""),
        ("C7", "Kareem Hossam", "kareem.hossam@example.com", "+201001110007", "Cairo", "Graduate", 0, "CSV Import", "New", 8, ""),
        ("C8", "Dina Mostafa", "dina.mostafa@example.com", "+201001110008", "6th of October", "Graduate", 4, "Referral", "Qualified", 2, ""),
        # near-duplicate of C1 on purpose, to demonstrate dedup detection
        ("C9", "Mariam Sabri", None, "+201001110001", "Cairo", "Graduate", 2, "Website", "New", 0, ""),
        # a previously-rejected-for-shift candidate the recycling engine should surface for J2 (remote/fixed shift)
        ("C10", "Hana Zaki", "hana.zaki@example.com", "+201001110010", "Cairo", "Graduate", 2, "Facebook", "Rejected", 20, "Rejected previously — reason: shift conflict (rotational not possible)."),
    ]
    cur.executemany(f"""INSERT INTO candidates (id,name,email,phone,location,education,experience_years,
        source,stage,last_contact_days,notes,organization_id,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,'{DEFAULT_ORG}','{now}')""", candidates)

    languages = [
        ("C1", "German", "B2"), ("C1", "English", "B1"),
        ("C2", "German", "B1"),
        ("C3", "German", "C1"), ("C3", "English", "B2"),
        ("C4", "English", "C1"),
        ("C5", "French", "B1"), ("C5", "English", "A2"),
        ("C6", "German", "B2"), ("C6", "French", "A1"),
        ("C7", "German", "A2"),
        ("C8", "English", "C2"), ("C8", "French", "B2"),
        ("C9", "German", "B2"), ("C9", "English", "B1"),
        ("C10", "English", "B2"),
    ]
    cur.executemany("INSERT INTO candidate_languages (candidate_id,lang,level) VALUES (?,?,?)", languages)

    skills = [
        ("C1", "Customer Service"), ("C1", "Communication"),
        ("C2", "Communication"),
        ("C3", "Customer Service"), ("C3", "Sales"), ("C3", "Team Leadership"),
        ("C4", "Technical Support"), ("C4", "Ticketing"), ("C4", "Troubleshooting"),
        ("C5", "Sales"),
        ("C6", "Customer Service"), ("C6", "Communication"), ("C6", "CRM"),
        ("C7", "Communication"),
        ("C8", "Technical Support"), ("C8", "Sales"), ("C8", "Fluent writing"),
        ("C9", "Customer Service"), ("C9", "Communication"),
        ("C10", "Technical Support"),
    ]
    cur.executemany("INSERT INTO candidate_skills (candidate_id,skill) VALUES (?,?)", skills)

    for cid in [c[0] for c in candidates]:
        cur.execute("INSERT INTO candidate_events (candidate_id, ts, type, detail) VALUES (?,?,?,?)",
                     (cid, now, "created", "Candidate record created (seed data)"))

    interviews = [
        ("I1", "C4", "J2", "Nadine (Hiring Manager)", "2026-09-03 11:00", "Scheduled", None),
        ("I2", "C1", "J1", "Hassan (Team Lead)", "2026-09-04 14:00", "Scheduled", None),
        ("I3", "C8", "J2", "Nadine (Hiring Manager)", "2026-08-29 10:00", "No-show", None),
        ("I4", "C3", "J1", "Hassan (Team Lead)", "2026-08-27 09:30", "Completed",
         "Strong communicator, shift availability still unconfirmed."),
    ]
    cur.executemany("""INSERT INTO interviews (id,candidate_id,job_id,interviewer,date,status,feedback)
        VALUES (?,?,?,?,?,?,?)""", interviews)

    offers = [("O1", "C1", "J1", "Draft", None), ("O2", "C8", "J2", "Sent", "2026-08-30"),
              ("O3", "C4", "J2", "Viewed", "2026-08-28")]
    cur.executemany("INSERT INTO offers (id,candidate_id,job_id,status,sent_at) VALUES (?,?,?,?,?)", offers)

    sources = [("Facebook Groups", 412, 96, 11), ("LinkedIn", 180, 71, 14),
               ("Telegram", 260, 38, 3), ("Referrals", 64, 41, 9)]
    cur.executemany(f"INSERT INTO sources (organization_id,name,applications,qualified,hires) VALUES ('{DEFAULT_ORG}',?,?,?,?)", sources)

    cur.execute("""INSERT INTO campaigns (id,organization_id,name,job_id,target,reach,clicks,applications,
        screened,qualified,interviews,selected,hires,attendance_rate,status) VALUES
        ('CAMP1',?,'German B2 Campaign','J1',100,24000,2100,640,410,180,96,41,23,0.58,'Active')""", (DEFAULT_ORG,))

    # 21 days of daily_metrics history for CAMP1 so anomaly detection / forecasting
    # have something real to compute against. Last 2 days simulate a genuine drop.
    import random
    random.seed(42)
    base_date = datetime.date(2026, 8, 30)
    for i in range(21):
        d = base_date - datetime.timedelta(days=20 - i)
        apps = random.randint(26, 34)
        if i >= 19:  # simulate the anomaly the dashboard should catch
            apps = random.randint(6, 10)
        qualified = round(apps * random.uniform(0.25, 0.32))
        interviews = round(qualified * random.uniform(0.45, 0.55))
        hires = round(interviews * random.uniform(0.2, 0.3))
        cur.execute("""INSERT INTO daily_metrics (campaign_id,date,applications,qualified,interviews,hires)
            VALUES ('CAMP1',?,?,?,?,?)""", (d.isoformat(), apps, qualified, interviews, hires))

    cur.execute(f"""INSERT INTO campaign_variants (campaign_id,name,variant_type,reach,clicks,applications,qualified,hires)
        VALUES ('CAMP1','A — Salary-focused','content',9000,820,260,102,9)""")
    cur.execute(f"""INSERT INTO campaign_variants (campaign_id,name,variant_type,reach,clicks,applications,qualified,hires)
        VALUES ('CAMP1','B — Career-growth-focused','content',8200,690,210,88,8)""")
    cur.execute(f"""INSERT INTO campaign_variants (campaign_id,name,variant_type,reach,clicks,applications,qualified,hires)
        VALUES ('CAMP1','C — Urgency-focused','content',6800,590,170,58,6)""")

    # ---- Call-center / team layer: assign candidates round-robin, seed a
    # realistic 21-day call history so best-call-time, objection trends,
    # and the fair leaderboard have real data to compute against.
    recruiter_pool = ["recruiter", "sara", "hassan"]
    candidate_ids = [c[0] for c in candidates]
    for i, cid in enumerate(candidate_ids):
        rec = recruiter_pool[i % len(recruiter_pool)]
        cur.execute("UPDATE candidates SET assigned_recruiter=? WHERE id=?", (rec, cid))
        cur.execute("INSERT INTO candidate_events (candidate_id, ts, type, detail) VALUES (?,?,?,?)",
                     (cid, now, "assigned", f"Assigned to {rec} (seed data)"))

    outcomes_weighted = (["no_answer"] * 5 + ["voicemail"] * 3 + ["answered"] * 4 +
                         ["interested"] * 2 + ["not_interested"] * 3 + ["callback_requested"] * 2)
    objection_pool = ["salary", "shift", "location", "language_level", "timing", "already_placed", "other"]
    # weight calls toward hours 10-13 and 17-20 so best-call-time has a real signal to find
    hour_pool = [9, 10, 10, 11, 11, 12, 13, 14, 15, 16, 17, 17, 18, 18, 19, 19, 20]
    base_dt = datetime.datetime(2026, 8, 31, 0, 0, 0)
    for day_offset in range(21):
        day = base_dt - datetime.timedelta(days=20 - day_offset)
        calls_today = random.randint(3, 7)
        for _ in range(calls_today):
            cid = random.choice(candidate_ids)
            rec = recruiter_pool[candidate_ids.index(cid) % len(recruiter_pool)]
            hour = random.choice(hour_pool)
            # calls in the "good" windows (10-13, 17-20) succeed noticeably more often
            good_window = hour in (10, 11, 12, 13, 17, 18, 19, 20)
            outcome = random.choice((["answered", "interested"] * 3 + outcomes_weighted) if good_window else outcomes_weighted)
            objection = random.choice(objection_pool) if outcome == "not_interested" else None
            ts = day.replace(hour=hour, minute=random.randint(0, 59))
            cur.execute("""INSERT INTO calls (candidate_id,recruiter,ts,hour_of_day,weekday,outcome,objection_reason,notes,callback_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (cid, rec, ts.isoformat(timespec="seconds"), hour, ts.weekday(), outcome, objection, "", None))

    # one seeded open escalation and one resolved, for the demo
    cur.execute("""INSERT INTO escalations (candidate_id,raised_by,ts,reason,status) VALUES (?,?,?,?,?)""",
                ("C3", "hassan", now, "Candidate is upset about being contacted 3 times with no update — needs a callback from a senior recruiter.", "open"))
    cur.execute("""INSERT INTO escalations (candidate_id,raised_by,ts,reason,status,resolved_by,resolved_at,resolution_note)
        VALUES (?,?,?,?,?,?,?,?)""",
        ("C8", "sara", now, "Candidate asked about relocation support — unclear on policy.", "resolved", "admin", now,
         "Confirmed no relocation support for this role; candidate informed."))

    conn.commit()
    conn.close()
    return True


def log_audit(conn, actor, action, detail, organization_id=DEFAULT_ORG):
    conn.execute("INSERT INTO audit_log (organization_id,ts,actor,action,detail) VALUES (?,?,?,?,?)",
                 (organization_id, datetime.datetime.now().isoformat(timespec="seconds"), actor, action, detail))
    conn.commit()


def log_candidate_event(conn, candidate_id, event_type, detail=""):
    conn.execute("INSERT INTO candidate_events (candidate_id, ts, type, detail) VALUES (?,?,?,?)",
                 (candidate_id, datetime.datetime.now().isoformat(timespec="seconds"), event_type, detail))
    conn.commit()
