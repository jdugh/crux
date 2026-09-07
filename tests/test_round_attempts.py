"""H1 and D4: what a failed attempt costs, and what a blocking finding owes.

Two guarantees that must hold together without contradicting each other:

  H1  A technical failure of the reviewers must not consume a round, and must
      not make the Stop hook ask for the same diff again - that would spin
      Stop -> review -> quota -> Stop, the wedged session I1 forbids.

  D4  A blocking finding that *was* recorded must receive an explicit
      disposition from Claude. No Codex run is needed to require it: it is local
      state already established, so neither `max_rounds`, nor the budget, nor
      `crux off` retires it. Claude may always reject it with a reason.
"""

from __future__ import annotations

import json

from ._support import CruxTestCase

from crux import (baseline, codex, config, decisions, findings, hooks, intent,
                  paths, review, state)


def blocking_finding(**kwargs) -> findings.Finding:
    base = dict(id="R1F1", reviewer="code-quality", severity="high",
                title="Index hors bornes", description="d", file="a.py")
    base.update(kwargs)
    return findings.Finding(**base)


# --------------------------------------------------------------------- H1 ---
class AttemptClassification(CruxTestCase):
    """The pivot is the scope authority: without it there is no drift verdict."""

    def results(self, spec):
        return [findings.ReviewerResult(reviewer=name, ok=ok)
                for name, ok in spec]

    def test_no_reviewer_answered(self):
        self.assertEqual(
            review.classify_attempt(
                self.results([("code-quality", False), ("security", False)]),
                "code-quality"),
            state.ATTEMPT_FAILED)

    def test_scope_authority_silent_is_unusable(self):
        self.assertEqual(
            review.classify_attempt(
                self.results([("code-quality", False), ("security", True)]),
                "code-quality"),
            state.ATTEMPT_UNUSABLE)

    def test_authority_answered_secondary_failed_is_degraded(self):
        self.assertEqual(
            review.classify_attempt(
                self.results([("code-quality", True), ("security", False)]),
                "code-quality"),
            state.ATTEMPT_DEGRADED)

    def test_everyone_answered_is_success(self):
        self.assertEqual(
            review.classify_attempt(
                self.results([("code-quality", True), ("security", True)]),
                "code-quality"),
            state.ATTEMPT_SUCCESS)

    def test_only_success_and_degraded_are_usable(self):
        self.assertEqual(set(state.ATTEMPT_USABLE),
                         {state.ATTEMPT_SUCCESS, state.ATTEMPT_DEGRADED})

    def test_unusable_outcomes_do_not_auto_retry(self):
        self.assertEqual(set(state.ATTEMPT_NO_AUTO_RETRY),
                         {state.ATTEMPT_UNUSABLE, state.ATTEMPT_FAILED})


class SessionStateMigration(CruxTestCase):
    def test_a_v01_state_file_keeps_its_fingerprint_as_the_successful_one(self):
        st = state.SessionState.from_dict(
            {"session_id": "s1", "round": 1, "last_diff_fingerprint": "abc"})
        self.assertEqual(st.last_successful_diff_fingerprint, "abc")

    def test_the_legacy_field_is_still_written_for_a_downgrade(self):
        st = state.SessionState(session_id="s1")
        st.record_successful_round(2, "abc")
        self.assertEqual(st.to_dict()["last_diff_fingerprint"], "abc")
        self.assertEqual(st.to_dict()["last_successful_diff_fingerprint"], "abc")

    def test_recording_an_attempt_does_not_touch_the_round(self):
        st = state.SessionState(session_id="s1", round=1)
        st.record_attempt("abc", state.ATTEMPT_FAILED)
        self.assertEqual(st.round, 1)
        self.assertIsNone(st.last_successful_diff_fingerprint)
        self.assertEqual(st.last_attempt_fingerprint, "abc")
        self.assertTrue(st.last_attempt_at)


