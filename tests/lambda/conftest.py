# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared fixtures for Lambda unit tests.

Mocks AWS credentials and boto3 clients to prevent any real AWS API
calls during test execution.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch: pytest.MonkeyPatch):
    """Mock AWS credentials and common Lambda environment variables.

    Prevents accidental API calls if real credentials are present in
    the environment.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@pytest.fixture(autouse=True)
def mock_boto3(monkeypatch):
    """Mock boto3.client and boto3.resource globally.

    Returns a dict with the mock objects for assertions in tests:
        {"client": <MagicMock>, "resource": <MagicMock>}
    """
    client_mock = MagicMock()
    boto3_client_mock = MagicMock(return_value=client_mock)
    monkeypatch.setattr("boto3.client", boto3_client_mock)

    resource_mock = MagicMock()
    boto3_resource_mock = MagicMock(return_value=resource_mock)
    monkeypatch.setattr("boto3.resource", boto3_resource_mock)

    return {"client": client_mock, "resource": resource_mock}
