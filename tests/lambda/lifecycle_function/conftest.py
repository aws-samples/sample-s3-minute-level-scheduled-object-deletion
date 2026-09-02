# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fixtures specific to the Lifecycle Lambda function tests."""

import json

import pytest


@pytest.fixture(autouse=True)
def lifecycle_env_vars(monkeypatch: pytest.MonkeyPatch):
    """Set environment variables expected by the Lifecycle Lambda."""
    monkeypatch.setenv(
        "REPOSITORY_CONFIG",
        json.dumps(
            {
                "repositories": [
                    {
                        "bucket_name": "sample-s3-scheduled-deletion-data-abc123",
                        "path_prefix": "retention-5-mins/",
                        "ttl_seconds": 300,
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
    monkeypatch.setenv("POWERTOOLS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv(
        "POWERTOOLS_METRICS_NAMESPACE", "SampleS3ScheduledDeletion"
    )
