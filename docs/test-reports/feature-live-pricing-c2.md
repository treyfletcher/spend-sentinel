# Test report — feature/live-pricing, chunk 2 (LivePricingSource + attribution)

Tester: tester-agent
Date: 2026-08-28
Scope: chunk-2 surface only — `LivePricingSource` (R24/R27/R28/R29),
`CostLine.price_source` + the `drain_lookups` hook in `core/cost.py`, and the
`LivePricingReport` model with A-c6..A-c9 semantics. CLI/renderer wiring is
chunk 3 and was not tested. All offline via `FixturePricingClient`; expected
fallback rates computed from the bundled snapshot.

## Result summary

- Full suite (v1 + c1 + c2): **638 tests — 635 passed, 1 skipped, 2 xfailed**
  (`strict=True`: BUG-5 from c1, BUG-6 new). ruff clean; `python3 -m mypy`
  clean.
- **One new bug (BUG-6, medium)** — the exact hardening hole flagged in the
  c1 report's observations, now a contract violation in chunk-2 code.
- Everything else held, including all four chunk-2 assumptions A-c6..A-c9 and
  the R22 promise that the snapshot path is byte-untouched (v1 goldens and
  AC11/AC13 determinism tests pass unchanged; the current JSON renderer emits
  no `price_source` key).

## Coverage table

| Requirement / assumption | Tests | Result |
| --- | --- | --- |
| R24 live rate wins on valid extraction; flows through `estimate()` to the cent with `price_source: "live"` | `TestProtocolAndFallback` (first 2 tests) | PASS |
| R24 per-key fallback for every key-level reason (no_match, ambiguous, parse_error, oversize_response, api_error, timeout, pagination_overflow) → snapshot rate, degraded status, `(reason, service/key)` warning | `test_r24_per_key_fallback_for_every_key_level_reason` (7-way) | PASS |
| R24 unmapped_value per-key; both-miss → `None` → v1 `unknown_price_key` taxonomy through `estimate()` unchanged | `test_r24_unmapped_value_falls_back_per_key`, `test_r24_both_miss_returns_none_for_r7_taxonomy` | PASS |
| R24 all-fallback run numerically identical to snapshot-only estimate | `test_r24_snapshot_identical_behavior_for_fallback_keys` | PASS |
| R27 run-level: unsupported region on the first lookup disables the API for the rest of the run — zero transport calls afterwards; status `unavailable`, empty-detail warning | `TestRunLevelDisable::test_r27_unsupported_region_disables_rest_of_run_no_transport_calls` | PASS |
| R27 disabled construction (`client=None` + boto3_missing/client_init_error; default reason) → snapshot-only, single run-level warning recorded once | rest of `TestRunLevelDisable` (4 tests) | PASS |
| R28 through the source: injected clock (2 × 16 s calls → third key `budget_exhausted`, key-level → `degraded` not `unavailable`); custom `budget_seconds` | `TestBudgetThroughSource` | PASS |
| R28 cache interplay: two resources sharing a key → 1 transport call, both attributed `live`, `lookups_live == 2`; cached failure warns once, counts per call | `TestCacheInterplay` | PASS |
| R29/AC16: RDS `mixed` (live Single-AZ hourly × 2 × 730 + snapshot storage, exact cents); `deploymentOption=Single-AZ` asserted on the recorded call; all-live RDS → `live` | `TestAttributionR29` (first 3 tests) | PASS |
| R29 isolation: per-resource sources independent; unpriced attempts and stale pre-estimate lookups never leak into the next line; `drain_lookups` returns-and-clears | `TestAttributionR29` (last 4 tests) | PASS |
| R22 snapshot path untouched: `price_source is None` on snapshot runs, `SnapshotPricingSource` has no `drain_lookups`, current JSON renderer output contains no `price_source`; v1 goldens/AC11 tests pass unchanged | `TestSnapshotPathUntouched` + pre-existing golden/determinism suites | PASS |
| A-c7 status: `ok` only with zero warnings and zero fallback/miss; any fallback or both-source miss → `degraded`; run-level → `unavailable` | `TestReportSemantics` (3 tests) + `TestRunLevelDisable` | PASS |
| A-c6 publication_dates: (earliest, latest) pair across keys, equal pair for one date, `None` with no accepted live rate | `TestReportSemantics` (3 tests) | PASS |
| A-c8 warning de-dup per (reason, key), insertion-ordered; run-level warnings once with empty detail | `test_a_c8_warnings_deduped_per_reason_and_key`, `test_r27_run_level_warning_recorded_once` | PASS |
| A-c9 counters count `get_rate` resolutions, not unique keys (4 lookups / 1 call) | `test_a_c9_counters_count_get_rate_calls_not_keys` | PASS |
| R31 warning details are internal keys only — hostile response content never reaches the report | `test_r31_warning_details_are_internal_keys_only` | PASS |
| Degradation containment for typed transport errors through `estimate()` | `test_typed_transport_errors_never_reach_estimate` | PASS |
| Untyped transport exception containment | `test_bug6_untyped_transport_exception_degrades_not_raises` | **XFAIL — BUG-6** |
| R32 default-path purity (snapshot-only analyze imports no live/boto3 modules) | pre-existing c1 subprocess proofs, still green | PASS |

