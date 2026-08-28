# PR review — feature/spend-sentinel-v1 (increments 1+2, R1–R8)

Reviewer: pr-reviewer-agent
Date: 2026-08-27
Scope: `git diff main...feature/spend-sentinel-v1` — plan ingestion (R1–R3) and
cost estimation + region resolution (R4–R8), per the spec's incremental gating.
R9+ are later work; their absence is not reviewed. Spec flags S1–S6 stay logged
with pm-planner (owner deferred spec amendments); nothing here amends the spec.

**Verdict: APPROVE WITH FIXES MADE** — three blockers (two of them the
tester's open BUG-2/BUG-3, one found in review) plus one test-coverage gap,
all confirmed by reproduction and fixed in review commits on this branch.
Merging remains the human's decision.

## Verification (before / after review fixes)

| Check | Reported | Verified before fixes | After fixes |
| --- | --- | --- | --- |
| pytest | 178 passed, 1 skipped, 5 xfailed | matches | **190 passed, 1 skipped, 0 xfailed** |
| ruff | clean | clean | clean |
| mypy (strict) | clean | clean | clean |
| coverage | 98% (inc-1 scope) | 94% overall | 95%+; snapshot-loader error paths now covered |

The 1 skip is the chmod-as-root unreadable-file test (expected). All five
former strict-xfails now run as ordinary tests asserting the fixed behavior.

## Findings

### [BLOCKER] BUG-2: non-finite numbers in size attributes escape as a traceback, exit 1 — ✅ addressed (145bbe9)

- Where: `spend_sentinel/core/cost.py:175` (`_require_number`).
- Issue (confirmed by repro): `json.loads` accepts `NaN`/`Infinity`/`-Infinity`
  and overflows `1e400` to `inf`; `Decimal(str(value))` constructs NaN/Infinity
  *successfully*, so the guarding `except InvalidOperation` never fired — the
  failure surfaced later (at `quantize`, or as a pydantic `finite_number`
  ValidationError building `CostLine`) as an uncaught traceback with exit 1,
  violating the fail-closed security requirement and colliding with the future
  R18 BLOCK exit code.
- Fix: explicit `number.is_finite()` check → `attributes_unknown`, consistent
  with A-i8 (the resource is visibly unpriced; one hostile resource cannot mask
  the rest of the report). Removed the 4 xfail markers and pinned the chosen
  outcome (exit 0, `unpriced: [attributes_unknown]`, delta `0.00`); added core
  coverage for non-finite `allocated_storage`, which the tester's report named
  as equally affected but only exercised via `size`.

### [BLOCKER] BUG-3: negative sizes price to negative create deltas (cost-gate bypass) — ✅ addressed (171fe05)

- Where: `spend_sentinel/core/cost.py:175` (`_require_number`).
- Issue (confirmed by repro): an EBS create with `size: -100` produced
  `monthly_delta_usd: "-8.00"`, exit 0, not unpriced. A crafted PR plan could
  carry a giant negative-size volume to offset real cost and slip under the
  future R14 `max_monthly_delta` gate — the exact bypass the policy exists to
  stop.
- Spec position: the spec is silent on numeric ranges (flag S4, logged by the
  tester, stays with pm-planner). Chosen reading, documented in the code:
  negative GB counts are impossible infrastructure → `attributes_unknown`.
  This is the safest of the defensible options within the fixed R7 taxonomy:
  the resource stays visible in `cost.unpriced` and will trigger
  `treat_unpriced_as` (default warn) under R14, rather than either
  contributing a negative delta (fail-open) or hard-failing the whole plan
  (which would let one hostile resource suppress the entire report, contrary
  to A-i8's rationale). Zero remains validly priced (`0.00`), unchanged.
- Fix: `number < 0` check beside the finiteness check; xfail marker removed;
  pinning test extended to negative `allocated_storage`.

### [BLOCKER] Diagnostic line injection: plan-derived strings can spoof stderr and break R2's one-line contract — ✅ addressed (0b7b28d)

- Where: `spend_sentinel/cli.py:31` (`_fail`); reachable through
  `core/plan.py:149` (resource address echoed by `summarize_plan`),
  `core/plan.py:105` (pydantic error locations include attacker-chosen
  `provider_config` dict keys), and `cli.py:79` (a plan-constant region echoed
  by the R8 unknown-region diagnostic).
- Issue (confirmed by repro on all three surfaces): a newline embedded in a
  resource address, a `provider_config` key, or a provider-region constant
  produced a two-line stderr including a fully attacker-controlled line — e.g.
  a spoofed `spend-sentinel: error: ...` line. That breaks R2's "one-line
  diagnostic" contract for hostile input and is the stderr analogue of the
  Markdown-injection concern the spec's Security section calls out.
- Fix: sanitize at the single `_fail` choke point — non-printable characters
  replaced with spaces. Printable identifier content still reaches stderr per
  the coder's A-i5 judgment (S2 stays logged with pm-planner). Pinning tests
  added for all three surfaces (`tests/test_security.py::TestDiagnosticLineInjection`).

### [IMPROVE] Snapshot loader's fail-closed paths were untested — ✅ addressed (974bb9f)

- Where: `spend_sentinel/pricing/snapshot.py:48-67`; tests in
  `tests/test_r4_pricing.py`.
- Issue: `SnapshotError` on malformed snapshot data (missing meta, wrong shape,
  non-decimal rate) had zero coverage — a regression in the loader's validation
  would pass the suite. The `data` constructor parameter exists precisely to
  test this.
- Fix: `TestSnapshotLoaderFailsClosed` — three malformed-shape cases plus a
  canary test that `SnapshotError` messages never echo snapshot content (they
  reach stderr via the CLI, so the R2 no-echo posture applies).

### [NIT] `--region ""` silently falls back to the plan's region

`cli.py:71`: `region_flag or resolve_plan_region(plan)` treats an explicit
empty `--region ""` as absent. Harmless in practice; if it ever matters,
`is not None` plus an explicit empty-string rejection would be clearer. Left
as a comment — no behavior change without a requirement.

### [NIT] `multi_az` truthiness doubles cost for any truthy value (S5)

`cost.py:133`: `if attrs.get("multi_az"):` — a hostile string like `"false"`
is truthy and doubles the instance component. Overstating cost is fail-safe
for a cost ceiling, and the tester already logged this as S5 (no action
needed now, document in T9). Left as a comment to avoid pre-empting the
deferred spec call; strict A-i8 consistency would treat a non-bool as
`attributes_unknown`.

### [NIT] `format_version` "1." is accepted

`plan.py:99`: `fv.startswith("1.")` admits the degenerate `"1."`. Terraform
never emits it; not worth a change.

### [NIT] Three near-identical subprocess CLI helpers in the test suite

`tests/test_cli.py::run_module`, `tests/test_security.py::run_cli_subprocess`,
`tests/test_r7_unpriced.py::TestHostileNumericValues.run_cli` could be one
conftest helper. Style-only; left alone to avoid churn.

### [PRAISE] Test design: snapshot-derived expectations with precondition guards

Computing every expected cost from the bundled snapshot at test time (never
hardcoding) is the right call for a hand-curated snapshot, and it is done
honestly: structure-sensitive tests guard their own preconditions (e.g.
`test_r5_half_cent_rounds_up_not_bankers` asserts the rate actually lands on
a half cent and that half-up genuinely differs from banker's rounding on it),
so a snapshot edit cannot silently hollow out the assertion. The suite would
fail on broken code, not just exercise it.

### [PRAISE] Fail-closed posture is real, not aspirational

Explicit allowlist action classification (`classify_actions`) that hard-fails
unknown combinations naming the resource; the 50 MB cap checked before any
read (and boundary-tested with a sparse file); content-echo canary tests;
`RecursionError` mapped to the R2 contract in both `json.loads` and pydantic
validation (BUG-1 fix verified); pydantic re-summarization that emits field
locations and error kinds, never values.

### [PRAISE] Modularity per spec

`core/` imports only the `PricingSource` protocol; the concrete
`SnapshotPricingSource` is wired in `cli.py` alone; Decimal end-to-end with
rates stored as strings and converted exactly once at load; monetary values
serialized as 2-decimal strings. The estimator is trivially testable with a
fake source, exactly as the Modularity notes intend.

## Review commits on this branch

| Commit | What |
| --- | --- |
| 145bbe9 | review: fail closed on non-finite size/allocated_storage (BUG-2) |
| 171fe05 | review: treat negative size/allocated_storage as attributes_unknown (BUG-3) |
| 0b7b28d | review: sanitize control characters in CLI diagnostics |
| 974bb9f | review: cover the snapshot loader's fail-closed paths |

Post-fix status: `python3 -m pytest` → 190 passed, 1 skipped; `ruff check .`
clean; `python3 -m mypy` (strict) clean.
