"""Remote commands use the shared connection only, and ssh's failures are told apart."""

from __future__ import annotations

import subprocess

import pytest

from jup.errors import ConfigError, KeyRefusedError, NoConnectionError, RemoteError
from jup.ssh import Remote, classify


def test_a_closed_connection_is_reported_before_anything_is_sent(run):
    run.reply("-O check", code=255)

    with pytest.raises(NoConnectionError, match="ssh cluster true"):
        Remote("cluster", run=run).execute("squeue --me")

    assert run.ran("squeue") == []


def test_a_command_runs_in_a_login_shell_in_the_given_directory(run):
    Remote("cluster", run=run).execute("ls", cwd="/e/project1/p/my dir")

    [sent] = run.ran("bash -lc")
    assert sent.endswith(
        """-- cluster bash -lc 'cd '"'"'/e/project1/p/my dir'"'"' && ls'"""
    )
    assert "BatchMode=yes" in sent


def test_the_host_never_stands_where_ssh_reads_options(run):
    remote = Remote("-oProxyCommand=x", run=run)
    remote.settings()
    remote.is_open()

    assert all(call[call.index("--") + 1] == "-oProxyCommand=x" for call in run.calls)


def test_a_failing_remote_command_is_quoted_with_what_it_printed(run):
    run.reply("sacct", code=1, err="sacct: error: bad job")

    with pytest.raises(RemoteError, match="code 1: sacct: error: bad job"):
        Remote("cluster", run=run).read("sacct -j x")


def test_a_remote_command_may_exit_with_sshs_own_code(run):
    run.reply("bash -lc", code=255, err="my script failed")

    assert Remote("cluster", run=run).execute("./script").returncode == 255


def test_a_dropped_connection_in_the_middle_of_a_command_is_sshs_failure(run):
    run.reply("bash -lc", code=255, err="Connection to node closed by remote host.")

    with pytest.raises(RemoteError, match="check the network"):
        Remote("cluster", run=run).execute("squeue")


def test_a_command_without_an_answer_names_the_hanging_connection(run):
    run.fail("bash -lc", subprocess.TimeoutExpired("ssh", 120))

    with pytest.raises(RemoteError, match="ssh -O exit cluster"):
        Remote("cluster", run=run).execute("squeue")


def test_lines_are_read_without_what_the_login_profile_printed(run):
    run.reply("bash -lc", out="Welcome to the cluster\nJUP|4711\nJUP|second\n")

    assert Remote("cluster", run=run).read_lines("sbatch job") == ["4711", "second"]


def test_an_alias_without_an_entry_is_told_from_one_with(run):
    assert Remote("cluster", run=run).has_entry()
    run.reply("-G", out="user me\nhostname cluster\n")

    assert not Remote("cluster", run=run).has_entry()


def test_a_configuration_ssh_cannot_read_is_a_settings_error(run):
    run.reply("-G", code=255, err="Bad configuration option: foo")

    with pytest.raises(ConfigError, match="Bad configuration option"):
        Remote("cluster", run=run).settings()


@pytest.mark.parametrize(
    ("stderr", "kind", "remedy"),
    [
        ("alice@node: Permission denied (publickey).", KeyRefusedError, "from= clause"),
        ("alice@node: Permission denied (publickey).", KeyRefusedError, "ssh-add"),
        ("Permission denied (keyboard-interactive).", NoConnectionError, "TOTP code"),
        (
            "ssh: connect to host node port 22: Operation timed out",
            RemoteError,
            "network",
        ),
    ],
)
def test_sshs_own_failures_name_their_cause(stderr, kind, remedy):
    error = classify("cluster", stderr)

    assert isinstance(error, kind)
    assert remedy in str(error)
