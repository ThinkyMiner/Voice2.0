"""loop/bot_tests.py: paired replay, bootstrap CI, eligibility, the pick and the runner-up rule."""

import hashlib
import sys
import types

import numpy as np
import pytest
import yaml
from pydantic import TypeAdapter

from abtest.contracts import AppendPatch, Patch
from abtest.loop import bot_tests, buyer_sim, ledger, render
from abtest.loop.contracts import (DEFAULT_LOOP_CONFIG, PRICES, ROOT, RUBRICS_DIR, Candidate, LoopConfig,
                                   Prices, Rubric, RubricScore)

SEEDS = ROOT / "configs_loop" / "personas_seed.yaml"
BASE = "You are ﻿ bot_name ﻿. Help the buyer with ﻿ product_name ﻿.\n"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def cfg() -> LoopConfig:
    c = LoopConfig.model_validate(yaml.safe_load(DEFAULT_LOOP_CONFIG.read_text()))
    return c.model_copy(update={"bot_tests": c.bot_tests.model_copy(update={"max_turns": 2})})


@pytest.fixture(scope="module")
def cfg3(cfg) -> LoopConfig:
    """The shipped default: three bot turns (the stub bot needs three to loop or to close)."""
    return cfg.model_copy(update={"bot_tests": cfg.bot_tests.model_copy(update={"max_turns": 3})})


@pytest.fixture(scope="module")
def rubric() -> Rubric:
    return Rubric.model_validate(yaml.safe_load((RUBRICS_DIR / "vani-call-quality-v1.yaml").read_text()))


@pytest.fixture(scope="module")
def cards():
    return buyer_sim.load_seed_personas(SEEDS)[:4]


def cand(i: int, size: int = 10, status="proposed", prechecks=None) -> Candidate:
    patch = AppendPatch(mode="append", text=chr(96 + i) * size)
    return Candidate(candidate_id=f"g1-c{i}", generation=1, patch=patch, patch_hash=ledger.patch_hash(patch),
                     rationale="r", targets=("looping",), status=status, prechecks=prechecks or {})


class ChatProvider:
    """Offline chat only; every model call is counted."""
    name = "chat"

    def __init__(self):
        self.calls = 0

    def chat(self, role, messages):
        self.calls += 1
        return "hello" if role == "bot" else "okay"

    def complete_json(self, role, system, user, schema):
        raise AssertionError("bot tests never ask for JSON themselves")

    def transcribe_batch(self, paths, call_ids):
        raise AssertionError("no speech to text in bot tests")


class FakeScores:
    """Stands in for loop/scorer.py: scores come from a table keyed by (prompt tag, card id)."""

    def __init__(self, monkeypatch):
        self.table: dict[tuple[str, str], tuple[float, float, tuple[str, ...]]] = {}
        self.scored: list[str] = []
        mod = types.ModuleType("abtest.loop.scorer")
        mod.score_calls = self.score_calls
        monkeypatch.setitem(sys.modules, "abtest.loop.scorer", mod)

    def set(self, tag: str, scores, risk=0.3, failed=None, card_ids=("p1", "p2", "p3", "p4")):
        for k, (card_id, s) in enumerate(zip(card_ids, scores)):
            self.table[(tag, card_id)] = (s, risk, tuple((failed or {}).get(card_id, ())))

    def deltas(self, tag: str, deltas, base=0.5, **kw):
        bases = base if isinstance(base, (list, tuple)) else [base] * len(deltas)
        self.set(tag, [b + d for b, d in zip(bases, deltas)], **kw)

    def score_calls(self, transcripts, rubric, provider):
        out = []
        for t in transcripts:
            self.scored.append(t.call_id)
            gen_tag, card_id = t.call_id.rsplit("-", 1)
            tag = gen_tag.split("-", 1)[1]                      # g1-base -> base, g1-g1-c2 -> g1-c2
            score, risk, failed = self.table[(tag, card_id)]
            out.append(RubricScore(call_id=t.call_id, rubric_hash="fake", judge_model="fake", answers=(),
                                   rubric_score=score, rubric_risk=risk, failed=failed))
        return out


