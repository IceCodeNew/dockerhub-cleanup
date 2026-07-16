"""Command-line interface for Docker Hub cleanup."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack
from datetime import datetime
from typing import Protocol, TextIO

from dockerhub_cleanup.crane import CraneClient
from dockerhub_cleanup.dockerhub import DockerHubClient
from dockerhub_cleanup.domain import Candidate, parse_cutoff
from dockerhub_cleanup.errors import CleanupError
from dockerhub_cleanup.image_management import ImageManagementClient
from dockerhub_cleanup.service import (
    CleanupPlan,
    CleanupService,
    DeletionFailure,
    DigestDiscovery,
    HubRepository,
    ManifestDeletion,
    ManifestReachability,
)

HubFactory = Callable[[str, str], HubRepository]
DiscoveryFactory = Callable[[str], DigestDiscovery]


class CraneOperations(ManifestDeletion, ManifestReachability, Protocol):
    """Crane operations shared by planning and apply."""


CraneFactory = Callable[[str, str], AbstractContextManager[CraneOperations]]
MANIFEST_DELETE_WORKERS = 4
CANDIDATE_KIND_MIN_WIDTH = 10


def cutoff_argument(value: str) -> datetime:
    """Adapt domain cutoff errors to argparse diagnostics."""

    try:
        return parse_cutoff(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the public command-line contract."""

    parser = argparse.ArgumentParser(
        description="Safely preview and clean stale or untagged Docker Hub images."
    )
    parser.add_argument("--namespace", required=True, help="Docker Hub user or organization")
    parser.add_argument(
        "--repository",
        action="append",
        dest="repositories",
        help="limit cleanup to a repository; repeatable",
    )
    parser.add_argument(
        "--before",
        type=cutoff_argument,
        help="stale cutoff as aware ISO-8601, 180d, 24h, or 8w",
    )
    parser.add_argument(
        "--untagged",
        action="store_true",
        help="find untagged manifests; requires DH_COOKIE",
    )
    parser.add_argument(
        "--include-never-pulled",
        action="store_true",
        help="include never-pulled tags pushed before the cutoff",
    )
    parser.add_argument(
        "--keep-tag",
        action="append",
        default=[],
        help="protect a case-sensitive tag glob; repeatable",
    )
    parser.add_argument("--apply", action="store_true", help="perform the planned deletions")
    parser.add_argument(
        "--confirm",
        metavar="NAMESPACE",
        help="required with --apply and must exactly match --namespace",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    interactive: bool | None = None,
    prompt: Callable[[str], str] = getpass.getpass,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    hub_factory: HubFactory = DockerHubClient,
    discovery_factory: DiscoveryFactory = ImageManagementClient,
    crane_factory: CraneFactory = CraneClient,
) -> int:
    """Run the CLI and return a process exit status."""

    try:
        args = build_parser().parse_args(argv)
        return _run(
            args,
            environ=os.environ if environ is None else environ,
            interactive=sys.stdin.isatty() if interactive is None else interactive,
            prompt=prompt,
            stdout=stdout,
            stderr=stderr,
            hub_factory=hub_factory,
            discovery_factory=discovery_factory,
            crane_factory=crane_factory,
        )
    except CleanupError as exc:
        print(f"ERROR: {exc}", file=stderr)
        return 2


def _run(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    interactive: bool,
    prompt: Callable[[str], str],
    stdout: TextIO,
    stderr: TextIO,
    hub_factory: HubFactory,
    discovery_factory: DiscoveryFactory,
    crane_factory: CraneFactory,
) -> int:
    if args.before is None and not args.untagged:
        raise CleanupError("select at least one policy: --before and/or --untagged")
    if args.include_never_pulled and args.before is None:
        raise CleanupError("--include-never-pulled requires --before")
    if args.apply and args.confirm != args.namespace:
        raise CleanupError("--apply requires --confirm to exactly match --namespace")

    username = environ.get("DH_USERNAME") or args.namespace
    pat = environ.get("DH_PAT")
    if not pat and interactive:
        pat = prompt("Docker Hub PAT: ")
    if not pat:
        raise CleanupError("set DH_PAT to a Docker Hub personal access token")

    cookie = environ.get("DH_COOKIE")
    if args.untagged and not cookie:
        raise CleanupError("set DH_COOKIE to enable untagged discovery")

    hub = hub_factory(username, pat)
    discovery = discovery_factory(cookie) if cookie and args.untagged else None
    with ExitStack() as stack:
        crane = stack.enter_context(crane_factory(username, pat)) if args.untagged else None
        service = CleanupService(hub, discovery, crane)
        plan = service.plan(
            args.namespace,
            repositories=args.repositories,
            cutoff=args.before,
            include_untagged=args.untagged,
            include_never_pulled=args.include_never_pulled,
            keep_patterns=args.keep_tag,
        )
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(
            f"{mode}: {len(plan.candidates)} candidate(s) in {plan.namespace}",
            file=stdout,
            flush=True,
        )
        for line in _format_candidates(plan):
            print(line, file=stdout, flush=True)

        if not args.apply or not plan.candidates:
            return 0

        def report_deleted(candidate: Candidate) -> None:
            print(f"deleted {_reference(plan.namespace, candidate)}", file=stdout, flush=True)

        def report_failure(failure: DeletionFailure) -> None:
            print(
                f"ERROR: {_reference(plan.namespace, failure.candidate)}: {failure.message}",
                file=stderr,
                flush=True,
            )

        if crane is not None:
            result = service.apply(
                plan,
                crane,
                on_deleted=report_deleted,
                on_failure=report_failure,
                manifest_workers=MANIFEST_DELETE_WORKERS,
            )
        else:
            result = service.apply(
                plan,
                on_deleted=report_deleted,
                on_failure=report_failure,
            )
        return 1 if result.failures else 0


def _reference(namespace: str, candidate: Candidate) -> str:
    separator = "@" if candidate.kind == "untagged" else ":"
    return f"{namespace}/{candidate.repository}{separator}{candidate.reference}"


def _format_candidates(plan: CleanupPlan) -> Iterator[str]:
    kind_width = _maximum_field_width(
        (candidate.kind for candidate in plan.candidates),
        minimum=CANDIDATE_KIND_MIN_WIDTH,
    )
    for candidate in plan.candidates:
        yield _format_candidate(plan.namespace, candidate, kind_width)


def _format_candidate(namespace: str, candidate: Candidate, kind_width: int) -> str:
    return f"{candidate.kind:<{kind_width}} {_reference(namespace, candidate)}  {candidate.reason}"


def _maximum_field_width(values: Iterable[str], *, minimum: int) -> int:
    return max(minimum, max((len(value) for value in values), default=0))
