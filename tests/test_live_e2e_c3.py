"""v1.1 chunk 3 — the --live-pricing CLI surface end to end (AC13-AC15,
AC18-AC20 e2e halves, A-c10..A-c12), renderer additions, and docs/IAM
conformance. The e2e seam is the documented one: monkeypatching
cli._make_live_pricing_source with a LivePricingSource over
FixturePricingClient. Everything offline; boto3 absent in this environment.
"""

from __future__ import annotations

import importlib.util
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from spend_sentinel.core.cost import HOURS_PER_MONTH
from spend_sentinel.pricing.fixture_client import FixturePricingClient
from spend_sentinel.pricing.live import (
    LiveFailureReason,
    LivePricingSource,
    build_query,
)

from .conftest import (
    fixture_path,
    load_snapshot,
    make_change,
    make_plan,
    run_analyze,
    run_analyze_json,
    write_plan,
)

BOTO3_INSTALLED = importlib.util.find_spec("boto3") is not None
REPO = Path(__file__).parent.parent
SNAPSHOT_DATA = load_snapshot()
SECRET = "HOSTILE-RESPONSE-9f1e2d"


def snap_rate(region: str, service: str, key: str) -> Decimal:
    return Decimal(SNAPSHOT_DATA["regions"][region][service][key])


def cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def entry(unit: str, usd: str, publication: str = "2026-08-20T00:00:00Z") -> str:
    return json.dumps(
        {
            "product": {"attributes": {}},
            "terms": {"OnDemand": {"T": {"priceDimensions": {
                "T.D": {"unit": unit, "pricePerUnit": {"USD": usd}}}}}},
            "publicationDate": publication,
        }
    )


def add(client: FixturePricingClient, service_key: str, price_key: str,
        payload, region: str = "us-east-1") -> None:
    spec = build_query(region, service_key, price_key)
    client.add(spec.service_code, spec.filters, payload)


def wire(monkeypatch, client: FixturePricingClient, **source_kwargs) -> None:
    """Install the fixture transport at the documented seam."""
    import spend_sentinel.cli as cli

    monkeypatch.setattr(
        cli,
        "_make_live_pricing_source",
        lambda snapshot: LivePricingSource(
            client, snapshot, endpoint_region="us-east-1", **source_kwargs
        ),
    )


def live_client_for_create_small() -> FixturePricingClient:
    """Valid fixtures for both create_small.json keys, rates differing from
    the snapshot, distinct publication dates (the AC14 setup)."""
    client = FixturePricingClient()
    add(client, "aws_instance", "t3.micro",
        [{"PriceList": [entry("Hrs", "0.0120", "2026-08-25T00:00:00Z")]}])
    add(client, "aws_ebs_volume", "gp3",
        [{"PriceList": [entry("GB-Mo", "0.0850", "2026-08-20T00:00:00Z")]}])
    return client


def assert_live_pricing_schema(lp: dict) -> None:
    """meta.live_pricing must match docs/verdict-schema.md exactly."""
    assert set(lp) == {"requested", "status", "endpoint_region", "lookups",
                       "publication_dates", "warnings"}
    assert lp["requested"] is True
    assert lp["status"] in {"ok", "degraded", "unavailable"}
    assert isinstance(lp["endpoint_region"], str)
    assert set(lp["lookups"]) == {"live", "snapshot_fallback", "miss"}
    assert all(isinstance(v, int) and v >= 0 for v in lp["lookups"].values())
    if lp["publication_dates"] is not None:
        assert set(lp["publication_dates"]) == {"earliest", "latest"}
    for w in lp["warnings"]:
        assert set(w) == {"reason", "detail"}
        assert w["reason"] in {r.value for r in LiveFailureReason}


