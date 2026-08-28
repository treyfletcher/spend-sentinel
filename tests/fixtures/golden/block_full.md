Verdict: BLOCK

Changes: 1 created, 0 deleted, 1 updated, 0 replaced (2 total)

## Cost

Monthly delta: $16.43

| Resource | Action | Monthly delta (USD) |
| --- | --- | ---: |
| aws_security_group.app | create | 0.00 |
| aws_lb.edge | update | 16.43 |

## Drift

| Resource | Kind | Attribute | State value | Live value |
| --- | --- | --- | --- | --- |
| aws_instance.web | changed | instance_type | t3.micro | t3.medium |
| aws_instance.web | changed | tags | (sensitive) | (sensitive) |
| aws_s3_bucket.gone | missing | - | null | null |

1 resource(s) skipped:

- aws_lambda_function.fn (aws_lambda_function): unsupported_type

1 read error(s):

- aws_instance.err: AuthFailure: denied

## Policy

| Rule | Result | Detail |
| --- | --- | --- |
| max_monthly_delta | pass | monthly delta $16.43 within limit $200 |
| open_ingress | block | open ingress (0.0.0.0/0 or ::/0) on non-allowed ports: aws_security_group.app (port 22) |
| deletions | pass | no deletions in plan |
| drift | warn | 2 drift(s) on: aws_instance.web, aws_s3_bucket.gone; 1 read error(s) |

spend-sentinel 0.1.0-test — pricing snapshot 2026.08.0-test (2026-08-27) — region us-east-1
