"""Settings: defaults, the user's file, the project's file, then JUP_* variables."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jup.errors import ConfigError, UserError
from jup.slurm import parse_time

PROJECT_FILE = ".jupiter.json"
# A node of the booster partition is billed whole, at its 288 cores, whatever runs on it
# (`sacct` of job 2800157, 2026-11-09; JSC's accounting page).
BOOSTER_CORES = 288
# Each has four GH200 GPUs, which a job gets with the node whether it uses them or not.
BOOSTER_GPUS = 4

DEFAULTS: dict[str, Any] = {
    "host": "hpc-jupiter",
    "account": None,
    "partition": "booster",
    "remote_dir": "/e/project1/{account}/{user}/{project}",
    "sync_exclude": [],
    # What the budget counts for a node-hour: `jutil` reports in core-hours.
    "cores_per_node": BOOSTER_CORES,
    "gpus_per_node": BOOSTER_GPUS,
    # What the booster partition gives a job without --time, and at most (JSC's batch
    # page, 2026-11-12).
    "default_time": "01:00:00",
    "max_time": "12:00:00",
    "ask_above_nodes": 1,
    "ask_above_node_hours": 1,
    # Whether the helper keeps its local log of runs, which `jup feedback` reads.
    "log": True,
    # Where `jup feedback --send` files its report: this plugin's own repository.
    "feedback_repo": "nuinashco/hpc-tools",
}
# Settings a repository's file may not set: the ssh alias is each person's own, and the
# rest decide what the cost check sees.
PERSONAL = frozenset(
    {
        "host",
        "cores_per_node",
        "gpus_per_node",
        "default_time",
        "max_time",
        "log",
        "feedback_repo",
    }
)
# Limits of the cost check: a repository's file may lower them, never raise them.
LIMITS = frozenset({"ask_above_nodes", "ask_above_node_hours"})
_LISTS = frozenset({"sync_exclude"})
_NUMBERS = frozenset({"cores_per_node", "gpus_per_node", *LIMITS})
_TIMES = frozenset({"default_time", "max_time"})
_SWITCHES = frozenset({"log"})
_YES = frozenset({"true", "yes", "on", "1"})
_NO = frozenset({"false", "no", "off", "0"})
# What a value may hold. These strings reach command lines here and on the cluster, and
# the text Claude is shown, so anything else is refused.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")
_PATH = re.compile(r"^/[A-Za-z0-9._/{}-]+$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")


def user_file(env: Mapping[str, str] = os.environ) -> Path:
    """Return the path of the user's settings file."""
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "hpc-tools" / "jupiter.json"


def find_repository_root(start: Path) -> Path:
    """Return the nearest directory at or above `start` with `.git`, or `start`."""
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return start


def find_project_file(start: Path) -> Path | None:
    """Return the project file at or above `start`, no higher than its repository."""
    top = find_repository_root(start)
    for directory in (start, *start.parents):
        if (directory / PROJECT_FILE).is_file():
            return directory / PROJECT_FILE
        if directory == top:
            break
    return None


def check(key: str, value: Any) -> Any:
    """Return a setting's value in its own type, or say what it must be."""
    if key not in DEFAULTS:
        raise ConfigError(
            f"unknown setting {key}; the settings are {', '.join(DEFAULTS)}"
        )
    if key in _LISTS:
        items = value.split(",") if isinstance(value, str) else value
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ConfigError(f'{key} takes a list of patterns; write ["notes/"]')
        cleaned = [item.strip() for item in items if item.strip()]
        if any("\n" in item or item.startswith("-") for item in cleaned):
            raise ConfigError(f"{key} takes plain patterns such as notes/ or *.ipynb")
        return cleaned
    if key in _NUMBERS:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"{key} takes a number of 0 or more, not {value!r}")
        return value
    if key in _SWITCHES:
        if not isinstance(value, bool):
            raise ConfigError(f"{key} takes true or false, not {value!r}")
        return value
    if key == "account" and value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} takes text, not {value!r}")
    if key in _TIMES:
        try:
            if parse_time(value) <= 0:
                raise UserError("no time")
        except UserError:
            raise ConfigError(f"{key} takes a time limit such as 01:00:00") from None
        return value
    if key == "feedback_repo":
        if not _REPOSITORY.match(value):
            raise ConfigError(f"{key} takes a GitHub repository as owner/name")
        return value
    pattern = _PATH if key == "remote_dir" else _NAME
    if not pattern.match(value) or ".." in value.split("/"):
        allowed = "letters, digits and . _ @ -"
        if key == "remote_dir":
            allowed = "an absolute path of letters, digits and . _ - /, with {account}"
        raise ConfigError(f"{key} is {value!r}; it may hold only {allowed}")
    return value


