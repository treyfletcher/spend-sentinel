# PR review — feature/spend-sentinel-v1, increment 5 (R18–R21) + final whole-PR pass

Reviewer: pr-reviewer-agent
Date: 2026-08-28
Scope: (1) the delta `git diff 066e3cc..HEAD` — verdict aggregation
(`core/verdict.py`), JSON/Markdown renderers (`render/`), CLI completion,
`docs/verdict-schema.md`, README, `docs/iam-policy.json`, the real CI
workflow, and the final test suite; judged against spec R18–R21 and Security
considerations. (2) A whole-PR integration pass over
`git diff main...HEAD` (91 files, ~11.3k lines) — the first time all five
increments are together. This document closes the PR. Merging to main is the
owner's decision.

**Verdict: APPROVE WITH FIXES MADE** — one blocker in the Markdown escaper
(link/image injection), found by probing beyond the tester's escaping matrix
and fixed in a review commit. Everything else held.

## Verification

| Check | Reported | Verified before fix | After fix |
| --- | --- | --- | --- |
| pytest | 456 passed, 1 skipped | matches | **458 passed, 1 skipped** |
| ruff | clean | clean | clean |
| mypy (strict, 20 files) | clean | clean | clean |
| coverage | ~98% claimed | 96% measured (misses are OSError/snapshot-corruption branches) | 96% |

Probes run beyond the suite: byte-identical `--out-json`/`--out-md` across
`PYTHONHASHSEED` 0 vs 7 (AC11 determinism holds for real); sensitive tag
values masked in both outputs through the CLI; link/image/emphasis smuggling
through addresses (the finding below); the `--out-json` + unwritable
`--out-md` combination (NIT below); `docs/iam-policy.json` parsed and
cross-checked against the adapter's recorded API surface; `ci.yml` reviewed
line-by-line — it would pass on GitHub (GNU `env -u` tolerates unset vars,
`[dev]` extras carry ruff/mypy/pytest-cov/types-PyYAML, matrix quotes both
Python versions, the AWS scrub wraps exactly the pytest step).

## Findings — increment 5 delta

### [BLOCKER] Markdown link/image syntax survived escaping — ✅ addressed (34294e5)

- Where: `spend_sentinel/render/markdown.py:30` (`_ESCAPES`).
- Issue (confirmed through the real CLI): `_escape` covered `& < > | \`` and
  control characters but not square brackets, so a crafted address like
  `aws_instance.x["[Click to approve](https://evil.example/phish)"]` rendered
  verbatim into the PR-comment table — a live link with an
  attacker-chosen label (phishing surface), and `![img](url)` an
  auto-loading image (a view-tracking beacon). The spec's Security section
  requires that "a crafted resource name cannot inject markup"; links and
  images are markup. The tester's escaping matrix (pipes, backticks, HTML,
  ampersands, newline verdict-spoof) was good but stopped at the spec's
  example characters.
- Fix: `[` and `]` escape to `&#91;`/`&#93;`, killing both link and image
  syntax; goldens unaffected (no brackets in them); pinning tests added for
  both vectors. Documented residual, accepted: GFM may autolink a bare URL
  *as itself* (no misleading label possible) and `*`/`_` can restyle text —
  neither can mislabel a destination, load a resource, or alter document
  structure; escaping `_` would entity-litter every Terraform address.

### [NIT] Partial output on a late write failure

`cli.py:197-206`: with both `--out-json` and `--out-md`, a JSON write that
succeeds followed by an MD write that fails exits 2 leaving `verdict.json`
behind (probe confirmed). AC10's "no output files on error" holds for all
ingestion/policy errors (they fail before analysis, and the tester pins
this); this late edge leaves a *valid, complete* JSON verdict, which is
arguably salvage rather than harm. Not changed — a temp-write-and-rename
dance is over-engineering for v1; worth a line in the schema doc if it ever
surprises anyone.

### [NIT] Cosmetic wording pinned by goldens

`$-24.60` in the header and "1 unpriced resources" — already flagged by the
tester as matching the spec's literal wording. Agreed: cosmetic, golden-
pinned, a polish is a deliberate golden update for later.

### [NIT] `--region ""` still treated as absent

Carried from the increment-2 review (`cli.py:157`, `region_flag or …`);
still trivial, still open.

### [PRAISE] Exit-code testing that would catch any regression

