"""Rendering: a compact Markdown report for Claude, JSON for the logs.

Five columns, because there are five outcomes and only three of them are Claude's:
fixed, rejected with a reason, deferred, **decided by the human**, **awaiting the
human**.
"""

from __future__ import annotations

from typing import Dict, List

from . import decisions, findings

SEVERITY_MARK = {
    "critical": "CRITIQUE", "high": "ÉLEVÉ", "medium": "MOYEN", "low": "FAIBLE",
}


def _finding_block(finding: findings.Finding) -> str:
    where = finding.file or "—"
    if finding.line:
        where = f"{where}:{finding.line}"
    lines = [
        f"### [{finding.id}] {SEVERITY_MARK.get(finding.severity, finding.severity)}"
        f" · {finding.title}",
        f"*{finding.reviewer} · {where} · confiance {finding.confidence}*",
        "",
        finding.description,
    ]
    if finding.suggestion:
        lines += ["", f"**Piste :** {finding.suggestion}"]
    if finding.insistence:
        # Visible, and visibly not an obligation. The reviewer is allowed to say
        # it once more; Claude stays the technical authority and already gave a
        # reason. Saying so here is what keeps the insistence honest in both
        # directions - it is neither hidden nor re-armed as a blocker.
        lines += ["", f"**Insistance** — cette remarque reprend "
                      f"{finding.reiterates}, que tu as déjà rejeté avec une "
                      f"raison technique. Elle est enregistrée et NON bloquante. "
                      f"Aucun nouvel arbitrage n'est exigé."]
    return "\n".join(lines)


STOP_EXPLANATION = {
    "max_rounds": (
        "Le plafond `gate.max_rounds` est atteint. Aucune review Codex "
        "supplémentaire ne sera lancée pour cette session — ni par le hook "
        "Stop, ni par `crux review` à la main.\n\n"
        "Ce plafond ne retire aucune obligation déjà ouverte : les findings "
        "bloquants enregistrés restent à arbitrer avec `crux resolve`, et les "
        "décisions humaines en attente restent à poser. Ces deux gestes sont "
        "locaux et n'appellent pas Codex.\n\n"
        "Si l'humain veut réellement plus de rounds, cela se règle en "
        "configuration (`gate.max_rounds`) pour une prochaine session."),
    "no_delta": (
        "Aucun fichier n'a changé depuis le round précédent. Relancer des "
        "reviewers sur des octets identiques ne peut produire que la même "
        "réponse : le plafond de rounds est un maximum, jamais une cible. "
        "La boucle de review s'arrête ici."),
    "empty_diff": "Aucune modification de session à relire.",
    "no_reviewer": "Aucun reviewer n'a pu être sélectionné.",
    "clean": (
        "Le round est clos et rien n'est en suspens : aucun finding bloquant "
        "sans arbitrage, aucune décision humaine en attente. Un nouveau round "
        "ne se déclenchera que si le code bouge encore."),
}


def _render_stop(result) -> str:
    reason = getattr(result, "stop_reason", "")
    text = STOP_EXPLANATION.get(reason)
    if not text:
        return ""
    return f"\n## Fin de la boucle de review — `{reason}`\n{text}"


