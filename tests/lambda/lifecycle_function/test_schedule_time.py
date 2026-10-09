# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for deletion-time resolution."""

from datetime import datetime, timedelta, timezone

import pytest
from src.models import InvalidExpireAtError, ObjectMetadata
from src.schedule_creation_service import (
    MIN_LEAD_SECONDS,
    _resolve_schedule_time,
)

NOW = datetime(2026, 9, 8, 6, 0, 0, tzinfo=timezone.utc)
TTL_SECONDS = 300
MAX_RETENTION_SECONDS = 86400
OBJECT_KEY = "retention-5-mins/test.txt"


def _resolve(expire_at_raw, *, ttl_seconds=TTL_SECONDS, now=NOW):
    return _resolve_schedule_time(
        object_metadata=ObjectMetadata(expire_at=expire_at_raw),
        now=now,
        ttl_seconds=ttl_seconds,
        max_retention_seconds=MAX_RETENTION_SECONDS,
        object_key=OBJECT_KEY,
    )


# --- Timezone normalisation --------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # A positive offset means the instant is EARLIER in UTC.
        ("2026-09-08T12:00:00+05:00", datetime(2026, 9, 8, 7, 0, 0)),
        # A negative offset means the instant is LATER in UTC.
        ("2026-09-08T12:00:00-04:00", datetime(2026, 9, 8, 16, 0, 0)),
        ("2026-09-08T12:00:00Z", datetime(2026, 9, 8, 12, 0, 0)),
        ("2026-09-08T12:00:00+00:00", datetime(2026, 9, 8, 12, 0, 0)),
        # Naive values are documented as UTC and treated as such.
        ("2026-09-08T12:00:00", datetime(2026, 9, 8, 12, 0, 0)),
    ],
)
def test_expire_at_is_normalised_to_utc(raw, expected):
    """A non-UTC offset must shift the instant, not be dropped."""
    parsed = ObjectMetadata(expire_at=raw).parse_expire_at()

    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed.replace(tzinfo=None) == expected


def test_non_utc_expire_at_survives_full_resolution():
    """The offset conversion must reach the resolved schedule time."""
    resolution = _resolve("2026-09-08T12:00:00+05:00")

    assert resolution.expire_at_override is True
    assert resolution.schedule_time == datetime(
        2026, 9, 8, 7, 0, 0, tzinfo=timezone.utc
    )
    # Safe to format without an offset only because the value is UTC here.
    assert (
        resolution.schedule_time.strftime("%Y-%m-%dT%H:%M:%S")
        == "2026-09-08T07:00:00"
    )


# --- Upper bound -------------------------------------------------------------


def test_expire_at_beyond_ceiling_is_clamped():
    """An expireAt past the ceiling must not be honoured."""
    resolution = _resolve("2030-01-01T00:00:00Z")

    assert resolution.clamped_to_max is True
    assert resolution.schedule_time == NOW + timedelta(
        seconds=MAX_RETENTION_SECONDS
    )


def test_expire_at_at_the_ceiling_is_not_clamped():
    """The ceiling itself is allowed; only values beyond it are cut back."""
    resolution = _resolve(
        (NOW + timedelta(seconds=MAX_RETENTION_SECONDS)).isoformat()
    )

    assert resolution.clamped_to_max is False
    assert resolution.schedule_time == NOW + timedelta(
        seconds=MAX_RETENTION_SECONDS
    )


def test_expire_at_just_inside_the_ceiling_is_honoured():
    resolution = _resolve(
        (NOW + timedelta(seconds=MAX_RETENTION_SECONDS - 60)).isoformat()
    )

    assert resolution.clamped_to_max is False
    assert resolution.expire_at_override is True


# --- Lower bound -------------------------------------------------------------


def test_expire_at_inside_minimum_lead_time_is_pushed_out():
    resolution = _resolve((NOW + timedelta(seconds=30)).isoformat())

    assert resolution.clamped_to_min is True
    assert resolution.schedule_time == NOW + timedelta(
        seconds=MIN_LEAD_SECONDS
    )


def test_ttl_shorter_than_minimum_lead_time_is_pushed_out():
    resolution = _resolve(None, ttl_seconds=60)

    assert resolution.expire_at_override is False
    assert resolution.clamped_to_min is True
    assert resolution.schedule_time == NOW + timedelta(
        seconds=MIN_LEAD_SECONDS
    )


# --- Fallbacks to the rule's default TTL ------------------------------------


def test_absent_expire_at_uses_ttl():
    resolution = _resolve(None)

    assert resolution.expire_at_override is False
    assert resolution.clamped_to_max is False
    assert resolution.clamped_to_min is False
    assert resolution.schedule_time == NOW + timedelta(seconds=TTL_SECONDS)


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-timestamp",
        "2026-13-45T99:00:00Z",
        "",
        "1757311200",
    ],
)
def test_invalid_expire_at_raises_a_model_error(raw):
    with pytest.raises(InvalidExpireAtError):
        ObjectMetadata(expire_at=raw).parse_expire_at()


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-timestamp",
        "2026-13-45T99:00:00Z",
        "",
        "1757311200",
    ],
)
def test_unparseable_expire_at_falls_back_to_ttl(raw):
    resolution = _resolve(raw)

    assert resolution.expire_at_override is False
    assert resolution.schedule_time == NOW + timedelta(seconds=TTL_SECONDS)


@pytest.mark.parametrize(
    "raw",
    [
        "2020-01-01T00:00:00Z",
        # Equal to now counts as past: there is no lead time left.
        NOW.isoformat(),
        # Past in UTC despite a future-looking local wall clock.
        "2026-09-08T10:00:00+09:00",
    ],
)
def test_past_expire_at_falls_back_to_ttl(raw):
    resolution = _resolve(raw)

    assert resolution.expire_at_override is False
    assert resolution.schedule_time == NOW + timedelta(seconds=TTL_SECONDS)


# --- Reported time-to-deletion ----------------------------------------------


def test_seconds_until_deletion_reflects_the_effective_time():
    """The published TTL must describe reality, not the nominal rule TTL."""
    clamped = _resolve("2030-01-01T00:00:00Z")
    assert clamped.seconds_until_deletion(NOW) == MAX_RETENTION_SECONDS

    plain = _resolve(None)
    assert plain.seconds_until_deletion(NOW) == TTL_SECONDS

    overridden = _resolve((NOW + timedelta(hours=2)).isoformat())
    assert overridden.seconds_until_deletion(NOW) == 7200
