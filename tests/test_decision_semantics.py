"""The verdict comes from the option's ACTION, never from its wording.

The bug this replaces was worse than a string mismatch: every entry in
`alternatives` counted as an approval. A reviewer's remediation — "Restaurer heic
dans SUPPORTED", which *rejects* the drift — was recorded as approving it, and
the drift then joined the approved scope for every later review.

Policy for a free-text / "Other" answer: Crux does not guess. The decision stays
`pending_human`, the text is recorded, and Claude is told to re-ask with the
structured options. An unrecognised answer never becomes an approval, because an
approval permanently widens what later reviews accept.
"""

from __future__ import annotations

import unittest

from ._support import CruxTestCase

from crux import context, decisions, intent


def ask(session, decision_id, chosen, tool_use_id="toolu_sem_1",
        header=None, question="Que décidez-vous ?"):
    return {
        "hook_event_name": "PostToolUse", "session_id": session,
        "permission_mode": "default", "tool_name": "AskUserQuestion",
        "tool_use_id": tool_use_id,
        "tool_input": {"questions": [{
            "question": question,
            "header": header or f"Scope {decision_id}",
            "options": [{"label": chosen}]}]},
        "tool_response": {"answers": {question: chosen}},
    }


class ActionDecidesTheStatus(CruxTestCase):
    def open_one(self, session="s1", **kwargs):
        return decisions.propose(session, title="Retrait du support HEIC",
                                 why="hors périmètre", **kwargs)

    # 1
    def test_approve_option_yields_approved(self):
        decision = self.open_one()
        resolved, unclear = decisions.resolve_from_hook(
            "s1", ask("s1", decision.id, decisions.APPROVE_LABEL))
        self.assertEqual([d.status for d in resolved], [decisions.APPROVED])
        self.assertEqual(unclear, [])
        self.assertEqual(
            decisions.get("s1", decision.id).answer["evidence"]["action"],
            decisions.ACTION_APPROVE)

    # 2
    def test_reject_option_yields_rejected(self):
        decision = self.open_one()
        resolved, _ = decisions.resolve_from_hook(
            "s1", ask("s1", decision.id, decisions.REJECT_LABEL))
        self.assertEqual([d.status for d in resolved], [decisions.REJECTED])

    # 3
    def test_totally_different_wording_same_action_same_status(self):
        first = decisions.propose(
            "s1", title="A", why="w",
            reject_options=["Ne rien changer — priorité à l'intégrité → 🎯"])
        second = decisions.propose(
            "s1", title="B", why="w",
            reject_options=["Surtout pas : on garde l'existant"])
        decisions.resolve_from_hook("s1", ask(
            "s1", first.id, "Ne rien changer — priorité à l'intégrité → 🎯",
            tool_use_id="t1"))
        decisions.resolve_from_hook("s1", ask(
            "s1", second.id, "Surtout pas : on garde l'existant",
            tool_use_id="t2"))
        self.assertEqual(decisions.get("s1", first.id).status,
                         decisions.REJECTED)
        self.assertEqual(decisions.get("s1", second.id).status,
                         decisions.REJECTED)

    def test_an_approving_label_that_sounds_negative_still_approves(self):
        """Wording is never parsed: only the declared action counts."""
        decision = decisions.propose(
            "s1", title="A", why="w",
            approve_options=["Ne rien restaurer — on assume le retrait"])
        decisions.resolve_from_hook("s1", ask(
            "s1", decision.id, "Ne rien restaurer — on assume le retrait"))
        self.assertEqual(decisions.get("s1", decision.id).status,
                         decisions.APPROVED)

    # 4
    def test_accents_dashes_and_emoji_do_not_influence_the_verdict(self):
        for label in ("Refuser — revenir au périmètre demandé",
                      "Refuser ⛔ — périmètre inchangé é à ù",
                      "Non 🎯"):
            with self.subTest(label):
                decision = decisions.propose("s1", title="T", why="w",
                                             reject_options=[label])
                decisions.resolve_from_hook(
                    "s1", ask("s1", decision.id, label,
                              tool_use_id=f"t-{decision.id}"))
                self.assertEqual(decisions.get("s1", decision.id).status,
                                 decisions.REJECTED)

    # 5
    def test_ambiguous_free_text_is_never_auto_approved(self):
        decision = self.open_one()
        resolved, unclear = decisions.resolve_from_hook(
            "s1", ask("s1", decision.id, "Ne rien changer — priorité 🎯"))
        self.assertEqual(resolved, [])
        self.assertEqual([d.id for d in unclear], [decision.id])
        settled = decisions.get("s1", decision.id)
        self.assertEqual(settled.status, decisions.PENDING)
        self.assertNotEqual(settled.status, decisions.APPROVED)

    def test_free_text_is_recorded_and_the_question_must_be_asked_again(self):
        decision = self.open_one()
        decisions.mark_asked("s1", decision.id, "toolu_first")
        decisions.resolve_from_hook(
            "s1", ask("s1", decision.id, "je ne sais pas trop",
                      tool_use_id="toolu_second"))
        settled = decisions.get("s1", decision.id)
        self.assertEqual(settled.answer["chosen"], "je ne sais pas trop")
        self.assertEqual(settled.answer["evidence"]["action"],
                         decisions.ACTION_CUSTOM)
        self.assertEqual(settled.clarifications, 1)
        self.assertFalse(settled.asked, "it must be re-asked, not left answered")
        self.assertTrue(decisions.blocking("s1"))

    def test_free_text_still_reaches_the_approved_scope_as_words(self):
        decision = self.open_one()
        decisions.resolve_from_hook(
            "s1", ask("s1", decision.id, "plutôt conserver, à voir"))
        self.assertIn("plutôt conserver", intent.render("s1"))

    # 6
    def test_a_rejected_decision_never_joins_the_approved_scope(self):
        decision = self.open_one()
        decisions.resolve_from_hook(
            "s1", ask("s1", decision.id, decisions.REJECT_LABEL))
        self.assertNotIn(decision.id,
                         [d.id for d in decisions.approved_scope_entries("s1")])
        approved = [f"{d.id} — {d.title}"
                    for d in decisions.approved_scope_entries("s1")]
        persona = context.load_persona("code-quality", self.repo)
        pack = context.build_pack(context.PackInput(
            persona=persona, repo=self.repo, session_id="s1",
            diff_text="+x", changed_files=["a.py"],
            intent_text=intent.render("s1"), scope_authority=True,
            approved_decisions=approved))
        # The section listing what is now in scope must not be emitted at all.
        self.assertNotIn("# Décisions déjà tranchées par l'humain", pack)
        # It still appears in the approved scope as a *rejection*, so the next
        # reviewer knows it was settled and does not raise it as news.
        self.assertIn(decisions.REJECTED, pack)

    # 7
    def test_an_approved_decision_joins_the_scope_and_is_not_re_raised(self):
        decision = self.open_one()
        decisions.resolve_from_hook(
            "s1", ask("s1", decision.id, decisions.APPROVE_LABEL))
        approved = decisions.approved_scope_entries("s1")
        self.assertEqual([d.id for d in approved], [decision.id])

        pack = context.build_pack(context.PackInput(
            persona=context.load_persona("code-quality", self.repo),
            repo=self.repo, session_id="s1", diff_text="+x",
            changed_files=["a.py"], intent_text=intent.render("s1"),
            scope_authority=True,
            approved_decisions=[f"{d.id} — {d.title} → {d.status}"
                                for d in approved]))
        self.assertIn("déjà tranchées par l'humain", pack)
        self.assertIn(decision.id, pack)
        self.assertIn("ne les re-signale jamais", pack)

    def test_the_mapping_survives_a_reload_of_the_ledger(self):
        """The next Codex round must read the same actions back."""
        decision = decisions.propose(
            "s1", title="T", why="w",
            approve_options=["Garder le retrait"],
            reject_options=["Rétablir le format"])
        reloaded = decisions.get("s1", decision.id)
        self.assertEqual(reloaded.action_for("Garder le retrait"),
                         decisions.ACTION_APPROVE)
        self.assertEqual(reloaded.action_for("Rétablir le format"),
                         decisions.ACTION_REJECT)
        self.assertIsNone(reloaded.action_for("une phrase jamais offerte"))


