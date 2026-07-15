"""Cleanup planning and execution orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from dockerhub_cleanup.domain import (
    Candidate,
    Tag,
    select_stale_tags,
    select_untagged_digests,
)
from dockerhub_cleanup.errors import CleanupError, ReferencedManifestError


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
            stale_tags: list[Candidate] = []
            if cutoff is not None:
                stale_tags = select_stale_tags(
                    tags,
                    cutoff,
                    protected_patterns,
                    include_never_pulled=include_never_pulled,
                )
                candidates.extend(stale_tags)
            if include_untagged:
                assert self._discovery is not None
                stale_tag_names = {candidate.reference for candidate in stale_tags}
                candidates.extend(
                    select_untagged_digests(
                        repository,
                        self._discovery.all_digests(namespace, repository),
                        (tag.digest for tag in tags if tag.name not in stale_tag_names),
                    )
                )
        return CleanupPlan(namespace, tuple(candidates))

    def apply(
        self,
        plan: CleanupPlan,
        manifest_deletion: ManifestDeletion | None = None,
        *,
        on_deleted: Callable[[Candidate], None] | None = None,
        on_failure: Callable[[DeletionFailure], None] | None = None,
        manifest_workers: int = 1,
    ) -> ApplyResult:
        """Attempt every planned deletion and aggregate safe failures."""

        if manifest_workers < 1:
            raise CleanupError("manifest deletion workers must be positive")
        if (
            any(candidate.kind == "untagged" for candidate in plan.candidates)
            and manifest_deletion is None
        ):
            raise CleanupError("untagged candidates require a manifest deletion client")

        deleted: list[Candidate] = []
        failures: list[DeletionFailure] = []
        pending_manifests: list[Candidate] = []
        for candidate in plan.candidates:
            if candidate.kind == "untagged":
                pending_manifests.append(candidate)
                continue
            try:
                self._hub.delete_tag(
                    plan.namespace,
                    candidate.repository,
                    candidate.reference,
                )
            except CleanupError as exc:
                failure = DeletionFailure(candidate, str(exc))
                failures.append(failure)
                if on_failure is not None:
                    on_failure(failure)
            else:
                deleted.append(candidate)
                if on_deleted is not None:
                    on_deleted(candidate)

        while pending_manifests:
            deferred: list[DeletionFailure] = []
            progress = False
            assert manifest_deletion is not None
            with ThreadPoolExecutor(max_workers=manifest_workers) as executor:
                attempts = {
                    executor.submit(
                        _delete_manifest,
                        manifest_deletion,
                        plan.namespace,
                        candidate,
                    ): candidate
                    for candidate in pending_manifests
                }
                for attempt in as_completed(attempts):
                    candidate = attempts[attempt]
                    error = attempt.result()
                    if isinstance(error, ReferencedManifestError):
                        deferred.append(DeletionFailure(candidate, str(error)))
                    elif error is not None:
                        failure = DeletionFailure(candidate, str(error))
                        failures.append(failure)
                        if on_failure is not None:
                            on_failure(failure)
                    else:
                        deleted.append(candidate)
                        progress = True
                        if on_deleted is not None:
                            on_deleted(candidate)
            if not deferred:
                break
            if not progress:
                failures.extend(deferred)
                if on_failure is not None:
                    for failure in deferred:
                        on_failure(failure)
                break
            pending_manifests = [failure.candidate for failure in deferred]
        return ApplyResult(tuple(deleted), tuple(failures))


def _delete_manifest(
    deletion: ManifestDeletion,
    namespace: str,
    candidate: Candidate,
) -> CleanupError | None:
    try:
        deletion.delete_digest(namespace, candidate.repository, candidate.reference)
    except CleanupError as exc:
        return exc
    return None
