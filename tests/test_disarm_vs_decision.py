"""Three distinct things that must never be confused.

  crux off / /crux:off  -> disarms the REVIEWS.   A pending decision still blocks.
  CRUX_DISABLE=1        -> disables CRUX ENTIRELY, decision gate included.
                           Inert, never destructive: the decision survives.
  a real human answer   -> the only thing that RESOLVES a decision.

The scenario the design owes an answer to:
  1. Crux armed  2. D1 becomes pending_human  3. `crux off`
  4. Claude tries to end its turn  5. D1 still holds.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from ._support import CruxTestCase, SRC

from crux import decisions, state

from .test_human_provenance import ask_payload


def run_stop(session: str, cwd, crux_home, env_extra=None):
    import os
    env = dict(os.environ)
    env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(crux_home),
                "PYTHONIOENCODING": "utf-8"})
    env.pop("CRUX_DISABLE", None)
    env.pop("CRUX_GATE", None)
    env.update(env_extra or {})
    payload = {"hook_event_name": "Stop", "session_id": session,
               "cwd": str(cwd), "permission_mode": "default",
               "stop_hook_active": False}
    return subprocess.run(
        [sys.executable, "-m", "crux", "hook", "stop"],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env, shell=False)


def is_block(proc) -> bool:
    if not proc.stdout.strip():
        return False
    payload = json.loads(proc.stdout)
    return payload.get("hookSpecificOutput", {}).get("decision") == "block"


class DisarmingDoesNotClearADecision(CruxTestCase):
    """The exact five-step scenario."""

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")
        # 1. armed
        state.set_override("s1", "code")
        # 2. a decision becomes pending
        self.decision = decisions.propose(
            "s1", title="Retrait du support HEIC", why="…",
            approve_options=["Conserver HEIC"])

    def test_step_by_step(self):
        self.assertEqual(self.decision.status, decisions.PENDING)

        # 3. the user disarms
        state.set_override("s1", "off")
        self.assertEqual(state.load("s1").gate_override, "off")

        # 4-5. the turn still cannot end
        proc = run_stop("s1", self.repo, self.crux_home)
        self.assertTrue(is_block(proc),
                        "disarming reviews must not release a human decision")
        self.assertIn(self.decision.id, proc.stdout)
        self.assertEqual(decisions.get("s1", self.decision.id).status,
                         decisions.PENDING)

    def test_crux_off_command_says_the_decision_remains(self):
        import os
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home),
                    "CRUX_SESSION_ID": "s1", "PYTHONIOENCODING": "utf-8"})
        proc = subprocess.run(
            [sys.executable, "-m", "crux", "off"], capture_output=True,
            text=True, encoding="utf-8", cwd=str(self.repo), env=env,
            shell=False)
        self.assertEqual(proc.returncode, 6, "pending decision -> exit 6")
        self.assertIn("toujours en attente", proc.stdout)
        self.assertIn(self.decision.id, proc.stdout)

    def test_disarming_before_the_decision_changes_nothing(self):
        state.set_override("s2", "off")
        decisions.propose("s2", title="T", why="W")
        self.assertTrue(is_block(run_stop("s2", self.repo, self.crux_home)))

    def test_only_a_human_answer_releases_it(self):
        state.set_override("s1", "off")
        self.assertTrue(is_block(run_stop("s1", self.repo, self.crux_home)))
        decisions.resolve_from_hook(
            "s1", ask_payload("s1", self.decision.id, "Conserver HEIC"))
        self.assertFalse(is_block(run_stop("s1", self.repo, self.crux_home)))


class KillSwitchIsInertNotDestructive(CruxTestCase):
    """CRUX_DISABLE lifts everything, and destroys nothing."""

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")
        state.set_override("s1", "code")
        self.decision = decisions.propose("s1", title="T", why="W")

    def test_kill_switch_releases_even_the_decision_gate(self):
        """The escape hatch must work, or a Crux bug wedges the session."""
        proc = run_stop("s1", self.repo, self.crux_home,
                        env_extra={"CRUX_DISABLE": "1"})
        self.assertFalse(is_block(proc))
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.returncode, 0)

    def test_the_decision_survives_the_kill_switch(self):
        run_stop("s1", self.repo, self.crux_home, env_extra={"CRUX_DISABLE": "1"})
        settled = decisions.get("s1", self.decision.id)
        self.assertEqual(settled.status, decisions.PENDING,
                         "the kill switch must never resolve a decision")
        self.assertIsNone(settled.answer)

    def test_it_blocks_again_once_the_variable_is_removed(self):
        run_stop("s1", self.repo, self.crux_home, env_extra={"CRUX_DISABLE": "1"})
        proc = run_stop("s1", self.repo, self.crux_home)   # variable gone
        self.assertTrue(is_block(proc))
        self.assertIn(self.decision.id, proc.stdout)

    def test_kill_switch_also_silences_the_journalling_hooks(self):
        import os
        from crux import intent
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home),
                    "CRUX_DISABLE": "1", "CRUX_GATE": "code"})
        subprocess.run(
            [sys.executable, "-m", "crux", "hook", "user-prompt"],
            input=json.dumps({"hook_event_name": "UserPromptSubmit",
                              "session_id": "s9", "cwd": str(self.repo),
                              "user_input": "bonjour"}),
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(self.repo), env=env, shell=False)
        self.assertEqual(intent.read_all("s9"), [])

    def test_accepted_truthy_spellings(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            proc = run_stop("s1", self.repo, self.crux_home,
                            env_extra={"CRUX_DISABLE": value})
            self.assertFalse(is_block(proc), value)

    def test_a_falsy_value_does_not_engage_it(self):
        for value in ("0", "false", "", "no"):
            proc = run_stop("s1", self.repo, self.crux_home,
                            env_extra={"CRUX_DISABLE": value})
            self.assertTrue(is_block(proc), value)


class ThreeWaySemantics(CruxTestCase):
    """One table, asserted."""

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")
        self.decision = decisions.propose("s1", title="T", why="W")

    def test_the_matrix(self):
        cases = [
            # (label, env, override, expect_block, expect_status)
            ("armé", {}, "code", True, decisions.PENDING),
            ("crux off", {}, "off", True, decisions.PENDING),
            ("CRUX_DISABLE", {"CRUX_DISABLE": "1"}, "code", False,
             decisions.PENDING),
            ("off + CRUX_DISABLE", {"CRUX_DISABLE": "1"}, "off", False,
             decisions.PENDING),
        ]
        for label, env, override, expect_block, expect_status in cases:
            with self.subTest(label):
                state.set_override("s1", override)
                proc = run_stop("s1", self.repo, self.crux_home, env_extra=env)
                self.assertEqual(is_block(proc), expect_block, label)
                self.assertEqual(decisions.get("s1", self.decision.id).status,
                                 expect_status, label)


if __name__ == "__main__":
    unittest.main()
