# Test report — feature/spend-sentinel-v1, increment 3 (R9–R12)

Tester: tester-agent
Date: 2026-08-27
Scope: R9–R12 (drift detection), the security surface it adds (state-file
ingestion, sensitive_values masking, error-summary sanitization), the
Boto3AwsReader adapter (stubbed, offline), and suite maintenance after the
`drift` output section landed. Policy/verdict/renderers (R13–R21 remainder)
are still unimplemented and untested.

## Result summary

- Full suite (increments 1+2+3): **284 tests — 282 passed, 1 skipped,
  1 xfailed** (`strict=True`: BUG-4).
- BUG-2 and BUG-3 (increment-2 report) were fixed by the reviewer's commits
  riding this merge (145bbe9, 171fe05); the reviewer converted my strict-xfail
  tests into regular regression tests (assertions strengthened, incl. a new
  `allocated_storage` non-finite case) — verified green; I only refreshed the
  stale module docstring.
- Whole suite runs with boto3 genuinely uninstalled — every CLI test doubles
  as an R21 witness; boto3-dependent negative tests skip themselves if boto3
  is ever present.
- All drift tests are offline: FixtureAwsReader (in-memory and the committed
  `from_path` fixture), hand-rolled raising/never-call reader stubs, and a
  sys.modules-stubbed boto3 for the adapter.

## Coverage table

| Requirement / assumption | Tests | Result |
| --- | --- | --- |
| R9 aws_instance: instance_type + tags compared; other attrs ignored (allowlist) | `test_r9_comparators.py::TestInstanceComparator` (5 tests) | PASS |
| R9 aws_security_group: order-insensitive rule sets — reorder + AWS-style CIDR regrouping = no drift; extra/removed/modified rule = drift; ingress/egress independent; IPv4+IPv6 | `TestSecurityGroupComparator` (6 tests) | PASS |
| R9 aws_s3_bucket: tags + versioning status; A-i16 normalization (v3 block list, map form, absent) | `TestBucketComparator` (9 tests incl. parametrized) | PASS |
| R9 all live reads via AwsReader protocol | every drift test injects a fake; protocol re-export in `adapters/aws_reader.py` exercised via FixtureAwsReader import path | PASS |
| A-i17 managed-only, child modules walked, empty state valid | `TestStateScope`, `test_load_state_empty_state_is_valid` | PASS |
| AC7-flavored end-to-end fixture (resize drift, regrouped-SG no-drift, child-module versioning drift, missing bucket, unsupported skip, error) | `TestMixedFixtureScenario` (6 tests) over `tests/fixtures/states/state_mixed.json` + `tests/fixtures/aws_responses/live_mixed.json` | PASS |
| R10 missing kind for all three supported types | `test_r10_r12_reporting.py::TestMissingKindR10` (3 param) | PASS |
| R10 unsupported types → `drift.skipped` reason `unsupported_type` | `test_r10_unsupported_types_skipped_with_reason` | PASS |
| R10 drift records carry address, attribute path, state value, live value | `test_r10_drift_record_is_complete` + comparator assertions | PASS |
| A-i14 supported resource with no usable id → error, not silent skip | `test_r10_missing_id_is_error_not_missing_a_i14` | PASS |
| R10/A-i19 deterministic rule-set rendering | `TestRuleRenderingDeterminism` | **XFAIL — BUG-4** |
| R11 skipped status: no `--state`, `--skip-drift`, flag beats broken state path (state not even read) | `test_r11_skip.py::TestSkippedStatus` (4 tests) | PASS |
| R11 no-AWS-call guarantee: raising reader factory never invoked; subprocess proof boto3/adapter absent from sys.modules on both skip variants | `TestNoAwsCallPath` (3 tests) | PASS |
| R21 (this slice) skip path with boto3 uninstalled; `--state` without boto3 → one-line exit 2 naming boto3 and `--skip-drift` | `TestWithoutBoto3` (3 tests, skipif boto3 installed) | PASS |
| R12 one error never kills the run; all-error runs still report; A-i18 arbitrary exception types | `TestErrorsR12Core` (3 tests) | PASS |
| R12 exception summaries: type+message, single line, control chars stripped, 200-char cap | `TestExceptionSummary` (4 tests) | PASS |
| R12 CLI: errors → exit 2 with the JSON report still on stdout; error+clean-drift → 2; no errors → 0 | `TestErrorsR12Cli` (3 tests, reader injected via monkeypatched factory) | PASS |
| Security: state file 50 MB cap, missing/malformed/deep/hostile JSON fail closed exit 2 one-line, no content echo | `test_state_security.py::TestStateFailClosed` (11 tests) | PASS |
| Security: sensitive_values masks both state and live sides; per-attribute granularity; A-i15 whole-attr over-masking; empty mirror not a mark; secret never reaches CLI output | `TestSensitiveMasking` (5 tests) | PASS |
| Security: hostile reader error messages cannot inject lines/ANSI into the report | `TestErrorSummarySanitization` | PASS |
| Boto3AwsReader: response mapping (instances, SG rules incl. IPv6, bucket versioning/tags), NotFound→None vs propagation, NoSuchTagSet→{} | `test_boto3_reader.py` (13 tests over stubbed boto3) | PASS |
| Security: adapter API surface within the documented five IAM actions (exactly four used) | `TestApiSurface::test_security_api_surface_within_documented_actions` | PASS |
| Maintenance: CLI top-level keys now {summary, resources, cost, drift}; drift skipped by default | `test_cli.py` (updated) | PASS |

