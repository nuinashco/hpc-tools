"""Stand-ins for the programs `jup` starts: ssh, rsync and what runs on the cluster."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from jup.cli import Context
from jup.config import Config
from jup.ssh import Remote

SSH_ENTRY = (
    "user alice\nhostname login.example\ncontrolmaster auto\ncontrolpath /tmp/cm\n"
    "controlpersist 43200\nserveraliveinterval 60\n"
)


class FakeRun:
    """Answer each started program from a script of (text in the command, reply)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.replies: list[tuple[str, Any]] = []

    def reply(
        self, containing: str, code: int = 0, out: str = "", err: str = ""
    ) -> None:
        """Answer the next command that holds `containing`; later replies queue up."""
        self.replies.append((containing, (code, out, err)))

    def fail(self, containing: str, error: BaseException) -> None:
        """Raise `error` for the next command that holds `containing`."""
        self.replies.append((containing, error))

    def __call__(
        self, command: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        self.inputs.append(options.get("input"))
        line = " ".join(command)
        for index, (containing, answer) in enumerate(self.replies):
            if containing in line:
                del self.replies[index]
                if isinstance(answer, BaseException):
                    raise answer
                return subprocess.CompletedProcess(command, *answer)
        if command[:2] == ["ssh", "-G"]:
            return subprocess.CompletedProcess(command, 0, SSH_ENTRY, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def ran(self, containing: str) -> list[str]:
        """Return the started commands that hold `containing`, each as one line."""
        return [" ".join(c) for c in self.calls if containing in " ".join(c)]


@pytest.fixture
def run() -> FakeRun:
    return FakeRun()


@pytest.fixture(autouse=True)
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the helper's log and reports of every test out of the user's own."""
    home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test as at a terminal, also when Claude Code starts the tests."""
    monkeypatch.delenv("CLAUDECODE", raising=False)


@pytest.fixture
def settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the user's settings at an empty directory, and return it."""
    home = tmp_path / "settings"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    for name in ("JUP_HOST", "JUP_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def project(tmp_path: Path, settings_home: Path) -> Path:
    """Return a git repository's root with a `.jupiter.json` naming account `proj`."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".jupiter.json").write_text('{"account": "proj"}')
    return root


@pytest.fixture
def context(project: Path, run: FakeRun) -> Context:
    """Return that project on the cluster that `run` stands in for."""
    return Context(project, Config.load(project), Remote("cluster", run=run))
