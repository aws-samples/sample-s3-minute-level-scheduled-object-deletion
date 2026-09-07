# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Happy-path smoke test for the Lifecycle Lambda handler."""

import os
from unittest.mock import MagicMock

# Set env vars BEFORE importing the handler, because Powertools Metrics
# reads POWERTOOLS_METRICS_NAMESPACE at module level (Metrics() singleton).
os.environ.setdefault(
    "POWERTOOLS_METRICS_NAMESPACE", "SampleS3ScheduledDeletion"
)
os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "lifecycle_function")

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
