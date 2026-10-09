# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fixtures specific to the Lifecycle Lambda function tests."""

import json

import pytest

BUCKET_NAME = "sample-s3-scheduled-deletion-data-abc123"
PATH_PREFIX = "retention-5-mins/"
TTL_SECONDS = 300

# Mirrors the stack default of a 1-day Lifecycle expiration.
MAX_RETENTION_SECONDS = 86400


@pytest.fixture(autouse=True)
def lifecycle_env_vars(monkeypatch: pytest.MonkeyPatch):
    """Set environment variables expected by the Lifecycle Lambda."""
    monkeypatch.setenv(
        "REPOSITORY_CONFIG",
        json.dumps(
            {
                "repositories": [
                    {
                        "bucket_name": BUCKET_NAME,
                        "path_prefix": PATH_PREFIX,
                        "ttl_seconds": TTL_SECONDS,
                        # Mirrors the sample's own EXCLUSIVE bucket.
                        "delete_unmatched": True,
                    }
                ]
            }
        ),
    )
    monkeypatch.setenv("PROJECT_PREFIX", "sample-s3-scheduled-deletion")
    monkeypatch.setenv(
        "SCHEDULE_GROUP_NAME",
        "sample-s3-scheduled-deletion-deletion-schedules",
    )
    monkeypatch.setenv(
        "SCHEDULE_ROLE_ARN",
        "arn:aws:iam::111122223333:role/sample-s3-scheduled-deletion-schedule-role",
    )
    monkeypatch.setenv(
        "DELETION_FUNCTION_ARN",
        "arn:aws:lambda:aa-example-1:111122223333:function:sample-s3-scheduled-deletion-deletion-fn",
    )
    monkeypatch.setenv(
        "DATA_EVENT_BUS_NAME",
        "sample-s3-scheduled-deletion-deletion-events",
    )
    monkeypatch.setenv(
        "DLQ_ARN",
        "arn:aws:sqs:aa-example-1:111122223333:sample-s3-scheduled-deletion-deletion-dlq",
    )
    monkeypatch.setenv("MAX_RETENTION_SECONDS", str(MAX_RETENTION_SECONDS))
    monkeypatch.setenv("POWERTOOLS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv(
        "POWERTOOLS_METRICS_NAMESPACE", "SampleS3ScheduledDeletion"
    )
