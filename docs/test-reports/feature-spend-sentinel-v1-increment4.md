# Test report — feature/spend-sentinel-v1, increment 4 (R13–R17)

Tester: tester-agent
Date: 2026-08-28
Scope: R13–R17 (policy schema, loader, four rule evaluators, RuleResult
contract), the owner decision recorded in bf00af8 (`limit_usd` defaults to
200; explicit `null` = no ceiling), AC5/AC6 matrices, AC10's policy half, and
suite maintenance. R18–R21 (verdict/exit mapping, renderers, CI posture)
remain unimplemented; policy results are informational in the CLI output by
design and exit codes were not asserted beyond documenting the current
exit-0 behavior.

## Result summary

- Full suite (increments 1–4): **383 tests — 382 passed, 1 skipped, 0
  xfailed**. ruff clean.
- BUG-4 (increment-3 report) was fixed by the reviewer (f893a0b — full-tuple
  sort) and my strict-xfail converted to a regular regression test; verified
  passing.
- **No new implementation bugs found in increment 4.** The policy surface
  held up under every probe: exact-boundary limits, hostile YAML (python
  tags, alias bombs, deep nesting), resolution-order edge cases, fail-closed
  port ranges, and message-injection attempts.
- Probed and confirmed safe: PyYAML `safe_load` constructs aliases as shared
  references, so "billion-laughs" alias expansion does NOT explode memory
  (a 10^8-leaf alias document loads in ~0.2 s and then fails schema
  validation cleanly) — regression-tested anyway.

## Coverage table

| Requirement / assumption | Tests | Result |
| --- | --- | --- |
| R13 built-in defaults (limit 200/owner decision, treat_unpriced warn, allowed_ports [], deletions warn + no protected, drift warn); empty/null file = defaults never "no rules"; partial file fills defaults | `test_r13_policy_schema.py::TestDefaults` (7 tests) | PASS |
| R13 explicit `limit_usd: null` = no ceiling; omitted = 200 | `test_r13_explicit_null_limit_means_no_ceiling`, `test_r13_omitted_limit_gets_200_default`, plus R14 evaluator tests | PASS |
| A-i24 `version` optional, only 1 accepted, rejection names `version` | `test_r13_version_optional_and_literal_1` | PASS |
| R13 error contract: unknown top-level key / unknown rule / unknown enum ×2 / wrong type ×4 / non-mapping rules — each exit-2-level error naming the offending key, single line | `TestSchemaErrors` (9-case parametrized + 4 singles) | PASS |
| R13 resolution order: --policy > ./spend-sentinel.yaml > built-ins (CWD-sensitive via monkeypatch.chdir); broken CWD file fails the run rather than falling back | `TestResolutionOrder` (4 tests) | PASS |
| Security: `!!python` tag fails closed with no code execution (canary file); alias-heavy YAML; 5000-deep nesting; 50 MB cap (A-i27, sparse file); binary content; diagnostics never echo values | `TestHostileYaml` (6 tests) | PASS |
| AC10 policy half: unknown rule `max_cpu` → exit 2, stderr names `max_cpu` and the file, empty stdout, no output files | `TestAc10PolicyHalf` | PASS |
| R14 block when delta > limit (by a cent); pass at exactly the limit; negative deltas; custom limit; null limit "no limit configured" | `test_r14_r16_rules.py::TestMaxMonthlyDeltaR14` (6 tests) | PASS |
| R14 unpriced escalation: warn/block/ignore under limit; block escalation with null limit; over-limit BLOCK never downgraded; messages name unpriced resources and the computed delta | same class (5 more tests) | PASS |
| R16 action matrix warn/block/ignore; replaces count as deletions; delete+replace listed together; protected_types beats action incl. ignore and via replace; unprotected types unaffected; >5 offenders elided | `TestDeletionsR16` (8 tests incl. parametrized) | PASS |
| AC6 through CLI: default warn naming the DB, protected-beats-ignore block (exit 0 documented as informational until R18) | `TestAc6ThroughCli` | PASS |
| R15/AC5: port 22 blocks naming address+port; 443-only passes; range 80–81 blocks with only 80 allowed; fully-allowed range passes; protocol -1 blocks despite allowed_ports; ::/0; non-open CIDR passes; SG references and prefix lists never open (A7) | `test_r15_open_ingress.py::TestAc5Matrix` (9 tests) | PASS |
| R15 action scope: update inspected; replace inspected in both orders (A-i22); delete and no-op exempt; non-SG types with decoy ingress ignored | `TestActionsScope` (5 tests) | PASS |
| R15 standalone types: `aws_security_group_rule` ingress blocks / egress exempt; `aws_vpc_security_group_ingress_rule` v4/v6/protocol -1/allowed-port | `TestStandaloneRuleResources` (6 tests) | PASS |
| A-i23 unverifiable ranges fail closed (missing/non-int/inverted ports); 0–65535 fast path; multiple offenders all listed | `TestFailClosedRanges` (6 tests) | PASS |
| R17 all four rules always present, ordered, with name/result/message (core + CLI key-set) | `test_r17_rule_results.py::TestEveryRulePresent` | PASS |
| R17/R11 drift rule: skipped when drift did not run; pass when clean; warn/block/ignore matrix; A-i26 "ignored by policy"; A-i25 read-error count suffix; addresses deduplicated | `TestDriftRule` (7 tests) | PASS |
| R17 message sanitization: newline/ANSI/CR, U+2028/NEL through the CLI, long lists elided ("and N more") | `TestMessageSanitization` (4 tests) | PASS |
| Maintenance: CLI top-level keys {summary, resources, cost, drift, policy} | `test_cli.py` (updated) | PASS |

