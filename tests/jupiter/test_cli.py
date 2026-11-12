"""Commands price what will run, report a job's real end and refuse what is unsafe."""

from __future__ import annotations

import shlex

import pytest

from jup import cli
from jup.errors import NoConnectionError, RemoteError, UserError
from jup.ssh import MARK

SCRIPT = "#!/bin/bash\n#SBATCH -N 1\n#SBATCH -t 30\n#SBATCH --output=logs/%j.out\n"


def marked(text):
    """Return lines as a remote command prints them through `Remote.read_lines`."""
    return "".join(f"{MARK}{line}\n" for line in text.splitlines())


@pytest.fixture
def script(context):
    (context.cwd / "job.sbatch").write_text(SCRIPT)
    return "job.sbatch"


def test_a_dry_submission_prints_the_cost_and_the_command_with_the_account(
    context, script, run, capsys
):
    code = cli.run_submit(["--dry-run", "-t", "45", script, "x"], context)

    printed = capsys.readouterr().out
    assert code == 0
    assert "1 node for up to 0:45:00: at most 216 core-hours" in printed
    assert (
        "mkdir -p -- logs && sbatch --parsable --account=proj --partition=booster "
        "-t 45 job.sbatch x"
    ) in printed
    assert "/e/project1/proj/alice/repo" in printed
    assert run.ran("sbatch") == []


def test_a_script_that_names_its_account_keeps_it(context, capsys):
    (context.cwd / "own.sbatch").write_text("#SBATCH --account=other\n#SBATCH -p cpu\n")

    cli.run_submit(["--dry-run", "own.sbatch"], context)

    assert "sbatch --parsable own.sbatch" in capsys.readouterr().out


def test_a_submission_prices_the_copy_on_the_cluster_and_reports_the_job(
    context, script, run, capsys
):
    run.reply("cat -- job.sbatch", out=marked(SCRIPT))
    run.reply("sbatch --parsable", out="Message of the day\n" + marked("4711;jupiter"))

    assert cli.run_submit([script], context) == 0

    assert "submitted job 4711" in capsys.readouterr().out


def test_a_copy_on_the_cluster_that_asks_for_more_stops_the_submission(
    context, script, run
):
    run.reply("cat -- job.sbatch", out=marked(SCRIPT.replace("-N 1", "-N 64")))

    with pytest.raises(RemoteError, match="jup push"):
        cli.run_submit([script], context)

    assert run.ran("sbatch --parsable") == []


def test_a_script_missing_on_the_cluster_points_to_push(context, script, run):
    run.reply("cat -- job.sbatch", code=1, err="cat: job.sbatch: No such file")

    with pytest.raises(RemoteError, match="jup push"):
        cli.run_submit([script], context)


def test_output_without_a_job_number_is_not_reported_as_a_job(context, script, run):
    run.reply("cat -- job.sbatch", out=marked(SCRIPT))
    run.reply("sbatch --parsable", out="Welcome\n" + marked("Submitted, perhaps"))

    with pytest.raises(RemoteError, match="no job number"):
        cli.run_submit([script], context)


def test_a_test_flag_after_the_script_belongs_to_the_script(context, script, run):
    run.reply("cat -- job.sbatch", out=marked(SCRIPT))
    run.reply("sbatch --parsable", out=marked("9"))

    cli.run_submit([script, "--dry-run"], context)

    assert run.ran("sbatch --parsable")[0].count("job.sbatch --dry-run") == 1


def test_waiting_inside_sbatch_is_refused(context, script):
    with pytest.raises(UserError, match="jup wait"):
        cli.run_submit(["--wait", script], context)


def wait(context, arguments):
    return cli.run_wait([*arguments, "--interval", "30"], context, sleep=lambda _: None)


def test_waiting_prints_each_state_once_and_fails_for_a_failed_job(
    context, run, capsys
):
    for line in ("PENDING|0:00|Priority", "PENDING|0:00|Priority", "RUNNING|0:05|None"):
        run.reply("squeue --noheader --jobs=7", out=marked(line))
    run.reply(
        "squeue --noheader --jobs=7",
        code=1,
        err="slurm_load_jobs error: Invalid job id specified",
    )
    run.reply("sacct --jobs=7", out=marked("7|FAILED|00:12:01|1:0"))

    code = wait(context, ["7"])

    lines = [line.split(" ", 1)[1] for line in capsys.readouterr().out.splitlines()]
    assert code == 1
    assert lines == [
        "job 7 PENDING (Priority) after 0:00",
        "job 7 RUNNING after 0:05",
        "job 7 FAILED after 00:12:01, exit code 1:0",
    ]


