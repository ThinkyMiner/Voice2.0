# PLAN.md: Voice Bot A/B Testing & Auto-Rollout Platform

Read this entire file before doing anything. It is the source of truth for the project.

At the start of every phase, re-read that phase's section and the spec sections it references. Your memory of this file may be lost when context is compacted; the file itself is not.

---

## 0. Operating rules

### Your role
You are the orchestrator. You own:
- the shared contracts,
- integration of subagent work,
- the checkpoints.

Subagents build the modules, in parallel.

### Interview first
Phase 0 is questions only. Do not write code or create files until I confirm the Phase 0 answers.

### Phase loop (repeat for every phase)
1. Re-read the phase section and the spec sections it references.
2. Write or extend the shared contracts the phase needs, yourself, before launching any subagent. Contracts live in `src/abtest/contracts.py` and the store schema in `src/abtest/store.py`.
3. Launch the phase's subagents in parallel. Put every Task call in a single message so they run concurrently.
4. Integrate. Run `make test` and the phase's checkpoint commands yourself, and fix any integration breaks.
5. Commit and tag the commit `phase-N`.
6. Print the checkpoint block (format below), then STOP. Wait for me to say "go" before starting the next phase.

### Subagent brief template
Subagents cannot see this conversation, so every brief must be self-contained and include:
- **Goal:** one sentence, plus which PLAN.md sections to read.
- **Owned files:** files it may write. It must not edit any other file.
- **Readable files:** files it may read.
- **Contract:** the exact signatures from `contracts.py` it must implement.
- **Tests:** the tests it must write and pass (`uv run pytest tests/test_<module>.py`).
- **Done criteria:** what "finished" means for this module.
- **Contract problems:** "If the contract is wrong or insufficient, stop and report the proposed change. Never edit contracts.py."
- **Return format:** files changed, test results, assumptions made, open issues.

Why this works: agents writing disjoint files against frozen interfaces cannot collide. All cross-module changes happen in one place, your integration step.

### Checkpoint block
Print exactly this shape at the end of each phase:
```
## Checkpoint N: <name>
Built:          <one line per module>
Run:            <exact commands, copy-pasteable>
You should see: <concrete expected output, numbers, screens>
Decide:         <decisions I must make before the next phase, or "nothing">
Known gaps:     <honest list>
Tests:          <passed>/<total>
```

### Ask me before you
- change the statistical method or any decision rule,
- add a dependency not listed in §2.1,
- change a contract after its phase has been tagged,
- touch the provided data files. They are read-only, always.

### Determinism
- Every random process takes its seed from config.
- The same config and seed must produce identical results.
- No magic numbers in code. Every threshold either comes from config or is computed (for example, the boundaries).

### No LLM calls
Prompts are versioned and hashed but never executed. All outcomes come from the simulator (§2.5). Do not call any model API.

---

## 1. Context

### The problem
Changes to the AI Voice Bot prompt currently go live to all traffic on judgement alone. We are building an A/B workflow that:
1. defines variants A and B,
2. sends a configured share of traffic (for example 10%) to B for a fixed window,
3. tracks a primary goal (Meeting Fixed rate) and a secondary goal (Call Duration),
4. declares a statistically valid winner and auto-promotes it,
5. auto-stops a clearly worse variant early.

There is no live traffic. We replay historical call data with injected effects.

### Judging weights (build priority follows these)

| Criterion | Weight |
|---|---|
| Statistical validity of the winner call (method, sample size, repeated looks) | 30% |
| Auto-promotion and early-stop behaviour (correct, timely, logged) | 30% |
| Traffic split accuracy and stickiness | 20% |
| Primary and secondary metric support | 20% |

### Required deliverables
- A working workflow, end to end.
- A three-scenario demo: B wins and is promoted; B is worse and stopped early; the result is inconclusive.
- A one-slide presentation.
- Evidence of configured versus achieved traffic split.

### Core principle
The simulator injects a known true effect, and the engine never sees it. This gives us ground truth, so we can prove error rates with A/A validation instead of merely asserting them.

---

## 2. Technical spec

### 2.0 Data sources

All files come from the organisers' shared Drive folder. Download them into `data/` before Phase 0; the large ones exceed what any connector can fetch.

| File | Use |
|---|---|
| `Best-Time-to-Call - Call Attempts Apr-Sep 2026.csv` (75 MB) | Outcome source for the headline experiment (Dataset A) |
| `Best-Time-to-Call - Sellers.csv` (40 MB) | Optional seller attributes for segment breakdowns |
| `Best-Time-to-Call - Data Dictionary.md` | Column reference for Dataset A |
| `Call_Logs_Hackathon.csv` (1,000 rows) | Outcome source for the buyer-side experiment (Dataset B) |
| `Sarvam Prompt - Buyer Side VANI.docx` | Base prompt |
| `Current Definitions of Call Quality Matrix being used in VANI.png` | Reference only; the data has no per-call QA labels |

