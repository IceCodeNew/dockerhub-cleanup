from datetime import UTC, datetime, timedelta, timezone

import pytest

from dockerhub_cleanup.domain import (
    Tag,
    extract_digests,
    is_protected,
    parse_api_timestamp,
    parse_cutoff,
    select_stale_tags,
    select_untagged_digests,
)

UTC = UTC


def test_parse_api_timestamp_normalizes_offsets_and_missing_values() -> None:
    assert parse_api_timestamp(None) is None
    assert parse_api_timestamp("") is None
    assert parse_api_timestamp("2026-07-15T10:00:00+08:00") == datetime(2026, 7, 15, 2, tzinfo=UTC)
    assert parse_api_timestamp("2026-07-15T02:00:00") == datetime(2026, 7, 15, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("24h", datetime(2026, 7, 14, tzinfo=UTC)),
        ("8d", datetime(2026, 7, 7, tzinfo=UTC)),
        ("8W", datetime(2026, 5, 20, tzinfo=UTC)),
    ],
)
def test_parse_relative_cutoff(value: str, expected: datetime) -> None:
    assert parse_cutoff(value, datetime(2026, 7, 15, tzinfo=UTC)) == expected


def test_parse_absolute_cutoff_normalizes_to_utc() -> None:
    assert parse_cutoff("2026-07-15T10:00:00+08:00") == datetime(2026, 7, 15, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "now", "message"),
    [
        ("2026-07-15", None, "absolute cutoff"),
        ("1d", datetime(2026, 7, 15), "current time"),
    ],
)
def test_parse_cutoff_rejects_ambiguous_times(
    value: str, now: datetime | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_cutoff(value, now)


def test_tag_protection_uses_case_sensitive_globs() -> None:
    assert is_protected("prod-2026", ["latest", "prod-*"])
    assert not is_protected("Prod-2026", ["prod-*"])


def test_select_stale_tags_respects_cutoff_protection_and_never_pulled_policy() -> None:
    old = datetime(2025, 1, 1, tzinfo=UTC)
    recent = datetime(2026, 7, 1, tzinfo=UTC)
    tags = [
        Tag("app", "old", "sha256:a", old, old),
        Tag("app", "latest", "sha256:b", old, old),
        Tag("app", "never", "sha256:c", None, old),
        Tag("app", "never-recent", "sha256:d", None, recent),
        Tag("app", "missing-times", "sha256:e", None, None),
        Tag("app", "recent", "sha256:f", recent, old),
    ]

    default = select_stale_tags(tags, datetime(2026, 1, 1, tzinfo=UTC), ["latest"])
    assert [candidate.reference for candidate in default] == ["old"]

    inclusive = select_stale_tags(
        tags,
        datetime(2026, 1, 1, tzinfo=UTC),
        ["latest"],
        include_never_pulled=True,
    )
    assert [candidate.reference for candidate in inclusive] == ["old", "never"]
    assert inclusive[0].reason == "last pulled 2025-01-01T00:00:00+00:00"
    assert inclusive[1].reason == "never pulled; pushed 2025-01-01T00:00:00+00:00"


def test_select_stale_tags_normalizes_cutoff_and_metadata_offsets() -> None:
    tag = Tag(
        "app",
        "old",
        "sha256:a",
        datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8))),
        None,
    )
    cutoff = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=7)))
    assert select_stale_tags([tag], cutoff)[0].reference == "old"


def test_select_stale_tags_rejects_naive_cutoff() -> None:
    with pytest.raises(ValueError, match="cutoff"):
        select_stale_tags([], datetime(2026, 1, 1))


def test_extract_digests_walks_nested_json_and_rejects_noncanonical_values() -> None:
    digest = "sha256:" + "a" * 64
    payload = ["noise", digest, {"nested": [digest, "sha256:short", 3]}]
    assert extract_digests(payload) == {digest}


def test_select_untagged_digests_returns_sorted_set_difference() -> None:
    digest_a = "sha256:" + "a" * 64
    digest_b = "sha256:" + "b" * 64
    digest_c = "sha256:" + "c" * 64

    candidates = select_untagged_digests(
        "app", [digest_c, digest_a, digest_b, digest_c], [digest_b]
    )

    assert [candidate.reference for candidate in candidates] == [digest_a, digest_c]
    assert all(candidate.kind == "untagged" for candidate in candidates)
