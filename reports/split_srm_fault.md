# Split evidence: vani-meeting-v1-srm_fault

- Assigned leads (A 26,416 / B 2,411): configured 10.00%, achieved 8.36% [8.05%, 8.69%]; SRM χ² = 85.76, p = 2.031e-20: **SRM DETECTED**
- Triggered leads (A 22,178 / B 2,050): configured 10.00%, achieved 8.46% [8.12%, 8.82%]; SRM χ² = 63.74, p = 1.422e-15: **SRM DETECTED**
- Leads in both arms: 0
- Re-hash determinism: PASS (checked 28,827, mismatches 0)

SRM is flagged when p < 0.001 (`srm_p_threshold`).

## Per-day split (assigned leads, by first-exposure date)

| Date | A | B | Share B |
|---|---:|---:|---:|
| 2026-10-01 | 922 | 107 | 10.40% |
| 2026-10-02 | 936 | 83 | 8.15% |
| 2026-10-03 | 953 | 80 | 7.74% |
| 2026-10-04 | 941 | 86 | 8.37% |
| 2026-10-05 | 945 | 86 | 8.34% |
| 2026-10-06 | 948 | 78 | 7.60% |
| 2026-10-07 | 942 | 82 | 8.01% |
| 2026-10-08 | 943 | 82 | 8.00% |
| 2026-10-09 | 944 | 84 | 8.17% |
| 2026-10-10 | 937 | 91 | 8.85% |
| 2026-10-11 | 937 | 95 | 9.21% |
| 2026-10-12 | 947 | 83 | 8.06% |
| 2026-10-13 | 946 | 82 | 7.98% |
| 2026-10-14 | 971 | 67 | 6.45% |
| 2026-10-15 | 928 | 101 | 9.82% |
| 2026-10-16 | 939 | 94 | 9.10% |
| 2026-10-17 | 941 | 91 | 8.82% |
| 2026-10-18 | 943 | 91 | 8.80% |
| 2026-10-19 | 962 | 71 | 6.87% |
| 2026-10-20 | 939 | 89 | 8.66% |
| 2026-10-21 | 944 | 87 | 8.44% |
| 2026-10-22 | 944 | 93 | 8.97% |
| 2026-10-23 | 944 | 86 | 8.35% |
| 2026-10-24 | 942 | 80 | 7.83% |
| 2026-10-25 | 934 | 96 | 9.32% |
| 2026-10-26 | 954 | 76 | 7.38% |
| 2026-10-27 | 936 | 91 | 8.86% |
| 2026-10-28 | 954 | 79 | 7.65% |

## Method

Counts are leads, not calls: each lead is counted once, at its first exposure logged while the experiment was RUNNING (exposures after the first RUNNING state change are a legitimate allocation change and are excluded). Triggered leads are assigned leads with at least one answered call. Each count is tested with a 1-df chi-square goodness-of-fit test against the configured share from the frozen config, and the achieved share carries a Wilson 95% score interval. Leads in both arms counts leads whose RUNNING-period exposures carry more than one variant. The re-hash check recomputes `router.bucket(salt, lead_id)` for every RUNNING-period exposure and fails if any stored bucket, or stored variant under the configured allocation, disagrees.

![split](split_srm_fault.png)
