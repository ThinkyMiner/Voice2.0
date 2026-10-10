# Vani Lab: approach note

**Team Hare & Tortoise.** The hare ships a prompt change on a hunch; the tortoise ships it on evidence.

**Problem statements.** PS05, Agent A/B Testing & Auto-Rollout, extended with PS01's
audit-and-fix loop, so that the A/B engine tests prompt changes that come from the calls themselves.

**In one line.** Vani Lab listens to Vani's calls, grades each one on IndiaMART's own QA checklist,
finds the costliest prompt failure, writes the fix (Prompt B), proves it on 10% of leads with a
sequential A/B test, and rolls the winner out after a named person approves. Then it audits the new
prompt and starts again.

## Why

Today a Vani prompt change goes live to every caller on judgement alone. A bad change costs meetings
until someone notices, and a good one cannot be proven. The QA team audits calls by hand against an
18-column checklist, but those findings do not flow back into the prompt in a measurable way.

## How it works

```
 recordings ─▶ Listen ─▶ Judge ─▶ Improve ─▶ Gate ─▶ Replay ─▶ Prove on 10% ─▶ Approve ─▶ Roll out
                 ▲                                                                          │
                 └──────────────────────── next cycle audits the new prompt ◀────────────────┘
```

| Stage | What happens | How |
|---|---|---|
| Listen | Recordings become bot / buyer transcripts with timestamps; phone numbers masked; Hinglish in Latin script, as the auditors write it | Gemini 3 Flash (audio in) via OpenRouter |
| Judge | Every call graded Pass / Non-Fatal / Fatal on 18 rubrics, one per column of IndiaMART's QA sheet, plus 3 prompt rules (never claims to be human, no forbidden commitment, language policy). Each grade carries a reason and the quote behind it; the call's grade is its worst rubric, the QA team's own rule. Teams add their own rubrics in the Rubric Studio | Claude Sonnet 5.5 |
| Improve | Failures become named root causes ranked by `priority = 100 × (3·fatal + non-fatal) / (3·audited)`; the top prompt-fixable cause gets 1–3 minimal, cited edits | Claude Opus 5.5 |
| Gate | Five safety questions about the edit; it is blocked when adding a commitment, weakening a safety rule or contradicting the prompt is likely (p ≥ 0.5). `make gate-eval` checks the model on a safe edit and two unsafe ones (3 of 3 right on 10 Oct) | Jev (TypeSafe's Jev Router, via OpenRouter) |
| Replay | The same buyers, built from the real calls, are simulated against Prompt A and Prompt B and graded by the same judge | Claude Haiku 5.5 as bot and buyer |
| Prove | Prompt B gets 10% of leads through the A/B engine below | Python engine, no model calls |
| Roll out | A named approver promotes B; B becomes production in the prompt registry; the next cycle audits B | Prompt registry and decision log |

Every model call goes through one client that caches each request on disk (reruns are free and
reproducible) and enforces a spend cap. Nothing real is sent without an explicit
`--yes-send-real-*` flag.

## The A/B engine: proving a prompt change

- **Unit and metric.** The lead is the randomisation unit (sticky hash bucketing on the live
  allocation). Primary: Meeting Fixed per answered lead; guardrail: call duration (+10% margin).
- **Sequential design.** Group-sequential looks with Lan-DeMets O'Brien–Fleming win boundaries
  (α = 2.5% one-sided) and Pocock harm boundaries (α = 5%), recomputed at the information fractions
  actually observed. The guardrail uses repeated confidence intervals. The maximum sample size is the
  fixed-design n inflated by R ≈ 1.033, computed from the boundaries.
- **Decision rules,** in order: broken split (SRM p < 0.001) halts; harm stops B; a win with the
  guardrail cleared promotes (automatically, or pending a named approver); a win without the
  guardrail continues and is blocked at the end; otherwise inconclusive, and A stays. Each decision is
  logged with a plain-English reason and its weekly business impact.
- **Validation.** No live traffic exists, so outcomes replay real Apr–Sep 2026 call history with a
  known effect injected into B that the engine never sees. Over 2,000 no-effect runs the false-win
  rate is 2.30% (95% CI 1.69–3.06%) against 8.30% for naive peeking; at the designed effect the
  primary crosses 80.0% of the time. Each demo scenario (win, harm, inconclusive) lands right on
  17–20 of 20 random seeds.

## Results on real calls

Data: the human QA audit sheet's SquadStack inbound rows. These are buyer-side Vani calls, the bot
the provided prompt belongs to; 160 of them were transcribed and graded by both the judge and the
auditors. The 250-call production sample is the seller-side Vani, so it is transcribed but not
used to fix the buyer prompt.

- **Agreement with the auditors** (learned on the first audited calls, measured on the held-out
  rest: calibrated on 40 calls, measured on 120 others): the judge agrees well with the auditors on
  6 of 16 checks, 82–92% of the time (quantity, units, both specs, product change, AstBuy transfer;
  kappa 0.63–0.78).
  Agreement on the call's overall grade is low (47.5%, kappa 0.04): the auditors also judge what the bot
  saved into the lead record, which the judge cannot see, and audio-only columns (dead air,
  pronunciation) cannot be graded from text. The full table is in `reports/judge_agreement.md`.
- **Cycle 1 (10 Oct):** 160 calls judged on v1 (21 fatal, 113 non-fatal, 26 pass; fatal rate
  13.1%), grouped into 28 root causes (19 prompt, 2 flow, 4 speech, 2 catalogue, 1 platform).
  - Try 1, "Bot misstates or alters buyer's spec value" (priority 2.5, 4 fatal calls): the fix (v2)
    passed the gate but changed nothing in replay, so the loop dropped it by itself.
  - Try 2, "Buyer-volunteered specs ignored or deferred" (priority 2.5): v3, three cited edits;
    the gate passed (Jev); in replay the failure fell from 25% to 0%, but over only 4
    conversations, and Looping Behavior got worse (+75 pts). The simulated A/B test (an assumed
    +20% effect for B) crossed the win line on day 5 and now waits for a person; our
    recommendation is to reject it because of the looping regression, and the demo video shows that
    rejection, after which cycle 2 opens on v1.
- **Live calls through Sarvam:** the router places real outbound calls through the Sarvam agent
  (`Outbound-8a963506-5435`), A on agent version 1 and B on version 2, logging each lead's arm
  before the phone rings; a test call to a team member connected on 10 Oct. The video's live scene
  is an offline rehearsal with a simulated Sarvam, labelled as such.

## Honest limits

- A/B outcomes are simulated from real history with a stated, assumed effect for B; replay tests
  compare simulated conversations. Both are labelled wherever they appear.
- The judge's overall grade does not yet match the auditors'; per-column agreement is the number
  to trust, and adding the saved lead record to the judge's input is the next step.
- The recordings come from the SquadStack-run Vani, while the provided prompt is the Sarvam-run
  buyer prompt. Both follow the same checklist, so the failures carry over, but a fix must be tested
  on the bot it ships to.
- Recordings, transcripts and audits stay on the machine and are never committed.

## Run it

```bash
make setup && make demo          # the A/B engine: win, harm, inconclusive, approval
make platform                    # the platform on http://127.0.0.1:8600
make loop-status                 # the prompt registry, the cycles and the next action
```

Details: `README.md` (method, decision rules, validation, limitations), `skills.md` (how it was
built), `docs/loop_api.md` (the platform API).