def render_markdown(result, session_id: str, cfg) -> str:
    out: List[str] = []
    out.append(f"# Crux — review round {result.round}")
    capped_before_anything = (getattr(result, "stop_reason", "") == "max_rounds"
                              and not getattr(result, "executed", None))
    if not capped_before_anything:
        # Omitted past the ceiling, where nothing was computed: the diff and the
        # selection are zero because they were never asked for, and printing
        # "0 fichier(s) · reviewers : aucun" would read as an empty review rather
        # than as a review that did not take place.
        out.append(
            f"{len(result.diff_files)} fichier(s) · +{result.diff_added} "
            f"−{result.diff_removed} · reviewers : "
            f"{', '.join(result.selected) or 'aucun'}"
            + (f" · autorité de périmètre : {result.scope_authority}"
               if result.scope_authority else ""))

    if not getattr(result, "executed", None) and getattr(
            result, "stop_reason", ""):
        # Nothing ran. Say that first and say why, then stop: a report that goes
        # on to list "aucune remarque" after a ceiling reads like a clean review.
        out.append(_render_stop(result))
        out.append(_render_session_block(session_id, cfg, result))
        return "\n".join(part for part in out if part)

    targeting = getattr(result, "targeting", "")
    if targeting == "targeted":
        delta = getattr(result, "delta_paths", []) or []
        out.append(
            f"Round ciblé sur le delta depuis le round {result.round - 1} : "
            f"{len(delta)} fichier(s) — {', '.join(delta[:12])}"
            + ("…" if len(delta) > 12 else ""))
    elif targeting == "full_router":
        out.append("Sélection : routeur v0.1 sur le diff de session entier.")

    avoided = getattr(result, "avoided", []) or []
    if avoided:
        out.append(f"Reviewers évités par ciblage : {', '.join(avoided)}")
    for note in getattr(result, "override_notes", []) or []:
        out.append(f"Override : {note}")

    if result.codex_error:
        out.append("\n## Codex indisponible\n"
                   f"{result.codex_error}\n\n"
                   "La review n'a pas eu lieu. Ce n'est pas bloquant : "
                   "termine normalement.")
        return "\n".join(out)

    if result.warnings:
        out.append("\n## Avertissements")
        out.extend(f"- {w}" for w in result.warnings)

    failed = [r for r in result.reviewer_results if not r.ok]
    if failed:
        out.append("\n## Reviewers en échec")
        for reviewer in failed:
            out.append(f"- **{reviewer.reviewer}** : {reviewer.error} "
                       f"({reviewer.error_kind})")

    if not getattr(result, "usable", True):
        # A technical failure, never a verdict. Saying so plainly matters more
        # than the rest of the report: an unusable attempt that reads like a
        # clean review is worse than no review at all.
        reason = ("aucun reviewer n'a répondu"
                  if result.attempt_status == "failed"
                  else f"l'autorité de périmètre ({result.scope_authority}) "
                       f"n'a pas répondu")
        out.append(
            f"\n## Ce round n'a pas eu lieu\n"
            f"Cause : {reason}. C'est une panne technique, pas un verdict.\n\n"
            f"- le compteur de rounds n'a **pas** avancé ;\n"
            f"- aucun finding n'est enregistré, aucune décision n'est ouverte ;\n"
            f"- les réponses partielles restent dans `~/.crux/runs/"
            f"{result.run_id}/` pour diagnostic ;\n"
            f"- le tour peut se terminer normalement (fail-open) ;\n"
            f"- relance possible à la main avec `crux review`, ou "
            f"automatiquement dès que le diff bouge.")
        return "\n".join(out)

    if result.attempt_status == "degraded" and result.failed_reviewers:
        out.append(
            "\n## Round dégradé\n"
            f"L'autorité de périmètre a répondu, mais "
            f"{', '.join(result.failed_reviewers)} n'a pas abouti. Le round est "
            f"enregistré ; l'angle manquant n'a pas été couvert.")

    for reviewer in result.reviewer_results:
        if reviewer.ok and reviewer.summary:
            out.append(f"\n**{reviewer.reviewer}** ({reviewer.verdict}) : "
                       f"{reviewer.summary}")

    scopes = [r.scope for r in result.reviewer_results if r.ok and r.scope]
    if scopes:
        out.append("\n## Périmètre")
        for scope in scopes:
            marker = {"within_scope": "conforme", "scope_change": "DÉRIVE",
                      "uncertain": "à surveiller"}.get(scope.status, scope.status)
            out.append(f"- **{marker}** ({scope.reviewer}) : {scope.assessment}")

    open_now = decisions.open_decisions(session_id)
    if open_now:
        out.append("\n## ⛔ Décisions humaines en attente — tu ne peux pas les "
                   "trancher")
        for decision in open_now:
            out.append(f"\n### {decision.id} · {decision.title}")
            out.append(decision.why)
            out.append("\nOptions à proposer :")
            for label in decision.option_labels():
                out.append(f"- {label}")
            out.append(
                f"\n**Action requise :** pose la question avec `AskUserQuestion`, "
                f"en mettant `Scope {decision.id}` dans le champ `header`. "
                f"La réponse de l'humain clôt la décision — aucune commande "
                f"`crux` ne le peut.")

    block_on = str(cfg.get("gate.block_on", "high"))
    blocking = findings.blocking(result.all_findings, block_on)
    others = [f for f in result.all_findings
              if f not in blocking and not f.requires_human_decision]

    if blocking:
        out.append(f"\n## Findings bloquants (≥ {block_on}) — à arbitrer")
        out.append("Pour chacun : vérifie dans le code, puis "
                   "`crux resolve --id <ID> --status accepted|rejected|deferred "
                   "--reason \"...\"`.")
        for finding in blocking:
            out.append("\n" + _finding_block(finding))

    if others:
        out.append("\n## Findings non bloquants — à ton jugement")
        for finding in others:
            out.append("\n" + _finding_block(finding))

    if not result.all_findings and not open_now and not failed:
        out.append("\nAucune remarque. Un rapport vide est un résultat valide.")

    skipped = getattr(result, "skipped", []) or []
    if skipped:
        out.append("\n## Reviewers non relancés (aucun appel Codex)")
        for row in skipped:
            detail = row.get("reason") or "—"
            if row.get("triggers"):
                detail += " (" + ", ".join(row["triggers"]) + ")"
            out.append(f"- **{row.get('reviewer')}** : {detail}")

    duplicates = getattr(result, "duplicate_decisions", {}) or {}
    if duplicates:
        out.append("\n## Décisions humaines déjà tranchées, non rouvertes")
        out.append("Ces remarques sont revenues, mais elles portent la même "
                   "identité sûre qu'une décision déjà ouverte : la question "
                   "n'est pas reposée à l'humain.")
        for finding_id, decision_id in sorted(duplicates.items()):
            out.append(f"- {finding_id} → {decision_id}")

    out.append(_render_stop(result))
    out.append(_render_session_block(session_id, cfg, result))
    return "\n".join(part for part in out if part)


