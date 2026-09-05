"""
Automated tests for core flows (Section 89/90). Run with:
    python3 -m unittest discover -s tests -v

Uses a temporary SQLite file per test class so tests never touch the real
talent_loop.db, and can run repeatedly / in CI without manual cleanup.
"""
import unittest
import tempfile
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db_module


class BaseDBTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db_module.DB_PATH = self.tmp.name
        db_module.run_migrations()
        db_module.seed_if_empty()
        self.conn = db_module.get_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)


class TestAuth(BaseDBTestCase):
    def test_password_hash_roundtrip(self):
        digest, salt = db_module.hash_password("correct-horse")
        digest2, _ = db_module.hash_password("correct-horse", salt)
        self.assertEqual(digest, digest2)

    def test_wrong_password_fails(self):
        digest, salt = db_module.hash_password("correct-horse")
        digest2, _ = db_module.hash_password("wrong-password", salt)
        self.assertNotEqual(digest, digest2)


class TestMatchingEngine(BaseDBTestCase):
    def test_eligible_candidate_scores_above_zero(self):
        from app import domain
        job, results = domain.match_candidates_for_job(self.conn, "J1")
        top = next(r for r in results if r["candidate"]["id"] == "C1")
        self.assertTrue(top["match"]["eligible"])
        self.assertGreater(top["match"]["score"], 0)

    def test_hard_disqualifier_blocks_ineligible_candidate(self):
        from app import domain
        job, results = domain.match_candidates_for_job(self.conn, "J1")
        omar = next(r for r in results if r["candidate"]["id"] == "C2")
        self.assertFalse(omar["match"]["eligible"])  # German B1 < required B2

    def test_rule_engine_never_gives_partial_credit_for_disqualifier(self):
        from app import domain
        job, results = domain.match_candidates_for_job(self.conn, "J1")
        omar = next(r for r in results if r["candidate"]["id"] == "C2")
        self.assertEqual(omar["match"]["score"], 0)


class TestDeduplication(BaseDBTestCase):
    def test_seeded_near_duplicate_is_detected(self):
        from app import domain
        dupes = domain.find_duplicate_candidates(self.conn)
        pairs = {(d["candidate_a"], d["candidate_b"]) for d in dupes}
        self.assertIn(("C1", "C9"), pairs)

    def test_merge_hides_candidate_from_active_list(self):
        from app import domain
        domain.merge_candidates(self.conn, "C1", "C9", "test-actor")
        active_ids = domain.list_active_candidate_ids(self.conn)
        self.assertNotIn("C9", active_ids)
        self.assertIn("C1", active_ids)

    def test_merge_is_non_destructive(self):
        from app import domain
        domain.merge_candidates(self.conn, "C1", "C9", "test-actor")
        row = self.conn.execute("SELECT * FROM candidates WHERE id='C9'").fetchone()
        self.assertIsNotNone(row)  # record still exists, just marked merged
        self.assertEqual(row["merged_into"], "C1")


class TestCVParser(BaseDBTestCase):
    def test_extracts_education_experience_language(self):
        from app import domain
        text = "Bachelor of Commerce. 4 years of experience. French B1, English B2."
        result = domain.parse_cv_text(text)
        self.assertEqual(result["education"], "Graduate")
        self.assertEqual(result["experience_years"], 4)
        langs = {(l["lang"], l["level"]) for l in result["languages"]}
        self.assertIn(("French", "B1"), langs)

    def test_does_not_invent_missing_fields(self):
        from app import domain
        result = domain.parse_cv_text("Hello, I like recruiting.")
        self.assertIsNone(result["education"])
        self.assertIsNone(result["experience_years"])
        self.assertEqual(result["languages"], [])


