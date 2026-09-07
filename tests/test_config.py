"""Configuration cascade, provenance, and the `human` defaults."""

from __future__ import annotations

import unittest

from ._support import CruxTestCase

from crux import config, paths


class ConfigDefaults(CruxTestCase):
    def test_human_defaults_are_human(self):
        cfg = config.load(self.repo)
        self.assertEqual(cfg.get("human.questions.mode"), "human")
        self.assertEqual(cfg.get("human.scope_changes.mode"), "ask")
        self.assertEqual(cfg.get("gate.mode"), "off")

    def test_no_project_file_means_defaults(self):
        cfg = config.load(self.repo)
        self.assertIsNone(cfg.project_config)
        self.assertEqual(cfg.source_of("gate.mode"), "défaut intégré")

    def test_minimal_project_config(self):
        self.write_project_config(
            "version: 1\ngate: { mode: code }\ntests: { command: npm test }\n")
        cfg = config.load(self.repo)
        self.assertEqual(cfg.get("gate.mode"), "code")
        self.assertEqual(cfg.get("tests.command"), "npm test")
        # untouched keys keep their defaults
        self.assertEqual(cfg.get("gate.max_rounds"), 2)
        self.assertEqual(cfg.get("human.scope_changes.mode"), "ask")


class ConfigCascade(CruxTestCase):
    def test_project_overrides_user(self):
        paths.ensure_dir(self.crux_home)
        paths.config_path().write_text(
            "gate: { mode: both, max_rounds: 5 }\n", encoding="utf-8")
        self.write_project_config("gate: { mode: code }\n")
        cfg = config.load(self.repo)
        self.assertEqual(cfg.get("gate.mode"), "code")
        self.assertEqual(cfg.get("gate.max_rounds"), 5)
        self.assertTrue(cfg.source_of("gate.mode").endswith(".crux.yml"))
        self.assertTrue(cfg.source_of("gate.max_rounds").endswith("config.yml"))

    def test_lists_replace_rather_than_merge(self):
        self.write_project_config("reviewers: { auto: [security] }\n")
        cfg = config.load(self.repo)
        self.assertEqual(cfg.get("reviewers.auto"), ["security"])

    def test_config_found_from_a_subdirectory(self):
        self.write_project_config("gate: { mode: code }\n")
        sub = self.repo / "src" / "deep"
        sub.mkdir(parents=True)
        cfg = config.load(sub)
        self.assertEqual(cfg.get("gate.mode"), "code")


class ConfigValidation(CruxTestCase):
    def _expect_error(self, body: str, key: str):
        self.write_project_config(body)
        with self.assertRaises(config.ConfigError) as ctx:
            config.load(self.repo)
        self.assertEqual(ctx.exception.key, key)
        self.assertIn(".crux.yml", str(ctx.exception))

    def test_bad_gate_mode(self):
        self._expect_error("gate: { mode: sometimes }\n", "gate.mode")

    def test_bad_questions_mode(self):
        self._expect_error("human: { questions: { mode: robot } }\n",
                           "human.questions.mode")

    def test_bad_rounds(self):
        self._expect_error("gate: { max_rounds: 0 }\n", "gate.max_rounds")

    def test_unknown_reviewer(self):
        self._expect_error("reviewers: { auto: [telepathy] }\n", "reviewers.auto")

    def test_unsupported_version(self):
        self._expect_error("version: 2\n", "version")

    def test_broken_yaml_names_the_file(self):
        self.write_project_config("gate: { mode: code\n")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load(self.repo)
        self.assertIn(".crux.yml", str(ctx.exception))

    def test_questions_and_scope_are_independent(self):
        # the combination the design explicitly promises
        self.write_project_config(
            "human:\n  questions: { mode: auto }\n  scope_changes: { mode: ask }\n")
        cfg = config.load(self.repo)
        self.assertEqual(cfg.get("human.questions.mode"), "auto")
        self.assertEqual(cfg.get("human.scope_changes.mode"), "ask")



class YamlBooleanFolding(CruxTestCase):
    """`mode: off` is the documented spelling; YAML 1.1 folds it to False."""

    def test_bare_off_is_a_mode_not_a_boolean(self):
        self.write_project_config("gate: { mode: off }\n")
        cfg = config.load(self.repo)
        self.assertEqual(cfg.get("gate.mode"), "off")

    def test_quoted_off_also_works(self):
        self.write_project_config('gate: { mode: "off" }\n')
        self.assertEqual(config.load(self.repo).get("gate.mode"), "off")

    def test_genuinely_boolean_keys_keep_booleans(self):
        self.write_project_config(
            "human: { intent: { send_to_reviewers: false } }\n")
        cfg = config.load(self.repo)
        self.assertIs(cfg.get("human.intent.send_to_reviewers"), False)

    def test_session_edits_only_stays_boolean(self):
        self.write_project_config("scope: { session_edits_only: false }\n")
        self.assertIs(config.load(self.repo).get("scope.session_edits_only"), False)


if __name__ == "__main__":
    unittest.main()
