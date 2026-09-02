# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""S3 scheduled-deletion CDK construct and supporting data models."""

import json
from dataclasses import dataclass

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from cdk_nag import NagSuppressions
from constructs import Construct

from ..config import (
    DELETION_EVENT_BUS_NAME,
    DELETION_EVENT_LOG_GROUP_NAME,
    LOG_LEVEL,
    PROJECT_PREFIX,
)
from ..custom_resources.lambda_function import CustomFunction
from ..custom_resources.log_group import CustomLogGroup
from ..helpers import resource_name

# Lambda asset paths are resolved relative to the current working directory,
# so ``cdk synth`` / ``cdk deploy`` must be run from the repository root.
LAMBDA_SRC_ROOT = "sample_s3_scheduled_deletion/lambda"

# Smallest TTL a retention rule may declare.
MIN_RETENTION_TTL = Duration.minutes(2)


@dataclass
class RetentionRule:
    """Configuration for a single path-based retention rule."""

    path_prefix: str  # e.g. "uploads/"
    ttl: Duration  # e.g. Duration.hours(2)

    @staticmethod
    def _validate_rules(
        rules: list["RetentionRule"],
        default_lifecycle_expiration: Duration,
    ) -> None:
        """Validate a list of retention rules at synthesis time.

        Raises ``ValueError`` when any rule violates the constraints.
        """
        seen_prefixes: set[str] = set()

        for rule in rules:
            if not rule.path_prefix:
                raise ValueError(
                    "RetentionRule path_prefix must not be empty."
                )

            if not rule.path_prefix.endswith("/"):
                raise ValueError(
                    f"RetentionRule path_prefix must end with '/': "
                    f"'{rule.path_prefix}'"
                )

            ttl_ms = rule.ttl.to_milliseconds()
            if ttl_ms < MIN_RETENTION_TTL.to_milliseconds():
                raise ValueError(
                    f"RetentionRule TTL must be at least 2 minutes: "
                    f"'{rule.path_prefix}'"
                )

            lifecycle_ms = default_lifecycle_expiration.to_milliseconds()
            if ttl_ms >= lifecycle_ms:
                raise ValueError(
                    f"RetentionRule TTL must be less than "
                    f"default_lifecycle_expiration: '{rule.path_prefix}'"
                )

            if rule.path_prefix in seen_prefixes:
                raise ValueError(
                    f"Duplicate RetentionRule path_prefix: "
                    f"'{rule.path_prefix}'"
                )
            seen_prefixes.add(rule.path_prefix)


@dataclass
class BucketRetentionConfig:
    """Pairs an S3 bucket with its path-based retention rules."""

    bucket: s3.IBucket
    retention_rules: list[RetentionRule]