class FailedAttemptCostsNoRound(CruxTestCase):
    """The dogfooding regression: a quota outage used to burn half the budget."""

    def setUp(self):
        super().setUp()
        self.write("src/app.py", "APP = 1\n")
        self.commit("init")
        self.cfg = config.load(self.repo)
        baseline.capture("s1", self.repo, self.cfg)
        intent.record_prompt("s1", "Ajoute un compteur.")
        self.write("src/app.py", "APP = 2\n")
        state.record_edit("s1", "src/app.py", None)
        self.outcomes = {}

        real_run_many, real_ensure = codex.run_many, codex.ensure_available
        codex.run_many = self.fake_run_many
        codex.ensure_available = lambda cfg: None
        self.addCleanup(setattr, codex, "run_many", real_run_many)
        self.addCleanup(setattr, codex, "ensure_available", real_ensure)

    def fake_run_many(self, jobs, *args, **kwargs):
        out = []
        for reviewer, _prompt in jobs:
            if self.outcomes.get(reviewer, True):
                out.append(codex.CodexOutcome(reviewer=reviewer, duration=0.1, payload={
                    "verdict": "changes_requested", "reviewer": reviewer,
                    "summary": "s",
                    "findings": [{"severity": "high", "confidence": "high",
                                  "title": f"Remarque de {reviewer}",
                                  "description": "d", "file": "src/app.py",
                                  "line": 1}]}))
            else:
                out.append(codex.CodexOutcome(
                    reviewer=reviewer, duration=0.1, error_kind="quota",
                    error="quota ou limite de débit atteinte"))
        return out

    def fail(self, *reviewers):
        for name in reviewers:
            self.outcomes[name] = False

    def test_a_total_failure_does_not_advance_the_round(self):
        self.fail("code-quality", "architecture", "security", "tests",
                  "performance", "ux", "release")
        result = review.run("s1", self.repo, self.cfg)
        self.assertEqual(result.attempt_status, state.ATTEMPT_FAILED)
        self.assertFalse(result.usable)
        self.assertEqual(state.load("s1").round, 0)

    def test_a_total_failure_writes_no_round_journal(self):
        self.fail("code-quality", "architecture", "security", "tests",
                  "performance", "ux", "release")
        review.run("s1", self.repo, self.cfg)
        self.assertEqual(findings.load_rounds("s1"), [])

    def test_a_total_failure_leaves_the_successful_fingerprint_alone(self):
        self.fail("code-quality", "architecture", "security", "tests",
                  "performance", "ux", "release")
        review.run("s1", self.repo, self.cfg)
        self.assertIsNone(state.load("s1").last_successful_diff_fingerprint)

    def test_a_total_failure_records_the_attempt(self):
        self.fail("code-quality", "architecture", "security", "tests",
                  "performance", "ux", "release")
        result = review.run("s1", self.repo, self.cfg)
        st = state.load("s1")
        self.assertEqual(st.last_attempt_status, state.ATTEMPT_FAILED)
        self.assertEqual(st.last_attempt_fingerprint, result.diff_fingerprint)

    def test_a_total_failure_keeps_the_run_directory_for_diagnosis(self):
        self.fail("code-quality", "architecture", "security", "tests",
                  "performance", "ux", "release")
        result = review.run("s1", self.repo, self.cfg)
        self.assertTrue((paths.runs_root() / result.run_id / "run.json").is_file())
        self.assertTrue((paths.runs_root() / result.run_id / "report.md").is_file())

    def test_a_silent_scope_authority_makes_the_round_unusable(self):
        self.fail("code-quality")
        result = review.run("s1", self.repo, self.cfg)
        self.assertEqual(result.attempt_status, state.ATTEMPT_UNUSABLE)
        self.assertEqual(state.load("s1").round, 0)
        self.assertEqual(findings.load_rounds("s1"), [])

    def test_an_unusable_round_opens_no_human_decision(self):
        """A technical failure must never become an obligation."""
        def partial(jobs, *args, **kwargs):
            return [codex.CodexOutcome(
                reviewer=reviewer, duration=0.1,
                error_kind="quota", error="quota") if reviewer == "code-quality"
                else codex.CodexOutcome(reviewer=reviewer, duration=0.1, payload={
                    "verdict": "changes_requested", "reviewer": reviewer,
                    "summary": "s",
                    "findings": [{"severity": "high", "confidence": "high",
                                  "title": "Retrait de capacité",
                                  "description": "d", "file": "src/app.py",
                                  "line": 1,
                                  "requires_human_decision": True}]})
                for reviewer, _prompt in jobs]
        codex.run_many = partial
        review.run("s1", self.repo, self.cfg)
        self.assertEqual(decisions.load_all("s1"), [])

    def test_a_degraded_round_is_recorded_with_its_failed_reviewers(self):
        # `add` survives the micro-change filter, so the failing reviewer is
        # genuinely selected - otherwise the test would pass on a full success.
        self.fail("architecture")
        result = review.run("s1", self.repo, self.cfg, add=["architecture"])
        self.assertIn("architecture", result.selected)
        self.assertEqual(result.attempt_status, state.ATTEMPT_DEGRADED)
        self.assertTrue(result.usable)
        self.assertEqual(state.load("s1").round, 1)
        record = findings.load_rounds("s1")[0]
        self.assertIn("architecture", record["failed_reviewers"])
        self.assertTrue(record["degraded"])

    def test_a_full_success_is_recorded_as_such(self):
        result = review.run("s1", self.repo, self.cfg)
        self.assertEqual(result.attempt_status, state.ATTEMPT_SUCCESS)
        record = findings.load_rounds("s1")[0]
        self.assertEqual(record["failed_reviewers"], [])
        self.assertFalse(record["degraded"])

    def test_the_report_says_the_round_did_not_happen(self):
        self.fail("code-quality", "architecture", "security", "tests",
                  "performance", "ux", "release")
        result = review.run("s1", self.repo, self.cfg)
        rendered = (paths.runs_root() / result.run_id / "report.md").read_text(
            encoding="utf-8")
        self.assertIn("Ce round n'a pas eu lieu", rendered)
        self.assertIn("panne technique", rendered)