def test_an_array_completed_only_if_every_task_did(context, run, capsys):
    run.reply("squeue --noheader --jobs=7", out="")
    run.reply(
        "sacct --jobs=7",
        out=marked("7_0|COMPLETED|00:01:00|0:0\n7_1|FAILED|00:00:09|1:0"),
    )

    assert wait(context, ["7"]) == 1
    assert "job 7_1 FAILED" in capsys.readouterr().out


def test_a_job_accounting_still_shows_running_is_not_reported_as_ended(
    context, run, capsys
):
    run.reply("squeue --noheader --jobs=7", out="")
    for _ in range(6):
        run.reply("sacct --jobs=7", out=marked("7|RUNNING|00:01:00|0:0"))

    with pytest.raises(RemoteError, match="still shows it as RUNNING"):
        wait(context, ["7"])

    assert capsys.readouterr().out == ""


def test_a_queue_that_does_not_answer_is_not_taken_for_the_jobs_end(context, run):
    for _ in range(3):
        run.reply(
            "squeue --noheader --jobs=7",
            code=1,
            err="slurm_load_jobs error: Socket timed out",
        )

    with pytest.raises(RemoteError, match="three times in a row"):
        wait(context, ["7"])

    assert run.ran("sacct") == []


def test_waiting_can_end_when_the_job_starts(context, run, capsys):
    run.reply("squeue --noheader --jobs=7", out=marked("PENDING|0:00|Resources"))
    run.reply("squeue --noheader --jobs=7", out=marked("RUNNING|0:01|None"))

    assert wait(context, ["7", "--until-running"]) == 0
    assert run.ran("sacct") == []


def test_a_job_slurm_never_had_is_said_so(context, run):
    run.reply("squeue --noheader --jobs=7", code=1, err="Invalid job id specified")

    with pytest.raises(RemoteError, match="no record of job 7"):
        wait(context, ["7"])


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        (cli.run_wait, ["--me"]),
        (cli.run_wait, ["7; id"]),
        (cli.run_logs, ["x"]),
        (cli.run_cancel, ["7", "--me"]),
        (cli.run_cancel, ["-u", "alice"]),
    ],
)
def test_only_job_numbers_reach_the_cluster(context, run, command, arguments):
    with pytest.raises((UserError, SystemExit)):
        command(arguments, context)

    assert run.ran("bash -lc") == []


def test_polling_faster_than_a_shared_node_should_bear_is_refused(context):
    with pytest.raises(UserError, match="at least 30"):
        cli.run_wait(["7", "--interval", "1"], context)


def test_cancelling_names_the_jobs(context, run, capsys):
    cli.run_cancel(["7", "8_1"], context)

    assert run.ran("scancel")[0].endswith("'scancel 7 8_1'")


def test_logs_are_read_from_the_files_slurm_names_whatever_their_names_hold(
    context, run
):
    shown = (
        "JobId=7 JobName=x\n   StdErr=/e/p/my out's.log\n   StdOut=/e/p/my out's.log\n"
    )
    run.reply("scontrol show job 7", out=shown)

    cli.run_logs(["7", "-n", "5"], context)

    [call] = [c for c in run.calls if "tail -n 5" in c[-1]]
    [_, _, script] = shlex.split(call[-1])
    assert shlex.split(script.split("; ")[1]) == [
        "tail",
        "-n",
        "5",
        "--",
        "/e/p/my out's.log",
    ]
    assert script.count("; ") == 1


def test_a_job_slurm_has_forgotten_points_to_its_file(context, run):
    run.reply("scontrol show job 7", code=1, err="Invalid job id specified")

    with pytest.raises(RemoteError, match="by name"):
        cli.run_logs(["7"], context)


def destination(context, pattern, to=None):
    return cli.choose_destination(pattern, to, context)


