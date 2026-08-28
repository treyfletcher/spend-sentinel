# Spec: spend-sentinel v1.1 — Live AWS Pricing API adapter

Status: DRAFT
Branch: feature/live-pricing

## Summary

v1.1 adds an opt-in live pricing path: a new `PricingSource` implementation, `LivePricingSource`, that resolves on-demand rates from the AWS Pricing API (`GetProducts` via boto3) with per-key fallback to the bundled v1 snapshot. It is enabled only by `--live-pricing`; without the flag, behavior — including JSON and Markdown output bytes — is identical to v1 (R5/AC11 determinism holds). The design core is degradation, not the happy path: any live-pricing failure (no boto3, no credentials, API error, timeout, budget exhaustion, no/ambiguous match, unparseable response) falls back to the snapshot for the affected key(s) and never changes the run's exit code; the verdict's meta and outputs report which source priced each resource (live / snapshot / mixed) and the API's price publication date. All Pricing API interaction sits behind the same isolation pattern as `Boto3AwsReader`: boto3 is imported only in one new adapter module, and the filter-building, response-extraction, fallback, cache, and budget logic is pure and tested offline against fixture `GetProducts` responses. Scope is exactly the five resource types / price dimensions of v1 R4; the main intellectual content is the precise mapping from plan attributes to Pricing API filters, specified per dimension in R25.

## Requirements

Each requirement is verifiable offline with fixture `GetProducts` responses; no test may require network or AWS credentials (R21 continues to hold).

**Activation and the default path**

- R22: `spend-sentinel analyze --live-pricing ...` activates live pricing; the flag composes with every existing flag. Without `--live-pricing`, the wiring constructs exactly the v1 `SnapshotPricingSource` and produces byte-identical JSON and Markdown output for every v1 fixture scenario — v1 golden files are not modified, and a regression test runs a representative fixture set both before and after the change against the unmodified goldens.
- R23: `--live-pricing` does not extend region coverage: the R8 rule that the resolved region must be present in the snapshot is unchanged (exit 2 otherwise), because the snapshot is the guaranteed fallback for every key. Live pricing improves freshness only.

**Resolution and fallback semantics**

