# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""S3 adapter for reading object metadata and deleting objects."""

import boto3
from botocore.exceptions import ClientError
from src.models import MetadataLookupError, ObjectMetadata


def _version(version_id: str | None) -> dict[str, str]:
    """The ``VersionId`` argument, only when there is a version."""
    return {"VersionId": version_id} if version_id is not None else {}


class S3Repository:
    """Reads S3 object metadata and deletes objects for the retention flow."""

    @staticmethod
    def get_user_metadata(
        *, bucket: str, key: str, version_id: str | None = None
    ) -> ObjectMetadata:
        """Read the object's user metadata with ``HeadObject``.

        Raises:
            MetadataLookupError: If ``HeadObject`` fails, for example
                because it was throttled or the object is already gone.
        """
        try:
            response = boto3.client("s3").head_object(
                Bucket=bucket, Key=key, **_version(version_id)
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            raise MetadataLookupError(
                f"HeadObject failed with {code}"
            ) from exc
        return ObjectMetadata.from_s3_head_object_call(response)

    @staticmethod
    def delete_object(
        *, bucket: str, key: str, version_id: str | None = None
    ) -> None:
        """Delete the object, permanently when *version_id* is given."""
        boto3.client("s3").delete_object(
            Bucket=bucket, Key=key, **_version(version_id)
        )
