# Test report — feature/spend-sentinel-v1, increment 1 (R1–R3)

Tester: tester-agent
Date: 2026-08-27
Scope: R1, R2, R3 (plan ingestion + thin `analyze` CLI) and the plan-error half
of AC10 only, per the increment gating in the spec and PR description. R4–R18
are not implemented on this branch and were not tested.

## Result summary

- 81 tests collected: **79 passed, 1 skipped, 1 xfailed** (the xfail is BUG-1,
  `strict=True` so it flips to a failure when fixed).
- Skip: `test_r2_unreadable_file_exits_2` — chmod-based unreadable-file test is
  meaningless when the suite runs as root (CI as non-root will exercise it).
- Suite is offline, deterministic, order-independent; large/hostile inputs
  (50 MB sparse file, 100k-deep JSON) are generated in `tmp_path` at test time,
  never committed.
- Line coverage of `spend_sentinel/`: 98% (uncovered: the `__main__` guard in
  `cli.py` and two unreachable-in-practice OSError branches in `plan.py`).

## Coverage table

| Requirement / assumption | Tests | Result |
| --- | --- | --- |
| R1 field extraction (address, type, provider, actions, before/after) | `test_r1_parsing.py::TestLoadPlanExtraction` (7 tests) | PASS |
| R1 "single aws_instance create → changed == 1" | `test_r1_single_create_changed_count_is_1`, `TestCliR1::test_r1_cli_single_create_summary` | PASS |
| R1 format_version 1.x acceptance ("1", "1.0", "1.2") | `test_r1_format_version_1_0_and_bare_1_accepted` | PASS |
| R2 missing file → exit 2, stderr names path (AC10a) | `test_r2_missing_file_exits_2_names_path` | PASS |
| R2/AC10a no output files on error | `test_r2_missing_file_writes_no_output_files` | PASS |
| R2 unreadable / directory / bad path component | `test_r2_directory_as_plan_exits_2`, `test_r2_path_through_regular_file_exits_2`, `test_r2_unreadable_file_exits_2` | PASS (1 skipped as root) |
| R2 non-JSON content (malformed, empty, binary, wrong top-level type) | `TestNotJson` (5 tests) | PASS |
| R2 missing `format_version` / `resource_changes`, unsupported version, invalid structure | `TestMissingKeysAndStructure` (8 tests) | PASS |
| R2 one-line stderr diagnostic, empty stdout | `assert_error_contract` applied in every R2 test; `test_cli_error_one_line_stderr_empty_stdout_exit_2` | PASS |
| 50 MB cap (PR security claim): over-cap exit 2, exact-boundary accepted | `TestSizeCap` (3 tests) | PASS |
| R3 classification matrix (create/delete/update/replace) | `TestClassifyActionsUnit::test_r3_recognized_combinations` | PASS |
| R3 no-op exclusion; only-no-op plan; empty `resource_changes` (A-i6) | `test_r3_excluded_actions_return_none`, `test_r3_noop_only_plan_all_counts_zero`, `test_r3_empty_resource_changes_all_counts_zero`, CLI variants | PASS |
| A-i1 data-source `["read"]` treated as no-op | `test_r3_excluded_actions_return_none[read]`, `test_r3_cli_read_data_source_excluded` | PASS |
| A-i2 `["create","delete"]` (create-before-destroy) → replace | `test_r3_recognized_combinations`, `test_r3_replace_create_before_destroy_counted` | PASS |
| Unrecognized action combos fail closed, exit 2, name the resource | `test_r3_unrecognized_combinations_fail_closed` (9 combos), `test_r3_unknown_action_error_names_resource`, `test_r3_cli_unknown_action_exits_2_one_line` | PASS |
| R3 counts in verdict summary (created/deleted/updated/replaced/changed) | `TestSummarizePlan`, `TestCliR3` | PASS |
| CLI: exit 0/2, JSON on stdout only on success, determinism, `--version`, console script | `test_cli.py` (6 tests) | PASS |
| Security: diagnostics never echo file contents / attribute values | `TestNoContentEcho` (4 tests) | PASS |
| Security: hostile structures fail closed, no traceback | `test_hostile_structures_exit_2_no_traceback` (8 payloads) | PASS |
| Security: deeply nested JSON terminates promptly | `test_deeply_nested_json_terminates_promptly` | PASS |
| Security: deeply nested JSON fails closed (exit 2, no traceback) | `test_deeply_nested_json_fails_closed_exit_2` | **XFAIL — BUG-1** |
| Edge: unicode addresses, null before/after, unknown top-level keys, 2000-change wide plan | `test_r1_unicode_address_roundtrips`, `test_r1_null_before_after_accepted`, `test_r1_unknown_sibling_keys_ignored`, `test_huge_flat_resource_changes_handled` | PASS |

