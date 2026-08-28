Verdict: WARN

Changes: 1 created, 1 deleted, 0 updated, 1 replaced (3 total)

## Cost

Monthly delta: $-24.60

| Resource | Action | Monthly delta (USD) |
| --- | --- | ---: |
| aws_instance.new | create | 7.59 |
| aws_nat_gateway.gone | delete | -32.85 |
| aws_db_instance.re | replace | 0.66 |

1 unpriced resources:

- aws_lambda_function.fn (aws_lambda_function): unsupported_type

## Policy

| Rule | Result | Detail |
| --- | --- | --- |
| max_monthly_delta | warn | monthly delta $-24.60 within limit $200; 1 unpriced resource(s) (treat_unpriced_as: warn): aws_lambda_function.fn |
| open_ingress | pass | no open ingress on non-allowed ports |
| deletions | warn | 2 deletion(s) (includes replaces): aws_nat_gateway.gone, aws_db_instance.re |
| drift | skipped | drift detection did not run |

spend-sentinel 0.1.0-test — pricing snapshot 2026.08.0-test (2026-08-27) — region us-east-1
