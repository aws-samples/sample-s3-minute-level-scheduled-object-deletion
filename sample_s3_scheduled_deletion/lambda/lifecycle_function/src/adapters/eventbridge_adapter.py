# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""EventBridge adapter for publishing deletion lifecycle events."""

import json
from datetime import datetime, timezone
from typing import Any

import boto3
from aws_lambda_powertools import Logger

logger = Logger(child=True)

# NOTE: the stack's "deletion-events-log" EventBridge rule matches events by
# this Source value to route them to the consolidated CloudWatch log group.
# Keep it in sync with the rule's event pattern in constructs/deletion.py.
SOURCE = "retention.lifecycle"


class EventPublisher:
    """Publishes structured events to the data EventBridge bus.

    All events contain only metadata (identifiers, timestamps, operational
    info) — never object content.
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

    def object_created(
        self,
        *,
        bucket: str,
        key: str,
        prefix: str,
        ttl_seconds: int,
        version_id: str | None = None,
    ) -> None:
        """Object landed in an allowed prefix."""
        self._put_event(
            "ObjectCreated",
            {
                "bucket": bucket,
                "key": key,
                "prefix": prefix,
                "ttl_seconds": ttl_seconds,
                "version_id": version_id,
            },
        )

    def object_rejected_deleted(
        self,
        *,
        bucket: str,
        key: str,
        prefix: str,
        version_id: str | None = None,
    ) -> None:
        """Object outside allowed paths — immediately deleted."""
        self._put_event(
            "ObjectRejected.Deleted",
            {
                "bucket": bucket,
                "key": key,
                "prefix": prefix,
                "reason": "Object key does not match any allowed prefix",
                "version_id": version_id,
            },
        )

    def object_rejected_deletion_failed(
        self,
        *,
        bucket: str,
        key: str,
        prefix: str,
        error: str,
        version_id: str | None = None,
    ) -> None:
        """Object outside allowed paths — immediate deletion failed."""
        self._put_event(
            "ObjectRejected.DeletionFailed",
            {
                "bucket": bucket,
                "key": key,
                "prefix": prefix,
                "error": error,
                "version_id": version_id,
            },
        )

    def schedule_created(
        self,
        *,
        bucket: str,
        key: str,
        prefix: str,
        schedule_name: str,
        schedule_expression: str,
        ttl_seconds: int,
        expire_at_override: bool = False,
        version_id: str | None = None,
    ) -> None:
        """EventBridge schedule created for future deletion."""
        self._put_event(
            "ScheduleCreated",
            {
                "bucket": bucket,
                "key": key,
                "prefix": prefix,
                "schedule_name": schedule_name,
                "schedule_expression": schedule_expression,
                "ttl_seconds": ttl_seconds,
                "expire_at_override": expire_at_override,
                "version_id": version_id,
            },
        )

    def schedule_creation_failed(
        self,
        *,
        bucket: str,
        key: str,
        prefix: str,
        error: str,
        version_id: str | None = None,
    ) -> None:
        """EventBridge schedule creation failed."""
        self._put_event(
            "ScheduleCreationFailed",
            {
                "bucket": bucket,
                "key": key,
                "prefix": prefix,
                "error": error,
                "version_id": version_id,
            },
        )

    def metadata_expire_at_override(
        self,
        *,
        bucket: str,
        key: str,
        prefix: str,
        expire_at: str,
        default_ttl_seconds: int,
        version_id: str | None = None,
    ) -> None:
        """Object's expireAt metadata overrides the default TTL."""
        self._put_event(
            "MetadataExpireAtOverride",
            {
                "bucket": bucket,
                "key": key,
                "prefix": prefix,
                "expire_at": expire_at,
                "default_ttl_seconds": default_ttl_seconds,
                "version_id": version_id,
            },
        )