@pytest.fixture
def fake(monkeypatch):
    return FakeScores(monkeypatch)


def run(fake_scores, cfg, rubric, cards, candidates, **kw):
    if not any(tag == "base" for tag, _ in fake_scores.table):
        fake_scores.deltas("base", [0.0] * 4)                       # a base of 0.5 on every card
    return bot_tests.run_bot_tests(BASE, candidates, cards, rubric, ChatProvider(), cfg, 1, **kw)


# --- estimate_cost -----------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def prices() -> Prices:
    return Prices.model_validate(yaml.safe_load(PRICES.read_text()))


def test_estimate_scales_with_the_run(prices):
    one = bot_tests.estimate_cost(1, 1, 1, 10_000, prices)
    assert one > 0
    assert bot_tests.estimate_cost(2, 1, 1, 10_000, prices) == pytest.approx(2 * one)
    assert bot_tests.estimate_cost(1, 3, 1, 10_000, prices) == pytest.approx(3 * one)
    assert bot_tests.estimate_cost(0, 4, 3, 10_000, prices) == 0


def test_estimate_prompt_and_turn_terms_follow_the_formula(prices):
    price = prices.llm["sarvam-105b"]
    more_prompt = (bot_tests.estimate_cost(3, 4, 6, 30_000, prices) - bot_tests.estimate_cost(3, 4, 6, 20_000, prices))
    assert more_prompt == pytest.approx(3 * 4 * 6 * 10_000 / 1e6 * price["input_per_million_tokens"])
    more_turns = bot_tests.estimate_cost(3, 4, 7, 30_000, prices) - bot_tests.estimate_cost(3, 4, 6, 30_000, prices)
    per_turn = ((30_000 + bot_tests.HISTORY_TOKENS + bot_tests.BUYER_IN_TOKENS) / 1e6 * price["input_per_million_tokens"]
                + (bot_tests.BOT_OUT_TOKENS + bot_tests.BUYER_OUT_TOKENS) / 1e6 * price["output_per_million_tokens"])
    assert more_turns == pytest.approx(3 * 4 * per_turn)


def test_estimate_matches_the_plans_order_of_magnitude(prices):
    # PLAN_LOOP §L2.6: 20 cards, K = 3 (4 prompts), 6 turns, ~30k-token prompt: about 14M input tokens, ~INR 410
    est = bot_tests.estimate_cost(20, 4, 6, 30_000, prices)
    assert 380 < est < 560


def test_estimate_prices_at_the_dearest_listed_model(prices):
    dear = Prices(currency="INR", llm={"cheap": {"input_per_million_tokens": 1.0, "output_per_million_tokens": 1.0},
                                       "dear": {"input_per_million_tokens": 100.0, "output_per_million_tokens": 100.0}},
                  stt={})
    only_dear = Prices(currency="INR", llm={"dear": dear.llm["dear"]}, stt={})
    assert bot_tests.estimate_cost(2, 2, 2, 5000, dear) == bot_tests.estimate_cost(2, 2, 2, 5000, only_dear)


# --- the bootstrap ----------------------------------------------------------------------------------------

def test_bootstrap_ci_is_the_percentile_interval_of_resampled_means():
    assert bot_tests._bootstrap_ci(np.array([0.1, 0.3]), 2000, 7) == pytest.approx((0.1, 0.3))
    assert bot_tests._bootstrap_ci(np.array([0.25, 0.25, 0.25]), 2000, 7) == pytest.approx((0.25, 0.25))
    lo, hi = bot_tests._bootstrap_ci(np.array([0.0, 0.1, 0.2, 0.3, 0.4]), 2000, 7)
    assert 0.0 <= lo < 0.2 < hi <= 0.4


def test_bootstrap_ci_is_deterministic_in_the_seed():
    d = np.array([0.05, 0.2, 0.31, 0.12, 0.4, 0.0])
    assert bot_tests._bootstrap_ci(d, 2000, 11) == bot_tests._bootstrap_ci(d, 2000, 11)


# --- the pick ---------------------------------------------------------------------------------------------