def parse(key: str, text: str) -> Any:
    """Return a setting's value from the text a command line or a variable gives."""
    if key in _NUMBERS:
        try:
            return check(key, float(text) if "." in text else int(text))
        except ValueError:
            raise ConfigError(f"{key} takes a number, not {text!r}; pass one") from None
    if key in _SWITCHES:
        word = text.strip().lower()
        if word not in _YES | _NO:
            raise ConfigError(f"{key} takes true or false, not {text!r}")
        return word in _YES
    return check(key, text)


def read(path: Path) -> dict[str, Any]:
    """Return the settings a file holds, checked; a missing file holds none."""
    if not path.is_file():
        return {}
    try:
        values = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(
            f"{path} is not valid JSON ({error}); fix or remove it"
        ) from error
    if not isinstance(values, dict):
        raise ConfigError(f"{path} must hold one JSON object; fix or remove it")
    try:
        return {key: check(key, value) for key, value in values.items()}
    except ConfigError as error:
        raise ConfigError(f"in {path}: {error}") from None


def write(path: Path, key: str, text: str) -> None:
    """Set one setting in a file, keeping the others."""
    value = parse(key, text)
    if path.name == PROJECT_FILE and key in PERSONAL:
        raise ConfigError(
            f"{key} is each person's own setting; set it without --project"
        )
    values = read(path)
    values[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, indent=2) + "\n")


@dataclass(frozen=True)
class Config:
    """The settings in effect, and the file or variable each one came from."""

    host: str
    account: str | None
    partition: str
    remote_dir: str
    sync_exclude: tuple[str, ...]
    cores_per_node: int
    gpus_per_node: int
    default_time: str
    max_time: str
    ask_above_nodes: float
    ask_above_node_hours: float
    log: bool
    feedback_repo: str
    # Where `push` copies from and what {project} names.
    root: Path
    sources: dict[str, str] = field(default_factory=dict, compare=False)

    @classmethod
    def load(cls, cwd: Path, env: Mapping[str, str] = os.environ) -> Config:
        """Return the settings for a command run in `cwd`."""
        values = dict(DEFAULTS)
        sources = {key: "default" for key in DEFAULTS}
        personal = user_file(env)
        for key, value in read(personal).items():
            values[key], sources[key] = value, str(personal)
        project = find_project_file(cwd)
        for key, value in (read(project) if project else {}).items():
            if key in PERSONAL:
                raise ConfigError(
                    f"{project} sets {key}, which is each person's own setting; "
                    f"remove it there and run `jup config set {key} ...`"
                )
            if key in LIMITS and value >= values[key]:
                continue
            values[key], sources[key] = value, str(project)
        for key in DEFAULTS:
            variable = f"JUP_{key.upper()}"
            if env.get(variable):
                values[key], sources[key] = parse(key, env[variable]), f"${variable}"
        root = project.parent if project else find_repository_root(cwd)
        return cls(
            host=values["host"],
            account=values["account"],
            partition=values["partition"],
            remote_dir=values["remote_dir"],
            sync_exclude=tuple(values["sync_exclude"]),
            cores_per_node=int(values["cores_per_node"]),
            gpus_per_node=int(values["gpus_per_node"]),
            default_time=values["default_time"],
            max_time=values["max_time"],
            ask_above_nodes=values["ask_above_nodes"],
            ask_above_node_hours=values["ask_above_node_hours"],
            log=values["log"],
            feedback_repo=values["feedback_repo"],
            root=root,
            sources=sources,
        )

    def need_account(self) -> str:
        """Return the account, or say how to set it."""
        if not self.account:
            raise ConfigError(
                "no compute account is set; run `jup config set account <project>` "
                "(JuDoor lists your projects)"
            )
        return self.account

    def remote_path(self, user: str) -> str:
        """Return the project's directory on the cluster."""
        fields = {"user": user, "project": self.root.name}
        if "{account}" in self.remote_dir:
            fields["account"] = self.need_account()
        try:
            path = self.remote_dir.format(**fields)
        except (KeyError, IndexError, ValueError, AttributeError) as error:
            raise ConfigError(
                f"remote_dir cannot be filled in ({error}); use {{account}}, {{user}} "
                "and {project}"
            ) from error
        # The directory's name is the project's here, which may hold anything.
        if not _PATH.match(path) or "{" in path:
            raise ConfigError(
                f"the directory on the cluster would be {path!r}, which holds more "
                "than letters, digits and . _ - /; set remote_dir to a plain path"
            )
        return path
