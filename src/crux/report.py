"""Rendering: a compact Markdown report for Claude, JSON for the logs.

Five columns, because there are five outcomes and only three of them are Claude's:
fixed, rejected with a reason, deferred, **decided by the human**, **awaiting the
human**.
"""

from __future__ import annotations

from typing import List

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
    return "\n".join(lines)


def render_markdown(result, session_id: str, cfg) -> str:
    out: List[str] = []
    out.append(f"# Crux — review round {result.round}")
    out.append(
        f"{len(result.diff_files)} fichier(s) · +{result.diff_added} "
        f"−{result.diff_removed} · reviewers : "
        f"{', '.join(result.selected) or 'aucun'}"
        + (f" · autorité de périmètre : {result.scope_authority}"
           if result.scope_authority else ""))

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

    return "\n".join(out)


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
