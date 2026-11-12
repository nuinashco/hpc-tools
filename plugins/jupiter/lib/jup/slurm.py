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
        "ask_above_node_hours",
        "cores_per_node",
        "gpus_per_node",
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
        # `--dry-run` and `--stale-ok` are the helper's own, for `jup submit`.
        "dry-run",
        "stale-ok",
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
# Programs that hand their standard input on unchanged, or nearly: what they read
# reaches the next command of a pipe.
_FILTERS = frozenset(
    {"cat", "tee", "sed", "grep", "tr", "head", "tail", "sort", "uniq", "cut"}
)
# Programs that only read their standard input: it is data, and runs nothing.
_SINKS = frozenset(
    {
        "wc",
        "jq",
        "patch",
        "dd",
        "git",
        "gh",
        "curl",
        "diff",
        "base64",
        "shasum",
        "sha256sum",
        "md5sum",
        "xxd",
        "pbcopy",
        "less",
        "more",
    }
)
# Programs whose standard input is a script in a language this code does not read.
_INTERPRETERS = re.compile(r"^(python[\d.]*|node|perl|ruby|php|lua|Rscript|awk)$")
# Of these, the ones where only a call starts a program, which a pattern can find.
_READABLE = re.compile(r"^(python[\d.]*|node)$")
_STARTS_PROGRAMS = re.compile(
    r"subprocess|popen|child_process|\bpty\b|\bos\.(?:system|exec|spawn|posix_spawn)"
    r"|\b(?:system|exec\w*|spawn\w*)\s*\(",
    re.IGNORECASE,
)
# The words one of which a text must hold to submit anything.
NAMES_A_SUBMITTER = re.compile(
    rf"\b(?:{'|'.join((*SUBMITTERS, 'scontrol', 'scrontab', HELPER))})\b"
)
# What stands for a here-document's body in a command once the body is lifted out.
_MARK = "JUP_HERE_DOCUMENT_"
# What a script lets its programs see of the node's GPUs.
_VISIBLE_GPUS = re.compile(r"\bCUDA_VISIBLE_DEVICES=(\S*)")
# Where a here-document's word ends.
_WORD_ENDS = frozenset(" \t;&|()<>")
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


def format_hours(hours: float) -> str:
    """Return a number of hours with the decimals its size needs: 0.67, 2.7, 48."""
    if hours >= 100:
        return f"{hours:,.0f}"
    text = f"{hours:.1f}" if hours >= 10 else f"{hours:.2f}"
    return text.rstrip("0").rstrip(".")


def _count(hours: float, unit: str) -> str:
    amount = format_hours(hours)
    return f"{amount} {unit}{'' if amount == '1' else 's'}"


def describe_cost(node_hours: float, gpus_per_node: int) -> str:
    """Return a cost in node-hours, and in GPU-hours where the nodes have GPUs."""
    text = _count(node_hours, "node-hour")
    if gpus_per_node:
        text += f" ({_count(node_hours * gpus_per_node, 'GPU-hour')})"
    return text


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


