# Case study: spend-sentinel

*How I designed a four-agent AI development pipeline and directed it to ship a production-quality Terraform cost & drift gate — and what the process caught along the way.*

## The problem

Cloud cost and security regressions don't announce themselves. They land quietly in Terraform PRs: an instance resized three sizes up, a NAT gateway added to every AZ, a security group opened to `0.0.0.0/0` "temporarily." By the time the bill or the pentest finds them, they've been in production for a quarter. Reviewers can't be expected to price a plan diff in their heads or spot every open ingress rule in a 500-resource change.

spend-sentinel is a CI gate that does that work mechanically: it parses a Terraform plan, prices the monthly cost delta, detects drift between state and live AWS, evaluates policy rules (cost ceiling, open ingress, deletions, drift), and emits a PR-comment-ready Markdown verdict plus machine-readable JSON, with exit codes CI can gate on.

## What makes the design worth reading

**Offline determinism as a first-class requirement.** Every feature is testable with zero network access and zero AWS credentials — enforced in CI by scrubbing the credential environment. Pricing comes from a versioned, bundled snapshot rather than the live AWS Pricing API; that was a deliberate tradeoff (price freshness vs. deterministic, offline-testable CI runs), documented in the spec with the `PricingSource` protocol left open so a live adapter is additive, not a rewrite. All AWS drift reads go through a three-method `AwsReader` protocol; boto3 exists in exactly one module and is an optional install.

**Untrusted input as the threat model.** Plan JSON, state JSON, and policy YAML all arrive from a PR branch — attacker-influenced by definition. Everything parses through strict pydantic models that fail closed to exit 2; files are size-capped; YAML is `safe_load` only; resource addresses and tag values are escaped before they reach the Markdown PR comment; Terraform `sensitive_values` markers are honored end-to-end, drift diffs included. The tool ships a least-privilege IAM policy containing exactly the five read-only actions its adapter can call — and a test pins the adapter's API surface to that list.

**Small, honest scope.** v1 prices five resource types in three regions and detects drift on three types, with everything else routed to an explicit `unpriced`/`skipped` taxonomy — nothing silently dropped. The spec's "Out of scope" section is as long as its requirements, on purpose.

## How it was built

I designed a four-agent AI development pipeline — PM/planner, coder, tester, PR reviewer — with the role boundaries and quality gates of a real team, then directed it through five incremental deliveries:

- The **PM agent** turned the product brief into a spec: 21 numbered, testable requirements, 12 Given/When/Then acceptance criteria, a security section, and explicit module boundaries.
- The **coder** implemented each increment on feature branches (in parallel git worktrees once testing started), forbidden from writing tests or touching the spec.
- The **tester** wrote suites against the acceptance criteria and the code as written, forbidden from patching application code — failing tests became bug reports, not quiet fixes.
- The **PR reviewer** reviewed each increment's diff plus a whole-PR integration pass, empowered to commit its own fixes for confirmed blockers — with each fix pinned by a test in the same commit.

I sat above the pipeline as the engineering lead: approving specs, sequencing increments (R1–R3 first, to judge output quality before committing to the rest), resolving flagged ambiguities (e.g. ratifying a $200 default cost ceiling), deferring spec amendments deliberately, and holding the merge decision. Every stage produced an artifact — spec, PR description, test report, review — all preserved in `docs/` as the audit trail.

## What the process caught

Seven real bugs were found and fixed before anything reached main. The interesting ones are the three injection classes the adversarial review caught after the tests were already green:

| Found by | Bug | Why it matters |
|---|---|---|
| Tester | Deeply nested JSON escaped error handling as a `RecursionError` traceback | Broke the fail-closed exit-code contract |
| Tester | Non-finite numbers (`NaN`, `1e400`) crashed the cost estimator | Hostile plan could kill the gate |
| Tester | Negative resource sizes produced negative cost deltas | A crafted plan could offset real cost under the ceiling |
| Tester | Security-group rules rendered in hash-seed-dependent order | Broke byte-identical output determinism |
| Reviewer | Newlines in resource addresses could spoof extra stderr diagnostic lines | Log/diagnostic injection |
| Reviewer | YAML `allowed_ports: [true]` silently coerced to port 1 | A typo would have *exempted port 1* from the open-ingress gate |
| Reviewer | Markdown escaping missed `[ ]` — crafted addresses rendered live links/image beacons in the PR comment | Phishing/beacon injection into the review surface |

Every fix landed with a regression test. The bug that best justifies the whole design is the `allowed_ports` coercion: type-lax config parsing turning a typo into a security exemption is exactly the class of failure that policy tooling exists to prevent — and it was caught by a review gate, not by luck.

## By the numbers

~2,500 lines of application code guarded by ~5,700 lines of tests (458 tests, all 12 acceptance criteria covered, whole suite runs offline and credential-free in under 10 seconds); 48 commits across 5 increments; strict mypy and ruff clean throughout; 7 pre-merge bugs, 0 known bugs at release.

## What v2 looks like

A live AWS Pricing API adapter behind the existing `PricingSource` protocol with the snapshot as fallback; GitHub PR comment upsert and a marketplace Action; more priced types and drift comparators via the existing registries; per-resource region resolution for multi-region plans. Each lands behind an interface that already exists — which was the point of drawing the boundaries where they are.
