"""Which reviewers a second round would re-run, why, and against what delta.

This module *plans*. It never runs Codex, never opens, closes or touches a human
decision, never writes a round journal, and is not wired into ``review.run()``:
that is the next sub-milestone. Importing it must not pull in ``codex`` - the
working-tree hashing it needs lives in ``baseline``, which is why
``review.content_map`` is an alias there rather than a function here.

The plan answers four questions, and every answer is attributable:

``anchor_status``       may the previous round be used as a reference at all?
``delta_paths``         what has actually moved since that round?
``rows``                per reviewer: selected or not, under which trigger, or
                        excluded for which named reason.
``fallback``            when the anchor cannot be trusted, the v0.1 full router.

Two rules hold over the whole file:

* no reviewer is selected without a named trigger, and none is excluded without
  a named reason (``Round2Row`` cannot express either);
* the scope authority is present in every plan whose delta is non-empty, and no
  cap and no override may evict it.

Identity.  Findings are matched to resolutions and to human decisions by **id**
only (``R<n>F<m>``), never by ``finding_key``. A key can collide - see
``findings.FINDING_KEY_VERSION`` - and a collision that merged two remarks would
let one finding's arbitration silently settle another. ``safe_key_index`` exists
for the round-2 executor that will need cross-round matching: it drops ambiguous
keys outright, so an ambiguous key is always *unmatched*, never a reiteration and
never an excuse to skip a human decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Dict, Iterable, List, Mapping, Optional, Sequence,
                    Set, Tuple)

from . import baseline, decisions, findings, paths

# ------------------------------------------------------------ anchor status ---
ANCHORED = "anchored"                  # usable as the reference for a round 2
NO_PREVIOUS_ROUND = "no_previous_round"
NOT_ANCHORABLE = "not_anchorable"      # legacy journal: schema < 2
UNREADABLE = "unreadable"              # a round file exists and will not parse
INCONSISTENT = "inconsistent"          # journals disagree with the session state

# The only fallback there is: redo exactly what v0.1 does. 1c will consume it.
FALLBACK_FULL_ROUTER = "full_router"

# ---------------------------------------------------------------- triggers ---
# Ordered by how strong a claim each makes on a slot; the cap reads this order.
T_SCOPE_AUTHORITY = "scope_authority"   # non-negotiable while the delta is real
T_CONTESTED = "contested"               # an open obligation from the last round
T_FIX_VERIFICATION = "fix_verification"  # a fix was accepted, code has moved
T_NEW_SURFACE = "new_surface"           # the delta itself calls for a reviewer

TRIGGER_ORDER = (T_SCOPE_AUTHORITY, T_CONTESTED, T_FIX_VERIFICATION,
                 T_NEW_SURFACE)
TRIGGER_RANK = {name: i for i, name in enumerate(TRIGGER_ORDER)}

# ------------------------------------------------------- exclusion reasons ---
X_NEVER = "never"                       # reviewers.never
X_MAX_SELECTED = "max_selected"         # had triggers, lost to the cap
X_DEFERRED_ONLY = "deferred_only"       # deferred does not re-open a review
X_PENDING_HUMAN = "pending_human_only"  # the human gate owns these
X_HUMAN_SETTLED = "human_settled_only"  # already settled by a human
X_NO_TRIGGER = "no_trigger"

EXCLUSION_TEXT = {
    X_NEVER: "exclu par reviewers.never",
    X_MAX_SELECTED: "écarté par max_selected",
    X_DEFERRED_ONLY: "uniquement des findings deferred",
    X_PENDING_HUMAN: "uniquement des findings en décision humaine",
    X_HUMAN_SETTLED: "uniquement des findings tranchés par l'humain",
    X_NO_TRIGGER: "aucun déclencheur",
}

# --------------------------------------------------------- dispositions ---
# What became of one finding of the previous round. Exactly one applies.
D_ACCEPTED = "accepted"
D_REJECTED = "rejected"
D_DEFERRED = "deferred"
D_UNANSWERED = "unanswered"
D_PENDING_HUMAN = "pending_human"    # promoted, the human has not answered
D_HUMAN_SETTLED = "human_settled"    # promoted and closed, whatever the answer

# `rejected` and `unanswered` contest a reviewer. `deferred` deliberately does
# NOT: deferring is a disposition, not a dispute, and re-running a reviewer over
# something Claude explicitly parked would make `deferred` mean nothing. A
# deferred finding can still bring its reviewer back through another trigger.
CONTESTING = (D_REJECTED, D_UNANSWERED)


@dataclass
class Round2Row:
    """One reviewer's line. Selected with triggers, or excluded with a reason."""
    reviewer: str
    selected: bool = False
    triggers: List[str] = field(default_factory=list)
    exclusion_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"reviewer": self.reviewer, "selected": self.selected,
                "triggers": list(self.triggers),
                "exclusion_reason": self.exclusion_reason}


