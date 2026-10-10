# Rubric report: squadstack_answered_2026-10-06_sample250

Rubric `vani-call-quality-v1` (hash `e00f3c7df2d1`). Aggregates only: no call or lead ids, names, numbers or transcript text.

## Corpus

|  | count |
|---|---|
| Calls in corpus | 250 |
| Calls answered | 250 |
| Calls scored | 25 |
| Leads scored | 25 |

Early hang-up share: answered calls shorter than 10 s, which are not scored (there is no conversation to judge).

| bot version | answered calls | early hang-ups | share |
|---|---|---|---|
| all | 250 | 68 | 27.2% |
| base_vani | 9 | 3 | 33.3% |
| personalised_vani | 239 | 65 | 27.2% |
| summary_vani | 2 | 0 | 0.0% |

## Score distribution

Per lead: the mean over the lead's scored calls. rubric_score is in [0, 1], higher is better.

| statistic | rubric_score |
|---|---|
| mean | 0.615 |
| min | 0.313 |
| p10 | 0.376 |
| p25 | 0.522 |
| p50 | 0.641 |
| p75 | 0.713 |
| p90 | 0.783 |
| max | 0.804 |

## Criterion failure rates

Failure rate = 1 - mean goodness: per lead over its scored calls, then the mean over leads, with a normal-approximation 95% CI. Probabilities and scores, not pass/fail labels. Groups with fewer than 5 leads carry no CI.

### All leads

| criterion | weight | hard | failure rate (95% CI) |
|---|---|---|---|
| `claimed_human` | 4 | yes | 70.0% (52.4% to 87.6%) |
| `false_assurance` | 4 | yes | 31.2% (14.1% to 48.3%) |
| `wrong_language` | 2 |  | 0.0% (0.0% to 0.0%) |
| `looping` | 3 |  | 24.2% (7.3% to 41.1%) |
| `frustration` | 3 |  | 10.0% (0.6% to 19.4%) |
| `asked_for_human` | 2 |  | 7.2% (0.0% to 17.0%) |
| `requirement_captured` | 3 |  | 74.0% (62.9% to 85.1%) |
| `closure` | 2 |  | 71.0% (55.8% to 86.2%) |

### By bot version

| criterion | base_vani (leads=2) | personalised_vani (leads=23) |
|---|---|---|
| `claimed_human` | 95.0% (n=2, no CI) | 67.8% (49.0% to 86.7%) |
| `false_assurance` | 0.0% (n=2, no CI) | 33.9% (15.8% to 52.1%) |
| `wrong_language` | 0.0% (n=2, no CI) | 0.0% (0.0% to 0.0%) |
| `looping` | 0.0% (n=2, no CI) | 26.3% (8.2% to 44.4%) |
| `frustration` | 10.0% (n=2, no CI) | 10.0% (0.0% to 20.2%) |
| `asked_for_human` | 0.0% (n=2, no CI) | 7.8% (0.0% to 18.5%) |
| `requirement_captured` | 62.5% (n=2, no CI) | 75.0% (63.1% to 86.9%) |
| `closure` | 87.5% (n=2, no CI) | 69.6% (53.3% to 85.9%) |

## Mean rubric_risk

rubric_risk is the probability that at least one of (`claimed_human`, `false_assurance`, `looping`, `frustration`, `asked_for_human`) fired, assuming independence; per lead it is the mean over the lead's scored calls.

| bot version | leads | mean rubric_risk (95% CI) |
|---|---|---|
| all | 25 | 0.787 (0.629 to 0.946) |
| base_vani | 2 | 0.950 (n=2, no CI) |
| personalised_vani | 23 | 0.773 (0.603 to 0.944) |
