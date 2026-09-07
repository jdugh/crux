"""Invariant 7.

I1  A Crux failure never wedges a Claude session — fail open.
I2  An established human decision holds — fail closed.

The two do not conflict: enforcing I2 is a local file read, so it shares no
component with anything I1 protects against. The one genuine tension — a ledger
that exists but cannot be read — is resolved deliberately in favour of I2.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from ._support import CruxTestCase, SRC

from crux import decisions, paths, state


def run_hook(event: str, payload: dict, crux_home: Path, cwd: Path):
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["CRUX_HOME"] = str(crux_home)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("CRUX_DISABLE", None)
    return subprocess.run(
        [sys.executable, "-m", "crux", "hook", event],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env, shell=False)


def stop_payload(session: str, cwd: Path, gate: str = "code"):
    return {"hook_event_name": "Stop", "session_id": session, "cwd": str(cwd),
            "permission_mode": "default", "stop_hook_active": False}


class FailOpenOnTechnicalFailure(CruxTestCase):
    """I1 — every one of these must exit 0 and emit no block."""

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")

    def assert_not_blocked(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if proc.stdout.strip():
            payload = json.loads(proc.stdout)
            decision = payload.get("hookSpecificOutput", {}).get("decision")
            self.assertNotEqual(decision, "block", proc.stdout)

    def test_broken_project_config_does_not_block(self):
        self.write_project_config("gate: { mode: code\n")   # invalid YAML
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assert_not_blocked(proc)

    def test_invalid_config_value_does_not_block(self):
        self.write_project_config("gate: { mode: banana }\n")
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assert_not_blocked(proc)

    def test_outside_a_git_repository_does_not_block(self):
        outside = self.tmp / "not-a-repo"
        outside.mkdir()
        proc = run_hook("stop", stop_payload("s1", outside), self.crux_home,
                        outside)
        self.assert_not_blocked(proc)

    def test_empty_payload_does_not_block(self):
        proc = run_hook("stop", {}, self.crux_home, self.repo)
        self.assert_not_blocked(proc)

    def test_garbage_on_stdin_does_not_block(self):
        import os
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home)})
        proc = subprocess.run(
            [sys.executable, "-m", "crux", "hook", "stop"],
            input="not json at all", capture_output=True, text=True,
            encoding="utf-8", cwd=str(self.repo), env=env, shell=False)
        self.assert_not_blocked(proc)

    def test_corrupt_session_state_does_not_block(self):
        state.save(state.SessionState(session_id="s1"))
        state.state_path("s1").write_text("{{{", encoding="utf-8")
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assert_not_blocked(proc)

    def test_unarmed_session_is_never_blocked(self):
        """Invariant 1 again, this time through the real hook process."""
        self.write("a.py", "A = 2\n")
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assert_not_blocked(proc)
        self.assertEqual(proc.stdout.strip(), "")

    def test_unarmed_user_prompt_writes_nothing_at_all(self):
        proc = run_hook("user-prompt",
                        {"hook_event_name": "UserPromptSubmit",
                         "session_id": "s1", "cwd": str(self.repo),
                         "user_input": "salut"},
                        self.crux_home, self.repo)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_armed_user_prompt_still_writes_nothing_to_stdout(self):
        """stdout on UserPromptSubmit is injected into Claude's context."""
        import os
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home),
                    "CRUX_GATE": "code"})
        proc = subprocess.run(
            [sys.executable, "-m", "crux", "hook", "user-prompt"],
            input=json.dumps({"hook_event_name": "UserPromptSubmit",
                              "session_id": "s1", "cwd": str(self.repo),
                              "user_input": "refais le parseur"}),
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(self.repo), env=env, shell=False)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "",
                         "a logging hook must not inject anything")
        from crux import intent
        self.assertEqual([r["text"] for r in intent.read_all("s1")],
                         ["refais le parseur"])


class FailClosedOnHumanDecision(CruxTestCase):
    """I2 — a pending decision holds, whatever else is broken."""

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")

    def blocked(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "expected a block payload")
        payload = json.loads(proc.stdout)
        return payload["hookSpecificOutput"].get("decision") == "block"

    def test_pending_decision_blocks_the_turn(self):
        decisions.propose("s1", title="Retrait HEIC", why="…")
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assertTrue(self.blocked(proc))
        self.assertIn("AskUserQuestion", proc.stdout)

    def test_it_blocks_even_when_the_gate_was_never_armed(self):
        """Disarming must not be a way out of a decision already open."""
        decisions.propose("s1", title="T", why="W")
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assertTrue(self.blocked(proc))

    def test_it_blocks_even_after_crux_off(self):
        decisions.propose("s1", title="T", why="W")
        state.set_override("s1", "off")
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assertTrue(self.blocked(proc))

    def test_it_blocks_even_with_a_broken_config(self):
        decisions.propose("s1", title="T", why="W")
        self.write_project_config("gate: { mode: code\n")
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assertTrue(self.blocked(proc))

    def test_it_blocks_even_when_codex_is_absent(self):
        """A pending decision does not depend on Codex in any way."""
        decisions.propose("s1", title="T", why="W")
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assertTrue(self.blocked(proc))

    def test_once_asked_the_gate_steps_aside(self):
        """AskUserQuestion is synchronous: the session already waits on the human."""
        decision = decisions.propose("s1", title="T", why="W")
        decisions.mark_asked("s1", decision.id, "toolu_1")
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assertFalse(self.blocked(proc) if proc.stdout.strip() else False)

    def test_a_corrupt_ledger_fails_closed(self):
        """The one place the two invariants meet, resolved in favour of I2."""
        decisions.propose("s1", title="T", why="W")
        decisions.ledger_path("s1").write_text("{{{ not json\n", encoding="utf-8")
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assertTrue(self.blocked(proc))
        self.assertIn("illisible", proc.stdout)

    def test_an_absent_ledger_fails_open(self):
        """Absent means nothing was ever opened: that is I1 territory."""
        self.assertFalse(decisions.ledger_path("s1").exists())
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assertEqual(proc.stdout.strip(), "")

    def test_settled_decisions_stop_blocking(self):
        from .test_human_provenance import ask_payload
        decision = decisions.propose("s1", title="T", why="W")
        decisions.resolve_from_hook("s1", ask_payload("s1", decision.id, decisions.APPROVE_LABEL))
        proc = run_hook("stop", stop_payload("s1", self.repo), self.crux_home,
                        self.repo)
        self.assertEqual(proc.stdout.strip(), "")


class ExitCodes(CruxTestCase):
    def test_pending_decision_yields_exit_code_six(self):
        import os
        decisions.propose("s1", title="T", why="W")
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home),
                    "CRUX_SESSION_ID": "s1"})
        proc = subprocess.run(
            [sys.executable, "-m", "crux", "decision", "list", "--open"],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(self.repo), env=env, shell=False)
        self.assertEqual(proc.returncode, 6)

    def test_invalid_config_yields_exit_code_five(self):
        import os
        self.write_project_config("gate: { mode: banana }\n")
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home)})
        proc = subprocess.run(
            [sys.executable, "-m", "crux", "config"],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(self.repo), env=env, shell=False)
        self.assertEqual(proc.returncode, 5)

    def test_outside_a_repo_yields_exit_code_four(self):
        import os
        outside = self.tmp / "plain"
        outside.mkdir()
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home)})
        proc = subprocess.run(
            [sys.executable, "-m", "crux", "review"],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(outside), env=env, shell=False)
        self.assertEqual(proc.returncode, 4)


if __name__ == "__main__":
    unittest.main()
