from datetime import UTC, datetime

import pytest

from dockerhub_cleanup.domain import Candidate, Tag
from dockerhub_cleanup.errors import CleanupError
from dockerhub_cleanup.service import CleanupPlan, CleanupService

OLD = datetime(2025, 1, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


class FakeHub:
    def __init__(self) -> None:
        self.repository_names = ["one", "two"]
        self.tags_by_repository = {
            "one": [Tag("one", "old", DIGEST_A, OLD, OLD)],
            "two": [Tag("two", "latest", DIGEST_B, OLD, OLD)],
        }
        self.repository_calls: list[str] = []
        self.tag_calls: list[tuple[str, str]] = []
        self.delete_calls: list[tuple[str, str, str]] = []
        self.delete_errors: dict[str, CleanupError] = {}

    def repositories(self, namespace: str) -> list[str]:
        self.repository_calls.append(namespace)
        return self.repository_names

    def tags(self, namespace: str, repository: str) -> list[Tag]:
        self.tag_calls.append((namespace, repository))
        return self.tags_by_repository[repository]

    def delete_tag(self, namespace: str, repository: str, tag: str) -> None:
        self.delete_calls.append((namespace, repository, tag))
        if error := self.delete_errors.get(tag):
            raise error


class FakeDiscovery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def all_digests(self, namespace: str, repository: str) -> set[str]:
        self.calls.append((namespace, repository))
        return {DIGEST_A, DIGEST_B}


class FakeManifestDeletion:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.errors: dict[str, CleanupError] = {}

    def delete_digest(self, namespace: str, repository: str, digest: str) -> None:
        self.calls.append((namespace, repository, digest))
        if error := self.errors.get(digest):
            raise error


def test_plan_uses_explicit_repositories_without_listing_namespace() -> None:
    hub = FakeHub()
    plan = CleanupService(hub).plan("user", repositories=["one"], cutoff=CUTOFF)

    assert hub.repository_calls == []
    assert hub.tag_calls == [("user", "one")]
    assert [candidate.reference for candidate in plan.candidates] == ["old"]


def test_plan_lists_namespace_and_applies_protection() -> None:
    hub = FakeHub()
    plan = CleanupService(hub).plan("user", cutoff=CUTOFF, keep_patterns=["latest"])

    assert hub.repository_calls == ["user"]
    assert hub.tag_calls == [("user", "one"), ("user", "two")]
    assert [candidate.reference for candidate in plan.candidates] == ["old"]


def test_plan_combines_stale_and_untagged_without_duplicate_tag_fetches() -> None:
    hub = FakeHub()
    discovery = FakeDiscovery()
    plan = CleanupService(hub, discovery).plan(
        "user",
        repositories=["one"],
        cutoff=CUTOFF,
        include_untagged=True,
    )

    assert hub.tag_calls == [("user", "one")]
    assert discovery.calls == [("user", "one")]
    assert [(candidate.kind, candidate.reference) for candidate in plan.candidates] == [
        ("stale-tag", "old"),
        ("untagged", DIGEST_B),
    ]


def test_plan_supports_untagged_only_policy() -> None:
    hub = FakeHub()
    plan = CleanupService(hub, FakeDiscovery()).plan(
        "user", repositories=["one"], include_untagged=True
    )
    assert [(candidate.kind, candidate.reference) for candidate in plan.candidates] == [
        ("untagged", DIGEST_B)
    ]


def test_plan_forwards_never_pulled_policy() -> None:
    hub = FakeHub()
    hub.tags_by_repository["one"] = [Tag("one", "never", DIGEST_A, None, OLD)]
    plan = CleanupService(hub).plan(
        "user",
        repositories=["one"],
        cutoff=CUTOFF,
        include_never_pulled=True,
    )
    assert plan.candidates[0].reference == "never"


def test_plan_requires_a_policy_and_discovery_adapter() -> None:
    service = CleanupService(FakeHub())
    with pytest.raises(CleanupError, match="cleanup policy"):
        service.plan("user")
    with pytest.raises(CleanupError, match="Image Management"):
        service.plan("user", include_untagged=True)


def test_apply_dispatches_each_candidate() -> None:
    hub = FakeHub()
    manifests = FakeManifestDeletion()
    plan = CleanupPlan(
        "user",
        (
            Candidate("stale-tag", "one", "old", "reason"),
            Candidate("untagged", "one", DIGEST_B, "reason"),
        ),
    )

    result = CleanupService(hub).apply(plan, manifests)

    assert result.deleted == plan.candidates
    assert result.failures == ()
    assert hub.delete_calls == [("user", "one", "old")]
    assert manifests.calls == [("user", "one", DIGEST_B)]


def test_apply_requires_manifest_deletion_before_any_mutation() -> None:
    hub = FakeHub()
    plan = CleanupPlan(
        "user",
        (
            Candidate("stale-tag", "one", "old", "reason"),
            Candidate("untagged", "one", DIGEST_B, "reason"),
        ),
    )

    with pytest.raises(CleanupError, match="manifest deletion"):
        CleanupService(hub).apply(plan)
    assert hub.delete_calls == []


def test_apply_continues_after_tag_and_manifest_failures() -> None:
    hub = FakeHub()
    hub.delete_errors["bad"] = CleanupError("tag rejected")
    manifests = FakeManifestDeletion()
    manifests.errors[DIGEST_A] = CleanupError("manifest referenced")
    plan = CleanupPlan(
        "user",
        (
            Candidate("stale-tag", "one", "bad", "reason"),
            Candidate("stale-tag", "one", "good", "reason"),
            Candidate("untagged", "one", DIGEST_A, "reason"),
            Candidate("untagged", "one", DIGEST_B, "reason"),
        ),
    )

    result = CleanupService(hub).apply(plan, manifests)

    assert [candidate.reference for candidate in result.deleted] == ["good", DIGEST_B]
    assert [failure.message for failure in result.failures] == [
        "tag rejected",
        "manifest referenced",
    ]
    assert len(hub.delete_calls) == 2
    assert len(manifests.calls) == 2


def test_apply_accepts_an_empty_plan_without_manifest_client() -> None:
    assert CleanupService(FakeHub()).apply(CleanupPlan("user", ())).deleted == ()
