from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from io import StringIO
from typing import Self
from unittest.mock import patch

import pytest

from dockerhub_cleanup.cli import CraneOperations, cutoff_argument, main
from dockerhub_cleanup.domain import Tag
from dockerhub_cleanup.errors import CleanupError
from dockerhub_cleanup.service import ApplyResult, CleanupService, DigestDiscovery, HubRepository

OLD = datetime(2025, 1, 1, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


class FakeHub:
    def __init__(self) -> None:
        self.delete_errors: dict[str, CleanupError] = {}
        self.deleted: list[str] = []
        self.tags_result = [Tag("app", "old", DIGEST_A, OLD, OLD)]

    def repositories(self, namespace: str) -> list[str]:
        return ["app"]

    def tags(self, namespace: str, repository: str) -> list[Tag]:
        return self.tags_result

    def delete_tag(self, namespace: str, repository: str, tag: str) -> None:
        self.deleted.append(tag)
        if error := self.delete_errors.get(tag):
            raise error


class FakeDiscovery:
    def all_digests(self, namespace: str, repository: str) -> set[str]:
        return {DIGEST_A, DIGEST_B}


class FakeCrane(AbstractContextManager[CraneOperations]):
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.delete_errors: dict[str, CleanupError] = {}
        self.reachable_children: set[str] = set()
        self.reachability_calls: list[tuple[str, str, set[str]]] = []
        self.entered = False

    def __enter__(self) -> Self:
        self.entered = True
        return self

    def __exit__(self, *_: object) -> None:
        self.entered = False

    def delete_digest(self, namespace: str, repository: str, digest: str) -> None:
        self.deleted.append(digest)
        if error := self.delete_errors.get(digest):
            raise error

    def reachable_digests(
        self,
        namespace: str,
        repository: str,
        root_digests: Iterable[str],
    ) -> set[str]:
        roots = set(root_digests)
        self.reachability_calls.append((namespace, repository, roots))
        return roots | self.reachable_children


class Factories:
    def __init__(self) -> None:
        self.hub = FakeHub()
        self.discovery = FakeDiscovery()
        self.crane = FakeCrane()
        self.hub_credentials: tuple[str, str] | None = None
        self.discovery_cookie: str | None = None
        self.crane_credentials: tuple[str, str] | None = None

    def hub_factory(self, username: str, pat: str) -> HubRepository:
        self.hub_credentials = (username, pat)
        return self.hub

    def discovery_factory(self, cookie: str) -> DigestDiscovery:
        self.discovery_cookie = cookie
        return self.discovery

    def crane_factory(self, username: str, pat: str) -> AbstractContextManager[CraneOperations]:
        self.crane_credentials = (username, pat)
        return self.crane


def run_cli(
    argv: list[str],
    *,
    environ: Mapping[str, str] | None = None,
    interactive: bool = False,
    prompt=lambda _message: "prompted-pat",
    factories: Factories | None = None,
) -> tuple[int, str, str, Factories]:
    selected = Factories() if factories is None else factories
    stdout = StringIO()
    stderr = StringIO()
    status = main(
        argv,
        environ={} if environ is None else environ,
        interactive=interactive,
        prompt=prompt,
        stdout=stdout,
        stderr=stderr,
        hub_factory=selected.hub_factory,
        discovery_factory=selected.discovery_factory,
        crane_factory=selected.crane_factory,
    )
    return status, stdout.getvalue(), stderr.getvalue(), selected


def test_cutoff_argument_adapts_domain_error() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="timezone"):
        cutoff_argument("2026-01-01")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--namespace", "user"], "at least one policy"),
        (
            ["--namespace", "user", "--untagged", "--include-never-pulled"],
            "requires --before",
        ),
        (
            [
                "--namespace",
                "user",
                "--before",
                "1d",
                "--apply",
                "--confirm",
                "other",
            ],
            "exactly match",
        ),
    ],
)
def test_safety_validation_happens_before_authentication(argv: list[str], message: str) -> None:
    status, _, stderr, factories = run_cli(argv, environ={"DH_PAT": "pat"})
    assert status == 2
    assert message in stderr
    assert factories.hub_credentials is None


@pytest.mark.parametrize("workers", ["0", "not-an-integer"])
def test_manifest_workers_must_be_positive_before_authentication(workers: str) -> None:
    factories = Factories()

    with pytest.raises(SystemExit, match="2"):
        run_cli(
            [
                "--namespace",
                "user",
                "--untagged",
                "--manifest-workers",
                workers,
            ],
            environ={"DH_PAT": "pat", "DH_COOKIE": "session=cookie"},
            factories=factories,
        )

    assert factories.hub_credentials is None
    assert factories.discovery_cookie is None
    assert factories.crane_credentials is None


def test_pat_is_required_in_noninteractive_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DH_PAT", "ambient-pat")
    status, _, stderr, factories = run_cli(["--namespace", "user", "--before", "1d"])
    assert status == 2
    assert "DH_PAT" in stderr
    assert factories.hub_credentials is None


