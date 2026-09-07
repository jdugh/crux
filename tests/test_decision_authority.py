"""Invariants 4, 5 and 6.

4. A pending human decision cannot be settled by Claude.
5. An approved scope change joins the session's approved scope and is not
   re-raised afterwards.
6. A rejected scope change stays outside the approved scope.
"""

from __future__ import annotations

import unittest

from ._support import CruxTestCase

from crux import context, decisions, findings, intent

from .test_human_provenance import ask_payload


class PromotionOutOfClaudeAuthority(CruxTestCase):
    """Invariant 4 — the prohibition is a missing command, not a rule."""

    def setUp(self):
        super().setUp()
        payload = {
            "verdict": "changes_requested",
            "reviewer": "code-quality",
            "summary": "s",
            "findings": [
                {"severity": "high", "title": "Bug d'index",
                 "description": "Hors bornes ligne 12.", "file": "a.py"},
                {"severity": "high", "title": "Le support HEIC disparaît",
                 "description": "Le décodeur ne garde que JPEG.",
                 "file": "formats.py", "requires_human_decision": True},
            ],
        }
        self.result = findings.parse_reviewer_payload("code-quality", payload)
        self.merged, _ = findings.dedupe([self.result])
        promoted = {}
        for finding in findings.to_promote(self.merged):
            decision = decisions.propose(
                "s1", title=finding.title, why=finding.description,
                blast_radius_paths=[finding.file])
            promoted[finding.id] = decision.id
        findings.save_round("s1", 1, self.merged, promoted)
        self.promoted = promoted

    def test_a_functional_finding_is_promoted_to_a_decision(self):
        self.assertEqual(len(self.promoted), 1)
        decision_id = next(iter(self.promoted.values()))
        self.assertEqual(decisions.get("s1", decision_id).status,
                         decisions.PENDING)

    def test_claude_cannot_resolve_a_promoted_finding(self):
        finding_id = next(iter(self.promoted))
        for status in ("accepted", "rejected", "deferred"):
            with self.assertRaises(findings.PromotedFinding) as ctx:
                findings.resolve("s1", finding_id, status, reason="je pense que non")
            self.assertIn("décision humaine", str(ctx.exception))
        self.assertEqual(findings.resolutions("s1"), {})

    def test_claude_can_still_resolve_an_ordinary_finding(self):
        ordinary = next(f.id for f in self.merged
                        if not f.requires_human_decision)
        entry = findings.resolve("s1", ordinary, "accepted")
        self.assertEqual(entry["status"], "accepted")

    def test_a_rejection_requires_a_reason(self):
        ordinary = next(f.id for f in self.merged
                        if not f.requires_human_decision)
        with self.assertRaises(ValueError):
            findings.resolve("s1", ordinary, "rejected", reason="  ")

    def test_promoted_findings_are_excluded_from_the_blocking_set(self):
        blocking = findings.blocking(self.merged, "high")
        self.assertTrue(all(not f.requires_human_decision for f in blocking))

    def test_claude_can_only_withdraw_with_a_justification(self):
        decision_id = next(iter(self.promoted.values()))
        with self.assertRaises(ValueError):
            decisions.withdraw("s1", decision_id, "")
        withdrawn = decisions.withdraw("s1", decision_id, "le code a changé")
        self.assertEqual(withdrawn.status, decisions.WITHDRAWN)
        # it stays in the ledger, it is not erased
        self.assertIsNotNone(decisions.get("s1", decision_id))

    def test_withdraw_cannot_launder_an_already_settled_decision(self):
        decision_id = next(iter(self.promoted.values()))
        decisions.resolve_from_hook(
            "s1", ask_payload("s1", decision_id, decisions.REJECT_LABEL))
        with self.assertRaises(decisions.ProvenanceError):
            decisions.withdraw("s1", decision_id, "je préfère continuer")