**Dataset A: Best-Time-to-Call attempts (headline experiment).** Seller-side VANI bot, 1 Apr to 30 Sep 2026, 530,748 dialed attempts across 149,363 sellers. It is the only dataset that carries both goals named in the problem statement: Meeting Fixed and Call Duration.

| Canonical | Source column | Notes |
|---|---|---|
| lead_id | `fk_glusr_usr_id` | Seller GLID; the randomisation unit |
| call_id | `data_hotlead_disposition_dtlid` | |
| ts | `call_start_time` | Actual dial time, 98.7% filled; drop rows where it is null |
| answered | `lead_call_status == "Answered"` | |
| disposition | `disposition_label` | Meeting Fixed, Not Interested, General, Call Later / Busy, Not Answered |
| duration_s | `lead_call_duration` | Phase 1 confirms the unit |
| attempt_no | `call_attempt_count` | Resets to 1 for each new hot lead |
| bot_version | `lead_bot_version` | main_vani, arrowhead, campaign variants |

**Dataset B: `Call_Logs_Hackathon.csv` (buyer-side experiment).** Buyer-side VANI bot, which matches the provided prompt. 5 to 9 Oct 2026, 1,000 calls, 825 buyers.

| Canonical | Source column | Notes |
|---|---|---|
| lead_id | `fk_glusr_usr_id` | Buyer GLID; 3 nulls, drop them |
| call_id | `fk_leap_call_records_id` | |
| ts | `entered_on` | Format `%d/%m/%y %H:%M` |
| answered | `is_call_answered == 1` | 142 nulls; infer from the disposition and report how many rows were inferred |
| disposition | `disposition_remarks` | 8 nulls, map to `UNKNOWN` |
| duration_s | none | See quirks below |

Dataset B quirks:
- **No duration column.** Do not derive one from `last_modified_date − entered_on`: it has minute resolution and measures when the record was updated, not talk time ("Technical Issue - Call Not Connected" rows show about 35 minutes). The buyer-side experiment therefore uses a disposition-based guardrail.
- **Outlier lead.** One buyer has 28 calls (the next highest has 6), likely a test account. Exclude leads above `exclude_leads_with_calls_gt` and report the exclusion in the profile.
- **Goals.** Success dispositions are `BL Enriched` (449) and `BL Approved` (64), about 51% of calls. The harm signal is `Abruptly Disconnected Call` (118), which the quality matrix links to buyer irritation.
- **Volume.** 1,000 rows over 5 days is too small for a 10% split, so this experiment always runs on scaled bootstrap volume, disclosed on the dashboard.

**Prompt.** Extract the docx to `prompts/base_buyer_vani.txt` with python-docx, preserving the Jinja blocks (`{% if %}…{% endif %}`) exactly. Do not go through a Markdown export: Markdown conversion escapes `_` and `!`, which corrupts the Jinja. The prompt is about 24,000 words, so variants patch it rather than copy it.

### 2.1 Stack
- **Language and tooling:** Python 3.11+, uv.
- **Data and stats:** pandas, numpy, scipy, statsmodels, pydantic v2.
- **Storage:** DuckDB for reading the historical data; SQLite in WAL mode for the experiment store.
- **Serving and UI:** FastAPI + uvicorn, Streamlit, plotly + kaleido.
- **Documents:** python-pptx for the slide, python-docx for extracting the base prompt.
- **Testing:** pytest, hypothesis.
- **Human interface:** a Makefile.

Why SQLite for the store: WAL mode lets the dashboard read while the runner writes, which the live demo needs. DuckDB permits only one process when anyone has write access.

### 2.2 Repo layout
```
PLAN.md  Makefile  pyproject.toml  README.md
configs/
  experiments/    # read by the engine
  sims/           # simulator ground truth; the engine must never read these
  scenarios/      # each scenario = {experiment, sim, seed}
prompts/base_buyer_vani.txt  # extracted from the provided .docx (§2.0)
src/abtest/
  contracts.py    # orchestrator-owned: pydantic models + function signatures
  store.py        # orchestrator-owned: SQLite schema + helpers
  config.py       # load, validate, freeze config; resolve variant prompts
  data.py         # one adapter per dataset (§2.0) -> canonical schema
  profile.py      # data profile report
  power.py        # pre-flight sample size and days needed
  router.py       # hash bucketing + live allocation
  api.py          # FastAPI /assign
  simulator.py    # cluster-bootstrap replay + effect injection + fault injection
  srm.py          # sample ratio mismatch check + split report
  boundaries.py   # alpha-spending boundaries + inflation factor
  metrics.py      # per-lead aggregation with maturation
  analysis.py     # z-statistics, CIs, repeated CIs -> LookResult
  rules.py        # pure function: evaluate_look(LookResult, boundaries, config) -> Verdict
  decision.py     # state machine, allocation changes, approval, decision log
  runner.py       # drives sim clock, schedules looks, applies decisions
  validation.py   # A/A, power, naive-peeking comparison
dashboard/app.py
slide/build_slide.py
tests/  reports/  logs/decisions.jsonl
data/             # gitignored symlink to the provided data
```

