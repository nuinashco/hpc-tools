"""What a shell command submits to Slurm, and what each submission reserves."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from jup.errors import UserError

# The helper's own name, as it stands at the start of a command.
HELPER = "jup"
SUBMITTERS = ("sbatch", "salloc", "srun")
# Settings that decide what the cost check asks about; changing one is asked about too.
GUARD_SETTINGS = frozenset(
    {
        "ask_above_nodes",
        "ask_above_core_hours",
        "cores_per_node",
        "default_time",
        "max_time",
    }
)

# Options that take no value, for each command (Slurm 25.05, `has_arg` of each option in
# src/common/slurm_opt.c, read 2026-11-12). An option whose value is optional takes it
# only when glued on (`--exclusive=user`, `-k0`), so those stand here as well.
_SHARED_FLAGS = frozenset(
    {
        "contiguous",
        "exclusive",
        "help",
        "hold",
        "nice",
        "no-kill",
        "oom-kill-step",
        "overcommit",
        "oversubscribe",
        "propagate",
        "quiet",
        "reboot",
        "resv-ports",
        "spread-job",
        "stepmgr",
        "test-only",
        "usage",
        "use-min-nodes",
        "verbose",
        "version",
    }
)
_FLAGS = {
    "sbatch": _SHARED_FLAGS
    | {
        # `--dry-run` is the helper's own, for `jup submit`.
        "dry-run",
        "external",
        "get-user-env",
        "ignore-pbs",
        "no-requeue",
        "parsable",
        "requeue",
        "wait",
    },
    "salloc": _SHARED_FLAGS
    | {"bell", "immediate", "kill-command", "no-bell", "no-shell", "x11"},
    "srun": _SHARED_FLAGS
    | {
        "bcast",
        "compress",
        "debugger-test",
        "disable-status",
        "exact",
        "external-launcher",
        "immediate",
        "interactive",
        "kill-on-bad-exit",
        "label",
        "multi-prog",
        "no-allocate",
        "overlap",
        "preserve-env",
        "pty",
        "quit-on-interrupt",
        "send-libs",
        "unbuffered",
        "wait-for-children",
        "whole",
        "x11",
    },
}
# Letters without a value. For srun, -W takes one.
_SHORT_FLAGS = {"sbatch": "hHOsQvVW", "salloc": "hHOsQvV", "srun": "HlZOsEQuvVX"}
# Letters whose value is optional and must be glued on.
_SHORT_OPTIONAL = {"sbatch": "k", "salloc": "kIK", "srun": "kIK"}
_SHORT_NAMES = {
    "A": "account",
    "a": "array",
    "c": "cpus-per-task",
    "D": "chdir",
    "e": "error",
    "G": "gpus",
    "H": "hold",
    "J": "job-name",
    "N": "nodes",
    "n": "ntasks",
    "o": "output",
    "p": "partition",
    "Q": "quiet",
    "t": "time",
    "W": "wait",
    "w": "nodelist",
}
# Long options with a value that matter here, so that an abbreviation of one is read as
# Slurm reads it (`--arr=0-9` is `--array`).
_VALUED = frozenset(
    {
        "account",
        "array",
        "chdir",
        "cpus-per-task",
        "error",
        "export",
        "gpus",
        "gpus-per-node",
        "gpus-per-task",
        "gres",
        "jobid",
        "job-name",
        "nodelist",
        "nodes",
        "ntasks",
        "ntasks-per-gpu",
        "ntasks-per-node",
        "output",
        "partition",
        "reservation",
        "time",
        "time-min",
        "wrap",
    }
)
# Options from which Slurm works out a node count when --nodes is missing.
_NODE_HINTS = (
    "ntasks",
    "ntasks-per-node",
    "ntasks-per-gpu",
    "gpus",
    "gpus-per-node",
    "nodelist",
)
_NO_LIMIT = frozenset({"0", "-1", "unlimited", "infinite"})
_DIRECTIVE = re.compile(r"#SBATCH\s+(.*)$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SCHEDULER_VARIABLE = re.compile(r"^(SBATCH|SALLOC|SLURM|SRUN)_")
JOB_ID = re.compile(r"^\d+(_(\d+|\[[\d,%:-]+\]))?([+.]\w+)?$")

# What separates one command from the next; anything else of the shell's punctuation
# is a redirection.
_SEPARATORS = frozenset(";&|()\n")
# Words in front of a command that leave it the command.
_KEYWORDS = frozenset({"do", "then", "else", "elif", "{", "!", "if"})
_WRAPPERS = frozenset(
    {
        "builtin",
        "command",
        "env",
        "exec",
        "nice",
        "nohup",
        "parallel",
        "stdbuf",
        "sudo",
        "time",
        "xargs",
    }
)
_WRAPPERS_WITH_A_WORD = frozenset({"timeout"})
_LOOPS = frozenset({"for", "while", "until", "xargs", "parallel"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "eval"})
# Letters of ssh that take a value (ssh(1), OpenSSH 9).
_SSH_VALUED = frozenset("BbcDEeFIiJLlmOopQRSWw")
# What `scontrol update` can change that raises a job's cost.
_COSTLY_UPDATES = ("timelimit", "numnodes", "minnodes", "arraytaskthrottle")


def parse_time(text: str) -> int:
    """Return a Slurm time limit in seconds."""
    days, _, clock = text.strip().rpartition("-")
    try:
        numbers = [int(part) for part in clock.split(":")]
        whole_days = int(days) if days else 0
    except ValueError:
        raise UserError(
            f"{text!r} is no Slurm time; write minutes, H:M:S or D-H:M:S"
        ) from None
    if days:
        hours, minutes, seconds = [*numbers, 0, 0][:3]
    elif len(numbers) == 1:
        hours, minutes, seconds = 0, numbers[0], 0
    elif len(numbers) == 2:
        hours, minutes, seconds = 0, numbers[0], numbers[1]
    else:
        hours, minutes, seconds = numbers[:3]
    return ((whole_days * 24 + hours) * 60 + minutes) * 60 + seconds


def format_time(seconds: int) -> str:
    """Return seconds as H:MM:SS."""
    minutes, rest = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{rest:02d}"


def resolve(name: str, kind: str) -> str:
    """Return the long option a name or its unique abbreviation stands for."""
    known = _FLAGS[kind] | _VALUED
    if name in known:
        return name
    matches = [option for option in known if option.startswith(name)]
    return matches[0] if len(matches) == 1 else name


def parse_options(
    arguments: Sequence[str], kind: str = "sbatch"
) -> tuple[dict[str, str], list[str]]:
    """Split a submitter's arguments into its options and the rest, script first."""
    flags, letters = _FLAGS[kind], _SHORT_FLAGS[kind]
    options: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--":
            index += 1
            break
        if word.startswith("--"):
            name, glued, value = word[2:].partition("=")
            name = resolve(name, kind)
            if glued or name in flags:
                options[name] = value
            elif index + 1 < len(arguments):
                index += 1
                options[name] = arguments[index]
            else:
                options[name] = ""
        elif word.startswith("-") and len(word) > 1:
            # Letters may be bundled, the last one with its value: -HN4.
            for position, letter in enumerate(word[1:], start=2):
                name = _SHORT_NAMES.get(letter, letter)
                if letter in letters:
                    options[name] = ""
                    continue
                if letter in _SHORT_OPTIONAL[kind] or word[position:]:
                    options[name] = word[position:]
                elif index + 1 < len(arguments):
                    index += 1
                    options[name] = arguments[index]
                else:
                    options[name] = ""
                break
        else:
            break
        index += 1
    return options, list(arguments[index:])


