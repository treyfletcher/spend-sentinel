"""v1.1 chunk 1 — Boto3PricingClient over a stubbed boto3 (no network, no real
boto3), endpoint-region handling (A9/R26), error translation, and the
default-path purity guarantee (R32: snapshot-only runs never import
pricing.live or boto3).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types

import pytest

from spend_sentinel.pricing.live import LiveFailureReason, PricingApiError

from .conftest import fixture_path

BOTO3_INSTALLED = importlib.util.find_spec("boto3") is not None

ENV_VAR = "SPEND_SENTINEL_PRICING_ENDPOINT_REGION"


class StubBotoCoreError(Exception):
    pass


class StubClientError(Exception):
    def __init__(self, code: str = "AccessDenied") -> None:
        super().__init__(f"stub client error {code}")
        self.response = {"Error": {"Code": code}}


class StubConnectTimeout(Exception):
    pass


class StubReadTimeout(Exception):
    pass


class RecordingPricingStub:
    """Stub boto3 'pricing' client: canned responses/errors, records calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.response: object = {"PriceList": []}

    def get_products(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def __getattr__(self, name):
        raise AssertionError(f"unexpected pricing API method accessed: {name}")


@pytest.fixture()
def stub_boto3(monkeypatch):
    """Install stub boto3/botocore modules; returns a dict capturing the
    boto3.client() kwargs and the stub pricing client."""
    captured: dict = {"client_kwargs": None, "config_kwargs": None}
    pricing_stub = RecordingPricingStub()

    class StubConfig:
        def __init__(self, **kwargs) -> None:
            captured["config_kwargs"] = kwargs

    def client(service_name, **kwargs):
        assert service_name == "pricing"
        captured["client_kwargs"] = kwargs
        return pricing_stub

    boto3_mod = types.ModuleType("boto3")
    boto3_mod.client = client
    botocore_mod = types.ModuleType("botocore")
    config_mod = types.ModuleType("botocore.config")
    config_mod.Config = StubConfig
    exceptions_mod = types.ModuleType("botocore.exceptions")
    exceptions_mod.BotoCoreError = StubBotoCoreError
    exceptions_mod.ClientError = StubClientError
    exceptions_mod.ConnectTimeoutError = StubConnectTimeout
    exceptions_mod.ReadTimeoutError = StubReadTimeout
    botocore_mod.config = config_mod
    botocore_mod.exceptions = exceptions_mod

    monkeypatch.setitem(sys.modules, "boto3", boto3_mod)
    monkeypatch.setitem(sys.modules, "botocore", botocore_mod)
    monkeypatch.setitem(sys.modules, "botocore.config", config_mod)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions_mod)

    captured["pricing_stub"] = pricing_stub
    return captured


def make_client(**kwargs):
    from spend_sentinel.adapters.boto3_pricing import Boto3PricingClient

    return Boto3PricingClient(**kwargs)