@dataclass
class Round2Plan:
    previous_round: Optional[int] = None
    anchor_status: str = NO_PREVIOUS_ROUND
    delta_paths: List[str] = field(default_factory=list)
    selected_reviewers: List[str] = field(default_factory=list)
    rows: List[Round2Row] = field(default_factory=list)
    fallback: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    # Which reviewer carries the scope verdict in this plan. None only when no
    # persona at all is available, which is itself a warning.
    scope_authority: Optional[str] = None

    @property
    def anchored(self) -> bool:
        return self.anchor_status == ANCHORED and self.fallback is None

    def row(self, reviewer: str) -> Optional[Round2Row]:
        for row in self.rows:
            if row.reviewer == reviewer:
                return row
        return None

    def triggers_for(self, reviewer: str) -> List[str]:
        row = self.row(reviewer)
        return list(row.triggers) if row else []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "previous_round": self.previous_round,
            "anchor_status": self.anchor_status,
            "delta_paths": list(self.delta_paths),
            "selected_reviewers": list(self.selected_reviewers),
            "rows": [r.to_dict() for r in self.rows],
            "fallback": self.fallback,
            "warnings": list(self.warnings),
            "scope_authority": self.scope_authority,
        }

    def explain(self) -> str:
        """Deterministic, line-per-reviewer rendering. No Codex, no clock."""
        head = f"Plan round {(self.previous_round or 0) + 1}"
        lines = [f"{head} — ancrage : " + (
            f"round {self.previous_round}" if self.anchored
            else f"{self.anchor_status}")]
        if self.fallback:
            lines.append(f"Repli : {self.fallback} (comportement v0.1)")
        if self.delta_paths:
            lines.append(f"Delta : {len(self.delta_paths)} fichier(s) — "
                         + ", ".join(self.delta_paths))
        else:
            lines.append("Delta : aucun fichier modifié depuis ce round")
        if self.scope_authority:
            lines.append(f"Autorité de périmètre : {self.scope_authority}")
        lines.append("")
        lines.append(f"{'reviewer':<16}{'état':<12}déclencheurs / raison")
        for row in self.rows:
            if row.selected:
                lines.append(f"{row.reviewer:<16}{'SELECTED':<12}"
                             + ", ".join(row.triggers))
            else:
                detail = EXCLUSION_TEXT.get(row.exclusion_reason,
                                            row.exclusion_reason)
                if row.triggers:
                    detail += " (" + ", ".join(row.triggers) + ")"
                lines.append(f"{row.reviewer:<16}{'SKIPPED':<12}{detail}")
        for warning in self.warnings:
            lines.append("")
            lines.append(f"avertissement : {warning}")
        return "\n".join(lines)


# =============================================================== anchoring ===
def previous_round(session_id: str,
                   expected_round: Optional[int] = None
                   ) -> Tuple[Optional[Dict[str, Any]], str]:
    """The round a targeted round 2 may reason against, and why it may not.

    Four ways to refuse, and every one of them means the same thing to the
    caller: do not infer a delta, fall back to the full router (fail-open, I1).

    An unreadable journal is refused rather than skipped. ``load_rounds`` drops a
    file it cannot parse, so anchoring on whatever loaded would silently take the
    round *before* the real one as the reference - and every path touched in
    between would vanish from the delta.
    """
    records = findings.load_rounds(session_id)
    on_disk = _round_files(session_id)
    if len(records) != len(on_disk):
        return None, UNREADABLE
    if not records:
        return None, NO_PREVIOUS_ROUND

    record = records[-1]
    if not findings.round_is_anchorable(record):
        return None, NOT_ANCHORABLE
    if expected_round is not None and int(record.get("round") or 0) != expected_round:
        # The session says it completed round N and the journals say otherwise.
        # Neither is authoritative over the other, so nothing is anchored.
        return None, INCONSISTENT
    return record, ANCHORED