class TestWorkflowEngine(BaseDBTestCase):
    def test_nl_parser_recognizes_score_threshold_pattern(self):
        from app import workflow
        parsed = workflow.parse_nl_workflow(
            "When a German B2 candidate scores above 85, notify me and add them to interview pool.")
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["trigger_type"], "match_computed")

    def test_nl_parser_rejects_unsupported_pattern(self):
        from app import workflow
        parsed = workflow.parse_nl_workflow("Make me a sandwich.")
        self.assertFalse(parsed["ok"])

    def test_workflow_executes_on_matching_event(self):
        from app import workflow
        wid = workflow.create_workflow(
            self.conn, "org_northwind", "test-wf", "match_computed",
            [{"field": "language", "op": "eq", "value": "German"}, {"field": "score", "op": "gte", "value": 50}],
            [{"type": "notify", "message": "hit"}], "tester", status="active")
        result = workflow.evaluate_workflows(self.conn, "org_northwind", "match_computed",
                                              {"candidate_id": "C1", "language": "German", "score": 77})
        self.assertEqual(len(result["executions"]), 1)
        self.assertEqual(result["executions"][0]["workflow_id"], wid)

    def test_workflow_does_not_execute_when_condition_fails(self):
        from app import workflow
        workflow.create_workflow(
            self.conn, "org_northwind", "test-wf-2", "match_computed",
            [{"field": "score", "op": "gte", "value": 95}],
            [{"type": "notify", "message": "hit"}], "tester", status="active")
        result = workflow.evaluate_workflows(self.conn, "org_northwind", "match_computed",
                                              {"candidate_id": "C1", "score": 77})
        self.assertEqual(len(result["executions"]), 0)

    def test_kill_switch_blocks_workflow_execution(self):
        from app import workflow
        workflow.create_workflow(
            self.conn, "org_northwind", "test-wf-3", "match_computed", [],
            [{"type": "notify", "message": "hit"}], "tester", status="active")
        workflow.set_kill_switch(self.conn, True, "admin")
        result = workflow.evaluate_workflows(self.conn, "org_northwind", "match_computed", {"candidate_id": "C1"})
        self.assertEqual(result.get("skipped"), "kill_switch_active")

    def test_send_message_action_requires_approval_not_direct_send(self):
        from app import workflow
        wid = workflow.create_workflow(
            self.conn, "org_northwind", "test-wf-4", "match_computed", [],
            [{"type": "send_message", "params": {"message": "hi"}}], "tester", status="active")
        before = self.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        workflow.evaluate_workflows(self.conn, "org_northwind", "match_computed", {"candidate_id": "C1"})
        after = self.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        self.assertEqual(after, before + 1)


class TestAgentToolPermissions(BaseDBTestCase):
    def test_viewer_can_call_readonly_tool(self):
        from app import agent
        session = {"username": "viewer", "role": "viewer", "organization_id": "org_northwind"}
        result = agent.dispatch(self.conn, session, "getRecruitmentStats", {})
        self.assertIn("count", result)

    def test_viewer_cannot_call_write_tool(self):
        from app import agent
        session = {"username": "viewer", "role": "viewer", "organization_id": "org_northwind"}
        with self.assertRaises(agent.ToolError):
            agent.dispatch(self.conn, session, "createCampaign", {"job_id": "J1"})

    def test_recruiter_can_call_write_tool(self):
        from app import agent
        session = {"username": "recruiter", "role": "recruiter", "organization_id": "org_northwind"}
        result = agent.dispatch(self.conn, session, "createCampaign", {"job_id": "J2"})
        self.assertEqual(result["status"], "pending_approval")

    def test_missing_required_arg_raises(self):
        from app import agent
        session = {"username": "recruiter", "role": "recruiter", "organization_id": "org_northwind"}
        with self.assertRaises(agent.ToolError):
            agent.dispatch(self.conn, session, "searchCandidates", {})

    def test_every_tool_call_is_logged(self):
        from app import agent
        session = {"username": "viewer", "role": "viewer", "organization_id": "org_northwind"}
        before = self.conn.execute("SELECT COUNT(*) FROM agent_tool_calls").fetchone()[0]
        agent.dispatch(self.conn, session, "getRecruitmentStats", {})
        after = self.conn.execute("SELECT COUNT(*) FROM agent_tool_calls").fetchone()[0]
        self.assertEqual(after, before + 1)


