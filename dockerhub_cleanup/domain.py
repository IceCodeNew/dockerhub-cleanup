"""Registry-independent cleanup models and selection policies."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CandidateKind = Literal["stale-tag", "untagged"]


@dataclass(frozen=True)
class Tag:
    """Metadata needed to evaluate a registry tag."""

    repository: str
    name: str
    digest: str
    last_pulled: datetime | None
    last_pushed: datetime | None


@dataclass(frozen=True)
class Candidate:
    """A planned cleanup action with its audit reason."""

    kind: CandidateKind
    repository: str
    reference: str
    reason: str


def parse_api_timestamp(value: str | None) -> datetime | None:
    """Parse an API timestamp, treating an omitted offset as UTC."""

    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_cutoff(value: str, now: datetime | None = None) -> datetime:
    """Parse an aware ISO timestamp or a relative hours/days/weeks duration."""

    relative = re.fullmatch(r"(\d+)([dhw])", value.strip().lower())
    if relative:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("the current time must include a timezone")
        amount = int(relative.group(1))
        try:
            delta = {
                "h": timedelta(hours=amount),
                "d": timedelta(days=amount),
                "w": timedelta(weeks=amount),
            }[relative.group(2)]
            return (current - delta).astimezone(UTC)
        except OverflowError as exc:
            raise ValueError("relative cutoff is out of range") from exc

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("an absolute cutoff must include a timezone")
    return parsed.astimezone(UTC)


def is_protected(tag: str, patterns: Iterable[str]) -> bool:
    """Return whether a tag matches any case-sensitive protection glob."""

    return any(fnmatch.fnmatchcase(tag, pattern) for pattern in patterns)


def select_stale_tags(
    tags: Iterable[Tag],
    cutoff: datetime,
    keep_patterns: Iterable[str] = (),
    *,
    include_never_pulled: bool = False,
) -> list[Candidate]:
    """Select tags that satisfy the stale policy."""

    if cutoff.tzinfo is None:
        raise ValueError("cutoff must include a timezone")
    cutoff = cutoff.astimezone(UTC)
    protected_patterns = tuple(keep_patterns)
    candidates: list[Candidate] = []

    for tag in tags:
        if is_protected(tag.name, protected_patterns):
            continue
        if tag.last_pulled is not None and tag.last_pulled < cutoff:
            candidates.append(
                Candidate(
                    kind="stale-tag",
                    repository=tag.repository,
                    reference=tag.name,
                    reason=f"last pulled {tag.last_pulled.astimezone(UTC).isoformat()}",
                )
            )
        elif (
            include_never_pulled
            and tag.last_pulled is None
            and tag.last_pushed is not None
            and tag.last_pushed < cutoff
        ):
            candidates.append(
                Candidate(
                    kind="stale-tag",
                    repository=tag.repository,
                    reference=tag.name,
                    reason=(f"never pulled; pushed {tag.last_pushed.astimezone(UTC).isoformat()}"),
                )
            )
    return candidates


def iter_strings(value: object) -> Iterator[str]:
    """Yield every string contained in a JSON-like value."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)


def extract_digests(payload: object) -> set[str]:
    """Extract canonical SHA-256 digests from a JSON-like payload."""

    return {value for value in iter_strings(payload) if SHA256_RE.fullmatch(value)}


def select_untagged_digests(
    repository: str,
    all_digests: Iterable[str],
    tagged_digests: Iterable[str],
) -> list[Candidate]:
    """Select digests that are absent from retained tag references."""

    untagged = set(all_digests) - set(tagged_digests)
    return [
        Candidate(
            kind="untagged",
            repository=repository,
            reference=digest,
            reason="not referenced by a retained tag",
        )
        for digest in sorted(untagged)
    ]
