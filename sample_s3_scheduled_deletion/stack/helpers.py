# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Naming helpers and reusable data models for the CDK stack.

This module contains utility functions for building standardized resource
names and dataclasses that describe configuration shapes.
"""

import uuid
from dataclasses import dataclass

# --- Naming helpers ----------------------------------------------------------


def make_name_namespace(project_prefix: str) -> uuid.UUID:
    """Create a deterministic UUID namespace from a project prefix.

    Using uuid5 keeps the hash suffix stable across deploys so that
    resources with generated names are not replaced on every deploy.
    """
    return uuid.uuid5(uuid.NAMESPACE_DNS, project_prefix)


def resource_name(project_prefix: str, component: str) -> str:
    """Build a standardized resource name: ``<prefix>-<component>``.

    Used for resources whose names only need to be unique within an
    account/region (Lambdas, IAM roles, SQS queues, EventBridge rules,
    schedule groups). Example: ``sample-s3-scheduled-deletion-deletion-fn``.
    """
    return f"{project_prefix}-{component}"


def create_bucket_name(
    project_prefix: str,
    component: str,
    *,
    account: str,
    region: str,
    name_hash_length: int = 8,
) -> str:
    """Build a globally-unique S3 bucket name: ``<prefix>-<component>-<hash>``.

    S3 bucket names must be unique across ALL AWS accounts, so a hash is
    appended. The hash is the first ``name_hash_length`` hex characters of a
    UUID seeded by the project prefix, component, account and region. The
    deterministic seed keeps the name stable across deploys (so the bucket is
    not replaced) while differing per environment.

    Example: ``sample-s3-scheduled-deletion-data-3f2a9c1b``
    """
    namespace = make_name_namespace(project_prefix)
    seed = f"{component}:{account}:{region}"
    short_hash = uuid.uuid5(namespace, seed).hex[:name_hash_length]
    return f"{project_prefix}-{component}-{short_hash}"


# --- Data models -------------------------------------------------------------


@dataclass(frozen=True)
class RetentionRuleConfig:
    """A single path-prefix retention rule."""

    path_prefix: str  # e.g. "retention-5-mins/"
    ttl_minutes: int  # object is deleted this many minutes after creation
