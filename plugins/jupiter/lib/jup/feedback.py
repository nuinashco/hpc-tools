"""A report on one Claude Code session for the plugin's maintainers: its plugin part.

It is built from the session's transcript and the helper's local log, with secrets and
names taken out, and stays a local file until the user sends it. The transcript's
format is Claude Code's own and changes between versions, so every record is read for
what it may hold, and one that does not fit is skipped.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import platform
import re
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jup import journal
from jup.errors import RemoteError, UserError

PLUGIN = "jupiter"
# GitHub takes 65,536 characters in an issue's text; the report stays under that.
MAX_REPORT = 60_000
# The most of one command's output and of one message that a report quotes.
_MAX_OUTPUT = 3_000
_MAX_MESSAGE = 2_000
# The most records of the helper's log that a report lists, and the most of one turn.
_MAX_LOG = 150
_MAX_TURN = 10_000
# Commands that have to do with the cluster, wherever they stand in a command line.
_COMMANDS = re.compile(
    r"\b(jup|sbatch|salloc|srun|squeue|sacct|scancel|scontrol|sinfo|jutil)\b"
)
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
# A file of this plugin's skills: installed, it lies below a directory named by the
# version.
_SKILL_FILE = re.compile(rf"/{PLUGIN}/(?:[^/]+/)?skills/")
_FENCE = "~~~~"
# What is taken out of every text, with what stands in its place. The order matters:
# a whole key first, then tokens by their shape, then values by their names.
_SECRETS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "private key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "<private key>",
    ),
    (
        "token",
        re.compile(
            r"\b(hf_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
            r"|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
            r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|KGAT_[A-Za-z0-9]{10,}"
            r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,})"
        ),
        "<token>",
    ),
    (
        "secret value",
        re.compile(
            r"(?i)\b((?:[a-z0-9_]*(?:token|secret|password|passwd|api[_-]?key))"
            r"\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1<secret>",
    ),
    ("token", re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1<token>"),
    (
        "one-time code",
        re.compile(r"(?i)((?:totp|verification code)\D{0,40})\d{6}\b"),
        r"\1<code>",
    ),
    (
        "e-mail or login address",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "<address>",
    ),
    ("network address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    # JUPITER's storage paths name the project and the login, also when the settings
    # here do not.
    (
        "project or login in a path",
        re.compile(r"(/e/(?:project1|scratch|fscratch|data1)/)[A-Za-z0-9._-]+"),
        r"\1<account>",
    ),
    (
        "project or login in a path",
        re.compile(r"(/e/home/jusers/)[A-Za-z0-9._-]+"),
        r"\1<login>",
    ),
    (
        "project or login in a path",
        re.compile(r"(--account[= ]|-A )[A-Za-z0-9._-]+"),
        r"\1<account>",
    ),
)


# Where a text names the project or the login outright.
_GIVEAWAYS = (
    (
        re.compile(r"/e/(?:project1|scratch|fscratch|data1)/([A-Za-z0-9._-]+)"),
        "<account>",
    ),
    (re.compile(r"/e/home/jusers/([A-Za-z0-9._-]+)"), "<login>"),
    (re.compile(r"(?:--account[= ]|-A )([A-Za-z0-9._-]+)"), "<account>"),
)


@dataclass
class Redactor:
    """Takes secrets and names out of a text, and counts what it took."""

    # Texts to replace as they are: the home directory, logins, the account.
    names: Sequence[tuple[str, str]] = ()
    counts: Counter[str] = field(default_factory=Counter)

    @classmethod
    def for_user(cls, *, login: str, account: str | None) -> Redactor:
        """Return the redactor for this machine's user and their cluster names."""
        names = [(str(Path.home()), "~")]
        labelled = [(login, "<login>"), (account or "", "<account>")]
        with contextlib.suppress(KeyError, OSError):
            labelled.append((getpass.getuser(), "<user>"))
        # A short name would also be a part of ordinary words.
        names += [(name, label) for name, label in labelled if len(name) >= 4]
        return cls(names)

    def learn(self, text: str) -> None:
        """Note the project and login names that JUPITER's paths in a text give away.

        They are then taken out wherever they stand, not only inside a path.
        """
        known = {name for name, _ in self.names}
        learned = list(self.names)
        for pattern, label in _GIVEAWAYS:
            for name in sorted(set(pattern.findall(text)) - known):
                if len(name) >= 4:
                    learned.append((name, label))
                    known.add(name)
        self.names = learned

    def __call__(self, text: str) -> str:
        for kind, pattern, replacement in _SECRETS:
            text, found = pattern.subn(replacement, text)
            self.counts[kind] += found
        for name, label in self.names:
            if name.startswith("/"):
                found = text.count(name)
                text = text.replace(name, label)
            else:
                whole = rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])"
                text, found = re.subn(whole, label, text)
            self.counts["name or home path"] += found
        return text

    def summary(self) -> str:
        """Return what was taken out, in words."""
        found = [f"{count} x {kind}" for kind, count in self.counts.items() if count]
        return ", ".join(found) if found else "nothing matched"