def describe_submission(line: str) -> str:
    """Return what a job runs, from accounting's record of the line that submitted it.

    The script with its arguments, or the wrapped command. Slurm joins the words of
    the line with spaces and quotes none, so the answer is for reading, not running:
    an option's value that held a space leaves its tail in front of the script.
    """
    words = line.split()
    if not words or words[0].rpartition("/")[2] != "sbatch":
        return " ".join(words)
    for index, word in enumerate(words):
        if word == "--wrap" or word.startswith("--wrap="):
            command = [word.partition("=")[2], *words[index + 1 :]]
            return f"--wrap {' '.join(command).strip()}"
    _, rest = parse_options(words[1:])
    if not rest:
        return " ".join(words[1:])
    return " ".join([rest[0].rpartition("/")[2], *rest[1:]])


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
    # What in the request leaves GPUs it pays for unused.
    flags: tuple[str, ...] = ()
    # The batch script it was read from, as a path inside the project.
    script: str = ""

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
        # Without --nodes Slurm gives what the tasks need: one node for one task.
        hints = [f"--{name}" for name in _NODE_HINTS if name in merged]
        if "nodes" not in merged and hints:
            doubts.append(f"no --nodes, and {' '.join(hints)} decides how many")
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

    def node_hours(self) -> float:
        """Return the most node-hours the request can be billed: nodes go whole."""
        return self.tasks * self.nodes * self.seconds / 3600

    def describe(self, gpus_per_node: int, more: Sequence[str] = ()) -> str:
        """Return the request and its cost in words, with what makes them unsure."""
        jobs = f"{self.tasks} jobs of " if self.tasks > 1 else ""
        nodes = f"{self.nodes} node{'s' if self.nodes != 1 else ''}"
        text = (
            f"{jobs}{nodes} for up to {format_time(self.seconds)}: at most "
            f"{describe_cost(self.node_hours(), gpus_per_node)}"
        )
        doubts = [*self.doubts, *more]
        if doubts:
            text = f"{text} (not certain: {'; '.join(doubts)})"
        return f"{text}; {'; '.join(self.flags)}" if self.flags else text


@dataclass(frozen=True)
class Submission:
    """One thing a shell command does that spends, or may spend, the budget."""

    # A submitter (`sbatch`, `salloc`, `srun`); `scontrol`, `scrontab` and `config`,
    # which change what an earlier check allowed; or `program`, a script in another
    # language that may run any of them.
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


def _find_documents(
    line: str, quote: str, depth: int, count: int
) -> tuple[str, str, int, list[tuple[str, str, bool]]]:
    """Return a line with each `<<WORD` as `<< MARK`, and the documents it opens.

    `quote` is the quotation mark open where the line starts and `depth` how many
    `((` are; both come back as they stand where it ends.
    """
    out: list[str] = []
    opened: list[tuple[str, str, bool]] = []
    index = 0
    while index < len(line):
        character = line[index]
        if quote:
            if character == "\\" and quote == '"':
                out.append(line[index : index + 2])
                index += 2
                continue
            if character == quote:
                quote = ""
        elif character == "\\":
            out.append(line[index : index + 2])
            index += 2
            continue
        elif character in "'\"":
            quote = character
        elif character == "#" and (index == 0 or line[index - 1] in " \t;&|("):
            out.append(line[index:])
            break
        elif line.startswith("((", index):
            depth += 1
        elif line.startswith("))", index):
            depth = max(depth - 1, 0)
        elif line.startswith("<<<", index):
            out.append("<<<")
            index += 3
            continue
        # Inside `(( ))` it shifts a number.
        elif line.startswith("<<", index) and not depth:
            end = index + 2
            strip_tabs = line.startswith("-", end)
            end += strip_tabs
            while end < len(line) and line[end] in " \t":
                end += 1
            start = end
            inside = ""
            while end < len(line) and (inside or line[end] not in _WORD_ENDS):
                if line[end] in "'\"" and inside in ("", line[end]):
                    inside = "" if inside else line[end]
                end += 1
            word = re.sub(r"[\\'\"]", "", line[start:end])
            if word:
                mark = f"{_MARK}{count + len(opened)}"
                opened.append((mark, word, strip_tabs))
                out.append(f"<< {mark}")
                index = end
                continue
        out.append(character)
        index += 1
    return "".join(out), quote, depth, opened


