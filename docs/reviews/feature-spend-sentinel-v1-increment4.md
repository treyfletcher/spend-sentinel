# PR review — feature/spend-sentinel-v1, increment 4 (R13–R17 policy gates)

Reviewer: pr-reviewer-agent
Date: 2026-08-28
Scope: the delta `git diff 218af2a..HEAD` — policy schema/loader
(`core/policy.py`), the four rule evaluators, `RuleResult` models, CLI
`--policy` + `policy` JSON section, and the increment-4 test suite. Judged
against spec R13–R17 and Security considerations, including the owner
decision recorded in bf00af8 (`limit_usd` defaults to 200; explicit `null` =
no ceiling). Earlier reviews stand; spec flags S1–S12 remain deferred with
pm-planner — no spec edits here.

**Verdict: APPROVE WITH FIXES MADE** — no blockers; one improvement found by
independent probing (a pydantic lax-mode coercion hole the tester's matrix
missed) and fixed in a review commit. The tester's "no new bugs" claim
otherwise held up under my own edge probes.

## Verification (before / after review fix)

| Check | Reported | Verified before fix | After fix |
| --- | --- | --- | --- |
| pytest | 382 passed, 1 skipped | matches | **383 passed, 1 skipped** |
| ruff | clean | clean | clean |
| mypy (strict, 16 files) | clean | clean¹ | clean |

¹ mypy initially reported `import-untyped` for yaml in this checkout only
because `types-PyYAML` — correctly added to the dev extras in this increment —
was not installed in the review environment. Installed; no code issue.

Probes run beyond the suite (all behaved fail-closed unless noted):
`limit_usd: .nan`/`.inf` → rejected by pydantic's `finite_number` validation
(the BUG-2-class hole — Decimal NaN comparisons raise `InvalidOperation` — is
closed by the schema, verified); `limit_usd: true` → rejected;
`allowed_ports: [true]` → **accepted as port 1** (the IMPROVE below);
duplicate YAML rule keys → last silently wins (NIT); numeric-string and
negative limits → accepted (NIT, safe direction).

## Findings

### [IMPROVE] `allowed_ports: [true]` silently loaded as port 1 — ✅ addressed (43f9298)

- Where: `spend_sentinel/core/policy.py:65` (`OpenIngressRule.allowed_ports`).
- Issue (confirmed by probe): pydantic 2.13's lax mode coerces bool → int for
  `tuple[int, ...]`, so a YAML `true` in `allowed_ports` became port 1 —
  silently exempting port 1 from the `open_ingress` gate instead of exiting 2,
  contrary to R13's "wrong type exits 2" contract and to the codebase's own
  bool-is-not-a-number convention (cost estimator's A-i8 and the BUG-2/BUG-3
  fixes both exclude bools explicitly). Fail direction is toward allowing, so
  this deserved a fix rather than a note.
- Fix: a `mode="before"` validator rejects bools in the list; the diagnostic
  names `rules.open_ingress.allowed_ports` per the R13 contract. Pinned in the
  schema-error test matrix (which already covered `[80, http]` but not bools —
  the one gap in an otherwise thorough matrix).

### [NIT] Lenient scalar coercions on policy values

`limit_usd: "200"` (numeric string) and `limit_usd: -50` are accepted. The
string case is friendly-but-lenient against R13's wrong-type letter; the
negative case makes every delta ≥ −50 block, which is the fail-safe
direction. Neither is exploitable (the policy author controls the file);
left as-is to avoid strictness churn — worth one line in the T9 policy
reference.

### [NIT] Duplicate YAML keys: last one silently wins

PyYAML's `safe_load` accepts `rules: {deletions: …}` twice and keeps the last
(probe confirmed: a first `action: block` silently overridden by a later
`action: ignore`). Spec-silent, file is owner-authored, so comment only — a
duplicate-key-rejecting loader would be the hardening if pm-planner wants it;
otherwise a T9 doc caveat.

### [NIT] Plan-side bool ports pass `isinstance(int)` in `_ports_of`

`policy.py:322`: `from_port: true` in a crafted plan reads as port 1
(bool is an int subclass). No security delta — an attacker who controls the
plan can write the integer directly, and the port must still be in
`allowed_ports` — but it is the same inconsistency the fixed config-side hole
had. Not changed: plan-side attrs deliberately fail toward BLOCK elsewhere
(A-i23), and a bool here still cannot widen an exemption beyond what an int
could.

### [NIT] Open-CIDR matching is exact-string

`0.0.0.0/0` / `::/0` are matched verbatim (R15's literal wording).
Equivalent spellings (`::0/0`, `0:0::/0`) or a `/1 + /1` split would evade
the gate. Providers normalize CIDRs in practice and the spec defines "open"
as exactly these two strings, so the implementation is spec-correct — logged
here for pm-planner's deferred-flags pile rather than changed.

### [NIT] Commit attribution: bf00af8 (spec edit) carries the reviewer identity

The owner's `limit_usd` decision was committed under `pr-reviewer-agent` —
whoever recorded it in this shared checkout inherited the repo-local git
config set for review commits. No content concern (the coordinator confirms
the decision is the owner's); flagging so the history isn't misread as the
reviewer amending the spec, which this review has not done.

### [PRAISE] The port-range check is both correct and efficient

`_range_fully_allowed` gets every edge right: inclusive `range(from, to+1)`,
protocol `-1` unconditional block (S10's safe reading), unverifiable ranges
(missing/non-int/inverted) fail closed per A-i23, and the
`to - from + 1 > len(allowed)` cardinality fast path makes `0–65535` an O(1)
rejection instead of a 65k-iteration loop.

### [PRAISE] Schema strictness done properly

`extra="forbid"` at every nesting level, `Literal` enums, diagnostics that
name the offending key and never echo values, `yaml.safe_load` only, the
shared 50 MB cap, and — verified by probe — pydantic's `finite_number`
validation closing the `.nan`/`.inf` limit hole that bit the cost path in
increment 2. Defaults are the safe direction and an empty/absent file means
defaults, never "no rules".

### [PRAISE] Mutation-sensitive tests

The suite would catch the classic regressions this logic invites: a `>`→`>=`
swap (cent-over blocks AND exactly-at-limit passes are both pinned), a
range off-by-one (80–81 with only 80 allowed blocks; fully-allowed 80–81
passes; single-port cases), escalation-never-downgrades, protected-beats-
ignore including via replace, and both replace action orders for A-i22. The
hostile-YAML probes (verified alias-bomb non-explosion, `!!python` tag
no-execution canary, 5000-deep nesting) and the CLI-level U+2028/NEL message
sanitization tests are exactly the right paranoia.

### Judgments endorsed (no change)

A-i22 (replaced SGs inspected — the security-correct reading of R15's
"created or updated"); A-i26 (`ignore` renders `pass`, `skipped` reserved for
"did not run"); A-i25 (drift read errors as a count suffix, keeping their own
R12 exit path); A-i27 (50 MB cap extended to policy files); the owner's
limit_usd default implemented exactly (omitted → 200, explicit `null` → no
ceiling).

## Review commits on this branch (this increment)

| Commit | What |
| --- | --- |
| 43f9298 | review: reject boolean allowed_ports entries (R13 wrong-type contract) |

Post-fix status: `python3 -m pytest` → 383 passed, 1 skipped; `ruff check .`
clean; `python3 -m mypy` (strict, 16 files) clean.
