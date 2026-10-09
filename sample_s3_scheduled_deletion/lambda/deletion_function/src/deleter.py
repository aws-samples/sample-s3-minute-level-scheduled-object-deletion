# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Delete an object when its schedule fires.

Requests outside the managed scope (``DELETION_SCOPE``) are rejected.
"""

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from src.adapters.eventbridge_adapter import EventPublisher
from src.adapters.s3_adapter import S3Repository
from src.config import get_data_event_bus_name, get_deletion_scope
from src.models import DeletionRequest

logger = Logger(child=True)
metrics = Metrics()


def _extract_path_prefix(key: str) -> str | None:
    """First path segment of *key*, for metric labels; ``None`` at the root."""
    segment, separator, _ = key.partition("/")
    return f"{segment}/" if separator else None


def _add_path_dimension(path_prefix: str | None) -> None:
    """Label the deletion metrics; a root-level key has no ``Path``."""
    if path_prefix is not None:
        metrics.add_dimension(name="Path", value=path_prefix)


def _reject(event_publisher: EventPublisher, request: DeletionRequest) -> None:
    """Refuse an out-of-scope request without raising, so it isn't retried."""
    logger.warning(
        "Object is outside the managed scope, deletion rejected",
        extra={
            "bucket": request.bucket,
            "key": request.key,
            "version_id": request.version_id,
        },
    )
    event_publisher.deletion_rejected(
        bucket=request.bucket,
        key=request.key,
        version_id=request.version_id,
    )
    metrics.add_metric(name="DeletionRejected", unit=MetricUnit.Count, value=1)


def handle_deletion(request: DeletionRequest) -> None:
    """Delete the requested object and report the outcome.

    A failure is re-raised so the event is retried, then sent to the DLQ.
    """
    bucket = request.bucket
    key = request.key
    version_id = request.version_id

    logger.info(
        "Processing deletion request",
        extra={
            "bucket": bucket,
            "version_id": version_id,
        },
    )

    event_publisher = EventPublisher(get_data_event_bus_name())

    if not get_deletion_scope().covers(bucket=bucket, key=key):
        _reject(event_publisher, request)
        return

    event_publisher.deletion_triggered(
        bucket=bucket, key=key, version_id=version_id
    )

    path_prefix = _extract_path_prefix(key)

    try:
        if not S3Repository.object_exists(
            bucket=bucket, key=key, version_id=version_id
        ):
            logger.warning(
                "Object does not exist — skipping deletion",
                extra={
                    "bucket": bucket,
                    "key": key,
                    "version_id": version_id,
                },
            )
            event_publisher.deletion_failed_object_missing(
                bucket=bucket, key=key, version_id=version_id
            )
            _add_path_dimension(path_prefix)
            metrics.add_metric(
                name="DuplicateDeletion", unit=MetricUnit.Count, value=1
            )
            return

        S3Repository.delete_object(
            bucket=bucket, key=key, version_id=version_id
        )

        event_publisher.deletion_succeeded(
            bucket=bucket,
            key=key,
            prefix=path_prefix,
            version_id=version_id,
        )
        _add_path_dimension(path_prefix)
        metrics.add_metric(
            name="ObjectDeletion", unit=MetricUnit.Count, value=1
        )

        logger.info(
            "Deletion completed successfully",
            extra={
                "bucket": bucket,
                "version_id": version_id,
            },
        )

    except Exception as exc:
        # The traceback carries the S3 error code for a ClientError.
        logger.exception(
            "Deletion failed",
            extra={
                "bucket": bucket,
                "version_id": version_id,
            },
        )
        event_publisher.deletion_failed_error(
            bucket=bucket,
            key=key,
            error=str(exc),
            version_id=version_id,
        )
        # No dimension is added on this path, which is what the
        # DeletionError alarm watches.
        metrics.add_metric(
            name="DeletionError", unit=MetricUnit.Count, value=1
        )
        raise