- R24: `LivePricingSource` implements the existing `PricingSource` protocol (`get_rate(region, service_key, price_key) -> Decimal | None`) and wraps two collaborators injected at construction: a `PricingApiClient` (protocol, see Modularity notes) and the v1 `SnapshotPricingSource`. Resolution per key is: (1) if live pricing is disabled for the run (see R27 reasons) or the key's dimension/region is not mappable to API filters, use the snapshot; (2) otherwise query the API once, and on a valid single-rate extraction return the live rate; (3) on any failure, return the snapshot rate; (4) return `None` only when both live and snapshot miss — which flows into the existing R7 `unknown_price_key` unpriced taxonomy unchanged. A live rate that extracts successfully is converted to `Decimal` from the response's USD string (never via float) and then follows the same R5 math as snapshot rates.
- R25: Filter matrix. For each service_key, the adapter issues `GetProducts` with `ServiceCode`, `Filters` (all `Type: "TERM_MATCH"`), and extracts the `OnDemand` term's single price dimension `pricePerUnit.USD`. The exact matrix:

  | service_key | ServiceCode | Filters (TERM_MATCH) | Select dimension by | Expected unit |
  |---|---|---|---|---|
  | `aws_instance` (price_key = instance type, e.g. `t3.micro`) | `AmazonEC2` | `instanceType=<key>`, `location=<loc>`, `operatingSystem=Linux`, `tenancy=Shared`, `preInstalledSw=NA`, `capacitystatus=Used` | only OnDemand dimension | `Hrs` |
  | `aws_ebs_volume` (price_key = volume type) | `AmazonEC2` | `volumeApiName=<key>` (`gp2`,`gp3`,`io1`,`io2`,`st1`,`standard`), `location=<loc>`, `productFamily=Storage` | only OnDemand dimension | `GB-Mo` |
  | `aws_db_instance.instance` (price_key = `<engine>:<instance_class>`) | `AmazonRDS` | `instanceType=<class>`, `databaseEngine=<mapped engine>`, `deploymentOption=Single-AZ`, `location=<loc>` | only OnDemand dimension | `Hrs` |
  | `aws_db_instance.storage` (price_key = storage type) | `AmazonRDS` | `volumeType=<mapped type>`, `deploymentOption=Single-AZ`, `productFamily=Database Storage`, `location=<loc>` | only OnDemand dimension | `GB-Mo` |
  | `aws_nat_gateway` (price_key = `hourly`) | `AmazonEC2` | `productFamily=NAT Gateway`, `location=<loc>` | usagetype ends with `NatGateway-Hours` AND unit `Hrs` (excludes the GB-processed dimension) | `Hrs` |
  | `aws_lb` (price_key = `application` \| `network`) | `AWSELB` | `productFamily=Load Balancer-Application` or `Load Balancer-Network`, `location=<loc>` | usagetype ends with `LoadBalancerUsage` AND unit `Hrs` (excludes LCU dimensions) | `Hrs` |

  Attribute value maps (hardcoded tables in the pure module; an unmapped input value means the key is not live-mappable → snapshot fallback with reason `unmapped_value`):
  - RDS engine: `postgres → PostgreSQL`, `mysql → MySQL`, `mariadb → MariaDB` (exactly the engines the snapshot covers; others fall back).
  - RDS storage volumeType: `gp2 → General Purpose`, `gp3 → General Purpose-GP3`, `io1 → Provisioned IOPS`, `standard → Magnetic`.
  - Critical correctness rule: the RDS filter always uses `deploymentOption=Single-AZ` because `core/cost.py` itself doubles the instance cost when `multi_az` is true; fetching a Multi-AZ rate would double-count. A test asserts a Multi-AZ fixture plan priced live yields exactly 2 × the Single-AZ live rate × 730.
  - If, after dimension selection, zero products/dimensions match → reason `no_match`; more than one distinct USD rate survives selection → reason `ambiguous`. Both fall back to snapshot (never guess).
- R26: Region-code → location-name mapping is a static table bundled in the pure module (no `DescribeServices`/discovery calls), covering at minimum the snapshot's regions `us-east-1 → "US East (N. Virginia)"`, `us-west-2 → "US West (Oregon)"`, `eu-west-1 → "EU (Ireland)"`. A resolved region absent from the table disables live lookup for the whole run with reason `unsupported_region` (snapshot still prices it per R23). The Pricing API endpoint region is independent of the price-filter region: the client is always constructed against endpoint region `us-east-1` (env var `SPEND_SENTINEL_PRICING_ENDPOINT_REGION` may override, e.g. `ap-south-1`); the analyzed region appears only in the `location` filter.
- R27: Degradation never fails the run and never alters the exit code: with `--live-pricing`, every failure class resolves to snapshot fallback and the exit code is exactly what the same run would produce snapshot-only (0/1 per verdict; not 2 — justified in A11). The failure taxonomy, recorded per run or per key in `meta.live_pricing.warnings`: `boto3_missing` (the `[aws]` extra is not installed), `client_init_error` (no credentials / client construction fails), `api_error` (any botocore exception on a call), `timeout` (per-call), `budget_exhausted` (R28), `unsupported_region`, `unmapped_value`, `no_match`, `ambiguous`, `parse_error`, `pagination_overflow`, `oversize_response`. Run-level failures (`boto3_missing`, `client_init_error`, `unsupported_region`) disable the API for the whole run after being recorded once; key-level failures affect only that key. Every degradation also emits a one-line stderr warning (at most one line per distinct reason per run).

**Caching and time budget**

