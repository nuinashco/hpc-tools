"""Readings of a job's GPUs: parsing them, judging them, remembering the result."""

from __future__ import annotations

import pytest

from jup import gpus
from jup.ssh import MARK, Remote


def reading(*loads, node="n1"):
    return [gpus.Reading(node, index, load, 0) for index, load in enumerate(loads)]


def test_readings_are_parsed_and_other_lines_left():
    lines = ["n1, 0, 97, 23807", "srun: job 7 queued", "n1, 1, 0, 0", "n1, x, 1, 2"]

    assert gpus.parse_readings(lines) == [
        gpus.Reading("n1", 0, 97, 23807),
        gpus.Reading("n1", 1, 0, 0),
    ]


def test_a_gpu_is_idle_only_after_enough_idle_readings_in_a_row():
    watch = gpus.Watch(patience=3)
    for loads in ((90, 0), (95, 2), (80, 60), (85, 0), (90, 1)):
        watch.add(reading(*loads))

    assert watch.idle() == []

    watch.add(reading(88, 4))

    assert watch.idle() == [("n1", 1)]


def test_a_reading_that_failed_changes_nothing():
    watch = gpus.Watch(patience=2)
    watch.add(reading(0, 0))
    watch.add([])

    assert (watch.readings, watch.idle()) == (1, [])


def test_a_watch_sums_up_how_many_gpus_a_job_loaded():
    watch = gpus.Watch(patience=2)
    for loads in ((100, 0, 40, 4), (80, 0, 60, 4)):
        watch.add(reading(*loads))

    assert watch.result() == {
        "gpus": 4,
        "loaded": 2,
        "means": [90, 0, 50, 4],
        "readings": 2,
    }
    assert watch.describe() == [
        "n1: GPU 0 90%, GPU 1 0%, GPU 2 50%, GPU 3 4% (mean load over 2 readings)"
    ]


@pytest.mark.parametrize(
    ("minutes", "interval", "patience"), [(5, 60, 5), (5, 120, 3), (1, 600, 2)]
)
def test_patience_is_the_idle_minutes_in_readings(minutes, interval, patience):
    assert gpus.Watch.of(minutes, interval).patience == patience


def test_a_jobs_gpus_are_read_on_each_of_its_nodes(run):
    run.reply("srun --jobid=7", out=f"{MARK}n1, 0, 50, 10\n{MARK}n2, 0, 0, 0\n")

    found = gpus.sample(Remote("cluster", run=run), "7", 2)

    [command] = run.ran("srun --jobid=7")
    assert "--overlap --nodes=2 --ntasks-per-node=1" in command
    assert [(gpu.node, gpu.load) for gpu in found] == [("n1", 50), ("n2", 0)]


def test_gpus_that_cannot_be_read_give_no_reading(run):
    run.reply("srun --jobid=7", code=1, err="srun: error: job step denied\n")

    assert gpus.sample(Remote("cluster", run=run), "7", 1) == []


def test_a_scripts_last_result_is_remembered_and_told():
    name = "repo/job.sbatch"
    result = {"job": "9", "when": "2026-11-12T08:00:00Z", "gpus": 4, "loaded": 4}

    assert "no job of this script has been measured" in gpus.describe_last(None)

    gpus.remember(name, {**result, "digest": "abc"})

    assert gpus.recall(name) == {**result, "digest": "abc"}
    assert gpus.recall("repo/other.sbatch") is None
    told = gpus.describe_last(gpus.recall(name), "abc")
    assert told == "its last measured job (9, 2026-11-12) loaded 4 of 4 GPUs"
    assert gpus.describe_last(gpus.recall(name), "def").endswith("changed since")


def test_readings_are_shown_node_by_node():
    found = [*reading(97, 0, node="n2"), gpus.Reading("n1", 0, 5, 23807)]

    assert gpus.describe_readings(found) == [
        "n1: GPU 0 5% (23,807 MiB)",
        "n2: GPU 0 97% (0 MiB), GPU 1 0% (0 MiB)",
    ]
