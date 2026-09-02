# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Custom LogGroup construct with secure defaults and Checkov suppression.

Wraps ``aws_cdk.aws_logs.LogGroup`` to enforce six-month retention,
DESTROY removal policy, and auto-suppress the CKV_AWS_158 finding
(customer-managed KMS key not required for this sample).
"""

from aws_cdk import RemovalPolicy
from aws_cdk import aws_logs as logs
from constructs import Construct


class CustomLogGroup(logs.LogGroup):
    """LogGroup with opinionated defaults for this project.

    - retention: SIX_MONTHS
    - removal_policy: DESTROY
    - Checkov CKV_AWS_158 suppression applied automatically
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ):
        kwargs.setdefault("retention", logs.RetentionDays.SIX_MONTHS)
        kwargs.setdefault("removal_policy", RemovalPolicy.DESTROY)

        super().__init__(scope, construct_id, **kwargs)

        cfn_log_group = self.node.default_child
        cfn_log_group.add_metadata(
            "checkov",
            {
                "skip": [
                    {
                        "id": "CKV_AWS_158",
                        "comment": (
                            "CloudWatch Logs encrypts log data at rest by default. "
                            "A CMK KMS key is out of scope for this sample."
                        ),
                    }
                ]
            },
        )
