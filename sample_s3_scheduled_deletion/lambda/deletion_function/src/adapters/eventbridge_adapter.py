# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""EventBridge adapter for publishing deletion events."""

import json
from datetime import datetime, timezone
from typing import Any

import boto3
from aws_lambda_powertools import Logger

logger = Logger(child=True)

# NOTE: the stack's "deletion-events-log" EventBridge rule matches events by
# this Source value to route them to the consolidated CloudWatch log group.
# Keep it in sync with the rule's event pattern in constructs/deletion.py.
SOURCE = "retention.deletion"


class EventPublisher:
    """Publishes structured events to the data EventBridge bus.

    Events carry metadata (identifiers, timestamps, operational info), not
    object content.
    """

    def __init__(self, event_bus_name: str) -> None:
        self._bus_name = event_bus_name
        self._client = boto3.client("events")

    def _put_event(self, detail_type: str, detail: dict[str, Any]) -> None:
        """Put a single event on the bus. Failures are logged but not raised."""
        detail["timestamp"] = datetime.now(timezone.utc).isoformat()
        try:
            self._client.put_events(
                Entries=[
                    {
                        "Source": SOURCE,
                        "DetailType": detail_type,
                        "Detail": json.dumps(detail),
                        "EventBusName": self._bus_name,
                    }
                ]
            )
        except Exception:
            logger.exception(
                "Failed to publish event to EventBridge",
                extra={"detail_type": detail_type, "detail": detail},
            )

    def deletion_triggered(
        self,
        *,
        bucket: str,
        key: str,
        version_id: str | None = None,
    ) -> None:
        """Deletion request received."""
        self._put_event(
            "DeletionTriggered",
            {
                "bucket": bucket,
                "key": key,
                "version_id": version_id,
            },
        )

    def deletion_rejected(
        self,
        *,
        bucket: str,
        key: str,
        version_id: str | None = None,
    ) -> None:
        """Request refused: the object is not one the stack manages."""
        self._put_event(
            "DeletionRejected",
            {
                "bucket": bucket,
                "key": key,
                "reason": (
                    "Object is outside the buckets and prefixes this stack "
                    "manages"
                ),
                "version_id": version_id,
            },
        )

    def deletion_succeeded(
        self,
        *,
        bucket: str,
        key: str,
        prefix: str | None,
        version_id: str | None = None,
    ) -> None:
        """Object deleted successfully; *prefix* is ``None`` at the root."""
        self._put_event(
            "DeletionSucceeded",
            {
                "bucket": bucket,
                "key": key,
                "prefix": prefix,
                "version_id": version_id,
            },
        )

    def deletion_failed_object_missing(
        self,
        *,
        bucket: str,
        key: str,
        version_id: str | None = None,
    ) -> None:
        """Deletion failed because the object was already gone."""
        self._put_event(
            "DeletionFailed.ObjectMissing",
            {
                "bucket": bucket,
                "key": key,
                "reason": "Object does not exist",
                "version_id": version_id,
            },
        )

    def deletion_failed_error(
        self,
        *,
        bucket: str,
        key: str,
        error: str,
        version_id: str | None = None,
    ) -> None:
        """Deletion failed for reasons other than missing object."""
        self._put_event(
            "DeletionFailed.Error",
            {
                "bucket": bucket,
                "key": key,
                "error": error,
                "version_id": version_id,
            },
        )
