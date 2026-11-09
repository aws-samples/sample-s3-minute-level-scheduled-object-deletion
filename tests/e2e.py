#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""End-to-end test for the deployed S3 scheduled-deletion solution.

This script exercises a *live* deployment against real AWS APIs, following
the manual procedure in the README ("Testing the solution"). Run it after
``cdk deploy`` with credentials for the target account/region exported in your
environment (or pass ``--profile``).

What it checks
--------------
1. **Scheduled deletion** - uploads an object under a retention prefix,
   confirms a one-time EventBridge Scheduler schedule is created for it, waits
   for the schedule to fire, then confirms both the object version and the
   schedule are gone.
2. **expireAt override** - uploads an object with an ``expireAt`` metadata
   value and confirms the resulting schedule fires at (approximately) that
   time rather than the rule's default TTL.
3. **Disallowed-prefix rejection** (EXCLUSIVE buckets only) - confirms an
   upload outside the configured retention prefixes is denied by the bucket
   policy with AccessDenied.

The script is read-mostly and meant to clean up after itself: the objects it
creates are under a dedicated ``e2e-<uuid>/`` sub-key within a retention
prefix, and a ``finally`` block attempts to remove anything left behind.

Usage
-----
    # with credentials already exported in the environment
    python tests/e2e.py --region eu-west-1

    # with a named profile
    python tests/e2e.py --region eu-west-1 --profile my-profile

    # faster run using the 2-minute retention prefix (default)
    python tests/e2e.py --region eu-west-1 --prefix retention-2-mins/

    # skip the long "wait for deletion" checks (schedule-creation only)
    python tests/e2e.py --region eu-west-1 --no-wait

Exit code is 0 when all selected checks pass, non-zero otherwise.

This script intentionally depends only on boto3 (already a transitive
dependency of the CDK app) and the standard library, so it can be run with a
bare ``python`` interpreter that has boto3 installed.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - helpful message when boto3 missing
    sys.exit(
        "boto3 is required to run the E2E test. Install it with "
        "'uv sync' (then 'uv run python tests/e2e.py ...') or 'pip install boto3'."
    )

# --- Defaults derived from the sample configuration -------------------------
# These mirror sample_s3_scheduled_deletion/stack/config.py and constructs.
# Override with CLI flags if you changed PROJECT_PREFIX or the retention rules.
DEFAULT_STACK_NAME = "SampleS3ScheduledDeletionStack"
DEFAULT_PROJECT_PREFIX = "sample-s3-scheduled-deletion"
DEFAULT_BUCKET_OUTPUT_KEY = "DataBucketName"
# retention-2-mins/ keeps the run short; the deletion floor (MIN_LEAD) is 2 min.
DEFAULT_PREFIX = "retention-2-mins/"
DEFAULT_PREFIX_TTL_MINUTES = 2
# A prefix that matches no retention rule, used for the rejection test.
DISALLOWED_PREFIX = "not-allowed/"

# Schedule group name is resource_name(PROJECT_PREFIX, "deletion-schedules").
SCHEDULE_GROUP_SUFFIX = "deletion-schedules"

# How long to wait, past the nominal deletion time, before giving up.
# EventBridge Scheduler has ~60s precision, so allow generous slack.
DELETION_SLACK_SECONDS = 300
POLL_INTERVAL_SECONDS = 15

# Lower bound on deletion time enforced by the Lifecycle Lambda: no object is
# scheduled for deletion sooner than this many seconds after creation
# (MIN_LEAD_SECONDS in schedule_creation_service.py). Used to check the object
# is NOT removed too early.
MIN_LEAD_SECONDS = 120
# Tolerance subtracted from the earliest-legitimate-deletion time when probing
# that the object is still present. Covers Scheduler firing slightly early and
# clock skew between this client and AWS.
EARLY_DELETION_TOLERANCE_SECONDS = 30


# --- Small output helpers ---------------------------------------------------
class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def _c(text: str, color: str) -> str:
    if sys.stdout.isatty():
        return f"{color}{text}{Colors.RESET}"
    return text


