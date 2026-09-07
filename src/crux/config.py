"""Configuration cascade with provenance.

Order, lowest priority first:
    built-in defaults  <  ~/.crux/config.yml  <  <project>/.crux.yml

The gate mode has an ordering of its own on top of this (see ``state.resolve_gate``)
because CRUX_DISABLE and a live ``/crux:on`` outrank any file.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import paths

try:  # pragma: no cover - exercised implicitly everywhere
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "crux requires PyYAML.  Install it with:  python -m pip install pyyaml"
    ) from exc


class ConfigError(Exception):
    """Invalid configuration. Carries the offending key so the message is useful."""

    def __init__(self, message: str, key: str = "", source: str = ""):
        super().__init__(message)
        self.message = message
        self.key = key
        self.source = source

    def __str__(self) -> str:
        bits = [self.message]
        if self.key:
            bits.append(f"clé: {self.key}")
        if self.source:
            bits.append(f"fichier: {self.source}")
        return " — ".join(bits)


DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "human": {
        "questions": {"mode": "human"},
        "scope_changes": {"mode": "ask"},
        "intent": {"send_to_reviewers": True, "max_chars": 8000},
    },
    "gate": {
        "mode": "off",
        "max_rounds": 2,
        "plan_rounds": 1,
        "block_on": "high",
        "budget_seconds": 900,
    },
    "scope": {
        "base": "auto",
        "session_edits_only": True,
        "baseline": {
            "max_file_bytes": 2 * 1024 * 1024,
            "max_total_bytes": 50 * 1024 * 1024,
        },
        "exclude": [
            "**/*.lock",
            "**/package-lock.json",
            "**/dist/**",
            "**/build/**",
            "**/node_modules/**",
            "**/*.min.*",
            "**/__snapshots__/**",
            "**/*.svg",
        ],
        "max_diff_chars": 60000,
        "max_file_diff_lines": 1500,
    },
    "reviewers": {
        "always": ["code-quality"],
        "auto": ["architecture", "security"],
        "never": [],
        "max_selected": 4,
        "parallel": 3,
        "timeout_seconds": 300,
        "threshold": 50,
    },
    "tests": {
        "command": None,
        "auto_run": "when-selected",
        "timeout_seconds": 300,
        "max_output_lines": 200,
    },
    "codex": {
        "bin": "codex",
        "model": None,
        "sandbox": "read-only",
        "extra_args": [],
    },
    "logs": {
        "dir": "user",
        "keep_runs": 30,
        "redact": ["**/.env*", "**/secrets/**"],
    },
}

_ENUMS = {
    "human.questions.mode": {"human", "advise", "auto"},
    "human.scope_changes.mode": {"ask", "warn", "auto"},
    "human.intent.send_to_reviewers": {True, False, "redacted"},
    "gate.mode": {"off", "code", "plan", "both"},
    "gate.block_on": {"critical", "high", "medium", "low"},
    "tests.auto_run": {"never", "when-selected", "always"},
    "codex.sandbox": {"read-only", "workspace-write", "danger-full-access"},
    "logs.dir": {"user", "project"},
}

_POSITIVE_INTS = (
    "gate.max_rounds",
    "gate.plan_rounds",
    "gate.budget_seconds",
    "reviewers.max_selected",
    "reviewers.parallel",
    "reviewers.timeout_seconds",
    "scope.max_diff_chars",
    "scope.max_file_diff_lines",
    "scope.baseline.max_file_bytes",
    "scope.baseline.max_total_bytes",
    "tests.timeout_seconds",
    "tests.max_output_lines",
    "logs.keep_runs",
)

KNOWN_REVIEWERS = (
    "code-quality",
    "architecture",
    "security",
    "performance",
    "tests",
    "ux",
    "release",
)

# Personas actually shipped in the v0.1 MVP.
MVP_REVIEWERS = ("code-quality", "architecture", "security")


class Config:
    """Merged configuration plus, for every dotted key, where its value came from."""

    def __init__(self, data: Dict[str, Any], provenance: Dict[str, str],
                 project_config: Optional[Path] = None):
        self.data = data
        self.provenance = provenance
        self.project_config = project_config

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def source_of(self, dotted: str) -> str:
        return self.provenance.get(dotted, "défaut intégré")

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self.data)


def _flatten(node: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                out.update(_flatten(value, dotted))
            else:
                out[dotted] = value
    return out


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Dicts merge key by key; lists and scalars replace wholesale."""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"illisible ({exc.strerror})", source=str(path))
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"ligne {mark.line + 1}" if mark is not None else "YAML invalide"
        raise ConfigError(f"YAML invalide ({where})", source=str(path))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError("la racine doit être un mapping", source=str(path))
    return loaded


