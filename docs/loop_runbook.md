# Self-improving loop: runbook

What to run, where the files go, how the money is capped, how to stop it, and what is still missing before any real call.
The design is `PLAN_LOOP.md`; this build is its section L9: **no base file is edited, no real phone call is placed**.

## What is real and what is simulated

| Step | In this build |
|---|---|
| Observe a call corpus | Real: Saaras v4 batch speech-to-text with diarization, roles, rubric scores from `sarvam-105b`, a lead-level report and an audit sheet |
| Diagnose, propose, gate, bot tests | Real Sarvam models (or the offline stub): the proposer writes patches, the gate checks them, simulated buyers talk to each candidate prompt |
| Live A/B | **Simulated traffic** through the real engine (routing, looks, rules, decision log) on a synthetic buyer dataset. The lift and the `rubric_risk` shift are injected from the bot-test result or from a fixed sequence, so the A/B says nothing about real business impact |
| Audit gate, dogfood calls | Skipped in sim mode and recorded as skipped in each generation report: they guard real traffic |
| Roll-out | A promoted prompt becomes `data/derived/loop/prompts/<sha256>.txt` and the next generation's base |

## Commands

Everything runs as `uv run python -m abtest.loop <command>`; the `make` targets wrap them.

| Make target | Command | What it does |
|---|---|---|
| `make loop-test` | `pytest tests/loop -q` | The loop's tests. Offline: stub provider, no network |
| `make loop-demo` | `demo` | Three generations on the stub provider and a fresh `demo.db`, offline, about 15 seconds. Prints the table and exits 1 if no generation was promoted. Expect generation 1 PROMOTED and the next two NO_CANDIDATE: the stub proposer has only a few patches, and a tried patch is never proposed again |
| `make loop-observe SRC=<csv> [LIMIT=30]` | `observe --src CSV [--limit N] [--seed S]` | Keeps answered calls of at least `observe.min_duration_s`, takes a seeded sample, downloads, transcribes (resumable), scores, stores transcripts and scores in the loop DB, writes `reports/loop/rubric_<csv stem>.md` and the audit sheet, prints counts and spend. Needs the SDK, hence `uv run --with sarvamai` |
| `make loop-agreement SHEET=<csv>` | `agreement --sheet PATH` | Reads the audit sheet a reviewer filled and prints Spearman rho with its CI, per-criterion agreement, role accuracy and TRUSTED or NOT TRUSTED |
| `make loop-run [GENERATIONS=1]` | `run --generations N [--provider stub\|sarvam] [--db PATH] [--config PATH]` | Runs N generations after the last stored one on `data/derived/loop/loop.db`, one line each. Real Sarvam by default |
| `make loop-status` | `status [--db PATH]` | Generations table per loop id, total spend against the cap, pause state |
| `make loop-pause` / `make loop-resume` | `pause` / `resume` | Create / remove the PAUSE flag |

Other flags: `run` and `observe` take `--provider stub` for an offline pass (env `LOOP_PROVIDER` overrides every flag and the config); `run --quiet` prints one line per generation instead of one per state change.

Dashboard: `make dashboard`, then the **Loop** page. It reads the loop DB read-only and shows the generations, the latest generation's candidates (status, gate, bot-test score and CI, patch), spend against the cap and the guard log of the latest experiment. Use the sidebar (or `ABT_LOOP_DB=data/derived/loop/demo.db`) to look at the demo DB.

## One generation

`GEN_START`, `OBSERVED` (scores of the production prompt: reused if stored, else observed with base-only bot-test conversations), `AUDIT_PENDING` (skipped), `DIAGNOSED` (top `diagnose.top_k` rubric criteria by weighted failure), `PROPOSED` (`propose.candidates` patches, pre-checked), `GATED`, `BOT_TESTED` (paired bootstrap against the base on the seed personas; the pick rule needs the CI lower bound above `bot_tests.min_gap` and no hard-criterion failure), `DOGFOOD` (skipped), `LIVE_AB` (simulated), then the experiment's terminal state (`PROMOTED`, `STOPPED_HARM`, `BLOCKED_GUARDRAIL`, `INCONCLUSIVE`, `HALTED_SRM`, `REJECTED`), then `GEN_END`.

