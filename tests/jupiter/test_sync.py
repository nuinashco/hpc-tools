"""A push copies what git would track, without secrets; a pull stays in the project."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jup import sync
from jup.errors import UserError
from jup.ssh import Remote


def git(root, *arguments):
    subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True
    )


@pytest.fixture
def repository(tmp_path):
    """Return a repository with tracked, new, ignored, deleted and secret files."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("ignored.bin\n")
    for name in ("kept.py", "gone.py", ".env.example"):
        (root / name).write_text("")
    git(root, "add", ".")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    (root / "gone.py").unlink()
    for name in ("new.py", "ignored.bin", ".env", "deploy.pem", "#odd name.txt"):
        (root / name).write_text("")
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("")
    nested = root / "vendor" / "lib"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    (nested / "x.py").write_text("")
    return root


def test_the_list_holds_tracked_and_new_files_and_says_what_it_leaves_out(repository):
    listing = sync.list_files(repository, ["notes/"])

    assert sorted(listing.files) == [
        "#odd name.txt",
        ".env.example",
        ".gitignore",
        "kept.py",
        "new.py",
    ]
    assert sorted(listing.secrets) == [".env", "deploy.pem"]
    assert listing.nested == ["vendor/lib"]
    assert any("secrets" in note for note in listing.notes())
    assert any("repository of its own" in note for note in listing.notes())


@pytest.mark.parametrize(
    ("path", "pattern", "excluded"),
    [
        ("reports/a.md", "reports/", True),
        ("reports2/a.md", "reports/", False),
        ("a/b.ipynb", "*.ipynb", True),
        ("src/a.py", "*.ipynb", False),
    ],
)
def test_an_exclusion_is_a_directory_or_a_glob(path, pattern, excluded):
    assert sync.is_excluded(path, [pattern]) is excluded


@pytest.mark.parametrize(
    ("path", "secret"),
    [
        (".env", True),
        ("conf/.env.production", True),
        ("keys/id_ed25519", True),
        ("tls/server.key", True),
        (".env.example", False),
        ("environment.py", False),
        ("docs/keys.md", False),
    ],
)
def test_a_file_is_taken_for_a_secret_by_its_name(path, secret):
    assert sync.looks_secret(path) is secret


def test_a_push_sends_the_list_to_rsync_and_stamps_the_copy_with_the_commit(
    repository, run
):
    listing = sync.push(
        Remote("cluster", run=run), repository, "/e/p/repo", [], dry_run=False
    )

    assert listing is not None and len(listing.files) == 6
    [copy] = run.ran("rsync")
    assert " -rlptD " in copy and "--files-from=-" in copy
    assert copy.endswith("cluster:/e/p/repo/")
    sent = run.inputs[run.calls.index(next(c for c in run.calls if c[0] == "rsync"))]
    assert (
        "./kept.py\n" in sent and "./#odd name.txt\n" in sent and ".env\n" not in sent
    )
    [stamped] = run.ran(".synced_from")
    assert "commit: " in stamped and "uncommitted files: " in stamped


def test_a_dry_run_changes_nothing_on_the_cluster(repository, run):
    sync.push(Remote("cluster", run=run), repository, "/e/p/repo", [], dry_run=True)

    assert run.ran("mkdir") == [] and run.ran(".synced_from") == []
    assert "--dry-run" in run.ran("rsync")[0]


def test_differing_files_are_what_a_dry_run_would_copy_without_directories(
    repository, run
):
    run.reply(
        "rsync",
        out=".d..t......|notes/\ncd+++++++++|new/\n<f.st......|kept.py\n"
        ">f+++++++|./notes/a.md\n.f...p.....|run.sh\ncL+++++++++|link\n"
        "sent 10 bytes\n",
    )

    differing = sync.find_differing(
        Remote("cluster", run=run), repository, "/e/p/repo", []
    )

    assert differing == ["kept.py", "notes/a.md", "run.sh", "link"]
    [compare] = run.ran("rsync")
    assert "--dry-run" in compare and "--files-from=-" in compare
    assert run.ran("mkdir") == [] and run.ran(".synced_from") == []


def test_the_home_directory_is_compared_with_nothing(run, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert sync.find_differing(Remote("cluster", run=run), tmp_path, "/e/p/x", []) == []
    assert run.ran("rsync") == []


def test_a_directory_that_is_no_repository_is_not_copied_unasked(tmp_path, run):
    with pytest.raises(UserError, match="--all"):
        sync.push(Remote("cluster", run=run), tmp_path, "/e/p/x", [], dry_run=False)

    assert run.ran("rsync") == []


def test_copied_whole_a_directory_leaves_caches_and_secrets_home(tmp_path, run):
    sync.push(
        Remote("cluster", run=run),
        tmp_path,
        "/e/p/x",
        ["data/"],
        dry_run=False,
        everything=True,
    )

    [copy] = run.ran("rsync")
    assert all(
        f"--exclude={name}" in copy for name in (".venv", ".env", "*.pem", "data/")
    )


def test_the_home_directory_is_never_a_project(run, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(UserError, match="inside a project"):
        sync.push(
            Remote("cluster", run=run),
            tmp_path,
            "/e/p/x",
            [],
            dry_run=False,
            everything=True,
        )


def test_a_list_git_cannot_make_stops_the_push(repository, run, monkeypatch):
    failed = subprocess.CompletedProcess([], 128, "", "fatal: index file corrupt")
    real = sync._git
    monkeypatch.setattr(
        sync, "_git", lambda root, *a: failed if a[0] == "ls-files" else real(root, *a)
    )

    with pytest.raises(UserError, match="index file corrupt"):
        sync.push(
            Remote("cluster", run=run), repository, "/e/p/repo", [], dry_run=False
        )

    assert run.ran("rsync") == []


@pytest.mark.parametrize(
    "pattern", ["results/run_42", "runs/exp_*", "out/[ab]?.csv", "dir/"]
)
def test_a_path_or_pattern_inside_the_project_can_be_pulled(pattern):
    assert sync.check_pattern(pattern) == pattern.rstrip("/")


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        ".",
        "/etc/passwd",
        "../other",
        "a/../../b",
        "-e sh",
        "x; touch y",
        "a b",
        "$(id)",
    ],
)
def test_what_the_remote_shell_would_run_or_what_leaves_the_project_is_refused(pattern):
    with pytest.raises(UserError, match="cannot be pulled"):
        sync.check_pattern(pattern)


def test_a_pull_copies_into_the_destination_and_can_only_list(tmp_path, run):
    sync.pull(
        Remote("cluster", run=run), "/e/p/x", "runs/a/", tmp_path / "runs", dry_run=True
    )

    [copy] = run.ran("rsync")
    assert "--dry-run" in copy
    assert copy.endswith(f"cluster:/e/p/x/runs/a {tmp_path / 'runs'}/")
