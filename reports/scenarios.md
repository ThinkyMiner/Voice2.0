# Scenario results (20 seeds per scenario)

Statistics are from the pinned demo seed's run; the hit rate is over seeds 1..20. Δ successes/week is the engine's estimate if B served 100% of traffic.

| scenario | terminal state | stop look | lift estimate (95% CI) | guardrail Δ (RCI) | ≈ Δ successes/week at 100% | hit rate over 20 seeds |
|---|---|---|---|---|---:|---:|
| approval (seed 1101) | PENDING_APPROVAL | 4 (day 16) | +23.3% (+9.2%, +37.4%) | +1.7% (-6.0%, +9.5%) | +240 | 17/20 |
| buyer_disconnect_stop (seed 1107) | STOPPED_HARM | 5 (day 8) | +13.0% (+5.6%, +20.3%) | +7.5 pp (+2.1 pp, +12.9 pp) | +311 | 18/20 |
| guardrail_block (seed 1105) | BLOCKED_GUARDRAIL | 7 (day 24) | +31.7% (+20.4%, +42.9%) | +11.9% (+5.9%, +17.9%) | +337 | 20/20 |
| harm (seed 1103) | STOPPED_HARM | 2 (day 10) | -23.9% (-39.5%, -8.3%) | -0.4% (-14.0%, +13.3%) | -246 | 20/20 |
| inconclusive (seed 1104) | INCONCLUSIVE | 7 (day 24) | -5.9% (-15.8%, +4.0%) | -1.9% (-6.9%, +3.0%) | -64 | 19/20 |
| srm_fault (seed 1106) | HALTED_SRM | 1 (day 8) | +2.7% (-19.2%, +24.6%) | -1.9% (-23.5%, +19.8%) | untrusted (SRM) | 20/20 |
| win (seed 1101) | PROMOTED | 4 (day 16) | +23.3% (+9.2%, +37.4%) | +1.7% (-6.0%, +9.5%) | +240 | 17/20 |