## Bugs found

### BUG-1: deeply nested JSON crashes with an uncaught RecursionError (traceback, exit 1)

- Severity: medium (security posture / R2 contract, not a data-corruption risk).
- Where: `spend_sentinel/core/plan.py::load_plan` — the `json.loads` call
  catches only `(UnicodeDecodeError, json.JSONDecodeError)`; CPython's JSON
  parser raises `RecursionError` on deeply nested input.
- Repro:
  ```bash
  python3 - <<'EOF'
  open("deep.json", "w").write(
      '{"format_version":"1.2","resource_changes":[],"x":' + "["*100000 + "]"*100000 + "}")
  EOF
  spend-sentinel analyze --plan deep.json; echo "exit=$?"
  # -> 36-line RecursionError traceback on stderr, exit=1
  ```
- Expected (spec Security: "unknown/hostile structures fail closed with exit 2,
  never partial evaluation"; R2: one-line stderr diagnostic; PR claims "no
  traceback"): exit 2, single stderr line naming the file, no traceback.
  Exit 1 is especially bad because later increments reserve 1 for policy BLOCK
  (R18) — a hostile plan could masquerade as a policy verdict in CI.
- Suggested fix (coder's call): include `RecursionError` in the except clause
  around `json.loads` (and consider the same around `Plan.model_validate`).
- Test: `tests/test_security.py::TestFailClosedNoTraceback::test_deeply_nested_json_fails_closed_exit_2`,
  marked `xfail(strict=True)` referencing this report; it will start failing
  (XPASS) the moment the bug is fixed, prompting removal of the marker.

No other implementation bugs found. All PR-stated assumptions (A-i1..A-i6)
behave as documented.

## Spec ambiguities / notes for pm-planner

- S1 (R3 vs Terraform reality): R3's action list omits `["read"]` (data
  sources), `["create","delete"]` (create-before-destroy replace), and newer
  Terraform actions such as `forget` (removed blocks, TF ≥ 1.7). The coder's
  A-i1/A-i2 handling of the first two is sensible; `forget` currently hard-fails
  the whole plan with exit 2, which will reject otherwise-valid modern plans.
  The spec should state the intended behavior for `forget`/unknown future
  actions (skip-and-count vs fail).
- S2 (R2 diagnostic content vs security): R2 requires naming "the file and the
  problem" while the security section forbids echoing contents. The coder's
  A-i5 compromise (echo field locations, resource addresses, and action strings
  but never attribute values) is reasonable but is a judgment call the spec
  should ratify — addresses/action strings are attacker-influenced in a PR
  context and do reach stderr.
- S3 (increment-1 output shape): the minimal `{"summary", "resources"}` stdout
  JSON is not the R19 verdict schema and is not covered by any requirement; if
  anything downstream starts consuming it, renaming fields in the R19 work will
  be a silent break. Fine for scaffolding — just flagging that it is
  contract-free today.

## Untestable in this increment

- AC10's policy half (unknown rule `max_cpu`) and all of R4–R18: not
  implemented on this branch by design; no tests written.
- Unreadable-file permission path when running as root (skipped, see above);
  the code path is still exercised via the bad-path-component test.

## How to run

```bash
cd /home/claude/spend-sentinel
python3 -m pytest -q            # 79 passed, 1 skipped, 1 xfailed
python3 -m pytest --cov=spend_sentinel
ruff check tests/               # clean
```
