"""The approved scope: verbatim, append-only, and its truncation policy.

The rule that matters: a human decision never falls out of a review context, and
neither does the original request. That is what stops a reviewer re-raising, at
round 2, something already settled at round 1.
"""

from __future__ import annotations

import unittest

from ._support import CruxTestCase

from crux import config, context, decisions, intent


class LedgerBasics(CruxTestCase):
    def test_prompts_are_stored_verbatim(self):
        text = "Refais le parseur EXIF, garde HEIC. Ne touche pas à l'UI."
        intent.record_prompt("s1", text)
        records = intent.read_all("s1")
        self.assertEqual(records[0]["text"], text)
        self.assertEqual(records[0]["t"], intent.PROMPT)

    def test_entries_are_ordered_and_numbered(self):
        for i in range(3):
            intent.record_prompt("s1", f"prompt {i}")
        self.assertEqual([r["seq"] for r in intent.read_all("s1")], [1, 2, 3])

    def test_a_torn_line_is_skipped_not_fatal(self):
        intent.record_prompt("s1", "bon")
        with open(intent.ledger_path("s1"), "a", encoding="utf-8") as fh:
            fh.write("{ tronqué\n")
        intent.record_prompt("s1", "encore bon")
        self.assertEqual([r["text"] for r in intent.read_all("s1")],
                         ["bon", "encore bon"])

    def test_nothing_is_summarised(self):
        """No model ever rewrites the human's words."""
        long_text = "détail important " * 50
        intent.record_prompt("s1", long_text)
        self.assertEqual(intent.read_all("s1")[0]["text"], long_text)

    def test_summary_counts_by_kind(self):
        intent.record_prompt("s1", "a")
        intent.record_answer("s1", "q", "chosen")
        intent.record_decision("s1", "D1", decisions.APPROVED, "t")
        counts = intent.summary("s1")
        self.assertEqual(counts[intent.PROMPT], 1)
        self.assertEqual(counts[intent.ANSWER], 1)
        self.assertEqual(counts[intent.DECISION], 1)


class TruncationPolicy(CruxTestCase):
    def test_first_prompt_and_decisions_survive_truncation(self):
        intent.record_prompt("s1", "DEMANDE INITIALE : refais le parseur EXIF.")
        for i in range(60):
            intent.record_prompt("s1", f"clarification intermédiaire {i} " * 20)
        intent.record_decision("s1", "D1", decisions.APPROVED,
                               "DECISION CRUCIALE sur HEIC")

        rendered = intent.render("s1", max_chars=1500)
        self.assertLessEqual(len(rendered), 1600)
        self.assertIn("DEMANDE INITIALE", rendered)
        self.assertIn("DECISION CRUCIALE", rendered)
        self.assertIn("omise", rendered)

    def test_untruncated_when_it_fits(self):
        intent.record_prompt("s1", "court")
        rendered = intent.render("s1", max_chars=8000)
        self.assertIn("court", rendered)
        self.assertNotIn("omise", rendered)

    def test_multiple_decisions_all_survive(self):
        intent.record_prompt("s1", "initial")
        for i in range(40):
            intent.record_prompt("s1", f"bruit {i} " * 30)
            if i % 10 == 0:
                intent.record_decision("s1", f"D{i}", decisions.APPROVED,
                                       f"décision numéro {i}")
        rendered = intent.render("s1", max_chars=2000)
        for i in (0, 10, 20, 30):
            self.assertIn(f"décision numéro {i}", rendered)

    def test_empty_ledger_renders_empty(self):
        self.assertEqual(intent.render("s1"), "")


class SendToReviewers(CruxTestCase):
    def setUp(self):
        super().setUp()
        intent.record_prompt("s1", "Le token est sk-abcdefghijklmnopqrstuvwxyz012345")

    def test_default_sends_verbatim(self):
        cfg = config.load(self.repo)
        text = context.load_intent_for(cfg, "s1")
        self.assertIn("Le token est", text)

    def test_redacted_masks_secrets(self):
        self.write_project_config(
            "human: { intent: { send_to_reviewers: redacted } }\n")
        cfg = config.load(self.repo)
        text = context.load_intent_for(cfg, "s1")
        self.assertIn("<redacted>", text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", text)

    def test_false_withholds_the_scope_entirely(self):
        self.write_project_config(
            "human: { intent: { send_to_reviewers: false } }\n")
        cfg = config.load(self.repo)
        self.assertEqual(context.load_intent_for(cfg, "s1"), "")

    def test_context_packs_are_always_redacted(self):
        """Even verbatim mode strips things that look like credentials."""
        persona = context.load_persona("code-quality", self.repo)
        pack = context.build_pack(context.PackInput(
            persona=persona, repo=self.repo, session_id="s1",
            diff_text="+API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz012345'",
            changed_files=["a.py"]))
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", pack)
        self.assertIn("<redacted>", pack)


class ScopeAuthorityPack(CruxTestCase):
    def test_only_the_authority_receives_the_approved_scope(self):
        intent.record_prompt("s1", "DEMANDE : refais le parseur")
        authority = context.load_persona("code-quality", self.repo)
        other = context.load_persona("security", self.repo)

        with_scope = context.build_pack(context.PackInput(
            persona=authority, repo=self.repo, session_id="s1",
            diff_text="+x", changed_files=["a.py"],
            intent_text=intent.render("s1"), scope_authority=True))
        without = context.build_pack(context.PackInput(
            persona=other, repo=self.repo, session_id="s1",
            diff_text="+x", changed_files=["a.py"],
            intent_text="", scope_authority=False))

        self.assertIn("AUTORITÉ DE PÉRIMÈTRE", with_scope)
        self.assertIn("DEMANDE : refais le parseur", with_scope)
        self.assertNotIn("AUTORITÉ DE PÉRIMÈTRE", without)

    def test_authority_defaults_to_the_declared_persona(self):
        chosen = context.pick_scope_authority(
            ["security", "code-quality"], self.repo)
        self.assertEqual(chosen, "code-quality")

    def test_authority_is_never_left_unassigned(self):
        """If the declared authority is not selected, someone inherits it."""
        chosen = context.pick_scope_authority(["security"], self.repo)
        self.assertEqual(chosen, "security")

    def test_base_preamble_carries_the_frontier(self):
        preamble = context.base_preamble(self.repo)
        self.assertIn("requires_human_decision", preamble)
        self.assertIn("rapport vide", preamble)

    def test_project_persona_overrides_the_shipped_one(self):
        override = self.repo / ".crux" / "personas" / "security.md"
        override.parent.mkdir(parents=True)
        override.write_text(
            "---\nname: security\ntitle: Ma revue maison\n---\nRègles maison.\n",
            encoding="utf-8")
        persona = context.load_persona("security", self.repo)
        self.assertEqual(persona.title, "Ma revue maison")
        self.assertIn("Règles maison", persona.body)


if __name__ == "__main__":
    unittest.main()
