# Generation 3 of loop `vani-loop`

Final state GEN_END, outcome **NO_CANDIDATE**. Mode `sim`: the A/B runs on simulated traffic. Provider `sarvam`. Production prompt `43c6734389ea`, rubric `d67f34f6104e`.

## Steps

| Step | What happened |
| --- | --- |
| OBSERVED | Reused 3 stored scores of the production prompt. |
| AUDIT_PENDING | Skipped: the audit gate applies to going live on real traffic (PLAN_LOOP §L2.2, §L9); this build only simulates the live A/B, so an untrusted rubric may rank candidates here. |
| DIAGNOSED | Targets: frustration, closure, asked_for_human. |
| NO_CANDIDATE | The proposer returned no usable reply (Sarvam chat HTTP 422: {"error":{"message":"prompt_tokens (36718) + max_tokens (8192) = 44910 exceeds the model context window of 32000 tokens for sarvam-105b-conversations.","code":"unprocessable_entity_error","request_id":"20261010_5166781b-0689-4448-b153-1ad7495f1402"}}). |

## Diagnosis

| Rank | Criterion | Failure rate | 95% CI | Weight | Weighted cost | Leads | Target |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | frustration | 0.633 | [0.010, 1.000] | 3.0 | 1.900 | 3 | yes |
| 2 | closure | 0.667 | [0.503, 0.830] | 2.0 | 1.333 | 3 | yes |
| 3 | asked_for_human | 0.333 | [0.000, 0.987] | 2.0 | 0.667 | 3 | yes |
| 4 | requirement_captured | 0.083 | [0.000, 0.247] | 3.0 | 0.250 | 3 |  |
| 5 | claimed_human | 0.000 | [0.000, 0.000] | 4.0 | 0.000 | 3 |  |
| 6 | false_assurance | 0.000 | [0.000, 0.000] | 4.0 | 0.000 | 3 |  |
| 7 | wrong_language | 0.000 | [0.000, 0.000] | 2.0 | 0.000 | 3 |  |
| 8 | looping | 0.000 | [0.000, 0.000] | 3.0 | 0.000 | 3 |  |

## Candidates

None proposed.

## Live A/B (simulated traffic)

Not run.

## Spend

This generation INR 0.00; loop total INR 76.29 of the INR 150.00 cap.
