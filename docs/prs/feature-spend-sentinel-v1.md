# PR: spend-sentinel v1 — increment 1: plan ingestion (R1–R3)

Branch: `feature/spend-sentinel-v1` → `main`
Spec: `docs/specs/spend-sentinel-v1.md` (APPROVED, incremental delivery)

## Summary

First increment of spend-sentinel per the owner's gating instruction: tasks T1
(repo scaffold) and T2 (plan ingestion), covering requirements **R1, R2, R3
only**. The CLI gains a thin `analyze` command that loads a Terraform plan JSON
file (`terraform show -json` output), classifies its resource changes, and
prints a minimal JSON summary to stdout. Cost estimation, drift, policy,
renderers, region resolution (R8), and real CI are **not** in this increment
and are gated on owner review.

## Requirements coverage

| Req | Where | Notes |
| --- | --- | --- |
| R1 | `spend_sentinel/core/models.py` (`Plan`, `ResourceChange`, `Change`); `spend_sentinel/core/plan.py::load_plan`; `spend_sentinel/cli.py::analyze` | Parses `format_version` (1.x) and every `resource_changes` entry's address, type, provider, actions, `before`/`after`. A create-only single-resource plan yields `summary.changed == 1`. |
| R2 | `spend_sentinel/core/plan.py::load_plan` (raises `PlanError`); exit mapping in `spend_sentinel/cli.py::analyze` | Missing file, unreadable file, non-JSON content, JSON lacking `format_version`/`resource_changes`, and structurally invalid plans all exit 2 with a one-line stderr diagnostic naming the file and the problem; no traceback; nothing written to stdout. |
| R3 | `spend_sentinel/core/plan.py::classify_actions`, `summarize_plan`; counts in `spend_sentinel/core/models.py::PlanSummary` | `no-op` excluded; `create`/`delete`/`update`/`["delete","create"]` (replace) classified and counted as created/deleted/updated/replaced. |
| Security (spec §Security, this surface only) | `spend_sentinel/core/plan.py` | 50 MB size cap checked before reading (exit 2 beyond it); all parsing goes through pydantic models and fails closed with exit 2; diagnostics never echo file contents or environment variables (pydantic errors are re-summarized as field location + error kind only). |

T1 scaffold: `pyproject.toml` (hatchling; pinned `click`/`pydantic`/`PyYAML`;
`[aws]` extra declares boto3 but nothing imports it; dev deps
`pytest`/`pytest-cov`/`ruff`/`mypy`; console script `spend-sentinel`), ruff and
mypy (strict) config, `.gitignore`, and an empty CI workflow placeholder at
`.github/workflows/ci.yml` (real pipeline is T10, later increment).

Modularity per spec: `core/` is pure and adapter-free; `cli.py` is the only
wiring layer; file I/O is confined to `load_plan`, classification is pure.

## Not in this increment (deliberately)

- R8 region resolution (owner-scoped to a later increment; `plan.py` has no
  region code).
- Cost (T3), drift (T4), policy (T5), verdict/renderers (T6), full CLI flags
  (T7), tests (tester agent), docs (T9), real CI (T10).
- No `render/`, `pricing/`, `adapters/`, or `data/` packages yet — the spec's
  layout is honored as those tasks land; no empty stub modules were created.
- The R19 verdict structure. The `analyze` output here is deliberately minimal
  scaffolding: `{"summary": {created, deleted, updated, replaced, changed}, "resources": [{address, type, provider, action}]}`.

## Assumptions

- **A-i1 (`read` actions)**: `resource_changes` entries for data sources carry
  `actions: ["read"]`, which R3 does not list. Treated like `no-op` (excluded
  from classification and counts) rather than failing the whole plan, since
  data-source reads are not infrastructure changes. Any other unlisted action
  combination fails closed with exit 2 naming the entry and its actions.
- **A-i2 (replace ordering)**: both `["delete","create"]` and
  `["create","delete"]` (Terraform's create-before-destroy) classify as
  `replace`; R3 names only the former.
- **A-i3 (unknown sibling keys)**: the plan models validate the consumed subset
  strictly but ignore unrelated top-level/sibling keys (`planned_values`,
  `configuration`, …) — `extra="forbid"` would reject every real Terraform
  plan. "Fail closed" is applied to the fields we consume.
- **A-i4 (`format_version`)**: values other than `1` / `1.*` exit 2 with
  "unsupported plan format_version (expected 1.x)"; the offending value is not
  echoed (security: diagnostics never echo file contents).
- **A-i5 (diagnostics content)**: R2 requires naming "the file and the
  problem". Diagnostics include the file path, the failing field *location*
  (e.g. `resource_changes.0.change.actions`), and for unrecognized actions the
  resource address and the action strings themselves — these are minimal,
  attacker-visible-anyway identifiers needed to locate the problem; attribute
  *values* and file contents are never echoed. Flagging for reviewer attention
  since it is security-adjacent.
- **A-i6 (empty `resource_changes`)**: an empty list is valid (all counts 0,
  exit 0); only a *missing* `resource_changes` key is an R2 error.

## How to run

```bash
pip install -e ".[dev]"

# happy path: prints JSON summary, exit 0
terraform show -json plan.tfplan > plan.json
spend-sentinel analyze --plan plan.json

# error path: one-line stderr diagnostic, exit 2
spend-sentinel analyze --plan does-not-exist.json; echo $?

# checks (both clean on this branch)
ruff check .
mypy
```

Verified by hand: valid 5-change fixture (create/replace/update/delete/no-op →
counts 1/1/1/1, changed 4, no-op excluded, exit 0); malformed JSON, missing
file, missing `resource_changes`, wrong-typed `actions`, unknown action, and a
51 MB file all exit 2 with one-line stderr diagnostics and empty stdout.
