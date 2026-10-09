# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Log group construct with this sample's defaults."""

from aws_cdk import RemovalPolicy
from aws_cdk import aws_logs as logs
from constructs import Construct


class CustomLogGroup(logs.LogGroup):
    """Log group with six-month retention, destroyed with the stack.

    Both defaults can be overridden. Checkov's CKV_AWS_158 (no customer
    managed key) is suppressed: CloudWatch Logs already encrypts at rest.
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
