# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Schedule the deletion of each object created in a managed bucket.

An object that matches no retention rule is deleted on an ``EXCLUSIVE``
bucket and left alone on a ``SHARED`` one.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit, single_metric
from aws_lambda_powertools.utilities.parser.models import (
    S3EventNotificationEventBridgeModel,
)
from src.adapters.eventbridge_adapter import EventPublisher
from src.adapters.s3_adapter import S3Repository
from src.adapters.scheduler_repository import SchedulerRepository
from src.config import (
    get_data_event_bus_name,
    get_deletion_function_arn,
    get_dlq_arn,
    get_max_retention_seconds,
    get_project_prefix,
    get_repository_config,
    get_schedule_group_name,
    get_schedule_role_arn,
)
from src.models import (
    FolderMarkerEvent,
    InvalidExpireAtError,
    MetadataLookupError,
    ObjectMetadata,
    RepositoryEntry,
    S3ObjectCreatedEvent,
    ScheduleTimeResolution,
)

logger = Logger(child=True)
metrics = Metrics()

# One-time schedules have 60-second precision; keep a buffer on top of that.
MIN_LEAD_SECONDS = 120


def _match_path_prefix(
    bucket_name: str,
    object_key: str,
    repos: list[RepositoryEntry],
) -> RepositoryEntry | None:
    """Return the first matching repository entry for bucket/key.

    *repos* must be sorted by prefix length descending (longest-prefix
    match first), as returned by :func:`get_repository_config`.
    """
    for repo in repos:
        if repo.bucket_name == bucket_name and object_key.startswith(
            repo.path_prefix
        ):
            return repo
    return None


def _deletes_unmatched(
    bucket_name: str,
    repos: list[RepositoryEntry],
) -> bool:
    """Whether *bucket_name* deletes unmatched objects (``EXCLUSIVE``)."""
    return any(
        repo.delete_unmatched
        for repo in repos
        if repo.bucket_name == bucket_name
    )


def _count_failure(name: str) -> None:
    """Emit the failure metric *name* without the invocation's dimensions.

    The stack alarms on the dimensionless metric, so it must not pick up the
    ``Path`` dimension the other metrics carry.
    """
    with single_metric(name=name, unit=MetricUnit.Count, value=1):
        pass


def _read_object_metadata(
    s3_event: S3ObjectCreatedEvent,
    *,
    matched_repo: RepositoryEntry,
    event_publisher: EventPublisher,
) -> ObjectMetadata:
    """Read the object's user metadata, or none if the read fails.

    A failed read falls back to the rule's TTL instead of failing the
    invocation, and is reported as an event and a metric so that it
    cannot pass for an object uploaded without ``expireAt``.
    """
    try:
        return S3Repository.get_user_metadata(
            bucket=s3_event.bucket_name,
            key=s3_event.object_key,
            version_id=s3_event.version_id,
        )
    except MetadataLookupError as exc:
        logger.exception(
            "Cannot read object metadata, falling back to the rule's TTL",
            extra={
                "bucket": s3_event.bucket_name,
                "object_key": s3_event.object_key,
                "version_id": s3_event.version_id,
            },
        )
        event_publisher.metadata_lookup_failed(
            bucket=s3_event.bucket_name,
            key=s3_event.object_key,
            prefix=matched_repo.path_prefix,
            error=str(exc),
            default_ttl_seconds=matched_repo.ttl_seconds,
            version_id=s3_event.version_id,
        )
        _count_failure("MetadataLookupFailed")
        return ObjectMetadata()


def _build_schedule_name(
    project_prefix: str,
    bucket: str,
    object_key: str,
    version_id: str | None = None,
) -> str:
    """Return ``{project_prefix}-{hash}``, 64 characters at most.

    The hash covers the bucket, the key and the version ID if there is one,
    so each object version gets its own schedule.
    """
    hash_input = f"{bucket}:{object_key}"
    if version_id is not None:
        hash_input = f"{hash_input}:{version_id}"
    digest = hashlib.sha256(hash_input.encode()).hexdigest()
    # The stack caps PROJECT_PREFIX at 38 characters, which leaves at least
    # 25 hex characters of hash.
    return f"{project_prefix}-{digest[: 64 - len(project_prefix) - 1]}"


