"""Boto3AwsReader unit tests over a stubbed boto3 — no network, no real boto3.

Spec Security: the live adapter must call no AWS APIs outside the documented
read-only list (ec2:DescribeInstances, ec2:DescribeSecurityGroups,
s3:GetBucketVersioning, s3:GetBucketTagging, s3:GetBucketLocation). The stub
records every client method touched so the surface can be asserted.
"""

from __future__ import annotations

import importlib.util
import sys
import types

import pytest

BOTO3_INSTALLED = importlib.util.find_spec("boto3") is not None

#: boto3 client method -> IAM action, for the documented list in the spec.
DOCUMENTED_ACTIONS = {
    "describe_instances": "ec2:DescribeInstances",
    "describe_security_groups": "ec2:DescribeSecurityGroups",
    "get_bucket_versioning": "s3:GetBucketVersioning",
    "get_bucket_tagging": "s3:GetBucketTagging",
    "get_bucket_location": "s3:GetBucketLocation",
}


class StubClientError(Exception):
    def __init__(self, code: str, message: str = "stub error") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class RecordingClient:
    """Fake boto3 client: canned responses per method, records every access."""

    def __init__(self, service: str, responses: dict, calls: list) -> None:
        self._service = service
        self._responses = responses
        self._calls = calls

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def method(**kwargs):
            self._calls.append((self._service, name, kwargs))
            outcome = self._responses.get(name)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is None:
                raise AssertionError(
                    f"unexpected AWS API call: {self._service}.{name} — outside the "
                    "documented action list or missing a stub response"
                )
            return outcome

        return method


class StubSession:
    created_clients: list

    def __init__(self, region_name=None, responses=None, calls=None) -> None:
        self.region_name = region_name
        self._responses = responses or {}
        self._calls = calls if calls is not None else []
        StubSession.created_clients = []

    def client(self, service: str):
        StubSession.created_clients.append(service)
        return RecordingClient(service, self._responses.get(service, {}), self._calls)


@pytest.fixture()
def stub_boto3(monkeypatch):
    """Install stub `boto3` and `botocore.exceptions` modules; returns a
    factory building a Boto3AwsReader over canned responses + its call log."""
    calls: list = []
    holder: dict = {"responses": {}}

    class Session(StubSession):
        def __init__(self, region_name=None):
            super().__init__(region_name, holder["responses"], calls)

    boto3_mod = types.ModuleType("boto3")
    session_mod = types.ModuleType("boto3.session")
    session_mod.Session = Session
    boto3_mod.session = session_mod
    botocore_mod = types.ModuleType("botocore")
    exceptions_mod = types.ModuleType("botocore.exceptions")
    exceptions_mod.ClientError = StubClientError
    botocore_mod.exceptions = exceptions_mod

    monkeypatch.setitem(sys.modules, "boto3", boto3_mod)
    monkeypatch.setitem(sys.modules, "boto3.session", session_mod)
    monkeypatch.setitem(sys.modules, "botocore", botocore_mod)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions_mod)

    def build(responses: dict):
        from spend_sentinel.adapters.boto3_reader import Boto3AwsReader

        holder["responses"] = responses
        return Boto3AwsReader(region="us-east-1"), calls

    return build


class TestConstruction:
    def test_creates_only_ec2_and_s3_clients(self, stub_boto3):
        stub_boto3({})
        assert sorted(StubSession.created_clients) == ["ec2", "s3"]

    @pytest.mark.skipif(BOTO3_INSTALLED, reason="boto3 is installed in this environment")
    def test_raises_boto3_not_installed_without_stub(self):
        from spend_sentinel.adapters.boto3_reader import (
            Boto3AwsReader,
            Boto3NotInstalledError,
        )

        with pytest.raises(Boto3NotInstalledError):
            Boto3AwsReader(region="us-east-1")


