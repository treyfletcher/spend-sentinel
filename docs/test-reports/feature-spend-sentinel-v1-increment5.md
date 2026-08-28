# Test report — feature/spend-sentinel-v1, increment 5 (R18–R21, T8) — FINAL

Tester: tester-agent
Date: 2026-08-28
Scope: R18 (verdict/exit codes), R19 (JSON schema, output flags), R20
(Markdown renderer), R21/T10 (CI posture), T9 artifacts (IAM policy), and T8
end-to-end scenarios; plus the largest maintenance pass of the project
(stdout switched to Markdown, S11 exit-code pins flipped to the real R18
mapping). This closes v1: all 21 requirements are implemented and tested.

## Result summary

- Full suite: **457 tests — 456 passed, 1 skipped, 0 xfailed**. ruff clean.
- **No new implementation bugs found in increment 5.** The verdict logic,
  renderers, and CLI wiring survived every probe (exit-code matrix, schema
  walk, injection attempts, truncation boundaries, determinism).
- Maintenance: ~35 CLI tests re-pinned to the new contract exactly as the
  coder's PR flagged — no assertion weakened; the no-output-files-on-exit-2
  guarantee is now additionally asserted for `--out-json`/`--out-md`.
- A-i29 reviewed as instructed (WARN + `--fail-on-warn` + drift errors →
  exit 1): **I agree with the coder's reading.** `--fail-on-warn` turns WARN
  into a CI gate; A5's rationale ("the gate is actionable regardless of the
  partial failure") applies identically, and the error stays visible in
  `drift.errors`. Pinned in both the unit matrix and a CLI test.
- The skip (unreadable-file chmod test as root) disappears on CI's non-root
  runners; CI additionally scrubs AWS env vars (verified by a file-content
  test AND enforced in-suite by a session-scoped conftest guard).

## Requirement coverage (this increment)

| Requirement | Tests | Result |
| --- | --- | --- |
| R18 aggregation: any block→BLOCK, else any warn→WARN, else PASS; skipped inert | `test_r18_verdict.py::TestCombineR18` (8-row matrix) | PASS |
| R18 exit codes: 11-row matrix over (verdict × errors × fail_on_warn), incl. A5 (BLOCK beats error-2) and A-i29 | `TestExitCodeMatrix`, `TestExitCodesThroughCli` (4 CLI tests), `TestA5PrecedenceThroughCli` (3 tests — the S7 debt paid) | PASS |
| R18/AC10 exit-2 paths write no `--out-*` files (ingestion + policy errors) | `TestNoOutputFilesOnUsageErrors`, `test_t8f_malformed_plan_exit_2_no_outputs` | PASS |
| R19 JSON structure == docs/verdict-schema.md (recursive key-set/type/enum walk, maximal + minimal scenarios) | `test_r19_schema.py::assert_schema` + `TestSchemaConformance` (4 tests) | PASS |
| R19 money as 2-dp strings (incl. negative); meta carries tool version, snapshot version/date, region | `test_r19_negative_money_is_two_decimal_string`, `test_r19_meta_provenance` | PASS |
| R19 output flags: Markdown to stdout with no flags; quiet stdout with flags (A-i30); both files; unwritable path exit 2 | `TestOutputFlagBehavior`, `test_cli.py`, `test_t8_default_output_is_pr_ready_markdown` | PASS |
| R20 golden files per verdict level (snapshot-independent inputs) | `test_r20_markdown.py::TestGoldenFiles` + `tests/fixtures/golden/{pass_small,warn_mixed,block_full}.md` | PASS |
| R20 emoji-free header; drift table only when ran; unpriced line only when non-empty; policy table always; `(sensitive)` in MD | `TestHeaderAndSections` (5 tests) | PASS |
| R20/Security escaping: pipes, backticks, HTML, ampersands; embedded fake verdict line; hostile tag values stay one table row | `TestEscaping` (5 tests) | PASS |
| R20 truncation: 50-row boundary (49/50/51 behavior), "…and N more"; AC11 500-change plan < 65,536 chars via the CLI; JSON never truncates (A-i31) | `TestTruncation` (3 tests), `TestJsonNeverTruncates` | PASS |
| R21 suite runs with AWS env scrubbed (conftest session guard + in-suite assertion); package import pulls no boto3; ci.yml `env -u`s all six vars and runs ruff/mypy/pytest+cov on 3.11/3.12 | `test_r21_ci_posture.py` (4 tests) + guard in `conftest.py` | PASS |
| Security: docs/iam-policy.json is exactly the five documented read-only actions; matches the boto3 reader surface | `TestIamPolicyDoc` (2 tests) | PASS |
| T8 scenarios (a)–(g) | `test_e2e_t8.py` — see AC table | PASS |

## Acceptance criteria coverage (AC1–AC12, whole project)

