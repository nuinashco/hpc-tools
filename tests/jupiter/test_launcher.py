"""What Claude Code starts: the launcher and the commands of hooks.json."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "jupiter"
HOOKS = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]


def start(
    command: str, event: dict, cwd: Path, env: dict
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN)),
        shell=True,
        input=json.dumps(event),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


@pytest.fixture
def env(tmp_path):
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path),
    }


def test_the_launcher_runs_and_prints_its_usage(env, tmp_path):
    done = subprocess.run(
        [str(PLUGIN / "bin" / "jup"), "help"], capture_output=True, text=True, env=env
    )

    assert done.returncode == 0
    assert "jup submit" in done.stdout


def test_the_cost_check_sees_every_bash_call(tmp_path, env):
    [group] = HOOKS["PreToolUse"]
    [handler] = group["hooks"]

    assert group["matcher"] == "Bash" and "if" not in handler
    event = {
        "tool_input": {"command": "jup submit -N 9 -t 9:00:00 job.sh"},
        "cwd": str(tmp_path),
    }
    done = start(handler["command"], event, tmp_path, env)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_the_cost_check_still_asks_when_the_settings_are_broken(tmp_path, env):
    (tmp_path / ".jupiter.json").write_text("{broken")
    [handler] = HOOKS["PreToolUse"][0]["hooks"]
    event = {"tool_input": {"command": "jup submit job.sh"}, "cwd": str(tmp_path)}

    done = start(handler["command"], event, tmp_path, env)

    assert done.returncode == 0, done.stderr
    assert "could not run" in done.stdout


def test_the_session_note_runs_and_is_silent_outside_a_project(tmp_path, env):
    [handler] = HOOKS["SessionStart"][0]["hooks"]

    done = start(handler["command"], {"cwd": str(tmp_path)}, tmp_path, env)

    assert (done.returncode, done.stdout) == (0, "")
