# Test report — feature/spend-sentinel-v1, increment 2 (R4–R8)

Tester: tester-agent
Date: 2026-08-27
Scope: R4–R8 (cost estimation from the bundled snapshot + region resolution),
maintenance of the increment-1 suite after the R8 breaking change, and
verification of the BUG-1 fix (b39ee7f). Drift, policy, verdict/renderers
(R9–R21) remain unimplemented and untested.

## Result summary

- Full suite (increments 1+2): **184 tests — 178 passed, 1 skipped, 5 xfailed**.
  - Skip: unreadable-file chmod test, meaningless as root (unchanged).
  - Xfails (all `strict=True`, flip to failures when fixed): 4 × BUG-2
    (NaN/Infinity/-Infinity/1e400), 1 × BUG-3 (negative size).
- BUG-1 (increment-1 report) verified fixed: the deep-nesting test now passes
  as a normal test (marker removed) — exit 2, one-line stderr, no traceback,
  confirmed for 100k-deep arrays.
- Maintenance: 10 increment-1 tests updated for R8's spec-mandated "resolvable
  region required" behavior — CLI-exercised fixtures gained a constant
  `configuration.provider_config.aws` region (us-east-1); no assertions were
  weakened, only the now-required region supplied.
- All expected cost values are computed from
  `spend_sentinel/data/pricing_snapshot.json` at test time — no hardcoded
  prices; the suite survives snapshot rate changes (structure-sensitive tests
  guard their own preconditions, e.g. the half-cent rounding case).

## Coverage table