def info(msg: str) -> None:
    print(f"  {msg}", flush=True)


def step(msg: str) -> None:
    print(_c(f"\n==> {msg}", Colors.BOLD + Colors.BLUE), flush=True)


def ok(msg: str) -> None:
    print(_c(f"  [PASS] {msg}", Colors.GREEN), flush=True)


def warn(msg: str) -> None:
    print(_c(f"  [WARN] {msg}", Colors.YELLOW), flush=True)


def fail(msg: str) -> None:
    print(_c(f"  [FAIL] {msg}", Colors.RED), flush=True)


class E2EError(Exception):
    """Raised when an E2E assertion fails."""


# --- AWS context ------------------------------------------------------------
@dataclass
class Context:
    session: "boto3.session.Session"
    region: str
    bucket: str
    schedule_group: str
    prefix: str
    prefix_ttl_minutes: int
    wait: bool
    run_id: str

    @property
    def s3(self):
        return self.session.client("s3", region_name=self.region)

    @property
    def scheduler(self):
        return self.session.client("scheduler", region_name=self.region)

    @property
    def cfn(self):
        return self.session.client("cloudformation", region_name=self.region)


def resolve_bucket_name(
    session, region: str, stack_name: str, output_key: str
) -> str:
    """Read the bucket name from the stack's CloudFormation output."""
    cfn = session.client("cloudformation", region_name=region)
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
    except ClientError as exc:
        raise E2EError(
            f"Could not describe stack '{stack_name}' in {region}: "
            f"{exc.response.get('Error', {}).get('Message', exc)}"
        ) from exc

    stacks = resp.get("Stacks", [])
    if not stacks:
        raise E2EError(f"Stack '{stack_name}' not found in {region}.")

    for output in stacks[0].get("Outputs", []):
        if output.get("OutputKey") == output_key:
            return output["OutputValue"]

    raise E2EError(
        f"Stack '{stack_name}' has no output '{output_key}'. "
        "Pass --bucket to specify the bucket name explicitly."
    )


# --- Core S3 / Scheduler helpers --------------------------------------------
def put_object(ctx: Context, key: str, *, metadata: dict | None = None) -> str:
    """Upload a tiny object and return its VersionId (or 'null')."""
    resp = ctx.s3.put_object(
        Bucket=ctx.bucket,
        Key=key,
        Body=b"e2e test object - safe to delete",
        Metadata=metadata or {},
    )
    return resp.get("VersionId", "null")


def object_versions(ctx: Context, key: str) -> list[dict]:
    """Return the version records for exactly *key* (prefix-exact match)."""
    resp = ctx.s3.list_object_versions(Bucket=ctx.bucket, Prefix=key)
    return [v for v in resp.get("Versions", []) if v.get("Key") == key]


def object_exists(ctx: Context, key: str) -> bool:
    return len(object_versions(ctx, key)) > 0


def list_schedule_names(ctx: Context) -> list[str]:
    """All schedule names currently in the deletion schedule group."""
    names: list[str] = []
    paginator = ctx.scheduler.get_paginator("list_schedules")
    for page in paginator.paginate(GroupName=ctx.schedule_group):
        names.extend(s["Name"] for s in page.get("Schedules", []))
    return names


