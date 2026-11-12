"""A command's submissions are found as the shell runs them and read as Slurm does."""

from __future__ import annotations

import pytest

from jup import slurm
from jup.errors import UserError


def request(options, directives=None):
    return slurm.Request.of(
        options, directives or {}, default_time="1:00:00", max_time="12:00:00"
    )


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("30", 1800),
        ("90:30", 5430),
        ("1:30:00", 5400),
        ("2-00", 172800),
        ("1-12:30", 131400),
        ("0-00:10:05", 605),
    ],
)
def test_each_slurm_time_format_gives_its_seconds(text, seconds):
    assert slurm.parse_time(text) == seconds


def test_a_time_slurm_would_not_read_names_the_formats():
    with pytest.raises(UserError, match="H:M:S"):
        slurm.parse_time("soon")


def test_options_end_at_the_script_whose_own_arguments_follow():
    arguments = ["-N2", "--time=1:00:00", "-p", "booster", "job.sh", "--lr", "3"]

    options, rest = slurm.parse_options(arguments)

    assert options == {"nodes": "2", "time": "1:00:00", "partition": "booster"}
    assert rest == ["job.sh", "--lr", "3"]


@pytest.mark.parametrize(
    ("arguments", "kind", "nodes"),
    [
        (["-H", "-N", "64", "job.sh"], "sbatch", "64"),
        (["-HN4", "job.sh"], "sbatch", "4"),
        (["--exclusive", "-N", "3", "job.sh"], "sbatch", "3"),
        (["-k", "-N", "5", "job.sh"], "sbatch", "5"),
        (["--no-shell", "-N", "8"], "salloc", "8"),
        (["--pty", "-N", "2", "bash"], "srun", "2"),
    ],
)
def test_an_option_without_a_value_leaves_the_next_one_alone(arguments, kind, nodes):
    assert slurm.parse_options(arguments, kind)[0]["nodes"] == nodes


def test_srun_reads_a_value_after_the_letter_that_is_a_flag_for_sbatch():
    assert slurm.parse_options(["-W", "5", "-N", "2", "x"], "srun")[0]["nodes"] == "2"
    assert "nodes" in slurm.parse_options(["-W", "-N", "2", "x"], "sbatch")[0]


def test_an_abbreviated_long_option_is_read_as_slurm_reads_it():
    options, _ = slurm.parse_options(["--arr=0-9", "--part", "booster", "job.sh"])

    assert (options["array"], options["partition"]) == ("0-9", "booster")


def test_directives_are_read_past_comments_until_the_first_command():
    script = "\n".join(
        [
            "#!/bin/bash",
            "#SBATCH --nodes=2  # two",
            "  # an indented note",
            "",
            "#SBATCH --comment=a#b -t 30",
            "echo hi",
            "#SBATCH --nodes=9",
        ]
    )

    assert slurm.read_directives(script) == {
        "nodes": "2",
        "comment": "a#b",
        "time": "30",
    }


def test_a_heterogeneous_script_is_marked():
    assert "hetjob" in slurm.read_directives(
        "#SBATCH -N 2\n#SBATCH hetjob\n#SBATCH -N 62\n"
    )


@pytest.mark.parametrize(
    ("spec", "tasks"), [("0-9", 10), ("1,3,5", 3), ("0-99%4", 100), ("0-10:5", 3)]
)
def test_a_job_array_counts_its_jobs(spec, tasks):
    assert slurm.count_array_tasks(spec) == tasks


def test_command_options_win_over_the_scripts():
    asked = request({"nodes": "1-3", "time": "2:00:00"}, {"nodes": "8", "array": "0-1"})

    assert (asked.nodes, asked.seconds, asked.tasks, asked.doubts) == (3, 7200, 2, ())
    assert asked.node_hours() == 2 * 3 * 2


