"""Per-session state, and the single function that decides whether Crux is armed.

``resolve_gate`` is called first thing by every hook handler.  When it returns
``off`` the handler exits 0 without writing anything: a plain ``claude`` session
must be indistinguishable from one where Crux is not installed.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths
from .config import Config

GATE_MODES = ("off", "code", "plan", "both")
_TRUTHY = {"1", "true", "yes", "on"}


@dataclass
class GateDecision:
    mode: str
    source: str

    @property
    def armed(self) -> bool:
        return self.mode != "off"

    @property
    def code(self) -> bool:
        return self.mode in ("code", "both")

    @property
    def plan(self) -> bool:
        return self.mode in ("plan", "both")


class SessionResolutionError(Exception):
    """No session could be determined without guessing.

    Falling back to a shared ``default`` session is how D1 once landed in one
    session while the hooks were writing to another: the decision existed, and
    the gate that should have enforced it was looking somewhere else. Silence is
    not an option here - either the session is determined, or we say why not.
    """

    def __init__(self, message: str, candidates: Optional[List[str]] = None):
        super().__init__(message)
        self.message = message
        self.candidates = candidates or []


@dataclass
class SessionState:
    session_id: str
    repo: Optional[str] = None
    gate_override: Optional[str] = None      # set by /crux:on and /crux:off
    armed_at: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    head: Optional[str] = None
    branch: Optional[str] = None
    round: int = 0
    plan_round: int = 0
    last_diff_fingerprint: Optional[str] = None
    budget_spent: float = 0.0
    baseline_captured: bool = False
    last_run_id: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "repo": self.repo,
            "gate_override": self.gate_override,
            "armed_at": self.armed_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "head": self.head,
            "branch": self.branch,
            "round": self.round,
            "plan_round": self.plan_round,
            "last_diff_fingerprint": self.last_diff_fingerprint,
            "budget_spent": self.budget_spent,
            "baseline_captured": self.baseline_captured,
            "last_run_id": self.last_run_id,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SessionState":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})


def state_path(session_id: str) -> Path:
    return paths.session_dir(session_id) / "state.json"


def load(session_id: str) -> SessionState:
    raw = None
    try:
        raw = paths.read_json(state_path(session_id))
    except Exception:
        raw = None  # a corrupt state file is a technical failure: fail open (I1)
    if not isinstance(raw, dict):
        return SessionState(session_id=session_id)
    try:
        return SessionState.from_dict(raw)
    except TypeError:
        return SessionState(session_id=session_id)


def save(state: SessionState) -> None:
    paths.write_json(state_path(state.session_id), state.to_dict())


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _env_gate(env: Dict[str, str]) -> Optional[str]:
    raw = (env.get("CRUX_GATE") or "").strip().lower()
    return raw if raw in GATE_MODES else None


def resolve_gate(cfg: Config,
                 session_id: Optional[str] = None,
                 env: Optional[Dict[str, str]] = None) -> GateDecision:
    """Resolve the gate. First source that answers wins.

    1. CRUX_DISABLE        absolute kill switch
    2. session override    /crux:on and /crux:off
    3. CRUX_GATE           what the claude-review wrapper sets
    4. project .crux.yml   gate.mode
    5. ~/.crux/config.yml  gate.mode
    6. built-in default    off
    """
    env = dict(os.environ if env is None else env)

    if (env.get("CRUX_DISABLE") or "").strip().lower() in _TRUTHY:
        return GateDecision("off", "CRUX_DISABLE")

    if session_id:
        override = load(session_id).gate_override
        if override in GATE_MODES:
            return GateDecision(override, "session (/crux:on)")

    from_env = _env_gate(env)
    if from_env is not None:
        return GateDecision(from_env, "CRUX_GATE")

    mode = cfg.get("gate.mode", "off")
    source = cfg.source_of("gate.mode")
    if mode in GATE_MODES:
        return GateDecision(mode, source)
    return GateDecision("off", "défaut intégré")


def set_override(session_id: str, mode: Optional[str]) -> SessionState:
    """Arm or disarm from inside a session. ``None`` clears the override."""
    if mode is not None and mode not in GATE_MODES:
        raise ValueError(f"mode inconnu: {mode}")
    st = load(session_id)
    st.gate_override = mode
    if mode and mode != "off" and not st.armed_at:
        st.armed_at = now_iso()
    save(st)
    return st


def edits_path(session_id: str) -> Path:
    return paths.session_dir(session_id) / "edits.jsonl"


def record_edit(session_id: str, relpath: str, content_sha: Optional[str]) -> None:
    paths.append_jsonl(edits_path(session_id), {
        "ts": now_iso(),
        "path": relpath,
        "sha256": content_sha,
    })


def edited_paths(session_id: str) -> List[str]:
    """Files Claude wrote during this session, in first-touch order."""
    out: List[str] = []
    seen = set()
    path = edits_path(session_id)
    if not path.is_file():
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                import json
                rec = json.loads(line)
            except ValueError:
                continue  # torn line: skip, never crash a hook
            rel = rec.get("path")
            if rel and rel not in seen:
                seen.add(rel)
                out.append(rel)
    return out


def last_written_sha(session_id: str, relpath: str) -> Optional[str]:
    """Hash of the content Claude last wrote to ``relpath``, if any."""
    result = None
    path = edits_path(session_id)
    if not path.is_file():
        return None
    import json
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("path") == relpath:
                result = rec.get("sha256")
    return result


# --------------------------------------------------- session ↔ repo registry ---
# How long a session with no SessionEnd may still count as active. A crashed or
# force-quit session leaves no end marker; without a cutoff every past session in
# a repository would make resolution ambiguous forever. This is a cutoff, not a
# preference: two sessions inside the window raise ambiguity, they are never
# ranked by recency.
STALE_AFTER_SECONDS = 24 * 3600


def register(session_id: str, repo: Optional[Path]) -> SessionState:
    """Record that ``session_id`` belongs to ``repo``.

    Written on SessionStart whether or not the gate is armed. This is the only
    thing an unarmed session leaves behind: a few bytes inside Crux's own
    directory, no output, nothing in the project. It is what lets `/crux:on` and
    `crux decision propose` find the right session without being told.
    """
    st = load(session_id)
    st.repo = str(Path(repo).resolve()) if repo else st.repo
    if not st.started_at:
        st.started_at = now_iso()
    st.ended_at = None
    save(st)
    return st


def mark_ended(session_id: str) -> None:
    st = load(session_id)
    st.ended_at = now_iso()
    save(st)


def _iter_sessions() -> List[SessionState]:
    root = paths.sessions_root()
    out: List[SessionState] = []
    if not root.is_dir():
        return out
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        candidate = directory / "state.json"
        if not candidate.is_file():
            continue
        try:
            raw = paths.read_json(candidate)
        except Exception:
            continue
        if isinstance(raw, dict) and raw.get("session_id"):
            try:
                out.append(SessionState.from_dict(raw))
            except TypeError:
                continue
    return out


def _is_stale(st: SessionState) -> bool:
    path = state_path(st.session_id)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True
    return age > STALE_AFTER_SECONDS


def active_sessions(repo: Path) -> List[SessionState]:
    """Sessions registered against ``repo`` that have not ended or gone stale."""
    target = str(Path(repo).resolve())
    found = []
    for st in _iter_sessions():
        if st.repo != target or st.ended_at or st.gate_override == "off":
            continue
        if _is_stale(st):
            continue
        found.append(st)
    return sorted(found, key=lambda s: s.session_id)


def resolve_session(explicit: Optional[str], repo: Optional[Path],
                    env: Optional[Dict[str, str]] = None,
                    required: bool = True) -> str:
    """Determine the session deterministically, or refuse.

    1. ``--session`` when given
    2. ``CRUX_SESSION_ID`` when set (also explicit, just from the environment)
    3. exactly one active session registered against this repository
    4. none            -> error naming what to do
    5. several         -> ambiguity error listing them; never an arbitrary pick

    ``default`` is never reached implicitly. It survives only for deliberate
    out-of-Claude use, via an explicit ``--session default``.
    """
    if explicit:
        return explicit
    env = dict(os.environ if env is None else env)
    from_env = (env.get("CRUX_SESSION_ID") or "").strip()
    if from_env:
        return from_env

    if repo is None:
        if not required:
            return "default"
        raise SessionResolutionError(
            "aucune session déterminable hors d'un dépôt git.\n"
            "  → lancez la commande depuis le dépôt, ou passez --session <id>")

    candidates = active_sessions(repo)
    if len(candidates) == 1:
        return candidates[0].session_id
    if not candidates:
        if not required:
            return "default"
        raise SessionResolutionError(
            f"aucune session Crux active pour {repo}.\n"
            "  Une session est enregistrée au démarrage de Claude Code par le "
            "hook SessionStart.\n"
            "  → lancez `claude-review` dans ce dépôt, ou passez --session <id>")
    listing = "\n".join(f"    {s.session_id}" for s in candidates)
    raise SessionResolutionError(
        f"{len(candidates)} sessions Crux actives pour {repo} :\n{listing}\n"
        "  → précisez laquelle avec --session <id>",
        candidates=[s.session_id for s in candidates])
