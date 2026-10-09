# PLAN_EXTENSIONS.md: Autonomy, Webhooks, Jev and CUPED on top of PLAN.md

This file **adds to** PLAN.md. It never replaces anything in it.

- PLAN.md stays the source of truth for everything it defines. If the two files conflict, PLAN.md wins and you ask me.
- All of PLAN.md's operating rules still apply here: the phase loop, subagent briefs, checkpoint blocks, "ask me before", determinism.
- Re-read this file at the start of every extension wave.

---

## E0. Timing: when to act on this file

1. **If `phase-5` is not tagged yet:** keep building the base plan exactly as PLAN.md says. Do not start extension work. A working, tagged base demo is the insurance policy; extensions are layered on top of it.
2. **Once `phase-5` is tagged:** run X0, then X1, then the waves in order.
3. **Base Phase 6 (Codex review) runs after the extension waves**, with the extra review area in §E8 added. If Phase 6 already ran, do a focused Codex pass on the extension files only, using §E8.

---

## E1. Non-breaking rules (these override convenience)

### 1. Feature flags, default off
Every extension sits behind a flag:
- Experiment-level features use an optional config key (`variance_reduction`, `extra_guardrails`).
- Service-level features use an environment variable.

With every flag off, the system must produce **byte-identical** results to the tagged base. The golden test in X1 enforces this.

### 2. New code goes in new files
Put new code under `src/abtest/ext/`, `dashboard/pages/`, `tests/ext/` and `integrations/`. Base files may change only as listed in this table, and nothing else:

| Base file | Allowed change |
|---|---|
| `contracts.py` | New models, and new **optional** fields with defaults that preserve current behaviour |
| `config.py` | Accept the new optional keys. New keys must be **excluded from the canonical JSON when unset**, so existing `config_hash` values stay identical. |
| `simulator.py` | A `sink` parameter (default `direct` = current behaviour); `qa_risk` injection only when configured; attaching seller pre-period history only when CUPED is on |
| `runner.py` | Only if it has no single-look function: extract `evaluate_look(experiment_id, as_of)` in a behaviour-preserving refactor |
| `metrics.py`, `analysis.py`, `rules.py` | Dispatch to the CUPED estimator when configured. Evaluate `extra_guardrails`; an empty list must leave behaviour unchanged. |
| `power.py`, `validation.py` | An optional variance-reduction factor / `--cuped` option |
| `slide/build_slide.py`, `README.md` | Wave 4 only |
| `Makefile` | Append new targets only. Never rename or change existing ones. |
| Everything else in the base (`api.py`, `router.py`, `decision.py`, `boundaries.py`, `srm.py`, `store.py`, `dashboard/app.py`, …) | **No edits** |

### 3. Storage changes are additive only
- New tables live in `src/abtest/ext/store_ext.py`.
- Apply them through a migrations table (`ext_schema_version`).
- Never alter or drop a base table or column.

### 4. Log rows stay identical when features are off
New fields in decision-log rows (for example CUPED estimates) are written only when that feature is enabled. Otherwise rows are byte-identical to before.

### 5. One branch per wave
- Work on branch `ext/wave-N`.
- Merge to main only when the full suite and the golden test are green.
- Tag the merge `ext-N`.

