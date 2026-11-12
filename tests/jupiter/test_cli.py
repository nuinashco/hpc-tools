"""Commands price what will run, report a job's real end and refuse what is unsafe."""

from __future__ import annotations

import io
import json
import shlex

import pytest

from jup import cli, feedback, gpus, journal
from jup.errors import NoConnectionError, RemoteError, UserError
from jup.ssh import MARK

SCRIPT = "#!/bin/bash\n#SBATCH -N 1\n#SBATCH -t 30\n#SBATCH --output=logs/%j.out\n"


STAMP = "commit: abc\nuncommitted files: 0\npushed: 2026-01-02T03:04:05Z\nfrom: repo\n"


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
    assert "1 node for up to 0:45:00: at most 0.75 node-hours (3 GPU-hours)" in printed
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


def test_files_that_differ_from_the_pushed_copy_stop_the_submission(
    context, script, run
):
    run.reply("cat -- job.sbatch", out=marked(SCRIPT))
    run.reply("cat -- .synced_from", out=marked(STAMP))
    run.reply("rsync", out="<f.st......|src/train.py\n<f+++++++++|conf/new.yaml\n")

    with pytest.raises(RemoteError) as refusal:
        cli.run_submit([script], context)

    message = str(refusal.value)
    assert "2 files differ here from the copy on the cluster (pushed 2026-01-02T" in (
        message
    )
    assert "src/train.py, conf/new.yaml; run `jup push`" in message
    assert "--stale-ok" in message
    assert "--dry-run" in run.ran("rsync")[0]
    assert run.ran("sbatch --parsable") == []


def test_a_pushed_copy_that_matches_is_submitted(context, script, run):
    run.reply("cat -- job.sbatch", out=marked(SCRIPT))
    run.reply("cat -- .synced_from", out=marked(STAMP))
    run.reply("rsync", out=".d..t......|src/\n")
    run.reply("sbatch --parsable", out=marked("9"))

    assert cli.run_submit([script], context) == 0


def test_a_directory_that_no_push_made_is_not_compared(context, script, run):
    run.reply("cat -- job.sbatch", out=marked(SCRIPT))
    run.reply("sbatch --parsable", out=marked("9"))

    assert cli.run_submit([script], context) == 0

    assert run.ran("rsync") == []


def test_a_stale_copy_is_submitted_when_asked_and_sbatch_never_sees_the_flag(
    context, script, run
):
    run.reply("cat -- job.sbatch", out=marked(SCRIPT))
    run.reply("sbatch --parsable", out=marked("9"))

    cli.run_submit(["--stale-ok", script], context)

    assert run.ran("rsync") == [] and run.ran(".synced_from") == []
    assert "--stale-ok" not in run.ran("sbatch --parsable")[0]


def test_a_wrapped_command_is_held_to_the_pushed_copy_as_well(context, run):
    run.reply("cat -- .synced_from", out=marked(STAMP))
    run.reply("rsync", out="<f.st......|train.py\n")

    with pytest.raises(RemoteError, match="1 file differs here"):
        cli.run_submit(["-N", "1", "-t", "5", "--wrap", "python train.py"], context)


def test_a_comparison_that_fails_names_the_way_around_it(context, script, run):
    run.reply("cat -- job.sbatch", out=marked(SCRIPT))
    run.reply("cat -- .synced_from", out=marked(STAMP))
    run.reply("rsync", code=12)

    with pytest.raises(RemoteError, match="--stale-ok submits without it"):
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


def test_a_run_that_keeps_its_scripts_name_gets_a_note(context, script, capsys):
    cli.run_submit(["--dry-run", script, "configs/sync.yaml"], context)

    printed = capsys.readouterr().out
    assert "note: this job is named job.sbatch, like every run of job.sbatch" in printed
    assert "pass --job-name=" in printed


@pytest.mark.parametrize(
    "arguments",
    [["--job-name=swe-grpo-sync", "job.sbatch", "x"], ["job.sbatch"]],
    ids=["named at submission", "a script without arguments"],
)
def test_no_note_for_a_named_run_or_a_script_that_always_runs_the_same(
    context, script, capsys, arguments
):
    cli.run_submit(["--dry-run", *arguments], context)

    assert "note:" not in capsys.readouterr().out