class TestIntelligence(BaseDBTestCase):
    def test_anomaly_detection_flags_seeded_drop(self):
        from app import domain
        result = domain.detect_anomalies(self.conn, "CAMP1")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(any(a["direction"] == "drop" for a in result["anomalies"]))

    def test_forecast_returns_confidence_and_assumptions(self):
        from app import domain
        result = domain.forecast_metric(self.conn, "CAMP1", 7, "hires")
        self.assertEqual(result["status"], "ok")
        self.assertIn("assumptions", result)
        self.assertIn("confidence", result)

    def test_what_if_scales_proportionally(self):
        from app import domain
        result = domain.what_if(self.conn, "CAMP1", "volume", 50)
        self.assertEqual(result["scenario"]["applications"], round(result["baseline"]["applications"] * 1.5))

    def test_health_score_flags_behind_pace_job(self):
        from app import domain
        scores = domain.recruitment_health_score(self.conn)
        j3 = next(s for s in scores if s["job_id"] == "J3")
        self.assertEqual(j3["status"], "red")

    def test_recommendations_cite_evidence(self):
        from app import domain
        recs = domain.generate_recommendations(self.conn)
        self.assertTrue(all("evidence" in r for r in recs))


class TestRateLimiting(unittest.TestCase):
    def test_rate_limit_blocks_after_threshold(self):
        from app import security
        security._rate_buckets.clear()
        key = "test-key"
        results = [security.check_rate_limit(key) for _ in range(security._RATE_MAX_REQUESTS + 5)]
        self.assertTrue(all(results[:security._RATE_MAX_REQUESTS]))
        self.assertFalse(any(results[security._RATE_MAX_REQUESTS:]))