Makefile targets:
`setup test profile preflight simulate split-report boundaries validate scenarios live approve api dashboard slide demo`

Parameters are passed as `EXP=`, `SCENARIO=`, `DATASET=`, `SPEED=`, `ACTOR=`.

### 2.3 Configs

The experiment config is the only config the engine reads. It is frozen when the experiment starts, at which point we store:
- `config_hash`: SHA-256 of the canonical JSON,
- `prompt_hash`: SHA-256 of each resolved prompt.

Headline experiment, on Dataset A:
```yaml
id: vani-meeting-v1
dataset: bttc                  # bttc | buyer_logs
unit: lead_id
trigger: first_answered_call   # see §2.6
salt: "7f3a91"                 # new salt per experiment
allocation: {A: 0.90, B: 0.10}
variants:
  A: {base: prompts/base_buyer_vani.txt}
  B: {base: prompts/base_buyer_vani.txt, patch: {mode: append, text: "Offer two concrete meeting slots before asking open availability."}}
window: {days: 14, looks: 7}
maturation_days: 2             # from the Phase 1 profile
primary:   {name: meeting_fixed, type: binary, dispositions: ["Meeting Fixed"], better: increase, mde_rel: 0.15}
secondary: {name: answered_duration_s, type: continuous, role: guardrail, worse_if: increase, margin_rel: 0.10}
alpha_win: 0.025               # one-sided, O'Brien-Fleming-type spending
alpha_harm: 0.05               # one-sided, Pocock-type spending
power: 0.80
min_info_first_look: 0.20
srm_p_threshold: 0.001
promotion: auto                # auto | approval
```

Buyer-side experiment, on Dataset B. Only these keys differ:
```yaml
id: vani-buyer-qty-asks-v1
dataset: buyer_logs
exclude_leads_with_calls_gt: 10
variants:
  A: {base: prompts/base_buyer_vani.txt}
  B: {base: prompts/base_buyer_vani.txt, patch: {mode: replace_text, find: "quantity = 3", replace: "quantity = 2"}}
primary:   {name: bl_success, type: binary, dispositions: ["BL Enriched", "BL Approved"], better: increase, mde_rel: 0.10}
secondary: {name: abrupt_disconnect, type: binary, dispositions: ["Abruptly Disconnected Call"], role: guardrail, worse_if: increase, margin_abs: 0.02}
```
This B variant lowers the prompt's quantity ask limit from 3 to 2. The quality matrix treats repeated probing as looping behaviour, so it is a realistic change to test.

No seller-side prompt is in the folder, so the headline experiment versions the buyer-side prompt as a stand-in. Outcomes never depend on prompt text (§0), so this affects labels only. The README must say so.

Variant patch modes:
- `append`: add text to the end of the base prompt.
- `replace_section`: replace a marker-delimited section of the base prompt.
- `replace_text`: replace an exact string (`find`) that must occur exactly once in the base prompt; config validation fails otherwise.
- `full`: B is a complete prompt of its own.

Metric configuration:
- The primary metric is any set of dispositions. This is how "other dispositions as goals" is supported: by config, not code.
- The secondary metric is binary (a disposition set) or continuous (a numeric column). It is either a guardrail (`role: guardrail`, `worse_if`, and `margin_rel` or `margin_abs`) or a reported goal (`role: goal`, `better`).
- A secondary goal is reported with its CI but never gates the decision. The primary decides; the guardrail can only veto. Document this rule.

The sim config is ground truth and is read only by the simulator:
```yaml
true_effect: {primary_rel_lift: 0.25, secondary_change: 0.03}   # relative for continuous, absolute for binary
reference: {from: <date>, to: <date>, bot_version: <filter or null>}   # bootstrap pool, chosen at Checkpoint 1
volume: {daily_leads: <from profile>, scale: 1.0}
fault: {drop_b_exposure_rate: 0.0}   # set > 0 to demonstrate SRM detection
```

Required test: `analysis`, `rules` and `decision` functions accept only events, aggregates and the experiment config, and nothing under `configs/sims/` is ever imported or read on the engine path.