def _round_files(session_id: str) -> List[Path]:
    directory = paths.session_dir(session_id) / "findings"
    if not directory.is_dir():
        return []
    return sorted(directory.glob("round-*.json"))


def has_round_history(session_id: str) -> bool:
    """Has a round ever been journalled for this session?

    Reads the directory, not the parsed records. A caller that gated on
    ``load_rounds`` would go quiet on exactly the case worth reporting: a single
    corrupt ``round-1.json`` parses to nothing, so the session looks like it
    never had a round, and the `unreadable` fallback is never shown.
    """
    return bool(_round_files(session_id))


# =================================================================== delta ===
def compute_delta(previous_files: Mapping[str, Optional[str]],
                  current_files: Mapping[str, Optional[str]],
                  recorded: Optional[Set[str]] = None) -> List[str]:
    """Paths whose bytes differ between the round journal and the working tree.

    Both sides are content hashes, ``None`` meaning "the file is not there". For
    a path the previous round actually recorded, the four cases fall out of one
    comparison:

        hash -> other hash   modified
        None -> hash         created
        hash -> None         deleted
        None -> None         absent then, absent now; not a delta

    ``recorded`` is the set of paths ``previous_files`` genuinely describes, and
    it defaults to its keys. The distinction is not cosmetic: outside that set
    ``None`` means "never recorded", not "was absent", and reading the second for
    the first loses a whole class of change. A file that existed and was
    untouched at round 1 is absent from that round's journal; delete it
    afterwards and both sides read ``None``, so the deletion vanished from the
    delta - and with it the scope authority that a non-empty delta guarantees.
    An unrecorded path is therefore always a delta; ``delta_since`` only offers
    paths that are in the session diff now and were not in the round's, which is
    itself proof they moved after that round.

    Nothing here consults ``edits.jsonl``. A change made by Edit, by ``sed``, by
    a formatter or by the user in their own editor is the same event: the bytes
    moved. That independence is the whole point of hashing the tree.
    """
    known = set(previous_files) if recorded is None else set(recorded)
    out = []
    for relpath in set(previous_files) | set(current_files):
        if relpath not in known:
            out.append(relpath)
        elif previous_files.get(relpath) != current_files.get(relpath):
            out.append(relpath)
    return sorted(out)


def delta_since(record: Mapping[str, Any], repo: Path,
                extra_paths: Iterable[str] = ()) -> List[str]:
    """``compute_delta`` against the live tree.

    ``extra_paths`` widens the comparison beyond what the round reviewed - the
    session diff, typically - so a file created *after* that round is seen. A
    path the round already knows needs no help: it is in the journal's map.
    """
    previous = dict((record.get("diff") or {}).get("files") or {})
    watched = sorted(set(previous) | {p.replace("\\", "/") for p in extra_paths})
    return compute_delta(previous, baseline.content_map(repo, watched),
                         recorded=set(previous))


# ============================================ what became of each finding ===
def dispositions(record: Mapping[str, Any],
                 resolutions: Mapping[str, Mapping[str, Any]],
                 decision_status: Mapping[str, str]) -> Dict[str, str]:
    """finding id -> disposition, for every finding of the round.

    Matching is by finding id throughout. ``promoted`` maps a finding id to the
    decision it became, which is exact; ``finding_key`` is never consulted,
    because two different findings can share one (see the module docstring).

    A promoted finding whose decision cannot be found in the ledger counts as
    ``pending_human``. That is the fail-closed side of the pair: an unreadable or
    missing human decision must not be downgraded into something Claude may
    re-review away.
    """
    promoted = dict(record.get("promoted") or {})
    out: Dict[str, str] = {}
    for raw in record.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        finding_id = raw.get("id") or ""
        if not finding_id:
            continue
        if finding_id in promoted or raw.get("requires_human_decision"):
            status = decision_status.get(promoted.get(finding_id, ""), "")
            out[finding_id] = (D_HUMAN_SETTLED
                               if status and status != decisions.PENDING
                               else D_PENDING_HUMAN)
            continue
        status = str((resolutions.get(finding_id) or {}).get("status") or "")
        out[finding_id] = {
            findings.ACCEPTED: D_ACCEPTED,
            findings.REJECTED: D_REJECTED,
            findings.DEFERRED: D_DEFERRED,
        }.get(status, D_UNANSWERED)
    return out


