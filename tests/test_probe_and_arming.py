"""F1 and F3: two ways Crux could lie about its own state.

  F1  A probe has three outcomes. Recording a transient failure as "capability
      absent" outlives its cause by weeks, because the cache is only invalidated
      by a Codex version or model change. A quota outage during `crux doctor
      --probe` would silently downgrade every later review.

  F3  A SessionStart banner is a claim. Emitting "Crux est armé" when no baseline
      was captured describes a protection that does not exist - the one failure
      mode nobody thinks to check.
"""

from __future__ import annotations

import io
import json
import sys

from ._support import CruxTestCase

from crux import baseline, capabilities, config, findings, hooks, paths, state


# --------------------------------------------------------------------- F1 ---
class ProbeClassification(CruxTestCase):
    """`unsupported` needs proof. Everything else is a statement about the run."""

    def status(self, stderr):
        return capabilities.classify_probe(1, stderr, "")[0]

    def test_quota_is_indeterminate(self):
        self.assertEqual(
            self.status("ERROR: You've hit your usage limit. try again at 12:43"),
            capabilities.INDETERMINATE)

    def test_rate_limit_is_indeterminate(self):
        self.assertEqual(self.status("ERROR: 429 too many requests"),
                         capabilities.INDETERMINATE)

    def test_auth_is_indeterminate(self):
        self.assertEqual(self.status("ERROR: not logged in, run codex login"),
                         capabilities.INDETERMINATE)

    def test_network_is_indeterminate(self):
        self.assertEqual(self.status("ERROR: connection reset by peer"),
                         capabilities.INDETERMINATE)

    def test_server_error_is_indeterminate(self):
        self.assertEqual(self.status("ERROR: 503 service unavailable"),
                         capabilities.INDETERMINATE)

    def test_an_unclassified_error_is_indeterminate(self):
        """The default must be 'we do not know', never 'it is not there'."""
        self.assertEqual(self.status("ERROR: something went sideways"),
                         capabilities.INDETERMINATE)

    def test_an_unknown_option_is_real_proof(self):
        self.assertEqual(
            self.status("error: unexpected argument '--output-schema' found"),
            capabilities.UNSUPPORTED)

    def test_an_unrecognized_option_is_real_proof(self):
        self.assertEqual(self.status("error: unrecognized option --output-schema"),
                         capabilities.UNSUPPORTED)

    def test_a_model_refusing_the_parameter_is_real_proof(self):
        self.assertEqual(
            self.status('{"message": "response_format is not supported for '
                        'this model"}'),
            capabilities.UNSUPPORTED)

    def test_rejecting_another_option_is_not_proof_about_this_one(self):
        """R1F1: the probe also passes --cd, --ephemeral, --skip-git-repo-check.

        A rejection of one of those is a real rejection of a real option, and
        says nothing whatsoever about --output-schema.
        """
        for other in ("--ephemeral", "--cd", "--skip-git-repo-check"):
            with self.subTest(option=other):
                self.assertEqual(
                    self.status(f"error: unexpected argument '{other}' found"),
                    capabilities.INDETERMINATE)

    def test_the_api_parameter_name_also_counts_as_proof(self):
        for name in ("response_format", "text.format", "json_schema"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.status(f'{{"message": "{name} is not supported for '
                                f'this model"}}'),
                    capabilities.UNSUPPORTED)

    def test_a_quota_error_naming_an_option_stays_indeterminate(self):
        """Transient wins: proof must not be manufactured from a coincidence."""
        self.assertEqual(
            self.status("ERROR: 429 rate limit while parsing unexpected argument"),
            capabilities.INDETERMINATE)


