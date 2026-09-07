"""Regression guard for the class of bug that fail-open hides.

A missing import once turned the decision gate off silently: NameError was
swallowed by the guard, and a broken handler looked exactly like an inert one.
These tests run every handler as a real subprocess and assert that nothing
unexpected was swallowed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from ._support import CruxTestCase, SRC

from crux import hooks, paths


class EveryHandlerRunsCleanly(CruxTestCase):
    """Armed, in a real repo: no handler may log an unhandled error."""

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")

    def payload_for(self, event: str) -> dict:
        base = {"session_id": "s1", "cwd": str(self.repo),
                "permission_mode": "default"}
        shapes = {
            "session-start": {"hook_event_name": "SessionStart",
                              "start_reason": "startup"},
            "user-prompt": {"hook_event_name": "UserPromptSubmit",
                            "user_input": "refais le parseur"},
            "post-edit": {"hook_event_name": "PostToolUse", "tool_name": "Edit",
                          "tool_use_id": "toolu_1",
                          "tool_input": {"file_path": str(self.repo / "a.py")}},
            "post-ask": {"hook_event_name": "PostToolUse",
                         "tool_name": "AskUserQuestion", "tool_use_id": "toolu_2",
                         "tool_input": {"questions": [
                             {"question": "q", "header": "h", "options": []}]},
                         "tool_response": {"answers": {"q": "a"}}},
            "stop": {"hook_event_name": "Stop", "stop_hook_active": False},
            "session-end": {"hook_event_name": "SessionEnd",
                            "reason": "clear"},
        }
        base.update(shapes[event])
        return base

    def run_hook(self, event: str, env_extra=None):
        import os
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home),
                    "CRUX_GATE": "code", "PYTHONIOENCODING": "utf-8"})
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, "-m", "crux", "hook", event],
            input=json.dumps(self.payload_for(event)), capture_output=True,
            text=True, encoding="utf-8", errors="replace", cwd=str(self.repo),
            env=env, shell=False)

    def log_text(self) -> str:
        log = paths.log_path()
        return log.read_text(encoding="utf-8", errors="replace") \
            if log.is_file() else ""

    def test_no_handler_logs_an_unhandled_error(self):
        for event in hooks.HANDLERS:
            with self.subTest(event):
                proc = self.run_hook(event)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("erreur non gérée", self.log_text())
                self.assertNotIn("NameError", self.log_text())

    def test_every_declared_handler_is_reachable(self):
        for event in hooks.HANDLERS:
            proc = self.run_hook(event)
            self.assertEqual(proc.returncode, 0, event)

    def test_stdout_is_valid_json_or_empty(self):
        for event in hooks.HANDLERS:
            with self.subTest(event):
                out = self.run_hook(event).stdout.strip()
                if out:
                    json.loads(out)   # must parse, or Claude Code chokes

    def test_user_prompt_never_writes_to_stdout(self):
        self.assertEqual(self.run_hook("user-prompt").stdout, "")


class GuardMakesFailuresVisible(CruxTestCase):
    """Fail-open, but never in silence."""

    def test_a_crashing_handler_still_exits_zero_and_says_so(self):
        original = hooks.stop.__wrapped__ if hasattr(hooks.stop, "__wrapped__") \
            else None
        self.assertIsNone(original)  # documents that we wrap by name, not attr

        def boom() -> int:
            raise NameError("name 'os' is not defined")
        boom.__name__ = "stop"
        guarded = hooks._guard(boom)

        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = guarded()
        self.assertEqual(code, 0, "a Crux bug must never block the session")

        payload = json.loads(buf.getvalue())
        self.assertIn("erreur interne", payload["hookSpecificOutput"]["systemMessage"])
        self.assertIn("NameError", payload["hookSpecificOutput"]["systemMessage"])
        self.assertNotEqual(payload["hookSpecificOutput"].get("decision"), "block")

        log = paths.log_path().read_text(encoding="utf-8")
        self.assertIn("erreur non gérée", log)
        self.assertIn("NameError", log)

    def test_context_injecting_events_stay_silent_on_failure(self):
        """UserPromptSubmit stdout reaches Claude's context: never write there."""
        import io
        import contextlib

        def boom() -> int:
            raise RuntimeError("boom")
        for name in ("user_prompt", "session_start"):
            boom.__name__ = name
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = hooks._guard(boom)()
            self.assertEqual(code, 0, name)
            self.assertEqual(buf.getvalue(), "", name)


if __name__ == "__main__":
    unittest.main()
