# Simulation ground truth: srm_fault

The engine never sees this file or the sim config.

Experiment `vani-meeting-v1-srm_fault`, seed 1106.

| effect | Injected (sim config) | Realised (this run) |
|---|---|---|
| primary relative lift | +0.00% | +0.00% |
| secondary change (relative) | +0.00% | +0.00% |

| arm | leads | triggered leads | primary rate | secondary |
|---|---|---|---|---|
| A | 26,416 | 22,178 | 0.1752 | 40.29 |
| B, counterfactual (before injection) | 2,841 | 2,412 | 0.1737 | 39.52 |
| B, realised | 2,841 | 2,412 | 0.1737 | 39.52 |

Exposures dropped (fault injection): 430 B leads have calls but no logged exposure.

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