def by_reviewer(record: Mapping[str, Any],
                disposition: Mapping[str, str]) -> Dict[str, Set[str]]:
    """reviewer -> the set of dispositions it holds in this round.

    Reads ``reviewers``, the post-dedup list, not the ``"a+b"`` display string:
    when two personas made the same remark, an unanswered finding contests both.
    """
    out: Dict[str, Set[str]] = {}
    for raw in record.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        held = disposition.get(raw.get("id") or "")
        if held is None:
            continue
        names = [n for n in (raw.get("reviewers") or []) if n]
        if not names:
            names = [n for n in str(raw.get("reviewer") or "").split("+") if n]
        for name in names:
            out.setdefault(name, set()).add(held)
    return out


# ================================================= identity, safely indexed ===
def safe_key_index(round_findings: Sequence[Mapping[str, Any]]
                   ) -> Dict[str, Dict[str, Any]]:
    """``finding_key`` -> the single finding carrying it, ambiguous keys dropped.

    The guarantee 1c needs in one function: a key claimed by more than one
    finding is *absent* from the index, so a lookup returns nothing and the
    caller treats the finding as unmatched. Never a reiteration, never
    "already handled", never a reason to skip a human decision. False
    duplication beats false equivalence.
    """
    counts: Dict[str, int] = {}
    for raw in round_findings:
        key = (raw or {}).get("key") or ""
        if key:
            counts[key] = counts.get(key, 0) + 1
    return {raw["key"]: dict(raw) for raw in round_findings
            if (raw or {}).get("key") and counts.get(raw["key"]) == 1}


# ================================================= deterministic orderings ===
def order_candidates(names: Iterable[str], cfg) -> List[str]:
    """A total order over reviewers that does not depend on how they arrived.

    ``reviewers.always`` first in its declared order, then ``reviewers.auto``,
    then the remaining known reviewers, then anything else alphabetically. The
    plan must be identical whether a reviewer was learnt from the journal, from
    the router or from the config.
    """
    from .config import KNOWN_REVIEWERS

    rank: Dict[str, Tuple[int, int]] = {}
    for index, name in enumerate(list(cfg.get("reviewers.always") or [])):
        rank.setdefault(name, (0, index))
    for index, name in enumerate(list(cfg.get("reviewers.auto") or [])):
        rank.setdefault(name, (1, index))
    for index, name in enumerate(KNOWN_REVIEWERS):
        rank.setdefault(name, (2, index))
    return sorted(set(names), key=lambda n: (rank.get(n, (3, 0)), n))


def pick_authority(selected: Sequence[str], candidates: Sequence[str],
                   authority_personas: Set[str],
                   previous_authority: Optional[str] = None) -> Optional[str]:
    """Who carries the scope verdict, chosen without ever leaving it unassigned.

    In order: the authority of the previous round, if it really is one and is
    still available; a reviewer already selected that really is a scope
    authority; any available authority; and only then the v0.1 inheritance rule,
    where the first selected reviewer holds the role without the persona.

    Continuity comes first on purpose. Deriving the role from the current
    selection - as v0.1 does, and as an earlier revision of this function did -
    makes it a function of the delta's *content*: deleting a file pulls in
    `net_removal`, `architecture` gets selected, and the session's scope
    authority silently changes between round 1 and round 2. For the one role
    that receives the approved scope, holds the human decisions and decides
    whether an attempt is anchorable at all, a stable answer is worth the
    occasional extra reviewer - and over-selection is the bias this milestone
    was asked for anyway.

    The last case is worth naming: an inheriting reviewer fills the ``scope``
    block because the schema demands it, but its persona was never written to
    assess drift. It is never preferred, only accepted when nothing better
    exists, which is also why an *inherited* previous authority does not win the
    first test - it is not in ``authority_personas``.
    """
    if previous_authority in authority_personas and previous_authority in candidates:
        return previous_authority
    for name in selected:
        if name in authority_personas:
            return name
    for name in candidates:
        if name in authority_personas:
            return name
    return selected[0] if selected else (candidates[0] if candidates else None)


