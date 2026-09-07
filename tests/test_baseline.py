"""Invariant 8.

Modifications present before the session starts are excluded from the session
diff, even when Claude later edits the same file.

Plus the read-only git guarantee, which is structural rather than declarative.
"""

from __future__ import annotations

import unittest

from ._support import CruxTestCase, git

from crux import baseline, config, gitctx, state


def changed_lines(diff_text: str) -> list:
    """Only +/- lines. Context lines legitimately carry the human's prior work:
    a unified diff shows surroundings, and the reviewer needs them. The invariant
    is that prior edits are never presented as part of what Claude changed."""
    return [ln for ln in diff_text.splitlines()
            if (ln.startswith("+") and not ln.startswith("+++"))
            or (ln.startswith("-") and not ln.startswith("---"))]


class ReadOnlyGit(CruxTestCase):
    def test_writing_subcommands_raise_before_running(self):
        for sub in ("commit", "reset", "checkout", "stash", "clean",
                    "restore", "add", "rm", "push", "hash-object"):
            with self.assertRaises(gitctx.GitSafetyError, msg=sub):
                gitctx.run(self.repo, sub, "--whatever")

    def test_unknown_subcommand_raises(self):
        with self.assertRaises(gitctx.GitSafetyError):
            gitctx.run(self.repo, "frobnicate")

    def test_forbidden_and_allowed_sets_are_disjoint(self):
        self.assertFalse(
            gitctx.ALLOWED_SUBCOMMANDS & gitctx.FORBIDDEN_SUBCOMMANDS)

    def test_read_only_subcommands_work(self):
        self.write("a.txt", "hello\n")
        self.commit("init")
        self.assertTrue(gitctx.is_repo(self.repo))
        self.assertIsNotNone(gitctx.head_sha(self.repo))
        self.assertEqual(gitctx.current_branch(self.repo), "main")


class BaselineCapture(CruxTestCase):
    def setUp(self):
        super().setUp()
        self.write("keep.py", "def keep():\n    return 1\n")
        self.write("shared.py", "LINE_A = 1\n")
        self.commit("init")
        self.cfg = config.load(self.repo)

    def test_clean_tree_snapshots_nothing(self):
        manifest = baseline.capture("s1", self.repo, self.cfg)
        self.assertEqual(manifest.entries, {})
        self.assertIsNotNone(manifest.head)

    def test_dirty_files_are_snapshotted_byte_for_byte(self):
        self.write("shared.py", "LINE_A = 1\nMINE = 'human edit'\n")
        manifest = baseline.capture("s1", self.repo, self.cfg)
        self.assertIn("shared.py", manifest.entries)
        stored = baseline.baseline_bytes("s1", self.repo, "shared.py", manifest)
        self.assertEqual(stored.decode(), "LINE_A = 1\nMINE = 'human edit'\n")

    def test_capture_is_idempotent(self):
        self.write("shared.py", "LINE_A = 1\nMINE = 1\n")
        first = baseline.capture("s1", self.repo, self.cfg)
        self.write("shared.py", "LINE_A = 1\nMINE = 1\nCLAUDE = 2\n")
        second = baseline.capture("s1", self.repo, self.cfg)
        self.assertEqual(first.captured_at, second.captured_at)
        stored = baseline.baseline_bytes("s1", self.repo, "shared.py", second)
        self.assertNotIn("CLAUDE", stored.decode())

    def test_oversized_file_is_skipped_loudly(self):
        self.write_project_config(
            "scope: { baseline: { max_file_bytes: 10 } }\n")
        cfg = config.load(self.repo)
        self.write("shared.py", "LINE_A = 1\n" + "x" * 500 + "\n")
        manifest = baseline.capture("s1", self.repo, cfg)
        entry = manifest.entries["shared.py"]
        self.assertIsNone(entry.blob)
        self.assertIsNotNone(entry.skipped)
        self.assertTrue(any("trop volumineux" in w for w in manifest.warnings))


