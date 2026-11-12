"""How loaded a running job's GPUs are: read them, judge them, remember the result.

A node is billed with all its GPUs, so one that stays idle is paid for and not used.
A command cannot tell beforehand whether a job will load them; a reading of the job
while it runs can.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jup import journal
from jup.errors import UserError
from jup.ssh import Remote

# A GPU below this load, in percent, counts as idle: a server that waits shows 0 or 1.
IDLE_BELOW = 5
# Kept apart from the log, which forgets its oldest lines.
MEMORY = "gpu-load.json"
_QUERY = (
    "nvidia-smi --query-gpu=index,utilization.gpu,memory.used "
    "--format=csv,noheader,nounits"
)


@dataclass(frozen=True)
class Reading:
    """One GPU at one moment: its load in percent and its memory in use, in MiB."""

    node: str
    index: int
    load: int
    memory: int


def parse_readings(lines: Sequence[str]) -> list[Reading]:
    """Return the GPUs in lines of `node, index, load, memory`; other lines are left."""
    readings = []
    for line in lines:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 4 and all(part.isdigit() for part in parts[1:]):
            readings.append(Reading(parts[0], *(int(part) for part in parts[1:])))
    return readings


def sample(remote: Remote, job: str, nodes: int) -> list[Reading]:
    """Return the load of every GPU of a running job; none when it cannot be read.

    One short step on each of the job's nodes, beside whatever runs there.
    """
    inner = f'{_QUERY} | sed "s/^/$(hostname), /"'
    script = (
        f"srun --jobid={job} --overlap --nodes={nodes} --ntasks-per-node=1 "
        f"sh -c {shlex.quote(inner)}"
    )
    try:
        return parse_readings(remote.read_lines(script))
    except UserError:
        return []


def describe_readings(readings: Sequence[Reading]) -> list[str]:
    """Return one line for each node: its GPUs' load and memory now."""
    nodes: dict[str, list[Reading]] = {}
    for reading in readings:
        nodes.setdefault(reading.node, []).append(reading)
    return [
        f"{node}: "
        + ", ".join(
            f"GPU {gpu.index} {gpu.load}% ({gpu.memory:,} MiB)"
            for gpu in sorted(found, key=lambda gpu: gpu.index)
        )
        for node, found in sorted(nodes.items())
    ]


@dataclass
class Watch:
    """The readings of one job since its warm-up, and what they add up to."""

    # How many readings in a row make a GPU idle.
    patience: int
    readings: int = 0
    sums: dict[tuple[str, int], int] = field(default_factory=dict)
    # Readings in a row, up to the latest, in which each GPU was idle.
    streaks: dict[tuple[str, int], int] = field(default_factory=dict)

    @classmethod
    def of(cls, idle_minutes: float, interval: float) -> Watch:
        """Return a watch that calls a GPU idle after `idle_minutes` of readings."""
        return cls(max(2, math.ceil(idle_minutes * 60 / interval)))

    def add(self, readings: Sequence[Reading]) -> None:
        """Take in one reading of every GPU."""
        if not readings:
            return
        self.readings += 1
        for reading in readings:
            key = (reading.node, reading.index)
            self.sums[key] = self.sums.get(key, 0) + reading.load
            idle = reading.load < IDLE_BELOW
            self.streaks[key] = self.streaks.get(key, 0) + 1 if idle else 0

    def idle(self) -> list[tuple[str, int]]:
        """Return the GPUs that were idle in every one of the latest readings."""
        return sorted(key for key, run in self.streaks.items() if run >= self.patience)

    def means(self) -> dict[tuple[str, int], float]:
        """Return each GPU's mean load over the readings."""
        return {key: total / self.readings for key, total in sorted(self.sums.items())}

    def describe(self) -> list[str]:
        """Return one line for each node: its GPUs' mean load over the readings."""
        nodes: dict[str, list[str]] = {}
        for (node, index), mean in self.means().items():
            nodes.setdefault(node, []).append(f"GPU {index} {mean:.0f}%")
        over = f"mean load over {self.readings} readings"
        return [f"{node}: {', '.join(gpus)} ({over})" for node, gpus in nodes.items()]

    def result(self) -> dict[str, Any]:
        """Return what to remember of the job: how many GPUs it had and loaded."""
        means = self.means()
        return {
            "gpus": len(means),
            "loaded": sum(mean >= IDLE_BELOW for mean in means.values()),
            "means": [round(mean) for mean in means.values()],
            "readings": self.readings,
        }


def name_script(root: Path, script: str) -> str:
    """Return the name a script's results are kept under: its project and its path."""
    return f"{root.name}/{script.lstrip('./')}"


def read_digest(path: Path) -> str:
    """Return what tells one version of a script from another; nothing if unread."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _memory(env: Mapping[str, str]) -> Path:
    return journal.state_dir(env) / MEMORY


def recall(name: str, env: Mapping[str, str] = os.environ) -> dict[str, Any] | None:
    """Return the result of a script's last measured job, if one was measured."""
    try:
        known = json.loads(_memory(env).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    found = known.get(name) if isinstance(known, dict) else None
    return found if isinstance(found, dict) else None


def remember(
    name: str, result: Mapping[str, Any], env: Mapping[str, str] = os.environ
) -> None:
    """Keep the result of a script's job, in place of an earlier one."""
    path = _memory(env)
    try:
        known = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        known = {}
    if not isinstance(known, dict):
        known = {}
    known[name] = dict(result)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(known, indent=1) + "\n")
    except OSError:
        # A result that cannot be kept must not stop the command that measured it.
        return


def describe_last(result: Mapping[str, Any] | None, digest: str = "") -> str:
    """Return what is known of a script's GPU load from its last measured job."""
    if result is None:
        return (
            "no job of this script has been measured: a test that runs past its "
            "warm-up shows whether it loads every GPU"
        )
    changed = digest and result.get("digest") and digest != result["digest"]
    text = (
        f"its last measured job ({result.get('job', '?')}, "
        f"{str(result.get('when', '?'))[:10]}) loaded {result.get('loaded', '?')} of "
        f"{result.get('gpus', '?')} GPUs"
    )
    return f"{text}; the script has changed since" if changed else text
