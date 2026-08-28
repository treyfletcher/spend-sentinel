# Verdict JSON schema (R19)

Produced by `spend-sentinel analyze --out-json <path>`. The document is
deterministic: keys appear in the order below, and running the same inputs
twice yields byte-identical output.

**Monetary values are strings with exactly two decimals** (e.g. `"242.94"`,
`"-32.85"`). Costs are computed as `Decimal` end to end and rounded half-up to
cents per resource (R5); they are serialized as strings, never floats, to keep
determinism (Modularity notes).

```json
{
  "verdict": "PASS | WARN | BLOCK",
  "summary": {
    "created": 0,
    "deleted": 0,
    "updated": 0,
    "replaced": 0,
    "changed": 0
  },
  "cost": {
    "monthly_delta_usd": "0.00",
    "breakdown": [
      {
        "address": "aws_instance.web",
        "type": "aws_instance",
        "action": "create | delete | update | replace",
        "monthly_delta_usd": "7.59"
      }
    ],
    "unpriced": [
      {
        "address": "aws_lambda_function.fn",
        "type": "aws_lambda_function",
        "reason": "unsupported_type | unknown_price_key | attributes_unknown"
      }
    ]
  },
  "drift": {
    "status": "ran | skipped",
    "drifts": [
      {
        "address": "aws_instance.web",
        "kind": "changed | missing",
        "attribute": "instance_type",
        "state_value": "t3.micro",
        "live_value": "t3.medium"
      }
    ],
    "skipped": [
      {
        "address": "aws_lambda_function.fn",
        "type": "aws_lambda_function",
        "reason": "unsupported_type"
      }
    ],
    "errors": [
      {
        "address": "aws_instance.broken",
        "error": "ClientError: ..."
      }
    ]
  },
  "policy": {
    "rules": [
      {
        "name": "max_monthly_delta | open_ingress | deletions | drift",
        "result": "pass | warn | block | skipped",
        "message": "human-readable detail naming offending resources"
      }
    ]
  },
  "meta": {
    "tool_version": "0.1.0",
    "pricing_snapshot_version": "2026.08.0",
    "pricing_snapshot_date": "2026-08-27",
    "region": "us-east-1"
  }
}
```

Notes:

- `summary.changed` = created + deleted + updated + replaced; `no-op` and
  data-source `read` changes are excluded (R3).
- `drift.drifts[].kind == "missing"` marks a state resource of a supported
  type that does not exist in AWS; its `attribute` and values are `null`
  (R10).
- Drift `state_value`/`live_value` are `"(sensitive)"` when the state marks
  the attribute in `sensitive_values` (Security considerations).
- The `drift` policy rule is `skipped` when drift did not run (`--skip-drift`
  or no `--state`, R11); `skipped` never affects the overall verdict.
- The overall `verdict` is BLOCK if any rule blocks, else WARN if any rule
  warns, else PASS (R18). Exit codes: 0 = PASS/WARN, 1 = BLOCK (and WARN with
  `--fail-on-warn`), 2 = usage/runtime errors; an exit 1 outranks the drift
  read-error exit 2 (A5).
- All lists preserve evaluation order and are complete in JSON (only the
  Markdown report truncates long lists, R20).