| AC | Where tested | Status |
| --- | --- | --- |
| AC1 (small create → PASS, created==2, exact cents) | `test_e2e_t8.py::test_ac1_small_create_pass_exit_0_exact_cents` (spec-named `create_small.json`) | COVERED |
| AC2 (breach $200 → BLOCK exit 1, delta in message) | `test_ac2_breach_200_blocks_exit_1_with_delta_in_message` (`create_expensive.json`) | COVERED |
| AC3 (resize delta = cost(xlarge) − cost(large)) | `test_ac3_update_resize_breakdown_equals_rate_difference` + core-level `test_r6_update_resize_is_after_minus_before` | COVERED |
| AC4 (2 unpriced with both reasons; Markdown "2 unpriced resources" line) | `test_r7_ac4_*` (JSON) + `TestAc4MarkdownHalf` (Markdown line) | COVERED |
| AC5 (port-22 block naming address+port; 443-only pass) | `test_r15_open_ingress.py::TestAc5Matrix` + `TestAc5ThroughCli` (exit 1) + `test_t8c_sg_open_port_22_blocks_exit_1` | COVERED |
| AC6 (deletions warn→0, fail-on-warn→1, protected→BLOCK) | `TestAc6ThroughCli` (3 tests) + `TestScenarioD_Deletions` + R16 unit matrix | COVERED |
| AC7 (extra SG rule → one drift; absent bucket → missing) | `test_r9_comparators.py::TestMixedFixtureScenario` + `test_r9_sg_extra_live_rule_drifts` + `TestMissingKindR10` | COVERED |
| AC8 (skip-drift: raising stub untouched, status skipped, drift rule skipped, boto3-free) | `test_r11_skip.py` (reader-factory raise, sys.modules subprocess proof, boto3-absent runs) + `test_r17_drift_rule_skipped_when_drift_did_not_run` + `test_t8g` | COVERED |
| AC9 (error → drift.errors + exit 2; with cost breach → exit 1) | `TestErrorsR12Cli` + `TestA5PrecedenceThroughCli::test_block_plus_drift_errors_exits_1` | COVERED |
| AC10 (missing plan → 2 naming path; unknown rule max_cpu → 2 naming it; no output files) | `test_r2_errors.py::TestMissingAndUnreadable` + `TestAc10PolicyHalf` + `TestNoOutputFilesOnUsageErrors` | COVERED |
| AC11 (byte-identical outputs; schema match; 500-change MD < 65,536 with truncation) | `TestDeterminismAc11`, `assert_schema`, `test_ac11_500_change_plan_markdown_under_65536_chars` | COVERED |
| AC12 (plan-constant eu-west-1 used; unresolvable → exit 2 telling `--region`) | `test_r8_region.py::TestRegionThroughCliAc12` | COVERED |

All 12 ACs are covered by automated tests. All spec T8 scenarios (a)–(g) run
through the CLI entry point.

## Bugs found

None in increment 5. Across the project, four bugs were found and all four
were fixed and converted to passing regression tests:

- BUG-1 (increment 1, medium): deeply nested JSON → RecursionError traceback,
  exit 1. Fixed b39ee7f.
- BUG-2 (increment 2, medium): NaN/Infinity sizes → InvalidOperation
  traceback. Fixed 145bbe9.
- BUG-3 (increment 2, low-med): negative sizes priced to cost-masking
  negative deltas. Fixed 171fe05.
- BUG-4 (increment 3, low): SG rule rendering hash-seed nondeterminism.
  Fixed f893a0b.

Cosmetic observations (no action required): negative deltas render as
`$-24.60` in the Markdown header line, and the unpriced line reads
"1 unpriced resources" for a single entry — both match the spec's literal
wording and are pinned by goldens; a wording polish would be a golden update.

## Spec notes for pm-planner (final status)

- Resolved during the project: A-i21/S4 (owner decisions), S7 (precedence now
  tested), S11 (exit pins flipped as planned).
- Still open, deferred by owner: S1 (Terraform `forget` action hard-fails
  plans from TF ≥ 1.7), S2 (A-i5 diagnostic-content compromise awaiting
  ratification), S3 (superseded — the interim JSON is gone; the R19 schema is
  now the contract), S5/S10/S12 (documented in the README per T9 — consider
  closing), S6 (A-i10 provider-precedence wording), S8/S9 (doc notes).
- New, minor: A-i29's "fail-on-warn outranks error-2" is sensible but is an
  extension of A5 the spec text doesn't state; one sentence in R18 would
  close it.

## Overall v1 quality summary

- 457 automated tests, all green; line coverage of `spend_sentinel/` ~98%
  across increments; ruff and mypy (strict) clean per the coder/reviewer.
- Fully offline and deterministic: no network, no AWS credentials (enforced
  in-suite and in CI), boto3 optional and provably unimported on skip paths,
  byte-identical outputs pinned.
- Security posture is strong and regression-tested: 50 MB caps and fail-closed
  parsing on all three untrusted inputs (plan/state/policy), yaml.safe_load
  with no code execution, no content echo in diagnostics, Markdown injection
  neutralized, sensitive values masked in both outputs, IAM surface pinned.
- The four bugs found were all in hostile-input edge handling, all fixed
  promptly by the coder/reviewer, and all now carry permanent regression
  tests. I consider the branch releasable as v1.

## How to run

```bash
cd /home/claude/spend-sentinel
python3 -m pytest        # 456 passed, 1 skipped
ruff check tests/        # clean
```
