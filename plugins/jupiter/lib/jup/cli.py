"""`jup`: reach JSC's JUPITER from a project: check, copy, submit and watch jobs.

Usage:

    jup doctor [--probe]            check settings, SSH and the account
    jup config [set KEY VALUE [--project]]
    jup ssh [-C] COMMAND...         run a command on the login node (-C: in the project)
    jup push [--dry-run] [--all]    copy the project to the cluster
    jup pull PATTERN... [--to DIR] [--dry-run]
    jup cost SBATCH_ARGS...         what a submission would reserve and cost
    jup submit [--dry-run] SBATCH_ARGS...
    jup jobs [--days N]             queued, running and recent jobs
    jup wait JOB [--until-running] [--idle-ok]
                                    print each state change until the job ends, or
                                    until its GPUs sit idle (exit code 69)
    jup gpus JOB                    a running job's GPUs now: load and memory
    jup logs JOB [-n LINES]         the end of a job's output
    jup cancel JOB...
    jup budget                      what the account has left
    jup feedback NOTE... | --send FILE | --discard FILE

Commands that reach the cluster need the SSH connection the user opens with a TOTP
code; without it they exit with code 66 and say what the user runs. A message that
starts with `jup:` comes from this command, anything else from the cluster.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from jup import config as settings
from jup import feedback, gpus, hooks, journal, slurm, sync
from jup.config import Config
from jup.errors import ConfigError, RemoteError, UserError
from jup.ssh import MARK, Remote

# Seconds between two looks at a waiting job: its states last minutes, and each look is
# a command on a shared login node.
POLL_SECONDS = 60
# The least a caller may ask for.
LEAST_POLL_SECONDS = 30
# The launcher this code belongs to, to tell it from another `jup` on the PATH.
LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "jup"
_QUEUE = "%.12i %.10P %.30j %.10T %.10M %.10l %.5D %R"
_HISTORY = (
    "JobID%12,JobName%30,Partition%10,State%14,Elapsed,Timelimit,NNodes%6,ExitCode,End"
)
_OUTCOME = "JobID,State,Elapsed,ExitCode"
# States in which sacct shows a job that has not ended (sacct(1), job state codes).
_UNFINISHED = frozenset(
    {
        "PENDING",
        "RUNNING",
        "SUSPENDED",
        "REQUEUED",
        "RESIZING",
        "COMPLETING",
        "CONFIGURING",
    }
)
_SHARING = (
    "add `ControlMaster auto`, `ControlPath ~/.ssh/cm-%C` and `ControlPersist 12h` to "
    "its Host entry in ~/.ssh/config"
)
# Commands whose arguments go to sbatch, scancel or the remote shell.
_PASS_ON = frozenset({"ssh", "cost", "submit", "cancel"})
_OUTPUT_FILE = re.compile(r"^\s*Std(?:Out|Err)=(.+)$")
_JOB_FIELD = re.compile(r"^\s*(Command|WorkDir)=(.*)$", re.MULTILINE)
# What `jup wait` exits with when it stops for a job whose GPUs sit idle.
IDLE_GPUS = 69
_SUBMITTED = re.compile(r"^\d+(;\S+)?$")


@dataclass(frozen=True)
class Context:
    """What every command works with: where it runs, its settings, the cluster."""

    cwd: Path
    config: Config
    remote: Remote

    def target(self) -> str:
        """Return the project's directory on the cluster."""
        if not self.remote.has_entry():
            raise ConfigError(
                f"~/.ssh/config has no Host entry for {self.config.host}, so the "
                "login name on the cluster is unknown; the plugin's setup skill "
                "writes the entry"
            )
        return self.config.remote_path(self.remote.user())


def quote(arguments: Sequence[str]) -> str:
    """Return arguments as one shell string."""
    return " ".join(shlex.quote(argument) for argument in arguments)


def stamp() -> str:
    """Return the time of day, for a line of a job's history."""
    return time.strftime("%H:%M:%S")


def check_job(job: str) -> str:
    """Return a job number as it is, or refuse what is none."""
    if not slurm.JOB_ID.match(job):
        raise UserError(
            f"{job!r} is no job number; `jup jobs` lists them (123456, or 123456_7 "
            "for one task of an array)"
        )
    return job