class SessionDiffScoping(CruxTestCase):
    """The case a fingerprint could never handle."""

    def setUp(self):
        super().setUp()
        self.write("shared.py", "LINE_A = 1\n")
        self.write("other.py", "OTHER = 1\n")
        self.commit("init")

    def arm(self, session="s1"):
        cfg = config.load(self.repo)
        baseline.capture(session, self.repo, cfg)
        return cfg

    def test_prior_human_edits_excluded_even_in_a_file_claude_then_edits(self):
        # you were mid-edit before arming
        self.write("shared.py", "LINE_A = 1\nHUMAN_WIP = 'mine'\n")
        cfg = self.arm()
        # Claude then edits the very same file
        self.write("shared.py",
                   "LINE_A = 1\nHUMAN_WIP = 'mine'\nCLAUDE_ADDED = 'his'\n")
        state.record_edit("s1", "shared.py", None)

        diff = baseline.compute("s1", self.repo, cfg)
        changed = changed_lines(diff.text)
        self.assertEqual(changed, ["+CLAUDE_ADDED = 'his'"])
        self.assertFalse([ln for ln in changed if "HUMAN_WIP" in ln],
                         "the human's prior edit must not be attributed to Claude")
        self.assertIn("HUMAN_WIP", diff.text)   # present, but as context only
        self.assertEqual(diff.files, ["shared.py"])
        self.assertEqual(diff.added, 1)
        self.assertEqual(diff.removed, 0)

    def test_plain_git_diff_would_have_included_the_human_edit(self):
        """Guards against a regression to the naive HEAD-based diff."""
        self.write("shared.py", "LINE_A = 1\nHUMAN_WIP = 'mine'\n")
        cfg = self.arm()
        self.write("shared.py",
                   "LINE_A = 1\nHUMAN_WIP = 'mine'\nCLAUDE_ADDED = 'his'\n")
        state.record_edit("s1", "shared.py", None)
        naive = gitctx.working_tree_diff(self.repo, "HEAD")
        self.assertIn("+HUMAN_WIP = 'mine'", changed_lines(naive))   # must not do
        self.assertNotIn("+HUMAN_WIP = 'mine'",                      # what we do
                         changed_lines(baseline.compute("s1", self.repo, cfg).text))

    def test_files_never_changed_stay_out(self):
        cfg = self.arm()
        self.write("shared.py", "LINE_A = 1\nC = 1\n")
        state.record_edit("s1", "shared.py", None)
        diff = baseline.compute("s1", self.repo, cfg)
        self.assertEqual(diff.files, ["shared.py"])
        self.assertNotIn("other.py", diff.files)

    def test_a_change_with_no_tool_provenance_is_still_reviewed(self):
        """Anything differing from the baseline is in scope, whatever made it.

        Claude edits through Bash (sed, a Python one-liner) and the human edits
        in their own editor; PostToolUse on Edit/Write sees neither. Anchoring
        the candidate set on the journal dropped those changes from review.
        """
        cfg = self.arm()
        self.write("other.py", "OTHER = 2\n")     # no record_edit at all
        self.write("shared.py", "LINE_A = 1\nC = 1\n")
        state.record_edit("s1", "shared.py", None)
        diff = baseline.compute("s1", self.repo, cfg)
        self.assertIn("other.py", diff.files)
        self.assertEqual(diff.provenance["other.py"], "unknown")
        self.assertEqual(diff.provenance["shared.py"], "tool")
        self.assertTrue(any("provenance inconnue" in w for w in diff.warnings))

    def test_new_file_created_by_claude(self):
        cfg = self.arm()
        self.write("brand_new.py", "NEW = 1\n")
        state.record_edit("s1", "brand_new.py", None)
        diff = baseline.compute("s1", self.repo, cfg)
        self.assertIn("brand_new.py", diff.new_files)
        self.assertIn("+NEW = 1", diff.text)
        self.assertIn("--- /dev/null", diff.text)

    def test_file_deleted_by_claude(self):
        cfg = self.arm()
        (self.repo / "other.py").unlink()
        state.record_edit("s1", "other.py", None)
        diff = baseline.compute("s1", self.repo, cfg)
        self.assertIn("other.py", diff.deleted_files)
        self.assertIn("-OTHER = 1", diff.text)

    def test_no_change_yields_empty_diff(self):
        cfg = self.arm()
        state.record_edit("s1", "shared.py", None)
        self.assertTrue(baseline.compute("s1", self.repo, cfg).is_empty)

    def test_excluded_globs_are_dropped(self):
        cfg = self.arm()
        self.write("package-lock.json", '{"a": 1}\n')
        state.record_edit("s1", "package-lock.json", None)
        self.assertTrue(baseline.compute("s1", self.repo, cfg).is_empty)

    def test_diff_headers_are_repo_relative(self):
        cfg = self.arm()
        self.write("shared.py", "LINE_A = 2\n")
        state.record_edit("s1", "shared.py", None)
        text = baseline.compute("s1", self.repo, cfg).text
        self.assertIn("diff --git a/shared.py b/shared.py", text)
        self.assertIn("--- a/shared.py", text)
        self.assertIn("+++ b/shared.py", text)
        self.assertNotIn("crux-baseline-", text)   # no temp paths leak

    def test_fingerprint_is_stable_and_changes_with_content(self):
        cfg = self.arm()
        self.write("shared.py", "LINE_A = 2\n")
        state.record_edit("s1", "shared.py", None)
        first = baseline.compute("s1", self.repo, cfg)
        again = baseline.compute("s1", self.repo, cfg)
        self.assertEqual(first.fingerprint, again.fingerprint)
        self.write("shared.py", "LINE_A = 3\n")
        self.assertNotEqual(
            first.fingerprint, baseline.compute("s1", self.repo, cfg).fingerprint)

    def test_human_edit_after_claude_is_flagged(self):
        import hashlib
        cfg = self.arm()
        content = "LINE_A = 1\nCLAUDE = 1\n"
        self.write("shared.py", content)
        state.record_edit("s1", "shared.py",
                          hashlib.sha256(content.encode()).hexdigest())
        self.write("shared.py", content + "HUMAN_AFTER = 1\n")
        diff = baseline.compute("s1", self.repo, cfg)
        self.assertTrue(any("hors session" in w for w in diff.warnings))

    def test_session_scoping_can_be_disabled(self):
        self.write("shared.py", "LINE_A = 1\nHUMAN_WIP = 1\n")
        self.write_project_config("scope: { session_edits_only: false }\n")
        cfg = config.load(self.repo)
        baseline.capture("s1", self.repo, cfg)
        self.write("other.py", "OTHER = 9\n")
        diff = baseline.compute("s1", self.repo, cfg)
        self.assertIn("other.py", diff.files)


