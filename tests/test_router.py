"""The eleven routing signals, against fixture diffs.

The router is a pure function, which is the whole point: selection is
deterministic, explainable, and testable without touching Codex.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from ._support import CruxTestCase

from crux import baseline, config, router

FIXTURES = Path(__file__).resolve().parent / "fixtures"

MVP = ("code-quality", "architecture", "security")


def ctx(**kwargs) -> router.RouteContext:
    base = dict(paths=["src/a.py"], added_text="", changed_lines=40,
                files_changed=1, project_has_tests=False)
    base.update(kwargs)
    return router.RouteContext(**base)


class SignalDetection(CruxTestCase):
    def select(self, context, project_cfg="", available=None):
        if project_cfg:
            self.write_project_config(project_cfg)
        cfg = config.load(self.repo)
        return router.select(context, cfg, available=available)

    def test_code_quality_is_always_selected(self):
        result = self.select(ctx())
        self.assertIn("code-quality", result.selected)
        self.assertIn("always", result.fired)

    def test_css_change_stays_minimal(self):
        """A CSS tweak must not summon four reviewers."""
        result = self.select(ctx(paths=["src/app.css"], changed_lines=6,
                                 files_changed=1))
        self.assertEqual(result.selected, ["code-quality"])
        self.assertTrue(result.micro_change)

    def test_auth_path_pulls_security(self):
        result = self.select(ctx(paths=["src/auth/login.py"]))
        self.assertIn("security", result.selected)
        self.assertIn("auth_surface", result.fired)

    def test_auth_keyword_in_added_lines_pulls_security(self):
        result = self.select(ctx(added_text="token = jwt.encode(payload)"))
        self.assertIn("security", result.selected)

    def test_a_removed_line_does_not_trigger_the_content_signal(self):
        """Only added lines are inspected: deleting a password check is not
        the same event as adding one."""
        result = self.select(ctx(added_text=""))
        self.assertNotIn("auth_surface", result.fired)

    def test_sql_concatenation_pulls_security(self):
        result = self.select(ctx(
            added_text='cur.execute(f"SELECT * FROM t WHERE id={uid}")'))
        self.assertIn("external_input", result.fired)

    def test_migration_pulls_architecture(self):
        result = self.select(ctx(paths=["db/migrations/003_add_col.sql"]))
        self.assertIn("architecture", result.selected)
        self.assertIn("schema_migration", result.fired)

    def test_manifest_change_is_noticed(self):
        result = self.select(ctx(paths=["package.json"]))
        self.assertIn("dependencies", result.fired)

    def test_magnitude_pulls_architecture(self):
        result = self.select(ctx(changed_lines=800, files_changed=12))
        self.assertIn("architecture", result.selected)
        self.assertIn("magnitude", result.fired)

    def test_net_removal_is_a_signal(self):
        """Revision 2 addition: deletion is the quietest form of drift."""
        result = self.select(ctx(deleted_files=["src/heic.py"]))
        self.assertIn("net_removal", result.fired)
        self.assertIn("architecture", result.selected)

    def test_large_negative_balance_also_fires(self):
        result = self.select(ctx(net_lines=-200))
        self.assertIn("net_removal", result.fired)

    def test_hot_path_fires_on_image_libraries(self):
        result = self.select(ctx(added_text="from PIL import Image"))
        self.assertIn("hot_path", result.fired)

    def test_never_list_removes_a_reviewer(self):
        result = self.select(ctx(paths=["src/auth/login.py"]),
                             project_cfg="reviewers: { never: [security] }\n")
        self.assertNotIn("security", result.selected)

    def test_cap_is_honoured(self):
        result = self.select(
            ctx(paths=["src/auth/login.py", "db/migrations/x.sql"],
                changed_lines=900, files_changed=20),
            project_cfg="reviewers: { max_selected: 2, "
                        "auto: [architecture, security, performance, tests] }\n")
        self.assertLessEqual(len(result.selected), 2)
        self.assertTrue(result.capped)

    def test_unavailable_personas_are_not_selected(self):
        """The MVP ships three personas; the router must not pick the others."""
        result = self.select(
            ctx(paths=["src/app.tsx"], added_text="from PIL import Image"),
            project_cfg="reviewers: { auto: [architecture, security, "
                        "performance, ux, tests, release] }\n",
            available=MVP)
        for reviewer in result.selected:
            self.assertIn(reviewer, MVP)

    def test_explain_names_the_reason(self):
        result = self.select(ctx(paths=["src/auth/login.py"]))
        text = result.explain()
        self.assertIn("security", text)
        self.assertIn("authentification", text)
        self.assertIn("Signaux déclenchés", text)

    def test_a_broken_signal_never_breaks_routing(self):
        def explode(_ctx):
            raise RuntimeError("boom")
        bad = router.Signal("bad", "signal cassé", {"security": 500}, explode)
        original = list(router.SIGNALS)
        router.SIGNALS.append(bad)
        try:
            result = self.select(ctx())
            self.assertIn("code-quality", result.selected)
            self.assertNotIn("bad", result.fired)
        finally:
            router.SIGNALS[:] = original


class RoutingFromRealDiffs(CruxTestCase):
    """End to end: working tree -> session diff -> routing."""

    def arm(self):
        cfg = config.load(self.repo)
        baseline.capture("s1", self.repo, cfg)
        return cfg

    def route_for(self, files):
        from crux import review, state
        cfg = self.arm()
        for relpath, content in files.items():
            self.write(relpath, content)
            state.record_edit("s1", relpath, None)
        diff = baseline.compute("s1", self.repo, cfg)
        context = router.build_context(diff, self.repo,
                                       review.project_has_tests(self.repo))
        return router.select(context, cfg, available=MVP)

    def setUp(self):
        super().setUp()
        self.write("src/app.py", "APP = 1\n")
        self.commit("init")

    def test_auth_diff_selects_security(self):
        result = self.route_for({
            "src/auth/session.py":
                "import jwt\n\n\ndef make(uid):\n    return jwt.encode({'u': uid})\n"})
        self.assertIn("security", result.selected)

    def test_tiny_diff_selects_only_code_quality(self):
        result = self.route_for({"src/app.py": "APP = 2\n"})
        self.assertEqual(result.selected, ["code-quality"])

    def test_deleting_a_module_selects_architecture(self):
        from crux import state
        cfg = self.arm()
        self.write("src/heic.py", "def decode():\n    return 1\n")
        self.commit("add heic")
        cfg = config.load(self.repo)
        baseline.capture("s2", self.repo, cfg)
        (self.repo / "src" / "heic.py").unlink()
        state.record_edit("s2", "src/heic.py", None)
        diff = baseline.compute("s2", self.repo, cfg)
        from crux import review
        context = router.build_context(diff, self.repo,
                                       review.project_has_tests(self.repo))
        result = router.select(context, cfg, available=MVP)
        self.assertIn("architecture", result.selected)
        self.assertIn("net_removal", result.fired)


class MicroChangeExclusions(CruxTestCase):
    """A small change is capped - unless it deletes something."""

    def select(self, context, project_cfg=""):
        if project_cfg:
            self.write_project_config(project_cfg)
        return router.select(context, config.load(self.repo), available=MVP)

    def test_a_tiny_deletion_is_never_a_micro_change(self):
        result = self.select(ctx(changed_lines=3, files_changed=1,
                                 deleted_files=["src/heic.py"]))
        self.assertFalse(result.micro_change)
        self.assertIn("architecture", result.selected)

    def test_a_signal_exactly_at_the_threshold_defeats_the_cap(self):
        """>= must match the selection rule, not >."""
        result = self.select(ctx(changed_lines=3, files_changed=1,
                                 paths=["package.json"]))
        self.assertFalse(result.micro_change)

    def test_an_ordinary_tiny_edit_is_still_capped(self):
        result = self.select(ctx(changed_lines=3, files_changed=1,
                                 paths=["src/app.py"]))
        self.assertTrue(result.micro_change)
        self.assertEqual(result.selected, ["code-quality"])


if __name__ == "__main__":
    unittest.main()
