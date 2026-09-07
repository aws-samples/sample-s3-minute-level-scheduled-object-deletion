# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""S3 adapter for reading object metadata and deleting objects."""

import boto3
from aws_lambda_powertools import Logger

logger = Logger(child=True)


class S3Repository:
    """Reads S3 object metadata and deletes objects for the retention flow."""

    @staticmethod
    def get_user_metadata(
        *, bucket: str, key: str, version_id: str | None = None
    ) -> dict[str, str]:
        """Return the user-defined metadata dict for an S3 object.

        Uses ``HeadObject`` which is lightweight (no body transfer).
        When ``version_id`` is provided, retrieves metadata for that
        specific object version.

        Returns an empty dict if the call fails (e.g. the object was deleted
        between the notification and this call, or the function's role lacks
        ``s3:GetObject``). The caller then falls back to the rule's default
        TTL, so the exception is logged to keep that fallback visible instead
        of silent.
        """
        client = boto3.client("s3")
        try:
            if version_id is not None:
                response = client.head_object(
                    Bucket=bucket, Key=key, VersionId=version_id
                )
            else:
                response = client.head_object(Bucket=bucket, Key=key)
            return response.get("Metadata", {})
        except client.exceptions.ClientError:
            logger.exception(
                "HeadObject failed — continuing without user metadata",
                extra={
                    "bucket": bucket,
                    "object_key": key,
                    "version_id": version_id,
                },
            )
            return {}

    @staticmethod
    def delete_object(
        *, bucket: str, key: str, version_id: str | None = None
    ) -> None:
        """Delete an S3 object (or a specific version).

        Used for immediate removal of objects written outside allowed
        prefixes. When ``version_id`` is provided, permanently deletes
        that specific version. Silently succeeds if the object no longer
        exists.
        """
        client = boto3.client("s3")
        if version_id is not None:
            client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
        else:
            client.delete_object(Bucket=bucket, Key=key)