def enforce_scope_authority(selected: Sequence[str],
                            authority: Optional[str]) -> List[str]:
    """The invariant as a function: the authority is in the list, or it is added.

    Pure, and deliberately trivial, so 1c can apply it at the one place where the
    selection becomes a run - after the cap, after the overrides, last.
    """
    out = list(selected)
    if authority and authority not in out:
        out.insert(0, authority)
    return out


# ====================================================== the plan itself ======
def plan(*, previous_record: Optional[Mapping[str, Any]],
         anchor_status: str,
         delta_paths: Sequence[str],
         delta_selected: Sequence[str] = (),
         delta_evidence: Optional[Mapping[str, Sequence[str]]] = None,
         resolutions: Optional[Mapping[str, Mapping[str, Any]]] = None,
         decision_status: Optional[Mapping[str, str]] = None,
         candidates: Sequence[str] = (),
         never: Iterable[str] = (),
         max_selected: int = 4,
         authority_personas: Optional[Set[str]] = None,
         previous_authority: Optional[str] = None) -> Round2Plan:
    """Build the plan from already-gathered facts. No I/O, no Codex, no clock.

    ``candidates`` must already be ordered by ``order_candidates`` - that is what
    makes the result independent of the order reviewers were discovered in.
    ``delta_selected`` is the v0.1 router's answer on the delta, and
    ``delta_evidence`` maps each reviewer to the signal *names* that scored it,
    so ``always`` - which fires unconditionally and therefore says nothing about
    the delta - can be told apart from evidence.
    """
    delta_evidence = delta_evidence or {}
    resolutions = resolutions or {}
    decision_status = decision_status or {}
    authority_personas = authority_personas or set()
    never = set(never)
    result = Round2Plan(
        previous_round=(int(previous_record.get("round") or 0)
                        if previous_record else None),
        anchor_status=anchor_status,
        delta_paths=list(delta_paths))

    if anchor_status != ANCHORED or previous_record is None:
        # Fail-open: an anchor that cannot account for itself yields no targeted
        # delta at all. 1c re-runs the v0.1 router over the whole session diff
        # rather than guess, and the reason is on the record.
        result.fallback = FALLBACK_FULL_ROUTER
        result.warnings.append(_anchor_warning(anchor_status))
        return result

    disposition = dispositions(previous_record, resolutions, decision_status)
    held = by_reviewer(previous_record, disposition)
    has_delta = bool(delta_paths)

    triggers: Dict[str, List[str]] = {name: [] for name in candidates}
    for name in candidates:
        if name in never:
            continue
        states = held.get(name, set())
        if states & set(CONTESTING):
            triggers[name].append(T_CONTESTED)
        if has_delta and D_ACCEPTED in states:
            # Not restricted to the finding's own file: a correction is routinely
            # made somewhere else entirely, and the reviewer that asked for it is
            # the one able to say whether it landed. Slight over-selection is the
            # intended bias - a missed fix verification is the expensive error.
            triggers[name].append(T_FIX_VERIFICATION)
        if has_delta and name in delta_selected and _is_evidence(
                delta_evidence.get(name, ())):
            triggers[name].append(T_NEW_SURFACE)

    selected = [n for n in candidates if triggers.get(n)]

    authority = pick_authority(selected, [c for c in candidates if c not in never],
                               authority_personas, previous_authority)
    if has_delta:
        if authority is None:
            result.warnings.append(
                "aucune autorité de périmètre disponible : "
                "le plan ne peut pas garantir le verdict de périmètre")
        else:
            if authority not in triggers:
                triggers[authority] = []
            if T_SCOPE_AUTHORITY not in triggers[authority]:
                triggers[authority].append(T_SCOPE_AUTHORITY)
            if authority not in selected:
                selected.append(authority)
    result.scope_authority = authority if has_delta else None

    kept, capped = _apply_cap(selected, triggers, candidates, max_selected,
                              authority if has_delta else None)
    if capped:
        result.warnings.append(
            f"max_selected={max_selected} : {len(capped)} reviewer(s) "
            f"écarté(s) malgré un déclencheur ({', '.join(capped)})")

    order = {name: i for i, name in enumerate(candidates)}
    result.selected_reviewers = sorted(kept, key=lambda n: order.get(n, len(order)))
    result.rows = [
        Round2Row(reviewer=name, selected=name in kept,
                  triggers=list(triggers.get(name) or []),
                  exclusion_reason="" if name in kept else _exclusion(
                      name, triggers, never, held, capped))
        for name in candidates]
    return result


