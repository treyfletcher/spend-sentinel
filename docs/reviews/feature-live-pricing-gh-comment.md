## Review: v1.1 live Pricing API adapter — **Approve (with fixes pushed)**

Full review in `docs/reviews/feature-live-pricing.md`. I pushed two `review:` commits to this branch; everything else held up under independent probing.

### Fixed on the branch

- **[BLOCKER] BUG-6** (a9a4478): an untyped exception from a transport client escaped `LivePricingSource.get_rate` and crashed `estimate()` — a client fault became a dead CI run instead of the snapshot fallback R27 guarantees. Reproduced, then fixed with a defensive `except Exception → api_error` (mirroring drift's A-i18), negative-cached like other failures, no exception text retained. Xfail removed; test now pins the degraded outcome.
- **[IMPROVE]** (4bbf2c7): `publicationDate` was the one unguarded response→output channel — a probe pushed a 100 KB hostile "date" (escaped, but report-bloating past R20's size budget) into JSON meta and the Markdown summary. Dates now must match ISO-8601 or are dropped; the key still prices live. Pinned at extraction and end-to-end.

### Left as comments (no change)

- Budget isn't re-checked between pagination pages (spec-compliant; worst case bounded by timeouts).
- `publication_dates` includes non-contributing sibling products ("accepted items" hair).
- Warning `detail` carries plan-derived keys into JSON unbounded (consistent with v1's posture; never reaches Markdown/stderr — verified).

### Verified independently

Default-path purity (no new imports, v1 goldens byte-identical), flag/no-flag exit parity with boto3 absent, hostile-content sweeps (Markdown summary/Source column/stderr carry internal enums only), boto3 confined to `adapters/`, docs (`verdict-schema.md`, `iam-policy-pricing.json`, README) all test-backed and accurate.

**Praise:** the degradation lattice (12-reason taxonomy, run/key-level split, negative caching, injectable clock), the verbatim R25 filter matrix pinned on the wire, and the tester's fixture-matrix guard are genuinely strong work.

**Status:** 667 passed / 1 skipped, ruff clean, mypy strict clean. Merge is the owner's call.