class TestAc13DefaultPathUnchanged:
    def test_ac13_no_flag_outputs_carry_no_live_fields(self, runner):
        result, payload = run_analyze_json(
            runner, fixture_path("create_small.json"), "--skip-drift"
        )
        assert result.exit_code == 0
        dumped = json.dumps(payload)
        assert "live_pricing" not in dumped
        assert "price_source" not in dumped
        md = run_analyze(runner, fixture_path("create_small.json"), "--skip-drift")
        assert "Pricing:" not in md.stdout
        assert "| Source |" not in md.stdout

    def test_ac13_no_flag_byte_identical_across_runs(self, runner):
        first = run_analyze(runner, fixture_path("create_small.json"), "--skip-drift")
        second = run_analyze(runner, fixture_path("create_small.json"), "--skip-drift")
        assert first.stdout == second.stdout

    @pytest.mark.parametrize(
        ("plan_fixture", "expected_exit"),
        [
            ("create_small.json", 0),      # PASS
            ("create_expensive.json", 1),  # BLOCK
            ("delete_db.json", 0),         # WARN
        ],
    )
    def test_a11_exit_codes_never_differ_flag_vs_no_flag(
        self, runner, monkeypatch, plan_fixture, expected_exit
    ):
        """A11: degradation is warnings-only — with a fully failing live
        transport the exit code equals the snapshot-only run's."""
        plain = run_analyze(runner, fixture_path(plan_fixture), "--skip-drift")
        client = FixturePricingClient()  # everything falls back (no_match)
        wire(monkeypatch, client)
        live = run_analyze(
            runner, fixture_path(plan_fixture), "--skip-drift", "--live-pricing"
        )
        assert plain.exit_code == expected_exit
        assert live.exit_code == expected_exit


class TestAc14AllLive:
    def test_ac14_json_surface(self, runner, monkeypatch):
        wire(monkeypatch, live_client_for_create_small())
        result, payload = run_analyze_json(
            runner, fixture_path("create_small.json"), "--skip-drift",
            "--live-pricing",
        )
        assert result.exit_code == 0
        by_addr = {line["address"]: line for line in payload["cost"]["breakdown"]}
        instance = by_addr["aws_instance.web"]
        volume = by_addr["aws_ebs_volume.data"]
        assert instance["price_source"] == "live"
        assert volume["price_source"] == "live"
        assert instance["monthly_delta_usd"] == str(
            cents(Decimal("0.0120") * HOURS_PER_MONTH)
        )
        assert volume["monthly_delta_usd"] == str(cents(Decimal("0.0850") * 20))
        lp = payload["meta"]["live_pricing"]
        assert_live_pricing_schema(lp)
        assert lp["status"] == "ok"
        assert lp["lookups"] == {"live": 2, "snapshot_fallback": 0, "miss": 0}
        assert lp["publication_dates"] == {
            "earliest": "2026-08-20T00:00:00Z",
            "latest": "2026-08-25T00:00:00Z",
        }
        assert lp["warnings"] == []
        assert result.stderr == ""  # no degradation, no warning lines

    def test_ac14_markdown_surface(self, runner, monkeypatch):
        wire(monkeypatch, live_client_for_create_small())
        result = run_analyze(
            runner, fixture_path("create_small.json"), "--skip-drift",
            "--live-pricing",
        )
        assert result.exit_code == 0
        md = result.stdout
        assert "| Resource | Action | Monthly delta (USD) | Source |" in md
        assert md.count("| live |") == 2
        assert (
            "Pricing: live (2 live; prices published "
            "2026-08-20T00:00:00Z..2026-08-25T00:00:00Z)" in md
        )

    def test_r30_live_outputs_deterministic_across_runs(self, runner, monkeypatch):
        wire(monkeypatch, live_client_for_create_small())
        first = run_analyze(runner, fixture_path("create_small.json"),
                            "--skip-drift", "--live-pricing")
        # a fresh client for the second run (cache state must not matter)
        wire(monkeypatch, live_client_for_create_small())
        second = run_analyze(runner, fixture_path("create_small.json"),
                             "--skip-drift", "--live-pricing")
        assert first.stdout == second.stdout


