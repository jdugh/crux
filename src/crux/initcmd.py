"""`crux init`: detect the project, write .crux.yml, offer a .gitignore line.

Nothing is written without confirmation, and the file it produces is disarmed:
adopting Crux in a repository must never change what `claude` does there.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from . import gitctx, paths

TEMPLATE = """\
version: 1

# L'humain décide. Ces deux réglages sont indépendants.
human:
  questions:
    mode: human        # human | advise | auto   (advise/auto: hors MVP)
  scope_changes:
    mode: ask          # ask | warn | auto
  intent:
    send_to_reviewers: true    # true | redacted | false
    max_chars: 8000

gate:
  mode: "off"          # off | code | plan | both
  max_rounds: 2
  block_on: high       # critical | high | medium | low
  budget_seconds: 900

scope:
  base: auto
  session_edits_only: true
  exclude:
{excludes}
  max_diff_chars: 60000

reviewers:
  always: [code-quality]        # porte l'autorité de périmètre
  auto:   [architecture, security]
  never:  []
  max_selected: 4
  parallel: 3
  timeout_seconds: 300

tests:
  command: {test_command}
  auto_run: when-selected       # exécution des tests : v0.3

codex:
  bin: codex
  sandbox: read-only

logs:
  dir: user
  keep_runs: 30
"""

DEFAULT_EXCLUDES = [
    "**/*.lock", "**/package-lock.json", "**/dist/**", "**/build/**",
    "**/node_modules/**", "**/*.min.*", "**/__snapshots__/**", "**/*.svg",
    "**/*.log", "**/*.tmp",
]

UI_HINTS = (".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".html")


def detect_test_command(repo: Path) -> Optional[str]:
    """Detected, never trusted: detection alone does not authorise execution."""
    package = repo / "package.json"
    if package.is_file():
        try:
            import json
            data = json.loads(package.read_text(encoding="utf-8"))
            script = (data.get("scripts") or {}).get("test")
            if script:
                return "npm test"
        except (ValueError, OSError):
            pass
    if (repo / "pytest.ini").is_file() or (repo / "pyproject.toml").is_file() \
            or (repo / "setup.cfg").is_file():
        return "pytest -q"
    if (repo / "Cargo.toml").is_file():
        return "cargo test"
    if (repo / "go.mod").is_file():
        return "go test ./..."
    return None


def detect_ui(repo: Path) -> bool:
    try:
        proc = gitctx.run(repo, "ls-files", check=False)
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    return any(line.lower().endswith(UI_HINTS)
               for line in proc.stdout.splitlines())


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        answer = input(f"{question} [o/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("o", "oui", "y", "yes")


def run(force: bool = False, yes: bool = False) -> int:
    cwd = Path.cwd()
    if not gitctx.is_repo(cwd):
        sys.stderr.write(
            f"{cwd} n'est pas dans un dépôt git. Crux exige un dépôt git.\n")
        return 4
    repo = gitctx.repo_root(cwd)
    target = repo / paths.PROJECT_CONFIG_NAME

    if target.exists() and not force:
        sys.stderr.write(
            f"{target} existe déjà. Utilisez --force pour l'écraser.\n")
        return 2

    test_command = detect_test_command(repo)
    excludes = list(DEFAULT_EXCLUDES)
    body = TEMPLATE.format(
        excludes="\n".join(f"    - \"{glob}\"" for glob in excludes),
        test_command=f"{test_command}" if test_command else "null",
    )

    sys.stdout.write(f"Projet   : {repo}\n")
    sys.stdout.write(f"Tests    : {test_command or 'non détectés'}\n")
    sys.stdout.write(f"Interface: {'détectée' if detect_ui(repo) else 'non détectée'}\n")
    sys.stdout.write(f"Fichier  : {target}\n")
    sys.stdout.write("Le gate est écrit DÉSARMÉ (`mode: \"off\"`) : adopter Crux "
                     "ne change rien à `claude`.\n\n")

    if not _confirm(f"Écrire {target} ?", yes):
        sys.stdout.write("Annulé, aucun fichier écrit.\n")
        return 0

    paths.write_atomic(target, body)
    sys.stdout.write(f"Écrit    : {target}\n")

    gitignore = repo / ".gitignore"
    needs_line = True
    if gitignore.is_file():
        needs_line = ".crux/" not in gitignore.read_text(
            encoding="utf-8", errors="replace")
    if needs_line and _confirm("Ajouter `.crux/` à .gitignore ?", yes):
        with open(gitignore, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n# Crux (rapports locaux ; les registres vivent dans "
                     "~/.crux)\n.crux/\n")
        sys.stdout.write(f"Modifié  : {gitignore}\n")

    sys.stdout.write(
        "\nProchaine étape : `claude-review` pour armer le gate sur une session, "
        "ou `gate.mode: code` dans .crux.yml pour l'armer par défaut ici.\n")
    return 0
