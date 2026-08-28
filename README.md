# spend-sentinel

A Terraform drift & cost sentinel: a Python CLI, usable standalone or as a CI
step, that takes a Terraform plan (the JSON from `terraform show -json`),
estimates the monthly USD cost delta from a bundled AWS pricing snapshot,
optionally detects drift between Terraform state and live AWS attributes,
evaluates a small fixed set of policy gates from YAML, and emits a
PR-comment-ready Markdown report plus machine-readable JSON with exit codes CI
can gate on. Fully developed and testable offline.

## Quickstart

```bash
pip install .              # core (no AWS access needed)
pip install ".[aws]"       # adds boto3 for live drift detection

terraform plan -out=plan.tfplan
terraform show -json plan.tfplan > plan.json

# Markdown report to stdout; exit 0 PASS/WARN, 1 BLOCK, 2 error
spend-sentinel analyze --plan plan.json --skip-drift

# write both outputs
spend-sentinel analyze --plan plan.json --skip-drift \
  --out-json verdict.json --out-md report.md

# with drift detection (read-only AWS access, see docs/iam-policy.json)
terraform show -json > state.json
spend-sentinel analyze --plan plan.json --state state.json
```

Flags: `--plan` (required), `--region`, `--state`, `--skip-drift`, `--policy`,
`--out-json`, `--out-md`, `--fail-on-warn`, `--version`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Verdict PASS, or WARN without `--fail-on-warn` |
| 1 | Verdict BLOCK, or WARN with `--fail-on-warn` |
| 2 | Usage/runtime error (bad input file, unresolvable region, bad policy, drift read errors) |

An exit 1 takes precedence over the drift-read-error exit 2, so gating on
"blocked" stays reliable; the errors remain visible in the report's
`drift.errors`.

## CI usage (GitHub Actions)

```yaml
- name: spend-sentinel
  run: |
    terraform show -json plan.tfplan > plan.json
    spend-sentinel analyze --plan plan.json --skip-drift \
      --policy .ci/spend-sentinel.yaml \
      --out-json verdict.json --out-md report.md
- name: publish report
  if: always()
  run: cat report.md >> "$GITHUB_STEP_SUMMARY"
- name: upload verdict
  if: always()
  uses: actions/upload-artifact@v4
  with:
    name: spend-sentinel-verdict
    path: |
      verdict.json
      report.md
```

The `analyze` step fails the job on a BLOCK (exit 1); add `--fail-on-warn` to
gate on warnings too. **Pass `--policy` explicitly in CI**: without it the
tool picks up `spend-sentinel.yaml` from the current working directory when
present, so the effective policy would depend on the checkout/invocation
directory (and in PR workflows a branch could carry its own file).

## Pricing: what is (and is not) covered

Prices come from a bundled, versioned snapshot
(`spend_sentinel/data/pricing_snapshot.json`; see the verdict's `meta` block
for its version and date — values lag the live AWS pricing pages). Regions:
`us-east-1`, `us-west-2`, `eu-west-1`. Priced resource types:

| Type | Priced | Not priced (documented limitation) |
| --- | --- | --- |
| `aws_instance` | On-demand hourly (Linux, shared tenancy) × 730 h/month | Other platforms/tenancy, Savings Plans/RI/spot |
| `aws_ebs_volume` | Per-GB-month for gp2/gp3/io1/io2/st1/standard × size | Provisioned IOPS/throughput charges |
| `aws_db_instance` | Hourly per engine+class × 730 (×2 for `multi_az`) + storage per-GB-month | I/O, backups, aurora engines |
| `aws_nat_gateway` | Hourly × 730 | Data-processing charges |
| `aws_lb` | Hourly per type (application/network) × 730 | LCU charges |

Everything else — other resource types, price keys missing from the snapshot,
attributes unknown until apply — is never silently dropped: it appears in the
verdict's `cost.unpriced` list with a reason (`unsupported_type`,
`unknown_price_key`, `attributes_unknown`), and the Markdown report shows an
"N unpriced resources" line.

Region resolution: `--region` wins; otherwise the constant region from the
plan's provider configuration is used (single-region plans assumed — the
first constant found applies to all resources); otherwise the tool exits 2
asking for `--region`.

## Policy reference

Policy is YAML (`--policy <path>`; default `./spend-sentinel.yaml` when
present, else built-in defaults). An empty or absent file means the built-in
defaults below — never "no rules". Exactly these keys are allowed; anything
else exits 2 naming the offending key.

```yaml
version: 1
rules:
  max_monthly_delta:
    limit_usd: 200            # default 200; null disables the ceiling
    treat_unpriced_as: warn   # warn | ignore | block   (default warn)
  open_ingress:
    allowed_ports: [80, 443]  # default [] (nothing exempt)
  deletions:
    action: warn              # warn | block | ignore   (default warn)
    protected_types: []       # deletion of these types ALWAYS blocks
  drift:
    action: warn              # warn | block | ignore   (default warn)
```

- `max_monthly_delta` blocks when the total monthly delta exceeds
  `limit_usd`; when unpriced resources exist, `treat_unpriced_as` escalates
  the result to at least warn/block even under the limit.
- `open_ingress` blocks any created/updated security-group ingress open to
  `0.0.0.0/0` or `::/0` (inline `aws_security_group` rules,
  `aws_security_group_rule`, and `aws_vpc_security_group_ingress_rule`)
  unless **every** port in the rule's range is in `allowed_ports`. A rule
  with protocol `-1` (all traffic) **always blocks** — `allowed_ports`
  cannot exempt it, since it spans every port and protocol. Rules that
  reference other security groups or prefix lists are never treated as open.
- `deletions` warns (or blocks/ignores) listing each deleted resource;
  replaces (`delete`+`create`) count as deletions; deleting a type in
  `protected_types` blocks regardless of `action`.
- `drift` applies to detected drift; it evaluates to `skipped` when drift
  did not run.

## Drift detection

`--state <path>` takes `terraform show -json` output for the state and
compares a bounded set of attributes against live AWS: `aws_instance`
(`instance_type`, tags), `aws_security_group` (ingress/egress rule sets,
order-insensitive), `aws_s3_bucket` (tags, versioning status). Supported-type
resources missing from AWS are reported as drift of kind `missing`; other
types are counted as skipped. A per-resource read failure never kills the
run — it lands in `drift.errors` and the run exits 2 (unless a BLOCK already
exits 1).

AWS access is read-only: grant exactly the actions in
[`docs/iam-policy.json`](docs/iam-policy.json) (describe calls are not
resource-scopable, hence `Resource: "*"`). Recommended hardening: add an
`aws:RequestedRegion` condition limiting the credentials to the regions you
actually operate in. Credentials come from the standard AWS chain (env,
profile, instance role, OIDC in CI); the tool never accepts keys as flags and
never logs credential material. With `--skip-drift` (or no `--state`) no AWS
call is made and boto3 need not be installed.

Sensitive values: attributes marked in Terraform's `sensitive_values` render
as `(sensitive)` in both the JSON and Markdown outputs, including drift value
diffs.

## Outputs

- `--out-json` — machine-readable verdict, schema in
  [`docs/verdict-schema.md`](docs/verdict-schema.md); deterministic,
  monetary values as 2-decimal strings.
- `--out-md` — self-contained Markdown PR comment: verdict header, cost
  table, unpriced list, drift table (when drift ran), policy table. Long
  tables truncate with an "…and N more" line. With neither flag, the
  Markdown goes to stdout.
