# Sample: S3 Scheduled Deletion

Amazon S3 Lifecycle rules expire objects a whole number of days after creation, [rounded up to the next midnight UTC](https://docs.aws.amazon.com/AmazonS3/latest/userguide/intro-lifecycle-rules.html), and the removal that follows is asynchronous, so [an expired object can remain for some time](https://docs.aws.amazon.com/AmazonS3/latest/userguide/how-to-set-lifecycle-configuration-intro.html#lifecycle-considerations). This sample is designed to delete each object at a set time instead, with minute precision, after a time to live (TTL) configured for each key prefix or an `expireAt` timestamp set on the object.

When an object is uploaded, an AWS Lambda function creates a one-time [EventBridge Scheduler schedule](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html) for its deletion time. When the schedule fires, a second Lambda function attempts to delete that object version. An S3 Lifecycle rule stays in place, intended as a backstop for a deletion that does not run.

Please note that this sample solution and code are only for testing and non-production purposes.

## Solution overview

![Solution architecture diagram](assets/arch-diagram.jpg)

1. An object is created in the S3 bucket. A bucket policy denies uploads outside the allowed prefixes, the first layer of enforcement. The bucket has Amazon EventBridge notifications enabled, so Amazon S3 emits an `Object Created` event to the account default EventBridge bus.
2. An EventBridge rule routes S3 `Object Created` and `Object Deleted` events from the default bus to a custom bus dedicated to lifecycle and deletion events. `Object Deleted` is forwarded too, so that removals also appear in the consolidated log group (step 8).
3. Another EventBridge rule routes the event in the custom bus to the Lifecycle Lambda function.
4. The Lifecycle function is designed as follows. It matches the object key against the configured TTL rules. If no rule matches, it deletes the object immediately, as a second layer of enforcement behind the bucket policy of step 1. Otherwise, it sets the deletion time from the object's optional `expireAt` user metadata, falling back to `now + TTL` if `expireAt` is missing, unreadable, unparseable or in the past. Then, it creates a one-time schedule in the custom Scheduler group.
5. When the schedule triggers, EventBridge Scheduler invokes the Deletion Lambda with the bucket, key and, if any, version ID. The schedule is configured to be [automatically deleted after completion](https://docs.aws.amazon.com/scheduler/latest/UserGuide/managing-schedule-delete.html#managing-schedule-automatic-deletion), to help stay within the [quota of schedules](https://docs.aws.amazon.com/scheduler/latest/UserGuide/scheduler-quotas.html). The Deletion Lambda's resource-based policy and a dedicated execution role are configured so that only schedules in the stack's schedule group can invoke it.
6. The Deletion Lambda function attempts to permanently delete the object version and emits metrics and events.
7. Failed asynchronous invocations of either Lambda function are retried, then routed to a shared dead-letter queue (DLQ).
8. A rule on the custom bus routes application events to a single CloudWatch log group, intended to provide a consolidated record of deletion activity.
9. An S3 Lifecycle expiration is the fallback for a schedule that never fires. It targets both current and noncurrent versions, because expiring only the current version would add a delete marker and keep the data.
10. Amazon CloudWatch alarms watch the DLQ and the Lambda failure metrics. Their scope is to flag a failure that could change when, or whether, an object is deleted; see [Alarms](#alarms).

## High level considerations

- The bucket the stack creates is versioned, but the Lambda functions are also designed to handle a non-versioned bucket, such as an [existing bucket](#using-an-existing-bucket). On a versioned bucket, each object version gets its own deletion schedule, and the delete call names its `VersionId` so that the version is permanently removed and not hidden behind a delete marker. On a non-versioned bucket, the delete call names the key only.
- The steps above describe the bucket the stack creates and owns. The solution can instead be configured to manage only its own prefixes inside a bucket you already have; see [Using an existing bucket](#using-an-existing-bucket).
- The minimum TTL is two minutes, to allow some margin over the [60 second precision](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html) of EventBridge Scheduler. The stack rejects shorter TTLs at synthesis time, and the Lifecycle function clamps any earlier `expireAt` to the same floor.
- Schedule names are derived deterministically from the bucket, key and, if any, version ID. `CreateSchedule` is not an upsert, so a redelivered `Object Created` event raises `ConflictException` while the first schedule is pending. The Lifecycle function treats that as success and leaves the pending schedule in place.
- The Lambda functions run on the `python3.14` managed runtime and get `aws-lambda-powertools` and `pydantic` from the AWS-managed Powertools layer, whose ARN is resolved from SSM Parameter Store at deploy time. Nothing is bundled with the function code; the local development dependencies only serve to run the tests against the same libraries.

## Repository structure

```text
app.py                                  CDK app entry point
sample_s3_scheduled_deletion/
  stack/
    main_stack.py                       Stack: buckets + S3DataDeletion construct
    config.py                           Sample configuration file (edit this)
    constructs/deletion.py              Construct for deletion resources
    custom_resources/lambda_function.py Customized Lambda construct
  lambda/
    lifecycle_function/                 Schedules deletions on object creation
    deletion_function/                  Performs deletions
tests/                                  Stack and validation tests
```

Configure values in `sample_s3_scheduled_deletion/stack/config.py` to adapt the sample, for instance modifying the TTL parameters.

## Deployment

### Prerequisites

- An AWS account, already [bootstrapped for the AWS CDK](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping.html).
- Python (version 3.14 or later).
- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, for dependency management.
- The [AWS CDK](https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html) installed (version 2.1144.0 or later).
- The [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) installed (version 2.32.12 or later).
- AWS credentials with sufficient permissions to deploy the solution.

### Install dependencies

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.
Install the runtime dependencies into a virtual environment with:

```bash
uv sync
```

`uv sync` creates and manages `.venv` automatically. Prefix commands with `uv run` (for example, `uv run cdk synth`) to execute them inside that environment, or activate it manually with `source .venv/bin/activate` (`.venv\Scripts\activate.bat` on Windows).

### Deploy the stack

Verify that you have available credentials with sufficient permissions to deploy in the expected AWS account:

```bash
aws sts get-caller-identity
```

Deploy with the AWS CDK, replacing `111122223333` and `aa-example-1` with your account ID and Region (both required):

```bash
uv run cdk synth -c account=111122223333 -c region=aa-example-1
uv run cdk deploy -c account=111122223333 -c region=aa-example-1
```

## Testing the solution

After deployment, complete the following steps to test the solution.

### Open log trails

Open the log trails to see in near real-time the lifecycle events of the object you just uploaded:
1. Log in to the [AWS Management Console](https://console.aws.amazon.com/).
2. Open the [CloudWatch console](https://console.aws.amazon.com/cloudwatch/home) in the [Region](https://docs.aws.amazon.com/awsconsolehelpdocs/latest/gsg/select-region-procedure.html) where you deployed the solution.
3. In the navigation pane, choose Logs / Log Management.
4. Choose the log group `/aws/events/sample-s3-scheduled-deletion-deletion-events`.
5. Choose `Start tailing`

### Open CloudShell

You can test the solution using [AWS CloudShell](https://aws.amazon.com/cloudshell/), which provides a pre-authenticated browser-based shell environment with the AWS CLI pre-installed.

1. Open the AWS console in a new browser tab, in the Region where you deployed the solution.
2. Choose the CloudShell icon (terminal icon) in the navigation bar, and wait for the shell to initialize.

### Test scheduled deletion

Enter the following commands in the CloudShell terminal to upload a test file:
```bash
# The bucket name has a hash suffix; read it from the stack output
BUCKET_NAME=$(aws cloudformation describe-stacks \
  --stack-name SampleS3ScheduledDeletionStack \
  --query "Stacks[0].Outputs[?OutputKey=='DataBucketName'].OutputValue" \
  --output text)

# Upload test objects
echo "version 1" > test.txt
aws s3 cp test.txt s3://$BUCKET_NAME/retention-5-mins/test.txt

echo "version 2" > test.txt
aws s3 cp test.txt s3://$BUCKET_NAME/retention-5-mins/test.txt

# Confirm both versions exist
aws s3api list-object-versions --bucket $BUCKET_NAME \
  --prefix retention-5-mins/test.txt \
  --query "Versions[].{Key:Key,VersionId:VersionId,IsLatest:IsLatest}"
```

Following is an example of the expected response:
```json
[
  {
      "Key": "retention-5-mins/test.txt",
      "VersionId": "...",
      "IsLatest": true
  },
  {
      "Key": "retention-5-mins/test.txt",
      "VersionId": "...",
      "IsLatest": false
  }
]
```
With the default configuration, the object is expected to be deleted 5 minutes after creation [with 60 second precision](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html).

Enter the following commands in the CloudShell terminal to verify the deletion schedule has been created:
```bash
aws scheduler list-schedules \
  --group-name sample-s3-scheduled-deletion-deletion-schedules
```

Following is an example of the expected response:
```json
{
  ...
  "Name": "sample-s3-scheduled-deletion-...",
  "State": "ENABLED",
  "Target": {
    "Arn": "arn:aws:lambda:aa-example-1:111122223333:function:sample-s3-scheduled-deletion-deletion-fn"
  }
}
```

Go to the CloudWatch log trails you previously opened. Following is an example of what you should see:

![CloudWatch log group tail showing the object lifecycle events, from ObjectCreated and ScheduleCreated through to DeletionSucceeded](assets/log-trail.png)

After observing the `DeletionSucceeded` event in the logs, enter the following command in the CloudShell terminal to verify that both the schedule and object have been deleted:
```bash
aws scheduler list-schedules \
  --group-name sample-s3-scheduled-deletion-deletion-schedules
aws s3api list-object-versions --bucket $BUCKET_NAME \
  --prefix retention-5-mins/test.txt
```
Both responses should contain empty lists.

### Test deletion with custom TTL

Enter the following command in the CloudShell terminal to upload an object with an `expireAt` ISO 8601 timestamp, at least two minutes in the future, as user metadata to test the optional override:
```bash
aws s3 cp test.txt s3://$BUCKET_NAME/retention-5-mins/test.txt \
  --metadata expireAt=YYYY-MM-DDTHH:MM:SSZ
```

`expireAt` is bounded on both sides:

- A value less than two minutes from now is raised to the two-minute floor.
- A value greater than `MAX_RETENTION_MINUTES` is capped at it, and emits an `ExpireAtClamped` event and metric.
- A value in the past, or one that cannot be parsed, is ignored in favour of the rule's default TTL.

A timestamp with a timezone offset is converted to UTC. For example, `2026-09-08T12:00:00+05:00` schedules the deletion of the object for `2026-09-08T07:00:00Z`.

### Test upload to disallowed prefixes

Enter the following command in the CloudShell terminal to test the upload to a prefix not matching the expected rules:
```bash
aws s3 cp test.txt s3://$BUCKET_NAME/not-allowed/test.txt
```

Following is an example of the expected response:
```
upload failed: ./test.txt to s3:// [...] An error occurred (AccessDenied) when calling the PutObject operation: User: [...] is not authorized to perform: s3:PutObject on resource: [...] with an explicit deny in a resource-based policy
```

## Using an existing bucket

By default, the stack creates and manages its own Amazon S3 bucket. To use a bucket that already exists, set the `EXISTING_BUCKET_NAME` variable in `sample_s3_scheduled_deletion/stack/config.py`.

An existing bucket switches the ownership model from `EXCLUSIVE`, the default, to `SHARED`, which changes the objects the solution acts on. The following table compares the two ownership models.

| Event | `EXCLUSIVE` | `SHARED` |
| --- | --- | --- |
| An object is uploaded outside the retention prefixes | Deny the upload with a bucket policy | Defer to the existing bucket policy |
| An object matches no retention rule | Delete the object on arrival | Keep the object and report `ObjectUnmanaged` |
| AWS Lambda access to objects | The entire bucket | Only `<prefix>*` for each retention rule |
| A scheduled deletion names a key outside the retention prefixes, for example after a rule is removed | Delete the object | Skip the object and report `DeletionRejected` |
| Bucket configuration | Apply the bucket policy, the S3 Lifecycle rule, and Amazon EventBridge notifications | Leave the bucket configuration unchanged |

Before you deploy, prepare the existing bucket:

- **Required** – Enable EventBridge notifications. Amazon S3 publishes the `Object Created` events that the solution depends on only when notifications are enabled. For more information, see [Enabling Amazon EventBridge](https://docs.aws.amazon.com/AmazonS3/latest/userguide/enable-event-notifications-eventbridge.html).
- **Recommended** – Add an S3 Lifecycle expiration that covers the retention prefixes and lasts at least as long as `MAX_RETENTION_MINUTES`. Add a prefix filter to the rule so that it targets only the objects that the solution manages.

## Alarms

The stack creates three Amazon CloudWatch alarms, each configured to fire when its metric reaches 1 within 5 minutes. The alarms have no actions, so a firing alarm shows only in the CloudWatch console.

| Alarm | Fires when | What it means |
| --- | --- | --- |
| `<PROJECT_PREFIX>-deletion-dlq-not-empty` | The DLQ holds a message | An invocation failed after its retries. The object is left for the S3 Lifecycle backstop. The alarm stays in `ALARM` until the message is removed from the queue. |
| `<PROJECT_PREFIX>-deletion-error` | The Deletion Lambda emits `DeletionError` | A deletion attempt failed. A retry follows, then the DLQ if the retries fail too. |
| `<PROJECT_PREFIX>-metadata-lookup-failed` | The Lifecycle Lambda emits `MetadataLookupFailed` | An object's metadata could not be read, so its `expireAt` was ignored and the rule's TTL applied. |

## Troubleshooting

1. **S3 bucket name conflict** — The bucket name is derived from `PROJECT_PREFIX` in `sample_s3_scheduled_deletion/stack/config.py`. If the name is already taken, change it to a distinctive prefix of at most 38 characters.

2. **A second deployment in the same account and Region fails with "already exists"** — Resource names are derived from `PROJECT_PREFIX`, not from the stack name, so a second copy collides with the first even under another stack name. Give each copy its own `PROJECT_PREFIX`.

3. **Issues with `cdk synth` or `cdk deploy`** — Check the [prerequisites](#prerequisites), and that the expected versions of Python and the other tools are the ones running in your terminal.

4. **Objects not uploaded or deletion not scheduled** — Verify that the object was uploaded under an allowed prefix (for instance, `retention-5-mins/`) and that any `expireAt` metadata is valid and in the future. Check the Lifecycle Lambda's log group for errors.

5. **Objects created with `expireAt` metadata are not deleted when expected** — Check `expireAt` against the bounds in [Test deletion with custom TTL](#test-deletion-with-custom-ttl). If the Lifecycle function cannot read the object's metadata at all, for example because the request is throttled, it falls back to the default TTL and emits a `MetadataLookupFailed` event and metric.

6. **Stack deletion fails due to non-empty bucket** — The bucket has a `DESTROY` removal policy, but CDK does not empty buckets automatically. [Empty the bucket](https://docs.aws.amazon.com/AmazonS3/latest/userguide/empty-bucket.html) and retry.

7. **Nothing happens after uploading to an existing bucket** — Check that EventBridge notifications are enabled on the bucket (see [Using an existing bucket](#using-an-existing-bucket)). Without them, the upload succeeds but no event is published, so the Lifecycle Lambda logs nothing.

## Clean up

To clean up your resources, open the [AWS CloudFormation console](https://console.aws.amazon.com/cloudformation) and delete the stack `SampleS3ScheduledDeletionStack`.

Alternatively, you can also run `cdk destroy` from the same terminal where you ran `cdk deploy` and using the same AWS CDK CLI arguments as during deployment.

Remember to [empty the stack's S3 buckets](https://docs.aws.amazon.com/AmazonS3/latest/userguide/empty-bucket.html) before deleting the CloudFormation stack.

## Development

Install the developer tooling (nox runs the test and quality sessions, each
with its own dependencies installed with uv):

```bash
uv sync --group dev
```

The test suites are run through nox:

```bash
uv run nox -s tests_stack           # CDK synthesis tests (tests/stack/)
uv run nox -s run_lambda_fn_tests   # Lambda handler tests (tests/lambda/)
```

`tests/stack/` covers the synthesis-time checks (an empty rule list, empty
or non-`/`-terminated prefixes, TTLs below the two-minute minimum or above
`MAX_RETENTION_MINUTES`, duplicate prefixes, and a `PROJECT_PREFIX` longer than
38 characters) and the resources in the synthesized CloudFormation template.
`tests/lambda/` contains unit tests for the Lambda handlers.

You can also run the formatter and security scanners with nox:

```bash
uv run nox -s format     # black + isort
uv run nox -s security   # bandit
```

### End-to-end test against a live deployment

`tests/stack/` and `tests/lambda/` run entirely offline. To check a *real*
deployment, `tests/e2e.py` automates the steps of
[Testing the solution](#testing-the-solution) against live AWS APIs.

Deploy the stack first, then run the script with credentials for the target
account. It reads the bucket name from the stack output, so you only need to
pass the Region (and a profile, if you use one):

```bash
# with credentials already exported in your environment
uv run --with boto3 python tests/e2e.py --region aa-example-1

# with a named profile
uv run --with boto3 python tests/e2e.py --region aa-example-1 --profile my-profile
```

`boto3` is pulled in with `--with` because it is not a dependency of the CDK
app itself (the Lambda functions get it from the managed runtime).

Waiting for the deletions typically takes a few minutes. Useful flags:

```bash
# schedule-creation only, no waiting for deletion (quick smoke test)
uv run --with boto3 python tests/e2e.py --region aa-example-1 --no-wait

# upload under a different retention prefix / TTL
uv run --with boto3 python tests/e2e.py --region aa-example-1 \
  --prefix retention-5-mins/ --prefix-ttl-minutes 5
```

The script creates every test object under a unique `e2e-<id>/` sub-key and
aims to remove anything it leaves behind, so that it can be re-run. Run
`uv run --with boto3 python tests/e2e.py --help` for the full list of options
(including `--stack-name`, `--bucket`, `--skip-expire-at`, and
`--skip-disallowed`). The exit code is `0` only when all selected checks pass.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
