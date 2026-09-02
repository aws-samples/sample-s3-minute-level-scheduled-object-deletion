# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Schedule creation handler for S3 object creation events.

When an object is created in a managed bucket, this module:
1. Parses the S3 event into an ``S3ObjectCreatedEvent`` model
2. Matches the key against configured retention rules (longest-prefix match)
3. If no match: immediately deletes the object and emits a rejection event
4. Reads the object's user metadata for an optional ``expireAt`` timestamp
5. Creates a one-time EventBridge schedule to fire at ``expireAt`` or ``now + TTL``
6. Emits lifecycle events to the data EventBridge bus
"""

import hashlib
from datetime import datetime, timezone

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
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
    get_project_prefix,
    get_repository_config,
    get_schedule_group_name,
    get_schedule_role_arn,
)
from src.models import FolderMarkerEvent, RepositoryEntry, S3ObjectCreatedEvent

logger = Logger(child=True)
metrics = Metrics()


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


def _build_schedule_name(
    project_prefix: str,
    bucket: str,
    object_key: str,
    version_id: str | None = None,
) -> str:
    """Build a deterministic, EventBridge-valid schedule name (max 64 chars).

    Format: ``{project_prefix}-{hash}`` where the hash is a truncated
    SHA-256 hex digest of ``bucket``, ``object_key``, and optionally
    ``version_id``. When version_id is provided, different versions of
    the same key produce distinct schedule names."""

    hash_input = f"{bucket}:{object_key}"
    if version_id is not None:
        hash_input = f"{hash_input}:{version_id}"
    digest = hashlib.sha256(hash_input.encode()).hexdigest()

    max_hash_len = 64 - len(project_prefix) - 1
    if max_hash_len < 8:
        max_hash_len = 8

    truncated = digest[:max_hash_len]
    return f"{project_prefix}-{truncated}"[:64]


def handle_schedule_creation(
    event: S3EventNotificationEventBridgeModel,
) -> None:
    """Handle an S3 ObjectCreated event by scheduling a future deletion.

    Raises on error so the Lambda runtime routes the event to the DLQ.
    """
    # --- Parse and validate the S3 event ---
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
    except ValueError:
        logger.exception("Failed to parse S3 event")
        metrics.add_metric(
            name="KeyPatternViolation", unit=MetricUnit.Count, value=1
        )
        raise

    logger.info(
        "Processing S3 object creation",
        extra={
            "bucket": s3_event.bucket_name,
            "object_key": s3_event.object_key,
            "version_id": s3_event.version_id,
        },
    )

    # --- Initialise event publisher ---
    event_publisher = EventPublisher(get_data_event_bus_name())

    # --- Match against configured retention rules ---
    repos = get_repository_config()
    matched_repo = _match_path_prefix(
        s3_event.bucket_name,
        s3_event.object_key,
        repos,
    )

    if matched_repo is None:
        logger.warning(
            "Object key does not match any configured retention rule — "
            "deleting immediately",
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

    # Common event context — shared across all event publisher calls
    event_ctx = {
        "bucket": s3_event.bucket_name,
        "key": s3_event.object_key,
        "prefix": matched_repo.path_prefix,
        "version_id": s3_event.version_id,
    }

    # --- Emit ObjectCreated event ---
    event_publisher.object_created(
        **event_ctx,
        ttl_seconds=matched_repo.ttl_seconds,
    )

    # --- Environment variables (centralised config) ---
    project_prefix = get_project_prefix()
    schedule_group_name = get_schedule_group_name()
    schedule_role_arn = get_schedule_role_arn()

    now = datetime.now(timezone.utc)

    scheduler_repo = SchedulerRepository(
        group_name=schedule_group_name,
        role_arn=schedule_role_arn,
    )

    # --- Emit ObjectCreation metric ---
    metrics.add_dimension(name="Path", value=matched_repo.path_prefix)
    metrics.add_metric(name="ObjectCreation", unit=MetricUnit.Count, value=1)

    # --- Resolve schedule time ---
    # Prefer the ``expireAt`` user metadata (ISO 8601 UTC) if the object
    # creator set one; otherwise fall back to ``now + TTL``.
    metadata = S3Repository.get_user_metadata(
        bucket=s3_event.bucket_name,
        key=s3_event.object_key,
        version_id=s3_event.version_id,
    )
    expire_at_raw = metadata.get("expireat")  # S3 lowercases metadata keys

    schedule_time: datetime | None = None
    expire_at_override = False
    if expire_at_raw:
        try:
            schedule_time = datetime.fromisoformat(expire_at_raw)
            if schedule_time.tzinfo is None:
                schedule_time = schedule_time.replace(tzinfo=timezone.utc)
            if schedule_time <= now:
                logger.warning(
                    "expireAt is in the past, falling back to TTL",
                    extra={
                        "expire_at": expire_at_raw,
                        "object_key": s3_event.object_key,
                    },
                )
                schedule_time = None
            else:
                expire_at_override = True
                event_publisher.metadata_expire_at_override(
                    **event_ctx,
                    expire_at=expire_at_raw,
                    default_ttl_seconds=matched_repo.ttl_seconds,
                )
        except ValueError, TypeError:
            logger.warning(
                "Invalid expireAt metadata value, falling back to TTL",
                extra={
                    "expire_at": expire_at_raw,
                    "object_key": s3_event.object_key,
                },
            )
            schedule_time = None

    if schedule_time is None:
        schedule_time = datetime.fromtimestamp(
            now.timestamp() + matched_repo.ttl_seconds, tz=timezone.utc
        )

    # --- Enforce minimum lead time for EventBridge Scheduler ---
    # One-time schedules have 60-second precision. Set a minimum buffer
    # of 120 seconds.
    min_schedule_time = datetime.fromtimestamp(
        now.timestamp() + 120, tz=timezone.utc
    )
    if schedule_time < min_schedule_time:
        logger.info(
            "Schedule time too close to now, clamping to minimum buffer",
            extra={
                "original_schedule_time": schedule_time.isoformat(),
                "clamped_schedule_time": min_schedule_time.isoformat(),
            },
        )
        schedule_time = min_schedule_time

    # --- Create one-time EventBridge schedule ---
    schedule_name = _build_schedule_name(
        project_prefix,
        s3_event.bucket_name,
        s3_event.object_key,
        s3_event.version_id,
    )
    schedule_expression = f"at({schedule_time.strftime('%Y-%m-%dT%H:%M:%S')})"

    deletion_function_arn = get_deletion_function_arn()

    try:
        scheduler_repo.create_deletion_schedule(
            schedule_name=schedule_name,
            schedule_expression=schedule_expression,
            function_arn=deletion_function_arn,
            payload={
                "bucket": s3_event.bucket_name,
                "key": s3_event.object_key,
                "schedule_name": schedule_name,
                "source": "scheduled",
                "version_id": s3_event.version_id,
            },
            dlq_arn=get_dlq_arn(),
        )
    except Exception as exc:
        event_publisher.schedule_creation_failed(
            **event_ctx,
            error=str(exc),
        )
        raise

    # --- Emit ScheduleCreated event ---
    # When an expireAt override is active, report the actual seconds until
    # deletion rather than the rule's default TTL.
    effective_ttl_seconds = (
        int((schedule_time - now).total_seconds())
        if expire_at_override
        else matched_repo.ttl_seconds
    )
    event_publisher.schedule_created(
        **event_ctx,
        schedule_name=schedule_name,
        schedule_expression=schedule_expression,
        ttl_seconds=effective_ttl_seconds,
        expire_at_override=expire_at_override,
    )

    logger.info(
        "Schedule created successfully",
        extra={
            "schedule_name": schedule_name,
            "schedule_expression": schedule_expression,
        },
    )
