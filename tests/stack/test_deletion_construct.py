# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis tests for the S3 scheduled-deletion stack and construct."""

import json

import aws_cdk as cdk
import aws_cdk.assertions as assertions
import pytest
from aws_cdk import Duration
from aws_cdk import aws_s3 as s3

from sample_s3_scheduled_deletion.stack import config
from sample_s3_scheduled_deletion.stack.config import (
    DELETION_EVENT_BUS_NAME,
    DELETION_EVENT_LOG_GROUP_NAME,
    PROJECT_PREFIX,
)
from sample_s3_scheduled_deletion.stack.constructs import deletion
from sample_s3_scheduled_deletion.stack.constructs.deletion import (
    MAX_PROJECT_PREFIX_LENGTH,
    BucketOwnership,
    BucketRetentionConfig,
    RetentionRule,
    S3DataDeletion,
    _validate_project_prefix,
    _validate_retention_bounds,
)
from sample_s3_scheduled_deletion.stack.custom_resources import (
    lambda_function,
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
MAX_RETENTION = Duration.minutes(720)


def _validate(rules, max_retention=MAX_RETENTION):
    RetentionRule._validate_rules(rules, max_retention)


def test_valid_rules_pass():
    _validate(
        [
            RetentionRule("uploads/", Duration.hours(2)),
            RetentionRule("reports/", Duration.hours(6)),
        ]
    )


@pytest.mark.parametrize(
    "rules, max_retention, match",
    [
        ([RetentionRule("", Duration.hours(1))], MAX_RETENTION, "empty"),
        (
            [RetentionRule("no-slash", Duration.hours(1))],
            MAX_RETENTION,
            "end with",
        ),
        (
            [RetentionRule("data/", Duration.hours(0))],
            MAX_RETENTION,
            "at least 2 minutes",
        ),
        (
            [RetentionRule("data/", Duration.seconds(119))],
            MAX_RETENTION,
            "at least 2 minutes",
        ),
        (
            [RetentionRule("data/", Duration.hours(13))],
            MAX_RETENTION,
            "must not exceed max_retention",
        ),
        (
            [
                RetentionRule("uploads/", Duration.hours(1)),
                RetentionRule("uploads/", Duration.hours(2)),
            ],
            MAX_RETENTION,
            "Duplicate",
        ),
        # No rules denies every upload and manages nothing.
        ([], MAX_RETENTION, "at least one RetentionRule"),
    ],
)
def test_invalid_config_rejected(rules, max_retention, match):
    with pytest.raises(ValueError, match=match):
        _validate(rules, max_retention=max_retention)


def test_rule_ttl_may_equal_max_retention():
    """The ceiling is inclusive for rule TTLs."""
    _validate([RetentionRule("data/", MAX_RETENTION)])


# --- Retention ceiling and Lifecycle backstop -------------------------------


def _validate_bounds(max_retention, lifecycle=DEFAULT_LIFECYCLE):
    _validate_retention_bounds(max_retention, lifecycle)


def test_ceiling_below_backstop_is_valid():
    _validate_bounds(Duration.hours(12))


def test_ceiling_equal_to_backstop_is_valid():
    """The two are independent; equality is the boundary, not a conflict."""
    _validate_bounds(Duration.days(1))


def test_ceiling_above_backstop_is_rejected():
    """A ceiling past the backstop would let Lifecycle delete objects early."""
    with pytest.raises(
        ValueError, match="must not exceed default_lifecycle_expiration"
    ):
        _validate_bounds(Duration.days(2))


def test_ceiling_below_scheduler_precision_is_rejected():
    with pytest.raises(ValueError, match="at least 2 minutes"):
        _validate_bounds(Duration.minutes(1))


def test_ceiling_is_independent_of_the_backstop_granularity():
    """A multi-day backstop with minute-level retention is the point."""
    _validate_bounds(Duration.minutes(2160), lifecycle=Duration.days(3))


# --- Stack synthesis --------------------------------------------------------


@pytest.fixture(scope="module")
def template():
    """Synthesize the sample's default configuration: one EXCLUSIVE bucket.

    EXISTING_BUCKET_NAME is pinned to None so that these assertions describe
    the shipped sample rather than whichever bucket a local config.py happens
    to point at.
    """
    original = config.EXISTING_BUCKET_NAME
    config.EXISTING_BUCKET_NAME = None
    try:
        app = cdk.App()
        stack = SampleS3ScheduledDeletionStack(
            app,
            "TestStack",
            env=cdk.Environment(account=ACCOUNT_ID, region=REGION),
        )
        return assertions.Template.from_stack(stack)
    finally:
        config.EXISTING_BUCKET_NAME = original


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


def test_custom_bus_denies_foreign_put_events(template):
    """The custom bus carries a resource policy denying events:PutEvents to
    any principal other than the known publishers -- the two Lambda execution
    roles and the forward-rule delivery role (Security Hub EventBridge.3) --
    aimed at closing the forged-event injection vector."""
    policies = template.find_resources(
        "AWS::Events::EventBusPolicy",
        {
            "Properties": assertions.Match.object_like(
                {
                    "Statement": assertions.Match.object_like(
                        {
                            "Effect": "Deny",
                            "Action": "events:PutEvents",
                            "Condition": {
                                "ArnNotEquals": {
                                    "aws:PrincipalArn": (
                                        assertions.Match.any_value()
                                    )
                                }
                            },
                        }
                    )
                }
            )
        },
    )
    assert len(policies) == 1

    # The allow-list names exactly three principals: the lifecycle role, the
    # deletion role, and the forward-rule delivery role.
    (props,) = policies.values()
    allowed = props["Properties"]["Statement"]["Condition"]["ArnNotEquals"][
        "aws:PrincipalArn"
    ]
    assert len(allowed) == 3


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
                ownership=BucketOwnership.EXCLUSIVE,
            )
            for bucket in buckets
        ],
        metrics_namespace="MultiBucketTest",
        max_retention=MAX_RETENTION,
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


