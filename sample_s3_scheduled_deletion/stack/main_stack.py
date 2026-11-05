# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Self-contained stack that provisions the S3 scheduled-deletion sample."""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_s3 as s3
from cdk_nag import NagSuppressions
from constructs import Construct

from . import config
from .constructs.deletion import (
    BucketOwnership,
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
        bucket, ownership = self._resolve_bucket(lifecycle_expiration)

        bucket_retention = BucketRetentionConfig(
            bucket=bucket,
            retention_rules=[
                RetentionRule(
                    path_prefix=rule.path_prefix,
                    ttl=Duration.minutes(rule.ttl_minutes),
                )
                for rule in config.RETENTION_RULES
            ],
            ownership=ownership,
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
            max_retention=Duration.minutes(config.MAX_RETENTION_MINUTES),
            default_lifecycle_expiration=lifecycle_expiration,
        )

    def _resolve_bucket(
        self, expiration: Duration
    ) -> tuple[s3.IBucket, BucketOwnership]:
        """Import the bucket named in config, or create one this stack owns."""
        if config.EXISTING_BUCKET_NAME:
            return (
                s3.Bucket.from_bucket_name(
                    self, "DataBucket", config.EXISTING_BUCKET_NAME
                ),
                BucketOwnership.SHARED,
            )

        return (
            self._create_bucket_with_eventbridge(
                "DataBucket",
                expiration,
                config.BUCKET_COMPONENT,
            ),
            BucketOwnership.EXCLUSIVE,
        )

    def _create_bucket_with_eventbridge(
        self,
        construct_id: str,
        expiration: Duration,
        component: str | None = None,
    ) -> s3.Bucket:
        """Create a cdk-nag compliant bucket with EventBridge notifications.

        The bucket uses S3-managed encryption, enforces SSL-only access and
        blocks all public access.  EventBridge notifications are enabled
        through an L1 override to avoid the CDK BucketNotifications
        custom-resource Lambda.

        If ``component`` is provided, the bucket gets a standardized name
        ``<prefix>-<component>-<hash>`` (for example,
        ``sample-s3-scheduled-deletion-data-3f2a9c1b``); the hash is stable
        for each account and Region. Otherwise CDK auto-generates the name.
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
        # Backstop for objects the Deletion Lambda never gets to.
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
