# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis tests for the S3 scheduled-deletion stack and construct."""

import aws_cdk as cdk
import aws_cdk.assertions as assertions
import pytest
from aws_cdk import Duration

from sample_s3_scheduled_deletion.stack.config import (
    DELETION_EVENT_BUS_NAME,
    DELETION_EVENT_LOG_GROUP_NAME,
    PROJECT_PREFIX,
)
from sample_s3_scheduled_deletion.stack.constructs.deletion import (
    RetentionRule,
)
from sample_s3_scheduled_deletion.stack.helpers import resource_name
from sample_s3_scheduled_deletion.stack.main_stack import (
    SampleS3ScheduledDeletionStack,
)

ACCOUNT_ID = "111122223333"
REGION = "aa-example-1"

SCHEDULE_GROUP_NAME = resource_name(PROJECT_PREFIX, "deletion-schedules")


# --- RetentionRule validation ----------------------------------------------

DEFAULT_LIFECYCLE = Duration.days(1)


def _validate(rules, lifecycle=DEFAULT_LIFECYCLE):
    RetentionRule._validate_rules(rules, lifecycle)


def test_valid_rules_pass():
    _validate(
        [
            RetentionRule("uploads/", Duration.hours(2)),
            RetentionRule("reports/", Duration.hours(6)),
        ]
    )


@pytest.mark.parametrize(
    "rules, lifecycle, match",
    [
        ([RetentionRule("", Duration.hours(1))], DEFAULT_LIFECYCLE, "empty"),
        (
            [RetentionRule("no-slash", Duration.hours(1))],
            DEFAULT_LIFECYCLE,
            "end with",
        ),
        (
            [RetentionRule("data/", Duration.hours(0))],
            DEFAULT_LIFECYCLE,
            "at least 2 minutes",
        ),
        (
            [RetentionRule("data/", Duration.seconds(119))],
            DEFAULT_LIFECYCLE,
            "at least 2 minutes",
        ),
        (
            [RetentionRule("data/", Duration.days(2))],
            DEFAULT_LIFECYCLE,
            "less than",
        ),
        (
            [
                RetentionRule("uploads/", Duration.hours(1)),
                RetentionRule("uploads/", Duration.hours(2)),
            ],
            DEFAULT_LIFECYCLE,
            "Duplicate",
        ),
    ],
)
def test_invalid_config_rejected(rules, lifecycle, match):
    with pytest.raises(ValueError, match=match):
        _validate(rules, lifecycle=lifecycle)


# --- Stack synthesis --------------------------------------------------------


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    stack = SampleS3ScheduledDeletionStack(
        app,
        "TestStack",
        env=cdk.Environment(account=ACCOUNT_ID, region=REGION),
    )
    return assertions.Template.from_stack(stack)


def test_creates_two_lambda_functions(template):
    fns = template.find_resources(
        "AWS::Lambda::Function",
        {
            "Properties": assertions.Match.object_like(
                {"Architectures": ["arm64"], "Runtime": "python3.14"}
            )
        },
    )
    assert len(fns) == 2


def test_creates_schedule_group(template):
    template.has_resource_properties(
        "AWS::Scheduler::ScheduleGroup",
        assertions.Match.object_like({"Name": SCHEDULE_GROUP_NAME}),
    )


def test_creates_dlq(template):
    queues = template.find_resources("AWS::SQS::Queue")
    assert len(queues) >= 1


def test_creates_eventbridge_rule(template):
    rules = template.find_resources("AWS::Events::Rule")
    assert len(rules) >= 1


def test_lifecycle_lambda_environment(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        assertions.Match.object_like(
            {
                "Environment": {
                    "Variables": assertions.Match.object_like(
                        {
                            "REPOSITORY_CONFIG": assertions.Match.any_value(),
                            "PROJECT_PREFIX": "sample-s3-scheduled-deletion",
                            "SCHEDULE_GROUP_NAME": SCHEDULE_GROUP_NAME,
                            "SCHEDULE_ROLE_ARN": assertions.Match.any_value(),
                            "DELETION_FUNCTION_ARN": assertions.Match.any_value(),
                            "DATA_EVENT_BUS_NAME": DELETION_EVENT_BUS_NAME,
                        }
                    )
                }
            }
        ),
    )


def test_creates_custom_event_bus(template):
    template.has_resource_properties(
        "AWS::Events::EventBus",
        assertions.Match.object_like({"Name": DELETION_EVENT_BUS_NAME}),
    )