def _is_evidence(signals: Sequence[str]) -> bool:
    """Did the delta itself call for this reviewer?

    The ``always`` signal fires on every context that exists, so it carries no
    information about the delta - a reviewer held up only by it has not been
    shown a new surface. It still reaches the plan through
    ``scope_authority``, ``contested`` or ``fix_verification`` when it should.
    This is not a second routing logic: the router did the scoring, this only
    reads which of its signals actually looked at something.
    """
    return any(name != "always" for name in signals)


def _apply_cap(selected: Sequence[str], triggers: Mapping[str, Sequence[str]],
               candidates: Sequence[str], max_selected: int,
               authority: Optional[str]) -> Tuple[List[str], List[str]]:
    """Honour ``reviewers.max_selected`` without ever evicting the authority.

    Ranking, in order: strongest trigger held (``scope_authority`` then
    ``contested`` then ``fix_verification`` then ``new_surface``), then the
    candidate order, which is itself a total order. Deterministic end to end, and
    the losers keep their triggers on the record so the cap is visible as a cap.
    """
    order = {name: i for i, name in enumerate(candidates)}

    def rank(name: str) -> Tuple[int, int, str]:
        held = triggers.get(name) or []
        best = min((TRIGGER_RANK.get(t, len(TRIGGER_ORDER)) for t in held),
                   default=len(TRIGGER_ORDER))
        return (best, order.get(name, len(order)), name)

    ranked = sorted(selected, key=rank)
    limit = max(1, int(max_selected))
    if len(ranked) <= limit:
        return list(ranked), []

    kept = ranked[:limit]
    if authority and authority not in kept:
        # Cannot happen while the authority ranks first. Re-asserted rather than
        # trusted: the guarantee must not depend on the ranking staying put, and
        # `dropped` is recomputed from `kept` so no reviewer can fall out of both
        # lists and end up excluded without a reason.
        kept = [authority] + [n for n in ranked if n != authority][:limit - 1]
    dropped = [n for n in ranked if n not in kept]
    return list(kept), dropped


def _exclusion(name: str, triggers: Mapping[str, Sequence[str]],
               never: Set[str], held: Mapping[str, Set[str]],
               capped: Sequence[str]) -> str:
    """Why this reviewer is not in the plan. Never empty for an excluded row."""
    if name in never:
        return X_NEVER
    if name in capped:
        return X_MAX_SELECTED
    states = held.get(name, set())
    # It held findings, none of which contest. Name which kind, so a reader can
    # tell "parked" from "the human has it" from "nothing to say" - but claim a
    # specific reason only when it is the whole story. A mixed bag falls back to
    # the honest generic one: what is true of every case is that no trigger
    # fired, and a reason that overstates is worse than one that is merely broad.
    for disposition, reason in ((D_PENDING_HUMAN, X_PENDING_HUMAN),
                                (D_HUMAN_SETTLED, X_HUMAN_SETTLED),
                                (D_DEFERRED, X_DEFERRED_ONLY)):
        if states == {disposition}:
            return reason
    return X_NO_TRIGGER


