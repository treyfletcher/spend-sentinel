# PR review — feature/spend-sentinel-v1, increment 3 (R9–R12 drift detection)

Reviewer: pr-reviewer-agent
Date: 2026-08-27
Scope: the delta `git diff 0fd35dd..HEAD` — state ingestion, drift comparators,
adapters (protocol home, fixture reader, boto3 reader), CLI `--state` /
`--skip-drift` wiring and the `drift` JSON section, plus the increment-3 test
suite. Judged against spec R9–R12, Security considerations, and Modularity
notes. The increment-1/2 review (`docs/reviews/feature-spend-sentinel-v1.md`)
stands; spec flags S1–S9 remain logged with pm-planner (owner deferred; no
spec edits here).

**Verdict: APPROVE WITH FIXES MADE** — one blocker (the tester's BUG-4,
confirmed by reproduction) and one improvement, both fixed in review commits
on this branch. Merging remains the human's decision.

## Verification (before / after review fixes)

| Check | Reported | Verified before fixes | After fixes |
| --- | --- | --- | --- |
| pytest | 282 passed, 1 skipped, 1 xfailed | matches | **283 passed, 1 skipped, 0 xfailed** |
| ruff | clean | clean | clean |
| mypy (strict, 15 files) | clean | clean | clean |
| BUG-4 repro | seed 0 vs 5 differ | reproduced exactly | byte-identical across seeds 0/5/42 |

Also re-verified directly: no `core`/`pricing` module imports `adapters`;
boto3/botocore appear in `adapters/boto3_reader.py` only; the whole suite runs
with boto3 genuinely uninstalled.

## Findings

### [BLOCKER] BUG-4: SG rule rendering is hash-seed-dependent across processes — ✅ addressed (f893a0b)

- Where: `spend_sentinel/core/drift.py:238` (`_render_rules`).
- Issue (confirmed by repro): the sort key
  `(r[0], r[1] or -1, r[3], r[4])`-style tuple omitted index 2 (`to_port`), so
  atomic rules tying on protocol/from_port/family/cidr fell back to the
  iteration order of a `set` — `PYTHONHASHSEED`-dependent, so the same drift
  rendered `['tcp:80-80:…', 'tcp:80-8080:…']` under one seed and reversed
  under another. The drift *decision* (set comparison) was unaffected, but
  A-i19 promises deterministic rendering and AC11 requires byte-identical
  outputs across runs; this would also poison future golden-file tests (T6).
- Fix: sort on the full five-field atomic tuple with `None` ports mapped to
  `-1` (they sort before numeric ports). Verified byte-identical across hash
  seeds 0/5/42; xfail marker removed so the cross-seed subprocess test now
  asserts the fixed behavior.

### [IMPROVE] Drift-error exit 2 was silent on stderr — ✅ addressed (9f1255a)

- Where: `spend_sentinel/cli.py:175` (the R12 exit).
- Issue: every other exit-2 path (R2, R8, missing boto3) emits a one-line
  stderr diagnostic; the R12 path exited 2 with empty stderr, leaving CI logs
  with an unexplained failure whose reason is buried in stdout JSON. R12 does
  not mandate a diagnostic, so this is consistency/operability, not a spec
  violation.
- Fix: a count-only one-line notice pointing at `drift.errors` — resource
  addresses and error text are attacker-influenced and deliberately never
  reach stderr. Report-then-exit ordering (R12) unchanged; pinned in
  `test_r12_cli_error_exits_2_report_still_produced`.

### Judgment: A-i13 (AwsReader protocol defined in core, re-exported by adapters) — accepted

The spec's layout puts the `AwsReader` Protocol in `adapters/aws_reader.py`
while its Modularity rule forbids `core` from importing `adapters` — yet
`core/drift.py::detect` needs the type. These cannot all hold at once. The
coder's resolution (define in `core/drift.py`, re-export from
`adapters/aws_reader.py`, which stays the import point for implementations
and wiring) preserves the stronger rule — the dependency direction — and,
since Protocols are structural, changes nothing for implementers. This is the
cleanest reading of a self-contradictory layout; I endorse it. If pm-planner
later ratifies a layout amendment, nothing needs to move.

