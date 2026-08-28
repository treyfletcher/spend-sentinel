"""``Boto3PricingClient`` — the live Pricing API transport (v1.1, T13).

The ONLY new module importing boto3/botocore (R32), and it does so lazily in
the constructor so importing this module — let alone ``pricing.live`` — needs
no boto3. The client calls exactly one API, ``pricing:GetProducts`` (R33).

Endpoint (R26/A9): the Pricing API is served from us-east-1 (default) and
ap-south-1; the endpoint region is independent of the analyzed region, which
appears only in the ``location`` filter. ``SPEND_SENTINEL_PRICING_ENDPOINT_REGION``
overrides the endpoint; its value is validated against ``^[a-z0-9-]{1,32}$``
before being handed to boto3 and is not a secret. Credentials come from the
standard AWS chain only; nothing credential-related is logged, and botocore
exceptions are translated to :class:`PricingApiError` values carrying only
internal enum reasons — never response or exception text.
"""

from __future__ import annotations

import os
import re
from typing import Any

from spend_sentinel.pricing.live import (
    MAX_RESULTS_PER_PAGE,
    LiveFailureReason,
    PricingApiError,
)

_DEFAULT_ENDPOINT_REGION = "us-east-1"
_ENDPOINT_ENV_VAR = "SPEND_SENTINEL_PRICING_ENDPOINT_REGION"
_REGION_TOKEN = re.compile(r"^[a-z0-9-]{1,32}$")

#: Connect/read timeouts and retry cap for every Pricing API call (R28).
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 10
MAX_RETRIES = 2


class PricingClientUnavailable(Exception):
    """The live pricing client cannot be constructed (run-level failure, R27)."""

    def __init__(self, reason: LiveFailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class Boto3PricingClient:
    """Live :class:`~spend_sentinel.pricing.live.PricingApiClient` over boto3."""

    def __init__(self, endpoint_region: str | None = None) -> None:
        """Build the boto3 pricing client against the endpoint region.

        Raises:
            PricingClientUnavailable: reason ``boto3_missing`` when the
                ``[aws]`` extra is not installed; ``client_init_error`` for an
                invalid endpoint override or any client-construction failure
                (e.g. no resolvable credentials configuration).
        """
        region = endpoint_region or os.environ.get(_ENDPOINT_ENV_VAR) or _DEFAULT_ENDPOINT_REGION
        if not _REGION_TOKEN.match(region):
            raise PricingClientUnavailable(LiveFailureReason.CLIENT_INIT_ERROR)

        try:
            import boto3
            from botocore.config import Config
            from botocore.exceptions import (
                BotoCoreError,
                ClientError,
                ConnectTimeoutError,
                ReadTimeoutError,
            )
        except ImportError:
            raise PricingClientUnavailable(LiveFailureReason.BOTO3_MISSING) from None

        self._timeout_errors: tuple[type[Exception], ...] = (
            ConnectTimeoutError,
            ReadTimeoutError,
        )
        self._api_errors: tuple[type[Exception], ...] = (BotoCoreError, ClientError)
        try:
            self._client = boto3.client(
                "pricing",
                region_name=region,
                config=Config(
                    connect_timeout=CONNECT_TIMEOUT_SECONDS,
                    read_timeout=READ_TIMEOUT_SECONDS,
                    # standard mode counts the initial attempt: 3 = at most 2 retries.
                    retries={"max_attempts": MAX_RETRIES + 1, "mode": "standard"},
                ),
            )
        except Exception:  # no credential/config detail may leak (R31)
            raise PricingClientUnavailable(LiveFailureReason.CLIENT_INIT_ERROR) from None

    def get_products(
        self,
        service_code: str,
        filters: tuple[tuple[str, str], ...],
        next_token: str | None,
    ) -> dict[str, Any]:
        """One ``pricing:GetProducts`` page (the only API this client calls).

        Raises:
            PricingApiError: reason ``timeout`` for connect/read timeouts,
                ``api_error`` for every other botocore failure. The exception
                carries no botocore/response text.
        """
        kwargs: dict[str, Any] = {
            "ServiceCode": service_code,
            "Filters": [
                {"Type": "TERM_MATCH", "Field": field, "Value": value}
                for field, value in filters
            ],
            "MaxResults": MAX_RESULTS_PER_PAGE,
        }
        if next_token is not None:
            kwargs["NextToken"] = next_token
        try:
            response = self._client.get_products(**kwargs)
        except self._timeout_errors:
            raise PricingApiError(LiveFailureReason.TIMEOUT) from None
        except self._api_errors:
            raise PricingApiError(LiveFailureReason.API_ERROR) from None
        if not isinstance(response, dict):
            raise PricingApiError(LiveFailureReason.API_ERROR)
        return response
