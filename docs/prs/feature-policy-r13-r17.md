# PR: spend-sentinel v1 — increment 4: policy gates (R13–R17)

Branch: `feature/policy-r13-r17` (off `feature/spend-sentinel-v1` after the
increment-3 merge and reviewer fixes)
Spec: `docs/specs/spend-sentinel-v1.md` (APPROVED, incremental delivery)

## Summary

Task T5: the R13 policy config schema and loader, the four rule evaluators
(R14–R16), and the R17 `RuleResult` model, wired into `analyze` as `--policy`
and a `policy` section in the JSON output. **Rule results are informational in
this increment**: R18 verdict/exit-code logic is a later increment, so a
policy BLOCK does not yet change the exit code (noted in `cli.py`).

## Requirements coverage

| Req | Where | Notes |
| --- | --- | --- |
| R13 | `core/policy.py` (`Policy`, `PolicyRules`, per-rule models, `load_policy`, `_describe_validation_error`); resolution in `cli.py` | Exactly the R13 keys, `extra="forbid"` at every level; unknown top-level key / rule name / enum value / wrong type → exit 2 naming the offending key (e.g. `policy contains unknown key 'rules.max_cpu'`); `yaml.safe_load` only; 50 MB cap; `--policy` > `./spend-sentinel.yaml` if present > built-in defaults; empty/absent file = defaults, never "no rules". Defaults per spec: `treat_unpriced_as: warn`, `allowed_ports: []`, deletions `warn`, drift `warn`. |
| R14 | `core/policy.py::_eval_max_monthly_delta` | BLOCK when total delta > `limit_usd`; with unpriced resources and `treat_unpriced_as` warn/block the result is at least WARN/BLOCK even under the limit (verified); `ignore` leaves the result alone; the computed delta appears in the message (AC2 groundwork). |
| R15 | `core/policy.py::_eval_open_ingress` + helpers | Open = CIDR `0.0.0.0/0` or `::/0` only (A7: SG references and prefix lists never open) on `aws_security_group` inline ingress, `aws_security_group_rule` with `type: ingress`, and `aws_vpc_security_group_ingress_rule`, for changes that will exist after apply; exempt only when EVERY port in `from_port..to_port` is in `allowed_ports`; protocol `-1` blocks regardless; missing/invalid port ranges fail closed; message names the resource address and ports (AC5). |
| R16 | `core/policy.py::_eval_deletions` | `warn`/`block`/`ignore` with each deleted resource listed; replaces count as deletions; `protected_types` deletion → BLOCK regardless of `action` (verified with `action: ignore`). |
| R17 | `core/models.py` (`RuleOutcome`, `RuleResult`); `core/policy.py::evaluate` | Every rule appears in every evaluation with name, result `pass|warn|block|skipped`, and a message naming offending resources; the drift rule is `skipped` when drift did not run (R11) and warn/block/pass per config when it did. |
| Security (this surface) | `core/policy.py` | `yaml.safe_load` only; same 50 MB cap and fail-closed pydantic pattern as plan/state; diagnostics name keys, never values/file contents; plan-derived strings in rule messages get control characters stripped and long lists elided (consistent with the CLI stderr hardening); defaults are the safe direction. |

## CLI change

`spend-sentinel analyze ... [--policy <path>]` adds:

```json
"policy": {
  "rules": [
    {"name": "max_monthly_delta", "result": "pass", "message": "..."},
    {"name": "open_ingress", "result": "block", "message": "open ingress (0.0.0.0/0 or ::/0) on non-allowed ports: aws_security_group.app (port 22)"},
    {"name": "deletions", "result": "warn", "message": "..."},
    {"name": "drift", "result": "skipped", "message": "drift detection did not run"}
  ]
}
```

Exit codes are unchanged in this increment (informational until R18).

## Flagged for tester

- `tests/test_cli.py::TestExitCodesAndStreams::test_cli_success_json_stdout_empty_stderr_exit_0`
  pins the top-level key set (now `{summary, resources, cost, drift}`); the
  new `policy` key fails it — same pattern as the last two increments. All
  other 282 tests pass.

## Assumptions

- **A-i21 (`limit_usd` default)**: R13 shows `limit_usd: 200` as an example
  but names no default, and the built-in defaults must be safe rather than
  presumptuous; `limit_usd` is therefore optional — when unset the rule
  cannot block on the delta (message says "no limit configured") but the
  `treat_unpriced_as` escalation still applies. If the owner intended a
  default ceiling of $200, it is a one-line change.
- **A-i22 (R15 scope includes replaces)**: R15 says "created or updated"; a
  replaced SG also exists in the plan's `after` state, so create, update, and
  replace are inspected (only deletes and no-ops are exempt). Security-
  relevant reading — flagged rather than guessed narrow.
- **A-i23 (unverifiable ranges fail closed)**: an open-CIDR rule with
  missing/non-integer `from_port`/`to_port` (or `to_port < from_port`)
  blocks, since "every port in the range is allowed" cannot be proven.
- **A-i24 (`version`)**: optional, defaults to `1`; any other value is
  rejected via `Literal[1]` ("unknown value" naming `version`). Missing
  `version` in an otherwise-valid file is accepted.
- **A-i25 (drift rule inputs)**: the drift rule reacts to detected drifts
  only; R12 read errors keep their own exit-2 path and are appended to the
  rule message as a count for visibility.
- **A-i26 (`ignore` results)**: `ignore` renders as result `pass` with an
  "ignored by policy" message (R17 has no separate `ignored` outcome;
  `skipped` is reserved for "did not run" per R11).
- **A-i27 (policy size cap)**: the spec's 50 MB cap names plan/state files;
  the same cap is applied to policy YAML (coordinator instruction; one
  shared constant).

## How to run

```bash
.venv/bin/spend-sentinel analyze --plan plan.json                      # defaults or ./spend-sentinel.yaml
.venv/bin/spend-sentinel analyze --plan plan.json --policy policy.yaml
.venv/bin/ruff check . && .venv/bin/python -m mypy                     # both clean (16 files)
```

Verified by hand: AC5 matrix (port 22 open → block naming address+port;
443-only rule with `allowed_ports: [443]` → pass; range 80–81 with only 80,443
allowed → block; protocol `-1` → block; SG-reference/prefix-list rules and
deleted SGs not flagged); AC6 matrix (default warn listing delete+replace;
`action: block`; `protected_types: [aws_db_instance]` blocks even with
`action: ignore`); `treat_unpriced_as: block` escalates under-limit delta to
block; unknown rule name/enum/type/top-level key each exit 2 naming the key;
empty file and missing-file behavior; resolution order (CWD auto-pickup,
`--policy` precedence, built-in defaults); drift rule `skipped` without state
and `warn` naming drifted addresses when drift ran.
