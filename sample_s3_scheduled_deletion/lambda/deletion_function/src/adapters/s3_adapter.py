# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""S3 adapter for object deletion."""

import boto3
from botocore.exceptions import ClientError


def _version(version_id: str | None) -> dict[str, str]:
    """The ``VersionId`` argument, only when there is a version."""
    return {"VersionId": version_id} if version_id is not None else {}


class S3Repository:
    """Handles S3 object operations for the retention system."""

    @staticmethod
    def object_exists(
        *, bucket: str, key: str, version_id: str | None = None
    ) -> bool:
        """Whether the object exists; any error other than not-found raises."""
        try:
            boto3.client("s3").head_object(
                Bucket=bucket, Key=key, **_version(version_id)
            )
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
        """Delete the object, permanently when *version_id* is given."""
        boto3.client("s3").delete_object(
            Bucket=bucket, Key=key, **_version(version_id)
        )
