"""Commands on the cluster, over the SSH connection the user opened with a TOTP code."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from jup.errors import (
    ConfigError,
    KeyRefusedError,
    NoConnectionError,
    RemoteError,
    UserError,
)

Run = Callable[..., "subprocess.CompletedProcess[str]"]
# No prompt can be answered from here, so a connection that needs one fails at once.
BATCH = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=20")
# What ssh itself exits with; a remote command may exit with it too.
_SSH_FAILED = 255
# How ssh's own messages start or what they hold (ssh(1), OpenSSH 9).
_SSH_MESSAGES = (
    "Permission denied",
    "Connection ",
    "connect to host",
    "Could not resolve",
    "Host key verification",
    "ssh:",
    "kex_",
    "mux_client",
    "Control socket",
    "timed out",
)
# Seconds a command whose output is read may take: the queue and accounting commands
# answer within a few, so longer means a connection that hangs.
ANSWER_SECONDS = 120
# Put in front of each line a command prints, to tell it from what a login profile
# prints before it.
MARK = "JUP|"


@dataclass(frozen=True)
class Remote:
    """The cluster's login node behind an alias of the user's ssh config."""

    host: str
    run: Run = subprocess.run

    def _local(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Ask the local ssh about the alias, without touching the network."""
        command = ["ssh", *arguments, "--", self.host]
        return self.run(command, capture_output=True, text=True, errors="replace")

    def settings(self) -> dict[str, str]:
        """Return what the ssh config resolves the alias to."""
        done = self._local("-G")
        if done.returncode:
            detail = (done.stderr or "").strip() or "no message"
            raise ConfigError(
                f"ssh cannot read its configuration for {self.host} ({detail}); fix "
                "~/.ssh/config"
            )
        pairs = (line.split(None, 1) for line in done.stdout.splitlines())
        return {pair[0]: pair[1] for pair in pairs if len(pair) == 2}

    def user(self) -> str:
        """Return the login name ssh uses: the alias's, or the local one without it."""
        return self.settings().get("user", "")

    def has_entry(self) -> bool:
        """Report whether the ssh config knows the alias.

        Without an entry ssh takes the alias for the host's own name.
        """
        return "." in self.host or self.settings().get("hostname") != self.host

    def is_open(self) -> bool:
        """Report whether a shared connection is open; asks only the local ssh."""
        return self._local("-O", "check").returncode == 0

    def need_connection(self) -> None:
        """Raise when no shared connection is open, saying how the user opens one."""
        if not self.is_open():
            raise NoConnectionError(
                f"no SSH connection to {self.host} is open, and opening one needs a "
                f"TOTP code; the user runs `ssh {self.host} true` in a terminal (in "
                f"Claude Code: `! ssh {self.host} true`), then repeat the command"
            )

    def command(
        self, script: str, *, cwd: str | None = None, terminal: bool = False
    ) -> list[str]:
        """Return the ssh command that runs `script` in a login shell there.

        With `terminal` the script gets one, and so ends when this side does:
        without it, what a stopped command left running there goes on.
        """
        if cwd:
            script = f"cd {shlex.quote(cwd)} && {script}"
        options = [*BATCH, "-tt"] if terminal else BATCH
        # A login shell, since `module` and `jutil` are set up by the profile.
        return ["ssh", *options, "--", self.host, f"bash -lc {shlex.quote(script)}"]

    def execute(
        self,
        script: str,
        *,
        cwd: str | None = None,
        capture: bool = True,
        terminal: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run `script` on the login node; return it finished, whatever its code."""
        self.need_connection()
        try:
            done = self.run(
                self.command(script, cwd=cwd, terminal=terminal),
                capture_output=capture,
                text=True,
                errors="replace",
                timeout=ANSWER_SECONDS if capture else None,
            )
        except subprocess.TimeoutExpired:
            raise RemoteError(
                f"{self.host} gave no answer within {ANSWER_SECONDS} s; the shared "
                f"connection may hang: close it with `ssh -O exit {self.host}` and "
                "have the user log in again"
            ) from None
        stderr = done.stderr or ""
        if done.returncode == _SSH_FAILED and (
            not capture or any(message in stderr for message in _SSH_MESSAGES)
        ):
            raise classify(self.host, stderr)
        return done

    def read(self, script: str, *, cwd: str | None = None) -> str:
        """Return what `script` prints; raise when it fails."""
        done = self.execute(script, cwd=cwd)
        if done.returncode:
            detail = (done.stderr or done.stdout).strip()
            raise RemoteError(
                f"`{script}` failed on {self.host} with code {done.returncode}: "
                f"{detail or 'no output'}; fix the command or what it needs"
            )
        return done.stdout

    def read_lines(self, script: str, *, cwd: str | None = None) -> list[str]:
        """Return the lines `script` prints, without what the login profile printed."""
        marked = f"set -o pipefail; {{ {script}; }} | sed 's/^/{MARK}/'"
        lines = self.read(marked, cwd=cwd).splitlines()
        return [line[len(MARK) :] for line in lines if line.startswith(MARK)]

    def probe(self) -> UserError | None:
        """Try one new connection without prompts; return why it fails, if it does.

        This is a login attempt of its own, so nothing calls it unasked.
        """
        done = self.run(
            ["ssh", *BATCH, "-o", "ControlPath=none", "--", self.host, "true"],
            capture_output=True,
            text=True,
            errors="replace",
        )
        return classify(self.host, done.stderr or "") if done.returncode else None

    def copy(
        self,
        arguments: Sequence[str],
        *,
        files: str | None = None,
        capture: bool = False,
    ) -> str:
        """Run rsync over the shared connection; return its output when captured.

        Captured, it has `ANSWER_SECONDS`: that is for a comparison, not for a copy.
        """
        self.need_connection()
        shell = " ".join(["ssh", *BATCH])
        try:
            done = self.run(
                ["rsync", "-e", shell, *arguments],
                input=files,
                text=True,
                errors="replace",
                stdout=subprocess.PIPE if capture else None,
                timeout=ANSWER_SECONDS if capture else None,
            )
        except subprocess.TimeoutExpired:
            raise RemoteError(
                f"rsync to {self.host} gave no answer within {ANSWER_SECONDS} s; the "
                f"shared connection may hang: close it with `ssh -O exit {self.host}` "
                "and have the user log in again"
            ) from None
        if done.returncode:
            raise RemoteError(
                f"rsync ended with code {done.returncode}; its messages above say "
                "which files or directories it could not copy"
            )
        return done.stdout or ""


def classify(host: str, stderr: str) -> UserError:
    """Return the error that ssh's own message stands for."""
    if "(publickey" in stderr:
        return KeyRefusedError(
            f"{host} refused the SSH key; either this network is outside the key's "
            "from= clause in JuDoor (switch back, or add the address there and wait "
            "15 minutes), or the key is not loaded: the user runs `ssh-add` for it"
        )
    if "keyboard-interactive" in stderr or "Permission denied" in stderr:
        return NoConnectionError(
            f"{host} asks for a TOTP code; the user runs `ssh {host} true` in a "
            f"terminal (in Claude Code: `! ssh {host} true`), then repeat the command"
        )
    detail = stderr.strip().splitlines()[-1] if stderr.strip() else "no message"
    return RemoteError(
        f"ssh to {host} failed ({detail}); check the network and the host alias in "
        "~/.ssh/config"
    )
