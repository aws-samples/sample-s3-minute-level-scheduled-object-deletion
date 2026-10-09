# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Centralised configuration loading from environment variables."""

import os

from src.models import DeletionScope


def get_data_event_bus_name() -> str:
    """Return the ``DATA_EVENT_BUS_NAME`` environment variable."""
    return os.environ["DATA_EVENT_BUS_NAME"]


def get_deletion_scope() -> DeletionScope:
    """Parse the ``DELETION_SCOPE`` environment variable."""
    return DeletionScope.model_validate_json(os.environ["DELETION_SCOPE"])
