"""Invariants 1 and 2.

1. `claude` on its own: no Crux behaviour is active.
2. `claude-review`: the review gate is active.

Plus the full six-source resolution order.
"""

from __future__ import annotations

import unittest

from ._support import CruxTestCase

from crux import config, paths, state


class GateResolution(CruxTestCase):
    def cfg(self):
        return config.load(self.repo)

    # ---- invariant 1 -----------------------------------------------------
    def test_plain_claude_is_off(self):
        """No wrapper, no project file, no override: nothing is armed."""
        decision = state.resolve_gate(self.cfg(), session_id="s1", env={})
        self.assertEqual(decision.mode, "off")
        self.assertFalse(decision.armed)
        self.assertEqual(decision.source, "défaut intégré")

    def test_plain_claude_writes_nothing(self):
        state.resolve_gate(self.cfg(), session_id="s-untouched", env={})
        # resolving must not create session state on disk
        self.assertFalse(state.state_path("s-untouched").exists())

    def test_project_config_alone_does_not_arm_without_gate_mode(self):
        self.write_project_config("version: 1\ntests: { command: npm test }\n")
        decision = state.resolve_gate(self.cfg(), session_id="s1", env={})
        self.assertEqual(decision.mode, "off")

    # ---- invariant 2 -----------------------------------------------------
    def test_claude_review_wrapper_arms_code(self):
        env = {"CRUX_GATE": "code"}
        decision = state.resolve_gate(self.cfg(), session_id="s1", env=env)
        self.assertEqual(decision.mode, "code")
        self.assertTrue(decision.armed)
        self.assertTrue(decision.code)
        self.assertFalse(decision.plan)
        self.assertEqual(decision.source, "CRUX_GATE")

    def test_claude_plan_wrapper_arms_both(self):
        decision = state.resolve_gate(self.cfg(), session_id="s1",
                                      env={"CRUX_GATE": "both"})
        self.assertTrue(decision.code and decision.plan)

    # ---- the ordering ----------------------------------------------------
    def test_disable_beats_everything(self):
        self.write_project_config("gate: { mode: both }\n")
        state.set_override("s1", "both")
        decision = state.resolve_gate(
            self.cfg(), session_id="s1",
            env={"CRUX_DISABLE": "1", "CRUX_GATE": "both"})
        self.assertEqual(decision.mode, "off")
        self.assertEqual(decision.source, "CRUX_DISABLE")

    def test_session_override_beats_env(self):
        state.set_override("s1", "off")
        decision = state.resolve_gate(self.cfg(), session_id="s1",
                                      env={"CRUX_GATE": "code"})
        self.assertEqual(decision.mode, "off")
        self.assertIn("session", decision.source)

    def test_session_override_can_arm_a_plain_session(self):
        state.set_override("s1", "code")
        decision = state.resolve_gate(self.cfg(), session_id="s1", env={})
        self.assertEqual(decision.mode, "code")

    def test_env_beats_project_file(self):
        self.write_project_config("gate: { mode: off }\n")
        decision = state.resolve_gate(self.cfg(), session_id="s1",
                                      env={"CRUX_GATE": "code"})
        self.assertEqual(decision.mode, "code")

    def test_project_beats_user(self):
        paths.ensure_dir(self.crux_home)
        paths.config_path().write_text("gate: { mode: both }\n", encoding="utf-8")
        self.write_project_config("gate: { mode: code }\n")
        decision = state.resolve_gate(self.cfg(), session_id="s1", env={})
        self.assertEqual(decision.mode, "code")
        self.assertTrue(decision.source.endswith(".crux.yml"))

    def test_user_config_is_used_when_no_project_file(self):
        paths.ensure_dir(self.crux_home)
        paths.config_path().write_text("gate: { mode: code }\n", encoding="utf-8")
        decision = state.resolve_gate(self.cfg(), session_id="s1", env={})
        self.assertEqual(decision.mode, "code")
        self.assertTrue(decision.source.endswith("config.yml"))

    def test_garbage_env_value_is_ignored(self):
        decision = state.resolve_gate(self.cfg(), session_id="s1",
                                      env={"CRUX_GATE": "banana"})
        self.assertEqual(decision.mode, "off")


class SessionStatePersistence(CruxTestCase):
    def test_override_round_trips(self):
        state.set_override("abc", "code")
        self.assertEqual(state.load("abc").gate_override, "code")
        state.set_override("abc", None)
        self.assertIsNone(state.load("abc").gate_override)

    def test_corrupt_state_file_fails_open(self):
        st = state.SessionState(session_id="abc")
        state.save(st)
        state.state_path("abc").write_text("{ not json", encoding="utf-8")
        # a broken state file is a technical failure: never crash a hook
        self.assertEqual(state.load("abc").session_id, "abc")
        self.assertEqual(
            state.resolve_gate(config.load(self.repo), "abc", env={}).mode, "off")

    def test_awkward_session_id_stays_inside_sessions_dir(self):
        directory = paths.session_dir("../../escape")
        self.assertTrue(directory.resolve().is_relative_to(
            paths.sessions_root().resolve()))


if __name__ == "__main__":
    unittest.main()
