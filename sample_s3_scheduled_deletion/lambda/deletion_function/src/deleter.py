# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deletion handler for scheduled and on-demand S3 object removal.

Handles two trigger sources through a unified event model:
- **Scheduled**: EventBridge schedule fires after TTL expiry.
- **On-demand**: an application component requests immediate deletion.

Deletes the S3 object and emits CloudWatch metrics and EventBridge events.
"""

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import ClientError
from src.adapters.eventbridge_adapter import EventPublisher
from src.adapters.s3_adapter import S3Repository
from src.config import get_data_event_bus_name
from src.models import DeletionRequest, DeletionSource

logger = Logger(child=True)
metrics = Metrics()


def _extract_path_prefix(key: str) -> str:
    """Extract the path prefix (first segment) from an S3 object key.

    Raises:
        ValueError: If the key does not contain a ``/`` separator.
    """
    parts = key.split("/", 1)
    if len(parts) < 2:
        raise ValueError(
            f"Cannot derive path_prefix: key has no '/' separator: {key}"
        )
    return f"{parts[0]}/"


def _report_failure(
    event_publisher: EventPublisher,
    *,
    bucket: str,
    key: str,
    source: str,
    error: str,
    version_id: str | None,
) -> None:
    """Emit the failure event and metric for a deletion that failed."""
    event_publisher.deletion_failed_error(
        bucket=bucket,
        key=key,
        source=source,
        error=error,
        version_id=version_id,
    )
    metrics.add_metric(name="DeletionError", unit=MetricUnit.Count, value=1)


def handle_deletion(request: DeletionRequest) -> dict | None:
    """Delete an S3 object and emit metrics and events.

    Returns:
        A success dict for on-demand invocations, ``None`` for scheduled.

    Raises:
        Exception: Re-raised after logging so the Lambda runtime routes
            the event to the DLQ.
    """
    bucket = request.bucket
    key = request.key
    source = request.source
    version_id = request.version_id

    logger.info(
        "Processing deletion request",
        extra={
            "bucket": bucket,
            "source": source,
            "version_id": version_id,
        },
    )

    event_publisher = EventPublisher(get_data_event_bus_name())
    event_publisher.deletion_triggered(
        bucket=bucket, key=key, source=source, version_id=version_id
    )

    try:
        path_prefix = _extract_path_prefix(key)
    except ValueError as exc:
        logger.exception(
            "Cannot derive path prefix from object key",
            extra={
                "bucket": bucket,
                "source": source,
                "version_id": version_id,
            },
        )
        _report_failure(
            event_publisher,
            bucket=bucket,
            key=key,
            source=source,
            error=str(exc),
            version_id=version_id,
        )
        raise

    try:
        if not S3Repository.object_exists(
            bucket=bucket, key=key, version_id=version_id
        ):
            logger.warning(
                "Object does not exist — skipping deletion",
                extra={
                    "bucket": bucket,
                    "key": key,
                    "source": source,
                    "version_id": version_id,
                },
            )
            event_publisher.deletion_failed_object_missing(
                bucket=bucket, key=key, source=source, version_id=version_id
            )
            metrics.add_dimension(name="Path", value=path_prefix)
            metrics.add_dimension(name="Type", value=source)
            metrics.add_metric(
                name="DuplicateDeletion", unit=MetricUnit.Count, value=1
            )
            if source == DeletionSource.ON_DEMAND:
                return {
                    "statusCode": 200,
                    "body": {
                        "message": "Object already deleted",
                        "key": key,
                    },
                }
            return None

        S3Repository.delete_object(
            bucket=bucket, key=key, version_id=version_id
        )

        event_publisher.deletion_succeeded(
            bucket=bucket,
            key=key,
            source=source,
            prefix=path_prefix,
            version_id=version_id,
        )
        metrics.add_dimension(name="Path", value=path_prefix)
        metrics.add_dimension(name="Type", value=source)
        metrics.add_metric(
            name="ObjectDeletion", unit=MetricUnit.Count, value=1
        )

        logger.info(
            "Deletion completed successfully",
            extra={
                "bucket": bucket,
                "source": source,
                "version_id": version_id,
            },
        )

    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        error_msg = str(exc)
        logger.exception(
            "Deletion failed",
            extra={
                "bucket": bucket,
                "source": source,
                "error_code": error_code,
                "version_id": version_id,
            },
        )
        _report_failure(
            event_publisher,
            bucket=bucket,
            key=key,
            source=source,
            error=error_msg,
            version_id=version_id,
        )
        raise

    except Exception as exc:
        logger.exception(
            "Deletion failed",
            extra={
                "bucket": bucket,
                "source": source,
                "version_id": version_id,
            },
        )
        _report_failure(
            event_publisher,
            bucket=bucket,
            key=key,
            source=source,
            error=str(exc),
            version_id=version_id,
        )
        raise

    if source == DeletionSource.ON_DEMAND:
        return {
            "statusCode": 200,
            "body": {"message": "Object deleted successfully", "key": key},
        }
    return None
