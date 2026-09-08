"""Command layer. Thin on purpose: every decision lives in a module.

Exit codes
  0  nothing blocking      3  Codex unavailable
  1  blocking findings     4  invalid git context
  2  usage error           5  invalid configuration
                           6  a human decision is pending
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__

EXIT_OK = 0
EXIT_BLOCKING = 1
EXIT_USAGE = 2
EXIT_NO_CODEX = 3
EXIT_NO_GIT = 4
EXIT_BAD_CONFIG = 5
EXIT_PENDING_HUMAN = 6


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _session(args, repo: Optional[Path] = None, required: bool = True) -> str:
    """Resolve the session deterministically, or exit with an explanation.

    `default` is never a silent fallback: a command run from inside an armed
    session must never write to a different session than the hooks enforce.
    """
    from . import gitctx, state
    if repo is None:
        cwd = Path.cwd()
        repo = gitctx.repo_root(cwd) if gitctx.is_repo(cwd) else None
    try:
        return state.resolve_session(getattr(args, "session", None), repo,
                                     required=required)
    except state.SessionResolutionError as exc:
        _err(f"session indéterminable : {exc}")
        raise SystemExit(EXIT_USAGE)


def _repo_or_exit(cwd: Path):
    from . import gitctx
    if not gitctx.is_repo(cwd):
        _err(f"{cwd} n'est pas dans un dépôt git. Crux exige un dépôt git.")
        raise SystemExit(EXIT_NO_GIT)
    return gitctx.repo_root(cwd)


def _config_or_exit(cwd: Path):
    from . import config
    try:
        return config.load(cwd)
    except config.ConfigError as exc:
        _err(f"configuration invalide : {exc}")
        raise SystemExit(EXIT_BAD_CONFIG)


# ----------------------------------------------------------------- review ---
def cmd_review(args) -> int:
    from . import codex, decisions, report, review
    cwd = Path.cwd()
    cfg = _config_or_exit(cwd)
    repo = _repo_or_exit(cwd)
    session = _session(args)

    only = args.only.split(",") if args.only else None
    if args.reviewer:
        only = (only or []) + [args.reviewer]
    add = args.add.split(",") if args.add else None

    if args.explain and args.dry_run:
        result = review.run(session, repo, cfg, only=only, add=add,
                            select_all=args.all, dry_run=True)
        _out(result.route_explain)
        return EXIT_OK

    try:
        result = review.run(session, repo, cfg, only=only, add=add,
                            select_all=args.all, task_intent=args.intent or "",
                            dry_run=args.dry_run)
    except codex.CodexUnavailable as exc:
        _err(str(exc))
        return EXIT_NO_CODEX

    if args.json:
        import json
        _out(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _out(report.render_markdown(result, session, cfg))
        if args.explain:
            _out("\n---\n" + result.route_explain)

    if result.codex_error:
        return EXIT_NO_CODEX
    if decisions.blocking(session):
        return EXIT_PENDING_HUMAN
    # A ceiling is not a failure and not a verdict: nothing ran, nothing is
    # blocking *from this run*, and the report has already said why. Obligations
    # that predate it - unarbitrated findings, pending decisions - are enforced
    # where they always were, by the Stop hook, and the two lines above still
    # apply here. Inventing a new exit code for "capped" would break every
    # caller that reads 0 as "the turn may end".
    return EXIT_BLOCKING if result.blocking_ids else EXIT_OK


def cmd_route(args) -> int:
    """Explain the selection. Never runs Codex, in either form.

    Plain `crux route` is the v0.1 answer: the router over the whole session
    diff. `--explain` adds the round-2 plan once a round has been recorded -
    which reviewers a second round would re-run, under which trigger, and which
    are skipped for which reason. It is a projection, not an execution: nothing
    here selects reviewers for `review.run()`.
    """
    from . import baseline, gitctx, round2, router, context
    cwd = Path.cwd()
    cfg = _config_or_exit(cwd)
    repo = _repo_or_exit(cwd)
    session = _session(args)
    diff = baseline.compute(session, repo, cfg)
    if diff.is_empty:
        _out("Aucune modification de session : aucun reviewer à sélectionner.")
    else:
        ctx = router.build_context(diff, repo, gitctx.project_has_tests(repo))
        result = router.select(ctx, cfg,
                               available=context.available_personas(repo))
        result.scope_authority = context.pick_scope_authority(
            result.selected, repo)
        _out(result.explain())

    # Deliberately outside the branch above. An empty *session* diff is not an
    # empty *delta*: reverting a file to its baseline after a round leaves
    # nothing to review against the baseline while being a real change against
    # the round. Returning early there hid the plan exactly when it had
    # something to say.
    if args.explain and round2.has_round_history(session):
        plan = round2.build(session, repo, cfg, extra_paths=list(diff.files))
        if not diff.is_empty:
            _out("")
            _out("---")
        _out("")
        _out(plan.explain())
    return EXIT_OK


# -------------------------------------------------------------- resolutions ---
def cmd_resolve(args) -> int:
    from . import findings
    session = _session(args)
    try:
        entry = findings.resolve(session, args.id, args.status,
                                 args.reason or "")
    except findings.PromotedFinding as exc:
        _err(str(exc))
        return EXIT_PENDING_HUMAN
    except findings.AmbiguousFinding as exc:
        _err(str(exc))
        return EXIT_USAGE
    except KeyError:
        _err(f"finding inconnu : {args.id}")
        return EXIT_USAGE
    except ValueError as exc:
        _err(str(exc))
        return EXIT_USAGE
    _out(f"{entry['id']} → {entry['status']}")
    return EXIT_OK


# ---------------------------------------------------------------- decisions ---
def cmd_decision_propose(args) -> int:
    from . import decisions
    cwd = Path.cwd()
    cfg = _config_or_exit(cwd)
    session = _session(args)
    decision = decisions.propose(
        session,
        title=args.title,
        why=args.why,
        alternatives=list(args.alt or []),
        approve_options=list(args.approve_option or []),
        reject_options=list(args.reject_option or []),
        blast_radius_paths=(args.files.split(",") if args.files else []),
        from_findings=([args.from_finding] if args.from_finding else []),
        mode=str(cfg.get("human.scope_changes.mode", "ask")),
    )
    _out(f"{decision.id}  [{decision.status}]")
    if decision.status == decisions.PENDING:
        payload = decisions.question_payload(decision)
        _out("")
        _out("Pose maintenant cette question avec AskUserQuestion :")
        _out(f"  header   : {payload['header']}")
        _out(f"  question : {payload['question']}")
        _out("")
        _out("  Options — reprends ces libellés EXACTEMENT, sans les reformuler :")
        for option, meta in zip(payload["options"],
                                decision.structured_options()):
            effect = ("ACCEPTE l'évolution"
                      if meta["action"] == decisions.ACTION_APPROVE
                      else "REFUSE l'évolution")
            _out(f"    · {option['label']}")
            _out(f"        → {effect}")
        _out("")
        _out("Le statut vient de l'option choisie, jamais de sa formulation. "
             "Un libellé modifié ne correspondra à aucune option et la décision "
             "restera ouverte.")
        _out("La réponse de l'utilisateur clôt la décision. Aucune commande "
             "crux ne le peut.")
    return EXIT_OK


def cmd_decision_list(args) -> int:
    from . import decisions, report
    session = _session(args)
    try:
        if args.open:
            _out(decisions.render_open(session))
        else:
            _out(report.render_decisions(session))
    except decisions.DecisionsCorrupt as exc:
        _err(str(exc))
        return EXIT_PENDING_HUMAN
    return EXIT_PENDING_HUMAN if decisions.blocking(session) else EXIT_OK


def cmd_decision_show(args) -> int:
    from . import decisions
    import json
    session = _session(args)
    decision = decisions.get(session, args.id)
    if decision is None:
        _err(f"décision inconnue : {args.id}")
        return EXIT_USAGE
    _out(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    if decision.is_blocking:
        _out("")
        _out("Question à poser (AskUserQuestion) :")
        _out(json.dumps(decisions.question_payload(decision),
                        ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_decision_withdraw(args) -> int:
    from . import decisions
    session = _session(args)
    try:
        decision = decisions.withdraw(session, args.id, args.reason or "")
    except (KeyError, ValueError, decisions.ProvenanceError) as exc:
        _err(str(exc))
        return EXIT_USAGE
    _out(f"{decision.id} → {decision.status}")
    return EXIT_OK


def cmd_decision_resolve(args) -> int:
    """Manual fallback, for the human at a real terminal.

    Refuses without a TTY on both stdin and stdout.  An agent's Bash tool
    captures its streams and therefore cannot use this path; a `deny` rule in the
    arming file closes it a second time.
    """
    from . import decisions
    session = _session(args)
    stdin_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
    stdout_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    try:
        decision = decisions.resolve_manually(
            session, args.id, approve=args.approve,
            answer_text=args.answer or "", confirm_id=args.confirm or "",
            stdin_is_tty=stdin_tty, stdout_is_tty=stdout_tty)
    except decisions.ProvenanceError as exc:
        _err(str(exc))
        return EXIT_PENDING_HUMAN
    except KeyError:
        _err(f"décision inconnue : {args.id}")
        return EXIT_USAGE
    _out(f"{decision.id} → {decision.status}")
    return EXIT_OK


# ------------------------------------------------------------------ intent ---
def cmd_intent_show(args) -> int:
    from . import intent
    cwd = Path.cwd()
    cfg = _config_or_exit(cwd)
    session = _session(args)
    budget = args.budget or int(cfg.get("human.intent.max_chars", 8000))
    text = intent.render(session, max_chars=budget)
    _out(text or "Périmètre approuvé vide (aucun prompt journalisé).")
    return EXIT_OK


# ------------------------------------------------------------------- state ---
def cmd_status(args) -> int:
    from . import capabilities, config, decisions, gitctx, report, state
    cwd = Path.cwd()
    cfg = _config_or_exit(cwd)
    # Diagnostic command: it must still answer when no session can be resolved,
    # and say so, rather than refuse.
    session = _session(args, required=False)
    repo = gitctx.repo_root(cwd) if gitctx.is_repo(cwd) else None
    gate = state.resolve_gate(cfg, session)
    st = state.load(session)
    caps = capabilities.get(cfg)
    _out(report.render_status(session, cfg, gate, repo, st, caps))
    try:
        pending = decisions.blocking(session)
    except decisions.DecisionsCorrupt as exc:
        _out(f"décisions       ILLISIBLES — {exc}")
        return EXIT_PENDING_HUMAN
    _out(f"décisions       {len(pending)} en attente")
    if pending:
        for decision in pending:
            _out(f"                {decision.id} · {decision.title}")
        return EXIT_PENDING_HUMAN
    return EXIT_OK


def cmd_config(args) -> int:
    import json
    cwd = Path.cwd()
    cfg = _config_or_exit(cwd)
    if args.explain:
        flat = sorted(cfg.provenance.items())
        for key, source in flat:
            value = cfg.get(key)
            _out(f"{key:<42} {str(value):<28} {source}")
    else:
        _out(json.dumps(cfg.as_dict(), ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_on(args) -> int:
    from . import baseline, state
    cwd = Path.cwd()
    cfg = _config_or_exit(cwd)
    repo = _repo_or_exit(cwd)
    session = _session(args, repo=repo)
    state.set_override(session, args.mode)
    try:
        baseline.capture(session, repo, cfg)
    except Exception as exc:
        _err(f"avertissement : capture de baseline impossible ({exc})")
    _out(f"gate armé pour cette session : {args.mode}")
    return EXIT_OK


def cmd_off(args) -> int:
    from . import decisions, state
    session = _session(args)
    state.set_override(session, "off")
    _out("gate désarmé pour cette session.")
    pending = decisions.blocking(session)
    if pending:
        _out("")
        _out("Décisions toujours en attente (désarmer ne les referme pas) :")
        for decision in pending:
            _out(f"  {decision.id} · {decision.title}")
        return EXIT_PENDING_HUMAN
    return EXIT_OK


def cmd_personas(args) -> int:
    from . import context, gitctx
    cwd = Path.cwd()
    repo = gitctx.repo_root(cwd) if gitctx.is_repo(cwd) else None
    for name in context.available_personas(repo):
        persona = context.load_persona(name, repo)
        if persona is None:
            continue
        flag = " [autorité de périmètre]" if persona.scope_authority else ""
        _out(f"{name:<16} {persona.title}{flag}")
        _out(f"{'':<16} source: {persona.source}")
    return EXIT_OK


def cmd_hook(args) -> int:
    from . import hooks
    return hooks.dispatch(args.event)


def cmd_doctor(args) -> int:
    from . import doctor
    return doctor.run(probe=args.probe)


def cmd_init(args) -> int:
    from . import initcmd
    return initcmd.run(force=args.force, yes=args.yes)


def cmd_setup(args) -> int:
    from . import setupcmd
    report_data = setupcmd.run_setup(install_plugin_too=not args.no_plugin)
    for key, value in report_data.items():
        if isinstance(value, list):
            for item in value:
                _out(f"{key:<14} {item}")
        else:
            _out(f"{key:<14} {value}")
    _out("")
    _out("Lancez `crux doctor` pour vérifier l'installation.")
    return EXIT_OK


def cmd_uninstall(args) -> int:
    from . import setupcmd
    result = setupcmd.uninstall(keep_data=not args.purge)
    for path in result["removed"]:      # type: ignore[index]
        _out(f"supprimé  {path}")
    for path in result["kept"]:         # type: ignore[index]
        _out(f"conservé  {path}")
    return EXIT_OK


def cmd_report(args) -> int:
    from . import paths, state
    session = _session(args, required=False)
    if getattr(args, "summary", False):
        from . import report
        cwd = Path.cwd()
        _out(report.render_session_summary(session, _config_or_exit(cwd)))
        return EXIT_OK
    run_id = args.run or state.load(session).last_run_id
    if not run_id:
        _err("aucun run enregistré pour cette session.")
        return EXIT_USAGE
    directory = paths.runs_root() / run_id
    name = "run.json" if args.format == "json" else "report.md"
    candidate = directory / name
    if not candidate.is_file():
        _err(f"introuvable : {candidate}")
        return EXIT_USAGE
    _out(candidate.read_text(encoding="utf-8"))
    return EXIT_OK


# ------------------------------------------------------------------ parser ---
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crux",
        description="Review croisée Claude Code × Codex CLI, sous autorité humaine.")
    parser.add_argument("--version", action="version",
                        version=f"crux {__version__}")
    parser.add_argument("--session", help="identifiant de session Claude Code")
    sub = parser.add_subparsers(dest="command", required=True)

    review_p = sub.add_parser("review", help="relire le diff de session")
    review_p.add_argument("reviewer", nargs="?",
                          help="raccourci pour --only <reviewer>")
    review_p.add_argument("--session")
    review_p.add_argument("--only", help="liste de reviewers, séparés par des ,")
    review_p.add_argument("--add", help="reviewers supplémentaires")
    review_p.add_argument("--all", action="store_true")
    review_p.add_argument("--intent", help="résumé de la demande utilisateur")
    review_p.add_argument("--json", action="store_true")
    review_p.add_argument("--dry-run", action="store_true")
    review_p.add_argument("--explain", action="store_true")
    review_p.set_defaults(func=cmd_review)

    route_p = sub.add_parser("route", help="expliquer la sélection des reviewers")
    route_p.add_argument("--session")
    route_p.add_argument(
        "--explain", action="store_true",
        help="ajoute le plan du round suivant (déclencheurs par reviewer) "
             "quand un round a déjà été enregistré")
    route_p.set_defaults(func=cmd_route)

    resolve_p = sub.add_parser("resolve", help="arbitrage technique d'un finding")
    resolve_p.add_argument("--session")
    resolve_p.add_argument(
        "--id", required=True,
        help="identifiant du finding, par ex. R1F3 (la forme courte F3 est "
             "acceptée tant qu'elle ne désigne qu'un seul finding)")
    resolve_p.add_argument("--status", required=True,
                           choices=["accepted", "rejected", "deferred"])
    resolve_p.add_argument("--reason", default="")
    resolve_p.set_defaults(func=cmd_resolve)

    decision_p = sub.add_parser("decision", help="décisions humaines")
    decision_sub = decision_p.add_subparsers(dest="decision_command", required=True)

    propose_p = decision_sub.add_parser(
        "propose", help="ouvrir une décision (ne la clôt pas)")
    propose_p.add_argument("--session")
    propose_p.add_argument("--title", required=True)
    propose_p.add_argument("--why", required=True)
    propose_p.add_argument(
        "--alt", action="append", default=[],
        help="piste indicative affichée dans la question (PAS une option : "
             "son effet sur le périmètre est inconnu)")
    propose_p.add_argument(
        "--approve-option", dest="approve_option", action="append", default=[],
        help="libellé d'option supplémentaire signifiant ACCEPTER l'évolution")
    propose_p.add_argument(
        "--reject-option", dest="reject_option", action="append", default=[],
        help="libellé d'option supplémentaire signifiant REFUSER l'évolution")
    propose_p.add_argument("--files", default="")
    propose_p.add_argument("--from-finding", dest="from_finding", default="")
    propose_p.set_defaults(func=cmd_decision_propose)

    list_p = decision_sub.add_parser("list", help="lister les décisions")
    list_p.add_argument("--session")
    list_p.add_argument("--open", action="store_true")
    list_p.set_defaults(func=cmd_decision_list)

    show_p = decision_sub.add_parser("show", help="détail d'une décision")
    show_p.add_argument("--session")
    show_p.add_argument("--id", required=True)
    show_p.set_defaults(func=cmd_decision_show)

    withdraw_p = decision_sub.add_parser(
        "withdraw", help="retirer une décision devenue sans objet")
    withdraw_p.add_argument("--session")
    withdraw_p.add_argument("--id", required=True)
    withdraw_p.add_argument("--reason", required=True)
    withdraw_p.set_defaults(func=cmd_decision_withdraw)

    # Manual fallback. No --by flag exists anywhere in this CLI: a human status
    # cannot be requested, only obtained (see decisions.resolve_manually).
    dresolve_p = decision_sub.add_parser(
        "resolve",
        help="REPLI MANUEL — exige un terminal interactif ; refusé à un agent")
    dresolve_p.add_argument("--session")
    dresolve_p.add_argument("--id", required=True)
    dresolve_p.add_argument("--confirm", required=True,
                            help="retapez l'identifiant de la décision")
    group = dresolve_p.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true")
    group.add_argument("--reject", dest="approve", action="store_false")
    dresolve_p.add_argument("--answer", default="")
    dresolve_p.set_defaults(func=cmd_decision_resolve)

    intent_p = sub.add_parser("intent", help="périmètre approuvé")
    intent_sub = intent_p.add_subparsers(dest="intent_command", required=True)
    ishow_p = intent_sub.add_parser("show")
    ishow_p.add_argument("--session")
    ishow_p.add_argument("--budget", type=int)
    ishow_p.set_defaults(func=cmd_intent_show)

    status_p = sub.add_parser("status", help="état résolu du gate et de la session")
    status_p.add_argument("--session")
    status_p.set_defaults(func=cmd_status)

    config_p = sub.add_parser("config", help="configuration fusionnée")
    config_p.add_argument("--explain", action="store_true")
    config_p.set_defaults(func=cmd_config)

    on_p = sub.add_parser("on", help="armer le gate pour cette session")
    on_p.add_argument("--session")
    on_p.add_argument("mode", nargs="?", default="code",
                      choices=["code", "plan", "both"])
    on_p.set_defaults(func=cmd_on)

    off_p = sub.add_parser("off", help="désarmer le gate pour cette session")
    off_p.add_argument("--session")
    off_p.set_defaults(func=cmd_off)

    personas_p = sub.add_parser("personas", help="lister les personas résolus")
    personas_p.set_defaults(func=cmd_personas)

    report_p = sub.add_parser("report", help="rapport d'un run")
    report_p.add_argument("--session")
    report_p.add_argument("--run")
    report_p.add_argument("--format", choices=["md", "json"], default="md")
    report_p.add_argument(
        "--summary", action="store_true",
        help="bilan de session : rounds, reviewers exécutés et évités, "
             "findings, décisions humaines")
    report_p.set_defaults(func=cmd_report)

    doctor_p = sub.add_parser("doctor", help="diagnostic complet")
    doctor_p.add_argument("--probe", action="store_true",
                          help="exécute réellement les sondes de capacité")
    doctor_p.set_defaults(func=cmd_doctor)

    init_p = sub.add_parser("init", help="créer .crux.yml dans ce projet")
    init_p.add_argument("--force", action="store_true")
    init_p.add_argument("--yes", "-y", action="store_true")
    init_p.set_defaults(func=cmd_init)

    setup_p = sub.add_parser("setup", help="installer le plugin, shims, réglages")
    setup_p.add_argument("--no-plugin", action="store_true")
    setup_p.set_defaults(func=cmd_setup)

    uninstall_p = sub.add_parser("uninstall", help="désinstaller proprement")
    uninstall_p.add_argument("--purge", action="store_true",
                             help="supprime aussi ~/.crux")
    uninstall_p.set_defaults(func=cmd_uninstall)

    hook_p = sub.add_parser("hook", help="réservé à Claude Code")
    hook_p.add_argument("event", choices=sorted(
        ["session-start", "session-end", "user-prompt", "post-edit",
         "post-ask", "stop"]))
    hook_p.set_defaults(func=cmd_hook)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")     # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except SystemExit as exc:
        return int(exc.code or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