def test_creates_deletion_event_log_group(template):
    template.has_resource_properties(
        "AWS::Logs::LogGroup",
        assertions.Match.object_like(
            {"LogGroupName": DELETION_EVENT_LOG_GROUP_NAME}
        ),
    )


def test_log_rule_routes_lambda_events_from_custom_bus(template):
    rules = template.find_resources(
        "AWS::Events::Rule",
        {
            "Properties": assertions.Match.object_like(
                {
                    "EventBusName": assertions.Match.any_value(),
                    "EventPattern": {
                        "source": [
                            "aws.s3",
                            "retention.lifecycle",
                            "retention.deletion",
                        ]
                    },
                }
            )
        },
    )
    assert len(rules) == 1


def test_bucket_has_eventbridge_enabled(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        assertions.Match.object_like(
            {"NotificationConfiguration": {"EventBridgeConfiguration": {}}}
        ),
    )


def test_access_log_groups_are_unique_per_bucket():
    from aws_cdk import aws_s3 as s3

    from sample_s3_scheduled_deletion.stack.constructs.deletion import (
        BucketRetentionConfig,
        S3DataDeletion,
    )

    app = cdk.App()
    stack = cdk.Stack(
        app,
        "MultiBucketTestStack",
        env=cdk.Environment(account=ACCOUNT_ID, region=REGION),
    )
    bucket_names = ["first-access-log-bucket", "second-access-log-bucket"]
    buckets = [
        s3.Bucket(stack, f"Bucket{index}", bucket_name=bucket_name)
        for index, bucket_name in enumerate(bucket_names)
    ]

    S3DataDeletion(
        stack,
        "S3DataDeletion",
        bucket_configs=[
            BucketRetentionConfig(
                bucket=bucket,
                retention_rules=[RetentionRule("uploads/", Duration.hours(1))],
            )
            for bucket in buckets
        ],
        metrics_namespace="MultiBucketTest",
    )

    synthesized_template = assertions.Template.from_stack(stack)
    bucket_logical_ids = sorted(
        synthesized_template.find_resources("AWS::S3::Bucket")
    )
    log_groups = synthesized_template.find_resources("AWS::Logs::LogGroup")
    access_log_group_bucket_refs = []

    for resource in log_groups.values():
        log_group_name = resource.get("Properties", {}).get("LogGroupName")
        if not isinstance(log_group_name, dict):
            continue

        join_parts = log_group_name.get("Fn::Join")
        if join_parts and join_parts[1][0] == "/aws/s3/server-access/":
            access_log_group_bucket_refs.append(join_parts[1][1]["Ref"])

    assert sorted(access_log_group_bucket_refs) == bucket_logical_ids


def _checkov_skip_ids(resource):
    return {
        suppression["id"]
        for suppression in resource.get("Metadata", {})
        .get("checkov", {})
        .get("skip", [])
    }


def test_checkov_suppressions_are_resource_scoped(template):
    log_groups = template.find_resources("AWS::Logs::LogGroup")
    assert len(log_groups) == 4
    assert all(
        _checkov_skip_ids(resource) == {"CKV_AWS_158"}
        for resource in log_groups.values()
    )

    functions = template.find_resources("AWS::Lambda::Function")
    assert len(functions) == 2
    assert all(
        _checkov_skip_ids(resource)
        == {"CKV_AWS_116", "CKV_AWS_117", "CKV_AWS_173"}
        for resource in functions.values()
    )


def test_lambda_async_invocation_failures_use_shared_dlq(template):
    function_ids = set(template.find_resources("AWS::Lambda::Function"))
    queue_ids = set(template.find_resources("AWS::SQS::Queue"))
    invoke_configs = template.find_resources("AWS::Lambda::EventInvokeConfig")

    assert len(invoke_configs) == 2
    configured_function_ids = set()
    failure_destinations = set()

    for resource in invoke_configs.values():
        properties = resource["Properties"]
        assert properties["Qualifier"] == "$LATEST"
        configured_function_ids.add(properties["FunctionName"]["Ref"])
        destination = properties["DestinationConfig"]["OnFailure"][
            "Destination"
        ]["Fn::GetAtt"]
        failure_destinations.add(tuple(destination))

    assert configured_function_ids == function_ids
    assert len(failure_destinations) == 1
    destination_id, destination_attribute = failure_destinations.pop()
    assert destination_id in queue_ids
    assert destination_attribute == "Arn"
