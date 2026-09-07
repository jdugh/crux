"""UTF-8, end to end, through a real subprocess.

The interactive run persisted an answer as mojibake: `sys.stdin.read()` uses the
locale encoding, and on a French Windows console that is cp1252. Text the human
actually typed must survive the round trip byte for byte.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from ._support import CruxTestCase, SRC

from crux import decisions, intent, paths, state

# accents, a long dash, an arrow, an emoji, and a combining sequence
SAMPLE = "Conserver le décodeur — préférer l'option où é/à passent → OK 🎯 café"
SAMPLE_PROMPT = ("Corrige la génération d'aperçus : les fichiers déjà traités "
                 "sont ré-encodés — c'est un bug. Priorité → haute 🚀")


class HookIO(CruxTestCase):
    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")

    def run_hook(self, event: str, payload: dict):
        import os
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home),
                    "CRUX_GATE": "code"})
        # deliberately hostile: force a non-UTF-8 console encoding
        env["PYTHONIOENCODING"] = "cp1252"
        return subprocess.run(
            [sys.executable, "-m", "crux", "hook", event],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True, cwd=str(self.repo), env=env, shell=False)

    def test_prompt_survives_a_cp1252_console(self):
        self.run_hook("user-prompt", {
            "hook_event_name": "UserPromptSubmit", "session_id": "s1",
            "cwd": str(self.repo), "user_input": SAMPLE_PROMPT})
        records = intent.read_all("s1")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["text"], SAMPLE_PROMPT)

    def test_answer_survives_a_cp1252_console(self):
        # a structured option, not a reviewer hint: only an option carries an
        # action, and only an action settles a decision
        decision = decisions.propose("s1", title="Décodeur — HEIC", why="…",
                                     approve_options=[SAMPLE])
        question = "Que faire pour l'accès aux fichiers déjà traités ?"
        proc = self.run_hook("post-ask", {
            "hook_event_name": "PostToolUse", "session_id": "s1",
            "cwd": str(self.repo), "permission_mode": "default",
            "tool_name": "AskUserQuestion", "tool_use_id": "toolu_utf8",
            "tool_input": {"questions": [{
                "question": question, "header": f"Scope {decision.id}",
                "options": [{"label": SAMPLE}]}]},
            "tool_response": {"answers": {question: SAMPLE}}})

        settled = decisions.get("s1", decision.id)
        self.assertEqual(settled.status, decisions.APPROVED)
        self.assertEqual(settled.answer["chosen"], SAMPLE)
        self.assertTrue(settled.answer["evidence"]["matched_option"])

        answers = [r for r in intent.read_all("s1") if r["t"] == intent.ANSWER]
        self.assertEqual(answers[0]["chosen"], SAMPLE)
        self.assertEqual(answers[0]["question"], question)

        # the response Crux writes back is UTF-8 bytes, not console-encoded
        self.assertIn(decision.id, proc.stdout.decode("utf-8"))

    def test_no_mojibake_markers_anywhere_on_disk(self):
        self.run_hook("user-prompt", {
            "hook_event_name": "UserPromptSubmit", "session_id": "s1",
            "cwd": str(self.repo), "user_input": SAMPLE_PROMPT})
        raw = intent.ledger_path("s1").read_bytes().decode("utf-8")
        for marker in ("Ã©", "Ã ", "â€”", "â†’", "Ã¢"):
            self.assertNotIn(marker, raw, f"double encoding: {marker}")

    def test_each_character_class_round_trips(self):
        for text in ("é", "à", "—", "→", "🎯", "ç ü ñ", "café — naïve"):
            with self.subTest(text):
                session = "s-" + str(abs(hash(text)) % 10000)
                self.run_hook("user-prompt", {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session, "cwd": str(self.repo),
                    "user_input": text})
                self.assertEqual(intent.read_all(session)[0]["text"], text)

    def test_invalid_utf8_does_not_crash_the_hook(self):
        import os
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home),
                    "CRUX_GATE": "code"})
        proc = subprocess.run(
            [sys.executable, "-m", "crux", "hook", "stop"],
            input=b'{"hook_event_name": "Stop", "x": "\xff\xfe bad"}',
            capture_output=True, cwd=str(self.repo), env=env, shell=False)
        self.assertEqual(proc.returncode, 0)


class LedgersAreUtf8(CruxTestCase):
    def test_jsonl_writes_are_explicitly_utf8(self):
        intent.record_prompt("s1", SAMPLE_PROMPT)
        decisions.propose("s1", title=SAMPLE, why="pourquoi — à cause de ça")
        state.record_edit("s1", "chemin/accentué.py", None)

        for path in (intent.ledger_path("s1"), decisions.ledger_path("s1"),
                     state.edits_path("s1")):
            raw = path.read_bytes()
            decoded = raw.decode("utf-8")          # must not raise
            self.assertNotIn("Ã", decoded, str(path))

    def test_state_and_manifest_json_are_utf8(self):
        st = state.load("s1")
        st.branch = "feat/aperçu—rapide"
        state.save(st)
        self.assertEqual(state.load("s1").branch, "feat/aperçu—rapide")

    def test_decision_title_and_answer_round_trip(self):
        decision = decisions.propose("s1", title=SAMPLE, why=SAMPLE)
        again = decisions.get("s1", decision.id)
        self.assertEqual(again.title, SAMPLE)
        self.assertEqual(again.why, SAMPLE)

    def test_intent_render_preserves_the_text(self):
        intent.record_prompt("s1", SAMPLE_PROMPT)
        self.assertIn(SAMPLE_PROMPT, intent.render("s1"))



class CliArgumentsAreUnicodeSafe(CruxTestCase):
    """Unicode on the CLI, through a real shell, on Windows and Linux.

    During a smoke test the `--why` was written without accents "to be safe with
    shell quoting". That must not be a normal limitation of Crux: the text a
    reviewer wrote is the text the human reads, and transliterating it would
    quietly degrade the record.
    """

    RICH = "Décision d'intégrité — conserver l'HEIC → 🎯"

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")
        state.register("cli-utf8", self.repo)

    def run_cli(self, *args, encoding_env="cp1252"):
        import os
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home),
                    "CRUX_SESSION_ID": "cli-utf8"})
        # a hostile console encoding must not reach the ledger
        env["PYTHONIOENCODING"] = encoding_env
        return subprocess.run(
            [sys.executable, "-m", "crux", *args], capture_output=True,
            cwd=str(self.repo), env=env, shell=False)

    def test_title_and_why_round_trip_exactly(self):
        proc = self.run_cli("decision", "propose",
                            "--title", self.RICH, "--why", self.RICH)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        decision = decisions.get("cli-utf8", "D1")
        self.assertEqual(decision.title, self.RICH)
        self.assertEqual(decision.why, self.RICH)

    def test_structured_option_labels_round_trip_exactly(self):
        label = "Garder — même si é/à/→/🎯"
        self.run_cli("decision", "propose", "--title", "T", "--why", "W",
                     "--approve-option", label)
        decision = decisions.get("cli-utf8", "D1")
        self.assertIn(label, decision.option_labels())
        self.assertEqual(decision.action_for(label), decisions.ACTION_APPROVE)

    def test_reviewer_hints_round_trip_exactly(self):
        hint = "Rétablir « heic » — via pillow-heif → 🎯"
        self.run_cli("decision", "propose", "--title", "T", "--why", "W",
                     "--alt", hint)
        self.assertEqual(decisions.get("cli-utf8", "D1").alternatives, [hint])

    def test_resolve_reason_round_trips_exactly(self):
        from crux import findings
        finding = findings.Finding(id="F1", reviewer="code-quality",
                                   severity="high", title="T",
                                   description="D")
        findings.save_round("cli-utf8", 1, [finding], {})
        reason = "Faux positif — la clé était déjà validée en amont → OK 🎯"
        proc = self.run_cli("resolve", "--id", "F1", "--status", "rejected",
                            "--reason", reason)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(findings.resolutions("cli-utf8")["F1"]["reason"], reason)

    def test_stdout_is_utf8_whatever_the_console(self):
        """Claude reads these labels and must copy them back byte for byte."""
        self.run_cli("decision", "propose", "--title", self.RICH,
                     "--why", self.RICH)
        proc = self.run_cli("decision", "show", "--id", "D1")
        text = proc.stdout.decode("utf-8")      # must not raise
        self.assertIn(self.RICH, text)
        self.assertIn(decisions.APPROVE_LABEL, text)
        self.assertIn(decisions.REJECT_LABEL, text)

    def test_a_label_copied_from_stdout_matches_exactly(self):
        """The full loop: Crux prints a label, it comes back, it must match."""
        import json as _json
        self.run_cli("decision", "propose", "--title", "T", "--why", "W")
        shown = _json.loads(
            self.run_cli("decision", "show", "--id", "D1")
            .stdout.decode("utf-8").split("Question à poser (AskUserQuestion) :")[1])
        for option in shown["options"]:
            self.assertIsNotNone(
                decisions.get("cli-utf8", "D1").action_for(option["label"]),
                option["label"])

    def test_no_transliteration_happens_anywhere(self):
        self.run_cli("decision", "propose", "--title", self.RICH, "--why", "W")
        raw = decisions.ledger_path("cli-utf8").read_bytes().decode("utf-8")
        self.assertIn("→", raw)
        self.assertIn("🎯", raw)
        self.assertIn("é", raw)


if __name__ == "__main__":
    unittest.main()
