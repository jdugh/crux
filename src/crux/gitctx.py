"""The only module allowed to invoke git, and only through a closed allow-list.

Crux never writes to a repository: not a commit, not the index, not the object
database.  ``add``/``commit``/``reset``/``checkout``/``stash``/``restore`` are not
merely unused, they raise before reaching the process.  ``git diff --no-index`` is
used for session diffs precisely because it operates outside any repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# Read-only porcelain and plumbing. Anything not here cannot be run.
ALLOWED_SUBCOMMANDS = {
    "status", "diff", "rev-parse", "merge-base", "log",
    "ls-files", "show", "check-ignore", "cat-file", "check-attr", "version",
}

# Named explicitly so the intent is greppable and testable.
FORBIDDEN_SUBCOMMANDS = {
    "add", "commit", "reset", "checkout", "switch", "restore", "stash", "clean",
    "rm", "mv", "push", "pull", "fetch", "merge", "rebase", "cherry-pick",
    "revert", "apply", "am", "tag", "branch", "worktree", "gc", "prune",
    "update-ref", "update-index", "hash-object", "write-tree", "commit-tree",
    "symbolic-ref", "remote", "config", "init", "clone", "filter-branch",
    "reflog", "notes", "replace", "bisect", "submodule", "sparse-checkout",
}

TRUNK_CANDIDATES = ("main", "master", "trunk", "develop")


class GitSafetyError(RuntimeError):
    """A git subcommand outside the read-only allow-list was attempted."""


class GitError(RuntimeError):
    """git ran and failed for an ordinary reason."""


class NotARepository(GitError):
    pass


def _check(args: Sequence[str]) -> None:
    if not args:
        raise GitSafetyError("aucune sous-commande git")
    sub = args[0]
    if sub in FORBIDDEN_SUBCOMMANDS:
        raise GitSafetyError(
            f"git {sub!r} est interdit : crux n'écrit jamais dans un dépôt")
    if sub not in ALLOWED_SUBCOMMANDS:
        raise GitSafetyError(
            f"git {sub!r} n'est pas dans la liste blanche lecture seule")


def run(cwd: Path, *args: str, check: bool = True,
        allow_codes: Tuple[int, ...] = ()) -> subprocess.CompletedProcess:
    """Run a read-only git command. No shell, arguments as a list."""
    _check(args)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    if check and proc.returncode != 0 and proc.returncode not in allow_codes:
        raise GitError(
            f"git {' '.join(args)} a échoué ({proc.returncode}): "
            f"{proc.stderr.strip() or 'sans message'}")
    return proc


def git_available() -> Optional[str]:
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", shell=False)
    except (OSError, ValueError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def is_repo(cwd: Path) -> bool:
    try:
        proc = run(cwd, "rev-parse", "--is-inside-work-tree", check=False)
    except (OSError, GitSafetyError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def repo_root(cwd: Path) -> Path:
    proc = run(cwd, "rev-parse", "--show-toplevel", check=False)
    if proc.returncode != 0:
        raise NotARepository(f"{cwd} n'est pas dans un dépôt git")
    return Path(proc.stdout.strip())


def head_sha(cwd: Path) -> Optional[str]:
    proc = run(cwd, "rev-parse", "HEAD", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def current_branch(cwd: Path) -> Optional[str]:
    proc = run(cwd, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    name = proc.stdout.strip()
    return name if proc.returncode == 0 and name else None


def detect_trunk(cwd: Path) -> Optional[str]:
    for name in TRUNK_CANDIDATES:
        for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            proc = run(cwd, "rev-parse", "--verify", "--quiet", ref, check=False)
            if proc.returncode == 0:
                return ref
    return None


def merge_base(cwd: Path, a: str, b: str) -> Optional[str]:
    proc = run(cwd, "merge-base", a, b, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def status_entries(cwd: Path) -> List[Tuple[str, str]]:
    """[(status_code, relpath)] from ``git status --porcelain``.

    Renames report the destination path.  Paths use forward slashes.
    """
    proc = run(cwd, "status", "--porcelain", "--untracked-files=all")
    entries: List[Tuple[str, str]] = []
    for raw in proc.stdout.splitlines():
        if len(raw) < 4:
            continue
        code = raw[:2].strip() or "?"
        rest = raw[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip().strip('"')
        entries.append((code, rest.replace("\\", "/")))
    return entries


def tracked_at_head(cwd: Path, relpath: str) -> bool:
    proc = run(cwd, "cat-file", "-e", f"HEAD:{relpath}", check=False)
    return proc.returncode == 0


def show_blob(cwd: Path, ref: str, relpath: str) -> Optional[bytes]:
    """Content of ``relpath`` at an explicit ``ref``, or None if absent there.

    Always called with the commit captured at arming time, never with the
    literal "HEAD": a commit made mid-session must not move the baseline
    retroactively.
    """
    _check(("show",))
    proc = subprocess.run(
        ["git", "show", f"{ref}:{relpath}"],
        cwd=str(cwd), capture_output=True, shell=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def show_head_blob(cwd: Path, relpath: str) -> Optional[bytes]:
    """Content at the *live* HEAD. Not for baselines - see ``show_blob``."""
    return show_blob(cwd, "HEAD", relpath)


def is_ignored(cwd: Path, relpath: str) -> bool:
    proc = run(cwd, "check-ignore", "-q", "--", relpath, check=False)
    return proc.returncode == 0


def generated_paths(cwd: Path) -> List[str]:
    """Paths marked linguist-generated in .gitattributes."""
    proc = run(cwd, "ls-files", check=False)
    if proc.returncode != 0:
        return []
    files = [p for p in proc.stdout.splitlines() if p.strip()]
    if not files:
        return []
    _check(("check-attr",))
    marked: List[str] = []
    try:
        attr = subprocess.run(
            ["git", "check-attr", "--stdin", "linguist-generated"],
            cwd=str(cwd), input="\n".join(files), capture_output=True, text=True,
            encoding="utf-8", errors="replace", shell=False,
        )
    except OSError:
        return []
    if attr.returncode != 0:
        return []
    for line in attr.stdout.splitlines():
        # "<path>: linguist-generated: set"
        if line.endswith(": set"):
            marked.append(line.rsplit(": linguist-generated:", 1)[0])
    return marked


def diff_no_index(base_file: Path, current_file: Path, unified: int = 3) -> str:
    """Unified diff of two arbitrary files. Never touches a repository.

    ``--no-index`` exits 1 when the files differ, which is the normal case here.
    """
    _check(("diff",))
    proc = subprocess.run(
        ["git", "--no-pager", "diff", "--no-index", f"--unified={unified}",
         "--no-color", "--", str(base_file), str(current_file)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        shell=False,
    )
    if proc.returncode not in (0, 1):
        raise GitError(f"git diff --no-index a échoué: {proc.stderr.strip()}")
    return proc.stdout


def working_tree_diff(cwd: Path, base: str = "HEAD") -> str:
    """Classic ``git diff <base>``. Used only when session scoping is disabled."""
    proc = run(cwd, "diff", "--no-color", base, check=False)
    return proc.stdout


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8000]


# Directory and file names that mark a test tree. Used to decide whether a
# project has tests at all, which the router turns into the `code_without_tests`
# signal.
TEST_MARKERS = ("tests", "test", "spec", "__tests__")


def project_has_tests(repo: Path) -> bool:
    """Does this repository track any tests?

    Lives here rather than in ``review`` so the round-2 planner can ask without
    importing the module that drives Codex. ``review.project_has_tests`` is kept
    as an alias.
    """
    try:
        proc = run(repo, "ls-files", check=False)
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        lowered = line.lower()
        if any(f"/{m}/" in f"/{lowered}" for m in TEST_MARKERS):
            return True
        if lowered.startswith("test") or "_test." in lowered or ".test." in lowered:
            return True
    return False
