"""Settings are checked on the way in, and a repository's file is trusted least."""

from __future__ import annotations

import json

import pytest

from jup import config
from jup.config import Config
from jup.errors import ConfigError


def personal(settings_home, **values):
    path = settings_home / "hpc-tools" / "jupiter.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values))


def test_the_projects_file_wins_over_the_persons_and_a_variable_over_both(
    project, settings_home
):
    personal(settings_home, account="mine", partition="cpu", host="my-alias")
    (project / "src").mkdir()
    env = {"XDG_CONFIG_HOME": str(settings_home), "JUP_PARTITION": "other"}

    loaded = Config.load(project / "src", env=env)

    assert (loaded.account, loaded.partition, loaded.host) == (
        "proj",
        "other",
        "my-alias",
    )
    assert loaded.root == project
    assert loaded.sources["partition"] == "$JUP_PARTITION"


@pytest.mark.parametrize(
    "key", ["host", "cores_per_node", "gpus_per_node", "default_time", "max_time"]
)
def test_a_repository_cannot_set_what_is_each_persons_own(project, key):
    value = {"host": "evil-host"}.get(key, 1 if "per_node" in key else "00:01:00")
    (project / ".jupiter.json").write_text(json.dumps({key: value}))

    with pytest.raises(ConfigError, match="each person's own"):
        Config.load(project)


def test_a_repository_may_lower_the_cost_checks_limits_and_never_raise_them(project):
    limits = {"ask_above_nodes": 1000, "ask_above_node_hours": 0.5}
    (project / ".jupiter.json").write_text(json.dumps(limits))

    loaded = Config.load(project)

    assert (loaded.ask_above_nodes, loaded.ask_above_node_hours) == (1, 0.5)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("acount", "typo", "unknown setting acount"),
        ("account", 5, "takes text"),
        ("host", "-oProxyCommand=touch x", "may hold only"),
        ("account", "p; rm -rf ~", "may hold only"),
        ("remote_dir", "/e/a b", "may hold only"),
        ("remote_dir", "/e/../etc", "may hold only"),
        ("remote_dir", "relative/path", "may hold only"),
        ("sync_exclude", 7, "list of patterns"),
        ("ask_above_nodes", True, "number of 0 or more"),
        ("ask_above_node_hours", -1, "number of 0 or more"),
        ("ask_above_core_hours", 300, "unknown setting"),
        ("default_time", "soon", "time limit"),
    ],
)
def test_a_value_of_the_wrong_kind_is_refused_with_the_file_named(
    settings_home, tmp_path, key, value, message
):
    personal(settings_home, **{key: value})

    with pytest.raises(ConfigError, match=message) as failure:
        Config.load(tmp_path)

    assert "jupiter.json" in str(failure.value)


def test_patterns_may_be_a_list_or_one_text_with_commas(project):
    (project / ".jupiter.json").write_text('{"sync_exclude": "notes/, *.ipynb"}')

    assert Config.load(project).sync_exclude == ("notes/", "*.ipynb")


def test_the_remote_directory_is_built_from_account_user_and_project(project):
    assert Config.load(project).remote_path("alice") == "/e/project1/proj/alice/repo"


def test_a_project_directory_with_other_characters_needs_its_own_remote_dir(
    tmp_path, settings_home
):
    root = tmp_path / "my repo; rm"
    (root / ".git").mkdir(parents=True)
    loaded = Config.load(
        root, env={"XDG_CONFIG_HOME": str(settings_home), "JUP_ACCOUNT": "p"}
    )

    with pytest.raises(ConfigError, match="set remote_dir"):
        loaded.remote_path("alice")


def test_a_missing_account_says_how_to_set_it(tmp_path, settings_home):
    with pytest.raises(ConfigError, match="jup config set account"):
        Config.load(tmp_path).remote_path("alice")


def test_setting_a_value_keeps_the_others(tmp_path):
    path = tmp_path / "jupiter.json"

    config.write(path, "account", "proj")
    config.write(path, "sync_exclude", "reports/, *.ipynb")
    config.write(path, "ask_above_node_hours", "2.5")

    assert json.loads(path.read_text()) == {
        "account": "proj",
        "sync_exclude": ["reports/", "*.ipynb"],
        "ask_above_node_hours": 2.5,
    }


def test_a_persons_own_setting_is_not_written_to_a_repositorys_file(tmp_path):
    with pytest.raises(ConfigError, match="without --project"):
        config.write(tmp_path / ".jupiter.json", "host", "alias")


def test_a_project_file_above_the_repository_is_not_this_projects(
    tmp_path, settings_home
):
    (tmp_path / ".jupiter.json").write_text('{"account": "someone-elses"}')
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)

    loaded = Config.load(repository)

    assert loaded.account is None
    assert loaded.root == repository


@pytest.mark.parametrize(
    ("text", "value"), [("false", False), ("Off", False), ("yes", True)]
)
def test_the_log_switch_reads_the_usual_words(text, value):
    assert config.parse("log", text) is value


def test_a_switch_is_not_set_to_something_else():
    with pytest.raises(ConfigError, match="true or false"):
        config.parse("log", "maybe")


@pytest.mark.parametrize("key", ["log", "feedback_repo"])
def test_a_repository_cannot_say_where_reports_go_or_whether_runs_are_logged(
    project, key
):
    value = False if key == "log" else "someone/elses"
    (project / ".jupiter.json").write_text(json.dumps({key: value}))

    with pytest.raises(ConfigError, match="each person's own"):
        Config.load(project)


def test_reports_go_to_a_repository_named_as_owner_and_name():
    assert config.parse("feedback_repo", "team/tools") == "team/tools"
    with pytest.raises(ConfigError, match="owner/name"):
        config.parse("feedback_repo", "https://evil.example/x")