- R28: Within a run, at most one `GetProducts` query (including its pagination) is issued per unique `(region, service_key, price_key)` triple — results, including failures, are memoized in an in-run cache so repeated resources reuse the outcome. A run-level monotonic-clock budget of 30 seconds covers all Pricing API calls combined: once exceeded, all remaining uncached keys skip the API with reason `budget_exhausted`. Each boto3 call uses a botocore `Config` with bounded connect/read timeouts (5s/10s) and at most 2 retries. Pagination follows `NextToken` at most 3 pages per key (`MaxResults=100`); overflow → `pagination_overflow` fallback. No cross-run/disk cache in v1.1 (Out of scope). Per-key querying (rather than one bulk call per service/region) is the chosen batching strategy: exact TERM_MATCH filters return a handful of products per call, keeping responses small enough for the R31 bounds, whereas a per-service batch returns thousands of paginated offer items that would dominate the budget and the defensive-parsing surface for no benefit at 5 dimensions.

**Attribution and reporting**

- R29: Per-resource source attribution. `LivePricingSource` records every `get_rate` resolution as `live` or `snapshot` and exposes `drain_lookups() -> list[tuple[str, str, str]]` (service_key, price_key, source) returning and clearing the lookups since the last drain. `core/models.CostLine` gains an optional field `price_source: Literal["live", "snapshot", "mixed"] | None = None`, omitted from JSON when `None`; `core/cost.py::estimate` gains one small, isolated addition: after computing each resource's delta, if the pricing source has a `drain_lookups` attribute, it drains and sets `price_source` (`live` if all lookups were live, `snapshot` if all snapshot, else `mixed` — e.g. RDS instance live + storage fallback); it also drains before/after unpriced attempts so stale lookups never leak across resources. Justification for touching `core/cost.py` (permitted "trivial change"): only the estimator knows which lookups belong to which resource; the alternative — re-deriving attribute→price-key mappings in a second module to join against the adapter's log — duplicates R4 pricing logic and will drift. `SnapshotPricingSource` has no `drain_lookups`, so the default path takes the `None` branch and output stays byte-identical (R22).
- R30: Verdict reporting. With `--live-pricing`, the JSON verdict's `meta` gains a `live_pricing` object: `{requested: true, status: "ok" | "degraded" | "unavailable", endpoint_region, lookups: {live: N, snapshot_fallback: N, miss: N}, publication_dates: {"<earliest>", "<latest>"} | null, warnings: [{reason, detail}]}` — `status` is `ok` when every priced lookup was live, `unavailable` when a run-level failure disabled the API, else `degraded`. `publication_dates` come from the `publicationDate` field of accepted price-list items (the "API price date"); no wall-clock timestamp is written to any output, so fixture-driven live runs remain golden-testable. The Markdown report gains: a `Source` column in the cost table (values `live`/`snapshot`/`mixed`, only rendered when `--live-pricing` was passed) and a one-line pricing-sources summary under the header, e.g. `Pricing: live (9 live, 2 snapshot-fallback; prices published 2026-08-20..2026-08-27)` or `Pricing: snapshot v<ver> (<date>) — live pricing unavailable: client_init_error`. Without the flag, neither JSON nor Markdown changes in any byte. `docs/verdict-schema.md` is updated accordingly.

**Isolation, security, testability**

