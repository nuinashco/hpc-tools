"""Copy a project to the cluster and bring results back."""

from __future__ import annotations

import fnmatch
import re
import shlex
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jup.errors import UserError
from jup.ssh import Remote

# Written beside the copy: the cluster has no git, so nothing else says what ran.
STAMP = ".synced_from"
# Left out of a directory that is copied whole, where no git says what belongs to it.
_NEVER = (".git", ".venv", "__pycache__", "node_modules", ".DS_Store")
# File names that usually hold a secret. The project directory on the cluster is shared
# with the project's members, so these stay home even when git would add them.
_SECRETS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
)
_NOT_SECRETS = (".env.example", ".env.sample", ".env.template", ".env.dist")
# What a pattern to pull may hold: the remote shell expands it, so nothing that shell
# would run or split.
_PATTERN = re.compile(r"^[A-Za-z0-9._/*?\[\]@+=,-]+$")
# Modes and times, and no owner or group: a new file takes the project's group from the
# directory it lands in, as the cluster's project directories are set up to give.
_FLAGS = ("-rlptD",)
# What a dry run prints for each entry a copy would change: rsync's change flags, a
# bar, the name. The second flag is the entry's kind, `d` for a directory.
_ITEM_FORMAT = "%i|%n"
_ITEM = re.compile(r"^[<>ch.][fLDS][^|]*\|(?:\./)?(.+)$")


def is_excluded(path: str, patterns: Sequence[str]) -> bool:
    """Report whether `path` matches a pattern: a glob, or a directory ending in `/`."""
    for pattern in patterns:
        if pattern.endswith("/") and (path + "/").startswith(pattern):
            return True
        if fnmatch.fnmatch(path, pattern.rstrip("/")):
            return True
    return False


def looks_secret(path: str) -> bool:
    """Report whether a file's name is one that usually holds a secret."""
    name = path.rpartition("/")[2]
    if any(fnmatch.fnmatch(name, pattern) for pattern in _NOT_SECRETS):
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SECRETS)


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        errors="replace",
    )


