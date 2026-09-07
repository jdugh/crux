"""Probe capabilities, never infer them from a version or a model name.

The revision-1 design hard-coded "``--output-schema`` needs a gpt-5 model".  That
claim did not survive checking: the official documentation describes the flag with
no model restriction at all.  The lesson generalises into a rule this module
enforces - Crux *runs* the capability and looks at the result.  No branch anywhere
tests a model name.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from . import paths, state

PROBE_SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "additionalProperties": False,
    "properties": {"ok": {"type": "boolean"}},
}

PROBE_PROMPT = (
    "Reply with the JSON object {\"ok\": true} and nothing else. "
    "Do not read any file. Do not run any command."
)


def _cache() -> Dict[str, Any]:
    try:
        raw = paths.read_json(paths.capabilities_path())
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _save(payload: Dict[str, Any]) -> None:
    paths.write_json(paths.capabilities_path(), payload)


def resolve_binary(name: str = "codex") -> Optional[str]:
    """Absolute path to an executable, or None.

    Required on Windows, where npm installs ``codex`` as a ``.cmd`` shim:
    ``CreateProcess`` does not apply ``PATHEXT`` to a bare program name, so
    ``subprocess.run(["codex", ...], shell=False)`` raises FileNotFoundError even
    though the command works in every shell.  ``shutil.which`` does apply
    ``PATHEXT``, and the resolved ``.cmd`` runs fine.  A ``.ps1`` shim is skipped:
    it is not a Win32 executable.

    We never fall back to ``shell=True`` - passing a repository path through a
    shell is exactly the quoting hazard this project set out to avoid.
    """
    candidate = Path(name).expanduser()
    if candidate.is_absolute():
        return str(candidate) if candidate.is_file() else None

    found = shutil.which(name)
    if found and Path(found).suffix.lower() == ".ps1":
        for suffix in (".cmd", ".bat", ".exe", ""):
            alt = shutil.which(name + suffix)
            if alt and Path(alt).suffix.lower() != ".ps1":
                return alt
        return None
    return found


def codex_version(binary: str = "codex") -> Optional[str]:
    resolved = resolve_binary(binary)
    if resolved is None:
        return None
    try:
        proc = subprocess.run([resolved, "--version"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              shell=False, timeout=60)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def codex_available(binary: str = "codex") -> bool:
    return codex_version(binary) is not None


def codex_logged_in() -> Optional[bool]:
    """Best-effort: presence of the auth file written by `codex login`.

    Crux never reads its content - only whether it exists.
    """
    import os
    home = os.environ.get("CODEX_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    if not base.is_dir():
        return None
    return (base / "auth.json").is_file()


def cache_key(cfg) -> str:
    binary = cfg.get("codex.bin", "codex")
    return "|".join([
        str(codex_version(binary) or "absent"),
        str(cfg.get("codex.model") or "default"),
    ])


def probe_output_schema(cfg, repo: Optional[Path] = None,
                        timeout: int = 180) -> Dict[str, Any]:
    """Actually run `codex exec --output-schema` on a trivial prompt.

    The probe must reproduce the conditions of real use or it measures the wrong
    thing.  Codex refuses to run outside a trusted git repository, so probing in
    a scratch directory reported "not supported" for a capability that works
    perfectly inside a repository.  When a repository is available we probe
    there; otherwise we fall back to a scratch directory with
    ``--skip-git-repo-check`` - the one legitimate use of that flag, in a
    throwaway directory that is deliberately not a repository, and never on the
    nominal review path.
    """
    binary = resolve_binary(cfg.get("codex.bin", "codex"))
    if binary is None:
        return {"supported": None, "reason": "codex indisponible"}

    tmp = Path(tempfile.mkdtemp(prefix="crux-probe-"))
    try:
        schema_file = tmp / "probe.schema.json"
        schema_file.write_text(json.dumps(PROBE_SCHEMA), encoding="utf-8")
        out_file = tmp / "probe.out.json"
        workdir = repo if repo is not None else tmp
        argv = [binary, "exec", "--cd", str(workdir), "--sandbox", "read-only",
                "--output-schema", str(schema_file), "-o", str(out_file),
                "--ephemeral"]
        if repo is None:
            argv.append("--skip-git-repo-check")
        argv.append("-")
        try:
            proc = subprocess.run(argv, input=PROBE_PROMPT, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  shell=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"supported": False, "reason": "délai dépassé"}
        except OSError as exc:
            return {"supported": None, "reason": str(exc)}

        if proc.returncode != 0:
            return {"supported": False,
                    "reason": (proc.stderr or proc.stdout).strip()[:300]}
        if not out_file.is_file():
            return {"supported": False, "reason": "aucune sortie écrite"}
        try:
            parsed = json.loads(out_file.read_text(encoding="utf-8"))
        except ValueError:
            return {"supported": False, "reason": "sortie non JSON"}
        return {"supported": bool(isinstance(parsed, dict) and "ok" in parsed),
                "reason": ""}
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def get(cfg, refresh: bool = False,
        repo: Optional[Path] = None) -> Dict[str, Any]:
    """Cached capability map. Invalidated by codex version or model change."""
    key = cache_key(cfg)
    cached = _cache()
    if not refresh and cached.get("key") == key:
        return cached

    payload: Dict[str, Any] = {
        "key": key,
        "probed_at": state.now_iso(),
        "codex_version": codex_version(cfg.get("codex.bin", "codex")),
        "codex_logged_in": codex_logged_in(),
        # Not probed in the MVP: advise/auto are out of scope by product
        # decision, so nothing needs to know yet.
        "ask_updated_input": None,
    }
    if refresh:
        payload["output_schema"] = probe_output_schema(cfg, repo=repo)
    else:
        payload["output_schema"] = cached.get("output_schema") or {
            "supported": None, "reason": "non sondé"}
    _save(payload)
    return payload


def output_schema_supported(cfg) -> Optional[bool]:
    """True / False / None (unknown). Unknown means: try it and see."""
    caps = get(cfg)
    value = (caps.get("output_schema") or {}).get("supported")
    return value if isinstance(value, bool) else None
