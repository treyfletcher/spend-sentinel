"""R9: per-type drift comparators over the exact attribute allowlists,
exercised offline through detect() with FixtureAwsReader / in-memory fakes.

Also covers A-i16 (versioning normalization), A-i17 (managed-only, child
modules walked), and the committed AC7-flavored fixture scenario.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spend_sentinel.adapters.fixture_reader import FixtureAwsReader
from spend_sentinel.core.drift import detect
from spend_sentinel.core.models import DriftKind, DriftStatus, State

from .conftest import make_state, make_state_resource

FIXDIR = Path(__file__).parent / "fixtures"


def state_of(*resources, child_modules=None) -> State:
    return State.model_validate(make_state(list(resources), child_modules=child_modules))


def reader_of(**data) -> FixtureAwsReader:
    return FixtureAwsReader(data)


INSTANCE = {"id": "i-1", "instance_type": "t3.micro", "tags": {"Name": "web", "Env": "prod"}}


class TestInstanceComparator:
    def test_r9_instance_no_drift_when_equal(self):
        report = detect(
            state_of(make_state_resource(values=dict(INSTANCE))),
            reader_of(instances={"i-1": {"instance_type": "t3.micro",
                                         "tags": {"Name": "web", "Env": "prod"}}}),
        )
        assert report.status is DriftStatus.RAN
        assert report.drifts == ()
        assert report.errors == ()

    def test_r9_instance_type_drift(self):
        report = detect(
            state_of(make_state_resource(values=dict(INSTANCE))),
            reader_of(instances={"i-1": {"instance_type": "t3.medium",
                                         "tags": {"Name": "web", "Env": "prod"}}}),
        )
        assert len(report.drifts) == 1
        drift = report.drifts[0]
        assert drift.address == "aws_instance.web"
        assert drift.kind is DriftKind.CHANGED
        assert drift.attribute == "instance_type"
        assert drift.state_value == "t3.micro"
        assert drift.live_value == "t3.medium"

    def test_r9_instance_tags_drift(self):
        report = detect(
            state_of(make_state_resource(values=dict(INSTANCE))),
            reader_of(instances={"i-1": {"instance_type": "t3.micro",
                                         "tags": {"Name": "web"}}}),  # Env tag lost
        )
        assert [d.attribute for d in report.drifts] == ["tags"]
        assert report.drifts[0].state_value == {"Name": "web", "Env": "prod"}
        assert report.drifts[0].live_value == {"Name": "web"}

    def test_r9_attributes_outside_allowlist_ignored(self):
        """Only instance_type and tags are compared: other differing state
        attributes (ami, monitoring, ...) must not produce drift."""
        values = dict(INSTANCE, ami="ami-11111111", monitoring=True, ebs_optimized=False)
        report = detect(
            state_of(make_state_resource(values=values)),
            reader_of(instances={"i-1": {"instance_type": "t3.micro",
                                         "tags": {"Name": "web", "Env": "prod"}}}),
        )
        assert report.drifts == ()

    def test_r9_both_attributes_drift_two_entries(self):
        report = detect(
            state_of(make_state_resource(values=dict(INSTANCE))),
            reader_of(instances={"i-1": {"instance_type": "m5.large", "tags": {}}}),
        )
        assert sorted(d.attribute for d in report.drifts) == ["instance_type", "tags"]


SG_STATE = {
    "id": "sg-1",
    "ingress": [
        {"protocol": "tcp", "from_port": 80, "to_port": 80,
         "cidr_blocks": ["10.0.0.0/8", "192.168.0.0/16"], "ipv6_cidr_blocks": []},
        {"protocol": "tcp", "from_port": 443, "to_port": 443,
         "cidr_blocks": ["0.0.0.0/0"], "ipv6_cidr_blocks": ["::/0"]},
    ],
    "egress": [
        {"protocol": "-1", "from_port": 0, "to_port": 0,
         "cidr_blocks": ["0.0.0.0/0"], "ipv6_cidr_blocks": []},
    ],
}


def sg_resource():
    return make_state_resource(
        address="aws_security_group.app", type_="aws_security_group", values=SG_STATE
    )


class TestSecurityGroupComparator:
    def test_r9_sg_equal_rules_different_order_no_drift(self):
        """Order-insensitive comparison: same rules, reversed order and CIDRs
        regrouped one-per-rule (AWS-style) -> no drift."""
        live = {
            "ingress": [
                {"protocol": "tcp", "from_port": 443, "to_port": 443,
                 "ipv6_cidr_blocks": ["::/0"]},
                {"protocol": "tcp", "from_port": 443, "to_port": 443,
                 "cidr_blocks": ["0.0.0.0/0"]},
                {"protocol": "tcp", "from_port": 80, "to_port": 80,
                 "cidr_blocks": ["192.168.0.0/16"]},
                {"protocol": "tcp", "from_port": 80, "to_port": 80,
                 "cidr_blocks": ["10.0.0.0/8"]},
            ],
            "egress": [
                {"protocol": "-1", "from_port": 0, "to_port": 0,
                 "cidr_blocks": ["0.0.0.0/0"]},
            ],
        }
        report = detect(state_of(sg_resource()), reader_of(security_groups={"sg-1": live}))
        assert report.drifts == ()

    def test_r9_sg_extra_live_rule_drifts(self):
        """AC7: one extra live ingress rule -> exactly one drift naming the
        rule-set attribute."""
        live = {
            "ingress": SG_STATE["ingress"]
            + [{"protocol": "tcp", "from_port": 22, "to_port": 22,
                "cidr_blocks": ["0.0.0.0/0"]}],
            "egress": SG_STATE["egress"],
        }
        report = detect(state_of(sg_resource()), reader_of(security_groups={"sg-1": live}))
        assert len(report.drifts) == 1
        drift = report.drifts[0]
        assert drift.address == "aws_security_group.app"
        assert drift.attribute == "ingress"
        assert any("22" in rule for rule in drift.live_value)
        assert not any("22" in rule for rule in drift.state_value)

    def test_r9_sg_removed_live_rule_drifts(self):
        live = {"ingress": SG_STATE["ingress"][:1], "egress": SG_STATE["egress"]}
        report = detect(state_of(sg_resource()), reader_of(security_groups={"sg-1": live}))
        assert [d.attribute for d in report.drifts] == ["ingress"]

    def test_r9_sg_modified_port_drifts(self):
        live = {
            "ingress": [
                {"protocol": "tcp", "from_port": 80, "to_port": 8080,  # was 80-80
                 "cidr_blocks": ["10.0.0.0/8", "192.168.0.0/16"]},
                SG_STATE["ingress"][1],
            ],
            "egress": SG_STATE["egress"],
        }
        report = detect(state_of(sg_resource()), reader_of(security_groups={"sg-1": live}))
        assert [d.attribute for d in report.drifts] == ["ingress"]

    def test_r9_sg_egress_compared_independently(self):
        live = {"ingress": SG_STATE["ingress"], "egress": []}
        report = detect(state_of(sg_resource()), reader_of(security_groups={"sg-1": live}))
        assert [d.attribute for d in report.drifts] == ["egress"]

    def test_r9_sg_both_directions_drift(self):
        live = {"ingress": [], "egress": []}
        report = detect(state_of(sg_resource()), reader_of(security_groups={"sg-1": live}))
        assert sorted(d.attribute for d in report.drifts) == ["egress", "ingress"]


class TestBucketComparator:
    def bucket(self, versioning, tags=None):
        return make_state_resource(
            address="aws_s3_bucket.b",
            type_="aws_s3_bucket",
            values={"bucket": "my-bucket", "tags": tags or {"Team": "core"},
                    "versioning": versioning},
        )

    def test_r9_bucket_no_drift_when_equal(self):
        report = detect(
            state_of(self.bucket([{"enabled": True}])),
            reader_of(buckets={"my-bucket": {"tags": {"Team": "core"},
                                             "versioning_enabled": True}}),
        )
        assert report.drifts == ()

    def test_r9_bucket_tags_drift(self):
        report = detect(
            state_of(self.bucket([{"enabled": True}])),
            reader_of(buckets={"my-bucket": {"tags": {"Team": "other"},
                                             "versioning_enabled": True}}),
        )
        assert [d.attribute for d in report.drifts] == ["tags"]

    @pytest.mark.parametrize(
        ("state_versioning", "live_enabled", "expect_drift"),
        [
            ([{"enabled": True}], False, True),   # provider-v3 block list
            ([{"enabled": False}], True, True),
            ({"enabled": True}, True, False),     # A-i16 map form
            ([], False, False),                   # absent -> disabled
            (None, False, False),
            ([], True, True),
        ],
    )
    def test_r9_bucket_versioning_normalization(self, state_versioning, live_enabled,
                                                expect_drift):
        values = {"bucket": "my-bucket", "tags": {"Team": "core"}}
        if state_versioning is not None:
            values["versioning"] = state_versioning
        report = detect(
            state_of(make_state_resource(address="aws_s3_bucket.b", type_="aws_s3_bucket",
                                         values=values)),
            reader_of(buckets={"my-bucket": {"tags": {"Team": "core"},
                                             "versioning_enabled": live_enabled}}),
        )
        attrs = [d.attribute for d in report.drifts]
        assert attrs == (["versioning"] if expect_drift else [])

    def test_r9_bucket_versioning_values_reported(self):
        report = detect(
            state_of(self.bucket([{"enabled": True}])),
            reader_of(buckets={"my-bucket": {"tags": {"Team": "core"},
                                             "versioning_enabled": False}}),
        )
        drift = report.drifts[0]
        assert drift.state_value is True
        assert drift.live_value is False


class TestStateScope:
    def test_r9_data_sources_excluded_a_i17(self):
        """A data-source aws_instance in state must not be compared (or error)."""
        data_source = make_state_resource(
            address="data.aws_instance.lookup", mode="data", values={"id": "i-data"}
        )
        report = detect(state_of(data_source), reader_of())
        assert report.drifts == ()
        assert report.errors == ()
        assert report.skipped == ()

    def test_r9_child_module_resources_compared(self):
        child = {
            "resources": [
                make_state_resource(
                    address="module.a.aws_instance.deep",
                    values={"id": "i-deep", "instance_type": "t3.micro", "tags": {}},
                )
            ],
            "child_modules": [],
        }
        report = detect(
            state_of(child_modules=[child]),
            reader_of(instances={"i-deep": {"instance_type": "t3.large", "tags": {}}}),
        )
        assert [d.address for d in report.drifts] == ["module.a.aws_instance.deep"]

    def test_r9_empty_state_yields_empty_ran_report(self):
        report = detect(state_of(), reader_of())
        assert report.status is DriftStatus.RAN
        assert report.drifts == report.skipped == report.errors == ()


class TestMixedFixtureScenario:
    """The committed AC7-flavored fixture pair exercised end to end."""

    @pytest.fixture()
    def report(self):
        state = State.model_validate(
            __import__("json").loads((FIXDIR / "states" / "state_mixed.json").read_text())
        )
        reader = FixtureAwsReader.from_path(FIXDIR / "aws_responses" / "live_mixed.json")
        return detect(state, reader)

    def test_r9_fixture_instance_resize_detected(self, report):
        by_addr = {(d.address, d.attribute): d for d in report.drifts}
        drift = by_addr[("aws_instance.web", "instance_type")]
        assert drift.state_value == "t3.micro"
        assert drift.live_value == "t3.medium"

    def test_r9_fixture_regrouped_sg_rules_no_drift(self, report):
        assert not any(d.address == "aws_security_group.app" for d in report.drifts)

    def test_r9_fixture_child_module_versioning_drift(self, report):
        by_addr = {(d.address, d.attribute): d for d in report.drifts}
        assert ("module.store.aws_s3_bucket.logs", "versioning") in by_addr

    def test_r10_fixture_missing_bucket(self, report):
        missing = [d for d in report.drifts if d.kind is DriftKind.MISSING]
        assert [d.address for d in missing] == ["aws_s3_bucket.gone"]

    def test_r10_fixture_unsupported_type_skipped(self, report):
        assert [(s.address, s.type, s.reason) for s in report.skipped] == [
            ("aws_lambda_function.fn", "aws_lambda_function", "unsupported_type")
        ]

    def test_r12_fixture_error_captured_run_continues(self, report):
        assert [e.address for e in report.errors] == ["module.store.aws_instance.err"]
        assert "AuthFailure" in report.errors[0].error
        # the run still produced every other result
        assert len(report.drifts) >= 3