- The row is saved at every transition, and every candidate when it changes, in `loop_generations` and `loop_candidates`.
- The **production prompt** is the base prompt (the template's variant A, `prompts/base_buyer_vani.txt`) with every PROMOTED generation's pick applied in order, replayed from the ledger. The base file is never edited.
- `NO_CANDIDATE` ends a generation early: nothing passed the pre-checks, everything was gate-blocked, or the bot tests picked nobody.
- A generation cut short by the spend cap ends `GEN_END` with outcome `SPEND_CAP`.
- A generation a crash left unfinished is **re-run** by the next `run` (model calls replay from the disk cache), not skipped.
- If the engine ends in a state that needs a human (`PENDING_APPROVAL`), the generation ends with that outcome and nothing is rolled out. The loop's A/B sets promotion to auto, so this should not happen in sim mode.

Each generation writes `reports/loop/generation_<loop id>_<n>.md`: the diagnosis table, the candidates with pre-checks, gate probabilities and bot-test score with CI, the pick and why, the A/B's terminal state, looks, lift CI, rubric RCI and who decided, the injected truth labelled **simulated traffic**, and the spend. Aggregates only: no transcript text, no candidate rationale, no patch text. Those stay in the DB.

## Where the files go

Personal data stays under `data/derived/loop/` (gitignored). Tracked files hold aggregates only.

| Path | Holds | Tracked |
|---|---|---|
| `data/derived/loop/loop.db` | transcripts, rubric scores, generations, candidates, spend, guard log (`demo.db` for the demo) | no |
| `data/derived/loop/transcripts.jsonl` | one transcript per line; `observe` resumes by skipping call ids already there | no |
| `data/squadstack/recordings/<call_id>.mp3` | downloaded recordings | no |
| `data/derived/loop/audit/<corpus>.csv` | the sheet a reviewer fills | no |
| `data/derived/loop/prompts/<sha256>.txt` | each promoted prompt, content-addressed | no |
| `data/derived/loop/cache/` | the model-call cache; a generation replays from it with no network | no |
| `data/derived/loop/synthetic/`, `sims/` | the synthetic buyer dataset and per-generation sim configs for the A/B | no |
| `data/derived/loop/PAUSE` | the pause flag | no |
| `reports/loop/rubric_<corpus>.md` | per-criterion failure rates with lead-level CIs, early hang-ups | yes |
| `reports/loop/generation_<loop id>_<n>.md` | a generation's report | yes |

`make loop-demo` writes `reports/loop/generation_vani-loop-demo_<n>.md`. They are regenerated each time; do not commit them unless you want them in the repo.

The base `abtest.db` is never touched: the loop DB is separate. `SARVAM_API_KEY` goes in `.env` (gitignored) or the environment, never in chat, a config or a commit; the code never prints it.

## The spend cap

`caps.max_spend_inr` (default 150 for this build) covers every real model call across all generations of a loop DB. The provider reserves a `loop_spend` row before each paid call and settles it after, so a retry never double-counts, and it refuses a call that would cross the cap. Before the bot tests the controller also estimates their cost from `configs_loop/prices.yaml` and refuses to start them if the estimate exceeds what the cap has left. The stub provider spends nothing. Either way the generation ends `SPEND_CAP` and the loop stops. To see spend: `make loop-status`.

Rough sizes from the L0 measurements: speech-to-text with diarization about INR 0.75 per minute of audio; a bot turn with the rendered buyer prompt about INR 1; the default run (3 seed personas, 2 candidates, 3 bot turns, 3 generations) is well inside the cap.

## The pause switch

`make loop-pause` creates `data/derived/loop/PAUSE`; `ABT_LOOP_PAUSE=1` does the same without a file. The controller checks before each generation, so a running generation finishes cleanly and the next one does not start. `make loop-resume` removes the file. It does not touch the base engine. Pausing has no effect on a real-call surface yet because none is built (see below); the dialer is the enforcement point for those (PLAN_LOOP §L1.6).

## Stop conditions

`run` stops, and says why, on: the pause flag; `caps.max_generations` generations stored for this loop id; three consecutive `NO_CANDIDATE` generations (read from the ledger, so a new `run` stops too); total spend at the cap; the spend-cap stop above. To start over, give the loop a new `loop.id` in a copy of `configs_loop/default.yaml` (`--config`) or use a new `--db`; the old loop's rows, prompts and reports stay.

To revert production to an earlier prompt: production is replayed from the ledger, so use a new loop id (it starts from the base prompt); the promoted prompt files are never edited.

## Pre-flight checklist for a real run

1. `.env` has `SARVAM_API_KEY` (a real key; never paste it anywhere else).
2. `uv run pytest tests/loop -q` is green, `make loop-demo` ends with a PROMOTED generation.
3. `configs_loop/default.yaml` is what you mean: `caps.max_spend_inr`, `caps.max_generations`, `bot_tests.personas`, `propose.candidates`. `loop.mode` must stay `sim`; `live` is refused.
4. For `observe`: the corpus CSV with `glid, call_id, bot, call_status, duration_sec, call_start_ist, recording_url` (the SquadStack sample); the recordings download into `data/squadstack/recordings/`. Run `make loop-observe SRC=... LIMIT=30` first and read `reports/loop/rubric_<name>.md`.
5. Fill the audit sheet (`human_score` 1 to 5, `human_role_ok` y/n, and `human_<criterion id>` y/n for each hard criterion) and run `make loop-agreement SHEET=...`. The rubric is trusted for a live guardrail only when rho and every hard criterion pass the thresholds (`audit.min_rho`, `audit.min_hard_agreement`). Sim mode does not wait for this.
6. `make loop-run GENERATIONS=1`, then `make loop-status` and the Loop page.

## What is still needed for real calls

Nothing below exists in this build. It is the production route (PLAN_LOOP §L3, §L4 L3, §L4 L4) and needs answers to the L0 questions first.

- **Answers to the L0 questions** (PLAN_LOOP §L4 L0): which flow the loop improves (the buyer prompt is in the repo; the seller corpus needs its prompt supplied); which live surface (S1 the Sarvam Voice Agents platform, S2 a self-hosted Pipecat agent on Twilio or Exotel, or sim only); where the live disposition comes from (the platform's own end-of-call disposition, never the rubric judge; no source means no live A/B); who takes dogfood calls (names and numbers go into the ignored `configs_loop/dogfood.yaml`, never chat) and when; the live caps (`max_share_b`, `max_live_calls_per_day`, `max_spend_inr`, `max_generations`; the volume arithmetic in L0 Q5); compliance (calling hours, do-not-call handling, recording disclosure, any line the bot must say verbatim, each becoming a hard criterion and a gate question); and the time left.
- **Surface access** (PLAN_LOOP §L2.4): for S1 a KYC-verified Sarvam organisation, a number, a public webhook URL and whether the prompt can be set per call; for S2 a Twilio or Exotel account and number allowed to call Indian mobiles and a public `wss` URL. The S1 adapter in `loop/telephony/s1_sarvam.py` refuses every call unless `live.surface: s1`, `caps.max_live_calls_per_day` is above zero and `configs_loop/dogfood.yaml` exists; its endpoint paths are marked `TODO(L0)`.
- **The dialer** (`loop/dialer.py`, not built): the single place that checks the caps and the pause flag before each call, assigns the variant with `router.assign`, verifies the prompt hash and places the call.
- **Ingest and the look path** (not built): fetch the recording, transcribe, role-label, score, write `loop_rubric_scores`, wait for the scoring watermark, join `rubric_risk` into the look.
- **Dogfood** (not built): three real calls from the picked candidate to consenting team members, scored; an explicit human approval before any lead is called.
- **Base-file prerequisites for production** (PLAN_LOOP §L3 P1 to P6, requests to Lane A): extra guardrails in the engine, the `/hooks/calls` and `/hooks/proposals` service, the daemon tick, the registry. Until they land the rubric guardrail runs in the loop's own look driver (`loop/live_ab.py`, `loop/guard.py`).
- **Controller changes** for live mode: the waiting states `AUDIT_PENDING`, `DOGFOOD_REVIEW` and `LIVE_AB` while the base experiment is RUNNING or PENDING_APPROVAL, the untrusted-rubric stop, and doubling K after a `NO_CANDIDATE` (PLAN_LOOP §L2.6). This build always uses `propose.candidates`.

## Troubleshooting

- `error: loop module X is not built yet`: that module's file under `src/abtest/loop/` is missing.
- `error: seed personas not found`: `configs_loop/personas_seed.yaml` is missing; the bot tests need at least one card.
- `batch speech-to-text needs the sarvamai SDK, which is not installed ...`: run through `make loop-observe` (it adds `--with sarvamai`) or `uv run --with sarvamai python -m abtest.loop observe ...`.
- `stopped: caps.max_generations (3) reached`: raise `caps.max_generations` in a copied config, or start a new loop id.
- A run ended `SPEND_CAP`: read the generation report's Steps table for which step; raise `caps.max_spend_inr` deliberately if you mean to continue.