class ProbeOutcomes(CruxTestCase):
    """The probe itself, with the subprocess boundary stubbed."""

    def setUp(self):
        super().setUp()
        self.cfg = config.load(self.repo)
        self._real_resolve = capabilities.resolve_binary
        capabilities.resolve_binary = lambda name="codex": "codex"
        self.addCleanup(setattr, capabilities, "resolve_binary",
                        self._real_resolve)

    def run_probe(self, returncode=0, stderr="", written=None, raises=None):
        import subprocess as sp

        class Proc:
            pass

        def fake_run(argv, **kwargs):
            if raises is not None:
                raise raises
            out_index = argv.index("-o") + 1
            if written is not None:
                from pathlib import Path
                Path(argv[out_index]).write_text(written, encoding="utf-8")
            proc = Proc()
            proc.returncode = returncode
            proc.stdout = ""
            proc.stderr = stderr
            return proc

        real = sp.run
        sp.run = fake_run
        try:
            return capabilities.probe_output_schema(self.cfg, repo=self.repo)
        finally:
            sp.run = real

    def test_a_conforming_reply_is_supported(self):
        self.assertEqual(self.run_probe(written='{"ok": true}')["status"],
                         capabilities.SUPPORTED)

    def test_a_rejected_option_is_unsupported(self):
        result = self.run_probe(
            returncode=2, stderr="error: unexpected argument '--output-schema'")
        self.assertEqual(result["status"], capabilities.UNSUPPORTED)

    def test_quota_is_not_a_verdict(self):
        result = self.run_probe(returncode=1,
                                stderr="ERROR: You've hit your usage limit.")
        self.assertEqual(result["status"], capabilities.INDETERMINATE)

    def test_a_timeout_is_not_a_verdict(self):
        import subprocess as sp
        result = self.run_probe(raises=sp.TimeoutExpired("codex", 180))
        self.assertEqual(result["status"], capabilities.INDETERMINATE)

    def test_no_output_written_is_not_a_verdict(self):
        self.assertEqual(self.run_probe(written=None)["status"],
                         capabilities.INDETERMINATE)

    def test_unreadable_output_is_not_a_verdict(self):
        self.assertEqual(self.run_probe(written="pas du json")["status"],
                         capabilities.INDETERMINATE)

    def test_non_conforming_json_is_not_a_verdict(self):
        """The flag was accepted and the run completed: not proof it is unknown."""
        self.assertEqual(self.run_probe(written='{"autre": 1}')["status"],
                         capabilities.INDETERMINATE)

    def test_a_missing_binary_is_not_a_verdict(self):
        capabilities.resolve_binary = lambda name="codex": None
        self.assertEqual(
            capabilities.probe_output_schema(self.cfg)["status"],
            capabilities.INDETERMINATE)