# --- Ownership modes --------------------------------------------------------

SHARED_RULES = [
    RetentionRule("managed/", Duration.hours(1)),
    RetentionRule("also-managed/", Duration.hours(2)),
]


def _synth_with_ownership(ownership, *, imported, rules=None):
    """Synthesize a one-bucket stack in *ownership* mode."""
    app = cdk.App()
    stack = cdk.Stack(
        app,
        "OwnershipStack",
        env=cdk.Environment(account=ACCOUNT_ID, region=REGION),
    )
    bucket = (
        s3.Bucket.from_bucket_name(stack, "Bucket", "pre-existing-bucket")
        if imported
        else s3.Bucket(stack, "Bucket", versioned=True)
    )
    S3DataDeletion(
        stack,
        "S3DataDeletion",
        bucket_configs=[
            BucketRetentionConfig(
                bucket=bucket,
                retention_rules=list(rules or SHARED_RULES),
                ownership=ownership,
            )
        ],
        metrics_namespace="OwnershipTest",
        max_retention=MAX_RETENTION,
    )
    return assertions.Template.from_stack(stack)


def _prefix_deny_statements(template):
    return [
        statement
        for policy in template.find_resources("AWS::S3::BucketPolicy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if statement.get("Sid") == "DenyUploadsOutsideAllowedPrefixes"
    ]


def _flatten(value):
    """Collapse a synthesized value to a string, intrinsics as ``<token>``.

    A bucket created by CDK contributes a ``Ref``, and an imported bucket's
    ARN contains ``AWS::Partition``, so these values are ``Fn::Join`` dicts
    rather than plain strings.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_flatten(part) for part in value)
    if isinstance(value, dict):
        join = value.get("Fn::Join")
        if join is None:
            return "<token>"
        separator, parts = join
        return separator.join(_flatten(part) for part in parts)
    return str(value)


def _granted_object_paths(template):
    """Object-level S3 grants on the Lambda roles, as ``bucket/key`` globs."""
    paths = set()
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if "s3:DeleteObject" not in statement.get("Action", []):
                continue
            entries = statement["Resource"]
            if not isinstance(entries, list):
                entries = [entries]
            paths.update(_flatten(entry).split(":::")[-1] for entry in entries)
    return paths


def test_exclusive_bucket_denies_uploads_outside_prefixes():
    statements = _prefix_deny_statements(
        _synth_with_ownership(BucketOwnership.EXCLUSIVE, imported=False)
    )

    assert len(statements) == 1
    assert statements[0]["Effect"] == "Deny"
    # Denying the abort would only block cleanup of stranded uploads.
    assert statements[0]["Action"] == "s3:PutObject"
    assert len(statements[0]["NotResource"]) == len(SHARED_RULES)


def test_shared_bucket_has_no_bucket_policy():
    """Other prefixes belong to somebody else; do not police their uploads."""
    template = _synth_with_ownership(BucketOwnership.SHARED, imported=True)

    assert _prefix_deny_statements(template) == []
    assert template.find_resources("AWS::S3::BucketPolicy") == {}


def test_exclusive_bucket_must_accept_the_deny():
    """An imported bucket silently drops it, so refuse the combination."""
    with pytest.raises(ValueError, match="declared EXCLUSIVE"):
        _synth_with_ownership(BucketOwnership.EXCLUSIVE, imported=True)


def test_exclusive_grants_reach_the_whole_bucket():
    """Deleting unmatched objects needs reach beyond the prefixes."""
    granted = _granted_object_paths(
        _synth_with_ownership(BucketOwnership.EXCLUSIVE, imported=False)
    )

    assert granted == {"<token>/*"}


def test_shared_grants_reach_only_the_managed_prefixes():
    """IAM, not Lambda logic, keeps unmanaged prefixes out of reach."""
    granted = _granted_object_paths(
        _synth_with_ownership(BucketOwnership.SHARED, imported=True)
    )

    assert granted == {
        "pre-existing-bucket/managed/*",
        "pre-existing-bucket/also-managed/*",
    }


@pytest.mark.parametrize(
    "ownership, imported, expected",
    [
        (BucketOwnership.EXCLUSIVE, False, "true"),
        (BucketOwnership.SHARED, True, "false"),
    ],
    ids=["exclusive", "shared"],
)
def test_repository_config_carries_the_unmatched_decision(
    ownership, imported, expected
):
    """The Lambda must not have to re-derive policy from the mode name."""
    template = _synth_with_ownership(ownership, imported=imported)
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {
            "Properties": assertions.Match.object_like(
                {
                    "Environment": {
                        "Variables": assertions.Match.object_like(
                            {"REPOSITORY_CONFIG": assertions.Match.any_value()}
                        )
                    }
                }
            )
        },
    )

    assert len(functions) == 1
    raw = next(iter(functions.values()))["Properties"]["Environment"][
        "Variables"
    ]["REPOSITORY_CONFIG"]
    # The bucket name is a token, so the value synthesizes as an Fn::Join
    # rather than a JSON string and cannot be parsed back.
    config_json = _flatten(raw)

    assert config_json.count(f'"delete_unmatched": {expected}') == len(
        SHARED_RULES
    )
    assert (
        f'"delete_unmatched": {"false" if expected == "true" else "true"}'
        not in config_json
    )


def test_shared_bucket_gets_no_access_log_delivery():
    """Access logs cover the whole bucket, so logging it is the owner's call."""
    template = _synth_with_ownership(BucketOwnership.SHARED, imported=True)

    for resource_type in (
        "AWS::Logs::DeliverySource",
        "AWS::Logs::DeliveryDestination",
        "AWS::Logs::Delivery",
    ):
        assert template.find_resources(resource_type) == {}, resource_type

    log_group_names = [
        _flatten(resource["Properties"].get("LogGroupName", ""))
        for resource in template.find_resources("AWS::Logs::LogGroup").values()
    ]
    assert not [name for name in log_group_names if "server-access" in name]


def test_exclusive_bucket_still_gets_access_log_delivery():
    template = _synth_with_ownership(BucketOwnership.EXCLUSIVE, imported=False)

    assert len(template.find_resources("AWS::Logs::DeliverySource")) == 1
    assert len(template.find_resources("AWS::Logs::Delivery")) == 1


def test_access_log_delivery_source_name_is_unsuffixed_for_one_owned_bucket():
    """A SHARED bucket alongside an EXCLUSIVE one must not rename its source.

    The delivery source name is its physical ID, and CloudWatch Logs rejects
    repointing an existing source at a different bucket.
    """
    app = cdk.App()
    stack = cdk.Stack(
        app,
        "MixedOwnershipStack",
        env=cdk.Environment(account=ACCOUNT_ID, region=REGION),
    )
    rules = [RetentionRule("managed/", Duration.hours(1))]
    S3DataDeletion(
        stack,
        "S3DataDeletion",
        bucket_configs=[
            BucketRetentionConfig(
                bucket=s3.Bucket(stack, "Owned", versioned=True),
                retention_rules=list(rules),
                ownership=BucketOwnership.EXCLUSIVE,
            ),
            BucketRetentionConfig(
                bucket=s3.Bucket.from_bucket_name(
                    stack, "Imported", "pre-existing-bucket"
                ),
                retention_rules=list(rules),
                ownership=BucketOwnership.SHARED,
            ),
        ],
        metrics_namespace="MixedTest",
        max_retention=MAX_RETENTION,
    )

    sources = assertions.Template.from_stack(stack).find_resources(
        "AWS::Logs::DeliverySource"
    )

    assert len(sources) == 1
    assert next(iter(sources.values()))["Properties"]["Name"] == resource_name(
        PROJECT_PREFIX, "sal-source"
    )


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


# --- Retention ceiling handed to the Lifecycle Lambda -----------------------


def test_lifecycle_lambda_receives_the_retention_ceiling(template):
    """MAX_RETENTION_SECONDS comes from MAX_RETENTION_MINUTES, not the
    Lifecycle expiration."""
    expected = str(config.MAX_RETENTION_MINUTES * 60)
    assert expected != str(
        config.DEFAULT_LIFECYCLE_EXPIRATION_DAYS * 24 * 60 * 60
    ), "sample config should demonstrate the two being independent"
    template.has_resource_properties(
        "AWS::Lambda::Function",
        assertions.Match.object_like(
            {
                "Environment": {
                    "Variables": assertions.Match.object_like(
                        {"MAX_RETENTION_SECONDS": expected}
                    )
                }
            }
        ),
    )


# --- PROJECT_PREFIX length --------------------------------------------------

# Service limits on the physical names derived from PROJECT_PREFIX.
_NAME_LIMITS = {
    "AWS::Events::Rule": ("Name", 64),
    "AWS::Lambda::Function": ("FunctionName", 64),
    "AWS::Scheduler::ScheduleGroup": ("Name", 64),
    "AWS::SQS::Queue": ("QueueName", 80),
    "AWS::Logs::DeliverySource": ("Name", 60),
    "AWS::Logs::DeliveryDestination": ("Name", 60),
}


def _use_project_prefix(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    """Point every module that names resources at *prefix*."""
    monkeypatch.setattr(deletion, "PROJECT_PREFIX", prefix)
    monkeypatch.setattr(lambda_function, "PROJECT_PREFIX", prefix)


def test_sample_project_prefix_fits():
    _validate_project_prefix(config.PROJECT_PREFIX)


def test_project_prefix_over_the_limit_is_rejected():
    with pytest.raises(ValueError, match="at most 38 characters"):
        _validate_project_prefix("p" * (MAX_PROJECT_PREFIX_LENGTH + 1))


def test_construct_rejects_a_project_prefix_over_the_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    _use_project_prefix(monkeypatch, "p" * (MAX_PROJECT_PREFIX_LENGTH + 1))

    with pytest.raises(ValueError, match="PROJECT_PREFIX"):
        _synth_with_ownership(BucketOwnership.EXCLUSIVE, imported=False)


def test_derived_names_fit_their_limits_at_the_longest_prefix(
    monkeypatch: pytest.MonkeyPatch,
):
    """The limit is exact: every name fits, and one uses all of its room."""
    longest = "p" * MAX_PROJECT_PREFIX_LENGTH
    _use_project_prefix(monkeypatch, longest)
    template = _synth_with_ownership(BucketOwnership.EXCLUSIVE, imported=False)

    headroom: dict[str, int] = {}
    for resource_type, (name_property, limit) in _NAME_LIMITS.items():
        for resource in template.find_resources(resource_type).values():
            name = resource["Properties"].get(name_property)
            if isinstance(name, str) and name.startswith(longest):
                headroom[name] = limit - len(name)

    assert headroom, "no names derived from PROJECT_PREFIX were found"
    assert min(headroom.values()) == 0, headroom


# --- Alarms -----------------------------------------------------------------


def _alarms_by_metric(template: assertions.Template) -> dict[str, dict]:
    return {
        resource["Properties"]["MetricName"]: resource["Properties"]
        for resource in template.find_resources(
            "AWS::CloudWatch::Alarm"
        ).values()
    }


def test_alarms_cover_the_dlq_and_the_failure_metrics(template):
    alarms = _alarms_by_metric(template)

    assert set(alarms) == {
        "ApproximateNumberOfMessagesVisible",
        "DeletionError",
        "MetadataLookupFailed",
    }
    for properties in alarms.values():
        assert properties["Threshold"] == 1
        assert properties["ComparisonOperator"] == (
            "GreaterThanOrEqualToThreshold"
        )
        assert properties["TreatMissingData"] == "notBreaching"


def test_dlq_alarm_watches_the_dlq(template):
    (queue_id,) = template.find_resources("AWS::SQS::Queue")
    properties = _alarms_by_metric(template)[
        "ApproximateNumberOfMessagesVisible"
    ]

    assert properties["Namespace"] == "AWS/SQS"
    assert properties["Statistic"] == "Maximum"
    assert properties["Dimensions"] == [
        {
            "Name": "QueueName",
            "Value": {"Fn::GetAtt": [queue_id, "QueueName"]},
        }
    ]


@pytest.mark.parametrize("metric", ["DeletionError", "MetadataLookupFailed"])
def test_failure_metric_alarms_match_what_the_lambdas_emit(template, metric):
    """The Lambdas emit these without dimensions, including no ``service``."""
    properties = _alarms_by_metric(template)[metric]

    assert properties["Namespace"] == config.METRICS_NAMESPACE
    assert properties["Statistic"] == "Sum"
    assert "Dimensions" not in properties


def test_alarms_have_no_actions(template):
    """Where to send notifications is the deployer's choice."""
    for properties in _alarms_by_metric(template).values():
        assert "AlarmActions" not in properties


# --- Deletion Lambda scope --------------------------------------------------


@pytest.mark.parametrize(
    "ownership, imported, expected_prefixes",
    [
        (BucketOwnership.EXCLUSIVE, False, [""]),
        (
            BucketOwnership.SHARED,
            True,
            [rule.path_prefix for rule in SHARED_RULES],
        ),
    ],
    ids=["exclusive", "shared"],
)
def test_deletion_lambda_scope_matches_its_iam_scope(
    ownership, imported, expected_prefixes
):
    """The Lambda must refuse exactly what IAM would deny it."""
    template = _synth_with_ownership(ownership, imported=imported)
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {
            "Properties": assertions.Match.object_like(
                {
                    "Environment": {
                        "Variables": assertions.Match.object_like(
                            {"DELETION_SCOPE": assertions.Match.any_value()}
                        )
                    }
                }
            )
        },
    )

    assert len(functions) == 1
    raw = next(iter(functions.values()))["Properties"]["Environment"][
        "Variables"
    ]["DELETION_SCOPE"]
    # The bucket name is a token, so the value is an Fn::Join, not JSON.
    assert f'"key_prefixes": {json.dumps(expected_prefixes)}' in _flatten(raw)
    granted = {
        path.split("/", 1)[1] for path in _granted_object_paths(template)
    }
    assert granted == {f"{prefix}*" for prefix in expected_prefixes}


# --- Deletion Lambda invocation ---------------------------------------------


def _deletion_function_id(template: assertions.Template) -> str:
    """Logical ID of the Lambda that carries the deletion scope."""
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {
            "Properties": assertions.Match.object_like(
                {
                    "Environment": {
                        "Variables": assertions.Match.object_like(
                            {"DELETION_SCOPE": assertions.Match.any_value()}
                        )
                    }
                }
            )
        },
    )
    assert len(functions) == 1
    return next(iter(functions))


