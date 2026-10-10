# Generation 1 of loop `vani-loop-r2`

Final state GEN_END, outcome **NO_CANDIDATE**. Mode `sim`: the A/B runs on simulated traffic. Provider `sarvam`. Production prompt `43c6734389ea`, rubric `d67f34f6104e`.

## Steps

| Step | What happened |
| --- | --- |
| OBSERVED | Reused 3 stored scores of the production prompt. |
| AUDIT_PENDING | Skipped: the audit gate applies to going live on real traffic (PLAN_LOOP §L2.2, §L9); this build only simulates the live A/B, so an untrusted rubric may rank candidates here. |
| DIAGNOSED | Targets: frustration, closure, asked_for_human. |
| PROPOSED | 3 proposed, 2 passed the pre-checks. |
| GATED | 2 of 2 passed the patch gate. |
| BOT_TESTED | Estimated bot-test cost INR 35.85; INR 69.80 left under the cap. |
| BOT_TESTED | No candidate cleared the bar: the best, g1-c1, gains +0.054 (95% CI [+0.011, +0.128]), but the bar is a CI lower bound above 0.020 and no hard-criterion failure. |
| NO_CANDIDATE | Bot tests picked no candidate, so nothing goes to the A/B. |

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

| Candidate | Mode | Chars | Targets | Pre-checks | Gate (p per question) | Bot-test score | 95% CI | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| g1-c1 | replace_section | 343 | frustration | ok | alters_commitments=0.10, weakens_safety_rule=0.00, contradicts_instruction=0.00, raises_ask_limit=0.00, likely_longer_calls=0.30 | +0.054 | [+0.011, +0.128] | not_picked |
| g1-c2 | replace_section | 327 | closure | failed: applies, jinja_parses, renders | - | n/a | n/a | precheck_failed |
| g1-c3 | replace_text | 95 | asked_for_human | ok | alters_commitments=0.00, weakens_safety_rule=0.10, contradicts_instruction=0.05, raises_ask_limit=0.00, likely_longer_calls=0.00 | -0.092 | [-0.163, -0.022] | not_picked |

Pick: none. No candidate cleared the bar: the best, g1-c1, gains +0.054 (95% CI [+0.011, +0.128]), but the bar is a CI lower bound above 0.020 and no hard-criterion failure.

## Live A/B (simulated traffic)

Not run.

## Spend

This generation INR 15.14; loop total INR 93.78 of the INR 150.00 cap.
