# Demo script: about 6¾ minutes

Vani Lab, by Team Hare & Tortoise. **Part 1 (3:00)** proves a prompt change with the A/B
engine on the Streamlit dashboard: B is worse and stopped early, B wins and is promoted,
inconclusive, then the approval gate and the split evidence. **Live calls (0:35)** shows the same
router on real phone calls through Sarvam. **Part 2 (3:10)** shows where Prompt B comes from: the
loop on the platform. The spoken lines (in quotes) are about 950 words in all.

The figures quoted are from the pinned demo seeds in `configs/scenarios/`: harm 1103, win 1101,
inconclusive 1104, and approval 1101, which is the win run's traffic. They match
`reports/scenarios.md` and a full rehearsal on 10 Oct. If a seed is re-pinned, read the numbers off
the screen instead; the sentence stays the same.

`ACTOR` is one word, no spaces (`ACTOR=kartik`, not `ACTOR="Kartik Sehgal"`).

## Before the clock starts (about 2 minutes, off stage)

The `data/` folder must hold the provided files (see the README's Setup section).

Terminal 1: build the store with the batch scenarios, then serve the dashboard and leave it
running. Keep this terminal off camera: Streamlit prints the machine's network and public addresses.

```bash
make setup
make simulate SCENARIO=win
make simulate SCENARIO=inconclusive
make simulate SCENARIO=approval
make split-report SCENARIO=win
make dashboard
```

Check the `make simulate SCENARIO=approval` output: its third line from the end must say
`final state PENDING_APPROVAL`. If it says anything else, skip the approval segment and give its 25
seconds to the split evidence.

Optional, only if you may open the **Validation** page: `make validate` (under a minute). It
rewrites `reports/validation.md` and the two charts, and all four checks should print PASS.

Open http://localhost:8501 once before recording: the first load takes a few seconds. Record at
1920 px wide, or collapse the sidebar: at 1440 px the decision log's Actor column is cut off. The
sidebar has the **Experiment** picker and the auto-refresh toggle (leave it on); the page shows the
experiments table, with its **Business impact** column, then the picked experiment's detail.
Everything below the headline needs scrolling; the auto-refresh keeps your scroll position.

Terminal 2: type this now, but do not press Enter until 0:15.

```bash
make live SCENARIO=harm SPEED=3
```

It takes about 35 seconds: a few seconds to start, then 3 seconds per simulated day. Look 1 (day
7) prints about 22 seconds after Enter and the stop (day 10) about 9 seconds later; the dashboard
re-reads the store every 3 seconds, so allow up to 2 more on screen. Pressing Enter at about 0:15
puts look 1 on screen at about 0:38 and the stop at about 0:47, while you talk about them. These
times were measured on this laptop; a slower machine shifts them.

## 0:00 to 0:35: the problem

**Screen:** the experiments list, under the banner saying the traffic is simulated.

**Do:** at about 0:15, while you talk, press Enter in terminal 2.

"Today a VANI prompt change goes live to every seller on someone's judgement. This platform gives
the new prompt ten percent of leads, tracks Meeting Fixed per answered lead, with call duration as a
guardrail, and lets the evidence decide. All traffic here is replayed from six months of real
calls, with a known effect injected that the engine never sees. The smallest change it is sized
to catch, fifteen percent, is worth about a hundred and sixty-five meetings a week."

No screen shows that weekly figure; the 15% itself is in the decision log's first row and the
config panel.

## 0:35 to 1:10: B is worse, stopped early (live)

**Do:** pick `vani-meeting-v1-harm` in the sidebar (the run you started at 0:15 with
`make live SCENARIO=harm SPEED=3`). Wait for look 1 to appear before saying "Day seven", and for
look 2 before "Day ten".

**Point at:** the z-path chart ("Sequential test: z-path across looks"). The crimson harm boundary
sits near z = −2.2 at every look. Look 1 (day 7) lands at z = −2.01, just inside it (−2.15); its dot
sits almost on the boundary's marker, so point at the **z vs boundaries** card above the chart
instead. Look 2 (day 10) lands at z = −2.74, past −2.19. The state badge flips to STOPPED HARM,
the headline above the charts reads "B stopped on day 10, protecting ≈ 246 Meeting Fixed a week at
full rollout", and the decision log gets its reason row.

"This variant secretly loses thirty percent of meetings. Watch the z-path. Day seven: B is behind,
but not past the harm line yet, so the engine keeps going. Day ten: it crosses. The engine stops B
and sends all traffic back to A. At full rollout, B would have cost about two hundred and fifty
Meeting Fixed a week. We caught it in ten days, and only one lead in ten ever heard it. The harm
line is built to stop fast."

## 1:10 to 1:45: B wins, promoted automatically

**Do:** pick `vani-meeting-v1-win`.

**Point at:** the gold win boundary, which starts near z = 4.7 at the first look and falls towards
2. At look 3 (day 13) the z-path crosses it (3.16 against 2.93), but the guardrail chart
("Guardrail: repeated CI vs margin") shows the duration RCI still reaching past the +10% margin, so
the run continues. At look 4 (day 16) z = 3.45 clears 2.61 and the RCI sits inside the margin:
PROMOTED, with the headline "B promoted on day 16: ≈ +240 Meeting Fixed a week at full rollout".
Then open the collapsed "Ground truth (engine never sees this)" panel at the bottom.

"Here B tells the bot to offer two concrete meeting slots. The win line starts near five standard
errors, so a lucky first week can never promote anything. At look three B crosses the line, but the
duration guardrail is not proven safe yet, so the engine waits. At look four, on day sixteen, both
clear and B is promoted automatically, worth about two hundred and forty extra Meeting Fixed a
week. And the ground truth: we injected plus twenty-five percent; the engine measured plus
twenty-three."

## 1:45 to 2:15: inconclusive

**Do:** pick `vani-meeting-v1-inconclusive`.

**Point at:** the lift tile, −5.9% with its 95% CI from −16% to +4%; the headline, which gives B's
effect as a 95% range of Meeting Fixed a week; the z-path, which stays between the two boundaries
for all seven looks; and the final decision-log reason: the lift CI, the lift this sample could
have detected (about 15%), and "A stays".

"Most ideas are not winners. We secretly made this one three percent better, far below what is
worth detecting; the engine sees anything from minus sixteen to plus four percent. The z-path never
leaves the band. At the final look the engine says so: B is somewhere between minus one
hundred and seventy and plus forty-three meetings a week, too small to call. This sample could have
caught a fifteen percent lift; it saw nothing like that. A stays, and no coin flip goes live."

## 2:15 to 2:40: the approval gate

**Do:** pick `vani-meeting-v1-approval` (badge PENDING APPROVAL). The **Approver** box holds this
machine's user name: select all of it, type your name (one word), and click **Approve: promote B**.
Or, in terminal 2:

```bash
make approve EXP=vani-meeting-v1-approval ACTOR=kartik
```

**Point at:** the "Decision needed" box with what approving is worth a week (≈ +240 at seed 1101,
the same draw as the win run), then the badge going from PENDING APPROVAL to PROMOTED, the headline
"(approved by <name>)" and the new decision-log row with your name as the actor. The green
confirmation and the Decision needed box vanish at the next refresh, within 3 seconds, and the page
moves up.

"In approval mode the same kind of win waits for a person. The headline shows what approving is
worth, about two hundred and forty meetings a week, and B keeps its ten percent until someone signs
off. I approve, and the log records my name, the reason and that value. Every decision, automatic
or human, lands in an append-only log with the config and prompt hashes."

## 2:40 to 3:00: the split evidence

**Do:** pick `vani-meeting-v1-win` again and scroll to the **Traffic split** panel (the same
numbers are in `reports/split_win.md`).

**Point at:** configured 10.00% against achieved 9.91% with its 95% CI [9.46%, 10.37%], SRM
p = 0.68, leads in both arms 0, re-hash PASS.

"And the split is real: ten percent configured, nine point nine one achieved, inside its
confidence interval. No lead ever heard both prompts. If B's exposures ever stop being logged, the
SRM check halts the experiment before anyone trusts a number. One command,
make demo, rebuilds all of this."

## 3:00 to 3:35: the same router on real calls

**Before the clock** (on the day, between 10:00 and 21:00 IST; see
[docs/runbook_live_calls.md](runbook_live_calls.md)): the live experiment has run on the
consenting team phones in `data/live/team_phones.yaml`.

```bash
make calls ARGS="start --split 0.5"
make calls ARGS="call --yes-place-real-calls --max 4"
make calls ARGS=fetch       # once the calls have ended; again until nothing is waiting
make calls ARGS=status
make judge SOURCE=live ARGS=--yes-send-real-transcripts   # optional: the judge grades each live call
```

The arm each lead gets is fixed by a hash of the experiment id and the lead id: at `vani-live-c1`,
`team-01` gets B and `team-02` gets A. Don't rename leads to balance the arms; that would be
choosing the split by hand.

**Screen:** the platform (http://127.0.0.1:8600), **Experiments**, then `vani-live-c1` under
*Live calls*.

**Point at:** the title (“quantity = 3” becomes “quantity = 2”), the change card's "A is agent
version 1, B is version 2", the "Real calls" line, and the **Calls per arm** card: the Sarvam agent
ID, and each arm's agent version, leads assigned, calls placed and connected, and outcomes. Then
the decision log's first row (B gets 50%). Optional: **Calls**, the **Live** filter, and one call,
which shows its variant and agent version.

"The router is not just for simulation. This live experiment sends real phone calls through Sarvam
to our own team, split fifty-fifty so a handful of calls lands in both arms. Each lead is hashed to
A or B and logged before the phone rings: A rings on agent version one, B on version two, which
asks the quantity at most twice. Outcomes come back into the same engine, transcripts to the same
judge, and with a handful of calls the engine takes no look: it never decides early."

If the live calls did not run, skip this segment.

## One-shot and fallback

```bash
make demo ACTOR=kartik
```

`make demo` runs win, harm and inconclusive with a split report each, then the approval scenario,
and approves it as ACTOR (under a minute). Use it to rebuild the store, or as the fallback if the
dashboard is down: every look prints its z, boundaries and decision in the terminal, and the split
and truth reports land in `reports/`. Because `make demo` approves the approval run, run
`make simulate SCENARIO=approval` again afterwards if you want to click Approve live.

If `make live` was already run, running it again replaces the earlier harm run: the dashboard keeps
the old result for about 5 seconds, then the run starts again from day 1.


---

# Part 2: the loop on the platform (3:35 to 6:45)

## Before the clock starts

- The real run in [docs/runbook.md](runbook.md) (step 3) has finished: cycle 1 is at
  **PENDING_APPROVAL**. Do not approve it yet; the approval happens on camera.
- The platform is running with the go-ahead, so its buttons can run cycle 2 (whose replay calls are
  built from real buyers): `LOOP_ALLOW_REAL=1 make platform`. Open http://127.0.0.1:8600 in a second
  tab, light theme.
- **Approve is one-way.** On the real platform one click makes v2 production and opens cycle 2.
  Rehearse the click on the demo copy (below), where it lasts only until the tab reloads. To allow a
  retake on the real platform, take a snapshot first, with the platform stopped:

  ```bash
  tar czf ~/vani-before-approve.tgz abtest.db* data/derived/loop logs/decisions.jsonl
  ```

  To go back to it, stop the platform again, then
  `mv data/derived/loop ~/vani-loop-after && rm -f abtest.db-wal abtest.db-shm && tar xzf ~/vani-before-approve.tgz`.
- **Film Issues and Fixes before you approve.** Afterwards Issues opens on v2, which has no calls
  of its own yet.
- **Customer calls on camera.** These screens show real customer content: Calls, a call page, the
  rubric studio's **Try it on a call** card, the Audit page's calls to listen to first, the example
  calls on Issues, the example calls and "Cases that changed" on Fixes, and the ⌘K search. Film
  Calls, the call page and the rubric test from the demo copy (`python3 -m http.server -d
  src/abtest/ext/web 8611`, then http://127.0.0.1:8611/#/calls: invented calls, labelled as demo
  data). Keep the call lists on Audit, Issues and Fixes out of frame or blur them, and don't open
  ⌘K. The demo copy sits at a later cycle (cycle 3, v2 in production), so film only those three
  screens from it.
- Numbers below in angle brackets come from the run: read them off the screen.

## 3:35 to 3:55: where Prompt B comes from

**Screen:** the Loop home: the ring with v1 in production and cycle 1 at Prove → approval ("Cycle 1
needs your decision").

"Part one proved that a prompt change is safe to ship. But where does Prompt B come from? Today
someone writes it on a hunch. Vani Lab writes it from the calls themselves, and this ring is
the loop it runs."

## 3:55 to 4:30: listen and judge

**Do:** open **Calls**, then one call (from the demo copy). Point at the recording and the
transcript under it, the judge's notes beside the turns they quote, the scorecard, and one failed
grade's quote.

"Every call is transcribed and graded on IndiaMART's own QA checklist: the same eighteen columns
the QA team fills in by hand, plus three prompt rules, like never claiming to be human. Each failed
grade carries the quote that proves it, and the QA team's own grade sits beside it."

## 4:30 to 4:55: how far to trust the judge

**Do:** open **Audit**. Point at the agreement card and the per-check table ("Where the judge and
QA differ", worst first).

"We measured the judge against the QA team on <n> audited calls it was not tuned on. Check by
check, the table shows where it agrees and where it does not. On the overall verdict it does not
agree yet: the card says so, and the Docs say why: the auditors also see the lead record the bot
saved, and the judge cannot. The platform shows that instead of hiding it."

<n> is the "of N calls" on the agreement card. If the card says "Reliable", drop the sentence about
the overall verdict.

## 4:55 to 5:10: your own rubrics

**Do:** on the demo copy, open **Rubrics**, then **New rubric**, with its four fields (the name, the
question the judge answers, when it applies, when it is fatal) filled in before this segment. Pick a
call under **Try it on a call** and press **Grade**; do not save. The demo copy has no judge model
and matches phrases only, so add a fail phrase that occurs in the call (for sample-0001, "do sau
meter"), or the result is N/A.

"Teams add their own checks in plain English, test them on a call before saving, and the next cycle
grades every call with them."

## 5:10 to 5:50: root cause, fix, gate, replay

**Do:** open **Issues** and its **How ranking works** for the formula, then the top cause's **Open
the fix →** (the card that reads "Fix drafted (v2)"). Point at the priority, the redline, the gate's
checks and the model the gate card names (`typesafe/jev-router →` the model Jev routed to), and the
replay's before and after.

"Failures become named root causes, ranked by a published formula. The top one a prompt can fix is
<name>, in <n> calls. The fix is a minimal edit to the prompt, shown as a redline. A safety gate,
running on Jev, blocks any edit that adds a promise or weakens an honesty rule. Then the same
buyers are simulated against Prompt A and Prompt B: the failure falls from <x> to <y>."

If the gate card does not name `typesafe/jev-router`, say "a safety gate" without "running on Jev".

## 5:50 to 6:20: prove it on ten percent, then approve

**Do:** press **Review and decide** on the Loop home, or open **Experiments** and the **Cycle 1**
row under *From the loop* (`vani-loop-c1`). Point at the 90/10 split, the stated assumption for the
simulated traffic, and the z-path between its boundaries. Type your name in **Your name**, press
**Approve B**, and confirm **Approve B** in the dialog.

"Prompt B then gets ten percent of leads through the same engine as part one. The traffic is
simulated, and the screen says exactly what we assumed. It wins, and waits for a person. I
approve."

## 6:20 to 6:45: the loop starts again

**Do:** open **Prompts** (v2 is production), then the **Loop** home: cycle 2 has opened on v2
("Cycle 2: count v2's calls"). Press **Run until it needs you** and confirm: the dialog lists the
models and the spend so far, and the cycle runs in the background (about $2–3 by our estimate). End
on the **Docs** tab.

"Prompt B is now production, and the loop starts again on it: every call teaches the next prompt.
It is all documented in the Docs tab. We are team Hare and Tortoise. The hare ships a prompt change
on a hunch. The tortoise ships it on evidence."
