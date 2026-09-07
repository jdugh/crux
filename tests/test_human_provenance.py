"""Invariant 4b — provenance of human decisions.

No CLI call reachable from the agent path can turn a `pending_human` decision
into `approved_by_human` or `rejected_by_human`.

Four properties are asserted here, matching ARCHITECTURE.md §7:
  1. the command does not exist          - argparse rejects a human status
  2. a deny rule closes the hook path    - checked in the shipped arming file
  3. the manual fallback needs a TTY     - the agent's Bash has none
  4. replay and matching                 - one tool_use_id settles one decision
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from ._support import CruxTestCase, SRC

from crux import decisions, intent, paths, setupcmd


def run_cli(*args: str, cwd: Path, env_extra=None, stdin: str = ""):
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "crux", *args],
        cwd=str(cwd), input=stdin, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, shell=False,
    )


def ask_payload(session_id: str, decision_id: str, chosen: str = None,
                tool_use_id: str = "toolu_real_01",
                permission_mode: str = "default"):
    """A realistic PostToolUse payload for a real AskUserQuestion round-trip.

    ``chosen`` defaults to the canonical approve option: a verdict comes from an
    option's declared action, so a payload must carry a label Crux itself
    offered. Free text is a separate, deliberately unresolved case
    (see test_decision_semantics.py).
    """
    if chosen is None:
        chosen = decisions.APPROVE_LABEL
    question = f"Faut-il conserver le support HEIC ? ({decision_id})"
    return {
        "hook_event_name": "PostToolUse",
        "session_id": session_id,
        "permission_mode": permission_mode,
        "tool_name": "AskUserQuestion",
        "tool_use_id": tool_use_id,
        "tool_input": {
            "questions": [{
                "question": question,
                "header": f"Scope {decision_id}",
                "multiSelect": False,
                "options": [{"label": decisions.APPROVE_LABEL, "description": "…"},
                            {"label": decisions.REJECT_LABEL, "description": "…"}],
            }]
        },
        "tool_response": {"answers": {question: chosen}},
    }


class HumanProvenanceNominalPath(CruxTestCase):
    def open_one(self, session="s1"):
        return decisions.propose(
            session, title="Retrait du support HEIC",
            why="La refonte du décodeur ne garde que JPEG et PNG.",
            approve_options=["Conserver HEIC"],
            blast_radius_paths=["src/formats.py"])

    def test_only_the_hook_can_write_a_human_status(self):
        decision = self.open_one()
        self.assertEqual(decision.status, decisions.PENDING)
        resolved, unclear = decisions.resolve_from_hook(
            "s1", ask_payload("s1", decision.id, "Conserver HEIC"))
        self.assertEqual([d.id for d in resolved], [decision.id])
        self.assertEqual(unclear, [])
        self.assertEqual(decisions.get("s1", decision.id).status,
                         decisions.APPROVED)

    def test_rejection_option_yields_rejected(self):
        decision = self.open_one()
        decisions.resolve_from_hook(
            "s1", ask_payload("s1", decision.id, decisions.REJECT_LABEL))
        self.assertEqual(decisions.get("s1", decision.id).status,
                         decisions.REJECTED)

    def test_evidence_records_how_the_answer_was_obtained(self):
        decision = self.open_one()
        decisions.resolve_from_hook(
            "s1", ask_payload("s1", decision.id, "Conserver HEIC"))
        evidence = decisions.get("s1", decision.id).answer["evidence"]
        self.assertEqual(evidence["hook_event"], "PostToolUse")
        self.assertEqual(evidence["tool_name"], "AskUserQuestion")
        self.assertEqual(evidence["tool_use_id"], "toolu_real_01")
        self.assertEqual(evidence["permission_mode"], "default")
        self.assertTrue(evidence["matched_option"])
        self.assertEqual(evidence["action"], decisions.ACTION_APPROVE)

    def test_bypass_permissions_is_recorded_as_degraded(self):
        decision = self.open_one()
        decisions.resolve_from_hook("s1", ask_payload(
            "s1", decision.id, "Conserver HEIC",
            permission_mode="bypassPermissions"))
        self.assertTrue(decisions.get("s1", decision.id).degraded_provenance)

    def test_answer_joins_the_approved_scope(self):
        decision = self.open_one()
        decisions.resolve_from_hook(
            "s1", ask_payload("s1", decision.id, "Conserver HEIC"))
        kinds = [r["t"] for r in intent.read_all("s1")]
        self.assertIn(intent.ANSWER, kinds)
        self.assertIn(intent.DECISION, kinds)
        self.assertIn("Conserver HEIC", intent.render("s1"))


class HumanProvenanceCannotBeForged(CruxTestCase):
    def open_one(self, session="s1"):
        return decisions.propose(session, title="T", why="W",
                                 approve_options=["A"])

    # --- property 1: the command does not exist -------------------------
    def test_no_cli_flag_can_set_a_human_status(self):
        decision = self.open_one()
        for args in (
            ["decision", "resolve", "--id", decision.id,
             "--status", "approved_by_human"],
            ["decision", "resolve", "--id", decision.id, "--by", "human"],
            ["decision", "approve", "--id", decision.id],
            ["decision", "set-status", "--id", decision.id,
             "--status", "approved_by_human"],
        ):
            proc = run_cli(*args, cwd=self.repo,
                           env_extra={"CRUX_HOME": str(self.crux_home)})
            self.assertNotEqual(proc.returncode, 0, f"{args} should not succeed")
        self.assertEqual(decisions.get("s1", decision.id).status,
                         decisions.PENDING)

    def test_by_human_flag_does_not_exist_anywhere(self):
        source = (SRC / "crux").rglob("*.py")
        for path in source:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn('"--by"', text, f"{path} exposes a --by flag")

    # --- property 2: a deny rule closes the hook path -------------------
    def test_arming_file_denies_hook_and_manual_resolve(self):
        setupcmd.write_gate_files()
        gate = json.loads(
            (paths.claude_dir() / "gate-code.json").read_text(encoding="utf-8"))
        deny = gate["permissions"]["deny"]
        self.assertIn("Bash(crux hook:*)", deny)
        self.assertIn("Bash(crux decision resolve:*)", deny)

    def test_allowlist_is_an_enumeration_never_a_wildcard(self):
        setupcmd.write_gate_files()
        gate = json.loads(
            (paths.claude_dir() / "gate-code.json").read_text(encoding="utf-8"))
        allow = gate["permissions"]["allow"]
        self.assertNotIn("Bash(crux:*)", allow)
        self.assertNotIn("Bash(crux hook:*)", allow)
        for rule in allow:
            self.assertTrue(rule.startswith("Bash(crux "), rule)

    # --- property 3: the manual fallback needs a TTY --------------------
    def test_manual_resolve_refuses_without_a_tty(self):
        decision = self.open_one()
        with self.assertRaises(decisions.ProvenanceError) as ctx:
            decisions.resolve_manually(
                "s1", decision.id, approve=True, answer_text="ok",
                confirm_id=decision.id, stdin_is_tty=False, stdout_is_tty=True)
        self.assertIn("terminal interactif", str(ctx.exception))
        self.assertEqual(decisions.get("s1", decision.id).status,
                         decisions.PENDING)

    def test_manual_resolve_from_the_agent_path_fails_end_to_end(self):
        """What Claude would actually run: a subprocess with captured streams."""
        decision = self.open_one()
        proc = run_cli("decision", "resolve", "--id", decision.id, "--approve",
                       "--answer", "yes", "--confirm", decision.id,
                       cwd=self.repo,
                       env_extra={"CRUX_HOME": str(self.crux_home)})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(decisions.get("s1", decision.id).status,
                         decisions.PENDING)

    def test_manual_resolve_requires_retyping_the_id(self):
        decision = self.open_one()
        with self.assertRaises(decisions.ProvenanceError):
            decisions.resolve_manually(
                "s1", decision.id, approve=True, answer_text="ok",
                confirm_id="D999", stdin_is_tty=True, stdout_is_tty=True)

    def test_manual_resolve_works_for_a_real_terminal(self):
        decision = self.open_one()
        result = decisions.resolve_manually(
            "s1", decision.id, approve=True, answer_text="je valide",
            confirm_id=decision.id, stdin_is_tty=True, stdout_is_tty=True)
        self.assertEqual(result.status, decisions.APPROVED)
        self.assertEqual(result.answer["evidence"]["permission_mode"],
                         "interactive-tty")

    # --- property 4: replay and matching --------------------------------
    def test_a_tool_use_id_settles_only_one_decision(self):
        first = self.open_one()
        second = decisions.propose("s1", title="T2", why="W2")
        decisions.resolve_from_hook(
            "s1", ask_payload("s1", first.id, "A", tool_use_id="toolu_x"))
        with self.assertRaises(decisions.ProvenanceError) as ctx:
            decisions.resolve_from_hook(
                "s1", ask_payload("s1", second.id, "A", tool_use_id="toolu_x"))
        self.assertIn("déjà consommé", str(ctx.exception))
        self.assertEqual(decisions.get("s1", second.id).status,
                         decisions.PENDING)

    def test_payload_from_another_tool_is_refused(self):
        decision = self.open_one()
        payload = ask_payload("s1", decision.id, "A")
        payload["tool_name"] = "Bash"
        with self.assertRaises(decisions.ProvenanceError):
            decisions.resolve_from_hook("s1", payload)
        self.assertEqual(decisions.get("s1", decision.id).status,
                         decisions.PENDING)

    def test_payload_from_another_event_is_refused(self):
        decision = self.open_one()
        payload = ask_payload("s1", decision.id, "A")
        payload["hook_event_name"] = "PreToolUse"
        with self.assertRaises(decisions.ProvenanceError):
            decisions.resolve_from_hook("s1", payload)

    def test_answer_without_a_matching_decision_id_settles_nothing(self):
        decision = self.open_one()
        payload = ask_payload("s1", "D999", "A")
        decisions.resolve_from_hook("s1", payload)
        self.assertEqual(decisions.get("s1", decision.id).status,
                         decisions.PENDING)
        # ...but the human's words still join the approved scope
        self.assertIn(intent.ANSWER, [r["t"] for r in intent.read_all("s1")])

    def test_hook_subcommand_is_not_in_the_allowlist_shape(self):
        """Even if Claude tried, the payload path is a hook subcommand."""
        setupcmd.write_gate_files()
        gate = json.loads(
            (paths.claude_dir() / "gate-code.json").read_text(encoding="utf-8"))
        self.assertNotIn("Bash(crux hook:*)", gate["permissions"]["allow"])
        self.assertIn("Bash(crux hook:*)", gate["permissions"]["deny"])


if __name__ == "__main__":
    unittest.main()
