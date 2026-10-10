# Generation 1 of loop `vani-loop-r3`

Final state GEN_END, outcome **SPEND_CAP**. Mode `sim`: the A/B runs on simulated traffic. Provider `sarvam`. Production prompt `43c6734389ea`, rubric `d67f34f6104e`.

## Steps

| Step | What happened |
| --- | --- |
| OBSERVED | Observed the production prompt with base-only bot-test conversations (12 scored). |
| AUDIT_PENDING | Skipped: the audit gate applies to going live on real traffic (PLAN_LOOP §L2.2, §L9); this build only simulates the live A/B, so an untrusted rubric may rank candidates here. |
| DIAGNOSED | Targets: closure, frustration, requirement_captured. |
| PROPOSED | Re-test of 1 stored candidate(s) from loop `vani-loop-r2` (g1-c1) instead of new proposals. |
| GATED | 1 of 1 passed the patch gate. |
| BOT_TESTED | Estimated bot-test cost INR 47.80; INR 32.29 left under the cap. |
| SPEND_CAP | Stopped at the spend cap: the estimated bot-test cost exceeds the spend left under the cap |

## Diagnosis

| Rank | Criterion | Failure rate | 95% CI | Weight | Weighted cost | Leads | Target |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | closure | 0.833 | [0.670, 0.997] | 2.0 | 1.667 | 6 | yes |
| 2 | frustration | 0.483 | [0.059, 0.908] | 3.0 | 1.450 | 6 | yes |
| 3 | requirement_captured | 0.250 | [0.000, 0.503] | 3.0 | 0.750 | 6 | yes |
| 4 | wrong_language | 0.167 | [0.000, 0.493] | 2.0 | 0.333 | 6 |  |
| 5 | asked_for_human | 0.167 | [0.000, 0.493] | 2.0 | 0.333 | 6 |  |
| 6 | claimed_human | 0.000 | [0.000, 0.000] | 4.0 | 0.000 | 6 |  |
| 7 | false_assurance | 0.000 | [0.000, 0.000] | 4.0 | 0.000 | 6 |  |
| 8 | looping | 0.000 | [0.000, 0.000] | 3.0 | 0.000 | 6 |  |

## Candidates

| Candidate | Mode | Chars | Targets | Pre-checks | Gate (p per question) | Bot-test score | 95% CI | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| g1-r1 | replace_section | 343 | frustration | ok | alters_commitments=0.10, weakens_safety_rule=0.00, contradicts_instruction=0.00, raises_ask_limit=0.00, likely_longer_calls=0.30 | n/a | n/a | proposed |

Pick: none.

## Live A/B (simulated traffic)

Not run.

## Spend

This generation INR 7.66; loop total INR 117.71 of the INR 150.00 cap.
