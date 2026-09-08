"""Reviewer output: validation, dedup, identity, the severity gate, and the one
transition that matters - promotion out of Claude's authority.

A finding carries three distinct identities, and conflating them is what broke
resolution across rounds:

``id``         ``R1F3`` - the arbitration address Claude passes to ``crux resolve``.
               Scoped by round, because a bare ``F3`` reused at round 2 was being
               matched against a round 1 resolution and silently counted as
               already settled.
``key``        ``finding-key-v1`` - the *content* identity, stable across rounds:
               "is this the same remark as last round?".  See ``finding_key``.
``reviewers``  who emitted it, after dedup.  ``reviewer`` stays the display
               string (``"a+b"``); ``reviewers`` is the list to reason with.

A finding carrying ``requires_human_decision`` leaves this ledger and becomes a
decision.  ``resolve`` then refuses to touch it: the prohibition is a command that
is not there, not a sentence in a prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths, state

SEVERITIES = ("low", "medium", "high", "critical")
SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITIES)}

ACCEPTED = "accepted"
REJECTED = "rejected"
DEFERRED = "deferred"
PROMOTED = "promoted"
RESOLUTIONS = {ACCEPTED, REJECTED, DEFERRED}

SCOPE_STATUSES = ("within_scope", "scope_change", "uncertain")
SCOPE_KINDS = (
    "added_feature", "removed_capability", "behavior_change", "ux_change",
    "api_change", "data_format_change", "incompatibility", "product_decision",
)


class PromotedFinding(Exception):
    """`crux resolve` was pointed at something only the human can settle."""


class AmbiguousFinding(Exception):
    """A short id matched more than one finding.

    Deterministic or refuse: resolving ``F1`` against "the most recent round"
    would silently arbitrate the wrong finding, which is the exact failure the
    round-scoped ids exist to remove. The caller is told which full ids matched.
    """

    def __init__(self, ref: str, candidates: List[str]):
        self.ref = ref
        self.candidates = candidates
        super().__init__(
            f"identifiant ambigu : {ref} correspond à "
            f"{len(candidates)} findings ({', '.join(candidates)}).\n"
            f"  → reprenez l'identifiant complet, par exemple "
            f"`--id {candidates[0]}`")


# ----------------------------------------------------------------- identity ---
# `finding-key-v1` - the content identity of a finding, stable over a session.
#
# Built ONLY from fields the shared output schema actually carries and that a
# correction does not move:
#
#   * ``file``   normalised (separators, leading ./ and /), never lowercased -
#                case distinguishes real files on Linux.
#   * ``title``  normalised (lowercased, non-alphanumerics collapsed).
#
# Deliberately excluded, and why:
#
#   * ``reviewer``     two personas saying the same thing is one remark.
#   * ``line``         a fix above the finding shifts it without changing it.
#   * ``description``  / ``evidence`` / ``suggestion``: rewritten after a fix.
#   * ``severity``     a judgement the reviewer may revise between rounds.
#
# The schema has no ``kind``/``category``/``symbol`` for findings (only the
# `scope.changes[].kind` block does, which is not a finding). Adding one would be
# a schema migration this milestone does not need, so the key stays minimal and
# says so in its version prefix: a v2 can add a locator without ambiguity.
#
# Because the key is built from two fields, two genuinely different findings can
# collide on it. The key is therefore NEVER the identity of record on its own:
# ``identity`` is ``key#occurrence``, and ``occurrence`` separates collisions
# within a round. A false equivalence would make one finding's arbitration
# silently settle another, mark a fresh remark as a mere reiteration, or suppress
# a human decision that was never asked - all worse than a duplicate. When the
# schema cannot prove two findings are the same, they stay apart.
FINDING_KEY_VERSION = "finding-key-v1"


def norm_path(value: Optional[str]) -> str:
    """Path form used for identity: POSIX separators, no ./ or / prefix."""
    if not value:
        return ""
    text = str(value).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def finding_key(file: Optional[str], title: str) -> str:
    """Content identity of a finding. See FINDING_KEY_VERSION for the contract."""
    material = "\x00".join(
        (FINDING_KEY_VERSION, norm_path(file), _norm_title(title or "")))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


_ID_RE = re.compile(r"^R(\d+)(F\d+)$")


def make_finding_id(round_no: int, index: int) -> str:
    return f"R{round_no}F{index}"


def short_id(finding_id: str) -> str:
    """`R1F3` -> `F3`. Anything else is returned unchanged."""
    match = _ID_RE.match(finding_id or "")
    return match.group(2) if match else (finding_id or "")


def round_of(finding_id: str) -> Optional[int]:
    match = _ID_RE.match(finding_id or "")
    return int(match.group(1)) if match else None


@dataclass
class Finding:
    id: str
    reviewer: str
    severity: str
    title: str
    description: str
    confidence: str = "medium"
    file: Optional[str] = None
    line: Optional[int] = None
    suggestion: Optional[str] = None
    requires_human_decision: bool = False
    key: str = ""
    # Rank among the findings of a round sharing the same key. 1 unless the key
    # collided; see FINDING_KEY_VERSION for why collisions must not merge.
    occurrence: int = 1
    reviewers: List[str] = field(default_factory=list)
    # Reiteration (1c). `reiterates` is the id of the earlier finding this one
    # repeats, matched by *safe* identity only - an ambiguous key never matches.
    # `insistence` is set when that earlier finding was `rejected` by Claude: the
    # reviewer may say it once more, visibly, but it no longer blocks.
    reiterates: Optional[str] = None
    insistence: bool = False

    def __post_init__(self) -> None:
        """Backfill the two derived identities.

        Done here rather than at every construction site so a Finding built by a
        test, by a parser or by a round replay always carries the same identity.
        """
        if not self.reviewers:
            self.reviewers = [r for r in str(self.reviewer or "").split("+") if r]
        if not self.key:
            self.key = finding_key(self.file, self.title)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "reviewer": self.reviewer, "severity": self.severity,
            "title": self.title, "description": self.description,
            "confidence": self.confidence, "file": self.file, "line": self.line,
            "suggestion": self.suggestion,
            "requires_human_decision": self.requires_human_decision,
            "key": self.key, "occurrence": self.occurrence,
            "identity": self.identity, "reviewers": list(self.reviewers),
            "reiterates": self.reiterates, "insistence": self.insistence,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Finding":
        """Replay a persisted finding, including one written before v0.2.

        Unknown keys are dropped and missing ones fall back to the dataclass
        defaults, so a round journal written by v0.1 loads without migration:
        ``__post_init__`` recomputes ``key`` and ``reviewers`` from what is there.
        """
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        data = {k: v for k, v in raw.items() if k in known}
        data.setdefault("id", "")
        data.setdefault("reviewer", "")
        data.setdefault("severity", "medium")
        data.setdefault("title", "")
        data.setdefault("description", "")
        return cls(**data)

    @property
    def identity(self) -> str:
        """Session identity of record: the content key plus its occurrence.

        Two findings that collide on ``key`` have different identities, so an
        arbitration, a reiteration or a settled decision attached to one never
        applies to the other.
        """
        return f"{self.key}#{self.occurrence}"

    @property
    def blocking_rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 0)


@dataclass
class ScopeAssessment:
    reviewer: str
    status: str = "within_scope"
    assessment: str = ""
    changes: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_change(self) -> bool:
        return self.status == "scope_change"

    def human_changes(self) -> List[Dict[str, Any]]:
        return [c for c in self.changes if c.get("requires_human_decision")]

    def to_dict(self) -> Dict[str, Any]:
        return {"reviewer": self.reviewer, "status": self.status,
                "assessment": self.assessment, "changes": self.changes}


@dataclass
class ReviewerResult:
    reviewer: str
    ok: bool = True
    verdict: str = "approve"
    summary: str = ""
    findings: List[Finding] = field(default_factory=list)
    scope: Optional[ScopeAssessment] = None
    error: Optional[str] = None
    error_kind: Optional[str] = None
    duration: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reviewer": self.reviewer, "ok": self.ok, "verdict": self.verdict,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "scope": self.scope.to_dict() if self.scope else None,
            "error": self.error, "error_kind": self.error_kind,
            "duration": round(self.duration, 2),
        }


def _clean(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").strip()
    return text[:limit]


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def parse_reviewer_payload(reviewer: str, payload: Any,
                           start_index: int = 0,
                           round_no: int = 1) -> ReviewerResult:
    """Turn one reviewer's JSON into a validated result. Never raises.

    ``round_no`` scopes the ids it mints. Without it every round restarted at
    ``F1`` and a round 1 resolution matched a round 2 finding.
    """
    result = ReviewerResult(reviewer=reviewer)
    if not isinstance(payload, dict):
        result.ok = False
        result.error = "réponse non conforme (objet JSON attendu)"
        result.error_kind = "schema"
        return result

    verdict = payload.get("verdict")
    result.verdict = verdict if verdict in ("approve", "changes_requested") \
        else "changes_requested"
    result.summary = _clean(payload.get("summary"), 600)

    raw_scope = payload.get("scope")
    if isinstance(raw_scope, dict):
        status = raw_scope.get("status")
        changes = []
        for change in (raw_scope.get("changes") or [])[:6]:
            if not isinstance(change, dict):
                continue
            kind = change.get("kind")
            changes.append({
                "kind": kind if kind in SCOPE_KINDS else "product_decision",
                "description": _clean(change.get("description"), 600),
                "evidence": _clean(change.get("evidence"), 200) or None,
                "alternatives": [
                    _clean(a, 200) for a in (change.get("alternatives") or [])[:4]
                    if _clean(a, 200)],
                "requires_human_decision": bool(
                    change.get("requires_human_decision")),
            })
        result.scope = ScopeAssessment(
            reviewer=reviewer,
            status=status if status in SCOPE_STATUSES else "uncertain",
            assessment=_clean(raw_scope.get("assessment"), 400),
            changes=changes,
        )

    index = start_index
    for raw in (payload.get("findings") or [])[:12]:
        if not isinstance(raw, dict):
            continue
        title = _clean(raw.get("title"), 120)
        description = _clean(raw.get("description"), 1200)
        if not title or not description:
            continue
        severity = raw.get("severity")
        confidence = raw.get("confidence")
        line = raw.get("line")
        index += 1
        result.findings.append(Finding(
            id=make_finding_id(round_no, index),
            reviewer=reviewer,
            severity=severity if severity in SEVERITIES else "medium",
            confidence=confidence if confidence in ("low", "medium", "high")
            else "medium",
            title=title,
            description=description,
            file=_clean(raw.get("file"), 300) or None,
            line=line if isinstance(line, int) and not isinstance(line, bool)
            else None,
            suggestion=_clean(raw.get("suggestion"), 1200) or None,
            requires_human_decision=bool(raw.get("requires_human_decision")),
        ))
    return result


def dedupe(results: List[ReviewerResult]) -> Tuple[List[Finding], List[str]]:
    """Same file + same normalised title from two reviewers is one finding.

    Merging is keyed on ``finding.key`` - the same identity used to recognise a
    remark across rounds. One identity function, not two that could drift apart.
    """
    # Pass 1 - decide ambiguity for the whole round, before merging anything.
    # Doing it incrementally made the outcome depend on the order reviewers
    # happened to arrive in: a key that was still unambiguous when the second
    # reviewer landed on it would merge, and the same three findings in the other
    # order would not. Reviewer order comes from router scores, which have
    # nothing to do with whether two remarks are the same.
    #
    # A key is ambiguous as soon as ONE reviewer claims it twice: that reviewer
    # is asserting two remarks, and nothing in the schema says which of them
    # another reviewer's finding answers.
    claims: Dict[Tuple[str, str], int] = {}
    for result in results:
        for finding in result.findings:
            for name in finding.reviewers:
                claims[(finding.key, name)] = claims.get((finding.key, name), 0) + 1
    ambiguous = {key for (key, _name), count in claims.items() if count > 1}

    # Pass 2 - merge only on keys that designate exactly one thing.
    #
    #   key not ambiguous, slot taken -> the case merging exists for: two
    #                                    personas making the same remark.
    #   key ambiguous                 -> never merge. Picking a slot would be a
    #                                    guess, and a wrong guess makes one
    #                                    finding's arbitration settle another.
    #
    # False duplication beats false equivalence, every time.
    slots: Dict[str, List[Finding]] = {}
    order: List[Finding] = []
    merged_notes: List[str] = []
    for result in results:
        for finding in result.findings:
            existing = slots.setdefault(finding.key, [])
            # On a non-ambiguous key no reviewer claims it twice, so `existing`
            # holds at most one finding and it is someone else's.
            target = existing[0] if (existing and finding.key not in ambiguous) \
                else None
            if target is not None:
                if finding.blocking_rank > target.blocking_rank:
                    target.severity = finding.severity
                target.requires_human_decision = (
                    target.requires_human_decision
                    or finding.requires_human_decision)
                if finding.reviewer not in target.reviewer:
                    target.reviewer = f"{target.reviewer}+{finding.reviewer}"
                for name in finding.reviewers:
                    if name not in target.reviewers:
                        target.reviewers.append(name)
                merged_notes.append(f"{finding.id} fusionné dans {target.id}")
                continue
            finding.occurrence = len(existing) + 1
            existing.append(finding)
            order.append(finding)
    return order, merged_notes


def ambiguous_keys(found: List[Finding]) -> set:
    """Keys carried by more than one finding after dedup.

    A later round matching on ``key`` alone would not know which one it is
    looking at. The rule that follows from FINDING_KEY_VERSION: an ambiguous key
    is treated as *unmatched* - never as a reiteration, never as an
    already-settled decision. Consumed by the round 2 selector (1b/1c).
    """
    counts: Dict[str, int] = {}
    for finding in found:
        counts[finding.key] = counts.get(finding.key, 0) + 1
    return {key for key, total in counts.items() if total > 1}


def is_blocking(finding: Finding, block_on: str) -> bool:
    """The single severity gate. Three exclusions, and only three.

    * ``requires_human_decision`` - promoted, no longer Claude's to resolve;
    * ``insistence`` - a remark Claude already rejected with a technical reason.
      The reviewer may restate it once, visibly, but a rejection Claude is
      entitled to make must not come back as an obligation; that is how a
      disagreement becomes an unbounded escalation over three rounds. See
      ``mark_reiterations``.
    * severity below the configured floor.

    Used by ``blocking`` (the round's report) and by ``unarbitrated`` (D4), so
    the set of findings Claude owes an answer for and the set the report calls
    blocking cannot drift apart.
    """
    if finding.requires_human_decision or finding.insistence:
        return False
    return finding.blocking_rank >= SEVERITY_RANK.get(
        block_on, SEVERITY_RANK["high"])


def blocking(found: List[Finding], block_on: str) -> List[Finding]:
    """Findings severe enough to hold the turn open."""
    return [f for f in found if is_blocking(f, block_on)]


# ------------------------------------------------------------ reiterations ---
def rejected_key_index(session_id: str,
                       up_to_round: Optional[int] = None
                       ) -> Dict[str, Dict[str, Any]]:
    """``finding_key`` -> the earlier finding Claude *rejected*, safely matched.

    Built over every recorded round, not just the last one. That is what bounds
    the escalation `max_rounds > 2` would otherwise allow: once a remark has been
    rejected with a technical reason, it stays a non-blocking insistence for the
    rest of the session rather than becoming blocking again two rounds later.

    Safety, in order:

    * a key claimed by more than one finding *anywhere* in the session is dropped
      outright - an ambiguous key never matches, so a reiteration is never
      inferred from a collision (see ``FINDING_KEY_VERSION``);
    * only ``rejected`` qualifies. ``deferred`` is a parking decision, not a
      dispute, and ``accepted`` reappearing means the reviewer judges the fix
      insufficient - a normal finding, blocking on its own severity.
    """
    settled = resolutions(session_id)
    counts = session_key_counts(session_id, up_to_round=up_to_round)
    candidates: Dict[str, Dict[str, Any]] = {}
    for payload in load_rounds(session_id):
        round_no = payload.get("round")
        if up_to_round is not None and (round_no or 0) >= up_to_round:
            continue
        promoted = payload.get("promoted") or {}
        for raw in payload.get("findings") or []:
            if not isinstance(raw, dict):
                continue
            key = raw.get("key") or ""
            finding_id = raw.get("id") or ""
            if not key:
                continue
            if finding_id in promoted or raw.get("requires_human_decision"):
                continue
            status = str((settled.get(finding_id) or {}).get("status") or "")
            if status != REJECTED:
                continue
            candidates.setdefault(key, {
                "id": finding_id,
                "round": round_no,
                "reason": str((settled.get(finding_id) or {}).get("reason") or ""),
            })
    return {key: value for key, value in candidates.items()
            if counts.get(key) == 1}


def session_key_counts(session_id: str,
                       up_to_round: Optional[int] = None) -> Dict[str, int]:
    """``finding_key`` -> the most findings that ever claimed it in ONE round.

    The cross-round counterpart of ``round2.safe_key_index``, and the per-round
    scope is the whole subtlety. Ambiguity is a *collision*: two different
    remarks that the two-field key cannot tell apart. That can only happen inside
    a single round, where both exist at once and nothing says which is which.

    The same key appearing again in a later round is the opposite of ambiguity -
    it is the reviewer restating a remark, which is exactly what a reiteration
    is. Counting occurrences session-wide would have made every repetition look
    like a collision, so a rejected finding restated twice would have escaped the
    insistence rule and become blocking again at round 3: the unbounded
    escalation §8 forbids.

    A value of 1 therefore means "this key designates one remark, in every round
    it appears in" and is safe to match on. Anything higher is a real collision
    and must never match.
    """
    peak: Dict[str, int] = {}
    for payload in load_rounds(session_id):
        if up_to_round is not None and (payload.get("round") or 0) >= up_to_round:
            continue
        seen: Dict[str, int] = {}
        for raw in payload.get("findings") or []:
            if not isinstance(raw, dict):
                continue
            key = raw.get("key") or ""
            if key:
                seen[key] = seen.get(key, 0) + 1
        for key, count in seen.items():
            peak[key] = max(peak.get(key, 0), count)
    return peak


def mark_reiterations(found: List[Finding],
                      rejected: Dict[str, Dict[str, Any]]) -> List[Finding]:
    """Flag the findings that repeat something Claude already rejected.

    Mutates in place and returns the same list, so the caller keeps one object
    graph. A finding whose key is not in ``rejected`` - unknown, previously
    accepted or deferred - is untouched and stays a normal finding.

    Ambiguity is checked on BOTH sides, and the second side is not redundant.
    ``rejected`` only guarantees its keys were unambiguous in the rounds it read;
    it says nothing about *this* round. A key rejected once at round 1 and
    claimed by two genuinely different findings at round 2 would otherwise mark
    both as insistences, and both would leave the blocking set and the D4
    obligation at once - one arbitration silently settling a remark it never saw,
    which is exactly the false equivalence ``FINDING_KEY_VERSION`` forbids.
    """
    ambiguous = ambiguous_keys(found)
    for finding in found:
        earlier = rejected.get(finding.key)
        if not earlier or finding.key in ambiguous:
            continue
        finding.reiterates = earlier.get("id") or None
        finding.insistence = True
    return found


def to_promote(found: List[Finding]) -> List[Finding]:
    return [f for f in found if f.requires_human_decision]


# ------------------------------------------------------------- resolutions ---
def findings_path(session_id: str, round_no: int) -> Path:
    return paths.ensure_dir(
        paths.session_dir(session_id) / "findings") / f"round-{round_no}.json"


def resolutions_path(session_id: str) -> Path:
    return paths.session_dir(session_id) / "resolutions.jsonl"


EMPTY_ROUND_DIFF: Dict[str, Any] = {"fingerprint": "", "files": {}}

# Journal format. Version 2 records what a round actually was - which reviewers
# ran, which failed, whether the scope authority answered, and the content of the
# tree at the time. A version 1 journal carries none of that, so whether it is a
# usable anchor cannot be established after the fact: an empty v1 round may be a
# genuine `approve` with nothing to say, or the wreckage of a run where every
# reviewer failed. Both look identical on disk.
ROUND_SCHEMA_VERSION = 2


def save_round(session_id: str, round_no: int, found: List[Finding],
               promoted: Dict[str, str], *,
               selected: Optional[List[str]] = None,
               scope_authority: Optional[str] = None,
               failed_reviewers: Optional[List[str]] = None,
               degraded: bool = False,
               diff: Optional[Dict[str, Any]] = None,
               skipped: Optional[List[Dict[str, Any]]] = None,
               delta_paths: Optional[List[str]] = None,
               targeting: str = "") -> None:
    """Persist one round.

    ``selected``, ``scope_authority`` and ``diff`` are what a later round needs
    to know what was reviewed and what has moved since. ``diff["files"]`` maps a
    path to the sha256 of its bytes *at review time*: hashing the working tree
    rather than the edits journal is what keeps the round-to-round delta
    independent of the tool that made the change.

    ``skipped``, ``delta_paths`` and ``targeting`` are what 1c added: which
    reviewers the targeting spared and for which named reason, what the round was
    targeted at, and whether it was targeted at all. They are read only by the
    report - no plan is ever rebuilt from them - so they are additive fields on
    the same schema version rather than a migration: bumping the version would
    make every journal written by 1a/1b unanchorable and silently send a live
    session back to the full router.
    """
    paths.write_json(findings_path(session_id, round_no), {
        "round": round_no,
        "schema": ROUND_SCHEMA_VERSION,
        "saved_at": state.now_iso(),
        "selected": list(selected or []),
        "scope_authority": scope_authority,
        "failed_reviewers": list(failed_reviewers or []),
        "degraded": bool(degraded),
        "diff": dict(diff) if diff else dict(EMPTY_ROUND_DIFF),
        "findings": [f.to_dict() for f in found],
        "promoted": promoted,
        "skipped": list(skipped or []),
        "delta_paths": list(delta_paths or []),
        "targeting": targeting,
    })


def round_is_anchorable(record: Dict[str, Any]) -> bool:
    """Whether this round may be trusted as the reference for a targeted round 2.

    Only a journal that describes its own outcome qualifies. An emptiness test is
    deliberately NOT used: a real `approve` round legitimately holds zero
    findings, and treating that as invalid would discard good history.

    The consumer (1b) must fall back to the full router with a warning rather
    than infer a delta from a journal that cannot account for itself.
    """
    return int(record.get("schema") or 1) >= ROUND_SCHEMA_VERSION


def _backfill_round(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Give a round record from any version the shape callers may assume."""
    payload.setdefault("schema", 1)
    payload.setdefault("selected", [])
    payload.setdefault("scope_authority", None)
    payload.setdefault("failed_reviewers", [])
    payload.setdefault("degraded", False)
    payload.setdefault("skipped", [])
    payload.setdefault("delta_paths", [])
    payload.setdefault("targeting", "")
    if not isinstance(payload.get("diff"), dict):
        payload["diff"] = dict(EMPTY_ROUND_DIFF)
    payload["diff"].setdefault("fingerprint", "")
    if not isinstance(payload["diff"].get("files"), dict):
        payload["diff"]["files"] = {}
    for raw in payload.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        if not raw.get("key"):
            raw["key"] = finding_key(raw.get("file"), raw.get("title") or "")
        if not raw.get("reviewers"):
            raw["reviewers"] = [
                r for r in str(raw.get("reviewer") or "").split("+") if r]
        if not isinstance(raw.get("occurrence"), int) or raw["occurrence"] < 1:
            raw["occurrence"] = 1
        raw["identity"] = f"{raw['key']}#{raw['occurrence']}"
    return payload


def load_rounds(session_id: str) -> List[Dict[str, Any]]:
    directory = paths.session_dir(session_id) / "findings"
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("round-*.json"),
                       key=lambda p: (len(p.stem), p.stem)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(payload, dict):
            out.append(_backfill_round(payload))
    return out


def _matches_ref(finding_id: str, ref: str) -> bool:
    """A reference designates a finding by its full id, or by its short form."""
    return bool(finding_id) and (finding_id == ref or short_id(finding_id) == ref)


def find_finding(session_id: str, ref: str) -> Optional[Dict[str, Any]]:
    """Resolve a finding reference, or refuse when it is ambiguous.

    ``R2F1`` names exactly one finding. ``F1`` is accepted only while it still
    designates one: as soon as two rounds carry it, the caller is asked for the
    full id rather than having the most recent round picked for them. Choosing
    silently is how a round 1 arbitration ended up applied to a round 2 finding.
    """
    matches: List[Dict[str, Any]] = []
    for payload in load_rounds(session_id):
        for raw in payload.get("findings", []):
            if not isinstance(raw, dict) or not _matches_ref(raw.get("id", ""), ref):
                continue
            record = dict(raw)
            record["_round"] = payload.get("round")
            record["_promoted_to"] = (
                payload.get("promoted") or {}).get(raw.get("id"))
            matches.append(record)
    if not matches:
        return None
    if len(matches) > 1:
        raise AmbiguousFinding(ref, [m.get("id", "?") for m in matches])
    return matches[0]


def resolve(session_id: str, finding_id: str, status: str,
            reason: str = "") -> Dict[str, Any]:
    """Claude's technical arbitration. Refuses anything promoted to the human.

    ``finding_id`` may be a full id or an unambiguous short one; the entry is
    always written under the full id, so a resolution can never be attributed to
    a finding from another round.
    """
    if status not in RESOLUTIONS:
        raise ValueError(
            f"statut inconnu {status!r} (attendu: {', '.join(sorted(RESOLUTIONS))})")

    record = find_finding(session_id, finding_id)
    if record is None:
        raise KeyError(finding_id)
    canonical = record.get("id") or finding_id

    if record.get("requires_human_decision") or record.get("_promoted_to"):
        target = record.get("_promoted_to") or "une décision"
        raise PromotedFinding(
            f"{finding_id} relève d'une décision humaine (promu en {target}), "
            f"pas d'un arbitrage technique.\n"
            f"  → posez la question via AskUserQuestion ; la réponse humaine "
            f"clôt la décision.\n"
            f"  Un finding promu ne peut pas être résolu par `crux resolve`.")

    if status == REJECTED and not reason.strip():
        raise ValueError("un rejet exige une raison technique (--reason)")

    entry = {
        "ts": state.now_iso(),
        "id": canonical,
        "given": finding_id,
        "round": record.get("_round"),
        "key": record.get("key"),
        "status": status,
        "reason": reason.strip(),
        "by": "claude",
    }
    paths.append_jsonl(resolutions_path(session_id), entry)
    return entry


def resolutions(session_id: str) -> Dict[str, Dict[str, Any]]:
    path = resolutions_path(session_id)
    if not path.is_file():
        return {}
    latest: Dict[str, Dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("id"):
                latest[record["id"]] = record
    return latest


def unresolved(session_id: str, found: List[Finding], block_on: str) -> List[Finding]:
    done = resolutions(session_id)
    return [f for f in blocking(found, block_on) if f.id not in done]


def unarbitrated(session_id: str, block_on: str) -> List[Finding]:
    """Blocking findings, already recorded, that Claude has not disposed of.

    Session-wide and read from the round journals, so the obligation survives
    `max_rounds` and the time budget: once a reviewer has produced a blocking
    finding, requiring an explicit disposition needs no further Codex run - it is
    local state that is already established.

    Claude remains the technical authority. `rejected` with a reason settles a
    finding just as well as `accepted` does; what is refused is silence.

    Excluded: anything promoted to a human decision (that is the decision gate's
    business, not Claude's) and anything already carrying a resolution.
    """
    settled = resolutions(session_id)
    out: List[Finding] = []
    for payload in load_rounds(session_id):
        promoted = payload.get("promoted") or {}
        for raw in payload.get("findings", []):
            if not isinstance(raw, dict):
                continue
            finding_id = raw.get("id") or ""
            if finding_id in settled or finding_id in promoted:
                continue
            if raw.get("requires_human_decision"):
                continue
            replayed = Finding.from_dict(raw)
            if is_blocking(replayed, block_on):
                out.append(replayed)
    return out