### 2.4 Router
- **Bucketing:** `bucket = int(sha256(f"{salt}:{lead_id}").hexdigest()[:15], 16) % 10000`. Assign B if `bucket < share_B * 10000`, otherwise A.
- **Stickiness:** while the experiment is RUNNING, assignment is a pure function of (salt, lead_id, configured allocation). It is sticky with no stored state.
- **After a decision:** the live allocation in `experiment_state` changes. PROMOTED means B gets 100%; STOPPED means A gets 100%. The router always reads the live allocation.
- **Exposure log:** every assignment is logged with `experiment_id, lead_id, variant, bucket, ts`.
- **API:** `GET /assign?experiment_id=&lead_id=` returns `{variant, state, config_hash, prompt_hash}`.

### 2.5 Simulator
**Canonical schema.** Each dataset adapter in `data.py` normalises its file to `call_id, lead_id, ts, answered, disposition, duration_s, attempt_no, bot_version`, using the mappings in §2.0. Fields a dataset lacks are null. Any other columns pass through unchanged.

**Cluster bootstrap.**
- Sample whole leads, with all their calls and the relative timing between those calls, with replacement, at `daily_leads × scale` leads per simulated day.
- Give each sampled lead a fresh ID: `sim-<seed>-<n>`.
- Route every new lead through the real `router.py`, not a copy of it.

**Arm A** keeps the historical outcome unchanged.

**Arm B, primary effect.** Inject at the lead level, and only into triggered leads (§2.6): a lead that never answers never heard either prompt, so its outcome cannot change. p is the historical success rate per triggered lead.
- If L > 0: each non-success lead becomes a success with probability `q = p·L/(1−p)`. Its last answered call takes the first disposition in the primary set, and that call's duration (where the dataset has one) is resampled from historical calls with that disposition. The B rate then moves exactly from p to p(1+L).
- If L < 0: each success lead loses its success with probability `|L|`. The success call's disposition is resampled from the historical distribution of answered non-success dispositions, and its duration from that disposition's durations.

**Arm B, secondary effect.**
- Continuous: multiply the duration of every answered B call by `(1 + secondary_change)`.
- Binary: after the primary injection, move triggered B leads into the secondary disposition set so its per-lead rate rises by `secondary_change` (absolute). Only use leads the primary injection did not touch.

**Truth report.** The simulator knows the counterfactual for every B lead, so it reports the realised true lift and realised secondary change in `reports/sim_truth_<scenario>.md`.

**Fault injection.** Drop `drop_b_exposure_rate` of B exposure events before they are logged. SRM detection must catch this.

**Clock.** Simulated days advance in steps. `SPEED` (seconds per simulated day) controls the live mode; batch mode runs as fast as possible.

### 2.6 Metrics and unit of analysis
- **Unit:** the randomisation unit is the lead, and so is the analysis unit.
- **Trigger:** a lead is assigned at its first dial but enters the analysis only at its first answered call, the first moment it hears the bot. Unanswered leads experienced neither prompt, so counting them adds noise and no information.
  - This is valid because the first pickup happens before the prompt says anything, so the variant cannot influence it.
  - Never trigger on a later pickup: whether someone answers a second call can depend on how the first call went.
- **SRM** is checked on both assigned leads and triggered leads (§2.8 rule 1).
- **Primary, per lead:** 1 if any answered call in the window has a disposition in the configured set, otherwise 0.
- **Secondary, per lead:**
  - Continuous: the mean over the lead's answered calls. Unanswered calls have no talk time; including them would turn the metric into answer rate.
  - Binary: the same rule as the primary.
- **Never compute statistics over calls.** Calls from the same lead are correlated and always share a variant, so counting calls overstates the sample size and produces false wins.
- **Maturation:** a lead enters the analysis at a look only once `first_exposure + maturation_days <= look_time`. Without this, leads whose follow-up calls have not happened yet are counted as failures.

### 2.7 Sequential method
**Test statistics.**
- Primary: two-proportion z-statistic, using the pooled SE for the z-statistic and the unpooled SE for the CI.
- Guardrail, continuous: relative change in means, `Δ = (μB − μA)/μA`, with a delta-method SE.
- Guardrail, binary: absolute difference in rates, `Δ = pB − pA`, with the unpooled SE.

**Information fraction.** At look k, `t_k = n_B,k / n_B,max`, counting matured B leads only.

**Maximum sample size `n_B,max`.**
1. Run a fixed-design two-proportion power calculation using: the allocation ratio from config, the baseline p from the profile, and the MDE.
2. Multiply by an inflation factor R, computed numerically from the boundaries. Find the drift θ at which P(crossing the win boundary by the final look | θ) equals the configured power; then `R = (θ / (z_{1−α} + z_{1−β}))²`.
3. Never hard-code R.

