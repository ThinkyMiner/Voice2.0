# Scenario calibration (Phase 4)

These are predicted outcomes for the seven scenarios, made before the engine exists. They show whether each injected effect can reach its expected §2.8 state. The authoritative numbers come from the orchestrator's 20-seed `make scenarios` sweep once Phase 3 lands.

## Method

- **Traffic.** Real simulator and router: `simulator.build_pool` and `simulate_day` for every day of the window, with a RUNNING state at the configured 90/10 split, seeds 1–20. No engine code is used.
- **Leads at a look.** Per-lead realised outcomes come from `SimDay.truth`. These cover the full window, so outcome censoring at the look is ignored. A lead's trigger time is its first answered call in `SimDay.calls`, and the lead counts at a look once trigger time + `maturation_days` ≤ look time. Only leads with a logged exposure count.
- **Look schedule.** As in `runner.py`: a look after each sim day once t = matured triggered B / n_B,max reaches the next grid point. The first look is at t ≥ max(1/7, 0.20), each later one at the next 1/7 point above t, and the final look at t ≥ 1 or on the last day. n_B,max comes from `power.n_max` with R = 1.0, and everything was rerun at R = 1.1 as a stand-in for Phase 3's inflation factor.
- **Statistics.** Primary: pooled two-proportion z. Guardrail: the relative change in per-lead mean duration with a delta-method SE (bttc), or the absolute difference in per-lead disconnect rate with an unpooled SE (buyer). Repeated CI (RCI) = Δ̂ ± c_k·SE. SRM: `srm.srm_test` on assigned and on triggered leads, flagged at p < 0.001.
- **Rules.** §2.8 rules 1–5 are applied in order at each look, and the first look that decides gives the predicted outcome. A guardrail "within the margin" means the RCI upper bound < margin, and "beyond the margin" means the RCI lower bound > margin, both in the `worse_if: increase` direction.
- **Boundaries are approximate until Phase 3.**
  - Win and RCI: c_k = max(Φ⁻¹(1 − α_w(t_k)), 2.03/√t_k). This matches the PLAN's 5-look reference (4.877, 3.357, 2.680, 2.290, 2.031) to within 0.02 and equals 2.03 at t ≥ 1.
  - Harm: h_k = 2.45 at every look. The exact Pocock-type first-look value at t = 0.20 is 2.18, so the real harm stops should come at or before these predictions.

## Results (20 seeds each, R = 1.0)

| scenario | injected effect (realised mean in B) | predicted outcome rates | predicted hit rate | median day reached |
|---|---|---|---|---|
| win | meetings +25%, duration +3% (+25.0%, +2.8%) | PROMOTED 18, BLOCKED_GUARDRAIL 2 | 18/20 (90%) | 20 |
| harm | meetings −30% (−30.5%) | STOPPED_HARM 20 (look 1: 16, look 2: 1, look 3: 3) | 20/20 (100%), all by look 3 | 7 |
| inconclusive | meetings +3% (+3.1%) | INCONCLUSIVE 19, PENDING_APPROVAL 1 | 19/20 (95%) | 23 |
| guardrail_block | meetings +30%, duration +10% = margin (+30.2%, +9.8%) | BLOCKED_GUARDRAIL 20 | 20/20 (100%) | 23 |
| srm_fault | 15% of B exposures dropped, no effect | HALTED_SRM 20 (look 1: 16, looks 2–4: 4) | 20/20 (100%) | 8 |
| approval | the win effect, `promotion: approval` | PENDING_APPROVAL 18, BLOCKED_GUARDRAIL 2 | 18/20 (90%) | 20 |
| buyer_disconnect_stop | BL success +10%, abrupt disconnects +5 pp (+9.4%, +5.1 pp) | BLOCKED_GUARDRAIL 13, STOPPED_HARM 5, INCONCLUSIVE 2 | **5/20 (25%)** | 9.5 |

- "Day reached" is the sim day on which the run stops: the deciding look, or the final look. The window is 28 days for bttc and 14 for the buyer experiment.
- With no early stop, the final look comes on day 23 for bttc and day 10 for the buyer experiment.
- At R = 1.1 every hit rate is unchanged except buyer_disconnect_stop, which goes to 6/20. The bttc final looks move to about day 25.5, still inside the 28-day window.
- Checking only the final look (the t ≥ 1 look alone) gives the same rates, except win and approval at 17/20. Seven of the 18 win promotions come at looks 3–6.

## Business view (full traffic = B serving 100%, historical volume)

Historical volume: bttc has 885.5 answered leads a day (≈ 6,200 a week); the buyer file has 128.4 a day (≈ 900 a week).

