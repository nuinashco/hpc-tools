"""Refuse a commit that changes a plugin's files without changing its version.

Claude Code gives users a new copy of a plugin only when the `version` in its
`plugin.json` changes. Run by pre-commit; a README alone needs no new version.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *arguments], capture_output=True, text=True)


def read_version(source: str, manifest: str) -> str | None:
    """Return the version in a manifest as git holds it at `source`; None without it."""
    shown = git("show", f"{source}:{manifest}")
    if shown.returncode:
        return None
    return json.loads(shown.stdout).get("version")


def find_stale(staged: list[str], plugins: list[str]) -> list[str]:
    """Return the plugins whose staged files change while their version stays."""
    stale = []
    for plugin in plugins:
        prefix = f"plugins/{plugin}/"
        manifest = f"{prefix}.claude-plugin/plugin.json"
        changed = [
            name
            for name in staged
            if name.startswith(prefix) and not name.endswith("README.md")
        ]
        before = read_version("HEAD", manifest)
        # A plugin that is new to the repository has no earlier version to differ from.
        if changed and before is not None and before == read_version("", manifest):
            stale.append(plugin)
    return stale


def main() -> int:
    staged = git("diff", "--cached", "--name-only").stdout.splitlines()
    plugins = sorted(path.name for path in Path("plugins").iterdir() if path.is_dir())
    stale = find_stale(staged, plugins)
    for plugin in stale:
        print(
            f"plugins/{plugin} changes but its version does not; raise `version` in "
            f"plugins/{plugin}/.claude-plugin/plugin.json so that users get the change"
        )
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