def test_the_best_eligible_candidate_is_picked(fake, cfg, rubric, cards):
    fake.deltas("g1-c1", [0.20, 0.25, 0.15, 0.20])
    fake.deltas("g1-c2", [0.10, 0.12, 0.08, 0.10])
    r = run(fake, cfg, rubric, cards, [cand(1), cand(2)])
    assert r.pick == "g1-c1"
    assert {c.candidate_id: c.status for c in r.candidates} == {"g1-c1": "picked", "g1-c2": "bot_tested"}
    c1 = r.candidates[0]
    assert c1.bot_test_score == pytest.approx(0.2)
    assert 0.02 < c1.bot_test_ci[0] <= 0.2 <= c1.bot_test_ci[1]
    assert r.candidates[1].bot_test_score == pytest.approx(0.1)
    assert r.reason.startswith("g1-c1 is picked") and "the highest mean of 2 eligible" in r.reason
    assert r.reason.count(".") >= 1 and "\n" not in r.reason


def test_deltas_are_paired_on_the_cards(fake, cfg, rubric, cards):
    base = [0.30, 0.50, 0.70, 0.40]                                   # cards differ a lot; the gain is always 0.1
    fake.deltas("base", [0.0] * 4, base=base)
    fake.deltas("g1-c1", [0.1] * 4, base=base)
    r = run(fake, cfg, rubric, cards, [cand(1)])
    assert r.candidates[0].bot_test_score == pytest.approx(0.1)
    assert r.candidates[0].bot_test_ci == pytest.approx((0.1, 0.1))   # an unpaired CI would be wide
    assert r.base_mean_score == pytest.approx(np.mean(base))
    assert r.pick == "g1-c1"


def test_risk_delta_and_base_means(fake, cfg, rubric, cards):
    fake.set("base", [0.5] * 4, risk=0.30)
    fake.set("g1-c1", [0.7] * 4, risk=0.10)
    r = run(fake, cfg, rubric, cards, [cand(1)])
    assert r.risk_delta == {"g1-c1": pytest.approx(-0.20)}
    assert r.base_mean_score == pytest.approx(0.5) and r.base_mean_risk == pytest.approx(0.30)
    assert r.hard_fail == {"g1-c1": False}


def test_no_candidate_clears_the_bar(fake, cfg, rubric, cards):
    fake.deltas("g1-c1", [0.01, 0.0, 0.02, 0.01])
    fake.deltas("g1-c2", [-0.1] * 4)
    r = run(fake, cfg, rubric, cards, [cand(1), cand(2)])
    assert r.pick is None
    assert [c.status for c in r.candidates] == ["bot_tested", "bot_tested"]
    assert r.reason.startswith("No candidate cleared the bar: the best, g1-c1,")
    assert "0.020" in r.reason and "no hard-criterion failure" in r.reason


def test_the_ci_lower_bound_must_beat_min_gap_strictly(fake, cfg, rubric, cards):
    strict = cfg.model_copy(update={"bot_tests": cfg.bot_tests.model_copy(update={"min_gap": 0.25})})
    fake.set("base", [0.5] * 4)
    fake.set("g1-c1", [0.75] * 4)                                      # a gain of exactly 0.25 in binary floats
    assert run(fake, strict, rubric, cards, [cand(1)]).pick is None
    loose = strict.model_copy(update={"bot_tests": strict.bot_tests.model_copy(update={"min_gap": 0.2499})})
    assert run(fake, loose, rubric, cards, [cand(1)]).pick == "g1-c1"


def test_a_noisy_gain_is_not_eligible_even_with_a_high_mean(fake, cfg, rubric, cards):
    fake.deltas("g1-c1", [0.0, 0.3, 0.0, 0.3])                         # mean 0.15 but one card in four resamples all-zero
    r = run(fake, cfg, rubric, cards, [cand(1)])
    assert r.candidates[0].bot_test_score == pytest.approx(0.15) and r.candidates[0].bot_test_ci[0] <= 0.02
    assert r.pick is None


