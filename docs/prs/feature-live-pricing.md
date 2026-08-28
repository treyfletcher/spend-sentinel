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

## Chunk 2 — integration (T12 completion, R24/R27/R29/R30-model)

### What landed

- `pricing/live.py::LivePricingSource` — implements the existing
  `PricingSource` protocol wrapping an injected `PricingApiClient` and the
  snapshot fallback (typed as `PricingSource`, so any source can back it):
  R24 resolution order; run-level failures disable the API for the rest of
  the run after one recorded warning; per-call lookup counters and
  `publicationDate` range capture; warning de-dup by `(reason, detail)` with
  details from internal keys only; `drain_lookups()` attribution;
  `report() -> LivePricingReport`. Constructible with `client=None` +
  `disabled_reason` (chunk 3 uses this for `boto3_missing`/
  `client_init_error`).
- `core/models.py`: `CostLine.price_source: Literal["live","snapshot",
  "mixed"] | None = None`; `LivePricingStatus`, `LivePricingWarning`,
  `LivePricingReport` (`requested`, `status`, `endpoint_region`,
  `lookups_live/snapshot_fallback/miss`, `publication_dates: (earliest,
  latest) | None`, `warnings`).
- `core/cost.py::estimate` — the sanctioned R29 hook: `getattr`-discovered
  `drain_lookups`, drained before the loop, after unpriced attempts, and
  after each priced resource to set `price_source`.

### Chunk-3 consumer surface

- Wiring: build `Boto3PricingClient` (guarded; on `PricingClientUnavailable`
  construct `LivePricingSource(None, snapshot, disabled_reason=exc.reason)`),
  else `LivePricingSource(client, snapshot, endpoint_region=...)`; pass it to
  the existing `estimate()` unchanged.