def read_directives(script: str) -> dict[str, str]:
    """Return the options a batch script sets in its `#SBATCH` lines.

    Slurm reads them up to the first line that is neither blank nor a comment.
    """
    options: dict[str, str] = {}
    for line in script.splitlines():
        text = line.strip()
        if not text:
            continue
        if not text.startswith("#"):
            break
        match = _DIRECTIVE.match(text)
        if not match:
            continue
        # A comment ends a directive only after white space: --comment=a#b is a value.
        words = re.split(r"\s#", match.group(1), maxsplit=1)[0]
        try:
            found, rest = parse_options(shlex.split(words))
        except ValueError:
            continue
        options.update(found)
        if rest[:1] == ["hetjob"]:
            options["hetjob"] = ""
    return options


def find_script(arguments: Sequence[str], directories: Sequence[Path]) -> Path | None:
    """Return the batch script that sbatch `arguments` name, where it is found."""
    options, rest = parse_options(arguments)
    if "wrap" in options or not rest:
        return None
    for directory in directories:
        if (directory / rest[0]).is_file():
            return directory / rest[0]
    return None


def find_output_directories(options: Mapping[str, str]) -> list[str]:
    """Return the directories a job's output files go to; Slurm creates none of them."""
    found: list[str] = []
    for name in ("output", "error"):
        directory = options.get(name, "").rpartition("/")[0]
        # A pattern such as %j in a directory's name is filled in when the job starts.
        if directory and "%" not in directory and directory not in found:
            found.append(directory)
    return found