## Bugs found

None in the increment-4 code. Previously reported BUG-1..BUG-4 are all fixed
and covered by regular regression tests.

Two observations verified as non-bugs:

- PyYAML alias expansion is reference-shared, not copied — no DoS via alias
  bombs (probed at 10^8 logical leaves: 0.2 s, then clean schema rejection).
- The exit-2-on-drift-errors stderr notice added by the reviewer (9f1255a)
  carries only a count, never addresses/error text — consistent with the
  diagnostics hardening; existing R12 tests still pass unchanged.

## Spec ambiguities / notes for pm-planner

(S1–S9 from earlier reports remain logged; S4 was resolved by the BUG-3 fix,
A-i21 resolved by the owner's limit_usd decision.)

- S10 (R15 `allowed_ports` semantics for protocol -1): the spec's "A rule
  with ... protocol `-1` BLOCKs" is implemented as an unconditional block even
  when `allowed_ports` covers the rule's declared port range (protocol -1
  means all protocols/ports, so this is the safe reading). Fine — worth one
  sentence in the T9 policy reference so users don't expect
  `allowed_ports: [0]` to exempt all-traffic rules.
- S11 (informational exit codes): until R18 lands, a policy BLOCK exits 0.
  Two CLI tests pin the current exit-0 with an "informational until R18"
  comment — the R18 increment should expect exactly those assertions to be
  updated (deliberate breadcrumbs, not accidental couplings). S7 (exit
  1-beats-2 precedence test) still pending for R18 as previously flagged.
- S12 (policy file discovery): the CWD auto-pickup of `spend-sentinel.yaml`
  means the effective policy depends on where CI invokes the tool; the R13
  behavior is spec'd, but T9 docs should call out that `--policy` pinning is
  the reproducible option for CI.

## Untestable in this increment

- R18 verdict/exit mapping (BLOCK→1, WARN→0, `--fail-on-warn`), AC2/AC6 exit
  codes, and the R12/A-5 precedence rule: not implemented (policy results are
  informational by design this increment).
- R19 verdict schema / R20 Markdown rendering of policy results: renderers
  do not exist yet; only the pre-R19 JSON `policy.rules` shape is covered.

## How to run

```bash
cd /home/claude/spend-sentinel
python3 -m pytest        # 382 passed, 1 skipped
ruff check tests/        # clean
```