| Requirement / assumption | Tests | Result |
| --- | --- | --- |
| R4 aws_instance hourly × 730 (4 types × 3 regions) | `test_r4_pricing.py::TestInstancePricing` | PASS |
| R4 aws_ebs_volume per-GB (all 6 types); A-i7 default gp2 | `TestEbsPricing` | PASS |
| R4 aws_db_instance hourly + storage; multi_az ×2 instance-only (A-i12); A-i7 storage default gp2 | `TestDbInstancePricing` (5 tests) | PASS |
| R4 aws_nat_gateway hourly × 730 (3 regions); aws_lb by type + A-i7 default application | `TestNatAndLbPricing` | PASS |
| R4/T3 snapshot integrity: 3 regions, ≥10 EC2 types, 6 EBS types, ≥5 RDS classes per engine, NAT, ALB/NLB, meta provenance, positive Decimal rates | `TestSnapshotIntegrity` (6 tests) | PASS |
| R5 half-up rounding at resource level (half-cent case discriminating from banker's; negative half away from zero; single rounding per resource; no `-0.00`) | `test_r5_r6_cost_math.py::TestRoundingR5` (4 tests) | PASS |
| R5 determinism (estimate twice; CLI byte-identical; 2-decimal strings in JSON) | `TestDeterminismR5` (3 tests), `test_cli.py` determinism test | PASS |
| R6 create/delete/update/replace delta semantics; AC3 resize fixture; total == sum(breakdown) | `TestDeltaSemanticsR6` (7 tests) | PASS |
| R7 unsupported_type / unknown_price_key / attributes_unknown; A-i9 engine alias; missing/null attrs; unknown after-side on update | `test_r7_unpriced.py::TestTaxonomy` (10 tests) | PASS |
| R7/A-i8 wrong-typed attributes → attributes_unknown (11 hostile shapes) | `TestWrongTypedAttributes` | PASS |
| R7/AC4 exactly two unpriced entries with correct reasons (core + CLI) | `TestAc4Style` | PASS |
| R7 nothing silently dropped (breakdown ∪ unpriced == classified changes) | `TestNothingSilentlyDropped` | PASS |
| R7/security non-finite JSON numbers fail closed | `TestHostileNumericValues::test_r7_nonfinite_size_fails_closed` | **XFAIL — BUG-2** |
| R7/security negative sizes fail closed | `test_r7_negative_size_fails_closed` | **XFAIL — BUG-3** |
| R7 edge: zero size → 0.00 priced; 10^15 GB exact Decimal | `test_r7_zero_size_prices_to_zero`, `test_r7_huge_size_does_not_crash_or_lose_precision` | PASS |
| R8 resolve_plan_region unit matrix (constant, none, non-constant refs, non-string, non-aws, alias, A-i10 sorted order) | `test_r8_region.py::TestResolvePlanRegionUnit` (9 tests) | PASS |
| R8/AC12 plan-constant eu-west-1 used; --region overrides; no region → exit 2 "pass --region" naming the file; unknown region (flag or plan) → exit 2 naming region + supported regions | `TestRegionThroughCliAc12` (7 tests) | PASS |
| R8 error-precedence: plan errors before region errors; region errors write nothing to stdout | `test_r8_plan_errors_take_precedence_over_region_errors`, assertions in the exit-2 tests | PASS |

## Bugs found

### BUG-2: non-finite JSON numbers in size attributes crash with an uncaught `decimal.InvalidOperation` (traceback, exit 1)

- Severity: medium (same class as BUG-1 — hostile plan input escapes the
  fail-closed contract; exit 1 collides with the future R18 BLOCK meaning).
- Where: `spend_sentinel/core/cost.py`. `_require_number` guards
  `Decimal(str(value))` with `except InvalidOperation`, but
  `Decimal("nan")`/`Decimal("inf")` construct *successfully* — the exception
  fires later, in `estimate()` at `delta.quantize(...)`, where nothing catches
  it. Python's `json.loads` accepts `NaN`/`Infinity`/`-Infinity` by default,
  and `1e400` parses to `inf`.
- Repro:
  ```bash
  cat > nan.json <<'EOF'
  {"format_version":"1.2","resource_changes":[{"address":"aws_ebs_volume.x",
  "mode":"managed","type":"aws_ebs_volume","name":"x","provider_name":"aws",
  "change":{"actions":["create"],"before":null,"after":{"type":"gp3","size":NaN}}}]}
  EOF
  spend-sentinel analyze --plan nan.json --region us-east-1; echo "exit=$?"
  # -> decimal.InvalidOperation traceback, exit=1
  ```
  Same for `Infinity`, `-Infinity`, `1e400`; `allocated_storage` is equally
  affected.
- Expected: fail closed — either `attributes_unknown` (matching A-i8's
  treatment of other unpriceable values, my recommendation) or an R2-style
  one-line exit 2. Never a traceback, never exit 1.
- Suggested fix (coder's call): reject non-finite values in `_require_number`
  (`Decimal.is_finite()`), or parse plan JSON with
  `json.loads(..., parse_constant=...)` that raises, which `load_plan` would
  then map to exit 2.
- Tests: `tests/test_r7_unpriced.py::TestHostileNumericValues::test_r7_nonfinite_size_fails_closed`
  (4 params, `xfail(strict=True)` — they accept either fail-closed outcome, so
  they turn green under whichever fix is chosen).

### BUG-3: negative sizes are priced, producing negative deltas on create (fail-open; also spec gap S4)

- Severity: low-medium, security-adjacent. `{"size": -100}` on an EBS create
  yields `monthly_delta_usd: "-8.00"` — a crafted PR plan can include a
  giant negative-size volume to offset real cost and slip under the future
  `max_monthly_delta` gate (R14), the exact bypass the policy exists to stop.
- Repro: create `aws_ebs_volume` with `size: -100` → exit 0, breakdown delta
  −8.00, not unpriced.
- Expected (my reading of the security section's fail-closed principle):
  negative GB counts for `size`/`allocated_storage` are impossible
  infrastructure and should surface as `attributes_unknown`.
- Spec caveat: no requirement addresses negative attribute values — logged as
  spec flag S4 below; the xfail records the recommended direction, pm-planner
  may overrule.
- Test: `tests/test_r7_unpriced.py::TestHostileNumericValues::test_r7_negative_size_fails_closed`
  (`xfail(strict=True)`).

No other implementation bugs found. Verified correct: A-i7 defaults, A-i8
wrong-typed handling, A-i9 alias behavior, A-i10 deterministic provider scan,
A-i12 multi_az semantics, and the BUG-1 fix.

## Spec ambiguities / notes for pm-planner

(Owner deferred S1–S3 from the increment-1 report; still logged there and
still applicable.)

- S4 (new, from BUG-3): the spec never constrains numeric attribute ranges.
  Recommend R7 gain a sentence: pricing-relevant numeric attributes that are
  negative or non-finite are treated as `attributes_unknown`.
- S5 (observation, no action needed now): A-i12 doubles the instance component
  for *any truthy* `multi_az` — a hostile string value like `"false"` is
  truthy in Python and doubles the cost. Overstating cost is fail-safe for a
  cost ceiling, so this is fine, but worth a line in the R4 docs when T9 lands.
- S6 (R8 wording): R8's "provider `configuration` block when it is a constant"
  doesn't say which provider wins with multiple aliased AWS providers; A-i10's
  sorted-scan (primary before aliases) is deterministic and sensible — worth
  ratifying in the spec text alongside deferred A1.

## Untestable in this increment

- R9–R21 (drift, policy, verdict schema, Markdown renderer, CI posture): not
  implemented. The R7 requirement that "the Markdown output shows an
  'N unpriced resources' line" is renderer work (T6) and explicitly out of this
  increment per the PR; only the JSON `unpriced` list is testable today.
- AC1/AC2/AC11 in full: they depend on verdict/policy/output-file flags not
  yet built; the cost-math halves are covered (see coverage table).

## How to run

```bash
cd /home/claude/spend-sentinel
python3 -m pytest        # 178 passed, 1 skipped, 5 xfailed
ruff check tests/        # clean
```
