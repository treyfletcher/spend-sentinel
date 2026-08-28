# PR review — feature/live-pricing (v1.1, R22–R33)

Reviewer: pr-reviewer-agent
Date: 2026-08-28
Scope: the full delta `git diff feature/spend-sentinel-v1...feature/live-pricing`
— the live Pricing API adapter delivered in three chunks (pure query layer,
`LivePricingSource`, CLI/renderer/docs surface) — judged against spec
R22–R33 and its Security considerations. Spec flag S13 stays logged with
pm-planner; no spec edits here.

**Verdict: APPROVE WITH FIXES MADE** — one blocker (the tester's BUG-6,
confirmed by reproduction) and one improvement found by probing beyond the
suite (unbounded `publicationDate`), both fixed in review commits. Every
other invariant held under my own probes.

## Verification (before / after review fixes)

| Check | Reported | Verified before fixes | After fixes |
| --- | --- | --- | --- |
| pytest | 660 passed, 1 skipped, 1 xfailed | matches | **667 passed, 1 skipped, 0 xfailed** |
| ruff | clean | clean | clean |
| mypy (strict, 23 files) | clean | clean | clean |

Probes run beyond the suite: BUG-6 reproduced exactly as reported; a 100 KB
hostile `publicationDate` flowed into `meta.live_pricing` and the Markdown
summary (the IMPROVE below); default-path subprocess purity (no
`pricing.live`/`fixture_client`/boto3 modules in `sys.modules` after a
no-flag run); flag-vs-no-flag exit parity with boto3 genuinely absent
(both 0, single `boto3_missing` stderr line, correct `unavailable` summary);
a hostile `instance_type` reaching only the JSON warning `detail`
(JSON-encoded; never Markdown or stderr, which carry reasons only); boto3
imports confined to `adapters/boto3_reader.py` and `adapters/boto3_pricing.py`;
v1 golden files untouched by the diff and still byte-compared; README and
`docs/verdict-schema.md` claims each checked against the implementation —
all accurate, including the `lookups` object shape and the
`{earliest, latest}` dates object.

## Findings

### [BLOCKER] BUG-6: an untyped transport exception escapes `get_rate` and kills `estimate()` — ✅ addressed (a9a4478)

- Where: `spend_sentinel/pricing/live.py:470` (`resolve_live_rate`).
- Issue (confirmed by repro): only the three typed errors
  (`UnmappableKeyError`, `ExtractionError`, `PricingApiError`) were caught; a
  `PricingApiClient` raising anything else — a botocore edge case outside
  `(BotoCoreError, ClientError)`, a future client bug — propagated through
  `get_rate` (whose docstring promises "never raises") and crashed
  `estimate()`, turning a client fault into a dead CI run instead of a
  snapshot fallback. That is exactly the failure class R27 promises away,
  and the spec's own threat model ("the account, proxy, or endpoint override
  could be hostile") demands defense in depth here, as drift's A-i18 already
  does.
- Fix: a trailing `except Exception` maps to `api_error`. No exception
  detail is retained (it could carry response or credential text — the
  warning `detail` stays the internal service/price key). The failure is
  negative-cached like any other: verified one transport call for three
  resources sharing the key, all `price_source: "snapshot"`, status
  `degraded`, single warning. Xfail marker removed; the test now pins the
  degraded outcome.

### [IMPROVE] `publicationDate` was an unbounded response→output channel — ✅ addressed (4bbf2c7)

- Where: `spend_sentinel/pricing/live.py` (`extract_rate`).
- Issue (confirmed by probe): R31 allows exactly two response-derived values
  into outputs — the rate (bounds-checked) and `publicationDate` (not
  checked at all). A hostile response shipping a 100 KB "date" sailed into
  `meta.live_pricing.publication_dates`, the JSON verdict, and the Markdown
  summary line: escaped downstream, so no injection, but enough to blow
  R20's 65,536-character budget and bloat the PR comment through the one
  unguarded channel.
- Fix: dates must `fullmatch` the API's ISO-8601 shape
  (`YYYY-MM-DDThh:mm:ss[.fff][Z]`); anything else is silently dropped — the
  date is informational, so a bad one never fails the key and the rate still
  prices live. Pinned at extraction (five hostile shapes dropped, three
  valid shapes kept) and end-to-end (the renderer-escaping e2e test was
  re-pinned to the stronger property: a hostile date reaches the report in
  no form, and the summary omits its publication clause). The renderer's
  escaping of dates remains as defense in depth.

### [NIT] Budget is not re-checked between pagination pages

