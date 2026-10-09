# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the Lifecycle Lambda handler."""

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from conftest import (
    BUCKET_NAME,
    MAX_RETENTION_SECONDS,
    PATH_PREFIX,
    TTL_SECONDS,
)

# Set env vars BEFORE importing the handler, because Powertools Metrics
# reads POWERTOOLS_METRICS_NAMESPACE at module level (Metrics() singleton).
os.environ.setdefault(
    "POWERTOOLS_METRICS_NAMESPACE", "SampleS3ScheduledDeletion"
)

from index import handler  # noqa: E402


def _make_s3_object_created_event(
    bucket: str = "sample-s3-scheduled-deletion-data-abc123",
    key: str = "retention-5-mins/test.txt",
) -> dict:
    """Build a full EventBridge S3 Object Created event."""
    return {
        "version": "0",
        "id": "12345678-1234-1234-1234-123456789012",
        "source": "aws.s3",
        "account": "111122223333",
        "time": "2024-01-15T12:00:00Z",
        "region": "us-east-1",
        "resources": [f"arn:aws:s3:::{bucket}"],
        "detail-type": "Object Created",
        "detail": {
            "version": "0",
            "request-id": "AAAAAAAAAAAAAAA",
            "requester": "111122223333",
            "bucket": {"name": bucket},
            "object": {"key": key, "size": 1024, "etag": "abcdef123456"},
        },
    }


def test_handler_happy_path_completes_without_error(mock_boto3):
    """Handler processes a valid S3 event, creates a schedule, and returns."""
    client_mock = mock_boto3["client"]

    # head_object returns empty metadata (no expireAt override)
    client_mock.head_object.return_value = {"Metadata": {}}

    # create_schedule succeeds
    client_mock.create_schedule.return_value = {}

    # put_events succeeds
    client_mock.put_events.return_value = {
        "FailedEntryCount": 0,
        "Entries": [{"EventId": "fake-id"}],
    }

    context = MagicMock()
    event = _make_s3_object_created_event()

    handler(event, context)

    # Verify a schedule was created
    client_mock.create_schedule.assert_called_once()


def test_handler_tolerates_an_existing_schedule(mock_boto3):
    """A duplicate S3 event must not fail the invocation.

    EventBridge delivers at least once and ``CreateSchedule`` is not an
    upsert, so a redelivered event hits ``ConflictException``. The pending
    schedule already covers the object, so the handler treats it as success
    instead of routing the event to the DLQ.
    """
    client_mock = mock_boto3["client"]

    client_mock.head_object.return_value = {"Metadata": {}}
    client_mock.put_events.return_value = {
        "FailedEntryCount": 0,
        "Entries": [{"EventId": "fake-id"}],
    }

    class ConflictException(Exception):
        """Stands in for the boto3-modelled scheduler exception."""

    client_mock.exceptions.ConflictException = ConflictException
    client_mock.create_schedule.side_effect = ConflictException(
        "Schedule sample-s3-scheduled-deletion-abc123 already exists."
    )

    # Must not raise.
    handler(_make_s3_object_created_event(), MagicMock())

    client_mock.create_schedule.assert_called_once()


# --- expireAt handling, end to end ------------------------------------------


def _created_schedule_expression(client_mock) -> str:
    """Return the ScheduleExpression from the single create_schedule call."""
    client_mock.create_schedule.assert_called_once()
    return client_mock.create_schedule.call_args.kwargs["ScheduleExpression"]


def _prime(client_mock, *, metadata: dict[str, str] | None = None) -> None:
    client_mock.head_object.return_value = {"Metadata": metadata or {}}
    client_mock.create_schedule.return_value = {}
    client_mock.put_events.return_value = {
        "FailedEntryCount": 0,
        "Entries": [{"EventId": "fake-id"}],
    }


