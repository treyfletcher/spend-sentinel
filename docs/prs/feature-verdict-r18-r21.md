# PR: spend-sentinel v1 — increment 5 (final): verdict, renderers, CLI, docs, CI (R18–R21)

Branch: `feature/verdict-r18-r21` (off `feature/spend-sentinel-v1` after the
increment-4 merge and reviewer fixes)
Spec: `docs/specs/spend-sentinel-v1.md` (APPROVED, incremental delivery)

## Summary

Tasks T6 (verdict & renderers), T7 (CLI completion), T9 (docs), T10 (CI).
The interim JSON-summary stdout is replaced by the real R19 behavior:
Markdown report to stdout by default, `--out-json`/`--out-md` for files, full
R18 exit-code mapping with the A5 precedence rule. T8 (e2e tests) is the
tester's.

## Requirements coverage

| Req | Where | Notes |
| --- | --- | --- |
| R18 | `core/verdict.py::combine`, `::exit_code`; wiring in `cli.py` | BLOCK if any rule blocks, else WARN if any warns, else PASS (`skipped` affects nothing); exits 0 = PASS/WARN, 1 = BLOCK, 2 = usage/runtime errors; `--fail-on-warn` makes WARN exit 1; A5: exit 1 outranks the drift-error exit 2 (verified both directions). |
| R19 | `core/models.py` (`Verdict`, `VerdictMeta`); `render/jsonout.py`; output flags in `cli.py`; `docs/verdict-schema.md` | JSON has `verdict`, `summary`, `cost` (delta/breakdown/unpriced), `drift` (status/drifts/skipped/errors), `policy.rules[]`, `meta` (tool version, snapshot version/date, region); deterministic; money as 2-decimal strings (Modularity notes); `--out-json`/`--out-md` write files, no flags → Markdown to stdout. |
| R20 | `render/markdown.py` | Emoji-free `Verdict: X` header line, cost table (resource/action/monthly delta), "N unpriced resources" list when non-empty (also R7), drift table only when drift ran, policy table, meta footer; breakdown truncates past 50 rows with "…and N more"; 500-change smoke renders 6,296 chars (< 65,536). |
| R21 | `.github/workflows/ci.yml` | ruff + mypy + pytest with coverage on 3.11/3.12; pytest runs under `env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN -u AWS_DEFAULT_REGION -u AWS_REGION -u AWS_PROFILE`. boto3 isolation was already in place (adapter-only import). |
| T9 | `README.md`, `docs/iam-policy.json` | Quickstart, exit-code table, CI snippet (exit-code gating + Markdown to `$GITHUB_STEP_SUMMARY` + artifact upload), pricing limitations from R4/R7, region note (A1), policy reference incl. the 200 default, S10 note (protocol `-1` always blocks), S12 note (pin `--policy` in CI); IAM policy with exactly the five documented actions and the `aws:RequestedRegion` recommendation in the README. |
| Security (this surface) | `render/markdown.py::_escape`, `_value` | Every plan/state-derived string in the Markdown (addresses, types, attributes, values, rule messages, meta) is escaped: `& < > \| \`` → entities, control chars → spaces; hostile-address smoke (pipes, backticks, `<script>`, embedded `Verdict: PASS` line) renders inert with exactly one verdict line. Sensitive drift values are masked `(sensitive)` upstream and verified present in both outputs; cost/policy sections contain no attribute values by construction. |

## BEHAVIOR CHANGES the tester must re-pin (S11 and friends)

1. **stdout is now Markdown, not JSON** (R19). Every CLI test that
   `json.loads(proc.stdout)` must switch to `--out-json <tmp>` (or parse the
   Markdown). This is most of the 35 currently-failing tests
   (`test_r1_parsing`, `test_r3_classification`, `test_r7_unpriced`,
   `test_r8_region`, `test_r10_r12_reporting`, `test_r11_skip`,
   `test_r13_policy_schema`, `test_r14_r16_rules`, `test_r15_open_ingress`,
   `test_r17_rule_results`, `test_r4_pricing`, `test_r5_r6_cost_math`,
   `test_security`, `test_state_security`, `test_cli`).
