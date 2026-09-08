"""1c: the planner wired into the real review loop.

What 1b proved on paper - a delta, named triggers, a cap that spares the scope
authority - is asserted here against ``review.run()`` with Codex stubbed at the
``run_many`` boundary. The load-bearing assertion is always the same one, and it
is about what did NOT happen: ``self.calls[-1]`` is the exact list of reviewers
that reached ``codex.run_many``, so a reviewer the plan skipped costs zero
``codex exec``.

No test in this file starts a real Codex process. ``codex.ensure_available`` and
``codex.run_many`` are replaced in ``setUp``; anything that reached the real ones
would fail on a machine without the CLI, which is exactly the isolation the
ROADMAP asks for.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

from ._support import CruxTestCase

from crux import (baseline, cli, codex, config, context, decisions, findings,
                  intent, paths, report, review, round2, state)

SESSION = "s1"


def payload(reviewer: str, *, verdict: str = "changes_requested",
            findings_list=None, scope=None) -> dict:
    body = {
        "verdict": verdict,
        "reviewer": reviewer,
        "summary": f"résumé {reviewer}",
        "findings": list(findings_list or []),
    }
    if scope is not None:
        body["scope"] = scope
    return body


def one_finding(title: str, file: str = "src/app.py", severity: str = "high",
                **extra) -> dict:
    base = {"severity": severity, "confidence": "high", "title": title,
            "description": "Description technique.", "file": file, "line": 1}
    base.update(extra)
    return base


class LoopCase(CruxTestCase):
    """A repo whose session diff selects all three shipped personas.

    ``always`` gives ``code-quality`` 100, the auth path gives ``security`` 90,
    and the SQL migration gives ``architecture`` 80 - so round 1 has three
    reviewers and a targeted round 2 has something to leave out.
    """

    def setUp(self):
        super().setUp()
        self.write("src/app.py", "APP = 1\n")
        self.commit("init")
        self.cfg = config.load(self.repo)
        baseline.capture(SESSION, self.repo, self.cfg)
        intent.record_prompt(SESSION, "Ajoute la connexion et la migration.")

        self.edit("src/app.py", "APP = 2\n")
        self.edit("src/auth/login.py", "PASSWORD_FIELD = 'password'\n")
        self.edit("migrations/001.sql", "CREATE TABLE t (id int);\n")

        # Codex, entirely stubbed. `calls` is the record every assertion in this
        # file is really about: one entry per `run_many`, holding exactly the
        # reviewers that would have spawned a `codex exec`.
        self.calls = []
        self.packs = {}
        self.responses = {}
        self.failing = set()
        self._real_run = codex.run_many
        codex.run_many = self.fake_run_many
        self.addCleanup(setattr, codex, "run_many", self._real_run)
        self._real_ensure = codex.ensure_available
        codex.ensure_available = lambda cfg: None
        self.addCleanup(setattr, codex, "ensure_available", self._real_ensure)

    # ------------------------------------------------------------- helpers ---
    def edit(self, relpath: str, content: str, session: str = SESSION):
        self.write(relpath, content)
        state.record_edit(session, relpath, None)

    def fake_run_many(self, jobs, *args, **kwargs):
        names = [reviewer for reviewer, _pack in jobs]
        self.calls.append(names)
        self.packs = {reviewer: pack for reviewer, pack in jobs}
        out = []
        for reviewer, _pack in jobs:
            if reviewer in self.failing:
                out.append(codex.CodexOutcome(
                    reviewer=reviewer, error="timeout",
                    error_kind=codex.TIMEOUT, duration=0.1))
                continue
            body = self.responses.get(reviewer)
            if body is None:
                body = payload(reviewer, verdict="approve")
            out.append(codex.CodexOutcome(reviewer=reviewer, payload=body,
                                          duration=0.1))
        return out

    def respond(self, **by_reviewer):
        self.responses = dict(by_reviewer)

    def run_review(self, session: str = SESSION, **kwargs):
        return review.run(session, self.repo, self.cfg, **kwargs)

    def last_call(self):
        return self.calls[-1] if self.calls else []


# ============================================================ round 1 vs N ===
class RoundOneIsUnchanged(LoopCase):
    def test_round_one_uses_the_v01_router(self):
        result = self.run_review()
        self.assertEqual(result.round, 1)
        self.assertEqual(result.targeting, review.TARGETING_ROUTER)
        self.assertEqual(result.plan_explain, "")
        self.assertEqual(result.delta_paths, [])

    def test_round_one_selects_the_three_personas(self):
        result = self.run_review()
        self.assertEqual(sorted(result.selected),
                         ["architecture", "code-quality", "security"])
        self.assertEqual(sorted(self.last_call()), sorted(result.selected))

    def test_round_one_avoids_nobody(self):
        """Nothing is claimed as saved when nothing was."""
        result = self.run_review()
        self.assertEqual(result.avoided, [])
        self.assertEqual(result.skipped, [])

    def test_the_authority_still_carries_the_intent(self):
        self.run_review()
        self.assertIn("AUTORITÉ DE PÉRIMÈTRE", self.packs["code-quality"])
        self.assertIn("Ajoute la connexion", self.packs["code-quality"])
        self.assertNotIn("AUTORITÉ DE PÉRIMÈTRE", self.packs["security"])


class RoundTwoIsTargeted(LoopCase):
    """Round 1, an arbitration, an edit - then only what the delta justifies."""

    def first_round(self, **responses):
        self.respond(**responses)
        return self.run_review()

    def test_round_two_uses_the_planner(self):
        self.first_round()
        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review()
        self.assertEqual(result.round, 2)
        self.assertEqual(result.targeting, review.TARGETING_TARGETED)
        self.assertIn("Plan round 2", result.plan_explain)
        self.assertEqual(result.delta_paths, ["src/app.py"])

    def test_only_the_selected_reviewers_reach_codex(self):
        self.first_round()
        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review()
        self.assertEqual(self.last_call(), result.selected)
        self.assertEqual(result.executed, result.selected)

    def test_a_skipped_reviewer_costs_no_codex_call(self):
        """The whole point of the milestone, stated as an assertion."""
        self.first_round()
        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review()
        skipped = {row["reviewer"] for row in result.skipped}
        self.assertTrue(skipped, "le ciblage n'a écarté personne")
        for name in skipped:
            self.assertNotIn(name, self.last_call())
        self.assertTrue(all(row["reason"] for row in result.skipped))

    def test_an_override_that_revives_a_skipped_reviewer_clears_its_row(self):
        """`skipped` must mean "did not run", everywhere it is read.

        Found by Crux itself during the 1c dogfooding round: the rows came from
        the plan and were never reconciled with the selection, so `--all` could
        run a reviewer while the report said it had been spared and the session
        summary counted the call as a saving.
        """
        self.first_round()
        self.edit("src/app.py", "APP = 3\n")
        plain = self.run_review(dry_run=True)
        spared = {row["reviewer"] for row in plain.skipped}
        self.assertTrue(spared)

        result = self.run_review(select_all=True)
        for name in spared:
            self.assertIn(name, self.last_call())
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.avoided, [])
        summary = report.render_session_summary(SESSION, self.cfg)
        self.assertIn("reviewer executions avoided by targeting: 0", summary)

    def test_the_avoided_count_matches_what_v01_would_have_run(self):
        self.first_round()
        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review()
        self.assertEqual(
            set(result.avoided),
            set(result.would_run) - set(result.executed))
        self.assertTrue(result.avoided)
        for name in result.avoided:
            self.assertNotIn(name, self.last_call())

    def test_round_three_targets_round_two(self):
        self.write_project_config("gate:\n  max_rounds: 3\n")
        self.cfg = config.load(self.repo)
        self.first_round()
        self.edit("src/app.py", "APP = 3\n")
        self.run_review()
        self.edit("src/app.py", "APP = 4\n")
        result = self.run_review()
        self.assertEqual(result.round, 3)
        self.assertEqual(result.targeting, review.TARGETING_TARGETED)
        self.assertIn("Plan round 3", result.plan_explain)


class TheFallback(LoopCase):
    def test_an_unreadable_journal_falls_back_to_the_full_router(self):
        self.run_review()
        journal = paths.session_dir(SESSION) / "findings" / "round-1.json"
        journal.write_text("{ pas du json", encoding="utf-8")
        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review()
        self.assertEqual(result.targeting, review.TARGETING_ROUTER)
        self.assertTrue(any("repli" in w for w in result.warnings))
        self.assertEqual(sorted(result.selected),
                         ["architecture", "code-quality", "security"])

    def test_a_planner_crash_falls_back_rather_than_wedging(self):
        self.run_review()
        real = round2.build

        def boom(*args, **kwargs):
            raise RuntimeError("planificateur cassé")

        round2.build = boom
        self.addCleanup(setattr, round2, "build", real)
        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review()
        self.assertEqual(result.targeting, review.TARGETING_ROUTER)
        self.assertTrue(any("planificateur" in w for w in result.warnings))
        self.assertTrue(result.selected)

    def test_the_fallback_still_honours_max_rounds(self):
        """A degraded selection is not a licence to keep going."""
        self.run_review()
        journal = paths.session_dir(SESSION) / "findings" / "round-1.json"
        journal.write_text("{ pas du json", encoding="utf-8")
        self.edit("src/app.py", "APP = 3\n")
        self.run_review()                      # round 2, via the fallback
        self.edit("src/app.py", "APP = 4\n")
        before = len(self.calls)
        result = self.run_review()
        self.assertEqual(result.stop_reason, review.STOP_MAX_ROUNDS)
        self.assertEqual(len(self.calls), before)

    def test_the_fallback_keeps_a_scope_authority(self):
        self.run_review()
        (paths.session_dir(SESSION) / "findings" / "round-1.json").write_text(
            "nope", encoding="utf-8")
        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review()
        self.assertEqual(result.scope_authority, "code-quality")
        self.assertIn("code-quality", self.last_call())


# ============================================================== triggers ===
class Triggers(LoopCase):
    """Each 1b trigger, observed through what actually runs."""

    def round_one_with(self, **responses):
        self.respond(**responses)
        return self.run_review()

    def plan_for_next_round(self):
        diff = baseline.compute(SESSION, self.repo, self.cfg)
        return round2.build(SESSION, self.repo, self.cfg,
                            extra_paths=list(diff.files))

    def test_accepted_plus_delta_triggers_fix_verification(self):
        self.round_one_with(security=payload(
            "security", findings_list=[one_finding("Mot de passe en clair",
                                                   file="src/auth/login.py")]))
        finding_id = findings.load_rounds(SESSION)[0]["findings"][0]["id"]
        findings.resolve(SESSION, finding_id, "accepted")
        self.edit("src/auth/login.py", "PASSWORD_FIELD = 'pwd'\n")
        plan = self.plan_for_next_round()
        self.assertIn(round2.T_FIX_VERIFICATION, plan.triggers_for("security"))
        result = self.run_review()
        self.assertIn("security", self.last_call())

    def test_rejected_contests_the_reviewer(self):
        self.round_one_with(architecture=payload(
            "architecture", findings_list=[one_finding(
                "Migration non réversible", file="migrations/001.sql")]))
        finding_id = findings.load_rounds(SESSION)[0]["findings"][0]["id"]
        findings.resolve(SESSION, finding_id, "rejected", reason="faux positif")
        self.edit("src/app.py", "APP = 3\n")
        plan = self.plan_for_next_round()
        self.assertIn(round2.T_CONTESTED, plan.triggers_for("architecture"))
        self.run_review()
        self.assertIn("architecture", self.last_call())

    def test_unanswered_contests_the_reviewer(self):
        self.round_one_with(architecture=payload(
            "architecture", findings_list=[one_finding(
                "Migration non réversible", file="migrations/001.sql")]))
        self.edit("src/app.py", "APP = 3\n")
        plan = self.plan_for_next_round()
        self.assertIn(round2.T_CONTESTED, plan.triggers_for("architecture"))

    def test_deferred_alone_does_not_bring_a_reviewer_back(self):
        self.round_one_with(architecture=payload(
            "architecture", findings_list=[one_finding(
                "Nommage discutable", file="migrations/001.sql",
                severity="low")]))
        finding_id = findings.load_rounds(SESSION)[0]["findings"][0]["id"]
        findings.resolve(SESSION, finding_id, "deferred", reason="plus tard")
        # The delta touches a file that calls for nobody in particular.
        self.edit("src/app.py", "APP = 3\n")
        plan = self.plan_for_next_round()
        row = plan.row("architecture")
        self.assertFalse(row.selected)
        self.assertEqual(row.exclusion_reason, round2.X_DEFERRED_ONLY)
        self.run_review()
        self.assertNotIn("architecture", self.last_call())

    def test_new_surface_brings_a_reviewer_back(self):
        """A file the delta creates, calling for a reviewer round 1 never used."""
        self.write_project_config(
            "reviewers:\n  always: [code-quality]\n  auto: [architecture]\n")
        self.cfg = config.load(self.repo)
        self.run_review()
        self.assertNotIn("security", self.calls[-1])
        self.write_project_config(
            "reviewers:\n  always: [code-quality]\n"
            "  auto: [architecture, security]\n")
        self.cfg = config.load(self.repo)
        self.edit("src/auth/token.py", "API_KEY = 'x'\nimport subprocess\n")
        plan = self.plan_for_next_round()
        self.assertIn(round2.T_NEW_SURFACE, plan.triggers_for("security"))
        self.run_review()
        self.assertIn("security", self.last_call())

    def test_a_failed_secondary_is_re_asked_next_round(self):
        """Its silence must never read as an approve (§12)."""
        self.failing = {"security"}
        result = self.run_review()
        self.assertEqual(result.attempt_status, state.ATTEMPT_DEGRADED)
        self.assertEqual(result.failed_reviewers, ["security"])
        self.failing = set()
        self.edit("src/app.py", "APP = 3\n")
        plan = self.plan_for_next_round()
        self.assertIn(round2.T_PREVIOUS_FAILURE, plan.triggers_for("security"))
        self.run_review()
        self.assertIn("security", self.last_call())


# ========================================================= scope authority ===
class ScopeAuthorityIsNotNegotiable(LoopCase):
    def test_only_cannot_evict_it(self):
        """`crux review --only security` runs security AND code-quality."""
        result = self.run_review(only=["security"])
        self.assertEqual(result.scope_authority, "code-quality")
        self.assertIn("code-quality", result.selected)
        self.assertIn("security", result.selected)
        self.assertIn("code-quality", self.last_call())
        self.assertTrue(any("réintroduite" in n
                            for n in result.override_notes))

    def test_only_on_a_targeted_round_cannot_evict_it_either(self):
        self.run_review()
        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review(only=["security"])
        self.assertEqual(result.scope_authority, "code-quality")
        self.assertIn("code-quality", self.last_call())

    def test_add_widens_without_touching_the_authority(self):
        """`--add` reaches past the cap. It stays inside the configured pool:
        a reviewer in neither `always` nor `auto` was never a candidate, and
        that is v0.1 behaviour this milestone does not change."""
        self.write_project_config("reviewers:\n  max_selected: 1\n")
        self.cfg = config.load(self.repo)
        plain = self.run_review(dry_run=True)
        self.assertEqual(plain.selected, ["code-quality"])
        result = self.run_review(add=["security"])
        self.assertIn("security", result.selected)
        self.assertIn("code-quality", result.selected)
        self.assertEqual(sorted(self.last_call()), sorted(result.selected))

    def test_all_selects_every_candidate(self):
        result = self.run_review(select_all=True)
        self.assertEqual(sorted(result.selected),
                         ["architecture", "code-quality", "security"])
        self.assertEqual(result.scope_authority, "code-quality")

    def test_never_still_removes_a_reviewer_under_all(self):
        self.write_project_config("reviewers:\n  never: [security]\n")
        self.cfg = config.load(self.repo)
        result = self.run_review(select_all=True)
        self.assertNotIn("security", result.selected)
        self.assertNotIn("security", self.last_call())

    def test_the_authority_survives_a_targeted_round_with_no_trigger_of_its_own(self):
        self.run_review()
        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review()
        self.assertIsNotNone(result.scope_authority)
        self.assertIn(result.scope_authority, self.last_call())

    def test_the_authority_is_stable_across_rounds(self):
        """Not recomputed from the delta's content (1b's continuity rule)."""
        first = self.run_review()
        # A pure deletion: `net_removal` would otherwise pull `architecture` in
        # and let the role drift to it.
        os.remove(self.repo / "migrations" / "001.sql")
        state.record_edit(SESSION, "migrations/001.sql", None)
        second = self.run_review()
        self.assertEqual(second.scope_authority, first.scope_authority)


class MaxSelectedIsAppliedOnce(LoopCase):
    def test_the_cap_holds_and_spares_the_authority(self):
        self.write_project_config("reviewers:\n  max_selected: 2\n")
        self.cfg = config.load(self.repo)
        result = self.run_review()
        self.assertEqual(len(result.selected), 2)
        self.assertIn("code-quality", result.selected)
        self.assertEqual(sorted(self.last_call()), sorted(result.selected))

    def test_a_capped_reviewer_is_named_and_never_run(self):
        self.write_project_config("reviewers:\n  max_selected: 1\n")
        self.cfg = config.load(self.repo)
        self.run_review()
        self.edit("src/app.py", "APP = 3\n")
        self.edit("src/auth/login.py", "PASSWORD_FIELD = 'pwd'\n")
        result = self.run_review()
        self.assertEqual(result.selected, ["code-quality"])
        self.assertEqual(self.last_call(), ["code-quality"])
        capped = [row for row in result.skipped
                  if row["reason"] == round2.X_MAX_SELECTED]
        self.assertTrue(capped)
        self.assertTrue(all(row["triggers"] for row in capped))

    def test_an_override_is_not_re_capped(self):
        """`--all` past the cap is the operator asking; nothing re-truncates."""
        self.write_project_config("reviewers:\n  max_selected: 1\n")
        self.cfg = config.load(self.repo)
        result = self.run_review(select_all=True)
        self.assertEqual(len(result.selected), 3)
        self.assertEqual(len(self.last_call()), 3)


# ============================================================ the delta pack ===
class TheTargetedPack(LoopCase):
    def prepare(self):
        self.respond(security=payload(
            "security", findings_list=[one_finding("Mot de passe en clair",
                                                   file="src/auth/login.py")]))
        self.run_review()
        finding_id = findings.load_rounds(SESSION)[0]["findings"][0]["id"]
        findings.resolve(SESSION, finding_id, "rejected",
                         reason="c'est un nom de champ, pas un secret")
        self.responses = {}
        self.edit("src/auth/login.py", "PASSWORD_FIELD = 'pwd'\n")
        return self.run_review()

    def test_the_delta_section_is_labelled_for_what_it_is(self):
        self.prepare()
        pack = self.packs["security"]
        self.assertIn("À VÉRIFIER EN PRIORITÉ", pack)
        self.assertIn("BASELINE DE SESSION", pack)
        self.assertIn("Ce n'est PAS un patch", pack)
        self.assertIn("src/auth/login.py", pack)

    def test_the_session_diff_is_kept_as_secondary_context(self):
        self.prepare()
        self.assertIn("Diff de session (contexte secondaire)",
                      self.packs["security"])

    def test_the_reviewer_sees_its_own_finding_and_claudes_answer(self):
        self.prepare()
        pack = self.packs["security"]
        self.assertIn("REJETÉ par Claude", pack)
        self.assertIn("nom de champ", pack)
        self.assertIn("[round 1]", pack)

    def test_it_does_not_receive_another_reviewers_findings(self):
        self.respond(
            security=payload("security", findings_list=[
                one_finding("Secret en clair", file="src/auth/login.py")]),
            architecture=payload("architecture", findings_list=[
                one_finding("Migration non réversible",
                            file="migrations/001.sql")]))
        self.run_review()
        self.responses = {}
        self.edit("src/auth/login.py", "PASSWORD_FIELD = 'pwd'\n")
        self.edit("migrations/001.sql", "CREATE TABLE t (id bigint);\n")
        self.run_review()
        self.assertNotIn("Migration non réversible", self.packs["security"])
        self.assertIn("Secret en clair", self.packs["security"])

    def test_a_shared_finding_reaches_both_of_its_reviewers(self):
        shared = one_finding("Même remarque", file="src/app.py")
        self.respond(security=payload("security", findings_list=[shared]),
                     architecture=payload("architecture",
                                          findings_list=[dict(shared)]))
        self.run_review()
        self.responses = {}
        self.edit("src/app.py", "APP = 3\n")
        self.run_review()
        for name in ("security", "architecture"):
            if name in self.packs:
                self.assertIn("Même remarque", self.packs[name])

    def test_a_settled_human_decision_is_shown_as_settled(self):
        """A settled decision follows the reviewer that raised it.

        The reviewer needs a reason to come back at all - a settled finding on
        its own is an exclusion, not a trigger - so it also carries an accepted
        technical finding, which `fix_verification` brings back.
        """
        self.respond(security=payload("security", findings_list=[
            one_finding("Retrait du support HEIC", file="src/app.py",
                        requires_human_decision=True),
            one_finding("Mot de passe en clair", file="src/auth/login.py")]))
        self.run_review()
        decision = decisions.blocking(SESSION)[0]
        self._settle(decision.id)
        findings.resolve(SESSION, "R1F2", "accepted")
        self.responses = {}
        self.edit("src/auth/login.py", "PASSWORD_FIELD = 'pwd'\n")
        self.run_review()
        pack = self.packs.get("security", "")
        self.assertIn("ce sont des décisions, pas des propositions", pack)
        self.assertIn(decision.id, pack)
        self.assertIn("Retrait du support HEIC", pack)

    def _settle(self, decision_id: str):
        """Close a decision the way a human answer does, without the hook."""
        decision = decisions.get(SESSION, decision_id)
        decision.status = decisions.APPROVED
        decision.answer = {"by": "human", "chosen": "Accepter",
                           "at": state.now_iso()}
        decisions._write(SESSION, decision)


# ============================================================== reiteration ===
class Reiteration(LoopCase):
    def reject_then_repeat(self, title="Mot de passe en clair"):
        self.respond(security=payload("security", findings_list=[
            one_finding(title, file="src/auth/login.py")]))
        self.run_review()
        finding_id = findings.load_rounds(SESSION)[0]["findings"][0]["id"]
        findings.resolve(SESSION, finding_id, "rejected",
                         reason="nom de champ, pas un secret")
        self.edit("src/auth/login.py", "PASSWORD_FIELD = 'pwd'\n")
        return self.run_review()

    def test_a_repeated_rejection_is_an_insistence_and_stops_blocking(self):
        result = self.reject_then_repeat()
        repeated = [f for f in result.all_findings if f.insistence]
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0].reiterates, "R1F1")
        self.assertNotIn(repeated[0].id, result.blocking_ids)

    def test_an_insistence_owes_no_new_arbitration(self):
        """D4 and the report agree: it is not an obligation."""
        self.reject_then_repeat()
        owed = [f.id for f in findings.unarbitrated(SESSION, "high")]
        self.assertEqual(owed, [])

    def test_the_insistence_is_visible_in_the_report(self):
        result = self.reject_then_repeat()
        text = report.render_markdown(result, SESSION, self.cfg)
        self.assertIn("Insistance", text)
        self.assertIn("R1F1", text)

    def test_it_stays_non_blocking_at_round_three(self):
        """No unbounded escalation once `max_rounds` allows a third round."""
        self.write_project_config("gate:\n  max_rounds: 3\n")
        self.cfg = config.load(self.repo)
        self.reject_then_repeat()
        self.edit("src/auth/login.py", "PASSWORD_FIELD = 'secret_field'\n")
        result = self.run_review()
        self.assertEqual(result.round, 3)
        for finding in result.all_findings:
            if finding.title == "Mot de passe en clair":
                self.assertTrue(finding.insistence)
                self.assertNotIn(finding.id, result.blocking_ids)

    def test_an_accepted_finding_that_reappears_is_a_normal_finding(self):
        """The reviewer judges the fix insufficient. That still blocks."""
        self.respond(security=payload("security", findings_list=[
            one_finding("Mot de passe en clair", file="src/auth/login.py")]))
        self.run_review()
        findings.resolve(SESSION, "R1F1", "accepted")
        self.edit("src/auth/login.py", "PASSWORD_FIELD = 'pwd'\n")
        result = self.run_review()
        repeated = [f for f in result.all_findings
                    if f.title == "Mot de passe en clair"]
        self.assertEqual(len(repeated), 1)
        self.assertFalse(repeated[0].insistence)
        self.assertIn(repeated[0].id, result.blocking_ids)

    def test_a_deferred_finding_that_reappears_is_not_an_insistence(self):
        self.respond(security=payload("security", findings_list=[
            one_finding("Mot de passe en clair", file="src/auth/login.py")]))
        self.run_review()
        findings.resolve(SESSION, "R1F1", "deferred", reason="plus tard")
        self.edit("src/auth/login.py", "PASSWORD_FIELD = 'pwd'\n")
        result = self.run_review()
        repeated = [f for f in result.all_findings
                    if f.title == "Mot de passe en clair"]
        self.assertTrue(repeated)
        self.assertFalse(repeated[0].insistence)

    def test_a_key_that_becomes_ambiguous_later_stops_matching(self):
        """Unambiguous when rejected, collided-on afterwards: neither matches.

        Found by Crux itself during the 1c dogfooding round. The historical
        rejection was safe, so the index offered it - and both of the new
        findings took it, leaving the blocking set and the D4 obligation
        together. Ambiguity has to be checked on the round being marked, not
        only on the rounds the index was built from.
        """
        self.respond(security=payload("security", findings_list=[
            one_finding("Doublon tardif", file="src/app.py")]))
        self.run_review()
        findings.resolve(SESSION, "R1F1", "rejected", reason="faux positif")

        same = one_finding("Doublon tardif", file="src/app.py")
        self.responses = {"security": payload(
            "security", findings_list=[dict(same), dict(same)])}
        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review()
        repeated = [f for f in result.all_findings
                    if f.title == "Doublon tardif"]
        self.assertEqual(len(repeated), 2)
        for finding in repeated:
            self.assertFalse(finding.insistence)
            self.assertIn(finding.id, result.blocking_ids)
        self.assertEqual(
            sorted(f.id for f in findings.unarbitrated(SESSION, "high")),
            sorted(f.id for f in repeated))

    def test_an_ambiguous_key_never_matches(self):
        """Two round-1 findings share a key: neither can be reiterated."""
        same = one_finding("Doublon", file="src/app.py")
        self.respond(security=payload("security", findings_list=[
            dict(same), dict(same)]))
        self.run_review()
        recorded = findings.load_rounds(SESSION)[0]["findings"]
        self.assertEqual(len({f["key"] for f in recorded}), 1)
        for raw in recorded:
            findings.resolve(SESSION, raw["id"], "rejected", reason="non")
        self.responses = {"security": payload("security",
                                              findings_list=[dict(same)])}
        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review()
        repeated = [f for f in result.all_findings if f.title == "Doublon"]
        self.assertTrue(repeated)
        self.assertFalse(repeated[0].insistence,
                         "une clé ambiguë ne doit jamais matcher")


# ======================================================== human decisions ===
class HumanDecisionsAreNotReopened(LoopCase):
    def promote_once(self):
        self.respond(security=payload("security", findings_list=[
            one_finding("Retrait du support HEIC", file="src/app.py",
                        requires_human_decision=True)]))
        result = self.run_review()
        self.assertTrue(result.opened_decisions)
        return result.opened_decisions[0]

    def test_a_promoted_finding_goes_to_the_human_not_back_to_a_reviewer(self):
        decision_id = self.promote_once()
        self.edit("src/app.py", "APP = 3\n")
        plan = round2.build(SESSION, self.repo, self.cfg)
        row = plan.row("security")
        self.assertFalse(row.selected)
        self.assertEqual(row.exclusion_reason, round2.X_PENDING_HUMAN)
        self.assertTrue(decisions.blocking(SESSION))

    def test_the_same_settled_decision_is_not_opened_twice(self):
        decision_id = self.promote_once()
        decision = decisions.get(SESSION, decision_id)
        decision.status = decisions.APPROVED
        decision.answer = {"by": "human", "chosen": "Accepter"}
        decisions._write(SESSION, decision)

        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review(select_all=True)
        self.assertEqual(result.opened_decisions, [])
        self.assertIn(decision_id, result.duplicate_decisions.values())
        self.assertEqual(len(decisions.load_all(SESSION)), 1)

    def test_an_ambiguous_key_reopens_rather_than_swallows(self):
        """Duplication beats a swallowed question."""
        same = one_finding("Retrait du support HEIC", file="src/app.py",
                           requires_human_decision=True)
        self.respond(security=payload("security",
                                      findings_list=[dict(same), dict(same)]))
        first = self.run_review()
        self.assertEqual(len(first.opened_decisions), 2)
        self.edit("src/app.py", "APP = 3\n")
        result = self.run_review(select_all=True)
        self.assertEqual(result.duplicate_decisions, {})


# ============================================================== H1 attempts ===
class AttemptsDoNotConsumeRounds(LoopCase):
    def test_a_failed_attempt_records_no_round(self):
        self.failing = {"code-quality", "architecture", "security"}
        result = self.run_review()
        self.assertEqual(result.attempt_status, state.ATTEMPT_FAILED)
        self.assertEqual(state.load(SESSION).round, 0)
        self.assertEqual(findings.load_rounds(SESSION), [])

    def test_an_unusable_attempt_records_no_round(self):
        self.failing = {"code-quality"}
        result = self.run_review()
        self.assertEqual(result.attempt_status, state.ATTEMPT_UNUSABLE)
        self.assertEqual(state.load(SESSION).round, 0)
        self.assertEqual(findings.load_rounds(SESSION), [])

    def test_a_degraded_attempt_records_a_round(self):
        self.failing = {"security"}
        result = self.run_review()
        self.assertEqual(result.attempt_status, state.ATTEMPT_DEGRADED)
        self.assertEqual(state.load(SESSION).round, 1)
        self.assertEqual(
            findings.load_rounds(SESSION)[0]["failed_reviewers"], ["security"])


# ================================================================ no delta ===
class NoDeltaEndsTheLoop(LoopCase):
    def test_a_second_review_with_nothing_changed_runs_no_codex(self):
        self.run_review()
        before = len(self.calls)
        result = self.run_review()
        self.assertEqual(result.stop_reason, review.STOP_NO_DELTA)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(state.load(SESSION).round, 1)

    def test_it_writes_no_round_two(self):
        self.run_review()
        self.run_review()
        self.assertFalse(
            (paths.session_dir(SESSION) / "findings" / "round-2.json").is_file())

    def test_an_override_does_not_manufacture_a_round(self):
        """`--all` on an unchanged tree is still a round over identical bytes."""
        self.run_review()
        before = len(self.calls)
        for kwargs in ({"select_all": True}, {"only": ["security"]},
                       {"add": ["security"]}):
            with self.subTest(**kwargs):
                result = self.run_review(**kwargs)
                self.assertEqual(result.stop_reason, review.STOP_NO_DELTA)
        self.assertEqual(len(self.calls), before)

    def test_a_failed_reviewer_with_no_delta_is_reported_not_approved(self):
        """§12's open case: no next round happens, so the gap is stated instead.

        The reviewer is a candidate for a round that happens; none does here.
        What must never occur is its silence reading as an approve - so the
        journal, the round report and the session summary all name it.
        """
        self.failing = {"security"}
        first = self.run_review()
        self.failing = set()
        self.assertEqual(first.attempt_status, state.ATTEMPT_DEGRADED)

        before = len(self.calls)
        second = self.run_review()
        self.assertEqual(second.stop_reason, review.STOP_NO_DELTA)
        self.assertEqual(len(self.calls), before)

        self.assertEqual(findings.load_rounds(SESSION)[0]["failed_reviewers"],
                         ["security"])
        self.assertIn("security",
                      report.render_markdown(first, SESSION, self.cfg))
        self.assertIn("security", report.render_session_summary(
            SESSION, self.cfg))

    def test_the_report_names_the_reason(self):
        self.run_review()
        result = self.run_review()
        text = report.render_markdown(result, SESSION, self.cfg)
        self.assertIn("no_delta", text)
        self.assertIn("jamais une cible", text)


# ================================================================= report ===
class TheFinalReport(LoopCase):
    def test_the_summary_counts_executions_and_avoided(self):
        self.respond(security=payload("security", findings_list=[
            one_finding("Mot de passe en clair", file="src/auth/login.py")]))
        self.run_review()
        findings.resolve(SESSION, "R1F1", "accepted")
        self.responses = {}
        self.edit("src/auth/login.py", "PASSWORD_FIELD = 'pwd'\n")
        result = self.run_review()
        summary = report.render_session_summary(SESSION, self.cfg)
        self.assertIn("reviewer executions:", summary)
        self.assertIn("reviewer executions avoided by targeting:", summary)
        executions = int(summary.split("reviewer executions:")[1]
                         .splitlines()[0].strip())
        self.assertEqual(executions, 3 + len(result.selected))

    def test_the_summary_lists_rounds_and_human_decisions(self):
        self.respond(security=payload("security", findings_list=[
            one_finding("Retrait du support HEIC", file="src/app.py",
                        requires_human_decision=True)]))
        self.run_review()
        summary = report.render_session_summary(SESSION, self.cfg)
        self.assertIn("round 1", summary)
        self.assertIn("décisions humaines : 1", summary)
        self.assertIn("D1", summary)

    def test_the_round_journal_records_who_was_skipped(self):
        self.run_review()
        self.edit("src/app.py", "APP = 3\n")
        self.run_review()
        record = findings.load_rounds(SESSION)[1]
        self.assertEqual(record["targeting"], review.TARGETING_TARGETED)
        self.assertEqual(record["delta_paths"], ["src/app.py"])
        self.assertTrue(record["skipped"])

    def test_a_clean_round_says_so(self):
        result = self.run_review()
        self.assertEqual(result.stop_reason, review.STOP_CLEAN)
        text = report.render_markdown(result, SESSION, self.cfg)
        self.assertIn("raison de fin : clean", text)

    def test_an_open_obligation_is_not_clean(self):
        self.respond(security=payload("security", findings_list=[
            one_finding("Mot de passe en clair", file="src/auth/login.py")]))
        result = self.run_review()
        self.assertTrue(result.blocking_ids)
        self.assertEqual(result.stop_reason, "")

    def test_a_pending_human_decision_is_not_clean(self):
        self.respond(security=payload("security", findings_list=[
            one_finding("Retrait du support HEIC", file="src/app.py",
                        requires_human_decision=True)]))
        result = self.run_review()
        self.assertEqual(result.stop_reason, "")

    def test_the_ceiling_outranks_clean(self):
        self.write_project_config("gate:\n  max_rounds: 1\n")
        self.cfg = config.load(self.repo)
        result = self.run_review()
        self.assertEqual(result.stop_reason, review.STOP_MAX_ROUNDS)

    def test_a_legacy_journal_still_loads(self):
        """The added columns are additive: schema 2 stays schema 2."""
        self.run_review()
        record = findings.load_rounds(SESSION)[0]
        self.assertEqual(record["schema"], findings.ROUND_SCHEMA_VERSION)
        self.assertTrue(findings.round_is_anchorable(record))


if __name__ == "__main__":
    import unittest
    unittest.main()
