"""Real Codex CLI, end to end. Skipped cleanly when codex is not installed.

Exercises what the stubbed suite cannot: the actual subprocess, the real
`--output-schema` round trip, and the read-only guarantee.

Slow by nature (a review takes ~1-2 minutes). Run it on its own with:
    PYTHONPATH=src python -m unittest tests.test_codex_integration
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from ._support import CruxTestCase

from crux import (baseline, capabilities, codex, config, decisions, findings,
                  review, state)

CODEX = capabilities.resolve_binary("codex")
REASON = "codex CLI absent du PATH : test d'intégration ignoré"


def repo_fingerprint(repo: Path) -> str:
    """Hash of every tracked and untracked file, .git excluded."""
    digest = hashlib.sha256()
    for path in sorted(p for p in repo.rglob("*")
                       if p.is_file() and ".git" not in p.parts):
        digest.update(path.relative_to(repo).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@unittest.skipUnless(CODEX, REASON)
class CodexCapability(CruxTestCase):
    def test_binary_resolves_to_an_executable_shim(self):
        self.assertTrue(Path(CODEX).is_file())
        self.assertNotEqual(Path(CODEX).suffix.lower(), ".ps1",
                            "a .ps1 shim is not a Win32 executable")

    def test_version_is_readable_without_a_shell(self):
        self.assertIsNotNone(capabilities.codex_version())

    def test_auth_is_a_chatgpt_session_not_an_api_key(self):
        self.assertTrue(capabilities.codex_logged_in())

    def test_output_schema_probe_succeeds_inside_a_repository(self):
        """The probe must reproduce real conditions: Codex needs a repo."""
        self.write("a.py", "A = 1\n")
        self.commit("init")
        cfg = config.load(self.repo)
        result = capabilities.probe_output_schema(cfg, repo=self.repo)
        # A transient failure is not a verdict: skip rather than claim the
        # capability is absent, which is the whole point of the tri-state.
        if result.get("status") == capabilities.INDETERMINATE:
            self.skipTest(f"sonde indéterminée : {result.get('reason')}")
        self.assertEqual(result.get("status"), capabilities.SUPPORTED,
                         f"sonde échouée : {result.get('reason')}")


@unittest.skipUnless(CODEX, REASON)
class RealReviewRoundTrip(CruxTestCase):
    """One genuine review: real diff in, structured JSON out, repo untouched."""

    def setUp(self):
        super().setUp()
        self.write("src/formats.py",
                   '"""Formats."""\n\nSUPPORTED = ["jpeg", "png", "heic"]\n\n\n'
                   'def is_supported(name):\n'
                   '    return name.rsplit(".", 1)[-1].lower() in SUPPORTED\n')
        self.write("src/auth.py",
                   "import hashlib\n\n\ndef check_password(stored, candidate):\n"
                   "    return hashlib.md5(candidate.encode()).hexdigest() == stored\n")
        self.commit("init")

        self.cfg = config.load(self.repo)
        baseline.capture("int-1", self.repo, self.cfg)

        from crux import intent
        intent.record_prompt(
            "int-1", "Corrige la comparaison de mot de passe dans auth.py. "
                     "Ne touche à rien d'autre.")

        # the fix, plus an unrequested removal: the canonical drift
        self.write("src/auth.py",
                   "import hashlib\nimport hmac\n\n\n"
                   "def check_password(stored, candidate):\n"
                   "    digest = hashlib.md5(candidate.encode()).hexdigest()\n"
                   "    return hmac.compare_digest(digest, stored)\n")
        self.write("src/formats.py",
                   '"""Formats."""\n\nSUPPORTED = ["jpeg", "png"]\n\n\n'
                   'def is_supported(name):\n'
                   '    return name.rsplit(".", 1)[-1].lower() in SUPPORTED\n')
        for rel in ("src/auth.py", "src/formats.py"):
            state.record_edit("int-1", rel, None)

    def test_full_review_against_real_codex(self):
        before = repo_fingerprint(self.repo)

        result = review.run(
            "int-1", self.repo, self.cfg,
            only=["code-quality"],          # one reviewer keeps this affordable
            task_intent="Corriger uniquement la comparaison de mot de passe.")

        # --- a real subprocess actually ran -----------------------------
        self.assertIsNone(result.codex_error, result.codex_error)
        self.assertEqual(result.selected, ["code-quality"])
        reviewer = result.reviewer_results[0]
        self.assertTrue(reviewer.ok,
                        f"{reviewer.error_kind}: {reviewer.error}")

        # --- structured output was really parsed ------------------------
        self.assertIn(reviewer.verdict, ("approve", "changes_requested"))
        self.assertTrue(reviewer.summary.strip())
        self.assertIsNotNone(reviewer.scope, "the authority must fill `scope`")
        self.assertIn(reviewer.scope.status, findings.SCOPE_STATUSES)

        # --- the read-only guarantee ------------------------------------
        self.assertEqual(before, repo_fingerprint(self.repo),
                         "Codex must not modify a single byte of the repository")

        # --- the run was archived for audit -----------------------------
        from crux import paths
        run_dir = paths.runs_root() / result.run_id
        self.assertTrue((run_dir / "run.json").is_file())
        self.assertTrue((run_dir / "context" / "code-quality.md").is_file())

    def test_the_invocation_carries_the_read_only_sandbox(self):
        argv = codex.build_argv(self.cfg, self.repo, Path("out.json"), None)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertNotIn("--skip-git-repo-check", argv)
        self.assertTrue(Path(argv[0]).is_file())


@unittest.skipUnless(CODEX, REASON)
class DriftDetectionAgainstRealCodex(CruxTestCase):
    """The reviewer sees the approved scope and flags what was not asked for.

    Asserted loosely on purpose: a model's wording is not a stable contract.
    What must hold is the mechanism - a scope verdict comes back, and anything
    it marks `requires_human_decision` becomes a decision Claude cannot close.
    """

    def test_a_removed_capability_reaches_the_decision_ledger(self):
        self.write("src/formats.py",
                   'SUPPORTED = ["jpeg", "png", "heic"]\n\n\n'
                   'def is_supported(name):\n'
                   '    return name.rsplit(".", 1)[-1].lower() in SUPPORTED\n')
        self.commit("init")
        cfg = config.load(self.repo)
        baseline.capture("int-2", self.repo, cfg)

        from crux import intent
        intent.record_prompt("int-2",
                             "Renomme la fonction is_supported en accepts. "
                             "Ne change aucun comportement.")
        self.write("src/formats.py",
                   'SUPPORTED = ["jpeg", "png"]\n\n\n'
                   'def accepts(name):\n'
                   '    return name.rsplit(".", 1)[-1].lower() in SUPPORTED\n')
        state.record_edit("int-2", "src/formats.py", None)

        result = review.run("int-2", self.repo, cfg, only=["code-quality"],
                            task_intent="Renommage seul, aucun changement de "
                                        "comportement.")
        self.assertIsNone(result.codex_error)
        reviewer = result.reviewer_results[0]
        self.assertTrue(reviewer.ok, f"{reviewer.error_kind}: {reviewer.error}")
        self.assertIsNotNone(reviewer.scope)

        if reviewer.scope.is_change and reviewer.scope.human_changes():
            opened = decisions.blocking("int-2")
            self.assertTrue(opened, "a scope_change must open a decision")
            for decision in opened:
                self.assertEqual(decision.status, decisions.PENDING)
                self.assertTrue(decision.origin.startswith("codex:"))
                self.assertTrue(decision.title.strip())
                self.assertFalse(decision.title.endswith(" "))
        else:
            self.skipTest(
                f"le reviewer n'a pas qualifié la dérive ce coup-ci "
                f"(status={reviewer.scope.status}) — mécanisme non exercé")


if __name__ == "__main__":
    unittest.main()
