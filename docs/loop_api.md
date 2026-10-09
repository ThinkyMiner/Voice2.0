# Slow & Steady platform API (contract)

Served by `src/abtest/ext/service.py` (`make platform`, port 8600): the base FastAPI app plus these
JSON routes, plus the static site from `src/abtest/ext/web/` at `/`. Every object below is a model in
`src/abtest/ext/models.py` (or `abtest.contracts`) dumped with `model_dump(mode="json")`.
Errors: 404 for an unknown id, 422 for a bad body, 409 for an illegal state change.

## Read

| Route | Returns |
|---|---|
| `GET /api/overview` | `{production: PromptVersion, cycle: Cycle \| null, cycles: [Cycle] (newest first), kpis: {calls_total, calls_audited, fatal_rate, non_fatal_rate, pass_rate, agreement_overall, agreement_kappa, fatal_vs_rest_accuracy, root_causes, fixes_tested, experiments, per_week_gained, per_week_protected} (over the calls the loop judges), corpus: "audited" \| "production" \| "replay", simulated_traffic: true}` |
| `GET /api/calls?source=&overall=&flag=&q=&limit=200` | `{total, items: [{call_id, source, duration_s, language, prompt_version, overall, disposition, risk_flags, summary, n_fatal, n_non_fatal, human_overall}]}`; `overall`/`human_overall` are null until graded |
| `GET /api/calls/{call_id}` | `{transcript: Transcript, audit: CallAudit \| null, human: {overall, grades: {rubric_id: Grade}, remarks: {rubric_id: str}} \| null, audio_url: str \| null, rubrics: {rubric_id: {name, category, root_type}}}` |
| `GET /api/recordings/{call_id}` | the local mp3 (`audio/mpeg`); 404 when it is not on this machine |
| `GET /api/audit?version=v1&source=` | source: a transcript source or `all`; default the loop's corpus on v1. `{version, source, n_calls, n_audited, split: {pass, non_fatal, fatal}, human_split: {...} \| null, agreement: AgreementReport \| null, by_rubric: [{rubric_id, name, category, root_type, n_applicable, pass, non_fatal, fatal, na}], risky: [{call_id, flags, overall, summary}]}` |
| `GET /api/issues?version=v1` | `{version, formula: "priority = 100 × (3·fatal + non-fatal) / (3·audited)", items: [RootCause] (priority desc)}` |
| `GET /api/fixes` | `{items: [Fix]}` (newest first) |
| `GET /api/fixes/{id}` | `{fix: Fix, root_cause: RootCause}` |
| `GET /api/prompts` | `{production: str, items: [PromptVersion]}` |
| `GET /api/prompts/{version}/text` | the prompt text (`text/plain`) |
| `GET /api/experiments` | `{items: [{experiment_id, state, looks_done, n_looks, allocation: {A, B}, lift_rel, started, updated, cycle: int \| null, simulated: true, assumed_effect: str \| null}]}` |
| `GET /api/experiments/{id}` | `{experiment_id, state, config, allocation, n_b_max, looks: [{look, info_fraction, z, win_bound, harm_bound, lift_rel, ci95: [lo, hi], n_a, n_b, decision, reason, ts_sim}], decisions: [Decision], impact: {per_week, unit}, cycle: int \| null, assumed_effect: str \| null}` |
| `GET /api/rubrics` | `{items: [Rubric]}` (latest version of each; archived ones have `active: false`) |
| `GET /api/rubrics/{id}/versions` | `{items: [Rubric]}`, every saved version, oldest first |
| `GET /api/settings` | models per role, spend and cap, data locations, `allow_real_rubric_tests` |
| `GET /api/loop` | `{cycle: Cycle \| null, next_action: str, busy: bool, log: [str]}` |

## Write

| Route | Body | Effect |
|---|---|---|
| `POST /api/rubrics` | Rubric fields except `version`, `builtin`; plus `actor` | creates a custom rubric, version 1 |
| `PUT /api/rubrics/{id}` | same | saves a new version (old versions are kept; audits cite `rubric_version`) |
| `DELETE /api/rubrics/{id}` | – | archives it (`active: false`); built-ins can be archived, never deleted |
| `POST /api/rubrics/test` | `{rubric: {...}, call_id}` | grades one transcript with that rubric: `RubricVerdict`; 403 for a real call unless the platform runs with `LOOP_ALLOW_REAL=1` |
| `POST /api/experiments/{id}/decision` | `{actor, approved}` | approve or reject a PENDING_APPROVAL experiment (base `decision.approve`) |
| `POST /api/loop/advance` | `{until: Stage \| null}` | runs the loop in the background up to that stage: 202 `{accepted, until}`, 409 if a run is in progress; `GET /api/loop` shows progress. The web never sends real calls to the model; that needs the CLI flag |

## Honesty rules the UI must keep
- There is no live traffic: every experiment is simulated replay of real call history with an assumed effect for B. Show `assumed_effect` wherever an experiment result appears.
- Replay tests are simulated conversations; label them so, and compare simulated A against simulated B.
- Transcripts and recordings are real customer calls: the platform binds to 127.0.0.1 and nothing under `data/` is ever committed.
