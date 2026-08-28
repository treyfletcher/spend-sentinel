"""Live Pricing API query layer (v1.1 chunk 1: T11 + T12 primitives). Pure — no boto3.

This module holds everything about the AWS Pricing API that can be known
without a network: the transport protocol (:class:`PricingApiClient`), the
region-code → location-name table (R26), the per-dimension filter matrix
(R25), the defensive response extractor (R31), and the in-run cache / 30s
budget primitives (R28). Chunk 2's ``LivePricingSource`` composes these via
:func:`cached_resolve`; chunk 3 wires the boto3 transport from
``adapters/boto3_pricing.py``.

Security posture (R31): ``GetProducts`` responses are untrusted. Every
price-list entry is size-capped (256 KiB), at most 50 products are examined
per key and 3 pages followed, extraction goes through pydantic models that
validate only the navigated path, and the USD value must be a finite,
non-negative Decimal below 1,000,000. Failures are per-key outcomes — never
exceptions escaping to the caller, never response text in any diagnostic:
the only response-derived data that leaves this module is the extracted
rate and ``publicationDate`` strings.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spend_sentinel.pricing.source import PricingSource

if TYPE_CHECKING:
    from spend_sentinel.core.models import LivePricingReport

# --- Bounds (R28, R31) ------------------------------------------------------

MAX_PRICE_LIST_ENTRY_BYTES = 256 * 1024
MAX_PRODUCTS_PER_KEY = 50
MAX_PAGES_PER_KEY = 3
MAX_RESULTS_PER_PAGE = 100
BUDGET_SECONDS = 30.0
_MAX_USD = Decimal("1000000")

# --- Failure taxonomy (R27) -------------------------------------------------


class LiveFailureReason(StrEnum):
    """Why a key (or the run) fell back to the snapshot (R27)."""

    BOTO3_MISSING = "boto3_missing"
    CLIENT_INIT_ERROR = "client_init_error"
    API_ERROR = "api_error"
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    UNSUPPORTED_REGION = "unsupported_region"
    UNMAPPED_VALUE = "unmapped_value"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    PARSE_ERROR = "parse_error"
    PAGINATION_OVERFLOW = "pagination_overflow"
    OVERSIZE_RESPONSE = "oversize_response"

#: Failure reasons that disable the API for the remainder of the run (R27).
RUN_LEVEL_REASONS = frozenset(
    {
        LiveFailureReason.BOTO3_MISSING,
        LiveFailureReason.CLIENT_INIT_ERROR,
        LiveFailureReason.UNSUPPORTED_REGION,
    }
)


class PricingApiError(Exception):
    """A transport-level failure, raised by :class:`PricingApiClient` impls.

    ``reason`` is :attr:`LiveFailureReason.TIMEOUT` or
    :attr:`LiveFailureReason.API_ERROR`; the message carries no response or
    credential content (adapters translate botocore exceptions to this type
    with an internal-enum message only).
    """

    def __init__(self, reason: LiveFailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


# --- Transport protocol (Modularity notes) ----------------------------------


class PricingApiClient(Protocol):
    """Transport-only view of ``pricing:GetProducts`` (the ONLY API used, R33).

    Returns the raw response dict (``PriceList`` of JSON strings, optional
    ``NextToken``). Implementations raise :class:`PricingApiError` on
    transport failure; pagination is driven by the caller via ``next_token``
    (the protocol is single-call, matching the Modularity notes' signature).
    """

    def get_products(
        self,
        service_code: str,
        filters: tuple[tuple[str, str], ...],
        next_token: str | None,
    ) -> dict[str, Any]:
        """One GetProducts page for TERM_MATCH ``filters`` ((Field, Value) pairs)."""
        ...


# --- Region and attribute-value maps (R25, R26) -----------------------------

#: Region code -> Pricing API ``location`` name. Static, extensible; a region
#: absent here disables live lookup for the run (reason ``unsupported_region``).
REGION_LOCATIONS: dict[str, str] = {
    "us-east-1": "US East (N. Virginia)",
    "us-west-2": "US West (Oregon)",
    "eu-west-1": "EU (Ireland)",
}

#: RDS engine (plan attribute) -> Pricing API ``databaseEngine`` value.
RDS_ENGINE_MAP: dict[str, str] = {
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "mariadb": "MariaDB",
}

#: RDS storage type -> Pricing API ``volumeType`` value.
RDS_STORAGE_MAP: dict[str, str] = {
    "gp2": "General Purpose",
    "gp3": "General Purpose-GP3",
    "io1": "Provisioned IOPS",
    "standard": "Magnetic",
}

_EBS_VOLUME_TYPES = frozenset({"gp2", "gp3", "io1", "io2", "st1", "standard"})
_LB_TYPES = {"application": "Load Balancer-Application", "network": "Load Balancer-Network"}


class UnmappableKeyError(Exception):
    """The (region, service_key, price_key) triple cannot become API filters."""

    def __init__(self, reason: LiveFailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class DimensionRule:
    """How to select the single OnDemand price dimension (R25 last column)."""

    unit: str
    usagetype_suffix: str | None = None


@dataclass(frozen=True)
class QuerySpec:
    """One GetProducts query: service code, TERM_MATCH filters, selection rule."""

    service_code: str
    filters: tuple[tuple[str, str], ...]
    rule: DimensionRule


def build_query(region: str, service_key: str, price_key: str) -> QuerySpec:
    """The R25 filter matrix, verbatim.

    Raises:
        UnmappableKeyError: reason ``unsupported_region`` when the region is
            not in :data:`REGION_LOCATIONS`; ``unmapped_value`` when the
            price key (or a component of it) has no API mapping.
    """
    location = REGION_LOCATIONS.get(region)
    if location is None:
        raise UnmappableKeyError(LiveFailureReason.UNSUPPORTED_REGION)

    if service_key == "aws_instance":
        return QuerySpec(
            service_code="AmazonEC2",
            filters=(
                ("instanceType", price_key),
                ("location", location),
                ("operatingSystem", "Linux"),
                ("tenancy", "Shared"),
                ("preInstalledSw", "NA"),
                ("capacitystatus", "Used"),
            ),
            rule=DimensionRule(unit="Hrs"),
        )
    if service_key == "aws_ebs_volume":
        if price_key not in _EBS_VOLUME_TYPES:
            raise UnmappableKeyError(LiveFailureReason.UNMAPPED_VALUE)
        return QuerySpec(
            service_code="AmazonEC2",
            filters=(
                ("volumeApiName", price_key),
                ("location", location),
                ("productFamily", "Storage"),
            ),
            rule=DimensionRule(unit="GB-Mo"),
        )
    if service_key == "aws_db_instance.instance":
        engine, sep, instance_class = price_key.partition(":")
        mapped_engine = RDS_ENGINE_MAP.get(engine)
        if not sep or not instance_class or mapped_engine is None:
            raise UnmappableKeyError(LiveFailureReason.UNMAPPED_VALUE)
        return QuerySpec(
            service_code="AmazonRDS",
            filters=(
                ("instanceType", instance_class),
                ("databaseEngine", mapped_engine),
                # Always Single-AZ: core/cost.py doubles for multi_az itself;
                # a Multi-AZ rate here would double-count (R25 correctness rule).
                ("deploymentOption", "Single-AZ"),
                ("location", location),
            ),
            rule=DimensionRule(unit="Hrs"),
        )
    if service_key == "aws_db_instance.storage":
        mapped_type = RDS_STORAGE_MAP.get(price_key)
        if mapped_type is None:
            raise UnmappableKeyError(LiveFailureReason.UNMAPPED_VALUE)
        return QuerySpec(
            service_code="AmazonRDS",
            filters=(
                ("volumeType", mapped_type),
                ("deploymentOption", "Single-AZ"),
                ("productFamily", "Database Storage"),
                ("location", location),
            ),
            rule=DimensionRule(unit="GB-Mo"),
        )
    if service_key == "aws_nat_gateway":
        if price_key != "hourly":
            raise UnmappableKeyError(LiveFailureReason.UNMAPPED_VALUE)
        return QuerySpec(
            service_code="AmazonEC2",
            filters=(("productFamily", "NAT Gateway"), ("location", location)),
            rule=DimensionRule(unit="Hrs", usagetype_suffix="NatGateway-Hours"),
        )
    if service_key == "aws_lb":
        family = _LB_TYPES.get(price_key)
        if family is None:
            raise UnmappableKeyError(LiveFailureReason.UNMAPPED_VALUE)
        return QuerySpec(
            service_code="AWSELB",
            filters=(("productFamily", family), ("location", location)),
            rule=DimensionRule(unit="Hrs", usagetype_suffix="LoadBalancerUsage"),
        )
    raise UnmappableKeyError(LiveFailureReason.UNMAPPED_VALUE)


# --- Defensive extraction (R31) ---------------------------------------------


class _PriceDimension(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pricePerUnit: dict[str, str] = Field(default_factory=dict)
    unit: str = ""
    usagetype: str | None = None


class _OnDemandTerm(BaseModel):
    model_config = ConfigDict(extra="ignore")

    priceDimensions: dict[str, _PriceDimension] = Field(default_factory=dict)


class _Terms(BaseModel):
    model_config = ConfigDict(extra="ignore")

    OnDemand: dict[str, _OnDemandTerm] = Field(default_factory=dict)


class _Product(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attributes: dict[str, str] = Field(default_factory=dict)


class _PriceListEntry(BaseModel):
    """Only the navigated path is validated; everything else is ignored (R31)."""

    model_config = ConfigDict(extra="ignore")

    product: _Product = Field(default_factory=_Product)
    terms: _Terms = Field(default_factory=_Terms)
    publicationDate: str = ""


class ExtractionError(Exception):
    """The key's response could not yield exactly one usable USD rate."""

    def __init__(self, reason: LiveFailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class LiveRate:
    """A successfully extracted live rate plus its price-list publication dates."""

    rate: Decimal
    publication_dates: tuple[str, ...]


def extract_rate(pages: list[dict[str, Any]], rule: DimensionRule) -> LiveRate:
    """Extract the single OnDemand USD rate from raw GetProducts pages (R25/R31).

    Selection: every OnDemand price dimension whose ``unit`` equals the
    rule's, and — when the rule carries a ``usagetype_suffix`` — whose
    usagetype (dimension-level, else the product attribute) ends with it.
    Zero surviving rates -> ``no_match``; more than one distinct USD value ->
    ``ambiguous``. Fail-closed per key: any malformed/oversized entry fails
    the whole key rather than cherry-picking around it.

    Raises:
        ExtractionError: with the R27 reason.
    """
    rates: set[Decimal] = set()
    dates: set[str] = set()
    products_seen = 0

    for page in pages:
        price_list = page.get("PriceList")
        if price_list is None:
            continue
        if not isinstance(price_list, list):
            raise ExtractionError(LiveFailureReason.PARSE_ERROR)
        for raw_entry in price_list:
            products_seen += 1
            if products_seen > MAX_PRODUCTS_PER_KEY:
                raise ExtractionError(LiveFailureReason.OVERSIZE_RESPONSE)
            entry = _parse_entry(raw_entry)
            if entry.publicationDate:
                dates.add(entry.publicationDate)
            product_usagetype = entry.product.attributes.get("usagetype")
            for term in entry.terms.OnDemand.values():
                for dim in term.priceDimensions.values():
                    if dim.unit != rule.unit:
                        continue
                    if rule.usagetype_suffix is not None:
                        usagetype = dim.usagetype or product_usagetype or ""
                        if not usagetype.endswith(rule.usagetype_suffix):
                            continue
                    usd = dim.pricePerUnit.get("USD")
                    if usd is None:
                        continue
                    rates.add(_validate_usd(usd))

    if not rates:
        raise ExtractionError(LiveFailureReason.NO_MATCH)
    if len(rates) > 1:
        raise ExtractionError(LiveFailureReason.AMBIGUOUS)
    return LiveRate(rate=next(iter(rates)), publication_dates=tuple(sorted(dates)))


def _parse_entry(raw_entry: Any) -> _PriceListEntry:
    """One PriceList item: JSON string (the API shape) or pre-parsed dict."""
    if isinstance(raw_entry, str):
        if len(raw_entry.encode("utf-8", errors="replace")) > MAX_PRICE_LIST_ENTRY_BYTES:
            raise ExtractionError(LiveFailureReason.OVERSIZE_RESPONSE)
        try:
            data = json.loads(raw_entry)
        except (ValueError, RecursionError):
            raise ExtractionError(LiveFailureReason.PARSE_ERROR) from None
    elif isinstance(raw_entry, dict):
        data = raw_entry
    else:
        raise ExtractionError(LiveFailureReason.PARSE_ERROR)
    if not isinstance(data, dict):
        raise ExtractionError(LiveFailureReason.PARSE_ERROR)
    try:
        return _PriceListEntry.model_validate(data)
    except (ValidationError, RecursionError):
        raise ExtractionError(LiveFailureReason.PARSE_ERROR) from None


def _validate_usd(usd: str) -> Decimal:
    """Finite, non-negative Decimal strictly below 10^6, from the USD string."""
    try:
        value = Decimal(usd)
    except InvalidOperation:
        raise ExtractionError(LiveFailureReason.PARSE_ERROR) from None
    if not value.is_finite() or value < 0 or value >= _MAX_USD:
        raise ExtractionError(LiveFailureReason.PARSE_ERROR)
    return value


# --- Pagination (R28; the protocol is single-call, the pure layer drives it) --


def fetch_pages(client: PricingApiClient, spec: QuerySpec) -> list[dict[str, Any]]:
    """Follow ``NextToken`` up to :data:`MAX_PAGES_PER_KEY` pages.

    Raises:
        PricingApiError: propagated from the client (``api_error``/``timeout``).
        ExtractionError: reason ``pagination_overflow`` past the page cap, or
            ``parse_error`` for a non-dict page.
    """
    pages: list[dict[str, Any]] = []
    next_token: str | None = None
    for _ in range(MAX_PAGES_PER_KEY):
        page = client.get_products(spec.service_code, spec.filters, next_token)
        if not isinstance(page, dict):
            raise ExtractionError(LiveFailureReason.PARSE_ERROR)
        pages.append(page)
        raw_token = page.get("NextToken")
        if raw_token is None:
            return pages
        if not isinstance(raw_token, str) or not raw_token:
            raise ExtractionError(LiveFailureReason.PARSE_ERROR)
        next_token = raw_token
    raise ExtractionError(LiveFailureReason.PAGINATION_OVERFLOW)


# --- In-run cache and time budget (R28) --------------------------------------


@dataclass(frozen=True)
class LookupOutcome:
    """The memoized result of one key's live attempt: a rate or a reason."""

    rate: Decimal | None = None
    publication_dates: tuple[str, ...] = ()
    failure: LiveFailureReason | None = None

    @property
    def ok(self) -> bool:
        return self.rate is not None


class Budget:
    """Monotonic run-level time budget for all Pricing API calls (R28)."""

    def __init__(
        self,
        seconds: float = BUDGET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._seconds = seconds
        self._clock = clock
        self._start = clock()

    @property
    def exhausted(self) -> bool:
        return self._clock() - self._start >= self._seconds


@dataclass
class RunCache:
    """One outcome per unique (region, service_key, price_key) triple (R28)."""

    _entries: dict[tuple[str, str, str], LookupOutcome] = field(default_factory=dict)

    def get(self, region: str, service_key: str, price_key: str) -> LookupOutcome | None:
        return self._entries.get((region, service_key, price_key))

    def put(
        self, region: str, service_key: str, price_key: str, outcome: LookupOutcome
    ) -> None:
        self._entries[(region, service_key, price_key)] = outcome

    def __len__(self) -> int:
        return len(self._entries)


def resolve_live_rate(
    client: PricingApiClient, region: str, service_key: str, price_key: str
) -> LookupOutcome:
    """Uncached single-key resolution: build filters, fetch, extract.

    Never raises: every failure becomes a :class:`LookupOutcome` with an R27
    reason (transport errors included).
    """
    try:
        spec = build_query(region, service_key, price_key)
        pages = fetch_pages(client, spec)
        live = extract_rate(pages, spec.rule)
    except UnmappableKeyError as exc:
        return LookupOutcome(failure=exc.reason)
    except ExtractionError as exc:
        return LookupOutcome(failure=exc.reason)
    except PricingApiError as exc:
        return LookupOutcome(failure=exc.reason)
    except Exception:  # noqa: BLE001 - BUG-6 defensive catch-all
        # A transport that violates the protocol by raising an untyped
        # exception must degrade like any other failure, never crash the run
        # (R27; mirrors drift's A-i18 posture). No exception detail is kept —
        # it could carry response or credential text.
        return LookupOutcome(failure=LiveFailureReason.API_ERROR)
    return LookupOutcome(rate=live.rate, publication_dates=live.publication_dates)


def cached_resolve(
    client: PricingApiClient,
    cache: RunCache,
    budget: Budget,
    region: str,
    service_key: str,
    price_key: str,
) -> LookupOutcome:
    """The chunk-2 entry point: cache hit, else budget check, else one query.

    At most one GetProducts query (with pagination) per unique triple; both
    successes and failures are memoized, and once the budget is exhausted all
    remaining uncached keys resolve to ``budget_exhausted`` (R28).
    """
    cached = cache.get(region, service_key, price_key)
    if cached is not None:
        return cached
    if budget.exhausted:
        outcome = LookupOutcome(failure=LiveFailureReason.BUDGET_EXHAUSTED)
    else:
        outcome = resolve_live_rate(client, region, service_key, price_key)
    cache.put(region, service_key, price_key, outcome)
    return outcome


# --- LivePricingSource (T12: R24, R27, R28, R29) ------------------------------


class LivePricingSource:
    """A :class:`~spend_sentinel.pricing.source.PricingSource` with live rates.

    Wraps an injected :class:`PricingApiClient` and the v1
    ``SnapshotPricingSource`` fallback (R24). Per-key resolution: run
    disabled or key unmappable -> snapshot; else one cached API query; any
    failure -> snapshot; ``None`` only when both miss (flows into R7
    ``unknown_price_key`` unchanged). Degradation is total: ``get_rate``
    never raises, and run-level failures (``boto3_missing``,
    ``client_init_error``, ``unsupported_region``) switch the rest of the
    run to snapshot-only after being recorded once (R27).

    Construct with ``client=None`` plus ``disabled_reason`` when the
    transport could not be built (chunk-3 wiring), so reporting still works.
    """

    def __init__(
        self,
        client: PricingApiClient | None,
        snapshot: PricingSource,
        endpoint_region: str = "us-east-1",
        budget_seconds: float = BUDGET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        disabled_reason: LiveFailureReason | None = None,
    ) -> None:
        if client is None and disabled_reason is None:
            disabled_reason = LiveFailureReason.CLIENT_INIT_ERROR
        self._client = client
        self._snapshot = snapshot
        self._endpoint_region = endpoint_region
        self._cache = RunCache()
        self._budget = Budget(seconds=budget_seconds, clock=clock)
        self._disabled: LiveFailureReason | None = None
        self._warnings: dict[tuple[str, str], None] = {}  # ordered de-dup
        self._pending_lookups: list[tuple[str, str, str]] = []
        self._live_count = 0
        self._fallback_count = 0
        self._miss_count = 0
        self._publication_dates: set[str] = set()
        if disabled_reason is not None:
            self._disable(disabled_reason)

    # -- PricingSource protocol --

    def get_rate(self, region: str, service_key: str, price_key: str) -> Decimal | None:
        """Resolve one rate per R24; records attribution and never raises."""
        if self._disabled is None and region not in REGION_LOCATIONS:
            self._disable(LiveFailureReason.UNSUPPORTED_REGION)

        if self._disabled is None and self._client is not None:
            outcome = cached_resolve(
                self._client, self._cache, self._budget, region, service_key, price_key
            )
            if outcome.ok:
                self._live_count += 1
                self._publication_dates.update(outcome.publication_dates)
                self._pending_lookups.append((service_key, price_key, "live"))
                return outcome.rate
            failure = outcome.failure or LiveFailureReason.API_ERROR
            self._warn(failure, f"{service_key}/{price_key}")
            if failure in RUN_LEVEL_REASONS:  # defensive: normally pre-checked
                self._disable(failure)

        rate = self._snapshot.get_rate(region, service_key, price_key)
        self._pending_lookups.append((service_key, price_key, "snapshot"))
        if rate is None:
            self._miss_count += 1
        else:
            self._fallback_count += 1
        return rate

    # -- attribution (R29) --

    def drain_lookups(self) -> list[tuple[str, str, str]]:
        """(service_key, price_key, source) since the last drain; clears them."""
        drained = self._pending_lookups
        self._pending_lookups = []
        return drained

    # -- reporting (R27/R30, consumed by chunk 3) --

    def report(self) -> LivePricingReport:
        """The verdict-meta ``live_pricing`` object for this run."""
        from spend_sentinel.core.models import (
            LivePricingReport,
            LivePricingStatus,
            LivePricingWarning,
        )

        if self._disabled is not None:
            status = LivePricingStatus.UNAVAILABLE
        elif self._warnings or self._fallback_count or self._miss_count:
            status = LivePricingStatus.DEGRADED
        else:
            status = LivePricingStatus.OK

        dates: tuple[str, str] | None = None
        if self._publication_dates:
            ordered = sorted(self._publication_dates)
            dates = (ordered[0], ordered[-1])

        return LivePricingReport(
            requested=True,
            status=status,
            endpoint_region=self._endpoint_region,
            lookups_live=self._live_count,
            lookups_snapshot_fallback=self._fallback_count,
            lookups_miss=self._miss_count,
            publication_dates=dates,
            warnings=tuple(
                LivePricingWarning(reason=reason, detail=detail)
                for reason, detail in self._warnings
            ),
        )

    # -- internals --

    def _disable(self, reason: LiveFailureReason) -> None:
        if self._disabled is None:
            self._disabled = reason
            self._warn(reason, "")

    def _warn(self, reason: LiveFailureReason, detail: str) -> None:
        self._warnings.setdefault((reason.value, detail))