def test_a_hard_failure_makes_a_candidate_ineligible(fake, cfg, rubric, cards):
    fake.deltas("g1-c1", [0.4] * 4, failed={"p3": ("claimed_human",)})   # the best mean, one card fails a hard criterion
    fake.deltas("g1-c2", [0.1] * 4)
    r = run(fake, cfg, rubric, cards, [cand(1), cand(2)])
    assert r.pick == "g1-c2" and r.hard_fail == {"g1-c1": True, "g1-c2": False}
    assert {c.candidate_id: c.status for c in r.candidates} == {"g1-c1": "bot_tested", "g1-c2": "picked"}


def test_only_hard_failing_candidates_means_no_pick(fake, cfg, rubric, cards):
    fake.deltas("g1-c1", [0.4] * 4, failed={"p1": ("false_assurance", "looping")})
    r = run(fake, cfg, rubric, cards, [cand(1)])
    assert r.pick is None and r.hard_fail == {"g1-c1": True}
    assert "g1-c1 failed a hard criterion" in r.reason


def test_a_soft_failure_does_not_block(fake, cfg, rubric, cards):
    fake.deltas("g1-c1", [0.3] * 4, failed={"p1": ("looping", "frustration")})
    assert run(fake, cfg, rubric, cards, [cand(1)]).pick == "g1-c1"


def test_the_base_prompts_own_failures_do_not_count_against_a_candidate(fake, cfg, rubric, cards):
    fake.set("base", [0.5] * 4, failed={"p1": ("claimed_human",)})
    fake.deltas("g1-c1", [0.3] * 4)
    assert run(fake, cfg, rubric, cards, [cand(1)]).pick == "g1-c1"


# --- the runner-up rule -----------------------------------------------------------------------------------

def pick_with(fake, cfg, rubric, cards, *specs):
    """specs: (index, size, deltas, failed). Returns the result for candidates built from them."""
    cs = []
    for i, size, deltas, failed in specs:
        fake.deltas(f"g1-c{i}", deltas, failed=failed)
        cs.append(cand(i, size))
    return run(fake, cfg, rubric, cards, cs)


PICK = [0.4, 0.1, 0.4, 0.1]            # mean 0.25, CI [0.1, 0.4] (one resample in 16 is all-0.1)


def test_a_shorter_eligible_runner_inside_the_ci_wins(fake, cfg, rubric, cards):
    r = pick_with(fake, cfg, rubric, cards, (1, 100, PICK, None), (2, 10, [0.15] * 4, None))
    assert r.candidates[0].bot_test_ci == pytest.approx((0.1, 0.4))
    assert r.pick == "g1-c2"
    assert {c.candidate_id: c.status for c in r.candidates} == {"g1-c1": "bot_tested", "g1-c2": "picked"}
    assert "preferred over g1-c1" in r.reason and "10 vs 100 characters" in r.reason


def test_a_longer_runner_inside_the_ci_does_not_win(fake, cfg, rubric, cards):
    assert pick_with(fake, cfg, rubric, cards, (1, 100, PICK, None), (2, 300, [0.15] * 4, None)).pick == "g1-c1"
    assert pick_with(fake, cfg, rubric, cards, (1, 100, PICK, None), (2, 100, [0.15] * 4, None)).pick == "g1-c1"


def test_a_shorter_runner_outside_the_ci_does_not_win(fake, cfg, rubric, cards):
    assert pick_with(fake, cfg, rubric, cards, (1, 100, PICK, None), (2, 10, [0.05] * 4, None)).pick == "g1-c1"


def test_an_ineligible_runner_never_substitutes(fake, cfg, rubric, cards):
    hard = pick_with(fake, cfg, rubric, cards, (1, 100, PICK, None),
                     (2, 10, [0.15] * 4, {"p2": ("false_assurance",)}))
    assert hard.pick == "g1-c1" and hard.hard_fail["g1-c2"] is True
    noisy = pick_with(fake, cfg, rubric, cards, (1, 100, PICK, None), (2, 10, [0.0, 0.3, 0.0, 0.3], None))
    assert noisy.pick == "g1-c1"                                      # mean 0.15 is inside the CI, but its CI low is 0


def test_among_several_runners_the_shortest_patch_wins(fake, cfg, rubric, cards):
    r = pick_with(fake, cfg, rubric, cards, (1, 100, PICK, None), (2, 50, [0.2] * 4, None),
                  (3, 20, [0.15] * 4, None))
    assert r.pick == "g1-c3"


