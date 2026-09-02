# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Happy-path smoke test for the Deletion Lambda handler."""

import os
from unittest.mock import MagicMock

# Set env vars BEFORE importing the handler, because Powertools Metrics
# reads POWERTOOLS_METRICS_NAMESPACE at module level (Metrics() singleton).
os.environ.setdefault(
    "POWERTOOLS_METRICS_NAMESPACE", "SampleS3ScheduledDeletion"
)
os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "deletion_function")
os.environ.setdefault(
    "DATA_EVENT_BUS_NAME", "sample-s3-scheduled-deletion-deletion-events"
)

from index import handler  # noqa: E402


def _make_scheduled_deletion_event(
    bucket: str = "amzn-s3-demo-bucket",
    key: str = "retention-5-mins/test.txt",
) -> dict:
    """Build a minimal scheduled deletion payload."""
    return {
        "bucket": bucket,
        "key": key,
        "source": "scheduled",
    }


def test_handler_happy_path_completes_without_error(mock_boto3):
    """Handler deletes an existing object and returns without error."""
    client_mock = mock_boto3["client"]

    # head_object succeeds — object exists
    client_mock.head_object.return_value = {}

    # delete_object succeeds
    client_mock.delete_object.return_value = {}

    # put_events succeeds
    client_mock.put_events.return_value = {
        "FailedEntryCount": 0,
        "Entries": [{"EventId": "fake-id"}],
    }

    context = MagicMock()
    event = _make_scheduled_deletion_event()

    result = handler(event, context)

    # Scheduled deletions return None on success
    assert result is None

    # Verify the object was deleted
    client_mock.delete_object.assert_called_once()
