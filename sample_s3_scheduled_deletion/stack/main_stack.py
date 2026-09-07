# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Self-contained stack that provisions the S3 scheduled-deletion sample."""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_s3 as s3
from cdk_nag import NagSuppressions
from constructs import Construct

from . import config
from .constructs.deletion import (
    BucketRetentionConfig,
    RetentionRule,
    S3DataDeletion,
)
from .helpers import create_bucket_name


class SampleS3ScheduledDeletionStack(Stack):
    """Creates the managed bucket(s) and the scheduled-deletion service."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("project", config.PROJECT_PREFIX)

        lifecycle_expiration = Duration.days(
            config.DEFAULT_LIFECYCLE_EXPIRATION_DAYS
        )

        # --- Single managed bucket ---
        bucket = self._create_bucket_with_eventbridge(
            "DataBucket",
            lifecycle_expiration,
            config.BUCKET_COMPONENT,
        )

        bucket_retention = BucketRetentionConfig(
            bucket=bucket,
            retention_rules=[
                RetentionRule(
                    path_prefix=rule.path_prefix,
                    ttl=Duration.minutes(rule.ttl_minutes),
                )
                for rule in config.RETENTION_RULES
            ],
        )

        CfnOutput(
            self,
            "DataBucketName",
            value=bucket.bucket_name,
            description="Name of the managed S3 bucket.",
        )

        self.s3_data_deletion = S3DataDeletion(
            self,
            "S3DataDeletion",
            bucket_configs=[bucket_retention],
            metrics_namespace=config.METRICS_NAMESPACE,
            default_lifecycle_expiration=lifecycle_expiration,
        )

        # Deletion Lambda name, so an on-demand deletion can be triggered by
        # invoking the function directly.
        CfnOutput(
            self,
            "DeletionFunctionName",
            value=self.s3_data_deletion.deletion_function.function_name,
            description=(
                "Name of the Deletion Lambda (invoke directly for "
                "on-demand deletions)."
            ),
        )

    def _create_bucket_with_eventbridge(
        self,
        construct_id: str,
        expiration: Duration,
        component: str | None = None,
    ) -> s3.Bucket:
        """Create a cdk-nag compliant bucket with EventBridge notifications.

        The bucket uses S3-managed encryption, enforces SSL-only access and
        blocks all public access.  EventBridge notifications are enabled via
        an L1 override to avoid the CDK BucketNotifications custom-resource
        Lambda.

        If ``component`` is provided, the bucket gets a standardized name
        ``<prefix>-<component>-<hash>`` (e.g.
        ``sample-s3-scheduled-deletion-data-3f2a9c1b``); the hash is stable
        per account/region. Otherwise CDK auto-generates the name.
        """
        bucket_name = (
            create_bucket_name(
                config.PROJECT_PREFIX,
                component,
                account=self.account,
                region=self.region,
                name_hash_length=config.NAME_HASH_LENGTH,
            )
            if component
            else None
        )
        bucket = s3.Bucket(
            self,
            f"{construct_id}",
            bucket_name=bucket_name,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
        )
        bucket.add_lifecycle_rule(
            expiration=expiration,
            noncurrent_version_expiration=expiration,
        )

        cfn_bucket = bucket.node.default_child
        cfn_bucket.notification_configuration = (
            s3.CfnBucket.NotificationConfigurationProperty(
                event_bridge_configuration=(
                    s3.CfnBucket.EventBridgeConfigurationProperty(
                        event_bridge_enabled=True,
                    )
                ),
            )
        )

        NagSuppressions.add_resource_suppressions(
            bucket,
            suppressions=[
                {
                    "id": "AwsSolutions-S1",
                    "reason": ("Server access logging enabled to CloudWatch."),
                },
            ],
        )

        # Checkov suppression for the same rule (CKV_AWS_18)
        cfn_bucket.add_metadata(
            "checkov",
            {
                "skip": [
                    {
                        "id": "CKV_AWS_18",
                        "comment": "Server access logging enabled to CloudWatch.",
                    }
                ]
            },
        )

        return bucket
