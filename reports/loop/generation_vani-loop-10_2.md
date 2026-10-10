# Generation 2 of loop `vani-loop-10`

Final state GEN_END, outcome **NO_CANDIDATE**. Mode `sim`: the A/B runs on simulated traffic. Provider `sarvam`. Production prompt `43c6734389ea`, rubric `d67f34f6104e`.

## Steps

| Step | What happened |
| --- | --- |
| OBSERVED | Reused 19 stored scores of the production prompt. |
| AUDIT_PENDING | Skipped: the audit gate applies to going live on real traffic (PLAN_LOOP §L2.2, §L9); this build only simulates the live A/B, so an untrusted rubric may rank candidates here. |
| DIAGNOSED | Targets: closure, frustration, requirement_captured. |
| PROPOSED | 3 proposed, 2 passed the pre-checks. |
| GATED | 2 of 2 passed the patch gate. |
| BOT_TESTED | Estimated bot-test cost INR 48.89; INR 138.92 left under the cap. |
| BOT_TESTED | No candidate cleared the bar: the best, g2-c3, gains +0.012 (95% CI [-0.041, +0.069]), but the bar is a CI lower bound above 0.020 and no hard-criterion failure. |
| NO_CANDIDATE | Bot tests picked no candidate, so nothing goes to the A/B. |

## Diagnosis

| Rank | Criterion | Failure rate | 95% CI | Weight | Weighted cost | Leads | Target |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | closure | 0.775 | [0.621, 0.929] | 2.0 | 1.550 | 10 | yes |
| 2 | frustration | 0.490 | [0.169, 0.811] | 3.0 | 1.470 | 10 | yes |
| 3 | requirement_captured | 0.325 | [0.145, 0.505] | 3.0 | 0.975 | 10 | yes |
| 4 | wrong_language | 0.100 | [0.000, 0.296] | 2.0 | 0.200 | 10 |  |
| 5 | asked_for_human | 0.100 | [0.000, 0.296] | 2.0 | 0.200 | 10 |  |
| 6 | claimed_human | 0.000 | [0.000, 0.000] | 4.0 | 0.000 | 10 |  |
| 7 | false_assurance | 0.000 | [0.000, 0.000] | 4.0 | 0.000 | 10 |  |
| 8 | looping | 0.000 | [0.000, 0.000] | 3.0 | 0.000 | 10 |  |

## Candidates

| Candidate | Mode | Chars | Targets | Pre-checks | Gate (p per question) | Bot-test score | 95% CI | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| g2-c1 | replace_text | 304 | frustration, closure | ok | alters_commitments=0.00, weakens_safety_rule=0.00, contradicts_instruction=0.20, raises_ask_limit=0.00, likely_longer_calls=0.00 | -0.007 | [-0.062, +0.052] | not_picked |
| g2-c2 | replace_text | 114 | frustration, requirement_captured | failed: applies, jinja_parses, renders | - | n/a | n/a | precheck_failed |
| g2-c3 | replace_text | 231 | closure, frustration | ok | alters_commitments=0.10, weakens_safety_rule=0.20, contradicts_instruction=0.00, raises_ask_limit=0.00, likely_longer_calls=0.00 | +0.012 | [-0.041, +0.069] | not_picked |

Pick: none. No candidate cleared the bar: the best, g2-c3, gains +0.012 (95% CI [-0.041, +0.069]), but the bar is a CI lower bound above 0.020 and no hard-criterion failure.

## Live A/B (simulated traffic)

Not run.

## Spend

This generation INR 45.55; loop total INR 203.96 of the INR 300.00 cap.
