"""`crux doctor`: what is installed, what is armed, and what actually works.

Capabilities are *probed*, never inferred from a version string.  One line is an
invariant check rather than information: it confirms that the shipped hooks.json
registers no PreToolUse matcher on AskUserQuestion, i.e. that the mechanism which
could answer on your behalf is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from . import (capabilities, config, context, gitctx, paths, setupcmd, state)

OK = "✓"
BAD = "✗"
WARN = "!"
UNKNOWN = "?"
DASH = "–"


class Report:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.blocking = 0

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(f"  {title}")

    def row(self, mark: str, label: str, value: str = "", hint: str = "") -> None:
        text = f"  {mark} {label:<18}{value}"
        if hint:
            # Always keep a gap: a long value must not run into its hint.
            text = f"{text:<52}" if len(text) < 52 else f"{text}  "
            text += hint
        self.lines.append(text.rstrip())
        if mark == BAD:
            self.blocking += 1

    def render(self) -> str:
        return "\n".join(self.lines)


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _version(cmd: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", shell=False,
                              timeout=20)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and \
        proc.stdout.strip() else None


def _codex_paths_across_shells() -> List[Tuple[str, Optional[str]]]:
    """PowerShell and Git Bash do not share a PATH on Windows; check both."""
    results: List[Tuple[str, Optional[str]]] = [("process", _which("codex"))]
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Command codex -ErrorAction SilentlyContinue).Source"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", shell=False, timeout=25)
            results.append(("powershell", proc.stdout.strip() or None))
        except (OSError, subprocess.TimeoutExpired):
            results.append(("powershell", None))
    return results


def _ask_user_question_rows() -> List[Tuple[str, str, str]]:
    """Three separate statements, because one line conflated them.

    "no AskUserQuestion matcher" read as though Crux ignored the tool entirely,
    while a PostToolUse matcher is exactly what carries human provenance. The
    guarantee is narrower and worth stating precisely: no PRE-tool auto-answer,
    and a POST-tool recorder that is present and must stay.
    """
    rows: List[Tuple[str, str, str]] = []
    data = _installed_hooks()
    if data is None:
        return [(WARN, "AskUserQuestion", "hooks.json illisible")]

    pre = [g.get("matcher", "") for g in data.get("PreToolUse", []) or []]
    post = [g.get("matcher", "") for g in data.get("PostToolUse", []) or []]
    auto_answer = any("AskUserQuestion" in m for m in pre)
    recorder = any("AskUserQuestion" in m for m in post)

    rows.append((OK, "AskUserQuestion", "mode human"))
    rows.append((BAD if auto_answer else OK, "  auto-réponse",
                 "PreToolUse ENREGISTRÉ — Crux pourrait répondre à votre place"
                 if auto_answer else "aucun PreToolUse d'auto-réponse"))
    rows.append((OK if recorder else BAD, "  provenance",
                 "PostToolUse actif — la réponse humaine est enregistrée"
                 if recorder else "PostToolUse ABSENT — aucune provenance humaine"))
    return rows


def _launcher_rows() -> List[Tuple[str, str, str, str]]:
    """Separate what exists from what actually runs.

    "✓ crux installé" was true and useless while Device Guard refused to spawn
    the console script: the package was installed, the script was there, and
    every hook died at spawn. These rows never conflate the two.
    """
    from . import launcher as _launcher
    rows: List[Tuple[str, str, str, str]] = []

    script = _launcher.console_script_path()
    if script is None:
        rows.append((DASH, "console-script", "absent",
                     "sans importance si un lanceur Python fonctionne"))
        script_ok = None
    else:
        probed = _launcher.probe(_launcher.Launcher([str(script)],
                                                    _launcher.CONSOLE_SCRIPT, ""))
        script_ok = probed.ok
        rows.append((OK, "console-script", "présent", str(script)))
        rows.append((OK if probed.ok else BAD, "  exécutable",
                     "oui" if probed.ok else "NON — refusé au lancement",
                     "" if probed.ok else (probed.error or "")[:64]))

    module = _launcher.probe(_launcher.Launcher(
        [sys.executable, "-m", "crux"], _launcher.PYTHON_MODULE, ""))
    rows.append((OK if module.ok else BAD, "python -m crux",
                 "fonctionne" if module.ok else "NE FONCTIONNE PAS",
                 sys.executable if module.ok else (module.error or "")[:64]))

    chosen = _launcher.load()
    if chosen is None:
        rows.append((WARN, "lanceur retenu", "aucun enregistré", "→ crux setup"))
    else:
        live = _launcher.probe(_launcher.Launcher(list(chosen.argv),
                                                  chosen.kind, chosen.source))
        rows.append((OK if live.ok else BAD, "lanceur retenu",
                     chosen.display()[:44],
                     chosen.source if live.ok
                     else f"NE FONCTIONNE PLUS — {(live.error or '')[:40]} → crux setup"))

    hooks_cmd = _hook_launcher()
    shim_cmd = _shim_launcher()
    expected = chosen.argv if chosen else None

    rows.append(_agreement_row("lanceur des hooks", hooks_cmd, expected,
                               script_ok))
    rows.append(_agreement_row("lanceur des shims", shim_cmd, expected,
                               script_ok))
    return rows


def _agreement_row(label: str, actual: Optional[List[str]],
                   expected: Optional[List[str]],
                   script_ok: Optional[bool]) -> Tuple[str, str, str, str]:
    if actual is None:
        return (WARN, label, "introuvable", "→ crux setup")
    shown = " ".join(actual)[:44]
    from . import launcher as _launcher
    script = _launcher.console_script_path()
    if script is not None and script_ok is False and Path(actual[0]) == script:
        return (BAD, label, shown,
                "pointe sur un exécutable bloqué → crux setup")
    if expected is not None and list(actual) != list(expected):
        return (WARN, label, shown, "diffère du lanceur retenu → crux setup")
    return (OK, label, shown, "")


def _hook_launcher() -> Optional[List[str]]:
    data = _installed_hooks()
    if not data:
        return None
    for groups in data.values():
        for group in groups or []:
            for hook in group.get("hooks") or []:
                args = list(hook.get("args") or [])
                prefix = args[:-2] if len(args) >= 2 else []
                return [hook.get("command", "?"), *prefix]
    return None


def _shim_launcher() -> Optional[List[str]]:
    """Read back what the generated shim really invokes."""
    import shlex
    name = "crux.cmd" if os.name == "nt" else "crux"
    candidate = setupcmd.shim_dir() / name
    if not candidate.is_file():
        return None
    for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("@echo", "REM", "#", "exec")):
            if not stripped.startswith("exec "):
                continue
            stripped = stripped[len("exec "):]
        try:
            parts = shlex.split(stripped, posix=(os.name != "nt"))
        except ValueError:
            continue
        parts = [p for p in parts if p not in ("%*", '"$@"', "$@")]
        if parts:
            return parts
    return None


def _installed_hooks():
    candidates = [setupcmd.installed_plugin_dir() / "hooks" / "hooks.json",
                  setupcmd.package_plugin_dir() / "hooks" / "hooks.json"]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            return json.loads(candidate.read_text(encoding="utf-8")).get("hooks", {})
        except (ValueError, OSError):
            return None
    return None


def _hooks_invariant() -> Tuple[str, str]:
    """How many hooks are registered. The AskUserQuestion guarantee is separate."""
    candidates = [setupcmd.installed_plugin_dir() / "hooks" / "hooks.json",
                  setupcmd.package_plugin_dir() / "hooks" / "hooks.json"]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return WARN, "hooks.json illisible"
        events = data.get("hooks", {})
        registered = []
        for event, groups in events.items():
            for group in groups or []:
                matcher = group.get("matcher", "")
                registered.append(f"{event}:{matcher}" if matcher else event)
        return OK, f"{len(registered)} enregistrés"
    return WARN, "hooks.json introuvable"


def run(probe: bool = False) -> int:
    report = Report()
    cwd = Path.cwd()

    report.section("Environnement")
    report.row(OK, "python", sys.version.split()[0])
    git_version = gitctx.git_available()
    report.row(OK if git_version else BAD, "git", git_version or "introuvable",
               "" if git_version else "→ installez git")
    claude = _which("claude")
    claude_version = _version(["claude", "--version"]) if claude else None
    report.row(OK if claude else WARN, "claude",
               claude_version or ("introuvable" if not claude else "?"),
               claude or "")

    codex_bin = None
    for label, found in _codex_paths_across_shells():
        if found:
            codex_bin = found
            break
    codex_version = capabilities.codex_version() if codex_bin else None
    report.row(OK if codex_version else BAD, "codex",
               codex_version or "introuvable",
               "" if codex_version else "→ npm i -g @openai/codex")
    logged_in = capabilities.codex_logged_in()
    if not codex_version:
        # An auth file can exist while the binary is unreachable (installed
        # elsewhere, or missing from this shell's PATH). Reporting a tick here
        # would contradict the line above, so stay neutral and say which it is.
        report.row(DASH, "codex auth",
                   "session trouvée, binaire non atteignable" if logged_in
                   else "non vérifiable (codex absent)",
                   "→ rendez `codex` accessible depuis le PATH")
    elif logged_in is True:
        report.row(OK, "codex auth", "session enregistrée")
    else:
        report.row(BAD, "codex auth",
                   "aucune session" if logged_in is False else DASH,
                   "→ codex login   (abonnement ChatGPT)")

    if os.name == "nt":
        found = dict(_codex_paths_across_shells())
        # npm ships codex.cmd and codex.ps1 side by side; different extensions
        # from the same directory are normal, not a divergence worth flagging.
        dirs = {str(Path(p).parent).lower()
                for p in found.values() if p}
        if len(dirs) > 1:
            report.row(WARN, "codex PATH",
                       "résolution différente entre shells",
                       f"process={found.get('process')} ps={found.get('powershell')}")

    # ---------------------------------------------------------- capabilities
    report.section("Capacités  (sondées, jamais déduites d'une version)")
    try:
        cfg_for_caps = config.load(cwd)
    except config.ConfigError:
        cfg_for_caps = None

    if cfg_for_caps is None:
        report.row(UNKNOWN, "sondes", "configuration invalide")
    elif not codex_version:
        report.row(UNKNOWN, "codex exec", "non testé (codex absent)")
        report.row(UNKNOWN, "--output-schema", "non testé (codex absent)")
    elif probe:
        probe_repo = gitctx.repo_root(cwd) if gitctx.is_repo(cwd) else None
        result = capabilities.probe_output_schema(cfg_for_caps, repo=probe_repo)
        supported = result.get("supported")
        report.row(OK if supported else BAD, "--output-schema",
                   "supporté" if supported else "non supporté",
                   result.get("reason", "")[:60])
        capabilities.get(cfg_for_caps, refresh=True, repo=probe_repo)
        report.row(OK, "codex exec", "testé de bout en bout")
    else:
        caps = capabilities.get(cfg_for_caps)
        cached = (caps.get("output_schema") or {}).get("supported")
        label = {True: "supporté", False: "non supporté"}.get(cached, "non sondé")
        report.row(OK if cached else UNKNOWN, "--output-schema", label,
                   "→ crux doctor --probe pour tester réellement")
    report.row(DASH, "updatedInput/Ask", "non sondé",
               "modes advise/auto hors MVP (décision produit)")

    # --------------------------------------------------------- installation
    report.section("Lancement")
    for row_mark, label, value, hint in _launcher_rows():
        report.row(row_mark, label, value, hint)

    report.section("Installation")
    from . import __version__
    report.row(OK, "crux (paquet)", __version__)
    plugin_dir = setupcmd.installed_plugin_dir()
    report.row(OK if plugin_dir.is_dir() else WARN, "plugin crux-cc",
               "installé" if plugin_dir.is_dir() else "absent",
               "" if plugin_dir.is_dir() else "→ crux setup")
    mark, detail = _hooks_invariant()
    report.row(mark, "hooks", detail)
    for row_mark, label, value in _ask_user_question_rows():
        report.row(row_mark, label, value)

    shims = [p for p in (setupcmd.shim_dir().glob("claude-review*"))]
    report.row(OK if shims else WARN, "shims",
               ", ".join(p.name for p in shims) or "absents",
               "" if shims else "→ crux setup")
    on_path = _which("claude-review") is not None
    report.row(OK if on_path else WARN, "shims dans PATH",
               "oui" if on_path else "non",
               "" if on_path else f"→ ajoutez {setupcmd.shim_dir()} au PATH")

    gate_file = paths.claude_dir() / "gate-code.json"
    if gate_file.is_file():
        try:
            gate_data = json.loads(gate_file.read_text(encoding="utf-8"))
            deny = gate_data.get("permissions", {}).get("deny", [])
            allow = gate_data.get("permissions", {}).get("allow", [])
            good = ("Bash(crux hook:*)" in deny
                    and "Bash(crux decision resolve:*)" in deny
                    and "Bash(crux:*)" not in allow)
            report.row(OK if good else BAD, "provenance",
                       "règles deny en place" if good
                       else "règles deny manquantes",
                       "" if good else "→ crux setup")
        except (ValueError, OSError):
            report.row(BAD, "provenance", "gate-code.json illisible")
    else:
        report.row(WARN, "provenance", "gate-code.json absent", "→ crux setup")

    home = paths.home()
    report.row(OK if home.is_dir() else WARN, "~/.crux",
               str(home) if home.is_dir() else "absent")

    # -------------------------------------------------------------- project
    report.section(f"Projet — {cwd}")
    if not gitctx.is_repo(cwd):
        report.row(WARN, "dépôt git", "hors dépôt",
                   "Crux exige un dépôt git pour s'armer")
    else:
        repo = gitctx.repo_root(cwd)
        branch = gitctx.current_branch(repo) or "?"
        dirty = len(gitctx.status_entries(repo))
        report.row(OK, "dépôt git", f"branche {branch}, {dirty} fichier(s) modifié(s)")
        try:
            cfg = config.load(cwd)
        except config.ConfigError as exc:
            report.row(BAD, ".crux.yml", "invalide", str(exc)[:70])
            cfg = None
        if cfg is not None:
            report.row(OK if cfg.project_config else WARN, ".crux.yml",
                       "valide (version 1)" if cfg.project_config else "absent",
                       "" if cfg.project_config else "→ crux init")
            report.row(OK, "autorité",
                       f"questions={cfg.get('human.questions.mode')} · "
                       f"scope_changes={cfg.get('human.scope_changes.mode')}")
            personas = context.available_personas(repo)
            authority = [p for p in personas
                         if (context.load_persona(p, repo) or
                             context.Persona("", "", "")).scope_authority]
            report.row(OK if authority else WARN, "personas",
                       f"{len(personas)} disponibles",
                       f"autorité: {', '.join(authority) or 'aucune'}")
            gate = state.resolve_gate(cfg, None)
            report.row(WARN if not gate.armed else OK, "gate",
                       f"{gate.mode} (source : {gate.source})",
                       "" if gate.armed
                       else "armez avec `claude-review` ou `/crux:on`")

    report.section("Bout-en-bout")
    if not codex_version:
        report.row(BAD, "codex exec", "ignoré (codex absent)")
    elif probe:
        report.row(OK, "codex exec", "voir la sonde ci-dessus")
    else:
        report.row(UNKNOWN, "codex exec", "non exécuté",
                   "→ crux doctor --probe")

    sys.stdout.write(report.render() + "\n\n")
    if report.blocking:
        sys.stdout.write(
            f"  {report.blocking} problème(s) bloquant(s). "
            f"Corrigez puis relancez `crux doctor`.\n")
        return 1
    sys.stdout.write("  Tout est en place.\n")
    return 0
