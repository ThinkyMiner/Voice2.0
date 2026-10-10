"""Bot tests: replay simulated buyers against the base prompt and each candidate, then rank
(PLAN_LOOP.md §L2.6, §L7.2, §L9).

Every persona card is replayed against the base and against every candidate that passed the
pre-checks and the gate (`status == "proposed"`), text only, and each conversation is scored with the
rubric. The comparison is paired on the cards: a candidate's score is the mean over cards of
`rubric_score(candidate) - rubric_score(base)`, with a bootstrap 95% CI that resamples cards.

The score ranks candidates; it does not prove them. A simulated buyer is a model reading a
transcript, and the pick is chosen on the same rubric that measures it, so the score is optimistic
(§L2.6). A candidate is **eligible** when the CI's lower bound exceeds `bot_tests.min_gap` and none
of its cards failed a hard criterion. The pick is the eligible candidate with the highest mean, unless
another eligible candidate's mean lies inside the pick's CI and its patch is shorter: then the
shortest such patch wins (§L9: inside the noise, prefer the smaller change).

With few cards the bootstrap is coarse. Three cards can produce only ten distinct resampled means,
so the CI is close to the range of the three paired deltas; it is a screen against a lucky card, not an
interval to quote. The CI has no spread at all with one card.
"""

import hashlib
import logging
import sqlite3
from collections.abc import Sequence

import numpy as np

from abtest.loop import buyer_sim, render
from abtest.loop.contracts import (BotTestResult, Candidate, LoopConfig, PersonaCard, Prices, Provider,
                                   Rubric, RubricScore)

log = logging.getLogger(__name__)

BASE_TAG = "base"
CI_PERCENTILES = (2.5, 97.5)

# Token model behind `estimate_cost` (PLAN_LOOP §L2.6): a bot turn reads the whole rendered prompt
# plus the conversation so far; the simulated buyer and the judge read short texts. Output counts the
# reasoning tokens that sarvam-105b spends before it answers (§L9: roughly 800-1,800 per small call).
HISTORY_TOKENS = 400           # conversation so far, added to a bot turn's input
BUYER_IN_TOKENS = 800          # persona prompt + conversation, per simulated buyer turn
BOT_OUT_TOKENS = 600
BUYER_OUT_TOKENS = 400
JUDGE_IN_TOKENS = 2000         # rubric + transcript
JUDGE_OUT_TOKENS = 1500


def estimate_cost(n_cards: int, n_prompts: int, max_turns: int, prompt_tokens: int, prices: Prices) -> float:
    """INR for `n_cards` x `n_prompts` conversations of `max_turns` bot turns each, plus one judge call
    per conversation: cards x prompts x (turns x (prompt + turn tokens) + judge tokens), priced from
    `prices` at the dearest listed model (an estimate must not come in under the real cost). `n_prompts`
    counts the base. The estimate comes from fixed token constants (module top), so it is a planning
    figure, not a measurement."""
    conversations = n_cards * n_prompts
    tokens_in = conversations * (max_turns * (prompt_tokens + HISTORY_TOKENS + BUYER_IN_TOKENS) + JUDGE_IN_TOKENS)
    tokens_out = conversations * (max_turns * (BOT_OUT_TOKENS + BUYER_OUT_TOKENS) + JUDGE_OUT_TOKENS)
    price = max(prices.llm.values(),
                key=lambda p: p["input_per_million_tokens"] + p["output_per_million_tokens"])
    return (tokens_in / 1e6 * price["input_per_million_tokens"]
            + tokens_out / 1e6 * price["output_per_million_tokens"])


def _bootstrap_ci(deltas: np.ndarray, resamples: int, seed: int) -> tuple[float, float]:
    """Percentile CI of the mean of `deltas`, resampling the cards with replacement. The generator is
    seeded afresh per call, so a candidate's CI depends on its own deltas and the seed only."""
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(deltas), size=(resamples, len(deltas)))
    lo, hi = np.percentile(deltas[idx].mean(axis=1), CI_PERCENTILES)
    return float(lo), float(hi)


def _pick(rows: dict[str, dict], sizes: dict[str, int]) -> tuple[str | None, str | None]:
    """(pick, displaced): the best eligible candidate by mean, then the shortest eligible patch whose
    mean lies inside that candidate's CI if it is shorter. `displaced` is the best one when replaced."""
    eligible = [cid for cid, r in rows.items() if r["eligible"]]
    if not eligible:
        return None, None
    best = min(eligible, key=lambda cid: (-rows[cid]["mean"], sizes[cid], cid))
    lo, hi = rows[best]["ci"]
    inside = [cid for cid in eligible if cid != best and lo <= rows[cid]["mean"] <= hi
              and sizes[cid] < sizes[best]]
    if not inside:
        return best, None
    return min(inside, key=lambda cid: (sizes[cid], -rows[cid]["mean"], cid)), best


