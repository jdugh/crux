"""No silent fallback to the `default` session.

The first interactive run failed exactly here: `crux decision propose` without
`--session` wrote D1 into `default`, while the hooks enforced the real Claude
session. The decision existed and the gate that should have held it was looking
somewhere else — the worst possible failure for an authority mechanism.

Resolution is deterministic or it refuses:
  1. --session          explicit
  2. CRUX_SESSION_ID    explicit, from the environment
  3. exactly one active session registered against this repository
  4. none               -> error naming the fix
  5. several            -> ambiguity error; never an arbitrary pick
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from ._support import CruxTestCase, SRC

from crux import decisions, paths, state


def run_cli(*args, cwd, crux_home, env_extra=None):
    import os
    env = dict(os.environ)
    env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(crux_home),
                "PYTHONIOENCODING": "utf-8"})
    env.pop("CRUX_SESSION_ID", None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "crux", *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env, shell=False)


class ResolutionOrder(CruxTestCase):
    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")

    def test_explicit_session_always_wins(self):
        state.register("registered", self.repo)
        self.assertEqual(
            state.resolve_session("chosen", self.repo, env={}), "chosen")

    def test_env_variable_counts_as_explicit(self):
        state.register("registered", self.repo)
        self.assertEqual(
            state.resolve_session(None, self.repo,
                                  env={"CRUX_SESSION_ID": "from-env"}),
            "from-env")

    def test_exactly_one_active_session_resolves(self):
        state.register("only-one", self.repo)
        self.assertEqual(
            state.resolve_session(None, self.repo, env={}), "only-one")

    def test_no_active_session_is_an_explicit_error(self):
        with self.assertRaises(state.SessionResolutionError) as ctx:
            state.resolve_session(None, self.repo, env={})
        message = str(ctx.exception)
        self.assertIn("aucune session", message.lower())
        self.assertIn("claude-review", message)
        self.assertNotIn("default", message)

    def test_two_active_sessions_raise_ambiguity_never_a_pick(self):
        state.register("session-a", self.repo)
        state.register("session-b", self.repo)
        with self.assertRaises(state.SessionResolutionError) as ctx:
            state.resolve_session(None, self.repo, env={})
        self.assertEqual(sorted(ctx.exception.candidates),
                         ["session-a", "session-b"])
        self.assertIn("--session", str(ctx.exception))

    def test_a_session_from_another_repo_does_not_count(self):
        other = self.tmp / "other-repo"
        other.mkdir()
        state.register("elsewhere", other)
        state.register("here", self.repo)
        self.assertEqual(state.resolve_session(None, self.repo, env={}), "here")

    def test_an_ended_session_stops_competing(self):
        state.register("first", self.repo)
        state.register("second", self.repo)
        state.mark_ended("first")
        self.assertEqual(state.resolve_session(None, self.repo, env={}),
                         "second")

    def test_a_disarmed_session_stops_competing(self):
        state.register("armed", self.repo)
        state.register("disarmed", self.repo)
        state.set_override("disarmed", "off")
        self.assertEqual(state.resolve_session(None, self.repo, env={}), "armed")

    def test_a_stale_session_stops_competing(self):
        import os
        import time
        state.register("ancient", self.repo)
        state.register("fresh", self.repo)
        old = time.time() - (state.STALE_AFTER_SECONDS + 3600)
        os.utime(state.state_path("ancient"), (old, old))
        self.assertEqual(state.resolve_session(None, self.repo, env={}), "fresh")

    def test_outside_a_repository_it_refuses(self):
        with self.assertRaises(state.SessionResolutionError):
            state.resolve_session(None, None, env={})

    def test_diagnostic_callers_may_opt_out(self):
        """`crux status` must still answer when nothing can be resolved."""
        self.assertEqual(
            state.resolve_session(None, self.repo, env={}, required=False),
            "default")


class NoAgentCommandWritesToDefault(CruxTestCase):
    """The exact failure that broke the first interactive run."""

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")

    def test_decision_propose_without_session_refuses_when_none_is_active(self):
        proc = run_cli("decision", "propose", "--title", "T", "--why", "W",
                       cwd=self.repo, crux_home=self.crux_home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("session indéterminable", proc.stderr)
        self.assertFalse(decisions.ledger_path("default").exists(),
                         "nothing may be written to the `default` session")

    def test_decision_propose_uses_the_active_session_automatically(self):
        state.register("real-session", self.repo)
        proc = run_cli("decision", "propose", "--title", "Retrait HEIC",
                       "--why", "hors périmètre", "--alt", "Conserver HEIC",
                       cwd=self.repo, crux_home=self.crux_home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("D1", proc.stdout)
        self.assertTrue(decisions.blocking("real-session"))
        self.assertFalse(decisions.ledger_path("default").exists())

    def test_decision_propose_refuses_when_ambiguous(self):
        state.register("s-one", self.repo)
        state.register("s-two", self.repo)
        proc = run_cli("decision", "propose", "--title", "T", "--why", "W",
                       cwd=self.repo, crux_home=self.crux_home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("s-one", proc.stderr)
        self.assertIn("s-two", proc.stderr)
        self.assertFalse(decisions.ledger_path("default").exists())

    def test_decision_list_follows_the_same_rule(self):
        proc = run_cli("decision", "list", "--open",
                       cwd=self.repo, crux_home=self.crux_home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("session indéterminable", proc.stderr)

    def test_resolve_follows_the_same_rule(self):
        proc = run_cli("resolve", "--id", "F1", "--status", "accepted",
                       cwd=self.repo, crux_home=self.crux_home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("session indéterminable", proc.stderr)

    def test_review_follows_the_same_rule(self):
        proc = run_cli("review", cwd=self.repo, crux_home=self.crux_home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("session indéterminable", proc.stderr)

    def test_status_still_answers_without_a_session(self):
        """Diagnostics must not be the thing that refuses to help."""
        proc = run_cli("status", cwd=self.repo, crux_home=self.crux_home)
        self.assertIn("gate", proc.stdout)

    def test_a_decision_and_the_gate_agree_on_the_session(self):
        """End to end: propose without --session, then the Stop hook sees it."""
        state.register("the-session", self.repo)
        run_cli("decision", "propose", "--title", "T", "--why", "W",
                cwd=self.repo, crux_home=self.crux_home)

        import os
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home)})
        proc = subprocess.run(
            [sys.executable, "-m", "crux", "hook", "stop"],
            input=json.dumps({"hook_event_name": "Stop",
                              "session_id": "the-session",
                              "cwd": str(self.repo), "stop_hook_active": False}),
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(self.repo), env=env, shell=False)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["decision"], "block")


class SessionStartRegisters(CruxTestCase):
    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")

    def start(self, session: str, armed: bool):
        import os
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home)})
        if armed:
            env["CRUX_GATE"] = "code"
        else:
            env.pop("CRUX_GATE", None)
        return subprocess.run(
            [sys.executable, "-m", "crux", "hook", "session-start"],
            input=json.dumps({"hook_event_name": "SessionStart",
                              "session_id": session, "cwd": str(self.repo)}),
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(self.repo), env=env, shell=False)

    def test_an_armed_session_is_registered(self):
        self.start("armed-one", armed=True)
        self.assertEqual(state.resolve_session(None, self.repo, env={}),
                         "armed-one")

    def test_an_unarmed_session_is_registered_but_stays_silent(self):
        """The registry is the only trace; nothing reaches Claude."""
        proc = self.start("plain-one", armed=False)
        self.assertEqual(proc.stdout, "", "an unarmed session must emit nothing")
        self.assertEqual(state.load("plain-one").repo,
                         str(self.repo.resolve()))
        self.assertFalse(state.load("plain-one").armed_at)

    def test_session_end_clears_it(self):
        self.start("ending", armed=True)
        import os
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home)})
        subprocess.run(
            [sys.executable, "-m", "crux", "hook", "session-end"],
            input=json.dumps({"hook_event_name": "SessionEnd",
                              "session_id": "ending", "cwd": str(self.repo),
                              "reason": "clear"}),
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(self.repo), env=env, shell=False)
        with self.assertRaises(state.SessionResolutionError):
            state.resolve_session(None, self.repo, env={})


if __name__ == "__main__":
    unittest.main()
