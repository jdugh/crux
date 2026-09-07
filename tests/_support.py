"""Shared test scaffolding: an isolated $CRUX_HOME and a throwaway git repo."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0 and args[:1] != ("diff",):
        raise AssertionError(f"git {' '.join(args)} failed: {out.stderr}")
    return out.stdout


class CruxTestCase(unittest.TestCase):
    """Every test runs against its own CRUX_HOME and its own git repo."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="crux-test-"))
        self.crux_home = self.tmp / "cruxhome"
        self.repo = self.tmp / "repo"
        self.repo.mkdir(parents=True)
        self._env_backup = dict(os.environ)
        os.environ["CRUX_HOME"] = str(self.crux_home)
        for var in ("CRUX_GATE", "CRUX_DISABLE"):
            os.environ.pop(var, None)
        self.init_repo()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def init_repo(self) -> None:
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Crux Test")
        git(self.repo, "config", "commit.gpgsign", "false")

    def write(self, relpath: str, content: str) -> Path:
        path = self.repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def commit(self, message: str = "wip") -> None:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)

    def write_project_config(self, body: str) -> Path:
        path = self.repo / ".crux.yml"
        path.write_text(body, encoding="utf-8", newline="\n")
        return path
