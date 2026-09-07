"""Orchestration of one review round.

Kept out of cli.py so the command layer stays thin and this stays testable.
Sequence: session diff -> router -> context packs -> codex (bounded pool) ->
validate -> dedupe -> promote to human decisions -> render.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import (baseline, capabilities, codex, context, decisions,
               findings, gitctx, paths, report, state)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "finding.schema.json"

TEST_MARKERS = ("tests", "test", "spec", "__tests__")


@dataclass
class RunResult:
    run_id: str
    session_id: str
    repo: str
    round: int
    started_at: str
    selected: List[str] = field(default_factory=list)
    route_explain: str = ""
    scope_authority: Optional[str] = None
    diff_files: List[str] = field(default_factory=list)
    diff_added: int = 0
    diff_removed: int = 0
    diff_fingerprint: str = ""
    warnings: List[str] = field(default_factory=list)
    reviewer_results: List[findings.ReviewerResult] = field(default_factory=list)
    all_findings: List[findings.Finding] = field(default_factory=list)
    promoted: Dict[str, str] = field(default_factory=dict)
    opened_decisions: List[str] = field(default_factory=list)
    blocking_ids: List[str] = field(default_factory=list)
    duration: float = 0.0
    codex_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id, "session_id": self.session_id,
            "repo": self.repo, "round": self.round,
            "started_at": self.started_at, "duration": round(self.duration, 2),
            "selected": self.selected, "scope_authority": self.scope_authority,
            "route_explain": self.route_explain,
            "diff": {"files": self.diff_files, "added": self.diff_added,
                     "removed": self.diff_removed,
                     "fingerprint": self.diff_fingerprint},
            "warnings": self.warnings,
            "reviewers": [r.to_dict() for r in self.reviewer_results],
            "findings": [f.to_dict() for f in self.all_findings],
            "promoted": self.promoted,
            "opened_decisions": self.opened_decisions,
            "blocking": self.blocking_ids,
            "codex_error": self.codex_error,
        }


def project_has_tests(repo: Path) -> bool:
    try:
        proc = gitctx.run(repo, "ls-files", check=False)
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


def new_run_id(repo: Path) -> str:
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    return f"{stamp}_{paths.safe_component(repo.name)}"


def run_dir(run_id: str) -> Path:
    return paths.ensure_dir(paths.runs_root() / run_id)


def rotate_runs(keep: int) -> None:
    root = paths.runs_root()
    entries = sorted((p for p in root.iterdir() if p.is_dir()),
                     key=lambda p: p.name)
    import shutil
    for stale in entries[:-keep] if keep > 0 else []:
        shutil.rmtree(stale, ignore_errors=True)


def schema_inline() -> str:
    try:
        return SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def previous_findings_digest(session_id: str, reviewer: str) -> str:
    """What this reviewer said last round, and what Claude did with it."""
    rounds = findings.load_rounds(session_id)
    if not rounds:
        return ""
    resolutions = findings.resolutions(session_id)
    lines: List[str] = []
    for raw in rounds[-1].get("findings", []):
        if reviewer not in str(raw.get("reviewer", "")):
            continue
        resolution = resolutions.get(raw.get("id", ""), {})
        verdict = resolution.get("status", "sans réponse")
        reason = resolution.get("reason", "")
        lines.append(f"- [{raw.get('id')}] {raw.get('title')} → {verdict}"
                     + (f" : {reason}" if reason else ""))
    if not lines:
        return ""
    return ("Tu as déjà émis ces remarques. Claude y a répondu. Tu peux insister "
            "UNE fois si tu penses qu'il a tort, sinon n'y reviens pas.\n"
            + "\n".join(lines))


def run(session_id: str, repo: Path, cfg,
        only: Optional[Sequence[str]] = None,
        add: Optional[Sequence[str]] = None,
        select_all: bool = False,
        task_intent: str = "",
        dry_run: bool = False,
        explain: bool = False) -> RunResult:
    from . import router

    started = time.time()
    st = state.load(session_id)
    round_no = st.round + 1
    run_id = new_run_id(repo)

    result = RunResult(
        run_id=run_id, session_id=session_id, repo=str(repo),
        round=round_no, started_at=state.now_iso())

    diff = baseline.compute(session_id, repo, cfg)
    result.diff_files = list(diff.files)
    result.diff_added = diff.added
    result.diff_removed = diff.removed
    result.diff_fingerprint = diff.fingerprint
    result.warnings.extend(diff.warnings)

    if diff.is_empty:
        result.warnings.append("aucune modification de session à relire")
        result.duration = time.time() - started
        return result

    available = context.available_personas(repo)
    ctx = router.build_context(diff, repo, project_has_tests(repo))
    route = router.select(ctx, cfg, only=only, add=add, select_all=select_all,
                          available=available)
    result.selected = list(route.selected)
    result.route_explain = route.explain()
    result.scope_authority = context.pick_scope_authority(route.selected, repo)
    route.scope_authority = result.scope_authority

    if dry_run or explain and dry_run:
        result.duration = time.time() - started
        return result

    if not result.selected:
        result.warnings.append("aucun reviewer sélectionné")
        result.duration = time.time() - started
        return result

    try:
        codex.ensure_available(cfg)
    except codex.CodexUnavailable as exc:
        result.codex_error = str(exc)
        result.duration = time.time() - started
        return result

    intent_text = context.load_intent_for(cfg, session_id)
    approved = [f"{d.id} — {d.title} → {d.status}"
                for d in decisions.approved_scope_entries(session_id)]
    schema_text = schema_inline()
    use_schema = capabilities.output_schema_supported(cfg) is not False
    schema_arg = SCHEMA_PATH if (use_schema and SCHEMA_PATH.is_file()) else None

    jobs: List[tuple] = []
    directory = run_dir(run_id)
    ctx_dir = paths.ensure_dir(directory / "context")
    for reviewer in result.selected:
        persona = context.load_persona(reviewer, repo)
        if persona is None:
            result.warnings.append(f"persona introuvable: {reviewer}")
            continue
        is_authority = reviewer == result.scope_authority
        pack = context.build_pack(context.PackInput(
            persona=persona, repo=repo, session_id=session_id,
            diff_text=diff.text, changed_files=list(diff.files),
            intent_text=intent_text if is_authority else "",
            scope_authority=is_authority,
            approved_decisions=approved if is_authority else [],
            previous_findings=previous_findings_digest(session_id, reviewer),
            schema_inline="" if schema_arg else schema_text,
            task_intent=task_intent,
        ))
        try:
            (ctx_dir / f"{reviewer}.md").write_text(pack, encoding="utf-8")
        except OSError:
            pass
        jobs.append((reviewer, pack))

    outcomes = codex.run_many(jobs, cfg, repo, schema_arg,
                              raw_dir=directory / "raw")

    index = 0
    for outcome in outcomes:
        if not outcome.ok:
            result.reviewer_results.append(findings.ReviewerResult(
                reviewer=outcome.reviewer, ok=False,
                error=outcome.error, error_kind=outcome.error_kind,
                duration=outcome.duration))
            continue
        parsed = findings.parse_reviewer_payload(
            outcome.reviewer, outcome.payload, start_index=index)
        parsed.duration = outcome.duration
        index += len(parsed.findings)
        result.reviewer_results.append(parsed)

    merged, _notes = findings.dedupe(
        [r for r in result.reviewer_results if r.ok])
    result.all_findings = merged

    # --- promotion: anything functional leaves Claude's authority ---------
    mode = str(cfg.get("human.scope_changes.mode", "ask"))
    for finding in findings.to_promote(merged):
        decision = decisions.propose(
            session_id,
            title=finding.title,
            why=finding.description,
            alternatives=[finding.suggestion] if finding.suggestion else [],
            blast_radius_paths=[finding.file] if finding.file else [],
            origin=f"codex:{finding.reviewer}",
            from_findings=[finding.id],
            round_no=round_no,
            mode=mode,
        )
        result.promoted[finding.id] = decision.id
        result.opened_decisions.append(decision.id)

    for reviewer_result in result.reviewer_results:
        scope = reviewer_result.scope
        # Only the designated authority's verdict opens decisions: other
        # personas must fill the block (strict schema) but do not assess it.
        if reviewer_result.reviewer != result.scope_authority:
            continue
        if scope is None or not scope.is_change:
            continue
        for change in scope.human_changes():
            decision = decisions.propose(
                session_id,
                title=decisions.make_title(change.get("kind", ""),
                                           change.get("description", "")),
                why=(f"{scope.assessment}\n\nType : {change.get('kind')}\n"
                     f"Constat : {change.get('description', '')}\n"
                     f"Preuve : {change.get('evidence') or '—'}").strip(),
                alternatives=list(change.get("alternatives") or []),
                blast_radius_paths=(
                    [change["evidence"].split(":")[0]]
                    if change.get("evidence") else []),
                origin=f"codex:{scope.reviewer}",
                round_no=round_no,
                mode=mode,
            )
            result.opened_decisions.append(decision.id)

    findings.save_round(session_id, round_no, merged, result.promoted)
    blocking = findings.blocking(merged, str(cfg.get("gate.block_on", "high")))
    result.blocking_ids = [f.id for f in blocking]

    st.round = round_no
    st.last_diff_fingerprint = diff.fingerprint
    st.last_run_id = run_id
    st.budget_spent += time.time() - started
    state.save(st)

    result.duration = time.time() - started
    paths.write_json(directory / "run.json", result.to_dict())
    try:
        (directory / "report.md").write_text(
            report.render_markdown(result, session_id, cfg), encoding="utf-8")
    except OSError:
        pass
    rotate_runs(int(cfg.get("logs.keep_runs", 30)))
    return result