class TestAc15PartialFallback:
    def wire_partial(self, monkeypatch) -> FixturePricingClient:
        client = FixturePricingClient()
        add(client, "aws_instance", "t3.micro",
            [{"PriceList": [entry("Hrs", "0.0120", "2026-08-25T00:00:00Z")]}])
        add(client, "aws_ebs_volume", "gp3", [{"PriceList": []}])  # no_match
        wire(monkeypatch, client)
        return client

    def test_ac15_json_surface(self, runner, monkeypatch):
        self.wire_partial(monkeypatch)
        result, payload = run_analyze_json(
            runner, fixture_path("create_small.json"), "--skip-drift",
            "--live-pricing",
        )
        assert result.exit_code == 0  # equals the snapshot-only run's exit
        by_addr = {line["address"]: line for line in payload["cost"]["breakdown"]}
        assert by_addr["aws_instance.web"]["price_source"] == "live"
        volume = by_addr["aws_ebs_volume.data"]
        assert volume["price_source"] == "snapshot"
        assert volume["monthly_delta_usd"] == str(
            cents(snap_rate("us-east-1", "aws_ebs_volume", "gp3") * 20)
        )
        lp = payload["meta"]["live_pricing"]
        assert_live_pricing_schema(lp)
        assert lp["status"] == "degraded"
        assert lp["lookups"] == {"live": 1, "snapshot_fallback": 1, "miss": 0}
        assert lp["warnings"] == [
            {"reason": "no_match", "detail": "aws_ebs_volume/gp3"}
        ]

    def test_ac15_single_stderr_warning_line_reasons_only(self, runner, monkeypatch):
        """A-c12: one line per distinct reason, internal enum only — no keys,
        no response text."""
        self.wire_partial(monkeypatch)
        result = run_analyze(
            runner, fixture_path("create_small.json"), "--skip-drift",
            "--live-pricing",
        )
        lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert lines == [
            "spend-sentinel: warning: live pricing degraded (no_match); "
            "snapshot fallback used"
        ]

    def test_ac15_markdown_mixed_sources(self, runner, monkeypatch):
        self.wire_partial(monkeypatch)
        result = run_analyze(
            runner, fixture_path("create_small.json"), "--skip-drift",
            "--live-pricing",
        )
        md = result.stdout
        assert "| live |" in md
        assert "| snapshot |" in md
        assert "Pricing: live (1 live, 1 snapshot-fallback; prices published" in md


class TestAc16MixedRdsThroughCli:
    def test_ac16_rds_mixed_source_rendered(self, runner, monkeypatch, tmp_path):
        client = FixturePricingClient()
        add(client, "aws_db_instance.instance", "postgres:db.t3.micro",
            [{"PriceList": [entry("Hrs", "0.0200")]}])
        # storage stays unregistered -> no_match -> snapshot
        wire(monkeypatch, client)
        plan = write_plan(
            tmp_path,
            make_plan(
                [
                    make_change(
                        address="aws_db_instance.db",
                        type_="aws_db_instance",
                        actions=["create"],
                        after={"engine": "postgres", "instance_class": "db.t3.micro",
                               "allocated_storage": 100, "multi_az": True},
                    )
                ],
                provider_region="us-east-1",
            ),
        )
        _result, payload = run_analyze_json(runner, plan, "--skip-drift",
                                            "--live-pricing")
        line = payload["cost"]["breakdown"][0]
        assert line["price_source"] == "mixed"
        expected = cents(
            Decimal("0.0200") * HOURS_PER_MONTH * 2
            + snap_rate("us-east-1", "aws_db_instance.storage", "gp2") * 100
        )
        assert line["monthly_delta_usd"] == str(expected)
        md = run_analyze(runner, plan, "--skip-drift", "--live-pricing")
        assert "| mixed |" in md.stdout