class StopDoesNotSpinOnAFailedAttempt(CruxTestCase):
    """The loop H1 exists to prevent."""

    def setUp(self):
        super().setUp()
        self.write("src/app.py", "APP = 1\n")
        self.commit("init")
        self.cfg = config.load(self.repo)
        baseline.capture("s1", self.repo, self.cfg)
        self.write("src/app.py", "APP = 2\n")
        state.record_edit("s1", "src/app.py", None)
        self.fingerprint = baseline.compute("s1", self.repo, self.cfg).fingerprint

    def stop(self):
        from . import _support  # noqa: F401
        import io
        import sys
        payload = json.dumps({"session_id": "s1", "cwd": str(self.repo)})
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin = io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")),
                                     encoding="utf-8")
        sys.stdout = captured = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        try:
            hooks.stop()
            captured.flush()
            captured.buffer.seek(0)
            return captured.buffer.read().decode("utf-8")
        finally:
            sys.stdin, sys.stdout = stdin, stdout

    def arm(self):
        import os
        os.environ["CRUX_GATE"] = "code"
        self.addCleanup(os.environ.pop, "CRUX_GATE", None)

    def test_a_fresh_diff_is_asked_to_be_reviewed(self):
        self.arm()
        self.assertIn("crux review", self.stop())

    def test_the_same_diff_is_not_asked_again_after_a_failed_attempt(self):
        self.arm()
        st = state.load("s1")
        st.record_attempt(self.fingerprint, state.ATTEMPT_FAILED)
        state.save(st)
        self.assertEqual(self.stop().strip(), "")

    def test_the_same_diff_is_not_asked_again_after_an_unusable_attempt(self):
        self.arm()
        st = state.load("s1")
        st.record_attempt(self.fingerprint, state.ATTEMPT_UNUSABLE)
        state.save(st)
        self.assertEqual(self.stop().strip(), "")

    def test_a_new_edit_lifts_the_hold(self):
        self.arm()
        st = state.load("s1")
        st.record_attempt(self.fingerprint, state.ATTEMPT_FAILED)
        state.save(st)
        self.write("src/app.py", "APP = 3\n")
        state.record_edit("s1", "src/app.py", None)
        self.assertIn("crux review", self.stop())

    def test_a_successful_round_still_stops_the_re_review(self):
        self.arm()
        st = state.load("s1")
        st.record_successful_round(1, self.fingerprint)
        st.record_attempt(self.fingerprint, state.ATTEMPT_SUCCESS)
        state.save(st)
        self.assertEqual(self.stop().strip(), "")


