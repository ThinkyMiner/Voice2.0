# Simulation ground truth: buyer_disconnect_stop

The engine never sees this file or the sim config.

Experiment `vani-buyer-qty-asks-v1-buyer_disconnect_stop`, seed 1107.

| effect | Injected (sim config) | Realised (this run) |
|---|---|---|
| primary relative lift | +10.00% | +8.37% |
| secondary change (absolute) | +0.0900 | +0.0740 |

The injected secondary change is the **total** true change in B. The extra meetings alone would
move it by -0.0215 (meeting calls run longer), so the direct effect is
sized to make the total land on the injected value.

| arm | leads | triggered leads | primary rate | secondary |
|---|---|---|---|---|
| A | 4,765 | 3,720 | 0.6634 | 0.1358 |
| B, counterfactual (before injection) | 508 | 392 | 0.6709 | 0.1429 |
| B, realised | 508 | 392 | 0.7270 | 0.2168 |

## Business impact of the true effect (if B served 100% of traffic)

- Primary: 671 → 727 successes per 1,000 answered leads (+56); about **+115 per week** at 2,056 answered leads a week.
- Secondary rate: 14.3% → 21.7% of answered leads (+7.4 pp); about +152 leads a week.

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
