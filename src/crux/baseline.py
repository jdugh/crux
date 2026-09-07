"""Exact baseline of the working tree at arming time, and the session diff.

A hash tells you *that* something changed; it can never tell you *which lines*
were already yours.  So at arming we snapshot the byte content of every file that
already carried uncommitted changes.  Clean files need no copy: their baseline is
their HEAD blob.  The baseline is therefore, literally, the working tree at the
moment you armed - your prior edits included.

The session diff is then ``diff(baseline, current)``, which excludes your prior
work even inside a file Claude later touches.

This is a separate artefact from the anti-loop fingerprint (see review.py): two
problems, two mechanisms.
"""

from __future__ import annotations

import fnmatch
import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import gitctx, paths, state

MANIFEST = "manifest.json"


@dataclass
class BaselineEntry:
    status: str                  # git porcelain code at capture time
    blob: Optional[str] = None   # snapshot filename, or None when HEAD suffices
    size: int = 0
    skipped: Optional[str] = None  # why no snapshot was taken


@dataclass
class Manifest:
    head: Optional[str]
    branch: Optional[str]
    captured_at: str
    entries: Dict[str, BaselineEntry] = field(default_factory=dict)
    total_bytes: int = 0
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "head": self.head,
            "branch": self.branch,
            "captured_at": self.captured_at,
            "total_bytes": self.total_bytes,
            "warnings": self.warnings,
            "entries": {
                path: {"status": e.status, "blob": e.blob,
                       "size": e.size, "skipped": e.skipped}
                for path, e in self.entries.items()
            },
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Manifest":
        entries = {
            path: BaselineEntry(
                status=val.get("status", "?"), blob=val.get("blob"),
                size=val.get("size", 0), skipped=val.get("skipped"))
            for path, val in (raw.get("entries") or {}).items()
        }
        return cls(
            head=raw.get("head"), branch=raw.get("branch"),
            captured_at=raw.get("captured_at", ""), entries=entries,
            total_bytes=raw.get("total_bytes", 0),
            warnings=list(raw.get("warnings") or []),
        )


def manifest_path(session_id: str) -> Path:
    return paths.baseline_dir(session_id) / MANIFEST


def load_manifest(session_id: str) -> Optional[Manifest]:
    try:
        raw = paths.read_json(manifest_path(session_id))
    except Exception:
        return None
    return Manifest.from_dict(raw) if isinstance(raw, dict) else None


def capture(session_id: str, repo: Path, cfg) -> Manifest:
    """Snapshot the working tree. Idempotent: an existing baseline is kept.

    Re-capturing would silently move the reference point and hide work Claude
    already did, so the first capture of a session wins.
    """
    existing = load_manifest(session_id)
    if existing is not None:
        return existing

    max_file = int(cfg.get("scope.baseline.max_file_bytes", 2 * 1024 * 1024))
    max_total = int(cfg.get("scope.baseline.max_total_bytes", 50 * 1024 * 1024))

    manifest = Manifest(
        head=gitctx.head_sha(repo),
        branch=gitctx.current_branch(repo),
        captured_at=state.now_iso(),
    )

    bdir = paths.baseline_dir(session_id)
    total = 0
    for code, relpath in gitctx.status_entries(repo):
        source = repo / relpath
        if not source.is_file():
            # staged deletion: HEAD is the right baseline, nothing to copy
            manifest.entries[relpath] = BaselineEntry(status=code, blob=None)
            continue
        try:
            size = source.stat().st_size
        except OSError as exc:
            manifest.entries[relpath] = BaselineEntry(
                status=code, skipped=f"illisible: {exc.strerror}")
            manifest.warnings.append(f"{relpath}: illisible à la capture")
            continue

        if size > max_file:
            manifest.entries[relpath] = BaselineEntry(
                status=code, size=size, skipped="fichier trop volumineux")
            manifest.warnings.append(
                f"{relpath}: trop volumineux pour un instantané "
                f"({size} o > {max_file} o) ; vos modifications antérieures y "
                f"seront incluses dans le diff relu")
            continue
        if total + size > max_total:
            manifest.entries[relpath] = BaselineEntry(
                status=code, size=size, skipped="budget d'instantané dépassé")
            manifest.warnings.append(
                f"{relpath}: budget d'instantané dépassé ; vos modifications "
                f"antérieures y seront incluses dans le diff relu")
            continue

        blob = paths.blob_name(relpath)
        try:
            shutil.copy2(str(source), str(bdir / blob))
        except OSError as exc:
            manifest.entries[relpath] = BaselineEntry(
                status=code, size=size, skipped=f"copie impossible: {exc.strerror}")
            manifest.warnings.append(f"{relpath}: instantané impossible")
            continue
        total += size
        manifest.entries[relpath] = BaselineEntry(status=code, blob=blob, size=size)

    manifest.total_bytes = total
    paths.write_json(manifest_path(session_id), manifest.to_dict())
    return manifest


