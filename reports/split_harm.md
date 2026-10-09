# Split evidence: vani-meeting-v1-harm

- Assigned leads (A 9,387 / B 1,062): configured 10.00%, achieved 10.16% [9.60%, 10.76%]; SRM χ² = 0.31, p = 0.5771: OK
- Triggered leads (A 7,958 / B 904): configured 10.00%, achieved 10.20% [9.59%, 10.85%]; SRM χ² = 0.40, p = 0.5285: OK
- Leads in both arms: 0
- Re-hash determinism: PASS (checked 10,449, mismatches 0)

SRM is flagged when p < 0.001 (`srm_p_threshold`).

## Per-day split (assigned leads, by first-exposure date)

| Date | A | B | Share B |
|---|---:|---:|---:|
| 2026-10-01 | 949 | 95 | 9.10% |
| 2026-10-02 | 938 | 107 | 10.24% |
| 2026-10-03 | 938 | 107 | 10.24% |
| 2026-10-04 | 943 | 102 | 9.76% |
| 2026-10-05 | 945 | 100 | 9.57% |
| 2026-10-06 | 945 | 100 | 9.57% |
| 2026-10-07 | 932 | 113 | 10.81% |
| 2026-10-08 | 929 | 116 | 11.10% |
| 2026-10-09 | 929 | 116 | 11.10% |
| 2026-10-10 | 939 | 106 | 10.14% |

## Method

Counts are leads, not calls: each lead is counted once, at its first exposure logged while the experiment was RUNNING (exposures after the first RUNNING state change are a legitimate allocation change and are excluded). Triggered leads are assigned leads with at least one answered call. Each count is tested with a 1-df chi-square goodness-of-fit test against the configured share from the frozen config, and the achieved share carries a Wilson 95% score interval. Leads in both arms counts leads whose RUNNING-period exposures carry more than one variant. The re-hash check recomputes `router.bucket(salt, lead_id)` for every RUNNING-period exposure and fails if any stored bucket, or stored variant under the configured allocation, disagrees.

![split](split_harm.png)