### [NIT] `tags` vs `tags_all`: default_tags will read as drift

`drift.py:196` compares state `values.tags` against the live tag set. With
provider `default_tags`, live AWS carries the defaults but state `tags` does
not (`tags_all` does), so every defaulted tag reports as drift. R9 names
`tags` explicitly, so this is spec-compliant as written — flagging as a
likely real-world false-positive for pm-planner to consider alongside the
deferred flags (a one-word spec change to `tags_all` would fix it). No code
change: the spec wins.

### [NIT] SG protocol strings are not normalized (`"6"` vs `"tcp"`)

`drift.py:222`: protocols compare as raw strings. A rule stored with the
numeric protocol (`"6"`) drifts against AWS's `"tcp"` even when equivalent.
Rare in Terraform-managed state and the spec is silent; noted only. Fixing it
would be scope creep (a protocol-number alias table).

### [NIT] Cross-region buckets land in `drift.errors` rather than being resolved

`boto3_reader.py:91`: `get_bucket_versioning` against a bucket in another
region raises a redirect-style `ClientError`, which propagates to
`drift.errors` (run continues, exit 2). The documented IAM list includes
`s3:GetBucketLocation` precisely for this, but the adapter doesn't call it.
Defensible for v1 (fail visible, not wrong), and the PR documents the unused
action; worth a line in the T9 docs.

### [NIT] `FixtureAwsReader` ships in the installed package

`adapters/fixture_reader.py` is test infrastructure but lives in
`spend_sentinel/` (the spec's layout lists only `aws_reader.py` and
`boto3_reader.py` under adapters). Harmless — it may even be useful for
offline dry-runs — but it is public API surface the moment it ships. Fine to
keep; just a conscious choice to note.

### [PRAISE] The R11 no-AWS-call guarantee is proven, not asserted

Three independent mechanisms: a monkeypatched reader factory that raises if
the CLI even tries to construct a reader; a subprocess check that neither
boto3, botocore, nor the adapter module appears in `sys.modules` after both
skip variants; and boto3 being genuinely absent from the environment so every
CLI test doubles as an R21 witness (with `skipif` guards keeping the suite
honest if boto3 ever appears). This is exactly how a "no call path exercised"
requirement should be tested.

### [PRAISE] Defensive core drift code

Iterative module walking (`state.py:64`) so hostile module nesting cannot
blow the Python stack; per-resource `except Exception` containment (A-i18)
so neither a reader failure nor a comparator bug kills the run; exception
summaries that are single-line, control-character-sanitized, and
length-capped before entering the report (`_summarize_exception`), with tests
driving hostile ANSI/newline payloads through the CLI.

### [PRAISE] Boto3 adapter test design

The stubbed-boto3 `RecordingClient` fails the test on any unstubbed API call
and records every method touched, letting `TestApiSurface` assert the
adapter's real call surface stays inside the documented five IAM actions
(and pins the exact four used). The adapter itself imports boto3 lazily in
the constructor, keeping module import safe without the `[aws]` extra.

### [PRAISE] Sensitive-values masking is tested at the right level

Both sides masked, per-attribute granularity preserved, empty mirror
structures correctly not treated as marks (A-i15), and a CLI-level canary
asserting the secret string never appears anywhere in stdout — not just that
the placeholder appears.

## Review commits on this branch (this increment)

| Commit | What |
| --- | --- |
| f893a0b | review: sort SG rule rendering on the full atomic tuple (BUG-4) |
| 9f1255a | review: one-line stderr notice when drift errors cause exit 2 |

Post-fix status: `python3 -m pytest` → 283 passed, 1 skipped, 0 xfailed;
`ruff check .` clean; `python3 -m mypy` (strict) clean.