class BaselineIsPinnedToTheCapturedCommit(CruxTestCase):
    """A commit made mid-session must not move the reference point.

    Resolving a clean file against the live HEAD would silently drop earlier
    session work out of the diff the moment anything got committed.
    """

    def setUp(self):
        super().setUp()
        self.write("stable.py", "VERSION = 1\n")
        self.commit("c1")
        self.first_head = git(self.repo, "rev-parse", "HEAD").strip()

    def test_reference_stays_at_the_captured_head_after_a_commit(self):
        cfg = config.load(self.repo)
        manifest = baseline.capture("s1", self.repo, cfg)
        self.assertEqual(manifest.head, self.first_head)

        # Claude edits, then something commits mid-session (Claude or the human)
        self.write("stable.py", "VERSION = 2\n")
        state.record_edit("s1", "stable.py", None)
        self.commit("c2 mid-session")
        self.assertNotEqual(git(self.repo, "rev-parse", "HEAD").strip(),
                            self.first_head)

        # ...and Claude keeps working
        self.write("stable.py", "VERSION = 3\n")

        changed = changed_lines(baseline.compute("s1", self.repo, cfg).text)
        # The whole session is still visible: 1 -> 3, not 2 -> 3.
        self.assertIn("-VERSION = 1", changed)
        self.assertIn("+VERSION = 3", changed)
        self.assertNotIn("-VERSION = 2", changed)

    def test_baseline_bytes_reads_the_captured_commit_not_head(self):
        cfg = config.load(self.repo)
        manifest = baseline.capture("s1", self.repo, cfg)
        self.write("stable.py", "VERSION = 99\n")
        self.commit("moves HEAD")
        content = baseline.baseline_bytes("s1", self.repo, "stable.py", manifest)
        self.assertEqual(content.decode(), "VERSION = 1\n")

    def test_a_branch_switch_does_not_move_the_baseline(self):
        cfg = config.load(self.repo)
        baseline.capture("s1", self.repo, cfg)
        self.write("stable.py", "VERSION = 2\n")
        state.record_edit("s1", "stable.py", None)
        self.commit("c2")
        git(self.repo, "checkout", "-q", "-b", "side")
        self.write("stable.py", "VERSION = 4\n")
        changed = changed_lines(baseline.compute("s1", self.repo, cfg).text)
        self.assertIn("-VERSION = 1", changed)

    def test_no_captured_head_means_no_phantom_baseline(self):
        """A repository with no commit yet: nothing to compare a clean file to."""
        empty = self.tmp / "empty"
        empty.mkdir()
        git(empty, "init", "-q", "-b", "main")
        git(empty, "config", "user.email", "t@t.invalid")
        git(empty, "config", "user.name", "T")
        cfg = config.load(empty)
        manifest = baseline.capture("s-empty", empty, cfg)
        self.assertIsNone(manifest.head)
        self.assertEqual(
            baseline.baseline_bytes("s-empty", empty, "nope.py", manifest), b"")

    def test_gitctx_show_blob_takes_an_explicit_ref(self):
        self.write("stable.py", "VERSION = 2\n")
        self.commit("c2")
        old = gitctx.show_blob(self.repo, self.first_head, "stable.py")
        new = gitctx.show_blob(self.repo, "HEAD", "stable.py")
        self.assertEqual(old.decode(), "VERSION = 1\n")
        self.assertEqual(new.decode(), "VERSION = 2\n")


if __name__ == "__main__":
    unittest.main()
