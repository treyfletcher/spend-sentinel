# Spec: spend-sentinel v1 — Terraform drift & cost sentinel

Status: DRAFT
Branch: feature/spend-sentinel-v1

## Summary

spend-sentinel is a Python CLI, usable standalone or as a CI step, that takes a Terraform plan (the JSON emitted by `terraform show -json <planfile>`), estimates the monthly USD cost delta of the change from a bundled AWS pricing snapshot, optionally detects drift between Terraform state and live AWS resource attributes for a bounded set of resource types, evaluates a small fixed set of policy gates from a YAML config (cost ceiling, open security-group ingress, deletions, drift severity), and emits a structured verdict as PR-comment-ready Markdown plus machine-readable JSON, with exit codes CI can gate on. v1 is designed to be developed and tested entirely offline: all AWS interaction (drift reads) sits behind a narrow injectable interface with a fixture-backed fake, and pricing comes from a versioned snapshot file shipped in the package.

## Requirements

Each requirement is verifiable offline with fixture files unless noted.

**Plan ingestion**

- R1: `spend-sentinel analyze --plan <path>` parses a Terraform plan JSON file (output of `terraform show -json`, `format_version` "1.x") and extracts, for every entry in `resource_changes`, its address, type, provider, change actions, and `before`/`after` attribute maps. A plan containing only a `aws_instance` create produces a verdict JSON whose `resources.changed` count is 1.
- R2: A missing plan file, unreadable file, non-JSON content, or JSON lacking `format_version`/`resource_changes` causes exit code 2 with a one-line diagnostic on stderr naming the file and the problem. No verdict output files are written in this case.
- R3: Resource changes with `actions: ["no-op"]` are excluded from cost, drift, and policy evaluation; all other action combinations (`create`, `delete`, `update`, `["delete","create"]` replace) are classified and counted in the verdict summary as created/deleted/updated/replaced.

**Cost estimation**

- R4: The tool prices exactly these resource types in v1, from a bundled pricing snapshot (JSON data file packaged with the tool, containing at minimum regions `us-east-1`, `us-west-2`, `eu-west-1`):
  - `aws_instance`: on-demand hourly rate for `instance_type` (Linux, shared tenancy) × 730 hours/month.
  - `aws_ebs_volume`: per-GB-month rate for `type` (gp2, gp3, io1, io2, st1, standard) × `size`. Provisioned IOPS/throughput charges are not priced (documented limitation).
  - `aws_db_instance`: on-demand hourly rate for `instance_class` and `engine` × 730, doubled when `multi_az` is true, plus per-GB-month storage rate for `storage_type` × `allocated_storage`.
  - `aws_nat_gateway`: hourly rate × 730. Data-processing charges are not priced (documented limitation).
  - `aws_lb`: hourly rate for `load_balancer_type` (application, network) × 730. LCU charges are not priced (documented limitation).
- R5: Monthly cost math is deterministic: cost = rate × 730 (hours) or rate × GB, computed as Decimal and rounded half-up to cents at the resource level. Given the bundled snapshot and a fixture plan, the same delta is produced on every run (asserted to the cent in tests).
- R6: The cost delta per resource is: create → +monthly cost of `after`; delete → −monthly cost of `before`; update/replace → (cost of `after`) − (cost of `before`). The verdict reports the total delta and a per-resource breakdown.
- R7: Resources of any other type, priced types with an unknown key (e.g. an `instance_type` absent from the snapshot), and priced types with unknown-until-apply attributes are never silently dropped: each appears in the verdict's `cost.unpriced` list with address, type, and reason (`unsupported_type` | `unknown_price_key` | `attributes_unknown`), and the Markdown output shows an "N unpriced resources" line whenever the list is non-empty.
- R8: Pricing region resolution: `--region <r>` flag wins; otherwise the region is taken from the plan's provider `configuration` block when it is a constant; otherwise exit 2 with a diagnostic telling the user to pass `--region`. A region absent from the snapshot exits 2 naming the region and the snapshot's supported regions.

**Drift detection**