def test_a_wrapped_command_without_a_name_is_told_slurms_name(context, capsys):
    cli.run_submit(["--dry-run", "-N", "1", "-t", "5", "--wrap", "hostname"], context)

    assert "Slurm names this job `wrap`" in capsys.readouterr().out


def test_the_submission_reports_the_name_the_job_carries(context, script, run, capsys):
    run.reply("cat -- job.sbatch", out=marked(SCRIPT))
    run.reply("sbatch --parsable", out=marked("9"))

    cli.run_submit(["-J", "swe-grpo-sync", script, "x"], context)

    assert "submitted job 9, named swe-grpo-sync" in capsys.readouterr().out


def test_the_job_list_says_what_each_job_runs_and_cuts_no_name(context, run, capsys):
    sbatch = "sbatch --parsable --account=proj --partition=booster"
    long_name = "swe-grpo-sync-with-a-name-longer-than-thirty"
    run.reply(
        "squeue --me",
        out=marked(
            f"12|booster|{long_name}|RUNNING|5:00|40:00|1|node-01\n"
            "13_[2-4]|booster|sweep|PENDING|0:00|10:00|1|(Priority)\n"
            "--\n"
            f"11|swe-eval|booster|CANCELLED by 1000|00:02:00|00:10:00|1|0:0|"
            f"2026-01-02T03:04:05|{sbatch} --wrap python -c 'a | b'\n"
            f"12|{long_name}|booster|RUNNING|00:05:00|00:40:00|1|0:0|Unknown|"
            f"{sbatch} --time=00:40:00 scripts/train.sbatch configs/sync.yaml\n"
            f"13_[2-4]|sweep|booster|PENDING|00:00:00|00:10:00|1|0:0|Unknown|"
            f"{sbatch} --array=1-4 sweep.sbatch\n"
        ),
    )

    assert cli.run_jobs(["--days", "3"], context) == 0

    queue, _, history = capsys.readouterr().out.partition("ended in the last 3 days:")
    running, pending = queue.splitlines()[2:4]
    assert running.split()[2] == long_name
    assert running.split()[-3:] == ["node-01", "train.sbatch", "configs/sync.yaml"]
    assert pending.split()[-2:] == ["(Priority)", "sweep.sbatch"]
    [ended] = history.splitlines()[2:]
    assert ended.split()[:5] == ["11", "swe-eval", "booster", "CANCELLED", "00:02:00"]
    assert ended.endswith("--wrap python -c 'a | b'")


def test_a_long_command_is_cut_in_the_job_list(context, run, capsys):
    words = " ".join(f"task_{number:03}" for number in range(40))
    run.reply(
        "squeue --me",
        out=marked(
            f"--\n7|record|booster|COMPLETED|00:01:00|00:10:00|1|0:0|"
            f"2026-01-02T03:04:05|sbatch record.sbatch {words}\n"
        ),
    )

    cli.run_jobs([], context)

    printed = capsys.readouterr().out
    assert "queued and running:\n  none" in printed
    assert printed.rstrip().endswith("task_008 task_ ...")


def test_the_queue_is_listed_also_when_accounting_does_not_answer(context, run, capsys):
    run.reply(
        "squeue --me",
        out=marked("12|booster|swe-grpo-sync|RUNNING|5:00|40:00|1|node-01\n--\n!\n"),
    )

    assert cli.run_jobs([], context) == 0

    printed = capsys.readouterr().out
    assert "swe-grpo-sync  RUNNING" in printed
    assert "accounting (sacct) gave no answer" in printed


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


def test_cancelling_says_which_jobs_had_already_ended_and_cancels_the_rest(
    context, run, capsys
):
    run.reply(
        "sacct --jobs=7,8,9",
        out=marked(
            "7|COMPLETED|00:09:06|0:0\n8|RUNNING|00:01:00|0:0\n"
            "9_1|COMPLETED|00:02:00|0:0\n9_2|FAILED|00:00:10|1:0"
        ),
    )

    assert cli.run_cancel(["7", "8", "9"], context) == 0

    printed = capsys.readouterr().out
    assert "job 7 had already ended (COMPLETED after 00:09:06): nothing to" in printed
    assert "job 9 had already ended (2 tasks: COMPLETED, FAILED)" in printed
    assert "cancelled 8\n" in printed
    assert run.ran("scancel")[0].endswith("'scancel 8'")


