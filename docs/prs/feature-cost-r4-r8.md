# PR: spend-sentinel v1 — increment 2: cost estimation + region resolution (R4–R8)

Branch: `feature/cost-r4-r8` (worktree off `feature/spend-sentinel-v1`)
Spec: `docs/specs/spend-sentinel-v1.md` (APPROVED, incremental delivery)

## Summary

Second increment: task T3 (pricing) plus R8 region resolution. The `analyze`
command now prices the plan's changes from a bundled, versioned pricing
snapshot and adds a `cost` section to its JSON output. Drift, policy, the R19
verdict schema, renderers, and real CI remain out of scope.

## Requirements coverage

| Req | Where | Notes |
| --- | --- | --- |
| R4 | `spend_sentinel/core/cost.py::_monthly_cost`; `spend_sentinel/data/pricing_snapshot.json` | Exactly the five types: `aws_instance` (hourly × 730, Linux/shared), `aws_ebs_volume` (per-GB-month × size), `aws_db_instance` (hourly per engine:class × 730, ×2 for `multi_az`, + storage per-GB-month × `allocated_storage`), `aws_nat_gateway` (hourly × 730), `aws_lb` (hourly per `load_balancer_type` × 730). Snapshot covers 12 EC2 types, all six EBS types, 6 RDS classes × postgres/mysql, NAT, ALB/NLB in us-east-1/us-west-2/eu-west-1; `meta.version`, `meta.snapshot_date` (2026-08-27), `meta.sources`, and documented limitations (no IOPS/throughput, NAT data processing, LCU). |
| R5 | `core/cost.py` (`HOURS_PER_MONTH`, `estimate`) | Decimal end to end (snapshot rates are strings, converted once at load); per-resource delta quantized `ROUND_HALF_UP` to cents; total = sum of rounded deltas; output byte-identical across runs (verified). |
| R6 | `core/cost.py::_delta` | create → +cost(after); delete → −cost(before); update/replace → cost(after) − cost(before); total + per-resource breakdown in `CostReport`. |
| R7 | `core/cost.py::estimate`; models in `core/models.py` (`UnpricedReason`, `UnpricedResource`) | Nothing silently dropped: `unsupported_type` (type outside R4), `unknown_price_key` (key absent from snapshot), `attributes_unknown` (pricing-relevant attribute missing/None/of the wrong type). Markdown "N unpriced" line is T6 (renderers), not this increment. |
| R8 | `core/plan.py::resolve_plan_region`; wiring + diagnostics in `cli.py::analyze`; `pricing/snapshot.py::supported_regions` | `--region` wins; else first constant AWS provider region from `configuration.provider_config` (spec A1); else exit 2 "pass --region"; unknown region exits 2 naming it and the supported regions. |
| Modularity | `pricing/source.py::PricingSource` (Protocol); `pricing/snapshot.py::SnapshotPricingSource` | `core/cost.py` depends only on the protocol; the concrete snapshot source is wired in `cli.py` only. |

## CLI change

`spend-sentinel analyze --plan <path> [--region <r>]` now prints, in addition
to `summary`/`resources`:

```json
"cost": {
  "region": "us-east-1",
  "monthly_delta_usd": "110.99",
  "breakdown": [{"address": "...", "type": "...", "action": "create", "monthly_delta_usd": "7.59"}],
  "unpriced": [{"address": "...", "type": "...", "reason": "unsupported_type"}]
}
```

Monetary values are strings with 2 decimals (spec Modularity note). Still not
the R19 verdict schema.

## BREAKING for the increment-1 test suite (flagged for tester)

R8 makes a resolvable region mandatory for `analyze`: a plan with no constant
provider region and no `--region` now exits 2 (spec-mandated). 9 of the
increment-1 CLI-level tests fail on this branch because their fixtures have no
`configuration` block and no `--region` is passed (`tests/test_cli.py`,
`TestCliR1`/`TestCliR3` classes, one `test_security.py` case). All non-CLI
unit tests still pass (70 passed). The fixtures/tests need a region for
increment 2; I did not touch them — tests are the tester's surface.

## Assumptions

- **A-i7 (provider defaults)**: where the AWS provider documents a default for
  an optional pricing-relevant attribute, it is applied when the attribute is
  absent/null: `aws_ebs_volume.type` → `gp2`, `aws_db_instance.storage_type`
  → `gp2`, `aws_lb.load_balancer_type` → `application`. Attributes with no
  default (`instance_type`, `size`, `engine`, `instance_class`,
  `allocated_storage`) mark the resource `attributes_unknown` when missing —
  a missing value at plan time means unknown-until-apply (`after_unknown` is
  not separately parsed; absence from `before`/`after` is the signal).
- **A-i8 (wrong-typed attributes)**: a pricing-relevant attribute of the wrong
  JSON type (e.g. non-numeric `size`, boolean where string expected) is
  treated as `attributes_unknown` rather than a hard exit 2 — the resource is
  visibly unpriced, not silently dropped, and one hostile resource cannot mask
  the rest of the report. Flagging as security-adjacent for review.
- **A-i9 (RDS pricing key)**: RDS rates are keyed `engine:instance_class`
  verbatim from the plan (e.g. `postgres:db.t3.medium`); engine aliases like
  `aurora-postgresql` are not in the snapshot and surface as
  `unknown_price_key`.
- **A-i10 (region scan order)**: `provider_config` keys are scanned in sorted
  order for determinism; the root `aws` entry sorts before aliases
  (`aws.west`), so the primary provider's constant region wins (spec A1
  "first constant found" made deterministic).
- **A-i11 (snapshot values)**: prices are realistic on-demand USD rates from
  the author's knowledge of public AWS pricing pages, internally consistent
  per region; `meta` records date/sources so staleness is judgeable (spec A3).
- **A-i12 (multi_az)**: any truthy `multi_az` doubles the instance component
  only (not storage), matching R4's wording.

## How to run

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# region from the plan's provider configuration
.venv/bin/spend-sentinel analyze --plan plan.json

# or explicit
.venv/bin/spend-sentinel analyze --plan plan.json --region us-east-1

.venv/bin/ruff check .          # clean
.venv/bin/python -m mypy        # clean (strict)
```

Verified by hand: AC3-style resize t3.large→t3.xlarge = +60.74 (0.0832×730
rounded half-up); mixed 8-resource plan totals 110.99 with all three unpriced
reasons present; AC12-style: plan-constant eu-west-1 prices used without a
flag (8.32 vs 7.59 in us-east-1), `--region` overrides, unresolvable region
exits 2 telling the user to pass `--region`, unknown region exits 2 naming
supported regions; output byte-identical across runs; snapshot present in the
built wheel.
