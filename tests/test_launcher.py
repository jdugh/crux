"""How Crux decides to start itself, and why it never assumes.

A console script that exists is not a console script that runs. Under Windows
App Control the generated `crux.exe` was refused at spawn (`WinError 4551`)
while looking perfectly installed, so every hook died with `uv_spawn` and the
gate merely appeared inert — the failure this project keeps having to design
against.

The Device Guard refusal is simulated with a launcher that fails; no test ever
touches the machine's actual security policy.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from ._support import CruxTestCase, SRC

from crux import launcher, setupcmd

WINDOWS = os.name == "nt"


def fake_launcher(tmp: Path, name: str, *, works: bool,
                  message: str = "blocked by policy") -> Path:
    """A stand-in executable: a tiny Python script wrapped in a runnable file."""
    body = (f'import sys; print("crux 0.1.0")' if works
            else f'import sys; sys.stderr.write({message!r}); sys.exit(1)')
    script = tmp / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    if WINDOWS:
        path = tmp / f"{name}.cmd"
        path.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
                        encoding="utf-8", newline="")
    else:
        path = tmp / name
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                        encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class ProbeDecidesNotExistence(CruxTestCase):
    def test_a_working_launcher_probes_ok(self):
        path = fake_launcher(self.tmp, "good", works=True)
        result = launcher.probe(launcher.Launcher([str(path)], "test", "fixture"))
        self.assertTrue(result.ok, result.error)

    def test_a_present_but_unrunnable_launcher_probes_false(self):
        """The Device Guard shape: the file exists, the spawn is refused."""
        path = fake_launcher(self.tmp, "blocked", works=False,
                             message="Une strategie de controle d application "
                                     "a bloque ce fichier")
        result = launcher.probe(launcher.Launcher([str(path)], "test", "fixture"))
        self.assertFalse(result.ok)
        self.assertIn("bloque", (result.error or "").lower())

    def test_a_missing_launcher_probes_false(self):
        result = launcher.probe(
            launcher.Launcher([str(self.tmp / "nope")], "test", "fixture"))
        self.assertFalse(result.ok)
        self.assertIn("introuvable", result.error or "")

    def test_a_launcher_printing_something_else_is_rejected(self):
        """A stub that exits 0 without being Crux is not a launcher."""
        script = self.tmp / "impostor.py"
        script.write_text("print('Microsoft Store stub')", encoding="utf-8")
        result = launcher.probe(launcher.Launcher(
            [sys.executable, str(script)], "test", "fixture"))
        self.assertFalse(result.ok)
        self.assertIn("inattendue", result.error or "")

    def test_the_probe_has_no_side_effects(self):
        """`--version` must not create session state or ledgers."""
        launcher.probe(launcher.Launcher([sys.executable, "-m", "crux"],
                                         "test", "fixture"))
        from crux import paths
        sessions = paths.sessions_root()
        self.assertEqual(list(sessions.iterdir()), [])


class SelectionOrder(CruxTestCase):
    def test_python_module_is_a_candidate_and_uses_sys_executable(self):
        argvs = [c.argv for c in launcher.candidates()]
        self.assertIn([sys.executable, "-m", "crux"], argvs)

    @unittest.skipUnless(WINDOWS, "politique spécifique à Windows")
    def test_windows_prefers_the_python_module_over_the_console_script(self):
        """Even when the console script probes fine.

        The same crux.exe path was refused and then allowed within an hour: the
        verdict is per-file and reputation-based, so passing a probe today says
        nothing about the next `pip install`.
        """
        kinds = [c.kind for c in launcher.candidates()]
        self.assertEqual(kinds[0], launcher.PYTHON_MODULE)
        if launcher.CONSOLE_SCRIPT in kinds:
            self.assertLess(kinds.index(launcher.PYTHON_MODULE),
                            kinds.index(launcher.CONSOLE_SCRIPT))

    @unittest.skipIf(WINDOWS, "politique spécifique aux systèmes POSIX")
    def test_posix_prefers_the_console_script(self):
        kinds = [c.kind for c in launcher.candidates()]
        if launcher.CONSOLE_SCRIPT in kinds:
            self.assertEqual(kinds[0], launcher.CONSOLE_SCRIPT)

    def test_candidates_are_deduplicated(self):
        argvs = [tuple(c.argv) for c in launcher.candidates()]
        self.assertEqual(len(argvs), len(set(argvs)))

    def test_detect_skips_a_broken_first_candidate(self):
        broken = launcher.Launcher([str(self.tmp / "gone")], "test", "cassé")
        working = launcher.Launcher([sys.executable, "-m", "crux"],
                                    launcher.PYTHON_MODULE, "ok")
        original = launcher.candidates
        launcher.candidates = lambda: [broken, working]
        try:
            chosen = launcher.detect()
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen.argv, working.argv)
        finally:
            launcher.candidates = original

    def test_detect_returns_none_when_nothing_runs(self):
        original = launcher.candidates
        launcher.candidates = lambda: [
            launcher.Launcher([str(self.tmp / "a")], "test", ""),
            launcher.Launcher([str(self.tmp / "b")], "test", "")]
        try:
            self.assertIsNone(launcher.detect())
        finally:
            launcher.candidates = original


class StrategyIsRecordedAndRechecked(CruxTestCase):
    def test_the_choice_is_stored_and_reloaded(self):
        chosen = launcher.resolve(refresh=True)
        self.assertIsNotNone(chosen)
        self.assertTrue(launcher.store_path().is_file())
        self.assertEqual(launcher.load().argv, chosen.argv)

    def test_a_stored_launcher_that_stopped_working_is_replaced(self):
        """The upgrade case: the recorded strategy must be re-probed, not trusted."""
        launcher.save(launcher.Launcher([str(self.tmp / "vanished")],
                                        "test", "obsolète"))
        chosen = launcher.resolve()
        self.assertIsNotNone(chosen)
        self.assertNotEqual(chosen.argv, [str(self.tmp / "vanished")])
        self.assertEqual(launcher.load().argv, chosen.argv)


class GeneratedArtefactsUseOneLauncher(CruxTestCase):
    def setUp(self):
        super().setUp()
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")
        os.environ["HOME"] = str(self.tmp / "fakehome")
        os.environ["USERPROFILE"] = str(self.tmp / "fakehome")

    def installed_hooks(self):
        target, _ = setupcmd.install_plugin()
        return json.loads(
            (target / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]

    def test_every_hook_uses_the_chosen_launcher(self):
        expected = setupcmd.crux_executable()
        for event, groups in self.installed_hooks().items():
            for group in groups:
                for hook in group["hooks"]:
                    prefix = [hook["command"], *hook["args"][:-2]]
                    self.assertEqual(prefix, expected, event)

    def test_session_start_and_session_end_share_the_launcher(self):
        hooks = self.installed_hooks()
        def prefix(event):
            hook = hooks[event][0]["hooks"][0]
            return [hook["command"], *hook["args"][:-2]]
        self.assertEqual(prefix("SessionStart"), prefix("SessionEnd"))

    def test_hook_args_still_end_with_the_event(self):
        for event, groups in self.installed_hooks().items():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertEqual(hook["args"][-2], "hook", event)

    def test_the_shim_invokes_the_same_launcher(self):
        setupcmd.write_crux_shim()
        expected = setupcmd.crux_executable()
        name = "crux.cmd" if WINDOWS else "crux"
        body = (setupcmd.shim_dir() / name).read_text(encoding="utf-8")
        for part in expected:
            self.assertIn(part.replace("\\", "\\"), body)

    @unittest.skipUnless(WINDOWS, "shims Windows")
    def test_windows_writes_both_a_cmd_and_an_sh_shim(self):
        setupcmd.write_crux_shim()
        self.assertTrue((setupcmd.shim_dir() / "crux.cmd").is_file())
        self.assertTrue((setupcmd.shim_dir() / "crux").is_file())

    def test_paths_with_spaces_are_quoted(self):
        spaced = self.tmp / "Program Files" / "py"
        spaced.parent.mkdir(parents=True, exist_ok=True)
        original = setupcmd.crux_executable
        setupcmd.crux_executable = lambda: [str(spaced / "python.exe"),
                                            "-m", "crux"]
        try:
            setupcmd.write_crux_shim()
            name = "crux.cmd" if WINDOWS else "crux"
            body = (setupcmd.shim_dir() / name).read_text(encoding="utf-8")
            self.assertIn('"', body, "a path with spaces must be quoted")
            self.assertIn("Program Files", body)
        finally:
            setupcmd.crux_executable = original

    def test_hook_entry_shape(self):
        chosen = launcher.Launcher([str(self.tmp / "py.exe"), "-m", "crux"],
                                   launcher.PYTHON_MODULE, "fixture")
        entry = launcher.hook_entry(chosen, "stop")
        self.assertEqual(entry["command"], str(self.tmp / "py.exe"))
        self.assertEqual(entry["args"], ["-m", "crux", "hook", "stop"])

    def test_the_packaged_template_is_still_untouched(self):
        self.installed_hooks()
        template = json.loads(
            (SRC / "crux" / "plugin" / "hooks" / "hooks.json")
            .read_text(encoding="utf-8"))
        commands = {h["command"] for gs in template["hooks"].values()
                    for g in gs for h in g["hooks"]}
        self.assertEqual(commands, {"crux"})


class HooksActuallyRunThroughTheChosenLauncher(CruxTestCase):
    """The end of the loop: the generated command must really start Crux."""

    def setUp(self):
        super().setUp()
        self.write("a.py", "A = 1\n")
        self.commit("init")

    def test_the_generated_hook_command_executes(self):
        argv = setupcmd.crux_executable()
        payload = json.dumps({"hook_event_name": "SessionStart",
                              "session_id": "launch-1",
                              "cwd": str(self.repo)})
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(SRC), "CRUX_HOME": str(self.crux_home),
                    "CRUX_GATE": "code"})
        proc = subprocess.run([*argv, "hook", "session-start"],
                              input=payload, capture_output=True, text=True,
                              encoding="utf-8", cwd=str(self.repo), env=env,
                              shell=False, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        from crux import state
        self.assertEqual(state.load("launch-1").repo, str(self.repo.resolve()))


if __name__ == "__main__":
    unittest.main()