def test_nothing_is_cancelled_when_every_named_job_had_ended(context, run, capsys):
    run.reply("sacct --jobs=7", out=marked("7|CANCELLED by 1000|00:02:55|0:0"))

    assert cli.run_cancel(["7"], context) == 0

    assert run.ran("scancel") == []
    assert "cancelled 7" not in capsys.readouterr().out


def test_an_array_with_an_unfinished_task_is_cancelled(context, run):
    run.reply(
        "sacct --jobs=9",
        out=marked("9_1|COMPLETED|00:02:00|0:0\n9_[2-4]|PENDING|00:00:00|0:0"),
    )

    cli.run_cancel(["9"], context)

    assert run.ran("scancel")[0].endswith("'scancel 9'")


def test_jobs_are_cancelled_also_when_accounting_does_not_answer(context, run):
    run.reply("sacct --jobs=7", code=1, err="sacct: error: connection refused")

    cli.run_cancel(["7"], context)

    assert run.ran("scancel")[0].endswith("'scancel 7'")


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


def write_session(tmp_path):
    """Write a one-turn transcript that used the plugin, and return its path."""
    records = [
        {
            "type": "user",
            "timestamp": "2026-11-12T10:00:00.000Z",
            "message": {"content": "check my jobs"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-11-12T10:00:02.000Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "b",
                        "name": "Bash",
                        "input": {"command": "jup jobs"},
                    }
                ]
            },
        },
    ]
    path = tmp_path / "abc123.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_a_report_is_prepared_from_the_session_that_last_used_the_helper(
    context, tmp_path, capsys
):
    journal.remember_session("abc123", str(write_session(tmp_path)), str(context.cwd))

    assert cli.run_feedback(["it", "said", "nothing"], context) == 0

    printed = capsys.readouterr().out
    assert "session: abc123" in printed and "nothing has been sent" in printed
    [report] = (journal.state_dir() / "feedback").iterdir()
    text = report.read_text()
    assert text.startswith("# jupiter feedback: it said nothing") and "jup jobs" in text
    assert "--send" in printed and "nuinashco/hpc-tools" in printed


def test_without_a_known_session_the_transcript_must_be_named(context):
    with pytest.raises(UserError, match="--transcript"):
        cli.run_feedback(["note"], context)


def test_a_note_from_standard_input_keeps_every_character(
    context, tmp_path, monkeypatch
):
    monkeypatch.setattr("sys.stdin", io.StringIO('it ran $(rm -rf ~) `x`; "quoted"\n'))

    cli.run_feedback(["-", "--transcript", str(write_session(tmp_path))], context)

    [report] = (journal.state_dir() / "feedback").iterdir()
    assert 'it ran $(rm -rf ~) `x`; "quoted"' in report.read_text()


def test_a_report_can_be_discarded_and_only_a_report(context, tmp_path, capsys):
    cli.run_feedback(["n", "--transcript", str(write_session(tmp_path))], context)
    [report] = (journal.state_dir() / "feedback").iterdir()

    assert cli.run_feedback(["--discard", str(report)], context) == 0
    assert not report.exists()
    with pytest.raises(UserError, match="no report"):
        cli.run_feedback(["--send", str(tmp_path / "abc123.jsonl")], context)


def test_every_run_is_noted_in_the_local_log_with_how_it_ended(
    monkeypatch, settings_home, capsys
):
    monkeypatch.setenv("JUP_HOST", "jup-test.invalid")

    cli.main(["cost", "-N", "2", "-t", "30"])
    cli.main(["jobs"])

    first, second = journal.read()
    assert (first["name"], first["args"], first["exit"], first["error"]) == (
        "cost",
        ["-N", "2", "-t", "30"],
        0,
        "",
    )
    assert (second["name"], second["exit"]) == ("jobs", 66)
    assert second["error"].startswith("NoConnectionError")


def test_the_log_can_be_switched_off(monkeypatch, settings_home, capsys):
    monkeypatch.setenv("JUP_LOG", "false")

    cli.main(["cost", "-N", "2", "-t", "30"])

    assert journal.read() == []


JUTIL = """system=jupiter
cont-partition=JUPITER_BOOSTER
budget-account=proj
acct-period-start=2026-11-01
acct-period-end=2027-02-28
coreh-quota-total=360000
coreh-avail=351198.4
coreh-avail-percent=97.6
coreh-consumed-total=8801.6
coreh-consumed-curr-month=8801.6
"""


