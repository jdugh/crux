"""Filesystem layout for Crux user data.

Everything Crux persists lives under ``$CRUX_HOME`` (default ``~/.crux``).
Nothing is ever written into a project except ``.crux.yml`` on ``crux init``.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

PROJECT_CONFIG_NAME = ".crux.yml"


def home() -> Path:
    """Root of Crux user data. Honours $CRUX_HOME so the tests can isolate."""
    env = os.environ.get("CRUX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".crux"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            path.chmod(stat.S_IRWXU)  # 0700 - context packs hold source excerpts
        except OSError:
            pass
    return path


def config_path() -> Path:
    return home() / "config.yml"


def capabilities_path() -> Path:
    return home() / "capabilities.json"


def log_path() -> Path:
    return home() / "crux.log"


def claude_dir() -> Path:
    return ensure_dir(home() / "claude")


def sessions_root() -> Path:
    return ensure_dir(home() / "sessions")


def runs_root() -> Path:
    return ensure_dir(home() / "runs")


def personas_user_dir() -> Path:
    return home() / "personas"


def session_dir(session_id: str) -> Path:
    """A session directory, namespaced by a sanitised session id."""
    return ensure_dir(sessions_root() / safe_component(session_id))


def baseline_dir(session_id: str) -> Path:
    return ensure_dir(session_dir(session_id) / "baseline")


def safe_component(value: str) -> str:
    """Turn an arbitrary string into a safe single path component.

    Session ids are UUID-like in practice, but a hostile or merely awkward value
    must never escape the sessions directory or break on Windows.
    """
    keep = [c for c in str(value) if c.isalnum() or c in "-_"]
    cleaned = "".join(keep)[:64]
    if cleaned and cleaned == str(value):
        return cleaned
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
    return f"{cleaned or 'session'}-{digest}"


def blob_name(relpath: str) -> str:
    """Baseline blob filename: hash of the path, so Windows-illegal names are safe."""
    return hashlib.sha256(relpath.encode("utf-8")).hexdigest() + ".blob"


def write_atomic(path: Path, data: str) -> None:
    """Write a file atomically, UTF-8, LF endings, without clobbering on failure."""
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".crux-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, payload: object) -> None:
    write_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        raise


def append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON record. Small single-line writes are atomic in practice,
    and JSONL makes a torn line detectable and skippable on read."""
    ensure_dir(path.parent)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
        fh.flush()
