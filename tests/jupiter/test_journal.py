"""The local log takes records, gives them back by time, and never breaks a command."""

from __future__ import annotations

import json

from jup import journal


def test_records_come_back_in_order_from_a_given_time(state_home, monkeypatch):
    times = iter(
        ["2026-11-12T10:00:00Z", "2026-11-12T10:05:00Z", "2026-11-12T10:09:00Z"]
    )
    monkeypatch.setattr(journal, "now", lambda: next(times))

    for name in ("doctor", "push", "submit"):
        journal.append({"kind": "run", "name": name})

    assert [r["name"] for r in journal.read("2026-11-12T10:05:00Z")] == [
        "push",
        "submit",
    ]
    assert len(journal.read()) == 3


def test_a_log_that_grows_too_large_keeps_its_newest_lines(state_home, monkeypatch):
    monkeypatch.setattr(journal, "_MAX_BYTES", 2_000)
    monkeypatch.setattr(journal, "_KEEP_LINES", 5)

    for number in range(60):
        journal.append({"kind": "run", "name": f"n{number}"})

    kept = [record["name"] for record in journal.read()]
    assert kept[-1] == "n59"
    assert "n0" not in kept and len(kept) < 60


def test_a_line_that_is_no_record_is_skipped(state_home):
    journal.append({"kind": "run", "name": "ok"})
    with (journal.state_dir() / journal.LOG).open("a") as log:
        log.write("not json\n[1, 2]\n")

    assert [record["name"] for record in journal.read()] == ["ok"]


def test_a_log_that_cannot_be_written_does_not_stop_the_command(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))

    journal.append({"kind": "run", "name": "doctor"})
    journal.remember_session("s", "/t.jsonl", "/cwd")

    assert journal.read() == [] and journal.last_session() is None


def test_the_last_session_that_used_the_helper_is_remembered(state_home):
    assert journal.last_session() is None

    journal.remember_session("abc", "/home/x/.claude/projects/p/abc.jsonl", "/repo")

    known = journal.last_session()
    assert known is not None and known["transcript"].endswith("abc.jsonl")
    assert (
        json.loads((journal.state_dir() / journal.SESSION).read_text())["session"]
        == "abc"
    )
