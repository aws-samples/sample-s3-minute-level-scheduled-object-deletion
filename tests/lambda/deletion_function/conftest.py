# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fixtures specific to the Deletion Lambda function tests."""

import json

import pytest

# A bucket the stack owns: every key is in scope.
BUCKET_NAME = "amzn-s3-demo-bucket"
# A bucket shared with other users: only the managed prefix is in scope.
SHARED_BUCKET_NAME = "amzn-s3-demo-shared-bucket"
SHARED_PREFIX = "managed/"


@pytest.fixture(autouse=True)
def deletion_env_vars(monkeypatch: pytest.MonkeyPatch):
    """Set environment variables expected by the Deletion Lambda."""
    monkeypatch.setenv(
        "DATA_EVENT_BUS_NAME",
        "sample-s3-scheduled-deletion-deletion-events",
    )
    monkeypatch.setenv(
        "DELETION_SCOPE",
        json.dumps(
            {
                "buckets": [
                    {"bucket_name": BUCKET_NAME, "key_prefixes": [""]},
                    {
                        "bucket_name": SHARED_BUCKET_NAME,
                        "key_prefixes": [SHARED_PREFIX],
                    },
                ]
            }
        ),
    )
    monkeypatch.setenv("POWERTOOLS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv(
        "POWERTOOLS_METRICS_NAMESPACE", "SampleS3ScheduledDeletion"
    )
