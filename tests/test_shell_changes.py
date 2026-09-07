"""The session diff is anchored on the baseline, never on the edits journal.

Claude changes files through Bash all the time - sed, a Python one-liner, a
formatter, a codegen step - and `PostToolUse` on Edit|Write|NotebookEdit sees
none of it. Anchoring the candidate set on that journal silently excluded those
changes from review, which is exactly the class of change most worth reviewing.

    session diff = current working tree  −  state captured at arming

whatever produced the change. `edits.jsonl` survives only as provenance.
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from ._support import CruxTestCase, git

from crux import baseline, config, state


def changed_lines(diff_text: str) -> list:
    return [ln for ln in diff_text.splitlines()
            if (ln.startswith("+") and not ln.startswith("+++"))
            or (ln.startswith("-") and not ln.startswith("---"))]


class ChangesMadeOutsideEditTools(CruxTestCase):
    """No `record_edit` anywhere in these tests: that is the point."""

    def setUp(self):
        super().setUp()
        self.write("src/app.py", "VALUE = 1\nKEEP = 'yes'\n")
        self.write("src/other.py", "OTHER = 1\n")
        self.commit("init")
        self.cfg = config.load(self.repo)

    def arm(self, session="s1"):
        baseline.capture(session, self.repo, self.cfg)

    def shell_edit(self, relpath: str, new_content: str) -> None:
        """Simulate a change made through Bash: a real subprocess, no hook."""
        script = (
            "import pathlib, sys;"
            "p = pathlib.Path(sys.argv[1]);"
            "p.write_text(sys.argv[2], encoding='utf-8', newline='\\n')"
        )
        subprocess.run([sys.executable, "-c", script,
                        str(self.repo / relpath), new_content],
                       check=True, capture_output=True, shell=False)

    # --- 1. clean file, changed with no Edit hook ------------------------
    def test_clean_file_changed_via_shell_is_reviewed(self):
        self.arm()
        self.shell_edit("src/app.py", "VALUE = 2\nKEEP = 'yes'\n")
        diff = baseline.compute("s1", self.repo, self.cfg)
        self.assertIn("src/app.py", diff.files)
        self.assertIn("+VALUE = 2", changed_lines(diff.text))
        self.assertEqual(diff.provenance["src/app.py"], "unknown")

    # --- 2. dirty before arming, changed again via shell -----------------
    def test_only_the_delta_since_arming_appears(self):
        self.write("src/app.py", "VALUE = 1\nKEEP = 'yes'\nHUMAN_WIP = 1\n")
        self.arm()
        self.shell_edit(
            "src/app.py",
            "VALUE = 1\nKEEP = 'yes'\nHUMAN_WIP = 1\nSHELL_ADDED = 2\n")
        diff = baseline.compute("s1", self.repo, self.cfg)
        changed = changed_lines(diff.text)
        self.assertEqual(changed, ["+SHELL_ADDED = 2"])
        self.assertFalse([ln for ln in changed if "HUMAN_WIP" in ln])

    # --- 3. file created via shell ---------------------------------------
    def test_file_created_via_shell_is_detected(self):
        self.arm()
        self.shell_edit("src/generated.py", "GENERATED = True\n")
        diff = baseline.compute("s1", self.repo, self.cfg)
        self.assertIn("src/generated.py", diff.files)
        self.assertIn("src/generated.py", diff.new_files)
        self.assertIn("+GENERATED = True", changed_lines(diff.text))

    # --- 4. file deleted via shell ---------------------------------------
    def test_file_deleted_via_shell_is_detected(self):
        self.arm()
        (self.repo / "src" / "other.py").unlink()
        diff = baseline.compute("s1", self.repo, self.cfg)
        self.assertIn("src/other.py", diff.files)
        self.assertIn("src/other.py", diff.deleted_files)
        self.assertIn("-OTHER = 1", changed_lines(diff.text))

    # --- 5. an empty journal must not mean an empty review ---------------
    def test_no_journal_entries_still_yields_an_exact_review(self):
        self.arm()
        self.shell_edit("src/app.py", "VALUE = 99\nKEEP = 'yes'\n")
        self.assertEqual(state.edited_paths("s1"), [],
                         "this test is meaningless if the journal is populated")
        diff = baseline.compute("s1", self.repo, self.cfg)
        self.assertEqual(diff.files, ["src/app.py"])
        self.assertEqual(changed_lines(diff.text),
                         ["-VALUE = 1", "+VALUE = 99"])

    def test_a_git_command_moving_head_does_not_hide_shell_changes(self):
        self.arm()
        self.shell_edit("src/app.py", "VALUE = 2\nKEEP = 'yes'\n")
        self.commit("committed mid-session")
        self.shell_edit("src/app.py", "VALUE = 3\nKEEP = 'yes'\n")
        changed = changed_lines(baseline.compute("s1", self.repo, self.cfg).text)
        self.assertIn("-VALUE = 1", changed)
        self.assertIn("+VALUE = 3", changed)

    def test_reverting_a_prior_human_edit_is_itself_a_change(self):
        """Claude undoing your uncommitted work must not vanish from review."""
        self.write("src/app.py", "VALUE = 1\nKEEP = 'yes'\nHUMAN_WIP = 1\n")
        self.arm()
        self.shell_edit("src/app.py", "VALUE = 1\nKEEP = 'yes'\n")  # back to HEAD
        diff = baseline.compute("s1", self.repo, self.cfg)
        self.assertIn("src/app.py", diff.files)
        self.assertIn("-HUMAN_WIP = 1", changed_lines(diff.text))


class ProvenanceIsInformationalOnly(CruxTestCase):
    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")
        self.cfg = config.load(self.repo)
        baseline.capture("s1", self.repo, self.cfg)

    def test_tool_and_shell_changes_are_both_reviewed_and_labelled(self):
        self.write("a.py", "A = 2\n")
        state.record_edit("s1", "a.py", None)
        self.write("b.py", "B = 1\n")            # no journal entry
        diff = baseline.compute("s1", self.repo, self.cfg)
        self.assertEqual(sorted(diff.files), ["a.py", "b.py"])
        self.assertEqual(diff.provenance["a.py"], "tool")
        self.assertEqual(diff.provenance["b.py"], "unknown")

    def test_the_warning_names_the_unattributed_files(self):
        self.write("mystery.py", "M = 1\n")
        diff = baseline.compute("s1", self.repo, self.cfg)
        warning = [w for w in diff.warnings if "provenance inconnue" in w]
        self.assertTrue(warning)
        self.assertIn("mystery.py", warning[0])


class ScopeSessionEditsOnlySemantics(CruxTestCase):
    """The flag selects the reference point, not the candidate set.

    true  -> compare against the session baseline (your prior work excluded)
    false -> compare against the captured commit  (your prior work included)
    """

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")

    def test_true_excludes_pre_session_work(self):
        self.write("a.py", "A = 1\nHUMAN_WIP = 1\n")
        cfg = config.load(self.repo)
        baseline.capture("s1", self.repo, cfg)
        self.write("a.py", "A = 1\nHUMAN_WIP = 1\nLATER = 2\n")
        changed = changed_lines(baseline.compute("s1", self.repo, cfg).text)
        self.assertEqual(changed, ["+LATER = 2"])

    def test_false_includes_pre_session_work(self):
        self.write("a.py", "A = 1\nHUMAN_WIP = 1\n")
        self.write_project_config("scope: { session_edits_only: false }\n")
        cfg = config.load(self.repo)
        baseline.capture("s2", self.repo, cfg)
        self.write("a.py", "A = 1\nHUMAN_WIP = 1\nLATER = 2\n")
        changed = changed_lines(baseline.compute("s2", self.repo, cfg).text)
        self.assertIn("+HUMAN_WIP = 1", changed)
        self.assertIn("+LATER = 2", changed)


if __name__ == "__main__":
    unittest.main()
