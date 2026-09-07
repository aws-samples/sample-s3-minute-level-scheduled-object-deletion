# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Data models for the Lifecycle Lambda."""

import re
from urllib.parse import unquote_plus

from aws_lambda_powertools.utilities.parser.models import (
    S3EventNotificationEventBridgeModel,
)
from pydantic import BaseModel, Field

# Relaxed pattern: the key only needs a leading path prefix segment, e.g.
# "uploads/anything/here.pdf".  The prefix is the first path segment
# (including its trailing slash) and is used for longest-prefix matching.
_KEY_PATTERN = re.compile(r"^(?P<prefix>[^/]+/).+")


class FolderMarkerEvent(Exception):
    """Raised when an S3 event refers to a folder-marker object.

    A folder marker is a zero-byte object whose key ends in ``/`` (e.g.
    ``retention-5-mins/``). These are created when a "folder"/prefix is
    created in the console or via an empty object, and are not actionable
    files. Callers should skip them rather than treat them as errors.
    """


class RepositoryEntry(BaseModel):
    """A single bucket/prefix retention rule from REPOSITORY_CONFIG."""

    bucket_name: str = Field(min_length=1)
    path_prefix: str = Field(min_length=1)
    ttl_seconds: int = Field(gt=0)


class RepositoryConfig(BaseModel):
    """Parsed REPOSITORY_CONFIG environment variable."""

    repositories: list[RepositoryEntry]


class S3ObjectCreatedEvent(BaseModel):
    """Parsed S3 ObjectCreated event notification.

    Use :meth:`from_eventbridge_model` to build from a Powertools-parsed
    EventBridge S3 notification model.
    """

    bucket_name: str = Field(min_length=1)
    object_key: str = Field(min_length=1)
    key_prefix: str = Field(min_length=1)
    version_id: str | None = None

    @classmethod
    def from_eventbridge_model(
        cls, model: S3EventNotificationEventBridgeModel
    ) -> "S3ObjectCreatedEvent":
        """Build a validated domain event from a parsed Powertools model.

        The EventBridge envelope and S3 ``detail`` shape are already
        validated by ``S3EventNotificationEventBridgeModel``; this method
        applies the domain-specific rules (folder-marker handling and
        leading path-prefix extraction).

        The key must contain at least one ``/`` so a leading prefix can be
        derived.

        When S3 versioning is enabled, the ``version-id`` field from the
        event detail is extracted. The literal string ``"null"`` (used by
        S3 for non-versioned buckets) is normalised to ``None``.

        Raises:
            FolderMarkerEvent: If the key is a folder marker (ends in ``/``).
            ValueError: If the key has no path prefix.
        """
        bucket_name = model.detail.bucket.name
        object_key = unquote_plus(model.detail.object.key)

        # Folder-marker objects (zero-byte keys ending in "/") are not
        # actionable files; signal to the caller to skip them gracefully.
        if object_key.endswith("/"):
            raise FolderMarkerEvent(object_key)

        match = _KEY_PATTERN.match(object_key)
        if not match:
            raise ValueError(
                f"Object key has no path prefix (expected '{{prefix}}/...'): "
                f"{object_key}"
            )

        # Extract version-id when present (versioned buckets).
        # S3 uses the literal string "null" for non-versioned buckets.
        raw_version_id = getattr(model.detail.object, "version_id", None)
        if not raw_version_id or raw_version_id == "null":
            version_id = None
        else:
            version_id = raw_version_id

        return cls(
            bucket_name=bucket_name,
            object_key=object_key,
            key_prefix=match.group("prefix"),
            version_id=version_id,
        )