def baseline_bytes(session_id: str, repo: Path, relpath: str,
                   manifest: Optional[Manifest],
                   use_snapshots: bool = True) -> bytes:
    """The exact content of ``relpath`` when the session was armed.

    Three sources, in order: the snapshot when the file was already dirty, the
    blob at the **captured** commit when it was clean, empty when the file did
    not exist at arming time.

    The captured commit matters. Resolving a clean file against the live ``HEAD``
    would let a commit made mid-session move the baseline retroactively, and work
    Claude did earlier in the session would silently drop out of the diff. The
    reference point is fixed at arming and never moves.
    """
    entry = manifest.entries.get(relpath) if manifest else None
    if not use_snapshots:
        # scope.session_edits_only: false - compare against the captured
        # commit only, so changes that predate arming reappear in the diff.
        ref = manifest.head if manifest and manifest.head else None
        content = gitctx.show_blob(repo, ref, relpath) if ref else None
        return content if content is not None else b""
    if entry is not None and entry.blob:
        blob = paths.baseline_dir(session_id) / entry.blob
        try:
            return blob.read_bytes()
        except OSError:
            pass  # snapshot unreadable: fall through to the captured commit
    if entry is not None and entry.status.startswith("?") and not entry.blob:
        return b""

    ref = manifest.head if manifest and manifest.head else None
    if ref is None:
        # No baseline, or a repository with no commit yet: nothing to compare to.
        return b""
    content = gitctx.show_blob(repo, ref, relpath)
    return content if content is not None else b""


def _own_data_globs(repo: Path) -> List[str]:
    """Crux must never review its own bookkeeping.

    ``.crux/`` is the documented project directory, and $CRUX_HOME can legally be
    pointed inside a repository (a test harness does exactly that). Either way,
    baseline blobs and ledgers must not end up in a diff sent to a reviewer.
    """
    globs = [".crux/**", "**/.crux/**"]
    try:
        home = paths.home().resolve()
        inside = home.relative_to(Path(repo).resolve()).as_posix()
    except (ValueError, OSError):
        return globs
    if inside and inside != ".":
        globs.extend([f"{inside}/**", f"{inside}"])
    return globs


def _excluded(relpath: str, patterns: List[str]) -> bool:
    candidates = (relpath, "/" + relpath)
    for pattern in patterns:
        for candidate in candidates:
            if fnmatch.fnmatch(candidate, pattern):
                return True
        # `**/x` should also match a top-level `x`
        if pattern.startswith("**/") and fnmatch.fnmatch(relpath, pattern[3:]):
            return True
    return False


def _rewrite_headers(diff_text: str, relpath: str,
                     base_empty: bool, current_missing: bool) -> str:
    """Turn `--no-index` temp paths back into repo-relative a/ and b/ paths."""
    if not diff_text.strip():
        return ""
    out: List[str] = [f"diff --git a/{relpath} b/{relpath}"]
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            continue
        if line.startswith("--- "):
            out.append("--- /dev/null" if base_empty else f"--- a/{relpath}")
        elif line.startswith("+++ "):
            out.append("+++ /dev/null" if current_missing else f"+++ b/{relpath}")
        elif line.startswith("index ") or line.startswith("similarity index"):
            continue
        else:
            out.append(line)
    return "\n".join(out) + "\n"


@dataclass
class SessionDiff:
    text: str = ""
    fingerprint: str = ""
    files: List[str] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    deleted_files: List[str] = field(default_factory=list)
    new_files: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    truncated: bool = False
    # relpath -> "tool" (seen via Edit/Write) or "unknown" (shell, editor…)
    provenance: Dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    @property
    def changed_lines(self) -> int:
        return self.added + self.removed