def run_bot_tests(base_prompt: str, candidates: Sequence[Candidate], cards: Sequence[PersonaCard],
                  rubric: Rubric, provider: Provider, cfg: LoopConfig, generation: int,
                  conn: sqlite3.Connection | None = None) -> BotTestResult:
    """Replays every card against the base and each eligible candidate; scores every transcript
    (scorer.score_calls); stores transcripts and scores when conn is given (prompt_hash set);
    paired deltas per card; paired bootstrap CI (cfg.bot_tests.bootstrap, seed); pick rule of
    PLAN_LOOP §L2.6 with the eligibility fix (§L9).

    Replayed: the base plus every candidate with status `proposed` and passing pre-checks; others are
    not touched and not returned. With no candidate (the observe step) only the base is replayed and
    `pick` is None. Each conversation is stored and scored as soon as it is generated, so a
    SpendCapExceeded part-way leaves the finished conversations in the store and the provider's cache
    (a rerun repeats none of them). `call_id` is `g{generation}-{tag}-{card_id}`, with tag `base` or
    the candidate id; the score's `lead_id` is the card id and its `prompt_hash` the sha256 of the
    (patched) prompt text.
    """
    from abtest.loop.scorer import score_calls          # another module of the loop: imported on use

    if not cards:
        raise ValueError("bot tests need at least one persona card")
    bt = cfg.bot_tests
    replay = [c for c in candidates if c.status == "proposed" and all(c.prechecks.values())]
    prompts = [(BASE_TAG, base_prompt)] + [(c.candidate_id, render.apply_patch(base_prompt, c.patch))
                                           for c in replay]
    store = None
    if conn is not None:
        from abtest.loop import store_loop as store

    scores: dict[str, dict[str, RubricScore]] = {}       # tag -> card_id -> score
    for tag, text in prompts:
        prompt_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        scores[tag] = {}
        for card in cards:
            transcript = buyer_sim.converse(text, card, provider, bt.max_turns, f"g{generation}-{tag}-{card.card_id}")
            log.info("bot tests g%d: %s on card %s, %d turns", generation, tag, card.card_id, len(transcript.turns))
            if store:
                store.put_transcript(conn, transcript)
            for s in score_calls([transcript], rubric, provider):   # none if there was nothing to judge
                scores[tag][card.card_id] = s
                if store:
                    store.put_score(conn, s, lead_id=card.card_id, prompt_hash=prompt_hash)

    base = scores[BASE_TAG]
    if not base:
        raise RuntimeError("no base-prompt conversation could be scored (every transcript had fewer than "
                           "two turns); bot tests need a base score to compare against")
    base_score = float(np.mean([s.rubric_score for s in base.values()]))
    base_risk = float(np.mean([s.rubric_risk for s in base.values()]))
    hard_ids = {c.id for c in rubric.criteria if c.hard}

    rows: dict[str, dict] = {}
    for c in replay:
        mine = scores[c.candidate_id]
        paired = [k for k in (card.card_id for card in cards) if k in base and k in mine]
        hard_fail = any(hard_ids & set(s.failed) for s in mine.values())
        if not paired:
            rows[c.candidate_id] = {"mean": None, "ci": None, "risk": 0.0, "hard_fail": hard_fail,
                                    "eligible": False, "n": 0}
            continue
        d_score = np.array([mine[k].rubric_score - base[k].rubric_score for k in paired])
        d_risk = np.array([mine[k].rubric_risk - base[k].rubric_risk for k in paired])
        ci = _bootstrap_ci(d_score, bt.bootstrap, bt.seed)
        rows[c.candidate_id] = {"mean": float(d_score.mean()), "ci": ci, "risk": float(d_risk.mean()),
                                "hard_fail": hard_fail, "n": len(paired),
                                "eligible": ci[0] > bt.min_gap and not hard_fail}

    sizes = {c.candidate_id: render.patch_chars(c.patch) for c in replay}
    scored_rows = {cid: r for cid, r in rows.items() if r["mean"] is not None}
    pick, displaced = _pick(scored_rows, sizes)

    out = tuple(c.model_copy(update={"bot_test_score": rows[c.candidate_id]["mean"],
                                     "bot_test_ci": rows[c.candidate_id]["ci"],
                                     "status": "picked" if c.candidate_id == pick else "bot_tested"})
                for c in replay)
    result = BotTestResult(
        base_mean_score=base_score, base_mean_risk=base_risk, candidates=out,
        risk_delta={cid: r["risk"] for cid, r in rows.items()},
        hard_fail={cid: r["hard_fail"] for cid, r in rows.items()}, pick=pick,
        reason=_reason(rows, pick, displaced, sizes, n_cards=len(base), base_score=base_score,
                       min_gap=bt.min_gap))
    log.info("bot tests g%d: %s", generation, result.reason)
    return result


def _gain(row: dict) -> str:
    lo, hi = row["ci"]
    return f"{row['mean']:+.3f} (95% CI [{lo:+.3f}, {hi:+.3f}])"


def _reason(rows: dict[str, dict], pick: str | None, displaced: str | None, sizes: dict[str, int],
            n_cards: int, base_score: float, min_gap: float) -> str:
    """One readable sentence: why this pick, or why none."""
    if not rows:
        return f"No candidate was bot-tested; the base prompt scored {base_score:.3f} on {n_cards} card(s)."
    if pick is not None:
        eligible = sum(1 for r in rows.values() if r["eligible"])
        why = f"the highest mean of {eligible} eligible candidate(s)" if displaced is None else (
            f"preferred over {displaced} because its mean lies inside {displaced}'s CI and its patch is "
            f"shorter ({sizes[pick]} vs {sizes[displaced]} characters)")
        return (f"{pick} is picked: it gains {_gain(rows[pick])} over the base on {rows[pick]['n']} card(s), "
                f"{why}.")
    scored = {cid: r for cid, r in rows.items() if r["mean"] is not None}
    hard = sorted(cid for cid, r in rows.items() if r["hard_fail"])
    bar = f"the bar is a CI lower bound above {min_gap:.3f} and no hard-criterion failure"
    if not scored:
        return f"No candidate could be scored against the base ({n_cards} card(s)); no pick."
    best = max(scored, key=lambda cid: scored[cid]["mean"])
    failed = f"; {', '.join(hard)} failed a hard criterion" if hard else ""
    return (f"No candidate cleared the bar: the best, {best}, gains {_gain(scored[best])}, but {bar}{failed}.")
