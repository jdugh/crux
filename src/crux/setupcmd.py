"""`crux setup`: install the Claude Code plugin, write the arming files and shims.

Nothing here touches a project.  The plugin ships inside the Python package, so
one update moves both halves and they cannot drift apart.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import paths

PLUGIN_NAME = "crux-cc"

# The allow-list is an enumeration, never `Bash(crux:*)`.  `crux hook` and the
# manual `crux decision resolve` are denied outright: a deny rule is applied by
# Claude Code before execution and no allow rule can override it, which is what
# stops Claude fabricating a human decision (ARCHITECTURE.md §7).
ALLOW_RULES: List[str] = [
    "Bash(crux review:*)",
    "Bash(crux resolve:*)",
    "Bash(crux decision propose:*)",
    "Bash(crux decision list:*)",
    "Bash(crux decision show:*)",
    "Bash(crux decision withdraw:*)",
    "Bash(crux route:*)",
    "Bash(crux intent show:*)",
    "Bash(crux status)",
]

DENY_RULES: List[str] = [
    "Bash(crux hook:*)",
    "Bash(crux decision resolve:*)",
]


def gate_settings(mode: str) -> Dict:
    return {
        "env": {"CRUX_GATE": mode},
        "permissions": {"allow": list(ALLOW_RULES), "deny": list(DENY_RULES)},
    }


def write_gate_files() -> List[Path]:
    """The `--settings` files the wrappers pass. Session-scoped, never global."""
    written = []
    for name, mode in (("gate-code.json", "code"), ("gate-both.json", "both")):
        path = paths.claude_dir() / name
        paths.write_json(path, gate_settings(mode))
        written.append(path)
    return written


def package_plugin_dir() -> Path:
    return Path(__file__).resolve().parent / "plugin"


def claude_config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def installed_plugin_dir() -> Path:
    """Where the plugin is installed so Claude Code actually loads it.

    Not ``~/.claude/plugins/``: a directory dropped there is never discovered -
    that path is managed by ``claude plugin install`` and its marketplaces, and a
    bare copy shows up in no listing.  Any folder under ``~/.claude/skills/``
    carrying a ``.claude-plugin/plugin.json`` loads automatically instead, as
    ``<name>@skills-dir``, with no install step and no marketplace to publish.
    """
    return claude_config_dir() / "skills" / PLUGIN_NAME


def legacy_plugin_dir() -> Path:
    """Where v0.1.0 first put it, so `setup` and `uninstall` can clean up."""
    return claude_config_dir() / "plugins" / PLUGIN_NAME


def install_plugin() -> Tuple[Path, str]:
    """Copy the bundled plugin next to Claude Code's other plugins."""
    source = package_plugin_dir()
    if not source.is_dir():
        raise FileNotFoundError(f"plugin introuvable dans le paquet: {source}")
    target = installed_plugin_dir()
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    legacy = legacy_plugin_dir()
    if legacy.exists():
        shutil.rmtree(legacy, ignore_errors=True)
    pin_hook_command(target / "hooks" / "hooks.json")
    return target, "installé"


def pin_hook_command(hooks_file: Path) -> Optional[Path]:
    """Rewrite the installed hooks.json to call Crux by absolute path.

    The package ships ``"command": "crux"``, which reads well and keeps the
    template portable.  At install time we pin it, because resolving a bare name
    is not reliable: on Windows a ``pip --user`` console script is usually off
    PATH, and a spawn without a shell applies no PATHEXT, so ``crux`` would fail
    to launch and every hook would silently no-op - the failure mode hardest to
    notice, since an inert hook looks exactly like a disarmed one.

    Only the installed copy is rewritten; the packaged template is never touched.
    """
    if not hooks_file.is_file():
        return None
    try:
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None

    argv = crux_executable()
    command, extra = argv[0], argv[1:]
    for groups in (data.get("hooks") or {}).values():
        for group in groups or []:
            for hook in group.get("hooks") or []:
                if hook.get("command") == "crux":
                    hook["command"] = command
                    if extra:
                        hook["args"] = extra + list(hook.get("args") or [])
    paths.write_json(hooks_file, data)
    return hooks_file


def crux_executable() -> List[str]:
    """Argv prefix that runs Crux, whatever the install layout.

    ``pip install --user`` puts ``crux.exe`` in a Scripts directory that is
    routinely absent from PATH on Windows, and the hooks invoke ``crux`` by bare
    name. Falling back to ``<python> -m crux`` always works.
    """
    found = shutil.which("crux")
    if found and Path(found).parent != shim_dir():
        return [found]
    for candidate in (
        Path(sys.prefix) / "Scripts" / "crux.exe",
        Path(sys.prefix) / "bin" / "crux",
    ):
        if candidate.is_file():
            return [str(candidate)]
    try:
        import sysconfig
        for scheme in ("nt_user", "posix_user", "nt", "posix_prefix"):
            if scheme not in sysconfig.get_scheme_names():
                continue
            scripts = Path(sysconfig.get_path("scripts", scheme))
            for name in ("crux.exe", "crux"):
                candidate = scripts / name
                if candidate.is_file():
                    return [str(candidate)]
    except Exception:
        pass
    # Always correct, just slower to start: the module is importable wherever
    # this interpreter installed it.
    return [sys.executable, "-m", "crux"]


CRUX_CMD_SHIM = """@echo off
REM Generated by `crux setup` so the hooks can invoke `crux` by bare name.
{invocation} %*
"""

CRUX_SH_SHIM = """#!/bin/sh
# Generated by `crux setup` so the hooks can invoke `crux` by bare name.
exec {invocation} "$@"
"""


