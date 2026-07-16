"""Isolated crane authentication and manifest deletion adapter."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, Self

from dockerhub_cleanup.domain import SHA256_RE
from dockerhub_cleanup.errors import CleanupError, ReferencedManifestError

CRANE_TIMEOUT_SECONDS = 120.0
DOCKER_HUB_SECRET_ENV = frozenset({"DH_COOKIE", "DH_PAT"})
INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    }
)
LEAF_MEDIA_TYPES = frozenset(
    {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    }
)


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

    def __init__(self, timeout: float = CRANE_TIMEOUT_SECONDS):
        self._timeout = timeout

    def run(
        self,
        args: Sequence[str],
        *,
        input_text: str | None,
        env: Mapping[str, str],
    ) -> CommandResult:
        if not args:
            raise CleanupError("command cannot be empty")
        try:
            result = subprocess.run(
                list(args),
                input=input_text,
                text=True,
                capture_output=True,
                env=dict(env),
                check=False,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CleanupError(f"{args[0]} timed out") from exc
        except OSError as exc:
            raise CleanupError(f"could not start {args[0]}: {exc}") from exc
        return CommandResult(result.returncode, result.stdout, result.stderr)


def resolve_crane_command(
    which: Callable[[str], str | None] | None = None,
) -> tuple[str, ...]:
    """Prefer mise-managed crane, then fall back to a PATH binary."""

    find_command = shutil.which if which is None else which
    if find_command("mise"):
        return ("mise", "exec", "--", "crane")
    if find_command("crane"):
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
        self._runner = SubprocessRunner() if runner is None else runner
        self._command = tuple(command) if command is not None else resolve_crane_command()
        self._temporary = tempfile.TemporaryDirectory(prefix="dockerhub-cleanup-")
        self._env = {
            key: value for key, value in os.environ.items() if key not in DOCKER_HUB_SECRET_ENV
        }
        self._env["DOCKER_CONFIG"] = self._temporary.name
        try:
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
        except Exception:
            self.close()
            raise
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
            message = f"crane delete {reference} failed"
            if "cannot be deleted as it is referenced by other images" in result.stderr:
                raise ReferencedManifestError(f"{message}: manifest is referenced by other images")
            raise CleanupError(message)

    def reachable_digests(
        self,
        namespace: str,
        repository: str,
        root_digests: Iterable[str],
    ) -> set[str]:
        """Return retained roots and every manifest transitively referenced by an index."""

        pending = list(root_digests)
        reachable = set(pending)
        inspected: set[str] = set()
        while pending:
            digest = pending.pop()
            if digest in inspected:
                continue
            inspected.add(digest)
            for child_digest, media_type in self._manifest_children(
                namespace,
                repository,
                digest,
            ):
                reachable.add(child_digest)
                if media_type in INDEX_MEDIA_TYPES:
                    pending.append(child_digest)
        return reachable

    def _manifest_children(
        self,
        namespace: str,
        repository: str,
        digest: str,
    ) -> list[tuple[str, str]]:
        reference = f"index.docker.io/{namespace}/{repository}@{digest}"
        result = self._runner.run(
            [*self._command, "manifest", reference],
            input_text=None,
            env=self._env,
        )
        if result.returncode:
            raise CleanupError(f"crane manifest {reference} failed")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CleanupError(f"crane manifest {reference} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise CleanupError(f"crane manifest {reference} returned an invalid object")
        if "manifests" not in payload:
            return []
        descriptors = payload["manifests"]
        if not isinstance(descriptors, list):
            raise CleanupError(f"crane manifest {reference} returned invalid descriptors")

        children: list[tuple[str, str]] = []
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                raise CleanupError(f"crane manifest {reference} returned invalid descriptors")
            child_digest = descriptor.get("digest")
            media_type = descriptor.get("mediaType")
            if not isinstance(child_digest, str) or not SHA256_RE.fullmatch(child_digest):
                raise CleanupError(f"crane manifest {reference} returned an invalid digest")
            if media_type not in INDEX_MEDIA_TYPES | LEAF_MEDIA_TYPES:
                raise CleanupError(f"crane manifest {reference} returned an unknown media type")
            children.append((child_digest, media_type))
        return children


def _redact(value: str, secret: str) -> str:
    return value.replace(secret, "<redacted>").strip() if secret else value.strip()