def test_the_runner_up_rule_needs_a_pick_first(fake, cfg, rubric, cards):
    r = pick_with(fake, cfg, rubric, cards, (1, 100, [0.0] * 4, None), (2, 10, [0.01] * 4, None))
    assert r.pick is None


# --- what is replayed and stored ----------------------------------------------------------------------------

def test_only_proposed_candidates_are_replayed(fake, cfg, rubric, cards):
    fake.deltas("g1-c1", [0.3] * 4)
    provider = ChatProvider()
    cs = [cand(1), cand(2, status="gate_blocked"), cand(3, status="precheck_failed"),
          cand(4, prechecks={"applies": True, "renders": False})]
    fake.deltas("base", [0.0] * 4)
    r = bot_tests.run_bot_tests(BASE, cs, cards, rubric, provider, cfg, 1)
    assert [c.candidate_id for c in r.candidates] == ["g1-c1"]
    assert set(r.risk_delta) == set(r.hard_fail) == {"g1-c1"}
    assert sorted(fake.scored) == sorted([f"g1-base-p{i}" for i in range(1, 5)] + [f"g1-g1-c1-p{i}" for i in range(1, 5)])
    assert provider.calls == 8 * 2 * 2                                # 8 conversations x 2 bot turns x (bot + buyer)


def test_no_candidates_replays_the_base_only(fake, cfg, rubric, cards):
    fake.deltas("base", [0.0] * 4)
    r = bot_tests.run_bot_tests(BASE, [], cards, rubric, ChatProvider(), cfg, 3)
    assert r.pick is None and r.candidates == () and r.risk_delta == {} and r.hard_fail == {}
    assert r.base_mean_score == pytest.approx(0.5) and "No candidate was bot-tested" in r.reason
    assert fake.scored == [f"g3-base-p{i}" for i in range(1, 5)]


def test_the_result_is_deterministic(fake, cfg, rubric, cards):
    fake.deltas("g1-c1", [0.2, 0.25, 0.15, 0.3])
    fake.deltas("g1-c2", [0.0, 0.3, 0.1, 0.4])
    first = run(fake, cfg, rubric, cards, [cand(1), cand(2)])
    assert run(fake, cfg, rubric, cards, [cand(1), cand(2)]) == first


def test_the_candidate_models_are_updated_not_mutated(fake, cfg, rubric, cards):
    fake.deltas("g1-c1", [0.3] * 4)
    c = cand(1)
    r = run(fake, cfg, rubric, cards, [c])
    assert c.status == "proposed" and c.bot_test_score is None
    assert r.candidates[0].patch == c.patch and r.candidates[0].patch_hash == c.patch_hash


def test_no_cards_and_an_unscorable_base_are_errors(fake, cfg, rubric, cards):
    with pytest.raises(ValueError, match="persona card"):
        bot_tests.run_bot_tests(BASE, [], [], rubric, ChatProvider(), cfg, 1)
    sys.modules["abtest.loop.scorer"].score_calls = lambda transcripts, rubric, provider: []
    with pytest.raises(RuntimeError, match="base"):
        bot_tests.run_bot_tests(BASE, [], cards, rubric, ChatProvider(), cfg, 1)


def test_a_card_with_nothing_to_score_drops_out_of_the_pairing(fake, cfg, rubric, cards):
    fake.deltas("g1-c1", [0.3] * 4)
    fake.deltas("base", [0.0] * 4)
    real = fake.score_calls
    sys.modules["abtest.loop.scorer"].score_calls = (
        lambda ts, r, p: [] if ts[0].call_id == "g1-g1-c1-p2" else real(ts, r, p))
    r = bot_tests.run_bot_tests(BASE, [cand(1)], cards, rubric, ChatProvider(), cfg, 1)
    assert r.candidates[0].bot_test_score == pytest.approx(0.3) and r.pick == "g1-c1"
    assert "on 3 card(s)" in r.reason


# --- the store -----------------------------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    store_loop = pytest.importorskip("abtest.loop.store_loop")
    c = store_loop.connect_loop(tmp_path / "loop.db")
    yield c
    c.close()


