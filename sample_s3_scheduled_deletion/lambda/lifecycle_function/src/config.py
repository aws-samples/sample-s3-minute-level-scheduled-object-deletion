# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Centralised configuration loading from environment variables."""

import json
import os

from src.models import RepositoryConfig, RepositoryEntry


def get_repository_config() -> list[RepositoryEntry]:
    """Parse ``REPOSITORY_CONFIG`` env var and return entries sorted by
    prefix length descending (longest-prefix match first)."""

    raw = os.environ["REPOSITORY_CONFIG"]
    config = RepositoryConfig.model_validate(json.loads(raw))
    return sorted(
        config.repositories,
        key=lambda r: len(r.path_prefix),
        reverse=True,
    )


def get_project_prefix() -> str:
    """Return the ``PROJECT_PREFIX`` environment variable."""
    return os.environ["PROJECT_PREFIX"]


def get_schedule_group_name() -> str:
    """Return the ``SCHEDULE_GROUP_NAME`` environment variable."""
    return os.environ["SCHEDULE_GROUP_NAME"]


def get_schedule_role_arn() -> str:
    """Return the ``SCHEDULE_ROLE_ARN`` environment variable."""
    return os.environ["SCHEDULE_ROLE_ARN"]


def get_deletion_function_arn() -> str:
    """Return the ``DELETION_FUNCTION_ARN`` environment variable."""
    return os.environ["DELETION_FUNCTION_ARN"]


def get_data_event_bus_name() -> str:
    """Return the ``DATA_EVENT_BUS_NAME`` environment variable."""
    return os.environ["DATA_EVENT_BUS_NAME"]


def get_dlq_arn() -> str:
    """Return the ``DLQ_ARN`` environment variable."""
    return os.environ["DLQ_ARN"]


def get_max_retention_seconds() -> int:
    """Return the ``MAX_RETENTION_SECONDS`` environment variable."""
    return int(os.environ["MAX_RETENTION_SECONDS"])
