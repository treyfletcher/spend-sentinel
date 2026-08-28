# Test report — feature/live-pricing, chunk 1 (v1.1 foundation: T11/T13 + T12 primitives)

Tester: tester-agent
Date: 2026-08-28
Scope: the chunk-1 surface only — `pricing/live.py` (R25 filter matrix, R26
region table, R31 defensive extraction, R28 cache/budget primitives,
`cached_resolve`), `pricing/fixture_client.py`, and
`adapters/boto3_pricing.py`. `LivePricingSource`, CLI wiring, attribution,
and renderers are chunks 2–3 and were not tested. All new tests are offline:
no network, no real boto3, AWS env vars scrubbed (the v1 conftest guard is in
force in this re-cloned checkout).

## Result summary

- Full suite (v1 + chunk 1): **596 tests — 594 passed, 1 skipped, 1 xfailed**
  (`strict=True`: BUG-5). ruff clean; `python3 -m mypy` clean (23 files).
- v1 baseline verified intact on this branch before adding tests
  (458 passed, matching the coder's claim); boto3 confirmed absent from the
  environment, so every test doubles as an R21/R32 witness.
- One new bug found (BUG-5, low). Everything else held up, including all five
  chunk-1 assumptions A-c1..A-c5.
- New committed fixtures under `tests/fixtures/pricing_api/` are realistic
  raw GetProducts payloads (PriceList of JSON strings, Reserved terms
  included as decoys) and are resolved through `build_query`'s own filters in
  a guard test, so fixtures cannot silently drift from the R25 matrix.

## Coverage table

| Requirement / assumption | Tests | Result |
| --- | --- | --- |
| R26 region→location table (exact names, 3 snapshot regions); unmapped region → `unsupported_region` for all six service keys | `test_live_query.py::TestRegionLocationsR26` | PASS |
| R25 filter matrix verbatim: EC2 (6 TERM_MATCH filters), EBS (all 6 volume types), RDS instance (3 engines mapped, `deploymentOption=Single-AZ` always — the double-counting rule), RDS storage (4-value map), NAT + ELB (productFamily + usagetype-suffix/unit rules) | `TestBuildQueryMatrixR25` (18 tests incl. parametrized) | PASS |
| R25 unmappable values → `unmapped_value` (sc1, io2, unmapped engine, A14 colonless/empty keys, unknown lb/NAT keys, unsupported types); maps cover exactly the documented values | same class | PASS |
| R31 valid extraction: rate to the cent + publicationDate; Reserved terms ignored; multi-product same-rate not ambiguous; date span across products; GB-Mo unit | `TestExtractionValidR31` (6 tests) | PASS |
| AC17 groundwork: NAT hourly-vs-GB and NLB hourly-vs-LCU dimension selection (usagetype at product level and at dimension level — A-c2 both shapes) | `test_ac17_nat_*`, `test_ac17_nlb_*` | PASS |
| A-c4 unit mismatch → `no_match` (never a wrong rate) | `test_a_c4_unit_mismatch_yields_no_match`, `test_r31_wrong_suffix_usagetype_no_match` | PASS |
| AC17 ambiguous two distinct rates → `ambiguous` | `test_ac17_two_distinct_rates_ambiguous` | PASS |
| AC20/R31 hostile responses: NaN/negative/±Infinity/10^6/1e6/empty USD → `parse_error` (boundaries 999999.999999 and 0 accepted); 300 KiB entry → `oversize_response`; malformed JSON and non-object entries; `PriceList` non-list; 60k-deep nesting handled; missing-USD dimension skipped | `TestExtractionFailuresR31` (20+ tests) | PASS |
| A-c5 one bad entry fails the whole key (no cherry-picking) | `test_a_c5_one_bad_entry_fails_the_whole_key` | PASS |
| R31 product-count cap: 50 accepted, 51 → `oversize_response` | `test_r31_exactly_50_products_accepted`, `test_r31_product_count_cap_fails_closed` | PASS |
| R28/A-c1 pagination in the pure layer: NextToken chaining, 3-page cap boundary, overflow → `pagination_overflow` with calls bounded, transport errors propagate, hostile pages/tokens → `parse_error` | `TestFetchPagesR28` (7 tests) | PASS |
| R28 RunCache: one transport call per unique triple (10 lookups → 1 call, AC18 second half); negative caching; region in the key; unmappable cached with zero calls | `test_live_cache_budget.py::TestCachedResolveCaching` | PASS |
| R28 Budget: fake clock, 30 s default, exhausted at exactly the limit, custom budgets | `TestBudget` | PASS |
| AC18 groundwork: 4 keys × 16 s/call → exactly 2 queries then `budget_exhausted`; cache hits still served after exhaustion; exhausted outcomes negative-cached | `TestBudgetExhaustion` | PASS |
| `cached_resolve`/`resolve_live_rate` never raise: 10-mode failure-injection sweep + unmappable inputs, always a `LookupOutcome` with an R27-taxonomy reason | `TestNeverRaises` | PASS |
| FixturePricingClient contract (call recording, empty-page default, error injection) | `TestFixtureClientContract` | PASS |
| T13 endpoint handling: default us-east-1, env override, empty-env fallback, explicit-arg precedence; invalid values rejected before the boto3 import (works boto3-less) | `test_boto3_pricing.py::TestConstructionAndEndpoint` | PASS (one xfail, BUG-5) |
| T13/A-c3 botocore Config: 5 s connect / 10 s read, standard `max_attempts=3` | `test_r28_botocore_config_timeouts_and_retries` | PASS |
| T13 request/response: TERM_MATCH filter dicts, `MaxResults=100`, NextToken passthrough, verbatim response, non-dict response → `api_error` | `TestGetProducts` | PASS |
| T13 error translation: connect/read timeout → `timeout`, BotoCoreError/ClientError → `api_error`, message is the enum value only; `client_init_error` leaks no credential detail | `test_error_translation`, `test_client_construction_failure_is_client_init_error` | PASS |
| R33 surface: only `get_products` called (stub raises on any other method) | `test_r33_only_get_products_is_called` | PASS |
| R32/R22 purity: importing `pricing.live` or the adapter leaves boto3 unimported; a snapshot-only analyze leaves `pricing.live`/`fixture_client`/`boto3_pricing`/boto3 out of `sys.modules`; boto3-absent constructor → `boto3_missing` | `TestDefaultPathPurity`, `test_r32_boto3_missing_raises_unavailable` | PASS |
| R21 hold: whole suite creds-free (session guard) and network-free | conftest guard + boto3 absent in env | PASS |

## Bugs found

### BUG-5: endpoint-region validation accepts a trailing newline (regex `$` with `re.match`)

- Severity: low (hardening; degradation still contains the blast radius).
- Where: `spend_sentinel/adapters/boto3_pricing.py` —
  `_REGION_TOKEN = re.compile(r"^[a-z0-9-]{1,32}$")` used with `.match(...)`.
  In Python, `$` also matches just before a trailing `\n`, so
  `SPEND_SENTINEL_PRICING_ENDPOINT_REGION="eu-west-1\n"` passes validation
  and the newline-bearing value is handed to `boto3.client(region_name=...)`
  — violating R31's "validated ... before being handed to boto3" contract
  and the spec's `^[a-z0-9-]{1,32}$` intent (newline is outside the class).
- Repro (offline, deterministic — proven with a stubbed boto3 that records
  what reaches `boto3.client`):
  ```python
  os.environ["SPEND_SENTINEL_PRICING_ENDPOINT_REGION"] = "eu-west-1\n"
  Boto3PricingClient()   # constructs; region_name == "eu-west-1\n"
  ```
- Impact: in practice botocore would likely fail endpoint construction later
  (→ `client_init_error`/`api_error` degradation), so no crash — but the
  validation boundary is the point of the control, and a hostile env value
  should never reach boto3.
- Suggested fix (coder's call): `_REGION_TOKEN.fullmatch(region)` or anchor
  with `\Z` instead of `$`.
- Test: `tests/test_boto3_pricing.py::TestConstructionAndEndpoint::
  test_r31_trailing_newline_endpoint_rejected` (`xfail(strict=True)`; flips
  to XPASS-failure when fixed, prompting marker removal). The rest of the
  invalid-value matrix (uppercase, underscore, injection, overlong, leading
  control char) passes today.

No other bugs. Verified as documented: A-c1 (pure-layer pagination — the cap
also bounds transport calls), A-c2 (both usagetype homes), A-c3 (retry
arithmetic), A-c4 (unit guard), A-c5 (fail-closed mixed responses).

## Observations / notes for pm-planner

- S13 (product-count cap semantics): R31 says "at most 50 products are
  examined per key"; the implementation fails the key (`oversize_response`)
  on the 51st product rather than examining only the first 50. Fail-closed
  and arguably safer (a >50-product response means the TERM_MATCH filters
  were too loose to trust), but it is an interpretation — one clarifying
  sentence in R31 would pin it. Tested as implemented.
- Hardening observation (not a bug): `resolve_live_rate` catches the three
  typed errors (`UnmappableKeyError`, `ExtractionError`, `PricingApiError`).
  A transport client raising an untyped exception would escape the "never
  raises" contract; both real clients honor the protocol today, and chunk 2's
  wiring should keep it that way (worth a defensive catch-all when
  `LivePricingSource` lands, mirroring drift's A-i18 posture).
- The fixture-matrix guard test (`TestFixturesMatchTheMatrix`) is designed
  for chunk 2 reuse: the same fixture files can back `LivePricingSource` and
  AC14–AC17 e2e scenarios without re-deriving filters.

## Untestable in this chunk

- R22/R23 flag behavior, R24 fallback-to-snapshot semantics, R27 warning
  emission/stderr lines, R29 attribution, R30 meta/rendering, AC13–AC21
  end-to-end: `LivePricingSource` and CLI wiring do not exist yet (chunks
  2–3). The AC-numbered tests here (AC17/AC18/AC20 groundwork) cover the
  chunk-1 halves of those criteria only.
- Real botocore behavior (retry timing, endpoint resolution): out of scope by
  design (R32 network-free posture); covered at the translation boundary via
  stubs.

## How to run

```bash
cd /home/claude/spend-sentinel
python3 -m pytest        # 594 passed, 1 skipped, 1 xfailed
ruff check tests/        # clean
python3 -m mypy          # clean
```