def _anchor_warning(status: str) -> str:
    return {
        NO_PREVIOUS_ROUND: "aucun round précédent : rien à cibler",
        NOT_ANCHORABLE: "round précédent non ancrable (journal legacy, "
                        "schema < 2) : delta ciblé impossible",
        UNREADABLE: "round précédent non ancrable (journal illisible) : "
                    "delta ciblé impossible",
        INCONSISTENT: "round précédent non ancrable (journaux et état de "
                      "session en désaccord) : delta ciblé impossible",
    }.get(status, "round précédent non ancrable")


# ================================================ overrides, for 1c to use ===
def apply_overrides(selected: Sequence[str], *,
                    authority: Optional[str],
                    only: Optional[Sequence[str]] = None,
                    add: Optional[Sequence[str]] = None,
                    select_all: bool = False,
                    candidates: Sequence[str] = (),
                    never: Iterable[str] = ()) -> Tuple[List[str], List[str]]:
    """Apply ``--only`` / ``--add`` / ``--all`` and re-assert the authority.

    What the three flags do today, in ``router.select``:

    ``--all``   selects every candidate; the cap is skipped. The authority is in
                it by construction.
    ``--add``   only widens the selection. Harmless.
    ``--only``  *replaces* the selection, skips the cap, and is the one that can
                remove the designated scope authority. ``review.run`` then calls
                ``context.pick_scope_authority`` on what is left, so the role is
                re-assigned to a reviewer whose persona declares
                ``scope_authority: false`` - it fills the block because the
                schema demands it, without the mandate or the context to assess
                drift. The round is still classified ``success`` and still
                becomes an anchor.

    So the invariant "an override reachable by Claude cannot remove the scope
    authority" does NOT hold in ``review.run()`` as it stands. Closing it means
    changing the call site, which belongs to 1c; this function is the piece 1c
    needs, and it is deliberately the last step: whatever the override asked for,
    the authority comes back.

    Returns the selection and the notes explaining what was re-added.
    """
    never = set(never)
    notes: List[str] = []
    if select_all:
        out = [name for name in candidates if name not in never]
    elif only:
        out = [name for name in candidates if name in set(only) and name not in never]
    else:
        out = [name for name in selected if name not in never]
        for extra in (add or []):
            if extra in candidates and extra not in never and extra not in out:
                out.append(extra)

    if authority and authority not in out:
        out = enforce_scope_authority(out, authority)
        notes.append(
            f"autorité de périmètre {authority} réintroduite : un override "
            f"ne peut pas la retirer")
    return out, notes


# ================================================== I/O entry point (CLI) ====
def build(session_id: str, repo: Path, cfg,
          extra_paths: Optional[Sequence[str]] = None) -> Round2Plan:
    """Read the session's own records and plan the next round.

    The only impure function in the module, and it reads: round journals,
    resolutions, the decision ledger, the persona directory and the working tree.
    It writes nothing and runs nothing.
    """
    from . import context, router, state

    session_state = state.load(session_id)
    expected = session_state.round if session_state.round else None
    record, status = previous_round(session_id, expected_round=expected)

    available = context.available_personas(repo)
    authority_personas = set()
    for name in available:
        persona = context.load_persona(name, repo)
        if persona is not None and persona.scope_authority:
            authority_personas.add(name)

    if record is None:
        return plan(previous_record=None, anchor_status=status,
                    delta_paths=[], candidates=[],
                    authority_personas=authority_personas)

    if extra_paths is None:
        diff = baseline.compute(session_id, repo, cfg)
        extra_paths = list(diff.files)
    delta_paths = delta_since(record, repo, extra_paths)

    delta_selected: List[str] = []
    delta_evidence: Dict[str, List[str]] = {}
    if delta_paths:
        route = _route_delta(session_id, repo, cfg, delta_paths, available)
        # `eligible`, not `selected`: the router has already applied
        # `max_selected` to the latter, and a reviewer it dropped there would
        # reach the plan with no trigger at all - reported as "aucun
        # déclencheur" when the truth is that the cap took it. The plan applies
        # the cap itself, once, where it also knows to spare the scope
        # authority.
        delta_selected = list(route.eligible)
        delta_evidence = {row.reviewer: list(row.signals) for row in route.rows}

    from .config import KNOWN_REVIEWERS
    known = set(KNOWN_REVIEWERS)
    pool = set(cfg.get("reviewers.always") or []) | set(
        cfg.get("reviewers.auto") or [])
    # A reviewer that ran last round stays a candidate even if it is not in
    # `auto`: `crux review --only tests` can leave an unanswered finding, and
    # the reviewer that raised it is the one that must be asked again.
    pool |= {name for name in (record.get("selected") or []) if name in known}
    pool |= set(delta_selected)
    candidates = order_candidates(
        [name for name in pool if name in known and name in available], cfg)

    return plan(
        previous_record=record,
        anchor_status=status,
        delta_paths=delta_paths,
        delta_selected=delta_selected,
        delta_evidence=delta_evidence,
        resolutions=findings.resolutions(session_id),
        decision_status=_decision_status(session_id),
        candidates=candidates,
        never=cfg.get("reviewers.never") or [],
        max_selected=int(cfg.get("reviewers.max_selected", 4)),
        authority_personas=authority_personas,
        previous_authority=record.get("scope_authority"))