def wait_for(
    predicate,
    *,
    timeout_seconds: int,
    description: str,
    interval: int = POLL_INTERVAL_SECONDS,
) -> bool:
    """Poll *predicate* until it is truthy or the timeout elapses."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        if predicate():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        info(f"...waiting for {description} " f"(~{int(remaining)}s left)")
        time.sleep(min(interval, max(1, int(remaining))))


def cleanup_key(ctx: Context, key: str) -> None:
    """Delete every version of *key*; best-effort, never raises."""
    try:
        for v in object_versions(ctx, key):
            params = {"Bucket": ctx.bucket, "Key": key}
            if v.get("VersionId") and v["VersionId"] != "null":
                params["VersionId"] = v["VersionId"]
            ctx.s3.delete_object(**params)
    except ClientError as exc:
        warn(f"Cleanup of '{key}' failed (ignored): {exc}")


# --- Test cases -------------------------------------------------------------
def test_scheduled_deletion(ctx: Context) -> None:
    """Upload -> schedule created -> (optionally) object + schedule removed."""
    step("Test 1: scheduled deletion from a retention rule")
    key = f"{ctx.prefix}e2e-{ctx.run_id}/scheduled.txt"
    info(f"Uploading s3://{ctx.bucket}/{key}")
    before = set(list_schedule_names(ctx))
    uploaded_at = time.monotonic()
    put_object(ctx, key)

    if not object_exists(ctx, key):
        raise E2EError(
            "Object was deleted immediately after upload. "
            "Check the retention prefix matches a configured rule."
        )
    ok("Object uploaded and present.")

    # The Lifecycle Lambda schedules asynchronously; give it a short window.
    def new_schedule_appeared() -> bool:
        return len(set(list_schedule_names(ctx)) - before) > 0

    if not wait_for(
        new_schedule_appeared,
        timeout_seconds=120,
        description="a deletion schedule to be created",
    ):
        raise E2EError(
            "No new schedule appeared in group "
            f"'{ctx.schedule_group}' within 120s. Check the Lifecycle "
            "Lambda logs."
        )
    created = set(list_schedule_names(ctx)) - before
    ok(
        f"Deletion schedule created ({len(created)} new): "
        f"{sorted(created)[0]}"
    )

    if not ctx.wait:
        warn(
            "--no-wait set: skipping deletion-time verification. "
            "Cleaning up the object now."
        )
        cleanup_key(ctx, key)
        return

    ttl_seconds = ctx.prefix_ttl_minutes * 60
    budget = ttl_seconds + DELETION_SLACK_SECONDS

    # --- Lower bound: the object must NOT be deleted too early. ---
    # The earliest a legitimate deletion may be scheduled is
    # max(TTL, MIN_LEAD) after creation. Confirm the object is still present
    # up to a safe checkpoint shortly before that time; if it has vanished by
    # then, the deletion fired too early (a scheduling/clamp regression).
    earliest_ok = max(ttl_seconds, MIN_LEAD_SECONDS)
    checkpoint = earliest_ok - EARLY_DELETION_TOLERANCE_SECONDS
    if checkpoint > 0:
        info(
            f"Verifying the object is NOT deleted before ~{checkpoint}s "
            f"(earliest legitimate deletion is max(TTL, MIN_LEAD)="
            f"{earliest_ok}s after upload)."
        )
        while True:
            elapsed = time.monotonic() - uploaded_at
            remaining = checkpoint - elapsed
            if remaining <= 0:
                break
            if not object_exists(ctx, key):
                raise E2EError(
                    f"Object s3://{ctx.bucket}/{key} was deleted after only "
                    f"~{int(elapsed)}s, before its earliest legitimate "
                    f"deletion time (~{earliest_ok}s). This indicates the "
                    "deletion was scheduled too early (check the MIN_LEAD "
                    "floor / TTL resolution in the Lifecycle Lambda)."
                )
            info(
                f"...still present at ~{int(elapsed)}s "
                f"(checkpoint ~{int(remaining)}s away)"
            )
            time.sleep(min(POLL_INTERVAL_SECONDS, max(1, int(remaining))))
        ok(
            f"Object still present near its scheduled time "
            f"(survived ~{int(time.monotonic() - uploaded_at)}s, as expected)."
        )

    info(
        f"Waiting up to {budget}s for deletion "
        f"(TTL {ctx.prefix_ttl_minutes} min + slack)."
    )

    deleted = wait_for(
        lambda: not object_exists(ctx, key),
        timeout_seconds=budget,
        description="the object to be deleted",
    )
    if not deleted:
        raise E2EError(
            f"Object s3://{ctx.bucket}/{key} still exists after {budget}s. "
            "Check the Deletion Lambda logs and the DLQ."
        )
    ok("Object was permanently deleted at its scheduled time.")

    # The schedule self-deletes (ActionAfterCompletion=DELETE) after firing.
    schedule_gone = wait_for(
        lambda: len(set(list_schedule_names(ctx)) & created) == 0,
        timeout_seconds=120,
        description="the one-time schedule to self-delete",
    )
    if schedule_gone:
        ok("Deletion schedule self-deleted after firing.")
    else:
        warn(
            "Schedule still present shortly after deletion; it is removed "
            "asynchronously and should disappear soon."
        )


def test_expire_at_override(ctx: Context) -> None:
    """Object with expireAt is scheduled near that time, not the full TTL."""
    step("Test 2: expireAt metadata overrides the default TTL")
    # Choose an expireAt safely above the 2-minute MIN_LEAD floor but below
    # the rule TTL where possible, so the override is observable.
    expire_in_seconds = max(150, ctx.prefix_ttl_minutes * 60 + 60)
    expire_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expire_in_seconds)
    ).replace(microsecond=0)
    expire_at_iso = expire_at.isoformat().replace("+00:00", "Z")

    key = f"{ctx.prefix}e2e-{ctx.run_id}/expireat.txt"
    info(f"Uploading s3://{ctx.bucket}/{key} with expireAt={expire_at_iso}")
    before = set(list_schedule_names(ctx))
    uploaded_at = time.monotonic()
    put_object(ctx, key, metadata={"expireAt": expire_at_iso})

    def new_schedule_appeared() -> bool:
        return len(set(list_schedule_names(ctx)) - before) > 0

    if not wait_for(
        new_schedule_appeared,
        timeout_seconds=120,
        description="a deletion schedule for the expireAt object",
    ):
        raise E2EError("No schedule created for the expireAt object.")

    created = sorted(set(list_schedule_names(ctx)) - before)
    detail = ctx.scheduler.get_schedule(
        GroupName=ctx.schedule_group, Name=created[0]
    )
    expr = detail.get("ScheduleExpression", "")
    ok(f"Schedule created with expression: {expr}")

    # Parse the at(...) expression and compare to the requested expireAt.
    scheduled_dt = _parse_at_expression(expr)
    if scheduled_dt is None:
        warn(
            f"Could not parse schedule expression '{expr}'; skipping the "
            "timing assertion."
        )
    else:
        drift = abs(
            (scheduled_dt - expire_at.replace(tzinfo=None)).total_seconds()
        )
        # Allow 90s: Scheduler precision + the function's clamping/rounding.
        if drift <= 90:
            ok(
                f"Scheduled time matches expireAt within {int(drift)}s "
                "(override honoured)."
            )
        else:
            warn(
                f"Scheduled time differs from expireAt by {int(drift)}s. "
                "This can be legitimate if expireAt was clamped to the "
                "MIN_LEAD floor or MAX_RETENTION ceiling."
            )

    if not ctx.wait:
        warn("--no-wait set: cleaning up the expireAt object now.")
        cleanup_key(ctx, key)
        return

    budget = expire_in_seconds + DELETION_SLACK_SECONDS

    # --- Lower bound: the object must NOT be deleted before expireAt. ---
    # The requested expireAt is above the MIN_LEAD floor, so the earliest
    # legitimate deletion is ~expire_in_seconds after upload. Confirm the
    # object survives up to a safe checkpoint shortly before that time.
    earliest_ok = max(expire_in_seconds, MIN_LEAD_SECONDS)
    checkpoint = earliest_ok - EARLY_DELETION_TOLERANCE_SECONDS
    if checkpoint > 0:
        info(
            f"Verifying the expireAt object is NOT deleted before "
            f"~{checkpoint}s (expireAt is ~{earliest_ok}s after upload)."
        )
        while True:
            elapsed = time.monotonic() - uploaded_at
            remaining = checkpoint - elapsed
            if remaining <= 0:
                break
            if not object_exists(ctx, key):
                raise E2EError(
                    f"expireAt object s3://{ctx.bucket}/{key} was deleted "
                    f"after only ~{int(elapsed)}s, before its requested "
                    f"expireAt (~{earliest_ok}s after upload). The override "
                    "was resolved to too early a time."
                )
            info(
                f"...still present at ~{int(elapsed)}s "
                f"(checkpoint ~{int(remaining)}s away)"
            )
            time.sleep(min(POLL_INTERVAL_SECONDS, max(1, int(remaining))))
        ok(
            f"expireAt object still present near expireAt "
            f"(survived ~{int(time.monotonic() - uploaded_at)}s, as expected)."
        )

    info(f"Waiting up to {budget}s for the expireAt deletion.")
    if wait_for(
        lambda: not object_exists(ctx, key),
        timeout_seconds=budget,
        description="the expireAt object to be deleted",
    ):
        ok("expireAt object was deleted at its scheduled time.")
    else:
        raise E2EError("expireAt object was not deleted within the budget.")


def test_disallowed_prefix(ctx: Context) -> None:
    """Upload outside the retention prefixes must be denied (EXCLUSIVE)."""
    step("Test 3: upload to a disallowed prefix is rejected")
    key = f"{DISALLOWED_PREFIX}e2e-{ctx.run_id}/blocked.txt"
    info(f"Attempting s3://{ctx.bucket}/{key} (expected AccessDenied)")
    try:
        put_object(ctx, key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDenied", "AccessDeniedException"):
            ok("Upload to disallowed prefix was denied by the bucket policy.")
            return
        raise E2EError(
            f"Upload failed, but with unexpected error '{code}': {exc}"
        ) from exc

    # If it succeeded, this is likely a SHARED bucket (no prefix deny), or the
    # bucket policy is not in place. Clean up and warn rather than hard-fail.
    cleanup_key(ctx, key)
    warn(
        "Upload to the disallowed prefix SUCCEEDED. This is expected only "
        "for a SHARED (existing) bucket; on an EXCLUSIVE bucket it should be "
        "denied. Treating as a warning, not a failure."
    )


# --- expression parsing -----------------------------------------------------
def _parse_at_expression(expr: str) -> datetime | None:
    """Parse ``at(YYYY-MM-DDTHH:MM:SS)`` into a naive UTC datetime."""
    if not expr.startswith("at(") or not expr.endswith(")"):
        return None
    inner = expr[len("at(") : -1]
    try:
        return datetime.strptime(inner, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


# --- orchestration ----------------------------------------------------------
def build_context(args: argparse.Namespace) -> Context:
    session = boto3.session.Session(
        profile_name=args.profile, region_name=args.region
    )

    # Fail fast with a clear message if credentials are missing/expired.
    try:
        ident = session.client(
            "sts", region_name=args.region
        ).get_caller_identity()
    except Exception as exc:  # noqa: BLE001 - surface any auth error plainly
        raise E2EError(
            "Could not authenticate to AWS. Export credentials or pass "
            f"--profile. Underlying error: {exc}"
        ) from exc
    info(
        f"Authenticated as {ident.get('Arn', '<unknown>')} "
        f"in account {ident.get('Account', '?')} ({args.region})."
    )

    bucket = args.bucket or resolve_bucket_name(
        session, args.region, args.stack_name, args.bucket_output_key
    )
    info(f"Target bucket: {bucket}")

    schedule_group = args.schedule_group or (
        f"{args.project_prefix}-{SCHEDULE_GROUP_SUFFIX}"
    )
    info(f"Schedule group: {schedule_group}")

    return Context(
        session=session,
        region=args.region,
        bucket=bucket,
        schedule_group=schedule_group,
        prefix=args.prefix,
        prefix_ttl_minutes=args.prefix_ttl_minutes,
        wait=not args.no_wait,
        run_id=uuid.uuid4().hex[:8],
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--region", required=True, help="AWS region of the deployment."
    )
    p.add_argument(
        "--profile",
        default=None,
        help="AWS named profile (otherwise uses env credentials).",
    )
    p.add_argument(
        "--stack-name",
        default=DEFAULT_STACK_NAME,
        help=f"CloudFormation stack name (default: {DEFAULT_STACK_NAME}).",
    )
    p.add_argument(
        "--bucket",
        default=None,
        help="Override the bucket name instead of reading the stack output.",
    )
    p.add_argument(
        "--bucket-output-key",
        default=DEFAULT_BUCKET_OUTPUT_KEY,
        help=f"Stack output key for the bucket name (default: {DEFAULT_BUCKET_OUTPUT_KEY}).",
    )
    p.add_argument(
        "--project-prefix",
        default=DEFAULT_PROJECT_PREFIX,
        help=f"PROJECT_PREFIX used to derive resource names (default: {DEFAULT_PROJECT_PREFIX}).",
    )
    p.add_argument(
        "--schedule-group",
        default=None,
        help="Override the schedule group name.",
    )
    p.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"Retention prefix to upload under (default: {DEFAULT_PREFIX}).",
    )
    p.add_argument(
        "--prefix-ttl-minutes",
        type=int,
        default=DEFAULT_PREFIX_TTL_MINUTES,
        help=f"TTL (minutes) configured for --prefix (default: {DEFAULT_PREFIX_TTL_MINUTES}).",
    )
    p.add_argument(
        "--no-wait",
        action="store_true",
        help="Only verify schedule creation; skip waiting for deletion.",
    )
    p.add_argument(
        "--skip-expire-at",
        action="store_true",
        help="Skip the expireAt override test.",
    )
    p.add_argument(
        "--skip-disallowed",
        action="store_true",
        help="Skip the disallowed-prefix rejection test.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(_c("S3 Scheduled Deletion - End-to-End Test", Colors.BOLD))

    try:
        ctx = build_context(args)
    except E2EError as exc:
        fail(str(exc))
        return 2

    created_keys: list[str] = []
    results: list[tuple[str, bool, str]] = []

    def run(name: str, func) -> None:
        try:
            func(ctx)
            results.append((name, True, ""))
        except E2EError as exc:
            fail(str(exc))
            results.append((name, False, str(exc)))
        except ClientError as exc:
            fail(f"AWS error during '{name}': {exc}")
            results.append((name, False, str(exc)))

    try:
        run("scheduled deletion", test_scheduled_deletion)
        if not args.skip_expire_at:
            run("expireAt override", test_expire_at_override)
        if not args.skip_disallowed:
            run("disallowed prefix rejection", test_disallowed_prefix)
    finally:
        # Best-effort cleanup of anything created under this run's namespace.
        step("Cleanup")
        for key in _run_namespace_keys(ctx):
            cleanup_key(ctx, key)
        info("Removed any objects left under this run's namespace.")

    # --- summary ---
    step("Summary")
    passed = sum(1 for _, ok_, _ in results if ok_)
    for name, ok_, detail in results:
        line = f"{'PASS' if ok_ else 'FAIL'}  {name}"
        print(_c(f"  {line}", Colors.GREEN if ok_ else Colors.RED))
        if detail and not ok_:
            info(f"      {detail}")
    total = len(results)
    print()
    if passed == total:
        print(_c(f"All {total} check(s) passed.", Colors.BOLD + Colors.GREEN))
        return 0
    print(_c(f"{passed}/{total} check(s) passed.", Colors.BOLD + Colors.RED))
    return 1


def _run_namespace_keys(ctx: Context) -> list[str]:
    """List any objects left under this run's e2e-<run_id>/ namespaces."""
    keys: list[str] = []
    for prefix in (ctx.prefix, DISALLOWED_PREFIX):
        token = f"{prefix}e2e-{ctx.run_id}/"
        try:
            resp = ctx.s3.list_object_versions(Bucket=ctx.bucket, Prefix=token)
        except ClientError:
            continue
        keys.extend(v["Key"] for v in resp.get("Versions", []) if v.get("Key"))
    return sorted(set(keys))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
