"""Deterministic reviewer selection: signals, scores, a threshold, a cap.

No LLM call, no latency, no non-determinism, and unit-testable.  `crux route
--explain` prints exactly why each reviewer was picked - predictability was a
requirement, not a nicety.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

THRESHOLD_DEFAULT = 50


@dataclass
class RouteContext:
    paths: List[str] = field(default_factory=list)
    added_text: str = ""
    changed_lines: int = 0
    files_changed: int = 0
    deleted_files: List[str] = field(default_factory=list)
    net_lines: int = 0                     # added - removed
    project_has_tests: bool = False
    new_top_level_dirs: List[str] = field(default_factory=list)


@dataclass
class Signal:
    name: str
    label: str
    scores: Dict[str, int]
    detect: Callable[[RouteContext], bool]


def _any_path(ctx: RouteContext, patterns: Sequence[str]) -> bool:
    for path in ctx.paths:
        lowered = path.lower()
        for pattern in patterns:
            if fnmatch.fnmatch(lowered, pattern):
                return True
    return False


def _added_matches(ctx: RouteContext, pattern: str) -> bool:
    return re.search(pattern, ctx.added_text, re.IGNORECASE) is not None


TEST_PATH_PATTERNS = (
    "*test_*.py", "*_test.py", "*/tests/*", "tests/*", "*/test/*", "test/*",
    "*.test.js", "*.test.ts", "*.test.tsx", "*.spec.js", "*.spec.ts",
    "*.spec.tsx", "*_test.go", "*/spec/*", "spec/*",
)

AUTH_PATH_PATTERNS = (
    "*/auth/*", "auth/*", "*login*", "*session*", "*token*", "*permission*",
    "*/security/*", "*credential*", "*oauth*",
)

UI_PATH_PATTERNS = (
    "*.tsx", "*.jsx", "*.vue", "*.svelte", "*.css", "*.scss", "*.less",
    "*/templates/*", "*.html", "*/components/*",
)

DEPLOY_PATH_PATTERNS = (
    "dockerfile", "*/dockerfile", "docker-compose*", "*/.github/workflows/*",
    ".github/workflows/*", "*.env.example", "*/k8s/*", "*/helm/*",
    "*procfile*", "*/terraform/*",
)

MANIFEST_PATHS = (
    "package.json", "*/package.json", "pyproject.toml", "*/pyproject.toml",
    "requirements*.txt", "*/requirements*.txt", "cargo.toml", "*/cargo.toml",
    "go.mod", "*/go.mod", "gemfile", "composer.json",
)

SCHEMA_PATH_PATTERNS = (
    "*/migrations/*", "migrations/*", "*.sql", "*schema.prisma", "*models.py",
    "*.entity.ts", "*/migrate/*", "*alembic*",
)

SIGNALS: List[Signal] = [
    Signal("always", "reviewer toujours actif",
           {"code-quality": 100}, lambda ctx: True),

    Signal("auth_surface", "surface d'authentification",
           {"security": 90, "tests": 40},
           lambda ctx: _any_path(ctx, AUTH_PATH_PATTERNS) or _added_matches(
               ctx, r"\b(jwt|bcrypt|passwd|password|secret|api[_-]?key|"
                    r"authorization|hmac|csrf)\b")),

    Signal("external_input", "entrées externes non validées",
           {"security": 70},
           lambda ctx: _added_matches(
               ctx, r"(request\.|req\.body|req\.query|subprocess|os\.system|"
                    r"\beval\(|innerHTML|dangerouslySetInnerHTML|"
                    r"execute\(\s*f?[\"'].*(SELECT|INSERT|UPDATE|DELETE))")),

    Signal("dependencies", "manifeste de dépendances modifié",
           {"security": 50, "release": 50},
           lambda ctx: _any_path(ctx, MANIFEST_PATHS)),

    Signal("schema_migration", "schéma ou migration",
           {"architecture": 80, "release": 70, "tests": 50},
           lambda ctx: _any_path(ctx, SCHEMA_PATH_PATTERNS)),

    Signal("hot_path", "chemin chaud / traitement lourd",
           {"performance": 80},
           lambda ctx: _added_matches(
               ctx, r"\b(numpy|pandas|PIL|Image\.open|sharp|ffmpeg|cv2|"
                    r"Promise\.all|ThreadPool|ProcessPool|asyncio\.gather|"
                    r"multiprocessing)\b")),

    Signal("user_surface", "surface utilisateur",
           {"ux": 70}, lambda ctx: _any_path(ctx, UI_PATH_PATTERNS)),

    Signal("deployment", "déploiement / infrastructure",
           {"release": 80}, lambda ctx: _any_path(ctx, DEPLOY_PATH_PATTERNS)),

    Signal("code_without_tests", "code modifié sans test",
           {"tests": 60},
           lambda ctx: (ctx.project_has_tests
                        and bool(ctx.paths)
                        and not _any_path(ctx, TEST_PATH_PATTERNS))),

    Signal("magnitude", "ampleur du changement",
           {"architecture": 60, "tests": 30},
           lambda ctx: (ctx.changed_lines > 300 or ctx.files_changed > 8
                        or bool(ctx.new_top_level_dirs))),

    # Added in revision 2: a deletion is the most frequent and most discreet
    # form of functional drift.
    Signal("net_removal", "suppression nette",
           {"architecture": 50},
           lambda ctx: bool(ctx.deleted_files) or ctx.net_lines < -80),
]


@dataclass
class RouteRow:
    reviewer: str
    score: int
    reasons: List[str]
    selected: bool
    note: str = ""


@dataclass
class RouteResult:
    selected: List[str]
    rows: List[RouteRow]
    fired: List[str]
    capped: bool = False
    micro_change: bool = False
    scope_authority: Optional[str] = None

    def explain(self) -> str:
        lines = ["Signaux déclenchés : " + (", ".join(self.fired) or "aucun"), ""]
        lines.append(f"{'reviewer':<16}{'score':>6}  {'retenu':<8}raisons")
        for row in sorted(self.rows, key=lambda r: -r.score):
            mark = "oui" if row.selected else "non"
            reasons = ", ".join(row.reasons) or "—"
            lines.append(f"{row.reviewer:<16}{row.score:>6}  {mark:<8}{reasons}"
                         + (f"  [{row.note}]" if row.note else ""))
        if self.micro_change:
            lines.append("")
            lines.append("Micro-changement : plafonné au reviewer toujours actif.")
        if self.capped:
            lines.append("")
            lines.append("Plafond max_selected atteint : meilleurs scores retenus.")
        if self.scope_authority:
            lines.append("")
            lines.append(f"Autorité de périmètre : {self.scope_authority}")
        return "\n".join(lines)


def select(ctx: RouteContext, cfg,
           only: Optional[Sequence[str]] = None,
           add: Optional[Sequence[str]] = None,
           select_all: bool = False,
           available: Optional[Sequence[str]] = None) -> RouteResult:
    """Score every reviewer, keep those over the threshold, honour the cap."""
    from .config import KNOWN_REVIEWERS

    always = list(cfg.get("reviewers.always") or [])
    auto = list(cfg.get("reviewers.auto") or [])
    never = set(cfg.get("reviewers.never") or [])
    threshold = int(cfg.get("reviewers.threshold", THRESHOLD_DEFAULT))
    max_selected = int(cfg.get("reviewers.max_selected", 4))

    candidates = [r for r in KNOWN_REVIEWERS if r in set(always) | set(auto)]
    if available is not None:
        candidates = [r for r in candidates if r in available]

    scores: Dict[str, int] = {r: 0 for r in candidates}
    reasons: Dict[str, List[str]] = {r: [] for r in candidates}
    fired: List[str] = []

    for signal in SIGNALS:
        try:
            hit = signal.detect(ctx)
        except Exception:
            hit = False       # a broken signal must never break a review
        if not hit:
            continue
        fired.append(signal.name)
        for reviewer, points in signal.scores.items():
            if reviewer in scores:
                scores[reviewer] += points
                reasons[reviewer].append(signal.label)

    # A micro-change caps at the always-on reviewer. Two exclusions matter:
    # the comparison must match the selection rule (>= not >), and a deletion is
    # never a micro-change - removing a small module is precisely the quiet
    # functional drift the scope authority exists to catch.
    drift_signal = "net_removal" in fired
    micro = (ctx.changed_lines < 15
             and ctx.files_changed <= 1
             and not drift_signal
             and not any(s >= threshold for r, s in scores.items()
                         if r not in always))

    chosen: List[str]
    if select_all:
        chosen = [r for r in candidates if r not in never]
    elif only:
        chosen = [r for r in only if r in candidates and r not in never]
    else:
        chosen = [r for r in candidates
                  if r not in never and (r in always or scores[r] >= threshold)]
        if micro:
            chosen = [r for r in chosen if r in always]
        for extra in (add or []):
            if extra in candidates and extra not in never and extra not in chosen:
                chosen.append(extra)

    chosen.sort(key=lambda r: (-scores.get(r, 0), r))
    capped = False
    if not select_all and not only and len(chosen) > max_selected:
        chosen = chosen[:max_selected]
        capped = True

    rows = [RouteRow(reviewer=r, score=scores[r], reasons=reasons[r],
                     selected=r in chosen)
            for r in candidates]
    for row in rows:
        if row.reviewer in never:
            row.note = "never"

    return RouteResult(selected=chosen, rows=rows, fired=fired,
                       capped=capped, micro_change=micro)


def build_context(diff, repo, project_has_tests: bool) -> RouteContext:
    from . import baseline as _baseline
    top_level = set()
    for path in diff.new_files:
        head = path.split("/", 1)[0]
        if "/" in path and not (repo / head).exists():
            top_level.add(head)
    return RouteContext(
        paths=list(diff.files),
        added_text=_baseline.added_lines(diff.text),
        changed_lines=diff.changed_lines,
        files_changed=len(diff.files),
        deleted_files=list(diff.deleted_files),
        net_lines=diff.added - diff.removed,
        project_has_tests=project_has_tests,
        new_top_level_dirs=sorted(top_level),
    )