def _schedule_role_id(template: assertions.Template) -> str:
    """Logical ID of the role EventBridge Scheduler assumes."""
    roles = template.find_resources(
        "AWS::IAM::Role",
        {
            "Properties": assertions.Match.object_like(
                {
                    "AssumeRolePolicyDocument": assertions.Match.object_like(
                        {
                            "Statement": [
                                assertions.Match.object_like(
                                    {
                                        "Principal": {
                                            "Service": (
                                                "scheduler.amazonaws.com"
                                            )
                                        }
                                    }
                                )
                            ]
                        }
                    )
                }
            )
        },
    )
    assert len(roles) == 1
    return next(iter(roles))


def _invocation_template(
    mode: str, default: assertions.Template
) -> assertions.Template:
    """The default EXCLUSIVE stack, or one with an imported SHARED bucket."""
    if mode == "exclusive":
        return default
    return _synth_with_ownership(BucketOwnership.SHARED, imported=True)


@pytest.mark.parametrize("mode", ["exclusive", "shared"])
def test_deletion_lambda_resource_policy_denies_all_but_the_schedule_role(
    template: assertions.Template, mode: str
) -> None:
    synthesized = _invocation_template(mode, template)
    function_id = _deletion_function_id(synthesized)
    role_id = _schedule_role_id(synthesized)
    function_arn = {"Fn::GetAtt": [function_id, "Arn"]}

    # One Deny, no Allow: the role's identity policy grants the invoke.
    assert list(
        synthesized.find_resources("AWS::Lambda::ResourcePolicy").values()
    ) == [
        {
            "Type": "AWS::Lambda::ResourcePolicy",
            "Properties": {
                "ResourceArn": function_arn,
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "DenyInvokeExceptScheduleExecutionRole",
                            "Effect": "Deny",
                            "Principal": "*",
                            "Action": "lambda:InvokeFunction",
                            "Resource": [
                                function_arn,
                                {"Fn::Join": ["", [function_arn, ":*"]]},
                            ],
                            "Condition": {
                                "ArnNotEquals": {
                                    "aws:PrincipalArn": {
                                        "Fn::GetAtt": [role_id, "Arn"]
                                    }
                                },
                                # Lambda runs queued async events as itself.
                                "StringNotEqualsIfExists": {
                                    "aws:PrincipalServiceName": (
                                        "lambda.amazonaws.com"
                                    )
                                },
                            },
                        }
                    ],
                },
            },
        }
    ]