- R31: Defensive parsing of API responses (external data): each `PriceList` entry is a JSON string capped at 256 KiB (larger → `oversize_response` fallback for that key); at most 50 products are examined per key; extraction goes through a pydantic model that validates only the navigated path (`terms.OnDemand.*.priceDimensions.*.{pricePerUnit.USD, unit, usagetype}` plus `product.attributes` needed for selection and `publicationDate`), ignoring everything else; the USD value must parse as a finite, non-negative `Decimal` strictly below 1,000,000, else `parse_error`. No response content is ever echoed into diagnostics, warnings, or outputs beyond the extracted price value and `publicationDate`; warning `detail` strings are built from spend-sentinel's own enums/keys, never from response text. Credentials come from the standard AWS chain only — no new flags or env vars carry secrets, and nothing credential-related is logged.
- R32: boto3 isolation and offline tests (R21 extended): boto3/botocore are imported only inside the single new adapter module `adapters/boto3_pricing.py`; importing `spend_sentinel.pricing.live` must not import boto3 (asserted in a test, mirroring the existing Boto3AwsReader test). The whole suite still runs with no network and AWS env vars scrubbed; all live-path tests use a `FixturePricingClient` fed by canned raw `GetProducts` response JSON fixtures under `tests/fixtures/pricing_api/`. A run with `--live-pricing` and boto3 absent completes successfully in full-snapshot fallback (reason `boto3_missing`).
- R33: IAM: a new, separate optional policy document `docs/iam-policy-pricing.json` containing exactly the action `pricing:GetProducts` on `Resource: "*"` (the Pricing API is not resource-scopable), with a README note that it is needed only for `--live-pricing`. The existing drift policy `docs/iam-policy.json` is not modified (a test asserts its action list is unchanged). `pricing:DescribeServices` is not requested — the filter matrix is static, so the tool never calls it.

## Out of scope

