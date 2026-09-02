# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Nox sessions for test orchestration and code quality checks."""

import nox

nox.options.reuse_existing_virtualenvs = True
nox.options.default_venv_backend = "uv"

python_version = "3.14"

LOCATIONS = ["sample_s3_scheduled_deletion", "tests", "noxfile.py", "app.py"]

LAMBDA_SRC_ROOT = "sample_s3_scheduled_deletion/lambda"
LAMBDA_TESTS_ROOT = "tests/lambda"

# Lambda function names — the mirror convention means:
#   source: {LAMBDA_SRC_ROOT}/{name}
#   tests:  {LAMBDA_TESTS_ROOT}/{name}
LAMBDA_FUNCTIONS = ["lifecycle_function", "deletion_function"]


# --- Lambda function test sessions ------------------------------------------


@nox.parametrize(
    "function_name",
    LAMBDA_FUNCTIONS,
)
@nox.session(python=python_version)
def run_lambda_fn_tests(session, function_name: str):
    """Install deps and run pytest for a Lambda function.

    Installs only the test runner and the libraries the functions import at
    runtime (via the Powertools layer). The CDK project itself is not
    installed; the source is made importable through PYTHONPATH.
    """
    session.run_install(
        "uv",
        "sync",
        "--only-group",
        "test",
        "--only-group",
        "lambda-test",
        env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location},
    )
    session.env["PYTHONPATH"] = f"{LAMBDA_SRC_ROOT}/{function_name}"
    session.run(
        "pytest",
        f"{LAMBDA_TESTS_ROOT}/{function_name}",
        "-v",
        "--tb=short",
        *session.posargs,
    )


# --- CDK stack tests --------------------------------------------------------


@nox.session(python=python_version, name="tests_stack")
def tests_stack(session):
    """Run CDK synthesis tests with pytest."""
    # Installs the CDK project (default) plus the test runner.
    session.run_install(
        "uv",
        "sync",
        "--group",
        "test",
        env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location},
    )
    session.run("pytest", "tests/stack", "-v", "--tb=short", *session.posargs)


# --- Code quality -----------------------------------------------------------


@nox.session(python=python_version)
def format(session):
    """Format code with black and isort."""
    session.run_install(
        "uv",
        "sync",
        "--only-group",
        "lint",
        env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location},
    )
    session.run("black", "--line-length", "79", *LOCATIONS)
    session.run("isort", "--profile", "black", *LOCATIONS)


@nox.session(python=python_version)
def security(session):
    """Run security checks."""
    session.run_install(
        "uv",
        "sync",
        "--only-group",
        "security",
        env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location},
    )
    session.run("bandit", "-c", "bandit.yaml", "-r", ".")