def compute(session_id: str, repo: Path, cfg,
            only_paths: Optional[List[str]] = None) -> SessionDiff:
    """Diff of what changed since arming, restricted to what Claude touched."""
    manifest = load_manifest(session_id)
    result = SessionDiff()
    if manifest is not None:
        result.warnings.extend(manifest.warnings)

    session_only = bool(cfg.get("scope.session_edits_only", True))
    candidates: List[str]

    if only_paths is not None:
        candidates = list(only_paths)
    else:
        # The candidate set comes from the working tree and the baseline, never
        # from the edits journal. Claude can change files through Bash - sed, a
        # Python one-liner, a formatter - and PostToolUse on Edit/Write sees none
        # of it. Anchoring on the journal silently dropped those changes from
        # review. The baseline is the source of truth: anything whose current
        # content differs from the captured state is in scope, whatever produced
        # it.
        current = [p for _, p in gitctx.status_entries(repo)]
        from_baseline = list(manifest.entries) if manifest else []
        candidates = current + from_baseline

    seen = set()
    ordered = []
    for relpath in candidates:
        norm = relpath.replace("\\", "/")
        if norm not in seen:
            seen.add(norm)
            ordered.append(norm)

    # The journal no longer decides what is reviewed; it now only attributes what
    # was found, so an unattributed change is flagged rather than hidden.
    journal = set(state.edited_paths(session_id)) if session_only else set()

    excludes = list(cfg.get("scope.exclude") or [])
    excludes.extend(_own_data_globs(repo))
    generated = set(gitctx.generated_paths(repo))
    max_file_lines = int(cfg.get("scope.max_file_diff_lines", 1500))
    max_chars = int(cfg.get("scope.max_diff_chars", 60000))

    chunks: List[str] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="crux-baseline-"))
    try:
        for relpath in ordered:
            if _excluded(relpath, excludes) or relpath in generated:
                continue

            base = baseline_bytes(session_id, repo, relpath, manifest,
                                  use_snapshots=session_only)
            current_file = repo / relpath
            current_missing = not current_file.is_file()
            current = b"" if current_missing else current_file.read_bytes()

            if base == current:
                continue
            if gitctx.looks_binary(base) or gitctx.looks_binary(current):
                result.warnings.append(f"{relpath}: binaire, non relu")
                continue

            base_file = tmpdir / "base"
            cur_file = tmpdir / "cur"
            base_file.write_bytes(base)
            cur_file.write_bytes(current)

            raw = gitctx.diff_no_index(base_file, cur_file)
            text = _rewrite_headers(raw, relpath,
                                    base_empty=not base,
                                    current_missing=current_missing)
            if not text:
                continue

            lines = text.splitlines()
            if len(lines) > max_file_lines:
                kept = lines[:max_file_lines]
                text = "\n".join(kept) + (
                    f"\n… diff tronqué : {len(lines) - max_file_lines} lignes "
                    f"supplémentaires pour {relpath}\n")
                result.truncated = True

            for line in text.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    result.added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    result.removed += 1

            result.files.append(relpath)
            result.provenance[relpath] = "tool" if relpath in journal else "unknown"
            if current_missing:
                result.deleted_files.append(relpath)
            elif not base:
                result.new_files.append(relpath)
            chunks.append(text)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    text = "".join(chunks)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… diff tronqué (budget de caractères atteint)\n"
        result.truncated = True

    result.text = text
    result.fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()

    unattributed = [f for f in result.files
                    if result.provenance.get(f) == "unknown"]
    if unattributed and session_only:
        result.warnings.append(
            "modifiés sans passer par Edit/Write (shell, script ou éditeur) — "
            "relus quand même, provenance inconnue : "
            + ", ".join(sorted(unattributed)[:8])
            + ("…" if len(unattributed) > 8 else ""))

    # Did the human edit a file after Claude wrote it?
    for relpath in result.files:
        expected = state.last_written_sha(session_id, relpath)
        current_file = repo / relpath
        if expected and current_file.is_file():
            actual = hashlib.sha256(current_file.read_bytes()).hexdigest()
            if actual != expected:
                result.warnings.append(
                    f"{relpath}: modifié hors session après l'écriture de Claude")
    return result


def added_lines(diff_text: str) -> str:
    """Only the added lines - what the router inspects for content signals."""
    return "\n".join(
        line[1:] for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++"))


def diff_stats(diff: SessionDiff) -> Tuple[int, int, int]:
    return len(diff.files), diff.added, diff.removed