def test_a_pulled_path_lands_where_it_lies_in_the_project(context):
    root = context.config.root.resolve()

    assert destination(context, "runs/exp_1") == root / "runs"
    assert destination(context, "runs/exp_1/") == root / "runs"
    assert destination(context, "results.csv") == root
    assert destination(context, "runs/exp_*") == root / "runs"


def test_a_wildcard_in_a_directory_needs_a_destination(context):
    with pytest.raises(UserError, match="--to DIR"):
        destination(context, "runs/*/metrics.json")

    assert (
        destination(context, "runs/*/metrics.json", cli.Path("collected")).name
        == "collected"
    )


def test_nothing_is_pulled_to_outside_the_project(context):
    with pytest.raises(UserError, match="outside the project"):
        destination(context, "runs/a", cli.Path("../elsewhere"))


def test_a_project_setting_goes_to_the_file_that_is_read(context, capsys):
    below = context.cwd / "src"
    below.mkdir()
    inner = cli.Context(below, context.config, context.remote)

    cli.run_config(["set", "partition", "other", "--project"], inner)

    assert '"partition": "other"' in (context.cwd / ".jupiter.json").read_text()
    assert not (below / ".jupiter.json").exists()


def test_the_doctor_makes_no_login_attempt_unless_asked(
    context, run, capsys, monkeypatch
):
    monkeypatch.setattr(cli, "find_public_address", lambda: "203.0.113.7")
    run.reply("-O check", code=255)

    assert cli.run_doctor([], context) == 1

    printed = capsys.readouterr().out
    assert "no SSH connection is open" in printed and "203.0.113.7" not in printed
    assert run.ran("ControlPath=none") == []


def test_the_doctor_probes_once_when_asked(context, run, capsys, monkeypatch):
    monkeypatch.setattr(cli, "find_public_address", lambda: "203.0.113.7")
    run.reply("-O check", code=255)
    run.reply(
        "ControlPath=none", code=255, err="alice@node: Permission denied (publickey)."
    )

    assert cli.run_doctor(["--probe"], context) == 1

    printed = capsys.readouterr().out
    assert "refused the SSH key" in printed and "203.0.113.7" in printed
    assert len(run.ran("ControlPath=none")) == 1


def test_the_doctor_reads_the_identity_past_what_the_profile_prints(
    context, run, capsys
):
    run.reply(
        "id -un",
        out="Some banner\n" + marked("user=alice\ngroups=alice proj\nnode=login07"),
    )

    assert cli.run_doctor([], context) == 0

    printed = capsys.readouterr().out
    assert "logged in as alice on login07" in printed
    assert "ok   alice is a member of proj" in printed


def test_the_doctor_warns_about_a_connection_that_closes_with_its_terminal(
    context, run, capsys
):
    run.reply(
        "-G",
        out="user alice\nhostname login.example\ncontrolmaster auto\n"
        "controlpath /tmp/cm\n",
    )
    run.reply("-O check", code=255)

    cli.run_doctor([], context)

    assert "warn without ControlPersist" in capsys.readouterr().out


def test_an_unknown_command_exits_with_the_code_of_a_users_mistake(
    capsys, settings_home
):
    assert cli.main(["frobnicate"]) == 64
    assert "jup help" in capsys.readouterr().err


def test_a_closed_connection_has_its_own_exit_code(monkeypatch, settings_home, capsys):
    monkeypatch.setenv("JUP_HOST", "jup-test.invalid")

    assert cli.main(["jobs"]) == 66
    assert capsys.readouterr().err.startswith("jup: no SSH connection")


def test_a_missing_program_is_named_instead_of_a_traceback(
    monkeypatch, settings_home, capsys
):
    def missing(arguments, context):
        raise FileNotFoundError(2, "No such file or directory", "rsync")

    monkeypatch.setitem(cli.COMMANDS, "push", missing)

    assert cli.main(["push"]) == 64
    assert "rsync was not found" in capsys.readouterr().err


def test_the_closed_connection_error_is_raised_before_any_remote_command(context, run):
    run.reply("-O check", code=255)

    with pytest.raises(NoConnectionError):
        cli.run_jobs([], context)

    assert run.ran("squeue") == []