class S3DataDeletion(Construct):
    """Wires together all scheduled-deletion infrastructure.

    Creates a Lifecycle Lambda (schedules a future deletion when an object
    is created), a Deletion Lambda (performs scheduled + on-demand
    deletions), a DLQ for failed async invocations, an EventBridge schedule
    group, and a rule on the account **default** EventBridge bus that
    triggers the Lifecycle Lambda on S3 ``Object Created`` events.

    Each :class:`BucketRetentionConfig` pairs a bucket with its own set of
    retention rules; the same Lambdas and schedule group are shared across
    all buckets.  Path prefixes must be unique within each bucket.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bucket_configs: list[BucketRetentionConfig],
        metrics_namespace: str,
        default_lifecycle_expiration: Duration = Duration.days(1),
    ) -> None:
        super().__init__(scope, construct_id)

        if not bucket_configs:
            raise ValueError("bucket_configs must contain at least one entry.")

        # Validate each bucket independently so the same prefix can be reused
        # in different buckets, while duplicates within one bucket are rejected.
        for cfg in bucket_configs:
            RetentionRule._validate_rules(
                cfg.retention_rules,
                default_lifecycle_expiration,
            )

        self._bucket_configs = bucket_configs

        # --- S3 server access logging to CloudWatch Logs ---
        self.enable_access_logging()

        stack = Stack.of(self)
        project_prefix = PROJECT_PREFIX

        bucket_arns = [cfg.bucket.bucket_arn for cfg in bucket_configs]
        object_arns = [f"{arn}/*" for arn in bucket_arns]

        # --- Dedicated EventBridge bus for the Lambdas' own events ---
        # Receives S3 "Object Created" notifications and the application
        # lifecycle/deletion events.
        self._event_bus = events.EventBus(
            self,
            "DeletionEventBus",
            event_bus_name=DELETION_EVENT_BUS_NAME,
        )

        # --- Log group capturing every event from the custom bus ---
        self._event_log_group = CustomLogGroup(
            self,
            "DeletionEventLogGroup",
            log_group_name=DELETION_EVENT_LOG_GROUP_NAME,
            retention=logs.RetentionDays.ONE_MONTH,
        )

        # Route every application event on the custom bus to the log group.
        # The pattern matches the relevant ``Source`` values including
        # the ones used by Lambda EventPublisher adapters.
        events.CfnRule(
            self,
            "DeletionEventsLogRule",
            name=resource_name(PROJECT_PREFIX, "deletion-events-log"),
            event_bus_name=self._event_bus.event_bus_name,
            event_pattern={
                "source": [
                    "aws.s3",
                    "retention.lifecycle",
                    "retention.deletion",
                ],
            },
            targets=[
                events.CfnRule.TargetProperty(
                    arn=self._event_log_group.log_group_arn,
                    id="DeletionEventLogGroup",
                )
            ],
        )

        # Resource-based policy allowing EventBridge to deliver matched events
        # to the log group (scoped to this log group only).
        logs.CfnResourcePolicy(
            self,
            "DeletionEventLogGroupPolicy",
            policy_name=resource_name(PROJECT_PREFIX, "deletion-events-logs"),
            policy_document=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "AllowEventBridgeDelivery",
                            "Effect": "Allow",
                            "Principal": {
                                "Service": [
                                    "events.amazonaws.com",
                                    "delivery.logs.amazonaws.com",
                                ]
                            },
                            "Action": [
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                            ],
                            "Resource": (
                                f"{self._event_log_group.log_group_arn}:*"
                            ),
                        }
                    ],
                }
            ),
        )

        # --- Dead Letter Queue ---
        self._dlq = sqs.Queue(
            self,
            "DeletionDLQ",
            queue_name=resource_name(PROJECT_PREFIX, "deletion-dlq"),
            retention_period=Duration.days(14),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )
        NagSuppressions.add_resource_suppressions(
            self._dlq,
            suppressions=[
                {
                    "id": "AwsSolutions-SQS3",
                    "reason": (
                        "This queue IS the dead-letter queue for the "
                        "Lifecycle and Deletion Lambda async invocation "
                        "failure destination. A DLQ does not need its own DLQ."
                    ),
                },
            ],
        )
        cfn_dlq = self._dlq.node.default_child
        cfn_dlq.add_metadata(
            "checkov",
            {
                "skip": [
                    {
                        "id": "CKV_AWS_27",
                        "comment": "SQS queue encrypted with SSE-SQS.",
                    }
                ]
            },
        )

        # --- EventBridge Schedule Group ---
        self._schedule_group = scheduler.CfnScheduleGroup(
            self,
            "DeletionScheduleGroup",
            name=resource_name(PROJECT_PREFIX, "deletion-schedules"),
        )

        # --- Merge rules into the Lifecycle Lambda repository config ---
        repo_config = json.dumps(
            {
                "repositories": [
                    {
                        "bucket_name": cfg.bucket.bucket_name,
                        "path_prefix": rule.path_prefix,
                        "ttl_seconds": int(rule.ttl.to_seconds()),
                    }
                    for cfg in bucket_configs
                    for rule in cfg.retention_rules
                ]
            }
        )

        # --- Schedule execution role (assumed by EventBridge Scheduler) ---
        self._schedule_execution_role = iam.Role(
            self,
            "ScheduleExecutionRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )

        # --- Powertools layer (provides aws-lambda-powertools + pydantic) ---
        powertools_layer = _lambda.LayerVersion.from_layer_version_arn(
            self,
            "PowertoolsLayer",
            ssm.StringParameter.value_for_string_parameter(
                self,
                "/aws/service/powertools/python/arm64/python3.14/latest",
            ),
        )

        # --- Deletion Lambda (scheduled + on-demand deletions) ---
        self._deletion_function = CustomFunction(
            self,
            "DeletionFunction",
            component="deletion-fn",
            handler="index.handler",
            timeout=Duration.minutes(2),
            reserved_concurrent_executions=10,
            layers=[powertools_layer],
            code=_lambda.Code.from_asset(
                f"{LAMBDA_SRC_ROOT}/deletion_function",
                exclude=["__pycache__", "*.pyc"],
            ),
            environment={
                "POWERTOOLS_LOG_LEVEL": LOG_LEVEL,
                "POWERTOOLS_METRICS_NAMESPACE": metrics_namespace,
                "DATA_EVENT_BUS_NAME": DELETION_EVENT_BUS_NAME,
            },
        )

        # --- Lifecycle Lambda (schedule creation on S3 events) ---
        self._lifecycle_function = CustomFunction(
            self,
            "LifecycleFunction",
            component="lifecycle-fn",
            handler="index.handler",
            timeout=Duration.minutes(2),
            reserved_concurrent_executions=10,
            layers=[powertools_layer],
            code=_lambda.Code.from_asset(
                f"{LAMBDA_SRC_ROOT}/lifecycle_function",
                exclude=["__pycache__", "*.pyc"],
            ),
            environment={
                "POWERTOOLS_LOG_LEVEL": LOG_LEVEL,
                "POWERTOOLS_METRICS_NAMESPACE": metrics_namespace,
                "REPOSITORY_CONFIG": repo_config,
                "PROJECT_PREFIX": project_prefix,
                "SCHEDULE_GROUP_NAME": self._schedule_group.name,
                "SCHEDULE_ROLE_ARN": self._schedule_execution_role.role_arn,
                "DELETION_FUNCTION_ARN": self._deletion_function.function_arn,
                "DATA_EVENT_BUS_NAME": DELETION_EVENT_BUS_NAME,
                "DLQ_ARN": self._dlq.queue_arn,
            },
        )

        # --- Schedule execution role permissions ---
        self._schedule_execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[self._deletion_function.function_arn],
            )
        )
        # Allow Scheduler to deliver failed invocations to the DLQ
        self._dlq.grant_send_messages(self._schedule_execution_role)

        # --- Lifecycle Lambda permissions ---
        # Pass the schedule execution role to EventBridge Scheduler
        self._lifecycle_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[self._schedule_execution_role.role_arn],
            )
        )
        # Create one-time schedules in the deletion schedule group
        self._lifecycle_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["scheduler:CreateSchedule"],
                resources=[
                    stack.format_arn(
                        service="scheduler",
                        resource="schedule",
                        resource_name=f"{self._schedule_group.name}/*",
                    )
                ],
            )
        )
        # Read object metadata (HeadObject) and delete rejected objects
        self._lifecycle_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                ],
                resources=object_arns,
            )
        )
        self._lifecycle_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=bucket_arns,
            )
        )

        # --- Deletion Lambda permissions ---
        self._deletion_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                ],
                resources=object_arns,
            )
        )
        self._deletion_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=bucket_arns,
            )
        )

        # Both Lambdas publish lifecycle/deletion events to the custom bus
        self._event_bus.grant_put_events_to(self._lifecycle_function)
        self._event_bus.grant_put_events_to(self._deletion_function)

        # --- cdk-nag: object-level and schedule-group wildcards are required
        # and scoped to the managed buckets / deletion schedule group only. ---
        NagSuppressions.add_resource_suppressions(
            self._lifecycle_function.role,
            suppressions=[
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Object-level S3 access (bucket-ARN/*) and one-time "
                        "schedule creation (schedule-group/*) require a "
                        "wildcard suffix; both are scoped to the managed "
                        "bucket(s) and the deletion schedule group."
                    ),
                    "appliesTo": [{"regex": r"/^Resource::.*\*$/g"}],
                },
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            self._deletion_function.role,
            suppressions=[
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Object-level S3 access (bucket-ARN/*) requires a "
                        "wildcard suffix; scoped to the managed bucket(s) "
                        "only."
                    ),
                    "appliesTo": [{"regex": r"/^Resource::.*\*$/g"}],
                },
            ],
            apply_to_children=True,
        )

        # --- EventBridge: S3 Object Created (default bus) -> custom bus ---
        events.Rule(
            self,
            "S3ObjectCreatedForwardRule",
            rule_name=resource_name(
                PROJECT_PREFIX, "s3-object-created-forward"
            ),
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created", "Object Deleted"],
                resources=bucket_arns,
            ),
            targets=[targets.EventBus(self._event_bus)],
        )

        # --- EventBridge: S3 Object Created (custom bus) -> Lifecycle Lambda ---
        events.Rule(
            self,
            "S3ObjectCreatedRule",
            rule_name=resource_name(PROJECT_PREFIX, "s3-object-created"),
            event_bus=self._event_bus,
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                resources=bucket_arns,
            ),
            targets=[targets.LambdaFunction(self._lifecycle_function)],
        )

        # --- Async invocation with DLQ as on_failure destination ---
        self._lifecycle_function.configure_async_invoke(
            on_failure=destinations.SqsDestination(self._dlq),
        )
        self._deletion_function.configure_async_invoke(
            on_failure=destinations.SqsDestination(self._dlq),
        )

        # --- Restrict uploads to allowed prefixes only ---
        # The resource glob uses a trailing "*" (zero or more characters) so
        # that zero-byte "folder marker" objects whose key is exactly the
        # prefix (e.g. "retention-5-mins/") are permitted, letting users
        # create the empty folder manually.  Such markers have no content
        # after the prefix and are skipped by the lifecycle Lambda's key
        # model; the default lifecycle expiration is the backstop for them.
        for cfg in bucket_configs:
            allowed_arns = [
                f"{cfg.bucket.bucket_arn}/{rule.path_prefix}*"
                for rule in cfg.retention_rules
            ]
            cfg.bucket.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="DenyUploadsOutsideAllowedPrefixes",
                    effect=iam.Effect.DENY,
                    principals=[iam.AnyPrincipal()],
                    actions=["s3:PutObject", "s3:AbortMultipartUpload"],
                    not_resources=allowed_arns,
                )
            )

    @property
    def deletion_function(self) -> _lambda.IFunction:
        """The Lambda that performs scheduled and on-demand deletions."""
        return self._deletion_function

    def enable_access_logging(self) -> None:
        """Enable S3 server access log delivery to CloudWatch Logs.

        Uses CloudWatch Logs vended log delivery (``DeliverySource`` →
        ``DeliveryDestination`` → ``Delivery``) for each managed bucket.
        This delivers structured access logs to a CloudWatch Logs log group
        where they can be queried with Logs Insights.

        See: https://docs.aws.amazon.com/AmazonS3/latest/userguide/sal-cw-enabling.html
        """
        for idx, cfg in enumerate(self._bucket_configs):
            bucket = cfg.bucket
            suffix = f"-{idx}" if len(self._bucket_configs) > 1 else ""

            # Log group for access logs
            access_log_group = CustomLogGroup(
                self,
                f"AccessLogGroup{suffix}",
                log_group_name=f"/aws/s3/server-access/{bucket.bucket_name}",
            )

            # 1. Delivery source — registers the S3 bucket as a log source
            delivery_source = logs.CfnDeliverySource(
                self,
                f"AccessLogDeliverySource{suffix}",
                name=resource_name(PROJECT_PREFIX, f"sal-source{suffix}"),
                resource_arn=bucket.bucket_arn,
                log_type="S3_SERVER_ACCESS_LOGS",
            )

            # 2. Delivery destination — the CloudWatch Logs log group
            delivery_destination = logs.CfnDeliveryDestination(
                self,
                f"AccessLogDeliveryDestination{suffix}",
                name=resource_name(PROJECT_PREFIX, f"sal-destination{suffix}"),
                destination_resource_arn=access_log_group.log_group_arn,
            )

            # 3. Delivery — links the source to the destination
            delivery = logs.CfnDelivery(
                self,
                f"AccessLogDelivery{suffix}",
                delivery_source_name=delivery_source.name,
                delivery_destination_arn=delivery_destination.attr_arn,
            )
            delivery.add_resource_dependency(delivery_source)
            delivery.add_resource_dependency(delivery_destination)