def test_pat_can_be_prompted_without_appearing_in_output() -> None:
    status, stdout, stderr, factories = run_cli(
        ["--namespace", "user", "--before", "1d"], interactive=True
    )
    assert status == 0
    assert factories.hub_credentials == ("user", "prompted-pat")
    assert "prompted-pat" not in stdout + stderr


def test_untagged_policy_requires_cookie_before_authentication() -> None:
    status, _, stderr, factories = run_cli(
        ["--namespace", "user", "--untagged"], environ={"DH_PAT": "pat"}
    )
    assert status == 2
    assert "DH_COOKIE" in stderr
    assert factories.hub_credentials is None


def test_dry_run_prints_candidates_without_deleting() -> None:
    status, stdout, stderr, factories = run_cli(
        [
            "--namespace",
            "namespace",
            "--repository",
            "app",
            "--before",
            "2026-01-01T00:00:00Z",
        ],
        environ={"DH_USERNAME": "login", "DH_PAT": "pat"},
    )
    assert status == 0
    assert stderr == ""
    assert "DRY-RUN: 1 candidate(s) in namespace" in stdout
    assert "namespace/app:old" in stdout
    assert factories.hub_credentials == ("login", "pat")
    assert factories.hub.deleted == []
    assert factories.crane_credentials is None


def test_empty_apply_plan_does_not_start_crane() -> None:
    factories = Factories()
    factories.hub.tags_result = []
    status, stdout, stderr, factories = run_cli(
        [
            "--namespace",
            "user",
            "--before",
            "1d",
            "--apply",
            "--confirm",
            "user",
        ],
        environ={"DH_PAT": "pat"},
        factories=factories,
    )
    assert status == 0
    assert "APPLY: 0 candidate(s)" in stdout
    assert stderr == ""
    assert factories.crane_credentials is None


def test_apply_stale_tags_without_starting_crane() -> None:
    status, stdout, stderr, factories = run_cli(
        [
            "--namespace",
            "user",
            "--before",
            "2026-01-01T00:00:00Z",
            "--apply",
            "--confirm",
            "user",
        ],
        environ={"DH_PAT": "pat"},
    )
    assert status == 0
    assert "deleted user/app:old" in stdout
    assert stderr == ""
    assert factories.hub.deleted == ["old"]
    assert factories.crane_credentials is None


def test_apply_untagged_uses_cookie_and_crane() -> None:
    status, stdout, stderr, factories = run_cli(
        [
            "--namespace",
            "user",
            "--untagged",
            "--apply",
            "--confirm",
            "user",
        ],
        environ={"DH_PAT": "pat", "DH_COOKIE": "session=cookie"},
    )
    assert status == 0
    assert "user/app@" + DIGEST_B in stdout
    assert stderr == ""
    assert factories.discovery_cookie == "session=cookie"
    assert factories.crane_credentials == ("user", "pat")
    assert factories.crane.deleted == [DIGEST_B]
    assert factories.crane.reachability_calls == [("user", "app", {DIGEST_A})]
    assert not factories.crane.entered


def test_apply_passes_manifest_workers_to_service() -> None:
    with patch.object(CleanupService, "apply", autospec=True) as apply:
        apply.return_value = ApplyResult((), ())
        status, _, stderr, _ = run_cli(
            [
                "--namespace",
                "user",
                "--untagged",
                "--manifest-workers",
                "2",
                "--apply",
                "--confirm",
                "user",
            ],
            environ={"DH_PAT": "pat", "DH_COOKIE": "session=cookie"},
        )

    assert status == 0
    assert stderr == ""
    assert apply.call_args.kwargs["manifest_workers"] == 2


def test_untagged_dry_run_excludes_manifests_reachable_from_retained_tags() -> None:
    factories = Factories()
    factories.crane.reachable_children.add(DIGEST_B)

    status, stdout, stderr, factories = run_cli(
        ["--namespace", "user", "--untagged"],
        environ={"DH_PAT": "pat", "DH_COOKIE": "session=cookie"},
        factories=factories,
    )

    assert status == 0
    assert "DRY-RUN: 0 candidate(s)" in stdout
    assert stderr == ""
    assert factories.crane_credentials == ("user", "pat")
    assert factories.crane.deleted == []
    assert not factories.crane.entered


def test_partial_failure_returns_one_and_prints_safe_error() -> None:
    factories = Factories()
    factories.hub.delete_errors["old"] = CleanupError("tag rejected")
    status, stdout, stderr, _ = run_cli(
        [
            "--namespace",
            "user",
            "--before",
            "2026-01-01T00:00:00Z",
            "--apply",
            "--confirm",
            "user",
        ],
        environ={"DH_PAT": "pat"},
        factories=factories,
    )
    assert status == 1
    assert "APPLY: 1 candidate(s)" in stdout
    assert "ERROR: user/app:old: tag rejected" in stderr
