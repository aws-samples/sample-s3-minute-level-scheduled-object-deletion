# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Configuration for the S3 scheduled-deletion sample.

Edit this file to adapt the sample to your own buckets and retention rules.
This module contains high-level stack configurations.
"""

from .helpers import RetentionRuleConfig, resource_name

# --- Naming / observability -------------------------------------------------
# At most 38 characters; the stack names its resources from it.
PROJECT_PREFIX = "sample-s3-scheduled-deletion"
METRICS_NAMESPACE = "SampleS3ScheduledDeletion"

# Number of hex characters of the UUID used as the resource name suffix.
NAME_HASH_LENGTH = 8

# Lambda log level (POWERTOOLS_LOG_LEVEL).
LOG_LEVEL = "INFO"

# Amazon S3-generated "Object Created" events are delivered only to the
# default EventBridge bus, so there is a rule forwarding certain S3
# events from the default to the custom event bus. The latter is also used
# to publish lifecycle/deletion events from the Lambda functions and log them
# to a central CloudWatch log group.
DELETION_EVENT_BUS_NAME = resource_name(PROJECT_PREFIX, "deletion-events")

# CloudWatch log group that captures events published to the custom bus.
DELETION_EVENT_LOG_GROUP_NAME = f"/aws/events/{DELETION_EVENT_BUS_NAME}"

# Duration of the S3 lifecycle expiration on the buckets, applied to both
# current and noncurrent object versions.
# Intended as an asynchronous fallback in case of unexpected errors during
# the scheduled deletion.
DEFAULT_LIFECYCLE_EXPIRATION_DAYS = 1

# Longest retention a single object may be granted, in minutes. This is the
# ceiling applied to an object's ``expireAt`` metadata, and the upper bound
# for every retention rule TTL.
MAX_RETENTION_MINUTES = 720  # 12 hours


# --- Bucket + retention rules ----------------------------------------------

# Component name used to build the bucket name (<prefix>-<component>-<hash>).
BUCKET_COMPONENT = "data"

# Name of an existing bucket to manage instead of creating one. Leave as None
# to have the stack create and own its bucket.
# See "Using an existing bucket" in the README.
EXISTING_BUCKET_NAME: str | None = None

# Retention rules for the managed bucket. Each prefix gets its own TTL.
# In a stack-created bucket, uploads outside these prefixes are rejected; in
# an existing bucket, other prefixes are ignored.
# The prefix name does not have to reflect the TTL, rather, it could be
# descriptive of the data contained. Here it desscribes the TTL just
# for demonstrative purposes.
RETENTION_RULES: list[RetentionRuleConfig] = [
    RetentionRuleConfig(path_prefix="retention-5-mins/", ttl_minutes=5),
    RetentionRuleConfig(path_prefix="retention-2-mins/", ttl_minutes=2),
]
