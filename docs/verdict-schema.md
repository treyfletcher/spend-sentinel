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
        "monthly_delta_usd": "7.59",
        "price_source": "live | snapshot | mixed  (v1.1: present only under --live-pricing)"
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
    "region": "us-east-1",
    "live_pricing": {
      "requested": true,
      "status": "ok | degraded | unavailable",
      "endpoint_region": "us-east-1",
      "lookups": { "live": 0, "snapshot_fallback": 0, "miss": 0 },
      "publication_dates": { "earliest": "2026-08-20T00:00:00Z", "latest": "2026-08-27T00:00:00Z" },
      "warnings": [ { "reason": "no_match", "detail": "aws_ebs_volume/gp3" } ]
    }
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

v1.1 additions (present **only** when `--live-pricing` was passed; without the
flag the document is byte-identical to v1):

- `cost.breakdown[].price_source`: `live` (all of the resource's rate lookups
  came from the Pricing API), `snapshot` (all fell back), or `mixed` (e.g. an
  RDS instance rate live, storage rate fallback). Omitted entirely on default
  runs.
- `meta.live_pricing.status`: `ok` — every priced lookup was live;
  `unavailable` — a run-level failure (`boto3_missing`, `client_init_error`,
  `unsupported_region`) disabled the API; else `degraded`.
- `meta.live_pricing.lookups`: per-resolution counts (not unique keys).
- `meta.live_pricing.publication_dates`: earliest/latest `publicationDate`
  of accepted price-list items, or `null` when no live rate was accepted.
  No wall-clock timestamps appear anywhere (deterministic outputs).
- `meta.live_pricing.warnings[]`: degradation records; `reason` is one of
  `boto3_missing`, `client_init_error`, `api_error`, `timeout`,
  `budget_exhausted`, `unsupported_region`, `unmapped_value`, `no_match`,
  `ambiguous`, `parse_error`, `pagination_overflow`, `oversize_response`;
  `detail` names only spend-sentinel's own service/price keys.
- Live-pricing degradation never changes the exit code (it stays
  verdict-driven; unlike drift read errors, no exit 2).