def _decision_status(session_id: str) -> Dict[str, str]:
    """decision id -> status. An unreadable ledger yields nothing, on purpose.

    ``dispositions`` reads a missing status as ``pending_human``, so a ledger
    Crux cannot read leaves every promoted finding with the human - fail-closed
    on the decision side, exactly as I2 requires.
    """
    try:
        return {d.id: d.status for d in decisions.load_all(session_id)}
    except decisions.DecisionsCorrupt:
        return {}


def _current_text(repo: Path, delta_paths: Sequence[str], budget: int) -> str:
    """The current content of the delta paths, as the router's content input.

    A real round(N-1)→now patch is out of scope for this milestone, and the
    obvious stand-in - the added lines of the diff against the *arming baseline*
    - is not merely approximate, it can under-select. Take a file whose baseline
    calls ``subprocess``, which round 1 removed and the fix then restored: the
    file is back at its baseline, so that diff is empty, no line reads as added,
    and ``security`` is never pulled in although the delta genuinely
    reintroduced the call.

    Reading the current content instead is strictly conservative. Every line
    present now is offered to the signals, whether or not it moved since the
    baseline, so a content signal can fire for a file that merely appears in the
    delta. That is over-selection, which is the bias this milestone asked for -
    a missed reviewer is the expensive error. Binary files are skipped and the
    whole thing is capped, so a large delta cannot blow up the router.
    """
    from . import gitctx

    chunks: List[str] = []
    total = 0
    for relpath in delta_paths:
        candidate = repo / relpath
        if not candidate.is_file() or total >= budget:
            continue
        try:
            raw = candidate.read_bytes()
        except OSError:
            continue
        if gitctx.looks_binary(raw):
            continue
        text = raw.decode("utf-8", errors="replace")[:budget - total]
        chunks.append(text)
        total += len(text)
    return "\n".join(chunks)


def _route_delta(session_id: str, repo: Path, cfg, delta_paths: List[str],
                 available: Sequence[str]):
    """The v0.1 router, run over the delta and nothing else.

    Everything the context is built from comes from the delta: ``paths`` and
    ``deleted_files`` are the delta itself, and ``added_text`` is the current
    content of those paths (see ``_current_text`` for why the baseline diff is
    not safe to use here). The line counts stay as measured against the arming
    baseline - they only feed `magnitude`, whose other two conditions are exact.
    """
    from . import gitctx, router

    diff = baseline.compute(session_id, repo, cfg, only_paths=list(delta_paths))
    ctx = router.build_context(diff, repo, gitctx.project_has_tests(repo))
    ctx.paths = sorted(set(ctx.paths) | set(delta_paths))
    ctx.files_changed = len(ctx.paths)
    ctx.deleted_files = sorted(
        set(ctx.deleted_files) | {p for p in delta_paths
                                  if not (repo / p).is_file()})
    ctx.added_text = _current_text(
        repo, ctx.paths, int(cfg.get("scope.max_diff_chars", 60000)))
    return router.select(ctx, cfg, available=available)