## Bugs found

### BUG-6: an untyped transport exception escapes `LivePricingSource.get_rate` and kills `estimate()`

- Severity: medium (violates R27's "degradation never fails the run" and the
  method's own "never raises" docstring; turns a client bug into a crashed
  CI run instead of a snapshot fallback).
- Where: `spend_sentinel/pricing/live.py` — `resolve_live_rate` catches only
  the three typed errors (`UnmappableKeyError`, `ExtractionError`,
  `PricingApiError`); `cached_resolve` and `get_rate` add no further
  containment. Any other exception from a `PricingApiClient` implementation
  propagates through `get_rate` → `core/cost.py::_rate` → out of
  `estimate()` uncaught.
- Repro (offline):
  ```python
  class RogueClient:
      def get_products(self, service_code, filters, next_token):
          raise ValueError("untyped transport explosion")

  estimate(plan, LivePricingSource(RogueClient(), SnapshotPricingSource()),
           "us-east-1")
  # -> ValueError propagates; the run dies instead of degrading
  ```
- Reachability: both shipped clients nominally honor the typed protocol, but
  the design is explicitly defense-in-depth ("the account, proxy, or endpoint
  override could be hostile", R31) and `Boto3AwsReader`'s drift counterpart
  deliberately catches `Exception` for the same reason (v1 A-i18). A botocore
  edge case or future client bug outside `(BotoCoreError, ClientError)` would
  crash live-pricing runs — precisely the class R27 promises away. This was
  flagged as a hardening observation in the c1 report; chunk 2 makes it a
  contract violation because `get_rate`'s docstring now promises "never
  raises".
- Suggested fix (coder's call): a defensive `except Exception` in
  `resolve_live_rate` (or `get_rate`) mapping to `api_error`, mirroring
  drift's A-i18 posture; the warning detail stays the internal key.
- Test: `tests/test_live_source.py::TestNeverRaisesThroughEstimate::
  test_bug6_untyped_transport_exception_degrades_not_raises`
  (`xfail(strict=True)`) — asserts the run completes with the snapshot rate
  and `price_source: "snapshot"`; flips to a failure once fixed.

No other bugs. BUG-5 (c1, endpoint regex trailing newline) remains open and
xfailed.

## Observations / notes

- A-c7's reading of "ok when every priced lookup was live" (both-source miss
  also degrades) is the strict and sensible interpretation — verified; no
  spec change needed.
- `get_rate` pre-checks the region and short-circuits before the transport,
  so `unsupported_region` never consumes budget or transport calls — nice
  property, pinned by the zero-calls assertion.
- For chunk 3: the fallback path after a run-level disable still records
  `snapshot` lookups and counts fallbacks/misses, so `unavailable` runs will
  show `lookups.snapshot_fallback == N` — consistent with AC19's "all entries
  snapshot"; no issue, just noting the expected numbers for the e2e
  assertions.

## Untestable in this chunk

- R22/R30 CLI flag, meta serialization, Markdown Source column, stderr
  warning lines, AC13–AC15/AC18–AC19 end-to-end: chunk-3 wiring does not
  exist yet. The model-level halves (report object, price_source field,
  determinism of the snapshot path) are covered above.

## How to run

```bash
cd /home/claude/spend-sentinel
python3 -m pytest        # 635 passed, 1 skipped, 2 xfailed
ruff check tests/        # clean
python3 -m mypy          # clean
```
