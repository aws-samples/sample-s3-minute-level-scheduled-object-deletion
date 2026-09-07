# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fixtures specific to the Deletion Lambda function tests."""

import pytest


@pytest.fixture(autouse=True)
def deletion_env_vars(monkeypatch: pytest.MonkeyPatch):
    """Set environment variables expected by the Deletion Lambda."""
    monkeypatch.setenv(
        "DATA_EVENT_BUS_NAME",
        "sample-s3-scheduled-deletion-deletion-events",
    )
    monkeypatch.setenv("POWERTOOLS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv(
        "POWERTOOLS_METRICS_NAMESPACE", "SampleS3ScheduledDeletion"
    )