class TestAc18BudgetThroughCli:
    def test_ac18_two_calls_then_budget_exhausted(self, runner, monkeypatch, tmp_path):
        clock_value = [0.0]
        client = FixturePricingClient(
            on_call=lambda call: clock_value.__setitem__(0, clock_value[0] + 16.0)
        )
        keys = ["t3.micro", "t3.small", "t3.medium", "t3.large"]
        for key in keys:
            add(client, "aws_instance", key, [{"PriceList": [entry("Hrs", "0.01")]}])
        wire(monkeypatch, client, clock=lambda: clock_value[0])
        plan = write_plan(
            tmp_path,
            make_plan(
                [
                    make_change(address=f"aws_instance.i{n}", actions=["create"],
                                after={"instance_type": key})
                    for n, key in enumerate(keys)
                ],
                provider_region="us-east-1",
            ),
        )
        result, payload = run_analyze_json(runner, plan, "--skip-drift",
                                           "--live-pricing")
        assert result.exit_code == 0
        assert len(client.calls) == 2
        sources = [line["price_source"] for line in payload["cost"]["breakdown"]]
        assert sources.count("live") == 2
        assert sources.count("snapshot") == 2
        lp = payload["meta"]["live_pricing"]
        assert lp["status"] == "degraded"
        assert {w["reason"] for w in lp["warnings"]} == {"budget_exhausted"}
        # A-c12: the two budget_exhausted keys collapse to ONE stderr line
        stderr_lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert stderr_lines == [
            "spend-sentinel: warning: live pricing degraded (budget_exhausted); "
            "snapshot fallback used"
        ]


@pytest.mark.skipif(BOTO3_INSTALLED, reason="boto3 is installed in this environment")
class TestAc19Boto3Absent:
    def test_ac19_real_wiring_full_snapshot_fallback(self, runner, monkeypatch):
        """No monkeypatched seam: the production wiring degrades with
        boto3_missing and the run succeeds."""
        monkeypatch.delenv("SPEND_SENTINEL_PRICING_ENDPOINT_REGION", raising=False)
        plain = run_analyze(runner, fixture_path("create_small.json"), "--skip-drift")
        result, payload = run_analyze_json(
            runner, fixture_path("create_small.json"), "--skip-drift",
            "--live-pricing",
        )
        assert result.exit_code == plain.exit_code == 0
        assert all(
            line["price_source"] == "snapshot"
            for line in payload["cost"]["breakdown"]
        )
        lp = payload["meta"]["live_pricing"]
        assert_live_pricing_schema(lp)
        assert lp["status"] == "unavailable"
        assert lp["warnings"] == [{"reason": "boto3_missing", "detail": ""}]
        # c2 note: fallback lookups still counted on unavailable runs
        assert lp["lookups"] == {"live": 0, "snapshot_fallback": 2, "miss": 0}
        assert lp["publication_dates"] is None
        stderr_lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert stderr_lines == [
            "spend-sentinel: warning: live pricing degraded (boto3_missing); "
            "snapshot fallback used"
        ]

    def test_ac19_markdown_unavailable_summary_line(self, runner, monkeypatch):
        monkeypatch.delenv("SPEND_SENTINEL_PRICING_ENDPOINT_REGION", raising=False)
        result = run_analyze(
            runner, fixture_path("create_small.json"), "--skip-drift",
            "--live-pricing",
        )
        version = SNAPSHOT_DATA["meta"]["version"]
        date = SNAPSHOT_DATA["meta"]["snapshot_date"]
        assert (
            f"Pricing: snapshot v{version} ({date}) — live pricing unavailable: "
            "boto3_missing" in result.stdout
        )
        # the Source column still renders (all snapshot) so the report shape
        # is stable under the flag
        assert "| Source |" in result.stdout

    def test_a_c11_invalid_endpoint_env_reported_as_invalid_never_echoed(
        self, runner, monkeypatch
    ):
        hostile = "US-EAST-1;curl evil"
        monkeypatch.setenv("SPEND_SENTINEL_PRICING_ENDPOINT_REGION", hostile)
        result, payload = run_analyze_json(
            runner, fixture_path("create_small.json"), "--skip-drift",
            "--live-pricing",
        )
        assert result.exit_code == 0
        lp = payload["meta"]["live_pricing"]
        assert lp["status"] == "unavailable"
        assert lp["warnings"][0]["reason"] == "client_init_error"
        assert lp["endpoint_region"] == "invalid"
        assert hostile not in json.dumps(payload)
        assert hostile not in result.stderr
        md = run_analyze(runner, fixture_path("create_small.json"), "--skip-drift",
                         "--live-pricing")
        assert hostile not in md.stdout