def count_array_tasks(spec: str) -> int:
    """Return how many jobs an `--array` value starts at most."""
    indices, _, _ = spec.partition("%")
    total = 0
    wrong = UserError(f"{spec!r} is no job array; write it as 0-9, 1,3,5 or 0-99%4")
    for part in indices.split(","):
        span, _, step = part.partition(":")
        first, dash, last = span.partition("-")
        try:
            count = (int(last) - int(first)) // int(step or 1) + 1 if dash else 1
            int(first)
        except (ValueError, ZeroDivisionError):
            raise wrong from None
        if count < 1:
            raise wrong
        total += count
    return total


@dataclass(frozen=True)
class Request:
    """The nodes and time a submission reserves, as far as its options say."""

    nodes: int
    seconds: int
    tasks: int
    # Why the real cost may be higher than these numbers give.
    doubts: tuple[str, ...] = ()

    @classmethod
    def of(
        cls,
        options: Mapping[str, str],
        directives: Mapping[str, str],
        *,
        default_time: str,
        max_time: str,
    ) -> Request:
        """Return what `options` over a script's `directives` ask for."""
        merged = {**directives, **options}
        doubts: list[str] = []
        # `--nodes` may be a range, of which Slurm grants up to the larger.
        wanted = merged.get("nodes", "1")
        try:
            nodes = int(wanted.rpartition("-")[2])
        except ValueError:
            raise UserError(
                f"{wanted!r} is no node count; write a number or a range such as 1-2"
            ) from None
        if "nodes" not in merged:
            hints = [f"--{name}" for name in _NODE_HINTS if name in merged]
            doubts.append(
                f"no --nodes, and {' '.join(hints)} decides how many"
                if hints
                else "no --nodes, one assumed"
            )
        limit = merged.get("time")
        if limit is None:
            doubts.append(f"no --time, the default {default_time} assumed")
            limit = default_time
        elif limit.strip().lower() in _NO_LIMIT:
            doubts.append(f"--time={limit} asks for no limit, {max_time} assumed")
            limit = max_time
        if "hetjob" in merged:
            doubts.append("a heterogeneous job, only its first part counted")
        if "no-shell" in merged:
            doubts.append("--no-shell keeps the nodes until the job is cancelled")
        tasks = count_array_tasks(merged["array"]) if "array" in merged else 1
        return cls(nodes, parse_time(limit), tasks, tuple(doubts))

    def core_hours(self, cores_per_node: int) -> float:
        """Return the most core-hours the request can be billed."""
        return self.tasks * self.nodes * cores_per_node * self.seconds / 3600

    def describe(self, cores_per_node: int, more: Sequence[str] = ()) -> str:
        """Return the request and its cost in words, with what makes them unsure."""
        jobs = f"{self.tasks} jobs of " if self.tasks > 1 else ""
        nodes = f"{self.nodes} node{'s' if self.nodes != 1 else ''}"
        text = (
            f"{jobs}{nodes} for up to {format_time(self.seconds)}: at most "
            f"{self.core_hours(cores_per_node):,.0f} core-hours"
        )
        doubts = [*self.doubts, *more]
        return f"{text} (not certain: {'; '.join(doubts)})" if doubts else text


@dataclass(frozen=True)
class Submission:
    """One thing a shell command does that spends, or may spend, the budget."""

    # A submitter (`sbatch`, `salloc`, `srun`), or `scontrol`, `scrontab` and `config`,
    # which change what an earlier check allowed.
    kind: str
    arguments: tuple[str, ...]
    # How it reaches a cluster: `jup`, `ssh`, or `local` for this machine.
    route: str
    # The host of an ssh route.
    host: str = ""
    # What about the command around it makes its cost uncertain.
    doubts: tuple[str, ...] = ()


