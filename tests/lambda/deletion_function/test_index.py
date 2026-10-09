# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the Deletion Lambda handler."""

import os
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from conftest import BUCKET_NAME, SHARED_BUCKET_NAME, SHARED_PREFIX
from pydantic import ValidationError

# Set env vars BEFORE importing the handler, because Powertools Metrics
# reads POWERTOOLS_METRICS_NAMESPACE at module level (Metrics() singleton).
os.environ.setdefault(
    "POWERTOOLS_METRICS_NAMESPACE", "SampleS3ScheduledDeletion"
)
os.environ.setdefault(
    "DATA_EVENT_BUS_NAME", "sample-s3-scheduled-deletion-deletion-events"
)

from index import handler  # noqa: E402

OBJECT_KEY = "retention-5-mins/test.txt"

# Types of the fixtures in tests/lambda/conftest.py.
Boto3Mocks = dict[str, MagicMock]
PublishedEvent = Callable[[str], dict[str, object]]
EmittedMetrics = Callable[[], dict[str, set[tuple[str, ...]]]]


def _make_deletion_event(
    key: str = OBJECT_KEY,
    bucket: str = BUCKET_NAME,
) -> dict[str, Any]:
    """Build a minimal deletion payload."""
    return {"bucket": bucket, "key": key}


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def _prime(client_mock: MagicMock) -> None:
    """Make the object exist and every AWS call succeed."""
    client_mock.head_object.return_value = {}
    client_mock.delete_object.return_value = {}
    client_mock.put_events.return_value = {
        "FailedEntryCount": 0,
        "Entries": [{"EventId": "fake-id"}],
    }


def test_handler_happy_path_completes_without_error(mock_boto3):
    """Handler deletes an existing object and returns without error."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    result = handler(_make_deletion_event(), MagicMock())

    assert result is None
    client_mock.delete_object.assert_called_once()


def test_root_level_key_is_deleted_without_a_path_prefix(
    mock_boto3, published_event
):
    """The prefix only labels metrics; it must not decide the deletion."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    assert handler(_make_deletion_event(key="report.pdf"), MagicMock()) is None

    client_mock.delete_object.assert_called_once_with(
        Bucket=BUCKET_NAME, Key="report.pdf"
    )
    assert published_event("DeletionSucceeded")["prefix"] is None


def test_missing_object_is_skipped_without_deleting(
    mock_boto3, emitted_metrics, published_event
):
    """A repeated scheduled deletion is a no-op, not a failure."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)
    client_mock.head_object.side_effect = _client_error("404", "HeadObject")

    assert handler(_make_deletion_event(), MagicMock()) is None

    client_mock.delete_object.assert_not_called()
    assert published_event("DeletionFailed.ObjectMissing")
    assert "DuplicateDeletion" in emitted_metrics()


def test_deletion_events_carry_no_source(
    mock_boto3: Boto3Mocks, published_event: PublishedEvent
) -> None:
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    handler(_make_deletion_event(), MagicMock())

    assert "source" not in published_event("DeletionTriggered")
    assert "source" not in published_event("DeletionSucceeded")


def test_payload_from_a_pending_schedule_is_still_processed(
    mock_boto3: Boto3Mocks,
) -> None:
    """Schedules created before ``source`` was retired still carry it."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)
    pending_payload = {
        "bucket": BUCKET_NAME,
        "key": OBJECT_KEY,
        "schedule_name": "sample-s3-scheduled-deletion-abc",
        "source": "scheduled",
        "version_id": None,
    }

    assert handler(pending_payload, MagicMock()) is None
    client_mock.delete_object.assert_called_once_with(
        Bucket=BUCKET_NAME, Key=OBJECT_KEY
    )


def test_deletion_metrics_carry_only_the_path_dimension(
    mock_boto3: Boto3Mocks, emitted_metrics: EmittedMetrics
) -> None:
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    handler(_make_deletion_event(), MagicMock())

    assert emitted_metrics()["ObjectDeletion"] == {("Path",)}


