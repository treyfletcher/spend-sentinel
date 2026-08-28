"""``Boto3AwsReader`` — the live adapter, and the ONLY module importing boto3 (R21).

boto3 is imported lazily inside the constructor so that merely importing this
module (or anything else in the package) works without the ``[aws]`` extra;
``--skip-drift`` runs never construct it (R11).

Security (spec, docs/iam-policy.json to land with T9): this adapter calls no
AWS APIs outside the documented read-only list — ec2:DescribeInstances,
ec2:DescribeSecurityGroups, s3:GetBucketVersioning, s3:GetBucketTagging,
s3:GetBucketLocation. Credentials come only from the standard AWS chain; they
are never accepted as arguments or logged.
"""

from __future__ import annotations

from typing import Any

from spend_sentinel.core.models import (
    BucketAttrs,
    InstanceAttrs,
    SecurityGroupAttrs,
    SecurityGroupRule,
)


class Boto3NotInstalledError(Exception):
    """boto3 is not installed; drift needs `pip install spend-sentinel[aws]`."""


class Boto3AwsReader:
    """Live AwsReader over boto3 clients (structural protocol match)."""

    def __init__(self, region: str | None = None) -> None:
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError:
            raise Boto3NotInstalledError(
                "boto3 is not installed; install spend-sentinel[aws] or pass --skip-drift"
            ) from None
        self._client_error: type[Exception] = ClientError
        session = boto3.session.Session(region_name=region)
        self._ec2 = session.client("ec2")
        self._s3 = session.client("s3")

    # -- helpers -------------------------------------------------------------

    def _error_code(self, exc: Exception) -> str:
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            error = response.get("Error")
            if isinstance(error, dict):
                return str(error.get("Code", ""))
        return ""

    # -- AwsReader surface (exactly the documented API list) -----------------

    def get_instance(self, instance_id: str) -> InstanceAttrs | None:
        """ec2:DescribeInstances."""
        try:
            response = self._ec2.describe_instances(InstanceIds=[instance_id])
        except self._client_error as exc:
            if self._error_code(exc).startswith("InvalidInstanceID"):
                return None
            raise
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                return InstanceAttrs(
                    instance_type=str(instance.get("InstanceType", "")),
                    tags=_tag_list_to_dict(instance.get("Tags")),
                )
        return None

    def get_security_group(self, sg_id: str) -> SecurityGroupAttrs | None:
        """ec2:DescribeSecurityGroups."""
        try:
            response = self._ec2.describe_security_groups(GroupIds=[sg_id])
        except self._client_error as exc:
            if self._error_code(exc).startswith("InvalidGroup"):
                return None
            raise
        groups = response.get("SecurityGroups", [])
        if not groups:
            return None
        group = groups[0]
        return SecurityGroupAttrs(
            ingress=_permissions_to_rules(group.get("IpPermissions")),
            egress=_permissions_to_rules(group.get("IpPermissionsEgress")),
        )

    def get_bucket(self, name: str) -> BucketAttrs | None:
        """s3:GetBucketVersioning + s3:GetBucketTagging."""
        try:
            versioning = self._s3.get_bucket_versioning(Bucket=name)
        except self._client_error as exc:
            if self._error_code(exc) in ("NoSuchBucket", "404"):
                return None
            raise
        try:
            tagging = self._s3.get_bucket_tagging(Bucket=name)
            tags = {
                str(t.get("Key", "")): str(t.get("Value", ""))
                for t in tagging.get("TagSet", [])
            }
        except self._client_error as exc:
            code = self._error_code(exc)
            if code in ("NoSuchBucket", "404"):
                return None
            if code != "NoSuchTagSet":
                raise
            tags = {}
        return BucketAttrs(
            tags=tags,
            versioning_enabled=versioning.get("Status") == "Enabled",
        )


def _tag_list_to_dict(tags: Any) -> dict[str, str]:
    if not isinstance(tags, list):
        return {}
    return {
        str(t.get("Key", "")): str(t.get("Value", ""))
        for t in tags
        if isinstance(t, dict)
    }


def _permissions_to_rules(permissions: Any) -> tuple[SecurityGroupRule, ...]:
    rules: list[SecurityGroupRule] = []
    if not isinstance(permissions, list):
        return ()
    for perm in permissions:
        if not isinstance(perm, dict):
            continue
        from_port = perm.get("FromPort")
        to_port = perm.get("ToPort")
        rules.append(
            SecurityGroupRule(
                protocol=str(perm.get("IpProtocol", "")),
                from_port=from_port if isinstance(from_port, int) else None,
                to_port=to_port if isinstance(to_port, int) else None,
                cidr_blocks=tuple(
                    str(r.get("CidrIp"))
                    for r in perm.get("IpRanges", [])
                    if isinstance(r, dict) and r.get("CidrIp")
                ),
                ipv6_cidr_blocks=tuple(
                    str(r.get("CidrIpv6"))
                    for r in perm.get("Ipv6Ranges", [])
                    if isinstance(r, dict) and r.get("CidrIpv6")
                ),
            )
        )
    return tuple(rules)