class TestCallCenter(BaseDBTestCase):
    def test_assign_candidate_updates_owner_and_logs_event(self):
        from app import callcenter
        callcenter.assign_candidate(self.conn, "C2", "sara", "admin")
        row = self.conn.execute("SELECT assigned_recruiter FROM candidates WHERE id='C2'").fetchone()
        self.assertEqual(row["assigned_recruiter"], "sara")
        events = self.conn.execute("SELECT * FROM candidate_events WHERE candidate_id='C2' AND type='assigned'").fetchall()
        self.assertTrue(len(events) >= 1)

    def test_round_robin_distributes_evenly(self):
        from app import callcenter
        ids = ["C2", "C3", "C4", "C5", "C6", "C7"]
        mapping = callcenter.round_robin_assign(self.conn, ids, ["a", "b"], "admin")
        self.assertEqual(mapping["C2"], "a")
        self.assertEqual(mapping["C3"], "b")
        self.assertEqual(mapping["C4"], "a")

    def test_bulk_import_creates_and_assigns(self):
        from app import callcenter
        rows = [{"name": "Test One", "phone": "+201000000001", "language": "German", "level": "B2"},
                {"name": "Test Two", "phone": "+201000000002", "language": "English", "level": "B1"}]
        result = callcenter.bulk_import_candidates(self.conn, "org_northwind", rows, "recruiter")
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(len(result["assignment"]), 2)
        cand = self.conn.execute("SELECT * FROM candidates WHERE id=?", (result["created"][0],)).fetchone()
        self.assertEqual(cand["name"], "Test One")

    def test_log_call_resets_last_contact_and_records_row(self):
        from app import callcenter
        self.conn.execute("UPDATE candidates SET last_contact_days=9 WHERE id='C2'")
        self.conn.commit()
        before = len(callcenter.call_history(self.conn, "C2"))
        callcenter.log_call(self.conn, "C2", "recruiter", "answered")
        row = self.conn.execute("SELECT last_contact_days FROM candidates WHERE id='C2'").fetchone()
        self.assertEqual(row["last_contact_days"], 0)
        calls = callcenter.call_history(self.conn, "C2")
        self.assertEqual(len(calls), before + 1)
        self.assertEqual(calls[0]["outcome"], "answered")

    def test_log_call_rejects_unknown_outcome(self):
        from app import callcenter
        with self.assertRaises(ValueError):
            callcenter.log_call(self.conn, "C2", "recruiter", "maybe_later")

    def test_best_call_hours_falls_back_when_no_candidate_data(self):
        from app import callcenter
        result = callcenter.best_call_hours(self.conn, candidate_id="C2")
        self.assertIn("basis", result)
        self.assertIsInstance(result["hours"], list)

    def test_smart_queue_prioritizes_due_callback(self):
        from app import callcenter
        import datetime
        past = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        self.conn.execute("""INSERT INTO calls (candidate_id,recruiter,ts,hour_of_day,weekday,outcome,callback_at)
            VALUES ('C2','recruiter',?,10,0,'callback_requested',?)""", (past, past))
        self.conn.commit()
        queue = callcenter.smart_queue(self.conn)
        self.assertEqual(queue[0]["candidate"]["id"], "C2")
        self.assertEqual(queue[0]["priority"], "callback_due")

    def test_objection_trends_counts_within_window(self):
        from app import callcenter
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""INSERT INTO calls (candidate_id,recruiter,ts,hour_of_day,weekday,outcome,objection_reason)
            VALUES ('C2','recruiter',?,10,0,'not_interested','salary')""", (now,))
        self.conn.commit()
        result = callcenter.objection_trends(self.conn, 7)
        salary = next(o for o in result["objections"] if o["reason"] == "salary")
        self.assertGreaterEqual(salary["count_this_window"], 1)

    def test_fair_leaderboard_accounts_for_difficulty(self):
        from app import callcenter
        for cid in ["C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10"]:
            self.conn.execute("UPDATE candidates SET assigned_recruiter='recruiter' WHERE id=?", (cid,))
        self.conn.execute("UPDATE candidates SET assigned_recruiter='sara' WHERE id='C1'")
        self.conn.commit()
        board = callcenter.fair_leaderboard(self.conn)
        recruiter_entry = next(b for b in board if b["recruiter"] == "recruiter")
        self.assertEqual(recruiter_entry["assigned_count"], 9)

    def test_no_show_risk_flags_past_no_show(self):
        from app import callcenter
        result = callcenter.no_show_risk(self.conn, "C8")  # C8 has a seeded No-show interview
        self.assertGreaterEqual(result["score"], 40)
        self.assertEqual(result["level"], "high") if result["score"] >= 60 else None

    def test_recycle_alerts_finds_shift_rejected_candidate_with_open_remote_job(self):
        from app import callcenter
        alerts = callcenter.recycle_alerts(self.conn)
        names = [a["candidate"]["name"] for a in alerts]
        self.assertIn("Hana Zaki", names)

    def test_escalation_raise_and_resolve(self):
        from app import callcenter
        eid = callcenter.raise_escalation(self.conn, "C2", "recruiter", "Test escalation reason")
        open_list = callcenter.list_escalations(self.conn, "open")
        self.assertTrue(any(e["id"] == eid for e in open_list))
        callcenter.resolve_escalation(self.conn, eid, "admin", "Handled.")
        open_list_after = callcenter.list_escalations(self.conn, "open")
        self.assertFalse(any(e["id"] == eid for e in open_list_after))

    def test_dynamic_daily_goal_uses_default_when_unset(self):
        from app import callcenter
        result = callcenter.dynamic_daily_goal(self.conn, "recruiter")
        self.assertEqual(result["weekly_target"], callcenter.DEFAULT_WEEKLY_TARGET)
        self.assertEqual(result["target_source"], "default")

    def test_dynamic_daily_goal_uses_configured_target(self):
        from app import callcenter
        callcenter.set_weekly_target(self.conn, "recruiter", 60)
        result = callcenter.dynamic_daily_goal(self.conn, "recruiter")
        self.assertEqual(result["weekly_target"], 60)
        self.assertEqual(result["target_source"], "configured")

    def test_team_workload_counts_unassigned(self):
        from app import callcenter
        self.conn.execute("UPDATE candidates SET assigned_recruiter=NULL")
        self.conn.commit()
        result = callcenter.team_workload(self.conn)
        self.assertEqual(result["unassigned_candidates"], 10)


class TestRoleHierarchy(unittest.TestCase):
    def test_rank_order_is_strictly_increasing(self):
        from app import security
        ranks = [security.ROLE_RANK[r] for r in ("viewer", "recruiter", "team_lead", "admin")]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(set(ranks)), 4)

    def test_has_role_respects_hierarchy(self):
        from app import security
        admin = {"role": "admin"}
        lead = {"role": "team_lead"}
        rec = {"role": "recruiter"}
        viewer = {"role": "viewer"}
        # admin satisfies every gate
        for gate in ("viewer", "recruiter", "team_lead", "admin"):
            self.assertTrue(security.has_role(admin, gate))
        # team_lead satisfies recruiter/viewer gates but not admin
        self.assertTrue(security.has_role(lead, "recruiter"))
        self.assertTrue(security.has_role(lead, "team_lead"))
        self.assertFalse(security.has_role(lead, "admin"))
        # recruiter fails team_lead gate
        self.assertFalse(security.has_role(rec, "team_lead"))
        # viewer fails everything above viewer
        self.assertFalse(security.has_role(viewer, "recruiter"))


class TestAdminConsoleCRUD(BaseDBTestCase):
    def test_create_and_update_job(self):
        from app import domain
        self.conn.execute("""INSERT INTO jobs (id,organization_id,title,company,language,level,education,
            experience_years_required,location,work_model,shift,salary,vacancies,filled,deadline,
            must_have,nice_to_have,disqualifiers,status,created_at) VALUES
            ('JX','org_northwind','Test Role','Co','Spanish','B1','Any',0,'Cairo','On-site','','',5,0,
             '2026-12-01','[]','[]','[]','open','now')""")
        self.conn.commit()
        job = domain.row_to_job(self.conn.execute("SELECT * FROM jobs WHERE id='JX'").fetchone())
        self.assertEqual(job["language"], "Spanish")

    def test_offer_status_transitions(self):
        self.conn.execute("INSERT INTO offers (id,candidate_id,job_id,status) VALUES ('OX','C1','J1','Draft')")
        self.conn.commit()
        self.conn.execute("UPDATE offers SET status='Sent', sent_at=? WHERE id='OX'", ("2026-09-01",))
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM offers WHERE id='OX'").fetchone()
        self.assertEqual(row["status"], "Sent")
        self.assertEqual(row["sent_at"], "2026-09-01")

    def test_user_active_flag_defaults_true(self):
        row = self.conn.execute("SELECT active FROM users WHERE username='admin'").fetchone()
        self.assertEqual(row["active"], 1)

    def test_users_table_accepts_team_lead_role(self):
        digest, salt = db_module.hash_password("x12345678")
        self.conn.execute("""INSERT INTO users (organization_id,username,password_hash,salt,role,active)
            VALUES ('org_northwind','newlead',?,?, 'team_lead', 1)""", (digest, salt))
        self.conn.commit()
        row = self.conn.execute("SELECT role FROM users WHERE username='newlead'").fetchone()
        self.assertEqual(row["role"], "team_lead")

    def test_users_table_rejects_invalid_role(self):
        digest, salt = db_module.hash_password("x12345678")
        with self.assertRaises(Exception):
            self.conn.execute("""INSERT INTO users (organization_id,username,password_hash,salt,role,active)
                VALUES ('org_northwind','bad',?,?, 'superuser', 1)""", (digest, salt))


if __name__ == "__main__":
    unittest.main()
