"""The generation ledger as the proposer sees it (PLAN_LOOP.md §L1.7, §L2.5, §L2.9).

`patch_hash` identifies a patch by its content, so a candidate that was already tried (and gated,
tested or rejected) is never proposed again verbatim. `tried_hashes` and `ledger_summary` read the
candidates `store_loop` holds for one loop; nothing here writes.
"""

import re
import sqlite3

from abtest.contracts import Patch
from abtest.loop.contracts import Candidate, sha256_json

PREVIEW_CHARS = 160   # how much of a patch's text a ledger record carries: enough to recognise it


def patch_hash(patch: Patch) -> str:
    """sha256 of the canonical JSON of the patch (sorted keys), via the shared `sha256_json`."""
    return sha256_json(patch.model_dump(mode="json"))


# A candidate is "tried" once it reached a verdict that does not depend on the production prompt
# it was tested against: it went to the live A/B, or the gate or the dogfood calls stopped it.
# A candidate that only lost a bot test (or failed a pre-check) against an older prompt is not:
# after a promotion the production prompt changed, so it is re-tested against the new one
# (PLAN_LOOP §L1.7 forbids retrying verbatim what was decided, not what was outranked).
TRIED_STATUSES = frozenset({"picked", "live", "promoted", "not_promoted", "gate_blocked", "dogfood_failed"})


def tried_hashes(conn: sqlite3.Connection, loop_id: str) -> set[str]:
    """Patch hashes of this loop's candidates that reached a prompt-independent verdict
    (TRIED_STATUSES): resubmitting one of those would waste the slot."""
    from abtest.loop import store_loop  # lazy: the ledger needs a store only when it is used
    return {c.patch_hash for c in store_loop.get_candidates(conn, loop_id) if c.status in TRIED_STATUSES}


def _order(c: Candidate) -> tuple[int, int, str]:
    """Generation, then the number in `g<gen>-c<i>` (so c10 sorts after c9), then the id."""
    m = re.search(r"(\d+)$", c.candidate_id)
    return c.generation, int(m.group(1)) if m else 0, c.candidate_id


def _preview(c: Candidate) -> str:
    d = c.patch.model_dump()
    text = d.get("replace") if d.get("mode") == "replace_text" else d.get("text")
    return " ".join(str(text or "").split())[:PREVIEW_CHARS]


def ledger_summary(conn: sqlite3.Connection, loop_id: str, max_items: int = 20) -> list[dict]:
    """Short records of past candidates for the proposer: generation, mode, targets, rationale,
    status, bot_test_score, live terminal state. The most recent `max_items`, oldest first.

    `live_terminal` is the terminal state of the generation that picked the candidate (None for a
    candidate that was not picked or whose generation is still running). `patch_preview` is the first
    160 characters of what the patch adds, so the proposer can see what was tried, not only why.
    """
    from abtest.loop import store_loop
    terminal = {g.pick: g.terminal for g in store_loop.get_generations(conn, loop_id) if g.pick}
    recent = sorted(store_loop.get_candidates(conn, loop_id), key=_order)[-max_items:] if max_items > 0 else []
    return [{"generation": c.generation, "candidate_id": c.candidate_id, "mode": c.patch.mode,
             "targets": list(c.targets), "rationale": c.rationale, "status": c.status,
             "bot_test_score": c.bot_test_score, "live_terminal": terminal.get(c.candidate_id),
             "patch_preview": _preview(c)}
            for c in recent]
