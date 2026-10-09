# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""S3 scheduled-deletion CDK construct and supporting data models."""

import json
from dataclasses import dataclass
from enum import Enum

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
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

# Window each failure alarm evaluates.
ALARM_PERIOD = Duration.minutes(5)

# Longest PROJECT_PREFIX the derived resource names can carry. The binding
# name is the "<prefix>-s3-object-created-forward" rule; EventBridge rule
# names are capped at 64 characters.
MAX_PROJECT_PREFIX_LENGTH = 64 - len("-s3-object-created-forward")


def _validate_project_prefix(project_prefix: str) -> None:
    """Reject a prefix too long for the resource names derived from it."""
    if len(project_prefix) > MAX_PROJECT_PREFIX_LENGTH:
        raise ValueError(
            f"PROJECT_PREFIX must be at most {MAX_PROJECT_PREFIX_LENGTH} "
            f"characters, got {len(project_prefix)}; longer prefixes push "
            "derived resource names past their service limits."
        )


class BucketOwnership(Enum):
    """Ownership type for S3 buckets.

    - ``EXCLUSIVE``: the solution owns the whole S3 bucket: uploads outside the retention
    prefixes are denied, and an object matching no rule is deleted on arrival.
    - ``SHARED``: the solution only manages objects in the specified retention prefixes.
    """

    EXCLUSIVE = "EXCLUSIVE"
    SHARED = "SHARED"


def _validate_retention_bounds(
    max_retention: Duration,
    default_lifecycle_expiration: Duration,
) -> None:
    """Check the retention ceiling against its floor and the backstop.

    ``max_retention`` is the minute-granularity limit on scheduled deletion;
    ``default_lifecycle_expiration`` is the coarse S3 Lifecycle backstop. The
    two are configured independently, so the only relationship required is
    that the ceiling does not outlive the backstop.

    Raises ``ValueError`` when the ceiling is unusable.
    """
    max_ms = max_retention.to_milliseconds()

    if max_ms < MIN_RETENTION_TTL.to_milliseconds():
        raise ValueError(
            "max_retention must be at least 2 minutes, to leave room for "
            "EventBridge Scheduler's 60-second precision."
        )

    if max_ms > default_lifecycle_expiration.to_milliseconds():
        raise ValueError(
            "max_retention must not exceed default_lifecycle_expiration; "
            "otherwise the Lifecycle rule would delete objects before their "
            "scheduled deletion time."
        )


@dataclass
class RetentionRule:
    """Configuration for a single path-based retention rule."""

    path_prefix: str  # e.g. "uploads/"
    ttl: Duration  # e.g. Duration.hours(2)

    @staticmethod
    def _validate_rules(
        rules: list["RetentionRule"],
        max_retention: Duration,
    ) -> None:
        """Validate a list of retention rules at synthesis time.

        Raises ``ValueError`` when any rule violates the constraints.
        """
        if not rules:
            raise ValueError(
                "A bucket must declare at least one RetentionRule."
            )

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

            if ttl_ms > max_retention.to_milliseconds():
                raise ValueError(
                    f"RetentionRule TTL must not exceed max_retention: "
                    f"'{rule.path_prefix}'"
                )

            if rule.path_prefix in seen_prefixes:
                raise ValueError(
                    f"Duplicate RetentionRule path_prefix: "
                    f"'{rule.path_prefix}'"
                )
            seen_prefixes.add(rule.path_prefix)


@dataclass
class BucketRetentionConfig:
    """Pairs an S3 bucket with its retention rules and ownership mode."""

    bucket: s3.IBucket
    retention_rules: list[RetentionRule]
    ownership: BucketOwnership

    @property
    def managed_key_prefixes(self) -> list[str]:
        """Key prefixes the solution may delete from; ``""`` is every key.

        ``SHARED`` stops at the retention prefixes; ``EXCLUSIVE`` needs the
        whole bucket to delete unmatched objects.
        """
        if self.ownership is BucketOwnership.SHARED:
            return [rule.path_prefix for rule in self.retention_rules]
        return [""]

    @property
    def object_arns(self) -> list[str]:
        """Object ARNs the Lambdas may read and delete."""
        return [
            f"{self.bucket.bucket_arn}/{prefix}*"
            for prefix in self.managed_key_prefixes
        ]