def test_versioned_deletion_removes_only_that_version(mock_boto3):
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    handler({**_make_deletion_event(), "version_id": "v1"}, MagicMock())

    expected = {"Bucket": BUCKET_NAME, "Key": OBJECT_KEY, "VersionId": "v1"}
    client_mock.head_object.assert_called_once_with(**expected)
    client_mock.delete_object.assert_called_once_with(**expected)


@pytest.mark.parametrize(
    "failing_call, operation",
    [("head_object", "HeadObject"), ("delete_object", "DeleteObject")],
)
def test_s3_error_is_reported_and_re_raised(
    mock_boto3, emitted_metrics, published_event, failing_call, operation
):
    """AccessDenied must reach the DLQ, not pass as an absent object."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)
    getattr(client_mock, failing_call).side_effect = _client_error(
        "AccessDenied", operation
    )

    with pytest.raises(ClientError):
        handler(_make_deletion_event(), MagicMock())

    failure = published_event("DeletionFailed.Error")
    assert "AccessDenied" in failure["error"]
    # Dimensionless: that is the metric the DeletionError alarm watches.
    assert emitted_metrics()["DeletionError"] == {()}
    if failing_call == "head_object":
        client_mock.delete_object.assert_not_called()


# Rejected before any metric is added, so Powertools warns on the flush.
@pytest.mark.filterwarnings("ignore:No application metrics to publish")
def test_malformed_payload_is_rejected_before_touching_s3(mock_boto3):
    client_mock = mock_boto3["client"]

    with pytest.raises(ValidationError):
        handler({"bucket": BUCKET_NAME}, MagicMock())

    client_mock.head_object.assert_not_called()
    client_mock.delete_object.assert_not_called()


# --- Managed scope ----------------------------------------------------------


def test_request_outside_the_shared_prefixes_is_rejected(
    mock_boto3: Boto3Mocks,
    emitted_metrics: EmittedMetrics,
    published_event: PublishedEvent,
) -> None:
    """Somebody else's objects in a shared bucket are not ours to delete."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)
    key = "somebody-elses-data/report.pdf"

    result = handler(
        _make_deletion_event(key=key, bucket=SHARED_BUCKET_NAME),
        MagicMock(),
    )

    assert result is None
    client_mock.head_object.assert_not_called()
    client_mock.delete_object.assert_not_called()
    rejected = published_event("DeletionRejected")
    assert rejected["key"] == key
    assert "source" not in rejected
    assert "DeletionRejected" in emitted_metrics()


def test_request_inside_the_shared_prefixes_is_deleted(
    mock_boto3: Boto3Mocks,
) -> None:
    client_mock = mock_boto3["client"]
    _prime(client_mock)
    key = f"{SHARED_PREFIX}report.pdf"

    result = handler(
        _make_deletion_event(key=key, bucket=SHARED_BUCKET_NAME),
        MagicMock(),
    )

    assert result is None
    client_mock.delete_object.assert_called_once_with(
        Bucket=SHARED_BUCKET_NAME, Key=key
    )


def test_request_for_an_unmanaged_bucket_is_rejected(
    mock_boto3, published_event
):
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    result = handler(
        _make_deletion_event(bucket="some-other-bucket"),
        MagicMock(),
    )

    assert result is None
    client_mock.delete_object.assert_not_called()
    assert published_event("DeletionRejected")["bucket"] == "some-other-bucket"


def test_scheduled_request_outside_the_scope_is_not_retried(mock_boto3):
    """A rule removed after scheduling: retrying would be rejected again."""
    client_mock = mock_boto3["client"]
    _prime(client_mock)

    # Returning, rather than raising, keeps the event out of the DLQ.
    assert (
        handler(
            _make_deletion_event(
                key="retired-rule/report.pdf", bucket=SHARED_BUCKET_NAME
            ),
            MagicMock(),
        )
        is None
    )
    client_mock.delete_object.assert_not_called()