def write_crux_shim() -> Optional[Path]:
    """Put `crux` itself on PATH, in the directory the other shims use.

    Claude Code resolves the hook command from PATH. On Windows a
    ``pip install --user`` console script usually is not there, so without this
    every hook would fail silently and the gate would never arm.
    """
    target = shim_dir()
    argv = crux_executable()
    invocation = " ".join(f'"{part}"' if " " in part else part for part in argv)
    if os.name == "nt":
        path = target / "crux.cmd"
        path.write_text(CRUX_CMD_SHIM.format(invocation=invocation),
                        encoding="utf-8", newline="\r\n")
        # Windows also runs Git Bash, which executes neither .cmd nor .ps1 by
        # bare name. An extensionless sh script alongside covers it, and cmd.exe
        # ignores it because it is not in PATHEXT.
        #
        # It runs the interpreter rather than the console script: MSYS refuses to
        # exec a .exe living under AppData\Roaming ("Permission denied") even
        # though cmd.exe and PowerShell run it fine. `python -m crux` works in
        # every shell, and the console script stays the fast path elsewhere.
        sh_path = target / "crux"
        sh_argv = [sys.executable.replace("\\", "/"), "-m", "crux"]
        sh_invocation = " ".join(f'"{part}"' if " " in part else part
                                 for part in sh_argv)
        sh_path.write_text(CRUX_SH_SHIM.format(invocation=sh_invocation),
                           encoding="utf-8", newline="\n")
    else:
        path = target / "crux"
        if shutil.which("crux") and Path(shutil.which("crux")).parent != target:
            return None      # already on PATH from a real install
        path.write_text(CRUX_SH_SHIM.format(invocation=invocation),
                        encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return path


def shim_dir() -> Path:
    return paths.ensure_dir(Path.home() / ".local" / "bin")


SH_SHIM = """#!/bin/sh
# Generated by `crux setup`. Arms Crux for one Claude Code session only.
CRUX_SETTINGS="{settings}"
exec claude --settings "$CRUX_SETTINGS" {extra}"$@"
"""

PS_SHIM = """# Generated by `crux setup`. Arms Crux for one Claude Code session only.
$ErrorActionPreference = 'Stop'
$settings = '{settings}'
& claude --settings $settings {extra}@args
exit $LASTEXITCODE
"""

CMD_SHIM = """@echo off
REM Generated by `crux setup`. Arms Crux for one Claude Code session only.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0{ps_name}" %*
"""


def write_shims() -> List[Path]:
    """`claude-review` (and `claude-plan`, inert until v0.2) on both platforms."""
    target = shim_dir()
    written: List[Path] = []
    specs = [
        ("claude-review", paths.claude_dir() / "gate-code.json", ""),
        ("claude-plan", paths.claude_dir() / "gate-both.json",
         "--permission-mode plan "),
    ]
    for name, settings, extra in specs:
        settings_str = str(settings).replace("\\", "/")
        if os.name == "nt":
            ps_path = target / f"{name}.ps1"
            ps_path.write_text(
                PS_SHIM.format(settings=settings_str, extra=extra),
                encoding="utf-8", newline="\r\n")
            cmd_path = target / f"{name}.cmd"
            cmd_path.write_text(
                CMD_SHIM.format(ps_name=f"{name}.ps1"),
                encoding="utf-8", newline="\r\n")
            written.extend([ps_path, cmd_path])
        else:
            sh_path = target / name
            sh_path.write_text(
                SH_SHIM.format(settings=settings_str, extra=extra),
                encoding="utf-8", newline="\n")
            sh_path.chmod(sh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
            written.append(sh_path)
    return written


def user_config_stub() -> Path:
    path = paths.config_path()
    if not path.is_file():
        paths.ensure_dir(path.parent)
        path.write_text(
            "# Crux - your personal defaults. Shipped disarmed on purpose.\n"
            "version: 1\n"
            "gate:\n"
            '  mode: "off"\n'
            "human:\n"
            "  questions:\n"
            "    mode: human\n"
            "  scope_changes:\n"
            "    mode: ask\n",
            encoding="utf-8", newline="\n")
    return path


def run_setup(install_plugin_too: bool = True) -> Dict[str, object]:
    report: Dict[str, object] = {}
    paths.ensure_dir(paths.home())
    report["config"] = str(user_config_stub())
    report["gate_files"] = [str(p) for p in write_gate_files()]
    report["shims"] = [str(p) for p in write_shims()]
    crux_shim = write_crux_shim()
    if crux_shim is not None:
        report["crux on PATH"] = str(crux_shim)
    if install_plugin_too:
        try:
            target, status = install_plugin()
            report["plugin"] = f"{target} ({status})"
        except Exception as exc:  # never let setup half-fail silently
            report["plugin_error"] = str(exc)
    return report


def uninstall(keep_data: bool = True) -> Dict[str, object]:
    report: Dict[str, object] = {"removed": [], "kept": []}
    for target in (installed_plugin_dir(), legacy_plugin_dir()):
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            report["removed"].append(str(target))      # type: ignore[union-attr]
    for name in ("claude-review", "claude-plan", "crux"):
        for suffix in ("", ".ps1", ".cmd"):
            candidate = shim_dir() / f"{name}{suffix}"
            if candidate.exists():
                candidate.unlink()
                report["removed"].append(str(candidate))  # type: ignore[union-attr]
    if keep_data:
        report["kept"].append(str(paths.home()))          # type: ignore[union-attr]
    else:
        shutil.rmtree(paths.home(), ignore_errors=True)
        report["removed"].append(str(paths.home()))       # type: ignore[union-attr]
    return report
