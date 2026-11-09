# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Custom Lambda Function construct with a dedicated, narrowly scoped role.

Each function has a dedicated IAM role and log group with scoped logging permissions.
"""

from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from constructs import Construct

from ..config import PROJECT_PREFIX
from ..helpers import resource_name
from .log_group import CustomLogGroup


class CustomFunction(_lambda.Function):
    """Lambda Function with a dedicated role, log group and scoped permissions."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        component: str | None = None,
        **kwargs,
    ):
        function_name = log_group_name = None
        if component:
            function_name = resource_name(PROJECT_PREFIX, component)
            # Conventional /aws/lambda/<function-name> prefix keeps the
            # console "Monitor" tab log link working while still embedding
            # the standardized function name.
            log_group_name = f"/aws/lambda/{function_name}"

        role = iam.Role(
            scope,
            f"{construct_id}Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )

        log_group = CustomLogGroup(
            scope,
            f"{construct_id}LogGroup",
            log_group_name=log_group_name,
        )

        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[log_group.log_group_arn],
                effect=iam.Effect.ALLOW,
            )
        )

        super().__init__(
            scope,
            construct_id,
            function_name=function_name,
            role=role,
            log_group=log_group,
            architecture=_lambda.Architecture.ARM_64,
            runtime=_lambda.Runtime.PYTHON_3_14,
            **kwargs,
        )
        cfn_function = self.node.default_child
        cfn_function.add_metadata(
            "checkov",
            {
                "skip": [
                    {
                        "id": "CKV_AWS_117",
                        "comment": (
                            "Access to resources in VPC for AWS Lambda functions "
                            "is out of scope for this sample."
                        ),
                    },
                    {
                        "id": "CKV_AWS_173",
                        "comment": (
                            "AWS Lambda already encrypts environment variables."
                            "CMK KMS encryption is out of scope for this sample."
                        ),
                    },
                    {
                        "id": "CKV_AWS_116",
                        "comment": (
                            "An SQS DLQ is configured as destination for failed "
                            "asynchronous invocations."
                        ),
                    },
                ]
            },
        )
