# Runbook: live calls through Sarvam

This puts the A/B split on real phone calls. Our router gives each lead A or B and logs the exposure. The
call then goes out through Sarvam with that arm's committed agent version: version 1 holds Prompt A, version 2
holds Prompt B, and B asks the quantity at most twice instead of three times. The outcome comes back into the
A/B engine as a calls row, and the transcript comes back to the loop's judge. The code is
`src/abtest/ext/sarvam_live.py`; steps 1 to 4 below are `make calls ARGS="..."`, step 5 is `make judge` and step 6
`make platform`.

## Rules

- **Only consenting team members, never real buyers.** The code dials only the numbers in
  `data/live/team_phones.yaml`. Ask each person first, and add only the people who said yes.
- **Daytime only.** `call` refuses outside 09:00 to 21:00 IST, and checks the clock again before each call:
  a run that reaches 21:00 stops there. Keep test calls to office hours: one reading of TRAI's default time
  bands starts commercial calls at 10:00.
- **Cost.** Sarvam charges each call to the workspace wallet. The public docs give no per-minute price; the
  rate is under Settings → Plans & pricing. The rented number is billed every 30 days. Keep `--max` small; it
  defaults to 10 calls a run. Grading a live call with the judge costs about as much as grading one audited
  call (OpenRouter, capped by `LLM_BUDGET_USD`).
- **Privacy.** A team member's phone number is stored only in the allowlist. It never goes into transcripts,
  logs, reports or the store, and it is printed masked (`+91******3210`). The rented number lives only in
  `.env`. Transcripts, the raw Sarvam bodies and the split report stay under `data/derived/loop/live/` and
  `data/derived/loop/transcripts/`; the record of each call attempt is in the store (`abtest.db`, table
  `live_attempts`). All of these are git-ignored. Live transcripts are real call content, so the judge sends
  them to the model provider only with `--yes-send-real-transcripts`.

## Before the first call

1. **Build the agent** with two committed versions: see `docs/sarvam_agent_setup.md`. It declares the
   five input variables the calls send (`lead_id`, `ab_variant`, `buyer_name`, `product_name`, `city`) and
   the `disposition` output variable.
2. **Put these in `.env`** (git-ignored):

   | Name | What |
   | --- | --- |
   | `SARVAM_VA_API_KEY` | a Voice Agents API key (Settings → API Key). If it is unset, the code uses `SARVAM_API_KEY`, which may be a Model-API key that this API refuses |
   | `SARVAM_ORG_ID`, `SARVAM_WORKSPACE_ID` | from the dashboard URL or Settings |
   | `SARVAM_APP_ID` | the agent ID (··· → Copy agent ID) |
   | `SARVAM_VERSION_A`, `SARVAM_VERSION_B` | the two different committed version numbers that hold Prompt A and Prompt B (expect 1 and 2) |
   | `SARVAM_CONNECTION_ID` | the Sarvam Vobiz connection's ID (Deploy → Phone Numbers), not the number |
   | `SARVAM_AGENT_NUMBER` | the rented number listed under that connection, in E.164 (`+91XXXXXXXXXX`) |

   A missing name stops the command with a message that names it.
3. **Write the allowlist** `data/live/team_phones.yaml`. It is under `data/`, so git ignores it. Each phone
   is an E.164 string **in quotes** (`+` and 10 to 15 digits); without quotes YAML reads it as a number, and
   the code refuses it.

   ```yaml
   # Consenting team members only. Never real buyers.
   - {lead_id: team-01, phone: "+91XXXXXXXXXX", name: Asha, product: Office chair, city: Noida}
   - {lead_id: team-02, phone: "+91XXXXXXXXXX", name: Ravi, product: Steel pipes, city: Pune}
   ```

   `lead_id` is the unit the router hashes, so keep it stable. It is stored and printed as is, so it must not
   be a phone number; the code refuses one that looks like a number. `name`, `product` and `city` fill the
   agent's `buyer_name`, `product_name` and `city`. A broken file is reported by line and column only, so the
   message never shows a number.