def _target_instant(hours_ahead: int = 6) -> datetime:
    """A whole-second UTC instant inside the retention ceiling."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours_ahead)).replace(
        microsecond=0
    )


def test_non_utc_expire_at_schedules_the_correct_utc_instant(mock_boto3):
    """A +05:00 expireAt schedules the instant it denotes, not its clock."""
    client_mock = mock_boto3["client"]
    expected_utc_instant = _target_instant()
    utc_plus_five = timezone(timedelta(hours=5))
    _prime(
        client_mock,
        metadata={
            "expireat": expected_utc_instant.astimezone(
                utc_plus_five
            ).isoformat()
        },
    )

    handler(_make_s3_object_created_event(), MagicMock())

    assert _created_schedule_expression(client_mock) == (
        f"at({expected_utc_instant.strftime('%Y-%m-%dT%H:%M:%S')})"
    )
    kwargs = client_mock.create_schedule.call_args.kwargs
    assert kwargs["ScheduleExpressionTimezone"] == "UTC"


def test_utc_expire_at_schedules_verbatim(mock_boto3):
    client_mock = mock_boto3["client"]
    target = _target_instant()
    _prime(client_mock, metadata={"expireat": target.isoformat()})

    handler(_make_s3_object_created_event(), MagicMock())

    assert _created_schedule_expression(client_mock) == (
        f"at({target.strftime('%Y-%m-%dT%H:%M:%S')})"
    )


def test_far_future_expire_at_is_capped_at_the_retention_ceiling(mock_boto3):
    """An uploader must not be able to outlive the Lifecycle backstop."""
    client_mock = mock_boto3["client"]
    _prime(client_mock, metadata={"expireat": "2099-01-01T00:00:00Z"})

    handler(_make_s3_object_created_event(), MagicMock())

    expression = _created_schedule_expression(client_mock)
    assert expression != "at(2099-01-01T00:00:00)"

    scheduled_at = datetime.strptime(
        expression.removeprefix("at(").removesuffix(")"),
        "%Y-%m-%dT%H:%M:%S",
    ).replace(tzinfo=timezone.utc)
    ceiling = datetime.now(timezone.utc) + timedelta(
        seconds=MAX_RETENTION_SECONDS
    )
    # Slack for clock movement between the handler's ``now`` and this one.
    assert abs((scheduled_at - ceiling).total_seconds()) < 60


def test_unreadable_metadata_falls_back_to_the_ttl_and_is_reported(
    mock_boto3, emitted_metrics, published_event
):
    """A failed HeadObject loses the expireAt override, not the schedule."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)
    client_mock.head_object.side_effect = ClientError(
        {"Error": {"Code": "SlowDown", "Message": "Please reduce"}},
        "HeadObject",
    )

    handler(_make_s3_object_created_event(), MagicMock())

    scheduled_at = datetime.strptime(
        _created_schedule_expression(client_mock)
        .removeprefix("at(")
        .removesuffix(")"),
        "%Y-%m-%dT%H:%M:%S",
    ).replace(tzinfo=timezone.utc)
    expected = datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS)
    assert abs((scheduled_at - expected).total_seconds()) < 60
    # Dimensionless: that is the metric the MetadataLookupFailed alarm watches.
    assert emitted_metrics()["MetadataLookupFailed"] == {()}

    failure = published_event("MetadataLookupFailed")
    assert "SlowDown" in failure["error"]
    assert failure["prefix"] == PATH_PREFIX
    assert failure["default_ttl_seconds"] == TTL_SECONDS


# --- No-match path (deletes on arrival) -------------------------------------


def test_key_outside_every_retention_rule_is_deleted_immediately(mock_boto3):
    """An object matching no rule is deleted rather than scheduled."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    handler(
        _make_s3_object_created_event(key="not-a-managed-prefix/test.txt"),
        MagicMock(),
    )

    client_mock.delete_object.assert_called_once()
    assert client_mock.delete_object.call_args.kwargs["Key"] == (
        "not-a-managed-prefix/test.txt"
    )
    client_mock.create_schedule.assert_not_called()


# Keys observed verbatim in live S3 "Object Created" events on EventBridge.
@pytest.mark.parametrize(
    "key",
    [
        "retention-5-mins/a+b.txt",
        "retention-5-mins/a b.txt",
        "retention-5-mins/u\u00fc%25.txt",
    ],
    ids=["plus", "space", "unicode-and-percent"],
)
def test_special_characters_in_the_key_are_used_verbatim(mock_boto3, key):
    """Decoding the key would read and schedule a different object."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    handler(_make_s3_object_created_event(key=key), MagicMock())

    assert client_mock.head_object.call_args.kwargs["Key"] == key
    schedule = client_mock.create_schedule.call_args.kwargs
    assert json.loads(schedule["Target"]["Input"])["key"] == key