- Cross-run/disk caching of Pricing API results (Future work; in-run cache only per R28).
- Extending the priced dimension set beyond v1 R4's five types, or pricing dimensions the API could newly enable (LCUs, NAT data processing, gp3 provisioned IOPS/throughput, Windows/dedicated tenancy, Multi-AZ-specific rates beyond the ×2 convention).
- Extending region coverage beyond the snapshot (R23): live pricing is a freshness upgrade, not a coverage upgrade. Adding regions means adding them to the snapshot first.
- Savings Plans / RI / spot pricing; currencies other than USD.
- Using `pricing:DescribeServices`/attribute discovery, or the bulk offer files (hundreds of MB — the very reason v1 deferred this).
- Live pricing for drift detection or any non-cost feature; changes to policy rules, exit-code semantics, or the drift IAM policy.
- The `tools/refresh-pricing` snapshot-regeneration script (still Future work; v1.1's adapter serves runs, not snapshot curation).
- A `--pricing-timeout`/budget knob (30s fixed) and any retry tuning surface beyond R28's constants.

## Dependencies

- External: no new dependencies. `boto3 >= 1.34, < 2` stays an optional extra (`spend-sentinel[aws]`), now also providing the pricing client; botocore (bundled with boto3) supplies `Config` for timeouts/retries. Python/tooling pins unchanged from v1.
- Internal: builds on `pricing/source.py` (protocol, unchanged), `pricing/snapshot.py` (unchanged, used as the fallback collaborator), `core/cost.py` + `core/models.py` (the single trivial change of R29), `cli.py` (flag + wiring), `render/markdown.py` + `render/jsonout.py` (R30 additions), `docs/verdict-schema.md`.
- Ordering:
  - T11 (filters/extraction, pure) blocks T12.
  - T12 (LivePricingSource) blocks T14 and T15; T13 (boto3 adapter) is independent of T12's internals after the protocol lands in T11/T12 and blocks only T15's production wiring.
  - T14 (attribution/meta/renderers) needs T12; T15 (CLI + e2e) needs T12–T14; T16 (docs/IAM) can trail but before merge.

## Task breakdown

- T11: Pure query layer — `pricing/live.py` (or a `_query.py` sibling): the region→location table (R26), attribute value maps and per-dimension filter builders for the R25 matrix, and the defensive response extractor (pydantic model, size/count caps, USD validation, dimension-selection rules for NAT/LB, `publicationDate` capture) with the `no_match`/`ambiguous`/`parse_error`/`oversize_response` outcomes. Unit tests entirely on fixture `GetProducts` payloads, including realistic multi-product EC2 responses, a NAT response containing both `Hrs` and GB-processed dimensions, an LB response with LCU dimensions, an ambiguous two-rate response, an oversized entry, and malformed JSON. (R25, R26, R31.)
- T12: `LivePricingSource` — `PricingApiClient` protocol, per-key resolution order, in-run cache including negative caching, the 30s monotonic budget, run-level vs key-level failure handling with the R27 reason taxonomy, `drain_lookups()` attribution recording, and `FixturePricingClient` for tests (canned responses, programmable errors/latency injection via a fake clock). Table-driven unit tests per failure reason asserting fallback rate, recorded warning, and cache behavior (exactly one client call per unique key). (R24, R27, R28, R29-adapter-side.)
- T13: `adapters/boto3_pricing.py` — `Boto3PricingClient` implementing the protocol: client construction against the endpoint region (env override), botocore `Config` timeouts/retries, `NextToken` pagination with the page cap, translation of botocore exceptions into the protocol's error results. The only new module importing boto3; import-isolation test mirrors the Boto3AwsReader one. (R26-endpoint, R28, R32.)
- T14: Attribution and reporting — `CostLine.price_source` field + the guarded `drain_lookups` hook in `core/cost.py` (the R29 trivial change, with a test proving `SnapshotPricingSource` runs leave the field `None`); `meta.live_pricing` model; Markdown `Source` column + pricing-sources summary line; JSON serializer updates; `docs/verdict-schema.md` update. Golden-file tests for a live-mode fixture run (deterministic because no wall clock, R30) and byte-identity regression of default-path goldens. (R22, R29, R30.)
- T15: CLI wiring and e2e — `--live-pricing` flag; production wiring builds `Boto3PricingClient` inside a guarded import (degrading with `boto3_missing`/`client_init_error` instead of raising); stderr warning lines; e2e fixture scenarios through the CLI entry point: (a) all-live run with `Source` column and `status: ok`; (b) partial fallback (one key `no_match`) with `status: degraded`, correct per-resource sources including a `mixed` RDS line, exit code equal to the snapshot-only run's; (c) boto3 absent → full fallback, run succeeds; (d) budget exhaustion via fake clock; (e) default run byte-identical to v1 goldens. (R22, R23, R27, R32.)
- T16: Docs and IAM — README live-pricing section (opt-in, IAM, endpoint note, degradation semantics, freshness-vs-coverage caveat); `docs/iam-policy-pricing.json` (R33) plus the drift-policy-unchanged test; CI needs no changes (network-free posture already enforced) — verify R21's env-scrub job stays green with the new tests. (R33, R32.)

## Acceptance criteria

- AC13 (R22): Given any v1 e2e fixture scenario run without `--live-pricing` on this branch, When JSON and Markdown outputs are compared to the unmodified v1 golden files, Then they are byte-identical, and running twice remains byte-identical (AC11 still holds).
- AC14 (R24, R25, R29, R30): Given a fixture plan creating a t3.micro instance and a gp3 volume in us-east-1, and a `FixturePricingClient` serving valid `GetProducts` fixtures for both keys with rates differing from the snapshot, When run with `--live-pricing`, Then both breakdown entries carry `price_source: "live"` with deltas computed from the fixture rates to the cent, `meta.live_pricing.status == "ok"` with `lookups.live == 2`, `publication_dates` spans the fixtures' `publicationDate` values, and the Markdown shows the `Source` column and the pricing summary line.
- AC15 (R24, R27, R29): Given the same plan but the fixture client returns an empty `PriceList` for the volume key, When run with `--live-pricing`, Then the volume entry has `price_source: "snapshot"` with the snapshot rate, the instance stays `live`, `meta.live_pricing.status == "degraded"` with a `no_match` warning naming only the service/price key (no response text), stderr carries one warning line, and the exit code equals the snapshot-only run's exit code.
- AC16 (R25): Given a Multi-AZ postgres `aws_db_instance` fixture plan and a live fixture for `aws_db_instance.instance` (Single-AZ filter asserted on the recorded client call: `deploymentOption=Single-AZ`, `databaseEngine=PostgreSQL`) plus a storage fixture, When priced live, Then the instance component equals exactly 2 × live hourly rate × 730 and the recorded `GetProducts` calls carry the full R25 filter sets verbatim.
- AC17 (R25, R31): Given NAT-gateway and NLB fixture responses each containing both an hourly dimension and a usage dimension (GB-processed / LCU), When priced live, Then the hourly rate is selected by the usagetype-suffix + unit rule; and Given a response where two distinct USD hourly rates survive selection, Then the key falls back to snapshot with reason `ambiguous`.
- AC18 (R27, R28): Given a plan with 4 unique price keys, a fake clock, and a fixture client injecting per-call latency such that the 30s budget is exceeded after the second call, When run with `--live-pricing`, Then exactly 2 `GetProducts` queries were issued, the remaining keys are snapshot-priced with `budget_exhausted`, the run completes with the verdict-driven exit code; and a plan with 10 resources sharing one instance type issues exactly 1 query for that key.
- AC19 (R27, R32): Given boto3 uninstalled, When `analyze --live-pricing` runs on a fixture plan, Then the run completes with all entries `price_source: "snapshot"`, `meta.live_pricing.status == "unavailable"` with reason `boto3_missing`, a single stderr warning, and the same exit code as the run without the flag; and the whole test suite passes with AWS env vars scrubbed and no network.
- AC20 (R31): Given fixture responses containing (a) a 300 KiB `PriceList` entry, (b) a USD value of `"NaN"`, (c) a negative USD value, and (d) non-JSON price-list text, When priced live, Then each key falls back to snapshot with `oversize_response`/`parse_error` reasons, no exception propagates, and no fragment of the response bodies appears in stderr, warnings, JSON, or Markdown.
- AC21 (R33): Then `docs/iam-policy-pricing.json` exists and its statement actions equal exactly `["pricing:GetProducts"]`, and a test asserts `docs/iam-policy.json`'s action list is unchanged from v1.

## Security considerations

- External-data posture: `GetProducts` responses are untrusted input even though they come from AWS (the account, proxy, or endpoint override could be hostile). All extraction is schema-validated via pydantic with size caps (256 KiB per price-list entry, ≤ 50 products, ≤ 3 pages) and fails closed to snapshot fallback — never to partial parsing or crashes (R31). USD values are bounds-checked finite non-negative Decimals below 10^6, closing the same NaN/Infinity class as v1's BUG-2 fix in `core/cost.py`.
- No response echo: diagnostics, `meta.live_pricing.warnings`, and stderr lines are composed from internal enums and the tool's own service/price keys only; extracted price values and `publicationDate` are the only response-derived data that reaches any output. This keeps a hostile response from injecting Markdown into the PR comment (v1's escaping still applies to everything rendered).
- Credentials: standard AWS chain only; no key material in flags, config, logs, or outputs. `SPEND_SENTINEL_PRICING_ENDPOINT_REGION` accepts a region token validated against `^[a-z0-9-]{1,32}$` before being handed to boto3, and its value is not a secret.
- Least privilege: `pricing:GetProducts` lives in a separate optional policy doc (R33); the drift policy does not grow, so v1 deployments gain no new permissions unless they opt in. The pricing client calls no API other than `GetProducts` (asserted via the fixture client's method surface, mirroring the v1 AwsReader test).
- Availability abuse: the 30s budget, per-call timeouts, retry cap, and page cap bound the tool's runtime and request volume even against a slow/looping endpoint, so `--live-pricing` cannot turn a CI job into a hang.

## Modularity notes

New/changed modules (`core` still never imports `adapters`; boto3 still confined to `adapters/`):

```
spend_sentinel/
  pricing/
    source.py             # UNCHANGED — the protocol both sources implement
    snapshot.py           # UNCHANGED — now also the injected fallback
    live.py               # NEW: PricingApiClient (Protocol): get_products(service_code, filters, next_token) -> raw dict
                          #      region→location table; filter builders; defensive extractor (R25/R26/R31)
                          #      LivePricingSource(client, snapshot, clock=time.monotonic): fallback, cache,
                          #      budget, warnings, drain_lookups()  — pure, no boto3 import
  adapters/
    boto3_pricing.py      # NEW: Boto3PricingClient — the ONLY new module importing boto3;
                          #      endpoint-region handling, timeouts/retries/pagination
  core/
    models.py             # CostLine.price_source (optional, JSON-omitted when None); meta live_pricing model
    cost.py               # R29 hook only: guarded drain_lookups() attribution (~6 lines)
  cli.py                  # --live-pricing wiring; guarded boto3 import; stderr degradation warnings
  render/markdown.py      # Source column + pricing summary line (only when live requested)
  render/jsonout.py       # serializes the new optional fields
tests/fixtures/pricing_api/   # raw GetProducts response JSON fixtures
```

- `PricingApiClient` is a transport-only protocol returning raw response dicts, so every fixture is a realistic `GetProducts` payload and the entire mapping/extraction/fallback brain in `pricing/live.py` is exercised offline — the same split as `AwsReader`/`Boto3AwsReader`.
- `LivePricingSource` takes an injectable monotonic clock so budget tests are instant and deterministic.
- Attribution crosses the protocol boundary via the optional `drain_lookups` capability discovered with `getattr`, keeping `PricingSource` itself unchanged and the snapshot path untouched.
- No wall-clock values in any output (R30), so live-mode fixture runs stay golden-file-testable.

## Open questions / assumptions

- A9 (endpoint): The Pricing API is served only from us-east-1 and ap-south-1. Default endpoint region is `us-east-1`, overridable via `SPEND_SENTINEL_PRICING_ENDPOINT_REGION` (no CLI flag — keeps the surface minimal). Flag for Trey if a flag is preferred.
- A10 (location filter): We filter by `location` name via a static region-code map per the ratified design direction. The API also supports a `regionCode` product attribute for EC2/RDS which would remove the map; noted as a possible simplification for later, not taken now because its coverage across ServiceCodes is uneven and the static map is trivially testable.
- A11 (exit codes on degradation): Live-pricing degradation never yields exit 2, unlike drift read errors (v1 R12). Justification: snapshot fallback produces a complete, v1-quality verdict — the run's contract is fully met — whereas a drift read error leaves a requested check unperformed. CI gating semantics therefore stay identical with and without the flag. Flag for Trey since it deliberately diverges from R12's precedent.
- A12 (boto3 missing under an explicit flag): `--live-pricing` without the `[aws]` extra degrades with a warning rather than exiting 2, per the owner constraint "never fail the run because live pricing is unavailable". If Trey prefers fail-fast on an explicit misconfiguration, it is a two-line change in `cli.py`; the spec takes the constraint literally.
- A13 (price date): "The API's price date" is reported as the `publicationDate` of accepted price-list items (range across keys). The Pricing API exposes no per-rate effective date beyond this; no wall-clock retrieval timestamp is recorded, trading a nicety for output determinism in tests.
- A14 (price-key parsing): `aws_db_instance.instance` price keys arrive as `"<engine>:<instance_class>"` (the format `core/cost.py` already emits); the adapter splits on the first `:`. A key that does not contain `:` is treated as `unmapped_value`. This couples the adapter to the existing key format, which is stable since it is also the snapshot's key format.
- A15 (rate agreement not required): Live and snapshot rates may legitimately differ; the tool never warns on divergence. Verdict consumers judge freshness from `meta` (snapshot version/date vs. live publication dates), consistent with v1 assumption A3.
- A16 (budget scope): The 30s budget covers Pricing API time only, not total runtime; a plan with many unique keys under a slow-but-not-timing-out API can approach ~30s of added latency in the worst case. Accepted: opt-in flag, bounded, and typical plans have well under 15 unique keys.
