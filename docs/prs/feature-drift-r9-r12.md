# PR: spend-sentinel v1 — increment 3: drift detection (R9–R12)

Branch: `feature/drift-r9-r12` (off `feature/spend-sentinel-v1` after the
increment-2 merge and reviewer fixes)
Spec: `docs/specs/spend-sentinel-v1.md` (APPROVED, incremental delivery)

## Summary

Task T4 plus minimal CLI wiring: `analyze` gains `--state` and `--skip-drift`
and a `drift` section in its JSON output. State attributes are compared
against live AWS values through the narrow `AwsReader` interface; the boto3
adapter is the only module importing boto3 and is imported only when drift
will actually run. Policy, verdict/renderers, and the R19 schema remain out of
scope.

## Requirements coverage

| Req | Where | Notes |
| --- | --- | --- |
| R9 | `core/drift.py` (`COMPARATORS`, `_compare_instance`, `_compare_security_group`, `_compare_bucket`); `core/state.py::load_state`; attr models in `core/models.py` | Exactly the three types/allowlists: `aws_instance` `instance_type`+`tags`; `aws_security_group` ingress/egress rule sets (protocol, from_port, to_port, IPv4+IPv6 CIDRs) compared order-insensitively (rules expanded to atomic tuples, set-compared — also robust to AWS grouping CIDRs differently than state); `aws_s3_bucket` `tags` + versioning status. All live reads go through the `AwsReader` interface. |
| R10 | `core/drift.py::detect`, `_missing` | Each drift carries address, attribute path, state value, live value; a supported resource absent from AWS → kind `missing`; unsupported types → `skipped` with reason `unsupported_type`. |
| R11 | `cli.py::analyze` (+ `core/drift.py::skipped_report`) | No `--state` or `--skip-drift` → `drift.status == "skipped"`, no reader constructed, no adapter module imported, no AWS call path exercised; verified with boto3 absent from the venv. Drift policy rules evaluating to `skipped` is R13–R17 (later increment). |
| R12 | `core/drift.py::detect` (per-resource try/except), `_summarize_exception`; exit logic in `cli.py` | One resource's read failure (auth/throttle/timeout/anything) never kills the run: captured in `drift.errors` as a single-line, length-capped, control-character-sanitized `ExceptionType: message` summary; the JSON report is still produced; exit code 2 when any drift error exists. The "exit 1 (BLOCK) beats 2" precedence cannot be implemented yet — no policy rules exist — and lands with R18; noted in `cli.py`. |
| Security (this surface) | `core/state.py` (R2-pattern loading, 50 MB cap, pydantic fail-closed), `core/drift.py` (`_is_sensitive`/`SENSITIVE_PLACEHOLDER`), `adapters/boto3_reader.py` | State files get the same fail-closed contract as plans; drift values whose state attribute is marked in `sensitive_values` render as `(sensitive)` for BOTH state and live sides; error summaries never include env vars or credentials (the tool holds none); the boto3 adapter calls only ec2:DescribeInstances, ec2:DescribeSecurityGroups, s3:GetBucketVersioning, s3:GetBucketTagging (s3:GetBucketLocation is in the allowed list but not needed). |

## CLI change

`spend-sentinel analyze --plan <p> [--region <r>] [--state <s>] [--skip-drift]`
adds to the JSON output:

```json
"drift": {
  "status": "ran",
  "drifts": [{"address": "...", "kind": "changed", "attribute": "instance_type",
               "state_value": "t3.micro", "live_value": "t3.medium"}],
  "skipped": [{"address": "...", "type": "aws_lambda_function", "reason": "unsupported_type"}],
  "errors": [{"address": "...", "error": "ClientError: ..."}]
}
```

Exit codes: 0 clean; 2 for ingestion/region errors and for runs with drift
errors (report still printed first).

## Flagged for tester

- `tests/test_cli.py::TestExitCodesAndStreams::test_cli_success_json_stdout_empty_stderr_exit_0`
  pins the top-level key set to `{summary, resources, cost}`; the new `drift`
  key fails it. All other 193 tests pass. The pinned set needs `drift` added.

## Assumptions

- **A-i13 (protocol home)**: the spec puts the `AwsReader` Protocol in
  `adapters/aws_reader.py` AND forbids `core` from importing `adapters`;
  since `core/drift.py::detect` needs the type, the protocol (and attr
  models) are *defined* in core and re-exported by `adapters/aws_reader.py`,
  which remains the import point for adapter implementations and wiring.
  Protocols are structural, so nothing else changes.
- **A-i14 (lookup ids)**: instances and SGs are looked up by state `id`;
  buckets by state `bucket`. A supported-type state resource with no usable
  id is recorded in `drift.errors` (it cannot be proven `missing`), not
  silently skipped.
- **A-i15 (sensitive granularity)**: a `sensitive_values` mark anywhere under
  an allowlisted attribute (e.g. one tag key) masks that attribute's whole
  state AND live values — over-masking is the safe direction; empty mirror
  structures (`{}`, `[{}]`) are not marks.
- **A-i16 (versioning normalization)**: state `versioning` (provider-v3 block
  list or map) and live status are both normalized to a boolean
  enabled/disabled before comparison; `mfa_delete` is not compared (R9 says
  "versioning status").
- **A-i17 (state scope)**: only `mode == "managed"` resources are compared;
  data sources in state are excluded. Child modules are walked. An empty
  state (no `values`) is valid with zero resources.
- **A-i18 (error breadth)**: R12 names auth/throttle/timeout; `detect`
  catches *any* `Exception` from a comparator/reader — a hostile or buggy
  value can't kill the run either. Flagging as security-adjacent.
- **A-i19 (SG value rendering)**: drift values for rule sets are rendered as
  sorted `protocol:from-to:cidr` strings for deterministic, JSON-friendly
  output.
- **A-i20 (state format_version)**: same `1.x` acceptance as plans; missing
  `format_version` exits 2. The `values` key is optional (empty states omit
  resources legitimately), unlike the plan's mandatory `resource_changes`
  (which R2 names explicitly).

## How to run

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"     # no boto3
# offline / CI without AWS:
.venv/bin/spend-sentinel analyze --plan plan.json --skip-drift
# with drift (needs the [aws] extra and AWS credentials via the standard chain):
pip install -e ".[aws]"
.venv/bin/spend-sentinel analyze --plan plan.json --state state.json --region us-east-1

.venv/bin/ruff check .          # clean
.venv/bin/python -m mypy        # clean (strict, 15 files)
```

Verified by hand (FixtureAwsReader + 7-resource state fixture spanning root and
a child module): instance_type drift, order-insensitive SG comparison (equal
egress sets no-drift, extra live ingress rule drifts), versioning drift,
sensitive tags masked both sides, `missing` bucket, `unsupported_type` skip,
erroring resource captured with the run continuing (exit 2, report intact;
exit 0 once the error is removed); `--skip-drift` and no-`--state` runs with
boto3 uninstalled → `status: "skipped"`, exit 0; `--state` without boto3 →
one-line exit-2 diagnostic; malformed and key-missing state files → exit-2
R2-style diagnostics.
