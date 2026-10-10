# Simulation ground truth: c1

The engine never sees this file or the sim config.

Experiment `vani-loop-c1`, seed 4101.

| effect | Injected (sim config) | Realised (this run) |
|---|---|---|
| primary relative lift | +20.00% | +13.83% |
| secondary change (absolute) | -0.0400 | -0.0420 |

The injected secondary change is the **total** true change in B. The extra meetings alone would
move it by -0.0430 (meeting calls run longer), so the direct effect is
sized to make the total land on the injected value.

| arm | leads | triggered leads | primary rate | secondary |
|---|---|---|---|---|
| A | 2,971 | 2,311 | 0.6625 | 0.1411 |
| B, counterfactual (before injection) | 325 | 262 | 0.7176 | 0.1183 |
| B, realised | 325 | 262 | 0.8168 | 0.0763 |

## Business impact of the true effect (if B served 100% of traffic)

- Primary: 718 → 817 successes per 1,000 answered leads (+99); about **+128 per week** at 1,286 answered leads a week.
- Secondary rate: 11.8% → 7.6% of answered leads (-4.2 pp); about -54 leads a week.

Exposures dropped (fault injection): 0 B leads have calls but no logged exposure.

Bootstrap pool: 824 lead-windows; pool p = 0.6667.

## Definitions

- **Pool lead (lead-window):** one historical lead's calls inside one non-overlapping
  window-length block of the reference period, with their timing relative to that lead's first
  call in the block. Sim leads are drawn from the pool uniformly with replacement, renamed
  sim-<seed>-<n>, and routed through the real router; calls at or after the window end are not
  generated.
- **Triggered lead:** at least one answered call (§2.6). Rates and secondary values are over
  triggered leads only.
- **Primary per lead:** 1 if any answered call has a primary disposition, else 0.
- **Secondary per lead:** the mean over answered calls (continuous) or 1 if any answered call
  has a secondary disposition (binary).
- **Counterfactual:** the same B leads' replayed historical outcome before injection. Arm A is
  never changed.
- **Realised primary lift** = rate_B / rate_B_counterfactual − 1. **Realised secondary change**
  = secondary_B / secondary_B_counterfactual − 1 (relative) or secondary_B −
  secondary_B_counterfactual (absolute).
- **Pool p:** the primary rate per triggered pool lead over full lead-windows. The injection
  probability uses it, while the realised lift uses in-window calls, so the two can differ
  slightly.
