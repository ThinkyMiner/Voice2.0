# Split evidence: vani-meeting-v1-inconclusive

- Assigned leads (A 22,527 / B 2,550): configured 10.00%, achieved 10.17% [9.80%, 10.55%]; SRM χ² = 0.79, p = 0.3733: OK
- Triggered leads (A 19,047 / B 2,151): configured 10.00%, achieved 10.15% [9.75%, 10.56%]; SRM χ² = 0.51, p = 0.475: OK
- Leads in both arms: 0
- Re-hash determinism: PASS (checked 25,077, mismatches 0)

SRM is flagged when p < 0.001 (`srm_p_threshold`).

## Per-day split (assigned leads, by first-exposure date)

| Date | A | B | Share B |
|---|---:|---:|---:|
| 2026-10-01 | 932 | 112 | 10.73% |
| 2026-10-02 | 932 | 113 | 10.81% |
| 2026-10-03 | 950 | 95 | 9.09% |
| 2026-10-04 | 934 | 111 | 10.62% |
| 2026-10-05 | 920 | 125 | 11.96% |
| 2026-10-06 | 931 | 114 | 10.91% |
| 2026-10-07 | 936 | 109 | 10.43% |
| 2026-10-08 | 947 | 98 | 9.38% |
| 2026-10-09 | 938 | 107 | 10.24% |
| 2026-10-10 | 926 | 119 | 11.39% |
| 2026-10-11 | 946 | 98 | 9.39% |
| 2026-10-12 | 942 | 103 | 9.86% |
| 2026-10-13 | 959 | 86 | 8.23% |
| 2026-10-14 | 929 | 116 | 11.10% |
| 2026-10-15 | 956 | 89 | 8.52% |
| 2026-10-16 | 944 | 101 | 9.67% |
| 2026-10-17 | 936 | 109 | 10.43% |
| 2026-10-18 | 956 | 89 | 8.52% |
| 2026-10-19 | 932 | 113 | 10.81% |
| 2026-10-20 | 950 | 95 | 9.09% |
| 2026-10-21 | 931 | 113 | 10.82% |
| 2026-10-22 | 931 | 114 | 10.91% |
| 2026-10-23 | 937 | 108 | 10.33% |
| 2026-10-24 | 932 | 113 | 10.81% |

## Method

Counts are leads, not calls: each lead is counted once, at its first exposure logged while the experiment was RUNNING (exposures after the first RUNNING state change are a legitimate allocation change and are excluded). Triggered leads are assigned leads with at least one answered call. Each count is tested with a 1-df chi-square goodness-of-fit test against the configured share from the frozen config, and the achieved share carries a Wilson 95% score interval. Leads in both arms counts leads whose RUNNING-period exposures carry more than one variant. The re-hash check recomputes `router.bucket(salt, lead_id)` for every RUNNING-period exposure and fails if any stored bucket, or stored variant under the configured allocation, disagrees.

![split](split_inconclusive.png)
