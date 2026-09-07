"""Probe capabilities, never infer them from a version or a model name.

The revision-1 design hard-coded "``--output-schema`` needs a gpt-5 model".  That
claim did not survive checking: the official documentation describes the flag with
no model restriction at all.  The lesson generalises into a rule this module
enforces - Crux *runs* the capability and looks at the result.  No branch anywhere
tests a model name.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import paths, state

# A probe has three outcomes, not two. Collapsing the third into `unsupported`
# is how a quota outage could permanently downgrade every later review: the
# cache is only invalidated by a Codex version or model change, so a transient
# failure recorded as a verdict outlives its cause by weeks.
SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
INDETERMINATE = "indeterminate"

# Only these are worth remembering. `indeterminate` is never cached as a verdict,
# so the next probe runs again - fail-open, and self-healing.
CACHEABLE = (SUPPORTED, UNSUPPORTED)

# Bumped whenever the meaning of a cached entry changes. Version 1 stored a bare
# `supported: true|false|null` in which `false` could mean either "proved absent"
# or "the probe failed" - unusable after the fact, so v1 entries are discarded
# rather than interpreted.
CACHE_VERSION = 2

# `unsupported` requires proof that the capability is not there: the CLI or the
# API rejecting the option itself. Anything else - quota, auth, timeouts, a
# missing file, unreadable output - is a statement about this run, not about the
# capability.
_UNSUPPORTED_RE = re.compile(
    r"(unexpected argument|unrecognized (?:option|argument|subcommand)|"
    r"unknown (?:option|argument|flag)|no such option|"
    r"invalid (?:option|flag)|not a valid (?:option|flag)|"
    r"unsupported (?:parameter|option|field)|"
    r"is not supported (?:by|for) this model)",
    re.IGNORECASE)

# Names that identify the capability under probe, on the CLI side and on the API
# side. An "unknown option" error is only proof when it is about *this* option:
# the probe also passes --cd, --ephemeral and sometimes --skip-git-repo-check, and
# a Codex release renaming any of those would otherwise be recorded as
# "--output-schema unsupported" forever.
OUTPUT_SCHEMA_NAMES = (
    "--output-schema", "output-schema", "output_schema",
    "response_format", "text.format", "json_schema", "structured output",
)

_TRANSIENT_RE = re.compile(
    r"(rate.?limit|quota|429|too many requests|usage limit|"
    r"not logged in|unauthor|401|authentication|codex login|sign in|"
    r"timed? out|timeout|network|connection|econn|dns|proxy|"
    r"5\d\d\b|temporarily|unavailable|overloaded)",
    re.IGNORECASE)


def classify_probe(returncode: int, stderr: str, stdout: str,
                   names: Tuple[str, ...] = OUTPUT_SCHEMA_NAMES) -> Tuple[str, str]:
    """Why a probe run failed: the capability is absent, or the run was.

    Two conditions must both hold for `unsupported`, and the order matters:

    1. nothing transient in the error - a quota message wins outright, even if it
       happens to contain rejection-shaped words;
    2. the rejection names the capability being probed. "unexpected argument
       '--ephemeral'" is a real rejection of a real option, and says nothing
       whatsoever about --output-schema.

    Everything else is `indeterminate`, which is re-probed rather than cached.
    """
    blob = f"{stderr}\n{stdout}"
    region = "\n".join(
        line for line in blob.splitlines()
        if line.lstrip().upper().startswith("ERROR")
        or '"message"' in line or '"code"' in line) or blob[-2000:]
    detail = region.strip()[-300:]
    if _TRANSIENT_RE.search(region):
        return INDETERMINATE, detail
    if _UNSUPPORTED_RE.search(region):
        lowered = region.lower()
        if any(name.lower() in lowered for name in names):
            return UNSUPPORTED, detail
        # Rejection-shaped, but about another option: not evidence about us.
        return INDETERMINATE, detail
    # An unclassified non-zero exit says nothing about the capability.
    return INDETERMINATE, detail or f"code {returncode}"

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


def _outcome(status: str, reason: str) -> Dict[str, Any]:
    return {"status": status, "reason": reason, "probed_at": state.now_iso()}


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
        return _outcome(INDETERMINATE, "codex indisponible")

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
            return _outcome(INDETERMINATE, f"délai dépassé ({timeout} s)")
        except OSError as exc:
            return _outcome(INDETERMINATE, str(exc))

        if proc.returncode != 0:
            status, reason = classify_probe(proc.returncode, proc.stderr or "",
                                            proc.stdout or "")
            return _outcome(status, reason)
        if not out_file.is_file():
            # The run succeeded and wrote nothing. That is a statement about
            # this run, not about the flag: re-probe later.
            return _outcome(INDETERMINATE, "aucune sortie écrite")
        try:
            parsed = json.loads(out_file.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            return _outcome(INDETERMINATE, f"sortie illisible : {exc}")
        if isinstance(parsed, dict) and "ok" in parsed:
            return _outcome(SUPPORTED, "")
        # Well-formed JSON that does not match the schema. The flag was accepted
        # and the run completed, so this is not proof the flag is unknown - only
        # that the model did not comply this time. Not a verdict.
        return _outcome(INDETERMINATE,
                        "sortie JSON non conforme au schéma de sonde")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def get(cfg, refresh: bool = False,
        repo: Optional[Path] = None) -> Dict[str, Any]:
    """Cached capability map.

    Invalidated by a Codex version or model change, and by a cache version bump.
    A v1 entry is discarded outright: its `supported: false` could not be told
    apart from a probe that merely failed, so re-probing is the only safe reading.
    """
    key = cache_key(cfg)
    cached = _cache()
    if (not refresh
            and cached.get("key") == key
            and cached.get("cache_version") == CACHE_VERSION):
        return cached

    payload: Dict[str, Any] = {
        "key": key,
        "cache_version": CACHE_VERSION,
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
        payload["output_schema"] = _carry_over(cached) or _outcome(
            INDETERMINATE, "non sondé")
    _save(payload)
    return payload


def _carry_over(cached: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Reuse a previous verdict only if it is one, and only from this format."""
    if cached.get("cache_version") != CACHE_VERSION:
        return None
    previous = cached.get("output_schema")
    if not isinstance(previous, dict):
        return None
    return previous if previous.get("status") in CACHEABLE else None


def output_schema_status(cfg) -> str:
    caps = get(cfg)
    status = (caps.get("output_schema") or {}).get("status")
    return status if status in (SUPPORTED, UNSUPPORTED) else INDETERMINATE


def output_schema_supported(cfg) -> Optional[bool]:
    """True / False / None (unknown). Unknown means: try it and see.

    `indeterminate` maps to None, so the review path attempts the flag and lets
    the real call decide - `codex.run_many` already retries without the schema.
    """
    status = output_schema_status(cfg)
    if status == SUPPORTED:
        return True
    if status == UNSUPPORTED:
        return False
    return None