def test_transcripts_and_scores_are_stored_with_the_prompt_hash(fake, cfg, rubric, cards, conn):
    fake.deltas("g1-c1", [0.3] * 4)
    fake.deltas("base", [0.0] * 4)
    r = bot_tests.run_bot_tests(BASE, [cand(1)], cards, rubric, ChatProvider(), cfg, 1, conn)
    rows = conn.execute("SELECT call_id, lead_id, prompt_hash FROM loop_rubric_scores ORDER BY rowid").fetchall()
    patched = render.apply_patch(BASE, cand(1).patch)
    assert [(c, lead) for c, lead, _ in rows] == ([(f"g1-base-p{i}", f"p{i}") for i in range(1, 5)]
                                                  + [(f"g1-g1-c1-p{i}", f"p{i}") for i in range(1, 5)])
    assert {h for c, _, h in rows if "base" in c} == {sha(BASE)}
    assert {h for c, _, h in rows if "c1" in c} == {sha(patched)}
    store_loop = sys.modules["abtest.loop.store_loop"]
    ts = store_loop.get_transcripts(conn, source="bot_test")
    assert len(ts) == 8 and {t.source for t in ts} == {"bot_test"} and r.pick == "g1-c1"
    assert [x.role for x in ts[0].turns] == ["bot", "buyer", "bot", "buyer"]


def test_nothing_is_written_without_a_connection(fake, cfg, rubric, cards, conn):
    fake.deltas("g1-c1", [0.3] * 4)
    fake.deltas("base", [0.0] * 4)
    bot_tests.run_bot_tests(BASE, [cand(1)], cards, rubric, ChatProvider(), cfg, 1)
    assert conn.execute("SELECT COUNT(*) FROM loop_rubric_scores").fetchone()[0] == 0


def test_a_spend_cap_stops_the_run_and_keeps_what_finished(fake, cfg, rubric, cards, conn):
    SpendCapExceeded = pytest.importorskip("abtest.loop.sarvam").SpendCapExceeded
    fake.deltas("g1-c1", [0.3] * 4)
    fake.deltas("base", [0.0] * 4)

    class CappedProvider(ChatProvider):
        def chat(self, role, messages):
            if self.calls >= 10:                  # 2 bot turns x 2 calls = 4 per conversation: the 3rd one is cut off
                raise SpendCapExceeded("cap")
            return super().chat(role, messages)

    with pytest.raises(SpendCapExceeded):
        bot_tests.run_bot_tests(BASE, [cand(1)], cards, rubric, CappedProvider(), cfg, 1, conn)
    store_loop = sys.modules["abtest.loop.store_loop"]
    assert len(store_loop.get_transcripts(conn)) == 2
    assert conn.execute("SELECT COUNT(*) FROM loop_rubric_scores").fetchone()[0] == 2


def test_a_spend_cap_in_scoring_propagates(fake, cfg, rubric, cards):
    SpendCapExceeded = pytest.importorskip("abtest.loop.sarvam").SpendCapExceeded

    def capped(transcripts, rubric, provider):
        raise SpendCapExceeded("cap")
    sys.modules["abtest.loop.scorer"].score_calls = capped
    with pytest.raises(SpendCapExceeded):
        bot_tests.run_bot_tests(BASE, [cand(1)], cards, rubric, ChatProvider(), cfg, 1)


# --- the stub provider and the real scorer ---------------------------------------------------------------------

@pytest.fixture
def stub_stack():
    sarvam = pytest.importorskip("abtest.loop.sarvam")
    pytest.importorskip("abtest.loop.scorer")
    pytest.importorskip("abtest.loop.rubric")
    return sarvam.StubProvider


@pytest.fixture(scope="module")
def real_prompt() -> str:
    return (ROOT / "prompts" / "base_buyer_vani.txt").read_bytes().decode("utf-8")