## Bugs found

### BUG-4: security-group rule rendering is nondeterministic across processes when atomic rules tie on the sort key

- Severity: low (output determinism, no correctness/security impact on the
  comparison itself — the drift *decision* is set-based and unaffected).
- Where: `spend_sentinel/core/drift.py::_render_rules` — the sort key
  `(r[0], r[1] or -1, r[3], r[4])` skips index 2 (`to_port`). Two atomic rules
  differing only in `to_port` (e.g. `tcp 80-80 10.0.0.0/8` and
  `tcp 80-8080 10.0.0.0/8`) compare equal, so their relative order falls back
  to the iteration order of a set of strings — which is `PYTHONHASHSEED`-
  dependent and therefore varies across interpreter processes.
- Repro (deterministic):
  ```bash
  for seed in 0 5; do PYTHONHASHSEED=$seed python3 -c "
  from spend_sentinel.core.drift import _render_rules, _atomic_rules
  rules=[{'protocol':'tcp','from_port':80,'to_port':80,'cidr_blocks':['10.0.0.0/8']},
         {'protocol':'tcp','from_port':80,'to_port':8080,'cidr_blocks':['10.0.0.0/8']}]
  print(_render_rules(_atomic_rules(rules)))"; done
  # seed 0: ['tcp:80-80:...', 'tcp:80-8080:...']; seed 5: reversed
  ```
- Why it matters: A-i19 promises "deterministic, JSON-friendly" rendering, and
  AC11 requires byte-identical outputs across runs; two CI runs of the same
  drift can differ, breaking output diffing/caching and future golden files.
- Suggested fix (coder's call): include `to_port` (and the family, already
  index 3) in the sort key — e.g. `sorted(rules)` over the full tuple with
  None mapped to -1.
- Test: `tests/test_r10_r12_reporting.py::TestRuleRenderingDeterminism::
  test_r10_rule_rendering_stable_across_hash_seeds` (`xfail(strict=True)`,
  seeds 0 vs 5 chosen because they provably order these tuples differently).

No other implementation bugs found. Verified as documented: A-i13 (protocol
defined in core, re-exported by adapters — structural typing confirmed by
hand-rolled fakes), A-i14–A-i18, A-i20, and the BUG-2/BUG-3 fixes.

## Spec ambiguities / notes for pm-planner

(S1–S6 from earlier reports remain logged; owner deferred.)

- S7 (R12 exit-code wording): R12 says drift-error exit is "2 unless a policy
  rule already yields BLOCK". With no policy engine yet, the coder exits 2
  unconditionally on drift errors and defers the precedence to R18 — correct
  for this increment, but the R18 increment must remember to add the
  precedence test (AC9's second half). Tracking here so it is not lost.
- S8 (A-i15 over-masking): masking the whole `tags` attribute when a single
  tag key is sensitive also hides *which* non-sensitive tags drifted. Safe
  direction, spec-compatible; worth a doc note in T9 so users are not
  surprised.
- S9 (missing + sensitive): a `missing` drift for a resource with sensitive
  attributes reports no values at all (nothing to mask) — consistent, no
  action needed; noted for the R19 schema doc.

## Untestable in this increment

- Drift policy rules evaluating to `skipped` (R11's last clause) and the
  exit-1-beats-2 precedence (R12/A-5): policy engine does not exist yet.
- AC9's "same run also breaches the cost limit → exit 1": needs R14/R18.
- Live boto3 behavior beyond the stubbed contract (no network in tests, by
  design and per R21).

## How to run

```bash
cd /home/claude/spend-sentinel
python3 -m pytest        # 282 passed, 1 skipped, 1 xfailed
ruff check tests/        # clean
```
