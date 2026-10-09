# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Data models for the Deletion Lambda."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class DeletionRequest(BaseModel):
    """Deletion request sent by an object's schedule."""

    # Pending schedules still carry the retired "source" key.
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    bucket: str = Field(min_length=1)
    key: str = Field(min_length=1)
    version_id: str | None = Field(default=None, max_length=1024)


class BucketScope(BaseModel):
    """Keys this function may delete in one bucket."""

    bucket_name: str = Field(min_length=1)
    # "" covers every key: the stack owns the whole bucket.
    key_prefixes: list[str] = Field(min_length=1)


class DeletionScope(BaseModel):
    """Parsed ``DELETION_SCOPE`` environment variable."""

    buckets: list[BucketScope]

    def covers(self, *, bucket: str, key: str) -> bool:
        """Whether *key* in *bucket* is an object the stack manages."""
        return any(
            scope.bucket_name == bucket
            and key.startswith(tuple(scope.key_prefixes))
            for scope in self.buckets
        )
