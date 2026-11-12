"""A report holds the plugin's part of a session, without secrets, and stays local."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from jup import feedback, journal
from jup.errors import RemoteError, UserError

TOKEN = "hf_" + "a1B2c3D4e5F6g7H8i9J0k1L2"
# Where Claude Code puts an installed plugin: below a directory named by its version.
INSTALLED = f"{Path.home()}/.claude/plugins/cache/hpc-tools/jupiter/0.1.0"


def user(text, when="2026-11-12T10:00:00.000Z", **more):
    return {
        "type": "user",
        "timestamp": when,
        "message": {"role": "user", "content": text},
        **more,
    }


def assistant(*blocks, when="2026-11-12T10:00:05.000Z", **more):
    message = {"role": "assistant", "content": list(blocks)}
    return {
        "type": "assistant",
        "timestamp": when,
        "version": "2.1.322",
        "requestedModel": "claude-x",
        "message": message,
        **more,
    }


def bash(call, command):
    return {
        "type": "tool_use",
        "id": call,
        "name": "Bash",
        "input": {"command": command},
    }


def result(call, text, **more):
    block = {
        "type": "tool_result",
        "tool_use_id": call,
        "content": [{"type": "text", "text": text}],
    }
    block.update({k: v for k, v in more.items() if k == "is_error"})
    record = user([block], when="2026-11-12T10:00:09.000Z")
    record.update({k: v for k, v in more.items() if k != "is_error"})
    return record


@pytest.fixture
def transcript(tmp_path):
    """Write a session of three turns: one with the plugin, one without, one refused."""
    home = str(Path.home())
    records = [
        {"type": "api-request-blob", "blob": "x" * 50},
        user("run train.py on JUPITER<system-reminder>hidden rules</system-reminder>"),
        assistant(
            {"type": "thinking", "thinking": "private reasoning"},
            {
                "type": "tool_use",
                "id": "s1",
                "name": "Skill",
                "input": {"skill": "jupiter:submit"},
            },
            {
                "type": "tool_use",
                "id": "r0",
                "name": "Read",
                "input": {
                    "file_path": f"{INSTALLED}/skills/cluster/references/slurm.md"
                },
            },
            {
                "type": "tool_use",
                "id": "r1",
                "name": "Read",
                "input": {"file_path": f"{home}/repo/train.py"},
            },
            bash("b1", f"HF_TOKEN={TOKEN} jup submit --account=proj-1894 job.sbatch"),
            {"type": "text", "text": "Submitted job 4711."},
        ),
        result(
            "b1",
            f"copied to /e/project1/proj-1894/alice1/repo from {home}/repo\n"
            f"mail alice@example.org from 203.0.113.7 token {TOKEN}\n"
            "groups: alice1 proj-1894",
            permissionDecision={"decision": "accept", "source": "user"},
        ),
        user("now fix the typo in README", when="2026-11-12T10:10:00.000Z"),
        assistant(
            bash("b2", "sed -i s/teh/the/ README.md"), {"type": "text", "text": "Done."}
        ),
        result("b2", "password=hunter2"),
        user("cancel everything", when="2026-11-12T10:20:00.000Z"),
        assistant(bash("b3", "jup ssh 'scancel --me'")),
        result(
            "b3",
            "The user doesn't want to proceed",
            is_error=True,
            toolDenialKind="user-rejected",
        ),
        user("a meta line", isMeta=True),
        assistant(bash("b4", "jup jobs"), isSidechain=True),
        "not a record",
    ]
    path = tmp_path / "session.jsonl"
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records) + "{broken\n"
    )
    return path


def build(transcript, note="it priced the job wrong", **options):
    redact = feedback.Redactor.for_user(login="alice1", account=None)
    built = feedback.build(
        note,
        transcript,
        host="hpc-jupiter",
        redact=redact,
        version="0.1.0",
        settings={"partition": "booster"},
        **options,
    )
    return built, built.path.read_text()


def test_a_report_holds_the_turns_that_used_the_plugin_and_counts_the_rest(transcript):
    built, text = build(transcript)

    assert (built.turns_shown, built.turns_related, built.turns_total) == (2, 2, 3)
    assert "run train.py on JUPITER" in text and "cancel everything" in text
    assert "jup submit" in text and "Submitted job 4711." in text
    assert "loaded the skill** `jupiter:submit`" in text
    assert "read** `cluster/references/slurm.md`" in text
    assert "other tool calls (Read x 1)" in text
    assert "denied: user-rejected" in text and "permission: accept (user)" in text


def test_what_is_not_the_plugins_business_stays_out(transcript):
    _, text = build(transcript)

    for absent in (
        "fix the typo",
        "sed -i",
        "hunter2",
        "private reasoning",
        "hidden rules",
        "a meta line",
        "jup jobs",
    ):
        assert absent not in text


def test_secrets_and_names_are_replaced_and_counted(transcript):
    built, text = build(transcript)

    for secret in (
        TOKEN,
        "alice@example.org",
        "203.0.113.7",
        str(Path.home()),
        "alice1",
        "proj-1894",
    ):
        assert secret not in text
    assert "/e/project1/<account>/<login>/repo" in text
    assert "groups: <login> <account>" in text
    assert "token" in built.redacted and "name or home path" in built.redacted


@pytest.mark.parametrize(
    ("text", "kept", "gone"),
    [
        ("export API_KEY='abc def'", "API_KEY=<secret>", "abc def"),
        (
            "Authorization: Bearer abcdefgh12345678",
            "Bearer <token>",
            "abcdefgh12345678",
        ),
        ("TOTP Verification code: 123456", "<code>", "123456"),
        ("ssh me@login.jupiter.fz-juelich.de", "<address>", "me@login"),
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n"
            "-----END OPENSSH PRIVATE KEY-----",
            "<private key>",
            "AAAA",
        ),
        ("sbatch -A proj-77 job.sh", "-A <account>", "proj-77"),
        ("ls /e/home/jusers/bob12/x", "/e/home/jusers/<login>/x", "bob12"),
    ],
)
def test_each_kind_of_secret_has_its_placeholder(text, kept, gone):
    cleaned = feedback.Redactor()(text)

    assert kept in cleaned and gone not in cleaned


def test_an_ordinary_text_is_left_as_it_is():
    text = "jup submit -N 2 -t 01:30:00 job.sbatch  # Python 3.13.5, 25.05.9-1"

    assert feedback.Redactor()(text) == text


def test_a_session_too_long_for_one_report_keeps_its_latest_turns(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(feedback, "MAX_REPORT", 6_000)
    records = []
    for number in range(30):
        records += [
            user(f"request {number}"),
            assistant(bash(f"b{number}", f"jup jobs --days {number + 1}")),
            result(f"b{number}", "queue line\n" * 30),
        ]
    path = tmp_path / "long.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))

    built, text = build(path)

    assert built.turns_related == 30 and 0 < built.turns_shown < 30
    assert "request 29" in text and "request 0\n" not in text
    assert "are left out for length" in text
    assert built.characters <= 6_000


def test_every_turn_can_be_asked_for(transcript):
    built, text = build(transcript, whole=True)

    assert built.turns_shown == 3 and "fix the typo" in text


def test_the_helpers_log_of_the_session_is_part_of_the_report(transcript, monkeypatch):
    monkeypatch.setattr(journal, "now", lambda: "2026-11-12T10:00:30Z")
    journal.append(
        {
            "kind": "run",
            "name": "submit",
            "args": ["job.sbatch"],
            "exit": 68,
            "seconds": 1.5,
            "error": "RemoteError: x",
        }
    )
    journal.append(
        {
            "kind": "check",
            "decision": "ask",
            "reason": "2 nodes",
            "command": "jup submit",
        }
    )
    monkeypatch.setattr(journal, "now", lambda: "2026-11-11T09:00:00Z")
    journal.append(
        {"kind": "run", "name": "yesterday", "args": [], "exit": 0, "seconds": 0}
    )

    _, text = build(transcript)

    assert "jup submit job.sbatch -> exit 68 in 1.5 s: RemoteError: x" in text
    assert "cost check asked the user: 2 nodes | jup submit" in text
    assert "yesterday" not in text


def test_a_transcript_that_is_not_there_says_how_to_name_one(tmp_path):
    with pytest.raises(UserError, match="--transcript"):
        build(tmp_path / "missing.jsonl")


def test_only_a_report_of_the_helper_can_be_sent_or_deleted(transcript, tmp_path):
    built, _ = build(transcript)
    other = tmp_path / "secrets.txt"
    other.write_text("x")

    assert feedback.check_report(built.path) == built.path.resolve()
    with pytest.raises(UserError, match="no report"):
        feedback.check_report(other)


def test_sending_files_an_issue_with_the_reports_title_and_file(transcript):
    built, _ = build(transcript)
    calls = []

    def gh(command, **options):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, "https://github.com/o/r/issues/7\n", ""
        )

    assert feedback.send(built.path, "o/r", run=gh) == "https://github.com/o/r/issues/7"
    [command] = calls
    assert command[:5] == ["gh", "issue", "create", "--repo", "o/r"]
    assert (
        command[command.index("--title") + 1]
        == "jupiter feedback: it priced the job wrong"
    )
    assert command[command.index("--body-file") + 1] == str(built.path)


def test_a_failed_send_says_what_to_do_and_keeps_the_report(transcript):
    built, _ = build(transcript)

    def gh(command, **options):
        return subprocess.CompletedProcess(command, 1, "", "error: not logged in\n")

    with pytest.raises(RemoteError, match="gh auth switch"):
        feedback.send(built.path, "o/r", run=gh)

    assert built.path.exists()


def test_a_long_note_gives_a_title_cut_at_a_words_end(transcript):
    note = (
        "the cost came without the budget and I expected it to say how much of the "
        "account is left"
    )

    _, text = build(transcript, note=note)

    title = text.splitlines()[0]
    assert title.endswith("how much of the ...") and len(title) < 105
    assert note in text
