# skills.md: how we built Slow & Steady

**Team:** Hare & Tortoise.

**Problem statement:** PS05, Agent A/B Testing & Auto-Rollout. We extended it with the audit-and-fix
loop of PS01, so that the A/B engine tests prompt changes that come from the calls themselves.

**One line:** Slow & Steady listens to every Vani call, grades it against IndiaMART's own QA checklist,
finds the costliest failure, writes the prompt fix, proves it on 10% of traffic with a sequential A/B
test, and rolls the winner out. Then it starts again on the new prompt.

## The loop

| Stage | What happens | Built with |
|---|---|---|
| **Listen** | Call recordings become diarised bot / buyer transcripts, phone numbers masked, Hinglish in Latin script as IndiaMART's auditors write it | Gemini 3 Flash (audio input) via OpenRouter |
| **Judge** | Every call graded Pass / Non-Fatal / Fatal on 18 rubrics copied from IndiaMART's human QA checklist plus 3 prompt rules, each with a reason and the quote that proves it; the call's overall grade is its worst rubric, the QA team's own rule | Claude Sonnet 5.5 via OpenRouter; agreement measured on 160 calls IndiaMART's QA team had audited, calibrated on the first 40 and reported on the rest |
| **Improve** | Failures grouped into named root causes, ranked by a published formula, priority = 100 × (3·fatal + non-fatal) / (3·audited); the top prompt-fixable cause gets 1–3 minimal edits to the prompt, each citing the calls it should repair | Claude Opus 5.5 |
| **Gate** | A safety check reads the diff and blocks any change that adds a promise, weakens an honesty rule (for example "never claim to be human") or contradicts the prompt | Claude Sonnet 5.5 |
| **Replay** | The same buyers, simulated twice, once against Prompt A and once against Prompt B, then graded by the same judge: a fair before/after for the root cause | Claude Haiku 5.5 as bot and buyer |
| **Prove** | Prompt B gets 10% of leads through our A/B engine: sticky hash routing, sequential test with O'Brien–Fleming win and Pocock harm boundaries, a guardrail, a broken-split (SRM) check, and plain-English decisions | Python engine, phases 1–5 |
| **Roll out** | A person approves; B becomes production; the next cycle audits B | Prompt registry |

## Build journey

1. **Plan first.** `PLAN.md` fixed the method before any code: the unit (a lead), the metric, the
   sequential boundaries, the decision rules and the "ask me before" list. `PLAN_EXTENSIONS.md` and
   `TEAM.md` set how two developers work in lanes and merge through pull requests.
2. **Contracts, then parallel builders.** Every phase started with shared interfaces
   (`src/abtest/contracts.py`, later `src/abtest/ext/models.py`). Then several Claude Code subagents
   built modules in parallel against them, and the orchestrator integrated, tested, committed and
   tagged (`phase-1` to `phase-5`).
3. **Prove the statistics before trusting them.** With no live traffic, the simulator replays real
   call history and secretly injects a known effect into B. 2,000 no-effect runs gave a 2.30% false-win
   rate (target 2.5%), against 8.30% for naive repeated checking; at the designed effect the engine
   promotes about 80% of the time. Across 20 random seeds each demo scenario lands right 17–20 times.
4. **Independent review.** Lane B's statistical engine got a full review (47 findings, 32 confirmed
   and fixed), and every loop module had its own Codex CLI review before merge.
5. **The loop.** On day 2 we added PS01's audit-and-fix loop on top: the transcription, grading,
   fixing and loop modules, each built by a parallel subagent with offline tests (stubbed model,
   network blocked), then run on the real recordings.
6. **The platform.** Design research on Vapi, Sarvam, ElevenLabs, Retell, Cartesia, Linear, Resend
   and Braintrust (screenshots and computed styles through a headless browser) set an Indian-rooted
   but formal direction: khadi paper and indigo ink, Newsreader with Anek (a Mumbai foundry whose
   faces cover Latin, Devanagari and Telugu), a kolam-style loop emblem, audits set as a ledger, prompt
   fixes as contract redlines, and the A/B monitor drawn as the corridor between its win and harm
   boundaries.

## Tools

- **Claude Code** (Claude Opus 5.5) as orchestrator: planning, contracts, parallel subagent workflows,
  integration, git and pull requests.
- **Codex CLI** for independent code reviews.
- **OpenRouter** for every model call, through one client (`src/abtest/ext/llm.py`) that caches each
  request on disk (reruns are free and reproducible) and enforces a spend cap.
- **Sarvam AI platform** for the live voice agent (our teammate's part of the demo).
- **Python**: pandas, numpy, scipy, statsmodels, pydantic, FastAPI, SQLite (WAL), DuckDB, Streamlit,
  plotly, python-pptx, pytest and hypothesis. The platform website is plain HTML, CSS and JavaScript
  with no build step.

## Honesty and privacy

- There is no live traffic. Every A/B result is simulated from real history with a stated, assumed
  effect for B, and the platform labels it so wherever a result appears.
- Replay tests are simulated conversations, and they compare simulated A against simulated B.
- Recordings and transcripts stay on the machine (git-ignored). They go to the model provider only
  through explicit `--yes-send-real-*` flags that the team ran, and phone numbers are masked.
- The recordings come from the SquadStack-run Vani; the only prompt provided is the Sarvam-run Vani
  buyer prompt. Both follow the same QA checklist, so the failures and the fix carry over, but a fix
  must be tested on the bot it ships to before rollout.
