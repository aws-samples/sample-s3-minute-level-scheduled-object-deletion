# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""EventBridge Scheduler repository for deletion schedule management."""

import json

import boto3
from aws_lambda_powertools import Logger

logger = Logger(child=True)


class SchedulerRepository:
    """Creates one-time EventBridge Scheduler schedules for object deletion."""

    def __init__(
        self,
        *,
        group_name: str,
        role_arn: str,
    ) -> None:
        self._group_name = group_name
        self._role_arn = role_arn
        self._client = boto3.client("scheduler")

    def create_deletion_schedule(
        self,
        *,
        schedule_name: str,
        schedule_expression: str,
        function_arn: str,
        payload: dict,
        dlq_arn: str,
    ) -> None:
        """Create a one-time schedule that invokes the Lambda with *payload*.

        The schedule self-deletes after firing (``ActionAfterCompletion=DELETE``).
        Encryption at rest uses the AWS-owned key.

        ``CreateSchedule`` called with a ``schedule_name`` that already exists
        raises ``ConflictException``.
        The pending schedule already targets deletion of the same object version.

        On a bucket without versioning, ``version_id`` is absent from the
        name, so overwriting a key while its schedule is pending reuses that
        schedule and the new object inherits the earlier deletion time
        instead of a full TTL.

        Failed invocations are retried.
        If also retries fail, delivered to *dlq_arn* as a dead-letter
        event (invocation metadata + target response).
        """
        try:
            self._client.create_schedule(
                Name=schedule_name,
                GroupName=self._group_name,
                ScheduleExpression=schedule_expression,
                ScheduleExpressionTimezone="UTC",
                FlexibleTimeWindow={"Mode": "OFF"},
                ActionAfterCompletion="DELETE",
                Target={
                    "Arn": function_arn,
                    "RoleArn": self._role_arn,
                    "Input": json.dumps(payload),
                    "DeadLetterConfig": {"Arn": dlq_arn},
                    "RetryPolicy": {
                        "MaximumRetryAttempts": 2,
                        "MaximumEventAgeInSeconds": 3600,
                    },
                },
            )
        except self._client.exceptions.ConflictException:
            logger.info(
                "Deletion schedule already exists — treating as created",
                extra={
                    "schedule_name": schedule_name,
                    "schedule_expression": schedule_expression,
                },
            )