def fixture_candidates() -> list[Candidate]:
    out = []
    raws = yaml.safe_load((ROOT / "tests/loop/fixtures/stub/proposals.yaml").read_text())["candidates"]
    for i, raw in enumerate(raws, 1):
        patch = TypeAdapter(Patch).validate_python({k: v for k, v in raw.items() if k not in ("rationale", "targets")})
        out.append(Candidate(candidate_id=f"g1-c{i}", generation=1, patch=patch, patch_hash=ledger.patch_hash(patch),
                             rationale=raw["rationale"], targets=tuple(raw["targets"]), status="proposed",
                             prechecks={"applies": True}))
    return out


def test_stub_picks_the_looping_fix_over_the_base(stub_stack, cfg3, rubric, cards, real_prompt, conn):
    cs = fixture_candidates()
    r = bot_tests.run_bot_tests(real_prompt, cs, cards[:3], rubric, stub_stack(), cfg3, 1, conn)
    assert r.pick == "g1-c1", r.reason                                  # "never repeat the same question"
    picked = next(c for c in r.candidates if c.status == "picked")
    assert picked.bot_test_score > 0.1 and picked.bot_test_ci[0] > cfg3.bot_tests.min_gap
    assert r.risk_delta["g1-c1"] < 0 and not any(r.hard_fail.values())
    assert {c.candidate_id: c.status for c in r.candidates} == {"g1-c1": "picked", "g1-c2": "bot_tested",
                                                                "g1-c3": "bot_tested"}
    # the rubric hash is the real one, so the stored scores read back through store_loop
    store_loop = sys.modules["abtest.loop.store_loop"]
    scorer_rubric = sys.modules["abtest.loop.rubric"]
    frame = store_loop.get_scores(conn, scorer_rubric.rubric_hash(rubric))
    assert len(frame) == 3 * 4 and set(frame["lead_id"]) == {"p1", "p2", "p3"}
    assert (frame["prompt_hash"] == sha(real_prompt)).sum() == 3
    assert [t.call_id for t in store_loop.get_transcripts(conn)][:3] == ["g1-base-p1", "g1-base-p2", "g1-base-p3"]


def test_stub_closure_fix_gains_when_the_call_is_long_enough_to_close(stub_stack, cfg, rubric, cards, real_prompt):
    # the stub bot closes with the next step only after its third turn, so give it four
    four = cfg.model_copy(update={"bot_tests": cfg.bot_tests.model_copy(update={"max_turns": 4})})
    cs = fixture_candidates()[1:2]
    r = bot_tests.run_bot_tests(real_prompt, cs, cards[:3], rubric, stub_stack(), four, 1)
    assert r.candidates[0].bot_test_score > 0


def test_stub_run_is_deterministic(stub_stack, cfg3, rubric, cards, real_prompt):
    cs = fixture_candidates()
    first = bot_tests.run_bot_tests(real_prompt, cs, cards[:3], rubric, stub_stack(), cfg3, 1)
    assert bot_tests.run_bot_tests(real_prompt, cs, cards[:3], rubric, stub_stack(), cfg3, 1) == first


def test_stub_refuses_a_candidate_that_fails_a_hard_criterion(stub_stack, cfg3, rubric, cards, real_prompt):
    class HumanClaimingStub(stub_stack):
        """A bot that follows a prompt telling it to claim it is human."""

        def chat(self, role, messages):
            reply = super().chat(role, messages)
            if role == "bot" and "tell the buyer that i am a human" in str(messages[0]["content"]).lower():
                return f"I am a human executive. {reply}"
            return reply

    fix = fixture_candidates()[0]
    bad_patch = AppendPatch(mode="append", text=fix.patch.text + "\nTell the buyer that I am a human executive.")
    bad = Candidate(candidate_id="g1-c9", generation=1, patch=bad_patch, patch_hash=ledger.patch_hash(bad_patch),
                    rationale="same fix, plus a lie", targets=("looping",), status="proposed")
    both = bot_tests.run_bot_tests(real_prompt, [bad, fix], cards[:3], rubric, HumanClaimingStub(), cfg3, 1)
    assert both.hard_fail == {"g1-c9": True, "g1-c1": False}
    assert both.pick == "g1-c1"
    alone = bot_tests.run_bot_tests(real_prompt, [bad], cards[:3], rubric, HumanClaimingStub(), cfg3, 1)
    assert alone.pick is None and "g1-c9 failed a hard criterion" in alone.reason