def find_project_config(start: Path) -> Optional[Path]:
    """Nearest .crux.yml walking up from ``start``. None if there is none."""
    current = Path(start).resolve()
    for candidate in [current, *current.parents]:
        cfg = candidate / paths.PROJECT_CONFIG_NAME
        if cfg.is_file():
            return cfg
    return None


def _coerce_yaml_booleans(data: Dict[str, Any]) -> None:
    """Undo YAML 1.1 boolean folding for our string enums, in place.

    ``gate: { mode: off }`` is the documented spelling, but YAML 1.1 turns the
    bare words off/no/false into ``False`` (and on/yes/true into ``True``).
    Without this, the very example we ship would fail validation.  Only enums
    whose allowed values are all strings are coerced, so genuinely boolean keys
    such as ``human.intent.send_to_reviewers`` keep their booleans.
    """
    for dotted, allowed in _ENUMS.items():
        if any(isinstance(a, bool) for a in allowed):
            continue
        parts = dotted.split(".")
        node: Any = data
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        leaf = parts[-1]
        if isinstance(node, dict) and isinstance(node.get(leaf), bool):
            node[leaf] = "on" if node[leaf] else "off"


def validate(data: Dict[str, Any], source_map: Dict[str, str]) -> None:
    _coerce_yaml_booleans(data)
    flat = _flatten(data)

    for key, allowed in _ENUMS.items():
        if key not in flat:
            continue
        value = flat[key]
        if value not in allowed:
            pretty = " | ".join(sorted(str(a) for a in allowed))
            raise ConfigError(
                f"valeur {value!r} refusée (attendu: {pretty})",
                key=key,
                source=source_map.get(key, ""),
            )

    for key in _POSITIVE_INTS:
        if key not in flat:
            continue
        value = flat[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(
                f"entier strictement positif attendu, reçu {value!r}",
                key=key,
                source=source_map.get(key, ""),
            )

    for key in ("reviewers.always", "reviewers.auto", "reviewers.never",
                "scope.exclude", "logs.redact", "codex.extra_args"):
        value = flat.get(key)
        if value is not None and not isinstance(value, list):
            raise ConfigError(
                f"liste attendue, reçu {type(value).__name__}",
                key=key,
                source=source_map.get(key, ""),
            )

    for key in ("reviewers.always", "reviewers.auto", "reviewers.never"):
        for name in flat.get(key) or []:
            if name not in KNOWN_REVIEWERS:
                known = ", ".join(KNOWN_REVIEWERS)
                raise ConfigError(
                    f"reviewer inconnu {name!r} (connus: {known})",
                    key=key,
                    source=source_map.get(key, ""),
                )

    command = flat.get("tests.command")
    if command is not None and not isinstance(command, str):
        raise ConfigError("chaîne ou null attendu", key="tests.command",
                          source=source_map.get("tests.command", ""))

    version = data.get("version")
    if version not in (None, 1):
        raise ConfigError(
            f"version {version!r} non supportée par cette version de crux (attendu: 1)",
            key="version", source=source_map.get("version", ""))


def load(project_dir: Optional[Path] = None) -> Config:
    """Build the merged configuration for ``project_dir`` (default: cwd)."""
    start = Path(project_dir) if project_dir else Path.cwd()

    data = copy.deepcopy(DEFAULTS)
    provenance = {key: "défaut intégré" for key in _flatten(DEFAULTS)}

    user_cfg = paths.config_path()
    if user_cfg.is_file():
        overlay = _load_yaml(user_cfg)
        data = _deep_merge(data, overlay)
        for key in _flatten(overlay):
            provenance[key] = str(user_cfg)

    project_cfg = find_project_config(start)
    if project_cfg is not None:
        overlay = _load_yaml(project_cfg)
        data = _deep_merge(data, overlay)
        for key in _flatten(overlay):
            provenance[key] = str(project_cfg)

    validate(data, provenance)
    return Config(data, provenance, project_cfg)


def project_root(project_dir: Optional[Path] = None) -> Optional[Path]:
    cfg = find_project_config(Path(project_dir) if project_dir else Path.cwd())
    return cfg.parent if cfg else None


def gate_mode_from_files(cfg: Config) -> Tuple[str, str]:
    """The gate mode a config file asks for, and which file asked."""
    return cfg.get("gate.mode", "off"), cfg.source_of("gate.mode")