class CapabilityCache(CruxTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = config.load(self.repo)
        # Capture the original BEFORE replacing it: addCleanup evaluates its
        # arguments now, so passing `capabilities.resolve_binary` after the
        # assignment restored the stub and leaked it into every later test.
        self._real_resolve = capabilities.resolve_binary
        capabilities.resolve_binary = lambda name="codex": None
        self.addCleanup(setattr, capabilities, "resolve_binary",
                        self._real_resolve)
        self._real_version = capabilities.codex_version
        capabilities.codex_version = lambda binary="codex": "codex-cli 9.9.9"
        self.addCleanup(setattr, capabilities, "codex_version",
                        self._real_version)

    def write_cache(self, payload):
        paths.write_json(paths.capabilities_path(), payload)

    def test_an_indeterminate_result_is_not_carried_over(self):
        self.write_cache({
            "key": capabilities.cache_key(self.cfg),
            "cache_version": capabilities.CACHE_VERSION,
            "output_schema": {"status": capabilities.INDETERMINATE,
                              "reason": "quota"}})
        self.assertIsNone(capabilities.output_schema_supported(self.cfg))

    def test_a_real_verdict_is_carried_over(self):
        for status, expected in ((capabilities.SUPPORTED, True),
                                 (capabilities.UNSUPPORTED, False)):
            with self.subTest(status=status):
                self.write_cache({
                    "key": capabilities.cache_key(self.cfg),
                    "cache_version": capabilities.CACHE_VERSION,
                    "output_schema": {"status": status, "reason": ""}})
                self.assertIs(capabilities.output_schema_supported(self.cfg),
                              expected)

    def test_a_v1_cache_entry_is_discarded_not_interpreted(self):
        """The poisoning case: `supported: false` could mean either thing."""
        self.write_cache({
            "key": capabilities.cache_key(self.cfg),
            "output_schema": {"supported": False, "reason": "quota peut-être"}})
        self.assertIsNone(capabilities.output_schema_supported(self.cfg))

    def test_a_v1_positive_entry_is_also_discarded(self):
        self.write_cache({
            "key": capabilities.cache_key(self.cfg),
            "output_schema": {"supported": True, "reason": ""}})
        self.assertIsNone(capabilities.output_schema_supported(self.cfg))

    def test_a_fresh_cache_records_its_version(self):
        capabilities.get(self.cfg)
        cached = paths.read_json(paths.capabilities_path())
        self.assertEqual(cached["cache_version"], capabilities.CACHE_VERSION)

    def test_indeterminate_lets_the_review_still_try_the_flag(self):
        """Fail-open in the right direction: try it, and let the real call rule."""
        self.write_cache({
            "key": capabilities.cache_key(self.cfg),
            "cache_version": capabilities.CACHE_VERSION,
            "output_schema": {"status": capabilities.INDETERMINATE,
                              "reason": "quota"}})
        self.assertIsNot(capabilities.output_schema_supported(self.cfg), False)


# --------------------------------------------------------------------- F3 ---
class SessionStartBanner(CruxTestCase):
    def start(self, payload_bytes):
        import os
        os.environ["CRUX_GATE"] = "code"
        self.addCleanup(os.environ.pop, "CRUX_GATE", None)
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin = io.TextIOWrapper(io.BytesIO(payload_bytes), encoding="utf-8")
        sys.stdout = captured = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        try:
            code = hooks.session_start()
            captured.flush()
            captured.buffer.seek(0)
            return code, captured.buffer.read().decode("utf-8")
        finally:
            sys.stdin, sys.stdout = stdin, stdout

    def payload(self, **extra):
        return json.dumps({"cwd": str(self.repo), **extra}).encode("utf-8")

    def test_a_nominal_start_still_announces_and_captures(self):
        code, out = self.start(self.payload(session_id="s1"))
        self.assertEqual(code, 0)
        self.assertIn("Crux est armé", out)
        self.assertTrue(state.load("s1").baseline_captured)

    def test_an_unreadable_payload_never_claims_to_be_armed(self):
        code, out = self.start(b"{ ceci n'est pas du json")
        self.assertEqual(code, 0)
        self.assertNotIn("Crux est armé", out)

    def test_a_payload_without_a_session_id_never_claims_to_be_armed(self):
        code, out = self.start(self.payload())
        self.assertEqual(code, 0)
        self.assertNotIn("Crux est armé", out)

    def test_a_failed_baseline_capture_never_claims_to_be_armed(self):
        real = baseline.capture

        def boom(*args, **kwargs):
            raise OSError("disque plein")

        baseline.capture = boom
        self.addCleanup(setattr, baseline, "capture", real)
        code, out = self.start(self.payload(session_id="s1"))
        self.assertEqual(code, 0)
        self.assertNotIn("Crux est armé", out)
        self.assertFalse(state.load("s1").baseline_captured)

    def test_a_failure_is_logged(self):
        code, _ = self.start(b"pas du json")
        self.assertEqual(code, 0)
        log = paths.log_path()
        self.assertTrue(log.is_file())
        self.assertIn("bannière supprimée",
                      log.read_text(encoding="utf-8", errors="replace"))

    def test_it_never_blocks_whatever_happens(self):
        """Fail-open: a broken payload must not wedge a session."""
        for payload in (b"", b"null", b"[]", b"{ oops"):
            with self.subTest(payload=payload):
                self.assertEqual(self.start(payload)[0], 0)

    def test_an_unarmed_session_is_still_silent(self):
        """The v0.1 guarantee: plain `claude` writes nothing and says nothing."""
        import os
        os.environ.pop("CRUX_GATE", None)
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin = io.TextIOWrapper(io.BytesIO(self.payload(session_id="s2")),
                                     encoding="utf-8")
        sys.stdout = captured = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        try:
            hooks.session_start()
            captured.flush()
            captured.buffer.seek(0)
            self.assertEqual(captured.buffer.read().decode("utf-8").strip(), "")
        finally:
            sys.stdin, sys.stdout = stdin, stdout


# ------------------------------------------------- F2, the general half ------
class RoundAnchorability(CruxTestCase):
    def test_a_v02_journal_is_anchorable(self):
        findings.save_round("s1", 1, [], {}, selected=["code-quality"])
        self.assertTrue(findings.round_is_anchorable(
            findings.load_rounds("s1")[0]))

    def test_an_empty_v02_round_is_still_anchorable(self):
        """A genuine `approve` holds zero findings; emptiness proves nothing."""
        findings.save_round("s1", 1, [], {}, selected=["code-quality"])
        record = findings.load_rounds("s1")[0]
        self.assertEqual(record["findings"], [])
        self.assertTrue(findings.round_is_anchorable(record))

    def test_a_v01_journal_is_not_anchorable(self):
        findings.findings_path("s1", 1).write_text(
            json.dumps({"round": 1, "findings": [], "promoted": {}}),
            encoding="utf-8")
        self.assertFalse(findings.round_is_anchorable(
            findings.load_rounds("s1")[0]))

    def test_a_v01_journal_still_loads_and_resolves(self):
        """Not anchorable is not unusable: arbitration must keep working."""
        findings.findings_path("s1", 1).write_text(json.dumps(
            {"round": 1, "promoted": {},
             "findings": [{"id": "F1", "reviewer": "cq", "severity": "high",
                           "title": "T", "description": "d", "file": "a.py"}]}),
            encoding="utf-8")
        self.assertEqual(findings.resolve("s1", "F1", "accepted")["id"], "F1")