@pytest.mark.parametrize(
    ("time", "gpus", "cost"),
    [
        ("12:00:00", 4, "at most 12 node-hours (48 GPU-hours)"),
        ("40", 4, "at most 0.67 node-hours (2.67 GPU-hours)"),
        ("15", 4, "at most 0.25 node-hours (1 GPU-hour)"),
        ("1:00:00", 0, "at most 1 node-hour"),
    ],
)
def test_a_cost_is_told_in_node_hours_and_in_gpu_hours_where_nodes_have_gpus(
    time, gpus, cost
):
    assert request({"nodes": "1", "time": time}).describe(gpus).endswith(cost)


def test_a_large_cost_is_told_in_whole_hours():
    asked = request({"nodes": "100", "time": "12:00:00", "array": "0-1"})

    assert asked.describe(4).endswith("at most 2,400 node-hours (9,600 GPU-hours)")


@pytest.mark.parametrize(
    ("options", "doubt"),
    [
        ({"time": "10"}, "no --nodes, one assumed"),
        ({"time": "10", "ntasks": "2000"}, "--ntasks decides how many"),
        ({"nodes": "1"}, "no --time, the default 1:00:00 assumed"),
        ({"nodes": "1", "time": "UNLIMITED"}, "asks for no limit, 12:00:00 assumed"),
        ({"nodes": "1", "time": "0"}, "asks for no limit"),
        ({"nodes": "1", "time": "5", "no-shell": ""}, "keeps the nodes"),
        ({"nodes": "1", "time": "5", "hetjob": ""}, "heterogeneous"),
    ],
)
def test_what_a_request_leaves_open_is_named(options, doubt):
    asked = request(options)

    assert any(doubt in text for text in asked.doubts)
    assert "not certain" in asked.describe(4)


def test_no_limit_is_priced_at_the_partitions_longest_job():
    assert request({"nodes": "2", "time": "-1"}).seconds == 12 * 3600


def test_a_node_count_that_is_no_number_is_refused():
    with pytest.raises(UserError, match="node count"):
        request({"nodes": "1k"})


def test_output_directories_are_found_unless_their_names_are_patterns():
    options = {"output": "logs/%x-%j.out", "error": "err/%j/e.log"}

    assert slurm.find_output_directories(options) == ["logs"]


def found(command):
    result = slurm.scan(command)
    assert result is not None
    return [(s.kind, s.route, s.host, list(s.arguments)) for s in result]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "sbatch -N 2 job.sh && squeue --me",
            [("sbatch", "local", "", ["-N", "2", "job.sh"])],
        ),
        (
            "cd x\nsbatch -N 4 \\\n  -t 60 job.sh",
            [("sbatch", "local", "", ["-N", "4", "-t", "60", "job.sh"])],
        ),
        (
            "ssh -p 22 me@hpc 'cd p && sbatch -t 10 j.sh'",
            [("sbatch", "ssh", "hpc", ["-t", "10", "j.sh"])],
        ),
        (
            "jup submit --time=2:00:00 run.sbatch a b",
            [("sbatch", "jup", "", ["--time=2:00:00", "run.sbatch", "a", "b"])],
        ),
        (
            "jup ssh -C 'salloc -N 4 -t 60'; ls",
            [("salloc", "jup", "", ["-N", "4", "-t", "60"])],
        ),
        (
            "bash -c 'sbatch -N 8 job.sh'",
            [("sbatch", "local", "", ["-N", "8", "job.sh"])],
        ),
        (
            "env A=1 timeout 30 /usr/bin/sbatch -N 2 j.sh",
            [("sbatch", "local", "", ["-N", "2", "j.sh"])],
        ),
        ("echo $(sbatch -N 3 j.sh)", [("sbatch", "local", "", ["-N", "3", "j.sh"])]),
        ("x=`srun -N 2 hostname`", [("srun", "local", "", ["-N", "2", "hostname"])]),
        (
            "sbatch --comment=a#b -N 3 j.sh  # or sbatch -N 99",
            [("sbatch", "local", "", ["--comment=a#b", "-N", "3", "j.sh"])],
        ),
        (
            "sbatch -N 1 j.sh > out.txt 2>&1",
            [("sbatch", "local", "", ["-N", "1", "j.sh"])],
        ),
        (
            "jup config set ask_above_nodes 1000",
            [("config", "jup", "", ["ask_above_nodes", "1000"])],
        ),
        (
            "jup ssh 'scontrol update JobId=5 TimeLimit=12:00:00'",
            [("scontrol", "jup", "", ["update", "JobId=5", "TimeLimit=12:00:00"])],
        ),
        ("squeue --me | grep sbatch_logs", []),
        ("grep -rn sbatch scripts/ && echo sbatch", []),
        ("jup config set account proj; jup jobs; scontrol show job 5", []),
        ("# sbatch -N 64 job.sh\nls", []),
    ],
)
def test_submissions_are_found_wherever_a_command_holds_them(command, expected):
    assert found(command) == expected