# --------------------------------------------------------------------- D4 ---
class UnarbitratedFindings(CruxTestCase):
    def test_a_recorded_blocking_finding_is_unarbitrated(self):
        findings.save_round("s1", 1, [blocking_finding()], {})
        self.assertEqual([f.id for f in findings.unarbitrated("s1", "high")],
                         ["R1F1"])

    def test_any_explicit_disposition_settles_it(self):
        for status, reason in (("accepted", ""), ("rejected", "faux positif"),
                               ("deferred", "hors demande")):
            with self.subTest(status=status):
                session = f"s-{status}"
                findings.save_round(session, 1, [blocking_finding()], {})
                findings.resolve(session, "R1F1", status, reason=reason)
                self.assertEqual(findings.unarbitrated(session, "high"), [])

    def test_a_non_blocking_finding_owes_nothing(self):
        findings.save_round("s1", 1, [blocking_finding(severity="medium")], {})
        self.assertEqual(findings.unarbitrated("s1", "high"), [])

    def test_block_on_moves_the_floor(self):
        findings.save_round("s1", 1, [blocking_finding(severity="medium")], {})
        self.assertEqual([f.id for f in findings.unarbitrated("s1", "medium")],
                         ["R1F1"])

    def test_a_promoted_finding_belongs_to_the_human_not_to_claude(self):
        promoted = blocking_finding(requires_human_decision=True)
        findings.save_round("s1", 1, [promoted], {"R1F1": "D1"})
        self.assertEqual(findings.unarbitrated("s1", "high"), [])

    def test_it_spans_every_recorded_round(self):
        findings.save_round("s1", 1, [blocking_finding(id="R1F1")], {})
        findings.save_round("s1", 2, [blocking_finding(id="R2F1",
                                                       file="b.py")], {})
        findings.resolve("s1", "R1F1", "accepted")
        self.assertEqual([f.id for f in findings.unarbitrated("s1", "high")],
                         ["R2F1"])

    def test_no_journal_means_nothing_is_owed(self):
        self.assertEqual(findings.unarbitrated("never-reviewed", "high"), [])


class StopRequiresArbitration(CruxTestCase):
    def setUp(self):
        super().setUp()
        self.commit_initial()
        findings.save_round("s1", 1, [blocking_finding()], {})

    def commit_initial(self):
        self.write("a.py", "A = 1\n")
        self.commit("init")

    def stop(self, **extra):
        import io
        import sys
        payload = json.dumps({"session_id": "s1", "cwd": str(self.repo),
                              **extra})
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin = io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")),
                                     encoding="utf-8")
        sys.stdout = captured = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        try:
            hooks.stop()
            captured.flush()
            captured.buffer.seek(0)
            return captured.buffer.read().decode("utf-8")
        finally:
            sys.stdin, sys.stdout = stdin, stdout

    def test_it_blocks_and_names_the_finding(self):
        out = self.stop()
        self.assertIn('"block"', out)
        self.assertIn("R1F1", out)

    def test_it_says_claude_may_reject(self):
        """Codex gains no decisional power; silence is what is refused."""
        out = self.stop()
        self.assertIn("rejected", out)
        self.assertIn("autorité technique", out)

    def test_it_asks_for_no_new_review(self):
        out = self.stop()
        self.assertNotIn("crux review", out)

    def test_an_arbitrated_finding_lets_the_turn_end(self):
        findings.resolve("s1", "R1F1", "rejected", reason="faux positif")
        self.assertEqual(self.stop().strip(), "")

    def test_crux_off_does_not_retire_the_obligation(self):
        state.set_override("s1", "off")
        self.assertIn("R1F1", self.stop())

    def test_the_round_budget_does_not_retire_the_obligation(self):
        self.write_project_config("gate:\n  max_rounds: 1\n")
        st = state.load("s1")
        st.round = 5
        state.save(st)
        self.assertIn("R1F1", self.stop())

    def test_the_time_budget_does_not_retire_the_obligation(self):
        self.write_project_config("gate:\n  budget_seconds: 1\n")
        st = state.load("s1")
        st.budget_spent = 9000.0
        state.save(st)
        self.assertIn("R1F1", self.stop())

    def test_stop_hook_active_does_not_retire_the_obligation(self):
        self.assertIn("R1F1", self.stop(stop_hook_active=True))

    def test_the_kill_switch_still_lifts_everything(self):
        import os
        os.environ["CRUX_DISABLE"] = "1"
        self.addCleanup(os.environ.pop, "CRUX_DISABLE", None)
        self.assertEqual(self.stop().strip(), "")

    def test_a_pending_human_decision_still_comes_first(self):
        decisions.propose("s1", title="Retrait de HEIC", why="w")
        out = self.stop()
        self.assertIn("D1", out)
        self.assertNotIn("R1F1", out)

    def test_an_unreadable_ledger_falls_open(self):
        """I1: a read failure is a technical failure, and never wedges a turn."""
        real = findings.unarbitrated

        def boom(*args, **kwargs):
            raise OSError("disque illisible")

        findings.unarbitrated = boom
        self.addCleanup(setattr, findings, "unarbitrated", real)
        self.assertEqual(self.stop().strip(), "")
