"""Personas and the per-reviewer context pack.

Codex runs with ``--cd <repo> --sandbox read-only``, so a reviewer can open the
files it needs itself.  We therefore ship the diff, the intent and *pointers* -
not the repository.  That is what keeps a 200k-line project inside a 30k-character
prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import intent, paths

PACKAGE_PERSONAS = Path(__file__).resolve().parent / "personas"
BASE_PERSONA = "_base"

SECRET_PATTERNS = [
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})"),
    re.compile(r"\b(ghp_[A-Za-z0-9]{20,})"),
    re.compile(r"\b(gho_[A-Za-z0-9]{20,})"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*"
               r"[\"']?([A-Za-z0-9/+_\-]{24,})[\"']?"),
]


@dataclass
class Persona:
    name: str
    title: str
    body: str
    min_severity: str = "medium"
    scope_authority: bool = False
    context: List[str] = field(default_factory=lambda: ["diff", "project_docs"])
    escalate_on: List[str] = field(default_factory=list)
    source: Optional[Path] = None

    def wants(self, section: str) -> bool:
        return section in self.context


def _parse_frontmatter(text: str) -> (Dict[str, Any], str):
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, parts[2].lstrip("\n")


def persona_search_paths(name: str, repo: Optional[Path]) -> List[Path]:
    """Project override, then user override, then the shipped one. First wins."""
    candidates: List[Path] = []
    if repo is not None:
        candidates.append(repo / ".crux" / "personas" / f"{name}.md")
    candidates.append(paths.personas_user_dir() / f"{name}.md")
    candidates.append(PACKAGE_PERSONAS / f"{name}.md")
    return candidates


def load_persona(name: str, repo: Optional[Path] = None) -> Optional[Persona]:
    for candidate in persona_search_paths(name, repo):
        if not candidate.is_file():
            continue
        meta, body = _parse_frontmatter(candidate.read_text(encoding="utf-8"))
        return Persona(
            name=str(meta.get("name") or name),
            title=str(meta.get("title") or name),
            body=body.strip(),
            min_severity=str(meta.get("min_severity") or "medium"),
            scope_authority=bool(meta.get("scope_authority")),
            context=list(meta.get("context") or ["diff", "project_docs"]),
            escalate_on=list(meta.get("escalate_on") or []),
            source=candidate,
        )
    return None


def available_personas(repo: Optional[Path] = None) -> List[str]:
    names = set()
    for directory in (PACKAGE_PERSONAS, paths.personas_user_dir(),
                      (repo / ".crux" / "personas") if repo else None):
        if directory is None or not directory.is_dir():
            continue
        for path in directory.glob("*.md"):
            if path.stem != BASE_PERSONA:
                names.add(path.stem)
    return sorted(names)


def base_preamble(repo: Optional[Path] = None) -> str:
    persona = load_persona(BASE_PERSONA, repo)
    return persona.body if persona else ""


def pick_scope_authority(selected: List[str], repo: Optional[Path]) -> Optional[str]:
    """The reviewer that must fill the `scope` block.

    Never left unassigned: if the declared authority is not selected, the first
    selected reviewer inherits it, and `crux status` says so.
    """
    for name in selected:
        persona = load_persona(name, repo)
        if persona and persona.scope_authority:
            return name
    return selected[0] if selected else None


def redact(text: str) -> str:
    out = text
    for pattern in SECRET_PATTERNS:
        def _mask(match: "re.Match") -> str:
            whole = match.group(0)
            secret = match.group(match.lastindex or 0)
            return whole.replace(secret, "<redacted>")
        out = pattern.sub(_mask, out)
    return out


def _read_head(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:max_chars]


def repo_tree(repo: Path, depth: int = 3, limit: int = 200) -> str:
    from . import gitctx
    proc = gitctx.run(repo, "ls-files", check=False)
    if proc.returncode != 0:
        return ""
    dirs = set()
    files = []
    for line in proc.stdout.splitlines():
        parts = line.split("/")
        if len(parts) <= depth:
            files.append(line)
        for i in range(1, min(depth, len(parts))):
            dirs.add("/".join(parts[:i]) + "/")
    entries = sorted(dirs) + sorted(files)
    if len(entries) > limit:
        entries = entries[:limit] + [f"… {len(entries) - limit} entrées de plus"]
    return "\n".join(entries)


def project_docs(repo: Path, max_chars: int = 3000) -> str:
    chunks = []
    for name in ("CLAUDE.md", "AGENTS.md", "README.md"):
        candidate = repo / name
        if candidate.is_file():
            head = _read_head(candidate, max_chars // 2)
            if head.strip():
                chunks.append(f"--- {name} (début) ---\n{head}")
    return "\n\n".join(chunks)[:max_chars]


def dependency_manifest(repo: Path, max_chars: int = 2500) -> str:
    for name in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod",
                 "requirements.txt"):
        candidate = repo / name
        if candidate.is_file():
            return f"--- {name} ---\n{_read_head(candidate, max_chars)}"
    return ""


def full_files(repo: Path, relpaths: List[str], max_chars: int = 12000) -> str:
    chunks: List[str] = []
    budget = max_chars
    for relpath in relpaths:
        if budget <= 0:
            break
        candidate = repo / relpath
        if not candidate.is_file():
            continue
        head = _read_head(candidate, min(budget, 6000))
        if not head.strip():
            continue
        chunks.append(f"--- {relpath} ---\n{head}")
        budget -= len(head)
    return "\n\n".join(chunks)


@dataclass
class PackInput:
    persona: Persona
    repo: Path
    session_id: str
    diff_text: str
    changed_files: List[str]
    intent_text: str = ""
    scope_authority: bool = False
    test_output: str = ""
    approved_decisions: List[str] = field(default_factory=list)
    previous_findings: str = ""
    schema_inline: str = ""
    task_intent: str = ""


def build_pack(spec: PackInput) -> str:
    """Assemble one reviewer's prompt. Order matters: rules, scope, evidence."""
    persona = spec.persona
    parts: List[str] = []

    parts.append(base_preamble(spec.repo))
    parts.append(f"# Rôle : {persona.title}\n\n{persona.body}")

    if spec.task_intent:
        parts.append(f"# Intention de la tâche (résumé de Claude)\n"
                     f"{spec.task_intent}")

    if spec.scope_authority:
        parts.append(
            "# TU PORTES L'AUTORITÉ DE PÉRIMÈTRE\n"
            "Tu dois renseigner le bloc `scope` de ta réponse.\n\n"
            "Compare l'implémentation à la demande citée ci-dessous. Signale ce "
            "qui a été fait ET QUI N'A PAS ÉTÉ DEMANDÉ : une fonctionnalité "
            "ajoutée, une capacité retirée, un comportement observable modifié, "
            "un format changé, une API publique changée, une incompatibilité "
            "introduite.\n"
            "Ne signale pas les choix internes : un renommage de variable n'est "
            "pas un changement de périmètre ; la disparition d'un format de "
            "fichier accepté en est un.\n"
            "Les décisions déjà tranchées par l'humain FONT PARTIE du périmètre : "
            "ne les re-signale jamais.\n"
            "En cas de doute, utilise `uncertain` — ce statut ne bloque personne.")

    if spec.intent_text:
        parts.append("# Périmètre approuvé (mots de l'humain, verbatim)\n"
                     + spec.intent_text)

    if spec.approved_decisions:
        parts.append("# Décisions déjà tranchées par l'humain — dans le périmètre\n"
                     + "\n".join(f"- {d}" for d in spec.approved_decisions))

    if persona.wants("project_docs"):
        docs = project_docs(spec.repo)
        if docs:
            parts.append("# Documents du projet\n" + docs)

    if persona.wants("dependency_manifest"):
        manifest = dependency_manifest(spec.repo)
        if manifest:
            parts.append("# Dépendances\n" + manifest)

    if persona.wants("repo_tree"):
        tree = repo_tree(spec.repo)
        if tree:
            parts.append("# Arborescence du dépôt (profondeur 3)\n" + tree)

    if persona.wants("full_files"):
        contents = full_files(spec.repo, spec.changed_files)
        if contents:
            parts.append("# Contenu complet des fichiers touchés\n" + contents)

    if spec.test_output:
        parts.append("# Résultat des tests\n" + spec.test_output)

    if spec.previous_findings:
        parts.append("# Ton round précédent, et la réponse de Claude\n"
                     + spec.previous_findings)

    parts.append(
        "# Diff de session\n"
        "Ce diff ne contient QUE ce que Claude a modifié pendant cette session. "
        "Les modifications antérieures de l'humain en sont exclues ; elles "
        "peuvent apparaître comme lignes de contexte.\n\n"
        "```diff\n" + spec.diff_text.rstrip() + "\n```")

    parts.append(
        "Tu peux ouvrir n'importe quel fichier du dépôt en lecture seule pour "
        "vérifier une hypothèse avant de conclure.")

    if spec.schema_inline:
        parts.append("# Format de réponse OBLIGATOIRE\n"
                     "Réponds UNIQUEMENT avec un objet JSON conforme à ce schéma, "
                     "sans texte autour, sans bloc de code.\n\n"
                     + spec.schema_inline)

    return redact("\n\n".join(p for p in parts if p and p.strip()))


def load_intent_for(cfg, session_id: str) -> str:
    """The approved scope as configured: verbatim, redacted, or withheld."""
    mode = cfg.get("human.intent.send_to_reviewers", True)
    if mode is False:
        return ""
    text = intent.render(session_id,
                         max_chars=int(cfg.get("human.intent.max_chars", 8000)))
    if mode == "redacted":
        text = redact(text)
    return text