@pytest.mark.parametrize("mode", ["exclusive", "shared"])
def test_no_lambda_permission_targets_the_deletion_lambda(
    template: assertions.Template, mode: str
) -> None:
    """A Permission and the ResourcePolicy can overwrite each other."""
    synthesized = _invocation_template(mode, template)
    function_id = _deletion_function_id(synthesized)
    permissions = synthesized.find_resources("AWS::Lambda::Permission")

    # The Lifecycle Lambda's rule target has one, so the loop is not vacuous.
    assert permissions
    for resource in permissions.values():
        assert resource["Properties"]["FunctionName"] not in (
            {"Fn::GetAtt": [function_id, "Arn"]},
            {"Ref": function_id},
        )


@pytest.mark.parametrize("mode", ["exclusive", "shared"])
def test_only_the_stack_schedule_group_can_assume_the_schedule_role(
    template: assertions.Template, mode: str
) -> None:
    synthesized = _invocation_template(mode, template)
    role = synthesized.find_resources("AWS::IAM::Role")[
        _schedule_role_id(synthesized)
    ]
    groups = synthesized.find_resources("AWS::Scheduler::ScheduleGroup")
    assert len(groups) == 1
    group_id = next(iter(groups))

    assert role["Properties"]["AssumeRolePolicyDocument"]["Statement"] == [
        {
            "Action": "sts:AssumeRole",
            "Effect": "Allow",
            "Principal": {"Service": "scheduler.amazonaws.com"},
            "Condition": {
                "StringEquals": {
                    "aws:SourceAccount": ACCOUNT_ID,
                    "aws:SourceArn": {"Fn::GetAtt": [group_id, "Arn"]},
                }
            },
        }
    ]


def test_stack_does_not_output_the_deletion_function_name(
    template: assertions.Template,
) -> None:
    outputs = template.find_outputs("*")

    assert "DeletionFunctionName" not in outputs
    assert "DataBucketName" in outputs
