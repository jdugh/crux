"""How to start Crux on this machine — decided by probing, never by assuming.

A console script that *exists* is not a console script that *runs*. On a Windows
box under Device Guard / App Control, `pip install --user` produces a perfectly
valid ``crux.exe`` that the policy refuses to execute:

    Une stratégie de contrôle d'application a bloqué ce fichier

Claude Code spawns hooks directly, so every hook died with
``EUNKNOWN: unknown error, uv_spawn`` and the gate silently did nothing — the
failure mode this project keeps having to design against, because a hook that
cannot start looks exactly like a hook that decided not to act.

So ``crux setup`` builds a list of candidate launchers, **runs each one**, and
keeps the first that answers. The choice is recorded in ``~/.crux/launcher.json``
and reused for the plugin hooks and for every shim, so hooks, PowerShell, cmd and
Git Bash never diverge.

No path in this module is specific to a machine: candidates are derived from
``sys.executable``, ``sysconfig`` and ``PATH``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import paths

PROBE_TIMEOUT = 60

CONSOLE_SCRIPT = "console-script"
PYTHON_MODULE = "python-module"
PY_LAUNCHER = "py-launcher"


@dataclass
class Launcher:
    argv: List[str]
    kind: str
    source: str
    ok: Optional[bool] = None
    error: Optional[str] = None

    @property
    def command(self) -> str:
        return self.argv[0]

    @property
    def prefix_args(self) -> List[str]:
        return list(self.argv[1:])

    def display(self) -> str:
        return " ".join(f'"{a}"' if " " in a else a for a in self.argv)

    def to_dict(self) -> Dict[str, object]:
        return {"argv": self.argv, "kind": self.kind, "source": self.source,
                "ok": self.ok, "error": self.error}

    @classmethod
    def from_dict(cls, raw: Dict[str, object]) -> Optional["Launcher"]:
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv:
            return None
        return cls(argv=[str(a) for a in argv], kind=str(raw.get("kind", "?")),
                   source=str(raw.get("source", "")), ok=raw.get("ok"),
                   error=raw.get("error"))


def console_script_path() -> Optional[Path]:
    """Where this interpreter's ``crux`` console script lives, if anywhere."""
    names = ("crux.exe", "crux") if os.name == "nt" else ("crux",)
    schemes = ["nt_user", "nt", "posix_user", "posix_prefix"]
    seen = []
    try:
        available = set(sysconfig.get_scheme_names())
    except Exception:
        available = set()
    for scheme in schemes:
        if scheme not in available:
            continue
        try:
            seen.append(Path(sysconfig.get_path("scripts", scheme)))
        except Exception:
            continue
    seen.append(Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin"))
    for directory in seen:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def candidates() -> List[Launcher]:
    """Every plausible way to start Crux, best first. The probe still decides.

    The preference is platform-dependent, and deliberately so.

    On **Windows the Python module comes first**, even when the console script
    passes its probe. Measured on a machine under App Control: the very same
    ``crux.exe`` path was refused with ``WinError 4551`` and, once ``pip``
    regenerated the file, ran fine minutes later. The verdict is per-file and
    reputation-based, so a probe that passes at setup time is no promise about
    the next upgrade — and when it does flip, every hook dies at spawn with
    ``uv_spawn`` and the gate merely looks inert. ``python.exe -m crux`` reuses
    an interpreter the policy already trusts and has no separate binary to judge.
    The cost is one extra process start per hook, tens of milliseconds.

    Elsewhere the console script comes first: no such policy layer exists, it is
    the conventional entry point, and it saves that process start.
    """
    found: List[Launcher] = []
    script = console_script_path()

    module = (Launcher([sys.executable, "-m", "crux"], PYTHON_MODULE,
                       "sys.executable — interpréteur déjà autorisé")
              if sys.executable else None)
    console = (Launcher([str(script)], CONSOLE_SCRIPT,
                        "console script généré par pip")
               if script is not None else None)

    if os.name == "nt":
        found.extend(x for x in (module, console) if x is not None)
    else:
        found.extend(x for x in (console, module) if x is not None)

    if os.name == "nt":
        py = shutil.which("py")
        if py:
            version = f"-{sys.version_info.major}.{sys.version_info.minor}"
            found.append(Launcher([py, version, "-m", "crux"], PY_LAUNCHER,
                                  "lanceur py.exe, version épinglée"))

    # PATH interpreters last: on Windows the first `python.exe` on PATH is often
    # the WindowsApps stub, which is exactly why nothing here is trusted before
    # it has answered a probe.
    for name in ("python", "python3"):
        located = shutil.which(name)
        if not located:
            continue
        if any(Path(located) == Path(c.argv[0]) for c in found):
            continue
        found.append(Launcher([located, "-m", "crux"], PYTHON_MODULE,
                              f"{name} trouvé dans le PATH"))

    unique: List[Launcher] = []
    seen = set()
    for launcher in found:
        key = tuple(launcher.argv)
        if key not in seen:
            seen.add(key)
            unique.append(launcher)
    return unique


def probe(launcher: Launcher, timeout: int = PROBE_TIMEOUT) -> Launcher:
    """Actually run it. ``--version`` has no side effects and needs no repo.

    Success requires a zero exit *and* output that came from Crux: a stub that
    exits cleanly while printing something else is not a working launcher.
    """
    try:
        proc = subprocess.run([*launcher.argv, "--version"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", shell=False, timeout=timeout)
    except FileNotFoundError:
        launcher.ok, launcher.error = False, "introuvable"
        return launcher
    except PermissionError as exc:
        launcher.ok, launcher.error = False, f"exécution refusée : {exc}"
        return launcher
    except OSError as exc:
        # Where an App Control / Device Guard refusal surfaces on Windows.
        launcher.ok, launcher.error = False, f"lancement impossible : {exc}"
        return launcher
    except subprocess.TimeoutExpired:
        launcher.ok, launcher.error = False, f"délai dépassé ({timeout} s)"
        return launcher

    output = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode != 0:
        launcher.ok = False
        launcher.error = (output.splitlines() or [f"code {proc.returncode}"])[-1][:200]
        return launcher
    if "crux" not in proc.stdout.lower():
        launcher.ok = False
        launcher.error = f"sortie inattendue : {proc.stdout.strip()[:120]!r}"
        return launcher
    launcher.ok, launcher.error = True, None
    return launcher


def probe_all(timeout: int = PROBE_TIMEOUT) -> List[Launcher]:
    return [probe(candidate, timeout) for candidate in candidates()]


def detect(timeout: int = PROBE_TIMEOUT) -> Optional[Launcher]:
    """First candidate that actually runs, or None when none does."""
    for candidate in candidates():
        if probe(candidate, timeout).ok:
            return candidate
    return None


# ------------------------------------------------------------------ storage ---
def store_path() -> Path:
    return paths.home() / "launcher.json"


def save(launcher: Launcher) -> Path:
    from . import state
    payload = launcher.to_dict()
    payload["chosen_at"] = state.now_iso()
    paths.write_json(store_path(), payload)
    return store_path()


def load() -> Optional[Launcher]:
    try:
        raw = paths.read_json(store_path())
    except Exception:
        return None
    return Launcher.from_dict(raw) if isinstance(raw, dict) else None


def resolve(refresh: bool = False, timeout: int = PROBE_TIMEOUT) -> Optional[Launcher]:
    """The recorded launcher, re-probed; a fresh detection when it no longer runs."""
    if not refresh:
        stored = load()
        if stored is not None and probe(stored, timeout).ok:
            return stored
    found = detect(timeout)
    if found is not None:
        save(found)
    return found


# --------------------------------------------------------------- generation ---
def hook_entry(launcher: Launcher, event: str) -> Dict[str, object]:
    """The exec-form command + args a hooks.json entry needs for one event."""
    return {"command": launcher.command,
            "args": [*launcher.prefix_args, "hook", event]}
