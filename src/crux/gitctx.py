"""The only module allowed to invoke git, and only through a closed allow-list.

Crux never writes to a repository: not a commit, not the index, not the object
database.  ``add``/``commit``/``reset``/``checkout``/``stash``/``restore`` are not
merely unused, they raise before reaching the process.  ``git diff --no-index`` is
used for session diffs precisely because it operates outside any repository.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
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


class GitTimeout(GitError):
    """git was still running when its bound elapsed.

    Deliberately a distinct class, and deliberately *not* folded into any
    "answer" a caller could mistake for a fact about the repository. A stalled
    ``rev-parse`` must never read as "not a repository", and a stalled
    ``ls-files`` must never read as "no generated files": that would turn a
    technical failure into a functional statement, which is the one thing the
    fail-open rule (I1) is not allowed to do. It propagates as an error and the
    handlers' existing guard turns it into an inert, logged, exit-0 hook.
    """


class NotARepository(GitError):
    pass


# Every git process Crux starts is bounded. The calls on the hook path are
# `rev-parse` and `status`, measured at 35-60 ms on this repository, so ten
# seconds is a hundredfold margin - it is there for a git that has *stopped*
# (a held `index.lock`, a wedged filesystem, a credential helper waiting on a
# prompt), not for a git that is merely slow. Without it, `subprocess.run` waits
# for ever and the only thing that ends the hook is Claude Code killing it at
# its own timeout, which is exactly the 28.7 s run this bound exists to prevent.
DEFAULT_TIMEOUT_SECONDS = 10.0
TIMEOUT_ENV = "CRUX_GIT_TIMEOUT_SECONDS"


def default_timeout() -> float:
    """The bound, overridable by environment - the escape hatch for a slow box."""
    raw = (os.environ.get(TIMEOUT_ENV) or "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_TIMEOUT_SECONDS
        if value > 0:
            return value
    return DEFAULT_TIMEOUT_SECONDS


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


def _run_raw(args: Sequence[str], timeout: Optional[float] = None, **kwargs):
    """Every git process Crux starts, bounded for real.

    Two details here are load-bearing, and both were found by a test rather than
    by reasoning.

    **Output goes to temporary files, not pipes.** ``subprocess.run(timeout=…)``
    kills the child on time, then waits for its pipes to reach end of file - and
    a grandchild still holds them.  git spawns grandchildren routinely: a
    ``core.fsmonitor`` helper, a credential helper, a pager.  Measured: with a
    stalled fsmonitor, ``run`` had killed git within the bound and was still
    blocked sixty seconds later, so the bound was a promise the code did not
    keep.  A regular file has no end-of-file to wait for.

    **stdin is /dev/null.** A git that inherits the hook's stdin can sit waiting
    for input that will never come - a credential helper prompting is exactly
    that - and it would be reading the pipe Claude Code sends the hook payload
    on.  Neither is acceptable in a hook.
    """
    limit = default_timeout() if timeout is None else timeout
    text = bool(kwargs.pop("text", False))
    encoding = kwargs.pop("encoding", None)
    errors = kwargs.pop("errors", None)
    data = kwargs.pop("input", None)
    kwargs.pop("capture_output", None)      # always captured, see above

    def _decode(raw: bytes):
        if not (text or encoding):
            return raw
        return raw.decode(encoding or "utf-8", errors or "strict")

    with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
        stdin = subprocess.DEVNULL
        stack = None
        if data is not None:
            stack = tempfile.TemporaryFile()
            stack.write(data.encode(encoding or "utf-8", errors or "strict")
                        if isinstance(data, str) else data)
            stack.seek(0)
            stdin = stack
        try:
            proc = subprocess.run(
                list(args), stdin=stdin, stdout=out_file, stderr=err_file,
                timeout=limit, shell=False, **kwargs)
        except subprocess.TimeoutExpired:
            raise GitTimeout(
                f"git {' '.join(str(a) for a in args[1:4])} n'a pas rendu la "
                f"main en {limit:g} s — commande abandonnée")
        finally:
            if stack is not None:
                stack.close()
        out_file.seek(0)
        err_file.seek(0)
        return subprocess.CompletedProcess(
            list(args), proc.returncode,
            _decode(out_file.read()), _decode(err_file.read()))


def run(cwd: Path, *args: str, check: bool = True,
        allow_codes: Tuple[int, ...] = (),
        timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """Run a read-only git command. No shell, arguments as a list, always bounded."""
    _check(args)
    proc = _run_raw(
        ["git", *args],
        timeout=timeout,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0 and proc.returncode not in allow_codes:
        raise GitError(
            f"git {' '.join(args)} a échoué ({proc.returncode}): "
            f"{proc.stderr.strip() or 'sans message'}")
    return proc


def git_available() -> Optional[str]:
    try:
        proc = _run_raw(["git", "--version"], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    except (OSError, ValueError, GitTimeout):
        # The one place where a timeout *is* the answer: this function asks
        # "can this machine run git at all", and a git that never returns
        # cannot. It is a diagnostic; it states nothing about a repository.
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


@dataclass
class RepoInfo:
    """What a hook needs to know about a repository, resolved in one go."""

    root: Path
    head: Optional[str]      # None in a repository with no commit yet
    branch: Optional[str]    # "HEAD" when detached, as `--abbrev-ref` reports it


def describe(cwd: Path) -> Optional[RepoInfo]:
    """Work tree, root, HEAD and branch - one git process instead of four.

    ``git rev-parse`` answers several questions in a single run and prints the
    answers in the order they were asked, so the four separate calls the hooks
    used to make (`--is-inside-work-tree`, `--show-toplevel`, `HEAD`,
    `--abbrev-ref HEAD`) are one call here.

    There is exactly one case where reading the output by position would lie: a
    repository with no commit yet.  ``HEAD`` cannot be resolved, git exits 128
    and *omits that line entirely*, so the third line is the branch, not a sha -
    measured, not assumed.  Rather than guess which line went missing, that case
    falls back to the single-purpose helpers, which already handle it correctly.
    Three extra processes in a repository that has never been committed to is a
    price worth paying for not inventing a HEAD.

    Returns None when ``cwd`` is not inside a work tree (including a bare repo),
    which is what ``is_repo`` reports too.  A timeout is *not* an answer here:
    ``GitTimeout`` propagates, so a stalled git can never be read as "no repo".
    """
    proc = run(cwd, "rev-parse", "--is-inside-work-tree", "--show-toplevel",
               "HEAD", "--abbrev-ref", "HEAD", check=False)
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines or lines[0] != "true":
        return None
    if proc.returncode == 0 and len(lines) == 4:
        return RepoInfo(root=Path(lines[1]), head=lines[2] or None,
                        branch=lines[3] or None)
    root = Path(lines[1]) if len(lines) > 1 else repo_root(cwd)
    return RepoInfo(root=root, head=head_sha(cwd), branch=current_branch(cwd))


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
    proc = _run_raw(
        ["git", "show", f"{ref}:{relpath}"],
        cwd=str(cwd), capture_output=True,
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
        attr = _run_raw(
            ["git", "check-attr", "--stdin", "linguist-generated"],
            cwd=str(cwd), input="\n".join(files), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
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
    proc = _run_raw(
        ["git", "--no-pager", "diff", "--no-index", f"--unified={unified}",
         "--no-color", "--", str(base_file), str(current_file)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
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
