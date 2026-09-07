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

# Outcome of one review attempt. A round and an attempt are different things:
# a round is an anchor a later round reasons against, an attempt is something
# that was tried. Conflating them let a quota outage burn half the round budget.
ATTEMPT_SUCCESS = "success"      # every selected reviewer answered
ATTEMPT_DEGRADED = "degraded"    # scope authority answered, a secondary did not
ATTEMPT_UNUSABLE = "unusable"    # the scope authority did not answer
ATTEMPT_FAILED = "failed"        # no reviewer answered at all

# Attempts that produced a usable round: only these advance `round`.
ATTEMPT_USABLE = (ATTEMPT_SUCCESS, ATTEMPT_DEGRADED)

# After one of these, the Stop hook must NOT ask for the same diff to be
# reviewed again. With Codex out of quota, re-asking would spin
# Stop -> review -> quota -> Stop, which is precisely the wedged session I1
# exists to prevent. A new edit changes the fingerprint and lifts the hold;
# `crux review` by hand always retries, because the CLI never consults this.
ATTEMPT_NO_AUTO_RETRY = (ATTEMPT_UNUSABLE, ATTEMPT_FAILED)


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
    # Kept under its v0.1 name and mirrored on write so a downgrade still reads
    # it; `last_successful_diff_fingerprint` is the one the code reasons with.
    last_diff_fingerprint: Optional[str] = None
    last_successful_diff_fingerprint: Optional[str] = None
    last_attempt_fingerprint: Optional[str] = None
    last_attempt_status: Optional[str] = None
    last_attempt_at: Optional[str] = None
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
            "last_successful_diff_fingerprint":
                self.last_successful_diff_fingerprint,
            "last_attempt_fingerprint": self.last_attempt_fingerprint,
            "last_attempt_status": self.last_attempt_status,
            "last_attempt_at": self.last_attempt_at,
            "budget_spent": self.budget_spent,
            "baseline_captured": self.baseline_captured,
            "last_run_id": self.last_run_id,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SessionState":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        state = cls(**{k: v for k, v in raw.items() if k in known})
        if not state.last_successful_diff_fingerprint:
            # A v0.1 state file: its single fingerprint was only ever written
            # after a round that produced findings, so it *is* the successful one.
            state.last_successful_diff_fingerprint = state.last_diff_fingerprint
        return state

    def record_attempt(self, fingerprint: str, status: str) -> None:
        """Note that a review was tried. Says nothing about whether it worked."""
        self.last_attempt_fingerprint = fingerprint
        self.last_attempt_status = status
        self.last_attempt_at = now_iso()

    def record_successful_round(self, round_no: int, fingerprint: str) -> None:
        """Advance the round. Only a usable attempt may call this."""
        self.round = round_no
        self.last_successful_diff_fingerprint = fingerprint
        self.last_diff_fingerprint = fingerprint


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
                 env: Optional[Dict[str, str]] = None,
                 session_state: Optional[SessionState] = None) -> GateDecision:
    """Resolve the gate. First source that answers wins.

    1. CRUX_DISABLE        absolute kill switch
    2. session override    /crux:on and /crux:off
    3. CRUX_GATE           what the claude-review wrapper sets
    4. project .crux.yml   gate.mode
    5. ~/.crux/config.yml  gate.mode
    6. built-in default    off

    ``session_state`` lets a caller that has already loaded the session hand it
    over instead of paying for a second read of the same file - and, more to the
    point, lets a handler work from a single in-memory state rather than
    re-reading it at each step. Omit it and the behaviour is exactly what it
    always was.
    """
    env = dict(os.environ if env is None else env)

    if (env.get("CRUX_DISABLE") or "").strip().lower() in _TRUTHY:
        return GateDecision("off", "CRUX_DISABLE")

    if session_id:
        st = session_state if session_state is not None else load(session_id)
        override = st.gate_override
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


def register_into(st: SessionState, repo: Optional[Path]) -> SessionState:
    """The registration itself, on an in-memory state. Does not write.

    Split out of ``register`` so SessionStart can apply it to the same object it
    later stores the baseline result on, and write once. Two read-modify-write
    cycles over one file in one hook are two windows in which a concurrent
    writer's change is read and then written back over; they also let the two
    cycles disagree, which is how ``st.repo`` ended up stored resolved by this
    function and unresolved by the caller that wrote after it.
    """
    st.repo = str(Path(repo).resolve()) if repo else st.repo
    if not st.started_at:
        st.started_at = now_iso()
    st.ended_at = None
    return st


def register(session_id: str, repo: Optional[Path]) -> SessionState:
    """Record that ``session_id`` belongs to ``repo``, and store it.

    Written on SessionStart whether or not the gate is armed. This is the only
    thing an unarmed session leaves behind: a few bytes inside Crux's own
    directory, no output, nothing in the project. It is what lets `/crux:on` and
    `crux decision propose` find the right session without being told.
    """
    st = register_into(load(session_id), repo)
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