## Commands, in order

1. **Start the experiment.** This needs no network, but it reads `.env`.

   ```bash
   make calls ARGS="start --split 0.5"        # 50/50 is a DEMO split: a handful of calls lands in both arms
   make calls ARGS="start"                    # or 90/10, the template's split
   ```

   It builds `vani-live-c1` from the buyer template (`bl_success` primary, `abrupt_disconnect` guardrail,
   approval promotion). A = `prompts/live/vani_demo_A.txt` and B = `prompts/live/vani_demo_B.txt`, the texts
   the two agent versions hold. `maturation_days` is 0, because a live call's outcome is known when the call
   ends; the template's 1 day would keep tonight's calls out of every look until tomorrow. The experiment
   gets its own salt and is frozen, then set RUNNING. It also freezes the agent it calls: the org, the
   workspace, the agent ID, and which version is A and which is B. `call` refuses if `.env` names another
   agent or version later, and `fetch` always reads the frozen agent. The config is written to
   `data/derived/loop/live/vani-live-c1.yaml`. An id can't be reused: for a fresh run, use
   `--id vani-live-c2` (on this and on every later command). Every command takes only `vani-live-` ids, so a
   typo can never put real calls into a simulated experiment.

2. **Place the calls** (the user's go-ahead; only the full flag counts, not a prefix like `--yes`):

   ```bash
   make calls ARGS="call --yes-place-real-calls --max 4"
   ```

   For each allowlisted lead not called yet, it routes the lead (the exposure is logged first) and claims the
   call in `live_attempts` before it POSTs one Instant Outbound call with that arm's version. The record
   holds the attempt id, lead, arm, prompt hash and IST time, with no number. Sarvam has no idempotency key,
   so the claim is what keeps each lead to one call per experiment, even if the run is killed mid-call or
   two runs overlap. It refuses if a prompt file changed since `start`. What the POST's answer means:
   - **200** with an attempt id: the call is placed.
   - **422** (Sarvam's documented validation error), **401 or 403** (the key was refused; the run stops), or
     no connection at all (the run stops): Sarvam refused the request, so no phone rang. The next `call` run
     tries that lead again, and the refused try stays on record.
   - **Anything else** (a timeout, a 5xx, another status, or a run killed during the POST): it may have rung.
     The lead is never dialled again in this experiment, and the run stops there, so an outage costs one
     lead, not the allowlist. `status` lists it under "may have rung". The next `fetch` looks for its call in
     Sarvam's attempts list, by its `lead_id` and arm in the agent variables, and records it when exactly one
     matches. Never delete its record: it is the only trace of a call that may have happened.

   If `fetch` finds no call and **Monitor → Agent Analytics → Call Logs** shows none either, the person was
   not called. To call them anyway, give their allowlist entry a new `lead_id` (for example `team-01` →
   `team-01b`). That is a new lead to the router; the old one stays on record, assigned with no outcome,
   which is what happened.

3. **Bring the results back** once the calls have ended. Run it again until nothing is "still ringing or
   talking" or provisional; running it again changes nothing that is final.

   ```bash
   make calls ARGS=fetch
   ```

   It reads Sarvam's attempts list for the window since the experiment started, and matches our attempt ids.
   An attempt is recorded once it has ended: a connected call needs an end time (a duration alone proves
   nothing), and a status the docs don't list (they list none for this API) waits until it comes with an end
   time or a failure reason. Each waiting attempt is printed with Sarvam's own status. Each ended attempt
   becomes one calls row, with its time in IST, between the exposure and now:
   - `answered` is set when the call connected.
   - The disposition is the agent's `disposition` when it is `BL Enriched` or `BL Approved`. Otherwise it is
     `Abruptly Disconnected Call` when the buyer hung up within 20 s, else `No Enrichment Scope`. A call that
     did not connect gets `Not Connected`. The line shows Sarvam's `ended_by`: the buyer hung up when it
     holds `user`, `customer`, `human` or `buyer`. Check the first real call's value.

   The agent extracts `disposition` after the call ends, and the docs don't say how long that takes. So a
   connected call without one is recorded at once, but as **provisional**: each `fetch` corrects it when the
   disposition comes, and it is final 30 minutes after the call ended. `look` takes no look while an outcome
   is provisional. For a connected call, `fetch` also saves the transcript with source `live` and the arm,
   and keeps Sarvam's raw body, phone numbers removed, in `data/derived/loop/live/raw/`.

4. **See the split:**

   ```bash
   make calls ARGS=status     # per arm: assigned, called, outcomes back, connected, dispositions
   make calls ARGS=look       # the engine's look if one is due (a handful of calls: none), plus data/derived/loop/live/split_vani-live-c1.md
   ```

   `look` runs the simulation's look chain at the current time. It takes a look only when the plan says so:
   at the next planned information fraction, or at the 14-day window's end, and only once no outcome is
   provisional. The first planned look is at t = 0.20, about 153 matured B leads at 50/50 (306 leads in all),
   or 87 at 90/10 (865 in all). With a handful of calls it prints **no look due**, with t, the leads per arm,
   and the split report: configured against achieved share and the SRM test. That is by design: the engine
   never looks early, so a few calls cannot stop the test or crown B. The split report counts real calls, so
   it is written under `data/` (git-ignored), not `reports/`.

5. **Grade the calls** (the user's go-ahead: transcripts go to the model provider):

   ```bash
   make judge SOURCE=live ARGS=--yes-send-real-transcripts
   ```

   Live transcripts carry the prompt version `live` (they ran an agent version, not a registry version), so
   their audits go to `data/derived/loop/audits/live/`, and the loop never takes them for v1's calls.

6. **Show it**: `make platform`, then http://127.0.0.1:8600. **Experiments** lists `vani-live-c1` under *Live
   calls*. Its page says "Live experiment · real calls through Sarvam", shows B's one-line change as a redline,
   which agent version each arm calls, and a **Calls per arm** card (the same counts as `status`: leads
   assigned, calls placed and connected, outcomes). **Calls**, with the **Live** filter, lists the live
   transcripts with the judge's grade once step 5 ran; open one to see its arm and agent version.

   The arm a lead gets is fixed by a hash of the experiment id and the lead id, so the same allowlist splits
   the same way every time: at `vani-live-c1`, `team-01` gets B and `team-02` gets A. With a handful of leads an
   uneven split is chance; renaming leads to even it out would be choosing the arms by hand.

## When something is off

- **`HTTP 401/403` on call**: the key is a Model-API key or wrong. Set `SARVAM_VA_API_KEY`.
- **"may have rung"** in `status`: see step 2. Run `fetch` first; it records the call if Sarvam lists it.
- **`failed (exotel: Phone number is registered under TRAI NDNC)`** or similar: the provider blocked that
  number. The call is recorded as `Not Connected`.
- **"transcript not ready"**: run `fetch` later; the outcome row is already in. The docs don't say whether the
  `/` in an interaction id is sent raw or as `%2F`, so `fetch` tries both. If it never comes, compare the
  attempt in Call Logs.
- **"no turns found in Sarvam's transcript"**: Sarvam documents no transcript body, and this one is a shape
  the parser does not know. The raw body is in `data/derived/loop/live/raw/<attempt id>.json`; the parser is
  `_turns` in `sarvam_live.py`.
- **Every connected call stays provisional, then ends `No Enrichment Scope`** (or `Abruptly Disconnected Call`
  when the buyer hung up within 20 s): check in Call Logs that the
  agent fills `disposition`, and that the attempts API returns it in `agent_variables`. The docs don't say
  that it does.
