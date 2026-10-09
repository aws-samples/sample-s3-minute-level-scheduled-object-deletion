# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the Lifecycle Lambda S3 adapter."""

import pytest
from botocore.exceptions import ClientError
from src.adapters.s3_adapter import S3Repository
from src.models import MetadataLookupError, ObjectMetadata


def test_get_user_metadata_maps_expire_at(mock_boto3):
    """The adapter exposes the scheduling field, not the AWS response map."""
    client_mock = mock_boto3["client"]
    client_mock.head_object.return_value = {
        "Metadata": {
            "expireat": "2026-09-12T12:00:00Z",
            "unrelated": "value",
        }
    }

    metadata = S3Repository.get_user_metadata(
        bucket="example-bucket",
        key="retention-5-mins/test.txt",
    )

    assert metadata == ObjectMetadata(expire_at="2026-09-12T12:00:00Z")
    client_mock.head_object.assert_called_once_with(
        Bucket="example-bucket",
        Key="retention-5-mins/test.txt",
    )


def test_get_user_metadata_returns_empty_model_without_metadata(mock_boto3):
    client_mock = mock_boto3["client"]
    client_mock.head_object.return_value = {}

    metadata = S3Repository.get_user_metadata(
        bucket="example-bucket",
        key="retention-5-mins/test.txt",
    )

    assert metadata == ObjectMetadata()


def test_get_user_metadata_reads_the_requested_object_version(mock_boto3):
    client_mock = mock_boto3["client"]
    client_mock.head_object.return_value = {"Metadata": {}}

    S3Repository.get_user_metadata(
        bucket="example-bucket",
        key="retention-5-mins/test.txt",
        version_id="version-123",
    )

    client_mock.head_object.assert_called_once_with(
        Bucket="example-bucket",
        Key="retention-5-mins/test.txt",
        VersionId="version-123",
    )


def test_get_user_metadata_reports_a_failed_read(mock_boto3):
    """A failed read must not look like an object without metadata."""
    client_mock = mock_boto3["client"]
    client_mock.head_object.side_effect = ClientError(
        {"Error": {"Code": "SlowDown", "Message": "Please reduce"}},
        "HeadObject",
    )

    with pytest.raises(MetadataLookupError, match="SlowDown"):
        S3Repository.get_user_metadata(
            bucket="example-bucket",
            key="retention-5-mins/test.txt",
        )


def test_object_metadata_deserializes_only_s3_user_metadata():
    metadata = ObjectMetadata.from_s3_head_object_call(
        {
            "ContentLength": 42,
            "ETag": '"example-etag"',
            "Metadata": {
                "expireat": "2026-09-12T12:00:00Z",
                "unrelated": "value",
            },
        }
    )

    assert metadata == ObjectMetadata(expire_at="2026-09-12T12:00:00Z")


def test_object_metadata_defaults_when_s3_user_metadata_is_absent():
    assert ObjectMetadata.from_s3_head_object_call({}) == ObjectMetadata()
