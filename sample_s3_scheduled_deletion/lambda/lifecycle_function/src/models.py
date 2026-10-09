# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Data models for the Lifecycle Lambda."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Self

from aws_lambda_powertools.utilities.parser.models import (
    S3EventNotificationEventBridgeModel,
)
from pydantic import BaseModel, ConfigDict, Field

# The key's first path segment, including its trailing slash: "uploads/" in
# "uploads/anything/here.pdf". It labels logs and events; rule matching
# compares the whole key against each rule's prefix.
_KEY_PATTERN = re.compile(r"^(?P<prefix>[^/]+/).+")


class FolderMarkerEvent(Exception):
    """Raised for a folder marker, a key ending in ``/``; callers skip it."""


class RepositoryEntry(BaseModel):
    """A single bucket/prefix retention rule from REPOSITORY_CONFIG."""

    bucket_name: str = Field(min_length=1)
    path_prefix: str = Field(min_length=1)
    ttl_seconds: int = Field(gt=0)
    delete_unmatched: bool = False


@dataclass(frozen=True)
class ScheduleTimeResolution:
    """When an object should be deleted, and how that was decided."""

    schedule_time: datetime  # always UTC
    expire_at_override: bool = False  # expireAt used instead of the TTL
    clamped_to_max: bool = False
    clamped_to_min: bool = False

    def seconds_until_deletion(self, now: datetime) -> int:
        """Seconds from *now* until the resolved deletion time."""
        return int((self.schedule_time - now).total_seconds())


class InvalidExpireAtError(ValueError):
    """Raised when expireAt metadata is not an ISO 8601 timestamp."""


class MetadataLookupError(Exception):
    """Raised when an object's user metadata cannot be read."""


class ObjectMetadata(BaseModel):
    """S3 user metadata relevant to Lifecycle scheduling."""

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        validate_by_name=True,
    )

    expire_at: str | None = Field(
        default=None,
        validation_alias="expireat",
    )

    def parse_expire_at(self) -> datetime | None:
        """Return ``expireAt`` as an aware UTC timestamp.

        Raises:
            InvalidExpireAtError: If a present ``expireAt`` value is not an
                ISO 8601 timestamp.
        """
        if self.expire_at is None:
            return None

        try:
            parsed = datetime.fromisoformat(self.expire_at)
        except ValueError as exc:
            raise InvalidExpireAtError(
                "expireAt must be an ISO 8601 timestamp"
            ) from exc

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    @classmethod
    def from_s3_head_object_call(cls, response: Mapping[str, Any]) -> Self:
        """Build from the relevant user metadata in an S3 HeadObject response."""
        return cls.model_validate(response.get("Metadata", {}))


class RepositoryConfig(BaseModel):
    """Parsed REPOSITORY_CONFIG environment variable."""

    repositories: list[RepositoryEntry]


class S3ObjectCreatedEvent(BaseModel):
    """An S3 ``Object Created`` event; see :meth:`from_eventbridge_model`."""

    bucket_name: str = Field(min_length=1)
    object_key: str = Field(min_length=1)
    key_prefix: str | None = None  # None for a key at the bucket root
    version_id: str | None = None

    @classmethod
    def from_eventbridge_model(
        cls, model: S3EventNotificationEventBridgeModel
    ) -> "S3ObjectCreatedEvent":
        """Build the event from a model Powertools has already validated.

        A key at the bucket root gets ``key_prefix=None`` and matches no
        rule. The ``"null"`` version ID of a non-versioned bucket becomes
        ``None``.

        Raises:
            FolderMarkerEvent: If the key ends in ``/``.
        """
        bucket_name = model.detail.bucket.name
        # EventBridge delivers the key as stored, unlike the URL-encoded key
        # of S3 event notifications, so it must not be decoded.
        object_key = model.detail.object.key

        if object_key.endswith("/"):
            raise FolderMarkerEvent(object_key)

        match = _KEY_PATTERN.match(object_key)
        raw_version_id = getattr(model.detail.object, "version_id", None)
        if not raw_version_id or raw_version_id == "null":
            version_id = None
        else:
            version_id = raw_version_id

        return cls(
            bucket_name=bucket_name,
            object_key=object_key,
            key_prefix=match.group("prefix") if match else None,
            version_id=version_id,
        )