def test_schedule_payload_names_only_the_object_to_delete(
    mock_boto3: dict[str, MagicMock],
) -> None:
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    handler(_make_s3_object_created_event(), MagicMock())

    schedule_input: str = client_mock.create_schedule.call_args.kwargs[
        "Target"
    ]["Input"]
    payload: dict[str, object] = json.loads(schedule_input)
    assert set(payload) == {
        "bucket",
        "key",
        "schedule_name",
        "version_id",
    }


def test_root_level_key_is_deleted_immediately(mock_boto3, published_event):
    """A key with no prefix matches no rule; it is not a malformed event."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    handler(_make_s3_object_created_event(key="report.pdf"), MagicMock())

    client_mock.delete_object.assert_called_once_with(
        Bucket=BUCKET_NAME, Key="report.pdf"
    )
    client_mock.create_schedule.assert_not_called()
    assert published_event("ObjectRejected.Deleted")["prefix"] is None


def test_key_in_an_unconfigured_bucket_is_left_alone(mock_boto3):
    """Prefix alone is not enough — the bucket has to match too.

    An event from a bucket absent from REPOSITORY_CONFIG should not reach
    this function, since the EventBridge rule is scoped to the managed bucket
    ARNs. If one does, nothing here knows what that bucket's rules are, so
    deleting from it is never the right answer.
    """
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    handler(
        _make_s3_object_created_event(bucket="some-other-bucket"),
        MagicMock(),
    )

    client_mock.delete_object.assert_not_called()
    client_mock.create_schedule.assert_not_called()


def test_folder_marker_is_skipped_without_deleting_or_scheduling(mock_boto3):
    """A zero-byte prefix marker is not an actionable object."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    handler(
        _make_s3_object_created_event(key="retention-5-mins/"),
        MagicMock(),
    )

    client_mock.delete_object.assert_not_called()
    client_mock.create_schedule.assert_not_called()


# --- No-match path on a SHARED bucket (keeps the object) --------------------


def _use_shared_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-point REPOSITORY_CONFIG at the same bucket declared SHARED."""
    monkeypatch.setenv(
        "REPOSITORY_CONFIG",
        json.dumps(
            {
                "repositories": [
                    {
                        "bucket_name": BUCKET_NAME,
                        "path_prefix": PATH_PREFIX,
                        "ttl_seconds": TTL_SECONDS,
                        "delete_unmatched": False,
                    }
                ]
            }
        ),
    )


def test_shared_bucket_keeps_objects_outside_the_managed_prefixes(
    mock_boto3, monkeypatch
):
    """Somebody else's prefixes are not ours to delete from."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)
    _use_shared_bucket(monkeypatch)

    handler(
        _make_s3_object_created_event(key="somebody-elses-data/report.pdf"),
        MagicMock(),
    )

    client_mock.delete_object.assert_not_called()
    client_mock.create_schedule.assert_not_called()


def test_shared_bucket_reports_the_object_it_left_alone(
    mock_boto3, monkeypatch
):
    """Silence would make a misconfigured prefix undetectable."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)
    _use_shared_bucket(monkeypatch)

    handler(
        _make_s3_object_created_event(key="somebody-elses-data/report.pdf"),
        MagicMock(),
    )

    detail_types = [
        entry["DetailType"]
        for call in client_mock.put_events.call_args_list
        for entry in call.kwargs["Entries"]
    ]
    assert "ObjectUnmanaged" in detail_types
    assert "ObjectRejected.Deleted" not in detail_types


def test_shared_bucket_still_schedules_inside_the_managed_prefixes(
    mock_boto3, monkeypatch
):
    """SHARED changes only the no-match path, not the core behaviour."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)
    _use_shared_bucket(monkeypatch)

    handler(_make_s3_object_created_event(), MagicMock())

    client_mock.create_schedule.assert_called_once()
    client_mock.delete_object.assert_not_called()


def test_shared_bucket_keeps_a_root_level_object(
    mock_boto3, monkeypatch, published_event
):
    """A root-level upload must be reported, not fail into the DLQ."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)
    _use_shared_bucket(monkeypatch)

    handler(_make_s3_object_created_event(key="report.pdf"), MagicMock())

    client_mock.delete_object.assert_not_called()
    client_mock.create_schedule.assert_not_called()
    assert published_event("ObjectUnmanaged")["prefix"] is None