class ApprovedScopeChange(CruxTestCase):
    """Invariant 5 — approved, therefore in scope, therefore not re-raised."""

    def test_approval_enters_the_approved_scope(self):
        intent.record_prompt("s1", "Refais le parseur EXIF.")
        decision = decisions.propose(
            "s1", title="Retrait du support HEIC", why="…",
            approve_options=["Conserver HEIC"])
        decisions.resolve_from_hook(
            "s1", ask_payload("s1", decision.id, "Conserver HEIC"))

        self.assertEqual(decisions.get("s1", decision.id).status,
                         decisions.APPROVED)
        approved = decisions.approved_scope_entries("s1")
        self.assertEqual([d.id for d in approved], [decision.id])

        rendered = intent.render("s1")
        self.assertIn("Conserver HEIC", rendered)
        self.assertIn(decision.id, rendered)

    def test_approved_decision_reaches_the_reviewer_context(self):
        decision = decisions.propose("s1", title="Retrait HEIC", why="…")
        decisions.resolve_from_hook(
            "s1", ask_payload("s1", decision.id, decisions.APPROVE_LABEL))
        approved = [f"{d.id} — {d.title} → {d.status}"
                    for d in decisions.approved_scope_entries("s1")]
        persona = context.load_persona("code-quality", self.repo)
        pack = context.build_pack(context.PackInput(
            persona=persona, repo=self.repo, session_id="s1",
            diff_text="+x", changed_files=["a.py"],
            intent_text=intent.render("s1"), scope_authority=True,
            approved_decisions=approved))
        self.assertIn("déjà tranchées par l'humain", pack)
        self.assertIn(decision.id, pack)
        self.assertIn("ne les re-signale jamais", pack)

    def test_an_approved_decision_no_longer_blocks(self):
        decision = decisions.propose("s1", title="T", why="W")
        self.assertTrue(decisions.blocking("s1"))
        decisions.resolve_from_hook("s1", ask_payload("s1", decision.id, decisions.APPROVE_LABEL))
        self.assertFalse(decisions.blocking("s1"))

    def test_a_settled_decision_cannot_be_reopened_by_a_second_answer(self):
        decision = decisions.propose("s1", title="T", why="W")
        decisions.resolve_from_hook(
            "s1", ask_payload("s1", decision.id, decisions.APPROVE_LABEL,
                              tool_use_id="t1"))
        decisions.resolve_from_hook(
            "s1", ask_payload("s1", decision.id, decisions.REJECT_LABEL,
                              tool_use_id="t2"))
        self.assertEqual(decisions.get("s1", decision.id).status,
                         decisions.APPROVED)


class RejectedScopeChange(CruxTestCase):
    """Invariant 6 — refused, therefore still out of scope."""

    def test_rejection_is_recorded_and_stays_out_of_scope(self):
        decision = decisions.propose(
            "s1", title="Ajouter un cache Redis", why="…",
            approve_options=["Ajouter Redis"])
        decisions.resolve_from_hook(
            "s1", ask_payload("s1", decision.id, decisions.REJECT_LABEL))

        self.assertEqual(decisions.get("s1", decision.id).status,
                         decisions.REJECTED)
        self.assertNotIn(decision.id,
                         [d.id for d in decisions.approved_scope_entries("s1")])

    def test_a_rejection_still_appears_in_the_ledger_and_the_scope(self):
        decision = decisions.propose("s1", title="Ajouter Redis", why="…")
        decisions.resolve_from_hook(
            "s1", ask_payload("s1", decision.id, decisions.REJECT_LABEL))
        rendered = intent.render("s1")
        self.assertIn(decisions.REJECTED, rendered)

    def test_free_text_refusal_is_recorded_but_never_auto_classified(self):
        """Crux used to read the phrase. It no longer interprets anything."""
        decision = decisions.propose("s1", title="T", why="W")
        resolved, unclear = decisions.resolve_from_hook(
            "s1", ask_payload("s1", decision.id,
                              "Non, ne rien changer pour l'instant"))
        settled = decisions.get("s1", decision.id)
        self.assertEqual(resolved, [])
        self.assertEqual([d.id for d in unclear], [decision.id])
        self.assertEqual(settled.status, decisions.PENDING)
        self.assertEqual(settled.answer["chosen"],
                         "Non, ne rien changer pour l'instant")
        self.assertFalse(settled.answer["evidence"]["matched_option"])
        self.assertEqual(settled.answer["evidence"]["action"],
                         decisions.ACTION_CUSTOM)


class WarnAndAutoModes(CruxTestCase):
    def test_warn_records_without_blocking(self):
        decisions.propose("s1", title="T", why="W", mode="warn")
        self.assertFalse(decisions.blocking("s1"))
        self.assertEqual(decisions.load_all("s1")[0].status, decisions.NOTED)

    def test_auto_records_that_claude_settled_it(self):
        decisions.propose("s1", title="T", why="W", mode="auto")
        self.assertFalse(decisions.blocking("s1"))
        self.assertEqual(decisions.load_all("s1")[0].status,
                         decisions.AUTO_APPROVED)

    def test_ask_is_the_default_and_blocks(self):
        decisions.propose("s1", title="T", why="W")
        self.assertTrue(decisions.blocking("s1"))


if __name__ == "__main__":
    unittest.main()
