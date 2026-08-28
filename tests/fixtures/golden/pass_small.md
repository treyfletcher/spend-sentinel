Verdict: PASS

Changes: 2 created, 0 deleted, 0 updated, 0 replaced (2 total)

## Cost

Monthly delta: $9.19

| Resource | Action | Monthly delta (USD) |
| --- | --- | ---: |
| aws_instance.web | create | 7.59 |
| aws_ebs_volume.data | create | 1.60 |

## Policy

| Rule | Result | Detail |
| --- | --- | --- |
| max_monthly_delta | pass | monthly delta $9.19 within limit $200 |
| open_ingress | pass | no open ingress on non-allowed ports |
| deletions | pass | no deletions in plan |
| drift | skipped | drift detection did not run |

spend-sentinel 0.1.0-test — pricing snapshot 2026.08.0-test (2026-08-27) — region us-east-1
