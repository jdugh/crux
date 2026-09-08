"""Human decisions: a ledger separate from technical findings, and the only
place a functional change can be settled.

Why a separate ledger: a finding and a decision have neither the same authority,
the same lifecycle, nor the same effect.  Merging them would let Claude close a
functional question by writing ``rejected: "I don't think it's needed"``.  Here a
finding that needs a human is *promoted* out of the findings ledger, and
``crux resolve`` refuses to touch it.

Provenance.  There is no ``--by human`` flag.  ``approved_by_human`` and
``rejected_by_human`` are written by exactly one function, ``resolve_from_hook``,
whose input is the real ``tool_response`` of an ``AskUserQuestion`` call relayed by
Claude Code.  See ``ARCHITECTURE.md`` §7 for the four properties that close the
bypass, and for the limit under ``bypassPermissions``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import intent, paths, state

# ---------------------------------------------------------------- statuses ---
PENDING = "pending_human"
APPROVED = "approved_by_human"
REJECTED = "rejected_by_human"
NOTED = "noted"                # scope_changes.mode == warn
AUTO_APPROVED = "auto_approved"  # scope_changes.mode == auto
WITHDRAWN = "withdrawn"        # Claude: the decision became moot

BLOCKING = {PENDING}
HUMAN_STATUSES = {APPROVED, REJECTED}
IN_SCOPE_AFTER = {APPROVED, AUTO_APPROVED}

KIND_SCOPE = "scope"
KIND_ESCALATED = "technical_escalated"

# ------------------------------------------------------------- option actions ---
# The verdict is carried by the option's *action*, never by its wording. Crux
# once derived it from text - the canonical reject label, plus a list of refusal
# phrases - and it was wrong twice over:
#
#   * every entry in `alternatives` counted as an approval, so a reviewer's
#     remediation ("Restaurer heic dans SUPPORTED"), which *rejects* the drift,
#     was recorded as approving it and joined the approved scope;
#   * two semantically identical answers got opposite statuses depending on
#     whether their exact string was in the list.
#
# So Crux now always emits its own two structured options and matches on the
# action attached to the chosen label. No phrase is ever interpreted.
ACTION_APPROVE = "approve_scope_change"
ACTION_REJECT = "reject_scope_change"
ACTION_CUSTOM = "custom"

APPROVE_LABEL = "Accepter cette évolution — elle rejoint le périmètre approuvé"
REJECT_LABEL = "Refuser — revenir au périmètre demandé"

# Kept only so ledgers written before the structured options still read back.
LEGACY_REJECT_LABEL = "Ne rien changer — rester dans le périmètre demandé"

_ID_RE = re.compile(r"\bD(\d+)\b")


class DecisionsCorrupt(Exception):
    """The ledger exists but cannot be read.

    This is the one case where Crux fails *closed* (invariant I2): the file being
    there means something was in it, and an unreadable decision ledger is not a
    reason to take authority away from the human.
    """


class ProvenanceError(Exception):
    """An attempt to write a human status through a path that cannot prove it."""


# ------------------------------------------------------------------ record ---
@dataclass
class Decision:
    id: str
    kind: str = KIND_SCOPE
    status: str = PENDING
    origin: str = "claude"
    round: int = 0
    opened_at: str = ""
    title: str = ""
    why: str = ""
    alternatives: List[str] = field(default_factory=list)   # reviewer hints
    options: List[Dict[str, str]] = field(default_factory=list)
    clarifications: int = 0
    blast_radius: List[str] = field(default_factory=list)
    from_findings: List[str] = field(default_factory=list)
    # Content identity of the finding this decision came from (`finding-key-v1`).
    # Empty when the decision did not come from a finding - a `scope.changes[]`
    # entry carries no title, and inventing an identity out of its free-text
    # description could make two genuinely different drifts look like one. An
    # empty key means "identity unknown", and unknown must never suppress a
    # question: a duplicate question is cheap, a swallowed one is not.
    finding_key: str = ""
    question: Optional[Dict[str, Any]] = None
    answer: Optional[Dict[str, Any]] = None
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "origin": self.origin, "round": self.round,
            "opened_at": self.opened_at, "title": self.title, "why": self.why,
            "alternatives": self.alternatives, "options": self.options,
            "clarifications": self.clarifications,
            "blast_radius": self.blast_radius,
            "from_findings": self.from_findings,
            "finding_key": self.finding_key, "question": self.question,
            "answer": self.answer, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Decision":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})

    @property
    def is_blocking(self) -> bool:
        return self.status in BLOCKING

    @property
    def asked(self) -> bool:
        return bool(self.question)

    @property
    def degraded_provenance(self) -> bool:
        evidence = (self.answer or {}).get("evidence") or {}
        return evidence.get("permission_mode") in (
            "bypassPermissions", "dontAsk", "acceptEdits")

    def structured_options(self) -> List[Dict[str, str]]:
        """The options actually offered, each carrying its own action.

        Always exactly the two canonical outcomes, plus any option a caller
        tagged explicitly. A reviewer's free-text suggestions are never options:
        their action is unknown, and guessing it is the bug this replaces. They
        travel in the question body instead (see ``question_payload``).
        """
        options: List[Dict[str, str]] = []
        seen = set()
        for option in self.options or []:
            label = str(option.get("label", "")).strip()
            action = option.get("action")
            if not label or action not in (ACTION_APPROVE, ACTION_REJECT):
                continue
            if label not in seen:
                seen.add(label)
                options.append({"label": label, "action": action})
        for label, action in ((APPROVE_LABEL, ACTION_APPROVE),
                              (REJECT_LABEL, ACTION_REJECT)):
            if not any(o["action"] == action for o in options):
                options.append({"label": label, "action": action})
                seen.add(label)
        return options

    def option_labels(self) -> List[str]:
        return [o["label"] for o in self.structured_options()]

    def action_for(self, label: str) -> Optional[str]:
        """The action of an offered label, or None when nothing matched.

        Exact string comparison against labels Crux itself emitted. Accents,
        dashes and emoji are irrelevant: they are compared, never parsed.
        """
        chosen = (label or "").strip()
        for option in self.structured_options():
            if option["label"] == chosen:
                return option["action"]
        if chosen == LEGACY_REJECT_LABEL:
            return ACTION_REJECT
        return None


KIND_LABELS = {
    "added_feature": "Fonctionnalité ajoutée",
    "removed_capability": "Capacité retirée",
    "behavior_change": "Comportement modifié",
    "ux_change": "Expérience utilisateur modifiée",
    "api_change": "API publique modifiée",
    "data_format_change": "Format de données modifié",
    "incompatibility": "Incompatibilité introduite",
    "product_decision": "Décision produit",
}


def make_title(kind: str, description: str, limit: int = 110) -> str:
    """A readable one-line title: the kind, then a clause that ends on a word.

    Slicing a description at a fixed width produced titles cut mid-sentence
    ("...échouer même avec le"), which read as broken rather than shortened.
    """
    label = KIND_LABELS.get(kind, "Changement de périmètre")
    text = " ".join((description or "").split())
    room = limit - len(label) - 3
    if room <= 0:
        return label
    if len(text) <= room:
        return f"{label} — {text}" if text else label
    cut = text[:room]
    for stop in (". ", " ; ", ", ", " "):
        index = cut.rfind(stop)
        if index > room // 2:
            cut = cut[:index]
            break
    return f"{label} — {cut.rstrip(' ,;.')}…"


def ledger_path(session_id: str) -> Path:
    return paths.session_dir(session_id) / "decisions.jsonl"


def _read_records(session_id: str) -> List[Dict[str, Any]]:
    path = ledger_path(session_id)
    if not path.is_file():
        return []
    records: List[Dict[str, Any]] = []
    good = 0
    bad = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(record, dict) and record.get("id"):
                    records.append(record)
                    good += 1
                else:
                    bad += 1
    except OSError as exc:
        raise DecisionsCorrupt(
            f"registre de décisions illisible ({exc.strerror}) : "
            f"{path}") from exc
    if bad and not good:
        raise DecisionsCorrupt(
            f"registre de décisions présent mais illisible : {path}")
    return records


def load_all(session_id: str) -> List[Decision]:
    """Latest version of every decision, in creation order."""
    latest: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for record in _read_records(session_id):
        did = record["id"]
        if did not in latest:
            order.append(did)
        latest[did] = record
    return [Decision.from_dict(latest[d]) for d in order]


def get(session_id: str, decision_id: str) -> Optional[Decision]:
    for decision in load_all(session_id):
        if decision.id == decision_id:
            return decision
    return None


def open_decisions(session_id: str) -> List[Decision]:
    return [d for d in load_all(session_id) if d.is_blocking]


def blocking(session_id: str) -> List[Decision]:
    """Decisions that must stop the turn from ending (invariant I2)."""
    return open_decisions(session_id)


def unanswered(session_id: str) -> List[Decision]:
    return [d for d in open_decisions(session_id) if not d.asked]


def approved_scope_entries(session_id: str) -> List[Decision]:
    return [d for d in load_all(session_id) if d.status in IN_SCOPE_AFTER]


def blast_radius(session_id: str) -> List[str]:
    out: List[str] = []
    for decision in open_decisions(session_id):
        out.extend(decision.blast_radius)
    return out


def _write(session_id: str, decision: Decision) -> Decision:
    decision.updated_at = state.now_iso()
    paths.append_jsonl(ledger_path(session_id), decision.to_dict())
    return decision


def _next_id(session_id: str) -> str:
    existing = load_all(session_id)
    return f"D{len(existing) + 1}"


def by_finding_key(session_id: str) -> Dict[str, "Decision"]:
    """``finding_key`` -> the single decision opened from it. Ambiguous dropped.

    What it is for: a reviewer restating, at round 2, the very remark that
    already became a human decision at round 1. Re-proposing it would ask the
    human the same question twice, and a second pending decision for a question
    already answered is worse than noise - it reads as a new obligation.

    Two safety rules, both erring the same way:

    * a key carried by more than one decision is *absent* from the index, so it
      never matches. A false equivalence here would silently discard a genuine
      new question; a duplicate merely repeats one. Duplication wins.
    * ``withdrawn`` decisions are excluded. Claude retires a decision when it
      becomes moot; a remark that comes back is not moot, and must be askable
      again.

    Keyless decisions - anything opened from a ``scope`` block rather than from a
    finding - are not indexed: they have no finding identity to match on.
    """
    counts: Dict[str, int] = {}
    found: Dict[str, "Decision"] = {}
    for decision in load_all(session_id):
        key = decision.finding_key or ""
        if not key or decision.status == WITHDRAWN:
            continue
        counts[key] = counts.get(key, 0) + 1
        found.setdefault(key, decision)
    return {key: value for key, value in found.items() if counts.get(key) == 1}


def propose(session_id: str, title: str, why: str,
            alternatives: Optional[List[str]] = None,
            approve_options: Optional[List[str]] = None,
            reject_options: Optional[List[str]] = None,
            blast_radius_paths: Optional[List[str]] = None,
            kind: str = KIND_SCOPE, origin: str = "claude",
            from_findings: Optional[List[str]] = None,
            finding_key: str = "",
            round_no: int = 0, mode: str = "ask") -> Decision:
    """Open a decision. ``mode`` is ``human.scope_changes.mode``.

    ``ask`` opens it blocking, ``warn`` records it without blocking, ``auto``
    records that Claude settled it.  Only ``ask`` yields ``pending_human``.
    """
    status = {"ask": PENDING, "warn": NOTED, "auto": AUTO_APPROVED}.get(mode, PENDING)
    decision = Decision(
        id=_next_id(session_id),
        kind=kind,
        status=status,
        origin=origin,
        round=round_no,
        opened_at=state.now_iso(),
        title=title.strip(),
        why=why.strip(),
        alternatives=[a.strip() for a in (alternatives or []) if a.strip()],
        options=([{"label": a.strip(), "action": ACTION_APPROVE}
                  for a in (approve_options or []) if a.strip()]
                 + [{"label": r.strip(), "action": ACTION_REJECT}
                    for r in (reject_options or []) if r.strip()]),
        blast_radius=[p.replace("\\", "/") for p in (blast_radius_paths or [])],
        from_findings=list(from_findings or []),
        finding_key=finding_key or "",
    )
    _write(session_id, decision)
    if status != PENDING:
        intent.record_decision(session_id, decision.id, status, decision.title)
    return decision


def withdraw(session_id: str, decision_id: str, reason: str) -> Decision:
    """Claude may retire a decision that became moot. It stays in the ledger."""
    decision = get(session_id, decision_id)
    if decision is None:
        raise KeyError(decision_id)
    if decision.status != PENDING:
        raise ProvenanceError(
            f"{decision_id} n'est pas en attente (statut: {decision.status})")
    if not reason.strip():
        raise ValueError("une justification est requise pour retirer une décision")
    decision.status = WITHDRAWN
    decision.answer = {"by": "claude", "reason": reason.strip(),
                       "at": state.now_iso()}
    return _write(session_id, decision)


def mark_asked(session_id: str, decision_id: str, tool_use_id: str) -> None:
    decision = get(session_id, decision_id)
    if decision is None:
        return
    decision.question = {"tool_use_id": tool_use_id, "asked_at": state.now_iso()}
    _write(session_id, decision)


# ------------------------------------------------------------- provenance ---
def consumed_tool_use_ids(session_id: str) -> set:
    """Every tool_use_id that has already settled a decision (replay guard)."""
    used = set()
    for decision in load_all(session_id):
        evidence = (decision.answer or {}).get("evidence") or {}
        tool_use_id = evidence.get("tool_use_id")
        if tool_use_id:
            used.add(tool_use_id)
    return used


def decision_ids_in(text: str) -> List[str]:
    """Exact `D<n>` tokens. Matching an identifier we ourselves minted is an
    exact match, not a textual heuristic."""
    return [f"D{m.group(1)}" for m in _ID_RE.finditer(text or "")]


def _coerce_answers(tool_input: Any, tool_response: Any) -> List[Dict[str, str]]:
    """Pull (question, header, chosen, notes) out of an AskUserQuestion result.

    Defensive on purpose: the exact response shape is not something Crux should
    depend on, so several plausible shapes are accepted and anything unusable is
    reported rather than guessed at.
    """
    questions: List[Dict[str, Any]] = []
    if isinstance(tool_input, dict):
        raw_questions = tool_input.get("questions")
        if isinstance(raw_questions, list):
            questions = [q for q in raw_questions if isinstance(q, dict)]

    answers_map: Dict[str, Any] = {}
    notes_map: Dict[str, Any] = {}
    payloads = [tool_response]
    if isinstance(tool_response, dict):
        for key in ("answers", "response", "result", "output"):
            if key in tool_response:
                payloads.append(tool_response[key])
        annotations = tool_response.get("annotations")
        if isinstance(annotations, dict):
            for question, meta in annotations.items():
                if isinstance(meta, dict) and meta.get("notes"):
                    notes_map[str(question)] = str(meta["notes"])

    for payload in payloads:
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, (str, int, float)):
                    answers_map.setdefault(str(key), str(value))
                elif isinstance(value, list):
                    answers_map.setdefault(
                        str(key), ", ".join(str(v) for v in value))
        elif isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                question = item.get("question") or item.get("header") or ""
                chosen = (item.get("answer") or item.get("chosen")
                          or item.get("selected") or item.get("value") or "")
                if question:
                    answers_map.setdefault(str(question), str(chosen))
                if item.get("notes"):
                    notes_map[str(question)] = str(item["notes"])

    results: List[Dict[str, str]] = []
    if questions:
        for question in questions:
            text = str(question.get("question", "")).strip()
            header = str(question.get("header", "")).strip()
            chosen = ""
            for key in (text, header):
                if key and key in answers_map:
                    chosen = str(answers_map[key])
                    break
            if not chosen and len(answers_map) == 1 and len(questions) == 1:
                chosen = str(next(iter(answers_map.values())))
            results.append({
                "question": text, "header": header, "chosen": chosen,
                "notes": notes_map.get(text, notes_map.get(header, "")),
            })
    else:
        for key, value in answers_map.items():
            results.append({"question": key, "header": "", "chosen": str(value),
                            "notes": notes_map.get(key, "")})
    return results


def resolve_from_hook(session_id: str,
                      payload: Dict[str, Any]) -> Tuple[List[Decision],
                                                        List[Decision]]:
    """The one and only writer of approved_by_human / rejected_by_human.

    Its verdict is derived from the real tool_response of AskUserQuestion, not
    from an argument any caller could choose.
    """
    if payload.get("hook_event_name") != "PostToolUse":
        raise ProvenanceError(
            "provenance refusée : charge utile hors PostToolUse")
    if payload.get("tool_name") != "AskUserQuestion":
        raise ProvenanceError(
            "provenance refusée : l'outil n'est pas AskUserQuestion")

    tool_use_id = str(payload.get("tool_use_id") or "").strip()
    if not tool_use_id:
        raise ProvenanceError("provenance refusée : tool_use_id absent")
    if tool_use_id in consumed_tool_use_ids(session_id):
        raise ProvenanceError(
            f"provenance refusée : tool_use_id déjà consommé ({tool_use_id})")

    tool_input = payload.get("tool_input")
    answers = _coerce_answers(tool_input, payload.get("tool_response"))
    if not answers:
        return [], []

    permission_mode = str(payload.get("permission_mode") or "unknown")
    open_ids = {d.id for d in open_decisions(session_id)}
    resolved: List[Decision] = []
    needs_clarification: List[Decision] = []

    for answer in answers:
        # record the human's words in the approved scope, always
        candidates = decision_ids_in(answer.get("header", "")) or \
            decision_ids_in(answer.get("question", ""))
        target = next((cid for cid in candidates if cid in open_ids), None)

        intent.record_answer(
            session_id,
            question=answer.get("question", ""),
            chosen=answer.get("chosen", ""),
            notes=answer.get("notes", ""),
            decision=target,
            header=answer.get("header", ""),
        )
        if target is None:
            continue

        decision = get(session_id, target)
        if decision is None or not decision.is_blocking:
            continue

        chosen = (answer.get("chosen") or "").strip()
        action = decision.action_for(chosen)

        if action is None:
            # Free text, or "Other". Crux does not guess: an unrecognised answer
            # never becomes an approval, because an approval widens the approved
            # scope for every later review. The decision stays open and Claude is
            # told to re-ask with the structured options only.
            decision.clarifications += 1
            decision.question = None            # it must be asked again
            decision.answer = {
                "chosen": chosen,
                "notes": answer.get("notes", ""),
                "evidence": {
                    "hook_event": "PostToolUse",
                    "tool_name": "AskUserQuestion",
                    "tool_use_id": tool_use_id,
                    "session_id": str(payload.get("session_id") or session_id),
                    "permission_mode": permission_mode,
                    "matched_option": False,
                    "action": ACTION_CUSTOM,
                    "received_at": state.now_iso(),
                },
            }
            _write(session_id, decision)
            needs_clarification.append(decision)
            continue

        decision.status = APPROVED if action == ACTION_APPROVE else REJECTED
        decision.answer = {
            "chosen": chosen,
            "notes": answer.get("notes", ""),
            "evidence": {
                "hook_event": "PostToolUse",
                "tool_name": "AskUserQuestion",
                "tool_use_id": tool_use_id,
                "session_id": str(payload.get("session_id") or session_id),
                "permission_mode": permission_mode,
                "matched_option": True,
                "action": action,
                "received_at": state.now_iso(),
            },
        }
        _write(session_id, decision)
        intent.record_decision(session_id, decision.id, decision.status,
                               decision.title)
        open_ids.discard(target)
        resolved.append(decision)
    return resolved, needs_clarification


def resolve_manually(session_id: str, decision_id: str, approve: bool,
                     answer_text: str, confirm_id: str,
                     stdin_is_tty: bool, stdout_is_tty: bool) -> Decision:
    """Out-of-session fallback, for the human at a real terminal.

    The TTY requirement is the point: an agent's Bash tool captures its streams
    and has no terminal, so this path is closed to it.  A TTY is not proof of
    humanity - it is proof the call did not come from a captured stream, which is
    what is needed here.  A `deny` rule in the arming file closes it a second time.
    """
    if not (stdin_is_tty and stdout_is_tty):
        raise ProvenanceError(
            "refusé : `crux decision resolve` exige un terminal interactif. "
            "Une décision humaine ne peut pas être clôturée depuis un flux "
            "capturé. Répondez à la question dans Claude Code, ou lancez cette "
            "commande vous-même dans un terminal.")
    if confirm_id != decision_id:
        raise ProvenanceError(
            "refusé : l'identifiant de confirmation ne correspond pas")

    decision = get(session_id, decision_id)
    if decision is None:
        raise KeyError(decision_id)
    if not decision.is_blocking:
        raise ProvenanceError(
            f"{decision_id} n'est pas en attente (statut: {decision.status})")

    decision.status = APPROVED if approve else REJECTED
    decision.answer = {
        "chosen": answer_text.strip(),
        "notes": "",
        "evidence": {
            "hook_event": "manual-cli",
            "tool_name": None,
            "tool_use_id": None,
            "session_id": session_id,
            "permission_mode": "interactive-tty",
            "matched_option": False,
            "action": ACTION_APPROVE if approve else ACTION_REJECT,
            "received_at": state.now_iso(),
        },
    }
    _write(session_id, decision)
    intent.record_decision(session_id, decision.id, decision.status,
                           decision.title)
    return decision


DESCRIPTIONS = {
    ACTION_APPROVE: "L'évolution est retenue et rejoint le périmètre approuvé.",
    ACTION_REJECT: "L'évolution est écartée ; le périmètre demandé est conservé.",
}


def question_payload(decision: Decision) -> Dict[str, Any]:
    """The exact AskUserQuestion shape Claude must use for this decision.

    Reviewer suggestions travel inside the question text, never as options: an
    option's action decides the verdict, and a suggestion carries no action Crux
    could know. Offering "Restaurer heic dans SUPPORTED" as an option is how a
    rejection once got recorded as an approval.
    """
    body = [decision.title, "", decision.why, ""]
    if decision.alternatives:
        body.append("Pistes proposées par le reviewer (à titre indicatif) :")
        body.extend(f"  · {hint}" for hint in decision.alternatives)
        body.append("")
    body.append("Cette évolution sort du périmètre demandé. Que décidez-vous ?")
    return {
        "header": f"Scope {decision.id}"[:12],
        "question": "\n".join(body).strip(),
        "multiSelect": False,
        "options": [{"label": option["label"],
                     "description": DESCRIPTIONS[option["action"]]}
                    for option in decision.structured_options()],
    }


def render_open(session_id: str) -> str:
    rows = open_decisions(session_id)
    if not rows:
        return "Aucune décision en attente."
    lines = []
    for decision in rows:
        lines.append(f"{decision.id}  [{decision.status}]  {decision.title}")
        lines.append(f"    pourquoi : {decision.why}")
        for alt in decision.alternatives:
            lines.append(f"    · {alt}")
        lines.append(f"    · {REJECT_LABEL}")
        if decision.blast_radius:
            lines.append(f"    fichiers : {', '.join(decision.blast_radius)}")
        lines.append(f"    posée : {'oui' if decision.asked else 'NON'}")
    return "\n".join(lines)