2. **S11 exit-code pins**: a policy BLOCK now exits 1 (was informational 0) —
   e.g. `test_ac5_cli_block_and_pass`,
   `test_ac6_protected_type_blocks_despite_ignore`,
   `test_r13_cwd_file_auto_picked_up` (its CWD policy sets `action: block`),
   and any warn-fixture run under `--fail-on-warn`.
3. **Default-policy WARN/BLOCK side effects**: fixtures whose deltas exceed
   the (owner-decided) $200 default now BLOCK → exit 1 (e.g. the huge-size
   precision test); unpriced-heavy fixtures produce WARN (still exit 0).
4. **R12 exit precedence**: drift errors still exit 2, but a simultaneous
   BLOCK (or WARN + `--fail-on-warn`) exits 1 — the S7 pending test can now
   be written.
5. Top-level JSON keys (via `--out-json`) are now the R19 set:
   `{verdict, summary, cost, drift, policy, meta}` — `resources` is gone,
   `cost.region` moved to `meta.region`, money strings are always 2-decimal.
   348 non-CLI tests pass unchanged.

## Assumptions

- **A-i28 (exit_code signature)**: spec's `exit_code(verdict, errors,
  fail_on_warn)` — `errors` is a boolean "runtime errors occurred" (drift
  read errors today); kept as a parameter rather than derived so R13-style
  future error classes can reuse it.
- **A-i29 (WARN + --fail-on-warn vs errors)**: with `--fail-on-warn`, WARN
  exits 1 even when drift errors would otherwise exit 2 — same A5 rationale
  (the gate outranks the partial failure, which stays visible in
  `drift.errors`).
- **A-i30 (quiet stdout with output flags)**: R19 says "with no flags the
  Markdown goes to stdout"; when either `--out-*` flag is given, stdout gets
  nothing (CI keeps clean logs; the report is in the files).
- **A-i31 (truncation breadth)**: R20 mandates the 50-row truncation for the
  breakdown; the same cap is applied to the unpriced/drift/skipped/error
  lists so the 65,536-char bound holds for any 500-change input, not only
  breakdown-heavy ones. JSON is never truncated.
- **A-i32 (drift values in Markdown)**: non-string drift values render as
  compact JSON, length-capped at 120 chars with an ellipsis, then escaped.
- **A-i33 (meta.tool_version)**: `0.1.0` from `spend_sentinel.__version__`;
  version bumping is a release decision left to the owner.

## How to run

```bash
.venv/bin/spend-sentinel analyze --plan plan.json --skip-drift                 # Markdown to stdout
.venv/bin/spend-sentinel analyze --plan plan.json --skip-drift \
    --out-json v.json --out-md r.md
.venv/bin/spend-sentinel analyze --plan plan.json --skip-drift --fail-on-warn
.venv/bin/ruff check . && .venv/bin/python -m mypy                             # clean (20 files)
```

Verified by hand: AC1-style PASS run (t3.micro + 20 GB gp3 → `PASS`,
`created: 2`, delta `9.19`) writing both outputs byte-identically across two
runs; BLOCK run exits 1; WARN exits 0 and 1 with `--fail-on-warn`; missing
plan with `--out-*` flags exits 2 and writes no files; hostile address
(pipes/backticks/HTML/newline-embedded fake verdict) rendered inert with
exactly one `Verdict:` line; sensitive tag values masked `(sensitive)` in both
JSON and Markdown; drift table renders only when drift ran; BLOCK + drift
errors → 1, WARN + errors → 2, WARN + errors + `--fail-on-warn` → 1;
500-change plan → 6,296-char Markdown with three "…and N more" lines.