- After estimating: `source.report()` returns the `LivePricingReport` for
  `meta.live_pricing` (JSON) and the Markdown summary line; its `warnings`
  drive the one-line-per-distinct-reason stderr output (R27 — printing is
  chunk 3's job, the de-duped list is ready).
- `CostLine.price_source` feeds the Markdown `Source` column and the JSON
  field (serialize only when not `None` / only when `--live-pricing`).

### Chunk-2 assumptions (flagged)

- **A-c6 (publication_dates shape)**: R30 writes `publication_dates:
  {"<earliest>", "<latest>"} | null` — modeled as an ordered pair
  `(earliest, latest)` (equal when one date), `None` when no live rate was
  accepted.
- **A-c7 (status rule)**: `ok` requires zero warnings AND zero
  fallback/miss lookups; any degradation — including a key that missed in
  both sources — makes the run `degraded` (spec: "ok when every priced
  lookup was live").
- **A-c8 (warning granularity)**: key-level failures record one warning per
  distinct `(reason, service_key/price_key)`; run-level reasons once with
  empty detail. Stderr in chunk 3 collapses further to one line per
  distinct reason (R27).
- **A-c9 (counters count calls, not keys)**: `lookups.*` counts `get_rate`
  resolutions (AC14's `lookups.live == 2` semantics); the cache still
  guarantees one API query per unique key.

### Chunk-2 smoke results

Mixed 5-resource run: two t3.micro live (8.76 = 0.0120 × 730, one API call
for both), gp3 `no_match` → snapshot 0.80, Multi-AZ RDS = exactly
2 × live Single-AZ rate × 730 + snapshot storage → `mixed` (128.30), unknown
instance type missing in both → R7 `unknown_price_key`; report: lookups
3/2/1, `degraded`, date range spans fixtures, warnings name internal keys
only, 5 calls for 6 lookups; `boto3_missing` construction → all `snapshot`,
`unavailable`, single warning; unknown region on first `get_rate` → run
disabled with `unsupported_region`; snapshot-only `estimate` leaves every
`price_source` `None`; default import graph never loads `pricing.live`;
full v1 suite 458 passed; ruff + `python3 -m mypy` clean.

## Chunk 3 — surface (T15/T16 minus tests, R22/R27/R30/R33) + BUG-5

### What landed

- `cli.py`: `--live-pricing` flag; guarded wiring exactly per the chunk-2
  surface (`_make_live_pricing_source`); default path imports neither
  `pricing.live` nor `adapters.boto3_pricing` (smoke-asserted) and its
  outputs stay byte-identical; one stderr warning per distinct degradation
  reason; exit codes verdict-driven under the flag (A11).
- `adapters/boto3_pricing.py`: `resolve_endpoint_region()` (pure env
  resolution for reporting; invalid token reported as `"invalid"`), and
  **BUG-5 fix**: endpoint validation now `fullmatch` — `"eu-west-1\n"` no
  longer reaches boto3 (`fix(R31)` commit).
- `core/models.py`: `VerdictMeta.live_pricing: LivePricingReport | None`.
- `render/jsonout.py`: `price_source` per breakdown entry and
  `meta.live_pricing` (lookups, `publication_dates: {earliest, latest} |
  null`, warnings) — all emitted only when present.
- `render/markdown.py`: `Pricing: ...` summary line under the header
  (`live (N live, M snapshot-fallback; prices published A..B)` /
  `snapshot v… — live pricing unavailable: <reason>`) and a `Source`
  column — only when live pricing was requested; publication dates pass
  through the v1 escaper.
- `docs/iam-policy-pricing.json` (exactly `pricing:GetProducts`), README
  live-pricing section, `docs/verdict-schema.md` v1.1 fields marked
  flag-only. CI untouched.

### Behavior changes for the tester (v1.1 surface)

- New flag `--live-pricing`; with it: breakdown entries gain
  `price_source`, meta gains `live_pricing`, Markdown gains the summary
  line + `Source` column, stderr gains warning lines on degradation.
  Without it: zero byte changes (AC13 holds; verified by double-run cmp and
  key-absence checks). Exit codes never differ between flag/no-flag runs
  (A11) — degradation is warnings-only, unlike drift's exit-2 path.
- `FixturePricingClient` + monkeypatching `cli._make_live_pricing_source`
  (or injecting `LivePricingSource` directly) is the intended e2e seam, as
  in AC14–AC20.

### Chunk-3 assumptions (flagged)

- **A-c10 (summary-line format)**: degraded runs also list `K miss` when
  present; `ok` runs render `Pricing: live (N live; prices published …)` —
  spec gave examples, not a grammar.
- **A-c11 (endpoint reporting)**: an invalid
  `SPEND_SENTINEL_PRICING_ENDPOINT_REGION` yields `client_init_error` and
  `endpoint_region: "invalid"` in meta — the raw value is never echoed.
- **A-c12 (warning line format)**: `spend-sentinel: warning: live pricing
  degraded (<reason>); snapshot fallback used` — reasons only, no keys, no
  response text; ordering follows first occurrence.

### Chunk-3 smoke results

Mixed fixture-wired CLI run: sources `live`/`mixed`/`snapshot` per resource,
`status: degraded`, lookups 2/2/0, publication range spans fixtures, two
de-duped stderr warning lines, Markdown `Source` column + summary line,
exit 0. Snapshot-only run: byte-identical across runs, zero
`price_source`/`live_pricing`/`Pricing:` occurrences, imports guard holds.
`--live-pricing` with boto3 absent (real wiring): exit 0, one
`boto3_missing` stderr line, `status: unavailable`, all-snapshot sources,
MD shows `Pricing: snapshot … — live pricing unavailable: boto3_missing`.
BLOCK plan exits 1 with and without the flag. BUG-5: `"eu-west-1\n"`
override now rejected pre-boto3. ruff + `python3 -m mypy` clean; full v1
suite 458 passed.

## Overall summary (v1.1)

Chunks 1–3 deliver R22–R33: a pure filter/extraction/cache/budget layer
(`pricing/live.py`), the `FixturePricingClient` and `Boto3PricingClient`
transports, `LivePricingSource` with total degradation and attribution, the
sanctioned `core/cost.py` drain hook, flag wiring, renderer/meta surface,
and docs/IAM. Owner-facing invariants held throughout: default path
byte-identical (R22), fallback never fails a run or changes an exit code
(R27/A11), boto3 confined to `adapters/` (R32), no response text in any
output (R31). T8-style e2e tests remain the tester's (AC13–AC21 seams are
in place).