def lift_documents(command: str) -> tuple[str, dict[str, str]]:
    """Return a command without the bodies of its here-documents, and the bodies.

    Each body is named by the word left after its `<<`.
    """
    lines = command.split("\n")
    kept: list[str] = []
    bodies: dict[str, str] = {}
    waiting: list[tuple[str, str, bool]] = []
    quote, depth, index = "", 0, 0
    while index < len(lines):
        line, quote, depth, opened = _find_documents(
            lines[index], quote, depth, len(bodies) + len(waiting)
        )
        index += 1
        kept.append(line)
        waiting += opened
        # A body starts where the line ends, and a quotation or a backslash joins
        # the next line to this one.
        if quote or line.endswith("\\"):
            continue
        for mark, word, strip_tabs in waiting:
            body: list[str] = []
            while index < len(lines):
                text = lines[index]
                index += 1
                if (text.lstrip("\t") if strip_tabs else text) == word:
                    break
                body.append(text)
            bodies[mark] = "\n".join(body)
        waiting = []
    return "\n".join(kept), bodies


def split_pipeline(tokens: Sequence[str]) -> list[tuple[list[str], bool]]:
    """Return each simple command's words, and whether the next reads its output."""
    commands: list[tuple[list[str], bool]] = []
    words: list[str] = []
    for token in tokens:
        if token and all(character in _SEPARATORS for character in token):
            if words:
                commands.append((words, token.strip("\n()") in ("|", "|&")))
                words = []
        else:
            words.append(token)
    if words:
        commands.append((words, False))
    return commands


def split_commands(tokens: Sequence[str]) -> list[list[str]]:
    """Return the words of each simple command, redirections included."""
    return [words for words, _ in split_pipeline(tokens)]


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


def holds_unknown(arguments: Sequence[str], kind: str) -> bool:
    """Report whether a shell variable stands where it can change what is reserved.

    That is in an option or in the script's name, not in what the job itself is
    given to run.
    """
    try:
        options, rest = parse_options(arguments, kind)
    except UserError:
        return any("$" in argument for argument in arguments)
    head = arguments[: len(arguments) - len(rest)]
    wrapped = options.get("wrap")
    judged = [word for word in head if word not in (wrapped, f"--wrap={wrapped}")]
    return any("$" in word for word in (*judged, *rest[:1]))