class TestConstructionAndEndpoint:
    @pytest.mark.skipif(BOTO3_INSTALLED, reason="boto3 is installed in this environment")
    def test_r32_boto3_missing_raises_unavailable(self, monkeypatch):
        from spend_sentinel.adapters.boto3_pricing import PricingClientUnavailable

        monkeypatch.delenv(ENV_VAR, raising=False)
        with pytest.raises(PricingClientUnavailable) as excinfo:
            make_client()
        assert excinfo.value.reason is LiveFailureReason.BOTO3_MISSING

    @pytest.mark.parametrize(
        "bad_region",
        [
            "US-EAST-1",                      # uppercase
            "us_east_1",                      # underscore
            "region; rm -rf /",               # injection attempt
            "a" * 33,                         # too long
            "\neu-west-1",                    # leading control char
        ],
    )
    def test_r31_invalid_endpoint_env_rejected_before_boto3(self, monkeypatch,
                                                            bad_region):
        """Validation precedes the boto3 import: rejection works even without
        boto3 installed, and the hostile value never reaches a client."""
        from spend_sentinel.adapters.boto3_pricing import PricingClientUnavailable

        monkeypatch.setenv(ENV_VAR, bad_region)
        with pytest.raises(PricingClientUnavailable) as excinfo:
            make_client()
        assert excinfo.value.reason is LiveFailureReason.CLIENT_INIT_ERROR

    @pytest.mark.xfail(
        strict=True,
        reason="BUG-5 (see docs/test-reports/feature-live-pricing-c1.md): the "
        "endpoint-region regex is applied with re.match and a '$' anchor, which "
        "matches before a trailing newline — 'eu-west-1\\n' passes validation and "
        "is handed to boto3, violating R31's validate-before-boto3 contract "
        "(fix: re.fullmatch or a \\Z anchor)",
    )
    def test_r31_trailing_newline_endpoint_rejected(self, stub_boto3, monkeypatch):
        """Deterministic proof via the stub: the newline value must be rejected,
        never handed to boto3.client as region_name."""
        from spend_sentinel.adapters.boto3_pricing import PricingClientUnavailable

        monkeypatch.setenv(ENV_VAR, "eu-west-1\n")
        with pytest.raises(PricingClientUnavailable) as excinfo:
            make_client()
        assert excinfo.value.reason is LiveFailureReason.CLIENT_INIT_ERROR
        assert stub_boto3["client_kwargs"] is None  # boto3 never saw the value

    def test_a9_default_endpoint_region_us_east_1(self, stub_boto3, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        make_client()
        assert stub_boto3["client_kwargs"]["region_name"] == "us-east-1"

    def test_a9_env_override_honored(self, stub_boto3, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "ap-south-1")
        make_client()
        assert stub_boto3["client_kwargs"]["region_name"] == "ap-south-1"

    def test_a9_empty_env_falls_back_to_default(self, stub_boto3, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "")
        make_client()
        assert stub_boto3["client_kwargs"]["region_name"] == "us-east-1"

    def test_explicit_argument_wins_over_env(self, stub_boto3, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "ap-south-1")
        make_client(endpoint_region="us-east-1")
        assert stub_boto3["client_kwargs"]["region_name"] == "us-east-1"

    def test_r28_botocore_config_timeouts_and_retries(self, stub_boto3, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        make_client()
        config = stub_boto3["config_kwargs"]
        assert config["connect_timeout"] == 5
        assert config["read_timeout"] == 10
        # "at most 2 retries" -> standard mode max_attempts=3 (A-c3)
        assert config["retries"] == {"max_attempts": 3, "mode": "standard"}

    def test_client_construction_failure_is_client_init_error(self, stub_boto3,
                                                              monkeypatch):
        from spend_sentinel.adapters.boto3_pricing import PricingClientUnavailable

        monkeypatch.delenv(ENV_VAR, raising=False)

        def exploding_client(service_name, **kwargs):
            raise RuntimeError("no credentials configured; secret=hunter2")

        sys.modules["boto3"].client = exploding_client
        with pytest.raises(PricingClientUnavailable) as excinfo:
            make_client()
        assert excinfo.value.reason is LiveFailureReason.CLIENT_INIT_ERROR
        assert "hunter2" not in str(excinfo.value)  # no detail leaks (R31)


class TestGetProducts:
    def client_and_stub(self, stub_boto3, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        client = make_client()
        return client, stub_boto3["pricing_stub"]

    def test_request_shape_term_match_filters_and_max_results(self, stub_boto3,
                                                              monkeypatch):
        client, stub = self.client_and_stub(stub_boto3, monkeypatch)
        client.get_products(
            "AmazonEC2", (("instanceType", "t3.micro"), ("location", "US East (N. Virginia)")),
            None,
        )
        (call,) = stub.calls
        assert call["ServiceCode"] == "AmazonEC2"
        assert call["MaxResults"] == 100
        assert call["Filters"] == [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": "t3.micro"},
            {"Type": "TERM_MATCH", "Field": "location",
             "Value": "US East (N. Virginia)"},
        ]
        assert "NextToken" not in call

    def test_next_token_passed_through(self, stub_boto3, monkeypatch):
        client, stub = self.client_and_stub(stub_boto3, monkeypatch)
        client.get_products("AmazonEC2", (), "tok-123")
        assert stub.calls[0]["NextToken"] == "tok-123"

    def test_response_returned_verbatim(self, stub_boto3, monkeypatch):
        client, stub = self.client_and_stub(stub_boto3, monkeypatch)
        stub.response = {"PriceList": ["{}"], "NextToken": "n"}
        assert client.get_products("AmazonEC2", (), None) == stub.response

    @pytest.mark.parametrize(
        ("exc_factory", "reason"),
        [
            (StubConnectTimeout, LiveFailureReason.TIMEOUT),
            (StubReadTimeout, LiveFailureReason.TIMEOUT),
            (StubBotoCoreError, LiveFailureReason.API_ERROR),
            (StubClientError, LiveFailureReason.API_ERROR),
        ],
    )
    def test_error_translation(self, stub_boto3, monkeypatch, exc_factory, reason):
        client, stub = self.client_and_stub(stub_boto3, monkeypatch)
        stub.response = exc_factory()
        with pytest.raises(PricingApiError) as excinfo:
            client.get_products("AmazonEC2", (), None)
        assert excinfo.value.reason is reason
        # only the internal enum value, never botocore/response text (R31)
        assert str(excinfo.value) == reason.value

    def test_non_dict_response_is_api_error(self, stub_boto3, monkeypatch):
        client, stub = self.client_and_stub(stub_boto3, monkeypatch)
        stub.response = ["not", "a", "dict"]
        with pytest.raises(PricingApiError) as excinfo:
            client.get_products("AmazonEC2", (), None)
        assert excinfo.value.reason is LiveFailureReason.API_ERROR

    def test_r33_only_get_products_is_called(self, stub_boto3, monkeypatch):
        """The stub raises on any other method; drive a call and confirm the
        surface (mirrors the v1 AwsReader surface test)."""
        client, stub = self.client_and_stub(stub_boto3, monkeypatch)
        client.get_products("AWSELB", (("productFamily", "Load Balancer-Network"),),
                            None)
        assert len(stub.calls) == 1


class TestDefaultPathPurity:
    """R32/R22: without --live-pricing nothing live-related is imported."""

    def test_r32_importing_live_module_does_not_import_boto3(self):
        code = (
            "import sys\n"
            "import spend_sentinel.pricing.live\n"
            "assert 'boto3' not in sys.modules\n"
            "assert 'botocore' not in sys.modules\n"
            "print('ok')\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ok"

    def test_r22_snapshot_only_analyze_leaves_live_modules_unimported(self):
        """A default (no --live-pricing) analyze run must not touch
        pricing.live, the fixture client, the pricing adapter, or boto3."""
        plan = fixture_path("create_small.json")
        code = (
            "import json, sys\n"
            "from click.testing import CliRunner\n"
            "from spend_sentinel.cli import main\n"
            f"result = CliRunner().invoke(main, ['analyze', '--plan', {plan!r},"
            " '--skip-drift'])\n"
            "assert result.exit_code == 0, result.output\n"
            "leaked = [m for m in sys.modules if m in (\n"
            "    'spend_sentinel.pricing.live',\n"
            "    'spend_sentinel.pricing.fixture_client',\n"
            "    'spend_sentinel.adapters.boto3_pricing',\n"
            ") or m == 'boto3' or m.startswith('boto3.')\n"
            "  or m == 'botocore' or m.startswith('botocore.')]\n"
            "print(json.dumps(leaked))\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == []

    def test_r32_importing_the_adapter_module_does_not_import_boto3(self):
        """boto3 is imported lazily in the constructor, so merely importing the
        adapter must stay boto3-free."""
        code = (
            "import sys\n"
            "import spend_sentinel.adapters.boto3_pricing\n"
            "assert 'boto3' not in sys.modules\n"
            "print('ok')\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ok"