- R9: With `--state <path>` (output of `terraform show -json` on the state), the tool compares state attributes against live AWS values for exactly these resource types and attribute allowlists:
  - `aws_instance`: `instance_type`, `tags`.
  - `aws_security_group`: the set of ingress and egress rules (protocol, from_port, to_port, CIDR blocks), compared order-insensitively.
  - `aws_s3_bucket`: `tags`, and versioning status (drift when live versioning differs from state's `versioning` attribute).
  All live reads go through a single `AwsReader` interface; the boto3 implementation is selected only in production wiring, and tests use a fake reader loaded from fixture JSON.
- R10: Each detected drift is reported with resource address, attribute path, state value, and live value. A state resource of a supported type that does not exist in AWS is reported as drift of kind `missing`. Resource types outside R9's list are skipped and counted in the verdict as `drift.skipped` with reason `unsupported_type`.
- R11: When `--state` is omitted or `--skip-drift` is passed, no AWS call path is exercised (verified in tests by an `AwsReader` stub that raises on any call), the verdict's `drift` section carries `status: "skipped"`, and drift policy rules evaluate to `skipped` rather than pass or fail.
- R12: An AWS read failure (auth error, throttle, timeout) does not crash the run: the affected resource is reported as `drift.errors` with the exception summary, the Markdown/JSON verdict is still produced, and the exit code is 2 unless a policy rule already yields BLOCK (exit 1 takes precedence over 2).

**Policy gates**

- R13: Policy is a YAML file (`--policy <path>`, default `spend-sentinel.yaml` in the CWD if present, else built-in defaults) supporting exactly these keys and no others:
  ```yaml
  version: 1
  rules:
    max_monthly_delta:            # BLOCK if cost delta exceeds limit
      limit_usd: 200
      treat_unpriced_as: warn     # warn | ignore | block  (default warn)
    open_ingress:                 # BLOCK on 0.0.0.0/0 or ::/0 ingress
      allowed_ports: [80, 443]    # exempt these from blocking (default [])
    deletions:                    # deletions of any resource
      action: warn                # warn | block | ignore  (default warn)
      protected_types: []         # types whose deletion always BLOCKs
    drift:
      action: warn                # warn | block | ignore  (default warn)
  ```
  An unknown top-level key, unknown rule name, unknown enum value, or wrong type exits 2 with a diagnostic naming the offending key.
- R14: `max_monthly_delta` BLOCKs when total delta > `limit_usd`; when unpriced resources exist and `treat_unpriced_as` is `warn`/`block`, the rule result is at least WARN/BLOCK respectively even if the priced delta is under the limit.
- R15: `open_ingress` BLOCKs when any created or updated `aws_security_group` or `aws_security_group_rule` in the plan's `after` state contains an ingress rule with CIDR `0.0.0.0/0` or `::/0`, unless every port in the rule's range is in `allowed_ports`. A rule with `from_port`/`to_port` spanning any non-allowed port, or protocol `-1`, BLOCKs.
- R16: `deletions` yields WARN (or BLOCK per config) listing each deleted resource; deleting a resource whose type is in `protected_types` yields BLOCK regardless of `action`. Replaces (`["delete","create"]`) count as deletions for this rule.
- R17: Every rule evaluation appears in the verdict with rule name, result (`pass` | `warn` | `block` | `skipped`), and a human-readable message naming the offending resources.

**Verdict, output, exit codes**

- R18: The overall verdict is BLOCK if any rule blocks, else WARN if any rule warns, else PASS. Exit codes: 0 for PASS and (by default) WARN, 1 for BLOCK, 2 for usage/runtime errors (per R2/R8/R12/R13). `--fail-on-warn` makes WARN exit 1.
- R19: `--out-json <path>` and `--out-md <path>` write the JSON verdict and Markdown report; with no flags the Markdown goes to stdout. The JSON structure is documented in `docs/verdict-schema.md` and includes: `verdict`, `summary` (created/deleted/updated/replaced counts), `cost` (`monthly_delta_usd`, `breakdown[]`, `unpriced[]`), `drift` (`status`, `drifts[]`, `skipped[]`, `errors[]`), `policy` (`rules[]`), and `meta` (tool version, pricing snapshot version/date, region).
- R20: The Markdown report is a self-contained PR comment: a one-line verdict header with emoji-free status text (e.g. `Verdict: BLOCK`), a cost table (resource, action, monthly delta), an unpriced list when non-empty, a drift table when drift ran, and a policy results table. It renders under 65,536 characters for plans up to 500 resource changes (breakdown truncates past 50 rows with a "…and N more" line).
- R21: The whole test suite runs with no network access and no AWS credentials (enforced in CI by running tests with `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` unset and no default region). `boto3` is imported only inside the live-adapter module, so `--skip-drift` runs work even if boto3 is not installed.

## Out of scope

- Live AWS Pricing API adapter. Justification: the Pricing API's offer files are hundreds of MB, need pagination/filter tuning per service, and add auth + network variability to CI runs; a versioned snapshot gives deterministic, offline-testable results at v1 quality. The `PricingSource` interface (Modularity notes) is designed so a live adapter can be added without touching the estimator.
- Pricing beyond the five resource types in R4; usage-based charges (NAT data processing, LCUs, gp3 provisioned IOPS/throughput, data transfer); Savings Plans / RI / spot pricing; non-Linux or dedicated-tenancy EC2; regions beyond the three bundled.
- Drift detection beyond the three types/attribute allowlists in R9; auto-remediation of drift.
- A general policy/rules engine (OPA, CEL, user expressions) — v1 is the fixed rule set in R13 only.
- Posting to the GitHub API, PR comment upsert, GitHub Action packaging (marketplace action.yml). CI integration is exit codes + files/stdout only.
- Terraform Cloud/Enterprise integration; non-AWS providers; cost "current total" of the whole state (only the delta of the plan).
- Snapshot refresh tooling (`tools/refresh-pricing` script that regenerates the snapshot from the AWS Pricing API) — Future work, listed here so the coder does not build it; v1's snapshot is hand-curated with sources cited in the data file's `meta` block.
- Config for custom hours/month, currency conversion, cost allocation by tag.

## Dependencies

- External:
  - Python 3.11+ (pin CI matrix to 3.11 and 3.12).
  - `click >= 8.1, < 9` — CLI.
  - `pydantic >= 2.7, < 3` — plan/verdict/policy models and validation.
  - `PyYAML >= 6.0, < 7` — policy config.
  - `boto3 >= 1.34, < 2` — optional extra (`spend-sentinel[aws]`), imported only in the live drift adapter.
  - Dev: `pytest >= 8, < 9`, `pytest-cov`, `ruff >= 0.5`, `mypy >= 1.10`.
  - Packaging: `pyproject.toml` with hatchling; console script entry point `spend-sentinel`.
- Internal: greenfield repo — no existing modules. Package root `spend_sentinel/`; pricing snapshot packaged as data file `spend_sentinel/data/pricing_snapshot.json`.
- Ordering:
  - T1 (scaffold) blocks everything.
  - T2 (plan parser) blocks T3, T5, T6.
  - T3 (pricing) and T4 (drift) are independent of each other; both block T5 (policy) only insofar as policy consumes their result models — T5 can start from the models defined in T2/T3/T4.
  - T6 (verdict/renderers) needs T3, T4, T5 result models.
  - T7 (CLI wiring) needs T2–T6. T8 (e2e fixtures/tests) needs T7. T9 (docs) and T10 (CI) can trail but T10 needs T8.

## Task breakdown

- T1: Repo scaffold — `pyproject.toml` (deps pinned as above, `[aws]` extra, entry point), package layout per Modularity notes, ruff/mypy config, `pytest` wiring, empty CI workflow placeholder. (Supports all Rs.)
- T2: Plan ingestion — pydantic models for the subset of `terraform show -json` we consume; loader with the error handling of R2; action classification of R3; region extraction of R8. Unit tests with fixture plans: create-only, delete, update, replace, no-op, malformed. (R1–R3, R8.)
- T3: Pricing — `PricingSource` protocol; `SnapshotPricingSource` reading the bundled JSON; hand-curated snapshot covering a documented matrix (≥ 10 common EC2 instance types, EBS volume types, ≥ 5 RDS instance classes for postgres/mysql, NAT gateway, ALB/NLB, in the three regions) with `meta.version`, `meta.snapshot_date`, `meta.sources`; estimator implementing R4–R7 including Decimal rounding and the unpriced taxonomy. Unit tests assert exact cents. (R4–R8.)
- T4: Drift — `AwsReader` protocol (three narrow methods, see Modularity notes); `FixtureAwsReader` for tests; `Boto3AwsReader` in an isolated module; per-type comparators for R9's allowlists including order-insensitive SG rule comparison; `missing` and `skipped`/`errors` handling of R10–R12. Unit tests entirely on fixtures. (R9–R12, R21.)
- T5: Policy — pydantic-validated config schema of R13 with defaults; the four rule evaluators of R14–R16; rule result model of R17. Table-driven unit tests per rule, including allowed_ports edge cases (port ranges, protocol -1) and `treat_unpriced_as`. (R13–R17.)
- T6: Verdict & renderers — aggregate model of R19; overall verdict logic of R18; JSON serializer; Markdown renderer with truncation rule of R20; `docs/verdict-schema.md`. Golden-file tests for both outputs. (R17–R20.)
- T7: CLI — `analyze` command wiring flags `--plan`, `--state`, `--skip-drift`, `--policy`, `--region`, `--out-json`, `--out-md`, `--fail-on-warn`, `--version`; exit-code mapping of R18 with the R12 precedence rule; stderr diagnostics. (R2, R8, R11, R18, R19.)
- T8: End-to-end fixture scenarios exercised through the CLI entry point (subprocess or CliRunner): (a) small create plan → PASS, (b) big create plan breaching $200 → BLOCK exit 1, (c) SG with 0.0.0.0/0 on port 22 → BLOCK, (d) deletions → WARN exit 0 and exit 1 with `--fail-on-warn`, (e) drift via FixtureAwsReader → WARN, (f) malformed plan → exit 2, (g) `--skip-drift` with boto3 absent. (Acceptance criteria backing; R21.)
- T9: Docs — README: quickstart, CI usage snippet (GitHub Actions job step consuming exit code and uploading the Markdown as artifact/step summary), pricing limitations from R4/R7, policy reference from R13; `docs/iam-policy.json` least-privilege read-only policy (see Security). (R4 limitations, R13, Security.)
- T10: CI workflow — GitHub Actions: ruff, mypy, pytest with coverage on 3.11/3.12, with AWS env vars scrubbed per R21.

## Acceptance criteria

- AC1 (R1, R3, R6, R18, R19): Given fixture plan `create_small.json` (one t3.micro instance, one 20 GB gp3 volume, us-east-1) and default policy, When `spend-sentinel analyze --plan create_small.json --skip-drift --out-json v.json` runs, Then exit code is 0, `v.json` has `verdict: "PASS"`, `summary.created == 2`, and `cost.monthly_delta_usd` equals the snapshot-derived sum to the cent.
- AC2 (R4–R6, R14, R18): Given fixture plan `create_expensive.json` whose priced delta exceeds $200 and policy `limit_usd: 200`, When analyzed, Then exit code is 1, verdict is BLOCK, and the `max_monthly_delta` rule result is `block` with the computed delta in its message.
- AC3 (R6): Given fixture plan `update_resize.json` changing an instance from t3.large to t3.xlarge, When analyzed, Then the resource's breakdown entry equals cost(t3.xlarge) − cost(t3.large) and the total delta matches it.
- AC4 (R7): Given fixture plan `mixed_unpriced.json` containing an `aws_lambda_function` and an `aws_instance` with an instance type absent from the snapshot, When analyzed, Then `cost.unpriced` has exactly 2 entries with reasons `unsupported_type` and `unknown_price_key`, and the Markdown contains a "2 unpriced resources" line.
- AC5 (R15): Given a plan creating a security group with ingress `0.0.0.0/0` on ports 22–22 and policy `allowed_ports: [80, 443]`, When analyzed, Then the `open_ingress` rule is `block`, its message contains the SG address and port 22, and exit code is 1; and Given the same plan with the rule only on port 443, Then the rule is `pass`.
- AC6 (R16, R18): Given a plan deleting an `aws_db_instance` with `deletions.action: warn` and empty `protected_types`, When analyzed, Then verdict is WARN and exit code 0; with `--fail-on-warn` exit code is 1; and with `protected_types: [aws_db_instance]` verdict is BLOCK regardless of `action`.
- AC7 (R9, R10): Given state fixture `state_sg.json` and a `FixtureAwsReader` whose live SG data has one extra ingress rule, When drift runs, Then exactly one drift is reported for that SG with attribute path identifying the rule set, and an S3 bucket in state but absent from fixtures is reported as kind `missing`.
- AC8 (R11, R21): Given `--skip-drift` and an injected AwsReader stub that raises on any method call, When analyzed, Then no exception propagates, `drift.status == "skipped"`, the `drift` policy rule is `skipped`, and the run succeeds with boto3 uninstalled.
- AC9 (R12): Given a FixtureAwsReader configured to raise an auth error for one resource, When analyzed with default policy, Then the verdict is still produced, that resource appears in `drift.errors`, and exit code is 2; if the same run also breaches the cost limit, exit code is 1.
- AC10 (R2, R13): Given a nonexistent plan path, When analyzed, Then exit 2 and stderr names the path; Given a policy file containing an unknown rule `max_cpu`, Then exit 2 and stderr names `max_cpu`; no output files are created in either case.
- AC11 (R19, R20): Given any fixture scenario with `--out-md` and `--out-json`, When run twice, Then both outputs are byte-identical across runs (deterministic), the JSON matches `docs/verdict-schema.md`, and a 500-change synthetic plan produces Markdown under 65,536 characters with a truncation line.
- AC12 (R8): Given a plan whose provider region is a constant `eu-west-1`, When run without `--region`, Then eu-west-1 prices are used; Given a plan with no resolvable region and no flag, Then exit 2 with a message telling the user to pass `--region`.

## Security considerations

- Read-only AWS access only. Ship `docs/iam-policy.json` containing exactly: `ec2:DescribeInstances`, `ec2:DescribeSecurityGroups`, `s3:GetBucketVersioning`, `s3:GetBucketTagging`, `s3:GetBucketLocation` on `Resource: "*"` (describe calls are not resource-scopable), with a doc note recommending an `aws:RequestedRegion` condition. The Boto3AwsReader must call no APIs outside this list — a unit test asserts the fake's method surface equals the documented action list.
- No secrets handled directly: credentials come from the standard AWS chain (env/instance profile/OIDC in CI); the tool never accepts keys as CLI flags and never logs credential material. Diagnostics must not echo environment variables.
- Untrusted input surfaces: plan JSON, state JSON, and policy YAML may come from a PR branch. Parse YAML with `yaml.safe_load` only. Enforce a 50 MB size cap on plan/state files (exit 2 beyond it). All parsing goes through pydantic models — unknown/hostile structures fail closed with exit 2, never partial evaluation.
- Markdown injection: resource addresses and tag values from the plan are attacker-influenced in a PR context and are rendered into a PR comment. Escape pipe, backtick, and HTML-significant characters in all plan/state-derived strings placed into Markdown tables so a crafted resource name cannot inject markup or spoof the verdict line.
- Data exposure: plan `before`/`after` may contain sensitive values. Honor Terraform's `sensitive_values` markers — any attribute marked sensitive renders as `(sensitive)` in both Markdown and JSON outputs, including drift value diffs.
- Policy bypass hardening: policy file defaults are the safe direction (deletions warn, unpriced warn); an empty or absent policy file means built-in defaults, not "no rules".

## Modularity notes

Package layout (all public interfaces typed; `core` never imports `adapters`):

```
spend_sentinel/
  cli.py                  # click commands; only module that wires adapters
  core/
    models.py             # ResourceChange, PlanSummary, Verdict, RuleResult, Drift, CostLine (pydantic)
    plan.py               # load_plan(path) -> Plan; region resolution
    cost.py               # estimate(plan, pricing: PricingSource, region) -> CostReport
    drift.py              # detect(state, reader: AwsReader) -> DriftReport; per-type comparators
    policy.py             # load_policy(path|None) -> Policy; evaluate(policy, cost, drift, plan) -> [RuleResult]
    verdict.py            # combine(...) -> Verdict; exit_code(verdict, errors, fail_on_warn) -> int
  render/
    markdown.py           # render_md(verdict) -> str  (escaping lives here)
    jsonout.py            # render_json(verdict) -> str
  pricing/
    source.py             # class PricingSource(Protocol): get_rate(region, service_key, price_key) -> Decimal | None
    snapshot.py           # SnapshotPricingSource (bundled data file)
  adapters/
    aws_reader.py         # class AwsReader(Protocol):
                          #   get_instance(instance_id) -> InstanceAttrs | None
                          #   get_security_group(sg_id) -> SecurityGroupAttrs | None
                          #   get_bucket(name) -> BucketAttrs | None
    boto3_reader.py       # Boto3AwsReader; the ONLY module importing boto3
  data/
    pricing_snapshot.json
tests/
  fixtures/ (plans/, states/, aws_responses/, policies/, golden/)
```

- `PricingSource` and `AwsReader` are `typing.Protocol`s; production wiring happens only in `cli.py`, so every core module is testable by constructing objects directly with fakes.
- All monetary values are `decimal.Decimal` end to end; floats appear only at JSON serialization (rendered as strings with 2 decimals in JSON to keep determinism — document in the schema).
- Comparators in `core/drift.py` are a registry `dict[str, Comparator]` keyed by resource type so adding a type later is one entry + one adapter method.
- Renderers take the finished `Verdict` model only — no business logic in `render/`, enabling golden-file testing.

## Open questions / assumptions

- A1 (region): Assumed single-region plans; the first constant provider region found is used for all pricing. Multi-region/aliased-provider plans are not split per-resource in v1 — `--region` overrides globally. Flagged in README.
- A2 (drift input): Drift compares **state** (`terraform show -json` of the state file, passed via `--state`) against live AWS — not the plan file — because the plan's `before` is already a refreshed view in many workflows. If Trey prefers deriving drift from the plan's `prior_state` block when present, that is a small parser change; v1 keeps the explicit `--state` flag for clarity.
- A3 (pricing accuracy): The bundled snapshot is hand-curated from public AWS pricing pages at a recorded snapshot date; values will lag reality. Accepted for a portfolio v1 — the verdict's `meta` block exposes snapshot version/date so consumers can judge staleness. Live Pricing API adapter deferred (see Out of scope) because it breaks offline determinism and adds large-payload handling for marginal v1 value.
- A4 (730 hours/month): Fixed constant, not configurable in v1 — matches AWS's own monthly-estimate convention.
- A5 (exit-code precedence): When both a BLOCK and a runtime error (e.g. partial drift failure) occur, exit 1 wins over 2 so CI gating on "blocked" stays reliable; the error is still visible in `drift.errors`. Chosen because a BLOCK is actionable regardless of the partial failure.
- A6 (WARN exits 0): Default CI posture is non-blocking warnings; teams opt into strictness with `--fail-on-warn`. This matches the "warn on deletions" framing in the feature request.
- A7 (security-group rule sources): `open_ingress` inspects only IPv4/IPv6 CIDRs; rules referencing other SGs or prefix lists are never treated as open. Both inline `aws_security_group.ingress` and standalone `aws_security_group_rule`/`aws_vpc_security_group_ingress_rule` resources are covered.
- A8 (single command): One `analyze` command with flags, rather than `cost`/`drift`/`check` subcommands — smaller surface for v1; subcommands can be added later without breaking `analyze`.