class TestGetInstance:
    def test_maps_reservation_to_attrs(self, stub_boto3):
        reader, calls = stub_boto3(
            {
                "ec2": {
                    "describe_instances": {
                        "Reservations": [
                            {
                                "Instances": [
                                    {
                                        "InstanceType": "t3.micro",
                                        "Tags": [{"Key": "Name", "Value": "web"},
                                                 {"Key": "Env", "Value": "prod"}],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        )
        attrs = reader.get_instance("i-123")
        assert attrs is not None
        assert attrs.instance_type == "t3.micro"
        assert attrs.tags == {"Name": "web", "Env": "prod"}
        assert [(svc, name) for svc, name, _ in calls] == [("ec2", "describe_instances")]
        assert calls[0][2] == {"InstanceIds": ["i-123"]}

    def test_not_found_client_error_returns_none(self, stub_boto3):
        reader, _ = stub_boto3(
            {"ec2": {"describe_instances": StubClientError("InvalidInstanceID.NotFound")}}
        )
        assert reader.get_instance("i-gone") is None

    def test_other_client_error_propagates(self, stub_boto3):
        reader, _ = stub_boto3(
            {"ec2": {"describe_instances": StubClientError("AuthFailure")}}
        )
        with pytest.raises(StubClientError):
            reader.get_instance("i-123")

    def test_empty_reservations_returns_none(self, stub_boto3):
        reader, _ = stub_boto3({"ec2": {"describe_instances": {"Reservations": []}}})
        assert reader.get_instance("i-123") is None


class TestGetSecurityGroup:
    def test_maps_permissions_to_rules(self, stub_boto3):
        reader, _ = stub_boto3(
            {
                "ec2": {
                    "describe_security_groups": {
                        "SecurityGroups": [
                            {
                                "IpPermissions": [
                                    {
                                        "IpProtocol": "tcp",
                                        "FromPort": 443,
                                        "ToPort": 443,
                                        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                                        "Ipv6Ranges": [{"CidrIpv6": "::/0"}],
                                    }
                                ],
                                "IpPermissionsEgress": [
                                    {"IpProtocol": "-1",
                                     "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
                                ],
                            }
                        ]
                    }
                }
            }
        )
        attrs = reader.get_security_group("sg-123")
        assert attrs is not None
        rule = attrs.ingress[0]
        assert (rule.protocol, rule.from_port, rule.to_port) == ("tcp", 443, 443)
        assert rule.cidr_blocks == ("0.0.0.0/0",)
        assert rule.ipv6_cidr_blocks == ("::/0",)
        egress = attrs.egress[0]
        assert (egress.protocol, egress.from_port) == ("-1", None)

    def test_not_found_returns_none(self, stub_boto3):
        reader, _ = stub_boto3(
            {"ec2": {"describe_security_groups": StubClientError("InvalidGroup.NotFound")}}
        )
        assert reader.get_security_group("sg-gone") is None

    def test_empty_groups_returns_none(self, stub_boto3):
        reader, _ = stub_boto3(
            {"ec2": {"describe_security_groups": {"SecurityGroups": []}}}
        )
        assert reader.get_security_group("sg-123") is None


class TestGetBucket:
    def test_maps_versioning_and_tags(self, stub_boto3):
        reader, _ = stub_boto3(
            {
                "s3": {
                    "get_bucket_versioning": {"Status": "Enabled"},
                    "get_bucket_tagging": {"TagSet": [{"Key": "Team", "Value": "core"}]},
                }
            }
        )
        attrs = reader.get_bucket("my-bucket")
        assert attrs is not None
        assert attrs.versioning_enabled is True
        assert attrs.tags == {"Team": "core"}

    def test_suspended_versioning_is_disabled(self, stub_boto3):
        reader, _ = stub_boto3(
            {
                "s3": {
                    "get_bucket_versioning": {"Status": "Suspended"},
                    "get_bucket_tagging": {"TagSet": []},
                }
            }
        )
        attrs = reader.get_bucket("my-bucket")
        assert attrs.versioning_enabled is False

    def test_no_such_tagset_means_empty_tags(self, stub_boto3):
        reader, _ = stub_boto3(
            {
                "s3": {
                    "get_bucket_versioning": {},
                    "get_bucket_tagging": StubClientError("NoSuchTagSet"),
                }
            }
        )
        attrs = reader.get_bucket("my-bucket")
        assert attrs is not None
        assert attrs.tags == {}

    def test_no_such_bucket_returns_none(self, stub_boto3):
        reader, _ = stub_boto3(
            {"s3": {"get_bucket_versioning": StubClientError("NoSuchBucket")}}
        )
        assert reader.get_bucket("gone") is None

    def test_other_tagging_error_propagates(self, stub_boto3):
        reader, _ = stub_boto3(
            {
                "s3": {
                    "get_bucket_versioning": {},
                    "get_bucket_tagging": StubClientError("AccessDenied"),
                }
            }
        )
        with pytest.raises(StubClientError):
            reader.get_bucket("my-bucket")


class TestApiSurface:
    def test_security_api_surface_within_documented_actions(self, stub_boto3):
        """Drive every reader method; the union of AWS APIs touched must stay
        inside the documented five-action list (spec Security)."""
        reader, calls = stub_boto3(
            {
                "ec2": {
                    "describe_instances": {"Reservations": []},
                    "describe_security_groups": {"SecurityGroups": []},
                },
                "s3": {
                    "get_bucket_versioning": {},
                    "get_bucket_tagging": {"TagSet": []},
                },
            }
        )
        reader.get_instance("i-1")
        reader.get_security_group("sg-1")
        reader.get_bucket("b-1")
        touched = {name for _svc, name, _ in calls}
        assert touched <= set(DOCUMENTED_ACTIONS), (
            f"undocumented AWS API used: {touched - set(DOCUMENTED_ACTIONS)}"
        )
        # and the reader exercises exactly the four it needs
        assert touched == {
            "describe_instances",
            "describe_security_groups",
            "get_bucket_versioning",
            "get_bucket_tagging",
        }