def show_config(context: Context) -> None:
    """Print each setting with the file or variable it came from."""
    config = context.config
    for key in settings.DEFAULTS:
        print(f"{key} = {getattr(config, key)!r}   ({config.sources[key]})")
    print(f"project = {config.root}")
    try:
        print(f"remote directory = {context.target()}")
    except UserError as error:
        print(f"remote directory = unknown: {error}")


def run_config(arguments: Sequence[str], context: Context) -> int:
    parser = argparse.ArgumentParser(prog="jup config")
    parser.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    parser.add_argument("key", nargs="?")
    parser.add_argument("value", nargs="?")
    parser.add_argument("--project", action="store_true", help="write .jupiter.json")
    options = parser.parse_args(arguments)
    if options.action == "show":
        show_config(context)
        return 0
    if options.key is None or options.value is None:
        raise ConfigError("`jup config set` takes a setting and a value; pass both")
    if options.project:
        # The file that is read: the one in effect here, or a new one at the root.
        path = settings.find_project_file(context.cwd) or (
            settings.find_repository_root(context.cwd) / settings.PROJECT_FILE
        )
    else:
        path = settings.user_file()
    settings.write(path, options.key, options.value)
    print(f"{options.key} set in {path}")
    return 0


def find_public_address() -> str:
    """Return this machine's public IPv4 address, or nothing when it can't be asked."""
    try:
        done = subprocess.run(
            ["curl", "-4", "-s", "--max-time", "5", "https://api.ipify.org"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def run_doctor(arguments: Sequence[str], context: Context) -> int:
    parser = argparse.ArgumentParser(prog="jup doctor")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="with no connection open: make one login attempt and ask for the address",
    )
    options = parser.parse_args(arguments)
    config, remote = context.config, context.remote
    failures = 0

    def report(ok: bool, text: str, *, must: bool = True) -> None:
        nonlocal failures
        failures += must and not ok
        print(f"{'ok  ' if ok else 'FAIL' if must else 'warn'} {text}")

    ssh = remote.settings()
    entry = remote.has_entry()
    report(
        entry,
        f"ssh alias {config.host}: user {ssh.get('user')}, host {ssh.get('hostname')}"
        if entry
        else f"~/.ssh/config has no Host entry for {config.host}; the plugin's setup "
        "skill writes one",
    )
    shared = ssh.get("controlmaster") in {"auto", "yes", "true"} and (
        ssh.get("controlpath", "none") != "none"
    )
    report(
        shared,
        "ssh shares one connection between commands"
        if shared
        else "ssh opens a connection for each command, and each needs a TOTP code; "
        + _SHARING,
    )
    persists = ssh.get("controlpersist", "no") not in {"no", "false", "0"}
    report(
        persists,
        "the connection outlives the login that opened it (ControlPersist)"
        if persists
        else "without ControlPersist the connection closes with the terminal that "
        "opened it; " + _SHARING,
        must=False,
    )
    alive = ssh.get("serveraliveinterval", "0") not in {"0", ""}
    if not alive:
        report(
            False,
            "ServerAliveInterval is off, so a dead connection is noticed late; add "
            "`ServerAliveInterval 60` to the Host entry",
            must=False,
        )
    missing = "not set; run `jup config set account <project>`"
    report(bool(config.account), f"compute account: {config.account or missing}")
    first = shutil.which(LAUNCHER.name)
    if first and Path(first).resolve() != LAUNCHER:
        report(
            False,
            f"another `jup` comes first on the PATH ({first}); the skills would run "
            f"it and not this plugin's ({LAUNCHER})",
            must=False,
        )
    if not remote.is_open():
        print(
            f"FAIL no SSH connection is open; the user runs `ssh {config.host} true` "
            "and enters the TOTP code"
        )
        if options.probe:
            reason = remote.probe()
            print(f"     one login attempt without prompts: {reason or 'it succeeded'}")
            if address := find_public_address():
                print(f"     this machine's public address is {address}")
        else:
            print(
                "     if that login is refused, `jup doctor --probe` tells a refused "
                "key from a missing code: it makes one login attempt and asks "
                "api.ipify.org for this machine's address, so run it once, never "
                "in a loop"
            )
        return 1
    report(True, "the shared SSH connection is open")
    facts = dict(
        line.split("=", 1)
        for line in remote.read_lines(
            'echo "user=$(id -un)"; echo "groups=$(id -Gn)"; echo "node=$(hostname)"'
        )
        if "=" in line
    )
    groups = facts.get("groups", "").split()
    print(f"     logged in as {facts.get('user')} on {facts.get('node')}")
    print(f"     groups: {' '.join(groups)}")
    if config.account and entry:
        member = config.account in groups
        # A budget may have another name than the project's group, so this only warns.
        report(
            member,
            f"{facts.get('user')} is a member of {config.account}"
            if member
            else f"{config.account} is not among the login's groups; `jutil user "
            "projects` lists the projects, in case the account is misspelt",
            must=False,
        )
        target = context.target()
        there = remote.execute(f"test -d {shlex.quote(target)}").returncode == 0
        state = "there" if there else "not there yet (`jup push` creates it)"
        print(f"     project directory {target}: {state}")
    return 1 if failures else 0


def run_ssh(arguments: Sequence[str], context: Context) -> int:
    inside = bool(arguments) and arguments[0] == "-C"
    command = list(arguments[1:] if inside else arguments)
    if not command:
        raise UserError(
            "no command given; write `jup ssh squeue --me`, with -C first to run it "
            "in the project's directory"
        )
    cwd = context.target() if inside else None
    # One argument is a whole shell command; several are words to keep apart.
    script = command[0] if len(command) == 1 else quote(command)
    return context.remote.execute(script, cwd=cwd, capture=False).returncode


def run_push(arguments: Sequence[str], context: Context) -> int:
    parser = argparse.ArgumentParser(prog="jup push")
    parser.add_argument("--dry-run", action="store_true", help="only list the files")
    parser.add_argument(
        "--all",
        action="store_true",
        help="copy a directory that is no git repository, without caches and secrets",
    )
    options = parser.parse_args(arguments)
    config, target = context.config, context.target()
    listing = sync.push(
        context.remote,
        config.root,
        target,
        config.sync_exclude,
        dry_run=options.dry_run,
        everything=options.all,
    )
    listed = "the directory" if listing is None else f"{len(listing.files)} files"
    verb = "would copy" if options.dry_run else "copied"
    print(f"{verb} {listed} of {config.root} to {config.host}:{target}")
    for line in listing.notes() if listing else ():
        print(f"note: {line}")
    return 0


def choose_destination(pattern: str, to: Path | None, context: Context) -> Path:
    """Return where a pulled path lands here: inside the project, as it lies there."""
    root = context.config.root.resolve()
    parent = sync.check_pattern(pattern).rpartition("/")[0]
    if to is not None:
        destination = (context.cwd / to).resolve()
    elif any(character in parent for character in "*?["):
        raise UserError(
            f"{pattern!r} has a wildcard in a directory's name, so where its files "
            "belong here is not clear; pass --to DIR"
        )
    else:
        destination = (root / parent).resolve()
    if destination != root and not destination.is_relative_to(root):
        raise UserError(
            f"{destination} is outside the project {root}; pull into the project "
            "and move the files from there"
        )
    return destination


def run_pull(arguments: Sequence[str], context: Context) -> int:
    parser = argparse.ArgumentParser(prog="jup pull")
    parser.add_argument("patterns", nargs="+", help="paths in the project there")
    parser.add_argument("--to", type=Path, help="default: the same place here")
    parser.add_argument("--dry-run", action="store_true", help="only list the files")
    options = parser.parse_args(arguments)
    target = context.target()
    for pattern in options.patterns:
        destination = choose_destination(pattern, options.to, context)
        sync.pull(context.remote, target, pattern, destination, dry_run=options.dry_run)
        verb = "would pull" if options.dry_run else "pulled"
        print(f"{verb} {pattern} into {destination}, replacing files that differ")
    return 0


def run_cost(arguments: Sequence[str], context: Context) -> int:
    request = hooks.estimate(arguments, context.config, context.cwd)
    if request is None:
        raise UserError("the arguments only test; drop --dry-run or --test-only")
    print(request.describe(context.config.gpus_per_node))
    return 0


def read_remote_directives(context: Context, script: str) -> dict[str, str]:
    """Return the `#SBATCH` options of the copy of a script that the job will run."""
    try:
        lines = context.remote.read_lines(
            f"cat -- {shlex.quote(script)}", cwd=context.target()
        )
    except RemoteError as error:
        raise RemoteError(
            f"{script} or the project's directory is not on the cluster; run "
            "`jup push`, then submit again"
        ) from error
    return slurm.read_directives("\n".join(lines))


def run_submit(arguments: Sequence[str], context: Context) -> int:
    config = context.config
    options, rest = slurm.parse_options(arguments)
    # Only before the script: what follows it is the script's own.
    head = arguments[: len(arguments) - len(rest)]
    passed = [*(word for word in head if word != "--dry-run"), *rest]
    wrapped = "wrap" in options
    if not rest and not wrapped:
        raise UserError(
            "no batch script named; write `jup submit [sbatch options] SCRIPT "
            "[its arguments]`"
        )
    if "wait" in options:
        raise UserError(
            "--wait would hold the connection until the job ends; submit without it, "
            "then run `jup wait JOB`"
        )
    local = slurm.find_script(passed, (context.cwd, config.root))
    directives = (
        slurm.read_directives(local.read_text(errors="replace")) if local else {}
    )
    if "dry-run" not in options and not wrapped:
        there = read_remote_directives(context, rest[0])
        if local and there != directives:
            raise RemoteError(
                f"the copy of {rest[0]} on the cluster asks for something else than "
                f"the one here ({there} there, {directives} here); run `jup push`, "
                "then submit again"
            )
        directives = there
    request = slurm.Request.of(
        options, directives, default_time=config.default_time, max_time=config.max_time
    )
    chosen = {**directives, **options}
    body = local.read_text(errors="replace") if local else ""
    flags = slurm.find_gpu_flags(chosen, body, request.nodes, config.gpus_per_node)
    request = replace(request, flags=tuple(flags))
    added = []
    if "account" not in chosen:
        added.append(f"--account={config.need_account()}")
    if "partition" not in chosen:
        added.append(f"--partition={config.partition}")
    command = f"sbatch --parsable {quote([*added, *passed])}"
    if directories := slurm.find_output_directories(chosen):
        command = f"mkdir -p -- {quote(directories)} && {command}"
    print(request.describe(config.gpus_per_node))
    if local and config.gpus_per_node:
        with contextlib.suppress(ValueError):
            inside = str(local.resolve().relative_to(config.root.resolve()))
            print(f"GPU load: {hooks.describe_load(inside, config)}")
    if "dry-run" in options:
        print(f"would run in {context.target()}: {command}")
        return 0
    printed = context.remote.read_lines(command, cwd=context.target())
    # With --parsable sbatch prints `job` or `job;cluster`.
    if not printed or not _SUBMITTED.match(printed[-1]):
        raise RemoteError(
            f"sbatch printed {printed!r} and no job number; look at `jup jobs` before "
            "submitting again, since the job may be queued"
        )
    job = printed[-1].split(";")[0]
    print(f"submitted job {job}; follow it with `jup wait {job}`")
    return 0


def run_jobs(arguments: Sequence[str], context: Context) -> int:
    parser = argparse.ArgumentParser(prog="jup jobs")
    parser.add_argument("--days", type=int, default=2, help="how far back to look")
    options = parser.parse_args(arguments)
    if options.days < 1:
        raise UserError("--days is 1 or more; write `jup jobs --days 7`")
    script = (
        f"squeue --me -o {shlex.quote(_QUEUE)}; echo; "
        f"sacct -X --starttime now-{options.days}days -o {_HISTORY}"
    )
    return context.remote.execute(script, capture=False).returncode


def read_outcome(
    remote: Remote, job: str, sleep: Callable[[float], None] = time.sleep
) -> list[list[str]]:
    """Return the accounting rows of a job that left the queue, one for each task.

    The accounting database can lag the queue by some seconds.
    """
    rows: list[list[str]] = []
    for _ in range(6):
        lines = remote.read_lines(f"sacct --jobs={job} -X -n -P -o {_OUTCOME}")
        rows = [line.split("|") for line in lines if line.count("|") >= 3]
        if rows and not any(row[1].split()[0] in _UNFINISHED for row in rows):
            return rows
        sleep(10)
    if not rows:
        raise RemoteError(
            f"Slurm has no record of job {job}; check the number with `jup jobs`"
        )
    states = ", ".join(sorted({row[1] for row in rows}))
    raise RemoteError(
        f"the queue no longer lists job {job}, but accounting still shows it as "
        f"{states}; look again with `jup jobs` in a minute"
    )


def read_job_script(remote: Remote, job: str) -> str:
    """Return a job's batch script as a path inside the directory it was submitted in.

    Nothing for a job without one, or one whose script lies elsewhere.
    """
    shown = remote.execute(f"scontrol show job {job}")
    fields = dict(_JOB_FIELD.findall(shown.stdout))
    command = fields.get("Command", "").split(" ")[0]
    directory = fields.get("WorkDir", "").rstrip("/")
    if not directory or not command.startswith(f"{directory}/"):
        return ""
    return command[len(directory) + 1 :]


def keep_load(watch: gpus.Watch, job: str, context: Context) -> None:
    """Remember what a job's GPUs did, under the script it ran."""
    script = read_job_script(context.remote, job)
    if not script:
        return
    root = context.config.root
    result = {
        **watch.result(),
        "job": job,
        "when": journal.now(),
        "digest": gpus.read_digest(root / script),
    }
    gpus.remember(gpus.name_script(root, script), result)


def run_wait(
    arguments: Sequence[str],
    context: Context,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(prog="jup wait")
    parser.add_argument("job")
    parser.add_argument("--interval", type=int, default=POLL_SECONDS, help="seconds")
    parser.add_argument(
        "--until-running", action="store_true", help="end as soon as the job runs"
    )
    parser.add_argument(
        "--idle-ok", action="store_true", help="go on waiting when GPUs are idle"
    )
    options = parser.parse_args(arguments)
    if options.interval < LEAST_POLL_SECONDS:
        raise UserError(
            f"--interval is at least {LEAST_POLL_SECONDS} s: each look is a command "
            "on a shared login node"
        )
    remote, job, config = context.remote, check_job(options.job), context.config
    warmup = slurm.parse_time(config.gpu_warmup)
    watch = gpus.Watch.of(config.gpu_idle_minutes, options.interval)
    seen, misses = "", 0
    while True:
        look = remote.execute(
            f"squeue --noheader --jobs={job} -o {shlex.quote(MARK + '%T|%M|%r|%D|%l')}"
        )
        if look.returncode:
            # The queue forgets a job some minutes after it ended.
            if "Invalid job id" in (look.stderr or ""):
                break
            misses += 1
            if misses == 3:
                detail = (look.stderr or "").strip().splitlines()[-1:] or ["no message"]
                raise RemoteError(
                    f"squeue failed three times in a row ({detail[0]}); the job may "
                    "still run: look with `jup jobs`"
                )
            sleep(options.interval)
            continue
        misses = 0
        rows = [
            line[len(MARK) :].split("|")
            for line in look.stdout.splitlines()
            if line.startswith(MARK)
        ]
        if not rows:
            break
        states = Counter(row[0] for row in rows)
        if len(rows) == 1:
            state, elapsed, reason, nodes, limit = [*rows[0], "", "", "", ""][:5]
            held = state == "PENDING" and reason not in {"", "None"}
            now = f"{state}{f' ({reason})' if held else ''} after {elapsed}"
        else:
            state, elapsed, nodes, limit = "", "", "", ""
            now = ", ".join(
                f"{count} {state}" for state, count in sorted(states.items())
            )
        if now != seen:
            print(f"{stamp()} job {job} {now}", flush=True)
            seen = now
        if options.until_running and "RUNNING" in states:
            return 0
        if state == "RUNNING" and config.gpus_per_node and nodes.isdigit():
            spent = read_seconds(elapsed)
            if spent >= warmup:
                watch.add(gpus.sample(remote, job, int(nodes)))
            if watch.idle() and not options.idle_ok:
                keep_load(watch, job, context)
                report_idle(watch, job, int(nodes), spent, read_seconds(limit), config)
                return IDLE_GPUS
        sleep(options.interval)
    rows = read_outcome(remote, job, sleep)
    for row in rows[:20]:
        print(
            f"{stamp()} job {row[0]} {row[1]} after {row[2]}, exit code {row[3]}",
            flush=True,
        )
    if len(rows) > 20:
        print(f"... and {len(rows) - 20} more tasks; `jup jobs` lists them", flush=True)
    if watch.readings:
        keep_load(watch, job, context)
        for line in watch.describe():
            print(f"  {line}", flush=True)
    return 0 if all(row[1] == "COMPLETED" for row in rows) else 1


def read_seconds(text: str) -> int:
    """Return a time that Slurm printed as seconds; 0 for what is none."""
    try:
        return slurm.parse_time(text)
    except UserError:
        return 0


def report_idle(
    watch: gpus.Watch,
    job: str,
    nodes: int,
    spent: int,
    limit: int,
    config: Config,
    env: Mapping[str, str] = os.environ,
) -> None:
    """Print that a job's GPUs sit idle, what that costs, and what can be done."""
    idle, total = len(watch.idle()), len(watch.means())
    per_node = config.gpus_per_node
    so_far = slurm.describe_cost(nodes * spent / 3600, per_node)
    at_most = slurm.describe_cost(nodes * max(limit, spent) / 3600, per_node)
    print(
        f"{stamp()} job {job}: {idle} of its {total} GPUs idle in each of the last "
        f"{watch.patience} readings, {slurm.format_time(spent)} into the job",
        flush=True,
    )
    for line in watch.describe():
        print(f"  {line}", flush=True)
    # Claude Code sets CLAUDECODE in the shells it starts: there Claude reads this
    # report and the person does not, so the choice reaches them as a question.
    if env.get("CLAUDECODE"):
        choice = (
            "what happens next is the user's choice\n"
            "  Claude: ask the user now, in a question that holds the numbers above "
            "and these two\n  answers, and run the one they pick:"
        )
    else:
        choice = "to choose what happens next, run one of:"
    print(
        f"  it pays for all of them: {so_far} so far, {at_most} if it runs to its "
        f"limit\n  the job still runs and nothing was cancelled; {choice}\n"
        f"    jup cancel {job}            stops the job\n"
        f"    jup wait {job} --idle-ok    keeps it and goes on waiting",
        flush=True,
    )


def run_gpus(arguments: Sequence[str], context: Context) -> int:
    parser = argparse.ArgumentParser(prog="jup gpus")
    parser.add_argument("job")
    options = parser.parse_args(arguments)
    remote, job = context.remote, check_job(options.job)
    rows = [
        line.split("|")
        for line in remote.read_lines(
            f"squeue --noheader --jobs={job} -o {shlex.quote('%T|%D')}"
        )
    ]
    if len(rows) != 1 or rows[0][0] != "RUNNING" or not rows[0][-1].isdigit():
        state = rows[0][0] if len(rows) == 1 else "not one job in the queue"
        raise UserError(
            f"job {job} is {state}; its GPUs can be read while it runs, one job at "
            "a time"
        )
    readings = gpus.sample(remote, job, int(rows[0][1]))
    if not readings:
        raise RemoteError(
            f"the GPUs of job {job} could not be read; look at the job with `jup "
            f"ssh 'srun --jobid={job} --overlap -n1 nvidia-smi'`"
        )
    for line in gpus.describe_readings(readings):
        print(line)
    return 0


def run_logs(arguments: Sequence[str], context: Context) -> int:
    parser = argparse.ArgumentParser(prog="jup logs")
    parser.add_argument("job")
    parser.add_argument("-n", "--lines", type=int, default=50)
    options = parser.parse_args(arguments)
    job = check_job(options.job)
    shown = context.remote.execute(f"scontrol show job {job}")
    paths: list[str] = []
    for line in shown.stdout.splitlines():
        match = _OUTPUT_FILE.match(line)
        if match and match.group(1) not in (*paths, "(null)"):
            paths.append(match.group(1))
    if shown.returncode or not paths:
        raise RemoteError(
            f"Slurm no longer holds job {job}'s record; read its output file by name "
            "with `jup ssh -C 'tail -n 50 FILE'` (the script's --output names it; "
            "otherwise slurm-<job>.out where it was submitted)"
        )
    script = "; ".join(
        f"printf '==> %s\\n' {shlex.quote(path)}; "
        f"tail -n {options.lines} -- {shlex.quote(path)}"
        for path in paths[:8]
    )
    return context.remote.execute(script, capture=False).returncode


def run_cancel(arguments: Sequence[str], context: Context) -> int:
    if not arguments:
        raise UserError("no job named; write `jup cancel JOB...`")
    jobs = [check_job(argument) for argument in arguments]
    context.remote.read(f"scancel {quote(jobs)}")
    print(f"cancelled {' '.join(jobs)}")
    return 0


def describe_budget_unit(config: Config) -> str:
    """Return how `jutil`'s core-hours turn into the hours this helper speaks of."""
    cores = config.cores_per_node
    text = (
        f"jutil counts core-hours: on {config.partition} a node-hour is "
        f"{cores:,} of them"
    )
    if config.gpus_per_node:
        text += f" and a GPU-hour {cores / config.gpus_per_node:,.0f}"
    return f"{text}; divide its numbers to compare them with `jup cost`"


def read_budgets(text: str) -> list[dict[str, str]]:
    """Return the budgets in jutil's `name=value` lines, one for each `system=`."""
    budgets: list[dict[str, str]] = []
    for line in text.splitlines():
        name, found, value = line.strip().partition("=")
        if not found:
            continue
        if name == "system" or not budgets:
            budgets.append({})
        budgets[-1][name] = value
    return [budget for budget in budgets if "coreh-quota-total" in budget]


def describe_budget(budget: Mapping[str, str], config: Config) -> str:
    """Return one budget of jutil's in node-hours and GPU-hours."""

    def hours(name: str) -> str:
        try:
            cores = float(budget.get(name, ""))
        except ValueError:
            return "an amount jutil did not give"
        return slurm.describe_cost(cores / config.cores_per_node, config.gpus_per_node)

    name = budget.get("budget-account", "the account")
    start = budget.get("acct-period-start", "?")
    period = f"{start} to {budget.get('acct-period-end', '?')}"
    share = budget.get("coreh-avail-percent")
    left = f", {share}% of it" if share else ""
    return (
        f"{name}, {period}: {hours('coreh-quota-total')}\n"
        f"  used: {hours('coreh-consumed-total')}, of which this month "
        f"{hours('coreh-consumed-curr-month')}\n"
        f"  left: {hours('coreh-avail')}{left}"
    )


def run_budget(arguments: Sequence[str], context: Context) -> int:
    parser = argparse.ArgumentParser(prog="jup budget")
    parser.add_argument("--raw", action="store_true", help="jutil's own output")
    options = parser.parse_args(arguments)
    config = context.config
    account = shlex.quote(config.need_account())
    done = context.remote.execute(f"jutil project cpuquota -p {account}")
    if done.returncode:
        print(done.stdout, end="")
        print(done.stderr, end="", file=sys.stderr)
        return done.returncode
    budgets = [] if options.raw else read_budgets(done.stdout)
    if not budgets:
        # A form of jutil's output this code does not know is shown as it came.
        print(done.stdout, end="")
        print(describe_budget_unit(config))
        return 0
    for budget in budgets:
        print(describe_budget(budget, config))
    print(
        "as of last midnight: jutil counts once a night, and in core-hours "
        f"({config.cores_per_node:,} a node-hour); --raw shows its own output"
    )
    return 0


def read_version() -> str:
    """Return the plugin's version from its manifest."""
    manifest = LAUNCHER.parents[1] / ".claude-plugin" / "plugin.json"
    try:
        return str(json.loads(manifest.read_text()).get("version", "unknown"))
    except (OSError, json.JSONDecodeError):
        return "unknown"


def run_feedback(arguments: Sequence[str], context: Context) -> int:
    parser = argparse.ArgumentParser(prog="jup feedback")
    parser.add_argument(
        "note", nargs="*", help="what went wrong, in the user's words; - reads stdin"
    )
    parser.add_argument("--transcript", type=Path, help="default: the session in use")
    parser.add_argument(
        "--whole", action="store_true", help="every turn of the session"
    )
    parser.add_argument(
        "--digest",
        type=Path,
        metavar="REPORT",
        help="write a report's digest, read from standard input",
    )
    parser.add_argument(
        "--send", type=Path, metavar="FILE", help="file a digest or a report"
    )
    parser.add_argument(
        "--discard", type=Path, metavar="FILE", help="delete a report and its digest"
    )
    options = parser.parse_args(arguments)
    config = context.config
    if options.discard:
        report = feedback.check_report(options.discard)
        report.unlink()
        feedback.find_digest(report).unlink(missing_ok=True)
        print(f"deleted {options.discard}; nothing was sent")
        return 0
    readers = feedback.describe_readers(
        config.feedback_repo,
        feedback.read_visibility(config.feedback_repo, context.remote.run),
    )
    if options.digest:
        report = feedback.check_report(options.digest)
        redact = feedback.Redactor.for_user(
            login=context.remote.user() if context.remote.has_entry() else "",
            account=config.account,
        )
        digest = feedback.write_digest(report, sys.stdin.read(), redact)
        print(f"digest: {digest} ({len(digest.read_text()):,} characters)")
        print(f"taken out of it: {redact.summary()}")
        print(readers)
        print(
            "nothing has been sent; after the user has read the digest and agreed, "
            f"`jup feedback --send {digest}` files it as an issue; the report it "
            "was made from stays here"
        )
        return 0
    if options.send:
        report = feedback.check_report(options.send)
        address = feedback.send(report, config.feedback_repo)
        print(f"filed as {address}")
        return 0
    transcript = options.transcript
    if transcript is None:
        known = journal.last_session()
        if known is None:
            raise UserError(
                "no Claude Code session is known to have used `jup` yet; pass the "
                "session's file with --transcript (~/.claude/projects/<project>/"
                "<session>.jsonl)"
            )
        transcript = Path(known["transcript"])
        print(f"session: {transcript.stem}, last active {known.get('ts')}")
    redact = feedback.Redactor.for_user(
        login=context.remote.user() if context.remote.has_entry() else "",
        account=config.account,
    )
    shown = {
        "partition": config.partition,
        "account": "set" if config.account else "not set",
        "ask_above_nodes": config.ask_above_nodes,
        "ask_above_node_hours": config.ask_above_node_hours,
    }
    # From standard input, text reaches here without passing through a shell.
    note = sys.stdin.read() if options.note == ["-"] else " ".join(options.note)
    built = feedback.build(
        note,
        transcript,
        host=config.host,
        redact=redact,
        version=read_version(),
        settings=shown,
        whole=options.whole,
    )
    print(f"report: {built.path} ({built.characters:,} characters)")
    print(
        f"holds the latest {built.turns_shown} of {built.turns_related} turns that "
        f"used the plugin (the session has {built.turns_total}), and the helper's log"
    )
    print(f"taken out: {built.redacted}")
    print(readers)
    print(
        "nothing has been sent; `jup feedback --digest "
        f"{built.path}` takes a digest of it on standard input, and `--send FILE` "
        "files a digest or a report as an issue once the user has read it and agreed"
    )
    return 0


COMMANDS: dict[str, Callable[[Sequence[str], Context], int]] = {
    "doctor": run_doctor,
    "config": run_config,
    "ssh": run_ssh,
    "push": run_push,
    "pull": run_pull,
    "cost": run_cost,
    "submit": run_submit,
    "jobs": run_jobs,
    "wait": run_wait,
    "gpus": run_gpus,
    "logs": run_logs,
    "cancel": run_cancel,
    "budget": run_budget,
    "feedback": run_feedback,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run one `jup` command; return its exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    name, rest = arguments[0], arguments[1:]
    try:
        if name == "hook":
            # Before the settings are read: a hook must answer when they are broken.
            if len(rest) != 1:
                raise UserError("`jup hook` takes one name: pre-bash or session-start")
            if printed := hooks.run(rest[0], sys.stdin.read(), Path.cwd()):
                print(printed)
            return 0
        if name not in COMMANDS:
            raise UserError(f"unknown command {name}; `jup help` lists the commands")
        if name in _PASS_ON and rest[:1] in (["-h"], ["--help"]):
            # These hand their arguments on, so they have no parser to answer this.
            print(__doc__)
            return 0
        return run_logged(name, rest)
    except UserError as error:
        print(f"jup: {error}", file=sys.stderr)
        return error.exit_code
    except FileNotFoundError as error:
        program = error.filename or str(error)
        print(
            f"jup: {program} was not found; install it or fix the path", file=sys.stderr
        )
        return UserError.exit_code
    except KeyboardInterrupt:
        return 130


def run_logged(name: str, rest: Sequence[str]) -> int:
    """Run a command and note in the local log how it ended."""
    started = time.monotonic()
    # Set before the settings are read, so that a run they break is logged too.
    logging, code, failure = True, 1, ""
    try:
        cwd = Path.cwd()
        config = Config.load(cwd)
        logging = config.log
        code = COMMANDS[name](rest, Context(cwd, config, Remote(config.host)))
        return code
    except UserError as error:
        code, failure = error.exit_code, f"{type(error).__name__}: {error}"
        raise
    except BaseException as error:
        failure = type(error).__name__
        raise
    finally:
        if logging:
            journal.append(
                {
                    "kind": "run",
                    "name": name,
                    "args": [journal.clip(word) for word in rest],
                    "exit": code,
                    "seconds": round(time.monotonic() - started, 1),
                    "error": journal.clip(failure),
                }
            )
