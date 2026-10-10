# Sarvam agent setup: Vani Lab Buyer

One agent with two committed versions. **Version 1 runs Prompt A** (`prompts/live/vani_demo_A.txt`), **version 2
runs Prompt B** (`prompts/live/vani_demo_B.txt`). B differs from A in one line only: the quantity ask limit,
`quantity = 3` becomes `quantity = 2`. Our router gives each lead A or B and places the call with that
version's number. Each step cites the Sarvam docs page it follows. **Docs silent** marks what the docs don't say.

## Build the agent (dashboard, about 20 minutes)

1. Open **Build → Agents** (https://indus.sarvam.ai/samvaad/build/my-agents) and click **Create from Scratch**.
   You land on the Canvas, which has the tabs Instruction, Variables, Tools, Settings and Tests.
   ([Quickstart](https://docs.sarvam.ai/conversations/quickstart),
   [Navigating the build interface](https://docs.sarvam.ai/conversations/build/navigating-workspace))
2. Name it: top-bar **···** → **Edit name** → `Vani Lab Buyer`.
   ([Navigating › Agent menu](https://docs.sarvam.ai/conversations/build/navigating-workspace#agent-menu))
3. **Variables** tab, **Input variables** → **Add**, five times. Do this before the Instructions, so the chips
   exist when you need them. Leave the defaults blank (**Docs silent** on whether a blank default is
   allowed; if one is required, use the step 8 test values for the first three and `test` for the other two).
   Real calls always send all five values.

   | Name | Send to LLM |
   | --- | --- |
   | `buyer_name`, `product_name`, `city` | on (the default) |
   | `lead_id`, `ab_variant` | **off**: tracking only, so the bot never sees which arm it is in |

   ([Variables & Personalization › Controlling context sent to the LLM](https://docs.sarvam.ai/conversations/build/variables-personalization))
4. Still on **Variables**, **Output variables** → **Add** `disposition`: data type **Enum** with exactly these
   five values: `BL Enriched`, `BL Approved`, `BL Generated`, `No Enrichment Scope`, `Language Barrier`.
   Paste this as the **Extraction prompt**:

   ```text
   Read the whole call and pick the one value that describes how it ended. If more than one fits, pick the first that fits in this order.
   Language Barrier: the call could not go on because the buyer and the agent had no language in common.
   No Enrichment Scope: the buyer had no requirement, had already bought, or declined to go ahead or to continue the call.
   BL Enriched: the buyer confirmed the requirement and gave the product, a quantity and at least one specification.
   BL Approved: the buyer confirmed the requirement and agreed to be connected to sellers (to have it shared with sellers).
   BL Generated: the requirement was noted but not fully confirmed.
   ```

   The docs show one extraction prompt per variable. If the Enum editor also has a box per value, put each
   value's line there. You can skip **Successful when**: our A/B engine scores the calls itself (the docs
   don't say it is required).
   ([Variables & Personalization › Output variables](https://docs.sarvam.ai/conversations/build/variables-personalization))
5. **Instruction** tab → **Greeting**. It is played as written (the LLM doesn't generate it), so it needs a
   translation for each language: the **Translations** button drafts one, and you paste the English line below
   over it. Replace `{buyer_name}` and `{product_name}` with chips: delete the text, type `@`, pick the variable.
   - Hindi: `नमस्ते {buyer_name} जी, मैं IndiaMART की virtual assistant वाणी बोल रही हूँ। आपने हाल ही में {product_name} के लिए enquiry की थी। क्या आपको अभी भी इसकी requirement है?`
   - English (its translation): `Hello {buyer_name} ji, this is Vani, IndiaMART's virtual assistant. You recently made an enquiry for {product_name}. Do you still need it?`

   ([Navigating › Greeting](https://docs.sarvam.ai/conversations/build/navigating-workspace#greeting))
6. **Instructions** (below the greeting): paste all of `prompts/live/vani_demo_A.txt` (on a Mac,
   `pbcopy < prompts/live/vani_demo_A.txt` copies it). The three placeholders
   `{buyer_name}`, `{product_name}` and `{city}` each appear once, under ABOUT THIS CALL. Turn each into a
   chip the same way (delete, `@`, pick). Do not mention `lead_id` or `ab_variant` anywhere in the prompt,
   because any variable the prompt references is kept in the bot's context.
   ([Navigating › Instructions](https://docs.sarvam.ai/conversations/build/navigating-workspace#instructions))
7. **Settings** tab:
   - **Language personalisation**: Starting language **Hindi**, **Switch language during call** on,
     Languages allowed **Hindi** and **English**.
     ([Conversation settings › Language personalisation](https://docs.sarvam.ai/conversations/build/conversation-settings#language-personalisation))
   - **Voice**: model **Bulbul V4**, voice **Simran - Hinglish Support Agent** (any Hinglish voice works).
     ([Speakers & voice](https://docs.sarvam.ai/conversations/build/voice-language))
   - **Max call length**: **5** minutes. Leave everything else at its default.
     ([Conversation settings › Max call length](https://docs.sarvam.ai/conversations/build/conversation-settings#max-call-length))
8. **Test in the browser before committing**: click **test agent** → **Voice** → **Setup**. Set
   `buyer_name` = Rahul, `product_name` = Office Chair, `city` = Noida, then **Start Call**. It should
   say it is a virtual assistant when asked, ask for the number of chairs with a unit, ask about one to three
   specifications, confirm Noida, recap and close. Ask "price kya hai?": it must not quote a price. Fix the
   draft now if needed, because a commit is permanent. No phone call is needed or placed.
   ([Navigating › Testing the agent](https://docs.sarvam.ai/conversations/build/navigating-workspace#testing-the-agent),
   [MCP: "commit freezes a version for good"](https://docs.sarvam.ai/conversations/mcp))
9. **Commit version 1 (Prompt A)**: open the version selector next to the agent name (it reads like
   `v1 · Draft`) → **Commit agent**. Write down the committed version number (expect **1**): it is
   `SARVAM_VERSION_A`.
   ([Navigating › Versions and committing](https://docs.sarvam.ai/conversations/build/navigating-workspace#versions-and-committing),
   [Agent versioning](https://docs.sarvam.ai/conversations/build/agent/versioning))
10. **Make version 2 (Prompt B)**: in Instructions, find the line that starts `Total ask limits for this call:` and
    change `quantity = 3` to `quantity = 2`. Change nothing else, so the chips stay. That one edit gives the
    text of `prompts/live/vani_demo_B.txt`. **Commit agent** again and write down the number (expect **2**): it
    is `SARVAM_VERSION_B`, and it must differ from `SARVAM_VERSION_A`. Run one short Voice test.
11. **Copy the agent ID**: **···** → **Copy agent ID** ("for the API and SDK"). It is the **Sarvam agent ID** for
    the hackathon submission and `SARVAM_APP_ID`. The docs' example looks like `Conversatio-4889d972-cc49`.
    ([Navigating › Agent menu](https://docs.sarvam.ai/conversations/build/navigating-workspace#agent-menu))

## Copy the IDs into `.env` (git-ignored)

12. `SARVAM_ORG_ID` and `SARVAM_WORKSPACE_ID`: "Copy these identifiers from your dashboard URL, or from
    Settings." ([API introduction](https://docs.sarvam.ai/conversations/api/introduction)). **Docs silent** on
    which part of the URL holds them. MoEngage's Sarvam guide (a third party) shows both in Settings next to
    the API keys table, as UUIDs like `019f3b1a-9e97-707b-…`
    ([MoEngage](https://moengage.com/docs/partner-guide/messaging-channels/voice/sarvam-conversations)).
13. `SARVAM_CONNECTION_ID`: **Deploy → Phone Numbers** → the **Sarvam Vobiz** connection → **View details**.
    **Docs silent** on where the ID is shown. Sarvam's docs only say a rented number's connection appears as
    "Sarvam Vobiz" ([Rent from Sarvam](https://docs.sarvam.ai/conversations/deploy/telephony/rent-from-sarvam)),
    and MoEngage says the ID is "on the telephony connection" under Deploy > Phone Numbers. It is an ID like
    the docs' `Exotel-Sarv-0f9a37e1-adf6`, not the phone number. The rented number is listed under that
    connection: it is `SARVAM_AGENT_NUMBER`, in E.164 (`+91` and 10 digits). It stays in `.env`, out of git.
14. `SARVAM_VA_API_KEY`: **Settings → API Key** → create a key. If your dashboard also has **Deploy with Code →
    Generate API key**, that button is not in the docs. Calls send the key in the `X-API-Key` header. The
    Model APIs (api.sarvam.ai) use a different header, `api-subscription-key`, so the `SARVAM_API_KEY`
    already in `.env` may not work here. That is why this key gets its own name; the code prefers it.
    ([API Key](https://docs.sarvam.ai/conversations/settings/api-key),
    [API introduction](https://docs.sarvam.ai/conversations/api/introduction),
    [Model API authentication](https://docs.sarvam.ai/api-reference/authentication))

```bash
SARVAM_VA_API_KEY=...
SARVAM_ORG_ID=...
SARVAM_WORKSPACE_ID=...
SARVAM_APP_ID=...
SARVAM_VERSION_A=1
SARVAM_VERSION_B=2
SARVAM_CONNECTION_ID=...
SARVAM_AGENT_NUMBER=+91XXXXXXXXXX
```

Optional check, from the repo root. It is read-only, places no call and prints only the HTTP status.
**200** means the key, the org, the workspace and the agent ID all work. The docs don't say what a wrong
key returns here.

```bash
set -a; . ./.env; set +a
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Key: $SARVAM_VA_API_KEY" \
  "https://apps.sarvam.ai/api/analytics/v1/$SARVAM_ORG_ID/$SARVAM_WORKSPACE_ID/$SARVAM_APP_ID/attempts?start_datetime=$(date -u +%Y-%m-%dT00:00:00Z)&end_datetime=$(date -u +%Y-%m-%dT23:59:59Z)&limit=1"
```

([Get attempts](https://docs.sarvam.ai/conversations/api/analytics/attempts))

## Where the docs are silent

- Whether the `vN` in the version selector is the integer the API's `app_version` takes. It very likely is;
  use the number shown on each committed entry.
- Which version the test panel runs (the draft or the selected one). That is why step 8 tests before the commit.
- Whether input variables can have blank defaults, and whether an Enum value can carry its own description.
- Whether `disposition` comes back in the analytics API's `agent_variables`. The docs only say every agent
  variable surfaces as the call's final variables in Agent Analytics. Check the first real call in
  **Monitor → Agent Analytics → Call Logs**.
- How soon after a call the output variable, the transcript and the recording are available.
- Any length limit for the Instructions. Prompt A is about 11,000 characters; the original is 159,000.
- Whether instant outbound rejects agent-variable keys the agent doesn't declare. The docs state that rule
  only for campaign cohorts, so declare all five as in step 3.
- That the hackathon form's "Sarvam agent ID" means this ID. The docs only say the ID is "for the API and SDK".