def test_the_budget_is_told_in_node_hours_and_gpu_hours(context, run, capsys):
    run.reply("jutil project cpuquota -p proj", out=JUTIL)

    assert cli.run_budget([], context) == 0

    printed = capsys.readouterr().out
    assert (
        "proj, 2026-11-01 to 2027-02-28: 1,250 node-hours (5,000 GPU-hours)" in printed
    )
    assert "used: 30.6 node-hours (122 GPU-hours)" in printed
    assert "left: 1,219 node-hours (4,878 GPU-hours), 97.6% of it" in printed
    assert "coreh-avail" not in printed


def test_a_budget_in_a_form_not_known_is_shown_as_it_came(context, run, capsys):
    run.reply("jutil", out="quota 360000 core-h\n")

    cli.run_budget([], context)

    printed = capsys.readouterr().out
    assert "quota 360000 core-h" in printed
    assert "a node-hour is 288 of them and a GPU-hour 72" in printed


def test_the_budgets_own_output_can_be_asked_for(context, run, capsys):
    run.reply("jutil", out=JUTIL)

    cli.run_budget(["--raw"], context)

    assert "coreh-avail=351198.4" in capsys.readouterr().out


def test_a_budget_that_could_not_be_read_gets_no_note(context, run, capsys):
    run.reply("jutil", code=1, err="jutil: no such project\n")

    assert cli.run_budget([], context) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "no such project" in captured.err


def test_a_digest_goes_beside_its_report_with_who_can_read_it(
    context, run, tmp_path, monkeypatch, capsys
):
    cli.run_feedback(["n", "--transcript", str(write_session(tmp_path))], context)
    [report] = (journal.state_dir() / "feedback").iterdir()
    run.reply("gh repo view", out="PUBLIC\n")
    monkeypatch.setattr("sys.stdin", io.StringIO("# jupiter feedback: n\n\nbody\n"))

    assert cli.run_feedback(["--digest", str(report)], context) == 0

    printed = capsys.readouterr().out
    digest = feedback.find_digest(report)
    assert digest.read_text() == "# jupiter feedback: n\n\nbody\n"
    assert "is public: anyone can read" in printed and f"--send {digest}" in printed


def test_discarding_a_report_deletes_its_digest_too(context, tmp_path, monkeypatch):
    cli.run_feedback(["n", "--transcript", str(write_session(tmp_path))], context)
    [report] = (journal.state_dir() / "feedback").iterdir()
    monkeypatch.setattr("sys.stdin", io.StringIO("body"))
    cli.run_feedback(["--digest", str(report)], context)

    cli.run_feedback(["--discard", str(report)], context)

    assert list((journal.state_dir() / "feedback").iterdir()) == []


def running(elapsed):
    return marked(f"RUNNING|{elapsed}|None|1|12:00:00")


def loads(*values):
    return marked("\n".join(f"n1, {i}, {load}, 0" for i, load in enumerate(values)))


JOB = "JobId=7 JobName=x\n   Command=/e/p/repo/job.sbatch a b\n   WorkDir=/e/p/repo\n"


def watch(context, *arguments):
    """Wait for job 7 with a reading a minute, as the command does by default."""
    return cli.run_wait(
        ["7", *arguments, "--interval", "60"], context, sleep=lambda _: None
    )


def recall_loaded(name):
    return (gpus.recall(name) or {}).get("loaded")


def test_waiting_stops_when_gpus_the_job_pays_for_sit_idle(context, run, capsys):
    for minute in range(10, 16):
        run.reply("squeue --noheader --jobs=7", out=running(f"{minute}:00"))
        run.reply("srun --jobid=7", out=loads(96, 0, 0, 0))
    run.reply("scontrol show job 7", out=JOB)

    code = watch(context)

    printed = capsys.readouterr().out
    assert code == cli.IDLE_GPUS == 69
    assert "job 7: 3 of its 4 GPUs idle in each of the last 5 readings" in printed
    assert "n1: GPU 0 96%, GPU 1 0%, GPU 2 0%, GPU 3 0%" in printed
    assert "0.23 node-hours (0.93 GPU-hours) so far, 12 node-hours" in printed
    assert "nothing was cancelled; to choose what happens next, run one of" in printed
    assert "jup cancel 7 " in printed and "jup wait 7 --idle-ok " in printed
    assert "Claude" not in printed
    assert recall_loaded("repo/job.sbatch") == 1
    assert len(run.ran("srun --jobid=7")) == 5