class ReviewerHintsAreNeverOptions(CruxTestCase):
    """The exact shape of the original bug."""

    def test_a_remediation_hint_cannot_be_chosen_as_an_approval(self):
        decision = decisions.propose(
            "s1", title="Retrait HEIC", why="hors périmètre",
            alternatives=["Restaurer heic dans SUPPORTED"])
        self.assertNotIn("Restaurer heic dans SUPPORTED",
                         decision.option_labels())
        self.assertIsNone(decision.action_for("Restaurer heic dans SUPPORTED"))

        resolved, unclear = decisions.resolve_from_hook(
            "s1", ask("s1", decision.id, "Restaurer heic dans SUPPORTED"))
        self.assertEqual(resolved, [], "a hint must not settle a decision")
        self.assertEqual(decisions.get("s1", decision.id).status,
                         decisions.PENDING)
        self.assertTrue(unclear)

    def test_hints_appear_in_the_question_body_not_the_options(self):
        decision = decisions.propose(
            "s1", title="T", why="w",
            alternatives=["Restaurer heic", "Ajouter pillow-heif"])
        payload = decisions.question_payload(decision)
        self.assertIn("Restaurer heic", payload["question"])
        self.assertIn("indicatif", payload["question"])
        labels = [o["label"] for o in payload["options"]]
        self.assertNotIn("Restaurer heic", labels)

    def test_both_canonical_options_are_always_offered(self):
        decision = decisions.propose("s1", title="T", why="w")
        actions = [o["action"] for o in decision.structured_options()]
        self.assertIn(decisions.ACTION_APPROVE, actions)
        self.assertIn(decisions.ACTION_REJECT, actions)
        self.assertEqual(len(decision.structured_options()), 2)

    def test_every_offered_option_has_a_described_effect(self):
        decision = decisions.propose("s1", title="T", why="w",
                                     approve_options=["Garder"])
        payload = decisions.question_payload(decision)
        for option in payload["options"]:
            self.assertTrue(option["description"].strip())

    def test_a_legacy_ledger_still_reads_back_as_a_rejection(self):
        """Records written before structured options must not flip meaning."""
        decision = decisions.propose("s1", title="T", why="w")
        self.assertEqual(decision.action_for(decisions.LEGACY_REJECT_LABEL),
                         decisions.ACTION_REJECT)


if __name__ == "__main__":
    unittest.main()
