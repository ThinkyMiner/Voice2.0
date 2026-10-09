# Split evidence: vani-meeting-v1-win

- Assigned leads (A 26,363 / B 2,894): configured 10.00%, achieved 9.89% [9.55%, 10.24%]; SRM χ² = 0.38, p = 0.5367: OK
- Triggered leads (A 22,146 / B 2,458): configured 10.00%, achieved 9.99% [9.62%, 10.37%]; SRM χ² = 0.00, p = 0.9593: OK
- Leads in both arms: 0
- Re-hash determinism: PASS (checked 29,257, mismatches 0)

SRM is flagged when p < 0.001 (`srm_p_threshold`).

## Per-day split (assigned leads, by first-exposure date)

| Date | A | B | Share B |
|---|---:|---:|---:|
| 2026-10-01 | 953 | 91 | 8.72% |
| 2026-10-02 | 943 | 102 | 9.76% |
| 2026-10-03 | 939 | 106 | 10.14% |
| 2026-10-04 | 940 | 105 | 10.05% |
| 2026-10-05 | 936 | 109 | 10.43% |
| 2026-10-06 | 944 | 101 | 9.67% |
| 2026-10-07 | 940 | 105 | 10.05% |
| 2026-10-08 | 945 | 100 | 9.57% |
| 2026-10-09 | 945 | 100 | 9.57% |
| 2026-10-10 | 935 | 110 | 10.53% |
| 2026-10-11 | 920 | 124 | 11.88% |
| 2026-10-12 | 958 | 87 | 8.33% |
| 2026-10-13 | 944 | 101 | 9.67% |
| 2026-10-14 | 943 | 102 | 9.76% |
| 2026-10-15 | 941 | 104 | 9.95% |
| 2026-10-16 | 936 | 109 | 10.43% |
| 2026-10-17 | 942 | 103 | 9.86% |
| 2026-10-18 | 917 | 128 | 12.25% |
| 2026-10-19 | 937 | 108 | 10.33% |
| 2026-10-20 | 944 | 101 | 9.67% |
| 2026-10-21 | 948 | 96 | 9.20% |
| 2026-10-22 | 954 | 91 | 8.71% |
| 2026-10-23 | 934 | 111 | 10.62% |
| 2026-10-24 | 944 | 101 | 9.67% |
| 2026-10-25 | 945 | 100 | 9.57% |
| 2026-10-26 | 950 | 95 | 9.09% |
| 2026-10-27 | 946 | 99 | 9.47% |
| 2026-10-28 | 940 | 105 | 10.05% |

## Method

Counts are leads, not calls: each lead is counted once, at its first exposure logged while the experiment was RUNNING (exposures after the first RUNNING state change are a legitimate allocation change and are excluded). Triggered leads are assigned leads with at least one answered call. Each count is tested with a 1-df chi-square goodness-of-fit test against the configured share from the frozen config, and the achieved share carries a Wilson 95% score interval. Leads in both arms counts leads whose RUNNING-period exposures carry more than one variant. The re-hash check recomputes `router.bucket(salt, lead_id)` for every RUNNING-period exposure and fails if any stored bucket, or stored variant under the configured allocation, disagrees.

![split](split_win.png)
