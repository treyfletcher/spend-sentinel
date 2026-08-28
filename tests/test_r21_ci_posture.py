"""R21: the suite runs with no AWS credentials and no network; the CI workflow
scrubs the AWS env vars; the shipped IAM policy is exactly the documented
read-only action list (spec Security considerations).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .conftest import AWS_ENV_VARS

REPO = Path(__file__).parent.parent

DOCUMENTED_IAM_ACTIONS = [
    "ec2:DescribeInstances",
    "ec2:DescribeSecurityGroups",
    "s3:GetBucketVersioning",
    "s3:GetBucketTagging",
    "s3:GetBucketLocation",
]


class TestNoAwsCredentialsAtRuntime:
    def test_r21_aws_env_vars_absent_during_the_suite(self):
        """The session-scoped conftest guard scrubs these for every test; this
        asserts the guarantee is actually in force."""
        present = [var for var in AWS_ENV_VARS if var in os.environ]
        assert present == [], f"AWS env vars leaked into the test run: {present}"

    def test_r21_boto3_not_imported_by_the_package_import(self):
        """Importing every non-adapter module must not pull in boto3."""
        import sys

        import spend_sentinel.cli
        import spend_sentinel.core.drift
        import spend_sentinel.render.markdown  # noqa: F401

        assert "boto3" not in sys.modules


class TestCiWorkflow:
    def workflow(self) -> str:
        return (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    def test_r21_ci_scrubs_every_aws_env_var(self):
        content = self.workflow()
        for var in AWS_ENV_VARS:
            assert f"-u {var}" in content, f"ci.yml does not unset {var}"
        # the scrub must wrap the pytest invocation
        assert "env -u" in content
        assert "pytest" in content

    def test_t10_ci_runs_ruff_mypy_pytest_on_both_pythons(self):
        content = self.workflow()
        assert "ruff check" in content
        assert "mypy" in content
        assert "--cov=spend_sentinel" in content
        assert '"3.11"' in content and '"3.12"' in content


class TestIamPolicyDoc:
    def test_security_iam_policy_is_exactly_the_documented_actions(self):
        policy = json.loads((REPO / "docs" / "iam-policy.json").read_text())
        statements = policy["Statement"]
        assert len(statements) == 1
        statement = statements[0]
        assert statement["Effect"] == "Allow"
        assert statement["Resource"] == "*"
        assert sorted(statement["Action"]) == sorted(DOCUMENTED_IAM_ACTIONS)
        # every action is read-only (Describe/Get)
        for action in statement["Action"]:
            operation = action.split(":", 1)[1]
            assert operation.startswith(("Describe", "Get")), action

    def test_security_iam_actions_cover_the_boto3_reader_surface(self):
        """Cross-check with the adapter test's mapping: the reader uses a
        subset of the policy's actions (GetBucketLocation is allowed but
        unused, per the increment-3 PR)."""
        from .test_boto3_reader import DOCUMENTED_ACTIONS

        assert set(DOCUMENTED_ACTIONS.values()) == set(DOCUMENTED_IAM_ACTIONS)
