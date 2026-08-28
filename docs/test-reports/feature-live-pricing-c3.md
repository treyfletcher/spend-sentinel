# Test report — feature/live-pricing, chunk 3 (CLI surface + renderers + docs) — FINAL v1.1

Tester: tester-agent
Date: 2026-08-28
Scope: the `--live-pricing` CLI wiring, renderer additions (JSON
`meta.live_pricing` + `price_source`, Markdown summary line + Source column),
stderr degradation warnings, docs/IAM artifacts, and the coder's BUG-5 fix.
This closes v1.1: R22–R33 are implemented and tested across the three chunks.

## Result summary

- Full suite (v1 + v1.1): **662 tests — 660 passed, 1 skipped, 1 xfailed**.
  ruff clean; `python3 -m mypy` clean.
- **No new bugs found in chunk 3.** The CLI surface, renderers, and wiring
  held under every probe (byte-identity, exit-code parity, hostile
  publication dates, response-fragment sweeps, invalid endpoint env).
- BUG-5 verified fixed (`fullmatch`); its xfail removed — the test now pins
  the rejected-before-boto3 behavior as a regression.
- BUG-6 (c2 report: untyped transport exception escapes `estimate()`) is
  still open and remains a strict xfail; the reviewer picks it up next. It
  does not affect the shipped clients, which raise only typed errors.
