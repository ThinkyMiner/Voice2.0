# Judge agreement with IndiaMART's human QA audit

- Calls compared: 120 (SquadStack inbound audits that the judge also graded)
- Judge model: anthropic/claude-sonnet-5.5
- Held out: yes (yes = no compared call was used to tune the rubrics; their guidance summarises the auditors' remark patterns)
- Overall, 3 classes: accuracy 40.0%, Cohen's kappa -0.03. Like for like: on both sides the overall is the worst grade over the same text-observable QA parameters (audio-only ones and the prompt rules the sheet lacks are left out)
- Fatal vs rest: accuracy 62.5%

**Calibrated.** The judge's minor (non-fatal) flags were remapped to what the auditors call them, learned on 40 other audited calls (the first in sheet order) and applied unchanged here; fatal, pass and na grades are never changed. Remapped: Additional Details Missed Opportunity -> na, Category ISQ's Error -> na, Dead air -> pass, Looping Behavior -> pass, MCAT/ Search errors -> na, OnCall AstBuy Error (Transferred) -> pass, Product Change/ No Product Accuracy -> pass, Pronunciation -> pass, Quantity Accuracy (Numeric) -> pass, Quantity Missed Opportunity (Capture) -> na, Quantity Probing Error -> na, Spec1 Accuracy -> pass, Spec2 Accuracy -> pass, Spec Missed Opportunity -> na, WER/STT -> pass.
Uncalibrated, on the same calls: overall 30.0% (kappa 0.01), fatal vs rest 62.5%.

Per QA parameter (na counts as a class; audio parameters are graded from text, so na is expected):

| Parameter | n | Accuracy | Kappa | Fatal recall | Uncalibrated |
|---|---|---|---|---|---|
| Outcome Accuracy (Intent) | 120 | 95.8% | -0.02 | 0.0% | 95.8% |
| Quantity Accuracy (Numeric) | 120 | 85.0% | 0.69 | 0.0% | 84.2% |
| Quantity Unit Accuracy | 120 | 84.2% | 0.68 | 0.0% | 84.2% |
| Quantity Probing Error | 120 | 83.3% | 0.07 | 100.0% | 47.5% |
| Quantity Missed Opportunity (Capture) | 120 | 85.8% | 0.06 | 33.3% | 81.7% |
| Spec1 Accuracy | 120 | 84.2% | 0.65 | 25.0% | 78.3% |
| Spec2 Accuracy | 120 | 79.2% | 0.60 | 0.0% | 77.5% |
| Spec Missed Opportunity | 120 | 74.2% | 0.10 | 0.0% | 64.2% |
| Additional Details Accuracy | 120 | 65.0% | 0.18 | n/a | 65.0% |
| Additional Details Missed Opportunity | 120 | 89.2% | 0.00 | 0.0% | 76.7% |
| Looping Behavior | 120 | 85.8% | 0.38 | n/a | 46.7% |
| WER/STT | 120 | 79.2% | 0.33 | 16.7% | 56.7% |
| Dead air (audio) | 120 | 30.0% | 0.03 | 100.0% | 11.7% |
| Pronunciation (audio) | 120 | 11.7% | -0.02 | 0.0% | 9.2% |
| Product Change/ No Product Accuracy | 120 | 90.0% | 0.68 | 0.0% | 88.3% |
| MCAT/ Search errors | 120 | 80.8% | 0.41 | 0.0% | 80.8% |
| Category ISQ's Error | 120 | 84.2% | 0.00 | n/a | 76.7% |
| OnCall AstBuy Error (Transferred) | 120 | 85.8% | 0.71 | n/a | 78.3% |

## Confusion (rows: human grade, columns: judge grade)

### Outcome Accuracy (Intent)

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 115 | 1 | 1 | 0 |
| non_fatal | 0 | 0 | 0 | 0 |
| fatal | 3 | 0 | 0 | 0 |
| na | 0 | 0 | 0 | 0 |

### Quantity Accuracy (Numeric)

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 69 | 0 | 4 | 6 |
| non_fatal | 1 | 0 | 0 | 0 |
| fatal | 4 | 0 | 0 | 0 |
| na | 3 | 0 | 0 | 33 |

### Quantity Unit Accuracy

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 68 | 6 | 0 | 5 |
| non_fatal | 2 | 0 | 0 | 0 |
| fatal | 2 | 0 | 0 | 1 |
| na | 3 | 0 | 0 | 33 |

### Quantity Probing Error

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 0 | 0 | 0 | 0 |
| non_fatal | 0 | 0 | 0 | 1 |
| fatal | 0 | 0 | 1 | 0 |
| na | 4 | 0 | 15 | 99 |

### Quantity Missed Opportunity (Capture)

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 0 | 0 | 0 | 1 |
| non_fatal | 0 | 0 | 0 | 0 |
| fatal | 0 | 0 | 1 | 2 |
| na | 0 | 0 | 14 | 102 |

### Spec1 Accuracy

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 73 | 0 | 2 | 2 |
| non_fatal | 1 | 0 | 0 | 0 |
| fatal | 2 | 0 | 1 | 1 |
| na | 11 | 0 | 0 | 27 |

### Spec2 Accuracy

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 57 | 0 | 3 | 2 |
| non_fatal | 2 | 0 | 0 | 1 |
| fatal | 1 | 0 | 0 | 1 |
| na | 15 | 0 | 0 | 38 |

### Spec Missed Opportunity

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 3 | 0 | 0 | 2 |
| non_fatal | 0 | 0 | 1 | 10 |
| fatal | 0 | 0 | 0 | 3 |
| na | 11 | 0 | 4 | 86 |

### Additional Details Accuracy

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 9 | 5 | 0 | 17 |
| non_fatal | 1 | 0 | 0 | 4 |
| fatal | 0 | 0 | 0 | 0 |
| na | 12 | 3 | 0 | 69 |

### Additional Details Missed Opportunity

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 0 | 0 | 0 | 3 |
| non_fatal | 0 | 0 | 0 | 9 |
| fatal | 0 | 0 | 0 | 1 |
| na | 0 | 0 | 0 | 107 |

### Looping Behavior

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 97 | 0 | 3 | 2 |
| non_fatal | 5 | 0 | 1 | 0 |
| fatal | 0 | 0 | 0 | 0 |
| na | 6 | 0 | 0 | 6 |

### WER/STT

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 88 | 0 | 4 | 1 |
| non_fatal | 7 | 0 | 3 | 0 |
| fatal | 5 | 0 | 1 | 0 |
| na | 5 | 0 | 0 | 6 |

### Dead air (audio)

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 26 | 0 | 0 | 70 |
| non_fatal | 4 | 0 | 1 | 7 |
| fatal | 0 | 0 | 1 | 0 |
| na | 2 | 0 | 0 | 9 |

### Pronunciation (audio)

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 5 | 0 | 0 | 92 |
| non_fatal | 2 | 0 | 0 | 10 |
| fatal | 0 | 0 | 0 | 1 |
| na | 1 | 0 | 0 | 9 |

### Product Change/ No Product Accuracy

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 15 | 0 | 0 | 6 |
| non_fatal | 2 | 0 | 0 | 0 |
| fatal | 2 | 0 | 0 | 0 |
| na | 2 | 0 | 0 | 93 |

### MCAT/ Search errors

| human \ judge | pass | non_fatal | fatal | na |
|---|---|---|---|---|
| pass | 10 | 0 | 0 | 19 |
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
