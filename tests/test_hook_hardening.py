"""The hook path may be slow. It may not be unbounded.

Two waits on the SessionStart path had no bound at all: the read of the hook
payload from stdin, and every git subprocess. Either one turns a handler that
decided nothing into a hook Claude Code has to kill at its own timeout - the
28.7 s run that started this. These tests hold both bounds, and hold the two
reductions that came with them: one git process instead of four to resolve the
repository, one read and one write of the session state instead of three and two.

Everything here is offline. Nothing calls Codex.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from ._support import CruxTestCase, SRC

from crux import baseline, gitctx, hooks, paths, state


def run_handler(event: str, payload: dict) -> int:
    """Drive a handler in-process with a payload, without touching real stdin."""

    class FakeStdin:
        def __init__(self, data: bytes):
            self.buffer = io.BytesIO(data)

    class Sink:
        def __init__(self):
            self.buffer = io.BytesIO()

        def write(self, _text):
            pass

        def flush(self):
            pass

    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = FakeStdin(json.dumps(payload).encode("utf-8"))
    sys.stdout = Sink()
    try:
        return hooks.dispatch(event)
    finally:
        sys.stdin, sys.stdout = stdin, stdout


# --------------------------------------------------------------- stdin ------
class BoundedPayloadRead(CruxTestCase):
    """A payload that never arrives must not hold the session open.

    Run as real subprocesses on purpose. The failure this guards against is not
    a return value, it is what the *process* does: the reader thread is parked
    in an uninterruptible read, and a normal interpreter shutdown then dies in
    ``_enter_buffered_busy`` with an access violation. Only a real spawn can
    tell a clean exit 0 from a crash that also happens to end the process.
    """

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")

    def spawn(self, timeout_seconds="1.0"):
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home),
                    "CRUX_GATE": "code", "PYTHONIOENCODING": "utf-8",
                    hooks.PAYLOAD_TIMEOUT_ENV: timeout_seconds})
        return subprocess.Popen(
            [sys.executable, "-m", "crux", "hook", "session-start"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(self.repo), env=env)

    def payload(self) -> bytes:
        return json.dumps({"session_id": "s-stdin", "cwd": str(self.repo),
                           "hook_event_name": "SessionStart"}).encode("utf-8")

    def drive(self, write=None, close=True, wait=25.0):
        """Run the hook and wait for it *without* closing stdin ourselves.

        Deliberately not ``communicate()``: it closes stdin before waiting, so
        every "stdin is still open" case silently became a plain EOF and the
        tests passed for the wrong reason. Reading the pipes after the process
        has exited cannot deadlock here - a timed-out hook writes nothing.
        """
        proc = self.spawn()
        started = time.perf_counter()
        if write is not None:
            proc.stdin.write(write)
            proc.stdin.flush()
        if close:
            proc.stdin.close()
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail("le hook ne s'est pas termine en %.0f s" % wait)
        elapsed = time.perf_counter() - started
        out, err = proc.stdout.read(), proc.stderr.read()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        return proc.returncode, out, err, elapsed

    def test_normal_payload_still_works(self):
        rc, out, _err, _dt = self.drive(write=self.payload())
        self.assertEqual(rc, 0)
        self.assertIn("Crux est arm", out.decode("utf-8"))
        self.assertIsNotNone(baseline.load_manifest("s-stdin"),
                             "la baseline doit etre capturee sur le nominal")

    def test_invalid_payload_is_inert_not_fatal(self):
        rc, out, _err, _dt = self.drive(write=b"{ ceci n est pas du json")
        self.assertEqual(rc, 0)
        self.assertEqual(out, b"", "aucune banniere sans session_id exploitable")

    def test_stdin_open_and_empty_terminates_at_the_bound(self):
        rc, out, _err, elapsed = self.drive(write=None, close=False)
        self.assertEqual(rc, 0, "sortie non nulle (%r) : fail-open rompu" % rc)
        self.assertLess(elapsed, 8.0,
                        "le processus doit reellement se terminer au timeout")
        self.assertEqual(out, b"", "aucune banniere apres un timeout stdin")

    def test_partial_payload_without_eof_terminates_at_the_bound(self):
        rc, out, _err, elapsed = self.drive(write=b'{"session_id":"s-stdin"',
                                            close=False)
        self.assertEqual(rc, 0, "sortie non nulle (%r) : fail-open rompu" % rc)
        self.assertLess(elapsed, 8.0)
        self.assertEqual(out, b"")

    def test_timeout_leaves_no_baseline_and_an_explicit_log(self):
        rc, _out, _err, _dt = self.drive(write=None, close=False)
        self.assertEqual(rc, 0)
        self.assertIsNone(baseline.load_manifest("s-stdin"),
                          "aucune baseline ne doit etre presentee comme valide")
        log = paths.log_path().read_text(encoding="utf-8")
        self.assertIn("charge utile du hook non re", log)

    def test_no_crash_output_on_timeout(self):
        """A crashed shutdown printed a Fatal Python error. It must not return."""
        _rc, _out, err, _dt = self.drive(write=None, close=False)
        text = err.decode("utf-8", "replace")
        self.assertNotIn("Fatal Python error", text)
        self.assertNotIn("Traceback", text)


# ----------------------------------------------------------------- git ------
class GitCallsAreBounded(CruxTestCase):

    @staticmethod
    def _stalled(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=0.1)

    def test_timeout_becomes_a_typed_technical_error(self):
        real = gitctx.subprocess.run
        gitctx.subprocess.run = self._stalled
        try:
            with self.assertRaises(gitctx.GitTimeout):
                gitctx.run(self.repo, "status", "--porcelain")
            with self.assertRaises(gitctx.GitTimeout):
                gitctx.describe(self.repo)
            # A stalled git is never the statement "this is not a repository".
            with self.assertRaises(gitctx.GitTimeout):
                gitctx.is_repo(self.repo)
        finally:
            gitctx.subprocess.run = real

    def test_git_timeout_is_a_git_error(self):
        self.assertTrue(issubclass(gitctx.GitTimeout, gitctx.GitError))

    def test_bound_is_applied_to_every_git_process(self):
        seen = []
        real = gitctx.subprocess.run

        def spy(args, **kwargs):
            seen.append(kwargs.get("timeout"))
            return real(args, **kwargs)

        self.write("a.py", "A = 1\n")
        self.commit("init")
        gitctx.subprocess.run = spy
        try:
            gitctx.describe(self.repo)
            gitctx.status_entries(self.repo)
            gitctx.show_blob(self.repo, "HEAD", "a.py")
            gitctx.generated_paths(self.repo)
            gitctx.git_available()
        finally:
            gitctx.subprocess.run = real
        self.assertTrue(seen)
        self.assertTrue(all(t is not None and t > 0 for t in seen),
                        "un appel git sans borne: %r" % (seen,))

    def test_env_override(self):
        os.environ[gitctx.TIMEOUT_ENV] = "3.5"
        try:
            self.assertEqual(gitctx.default_timeout(), 3.5)
        finally:
            os.environ.pop(gitctx.TIMEOUT_ENV, None)
        self.assertEqual(gitctx.default_timeout(),
                         gitctx.DEFAULT_TIMEOUT_SECONDS)

    def test_session_start_stays_inert_and_fast_when_git_stalls(self):
        """A stalled git must fail open, not wedge, and never claim a baseline."""
        os.environ["CRUX_GATE"] = "code"
        real = gitctx.subprocess.run
        gitctx.subprocess.run = self._stalled
        try:
            started = time.perf_counter()
            rc = run_handler("session-start",
                             {"session_id": "s-git", "cwd": str(self.repo)})
        finally:
            gitctx.subprocess.run = real
        self.assertEqual(rc, 0)
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertIsNone(baseline.load_manifest("s-git"))
        log = paths.log_path().read_text(encoding="utf-8")
        self.assertIn("solution du d", log)   # "resolution du depot impossible"


class RealGitThatNeverReturns(CruxTestCase):
    """End-to-end, with the real git binary genuinely stuck.

    No fake `git` on PATH: on Windows ``CreateProcess`` appends ``.exe`` and
    nothing else, so a ``git.cmd`` shim is simply never found and the test would
    quietly exercise the real git - which is how this test first passed while
    proving nothing. ``core.fsmonitor`` stalls the real binary instead: git runs
    that command and waits for it, so ``git status`` hangs for real while
    ``rev-parse`` still answers. That is the exact shape of the failure the
    bound exists for - a repository that resolves, and a status that never
    returns.
    """

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")
        sleeper = self.tmp / "sleeper.py"
        # Bounded: git's helper is orphaned when git is killed, so it must end
        # on its own. Long enough to outlive the bound under test, short enough
        # not to leave a process loitering after the suite.
        sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        subprocess.run(["git", "config", "core.fsmonitor",
                        '"%s" "%s"' % (sys.executable, sleeper)],
                       cwd=str(self.repo), capture_output=True, check=True)

    def test_a_hung_status_does_not_wedge_session_start(self):
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home),
                    "CRUX_GATE": "code", "PYTHONIOENCODING": "utf-8",
                    gitctx.TIMEOUT_ENV: "2.0"})
        payload = json.dumps({"session_id": "s-hung", "cwd": str(self.repo)})
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, "-m", "crux", "hook", "session-start"],
            input=payload.encode("utf-8"), capture_output=True,
            cwd=str(self.repo), env=env, timeout=60)
        elapsed = time.perf_counter() - started

        self.assertEqual(
            proc.returncode, 0,
            "fail-open rompu: rc=%r %s" % (
                proc.returncode, proc.stderr.decode("utf-8", "replace")[:400]))
        self.assertLess(elapsed, 25.0, "un git bloque a immobilise le hook")
        self.assertEqual(proc.stdout, b"",
                         "aucune banniere quand la baseline n a pas ete capturee")
        self.assertIsNone(baseline.load_manifest("s-hung"),
                          "aucune baseline partielle presentee comme valide")
        # The session is still registered: a stalled git is a technical failure,
        # it must not cost the session->repo mapping.
        self.assertEqual(state.load("s-hung").repo, str(self.repo.resolve()))
        self.assertFalse(state.load("s-hung").baseline_captured)


class TheBoundKillsARealHangingProcess(CruxTestCase):
    """The bound is not just an argument that gets passed somewhere."""

    def test_run_raw_raises_git_timeout_on_a_process_that_never_exits(self):
        started = time.perf_counter()
        with self.assertRaises(gitctx.GitTimeout):
            gitctx._run_raw([sys.executable, "-c", "import time; time.sleep(120)"],
                            timeout=1.5, capture_output=True)
        self.assertLess(time.perf_counter() - started, 10.0)


# ------------------------------------------------- subprocess / IO counts ---
class SessionStartDoesLessWork(CruxTestCase):
    """The reductions are the point; a test is the only thing that keeps them."""

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")
        self.write("dirty.py", "B = 2\n")
        os.environ["CRUX_GATE"] = "code"

    def counted_run(self, session_id="s-count"):
        git_calls, loads, saves = [], [], []
        real_run = gitctx.subprocess.run
        real_load, real_save = state.load, state.save

        def spy_run(args, **kwargs):
            git_calls.append(" ".join(str(a) for a in args[:3]))
            return real_run(args, **kwargs)

        def spy_load(sid):
            loads.append(sid)
            return real_load(sid)

        def spy_save(st):
            saves.append(st.session_id)
            return real_save(st)

        gitctx.subprocess.run = spy_run
        state.load, state.save = spy_load, spy_save
        hooks.state.load, hooks.state.save = spy_load, spy_save
        try:
            rc = run_handler("session-start",
                             {"session_id": session_id, "cwd": str(self.repo)})
        finally:
            gitctx.subprocess.run = real_run
            state.load, state.save = real_load, real_save
            hooks.state.load, hooks.state.save = real_load, real_save
        return rc, git_calls, loads, saves

    def test_two_git_processes_and_one_read_one_write(self):
        rc, git_calls, loads, saves = self.counted_run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(git_calls), 2,
                         "attendu 1 rev-parse + 1 status, obtenu %r" % (git_calls,))
        self.assertEqual(len(loads), 1, "state.load appele %dx" % len(loads))
        self.assertEqual(len(saves), 1, "state.save appele %dx" % len(saves))

    def test_the_baseline_is_complete_before_the_hook_returns(self):
        """The invariant the whole milestone had to leave untouched."""
        rc, _c, _l, _s = self.counted_run()
        self.assertEqual(rc, 0)
        manifest = baseline.load_manifest("s-count")
        self.assertIsNotNone(manifest, "baseline absente au retour du hook")
        self.assertIn("dirty.py", manifest.entries)
        blob = paths.baseline_dir("s-count") / manifest.entries["dirty.py"].blob
        self.assertTrue(blob.is_file(), "snapshot non ecrit avant le retour")
        self.assertEqual(blob.read_bytes(), b"B = 2\n")
        st = state.load("s-count")
        self.assertTrue(st.baseline_captured)
        self.assertEqual(st.head, manifest.head)
        self.assertEqual(st.head, gitctx.head_sha(self.repo))
        self.assertEqual(st.branch, gitctx.current_branch(self.repo))

    def test_registration_survives_a_failed_capture(self):
        """One write must not mean losing the registration when capture fails."""
        real_capture = baseline.capture

        def boom(*_a, **_k):
            raise RuntimeError("capture cassee")

        hooks.baseline.capture = boom
        try:
            rc = run_handler("session-start",
                             {"session_id": "s-fail", "cwd": str(self.repo)})
        finally:
            hooks.baseline.capture = real_capture
        self.assertEqual(rc, 0)
        st = state.load("s-fail")
        self.assertEqual(st.repo, str(self.repo.resolve()),
                         "l enregistrement session->depot doit survivre")
        self.assertFalse(st.baseline_captured)

    def test_unarmed_session_is_still_registered(self):
        os.environ["CRUX_GATE"] = "off"
        rc = run_handler("session-start",
                         {"session_id": "s-off", "cwd": str(self.repo)})
        self.assertEqual(rc, 0)
        self.assertEqual(state.load("s-off").repo, str(self.repo.resolve()))
        self.assertIsNone(baseline.load_manifest("s-off"))

    def test_registered_repo_matches_what_session_resolution_looks_for(self):
        """`st.repo` must be the resolved form `active_sessions` compares to."""
        run_handler("session-start",
                    {"session_id": "s-resolve", "cwd": str(self.repo)})
        found = [s.session_id for s in state.active_sessions(self.repo)]
        self.assertIn("s-resolve", found)

    def test_capture_is_still_idempotent(self):
        self.counted_run(session_id="s-twice")
        first = baseline.load_manifest("s-twice")
        self.write("dirty.py", "B = 999\n")
        self.counted_run(session_id="s-twice")
        second = baseline.load_manifest("s-twice")
        self.assertEqual(first.captured_at, second.captured_at)
        blob = paths.baseline_dir("s-twice") / second.entries["dirty.py"].blob
        self.assertEqual(blob.read_bytes(), b"B = 2\n",
                         "la baseline a bouge: le point de reference doit etre fige")


class DescribeAgreesWithTheSinglePurposeHelpers(CruxTestCase):
    """One call, four answers - and the same four answers."""

    def test_repository_with_commits(self):
        self.write("a.py", "A = 1\n")
        self.commit("init")
        info = gitctx.describe(self.repo)
        self.assertIsNotNone(info)
        self.assertEqual(info.root, gitctx.repo_root(self.repo))
        self.assertEqual(info.head, gitctx.head_sha(self.repo))
        self.assertEqual(info.branch, gitctx.current_branch(self.repo))

    def test_repository_without_any_commit(self):
        """The case where positional parsing of rev-parse output would lie."""
        info = gitctx.describe(self.repo)
        self.assertIsNotNone(info)
        self.assertEqual(info.root, gitctx.repo_root(self.repo))
        self.assertIsNone(info.head)
        self.assertEqual(info.branch, gitctx.current_branch(self.repo))

    def test_detached_head(self):
        self.write("a.py", "A = 1\n")
        self.commit("init")
        head = gitctx.head_sha(self.repo)
        subprocess.run(["git", "checkout", "-q", "--detach", head],
                       cwd=str(self.repo), capture_output=True)
        info = gitctx.describe(self.repo)
        self.assertEqual(info.head, head)
        self.assertEqual(info.branch, gitctx.current_branch(self.repo))

    def test_outside_any_repository(self):
        outside = self.tmp / "not-a-repo"
        outside.mkdir()
        self.assertIsNone(gitctx.describe(outside))
        self.assertFalse(gitctx.is_repo(outside))


if __name__ == "__main__":
    unittest.main()