### 6. Model calls: a scoped exception to PLAN.md's "No LLM calls"
- Judge calls (§E4) are allowed **only** in `ext/judge*`, `ext/patch_gate.py` and `ext/qa_labeler.py`.
- The engine path (analysis, rules, decision, and the simulator's outcome generation) never calls a model.
- Tests never touch the network: they use the `stub` judge provider, a local notify receiver, and the local registry.

### 7. Allowed new dependencies
`httpx`, `python-dotenv`, and `langfuse` as an optional extra. Ask before adding anything else.

### 8. Every checkpoint adds one line
Add `Base files touched:` to each checkpoint block, generated from `git diff --stat phase-5..HEAD` and filtered to base files.

---

## E2. Shared conventions

### Environment variables
Keep a `.env.example`. Never commit secrets.

| Variable | Purpose |
|---|---|
| `ABT_WEBHOOK_SECRET` | HMAC secret for inbound and outbound webhooks |
| `ABT_APPROVAL_SECRET` | Signs approval links |
| `ABT_DEMO_MODE=1` | Allows proposals to launch simulator traffic (§E3.2) |
| `ABT_PUBLIC_URL` | Base URL used to build links in notifications |
| `NOTIFY_URLS` | Comma-separated outbound webhook targets |
| `JUDGE_PROVIDER` | `jev`, `litellm` or `stub` |
| `JEV_BASE_URL`, `JEV_API_KEY` | Jev access |
| `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `LITELLM_MODEL` | Fallback judge |
| `REGISTRY` | `local` or `langfuse` |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Langfuse registry |

### Webhook security (all inbound hooks and all outbound notifications)
- **Headers:** `X-ABT-Timestamp` (unix seconds, wall clock) and `X-ABT-Signature` = hex HMAC-SHA256 of `timestamp + "." + raw_body`.
- **Rejections:** return 401 for a bad signature or for clock skew over 300 s. Compare signatures in constant time.
- **Why:** the timestamp inside the signature stops someone replaying a captured request later.

### Idempotency
- Proposals are keyed by `proposal_id`; calls are keyed by `call_id`, enforced with a unique index and insert-or-ignore.
- A duplicate returns 200 with `{"duplicate": true}`.
- **Why:** webhooks are delivered at least once. Counting a retried delivery twice would corrupt the split, the SRM check and the metrics.

### Concurrency
- SQLite WAL with `busy_timeout = 5000`.
- Short transactions only; no connection is held open across ticks.

---

## E3. Autonomy core: webhooks, daemon, outbox

### E3.1 Service wrapper
`ext/service.py` builds the app by importing the base FastAPI `app` from `api.py` and mounting new routers on it. `api.py` itself is not edited. `make serve` runs the service and the daemon together.

### E3.2 `POST /hooks/proposals`
This is how a prompt change enters the system: from a person, from a PS01-style fix generator, or from `make propose`.

```json
{
  "proposal_id": "uuid",
  "template": "vani-meeting-v1",
  "variant_b_patch": {"mode": "replace_text", "find": "quantity = 3", "replace": "quantity = 2"},
  "overrides": {"allocation": {"A": 0.9, "B": 0.1}},
  "demo_sim": "win"
}
```

The pipeline runs in this order and stops at the first failure. Every outcome is recorded in a `proposals` table with its reasons.

1. **Authenticate and dedupe** (§E2).
2. **Build the config.** Start from the template config. The new experiment id is `<template>-<6-char hash of proposal_id>`, and the salt is derived from the proposal_id hash (deterministic, new per experiment). Validate with the base `config.py`; the patch must apply cleanly.
3. **Jev patch gate** (§E4.2), if enabled.
4. **Pre-flight power check** (base `power.py`). UNDERPOWERED means reject, unless `overrides.allow_underpowered: true`.
5. **Registry:** publish the A and B prompt versions (§E3.6).
6. **Start the experiment** through the base start path (DRAFT → RUNNING, which freezes the config).
7. **Demo traffic:** if `ABT_DEMO_MODE=1` and `demo_sim` names a sim config, launch the simulator as a subprocess for this experiment, posting to `/hooks/calls`.

The endpoint responds 202 with `{experiment_id, status, reasons, gate, preflight}`.

### E3.3 `POST /hooks/calls` and `POST /hooks/calls/batch`
These are call-completed events, in the same shape the live bot would send. Batches hold up to 500 events.

```json
{"experiment_id": "...", "call_id": "...", "lead_id": "...", "ts": "<event time>",
 "answered": true, "disposition": "Meeting Fixed", "duration_s": 212,
 "reported_variant": "B", "summary": "optional", "transcript": "optional"}
```

Handling rules:
- **The server re-derives the variant** with the base router; it never trusts `reported_variant`.
- **Assignment integrity:** count mismatches between the reported and derived variant in an `assignment_mismatch` counter, shown on the dashboard. A mismatch means the bot served the wrong prompt, and that invalidates the comparison the same way an SRM failure does.
- **Writes** go to the same exposure and outcome tables the base runner uses.
- **Live labelling:** if a `summary` is present and QA labelling is enabled, queue it for the labeller (§E4.3).

### E3.4 Simulator HTTP sink
- Add a `sink` parameter to `simulator.py`: `direct` (default, unchanged) or `http`.
- `http` calls the base `/assign` per new lead, as a real bot would, then posts calls in time order in batches to `/hooks/calls/batch`.
- The engine can't tell this traffic from a live bot. Going live means pointing the bot's webhook at the same endpoint.

### E3.5 Daemon (`ext/daemon.py`)
A plain asyncio loop, no scheduler library. Every `tick_seconds` it does three things:

1. **Heartbeat:** write a row to `daemon_heartbeat`.
2. **Evaluate due looks:**
   - For each RUNNING experiment, compute the **watermark**: the maximum event `ts` ingested for that experiment.
   - When the watermark passes the next scheduled look time, call the base `evaluate_look(experiment_id, as_of)`. Maturation is handled by the base code as before.
   - Looks are driven by event time, not wall-clock time, so a fast or slow replay produces the same decisions as batch mode.
3. **Run the outbox dispatcher** (§E3.6).

**Parity test (required):** for the same scenario and seed, the http-sink + daemon run must produce the same decision-log rows as the base direct run. Wall-clock timestamps are excluded from the comparison.

### E3.6 Outbox dispatcher, notifications, approvals, registry
Notifications and rollout are **derived from the decision log**, so `decision.py` is never edited. This is the transactional-outbox pattern:
- The dispatcher reads `decisions` and `proposals` rows past a stored cursor (`outbox_cursor`).
- After a crash it resumes from the cursor and resends whatever wasn't confirmed.
- All actions are idempotent, so resending is harmless.

**Notifications (`ext/notify.py`):**
- Each event is POSTed, signed, to every URL in `NOTIFY_URLS`.
- Retries: 3, with exponential backoff. Every attempt is recorded in a `deliveries` table.
- Event types: `proposal.rejected`, `experiment.started`, `experiment.look` (only if `notify_on_looks`), `experiment.pending_approval`, `experiment.promoted`, `experiment.stopped_harm`, `experiment.blocked_guardrail`, `experiment.inconclusive`, `experiment.halted_srm`, `experiment.rejected`.
- Payload: `{event, experiment_id, state, look, reason, primary: {lift_rel, ci95}, guardrails, links: {dashboard, approve?, reject?}}`.
- `make notify-sink` runs a local receiver that verifies signatures and prints payloads. Use it in tests and as the demo fallback.

**n8n workflow:**
- File: `integrations/n8n/approval_flow.json`, an importable workflow: Webhook node → Code node that verifies the HMAC → Switch on `event` → Email node.
- Pending-approval emails contain the approve and reject links. Every other state change gets a summary email.
- Document the import steps and the credentials to set in `integrations/n8n/README.md`.

**Approvals:**
- Links have the form `{ABT_PUBLIC_URL}/hooks/approvals/{token}`. The token is a URL-safe HMAC over `experiment_id|action|expiry|actor`, with a 24-hour expiry.
- **GET** shows a confirmation page with a button. **POST** performs the action through the same base function `make approve` uses, and records the actor.
- **Why GET must never approve:** email security scanners prefetch links, so an approve-on-GET link would approve itself.

**Registry (`ext/registry.py`)** has this interface:
```python
publish(experiment_id) -> {"A": version_ref, "B": version_ref}
set_production(version_ref) -> None
production() -> version_ref
```
- **`local` adapter (default):** writes `prompts/registry.json`. Each version records number, prompt_hash, path and labels.
- **`langfuse` adapter (Wave 4):**
  - Prompt name `vani-buyer`. Labels: `production`, `exp-<id>-A`, `exp-<id>-B`.
  - Check the method names against the **installed** Langfuse SDK version; do not assume them from memory.

**Rollout rule (applied by the dispatcher):**
- PROMOTED → `set_production(B)`.
- STOPPED_HARM, BLOCKED_GUARDRAIL, INCONCLUSIVE, REJECTED or HALTED_SRM → make sure production is A.

`GET /registry/production` returns the current production version.

---

## E4. Jev: judge, patch gate, QA labeller

### E4.1 Judge client (`ext/judge.py`)
```python
class Question(BaseModel):
    id: str
    kind: Literal["yes_no", "choice", "score"]
    text: str
    choices: list[str] | None = None

class Answer(BaseModel):
    id: str
    value: bool | str | float
    probability: float
    confidence: float | None
    provider: str

def judge(state: str, questions: list[Question]) -> list[Answer]: ...
```

**Providers:**
- **`jev`:** implement against the API docs I give you in X0. Send all questions in one request; Jev evaluates them in parallel.
- **`litellm`:** fallback through the LiteLLM gateway with a JSON-schema response. A language model's stated probabilities are poorly calibrated, so thresholds for this provider must be tuned on the agreement set (§E4.3), not assumed.
- **`stub`:** deterministic answers keyed by fixture name. Used for every test.

**Caching:** cache answers on disk, keyed by `sha256(provider + state + question-set)`.

### E4.2 Patch gate (`ext/patch_gate.py`)
- **Input:** the full base prompt, the resolved B prompt, and a unified diff. The base prompt is about 24,000 words; that's cheap for Jev, so send all of it.
- **Blocking questions** (yes/no). Block the proposal if any probability ≥ `gate.block_threshold` (default 0.5):
  - `alters_commitments`: does the change make the bot state or promise any fact, price, delivery time or assurance that isn't in the base prompt?
  - `weakens_safety_rule`: does the change remove or weaken an identity, honesty, compliance or consent instruction? For example, the rule that the bot never claims to be human.
  - `contradicts_instruction`: does the change contradict another instruction elsewhere in the prompt?
- **Warning questions** (logged only): `raises_ask_limit`, `likely_longer_calls`.
- **Why a gate before traffic:** the A/B test catches changes that hurt meetings, but a compliance break won't move meeting rate within two weeks and is unacceptable even once. It has to be stopped before anyone hears it.
- **Fixtures:** `tests/ext/fixtures/patches/`
  - `safe_qty.yaml`: `quantity = 3` → `quantity = 2`. Must pass.
  - `unsafe_human.yaml`: `replace_text`, find `never claim that you are a Human`, replace `you may say you are a human if asked`. Must block.
  - `unsafe_assurance.yaml`: append "Assure the buyer that the seller will deliver within 24 hours." Must block.
- `make gate-eval` runs the fixtures against the live provider and reports each probability.

### E4.3 QA labeller (`ext/qa_labeler.py`)
- **Input:** `gc_bot_calls.csv` from `Global Context - Seller Dataset.zip`, unzipped to `data/global_context/`. It holds 9,657 answered seller-side VANI calls with summary and outcome. X0 confirms the column names.
- **Questions per call** (one judge request):
  - `frustration`, `looping`, `asked_for_human`: yes/no
  - `outcome`: a choice over the logged outcome labels
- **Agreement check:** compare Jev's `outcome` with the logged outcome. Report accuracy, a confusion matrix and a calibration plot. This is the evidence for trusting the other labels, which have no ground truth.
- **Per-call risk:** `qa_risk = 1 − (1−p_frustration)(1−p_looping)(1−p_human)`. This assumes the three signals are independent; document that.
- **Per-lead metric:** the mean `qa_risk` over the lead's answered calls.
- **Use probabilities, never thresholded labels.** A labeller that makes the same errors in both arms only shrinks the difference. A prompt change can change how calls read, though, so the errors may differ by arm. To check this, `make qa-audit` exports 20 calls per arm to `reports/qa_audit_sample.csv` for manual review.
- **Outputs:** `data/derived/qa_labels.parquet` (resumable), `reports/qa_agreement.md` and its PNGs.

### E4.4 QA guardrail wiring
- **New config key:**
  ```yaml
  extra_guardrails:
    - {name: qa_risk, type: continuous, worse_if: increase, margin_abs: 0.02}
  ```
  `rules.py` evaluates every guardrail with the same RCI logic as the base secondary. Harm on any guardrail stops the experiment; promotion needs all guardrails cleared.
- **Keep it to one extra guardrail.** Each additional guardrail is another chance for a false block, which costs power.
- **Simulator:** answered calls draw `qa_risk` from the empirical Jev-labelled distribution. Arm B shifts it by `true_effect.qa_risk_change`, clipped to [0, 1]. The truth report includes the realised change.
- **New scenario:** `qa_guardrail_stop`, a win on the primary with `qa_risk_change: +0.06`. Expected outcome: STOPPED_HARM via `qa_risk`.

---

## E5. CUPED variance reduction

Applies to the bttc dataset only. Config validation rejects it for any dataset without multi-month history, and if the build uses only `Call_Logs_Hackathon.csv`, skip this wave.

```yaml
variance_reduction: {method: cuped, pre_period_days: 60}
```

**Mechanism.** Each seller's pre-experiment record predicts part of their outcome. That predictable part is noise for the A-versus-B comparison, so subtracting it shrinks the variance and smaller lifts become detectable. This is valid only because the covariate predates the experiment, so the treatment cannot touch it.

**Covariates**, computed strictly before the experiment start:
- `x1`: the seller's meeting rate over answered attempts in the pre-period. Sellers with no pre-period history get the pooled mean imputed.
- `x2`: `has_history`, 0 or 1.

**Estimator.**
- Regression-adjusted outcome: `Y_adj = Y − θ·(X − X̄)`, with θ estimated by OLS **pooled across both arms** at each look.
- The primary is then tested as a difference in means of `Y_adj` with a Welch SE.
- Boundaries and the information-fraction definition are unchanged.

**Pre-flight.** Estimate R² from historical data (pre-period against the following window) and set `n_max_cuped = n_max × (1 − R²)`. Freeze this at start like every other config value.

**Simulator.** The reference window must start at least `pre_period_days` after the data starts. Bootstrapped sellers carry their real pre-period history, and effect injection never touches the pre-period.

**Validation.**
- `make validate CUPED=1`: the A/A false-win rate must stay within the binomial CI of 2.5%.
- Report power at the MDE with and without CUPED, and the realised variance reduction.

**Dashboard.** Raw and adjusted lift side by side. The decision uses whichever the config names.

---

## E6. Phases

### X0: Repo audit and interview (no code)

**Do these and report:**
1. Run `git tag`, `git log --oneline -15`, `make test`, and list the Makefile targets and `src/abtest/`.
2. State which base phases are tagged.
3. State whether PLAN.md has a §2.0 Data sources section. If it doesn't, say which datasets the build actually uses; this decides whether CUPED applies.
4. Find the base functions that §E1 relies on and report their names and signatures: the single-look evaluation, start-experiment, approve/reject, and the decision-log reader. Say where a behaviour-preserving extraction would be needed.
5. Check `data/global_context/` for `gc_bot_calls.csv` and print its header.

**Then ask me:**
1. Jev access: the API docs URL or endpoint, and the key environment variable. If I don't have access, we use the `litellm` provider.
2. LiteLLM fallback: base URL, key environment variable, model.
3. Langfuse host and keys, or skip. Skipping means the `local` registry.
4. n8n webhook URL and notification email address, or skip. Skipping means `make notify-sink`.
5. Time remaining. The cut order is: Wave 4 Langfuse first, then Wave 3 (CUPED), then Wave 2's QA labeller. Never cut Wave 1 or the patch gate.

**Checkpoint X0 shows:** the audit, my answers, and the list of waves we will run.

### X1: Golden baseline (orchestrator only, no subagents)
- With fixed seeds, snapshot into `tests/golden/`:
  - `make scenarios`: terminal state, stop look, every decision-log row except wall-clock fields, and the config hashes,
  - the split reports,
  - the validation summary numbers.
- Add `tests/test_golden.py`. It reruns everything with every extension flag off and asserts equality.
- `make golden` regenerates the snapshots. Only regenerate them at a base phase tag, and never during extension work.
- Add the `ext/` package skeleton, `store_ext.py` with migrations, and `.env.example`.

**Checkpoint X1 shows:**
- `make test` passes, including the golden test.
- Proof that the golden test works: temporarily change one scenario seed, show the golden test fail, then revert.

### Wave 1: Autonomy core (requires `phase-5`)
**Orchestrator first:** add the new contracts (proposal, call event, delivery, registry version) and the extension tables.

| Agent | Owns | Delivers |
|---|---|---|
| A | `ext/service.py`, `ext/hooks.py`, `ext/security.py`, `ext/approvals.py`, `tests/ext/test_hooks*.py` | §E3.1–E3.3 plus approvals; tests for signatures, skew, idempotency, assignment mismatch, and prefetch-safe GET |
| B | `ext/daemon.py`, the `sink` change in `simulator.py`, the `evaluate_look` extraction if needed, `tests/ext/test_parity.py` | §E3.4–E3.5 and the parity test |
| C | `ext/outbox.py`, `ext/notify.py`, `ext/registry.py` (local adapter), `integrations/n8n/**`, `tests/ext/test_outbox*.py` | §E3.6, including crash-resume and idempotency tests |
| D | `dashboard/pages/1_Autonomy.py` | Proposals with their reasons, daemon heartbeat and watermark per experiment, assignment mismatches, delivery log, registry history |

New Makefile targets: `serve`, `notify-sink`, `propose SCENARIO=`, `parity SCENARIO=`.

**Checkpoint W1 shows:**
- Terminal 1: `make serve`. Terminal 2: `make notify-sink`. Terminal 3: `make propose SCENARIO=harm` returns 202 with an experiment id. No further commands are needed: daemon ticks advance the looks, the experiment stops itself, a signed `experiment.stopped_harm` payload is printed, and `curl $ABT_PUBLIC_URL/registry/production` still shows A.
- `make propose SCENARIO=approval`: the notification contains approve/reject links. Opening the approve link shows a confirmation page; confirming moves the state to PROMOTED, records the actor, and switches production to B.
- Re-posting the same batch leaves the counts unchanged. A bad signature returns 401.
- `make parity SCENARIO=win` reports IDENTICAL.
- If n8n is configured, the email arrives.
- The dashboard Autonomy page shows a live heartbeat.

### Wave 2: Jev (requires Wave 1)

| Agent | Owns | Delivers |
|---|---|---|
| A | `ext/judge.py`, `ext/patch_gate.py`, `tests/ext/test_gate.py`, `tests/ext/fixtures/**` | §E4.1–E4.2, wired into proposal pipeline step 3 |
| B | `ext/qa_labeler.py`, `tests/ext/test_qa_labeler.py` | §E4.3, with `make label-qa`, `make qa-report`, `make qa-audit` |
| C | The `extra_guardrails` change in `rules.py`, `qa_risk` injection in `simulator.py`, `configs/sims/qa_guardrail_stop.yaml`, `tests/ext/test_extra_guardrails.py` | §E4.4 |
| D | `dashboard/pages/2_Judge.py` | Gate verdicts per proposal (question, probability, threshold), QA agreement, calibration, audit sample |

**Checkpoint W2 shows:**
- `make propose SCENARIO=unsafe_human` returns REJECTED, with the gate probabilities and a `proposal.rejected` notification. No traffic is ever sent.
- The `safe_qty` proposal passes and starts.
- `make gate-eval` prints the live-provider probabilities for all three fixtures.
- `make qa-report` writes `reports/qa_agreement.md` with outcome accuracy and a calibration plot.
- The `qa_guardrail_stop` scenario ends in STOPPED_HARM, and the reason names `qa_risk`.
- Golden test still green.

### Wave 3: CUPED (requires `phase-5`; independent of Waves 1–2)

| Agent | Owns | Delivers |
|---|---|---|
| A | `ext/cuped.py`, the dispatch in `analysis.py`/`metrics.py`, `tests/ext/test_cuped.py` | §E5 estimator, plus a unit test showing θ is pooled and the covariates are pre-period only |
| B | The pre-period attach in `simulator.py`, the variance factor in `power.py`, the `--cuped` option in `validation.py` | §E5 simulator, pre-flight and validation |
| C | `dashboard/pages/3_Variance_Reduction.py` | Raw vs adjusted lift CIs, realised variance reduction, A/A with and without CUPED |

**Checkpoint W3 shows:**
- `make preflight EXP=configs/experiments/vani-meeting-v1.yaml CUPED=1` shows R², a smaller n_max, and a smaller detectable lift within the window.
- `make validate CUPED=1`: A/A false-win rate within the CI of 2.5%, and power at the MDE higher than without CUPED.
- Golden test still green.

### Wave 4: Rollout to Langfuse, auto-demo, docs (requires Wave 1)

| Agent | Owns | Delivers |
|---|---|---|
| A | The `langfuse` adapter in `ext/registry.py`, `tests/ext/test_registry_langfuse.py` (mocked) | Promotion moves the `production` label |
| B | `scripts/auto_demo.sh`, the `make auto-demo` target, `docs/autonomous_demo.md` | See below |
| C | `slide/build_slide.py`, `README.md` | Add the autonomous loop to the workflow diagram: Webhook → Jev gate → A/B engine → Outbox → Rollout. Add an "Autonomous mode" README section. |

`make auto-demo` starts the service, the daemon and the notify-sink, then fires four proposals: win, harm, inconclusive and unsafe_human. It then prints the dashboard URL and the commands for watching the results.

**Checkpoint W4 shows:**
- `make auto-demo` with no further input: within about a minute, the four proposals end in PROMOTED, STOPPED_HARM, INCONCLUSIVE and REJECTED.
- Notifications are delivered for each.
- The registry shows production on the promoted variant only.
- If Langfuse is configured, its UI shows the `production` label moved.

---

## E7. Definition of done (extensions)
- The golden test is green with every flag off.
- The parity test reports IDENTICAL.
- `make auto-demo` reaches all four terminal states with no manual input, and every state change has a delivered notification.
- The patch gate blocks both unsafe fixtures and passes the safe one.
- The QA agreement report exists. `qa_guardrail_stop` ends in STOPPED_HARM.
- With CUPED on, the A/A false-win rate is within its CI and the variance reduction is reported (if Wave 3 ran).
- No test makes a network call.

---

## E8. Extra area for the Codex review (base Phase 6)
Add a fifth review subagent alongside the four in PLAN.md:

| # | Area | Files | Look for |
|---|---|---|---|
| 5 | Integrations and autonomy | `src/abtest/ext/**`, the `sink` and `evaluate_look` changes, `integrations/**` | HMAC and skew handling, constant-time compare, idempotency under retries, approve-on-GET, outbox crash/resume and duplicate side effects, watermark logic and parity, judge provider fallback, test isolation from the network, CUPED pooled θ and pre-period leakage, config-hash stability with flags off |

Also give each of the four base areas the extension files that touch its modules.
