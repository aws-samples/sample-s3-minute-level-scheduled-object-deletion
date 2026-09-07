# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Data models for the Deletion Lambda."""

from enum import StrEnum

from pydantic import BaseModel, Field


class DeletionSource(StrEnum):
    """Origin of a deletion request."""

    SCHEDULED = "scheduled"
    ON_DEMAND = "onDemand"


class DeletionRequest(BaseModel):
    """Unified deletion request for both scheduled and on-demand deletions."""

    bucket: str = Field(min_length=1)
    key: str = Field(min_length=1)
    source: DeletionSource
    version_id: str | None = Field(default=None, max_length=1024)
