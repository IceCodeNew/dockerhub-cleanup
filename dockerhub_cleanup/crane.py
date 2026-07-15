"""Isolated crane authentication and manifest deletion adapter."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, Self

from dockerhub_cleanup.errors import CleanupError


@dataclass(frozen=True)
class CommandResult:
    """Captured command outcome."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Subprocess boundary used by the crane adapter."""

    def run(
        self,
        args: Sequence[str],
        *,
        input_text: str | None,
        env: Mapping[str, str],
    ) -> CommandResult: ...


class SubprocessRunner:
    """Execute a command and capture its text streams."""

    def run(
        self,
        args: Sequence[str],
        *,
        input_text: str | None,
        env: Mapping[str, str],
    ) -> CommandResult:
        result = subprocess.run(
            list(args),
            input=input_text,
            text=True,
            capture_output=True,
            env=dict(env),
            check=False,
        )
        return CommandResult(result.returncode, result.stdout, result.stderr)


def resolve_crane_command(
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:
    """Prefer mise-managed crane, then fall back to a PATH binary."""

    if which("mise"):
        return ("mise", "exec", "--", "crane")
    if which("crane"):
        return ("crane",)
    raise CleanupError("crane is unavailable; install it directly or through mise")


class CraneClient:
    """Use crane with credentials isolated from the user's Docker config."""

    def __init__(
        self,
        username: str,
        pat: str,
        *,
        runner: CommandRunner | None = None,
        command: Sequence[str] | None = None,
    ):
        self._runner = runner or SubprocessRunner()
        self._command = tuple(command) if command is not None else resolve_crane_command()
        self._temporary = tempfile.TemporaryDirectory(prefix="dockerhub-cleanup-")
        self._env = {**os.environ, "DOCKER_CONFIG": self._temporary.name}
        self._pat = pat
        result = self._runner.run(
            [
                *self._command,
                "auth",
                "login",
                "index.docker.io",
                "-u",
                username,
                "--password-stdin",
            ],
            input_text=pat,
            env=self._env,
        )
        if result.returncode:
            self.close()
            raise CleanupError(f"crane login failed: {_redact(result.stderr, pat)}")

    @property
    def docker_config(self) -> str:
        """Return the isolated config path for diagnostics and tests."""

        return self._env["DOCKER_CONFIG"]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Remove temporary authentication state."""

        self._temporary.cleanup()

    def delete_digest(self, namespace: str, repository: str, digest: str) -> None:
        """Delete one known manifest digest from Docker Hub."""

        reference = f"index.docker.io/{namespace}/{repository}@{digest}"
        result = self._runner.run(
            [*self._command, "delete", reference],
            input_text=None,
            env=self._env,
        )
        if result.returncode:
            raise CleanupError(f"crane delete {reference} failed: {result.stderr.strip()}")


def _redact(value: str, secret: str) -> str:
    return value.replace(secret, "<redacted>").strip() if secret else value.strip()