**Spending functions (one-sided).**
- Win (Lan-DeMets O'Brien-Fleming type): `α_w(t) = 2·(1 − Φ(z_{1−α_win/2} / √t))`
- Harm (Pocock type): `α_h(t) = α_harm · ln(1 + (e − 1)·t)`

**Boundary computation.**
- Compute boundaries at the actual observed information fractions. The Lan-DeMets approach keeps unequally spaced or unplanned looks valid.
- The first look happens only once `t ≥ min_info_first_look`.
- Treat each boundary as non-binding: compute it ignoring the other boundary. This keeps both error rates at or below nominal.
- Compute crossing probabilities exactly, by recursive numerical integration of the joint distribution of the z-statistics on a fine grid (Armitage-McPherson-Rowe recursion; Jennison & Turnbull 2000, ch. 19).
- Use Monte Carlo (≥10⁶ Brownian paths) only as a cross-check in tests. Monte Carlo cannot pin down the early win boundary, which sits around z ≈ 4.9; that is too far into the tail.

**Reference check.** For 5 equally spaced looks with α_win = 0.025, the win boundaries should be approximately 4.877, 3.357, 2.680, 2.290 and 2.031 (gsDesign `sfLDOF`). Test tolerance is ±0.01. If your numbers disagree, run the Monte Carlo cross-check and report to me. Do not tune the code until it matches.

**Guardrail at look k.** Use the repeated confidence interval `Δ̂ ± c_k·SE`, where c_k is the win boundary at look k. This keeps the guardrail check valid however many times we look.

**Why this method** (write this into the README and the slide):
- Alpha spending caps the overall false-win rate at 2.5%, no matter how many looks are taken.
- O'Brien-Fleming-type boundaries make early wins nearly impossible, so a lucky streak cannot trigger a promotion.
- The Pocock-type harm boundary stops bad variants fast, because harm costs real meetings.
- Rejected alternatives: Bayesian testing with a fixed P(B>A) threshold inflates errors under repeated checks; mSPRT needs a mixing-variance parameter that is hard to justify.

### 2.8 Decision rules
Evaluate these in order at each look. The first rule that matches decides; for guardrail margins, the direction follows `worse_if`.

| # | Condition | Outcome |
|---|---|---|
| 1 | SRM chi-square p < `srm_p_threshold` | **HALTED_SRM.** Results are untrusted; evaluate nothing further. |
| 2 | z_primary ≤ −h_k, OR the guardrail RCI lies entirely beyond the margin | **STOPPED_HARM.** Allocation becomes A 100%. |
| 3 | z_primary ≥ w_k AND the guardrail RCI lies entirely within the margin | **PROMOTED** (auto mode) or **PENDING_APPROVAL** (approval mode). On promotion, allocation becomes B 100%. |
| 4 | Primary crossed its win boundary but the guardrail is not cleared | Continue. At the final look, **BLOCKED_GUARDRAIL**; A stays. |
| 5 | Final look and nothing crossed | **INCONCLUSIVE.** A stays. Report the lift CI and the MDE this sample could have detected. |

**State machine.**
- DRAFT → RUNNING. This transition freezes the config.
- RUNNING → PROMOTED, PENDING_APPROVAL, STOPPED_HARM, BLOCKED_GUARDRAIL, INCONCLUSIVE or HALTED_SRM.
- PENDING_APPROVAL → PROMOTED or REJECTED.
- Terminal states are immutable.

### 2.9 Decision log
The log is append-only. It is written to both `logs/decisions.jsonl` and a `decisions` table, with one row per look (including CONTINUE) and one row per state change:

```
{ts_wall, ts_sim, experiment_id, config_hash, prompt_hash_A, prompt_hash_B, look, info_fraction,
 n_A, n_B,
 primary: {rate_A, rate_B, lift_abs, lift_rel, ci95, z, win_boundary, harm_boundary},
 guardrail: {mean_A, mean_B, rel_change, rci, margin},
 srm: {configured_share_B, observed_share_B, p},
 decision, reason, actor}
```

- `reason` is a readable sentence, for example: "Harm boundary crossed at look 2 (z = −3.10 ≤ −2.21); traffic returned to A."
- `actor` is either `engine` or the approver's name.

### 2.10 Dashboard (Streamlit)
**Styling.** Use IndiaMART colours: navy `#003366`, gold `#e8a517`, crimson `#C8102E` for harm, Calibri-style font stack.

**Banner on every page:** "Simulated traffic from <dataset>, volume ×<scale>: outcomes generated by replay with injected effects."

**Experiments list:** id, state badge, look k/K, n per arm, current decision.

**Experiment detail page:**
- Config and hashes.
- Split panel: configured versus achieved share with 95% CI, SRM p-value, and "leads in both arms: 0".
- Sample size versus n_max, as a progress bar.
- Primary and secondary metric values per arm, with CIs.
- **z-path chart** across looks with the win and harm boundaries drawn. This is the centrepiece.
- Guardrail RCI chart against the margin.
- Decision log table.
- Approve/Reject buttons when the state is PENDING_APPROVAL.
- A collapsed "Ground truth (engine never sees this)" panel showing the injected effect next to the engine's estimate.

**Validation page:**
- A/A false-win rate, sequential versus naive peeking.
- Power at the MDE.
- Distribution of the look at which experiments stopped.

The dashboard auto-refreshes so `make live` can be watched in real time.

### 2.11 Validation (proof pack)
- **A/A runs:** 2,000 runs with zero true effect. Report:
  - false-win rate, which should be ≈ 2.5%, with its binomial CI,
  - false-harm-stop rate, which should be ≤ 5%,
  - the same runs analysed with a naive fixed z = 1.96 threshold at every look, which should come out much higher.
- **Power runs:** 2,000 runs with true lift equal to the MDE. The promotion rate should be ≈ the configured power.
- **Fast path:** validation draws per-lead outcomes directly from the bootstrap distribution with vectorised numpy. It must call the same `analysis` and `rules` functions as the runner; no reimplemented stats. Target runtime is under 3 minutes on a laptop.
- **Output:** `reports/validation.md` plus PNG charts.

---

## 3. Phases

### Phase 0: Interview (no code)
The data context is already settled in §2.0. Do the following, report, then wait for "go".

1. List `data/` with file sizes and confirm every file in §2.0 is present. If any is missing, tell me which.
2. Print the header and first 3 rows of each CSV and confirm the §2.0 column mappings hold. Report any mismatch. Read nothing further yet.
3. Run `uv --version` and `python3 --version` and report the results.
4. Check your plugin list for the Codex plugin (typically `codex` and `codex-reply`) and report their exact names. If they are missing, tell me now; Phase 6 depends on them.
5. Ask me how much hackathon time remains, so you can flag which stretch items to drop.
6. Ask me the default promotion mode for the demo: auto or approval.

**Checkpoint 0 shows:** the file list, the mapping confirmation, tool versions, Codex tool names, and my two answers.

### Phase 1: Foundations and data profile
**Orchestrator first:**
- `git init`, `uv init`, and `pyproject.toml` with the §2.1 dependencies.
- The full directory layout and `.gitignore` (data/, the store DB, generated PNGs).
- `contracts.py` with these models: ExperimentConfig, SimConfig, Scenario, CallRecord, Exposure, LeadOutcome, LookResult, Verdict, Decision, ExperimentState. Include the function signatures for every module in §2.2.
- `store.py` with the SQLite schema.
- `prompts/base_buyer_vani.txt`, extracted per §2.0. Verify that `quantity = 3` occurs exactly once and the Jinja blocks survived intact.
- A Makefile with every target from §2.2, each printing "not built yet (Phase N)" until its phase is done.

**Parallel subagents:**

| Agent | Owns | Delivers |
|---|---|---|
| A | `data.py`, `profile.py`, their tests | both dataset adapters (§2.0) and `make profile DATASET=` |
| B | `config.py`, `configs/**`, their tests | config loading, validation, freezing, hashing, patch modes; placeholder scenario files for win, harm, inconclusive, guardrail_block, srm_fault, approval |
| C | `power.py`, its tests | fixed-design sample size and days needed; an inflation-factor hook that defaults to 1.0 until Phase 3; `make preflight` |

**Checkpoint 1 shows:**
- `make profile DATASET=bttc` and `make profile DATASET=buyer_logs` each write `reports/data_profile_<dataset>.md` containing:
  - row count, date range, lead count, and the rows the adapter dropped or inferred,
  - monthly volume and the `bot_version` mix (bttc),
  - the distribution of calls per lead, and any leads excluded as outliers (buyer_logs),
  - answer rate and triggered leads per day,
  - success rate per triggered lead (this is the baseline p),
  - duration distribution by disposition for answered calls, with the inferred unit (bttc),
  - follow-up call lag percentiles (these set `maturation_days`).
- `make preflight EXP=configs/experiments/vani-meeting-v1.yaml`, and the same for the buyer experiment, prints n_B,max, total n, days needed at historical volume against the window, and the smallest lift detectable within the window at a 10% split. It flags UNDERPOWERED if the days needed exceed the window.
- **Decide:**
  - the bttc bootstrap pool (date range and `bot_version` filter),
  - MDE, window and `maturation_days`,
  - the volume `scale` for each experiment. bttc should run at real volume (scale 1.0) if the power numbers allow; buyer_logs always needs scaling.

### Phase 2: Routing, simulator, split evidence
**Parallel subagents:**

| Agent | Owns | Delivers |
|---|---|---|
| A | `router.py`, `api.py`, their tests | hypothesis tests for stickiness, cross-process determinism, bucket uniformity, salt independence |
| B | `simulator.py`, its tests | everything in §2.5, including the truth report and fault injection |
| C | `srm.py`, its tests | SRM check and split report: configured vs achieved share with CI, per-day split, leads-in-both-arms count, re-hash determinism check |

**Orchestrator integration:** a minimal `runner.py` that simulates a scenario end to end into the store. No statistics yet.

**Checkpoint 2 shows:**
- `make simulate SCENARIO=win` prints leads and calls per arm.
- `make split-report SCENARIO=win` writes `reports/split_win.md` and a PNG showing:
  - configured 10.00% versus achieved share with 95% CI,
  - SRM p > 0.001,
  - leads in both arms = 0,
  - re-hash determinism: PASS.
- `make simulate SCENARIO=srm_fault && make split-report SCENARIO=srm_fault` shows SRM p < 0.001, flagged.
- `make api`, then two `curl` calls for the same lead return the same variant.
- `reports/sim_truth_win.md` shows the injected lift next to the realised lift.

### Phase 3: Statistical engine
**Parallel subagents:**

| Agent | Owns | Delivers |
|---|---|---|
| A | `boundaries.py`, `power.py` (taking ownership of the inflation hook), their tests | spending functions, recursive integration, Monte Carlo cross-check, inflation factor R |
| B | `metrics.py`, `analysis.py`, their tests | per-lead aggregation with maturation, z-statistics, CIs, delta-method guardrail, RCIs, returning LookResult |
| C | `rules.py`, `validation.py`, their tests | `evaluate_look` implementing §2.8 rules 1–5 as a pure function; the §2.11 validation harness, built against the contracts and run after integration |

**Checkpoint 3 shows:**
- `make boundaries` prints the win and harm boundaries for 5 and 7 equally spaced looks, with the reference check (§2.7) PASS and the Monte Carlo cross-check PASS.
- `make preflight` now shows the computed R and the updated n_max.
- `make validate` writes `reports/validation.md` showing:
  - A/A false-win rate ≈ 2.5% (with CI),
  - naive-peeking false-win rate, much higher,
  - false-harm-stop rate ≤ 5%,
  - power at the MDE ≈ the configured power.
- **Decide:** whether to accept these validation numbers before building rollout on top of them.

### Phase 4: Decisions, rollout, scenarios
**Parallel subagents:**

| Agent | Owns | Delivers |
|---|---|---|
| A | `decision.py`, its tests | state machine, allocation updates, approval flow, decision log (JSONL + table), `make approve EXP= ACTOR=` |
| B | `runner.py` (replacing the Phase 2 stub), its tests | sim clock, look scheduling by information fraction, the analysis → rules → decision loop, stopping at terminal states, `make live SCENARIO= SPEED=` |
| C | `configs/sims/*`, `configs/scenarios/*`, `tests/test_scenarios.py` | final scenario effects and terminal-state assertions |

Scenarios (all on bttc except the last):

| Scenario | Injected effect | Expected outcome |
|---|---|---|
| win | lift ≈ 1.5–2× MDE, duration +3% | PROMOTED |
| harm | lift −30% | STOPPED_HARM, at look ≤ 3 |
| inconclusive | lift +3%, well below the MDE | INCONCLUSIVE at the final look |
| guardrail_block | lift ≈ 2× MDE, duration change equal to the margin | BLOCKED_GUARDRAIL |
| srm_fault | drop 15% of B exposures | HALTED_SRM |
| approval | win scenario with `promotion: approval` | PENDING_APPROVAL → PROMOTED after approval |
| buyer_disconnect_stop | buyer experiment: BL success +10% relative, abrupt disconnects +5 pp | STOPPED_HARM via the binary guardrail |

**Orchestrator, after integration:** run each scenario across 20 seeds and report the hit rate. Then pin a demo seed that reaches the intended state. Report the rate honestly, for example "PROMOTED in 18/20 seeds".

**Checkpoint 4 shows:**
- `make scenarios` prints a table with these columns: scenario | terminal state | stop look | lift estimate (95% CI) | guardrail Δ (RCI) | hit rate over 20 seeds.
- `tail -n 20 logs/decisions.jsonl` shows one row per look, each with a readable reason.
- `make approve EXP=<approval experiment> ACTOR=kartik` moves PENDING_APPROVAL to PROMOTED, and the log records the actor.
- `make demo` runs the three required scenarios end to end.

### Phase 5: Dashboard, slide, docs
**Parallel subagents:**

| Agent | Owns | Delivers |
|---|---|---|
| A | `dashboard/**` | everything in §2.10. Reads the store only, with short-lived connections. |
| B | `slide/**` | one 16:9 slide via python-pptx in IndiaMART colours (see below), plus chart PNGs in `reports/` |
| C | `README.md`, `docs/demo_script.md` | README: setup, make targets, method justification, how outcomes are simulated, limitations. Demo script: a 3-minute walkthrough of the three scenarios, with exact commands. |

Slide layout:
- **Left:** a sample experiment's z-path chart with boundaries and a one-line result.
- **Middle:** the method and the decision rules in five short lines.
- **Right:** the workflow drawn as shapes: Route → Track → Analyse → Decide → Roll out.

**Checkpoint 5 shows:**
- `make dashboard` serves on localhost:8501.
- `make live SCENARIO=harm SPEED=3` running in a second terminal: you watch the z-path cross the harm boundary and the state flip to STOPPED_HARM.
- The approval scenario's buttons work.
- `make slide` produces `slide/one_slide.pptx`.
- The demo script reads cleanly start to finish.

### Phase 6: Codex review via MCP
**Preconditions:** `make test` and `make demo` are green and `phase-5` is tagged.

**Step 1: parallel review.** In one message, launch 4 subagents. Each calls the Codex MCP tool on this repo once, with a read-only sandbox and an approval policy of never.

Every Codex brief includes:
- PLAN.md §2 as the spec to review against,
- "Report only. Do not modify any file.",
- the required output format for each finding: id, severity (blocker/major/minor), file:line, what is wrong, mechanism (why it causes a wrong result), suggested fix, how to reproduce.

The four focus areas:

| # | Area | Files | Look for |
|---|---|---|---|
| 1 | Statistics | `boundaries.py`, `power.py`, `analysis.py`, `rules.py`, `validation.py` | spending formulas, recursion correctness, information fraction, trigger definition, per-lead unit, maturation, RCI use, inflation factor, validation reimplementing stats |
| 2 | Split and routing | `router.py`, `api.py`, `srm.py`, the simulator's routing path | hash bias, stickiness, salt handling, SRM test, fault injection, sim truth leaking into the engine |
| 3 | Decisions and rollout | `decision.py`, `runner.py` | rule order, state transitions, terminal-state immutability, allocation changes, approval flow, log completeness, config freeze |
| 4 | Metrics, data, UI, reproducibility | `metrics.py`, `data.py`, `dashboard/**`, `config.py` | adapter mappings, metric definitions, CI display, hashing, determinism, test coverage gaps |

Each subagent returns Codex's findings verbatim, plus its own one-line assessment of each.

If subagents cannot access the Codex MCP tool, issue the four Codex calls from the main session as parallel tool calls in a single message instead.

**Step 2: triage (orchestrator).**
- Deduplicate the findings.
- Accept or reject each one, with a reason.
- For each accepted finding, write a failing test that reproduces it first, then fix it.
- For each rejected finding, write the reason, citing the relevant PLAN.md section.

**Step 3: parallel fixes.** Group the accepted fixes by file ownership so no two subagents touch the same file. Launch the groups in one message.

**Step 4: re-verify.** Run `make test`, `make validate`, `make scenarios` and `make demo`.

**Step 5: second Codex pass.** Continue each Codex thread (`codex-reply` with the returned thread id), sending the fix diff and the triage table. Ask Codex to confirm the fixes and flag any regressions.
- If no thread id was returned, start a fresh session that includes the original findings.
- Do one round only. Anything still open goes into Known gaps.

**Checkpoint 6 shows:**
- `reports/code_review.md`: a findings table with these columns: id | area | severity | accepted/rejected | reason | test name | commit.
- All make targets green. Validation numbers are unchanged, or any change is explained.
- The commit is tagged `v1.0`.

---

## 4. Definition of done
- From a clean clone plus the data path, `make demo` runs the three required scenarios end to end.
- The A/A false-win rate falls within the binomial CI of 2.5%, and power at the MDE is within ±5 percentage points of target.
- Achieved split is within the CI of configured split; SRM p exceeds the threshold in every non-fault scenario; zero leads appear in both arms.
- Every look has a log row, and every state change has a reason and an actor.
- The dashboard shows: status, split, sample sizes, metrics with uncertainty, the z-path with boundaries, and the decision log.
- `slide/one_slide.pptx` exists.
- `reports/code_review.md` exists, with every blocker resolved.
