"""The helper's local log: what ran and what the cost check decided.

It stays on this machine. `jup feedback` reads it when the user asks for a report.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

LOG = "log.jsonl"
SESSION = "session.json"
# The log is cut back to its newest lines when it passes this size: a line is a few
# hundred bytes, so this is some thousand runs.
_MAX_BYTES = 1_000_000
_KEEP_LINES = 2_000
# The most of one text that a record keeps, a command line for one.
_MAX_TEXT = 2_000


def state_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Return the directory of the helper's own files on this machine."""
    base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "hpc-tools" / "jupiter"


def now() -> str:
    """Return the time in UTC as the log writes it."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def clip(text: str) -> str:
    """Return a text short enough for one record."""
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + " ..."


def append(record: Mapping[str, Any], env: Mapping[str, str] = os.environ) -> None:
    """Add one record with the time; a log that cannot be written is left alone."""
    try:
        directory = state_dir(env)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / LOG
        with path.open("a") as log:
            log.write(json.dumps({"ts": now(), **record}) + "\n")
        if path.stat().st_size > _MAX_BYTES:
            lines = path.read_text().splitlines()[-_KEEP_LINES:]
            path.write_text("".join(f"{line}\n" for line in lines))
    except OSError:
        pass


def read(since: str = "", env: Mapping[str, str] = os.environ) -> list[dict[str, Any]]:
    """Return the records written at or after `since`, oldest first."""
    try:
        lines = (state_dir(env) / LOG).read_text().splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and str(record.get("ts", "")) >= since:
            records.append(record)
    return records


def remember_session(
    session: str, transcript: str, cwd: str, env: Mapping[str, str] = os.environ
) -> None:
    """Note the Claude Code session that last used the helper, and its transcript."""
    try:
        directory = state_dir(env)
        directory.mkdir(parents=True, exist_ok=True)
        known = {"session": session, "transcript": transcript, "cwd": cwd, "ts": now()}
        (directory / SESSION).write_text(json.dumps(known) + "\n")
    except OSError:
        pass


def last_session(env: Mapping[str, str] = os.environ) -> dict[str, str] | None:
    """Return the session that last used the helper, if one is noted."""
    try:
        known = json.loads((state_dir(env) / SESSION).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return known if isinstance(known, dict) and known.get("transcript") else None
