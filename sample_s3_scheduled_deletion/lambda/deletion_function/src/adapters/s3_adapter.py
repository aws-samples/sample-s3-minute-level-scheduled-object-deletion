# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""S3 adapter for object deletion."""

import boto3
from botocore.exceptions import ClientError


class S3Repository:
    """Handles S3 object operations for the retention system."""

    @staticmethod
    def object_exists(
        *, bucket: str, key: str, version_id: str | None = None
    ) -> bool:
        """Check if an S3 object (or specific version) exists using HeadObject.

        Returns ``True`` if the object exists, ``False`` if it does not.
        Raises on unexpected errors.
        """
        client = boto3.client("s3")
        try:
            if version_id is not None:
                client.head_object(
                    Bucket=bucket, Key=key, VersionId=version_id
                )
            else:
                client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in (
                "404",
                "NoSuchKey",
                "MethodNotAllowed",
            ):
                return False
            raise

    @staticmethod
    def delete_object(
        *, bucket: str, key: str, version_id: str | None = None
    ) -> None:
        """Delete an S3 object (or a specific version).

        When ``version_id`` is provided, permanently deletes that specific
        version (bypasses delete markers on versioned buckets).
        S3 ``DeleteObject`` is idempotent — it returns success even if the
        object has already been deleted.
        """
        client = boto3.client("s3")
        if version_id is not None:
            client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
        else:
            client.delete_object(Bucket=bucket, Key=key)
