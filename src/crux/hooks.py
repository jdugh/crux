"""Hook handlers. Every one of them starts with the arming test.

Two invariants govern this module and they do not conflict, because they act on
different failure surfaces:

  I1  fail-open on technical failure.  Codex down, config broken, a bug in Crux:
      log it and exit 0.  A Claude session is never wedged by Crux.

  I2  fail-closed on an established human decision.  A `pending_human` decision
      holds the turn open.  Enforcing it is a local file read - no network, no
      Codex, no model - so it cannot fail in the way I1 protects against.

No handler ever calls Codex.  The Stop handler decides in milliseconds and hands
execution back to Claude.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

from . import (baseline, config, decisions, findings, intent, paths, state)

MAX_PROMPT_CHARS = 20000


def _log(message: str) -> None:
    """Append one diagnostic line.

    Creates the directory first: on a fresh install ``~/.crux`` may not exist,
    and the very first failure is exactly the one worth keeping.
    """
    try:
        log = paths.log_path()
        paths.ensure_dir(log.parent)
        with open(log, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{state.now_iso()} {message}\n")
    except OSError:
        pass


def read_payload() -> Dict[str, Any]:
    """Read the hook payload as UTF-8, always, whatever the console codepage.

    Claude Code sends UTF-8. Reading through ``sys.stdin`` uses the locale
    encoding, which on a French Windows console is cp1252: an answer containing
    "é" or "—" came back mojibake and was persisted that way into the ledgers.
    Decoding the raw bytes ourselves removes the platform from the equation.
    """
    try:
        buffer = getattr(sys.stdin, "buffer", None)
        raw = buffer.read() if buffer is not None else sys.stdin.read().encode(
            "utf-8", "surrogateescape")
    except (OSError, ValueError, AttributeError):
        return {}
    if not raw.strip():
        return {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Never lose a payload over one bad byte; the rest is still useful.
        text = raw.decode("utf-8", "replace")
        _log("charge utile non strictement UTF-8 : octets remplacés")
    try:
        payload = json.loads(text)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _emit(payload: Dict[str, Any]) -> None:
    """Write the hook response as UTF-8 bytes, bypassing the console codepage."""
    data = json.dumps(payload, ensure_ascii=False) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data.encode("utf-8"))
        buffer.flush()
    else:
        sys.stdout.write(data)
        sys.stdout.flush()


def _session_id(payload: Dict[str, Any]) -> Optional[str]:
    value = payload.get("session_id")
    return str(value) if value else None


def kill_switch_engaged() -> bool:
    """``CRUX_DISABLE=1`` - the total kill switch.

    Three distinct things, deliberately not conflated:

    * ``crux off`` / ``/crux:off`` disarms *the reviews*. An already open human
      decision keeps blocking: disarming automation is not a way out of a
      decision the human already owns.
    * ``CRUX_DISABLE=1`` disables *Crux entirely*, the decision gate included.
      It is the escape hatch for when Crux itself misbehaves - without it, a bug
      that wrongly opened a decision would wedge the session for good, which is
      exactly what invariant I1 exists to prevent. It is **inert, not
      destructive**: the decision stays ``pending_human`` in the ledger and
      blocks again the moment the variable is removed.
    * Only a real human answer *resolves* a decision.
    """
    return (os.environ.get("CRUX_DISABLE") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _cwd(payload: Dict[str, Any]) -> Path:
    value = payload.get("cwd")
    return Path(value) if value else Path.cwd()


def _resolve(payload: Dict[str, Any]):
    """(config, gate, repo). Returns gate 'off' on any configuration problem."""
    cwd = _cwd(payload)
    try:
        cfg = config.load(cwd)
    except Exception as exc:
        _log(f"config invalide, gate non armé: {exc}")
        return None, state.GateDecision("off", "configuration invalide"), None
    gate = state.resolve_gate(cfg, _session_id(payload))
    from . import gitctx
    repo = None
    try:
        if gitctx.is_repo(cwd):
            repo = gitctx.repo_root(cwd)
    except Exception as exc:
        _log(f"résolution du dépôt impossible depuis {cwd}: {exc}")
        repo = None
    if repo is None and gate.armed:
        # Armed but nowhere to work: stay inert (I1), but say so, otherwise this
        # is indistinguishable from "not armed" when diagnosing.
        _log(f"gate armé ({gate.mode}, {gate.source}) mais {cwd} n'est pas "
             f"résolu comme un dépôt git — handler inerte")
    return cfg, gate, repo


# Events whose stdout is a structured hook payload, so a diagnostic can be
# surfaced there. UserPromptSubmit and SessionStart are excluded on purpose:
# their plain stdout is injected into Claude's context.
_CAN_REPORT = {"stop": "Stop", "post_edit": "PostToolUse", "post_ask": "PostToolUse"}


def _guard(handler):
    """I1: any unexpected error is logged and swallowed, exit 0.

    Visible, though. A silent swallow makes a broken handler indistinguishable
    from an inert one - which is exactly how a missing import once turned the
    decision gate off without anyone noticing. Where stdout is a structured hook
    payload, the failure is surfaced as a systemMessage; it still never blocks.
    """
    def wrapper() -> int:
        try:
            return handler()
        except SystemExit:
            raise
        except BaseException as exc:  # noqa: BLE001 - deliberately total
            _log(f"erreur non gérée dans {handler.__name__}: {exc!r}\n"
                 f"{traceback.format_exc()}")
            event = _CAN_REPORT.get(handler.__name__)
            if event:
                try:
                    _emit({"hookSpecificOutput": {
                        "hookEventName": event,
                        "systemMessage": (
                            f"Crux : erreur interne dans le hook "
                            f"{handler.__name__} ({type(exc).__name__}). "
                            f"Rien n'est bloqué ; détails dans "
                            f"{paths.log_path()}."),
                    }})
                except Exception:
                    pass
            return 0
    wrapper.__name__ = handler.__name__
    return wrapper


# ------------------------------------------------------------ session-start ---
@_guard
def session_start() -> int:
    payload = read_payload()
    cfg, gate, repo = _resolve(payload)
    session_id = _session_id(payload)

    # Register session -> repo whether or not the gate is armed. This is the one
    # thing an unarmed session leaves behind: a few bytes inside Crux's own
    # directory, no output, nothing in the project. Without it, a command run
    # from inside a session has no way to know which session it belongs to, and
    # `crux decision propose` once wrote D1 into a shared `default` session while
    # the hooks enforced a different one.
    if session_id and repo is not None:
        try:
            state.register(session_id, repo)
        except Exception as exc:
            _log(f"enregistrement de session impossible: {exc}")

    if not gate.armed or repo is None or cfg is None:
        return 0

    # The banner is a claim, and it must be earned. Without a session id there
    # is nothing to attach a baseline to; without a baseline the session diff has
    # no reference point and a later review would compare against nothing. Saying
    # "Crux est armé" in either case describes a protection that does not exist,
    # which is worse than saying nothing: it is the one failure mode nobody
    # checks for. Stay fail-open - never block, never write - but stay quiet, and
    # leave a diagnosable trace.
    if not session_id:
        _log("SessionStart sans session_id exploitable (charge utile illisible "
             "ou incomplète) : aucune baseline capturée, bannière supprimée")
        return 0

    try:
        manifest = baseline.capture(session_id, repo, cfg)
        st = state.load(session_id)
        st.repo = str(repo)
        st.head = manifest.head
        st.branch = manifest.branch
        st.baseline_captured = True
        if not st.armed_at:
            st.armed_at = state.now_iso()
        state.save(st)
    except Exception as exc:
        _log(f"capture de baseline impossible pour {session_id}: {exc!r} — "
             f"bannière supprimée, la session n'est pas réellement protégée")
        return 0

    _emit({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": (
            "Crux est armé pour cette session (gate: "
            f"{gate.mode}, source: {gate.source}).\n"
            "Contrat d'autorité : tu décides du technique à l'intérieur du "
            "périmètre demandé. Tout ajout, retrait ou changement de "
            "comportement fonctionnel non demandé n'est PAS ta décision : ouvre "
            "`crux decision propose`, puis pose la question avec "
            "`AskUserQuestion`. Aucune commande crux ne peut clore une décision "
            "humaine.\n"
            "En fin de tour, un hook te demandera de lancer `crux review`."),
    }})
    return 0


# ------------------------------------------------------------- user-prompt ---
@_guard
def user_prompt() -> int:
    """Silent journalling of the human's prompt into the approved scope.

    Nothing is written to stdout: UserPromptSubmit is one of the few events where
    plain stdout is injected into Claude's context, and this hook must leave the
    prompt Claude receives byte-for-byte unchanged.
    """
    payload = read_payload()
    cfg, gate, _repo = _resolve(payload)
    if not gate.armed:
        return 0
    session_id = _session_id(payload)
    text = payload.get("user_input") or payload.get("prompt") or ""
    if session_id and isinstance(text, str) and text.strip():
        intent.record_prompt(session_id, text[:MAX_PROMPT_CHARS])
    return 0          # deliberately no output at all


# --------------------------------------------------------------- post-edit ---
@_guard
def post_edit() -> int:
    payload = read_payload()
    cfg, gate, repo = _resolve(payload)
    if not gate.armed or repo is None:
        return 0
    session_id = _session_id(payload)
    if not session_id:
        return 0

    tool_input = payload.get("tool_input") or {}
    raw_path = (tool_input.get("file_path") or tool_input.get("path")
                or tool_input.get("notebook_path"))
    if not raw_path:
        return 0
    try:
        relpath = Path(raw_path).resolve().relative_to(repo.resolve()).as_posix()
    except (ValueError, OSError):
        return 0

    digest = None
    candidate = repo / relpath
    try:
        if candidate.is_file():
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        digest = None
    state.record_edit(session_id, relpath, digest)
    return 0


# ---------------------------------------------------------------- post-ask ---
@_guard
def post_ask() -> int:
    """The sole writer of approved_by_human / rejected_by_human.

    Its verdict comes from the real tool_response of AskUserQuestion, relayed by
    Claude Code - not from an argument a caller could choose.
    """
    payload = read_payload()
    cfg, gate, _repo = _resolve(payload)
    if not gate.armed:
        return 0
    session_id = _session_id(payload)
    if not session_id:
        return 0

    try:
        resolved, unclear = decisions.resolve_from_hook(session_id, payload)
    except decisions.ProvenanceError as exc:
        _log(f"provenance refusée: {exc}")
        return 0
    except decisions.DecisionsCorrupt as exc:
        _log(f"registre de décisions illisible: {exc}")
        return 0

    messages = []
    if resolved:
        summary = "; ".join(f"{d.id} → {d.status}" for d in resolved)
        approved = [d.id for d in resolved if d.status == decisions.APPROVED]
        rejected = [d.id for d in resolved if d.status == decisions.REJECTED]
        parts = [f"Décision(s) tranchée(s) par l'humain : {summary}."]
        if approved:
            parts.append(
                f"{', '.join(approved)} : l'évolution est ACCEPTÉE et rejoint "
                "le périmètre approuvé.")
        if rejected:
            parts.append(
                f"{', '.join(rejected)} : l'évolution est REFUSÉE. Reviens au "
                "périmètre demandé — ce changement ne fait pas partie du "
                "périmètre approuvé.")
        messages.append(" ".join(parts))
    if unclear:
        ids = ", ".join(d.id for d in unclear)
        messages.append(
            f"Réponse libre enregistrée pour {ids}, mais elle ne correspond à "
            "aucune option structurée : le statut reste `pending_human`. Crux "
            "ne devine jamais un accord. Repose la question avec "
            "`AskUserQuestion` en reprenant EXACTEMENT les options rendues par "
            f"`crux decision show --id {unclear[0].id}`, sans en inventer.")
    if messages:
        _emit({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": " ".join(messages),
        }})
    return 0


# -------------------------------------------------------------------- stop ---
STOP_INSTRUCTIONS = (
    "Crux — le gate de review est armé et ce tour a produit des modifications.\n\n"
    "1. Lance : `crux review --session {session} --intent \"<résumé en une "
    "phrase de ce que l'utilisateur a demandé>\"`\n"
    "2. Traite le rapport selon le contrat d'autorité :\n"
    "   · finding technique réel → corrige, puis `crux resolve --id <ID> "
    "--status accepted`\n"
    "   · faux positif → `crux resolve --id <ID> --status rejected --reason "
    "\"<raison technique>\"`\n"
    "   · vrai mais hors de la demande → `crux resolve --id <ID> --status "
    "deferred --reason \"...\"`\n"
    "   · ajout/retrait/changement fonctionnel → ce n'est PAS ta décision : "
    "`crux decision propose`, puis `AskUserQuestion`\n"
    "3. Reprends la main normalement ensuite."
)

FINDING_INSTRUCTIONS = (
    "Crux — {count} finding(s) technique(s) bloquant(s) sans arbitrage : "
    "{ids}.\n\n"
    "Ils ont déjà été rendus par un reviewer : aucune nouvelle review n'est "
    "nécessaire, et aucune n'est demandée ici.\n"
    "Tu restes l'autorité technique dans le périmètre approuvé — tu peux "
    "parfaitement les rejeter. Ce qui est refusé, c'est le silence.\n"
    "Pour chacun, vérifie dans le code, puis :\n"
    "  · réel → corrige, puis `crux resolve --id <ID> --status accepted`\n"
    "  · faux positif → `crux resolve --id <ID> --status rejected --reason "
    "\"<raison technique>\"`\n"
    "  · réel mais hors de la demande → `crux resolve --id <ID> --status "
    "deferred --reason \"...\"`\n"
    "Détail d'un finding : `crux report --run <dernier>` ou le rapport déjà "
    "affiché."
)

DECISION_INSTRUCTIONS = (
    "Crux — {count} décision(s) humaine(s) en attente : {ids}.\n\n"
    "Tu ne peux pas terminer ce tour et tu ne peux pas les trancher toi-même.\n"
    "Pour chacune, pose la question à l'utilisateur avec `AskUserQuestion`, en "
    "mettant `Scope <ID>` dans le champ `header` et en proposant les options "
    "listées par `crux decision show --id <ID>`.\n"
    "La réponse de l'utilisateur clôt la décision ; aucune commande crux ne le "
    "peut."
)


def _block(reason: str) -> int:
    _emit({"hookSpecificOutput": {
        "hookEventName": "Stop",
        "decision": "block",
        "reason": reason,
    }})
    return 0


@_guard
def stop() -> int:
    payload = read_payload()

    # The kill switch comes first and lifts everything, decision gate included.
    # See kill_switch_engaged() for why, and for what it deliberately does not do
    # (it never resolves or erases a decision).
    if kill_switch_engaged():
        return 0

    cfg, gate, repo = _resolve(payload)
    session_id = _session_id(payload)

    # ---- I2: an established human decision outranks the gate --------------
    # Checked before the arming test on purpose: disarming with /crux:off must
    # not be a way out of a decision that is already open.
    if session_id:
        try:
            pending = decisions.blocking(session_id)
        except decisions.DecisionsCorrupt as exc:
            return _block(
                "Crux — le registre des décisions humaines est présent mais "
                f"illisible.\n{exc}\n"
                "Par prudence, ce tour ne peut pas se terminer : un registre "
                "illisible n'est pas une raison de retirer son autorité à "
                "l'utilisateur. Signale-le-lui.")
        if pending:
            unasked = [d for d in pending if not d.asked]
            if unasked:
                return _block(DECISION_INSTRUCTIONS.format(
                    count=len(unasked),
                    ids=", ".join(d.id for d in unasked)))
            # Already asked: AskUserQuestion is synchronous, so the session is
            # waiting on the human. Nothing more for the gate to do.

    # ---- D4: a recorded blocking finding must be arbitrated ---------------
    # Placed after the human decision gate and before the arming test, for the
    # same reason the decision gate is: `crux off` disarms *new* reviews, it does
    # not silently retire an obligation that already exists. Deliberately ahead
    # of the round and budget checks too - exhausting the review budget says
    # nothing about a finding already on disk.
    #
    # No Codex, no network, no model: two local file reads. It therefore cannot
    # fail the way I1 protects against, and a read failure falls open anyway.
    # `CRUX_DISABLE=1` remains the absolute escape hatch, as it is for a wrongly
    # opened decision.
    if session_id:
        try:
            floor = str(cfg.get("gate.block_on", "high")) if cfg else "high"
            unarbitrated = findings.unarbitrated(session_id, floor)
        except Exception as exc:      # noqa: BLE001 - I1 over the ledger read
            _log(f"lecture des findings impossible, garde ignoré: {exc!r}")
            unarbitrated = []
        if unarbitrated:
            return _block(FINDING_INSTRUCTIONS.format(
                count=len(unarbitrated),
                ids=", ".join(f.id for f in unarbitrated[:12])))

    if not gate.armed or repo is None or cfg is None or not session_id:
        return 0
    if not gate.code:
        return 0

    if payload.get("stop_hook_active"):
        return 0

    st = state.load(session_id)
    max_rounds = int(cfg.get("gate.max_rounds", 2))
    if st.round >= max_rounds:
        return 0
    budget = float(cfg.get("gate.budget_seconds", 900))
    if st.budget_spent >= budget:
        _log(f"budget épuisé ({st.budget_spent:.0f}s >= {budget:.0f}s)")
        return 0

    try:
        diff = baseline.compute(session_id, repo, cfg)
    except Exception as exc:
        _log(f"diff de session impossible: {exc}")
        return 0

    if diff.is_empty:
        return 0
    if diff.fingerprint == st.last_successful_diff_fingerprint:
        # Nothing moved since the last usable review: another round finds nothing.
        return 0
    if (diff.fingerprint == st.last_attempt_fingerprint
            and st.last_attempt_status in state.ATTEMPT_NO_AUTO_RETRY):
        # Already tried on this exact diff and the reviewers could not answer -
        # out of quota, offline, timed out. Asking again would spin
        # Stop -> review -> failure -> Stop. Fail open: the turn may end. A new
        # edit moves the fingerprint, and `crux review` by hand always retries.
        _log(f"tentative précédente {st.last_attempt_status} sur la même "
             f"empreinte — pas de relance automatique")
        return 0

    return _block(STOP_INSTRUCTIONS.format(session=session_id))


@_guard
def session_end() -> int:
    """Mark the session ended so it stops competing in session resolution."""
    payload = read_payload()
    session_id = _session_id(payload)
    if session_id:
        try:
            state.mark_ended(session_id)
        except Exception as exc:
            _log(f"clôture de session impossible: {exc}")
    return 0


HANDLERS = {
    "session-start": session_start,
    "session-end": session_end,
    "user-prompt": user_prompt,
    "post-edit": post_edit,
    "post-ask": post_ask,
    "stop": stop,
}


def dispatch(event: str) -> int:
    handler = HANDLERS.get(event)
    if handler is None:
        _log(f"événement de hook inconnu: {event}")
        return 0
    return handler()
