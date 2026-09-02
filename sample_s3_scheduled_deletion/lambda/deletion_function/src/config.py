# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Centralised configuration loading from environment variables."""

import os


def get_data_event_bus_name() -> str:
    """Return the ``DATA_EVENT_BUS_NAME`` environment variable."""
    return os.environ["DATA_EVENT_BUS_NAME"]
