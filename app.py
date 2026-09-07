#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import aws_cdk as cdk
from aws_cdk import Aspects
from cdk_nag import AwsSolutionsChecks

from sample_s3_scheduled_deletion.stack.main_stack import (
    SampleS3ScheduledDeletionStack,
)

app = cdk.App()

account = app.node.try_get_context("account")
if not account:
    raise ValueError(
        "Context parameter 'account' is required. Use: "
        "cdk synth -c account=111122223333 -c region=aa-example-1"
    )

region = app.node.try_get_context("region")
if not region:
    raise ValueError(
        "Context parameter 'region' is required. Use: "
        "cdk synth -c account=111122223333 -c region=aa-example-1"
    )

SampleS3ScheduledDeletionStack(
    app,
    "SampleS3ScheduledDeletionStack",
    env=cdk.Environment(account=account, region=region),
)

Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