def count_gpus(merged: Mapping[str, str], nodes: int) -> int | None:
    """Return how many GPUs each node of a request gets; None when it names none."""

    def count(text: str) -> int | None:
        last = text.rpartition(":")[2]
        return int(last) if last.isdigit() else None

    for part in merged.get("gres", "").split(","):
        if part == "gpu":
            return 1
        if part.startswith("gpu:"):
            return count(part)
    if "gpus-per-node" in merged:
        return count(merged["gpus-per-node"])
    total = count(merged["gpus"]) if "gpus" in merged else None
    return None if total is None else -(-total // max(nodes, 1))


def find_gpu_flags(
    merged: Mapping[str, str], script: str, nodes: int, gpus_per_node: int
) -> list[str]:
    """Return what in a request or its script leaves GPUs of a node unused."""
    flags: list[str] = []
    asked = count_gpus(merged, nodes)
    if asked is not None and asked < gpus_per_node:
        flags.append(
            f"it asks for {asked} of a node's {gpus_per_node} GPUs, and a node is "
            "billed with all of them"
        )
    shown: set[str] = set()
    for line in script.splitlines():
        for value in _VISIBLE_GPUS.findall(line.partition("#")[0]):
            if not re.fullmatch(r"\d+(,\d+)*", value.strip("\"';")):
                # A variable gives each program its own: how many is not known here.
                return flags
            shown.update(value.strip("\"';").split(","))
    if shown and len(shown) < gpus_per_node:
        flags.append(
            f"its script shows its programs {len(shown)} of a node's "
            f"{gpus_per_node} GPUs (CUDA_VISIBLE_DEVICES)"
        )
    return flags


def _read_inputs(words: Sequence[str], bodies: Mapping[str, str]) -> list[str]:
    """Return what a command reads from its here-documents and here-strings."""
    texts: list[str] = []
    for operator, word in zip(words, words[1:]):
        if operator == "<<" and word in bodies:
            texts.append(bodies[word])
        elif operator == "<<<":
            texts.append(word)
    return texts


def _scan_scripts(
    texts: Sequence[str], *, route: str, host: str
) -> list[Submission] | None:
    """Return what texts submit that a shell reads as its commands."""
    found: list[Submission] = []
    for text in texts:
        nested = scan(text, route=route, host=host)
        if nested is not None:
            found += nested
        # Unreadable, and it can submit only by naming a way to.
        elif NAMES_A_SUBMITTER.search(text):
            return None
    return found


def _scan_handed_on(
    words: Sequence[str], texts: Sequence[str], *, route: str, host: str
) -> list[Submission] | None:
    """Return what a command line submits that another shell runs, given its input."""
    if words:
        return scan(" ".join(words), route=route, host=host, stdin=texts)
    # Without a command of its own, a shell runs what it reads.
    return _scan_scripts(texts, route=route, host=host)


def _may_run_programs(words: Sequence[str], texts: Sequence[str]) -> bool:
    """Report whether a script in another language may submit something."""
    named = [text for text in texts if NAMES_A_SUBMITTER.search(text)]
    if any(_READABLE.match(_base(word)) for word in words):
        return any(_STARTS_PROGRAMS.search(text) for text in named)
    return bool(named)


def scan(
    command: str, *, route: str = "local", host: str = "", stdin: Sequence[str] = ()
) -> list[Submission] | None:
    """Return what a shell command submits, also inside the commands it hands on.

    `stdin` is what its first command reads. None when the command cannot be read
    as the shell reads it.
    """
    lifted, bodies = lift_documents(command)
    tokens = tokenize(lifted)
    if tokens is None:
        return None
    commands = split_pipeline(tokens)
    looped = any(_base(word) in _LOOPS for words, _ in commands for word in words[:3])
    found: list[Submission] = []
    carried = list(stdin)
    for raw, piped in commands:
        texts = [*carried, *_read_inputs(raw, bodies)]
        carried = []
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
            if holds_unknown(arguments, name):
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
        else:
            plain, _ = _drop_redirections(rest)
            nested: list[Submission] | None = []
            if name == HELPER:
                nested = _scan_helper(plain, tuple(doubts), texts)
            elif name == "ssh":
                target, remote = _split_ssh(plain)
                nested = _scan_handed_on(remote, texts, route="ssh", host=target)
            elif name in _SHELLS:
                script = plain[plain.index("-c") + 1 :][:1] if "-c" in plain else []
                script = plain if name == "eval" else script
                nested = _scan_handed_on(script, texts, route=route, host=host)
            elif name in _FILTERS:
                carried = texts if piped else []
            elif name in ("echo", "printf"):
                carried = [" ".join(plain)] if piped else []
            elif any(_INTERPRETERS.match(_base(word)) for word in words):
                # A script given as an argument is read like one given as input.
                given = [
                    script
                    for flag, script in zip(plain, plain[1:])
                    if flag in ("-c", "-e")
                ]
                if _may_run_programs(words, [*texts, *given]):
                    found.append(Submission("program", (), route, host))
            elif name not in _SINKS:
                # A program this code does not know may run what it reads.
                nested = _scan_scripts(texts, route=route, host=host)
            if nested is None:
                return None
            found += nested
    return found


def _scan_helper(
    words: Sequence[str], doubts: tuple[str, ...], texts: Sequence[str] = ()
) -> list[Submission] | None:
    """Return what one command of the helper submits, given what it reads."""
    if words[:1] == ["submit"]:
        arguments, _ = _drop_redirections(words[1:])
        if holds_unknown(arguments, "sbatch"):
            doubts = (
                *doubts,
                "it holds a shell variable whose value is not known here",
            )
        return [Submission("sbatch", tuple(arguments), HELPER, doubts=doubts)]
    if words[:1] == ["ssh"]:
        remote = [word for word in words[1:] if word != "-C"]
        return _scan_handed_on(remote, texts, route=HELPER, host="")
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