def tokenize(command: str) -> list[str] | None:
    """Return a shell command's words and operators; None when it cannot be read."""
    # A backslash before a line break joins the lines; a backquote starts a command.
    text = command.replace("\\\n", " ").replace("`", " \n ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
    # Words keep their colons and commas, and a line break ends a command.
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    # `shlex` would cut a word at any #; the shell starts a comment only where a word
    # starts.
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    kept: list[str] = []
    in_comment = False
    for token in tokens:
        if in_comment:
            in_comment = "\n" not in token
        if token.startswith("#"):
            in_comment = True
        if not in_comment:
            kept.append(token)
    return kept


def split_commands(tokens: Sequence[str]) -> list[list[str]]:
    """Return the words of each simple command, redirections included."""
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token and all(character in _SEPARATORS for character in token):
            commands.append([])
        else:
            commands[-1].append(token)
    return [words for words in commands if words]


def _base(word: str) -> str:
    return word.rpartition("/")[2]


def _strip_prefix(words: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return a command without the words in front of it, and what they make unsure."""
    doubts: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if _ASSIGNMENT.match(word):
            if _SCHEDULER_VARIABLE.match(word):
                doubts.append(
                    f"{word.partition('=')[0]} is set, which overrides options"
                )
        elif _base(word) in _WRAPPERS_WITH_A_WORD:
            index += 1
        elif not (
            word in _KEYWORDS
            or _base(word) in _WRAPPERS
            # An option of a wrapper, or its number: `nice -n 5`, `xargs -n 1`.
            or word.startswith("-")
            or word.isdigit()
        ):
            break
        index += 1
    return list(words[index:]), doubts


def _drop_redirections(words: Sequence[str]) -> tuple[list[str], bool]:
    """Return a command's words without redirections, and whether it reads stdin."""
    kept: list[str] = []
    reads_input = False
    skip = False
    for word in words:
        if skip:
            skip = False
        elif any(character in "<>" for character in word):
            reads_input = reads_input or "<" in word
            skip = True
            # The number of a file descriptor stands before its redirection: 2>&1.
            if kept and kept[-1].isdigit():
                kept.pop()
        else:
            kept.append(word)
    return kept, reads_input


def scan(
    command: str, *, route: str = "local", host: str = ""
) -> list[Submission] | None:
    """Return what a shell command submits, also inside the commands it hands on.

    None when the command cannot be read as the shell reads it.
    """
    tokens = tokenize(command)
    if tokens is None:
        return None
    commands = split_commands(tokens)
    looped = any(_base(word) in _LOOPS for words in commands for word in words[:3])
    found: list[Submission] = []
    for raw in commands:
        words, doubts = _strip_prefix(raw)
        if looped:
            doubts.append("it runs in a loop, so how many jobs it starts is not known")
        if not words or words[0] in ("for", "while", "until"):
            continue
        name, rest = _base(words[0]), words[1:]
        if name in SUBMITTERS:
            arguments, reads_input = _drop_redirections(rest)
            if reads_input:
                doubts.append("its script comes from standard input")
            if any("$" in argument for argument in arguments):
                doubts.append("it holds a shell variable whose value is not known here")
            if ":" in arguments:
                doubts.append("a heterogeneous job, only its first part counted")
            found.append(Submission(name, tuple(arguments), route, host, tuple(doubts)))
        elif name in ("scontrol", "scrontab"):
            text = " ".join(rest).lower()
            costly = (
                name == "scrontab"
                or "requeue" in text
                or ("update" in text and any(key in text for key in _COSTLY_UPDATES))
            )
            if costly:
                found.append(Submission(name, tuple(rest), route, host))
        elif name == HELPER:
            nested = _scan_helper(rest, tuple(doubts))
            if nested is None:
                return None
            found += nested
        elif name == "ssh":
            target, remote = _split_ssh(rest)
            nested = scan(" ".join(remote), route="ssh", host=target) if remote else []
            if nested is None:
                return None
            found += nested
        elif name in _SHELLS:
            script = rest[rest.index("-c") + 1 :][:1] if "-c" in rest else []
            script = rest if name == "eval" else script
            nested = scan(" ".join(script), route=route, host=host) if script else []
            if nested is None:
                return None
            found += nested
    return found


def _scan_helper(
    words: Sequence[str], doubts: tuple[str, ...]
) -> list[Submission] | None:
    """Return what one command of the helper submits."""
    if words[:1] == ["submit"]:
        arguments, _ = _drop_redirections(words[1:])
        if any("$" in argument for argument in arguments):
            doubts = (
                *doubts,
                "it holds a shell variable whose value is not known here",
            )
        return [Submission("sbatch", tuple(arguments), HELPER, doubts=doubts)]
    if words[:1] == ["ssh"]:
        remote = [word for word in words[1:] if word != "-C"]
        return scan(" ".join(remote), route=HELPER) if remote else []
    if words[:2] == ["config", "set"] and words[2:3] and words[2] in GUARD_SETTINGS:
        return [Submission("config", tuple(words[2:]), HELPER)]
    return []


def _split_ssh(words: Sequence[str]) -> tuple[str, list[str]]:
    """Return the host of an ssh command, without its user, and the remote command."""
    index = 0
    while index < len(words):
        word = words[index]
        if not word.startswith("-") or word == "--":
            break
        # A letter that takes a value has it glued on or in the next word.
        index += 2 if len(word) == 2 and word[1] in _SSH_VALUED else 1
    if index < len(words) and words[index] == "--":
        index += 1
    if index >= len(words):
        return "", []
    return words[index].rpartition("@")[2], list(words[index + 1 :])
