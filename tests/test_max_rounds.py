"""`gate.max_rounds`: what it counts, and the two ways past it that must both close.

The semantics, stated once:

    max_rounds = the maximum number of EXPLOITABLE rounds recorded in a session.

An exploitable round is one H1 lets become an anchor - ``success``, or
``degraded`` when the scope authority answered. A ``failed`` or ``unusable``
attempt, a quota outage, a technical error before any round: none of them
consume budget, because none of them produced anything a later round could reason
against.

Before 1c the ceiling was checked in exactly one place, ``hooks.stop``. That
closes the automatic path and only that path: Claude can type `crux review`
itself - the Stop hook's own instructions tell it to - and the CLI never
consulted `max_rounds`. Round 3 on a `max_rounds: 2` session needed no bug, only
a session where the hook blocked twice and Claude ran the command once more. That
is the RunningBoard observation, and these tests are what closes it.

Nothing here spawns Codex: `codex.run_many` is stubbed, and the tests that assert
"no Codex" assert it against that stub's call log.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

from ._support import CruxTestCase

from crux import (baseline, cli, codex, config, findings, hooks, intent,
                  paths, report, review, state)

SESSION = "s1"


class CeilingCase(CruxTestCase):
    def setUp(self):
        super().setUp()
        self.write("src/app.py", "APP = 1\n")
        self.commit("init")
        self.cfg = config.load(self.repo)
        state.set_override(SESSION, "code")
        baseline.capture(SESSION, self.repo, self.cfg)
        intent.record_prompt(SESSION, "Change l'application.")
        self.edit("src/app.py", "APP = 2\n")

        self.calls = []
        self.failing = set()
        self._real_run = codex.run_many
        codex.run_many = self.fake_run_many
        self.addCleanup(setattr, codex, "run_many", self._real_run)
        self._real_ensure = codex.ensure_available
        codex.ensure_available = lambda cfg: None
        self.addCleanup(setattr, codex, "ensure_available", self._real_ensure)

    def edit(self, relpath: str, content: str):
        self.write(relpath, content)
        state.record_edit(SESSION, relpath, None)

    def fake_run_many(self, jobs, *args, **kwargs):
        self.calls.append([reviewer for reviewer, _pack in jobs])
        out = []
        for reviewer, _pack in jobs:
            if reviewer in self.failing:
                out.append(codex.CodexOutcome(
                    reviewer=reviewer, error="quota", error_kind=codex.QUOTA,
                    duration=0.1))
                continue
            out.append(codex.CodexOutcome(reviewer=reviewer, payload={
                "verdict": "approve", "reviewer": reviewer,
                "summary": "rien", "findings": []}, duration=0.1))
        return out

    def configure(self, body: str):
        self.write_project_config(body)
        self.cfg = config.load(self.repo)

    def review_once(self, **kwargs):
        return review.run(SESSION, self.repo, self.cfg, **kwargs)

    def advance(self, marker: str):
        """One usable round, on a diff that really moved."""
        self.edit("src/app.py", f"APP = '{marker}'\n")
        return self.review_once()

    def widen(self):
        """Bring a second reviewer in, so a partial failure is expressible.

        On `src/app.py` alone the router selects the always-on reviewer and
        nothing else - and with a single reviewer neither `unusable` nor
        `degraded` can occur, because the scope authority is the only reviewer
        there is.
        """
        self.edit("src/auth/login.py", "PASSWORD_FIELD = 'password'\n")

    def round_files(self):
        directory = paths.session_dir(SESSION) / "findings"
        return sorted(p.name for p in directory.glob("round-*.json")) \
            if directory.is_dir() else []


# ================================================================ counting ===
class WhatCountsAsARound(CeilingCase):
    def test_max_rounds_one_stops_after_the_first(self):
        self.configure("gate:\n  max_rounds: 1\n")
        first = self.advance("a")
        self.assertEqual(first.round, 1)
        self.assertEqual(state.load(SESSION).round, 1)

        before = len(self.calls)
        second = self.advance("b")
        self.assertEqual(second.stop_reason, review.STOP_MAX_ROUNDS)
        self.assertEqual(len(self.calls), before, "un codex exec de trop")
        self.assertEqual(self.round_files(), ["round-1.json"])

    def test_max_rounds_two_allows_exactly_two(self):
        self.configure("gate:\n  max_rounds: 2\n")
        self.advance("a")
        self.advance("b")
        self.assertEqual(state.load(SESSION).round, 2)
        before = len(self.calls)
        third = self.advance("c")
        self.assertEqual(third.stop_reason, review.STOP_MAX_ROUNDS)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.round_files(), ["round-1.json", "round-2.json"])

    def test_max_rounds_three_allows_a_third_targeted_round(self):
        self.configure("gate:\n  max_rounds: 3\n")
        self.advance("a")
        second = self.advance("b")
        third = self.advance("c")
        self.assertEqual(third.round, 3)
        self.assertEqual(second.targeting, review.TARGETING_TARGETED)
        self.assertEqual(third.targeting, review.TARGETING_TARGETED)
        self.assertEqual(state.load(SESSION).round, 3)
        fourth = self.advance("d")
        self.assertEqual(fourth.stop_reason, review.STOP_MAX_ROUNDS)
        self.assertEqual(self.round_files(),
                         ["round-1.json", "round-2.json", "round-3.json"])

    def test_a_failed_attempt_consumes_nothing(self):
        self.configure("gate:\n  max_rounds: 1\n")
        self.failing = {"code-quality", "architecture", "security"}
        result = self.advance("a")
        self.assertEqual(result.attempt_status, state.ATTEMPT_FAILED)
        self.assertEqual(state.load(SESSION).round, 0)

        self.failing = set()
        retry = self.review_once()
        self.assertEqual(retry.round, 1)
        self.assertEqual(state.load(SESSION).round, 1)

    def test_an_unusable_attempt_consumes_nothing(self):
        self.configure("gate:\n  max_rounds: 1\n")
        self.widen()
        self.failing = {"code-quality"}          # the scope authority
        result = self.advance("a")
        self.assertEqual(result.attempt_status, state.ATTEMPT_UNUSABLE)
        self.assertEqual(state.load(SESSION).round, 0)
        self.assertEqual(self.round_files(), [])

    def test_a_degraded_attempt_does_consume_one(self):
        self.configure("gate:\n  max_rounds: 1\n")
        self.widen()
        self.failing = {"security"}              # a secondary only
        result = self.advance("a")
        self.assertEqual(result.attempt_status, state.ATTEMPT_DEGRADED)
        self.assertEqual(state.load(SESSION).round, 1)
        before = len(self.calls)
        self.assertEqual(self.advance("b").stop_reason, review.STOP_MAX_ROUNDS)
        self.assertEqual(len(self.calls), before)

    def test_the_quota_scenario_from_the_spec(self):
        """quota → 0, retry → 1, targeted round → 2, then nothing."""
        self.configure("gate:\n  max_rounds: 2\n")
        self.failing = {"code-quality", "architecture", "security"}
        self.advance("a")
        self.assertEqual(state.load(SESSION).round, 0)

        self.failing = set()
        self.review_once()
        self.assertEqual(state.load(SESSION).round, 1)

        self.advance("b")
        self.assertEqual(state.load(SESSION).round, 2)

        before = len(self.calls)
        capped = self.advance("c")
        self.assertEqual(capped.stop_reason, review.STOP_MAX_ROUNDS)
        self.assertEqual(len(self.calls), before)


# ============================================== the ceiling touches nothing ===
class PastTheCeilingNothingMoves(CeilingCase):
    def setUp(self):
        super().setUp()
        self.configure("gate:\n  max_rounds: 1\n")
        self.advance("a")
        self.snapshot = json.dumps(state.load(SESSION).to_dict(), sort_keys=True)
        self.calls_before = len(self.calls)

    def test_no_codex_call(self):
        self.advance("b")
        self.assertEqual(len(self.calls), self.calls_before)

    def test_the_session_state_is_untouched(self):
        self.advance("b")
        self.assertEqual(
            json.dumps(state.load(SESSION).to_dict(), sort_keys=True),
            self.snapshot)

    def test_no_round_beyond_the_ceiling_is_written(self):
        self.advance("b")
        self.assertEqual(self.round_files(), ["round-1.json"])
        self.assertFalse(
            (paths.session_dir(SESSION) / "findings" / "round-2.json").is_file())

    def test_the_reported_round_is_the_one_actually_reached(self):
        result = self.advance("b")
        self.assertEqual(result.round, 1)

    def test_the_report_explains_the_ceiling(self):
        result = self.advance("b")
        text = report.render_markdown(result, SESSION, self.cfg)
        self.assertIn("max_rounds", text)
        self.assertIn("restent à arbitrer", text)

    def test_it_does_not_read_as_an_empty_review(self):
        """Nothing was computed, so no counts line: `0 fichier(s) · reviewers :
        aucun` would describe a review that found nothing rather than one that
        never happened."""
        result = self.advance("b")
        text = report.render_markdown(result, SESSION, self.cfg)
        self.assertNotIn("reviewers : aucun", text)
        self.assertNotIn("0 fichier(s)", text)

    def test_a_round_that_ran_and_hit_the_ceiling_keeps_its_counts(self):
        summary = report.render_markdown(
            review.run(SESSION, self.repo, self.cfg, dry_run=True),
            SESSION, self.cfg)
        self.assertIn("max_rounds", summary)
        # And the round that actually ran, one call earlier, kept its header.
        self.configure("gate:\n  max_rounds: 2\n")
        result = self.advance("c")
        self.assertTrue(result.executed)
        text = report.render_markdown(result, SESSION, self.cfg)
        self.assertIn("fichier(s)", text)
        self.assertIn("code-quality", text)

    def test_a_dry_run_past_the_ceiling_runs_nothing_either(self):
        self.advance("b")
        result = self.review_once(dry_run=True)
        self.assertEqual(result.stop_reason, review.STOP_MAX_ROUNDS)
        self.assertEqual(len(self.calls), self.calls_before)

    def test_an_override_is_not_a_way_past_it(self):
        for kwargs in ({"select_all": True}, {"only": ["security"]},
                       {"add": ["security"]}):
            with self.subTest(**kwargs):
                result = self.review_once(**kwargs)
                self.assertEqual(result.stop_reason, review.STOP_MAX_ROUNDS)
        self.assertEqual(len(self.calls), self.calls_before)


# ==================================================== the two ways in, closed ===
class TheManualCliIsNotABypass(CeilingCase):
    """The RunningBoard hole: `crux review` typed by Claude after the hook stops."""

    def setUp(self):
        super().setUp()
        self.configure("gate:\n  max_rounds: 2\n")
        self._cwd = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self._cwd)

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(list(argv))
        return code, buffer.getvalue()

    def stop_hook(self, **extra):
        payload = json.dumps({"session_id": SESSION, "cwd": str(self.repo),
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

    def test_the_cli_refuses_a_third_round_and_says_so(self):
        self.advance("a")
        self.advance("b")
        before = len(self.calls)
        self.edit("src/app.py", "APP = 'c'\n")
        code, out = self.run_cli("review", "--session", SESSION)
        self.assertEqual(len(self.calls), before, "le CLI a relancé Codex")
        self.assertIn("max_rounds", out)
        self.assertEqual(code, cli.EXIT_OK)

    def test_the_stop_hook_stays_silent_past_the_ceiling(self):
        self.advance("a")
        self.advance("b")
        self.edit("src/app.py", "APP = 'c'\n")
        self.assertEqual(self.stop_hook().strip(), "")
        self.assertEqual(len(self.calls), 2)

    def test_the_hook_and_the_cli_agree_on_the_same_ceiling(self):
        """One round left: the hook asks, the CLI runs, then both stop."""
        self.advance("a")
        self.edit("src/app.py", "APP = 'b'\n")
        self.assertIn("crux review", self.stop_hook())
        code, _ = self.run_cli("review", "--session", SESSION)
        self.assertEqual(state.load(SESSION).round, 2)
        self.edit("src/app.py", "APP = 'c'\n")
        self.assertEqual(self.stop_hook().strip(), "")
        before = len(self.calls)
        self.run_cli("review", "--session", SESSION)
        self.assertEqual(len(self.calls), before)

    def test_the_summary_is_available_after_the_ceiling(self):
        self.advance("a")
        self.advance("b")
        _code, out = self.run_cli("report", "--session", SESSION, "--summary")
        self.assertIn("rounds exploitables : 2 / 2", out)
        self.assertIn("reviewer executions:", out)


# =========================================================== D4 outlives it ===
class D4SurvivesTheCeiling(CeilingCase):
    """A blocking finding already on disk is local state, not a review budget."""

    def setUp(self):
        super().setUp()
        self.configure("gate:\n  max_rounds: 1\n")
        findings.save_round(
            SESSION, 1,
            [findings.Finding(id="R1F1", reviewer="code-quality",
                              severity="high", title="Index hors bornes",
                              description="d", file="src/app.py")],
            {}, selected=["code-quality"], scope_authority="code-quality")
        st = state.load(SESSION)
        st.record_successful_round(1, "fp")
        state.save(st)

    def stop_hook(self):
        payload = json.dumps({"session_id": SESSION, "cwd": str(self.repo)})
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

    def test_the_obligation_still_blocks_at_the_ceiling(self):
        out = self.stop_hook()
        self.assertIn("R1F1", out)
        self.assertIn("aucune nouvelle review", out.lower())

    def test_arbitrating_it_needs_no_codex(self):
        before = len(self.calls)
        findings.resolve(SESSION, "R1F1", "rejected", reason="faux positif")
        self.assertEqual(self.stop_hook().strip(), "")
        self.assertEqual(len(self.calls), before)

    def test_review_past_the_ceiling_points_at_it_without_running(self):
        self.edit("src/app.py", "APP = 'z'\n")
        result = self.review_once()
        self.assertEqual(result.stop_reason, review.STOP_MAX_ROUNDS)
        self.assertEqual(self.calls, [])
        text = report.render_markdown(result, SESSION, self.cfg)
        self.assertIn("restent à arbitrer", text)


if __name__ == "__main__":
    import unittest
    unittest.main()