def _render_session_block(session_id: str, cfg, result=None) -> str:
    try:
        return "\n" + render_session_summary(
            session_id, cfg, stop_reason=getattr(result, "stop_reason", ""))
    except Exception:      # noqa: BLE001 - a report must never fail the run
        return ""


# --------------------------------------------------------- session summary ---
def render_session_summary(session_id: str, cfg,
                           stop_reason: str = "") -> str:
    """Everything the loop did, session-wide, in one block.

    Written for two readers. Claude, to know what is still owed: which findings
    have no disposition, which decisions are still the human's. And the
    benchmark, to compare a run against pre-1c behaviour without any dedicated
    instrumentation - rounds, reviewers executed per round, reviewers avoided by
    targeting, findings by disposition, human decisions.

    Deliberately not token accounting. `reviewer executions` is a count of
    `codex exec` invocations, which is the unit this milestone actually controls.
    """
    from . import findings, round2, state

    rounds = findings.load_rounds(session_id)
    resolutions = findings.resolutions(session_id)
    try:
        ledger = decisions.load_all(session_id)
    except decisions.DecisionsCorrupt:
        ledger = []
    status_of = {d.id: d.status for d in ledger}
    st = state.load(session_id)

    lines = ["## Bilan de session"]
    if stop_reason:
        lines.append(f"raison de fin : {stop_reason}")
    lines.append(f"rounds exploitables : {st.round} / "
                 f"{cfg.get('gate.max_rounds')}")

    executions = 0
    avoided_total = 0
    tally: Dict[str, int] = {}
    insistences: List[str] = []
    for record in rounds:
        selected = list(record.get("selected") or [])
        skipped = [row.get("reviewer") for row in (record.get("skipped") or [])
                   if isinstance(row, dict)]
        executions += len(selected)
        avoided_total += len(skipped)
        detail = f"round {record.get('round')} · {', '.join(selected) or 'aucun'}"
        if record.get("failed_reviewers"):
            detail += f" · en échec : {', '.join(record['failed_reviewers'])}"
        if skipped:
            detail += f" · non relancés : {', '.join(str(s) for s in skipped)}"
        if record.get("targeting"):
            detail += f" · {record['targeting']}"
        lines.append(f"  {detail}")

        disposition = round2.dispositions(record, resolutions, status_of)
        for raw in record.get("findings") or []:
            if not isinstance(raw, dict):
                continue
            held = disposition.get(raw.get("id") or "", round2.D_UNANSWERED)
            tally[held] = tally.get(held, 0) + 1
            if raw.get("insistence"):
                insistences.append(str(raw.get("id")))

    lines.append("")
    lines.append(f"reviewer executions: {executions}")
    lines.append(f"reviewer executions avoided by targeting: {avoided_total}")
    lines.append("")
    total = sum(tally.values())
    lines.append(f"findings : {total}")
    for name in (round2.D_ACCEPTED, round2.D_REJECTED, round2.D_DEFERRED,
                 round2.D_UNANSWERED, round2.D_PENDING_HUMAN,
                 round2.D_HUMAN_SETTLED):
        if tally.get(name):
            lines.append(f"  {name} : {tally[name]}")
    if insistences:
        lines.append(f"  insistances (non bloquantes) : {', '.join(insistences)}")

    human = [d for d in ledger if d.status != decisions.WITHDRAWN]
    lines.append(f"décisions humaines : {len(human)}")
    for decision in human:
        lines.append(f"  {decision.id} · {decision.status} · {decision.title}")
    return "\n".join(lines)


def render_status(session_id: str, cfg, gate, repo, st, caps) -> str:
    lines: List[str] = []
    lines.append(f"gate            {gate.mode}   (source : {gate.source})")
    lines.append(f"dépôt           {repo if repo else '— hors dépôt git'}")
    lines.append(f"config projet   {cfg.project_config or '— aucune'}")
    lines.append(f"autorité        questions={cfg.get('human.questions.mode')} · "
                 f"scope_changes={cfg.get('human.scope_changes.mode')}")
    lines.append(f"session         {session_id or '—'}")
    lines.append(f"round           {st.round} / {cfg.get('gate.max_rounds')}")
    lines.append(f"dernier run     {st.last_run_id or '—'}")
    codex_version = (caps or {}).get("codex_version")
    lines.append(f"codex           {codex_version or 'introuvable'}")
    return "\n".join(lines)


def render_decisions(session_id: str) -> str:
    rows = decisions.load_all(session_id)
    if not rows:
        return "Aucune décision enregistrée."
    lines = []
    for decision in rows:
        flag = " ⚠ provenance dégradée" if decision.degraded_provenance else ""
        lines.append(f"{decision.id}  {decision.status:<20} {decision.title}{flag}")
        if decision.answer and decision.answer.get("chosen"):
            lines.append(f"      réponse : {decision.answer['chosen']}")
    return "\n".join(lines)
