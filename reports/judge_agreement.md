# Judge agreement with IndiaMART's human QA audit

- Calls compared: 120 (SquadStack inbound audits that the judge also graded)
- Judge model: anthropic/claude-sonnet-5.5
- Held out: yes (yes = no compared call was used to tune the rubrics; their guidance summarises the auditors' remark patterns)
- Overall, 3 classes: accuracy 47.5%, Cohen's kappa 0.04. Like for like: on both sides the overall is the worst grade over the same text-observable QA parameters (audio-only ones and the prompt rules the sheet lacks are left out)
- Fatal vs rest: accuracy 70.0%

**Calibrated.** The judge's minor (non-fatal) flags were remapped to what the auditors call them, learned on 40 other audited calls (the first in sheet order) and applied unchanged here; fatal, pass and na grades are never changed. Remapped: Additional Details Missed Opportunity -> na, Category ISQ's Error -> na, Looping Behavior -> pass, OnCall AstBuy Error (Transferred) -> pass, Product Change/ No Product Accuracy -> pass, Quantity Missed Opportunity (Capture) -> na, Quantity Probing Error -> na, Quantity Unit Accuracy -> pass, Spec1 Accuracy -> na, Spec2 Accuracy -> pass, Spec Missed Opportunity -> na, WER/STT -> pass.
Uncalibrated, on the same calls: overall 30.0% (kappa -0.02), fatal vs rest 70.0%.

Per QA parameter (na counts as a class; audio parameters are graded from text, so na is expected):

| Parameter | n | Accuracy | Kappa | Fatal recall | Uncalibrated |
|---|---|---|---|---|---|
| Outcome Accuracy (Intent) | 120 | 95.8% | -0.01 | 0.0% | 95.8% |
| Quantity Accuracy (Numeric) | 120 | 85.0% | 0.69 | 0.0% | 85.0% |
| Quantity Unit Accuracy | 120 | 90.0% | 0.78 | 0.0% | 89.2% |
| Quantity Probing Error | 120 | 95.8% | -0.01 | 0.0% | 59.2% |
| Quantity Missed Opportunity (Capture) | 120 | 95.0% | -0.02 | 0.0% | 90.0% |
| Spec1 Accuracy | 120 | 81.7% | 0.63 | 25.0% | 79.2% |
| Spec2 Accuracy | 120 | 82.5% | 0.66 | 0.0% | 77.5% |
| Spec Missed Opportunity | 120 | 73.3% | 0.15 | 0.0% | 65.0% |
| Additional Details Accuracy | 120 | 65.0% | 0.18 | n/a | 65.0% |
| Additional Details Missed Opportunity | 120 | 89.2% | 0.06 | 0.0% | 76.7% |
| Looping Behavior | 120 | 86.7% | 0.45 | n/a | 47.5% |
| WER/STT | 120 | 76.7% | 0.25 | 16.7% | 53.3% |
| Dead air (audio) | 120 | 9.2% | 0.00 | 0.0% | 9.2% |
| Pronunciation (audio) | 120 | 8.3% | 0.00 | 0.0% | 8.3% |
| Product Change/ No Product Accuracy | 120 | 91.7% | 0.75 | 0.0% | 88.3% |
| MCAT/ Search errors | 120 | 83.3% | 0.52 | 0.0% | 83.3% |
| Category ISQ's Error | 120 | 84.2% | 0.00 | n/a | 77.5% |
| OnCall AstBuy Error (Transferred) | 120 | 85.8% | 0.71 | n/a | 76.7% |

## Confusion (rows: human grade, columns: judge grade)

### Outcome Accuracy (Intent)

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 115 | 2 | 0 | 0 |
| non_fatal | 0 | 0 | 0 | 0 |
| fatal | 3 | 0 | 0 | 0 |
| na | 0 | 0 | 0 | 0 |

### Quantity Accuracy (Numeric)

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 69 | 2 | 2 | 6 |
| non_fatal | 1 | 0 | 0 | 0 |
| fatal | 4 | 0 | 0 | 0 |
| na | 2 | 0 | 1 | 33 |

### Quantity Unit Accuracy

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 75 | 0 | 0 | 4 |
| non_fatal | 2 | 0 | 0 | 0 |
| fatal | 2 | 0 | 0 | 1 |
| na | 3 | 0 | 0 | 33 |

### Quantity Probing Error

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 0 | 0 | 0 | 0 |
| non_fatal | 0 | 0 | 0 | 1 |
| fatal | 0 | 0 | 0 | 1 |
| na | 3 | 0 | 0 | 115 |

### Quantity Missed Opportunity (Capture)

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 0 | 0 | 0 | 1 |
| non_fatal | 0 | 0 | 0 | 0 |
| fatal | 0 | 0 | 0 | 3 |
| na | 0 | 0 | 2 | 114 |

### Spec1 Accuracy

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 66 | 0 | 3 | 8 |
| non_fatal | 1 | 0 | 0 | 0 |
| fatal | 1 | 0 | 1 | 2 |
| na | 7 | 0 | 0 | 31 |

### Spec2 Accuracy

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 60 | 0 | 1 | 1 |
| non_fatal | 2 | 0 | 0 | 1 |
| fatal | 2 | 0 | 0 | 0 |
| na | 14 | 0 | 0 | 39 |

### Spec Missed Opportunity

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 3 | 0 | 1 | 1 |
| non_fatal | 2 | 0 | 1 | 8 |
| fatal | 0 | 0 | 0 | 3 |
| na | 12 | 0 | 4 | 85 |

### Additional Details Accuracy

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 8 | 6 | 0 | 17 |
| non_fatal | 1 | 0 | 0 | 4 |
| fatal | 0 | 0 | 0 | 0 |
| na | 10 | 4 | 0 | 70 |

### Additional Details Missed Opportunity

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 0 | 0 | 0 | 3 |
| non_fatal | 0 | 0 | 1 | 8 |
| fatal | 0 | 0 | 0 | 1 |
| na | 0 | 0 | 0 | 107 |

### Looping Behavior

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 96 | 0 | 4 | 2 |
| non_fatal | 6 | 0 | 0 | 0 |
| fatal | 0 | 0 | 0 | 0 |
| na | 4 | 0 | 0 | 8 |

### WER/STT

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 86 | 0 | 5 | 2 |
| non_fatal | 8 | 0 | 2 | 0 |
| fatal | 5 | 0 | 1 | 0 |
| na | 6 | 0 | 0 | 5 |

### Dead air (audio)

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 0 | 0 | 0 | 96 |
| non_fatal | 0 | 0 | 0 | 12 |
| fatal | 0 | 0 | 0 | 1 |
| na | 0 | 0 | 0 | 11 |

### Pronunciation (audio)

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 0 | 0 | 0 | 97 |
| non_fatal | 0 | 0 | 0 | 12 |
| fatal | 0 | 0 | 0 | 1 |
| na | 0 | 0 | 0 | 10 |

### Product Change/ No Product Accuracy

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 17 | 0 | 0 | 4 |
| non_fatal | 1 | 0 | 1 | 0 |
| fatal | 2 | 0 | 0 | 0 |
| na | 2 | 0 | 0 | 93 |

### MCAT/ Search errors

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 13 | 2 | 0 | 14 |
| non_fatal | 1 | 0 | 0 | 0 |
| fatal | 2 | 0 | 0 | 0 |
| na | 1 | 0 | 0 | 87 |

### Category ISQ's Error

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 0 | 0 | 0 | 18 |
| non_fatal | 0 | 0 | 0 | 1 |
| fatal | 0 | 0 | 0 | 0 |
| na | 0 | 0 | 0 | 101 |

### OnCall AstBuy Error (Transferred)

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 70 | 0 | 0 | 3 |
| non_fatal | 0 | 0 | 0 | 6 |
| fatal | 0 | 0 | 0 | 0 |
| na | 8 | 0 | 0 | 33 |
