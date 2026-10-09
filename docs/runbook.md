# Runbook: the real-data run

**Status (10 Oct, 04:05 IST): blocked on the OpenRouter budget.** The key's organisation caps it at
about $20 a month, and the run hit that at $20.23 ("Budget limit exceeded (monthly limit). Contact
your org admin."). 113 of the 160 audited calls are already graded with the current checklist and
cached; cycle 1 is open and waiting at the judge stage.

## 1. Unblock the key (office, morning)

1. Ask the OpenRouter organisation admin to raise this key's monthly limit to at least **$30**, or
   put a personal key in `.env` as `OPENROUTER_API_KEY=...`.
2. If the key has an IP allowlist, add both addresses the office network leaves from:
   **122.161.49.159** and **121.241.109.212**.
3. Check it (costs a fraction of a cent):

   ```bash
   uv run python -c "from abtest.ext import llm; print(llm.chat('anthropic/claude-haiku-5.5', [{'role': 'user', 'content': 'Say ok'}], max_tokens=5, cache=False).text)"
   ```

## 2. Run it (about $4–8, 15–25 minutes)

```bash
uv run python -m abtest.ext.judge audit --source audited --yes-send-real-transcripts \
  && uv run python -m abtest.ext.judge agreement \
  && uv run python -m abtest.ext.loop advance --yes-send-real-transcripts
```

- **Audit:** grades the 47 audited calls not yet graded with the current checklist (the 113 already
  graded are cached and free).
- **Agreement:** recomputes how far the judge agrees with the QA team and the calibration
  (`reports/judge_agreement.md`).
- **Cycle 1:** continues from the judge stage: root causes, Prompt B, the safety gate, the replay
  test, then the 10% A/B on simulated traffic. It stops at **PENDING_APPROVAL**.

If it stops on the spend limit again, raise the limit and rerun the same command: every finished
model call is cached, so it resumes where it stopped.

## 3. Approve and keep the loop going

1. On the platform (`make platform`, http://127.0.0.1:8600), open **Experiments → vani-loop-c1**
   and press **Approve**, or run
   `uv run python -m abtest.ext.loop approve vani-loop-c1 --actor <your-name>`.
2. Prompt B becomes production (v2) and cycle 2 opens on it. Cycle 2 and later grade v2's replay
   calls: simulated conversations, but seeded with the real buyers' personas and lines, so they are
   call content under the OpenRouter approval even though the loop runs them without the
   `--yes-send-real-*` flag. Press **Advance the loop** on the home page for each cycle (about
   $2–3 a cycle).

## 4. Then

Ask Claude to fill the real numbers into the README, `docs/approach_note.md`, `skills.md` and the
sample outputs (aggregates only: no call text or call ids leave the machine).
