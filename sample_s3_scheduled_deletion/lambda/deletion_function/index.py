# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deletion Lambda — single entry point for all S3 object deletions.

Handles both scheduled (EventBridge Scheduler) and on-demand (API/app)
deletions through a unified event model.
"""

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.utilities.parser import event_parser
from aws_lambda_powertools.utilities.typing import LambdaContext
from src.deleter import handle_deletion
from src.models import DeletionRequest

logger = Logger(service="deletion_function")
metrics = Metrics()


@logger.inject_lambda_context
@metrics.log_metrics
@event_parser(model=DeletionRequest)
def handler(event: DeletionRequest, context: LambdaContext) -> dict | None:
    """Delete an S3 object and emit metrics.

    The event is validated by Powertools' parser via ``@event_parser``;
    a malformed payload raises ``ValidationError`` before this function
    runs, routing it to the DLQ. Expected shape::

        {
            "bucket": "...",
            "key": "...",
            "source": "scheduled" | "onDemand"
        }
    """
    return handle_deletion(event)