@pytest.mark.parametrize(
    ("command", "doubt"),
    [
        ("for i in 1 2 3; do jup submit job.sh; done", "in a loop"),
        ("seq 9 | xargs -I{} sbatch job.sh {}", "in a loop"),
        ("SBATCH_TIMELIMIT=12:00:00 sbatch job.sh", "SBATCH_TIMELIMIT is set"),
        ("sbatch <<EOF\n#!/bin/bash\n#SBATCH -N 64\nEOF", "standard input"),
        ("sbatch -N $NODES job.sh", "shell variable"),
        ("sbatch -N2 : -N62 job.sh", "heterogeneous"),
    ],
)
def test_what_makes_a_submissions_cost_unsure_is_named(command, doubt):
    [first, *_] = slurm.scan(command) or [None]

    assert first is not None
    assert any(doubt in text for text in first.doubts)


def test_a_command_the_shell_would_not_read_gives_no_answer():
    assert slurm.scan("sbatch -N 64 'unbalanced") is None


@pytest.mark.parametrize(
    ("job", "valid"),
    [
        ("123456", True),
        ("123456_7", True),
        ("123_[0-9%2]", True),
        ("123+1", True),
        ("--me", False),
        ("-u", False),
        ("12;id", False),
        ("", False),
        ("abc", False),
    ],
)
def test_only_a_job_number_passes_as_one(job, valid):
    assert bool(slurm.JOB_ID.match(job)) is valid


@pytest.mark.parametrize(
    ("command", "kept", "bodies"),
    [
        ("cat <<EOF\na\nEOF\nls", "cat << M0\nls", ["a"]),
        ("cat <<'E O'\na 'b\nE O\nls", "cat << M0\nls", ["a 'b"]),
        ("cat <<-EOF\n\ta\n\tEOF", "cat << M0", ["\ta"]),
        ("cat <<A; cat <<B\n1\nA\n2\nB", "cat << M0; cat << M1", ["1", "2"]),
        ("cat <<EOF \\\n  -n\na\nEOF", "cat << M0 \\\n  -n", ["a"]),
        ("cat <<EOF\nnever closed", "cat << M0", ["never closed"]),
    ],
)
def test_a_here_documents_body_is_lifted_out_of_its_command(command, kept, bodies):
    lifted, found = slurm.lift_documents(command)

    assert lifted.replace("JUP_HERE_DOCUMENT_", "M") == kept
    assert list(found.values()) == bodies


@pytest.mark.parametrize(
    "command",
    [
        "cat <<< EOF\nls",
        "echo $((1 << 4))\nls",
        "echo 'a <<EOF'\nls\nEOF",
        'python3 -c "\nx = 1 <<EOF\n"\nls',
        "ls # <<EOF\npwd",
    ],
)
def test_what_only_looks_like_a_here_document_stays(command):
    assert slurm.lift_documents(command) == (command, {})


def test_a_pipe_is_told_from_what_ends_a_command():
    tokens = slurm.tokenize("a | b |&\n c || d; e |\n f")

    assert tokens is not None
    assert slurm.split_pipeline(tokens) == [
        (["a"], True),
        (["b"], True),
        (["c"], False),
        (["d"], False),
        (["e"], True),
        (["f"], False),
    ]
