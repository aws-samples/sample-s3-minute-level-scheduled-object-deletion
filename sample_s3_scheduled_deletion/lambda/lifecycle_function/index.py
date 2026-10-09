# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lifecycle Lambda — handles S3 object creation events and schedules
future deletions via EventBridge Scheduler.

Triggered by S3 ``Object Created`` events that S3 emits to the default
EventBridge bus and that a forwarding rule routes onto the custom
deletion bus, where the rule targeting this function lives.  The
separate Deletion Lambda deletes the objects.
"""

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.utilities.parser import event_parser
from aws_lambda_powertools.utilities.parser.models import (
    S3EventNotificationEventBridgeModel,
)
from aws_lambda_powertools.utilities.typing import LambdaContext
from src.schedule_creation_service import handle_schedule_creation

logger = Logger(service="lifecycle_function")
metrics = Metrics()


@logger.inject_lambda_context
@metrics.log_metrics
@event_parser(model=S3EventNotificationEventBridgeModel)
def handler(
    event: S3EventNotificationEventBridgeModel, context: LambdaContext
) -> None:
    """Handle S3 ObjectCreated events by scheduling a future deletion.

    The EventBridge envelope is validated by Powertools' parser via the
    ``@event_parser`` decorator; a malformed event raises ``ValidationError``
    before this function runs, routing it to the DLQ.
    """
    if event.detail_type != "Object Created":
        logger.error(
            "Unrecognised event type — expected 'Object Created'",
            extra={"detail_type": event.detail_type},
        )
        raise ValueError(
            "Unrecognised event type — expected 'Object Created', "
            f"got {event.detail_type!r}"
        )

    handle_schedule_creation(event)