`live.py::fetch_pages` checks the 30 s budget only before each key's first
call, so a key already in flight can overrun the budget by its own
worst-case pagination and retries (~3 pages × 3 attempts × 15 s timeouts).
This matches R28's letter ("remaining uncached keys skip") and A16 accepts
the bound; a between-pages `budget.exhausted` check would tighten it if the
worst case ever matters. Comment only.

### [NIT] `publication_dates` includes non-contributing sibling products

`extract_rate` collects dates from every parsed product of a successful key,
not only products whose dimension supplied the accepted rate — R30 says
"accepted price-list items". Within a TERM_MATCH-filtered response the
products are same-key siblings and the field is informational, so this is a
semantic hair, not a defect. Comment only.

### [NIT] Warning `detail` carries plan-derived keys unbounded into JSON

A hostile plan attribute (e.g. a 100 KB `instance_type`) lands in
`meta.live_pricing.warnings[].detail`. JSON-encoded, never rendered to
Markdown or stderr (verified), and consistent with v1's posture of shipping
full plan addresses in JSON under the 50 MB input cap — noting it so the
choice is on record.

### [NIT] `_pricing_summary` guards with a bare `assert`

`render/markdown.py:79`: stripped under `python -O`; unreachable via the
only caller, which checks `live_pricing is not None` first. Micro-nit.

### [PRAISE] The degradation architecture is the best part of the PR

A typed 12-reason taxonomy with an explicit run-level/key-level split,
negative caching so failures cost one transport call, an injectable
monotonic clock making budget tests instant, and `unsupported_region`
short-circuiting before any transport or budget spend. The R25 filter
matrix is implemented verbatim — including the Single-AZ double-counting
rule — and pinned on the wire by AC16's recorded-call assertions.

### [PRAISE] Default-path purity is proven three ways

Unmodified v1 goldens still byte-compared; subprocess proofs that a no-flag
run imports none of the new modules; and attribution crossing the protocol
boundary as an optional `getattr`-discovered capability, leaving
`PricingSource` and the snapshot path untouched. R22's "byte-identical"
claim is enforced, not asserted.

### [PRAISE] Defensive extraction closes v1's bug classes preemptively

Size/count/page caps before parsing, navigated-path pydantic models, USD as
finite non-negative Decimal below 10^6 (the BUG-2 NaN class, closed at the
boundary this time), fail-closed handling of partially hostile responses
(A-c5), and CLI-level secret-marker sweeps proving response bodies reach no
output. The tester's fixture-matrix guard test — resolving committed
fixtures through `build_query`'s own filters — is a quietly excellent
design that stops fixtures drifting from the R25 matrix.

### [PRAISE] The tester's bug work was precise

BUG-5 (regex `$` matching before a trailing newline — subtle, real, fixed by
the coder with `fullmatch` and de-xfailed) and BUG-6 flagged as a hardening
observation in chunk 1, then correctly escalated to a contract violation
when chunk 2's docstring promised "never raises". Boundary tests throughout
(50/51 products, 3/4 pages, budget at exactly 30 s, 999999.999999 accepted).

### Judgments endorsed (no change)

A-c1 (pagination in the pure layer — the testable reading of T13); A-c4
(unit guard on "only OnDemand dimension" types); A-c5 (no cherry-picking
around hostile entries); A-c7 (strict `ok`); A-c9 (counters count
resolutions, cache still one query per key); A-c11 (`endpoint_region:
"invalid"`, raw env value never echoed); A11/A12 (degradation never exits 2
— a deliberate, owner-flagged divergence from drift's R12, justified because
the snapshot fallback fully meets the run's contract); S13 (fail-the-key on
the 51st product) stays logged for pm-planner.

## Overall v1.1 assessment

Releasable. The feature does the hard thing well: the happy path is small,
and the overwhelming majority of the code — correctly — is the degradation
lattice, which now survives even protocol-violating transports. The
opt-in invariants that matter for v1 users all verify: byte-identical
default output, no new imports, no exit-code changes, one new opt-in IAM
action in a separate policy document. Across the three chunks the tester
found two real bugs (one fixed by the coder, one by review) and review
added one hardening fix on the single unguarded response channel; all are
permanent regression tests. 667 tests, ruff and strict mypy clean, offline
and deterministic throughout.

## Review commits on this branch

| Commit | What |
| --- | --- |
| a9a4478 | review: defensive catch-all for untyped transport exceptions (BUG-6) |
| 4bbf2c7 | review: validate publicationDate to ISO-8601 before it leaves extraction |

Post-fix status: `python3 -m pytest` → 667 passed, 1 skipped, 0 xfailed;
`ruff check .` clean; `python3 -m mypy` (strict, 23 files) clean.