| scenario | what B would do at full traffic | what the engine does |
|---|---|---|
| win | ≈ +278 meetings a week | promotes in 18 of 20 seeds |
| harm | ≈ −334 meetings a week | stops by about day 7, after ≈ 600 B leads; the test itself costs ≈ 33 meetings |
| inconclusive | ≈ +33 meetings a week | correctly makes no call: the 15% MDE is ≈ +167 a week |
| guardrail_block | ≈ +334 meetings a week, but ≈ +4 s of bot talk time per answered lead (≈ +6.9 hours a week) | never ships B |
| srm_fault | – | halts by about day 8, so no decision is made on corrupted assignment data |

## buyer_disconnect_stop: recommendation

At +5 pp, the abrupt-disconnect guardrail stops B early in only 5 of 20 seeds.

**Why.** n_B,max = 419 B leads, sized for the primary metric. At that size the SE of the disconnect difference is ≈ 1.9 pp, so the final-look RCI is ≈ ±3.9 pp. For the RCI to lie entirely above the 2 pp margin, the observed rise must exceed ≈ 5.9 pp; with a true +5 pp that happens about 25–32% of the time. More traffic would not help: it only reaches n_B,max sooner, at the same final-look width.

B is still never promoted at +5 pp: 13 seeds end BLOCKED_GUARDRAIL and 2 INCONCLUSIVE. What +5 pp changes is when traffic returns to A, not whether. STOPPED_HARM sends traffic back to A at the deciding look (about day 6 at +9 pp); BLOCKED_GUARDRAIL keeps the 10% split running until the final look (about day 10).

| secondary_change (total, absolute) | STOPPED_HARM (20 seeds) | normal approx. at the final look | median stop day |
|---|---|---|---|
| 0.05 (current) | 5/20 | 0.32 | 9.5 |
| 0.06 | 9/20 | 0.51 | 9.5 |
| 0.07 | 11/20 | 0.69 | 9 |
| 0.08 | 14/20 | 0.83 | 8 |
| **0.09** | **19/20** | **0.92** | **6** |
| 0.10 | 20/20 | 0.97 | 6 |

The normal approximation is Φ((change − 0.02)/SE − 2.03), using the median final-look SE.

**Recommendation: `secondary_change: 0.09`**, the smallest step with ≥ 90% predicted STOPPED_HARM. Applied: see the last line.

**The simulator accepts it.** At +10% BL success the simulator can inject up to +17.2 pp:
- 19.3% of triggered B leads are eligible for the injection;
- the primary injection alone moves disconnects by −2.1 pp;
- so +9 pp needs a direct rise of 11.1 pp, inside that 19.3%.

The sweep ran 0.05–0.12 without a ValueError.

The alternative is to keep +5 pp and change the expectation to BLOCKED_GUARDRAIL (13/20 today). That expectation is not robust either.

**Business line.** At the buyer file's real volume (≈ 900 answered buyers a week), B's +5 pp of abrupt disconnects means ≈ 45 more irritated buyers a week at full traffic; +9 pp would mean ≈ 81. That is set against ≈ +60 BL successes a week from the +10% lift. The simulation runs at 4× volume, so it sees four times these counts.

## Scenarios predicted below 80%

- **buyer_disconnect_stop: 25%.** The guardrail cannot reach its margin at a primary-sized sample (see above).
- **win and approval (90%) clear the bar, but with a thin margin.** The duration RCI at n_B,max is ≈ ±5.1% (2.03 × SE 2.5%). The observed duration change must therefore stay below ≈ +4.9% to clear the 10% margin, and B's true +2.8% misses that in about 1 seed in 5 at the final look. Early promotions bring the rate back to 90%, but the demo seed must be one that promotes.
- Every other scenario is at 95–100%.

## Placeholder seeds

`tests/test_scenarios.py` asserts a hit at each scenario's pinned seed. The current placeholders are predicted to give:

| scenario | seed | predicted outcome |
|---|---|---|
| win | 1101 | PROMOTED at look 4 |
| harm | 1103 | STOPPED_HARM at look 2 |
| inconclusive | 1104 | INCONCLUSIVE |
| guardrail_block | 1105 | BLOCKED_GUARDRAIL |
| srm_fault | 1106 | HALTED_SRM at look 1 |
| approval | 1102 | **BLOCKED_GUARDRAIL (miss)** |
| buyer_disconnect_stop | 1107 | **BLOCKED_GUARDRAIL (miss)**; STOPPED_HARM at look 5 if set to +9 pp |

Two ways to fix approval:
- Seed 1101 gives approval the same traffic as win (same salt and lead ids), so it is predicted PENDING_APPROVAL at look 4.
- Or pick any seed from the real sweep.

**Applied (Phase 4):** `configs/sims/buyer_disconnect_stop.yaml` uses `secondary_change: 0.09`.
