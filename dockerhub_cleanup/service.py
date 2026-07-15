"""Cleanup planning and execution orchestration."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from dockerhub_cleanup.domain import (
    Candidate,
    Tag,
    select_stale_tags,
    select_untagged_digests,
)
from dockerhub_cleanup.errors import CleanupError


class HubRepository(Protocol):
    """Docker Hub operations required by the cleanup service."""

    def repositories(self, namespace: str) -> list[str]: ...

    def tags(self, namespace: str, repository: str) -> list[Tag]: ...

    def delete_tag(self, namespace: str, repository: str, tag: str) -> None: ...


class DigestDiscovery(Protocol):
    """Full manifest inventory required for untagged discovery."""

    def all_digests(self, namespace: str, repository: str) -> set[str]: ...


class ManifestDeletion(Protocol):
    """Known-digest deletion boundary."""

    def delete_digest(self, namespace: str, repository: str, digest: str) -> None: ...


@dataclass(frozen=True)
class CleanupPlan:
    """Complete immutable candidate list produced before mutation begins."""

    namespace: str
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class DeletionFailure:
    """One failed candidate and its safe error message."""

    candidate: Candidate
    message: str


@dataclass(frozen=True)
class ApplyResult:
    """Aggregate outcome after attempting every candidate."""

    deleted: tuple[Candidate, ...]
    failures: tuple[DeletionFailure, ...]


class CleanupService:
    """Build complete cleanup plans and apply them with partial-failure isolation."""

    def __init__(self, hub: HubRepository, discovery: DigestDiscovery | None = None):
        self._hub = hub
        self._discovery = discovery

    def plan(
        self,
        namespace: str,
        *,
        repositories: Iterable[str] | None = None,
        cutoff: datetime | None = None,
        include_untagged: bool = False,
        include_never_pulled: bool = False,
        keep_patterns: Iterable[str] = (),
    ) -> CleanupPlan:
        """Collect all metadata and return a complete plan without mutations."""

        if cutoff is None and not include_untagged:
            raise CleanupError("select at least one cleanup policy")
        if include_untagged and self._discovery is None:
            raise CleanupError("untagged cleanup requires an Image Management client")

        repository_names = (
            list(repositories) if repositories is not None else self._hub.repositories(namespace)
        )
        protected_patterns = tuple(keep_patterns)
        candidates: list[Candidate] = []
        for repository in repository_names:
            tags = self._hub.tags(namespace, repository)
            if cutoff is not None:
                candidates.extend(
                    select_stale_tags(
                        tags,
                        cutoff,
                        protected_patterns,
                        include_never_pulled=include_never_pulled,
                    )
                )
            if include_untagged:
                assert self._discovery is not None
                candidates.extend(
                    select_untagged_digests(
                        repository,
                        self._discovery.all_digests(namespace, repository),
                        (tag.digest for tag in tags),
                    )
                )
        return CleanupPlan(namespace, tuple(candidates))

    def apply(
        self,
        plan: CleanupPlan,
        manifest_deletion: ManifestDeletion | None = None,
    ) -> ApplyResult:
        """Attempt every planned deletion and aggregate safe failures."""

        if (
            any(candidate.kind == "untagged" for candidate in plan.candidates)
            and manifest_deletion is None
        ):
            raise CleanupError("untagged candidates require a manifest deletion client")

        deleted: list[Candidate] = []
        failures: list[DeletionFailure] = []
        for candidate in plan.candidates:
            try:
                if candidate.kind == "stale-tag":
                    self._hub.delete_tag(
                        plan.namespace,
                        candidate.repository,
                        candidate.reference,
                    )
                else:
                    assert manifest_deletion is not None
                    manifest_deletion.delete_digest(
                        plan.namespace,
                        candidate.repository,
                        candidate.reference,
                    )
            except CleanupError as exc:
                failures.append(DeletionFailure(candidate, str(exc)))
            else:
                deleted.append(candidate)
        return ApplyResult(tuple(deleted), tuple(failures))
