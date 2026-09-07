"""Reviewer output: validation, dedup, stable ids, the severity gate, and the
one transition that matters - promotion out of Claude's authority.

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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "reviewer": self.reviewer, "severity": self.severity,
            "title": self.title, "description": self.description,
            "confidence": self.confidence, "file": self.file, "line": self.line,
            "suggestion": self.suggestion,
            "requires_human_decision": self.requires_human_decision,
        }

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
                           start_index: int = 0) -> ReviewerResult:
    """Turn one reviewer's JSON into a validated result. Never raises."""
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
            id=f"F{index}",
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
    """Same file + same normalised title from two reviewers is one finding."""
    seen: Dict[str, Finding] = {}
    order: List[str] = []
    merged_notes: List[str] = []
    for result in results:
        for finding in result.findings:
            key = hashlib.sha256(
                f"{finding.file or ''}|{_norm_title(finding.title)}".encode()
            ).hexdigest()[:16]
            if key in seen:
                kept = seen[key]
                if finding.blocking_rank > kept.blocking_rank:
                    kept.severity = finding.severity
                kept.requires_human_decision = (
                    kept.requires_human_decision or finding.requires_human_decision)
                if finding.reviewer not in kept.reviewer:
                    kept.reviewer = f"{kept.reviewer}+{finding.reviewer}"
                merged_notes.append(f"{finding.id} fusionné dans {kept.id}")
                continue
            seen[key] = finding
            order.append(key)
    return [seen[k] for k in order], merged_notes


def blocking(found: List[Finding], block_on: str) -> List[Finding]:
    """Findings severe enough to hold the turn open. Promoted ones are excluded:
    they are no longer Claude's to resolve."""
    floor = SEVERITY_RANK.get(block_on, SEVERITY_RANK["high"])
    return [f for f in found
            if not f.requires_human_decision and f.blocking_rank >= floor]


def to_promote(found: List[Finding]) -> List[Finding]:
    return [f for f in found if f.requires_human_decision]


# ------------------------------------------------------------- resolutions ---
def findings_path(session_id: str, round_no: int) -> Path:
    return paths.ensure_dir(
        paths.session_dir(session_id) / "findings") / f"round-{round_no}.json"


def resolutions_path(session_id: str) -> Path:
    return paths.session_dir(session_id) / "resolutions.jsonl"


def save_round(session_id: str, round_no: int, found: List[Finding],
               promoted: Dict[str, str]) -> None:
    paths.write_json(findings_path(session_id, round_no), {
        "round": round_no,
        "saved_at": state.now_iso(),
        "findings": [f.to_dict() for f in found],
        "promoted": promoted,
    })


def load_rounds(session_id: str) -> List[Dict[str, Any]]:
    directory = paths.session_dir(session_id) / "findings"
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("round-*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    return out


def find_finding(session_id: str, finding_id: str) -> Optional[Dict[str, Any]]:
    for payload in reversed(load_rounds(session_id)):
        for raw in payload.get("findings", []):
            if raw.get("id") == finding_id:
                raw = dict(raw)
                raw["_promoted_to"] = (payload.get("promoted") or {}).get(finding_id)
                return raw
    return None


def resolve(session_id: str, finding_id: str, status: str,
            reason: str = "") -> Dict[str, Any]:
    """Claude's technical arbitration. Refuses anything promoted to the human."""
    if status not in RESOLUTIONS:
        raise ValueError(
            f"statut inconnu {status!r} (attendu: {', '.join(sorted(RESOLUTIONS))})")

    record = find_finding(session_id, finding_id)
    if record is None:
        raise KeyError(finding_id)

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
        "id": finding_id,
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
