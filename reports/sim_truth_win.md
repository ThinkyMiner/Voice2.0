# Simulation ground truth: win

The engine never sees this file or the sim config.

Experiment `vani-meeting-v1-win`, seed 1101.

| effect | Injected (sim config) | Realised (this run) |
|---|---|---|
| primary relative lift | +25.00% | +24.94% |
| secondary change (relative) | +3.00% | +1.45% |

The injected secondary change is the **total** true change in B. The extra meetings alone would
move it by +4.68% (meeting calls run longer), so the direct effect is
sized to make the total land on the injected value.

| arm | leads | triggered leads | primary rate | secondary |
|---|---|---|---|---|
| A | 26,363 | 22,146 | 0.1713 | 39.76 |
| B, counterfactual (before injection) | 2,894 | 2,458 | 0.1745 | 40.96 |
| B, realised | 2,894 | 2,458 | 0.2181 | 41.56 |

## Business impact of the true effect (if B served 100% of traffic)

- Primary: 175 → 218 successes per 1,000 answered leads (+44); about **+268 per week** at 6,151 answered leads a week.
- Talk time per answered lead: 41.0 s → 41.6 s (+0.6 s, +1.5%).

Exposures dropped (fault injection): 0 B leads have calls but no logged exposure.

Bootstrap pool: 175,543 lead-windows; pool p = 0.1795.

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