def _resolve_schedule_time(
    *,
    object_metadata: ObjectMetadata,
    now: datetime,
    ttl_seconds: int,
    max_retention_seconds: int,
    object_key: str,
) -> ScheduleTimeResolution:
    """Decide when an object should be deleted.

    A usable ``expireAt`` wins over the rule's default TTL, then the result
    is bounded by *max_retention_seconds* above and ``MIN_LEAD_SECONDS``
    below. The minimum is applied last, so it wins over the maximum.
    """
    max_schedule_time = now + timedelta(seconds=max_retention_seconds)
    min_schedule_time = now + timedelta(seconds=MIN_LEAD_SECONDS)

    try:
        requested = object_metadata.parse_expire_at()
    except InvalidExpireAtError:
        logger.warning(
            "Invalid expireAt metadata value, falling back to TTL",
            extra={
                "expire_at": object_metadata.expire_at,
                "object_key": object_key,
            },
        )
        requested = None

    if requested is not None and requested <= now:
        logger.warning(
            "expireAt is in the past, falling back to TTL",
            extra={
                "expire_at": object_metadata.expire_at,
                "object_key": object_key,
            },
        )
        requested = None

    expire_at_override = requested is not None
    schedule_time = (
        requested
        if requested is not None
        else now + timedelta(seconds=ttl_seconds)
    )

    clamped_to_max = schedule_time > max_schedule_time
    if clamped_to_max:
        logger.warning(
            "Requested deletion time exceeds the maximum retention "
            "ceiling, clamping",
            extra={
                "requested_schedule_time": schedule_time.isoformat(),
                "clamped_schedule_time": max_schedule_time.isoformat(),
                "max_retention_seconds": max_retention_seconds,
                "object_key": object_key,
            },
        )
        schedule_time = max_schedule_time

    clamped_to_min = schedule_time < min_schedule_time
    if clamped_to_min:
        logger.info(
            "Schedule time too close to now, clamping to minimum buffer",
            extra={
                "requested_schedule_time": schedule_time.isoformat(),
                "clamped_schedule_time": min_schedule_time.isoformat(),
                "object_key": object_key,
            },
        )
        schedule_time = min_schedule_time

    return ScheduleTimeResolution(
        schedule_time=schedule_time,
        expire_at_override=expire_at_override,
        clamped_to_max=clamped_to_max,
        clamped_to_min=clamped_to_min,
    )


