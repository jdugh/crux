"""End to end orchestration, with Codex stubbed at the subprocess boundary.

The Codex invocation itself is asserted separately (flags, parsing), and the one
test that needs a real `codex` binary is skipped when it is absent - which it
currently is on this machine.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from ._support import CruxTestCase

from crux import (baseline, capabilities, codex, config, decisions, findings,
                  intent, paths, review, state)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CODEX_PRESENT = capabilities.codex_available()


def canned(reviewer: str, payload: dict) -> codex.CodexOutcome:
    return codex.CodexOutcome(reviewer=reviewer, payload=payload, duration=0.1)


class CodexInvocation(CruxTestCase):
    """The command line, without running anything."""

    def argv(self, project_cfg=""):
        if project_cfg:
            self.write_project_config(project_cfg)
        cfg = config.load(self.repo)
        return codex.build_argv(cfg, self.repo, Path("/tmp/out.json"),
                                Path("/tmp/schema.json"))

    def test_sandbox_is_read_only(self):
        argv = self.argv()
        self.assertIn("--sandbox", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")

    def test_skip_git_repo_check_is_absent(self):
        """Documented as unnecessary inside a repo, and Crux requires one."""
        self.assertNotIn("--skip-git-repo-check", self.argv())

    def test_prompt_arrives_on_stdin(self):
        self.assertEqual(self.argv()[-1], "-")

    def test_output_schema_and_output_file_are_passed(self):
        argv = self.argv()
        self.assertIn("--output-schema", argv)
        self.assertIn("-o", argv)
        self.assertIn("--ephemeral", argv)

    def test_no_model_is_forced_by_default(self):
        self.assertNotIn("--model", self.argv())

    def test_model_is_passed_when_configured(self):
        argv = self.argv("codex: { model: gpt-5-codex }\n")
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5-codex")

    def test_no_branch_ever_tests_a_model_name(self):
        """Capabilities are probed, never inferred (ARCHITECTURE.md §18).

        Executable code only: the prose explaining *why* we don't branch on a
        model name legitimately mentions one.
        """
        import ast
        source = (Path(__file__).resolve().parents[1] / "src" / "crux")
        for path in source.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node, clean=False)
                    if doc:
                        docstrings.add(doc)
            code = text
            for doc in docstrings:
                code = code.replace(doc, "")
            code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
            for family in ("gpt-5", "gpt-4", "o3", "claude-opus", "sonnet"):
                self.assertNotIn(
                    family, code.replace("gpt-5-codex", ""),
                    f"{path.name} appears to branch on the model name {family!r}")


class CodexParsing(CruxTestCase):
    def test_plain_json(self):
        self.assertEqual(codex.extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        text = 'Voici :\n```json\n{"a": 2}\n```\nvoilà.'
        self.assertEqual(codex.extract_json(text), {"a": 2})

    def test_json_embedded_in_prose(self):
        self.assertEqual(codex.extract_json('blah {"a": 3} blah'), {"a": 3})

    def test_unparseable_returns_none(self):
        self.assertIsNone(codex.extract_json("désolé, je n'ai pas compris"))

    def test_quota_errors_are_classified_apart(self):
        kind, _ = codex._classify(1, "Error: 429 rate limit exceeded", "")
        self.assertEqual(kind, codex.QUOTA)

    def test_auth_errors_are_classified_apart(self):
        kind, _ = codex._classify(1, "not logged in, run codex login", "")
        self.assertEqual(kind, codex.AUTH)


class ReviewOrchestration(CruxTestCase):
    def setUp(self):
        super().setUp()
        self.write("src/app.py", "APP = 1\n")
        self.write("src/formats.py",
                   "SUPPORTED = ['jpeg', 'png', 'heic']\n")
        self.commit("init")
        self.cfg = config.load(self.repo)
        baseline.capture("s1", self.repo, self.cfg)
        intent.record_prompt("s1", "Refais le parseur, garde tous les formats.")

        # Claude removes HEIC support - the canonical drift case
        self.write("src/formats.py", "SUPPORTED = ['jpeg', 'png']\n")
        state.record_edit("s1", "src/formats.py", None)

        self._real = codex.run_many
        codex.run_many = self.fake_run_many
        self.addCleanup(setattr, codex, "run_many", self._real)

        self._ensure = codex.ensure_available
        codex.ensure_available = lambda cfg: None
        self.addCleanup(setattr, codex, "ensure_available", self._ensure)

    def fake_run_many(self, *args, **kwargs):
        jobs = args[0]
        self.sent_packs = {reviewer: prompt for reviewer, prompt in jobs}
        out = []
        for reviewer, _prompt in jobs:
            payload = {
                "verdict": "changes_requested",
                "reviewer": reviewer,
                "summary": f"résumé {reviewer}",
                "findings": [{
                    "severity": "high", "confidence": "high",
                    "title": f"Bug détecté par {reviewer}",
                    "description": "Description technique.",
                    "file": "src/app.py", "line": 1,
                }],
            }
            if reviewer == "code-quality":
                payload["scope"] = {
                    "status": "scope_change",
                    "assessment": "Le support HEIC disparaît.",
                    "changes": [{
                        "kind": "removed_capability",
                        "description": "L'implémentation retire le support HEIC.",
                        "evidence": "src/formats.py:1",
                        "alternatives": ["Conserver HEIC"],
                        "requires_human_decision": True,
                    }],
                }
            out.append(canned(reviewer, payload))
        return out

    def run_review(self, **kwargs):
        return review.run("s1", self.repo, self.cfg, **kwargs)

    def test_a_review_produces_findings_and_a_report(self):
        result = self.run_review()
        self.assertTrue(result.selected)
        self.assertTrue(result.all_findings)
        self.assertEqual(result.round, 1)
        run_json = paths.runs_root() / result.run_id / "run.json"
        self.assertTrue(run_json.is_file())
        self.assertTrue((paths.runs_root() / result.run_id / "report.md").is_file())

    def test_context_packs_are_archived_for_audit(self):
        result = self.run_review()
        ctx_dir = paths.runs_root() / result.run_id / "context"
        self.assertTrue(any(ctx_dir.glob("*.md")))

    def test_scope_change_opens_a_human_decision(self):
        result = self.run_review()
        self.assertTrue(result.opened_decisions)
        opened = decisions.blocking("s1")
        self.assertTrue(opened)
        self.assertIn("HEIC", opened[0].title + opened[0].why)
        self.assertEqual(opened[0].status, decisions.PENDING)
        self.assertTrue(opened[0].origin.startswith("codex:"))

    def test_the_opened_decision_cannot_be_closed_by_claude(self):
        self.run_review()
        decision = decisions.blocking("s1")[0]
        with self.assertRaises(decisions.ProvenanceError):
            decisions.resolve_manually(
                "s1", decision.id, approve=True, answer_text="ok",
                confirm_id=decision.id, stdin_is_tty=False, stdout_is_tty=True)

    def test_only_the_authority_receives_the_approved_scope(self):
        self.run_review()
        authority = self.sent_packs.get("code-quality", "")
        self.assertIn("AUTORITÉ DE PÉRIMÈTRE", authority)
        self.assertIn("Refais le parseur", authority)
        for reviewer, pack in self.sent_packs.items():
            if reviewer != "code-quality":
                self.assertNotIn("AUTORITÉ DE PÉRIMÈTRE", pack)

    def test_the_pack_carries_the_session_diff_not_the_repo(self):
        self.run_review()
        pack = self.sent_packs["code-quality"]
        self.assertIn("Diff de session", pack)
        self.assertIn("heic", pack)
        self.assertLess(len(pack), 60000)

    def test_round_and_fingerprint_are_recorded(self):
        result = self.run_review()
        st = state.load("s1")
        self.assertEqual(st.round, 1)
        self.assertEqual(st.last_diff_fingerprint, result.diff_fingerprint)
        self.assertEqual(st.last_run_id, result.run_id)

    def test_dry_run_touches_nothing(self):
        result = self.run_review(dry_run=True)
        self.assertTrue(result.route_explain)
        self.assertEqual(state.load("s1").round, 0)
        self.assertFalse(decisions.load_all("s1"))

    def test_empty_diff_is_a_no_op(self):
        state.record_edit("s2", "src/app.py", None)
        baseline.capture("s2", self.repo, self.cfg)
        result = review.run("s2", self.repo, self.cfg)
        self.assertFalse(result.selected)
        self.assertTrue(any("aucune modification" in w for w in result.warnings))

    def test_codex_unavailable_is_reported_without_blocking(self):
        codex.ensure_available = self._raise_unavailable
        result = self.run_review()
        self.assertIsNotNone(result.codex_error)
        self.assertIn("codex login", result.codex_error)
        self.assertFalse(result.all_findings)

    @staticmethod
    def _raise_unavailable(cfg):
        raise codex.CodexUnavailable(
            "codex introuvable.\n  → npm i -g @openai/codex\n  → puis: codex login",
            codex.MISSING)

    def test_a_failed_reviewer_does_not_sink_the_others(self):
        def partial(*args, **kwargs):
            jobs = args[0]
            out = [codex.CodexOutcome(reviewer=jobs[0][0], error="timeout",
                                      error_kind=codex.TIMEOUT)]
            for reviewer, _ in jobs[1:]:
                out.append(canned(reviewer, {
                    "verdict": "approve", "reviewer": reviewer,
                    "summary": "rien", "findings": []}))
            return out
        codex.run_many = partial
        result = self.run_review(select_all=True)
        self.assertTrue(any(not r.ok for r in result.reviewer_results))
        self.assertTrue(any(r.ok for r in result.reviewer_results))

    def test_report_names_the_pending_decision_and_the_way_to_ask(self):
        from crux import report
        result = self.run_review()
        text = report.render_markdown(result, "s1", self.cfg)
        self.assertIn("Décisions humaines en attente", text)
        self.assertIn("AskUserQuestion", text)
        self.assertIn("aucune commande", text.lower())


@unittest.skipUnless(CODEX_PRESENT, "codex CLI absent: test d'intégration ignoré")
class RealCodexIntegration(CruxTestCase):
    """Only runs when a real `codex` binary is installed and logged in."""

    def test_output_schema_capability_probe(self):
        cfg = config.load(self.repo)
        result = capabilities.probe_output_schema(cfg)
        self.assertIn(result["status"], (capabilities.SUPPORTED,
                                         capabilities.UNSUPPORTED,
                                         capabilities.INDETERMINATE))


if __name__ == "__main__":
    unittest.main()