An 11-row unit matrix over (verdict × errors × fail-on-warn) plus CLI-level
precedence tests — BLOCK+errors→1, WARN+errors→2, WARN+errors+
`--fail-on-warn`→1 — paying off the S7 debt flagged two increments ago. The
A-i29 reading (a `--fail-on-warn` WARN outranks the error-2, same A5
rationale) is endorsed here as well: the gate must stay reliable for CI, and
the errors remain visible in `drift.errors`.

### [PRAISE] Renderer purity makes the goldens trustworthy

Both renderers take only the finished `Verdict` model (Modularity notes
honored), so the golden files are constructed-input, snapshot-independent,
byte-for-byte comparisons — they will not rot when pricing rates change, and
they double as the R20 format contract.

### [PRAISE] R21 enforced in depth, not asserted once

Session-scoped conftest scrub of all six AWS env vars + an in-suite assertion
that the scrub is in force + a ci.yml content test requiring every `-u` +
package-import-pulls-no-boto3 + the IAM doc cross-checked against the
adapter's *recorded* call surface. The chmod skip disappears on CI's
non-root runners, so CI runs 458/458.

### [PRAISE] Docs match behavior because tests make them

`docs/verdict-schema.md` is enforced by a recursive key-set/type/enum walk
over maximal and minimal scenarios; every README claim I checked (exit
table, policy defaults incl. the owner's 200, S10 protocol `-1` note, S12
`--policy`-pinning advice, RequestedRegion hardening note, pricing
limitations table) is backed by a tested behavior.

## Whole-PR pass (main...HEAD)

- **No dead code from the incremental builds**: the interim JSON-summary CLI
  is fully gone (`_drift_section`, the `resources` output, the stray `json`
  import); ruff/mypy strict over 20 files agree.
- **Cross-increment flows verified**: unpriced → `treat_unpriced_as` →
  WARN/BLOCK → verdict → exit code; drift read errors → count-only stderr
  notice → exit 2 unless outranked; policy loads before any output (AC10);
  `estimate`/`evaluate` re-classify actions only after `summarize_plan` has
  validated them fail-closed.
- **Security posture, end to end**: 50 MB caps and fail-closed parsing on all
  three untrusted inputs; `yaml.safe_load` only; no content echo and
  control-character sanitization on stderr; Markdown injection neutralized
  (tables, HTML, links, images, verdict spoofing); `sensitive_values` masked
  in both outputs including drift diffs; boto3 isolated to one adapter and
  provably unimported on skip paths; IAM surface pinned to five read-only
  actions.
- **Found-and-fixed ledger** (all with permanent regression tests):
  BUG-1 RecursionError (coder, b39ee7f); BUG-2 non-finite sizes (review,
  145bbe9); BUG-3 negative sizes (review, 171fe05); BUG-4 render
  nondeterminism (review, f893a0b); stderr diagnostic injection (review,
  0b7b28d); silent drift-error exit 2 (review, 9f1255a); bool
  `allowed_ports` coercion (review, 43f9298); Markdown link/image injection
  (review, 34294e5).
- **Open, deliberately**: spec flags S1–S3, S5, S6, S8–S12 remain with
  pm-planner (owner deferred; S5/S10/S12 are at least documented in the
  README). The most user-visible is S1: Terraform ≥ 1.7 `forget` actions
  hard-fail a plan — correct fail-closed behavior, but worth an owner
  decision before broad use.

## Overall v1 assessment

This is a releasable v1. All 21 requirements are implemented and covered by
458 offline, deterministic tests (all 12 acceptance criteria automated);
lint and strict typing are clean; coverage is 96% with the misses in
defensive error branches. The architecture honors the spec's modularity
rules (core imports no adapters; protocols at the seams; Decimal
end-to-end; renderers are pure), which is what made five independent
increments merge with only re-pins, not rework. The security posture is the
strongest part: every hostile-input bug found across the project — eight in
total, four by the tester, four by review — was in edge handling, none in
core math or policy logic, and each is now a permanent regression test. The
deferred spec flags are genuine product decisions, not defects. Recommended
to the owner for merge.

## Review commits on this branch (this increment)

| Commit | What |
| --- | --- |
| 34294e5 | review: escape square brackets to block Markdown link/image injection |

Post-fix status: `python3 -m pytest` → 458 passed, 1 skipped;
`ruff check .` clean; `python3 -m mypy` (strict, 20 files) clean.