class S3DataDeletion(Construct):
    """Scheduled deletion for the objects of one or more buckets.

    A rule on the default bus forwards the buckets' S3 events to a custom
    bus, where a second rule invokes the Lifecycle Lambda. That Lambda
    schedules each deletion, and the Deletion Lambda carries it out. The
    construct also creates the DLQ, the event log group and the alarms.

    The caller enables EventBridge notifications and the Lifecycle
    expiration on each bucket.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bucket_configs: list[BucketRetentionConfig],
        metrics_namespace: str,
        max_retention: Duration,
        default_lifecycle_expiration: Duration = Duration.days(1),
    ) -> None:
        super().__init__(scope, construct_id)

        if not bucket_configs:
            raise ValueError("bucket_configs must contain at least one entry.")

        _validate_project_prefix(PROJECT_PREFIX)

        # The ceiling must fit inside the lifecycle expiration.
        _validate_retention_bounds(max_retention, default_lifecycle_expiration)

        # Validate each bucket independently so the same prefix can be reused
        # in different buckets, while duplicates within one bucket are rejected.
        for cfg in bucket_configs:
            RetentionRule._validate_rules(cfg.retention_rules, max_retention)

        self._enable_access_logging(bucket_configs)

        bucket_arns = [cfg.bucket.bucket_arn for cfg in bucket_configs]
        # Scoped per bucket by ownership mode, so a SHARED bucket's unmanaged
        # prefixes are out of reach at the IAM layer.
        object_arns = [
            arn for cfg in bucket_configs for arn in cfg.object_arns
        ]

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
                        "ttl_seconds": rule.ttl.to_seconds(),
                        "delete_unmatched": (
                            cfg.ownership is BucketOwnership.EXCLUSIVE
                        ),
                    }
                    for cfg in bucket_configs
                    for rule in cfg.retention_rules
                ]
            }
        )

        # --- What the Deletion Lambda may delete: the same prefixes as IAM ---
        deletion_scope = json.dumps(
            {
                "buckets": [
                    {
                        "bucket_name": cfg.bucket.bucket_name,
                        "key_prefixes": cfg.managed_key_prefixes,
                    }
                    for cfg in bucket_configs
                ]
            }
        )

        # --- Schedule execution role (assumed by EventBridge Scheduler) ---
        # Only schedules in this stack's group may assume the role.
        self._schedule_execution_role = iam.Role(
            self,
            "ScheduleExecutionRole",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com"
            ).with_conditions(
                {
                    "StringEquals": {
                        "aws:SourceAccount": Stack.of(self).account,
                        "aws:SourceArn": self._schedule_group.attr_arn,
                    }
                }
            ),
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

        # --- Deletion Lambda (invoked by the deletion schedules) ---
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
                "DELETION_SCOPE": deletion_scope,
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
                "PROJECT_PREFIX": PROJECT_PREFIX,
                "SCHEDULE_GROUP_NAME": self._schedule_group.name,
                "SCHEDULE_ROLE_ARN": self._schedule_execution_role.role_arn,
                "DELETION_FUNCTION_ARN": self._deletion_function.function_arn,
                "DATA_EVENT_BUS_NAME": DELETION_EVENT_BUS_NAME,
                "DLQ_ARN": self._dlq.queue_arn,
                "MAX_RETENTION_SECONDS": str(max_retention.to_seconds()),
            },
        )

        # --- Schedule execution role permissions ---
        self._schedule_execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[self._deletion_function.function_arn],
            )
        )
        # Deny every invoker except the schedule execution role. Lambda runs
        # queued async events as itself, without aws:PrincipalArn, so it is
        # exempt. Never use add_permission or grant_invoke on this function:
        # an AWS::Lambda::Permission and this policy overwrite each other.
        deletion_function_arn = self._deletion_function.function_arn
        _ = _lambda.CfnResourcePolicy(
            self,
            "DeletionFunctionResourcePolicy",
            resource_arn=deletion_function_arn,
            policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "DenyInvokeExceptScheduleExecutionRole",
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "lambda:InvokeFunction",
                        "Resource": [
                            deletion_function_arn,
                            f"{deletion_function_arn}:*",
                        ],
                        "Condition": {
                            "ArnNotEquals": {
                                "aws:PrincipalArn": (
                                    self._schedule_execution_role.role_arn
                                )
                            },
                            "StringNotEqualsIfExists": {
                                "aws:PrincipalServiceName": (
                                    "lambda.amazonaws.com"
                                )
                            },
                        },
                    }
                ],
            },
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
                    Stack.of(self).format_arn(
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
        # No list call is made. With s3:ListBucket, HeadObject on a missing
        # key returns 404 instead of 403, so an object deleted before its
        # metadata is read is logged as missing, not as a permissions error.
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
        # No list call is made, but the existence check depends on it:
        # without s3:ListBucket, HeadObject on a missing key returns 403, not
        # 404, so an already-deleted object fails the invocation instead of
        # being skipped.
        self._deletion_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=bucket_arns,
            )
        )

        # Both Lambdas publish lifecycle/deletion events to the custom bus
        self._event_bus.grant_put_events_to(self._lifecycle_function)
        self._event_bus.grant_put_events_to(self._deletion_function)

        # Explicit delivery role for the S3 "Object Created/Deleted" forward
        # rule (default bus -> custom bus). Declaring it here (rather than
        # letting CDK auto-generate one) gives a deterministic ARN that the
        # bus resource policy below can allow-list, and lets us grant it only
        # events:PutEvents on this one bus.
        self._forward_rule_role = iam.Role(
            self,
            "S3ForwardRuleRole",
            assumed_by=iam.ServicePrincipal("events.amazonaws.com"),
        )
        self._event_bus.grant_put_events_to(self._forward_rule_role)

        # Deny events:PutEvents from every principal except the known
        # publishers: the two Lambda execution roles and the forward-rule
        # delivery role. This closes the forged-event injection vector:
        # without it, any in-account principal holding a broad
        # events:PutEvents grant could inject a fake "Object Created" event
        # onto the custom bus and have the Lifecycle Lambda schedule the
        # deletion of an in-scope object that was never actually (re)uploaded.
        # Aligns with AWS Security Hub control EventBridge.3 (custom event
        # buses should have a resource-based policy). The in-code
        # DELETION_SCOPE check remains as defense in depth.
        #
        # NOTE: the forward rule delivers via an IAM role (above), not the
        # bare events.amazonaws.com service principal, so it must be named
        # explicitly in the allow-list; a service-name exemption alone would
        # not cover it and would silently drop every forwarded S3 event.
        #
        # AWS::Events::EventBusPolicy requires StatementId and the statement's
        # Sid to be identical, and both must be alphanumeric.
        bus_policy_sid = "DenyPutEventsExceptKnownPublishers"
        _ = events.CfnEventBusPolicy(
            self,
            "DeletionEventBusPolicy",
            event_bus_name=self._event_bus.event_bus_name,
            statement_id=bus_policy_sid,
            statement={
                "Sid": bus_policy_sid,
                "Effect": "Deny",
                "Principal": "*",
                "Action": "events:PutEvents",
                "Resource": self._event_bus.event_bus_arn,
                "Condition": {
                    "ArnNotEquals": {
                        "aws:PrincipalArn": [
                            self._lifecycle_function.role.role_arn,
                            self._deletion_function.role.role_arn,
                            self._forward_rule_role.role_arn,
                        ]
                    }
                },
            },
        )

        # --- cdk-nag: the wildcards stop at the managed objects and group ---
        NagSuppressions.add_resource_suppressions(
            self._lifecycle_function.role,
            suppressions=[
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Object-level S3 access and one-time schedule "
                        "creation require a wildcard suffix. Objects are "
                        "scoped to bucket-ARN/* for a stack-owned bucket and "
                        "bucket-ARN/<prefix>* for a shared one; schedules to "
                        "the deletion schedule group."
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
                        "Object-level S3 access requires a wildcard suffix, "
                        "scoped to bucket-ARN/* for a stack-owned bucket and "
                        "bucket-ARN/<prefix>* for a shared one."
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
            targets=[
                targets.EventBus(self._event_bus, role=self._forward_rule_role)
            ],
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

        # --- Restrict uploads to allowed prefixes (EXCLUSIVE buckets only) ---
        # A SHARED bucket's other prefixes are not ours to police.
        for cfg in bucket_configs:
            if cfg.ownership is BucketOwnership.EXCLUSIVE:
                self._deny_uploads_outside_prefixes(cfg)

        # --- Alarms on the failure signals ---
        self._alarms = self._create_alarms(metrics_namespace)

    @property
    def deletion_function(self) -> _lambda.IFunction:
        """The Lambda that deletes objects when their schedules fire."""
        return self._deletion_function

    @property
    def alarms(self) -> list[cloudwatch.Alarm]:
        """Alarms on failed deletions; they have no actions until you add some."""
        return list(self._alarms)

    def _create_alarms(self, metrics_namespace: str) -> list[cloudwatch.Alarm]:
        """Alarm on failures that change when, or whether, an object is deleted.

        A failure either exhausts its retries into the DLQ or is handled by a
        fallback. The DLQ alarm covers the first kind. The application
        metric alarms cover the fallback that changes the deletion time, and
        give earlier warning of failed deletions than the DLQ does.
        """
        dlq_alarm = self._alarm(
            "DeletionDlqNotEmptyAlarm",
            name="deletion-dlq-not-empty",
            description=(
                "A Lambda invocation or a schedule delivery failed after its "
                "retries, so the object is left for the S3 Lifecycle backstop."
            ),
            metric=self._dlq.metric_approximate_number_of_messages_visible(
                period=ALARM_PERIOD,
                statistic=cloudwatch.Stats.MAXIMUM,
            ),
        )
        # Both application metrics are emitted without dimensions.
        deletion_error_alarm = self._alarm(
            "DeletionErrorAlarm",
            name="deletion-error",
            description=(
                "A deletion attempt failed. It is retried, and sent to the "
                "DLQ if the retries fail too."
            ),
            metric=self._failure_metric(metrics_namespace, "DeletionError"),
        )
        metadata_alarm = self._alarm(
            "MetadataLookupFailedAlarm",
            name="metadata-lookup-failed",
            description=(
                "An object's metadata could not be read, so its expireAt was "
                "ignored and the retention rule's TTL applied instead."
            ),
            metric=self._failure_metric(
                metrics_namespace, "MetadataLookupFailed"
            ),
        )
        return [dlq_alarm, deletion_error_alarm, metadata_alarm]

    @staticmethod
    def _failure_metric(namespace: str, name: str) -> cloudwatch.Metric:
        """A dimensionless application failure count."""
        return cloudwatch.Metric(
            namespace=namespace,
            metric_name=name,
            period=ALARM_PERIOD,
            statistic=cloudwatch.Stats.SUM,
        )

    def _alarm(
        self,
        construct_id: str,
        *,
        name: str,
        description: str,
        metric: cloudwatch.IMetric,
    ) -> cloudwatch.Alarm:
        """Alarm as soon as *metric* reaches 1 in a period."""
        return cloudwatch.Alarm(
            self,
            construct_id,
            alarm_name=resource_name(PROJECT_PREFIX, name),
            alarm_description=description,
            metric=metric,
            threshold=1,
            evaluation_periods=1,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

    def _deny_uploads_outside_prefixes(
        self, cfg: BucketRetentionConfig
    ) -> None:
        """Deny uploads outside *cfg*'s retention prefixes.

        The trailing ``*`` matches zero or more characters, so a zero-byte
        "folder marker" whose key is exactly the prefix is still permitted.
        ``s3:PutObject`` also authorizes creating, uploading and completing
        a multipart upload, so the Deny covers those without blocking the
        abort that cleans up a stranded one.

        Raises ``ValueError`` when CDK cannot attach the statement, as it
        cannot for a bucket created elsewhere.
        """
        allowed_arns = [
            f"{cfg.bucket.bucket_arn}/{rule.path_prefix}*"
            for rule in cfg.retention_rules
        ]
        result = cfg.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyUploadsOutsideAllowedPrefixes",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject"],
                not_resources=allowed_arns,
            )
        )
        if not result.statement_added:
            raise ValueError(
                f"Bucket '{cfg.bucket.node.id}' is declared EXCLUSIVE but the "
                "prefix Deny could not be attached to it. Declare it SHARED, "
                "or pass a bucket created in this app."
            )

    def _enable_access_logging(
        self, bucket_configs: list[BucketRetentionConfig]
    ) -> None:
        """Deliver server access logs of ``EXCLUSIVE`` buckets to CloudWatch.

        ``SHARED`` buckets are skipped: their access logs cover every
        request, not only the retention prefixes, so that is the owner's call.

        See: https://docs.aws.amazon.com/AmazonS3/latest/userguide/sal-cw-enabling.html
        """
        owned = [
            cfg
            for cfg in bucket_configs
            if cfg.ownership is BucketOwnership.EXCLUSIVE
        ]
        for idx, cfg in enumerate(owned):
            bucket = cfg.bucket
            suffix = f"-{idx}" if len(owned) > 1 else ""

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