def test_a_job_is_not_read_before_its_warm_up_nor_judged_by_one_reading(
    context, run, capsys
):
    for elapsed in ("2:00", "9:59", "10:00", "11:00"):
        run.reply("squeue --noheader --jobs=7", out=running(elapsed))
    run.reply("squeue --noheader --jobs=7", out="")
    for _ in range(2):
        run.reply("srun --jobid=7", out=loads(0, 0, 0, 0))
    run.reply("sacct --jobs=7", out=marked("7|COMPLETED|00:11:30|0:0"))
    run.reply("scontrol show job 7", out=JOB)

    assert watch(context) == 0

    assert len(run.ran("srun --jobid=7")) == 2
    assert "GPU 0 0%, GPU 1 0%" in capsys.readouterr().out
    assert recall_loaded("repo/job.sbatch") == 0


def test_idle_gpus_the_user_accepted_do_not_stop_the_wait(context, run, capsys):
    for minute in range(10, 18):
        run.reply("squeue --noheader --jobs=7", out=running(f"{minute}:00"))
        run.reply("srun --jobid=7", out=loads(96, 0, 0, 0))
    run.reply("squeue --noheader --jobs=7", out="")
    run.reply("sacct --jobs=7", out=marked("7|COMPLETED|00:18:00|0:0"))

    assert watch(context, "--idle-ok") == 0
    assert "(mean load over 8 readings)" in capsys.readouterr().out


def test_in_a_claude_code_session_the_idle_report_has_claude_ask_the_user(
    context, capsys
):
    watched = gpus.Watch.of(2, 60)
    for _ in range(2):
        watched.add(gpus.parse_readings(["n1, 0, 96, 0", "n1, 1, 0, 0"]))

    cli.report_idle(watched, "7", 1, 600, 3600, context.config, {"CLAUDECODE": "1"})

    printed = capsys.readouterr().out
    assert "is the user's choice\n  Claude: ask the user now, in a question" in printed
    assert "jup cancel 7 " in printed and "jup wait 7 --idle-ok " in printed


def test_a_running_jobs_gpus_can_be_read_at_any_time(context, run, capsys):
    run.reply("squeue --noheader --jobs=7", out=marked("RUNNING|1"))
    run.reply("srun --jobid=7", out=marked("n1, 0, 97, 23807\nn1, 1, 0, 0"))

    assert cli.run_gpus(["7"], context) == 0
    assert capsys.readouterr().out == "n1: GPU 0 97% (23,807 MiB), GPU 1 0% (0 MiB)\n"


def test_a_job_that_does_not_run_has_no_gpus_to_read(context, run):
    run.reply("squeue --noheader --jobs=7", out=marked("PENDING|1"))

    with pytest.raises(UserError, match="job 7 is PENDING"):
        cli.run_gpus(["7"], context)


def test_a_submission_says_what_is_known_of_the_scripts_gpu_load(context, capsys):
    (context.config.root / "one.sbatch").write_text(
        "#!/bin/bash\n#SBATCH -N 1\n#SBATCH -t 30\n#SBATCH --gres=gpu:1\n"
    )

    cli.run_submit(["--dry-run", "one.sbatch"], context)

    printed = capsys.readouterr().out
    assert "it asks for 1 of a node's 4 GPUs" in printed
    assert "GPU load: no job of this script has been measured" in printed


@pytest.mark.parametrize(
    "command", ["(null)", "/e/elsewhere/job.sbatch", "/e/p/repository/job.sbatch"]
)
def test_a_job_without_a_script_of_the_project_leaves_no_result(context, run, command):
    for minute in range(10, 12):
        run.reply("squeue --noheader --jobs=7", out=running(f"{minute}:00"))
        run.reply("srun --jobid=7", out=loads(96, 96, 96, 96))
    run.reply("squeue --noheader --jobs=7", out="")
    run.reply("sacct --jobs=7", out=marked("7|COMPLETED|00:12:00|0:0"))
    run.reply(
        "scontrol show job 7", out=f"   Command={command}\n   WorkDir=/e/p/repo\n"
    )

    assert watch(context) == 0
    assert not (journal.state_dir() / gpus.MEMORY).exists()