def is_work_tree(root: Path) -> bool:
    """Report whether `root` is inside a git repository."""
    return _git(root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


@dataclass(frozen=True)
class Listing:
    """What a push of a git repository copies, and what it leaves out and why."""

    files: list[str]
    # Left home: their names say they hold secrets.
    secrets: list[str] = field(default_factory=list)
    # Submodules and repositories inside this one: git lists each as one entry.
    nested: list[str] = field(default_factory=list)
    uses_lfs: bool = False

    def notes(self) -> list[str]:
        """Return what the user should know about the copy, one line each."""
        notes: list[str] = []
        more = " ..." if len(self.secrets) > 8 else ""
        if self.secrets:
            notes.append(
                f"left out, since their names say they hold secrets: "
                f"{', '.join(self.secrets[:8])}{more}"
            )
        if self.nested:
            notes.append(
                f"not copied, each a repository of its own (push it from inside): "
                f"{', '.join(self.nested[:8])}"
            )
        if self.uses_lfs:
            notes.append(
                "this repository uses Git LFS: a file not fetched here arrives as a "
                "pointer, not as its content"
            )
        return notes


def list_files(root: Path, exclude: Sequence[str]) -> Listing:
    """Return the files of a git repository that git tracks or would add."""
    listing = _git(root, "ls-files", "-co", "--exclude-standard", "-z")
    if listing.returncode:
        detail = listing.stderr.strip().splitlines()[-1:] or ["no message"]
        raise UserError(
            f"git could not list the files of {root} ({detail[0]}); fix that first, "
            "since the list decides what is copied"
        )
    files: list[str] = []
    secrets: list[str] = []
    nested: list[str] = []
    for name in listing.stdout.split("\0"):
        path = root / name
        if not name or "\n" in name or is_excluded(name, exclude):
            continue
        if path.is_dir() and not path.is_symlink():
            nested.append(name.rstrip("/"))
        elif looks_secret(name):
            secrets.append(name)
        # A tracked file that was deleted is still listed.
        elif path.exists() or path.is_symlink():
            files.append(name)
    attributes = root / ".gitattributes"
    uses_lfs = attributes.is_file() and "filter=lfs" in attributes.read_text(
        errors="replace"
    )
    return Listing(files, secrets, nested, uses_lfs)


def describe_source(root: Path) -> str:
    """Return the lines of the stamp: commit, uncommitted files, time and place."""
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    changed = len(_git(root, "status", "--porcelain").stdout.splitlines())
    pushed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return (
        f"commit: {commit or 'none (not a git repository)'}\n"
        f"uncommitted files: {changed}\npushed: {pushed}\nfrom: {root.name}\n"
        "files deleted or renamed there since an earlier push are still here\n"
    )


def _is_project(root: Path) -> bool:
    return root not in (Path.home(), Path(root.anchor))


def _select(
    root: Path, exclude: Sequence[str], tracked: bool
) -> tuple[list[str], str | None, Listing | None]:
    """Return rsync's arguments that choose the files, its input, and the listing."""
    if not tracked:
        skipped = [f"--exclude={name}" for name in (*_NEVER, *_SECRETS, *exclude)]
        return skipped, None, None
    listing = list_files(root, exclude)
    # A name with `./` in front cannot be read as a comment or an option.
    names = "".join(f"./{name}\n" for name in listing.files)
    return ["--files-from=-"], names, listing


def push(
    remote: Remote,
    root: Path,
    target: str,
    exclude: Sequence[str],
    *,
    dry_run: bool,
    everything: bool = False,
) -> Listing | None:
    """Copy the project to `target` on the cluster; return a repository's listing.

    Files that differ there are replaced, and nothing there is ever removed. Outside a
    git repository nothing says which files are private, so that needs `everything`.
    """
    if not _is_project(root):
        raise UserError(
            f"the project would be {root} itself; run the command inside a project's "
            "directory"
        )
    tracked = is_work_tree(root)
    if not tracked and not everything:
        raise UserError(
            f"{root} is no git repository, so nothing says which of its files are "
            "private; run `git init` there, or pass --all to copy everything but "
            "caches and files whose names say they hold secrets"
        )
    selection, names, listing = _select(root, exclude, tracked)
    flags = [*_FLAGS, *(["--dry-run", "-v"] if dry_run else [])]
    if not dry_run:
        remote.read(f"mkdir -p {shlex.quote(target)}")
    destination = f"{remote.host}:{target}/"
    remote.copy([*flags, *selection, f"{root}/", destination], files=names)
    if not dry_run:
        stamp = describe_source(root)
        remote.read(f"cat > {shlex.quote(target + '/' + STAMP)} <<'EOT'\n{stamp}EOT")
    return listing


def find_differing(
    remote: Remote, root: Path, target: str, exclude: Sequence[str]
) -> list[str]:
    """Return the project's files that a push would copy: new here, or other there.

    Directories are left out: a push changes their times, never what a job reads.
    """
    if not _is_project(root):
        return []
    selection, names, _ = _select(root, exclude, is_work_tree(root))
    flags = [*_FLAGS, "--dry-run", f"--out-format={_ITEM_FORMAT}"]
    destination = f"{remote.host}:{target}/"
    printed = remote.copy(
        [*flags, *selection, f"{root}/", destination], files=names, capture=True
    )
    return [found[1] for line in printed.splitlines() if (found := _ITEM.match(line))]


def check_pattern(pattern: str) -> str:
    """Return a path or pattern to pull, without a trailing slash, or refuse it."""
    cleaned = pattern.rstrip("/")
    parts = cleaned.split("/")
    if (
        not cleaned
        or cleaned == "."
        or cleaned.startswith(("/", "-"))
        or ".." in parts
        or not _PATTERN.match(cleaned)
    ):
        raise UserError(
            f"{pattern!r} cannot be pulled; name a path inside the project, such as "
            "results/run_42 or 'runs/exp_*', of letters, digits and . _ - / * ? [ ]"
        )
    return cleaned


def pull(
    remote: Remote, target: str, pattern: str, destination: Path, *, dry_run: bool
) -> None:
    """Copy what `pattern` matches under `target` on the cluster into `destination`.

    Files that differ here are replaced.
    """
    cleaned = check_pattern(pattern)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise UserError(
            f"{destination} cannot be created ({error.strerror}); pass another --to"
        ) from error
    flags = [*_FLAGS, *(["--dry-run", "-v"] if dry_run else [])]
    # The remote rsync expands the pattern, so it travels unquoted.
    remote.copy([*flags, f"{remote.host}:{target}/{cleaned}", f"{destination}/"])
