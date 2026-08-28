# PR: spend-sentinel v1.1 — live Pricing API adapter (R22–R33)

Branch: `feature/live-pricing`; built in pipelined chunks (C1 foundation on
`lp-c1`, C2 integration, C3 surface).
Spec: `docs/specs/live-pricing-v1.1.md` (APPROVED).

## Chunk 1 — foundation (T11, T13, T12 primitives)

### What landed

- `spend_sentinel/pricing/live.py` (pure, no boto3 — smoke-asserted):
  - `PricingApiClient` protocol: `get_products(service_code, filters,
    next_token) -> dict`; `PricingApiError` with reason `timeout`/`api_error`.
  - `REGION_LOCATIONS` (R26: us-east-1/us-west-2/eu-west-1 → location names,
    extensible dict), `RDS_ENGINE_MAP`, `RDS_STORAGE_MAP`.
  - `build_query(region, service_key, price_key) -> QuerySpec` — the R25
    filter matrix verbatim (EC2 Linux/Shared/NA/Used; EBS volumeApiName +
    productFamily=Storage; RDS engine map + **deploymentOption=Single-AZ**
    always; RDS storage map; NAT and ELB by productFamily with
    usagetype-suffix+unit dimension rules). Unmappable → `UnmappableKeyError`
    (`unsupported_region` | `unmapped_value`).
  - `extract_rate(pages, rule) -> LiveRate` — defensive extraction (R31):
    navigated-path pydantic models, 256 KiB entry cap
    (`oversize_response`), ≤ 50 products, USD parsed as finite non-negative
    `Decimal` < 10^6 straight from the string (`parse_error` otherwise),
    NAT/LB hourly-dimension selection, zero rates → `no_match`, >1 distinct
    USD → `ambiguous`, `publicationDate` capture. Fail-closed per key; no
    response text leaves the module.
  - `fetch_pages` — NextToken pagination, ≤ 3 pages → `pagination_overflow`.
  - `LiveFailureReason` (full 12-reason R27 taxonomy) + `RUN_LEVEL_REASONS`.
  - R28 primitives: `RunCache` (one memoized `LookupOutcome` per unique
    `(region, service_key, price_key)`, failures included), `Budget` (30 s,
    injectable monotonic clock), `resolve_live_rate` (never raises) and
    **`cached_resolve(client, cache, budget, region, service_key,
    price_key) -> LookupOutcome`** — the entry point chunk 2's
    `LivePricingSource` consumes.
- `spend_sentinel/pricing/fixture_client.py`: `FixturePricingClient`
  (canned pages keyed by service code + filter set, NextToken chaining,
  error injection, call recording for AC16, `on_call` hook for AC18 fake
  clocks) + `RecordedCall`, `fixture_key`.
- `spend_sentinel/adapters/boto3_pricing.py`: `Boto3PricingClient` — the
  only new module importing boto3 (lazily, in `__init__`);
  `PricingClientUnavailable(boto3_missing | client_init_error)`; endpoint
  us-east-1 default with `SPEND_SENTINEL_PRICING_ENDPOINT_REGION` override
  validated `^[a-z0-9-]{1,32}$` before boto3 sees it (A9/R31); botocore
  `Config(connect=5s, read=10s, retries standard max_attempts=3)`;
  `MaxResults=100`; only `pricing:GetProducts` is called; timeouts →
  `PricingApiError(timeout)`, other botocore errors → `api_error`.

Not yet wired: `LivePricingSource`, `PricingSource` integration, CLI flag,
attribution, renderers, docs/IAM — chunks 2/3. No existing file was modified;
the full v1 suite passes unchanged (458 passed).

### Chunk-1 assumptions (flagged)

- **A-c1 (pagination home)**: T13's wording puts "NextToken pagination" in
  the adapter, but the Modularity notes' protocol signature takes
  `next_token`, so the pure layer drives pagination (`fetch_pages`) and the
  adapter is single-call transport — matching the fixture-testability goal.
  The page cap is enforced in the pure layer.
- **A-c2 (usagetype source)**: the spec's navigated path lists `usagetype`
  under `priceDimensions`; in real GetProducts payloads it usually lives in
  `product.attributes`. Selection checks the dimension-level field first and
  falls back to the product attribute, so both shapes work.
- **A-c3 (retries)**: "at most 2 retries" → botocore standard mode
  `max_attempts=3` (initial attempt + 2 retries).
- **A-c4 ("only OnDemand dimension" types)**: for EC2/EBS/RDS the extractor
  additionally requires the R25 "Expected unit" (`Hrs`/`GB-Mo`) on selected
  dimensions, so a spurious non-hourly OnDemand dimension yields
  `no_match`/`ambiguous` rather than a wrong rate.
- **A-c5 (mixed-quality responses)**: one malformed/oversized PriceList
  entry fails the whole key (fail-closed) instead of cherry-picking valid
  siblings from a partially hostile response.

### Chunk-1 smoke results

Filter matrix incl. RDS Single-AZ + PostgreSQL mapping and
unsupported-region/unmapped-value errors; EC2 extraction to the cent with
publicationDate; NAT hourly-vs-GB dimension selection; ambiguous two-rate
fallback; no_match / NaN / negative / ≥10^6 / oversize / malformed each →
correct reason, no exception; 2-page pagination and 4-page overflow; cache =
exactly 1 client call per unique key incl. negative caching; fake-clock
budget: 4 keys, 16 s/call → exactly 2 calls, rest `budget_exhausted`;
`import spend_sentinel.pricing.live` leaves `boto3` out of `sys.modules`;
boto3-less venv: constructor → `boto3_missing`, invalid endpoint env →
`client_init_error`. ruff + `python3 -m mypy` clean (23 files).
