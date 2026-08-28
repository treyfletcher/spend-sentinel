"""v1.1 chunk 1 — R25 filter matrix, R26 region table, R31 defensive
extraction, and pagination (spend_sentinel/pricing/live.py). Pure/offline:
fixtures under tests/fixtures/pricing_api/ are realistic raw GetProducts
responses (PriceList of JSON strings).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from spend_sentinel.pricing.fixture_client import FixturePricingClient
from spend_sentinel.pricing.live import (
    MAX_PAGES_PER_KEY,
    MAX_PRICE_LIST_ENTRY_BYTES,
    MAX_PRODUCTS_PER_KEY,
    RDS_ENGINE_MAP,
    RDS_STORAGE_MAP,
    REGION_LOCATIONS,
    DimensionRule,
    ExtractionError,
    LiveFailureReason,
    PricingApiError,
    QuerySpec,
    UnmappableKeyError,
    build_query,
    extract_rate,
    fetch_pages,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pricing_api"

LOC_USE1 = "US East (N. Virginia)"


def load_pages(name: str) -> list[dict]:
    return [json.loads((FIXTURES / name).read_text(encoding="utf-8"))]


def price_entry(dimensions: list[dict], publication: str = "2026-08-20T00:00:00Z",
                attributes: dict | None = None) -> str:
    """A synthetic PriceList item (JSON string, as the API ships it)."""
    ondemand = {}
    for i, dim in enumerate(dimensions):
        price_dim = {"unit": dim["unit"], "pricePerUnit": {"USD": dim["usd"]}}
        if "usagetype" in dim:
            price_dim["usagetype"] = dim["usagetype"]
        if dim.get("no_usd"):
            price_dim["pricePerUnit"] = {}
        ondemand[f"T{i}"] = {"priceDimensions": {f"T{i}.D": price_dim}}
    return json.dumps(
        {
            "product": {"attributes": attributes or {}},
            "terms": {"OnDemand": ondemand},
            "publicationDate": publication,
        }
    )


def pages_of(*entries: str) -> list[dict]:
    return [{"PriceList": list(entries)}]


HRS = DimensionRule(unit="Hrs")


class TestRegionLocationsR26:
    @pytest.mark.parametrize(
        ("region", "location"),
        [
            ("us-east-1", "US East (N. Virginia)"),
            ("us-west-2", "US West (Oregon)"),
            ("eu-west-1", "EU (Ireland)"),
        ],
    )
    def test_r26_snapshot_regions_mapped_to_exact_location_names(self, region, location):
        assert REGION_LOCATIONS[region] == location

    def test_r26_unmapped_region_raises_unsupported_region_for_every_type(self):
        for service_key, price_key in [
            ("aws_instance", "t3.micro"),
            ("aws_ebs_volume", "gp3"),
            ("aws_db_instance.instance", "postgres:db.t3.micro"),
            ("aws_db_instance.storage", "gp2"),
            ("aws_nat_gateway", "hourly"),
            ("aws_lb", "application"),
        ]:
            with pytest.raises(UnmappableKeyError) as excinfo:
                build_query("ap-southeast-7", service_key, price_key)
            assert excinfo.value.reason is LiveFailureReason.UNSUPPORTED_REGION


class TestBuildQueryMatrixR25:
    def test_r25_ec2_filters_verbatim(self):
        spec = build_query("us-east-1", "aws_instance", "t3.micro")
        assert spec == QuerySpec(
            service_code="AmazonEC2",
            filters=(
                ("instanceType", "t3.micro"),
                ("location", LOC_USE1),
                ("operatingSystem", "Linux"),
                ("tenancy", "Shared"),
                ("preInstalledSw", "NA"),
                ("capacitystatus", "Used"),
            ),
            rule=DimensionRule(unit="Hrs"),
        )

    @pytest.mark.parametrize("volume_type", ["gp2", "gp3", "io1", "io2", "st1", "standard"])
    def test_r25_ebs_filters_verbatim(self, volume_type):
        spec = build_query("us-west-2", "aws_ebs_volume", volume_type)
        assert spec == QuerySpec(
            service_code="AmazonEC2",
            filters=(
                ("volumeApiName", volume_type),
                ("location", "US West (Oregon)"),
                ("productFamily", "Storage"),
            ),
            rule=DimensionRule(unit="GB-Mo"),
        )

    @pytest.mark.parametrize(
        ("engine", "mapped"),
        [("postgres", "PostgreSQL"), ("mysql", "MySQL"), ("mariadb", "MariaDB")],
    )
    def test_r25_rds_instance_filters_engine_map_and_single_az(self, engine, mapped):
        spec = build_query("eu-west-1", "aws_db_instance.instance",
                           f"{engine}:db.m5.large")
        assert spec.service_code == "AmazonRDS"
        assert spec.filters == (
            ("instanceType", "db.m5.large"),
            ("databaseEngine", mapped),
            ("deploymentOption", "Single-AZ"),
            ("location", "EU (Ireland)"),
        )
        assert spec.rule == DimensionRule(unit="Hrs")

    def test_r25_rds_always_single_az_never_multi_az(self):
        """The critical double-counting rule: the filter is Single-AZ no matter
        what — core/cost.py does the multi_az doubling itself."""
        spec = build_query("us-east-1", "aws_db_instance.instance",
                           "postgres:db.t3.micro")
        assert ("deploymentOption", "Single-AZ") in spec.filters
        assert all(v != "Multi-AZ" for _f, v in spec.filters)

    @pytest.mark.parametrize(
        ("storage", "mapped"),
        [
            ("gp2", "General Purpose"),
            ("gp3", "General Purpose-GP3"),
            ("io1", "Provisioned IOPS"),
            ("standard", "Magnetic"),
        ],
    )
    def test_r25_rds_storage_filters_verbatim(self, storage, mapped):
        spec = build_query("us-east-1", "aws_db_instance.storage", storage)
        assert spec.service_code == "AmazonRDS"
        assert spec.filters == (
            ("volumeType", mapped),
            ("deploymentOption", "Single-AZ"),
            ("productFamily", "Database Storage"),
            ("location", LOC_USE1),
        )
        assert spec.rule == DimensionRule(unit="GB-Mo")

    def test_r25_nat_filters_and_dimension_rule(self):
        spec = build_query("us-east-1", "aws_nat_gateway", "hourly")
        assert spec.service_code == "AmazonEC2"
        assert spec.filters == (("productFamily", "NAT Gateway"), ("location", LOC_USE1))
        assert spec.rule == DimensionRule(unit="Hrs", usagetype_suffix="NatGateway-Hours")

    @pytest.mark.parametrize(
        ("lb_type", "family"),
        [("application", "Load Balancer-Application"), ("network", "Load Balancer-Network")],
    )
    def test_r25_lb_filters_and_dimension_rule(self, lb_type, family):
        spec = build_query("us-east-1", "aws_lb", lb_type)
        assert spec.service_code == "AWSELB"
        assert spec.filters == (("productFamily", family), ("location", LOC_USE1))
        assert spec.rule == DimensionRule(unit="Hrs", usagetype_suffix="LoadBalancerUsage")

    @pytest.mark.parametrize(
        ("service_key", "price_key"),
        [
            ("aws_ebs_volume", "sc1"),                       # not in the R4 set
            ("aws_db_instance.instance", "aurora-postgresql:db.r5.large"),  # unmapped engine
            ("aws_db_instance.instance", "db.t3.micro"),     # no colon (A14)
            ("aws_db_instance.instance", "postgres:"),       # empty class
            ("aws_db_instance.storage", "io2"),              # not in storage map
            ("aws_nat_gateway", "monthly"),
            ("aws_lb", "gateway"),
            ("aws_lambda_function", "anything"),             # unsupported type
        ],
    )
    def test_r25_unmappable_values_raise_unmapped_value(self, service_key, price_key):
        with pytest.raises(UnmappableKeyError) as excinfo:
            build_query("us-east-1", service_key, price_key)
        assert excinfo.value.reason is LiveFailureReason.UNMAPPED_VALUE

    def test_r25_maps_cover_exactly_the_documented_values(self):
        assert set(RDS_ENGINE_MAP) == {"postgres", "mysql", "mariadb"}
        assert set(RDS_STORAGE_MAP) == {"gp2", "gp3", "io1", "standard"}


class TestExtractionValidR31:
    def test_r31_ec2_fixture_rate_and_publication_date(self):
        live = extract_rate(load_pages("ec2_t3micro_useast1.json"), HRS)
        assert live.rate == Decimal("0.0110000000")
        assert live.publication_dates == ("2026-08-25T02:11:00Z",)

    def test_r31_reserved_terms_ignored(self):
        """The fixture carries a Reserved term with a 9999 rate; only the
        OnDemand dimension may be read."""
        live = extract_rate(load_pages("ec2_t3micro_useast1.json"), HRS)
        assert live.rate < Decimal("1")

    def test_r31_multiple_products_with_same_rate_not_ambiguous(self):
        live = extract_rate(load_pages("ec2_multiproduct_same_rate.json"), HRS)
        assert live.rate == Decimal("0.0990000000")
        # publication dates span both products
        assert live.publication_dates == (
            "2026-08-20T00:00:00Z",
            "2026-08-27T00:00:00Z",
        )

    def test_r31_ebs_gb_mo_unit(self):
        live = extract_rate(load_pages("ebs_gp3_useast1.json"),
                            DimensionRule(unit="GB-Mo"))
        assert live.rate == Decimal("0.0850000000")

    def test_r31_rds_fixture(self):
        live = extract_rate(load_pages("rds_postgres_dbt3micro_useast1.json"), HRS)
        assert live.rate == Decimal("0.0190000000")

    def test_ac17_nat_hourly_dimension_selected_over_gb(self):
        """AC17: NAT response holds Hrs and GB dimensions; the usagetype-suffix
        + unit rule must pick the hourly one (usagetype at product level, A-c2)."""
        rule = DimensionRule(unit="Hrs", usagetype_suffix="NatGateway-Hours")
        live = extract_rate(load_pages("nat_useast1_two_dimensions.json"), rule)
        assert live.rate == Decimal("0.0460000000")

    def test_ac17_nlb_lcu_dimension_excluded(self):
        """AC17: NLB response holds LoadBalancerUsage and LCU dimensions; only
        the hourly one survives (usagetype at dimension level, A-c2)."""
        rule = DimensionRule(unit="Hrs", usagetype_suffix="LoadBalancerUsage")
        live = extract_rate(load_pages("nlb_useast1_with_lcu.json"), rule)
        assert live.rate == Decimal("0.0230000000")

    @pytest.mark.parametrize(
        "bad_date",
        [
            "9999-12-31T" + "A" * 100_000,      # unbounded hostile string
            "2026-08-20T00:00:00Z|`<script>`",  # markup smuggled via the date
            "not a date",
            "2026-08-20",                       # missing time component
            "",
        ],
    )
    def test_r31_invalid_publication_date_dropped_rate_still_accepted(self, bad_date):
        """publicationDate is the only response string besides the rate that
        reaches any output (R31): a non-ISO-8601 value is dropped, bounding
        what a hostile response can push into the report; the key still
        prices (the date is informational, never worth failing the key)."""
        entry = price_entry([{"unit": "Hrs", "usd": "0.05"}], publication=bad_date)
        live = extract_rate(pages_of(entry), HRS)
        assert live.rate == Decimal("0.05")
        assert live.publication_dates == ()

    def test_r31_valid_iso_publication_dates_kept(self):
        for good in ("2026-08-20T00:00:00Z", "2026-08-20T00:00:00.000Z",
                     "2026-08-20T23:59:59"):
            entry = price_entry([{"unit": "Hrs", "usd": "0.05"}], publication=good)
            live = extract_rate(pages_of(entry), HRS)
            assert live.publication_dates == (good,)

    def test_a_c4_unit_mismatch_yields_no_match(self):
        """A-c4: a spurious OnDemand dimension with the wrong unit must not be
        priced as if it were hourly."""
        entry = price_entry([{"unit": "GB-Mo", "usd": "0.05"}])
        with pytest.raises(ExtractionError) as excinfo:
            extract_rate(pages_of(entry), HRS)
        assert excinfo.value.reason is LiveFailureReason.NO_MATCH


class TestExtractionFailuresR31:
    def assert_reason(self, pages, rule, reason):
        with pytest.raises(ExtractionError) as excinfo:
            extract_rate(pages, rule)
        assert excinfo.value.reason is reason

    def test_ac17_two_distinct_rates_ambiguous(self):
        self.assert_reason(
            load_pages("ec2_ambiguous_two_rates.json"), HRS, LiveFailureReason.AMBIGUOUS
        )

    def test_r31_empty_price_list_no_match(self):
        self.assert_reason([{"PriceList": []}], HRS, LiveFailureReason.NO_MATCH)

    def test_r31_page_without_price_list_no_match(self):
        self.assert_reason([{}], HRS, LiveFailureReason.NO_MATCH)

    @pytest.mark.parametrize("usd", ["NaN", "-0.01", "Infinity", "-Infinity",
                                     "1000000", "1e6", "not-a-number", ""])
    def test_ac20_hostile_usd_values_parse_error(self, usd):
        entry = price_entry([{"unit": "Hrs", "usd": usd}])
        self.assert_reason(pages_of(entry), HRS, LiveFailureReason.PARSE_ERROR)

    def test_r31_boundary_usd_values_accepted(self):
        entry = price_entry([{"unit": "Hrs", "usd": "999999.999999"}])
        assert extract_rate(pages_of(entry), HRS).rate == Decimal("999999.999999")
        zero = price_entry([{"unit": "Hrs", "usd": "0"}])
        assert extract_rate(pages_of(zero), HRS).rate == Decimal("0")

    def test_ac20_oversize_entry_fails_key(self):
        big = json.dumps(
            {"product": {"attributes": {"pad": "x" * (MAX_PRICE_LIST_ENTRY_BYTES + 1024)}},
             "terms": {"OnDemand": {}}}
        )
        self.assert_reason(pages_of(big), HRS, LiveFailureReason.OVERSIZE_RESPONSE)

    def test_ac20_malformed_json_entry_parse_error(self):
        self.assert_reason(pages_of("{this is not json"), HRS,
                           LiveFailureReason.PARSE_ERROR)

    @pytest.mark.parametrize("raw", [42, None, ["nested"], json.dumps([1, 2])])
    def test_r31_non_object_entries_parse_error(self, raw):
        with pytest.raises(ExtractionError) as excinfo:
            extract_rate([{"PriceList": [raw]}], HRS)
        assert excinfo.value.reason is LiveFailureReason.PARSE_ERROR

    def test_r31_price_list_not_a_list_parse_error(self):
        self.assert_reason([{"PriceList": "surprise"}], HRS,
                           LiveFailureReason.PARSE_ERROR)

    def test_a_c5_one_bad_entry_fails_the_whole_key(self):
        """A-c5: fail-closed — a valid sibling must not be cherry-picked from a
        partially hostile response."""
        good = price_entry([{"unit": "Hrs", "usd": "0.05"}])
        self.assert_reason(pages_of(good, "{broken"), HRS,
                           LiveFailureReason.PARSE_ERROR)

    def test_r31_product_count_cap_fails_closed(self):
        entries = [price_entry([{"unit": "Hrs", "usd": "0.05"}])] * (
            MAX_PRODUCTS_PER_KEY + 1
        )
        self.assert_reason(pages_of(*entries), HRS,
                           LiveFailureReason.OVERSIZE_RESPONSE)

    def test_r31_exactly_50_products_accepted(self):
        entries = [price_entry([{"unit": "Hrs", "usd": "0.05"}])] * MAX_PRODUCTS_PER_KEY
        assert extract_rate(pages_of(*entries), HRS).rate == Decimal("0.05")

    def test_r31_missing_usd_dimension_skipped(self):
        entry = price_entry([{"unit": "Hrs", "usd": "ignored", "no_usd": True}])
        self.assert_reason(pages_of(entry), HRS, LiveFailureReason.NO_MATCH)

    def test_r31_deeply_nested_entry_parse_error_not_crash(self):
        deep = '{"product": ' + "[" * 60000 + "]" * 60000 + "}"
        self.assert_reason(pages_of(deep), HRS, LiveFailureReason.PARSE_ERROR)

    def test_r31_wrong_suffix_usagetype_no_match(self):
        entry = price_entry(
            [{"unit": "Hrs", "usd": "0.05", "usagetype": "NatGateway-Bytes"}]
        )
        rule = DimensionRule(unit="Hrs", usagetype_suffix="NatGateway-Hours")
        self.assert_reason(pages_of(entry), rule, LiveFailureReason.NO_MATCH)


class TestFetchPagesR28:
    def spec(self) -> QuerySpec:
        return build_query("us-east-1", "aws_instance", "t3.micro")

    def test_single_page(self):
        client = FixturePricingClient()
        spec = self.spec()
        client.add(spec.service_code, spec.filters, [{"PriceList": ["{}"]}])
        pages = fetch_pages(client, spec)
        assert len(pages) == 1
        assert len(client.calls) == 1
        assert client.calls[0].next_token is None

    def test_two_pages_follow_next_token(self):
        client = FixturePricingClient()
        spec = self.spec()
        client.add(spec.service_code, spec.filters,
                   [{"PriceList": ["{}"]}, {"PriceList": ["{}"]}])
        pages = fetch_pages(client, spec)
        assert len(pages) == 2
        assert [c.next_token for c in client.calls] == [None, "1"]

    def test_exactly_three_pages_allowed(self):
        client = FixturePricingClient()
        spec = self.spec()
        client.add(spec.service_code, spec.filters,
                   [{"PriceList": []} for _ in range(MAX_PAGES_PER_KEY)])
        assert len(fetch_pages(client, spec)) == MAX_PAGES_PER_KEY

    def test_fourth_page_pagination_overflow(self):
        client = FixturePricingClient()
        spec = self.spec()
        client.add(spec.service_code, spec.filters,
                   [{"PriceList": []} for _ in range(MAX_PAGES_PER_KEY + 1)])
        with pytest.raises(ExtractionError) as excinfo:
            fetch_pages(client, spec)
        assert excinfo.value.reason is LiveFailureReason.PAGINATION_OVERFLOW
        assert len(client.calls) == MAX_PAGES_PER_KEY  # the cap bounds calls too

    def test_transport_error_propagates(self):
        client = FixturePricingClient()
        spec = self.spec()
        client.add(spec.service_code, spec.filters,
                   PricingApiError(LiveFailureReason.TIMEOUT))
        with pytest.raises(PricingApiError) as excinfo:
            fetch_pages(client, spec)
        assert excinfo.value.reason is LiveFailureReason.TIMEOUT

    @pytest.mark.parametrize("hostile_page", [["not-a-dict"], "string-page", 42, None])
    def test_non_dict_page_parse_error(self, hostile_page):
        class HostileClient:
            def get_products(self, service_code, filters, next_token):
                return hostile_page

        with pytest.raises(ExtractionError) as excinfo:
            fetch_pages(HostileClient(), self.spec())
        assert excinfo.value.reason is LiveFailureReason.PARSE_ERROR

    @pytest.mark.parametrize("token", ["", 7, ["t"], {}])
    def test_hostile_next_token_parse_error(self, token):
        class TokenClient:
            def get_products(self, service_code, filters, next_token):
                return {"PriceList": [], "NextToken": token}

        with pytest.raises(ExtractionError) as excinfo:
            fetch_pages(TokenClient(), self.spec())
        assert excinfo.value.reason is LiveFailureReason.PARSE_ERROR


class TestFixturesMatchTheMatrix:
    """The committed fixtures must be reachable through build_query's own
    filters — guarding against fixtures drifting from the R25 matrix."""

    @pytest.mark.parametrize(
        ("service_key", "price_key", "fixture", "expected"),
        [
            ("aws_instance", "t3.micro", "ec2_t3micro_useast1.json", "0.0110000000"),
            ("aws_ebs_volume", "gp3", "ebs_gp3_useast1.json", "0.0850000000"),
            ("aws_db_instance.instance", "postgres:db.t3.micro",
             "rds_postgres_dbt3micro_useast1.json", "0.0190000000"),
            ("aws_nat_gateway", "hourly", "nat_useast1_two_dimensions.json",
             "0.0460000000"),
            ("aws_lb", "network", "nlb_useast1_with_lcu.json", "0.0230000000"),
        ],
    )
    def test_fixture_resolves_through_matrix_filters(self, service_key, price_key,
                                                     fixture, expected):
        spec = build_query("us-east-1", service_key, price_key)
        client = FixturePricingClient()
        client.add(spec.service_code, spec.filters, load_pages(fixture))
        pages = fetch_pages(client, spec)
        live = extract_rate(pages, spec.rule)
        assert live.rate == Decimal(expected)
