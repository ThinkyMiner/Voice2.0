# Demo script: 3 minutes

The three required scenarios (B is worse and stopped early, B wins and is promoted, inconclusive),
then the approval gate and the split evidence. The spoken lines (in quotes) are about 440 words.

The figures quoted are from the pinned demo seeds in `configs/scenarios/` (harm 1103, win 1101,
inconclusive 1104, and approval 1101 once it is re-pinned, see below). If a seed is re-pinned, read
the numbers off the screen instead; the sentence stays the same.

`ACTOR` is one word, no spaces (`ACTOR=kartik`, not `ACTOR="Kartik Sehgal"`).

## Before the clock starts (about 2 minutes, off stage)

The `data/` folder must hold the provided files (see the README's Setup section).

**Approval seed.** Today `configs/scenarios/approval.yaml` pins the placeholder `seed: 1102`, which
ends BLOCKED_GUARDRAIL, so the approval segment cannot be shown. Change that line to `seed: 1101`
first (verified: PENDING_APPROVAL at look 4, day 15). Skip this once the repo has the re-pin.

```bash
grep seed: configs/scenarios/approval.yaml    # must say: seed: 1101
```

Terminal 1: build the store with the batch scenarios, then serve the dashboard and leave it
running.

```bash
make setup
make simulate SCENARIO=win
make simulate SCENARIO=inconclusive
make simulate SCENARIO=approval
make split-report SCENARIO=win
make dashboard
```

Check the end of the `make simulate SCENARIO=approval` output: it must say
`final state PENDING_APPROVAL`. If it says anything else (BLOCKED_GUARDRAIL means the seed is still
1102), re-pin as above and rerun `make simulate SCENARIO=approval`, or skip the approval segment and
give its 25 seconds to the split evidence.

Optional, only if you may open the **Validation** page: `make validate` (under a minute). It writes
`reports/validation.md` and then ends with `make: *** [validate] Error 1`, because the power check
currently misses its target (see the README's Validation section). The page is fine either way.

Open http://localhost:8501. The sidebar has the **Experiment** picker and the auto-refresh toggle
(leave it on); the page shows the experiments table, with its **Business impact** column, then the
picked experiment's detail.

Terminal 2: type this now, but do not press Enter until 0:05.

```bash
make live SCENARIO=harm SPEED=3
```

It takes about 35 to 45 seconds: a few seconds to build the bootstrap pool, then 3 seconds per
simulated day. Look 1 (day 7) prints about 25 to 35 seconds after Enter and the stop (day 9) about 7
seconds later; the dashboard re-reads the store every 3 seconds, so allow up to 3 more on screen.
Pressing Enter at 0:05 puts both on screen while you talk about them.

## 0:00 to 0:35: the problem

**Screen:** the experiments list, under the banner saying the traffic is simulated.

**Do:** at about 0:05, while you talk, press Enter in terminal 2.

"Today a VANI prompt change goes live to every seller on someone's judgement. This platform gives
the new prompt ten percent of leads, tracks Meeting Fixed per answered lead, with call duration as a
guardrail, and lets the evidence decide. All traffic here is replayed from six months of real
calls, with a known effect injected that the engine never sees. The smallest change it is sized
to catch, fifteen percent, is worth about a hundred and seventy meetings a week."

## 0:35 to 1:10: B is worse, stopped early (live)

**Do:** pick `vani-meeting-v1-harm` in the sidebar (the run you started at 0:05 with
`make live SCENARIO=harm SPEED=3`). Wait for look 1 to appear before saying "Day seven", and for
look 2 before "Day nine".

**Point at:** the z-path chart ("Sequential test: z-path across looks"). The crimson harm boundary
sits near z = −2.2 at every look. Look 1 (day 7) lands at z = −2.01, just inside it. Look 2 (day 9)
lands at z = −2.90, past −2.27. The state badge flips to STOPPED HARM, the headline above the
charts reads "B stopped on day 9, protecting ≈ 282 Meeting Fixed a week at full rollout", and the
decision log gets its reason row.

"This variant secretly loses thirty percent of meetings. Watch the z-path. Day seven: B is behind,
but not past the harm line yet, so the engine keeps going. Day nine: it crosses. The engine stops B
and sends all traffic back to A. At full rollout, B would have cost about two hundred and eighty
Meeting Fixed a week. We caught it in nine days, and only one lead in ten ever heard it. The harm
line is built to stop fast."

## 1:10 to 1:45: B wins, promoted automatically

**Do:** pick `vani-meeting-v1-win`.

**Point at:** the gold win boundary, which starts near z = 4.7 at the first look and falls towards
2. At look 3 the z-path touches it (3.18 against 3.12), but the guardrail chart ("Guardrail:
repeated CI vs margin") shows the duration RCI still reaching past the +10% margin, so the run
continues. At look 4 (day 15) z = 3.30 clears 2.71 and the RCI sits inside the margin: PROMOTED,
with the headline "B promoted on day 15: ≈ +239 Meeting Fixed a week at full rollout". Then open
the collapsed "Ground truth (engine never sees this)" panel at the bottom.

"Here B tells the bot to offer two concrete meeting slots. The win line starts near five standard
errors, so a lucky first week can never promote anything. At look three B touches the line, but the
duration guardrail is not proven safe yet, so the engine waits. At look four, on day fifteen, both
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

**Do:** pick `vani-meeting-v1-approval` (badge PENDING APPROVAL), type your name (one word) in the
**Approver** box and click **Approve: promote B**. Or, in terminal 2:

```bash
make approve EXP=vani-meeting-v1-approval ACTOR=kartik
```

**Point at:** the "Decision needed" box with what approving is worth a week (≈ +239 at seed 1101,
the same draw as the win run), then the badge going from PENDING APPROVAL to PROMOTED and the new
decision-log row with your name as the actor.

"In approval mode the same kind of win waits for a person. The headline shows what approving is
worth, about two hundred and forty meetings a week, and B keeps its ten percent until someone signs
off. I approve, and the log records my name, the reason and that value. Every decision, automatic
or human, lands in an append-only log with the config and prompt hashes."

## 2:40 to 3:00: the split evidence

**Do:** pick `vani-meeting-v1-win` again and scroll to the **Traffic split** panel (the same
numbers are in `reports/split_win.md`, after the `make split-report SCENARIO=win` in the prep; the
committed copy of that file is older and differs slightly).

**Point at:** configured 10.00% against achieved 9.87% with its 95% CI [9.41%, 10.35%], SRM
p = 0.59, leads in both arms 0, re-hash PASS.

"And the split is real: ten percent configured, nine point eight seven achieved, inside its
confidence interval. No lead ever heard both prompts. If B's exposures ever stop being logged, the
SRM check halts the experiment before anyone trusts a number. One command,
make demo, rebuilds all of this."

## One-shot and fallback

```bash
make demo ACTOR=kartik
```

`make demo` runs win, harm and inconclusive with a split report each, then the approval scenario,
and approves it as ACTOR (under a minute). Until the approval seed is re-pinned to 1101 it stops at
that last step with "only PENDING_APPROVAL can be approved or rejected" and exits 2; the first three
scenarios and their reports are still built. Use it to rebuild the store, or as the fallback if the
dashboard is down: every look prints its z, boundaries and decision in the terminal, and the split
and truth reports land in `reports/`. Because `make demo` approves the approval run, run
`make simulate SCENARIO=approval` again afterwards if you want to click Approve live.

If `make live` was already run, running it again replaces the earlier harm run, so the dashboard
shows the experiment disappear for a moment and come back from day 1.

