"""Invariant 3.

With `human.questions.mode: human` — the default — Crux never answers an
AskUserQuestion on the user's behalf.

The guarantee is narrower than "Crux ignores AskUserQuestion", and saying it
loosely was itself a defect: a **PostToolUse** matcher on AskUserQuestion is
required — it is what records the human's real answer and carries provenance.
What must be absent is a **PreToolUse** matcher, the only hook shape that could
answer before the human sees the question.

    no PreToolUse/AskUserQuestion   ->  Crux cannot auto-answer
    PostToolUse/AskUserQuestion     ->  the human's answer is recorded

In the MVP the code path that could answer is not written at all, which is
stronger than a runtime `if mode == "human": return`.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from ._support import CruxTestCase, SRC

from crux import config, hooks, setupcmd

PLUGIN = SRC / "crux" / "plugin"
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"


class NoAnswerMechanismExists(CruxTestCase):
    def hooks_data(self):
        return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))

    def test_no_pretooluse_matcher_on_ask_user_question(self):
        """The only shape that could answer before the human does."""
        data = self.hooks_data()
        for group in data["hooks"].get("PreToolUse", []) or []:
            self.assertNotIn("AskUserQuestion", group.get("matcher", ""),
                             "Crux must not be able to answer for the user")

    def test_the_post_tool_recorder_is_required_not_forbidden(self):
        """Absence of the *name* would break provenance; only Pre must be gone."""
        data = self.hooks_data()
        post = [g.get("matcher", "") for g in data["hooks"].get("PostToolUse", [])]
        self.assertTrue(any("AskUserQuestion" in m for m in post),
                        "the human answer recorder must stay registered")

    def test_there_is_no_pretooluse_section_at_all_in_the_mvp(self):
        # ExitPlanMode and the blast-radius guard land in v0.2.
        self.assertNotIn("PreToolUse", self.hooks_data()["hooks"])

    def test_ask_user_question_is_only_observed_after_the_fact(self):
        data = self.hooks_data()
        matchers = [g.get("matcher", "")
                    for g in data["hooks"].get("PostToolUse", [])]
        self.assertIn("AskUserQuestion", matchers,
                      "we still need to capture the human's answer")

    def test_no_handler_can_produce_updated_input(self):
        source = (SRC / "crux" / "hooks.py").read_text(encoding="utf-8")
        self.assertNotIn("updatedInput", source)
        self.assertNotIn("permissionDecision", source)

    def test_default_questions_mode_is_human(self):
        self.assertEqual(
            config.load(self.repo).get("human.questions.mode"), "human")

    def test_advise_and_auto_are_accepted_by_config_but_unimplemented(self):
        """The config vocabulary exists; the mechanism does not. That is fine,
        and it is what keeps the MVP's guarantee airtight."""
        self.write_project_config("human: { questions: { mode: advise } }\n")
        cfg = config.load(self.repo)
        self.assertEqual(cfg.get("human.questions.mode"), "advise")
        for group in self.hooks_data()["hooks"].get("PreToolUse", []) or []:
            self.assertNotIn("AskUserQuestion", group.get("matcher", ""))


class HooksFileShape(CruxTestCase):
    """The shipped hooks.json against what Claude Code documents today."""

    def hooks_data(self):
        return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))

    def test_every_hook_uses_the_exec_form(self):
        """command + args, never a shell string: identical on Windows and Linux."""
        for event, groups in self.hooks_data()["hooks"].items():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertEqual(hook["type"], "command", event)
                    self.assertEqual(hook["command"], "crux", event)
                    self.assertIsInstance(hook["args"], list, event)
                    self.assertNotIn(" ", hook["command"])

    def test_no_hook_exceeds_twenty_seconds(self):
        """No hook calls Codex, so none needs a long timeout."""
        for event, groups in self.hooks_data()["hooks"].items():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertLessEqual(hook.get("timeout", 0), 20, event)

    def test_events_are_known_to_this_claude_code_version(self):
        known = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop",
                 "PreToolUse", "SubagentStop", "SessionEnd", "PreCompact"}
        for event in self.hooks_data()["hooks"]:
            self.assertIn(event, known)

    def test_every_declared_event_has_a_handler(self):
        args = []
        for groups in self.hooks_data()["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    args.append(hook["args"][1])
        for event in args:
            self.assertIn(event, hooks.HANDLERS, f"no handler for {event}")

    def test_plugin_manifest_points_at_the_hooks_file(self):
        manifest = json.loads(
            (PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], setupcmd.PLUGIN_NAME)
        self.assertEqual(manifest["hooks"], "./hooks/hooks.json")


class InstalledHooksArePinned(CruxTestCase):
    """The installed copy must not depend on PATH resolution.

    A bare `crux` that fails to launch produces a silent no-op, which looks
    exactly like a disarmed gate. The install pins an absolute path instead.
    """

    def install(self):
        import os
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")
        target, _ = setupcmd.install_plugin()
        return json.loads(
            (target / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    def test_installed_hooks_use_an_absolute_command(self):
        for groups in self.install()["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertTrue(Path(hook["command"]).is_absolute(),
                                    hook["command"])
                    self.assertNotEqual(hook["command"], "crux")

    def test_installed_hooks_keep_their_event_argument(self):
        events = []
        for groups in self.install()["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    events.append(hook["args"][-1])
        for event in hooks.HANDLERS:
            self.assertIn(event, events)

    def test_the_packaged_template_is_never_rewritten(self):
        self.install()
        data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        commands = {h["command"] for gs in data["hooks"].values()
                    for g in gs for h in g["hooks"]}
        self.assertEqual(commands, {"crux"})

    def test_install_registers_no_pretooluse_ask_matcher(self):
        for group in self.install()["hooks"].get("PreToolUse", []) or []:
            self.assertNotIn("AskUserQuestion", group.get("matcher", ""))

    def test_install_keeps_the_posttooluse_recorder(self):
        post = [g.get("matcher", "")
                for g in self.install()["hooks"].get("PostToolUse", [])]
        self.assertTrue(any("AskUserQuestion" in m for m in post))


if __name__ == "__main__":
    unittest.main()