def handle_schedule_creation(
    event: S3EventNotificationEventBridgeModel,
) -> None:
    """Handle an S3 ObjectCreated event by scheduling a future deletion.

    Raises on error so the Lambda runtime routes the event to the DLQ.
    """
    try:
        s3_event = S3ObjectCreatedEvent.from_eventbridge_model(event)
    except FolderMarkerEvent as exc:
        logger.info(
            "Skipping folder-marker object (no action required)",
            extra={"object_key": str(exc)},
        )
        metrics.add_metric(
            name="FolderMarkerSkipped", unit=MetricUnit.Count, value=1
        )
        return

    logger.info(
        "Processing S3 object creation",
        extra={
            "bucket": s3_event.bucket_name,
            "object_key": s3_event.object_key,
            "version_id": s3_event.version_id,
        },
    )

    event_publisher = EventPublisher(get_data_event_bus_name())
    repos = get_repository_config()
    matched_repo = _match_path_prefix(
        s3_event.bucket_name,
        s3_event.object_key,
        repos,
    )

    if matched_repo is None:
        if not _deletes_unmatched(s3_event.bucket_name, repos):
            logger.info(
                "Object key does not match any configured retention rule. "
                "The bucket is shared, so the object is kept.",
                extra={
                    "bucket": s3_event.bucket_name,
                    "object_key": s3_event.object_key,
                    "prefix": s3_event.key_prefix,
                },
            )
            event_publisher.object_unmanaged(
                bucket=s3_event.bucket_name,
                key=s3_event.object_key,
                prefix=s3_event.key_prefix,
                version_id=s3_event.version_id,
            )
            metrics.add_metric(
                name="UnmanagedObjectIgnored", unit=MetricUnit.Count, value=1
            )
            return

        logger.warning(
            "Object key does not match any configured retention rule. "
            "The object is deleted immediately.",
            extra={
                "object_key": s3_event.object_key,
                "prefix": s3_event.key_prefix,
            },
        )
        try:
            S3Repository.delete_object(
                bucket=s3_event.bucket_name,
                key=s3_event.object_key,
                version_id=s3_event.version_id,
            )
            event_publisher.object_rejected_deleted(
                bucket=s3_event.bucket_name,
                key=s3_event.object_key,
                prefix=s3_event.key_prefix,
                version_id=s3_event.version_id,
            )
        except Exception as exc:
            event_publisher.object_rejected_deletion_failed(
                bucket=s3_event.bucket_name,
                key=s3_event.object_key,
                prefix=s3_event.key_prefix,
                error=str(exc),
                version_id=s3_event.version_id,
            )
            raise
        metrics.add_metric(
            name="UnauthorizedObjectDeletion", unit=MetricUnit.Count, value=1
        )
        return

    logger.info(
        "Matched retention rule",
        extra={
            "path_prefix": matched_repo.path_prefix,
            "ttl_seconds": matched_repo.ttl_seconds,
        },
    )

    # Shared by every event published below.
    event_ctx = {
        "bucket": s3_event.bucket_name,
        "key": s3_event.object_key,
        "prefix": matched_repo.path_prefix,
        "version_id": s3_event.version_id,
    }
    event_publisher.object_created(
        **event_ctx, ttl_seconds=matched_repo.ttl_seconds
    )
    metrics.add_dimension(name="Path", value=matched_repo.path_prefix)
    metrics.add_metric(name="ObjectCreation", unit=MetricUnit.Count, value=1)

    now = datetime.now(timezone.utc)
    max_retention_seconds = get_max_retention_seconds()
    object_metadata = _read_object_metadata(
        s3_event,
        matched_repo=matched_repo,
        event_publisher=event_publisher,
    )
    resolution = _resolve_schedule_time(
        object_metadata=object_metadata,
        now=now,
        ttl_seconds=matched_repo.ttl_seconds,
        max_retention_seconds=max_retention_seconds,
        object_key=s3_event.object_key,
    )
    schedule_time = resolution.schedule_time

    if resolution.expire_at_override:
        event_publisher.metadata_expire_at_override(
            **event_ctx,
            expire_at=object_metadata.expire_at,
            default_ttl_seconds=matched_repo.ttl_seconds,
        )

    if resolution.clamped_to_max:
        event_publisher.expire_at_clamped(
            **event_ctx,
            expire_at=object_metadata.expire_at,
            clamped_schedule_time=schedule_time.isoformat(),
            max_retention_seconds=max_retention_seconds,
        )
        metrics.add_metric(
            name="ExpireAtClamped", unit=MetricUnit.Count, value=1
        )

    schedule_name = _build_schedule_name(
        get_project_prefix(),
        s3_event.bucket_name,
        s3_event.object_key,
        s3_event.version_id,
    )
    # No offset in an ``at()`` expression; schedule_time is UTC and the
    # schedule declares ``ScheduleExpressionTimezone="UTC"``.
    schedule_expression = f"at({schedule_time.strftime('%Y-%m-%dT%H:%M:%S')})"

    try:
        SchedulerRepository(
            group_name=get_schedule_group_name(),
            role_arn=get_schedule_role_arn(),
        ).create_deletion_schedule(
            schedule_name=schedule_name,
            schedule_expression=schedule_expression,
            function_arn=get_deletion_function_arn(),
            payload={
                "bucket": s3_event.bucket_name,
                "key": s3_event.object_key,
                "schedule_name": schedule_name,
                "version_id": s3_event.version_id,
            },
            dlq_arn=get_dlq_arn(),
        )
    except Exception as exc:
        event_publisher.schedule_creation_failed(**event_ctx, error=str(exc))
        raise

    # Report the actual seconds until deletion: an override or either clamp
    # makes the rule's nominal TTL the wrong number to publish.
    event_publisher.schedule_created(
        **event_ctx,
        schedule_name=schedule_name,
        schedule_expression=schedule_expression,
        ttl_seconds=resolution.seconds_until_deletion(now),
        expire_at_override=resolution.expire_at_override,
    )

    logger.info(
        "Schedule created successfully",
        extra={
            "schedule_name": schedule_name,
            "schedule_expression": schedule_expression,
        },
    )
