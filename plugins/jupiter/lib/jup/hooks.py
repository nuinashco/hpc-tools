"""Claude Code hooks: ask before a costly submission, and note a set-up project."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from jup import slurm
from jup.config import Config, find_project_file
from jup.errors import UserError
from jup.ssh import Remote

# A command without any of these words cannot submit; the check runs for every Bash
# call, so it must be done with the others at once.
_TRIGGERS = (*slurm.SUBMITTERS, "scontrol", "scrontab", slurm.HELPER)
# Names of JUPITER's own nodes (JSC's batch page: jpbl-sNN-NN to log in, jpbo-NNN-NN to
# compute), where a plain `sbatch` is a submission to it.
_NODE_PREFIXES = ("jpbl-", "jpbo-")
_DOMAIN = "jupiter.fz-juelich.de"
_SHARED = "The compute budget is shared by the project's members."


def is_cluster_node() -> bool:
    """Report whether this machine is one of JUPITER's nodes."""
    name = socket.gethostname()
    return name.startswith(_NODE_PREFIXES) or name.endswith(_DOMAIN)


def aims_here(submission: slurm.Submission, config: Config, on_cluster: bool) -> bool:
    """Report whether a submission goes to this cluster and not to another system."""
    if submission.route == slurm.HELPER:
        return True
    if submission.route == "ssh":
        return submission.host == config.host or submission.host.endswith(_DOMAIN)
    return on_cluster


def estimate(
    arguments: Sequence[str], config: Config, cwd: Path, kind: str = "sbatch"
) -> slurm.Request | None:
    """Return what one submission reserves; None when it reserves nothing."""
    options, rest = slurm.parse_options(arguments, kind)
    if "test-only" in options or "dry-run" in options:
        return None
    if kind == "srun" and "jobid" in options:
        # It joins a job that is running.
        return None
    directives: dict[str, str] = {}
    doubts: list[str] = []
    if kind == "sbatch" and rest and "wrap" not in options:
        script = slurm.find_script(arguments, (cwd, config.root))
        if script is None:
            doubts.append(f"its script {rest[0]} is not here to read its #SBATCH lines")
        else:
            directives = slurm.read_directives(script.read_text(errors="replace"))
    request = slurm.Request.of(
        options, directives, default_time=config.default_time, max_time=config.max_time
    )
    return replace(request, doubts=(*request.doubts, *doubts))


def ask(reason: str) -> dict[str, Any]:
    """Return the PreToolUse output that makes Claude Code ask the user."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def guard_submission(
    event: Mapping[str, Any], config: Config, *, on_cluster: bool | None = None
) -> dict[str, Any] | None:
    """Return a decision that asks the user when a command may cost much.

    It asks whenever it cannot read the cost for certain, and stays silent only for a
    submission it can price below the limits.
    """
    command = str((event.get("tool_input") or {}).get("command", ""))
    cwd = Path(str(event.get("cwd") or "."))
    found = slurm.scan(command)
    if found is None:
        return ask(
            "This command may submit to JUPITER, but it could not be read the way "
            f"the shell reads it, so its cost is unknown. {_SHARED}"
        )
    here = is_cluster_node() if on_cluster is None else on_cluster
    lines: list[str] = []
    unsure = False
    hours = 0.0
    nodes = 0
    for submission in found:
        if not aims_here(submission, config, here):
            continue
        if submission.kind == "config":
            unsure = True
            lines.append(
                f"it changes {submission.arguments[0]}, a setting of this check"
            )
        elif submission.kind not in slurm.SUBMITTERS:
            unsure = True
            lines.append(f"`{submission.kind}` here can raise what a job costs")
        else:
            try:
                request = estimate(submission.arguments, config, cwd, submission.kind)
            except UserError as error:
                unsure = True
                lines.append(
                    f"a {submission.kind} whose cost could not be read ({error})"
                )
                continue
            if request is None:
                continue
            unsure = unsure or bool(request.doubts or submission.doubts)
            hours += request.core_hours(config.cores_per_node)
            nodes += request.nodes * request.tasks
            lines.append(request.describe(config.cores_per_node, submission.doubts))
    over = hours > config.ask_above_core_hours or nodes > config.ask_above_nodes
    if not lines or not (unsure or over):
        return None
    total = f" In all at most {hours:,.0f} core-hours." if len(lines) > 1 else ""
    return ask(f"This submits to JUPITER: {'; '.join(lines)}.{total} {_SHARED}")


def note(text: str) -> dict[str, Any]:
    """Return the SessionStart output that adds `text` to what Claude knows."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }


def describe_project(
    event: Mapping[str, Any], connect: Callable[[str], Remote] = Remote
) -> dict[str, Any] | None:
    """Return SessionStart context for a project that has a `.jupiter.json`."""
    cwd = Path(str(event.get("cwd") or "."))
    if find_project_file(cwd) is None:
        return None
    start = (
        "This project has a .jupiter.json: it is set up for JSC's JUPITER "
        "supercomputer through the jupiter plugin (the `jup` command)."
    )
    try:
        config = Config.load(cwd)
        remote = connect(config.host)
        if not remote.has_entry():
            return note(
                f"{start} The ssh alias {config.host} has no entry in ~/.ssh/config on "
                "this machine yet, so the cluster cannot be reached before the "
                "plugin's setup is done."
            )
        where = config.remote_path(remote.user()) if config.account else "not set yet"
        state = (
            "open"
            if remote.is_open()
            else "closed (opening it needs the user's TOTP code)"
        )
    except UserError as error:
        return note(f"{start} Its settings cannot be used as they are: {error}.")
    return note(
        f"{start} Compute account: {config.account or 'not set'}. SSH host alias: "
        f"{config.host}. Project directory on the cluster: {where}. The shared SSH "
        f"connection is {state}."
    )


def run(name: str, stdin: str, cwd: Path) -> str:
    """Return what the hook `name` prints for the event Claude Code sent on stdin.

    A hook that fails lets the call it guards go ahead, so neither hook raises: the
    cost check asks when it cannot run, the note stays silent.
    """
    if name not in ("pre-bash", "session-start"):
        raise UserError(
            f"unknown hook {name}; the hooks are pre-bash and session-start"
        )
    try:
        event = json.loads(stdin) if stdin.strip() else {}
    except json.JSONDecodeError:
        event = {}
    if not isinstance(event, dict):
        event = {}
    event.setdefault("cwd", str(cwd))
    if name == "session-start":
        try:
            result = describe_project(event)
        except Exception:
            result = None
        return json.dumps(result) if result else ""
    command = str((event.get("tool_input") or {}).get("command", ""))
    if not any(trigger in command for trigger in _TRIGGERS):
        return ""
    try:
        result = guard_submission(event, Config.load(Path(str(event["cwd"]))))
    except Exception as error:
        submits = any(word in command for word in (*slurm.SUBMITTERS, "submit"))
        result = (
            ask(
                f"The cost check for JUPITER could not run ({type(error).__name__}: "
                f"{error}), so read this command's cost yourself. {_SHARED}"
            )
            if submits
            else None
        )
    return json.dumps(result) if result else ""
