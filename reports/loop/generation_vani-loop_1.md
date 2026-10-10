# Generation 1 of loop `vani-loop`

Final state GEN_END, outcome **NO_CANDIDATE**. Mode `sim`: the A/B runs on simulated traffic. Provider `sarvam`. Production prompt `43c6734389ea`, rubric `e00f3c7df2d1`.

## Steps

| Step | What happened |
| --- | --- |
| OBSERVED | Reused 1 stored scores of the production prompt. |
| AUDIT_PENDING | Skipped: the audit gate applies to going live on real traffic (PLAN_LOOP §L2.2, §L9); this build only simulates the live A/B, so an untrusted rubric may rank candidates here. |
| DIAGNOSED | Targets: claimed_human, closure, false_assurance. |
| PROPOSED | 2 proposed, 2 passed the pre-checks. |
| GATED | 1 of 2 passed the patch gate. |
| BOT_TESTED | Estimated bot-test cost INR 23.90; INR 107.54 left under the cap. |
| BOT_TESTED | No candidate cleared the bar: the best, g1-c1, gains -0.003 (95% CI [-0.043, +0.074]), but the bar is a CI lower bound above 0.020 and no hard-criterion failure. |
| NO_CANDIDATE | Bot tests picked no candidate, so nothing goes to the A/B. |

## Diagnosis

| Rank | Criterion | Failure rate | 95% CI | Weight | Weighted cost | Leads | Target |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | claimed_human | 0.900 | [0.900, 0.900] | 4.0 | 3.600 | 1 | yes |
| 2 | closure | 1.000 | [1.000, 1.000] | 2.0 | 2.000 | 1 | yes |
| 3 | false_assurance | 0.000 | [0.000, 0.000] | 4.0 | 0.000 | 1 | yes |
| 4 | wrong_language | 0.000 | [0.000, 0.000] | 2.0 | 0.000 | 1 |  |
| 5 | looping | 0.000 | [0.000, 0.000] | 3.0 | 0.000 | 1 |  |
| 6 | frustration | 0.000 | [0.000, 0.000] | 3.0 | 0.000 | 1 |  |
| 7 | asked_for_human | 0.000 | [0.000, 0.000] | 2.0 | 0.000 | 1 |  |
| 8 | requirement_captured | 0.000 | [0.000, 0.000] | 3.0 | 0.000 | 1 |  |

## Candidates

| Candidate | Mode | Chars | Targets | Pre-checks | Gate (p per question) | Bot-test score | 95% CI | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| g1-c1 | replace_section | 264 | claimed_human | ok | alters_commitments=0.00, weakens_safety_rule=0.00, contradicts_instruction=0.00, raises_ask_limit=0.00, likely_longer_calls=0.00 | -0.003 | [-0.043, +0.074] | not_picked |
| g1-c2 | append | 294 | closure | ok | alters_commitments=0.98, weakens_safety_rule=0.05, contradicts_instruction=0.95, raises_ask_limit=0.00, likely_longer_calls=0.15 | n/a | n/a | gate_blocked |

Pick: none. No candidate cleared the bar: the best, g1-c1, gains -0.003 (95% CI [-0.043, +0.074]), but the bar is a CI lower bound above 0.020 and no hard-criterion failure.

## Live A/B (simulated traffic)

Not run.

## Spend

This generation INR 18.34; loop total INR 55.49 of the INR 150.00 cap.
