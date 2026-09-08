"""Orchestration of one review round, and of the loop the rounds form.

Kept out of cli.py so the command layer stays thin and this stays testable.

Round 1
    session diff -> router -> context packs -> codex (bounded pool) -> validate
    -> dedupe -> promote to human decisions -> journal.

Round N > 1
    the same, except the selection comes from ``round2.build()`` instead of the
    router: only the reviewers that a named trigger brings back are packed, and
    only they reach ``codex.run_many``. A reviewer the plan skipped costs no
    ``codex exec`` at all - that is the whole point of the milestone. When the
    plan cannot anchor, the v0.1 router runs over the whole session diff and the
    degradation is on the record (fail-open, I1).

Three limits stop the loop, and none of them is a target:

``max_rounds``   the absolute ceiling. Counted in *exploitable* rounds - the
                 ones H1 lets become an anchor - so a Codex outage never eats
                 the budget. Enforced HERE, not only in the Stop hook: `crux
                 review` is a command Claude can type, and a ceiling only the
                 hook enforces is not a ceiling.
``no_delta``     nothing moved since the last round. Re-running reviewers over
                 identical bytes buys nothing, so the loop ends rather than
                 spending a round to reach the ceiling.
``empty diff``   nothing changed in the session at all.

None of them retires D4: a blocking finding already recorded still owes Claude
an explicit disposition, and that costs no Codex run.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import (baseline, capabilities, codex, context, decisions,
               findings, gitctx, paths, report, round2, state)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "finding.schema.json"

TEST_MARKERS = gitctx.TEST_MARKERS

# ----------------------------------------------------------- stop reasons ---
# Why the loop ended. Empty while it may still continue.
STOP_MAX_ROUNDS = "max_rounds"     # the ceiling; nothing else was even computed
STOP_NO_DELTA = "no_delta"         # nothing moved since the previous round
STOP_EMPTY_DIFF = "empty_diff"     # nothing changed in the session at all
STOP_NO_REVIEWER = "no_reviewer"   # no persona left to ask
STOP_CLEAN = "clean"               # the round closed and nothing is outstanding

STOP_TEXT = {
    STOP_MAX_ROUNDS: "plafond gate.max_rounds atteint",
    STOP_NO_DELTA: "aucun fichier modifié depuis le round précédent",
    STOP_EMPTY_DIFF: "aucune modification de session",
    STOP_NO_REVIEWER: "aucun reviewer sélectionné",
    STOP_CLEAN: "round clos, rien en suspens",
}

# How the reviewers for this round were chosen.
TARGETING_ROUTER = "full_router"   # v0.1: the router over the whole session diff
TARGETING_TARGETED = "targeted"    # 1b's plan over the delta


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
    failed_reviewers: List[str] = field(default_factory=list)
    attempt_status: str = state.ATTEMPT_FAILED
    usable: bool = False
    duration: float = 0.0
    codex_error: Optional[str] = None

    # --- 1c ------------------------------------------------------------------
    stop_reason: str = ""
    targeting: str = ""
    delta_paths: List[str] = field(default_factory=list)
    plan_explain: str = ""
    # Reviewers the targeting spared, each with the named reason it was spared
    # for. `avoided` is the count that matters for the economics: what the v0.1
    # router would have run this round, minus what actually ran.
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    would_run: List[str] = field(default_factory=list)
    executed: List[str] = field(default_factory=list)
    avoided: List[str] = field(default_factory=list)
    override_notes: List[str] = field(default_factory=list)
    insistences: List[str] = field(default_factory=list)
    # Findings whose human decision already exists and was not re-opened.
    duplicate_decisions: Dict[str, str] = field(default_factory=dict)

    @property
    def ran_codex(self) -> bool:
        return bool(self.executed)

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
            "failed_reviewers": self.failed_reviewers,
            "attempt_status": self.attempt_status,
            "usable": self.usable,
            "codex_error": self.codex_error,
            "stop_reason": self.stop_reason,
            "targeting": self.targeting,
            "delta_paths": self.delta_paths,
            "plan_explain": self.plan_explain,
            "skipped": self.skipped,
            "would_run": self.would_run,
            "executed": self.executed,
            "avoided": self.avoided,
            "override_notes": self.override_notes,
            "insistences": self.insistences,
            "duplicate_decisions": self.duplicate_decisions,
        }


# Both moved to modules that do not import `codex`, so the round-2 planner can
# use them. Kept under their original names: this is where they are used from.
project_has_tests = gitctx.project_has_tests


def classify_attempt(reviewer_results: List[findings.ReviewerResult],
                     scope_authority: Optional[str]) -> str:
    """How much of this attempt is usable as an anchor for a later round.

    The scope authority is the pivot: it is the only reviewer that fills the
    `scope` block, so without it the round has no verdict on drift and must not
    become the baseline a targeted round 2 reasons against. Partial answers are
    still archived under the run directory for diagnosis.
    """
    answered = {r.reviewer for r in reviewer_results if r.ok}
    if not answered:
        return state.ATTEMPT_FAILED
    if scope_authority is not None and scope_authority not in answered:
        return state.ATTEMPT_UNUSABLE
    if any(not r.ok for r in reviewer_results):
        return state.ATTEMPT_DEGRADED
    return state.ATTEMPT_SUCCESS


# Moved to `baseline` so the round-2 planner can hash the working tree without
# importing the module that drives Codex. Kept here under its original name:
# this is where the round journal's content map is written from.
content_map = baseline.content_map


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


# ------------------------------------------------- what a reviewer already said ---
_DISPOSITION_TEXT = {
    round2.D_ACCEPTED: "accepté par Claude (corrigé)",
    round2.D_REJECTED: "REJETÉ par Claude",
    round2.D_DEFERRED: "différé par Claude",
    round2.D_UNANSWERED: "SANS RÉPONSE de Claude",
    round2.D_PENDING_HUMAN:
        "promu en décision humaine — EN ATTENTE de l'humain",
    round2.D_HUMAN_SETTLED: "promu en décision humaine — TRANCHÉ par l'humain",
}


def previous_findings_digest(session_id: str, reviewer: str) -> str:
    """What this reviewer said in every earlier round, and what became of it.

    Scoped to the reviewer by the post-dedup ``reviewers`` list, not by a
    substring test on the ``"a+b"`` display string: a finding two personas made
    belongs to both of them, and nothing else does. Each line carries the round
    it came from, so a reviewer looking at round 3 can tell a remark it made
    twice from one it made once.

    Promoted findings appear as what they are - questions that left Claude's
    authority - and never as something to restate. Re-raising them is exactly the
    loop §9 forbids.
    """
    records = findings.load_rounds(session_id)
    if not records:
        return ""
    resolutions = findings.resolutions(session_id)
    status_of = {d.id: d.status for d in _decisions_safe(session_id)}

    lines: List[str] = []
    for record in records:
        disposition = round2.dispositions(record, resolutions, status_of)
        round_no = record.get("round")
        for raw in record.get("findings") or []:
            if not isinstance(raw, dict):
                continue
            owners = [n for n in (raw.get("reviewers") or []) if n]
            if not owners:
                owners = [n for n in str(raw.get("reviewer") or "").split("+") if n]
            if reviewer not in owners:
                continue
            held = disposition.get(raw.get("id") or "", round2.D_UNANSWERED)
            reason = str((resolutions.get(raw.get("id") or "") or {}).get(
                "reason") or "")
            lines.append(
                f"- [round {round_no}] [{raw.get('id')}] {raw.get('title')}"
                f" → {_DISPOSITION_TEXT.get(held, held)}"
                + (f" : {reason}" if reason else ""))
    if not lines:
        return ""
    return (
        "Voici tes remarques des rounds précédents et ce que Claude en a fait.\n"
        "Claude est l'autorité technique dans le périmètre approuvé : un rejet "
        "motivé est une réponse valide.\n"
        "Si tu penses qu'un rejet est une erreur, tu peux le redire UNE fois — "
        "ce sera enregistré comme une insistance, visible et non bloquante. "
        "N'y reviens pas au-delà.\n"
        "Ce qui est passé en décision humaine ne t'appartient plus : ne le "
        "re-signale pas.\n\n" + "\n".join(lines))


def settled_decisions_for(session_id: str, reviewer: str) -> List[str]:
    """Human decisions, already answered, born of THIS reviewer's own findings.

    Facts about the approved scope, sent to the reviewer that raised them so it
    stops treating a settled question as an open one. Never presented as a
    suggestion - see the pack section that renders them.
    """
    settled: List[str] = []
    seen: Set[str] = set()
    ledger = {d.id: d for d in _decisions_safe(session_id)}
    for record in findings.load_rounds(session_id):
        promoted = record.get("promoted") or {}
        for raw in record.get("findings") or []:
            if not isinstance(raw, dict):
                continue
            decision = ledger.get(promoted.get(raw.get("id") or "") or "")
            if decision is None or decision.status == decisions.PENDING:
                continue
            if decision.id in seen:
                continue
            owners = [n for n in (raw.get("reviewers") or []) if n]
            if not owners:
                owners = [n for n in str(raw.get("reviewer") or "").split("+") if n]
            if reviewer not in owners:
                continue
            seen.add(decision.id)
            answer = (decision.answer or {}).get("chosen") or decision.status
            settled.append(f"{decision.id} — {decision.title} → {answer}")
    return settled


def _decisions_safe(session_id: str) -> List[Any]:
    """The ledger, or nothing when it cannot be read.

    ``round2.dispositions`` reads a missing status as ``pending_human``, so an
    unreadable ledger leaves every promoted finding with the human. Fail-closed
    on the decision side (I2), while the review itself still runs (I1).
    """
    try:
        return decisions.load_all(session_id)
    except decisions.DecisionsCorrupt:
        return []


# ----------------------------------------------------------------- selection ---
@dataclass
class Selection:
    """Who runs this round, who does not, and how that was decided."""
    reviewers: List[str] = field(default_factory=list)
    authority: Optional[str] = None
    targeting: str = TARGETING_ROUTER
    delta_paths: List[str] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    would_run: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    override_notes: List[str] = field(default_factory=list)
    plan_explain: str = ""
    router_explain: str = ""
    stop_reason: str = ""


def authority_personas(repo: Path, available: Sequence[str]) -> Set[str]:
    out: Set[str] = set()
    for name in available:
        persona = context.load_persona(name, repo)
        if persona is not None and persona.scope_authority:
            out.add(name)
    return out


def _last_known_authority(session_id: str) -> Optional[str]:
    """The scope authority of the last readable round, for continuity.

    Best effort by design: it feeds ``round2.pick_authority``, which treats an
    unknown previous authority as "no preference" and falls through to its next
    rule. Used on the fallback path, where the plan itself could not anchor and
    therefore reports no authority of its own.
    """
    records = findings.load_rounds(session_id)
    return records[-1].get("scope_authority") if records else None


def plan_selection(session_id: str, repo: Path, cfg, diff, *,
                   available: Sequence[str],
                   only: Optional[Sequence[str]] = None,
                   add: Optional[Sequence[str]] = None,
                   select_all: bool = False) -> Tuple[Selection, Any]:
    """Decide who runs this round. Pure of Codex, and the only place overrides land.

    The order is the point, and it is the same for every round:

        1. the v0.1 router over the whole session diff - always computed, both
           because round 1 *is* that answer and because round N needs it as the
           yardstick for "reviewers avoided by targeting";
        2. the targeted plan, when a round has been recorded and can be anchored;
        3. the overrides, ``--only`` / ``--add`` / ``--all``;
        4. the scope authority, re-asserted last.

    Step 4 is why the overrides are applied here rather than inside
    ``router.select``. ``--only security`` used to *replace* the selection and
    the authority was then re-derived from what survived, handing the role to a
    persona that never declared it. Now the authority is decided before the
    override and put back after it: ``--only security`` on a session whose
    authority is ``code-quality`` runs both, and no flag reachable by Claude can
    produce ``security`` alone.

    ``max_selected`` is applied exactly once, by whichever of the router or the
    plan produced the base selection. Nothing here re-caps: an override widening
    past the cap is the operator asking for it, and the authority coming back is
    an invariant, not a candidate.
    """
    from . import router

    selection = Selection()
    ctx = router.build_context(diff, repo, project_has_tests(repo))
    route = router.select(ctx, cfg, available=available)
    selection.router_explain = route.explain()
    never = set(cfg.get("reviewers.never") or [])
    authorities = authority_personas(repo, available)

    plan = None
    if round2.has_round_history(session_id):
        try:
            plan = round2.build(session_id, repo, cfg,
                                extra_paths=list(diff.files))
        except Exception as exc:      # noqa: BLE001 - I1 over the whole planner
            selection.warnings.append(
                f"planificateur de round ciblé en échec ({exc!r}) : repli sur "
                f"le routeur complet v0.1")
            plan = None

    pool = list(route.candidates)
    if plan is not None:
        pool = [name for name in
                round2.order_candidates(
                    set(route.candidates) | {r.reviewer for r in plan.rows}, cfg)
                if name in available and name not in never]
        selection.plan_explain = plan.explain()
        selection.warnings.extend(plan.warnings)

    if plan is not None and plan.anchored:
        selection.targeting = TARGETING_TARGETED
        selection.delta_paths = list(plan.delta_paths)
        if not plan.delta_paths:
            # A maximum, never a target. Nothing moved since the previous round,
            # so a further round would show every reviewer the bytes it already
            # judged and could only repeat itself. The loop ends here; D4 and the
            # human gate are unaffected, they read local state.
            #
            # This holds for the overrides too. `--all` on an unchanged tree is
            # still a round over identical bytes, and letting a flag reachable by
            # Claude spend one would be the same hole `max_rounds` closes.
            #
            # It also decides the one case §12 leaves open: a secondary reviewer
            # that failed last round is a candidate *for a round that happens*,
            # and no round happens here. Its silence is not thereby read as an
            # approve - the round journal records it under `failed_reviewers`,
            # the round is marked `degraded`, and both the round report and the
            # session summary name the angle that was not covered. An unfilled
            # gap that is stated is not an implicit approval; manufacturing a
            # round over unchanged code to fill it would be.
            selection.stop_reason = STOP_NO_DELTA
            return selection, plan
        base = list(plan.selected_reviewers)
        authority = plan.scope_authority
        selection.skipped = [
            {"reviewer": row.reviewer, "reason": row.exclusion_reason,
             "triggers": list(row.triggers)}
            for row in plan.rows if not row.selected]
    else:
        if plan is not None:
            selection.warnings.append(
                f"repli {plan.fallback or round2.FALLBACK_FULL_ROUTER} : "
                f"routeur complet v0.1 sur le diff de session entier "
                f"(ancrage : {plan.anchor_status})")
        base = list(route.selected)
        authority = round2.pick_authority(
            route.selected, pool, authorities,
            previous_authority=_last_known_authority(session_id))

    reviewers, notes = round2.apply_overrides(
        base, authority=authority, only=only, add=add, select_all=select_all,
        candidates=pool, never=never)
    selection.reviewers = reviewers
    selection.authority = authority
    selection.override_notes = notes

    # The yardstick: what v0.1 would have executed this round, same overrides,
    # same authority guarantee. At round 1 it equals the selection, so nothing is
    # ever reported as avoided when nothing was.
    selection.would_run, _ = round2.apply_overrides(
        route.selected, authority=authority, only=only, add=add,
        select_all=select_all, candidates=pool, never=never)

    # The plan's exclusions describe the plan, not the run. An override - or the
    # authority coming back - can put a reviewer the plan skipped into the
    # selection, and a row left behind here would claim "no Codex call" for a
    # reviewer that is about to make one, and be counted as a saving in the
    # session summary. `skipped` must therefore mean the same thing everywhere:
    # reviewers that really did not run.
    selection.skipped = [row for row in selection.skipped
                         if row["reviewer"] not in set(selection.reviewers)]

    if not selection.reviewers:
        selection.stop_reason = STOP_NO_REVIEWER
    return selection, plan


# --------------------------------------------------------------------- run ---
def run(session_id: str, repo: Path, cfg,
        only: Optional[Sequence[str]] = None,
        add: Optional[Sequence[str]] = None,
        select_all: bool = False,
        task_intent: str = "",
        dry_run: bool = False,
        explain: bool = False) -> RunResult:
    started = time.time()
    st = state.load(session_id)
    round_no = st.round + 1
    run_id = new_run_id(repo)

    result = RunResult(
        run_id=run_id, session_id=session_id, repo=str(repo),
        round=round_no, started_at=state.now_iso())

    # ---- the ceiling, before anything else --------------------------------
    # Ahead of the diff, ahead of the router, ahead of any state write. `crux
    # review` is a command Claude can type at will, and the Stop hook's check
    # only governs the automatic path: a ceiling enforced on one of the two ways
    # in is not a ceiling. Past it the command still answers, still explains
    # itself, and touches nothing - no attempt recorded, no fingerprint moved, no
    # `round-(N+1).json`, no `codex exec`.
    max_rounds = max(1, int(cfg.get("gate.max_rounds", 2)))
    if st.round >= max_rounds:
        result.round = st.round
        result.stop_reason = STOP_MAX_ROUNDS
        result.warnings.append(
            f"gate.max_rounds={max_rounds} atteint ({st.round} round(s) "
            f"exploitable(s) enregistré(s)) : aucune nouvelle review Codex. "
            f"Les findings bloquants déjà enregistrés restent à arbitrer.")
        result.duration = time.time() - started
        return result

    diff = baseline.compute(session_id, repo, cfg)
    result.diff_files = list(diff.files)
    result.diff_added = diff.added
    result.diff_removed = diff.removed
    result.diff_fingerprint = diff.fingerprint
    result.warnings.extend(diff.warnings)

    if diff.is_empty:
        result.stop_reason = STOP_EMPTY_DIFF
        result.warnings.append("aucune modification de session à relire")
        result.duration = time.time() - started
        return result

    available = context.available_personas(repo)
    selection, _plan = plan_selection(
        session_id, repo, cfg, diff, available=available,
        only=only, add=add, select_all=select_all)

    result.selected = list(selection.reviewers)
    result.scope_authority = selection.authority
    result.targeting = selection.targeting
    result.delta_paths = list(selection.delta_paths)
    result.skipped = list(selection.skipped)
    result.would_run = list(selection.would_run)
    result.avoided = [name for name in selection.would_run
                      if name not in selection.reviewers]
    result.override_notes = list(selection.override_notes)
    result.plan_explain = selection.plan_explain
    result.warnings.extend(selection.warnings)
    result.route_explain = _explain(selection)

    if selection.stop_reason:
        result.stop_reason = selection.stop_reason
        result.warnings.append(STOP_TEXT.get(selection.stop_reason,
                                             selection.stop_reason))

    if dry_run or selection.stop_reason:
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

    delta_text = ""
    if selection.delta_paths:
        # Exactly what the pack calls it: the session-baseline diff narrowed to
        # the paths that moved since the previous round. Not a round-to-round
        # patch, and never labelled as one.
        delta_text = baseline.compute(
            session_id, repo, cfg, only_paths=list(selection.delta_paths)).text

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
            round_no=round_no,
            delta_paths=list(selection.delta_paths),
            delta_diff_text=delta_text,
            settled_decisions=settled_decisions_for(session_id, reviewer),
        ))
        try:
            (ctx_dir / f"{reviewer}.md").write_text(pack, encoding="utf-8")
        except OSError:
            pass
        jobs.append((reviewer, pack))

    # The invariant this milestone exists for: `jobs` is the complete list of
    # `codex exec` invocations for this round. A reviewer the plan skipped is not
    # in it, so it costs nothing.
    result.executed = [reviewer for reviewer, _pack in jobs]
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
            outcome.reviewer, outcome.payload, start_index=index,
            round_no=round_no)
        parsed.duration = outcome.duration
        index += len(parsed.findings)
        result.reviewer_results.append(parsed)

    result.failed_reviewers = [r.reviewer for r in result.reviewer_results
                              if not r.ok]
    result.attempt_status = classify_attempt(result.reviewer_results,
                                             result.scope_authority)
    result.usable = result.attempt_status in state.ATTEMPT_USABLE

    merged, _notes = findings.dedupe(
        [r for r in result.reviewer_results if r.ok])

    # --- reiteration: a remark Claude already rejected, said once more -------
    # Matched on the safe identity from 1a and nothing else. An ambiguous key is
    # unmatched, so it stays an ordinary finding. `accepted` reappearing is NOT
    # an insistence - it means the reviewer judges the fix insufficient, and it
    # blocks on its own severity like any other finding.
    findings.mark_reiterations(
        merged, findings.rejected_key_index(session_id, up_to_round=round_no))
    result.all_findings = merged
    result.insistences = [f.id for f in merged if f.insistence]

    if not result.usable:
        # Not an anchor, so nothing is recorded as one: no round, no findings
        # journal, no promotion. Opening a blocking human decision out of an
        # unanchored partial run would turn a technical failure into an
        # obligation, which is exactly what I1 forbids. The raw payloads and the
        # run record stay under ~/.crux/runs/<run_id>/ for diagnosis.
        st.record_attempt(diff.fingerprint, result.attempt_status)
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

    # --- promotion: anything functional leaves Claude's authority ---------
    mode = str(cfg.get("human.scope_changes.mode", "ask"))
    known_keys = decisions.by_finding_key(session_id)
    key_counts = findings.session_key_counts(session_id)
    round_ambiguous = findings.ambiguous_keys(merged)
    for finding in findings.to_promote(merged):
        # Never ask the same question twice. The match must be *provable*: one
        # decision carrying that key, one earlier finding carrying it, and no
        # collision inside this round either. Anything less and a new decision is
        # opened - a duplicate question costs the human a moment, a swallowed one
        # costs them the decision itself.
        existing = known_keys.get(finding.key)
        unambiguous = (finding.key not in round_ambiguous
                       and key_counts.get(finding.key, 0) <= 1)
        if existing is not None and unambiguous:
            result.duplicate_decisions[finding.id] = existing.id
            result.promoted[finding.id] = existing.id
            continue
        decision = decisions.propose(
            session_id,
            title=finding.title,
            why=finding.description,
            alternatives=[finding.suggestion] if finding.suggestion else [],
            blast_radius_paths=[finding.file] if finding.file else [],
            origin=f"codex:{finding.reviewer}",
            from_findings=[finding.id],
            finding_key=finding.key,
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

    findings.save_round(
        session_id, round_no, merged, result.promoted,
        selected=result.selected,
        scope_authority=result.scope_authority,
        failed_reviewers=result.failed_reviewers,
        degraded=result.attempt_status == state.ATTEMPT_DEGRADED,
        diff={"fingerprint": diff.fingerprint,
              "files": content_map(repo, diff.files)},
        skipped=result.skipped,
        delta_paths=result.delta_paths,
        targeting=result.targeting)
    blocking = findings.blocking(merged, str(cfg.get("gate.block_on", "high")))
    result.blocking_ids = [f.id for f in blocking]

    st.record_successful_round(round_no, diff.fingerprint)
    st.record_attempt(diff.fingerprint, result.attempt_status)
    st.last_run_id = run_id
    st.budget_spent += time.time() - started
    state.save(st)

    # Why the loop could end here. The ceiling wins when both apply: it is the
    # binding constraint, and reading `clean` on a session that also ran out of
    # rounds would hide the harder fact. `clean` is a statement about
    # obligations, not about the code - it means nothing is owed by anyone right
    # now, and a further edit still re-opens the loop while rounds remain.
    if st.round >= max_rounds:
        result.stop_reason = STOP_MAX_ROUNDS
    elif not _anything_outstanding(session_id, cfg):
        result.stop_reason = STOP_CLEAN

    result.duration = time.time() - started
    paths.write_json(directory / "run.json", result.to_dict())
    try:
        (directory / "report.md").write_text(
            report.render_markdown(result, session_id, cfg), encoding="utf-8")
    except OSError:
        pass
    rotate_runs(int(cfg.get("logs.keep_runs", 30)))
    return result


def _anything_outstanding(session_id: str, cfg) -> bool:
    """Is anything still owed - by Claude, or by the human?

    Three ledgers, all local, no Codex: blocking findings without a disposition
    (D4), decisions the human has not answered, and an unreadable decision ledger
    - which counts as outstanding, because I2 refuses to read a corrupt ledger as
    "nothing to decide".
    """
    try:
        if decisions.blocking(session_id):
            return True
    except decisions.DecisionsCorrupt:
        return True
    floor = str(cfg.get("gate.block_on", "high"))
    try:
        return bool(findings.unarbitrated(session_id, floor))
    except Exception:      # noqa: BLE001 - I1 over a ledger read
        return False


def _explain(selection: Selection) -> str:
    """One text covering how this round's reviewers were chosen.

    The plan when there is one, the router's own explanation otherwise, plus
    whatever an override changed. Rendered here rather than in `report` so
    `crux review --dry-run --explain` and the archived run record say the same
    thing.
    """
    lines: List[str] = []
    if selection.router_explain:
        lines.append(selection.router_explain)
    if selection.plan_explain:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(selection.plan_explain)
    lines.append("")
    lines.append(f"Sélection retenue : "
                 + (", ".join(selection.reviewers) or "aucune"))
    if selection.authority:
        lines.append(f"Autorité de périmètre : {selection.authority}")
    for note in selection.override_notes:
        lines.append(f"Override : {note}")
    avoided = [name for name in selection.would_run
               if name not in selection.reviewers]
    if avoided:
        lines.append("Évités par ciblage (le routeur v0.1 les aurait lancés) : "
                     + ", ".join(avoided))
    return "\n".join(lines).strip()