def clip(text: str, limit: int) -> str:
    """Return a text's start and end when it is longer than `limit`."""
    if len(text) <= limit:
        return text
    head, tail = limit * 2 // 5, limit * 3 // 5
    cut = len(text) - head - tail
    return f"{text[:head]}\n[... {cut} characters cut ...]\n{text[-tail:]}"


def read_records(path: Path) -> list[dict[str, Any]]:
    """Return the messages of a transcript, without Claude Code's own bookkeeping."""
    try:
        handle = path.open(errors="replace")
    except OSError as error:
        raise UserError(
            f"the transcript {path} cannot be read ({error.strerror}); pass the "
            "session's file with --transcript"
        ) from error
    records = []
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("type") in ("user", "assistant"):
                records.append(record)
    return records


def read_blocks(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a message's content as blocks, whichever way it is stored."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def read_text(content: Any) -> str:
    """Return the text of a tool's result, which is a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content if isinstance(b, dict)]
        return "\n".join(text for text in texts if isinstance(text, str))
    return ""


def is_related(command: str, host: str) -> bool:
    """Report whether a shell command has to do with the cluster."""
    if _COMMANDS.search(command):
        return True
    return bool(re.search(r"\b(ssh|rsync|scp)\b", command)) and (
        host in command or "fz-juelich.de" in command
    )


@dataclass
class Turn:
    """One request of the user and what Claude did for it."""

    time: str
    prompt: str
    # What a report shows of it, in order.
    entries: list[str] = field(default_factory=list)
    related: int = 0
    others: Counter[str] = field(default_factory=Counter)

    def render(self, number: int) -> str:
        """Return the turn as Markdown."""
        parts = [f"### Turn {number}, {self.time[11:16]} UTC"]
        if self.prompt:
            parts.append(f"**User:** {clip(self.prompt, _MAX_MESSAGE)}")
        parts += self.entries
        if self.others:
            listed = ", ".join(
                f"{name} x {n}" for name, n in sorted(self.others.items())
            )
            parts.append(f"_Not shown: other tool calls ({listed})._")
        return "\n\n".join(parts)


def _result_lines(result: Mapping[str, Any] | None) -> list[str]:
    """Return how a tool call ended: what it printed and what was decided about it."""
    if result is None:
        return ["_No result in the transcript._"]
    lines = []
    notes = [note for note in (result["denial"], result["decision"]) if note]
    if result["error"]:
        notes.insert(0, "it failed or was refused")
    if notes:
        lines.append(f"_{'; '.join(notes)}_")
    if result["text"].strip():
        lines.append(f"{_FENCE}\n{clip(result['text'].strip(), _MAX_OUTPUT)}\n{_FENCE}")
    return lines


def collect_results(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return each tool call's result by the call's id."""
    results: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("type") != "user":
            continue
        decided = record.get("permissionDecision")
        decision = ""
        if isinstance(decided, dict) and decided.get("decision"):
            decision = (
                f"permission: {decided['decision']} ({decided.get('source', '?')})"
            )
        denial = record.get("toolDenialKind")
        for block in read_blocks(record):
            if block.get("type") == "tool_result" and block.get("tool_use_id"):
                results[str(block["tool_use_id"])] = {
                    "text": read_text(block.get("content")),
                    "error": bool(block.get("is_error")),
                    "decision": decision,
                    "denial": f"denied: {denial}" if isinstance(denial, str) else "",
                }
    return results


def split_turns(
    records: Sequence[Mapping[str, Any]], host: str
) -> tuple[list[Turn], dict[str, Any]]:
    """Return the session as turns, and what is known of its start and versions."""
    results = collect_results(records)
    turns = [Turn(time="", prompt="")]
    facts: dict[str, Any] = {"start": "", "versions": set(), "models": set()}
    for record in records:
        stamp = str(record.get("timestamp") or "")
        facts["start"] = facts["start"] or stamp
        if record.get("isSidechain"):
            continue
        for key, found in (("version", "versions"), ("requestedModel", "models")):
            if isinstance(record.get(key), str):
                facts[found].add(record[key])
        blocks = read_blocks(record)
        if record.get("type") == "user":
            skipped = record.get("isMeta") or record.get("isCompactSummary")
            answers = any(block.get("type") == "tool_result" for block in blocks)
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            prompt = _REMINDER.sub(
                "", "\n".join(t for t in texts if isinstance(t, str))
            )
            if not skipped and not answers and prompt.strip():
                turns.append(Turn(time=stamp, prompt=prompt.strip()))
            continue
        turn = turns[-1]
        turn.time = turn.time or stamp
        for block in blocks:
            kind, data = block.get("type"), block.get("input")
            data = data if isinstance(data, dict) else {}
            if kind == "text" and str(block.get("text", "")).strip():
                said = clip(str(block["text"]).strip(), _MAX_MESSAGE)
                turn.entries.append(f"**Claude:** {said}")
            elif kind != "tool_use":
                continue
            elif block.get("name") in ("Bash", "Monitor") and is_related(
                str(data.get("command", "")), host
            ):
                turn.related += 1
                call = f"**Claude ran:**\n{_FENCE}\n{data['command']}\n{_FENCE}"
                ended = _result_lines(results.get(str(block.get("id"))))
                turn.entries.append("\n".join([call, *ended]))
            elif block.get("name") == "Skill" and str(data.get("skill", "")).startswith(
                PLUGIN
            ):
                turn.related += 1
                turn.entries.append(f"**Claude loaded the skill** `{data['skill']}`")
            elif block.get("name") == "Read" and _SKILL_FILE.search(
                str(data.get("file_path", ""))
            ):
                turn.related += 1
                read = str(data["file_path"]).rpartition("/skills/")[2]
                turn.entries.append(f"**Claude read** `{read}`")
            else:
                turn.others[str(block.get("name"))] += 1
    return [turn for turn in turns if turn.prompt or turn.entries], facts


def render_log(records: Sequence[Mapping[str, Any]]) -> str:
    """Return the helper's log records as lines."""
    lines = []
    for record in records[-_MAX_LOG:]:
        at = str(record.get("ts", ""))[11:19]
        if record.get("kind") == "run":
            words = " ".join(str(word) for word in record.get("args") or [])
            error = f": {record['error']}" if record.get("error") else ""
            lines.append(
                f"{at} jup {record.get('name')} {words} -> exit {record.get('exit')} "
                f"in {record.get('seconds')} s{error}"
            )
        elif record.get("kind") == "check":
            asked = record.get("decision") == "ask"
            reason = f": {record.get('reason')}" if asked else ""
            verdict = "asked the user" if asked else "passed"
            lines.append(f"{at} cost check {verdict}{reason} | {record.get('command')}")
    return "\n".join(lines) if lines else "no records"


@dataclass(frozen=True)
class Report:
    """A report as a file on this machine, and what went into it."""

    path: Path
    # Turns in the report, turns that used the plugin, and turns of the session.
    turns_shown: int
    turns_related: int
    turns_total: int
    redacted: str
    characters: int


def build(
    note: str,
    transcript: Path,
    *,
    host: str,
    redact: Redactor,
    version: str,
    settings: Mapping[str, Any],
    whole: bool = False,
    env: Mapping[str, str] = os.environ,
) -> Report:
    """Write the report on a session to the helper's state directory and return it.

    A session too long for one report keeps its latest turns: feedback is mostly
    about what just happened.
    """
    turns, facts = split_turns(read_records(transcript), host)
    related = [turn for turn in turns if whole or turn.related]
    since = str(facts["start"])[:19] + "Z" if facts["start"] else ""
    records = render_log(journal.read(since, env))
    redact.learn("\n".join([note, records, *(turn.render(0) for turn in related)]))
    said = redact(note.strip()) or "(no note)"
    log = clip(redact(records), _MAX_TURN)
    start = "\n\n".join(
        [
            "## What the user says",
            said,
            "## The helper's log for this session",
            f"{_FENCE}\n{log}\n{_FENCE}",
            f"## The session's turns{'' if whole else ' that used the plugin'}",
        ]
    )
    # The header is short; this leaves room for it.
    room = MAX_REPORT - len(start) - 1_500
    kept: list[str] = []
    for number in range(len(related), 0, -1):
        rendered = clip(redact(related[number - 1].render(number)), _MAX_TURN)
        if kept and len(rendered) + 2 > room:
            break
        kept.insert(0, rendered[:room])
        room -= len(rendered) + 2
    left_out = len(related) - len(kept)
    excerpt = "\n\n".join(kept) or "No turn of this session used the plugin."
    if left_out:
        excerpt = (
            f"_The first {left_out} of these turns are left out for length._\n\n"
            f"{excerpt}"
        )
    listed = ", ".join(f"{key}={value}" for key, value in settings.items())
    header = "\n".join(
        [
            f"# {PLUGIN} feedback: {said.splitlines()[0][:80]}",
            "",
            f"- plugin: {PLUGIN} {version}",
            f"- Claude Code: {', '.join(sorted(facts['versions'])) or 'unknown'}",
            f"- model: {', '.join(sorted(facts['models'])) or 'unknown'}",
            f"- machine: {platform.system()} {platform.machine()}, "
            f"Python {platform.python_version()}",
            f"- settings: {listed}",
            f"- shown: {len(kept)} of {len(related)} turns"
            f"{'' if whole else ' that used the plugin'}; the session has {len(turns)}",
            f"- taken out: {redact.summary()}",
            "",
        ]
    )
    text = f"{header}\n{start}\n\n{excerpt}\n"
    directory = journal.state_dir(env) / "feedback"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.md"
    path.write_text(text)
    return Report(
        path=path,
        turns_shown=len(kept),
        turns_related=len(related),
        turns_total=len(turns),
        redacted=redact.summary(),
        characters=len(text),
    )


def check_report(path: Path, env: Mapping[str, str] = os.environ) -> Path:
    """Return a report's path if it is one this helper wrote, or refuse it."""
    directory = (journal.state_dir(env) / "feedback").resolve()
    resolved = path.expanduser().resolve()
    if resolved.parent != directory or not resolved.is_file():
        raise UserError(
            f"{path} is no report of `jup feedback`; they are in {directory}"
        )
    return resolved


def send(
    path: Path,
    repository: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """File a report as an issue on `repository`; return the issue's address."""
    lines = path.read_text().splitlines()
    title = lines[0].lstrip("# ").strip() if lines else f"{PLUGIN} feedback"
    done = run(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            repository,
            "--title",
            title,
            "--body-file",
            str(path),
        ],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if done.returncode:
        detail = (done.stderr or "").strip().splitlines()[-1:] or ["no message"]
        raise RemoteError(
            f"gh could not file the issue on {repository} ({detail[0]}); log in with "
            f"`gh auth login`, or attach {path} to an issue there by hand"
        )
    return done.stdout.strip().splitlines()[-1] if done.stdout.strip() else repository
