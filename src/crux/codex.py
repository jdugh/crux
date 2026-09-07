"""Invoking Codex CLI: non-interactive, read-only, schema-constrained.

Never called from a hook.  Hooks decide in milliseconds and hand execution back
to Claude, which runs ``crux review`` with a proper timeout.

``--skip-git-repo-check`` is deliberately absent: the documentation says it is
unnecessary inside a repository, and Crux refuses to arm outside one, so the flag
only ever disabled a guard-rail that never fires here.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import capabilities

MISSING = "missing"
AUTH = "auth"
QUOTA = "quota"
TIMEOUT = "timeout"
SCHEMA = "schema"
FAILED = "failed"

_QUOTA_RE = re.compile(
    r"(rate.?limit|quota|429|too many requests|usage limit)", re.IGNORECASE)
_AUTH_RE = re.compile(
    r"(not logged in|unauthor|401|authentication|codex login|sign in)",
    re.IGNORECASE)
_SCHEMA_RE = re.compile(
    r"(invalid_json_schema|invalid schema|response_format|text\.format\.schema)",
    re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class CodexUnavailable(RuntimeError):
    """Codex is not installed or not authenticated."""

    def __init__(self, message: str, kind: str = MISSING):
        super().__init__(message)
        self.kind = kind


@dataclass
class CodexOutcome:
    reviewer: str
    payload: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    error_kind: Optional[str] = None
    duration: float = 0.0
    raw_stdout: str = ""
    raw_stderr: str = ""
    argv: List[str] = None  # type: ignore[assignment]

    @property
    def ok(self) -> bool:
        return self.payload is not None


def ensure_available(cfg) -> None:
    binary = cfg.get("codex.bin", "codex")
    if not capabilities.codex_available(binary):
        raise CodexUnavailable(
            f"codex introuvable ({binary}).\n"
            f"  → npm i -g @openai/codex\n"
            f"  → puis: codex login   (utilise votre abonnement ChatGPT, "
            f"aucune clé API requise)", MISSING)
    if capabilities.codex_logged_in() is False:
        raise CodexUnavailable(
            "codex est installé mais aucune session n'est enregistrée.\n"
            "  → codex login   (abonnement ChatGPT)", AUTH)


def build_argv(cfg, repo: Path, out_file: Path,
               schema_file: Optional[Path]) -> List[str]:
    resolved = capabilities.resolve_binary(cfg.get("codex.bin", "codex"))
    argv = [
        resolved or str(cfg.get("codex.bin", "codex")), "exec",
        "--cd", str(repo),
        "--sandbox", str(cfg.get("codex.sandbox", "read-only")),
        "-o", str(out_file),
        "--ephemeral",
    ]
    if schema_file is not None:
        argv += ["--output-schema", str(schema_file)]
    model = cfg.get("codex.model")
    if model:
        argv += ["--model", str(model)]
    argv += [str(a) for a in (cfg.get("codex.extra_args") or [])]
    argv.append("-")           # prompt arrives on stdin
    return argv


def _error_region(stderr: str, stdout: str) -> str:
    """The part of the output that actually describes the failure.

    Codex echoes the whole prompt to stderr, and our prompt contains a diff.
    Scanning all of it once classified a schema rejection as a quota error,
    because the string "429" appeared inside a baseline blob filename. Only the
    ERROR lines, or failing that the tail, may drive classification.
    """
    blob = f"{stderr}\n{stdout}"
    errors = [line for line in blob.splitlines()
              if line.lstrip().upper().startswith("ERROR")
              or '"message"' in line or '"code"' in line]
    if errors:
        return "\n".join(errors[-40:])
    return blob[-2000:]


def _classify(returncode: int, stderr: str, stdout: str) -> Tuple[str, str]:
    region = _error_region(stderr, stdout)
    if _QUOTA_RE.search(region):
        return QUOTA, "quota ou limite de débit atteinte"
    if _AUTH_RE.search(region):
        return AUTH, "authentification Codex requise (codex login)"
    if _SCHEMA_RE.search(region):
        return SCHEMA, f"schéma refusé par l'API : {region.strip()[-240:]}"
    lines = [line for line in region.strip().splitlines() if line.strip()]
    detail = lines[-1][:300] if lines else f"code {returncode}"
    return FAILED, detail


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Parse a reviewer reply: whole-document JSON first, fenced block second."""
    candidate = text.strip()
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    match = _FENCE_RE.search(candidate)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(candidate[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return None


def run_one(reviewer: str, prompt: str, cfg, repo: Path,
            schema_path: Optional[Path], timeout: int,
            raw_dir: Optional[Path] = None) -> CodexOutcome:
    """One reviewer, one `codex exec`. Errors are returned, never raised."""
    started = time.time()
    tmp = Path(tempfile.mkdtemp(prefix=f"crux-{reviewer}-"))
    out_file = tmp / "out.json"
    outcome = CodexOutcome(reviewer=reviewer, argv=[])
    try:
        argv = build_argv(cfg, repo, out_file, schema_path)
        outcome.argv = argv
        try:
            proc = subprocess.run(
                argv, input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace", shell=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            outcome.error_kind = TIMEOUT
            outcome.error = f"délai dépassé ({timeout} s)"
            return outcome
        except OSError as exc:
            outcome.error_kind = MISSING
            outcome.error = str(exc)
            return outcome

        outcome.raw_stdout = proc.stdout or ""
        outcome.raw_stderr = proc.stderr or ""

        if proc.returncode != 0:
            kind, detail = _classify(proc.returncode, outcome.raw_stderr,
                                     outcome.raw_stdout)
            outcome.error_kind = kind
            outcome.error = detail
            return outcome

        text = ""
        if out_file.is_file():
            text = out_file.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            text = outcome.raw_stdout

        payload = extract_json(text)
        if payload is None:
            outcome.error_kind = SCHEMA
            outcome.error = "réponse non JSON"
            return outcome
        outcome.payload = payload
        return outcome
    finally:
        outcome.duration = time.time() - started
        if raw_dir is not None:
            try:
                raw_dir.mkdir(parents=True, exist_ok=True)
                (raw_dir / f"{reviewer}.stdout.txt").write_text(
                    outcome.raw_stdout, encoding="utf-8")
                (raw_dir / f"{reviewer}.stderr.txt").write_text(
                    outcome.raw_stderr, encoding="utf-8")
                if out_file.is_file():
                    (raw_dir / f"{reviewer}.out.json").write_text(
                        out_file.read_text(encoding="utf-8", errors="replace"),
                        encoding="utf-8")
            except OSError:
                pass
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def run_many(jobs: List[Tuple[str, str]], cfg, repo: Path,
             schema_path: Optional[Path], raw_dir: Optional[Path] = None,
             retry_without_schema: bool = True) -> List[CodexOutcome]:
    """Run reviewers in a bounded pool. One retry each on a schema failure."""
    timeout = int(cfg.get("reviewers.timeout_seconds", 300))
    workers = max(1, int(cfg.get("reviewers.parallel", 3)))

    def _work(job: Tuple[str, str]) -> CodexOutcome:
        reviewer, prompt = job
        outcome = run_one(reviewer, prompt, cfg, repo, schema_path, timeout,
                          raw_dir)
        if outcome.ok or not retry_without_schema:
            return outcome
        if outcome.error_kind in (SCHEMA, FAILED) and schema_path is not None:
            # Degraded mode: schema inside the prompt, look for a fenced block.
            retry_prompt = (
                prompt + "\n\nTa réponse précédente n'a pas pu être analysée "
                f"({outcome.error}). Réponds cette fois avec UNIQUEMENT l'objet "
                "JSON, sans texte autour.")
            second = run_one(reviewer, retry_prompt, cfg, repo, None, timeout,
                             raw_dir)
            if second.ok:
                return second
        return outcome

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_work, jobs))