- The skip is the v1 chmod-as-root test (gone on CI's non-root runners).

## AC coverage table (AC13–AC21, whole v1.1)

| AC | Where tested | Status |
| --- | --- | --- |
| AC13 (no-flag byte identity; AC11 still holds) | `test_live_e2e_c3.py::TestAc13DefaultPathUnchanged` (no live fields in JSON/MD, double-run identity) + all v1 golden/determinism suites passing unchanged + c2's `TestSnapshotPathUntouched` | COVERED |
| AC14 (all-live run: sources, cents from fixture rates, status ok, lookups.live==2, date span, MD Source column + summary) | `TestAc14AllLive` (JSON + Markdown + determinism) | COVERED |
| AC15 (one key no_match → snapshot fallback entry, degraded, key-only warning, single stderr line, exit parity) | `TestAc15PartialFallback` (3 tests) | COVERED |
| AC16 (Multi-AZ RDS = 2 × live Single-AZ × 730; Single-AZ/PostgreSQL filters verbatim on the recorded call) | c2 `TestAttributionR29` (core, filters on the wire) + `TestAc16MixedRdsThroughCli` (e2e incl. `mixed` in MD) | COVERED |
| AC17 (NAT/NLB hourly-dimension selection; two-rate ambiguous fallback) | c1 `test_ac17_nat_hourly_dimension_selected_over_gb`, `test_ac17_nlb_lcu_dimension_excluded`, `test_ac17_two_distinct_rates_ambiguous`; fallback-through-source in c2 | COVERED |
| AC18 (fake clock: 2 calls then budget_exhausted, verdict-driven exit; shared key → 1 query) | c1/c2 budget suites + `TestAc18BudgetThroughCli` (e2e, incl. one collapsed stderr line) | COVERED |
| AC19 (boto3 absent: run completes, all snapshot, unavailable + boto3_missing, single stderr warning, exit parity; suite creds/network-free) | `TestAc19Boto3Absent` (real wiring, no seam; lookups 0/2/0 per the c2 note) + conftest AWS-env guard | COVERED |
| AC20 (300 KiB / NaN / negative / non-JSON responses → correct reasons, no exception, zero response fragments in any output) | c1 `TestExtractionFailuresR31` + `TestAc20HostileResponsesThroughCli` (secret-marker sweep over JSON/MD/stderr) | COVERED |
| AC21 (iam-policy-pricing.json == exactly `pricing:GetProducts`; drift policy unchanged) | `TestDocsAndIamAc21` (+ v1's `TestIamPolicyDoc` still pinning the drift list) | COVERED |

All nine v1.1 acceptance criteria are covered by automated tests, each at
both the pure layer (c1/c2) and the CLI surface (c3) where applicable.

## Requirement coverage (this chunk)

| Requirement / assumption | Tests | Result |
| --- | --- | --- |
| R22 flag composes; default path byte-identical and import-pure | `TestAc13DefaultPathUnchanged` + c1 purity subprocs (still green: default analyze imports no live/boto3 modules) | PASS |
| R27/A11 exit parity flag vs no-flag (PASS/WARN/BLOCK × failing transport) | `test_a11_exit_codes_never_differ_flag_vs_no_flag` | PASS |
| R27/A-c12 stderr: one line per distinct reason, exact format, reasons only (no keys/response text), collapsing multi-key degradations | `test_ac15_single_stderr_warning_line_reasons_only`, `TestAc18BudgetThroughCli` | PASS |
| R30 JSON: `meta.live_pricing` schema-walked (exact key sets incl. lookups and earliest/latest object) on ok/degraded/unavailable; `price_source` only under the flag | `assert_live_pricing_schema` applied across `TestAc14/15/19` | PASS |
| R30 Markdown: Source column header/rows (`live`/`snapshot`/`mixed`), summary line grammar for ok/degraded/unavailable (A-c10) | `test_ac14_markdown_surface`, `test_ac15_markdown_mixed_sources`, `test_ac19_markdown_unavailable_summary_line`, `TestAc16MixedRdsThroughCli` | PASS |
| R30 determinism under the flag (no wall clock; fixture-driven runs byte-identical) | `test_r30_live_outputs_deterministic_across_runs` | PASS |
| R31 escaping of the only response-derived rendered strings (publication dates with pipes/backticks/HTML) with the verdict header unspoofable | `TestRendererEscaping` | PASS |
| A-c11 invalid endpoint env → `client_init_error`, `endpoint_region: "invalid"`, raw value never echoed in JSON/MD/stderr | `test_a_c11_invalid_endpoint_env_reported_as_invalid_never_echoed` | PASS |
| BUG-5 fix regression (trailing-newline endpoint rejected before boto3) | `test_r31_trailing_newline_endpoint_rejected` (de-xfailed) | PASS |
| R33/AC21 docs artifacts; verdict-schema.md documents live_pricing | `TestDocsAndIamAc21` | PASS |

## Bugs

- New in chunk 3: **none**.
- BUG-5 (c1): fixed by the coder in this chunk (`fullmatch`); regression
  test in place.
- BUG-6 (c2, medium): still open — untyped transport exceptions escape
  `LivePricingSource.get_rate`/`estimate()`; strict xfail
  (`test_bug6_untyped_transport_exception_degrades_not_raises`) stays in
  place for the reviewer's fix and will flip to a failure when it lands.

## Notes

- A-c10 summary-line grammar and A-c12 warning-line format were spec-open
  ("examples, not a grammar"); the implemented formats are sensible and are
  now pinned by exact-string assertions — any future wording change is a
  deliberate test update, not drift.
- AC19's `lookups.snapshot_fallback == 2` on unavailable runs (fallbacks
  still counted) matches the c2 report's prediction; asserted explicitly.
- The v1.1 e2e tests deliberately run through both output modes (Markdown
  stdout and `--out-json`) so renderer and serializer stay in lockstep.

## Overall v1.1 quality summary

- 662 automated tests across v1 + v1.1, all green except one deliberate
  strict-xfail bug marker (BUG-6, reviewer-bound); ruff + mypy (strict)
  clean.
- Offline and deterministic throughout: no network, AWS env scrubbed
  in-suite and in CI, boto3 optional and provably unimported on default
  paths, fixture-driven live runs golden-stable (no wall-clock output).
- Degradation contract verified end to end: every failure class (transport,
  parsing, budget, mapping, region, boto3 absence, hostile env) falls back
  to the snapshot without changing exit codes, with de-duplicated,
  content-free warnings; the only open gap is BUG-6's untyped-exception
  hole, which shipped clients cannot trigger.
- Security posture: response bodies never reach any output (swept with
  secret markers at the CLI level), publication dates escaped, endpoint env
  validated before boto3 (BUG-5 fixed), IAM surface exactly one new opt-in
  action. Across v1+v1.1: six bugs found by testing, five fixed and pinned,
  one awaiting the reviewer. I consider v1.1 releasable once BUG-6's
  defensive catch lands.

## How to run

```bash
cd /home/claude/spend-sentinel
python3 -m pytest        # 660 passed, 1 skipped, 1 xfailed
ruff check tests/        # clean
python3 -m mypy          # clean
```
