from datetime import UTC, datetime
from threading import Barrier

import pytest

from dockerhub_cleanup.domain import Candidate, Tag
from dockerhub_cleanup.errors import CleanupError, ReferencedManifestError
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


def test_plan_reuses_one_shot_protection_patterns_across_repositories() -> None:
    hub = FakeHub()
    plan = CleanupService(hub).plan(
        "user",
        cutoff=CUTOFF,
        keep_patterns=(pattern for pattern in ["*"]),
    )

    assert plan.candidates == ()


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
        ("untagged", DIGEST_A),
        ("untagged", DIGEST_B),
    ]


def test_plan_keeps_digest_referenced_by_a_retained_tag() -> None:
    hub = FakeHub()
    hub.tags_by_repository["one"].append(Tag("one", "current", DIGEST_A, CUTOFF, CUTOFF))

    plan = CleanupService(hub, FakeDiscovery()).plan(
        "user",
        repositories=["one"],
        cutoff=CUTOFF,
        include_untagged=True,
    )

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


def test_apply_reports_final_results_through_progress_callbacks() -> None:
    hub = FakeHub()
    hub.delete_errors["bad"] = CleanupError("tag rejected")
    manifests = FakeManifestDeletion()
    plan = CleanupPlan(
        "user",
        (
            Candidate("stale-tag", "one", "bad", "reason"),
            Candidate("untagged", "one", DIGEST_B, "reason"),
        ),
    )
    events: list[tuple[str, str]] = []

    CleanupService(hub).apply(
        plan,
        manifests,
        on_deleted=lambda candidate: events.append(("deleted", candidate.reference)),
        on_failure=lambda failure: events.append(("failed", failure.candidate.reference)),
    )

    assert events == [("failed", "bad"), ("deleted", DIGEST_B)]


def test_apply_keeps_progress_callbacks_optional_for_all_failure_kinds() -> None:
    hub = FakeHub()
    hub.delete_errors["bad"] = CleanupError("tag rejected")
    manifests = FakeManifestDeletion()
    manifests.errors[DIGEST_A] = CleanupError("manifest rejected")
    manifests.errors[DIGEST_B] = ReferencedManifestError("manifest referenced")
    plan = CleanupPlan(
        "user",
        (
            Candidate("stale-tag", "one", "bad", "reason"),
            Candidate("untagged", "one", DIGEST_A, "reason"),
            Candidate("untagged", "one", DIGEST_B, "reason"),
        ),
    )

    result = CleanupService(hub).apply(plan, manifests)

    assert [failure.message for failure in result.failures] == [
        "tag rejected",
        "manifest rejected",
        "manifest referenced",
    ]


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


def test_apply_requires_positive_manifest_workers_before_mutation() -> None:
    hub = FakeHub()
    plan = CleanupPlan("user", (Candidate("stale-tag", "one", "old", "reason"),))

    with pytest.raises(CleanupError, match="workers must be positive"):
        CleanupService(hub).apply(plan, manifest_workers=0)

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

    reported_failures: list[str] = []
    result = CleanupService(hub).apply(
        plan,
        manifests,
        on_failure=lambda failure: reported_failures.append(failure.message),
    )

    assert [candidate.reference for candidate in result.deleted] == ["good", DIGEST_B]
    assert [failure.message for failure in result.failures] == [
        "tag rejected",
        "manifest referenced",
    ]
    assert len(hub.delete_calls) == 2
    assert len(manifests.calls) == 2
    assert reported_failures == ["tag rejected", "manifest referenced"]


def test_apply_retries_referenced_manifests_after_dependency_progress() -> None:
    class DependencyManifestDeletion(FakeManifestDeletion):
        def delete_digest(self, namespace: str, repository: str, digest: str) -> None:
            if digest == DIGEST_B:
                self.errors.pop(DIGEST_A)
            super().delete_digest(namespace, repository, digest)

    hub = FakeHub()
    manifests = DependencyManifestDeletion()
    manifests.errors[DIGEST_A] = ReferencedManifestError("manifest referenced")
    plan = CleanupPlan(
        "user",
        (
            Candidate("untagged", "one", DIGEST_A, "reason"),
            Candidate("untagged", "one", DIGEST_B, "reason"),
        ),
    )

    result = CleanupService(hub).apply(plan, manifests)

    assert [candidate.reference for candidate in result.deleted] == [DIGEST_B, DIGEST_A]
    assert result.failures == ()
    assert [call[2] for call in manifests.calls] == [DIGEST_A, DIGEST_B, DIGEST_A]


def test_apply_can_delete_independent_manifests_concurrently() -> None:
    class ConcurrentManifestDeletion(FakeManifestDeletion):
        def __init__(self) -> None:
            super().__init__()
            self.barrier = Barrier(2)

        def delete_digest(self, namespace: str, repository: str, digest: str) -> None:
            self.barrier.wait(timeout=10)
            super().delete_digest(namespace, repository, digest)

    manifests = ConcurrentManifestDeletion()
    plan = CleanupPlan(
        "user",
        (
            Candidate("untagged", "one", DIGEST_A, "reason"),
            Candidate("untagged", "one", DIGEST_B, "reason"),
        ),
    )

    result = CleanupService(FakeHub()).apply(plan, manifests, manifest_workers=2)

    assert {candidate.reference for candidate in result.deleted} == {DIGEST_A, DIGEST_B}
    assert result.failures == ()


def test_apply_reports_referenced_manifests_when_no_dependency_progress() -> None:
    hub = FakeHub()
    manifests = FakeManifestDeletion()
    manifests.errors[DIGEST_A] = ReferencedManifestError("still referenced")
    plan = CleanupPlan(
        "user",
        (Candidate("untagged", "one", DIGEST_A, "reason"),),
    )

    reported_failures: list[str] = []
    result = CleanupService(hub).apply(
        plan,
        manifests,
        on_failure=lambda failure: reported_failures.append(failure.message),
    )

    assert result.deleted == ()
    assert [failure.message for failure in result.failures] == ["still referenced"]
    assert reported_failures == ["still referenced"]
    assert len(manifests.calls) == 1


def test_apply_accepts_an_empty_plan_without_manifest_client() -> None:
    assert CleanupService(FakeHub()).apply(CleanupPlan("user", ())).deleted == ()


def test_apply_treats_unexpected_manifest_errors_as_safe_failures() -> None:
    class UnexpectedManifestDeletion(FakeManifestDeletion):
        def delete_digest(self, namespace: str, repository: str, digest: str) -> None:
            raise RuntimeError("boom")

    hub = FakeHub()
    plan = CleanupPlan(
        "user",
        (Candidate("untagged", "one", DIGEST_A, "reason"),),
    )

    result = CleanupService(hub).apply(plan, UnexpectedManifestDeletion())

    assert result.deleted == ()
    assert len(result.failures) == 1
    assert "RuntimeError" in result.failures[0].message