class TestAc20HostileResponsesThroughCli:
    @pytest.mark.parametrize(
        ("payload_pages", "reason"),
        [
            ([{"PriceList": [json.dumps({"pad": SECRET + "x" * (300 * 1024)})]}],
             "oversize_response"),
            ([{"PriceList": [entry("GB-Mo", "NaN")]}], "parse_error"),
            ([{"PriceList": [entry("GB-Mo", "-3.50")]}], "parse_error"),
            ([{"PriceList": [f'{{"broken": "{SECRET}"']}], "parse_error"),
        ],
    )
    def test_ac20_hostile_volume_response_falls_back_no_echo(
        self, runner, monkeypatch, payload_pages, reason
    ):
        client = FixturePricingClient()
        add(client, "aws_instance", "t3.micro",
            [{"PriceList": [entry("Hrs", "0.0120")]}])
        add(client, "aws_ebs_volume", "gp3", payload_pages)
        wire(monkeypatch, client)
        result, payload = run_analyze_json(
            runner, fixture_path("create_small.json"), "--skip-drift",
            "--live-pricing",
        )
        assert result.exit_code == 0
        by_addr = {line["address"]: line for line in payload["cost"]["breakdown"]}
        assert by_addr["aws_ebs_volume.data"]["price_source"] == "snapshot"
        lp = payload["meta"]["live_pricing"]
        assert {w["reason"] for w in lp["warnings"]} == {reason}
        # no fragment of the response bodies anywhere
        assert SECRET not in json.dumps(payload)
        assert SECRET not in result.stderr
        md = run_analyze(runner, fixture_path("create_small.json"), "--skip-drift",
                         "--live-pricing")
        assert SECRET not in md.stdout


class TestRendererEscaping:
    def test_r30_hostile_publication_dates_never_reach_markdown(self, runner,
                                                                monkeypatch):
        """A non-ISO publicationDate is dropped at extraction (R31 review
        hardening), so a hostile date reaches the report in NO form — neither
        raw nor escaped — and the summary omits the publication clause while
        the rates stay live. (The renderer still escapes valid-shaped dates
        as defense in depth.)"""
        hostile_date = "2026|08`<b>injected</b>"
        client = FixturePricingClient()
        add(client, "aws_instance", "t3.micro",
            [{"PriceList": [entry("Hrs", "0.0120", hostile_date)]}])
        add(client, "aws_ebs_volume", "gp3",
            [{"PriceList": [entry("GB-Mo", "0.0850", hostile_date)]}])
        wire(monkeypatch, client)
        result = run_analyze(
            runner, fixture_path("create_small.json"), "--skip-drift",
            "--live-pricing",
        )
        md = result.stdout
        assert hostile_date not in md          # raw form never appears
        assert "injected" not in md            # nor any escaped fragment
        assert "prices published" not in md    # no dates -> clause omitted
        assert "Pricing: live (2 live)" in md  # both rates still live
        # still exactly one verdict header line
        assert [ln for ln in md.splitlines() if ln.startswith("Verdict:")] == [
            "Verdict: PASS"
        ]


class TestDocsAndIamAc21:
    def test_ac21_pricing_policy_exactly_get_products(self):
        policy = json.loads(
            (REPO / "docs" / "iam-policy-pricing.json").read_text(encoding="utf-8")
        )
        statements = policy["Statement"]
        assert len(statements) == 1
        assert statements[0]["Action"] == ["pricing:GetProducts"]
        assert statements[0]["Effect"] == "Allow"
        assert statements[0]["Resource"] == "*"

    def test_ac21_drift_policy_unchanged_from_v1(self):
        """The v1 drift policy must not have grown (pinned action list)."""
        policy = json.loads(
            (REPO / "docs" / "iam-policy.json").read_text(encoding="utf-8")
        )
        assert sorted(policy["Statement"][0]["Action"]) == [
            "ec2:DescribeInstances",
            "ec2:DescribeSecurityGroups",
            "s3:GetBucketLocation",
            "s3:GetBucketTagging",
            "s3:GetBucketVersioning",
        ]

    def test_r30_schema_doc_documents_live_pricing(self):
        doc = (REPO / "docs" / "verdict-schema.md").read_text(encoding="utf-8")
        for needle in ("live_pricing", "publication_dates", "snapshot_fallback",
                       "endpoint_region", '"ok | degraded | unavailable"'):
            assert needle in doc
