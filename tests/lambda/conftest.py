# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared fixtures for Lambda unit tests.

Mocks AWS credentials and boto3 clients to prevent any real AWS API
calls during test execution.
"""

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch: pytest.MonkeyPatch):
    """Mock AWS credentials and common Lambda environment variables.

    Prevents accidental API calls if real credentials are present in
    the environment. ``POWERTOOLS_SERVICE_NAME`` is removed because the
    stack does not set it: with it, every metric gains a ``service``
    dimension that the deployed functions never emit.
    """
    monkeypatch.delenv("POWERTOOLS_SERVICE_NAME", raising=False)
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


@pytest.fixture
def emitted_metrics(
    capsys: pytest.CaptureFixture[str],
) -> Callable[[], dict[str, set[tuple[str, ...]]]]:
    """Return a reader for the EMF metrics flushed to stdout so far.

    The reader maps each metric name to the dimension sets it was emitted
    with; ``()`` is a dimensionless metric. Powertools writes Embedded
    Metric Format JSON when the handler returns or raises, so this checks
    what CloudWatch would receive.
    """

    def read() -> dict[str, set[tuple[str, ...]]]:
        dimension_sets: dict[str, set[tuple[str, ...]]] = {}
        for line in capsys.readouterr().out.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or "_aws" not in record:
                continue
            for directive in record["_aws"]["CloudWatchMetrics"]:
                for metric in directive["Metrics"]:
                    dimension_sets.setdefault(metric["Name"], set()).update(
                        tuple(names) for names in directive["Dimensions"]
                    )
        return dimension_sets

    return read


@pytest.fixture
def published_event(
    mock_boto3: dict[str, MagicMock],
) -> Callable[[str], dict[str, Any]]:
    """Return a reader for the one event of a detail type put on the bus."""

    def read(detail_type: str) -> dict[str, Any]:
        details = [
            json.loads(entry["Detail"])
            for call in mock_boto3["client"].put_events.call_args_list
            for entry in call.kwargs["Entries"]
            if entry["DetailType"] == detail_type
        ]
        if len(details) != 1:
            pytest.fail(f"Expected one {detail_type} event, got {details}")
        return details[0]

    return read
