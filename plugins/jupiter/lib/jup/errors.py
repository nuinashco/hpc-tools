"""Errors a user can fix: what is wrong, then what to do, each with an exit code."""

from __future__ import annotations


class UserError(Exception):
    """A failure the user can fix; the message reads `what is wrong; the remedy`.

    The codes start at 64, above what remote commands commonly exit with, since
    `jup ssh` hands a remote command's own code through.
    """

    exit_code = 64


class ConfigError(UserError):
    """A setting is missing or wrong."""

    exit_code = 65


class NoConnectionError(UserError):
    """No shared SSH connection is open, and opening one needs the user's TOTP code."""

    exit_code = 66


class KeyRefusedError(UserError):
    """The login node refused the SSH key."""

    exit_code = 67


class RemoteError(UserError):
    """A command on the cluster failed."""

    exit_code = 68
