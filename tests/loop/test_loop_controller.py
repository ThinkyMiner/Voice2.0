"""Generation controller (PLAN_LOOP.md §L2.9, §L9): the state sequence, NO_CANDIDATE, the roll-out of a
promoted prompt, the injected sim truth, the spend-cap stop, the pause flag and the stop conditions.

The sibling modules (rubric, diagnose, ledger, proposer, gate, bot_tests, render, live_ab, sarvam) are
swapped for small fakes through sys.modules, so these tests need none of them and keep passing as they land.
store_loop is the real one, on an on-disk SQLite DB. The last test runs two generations on the real modules
and the StubProvider; it skips until every module it needs exists. No test touches the network.
"""

import hashlib
import sys
import types
import uuid
from pathlib import Path

import pandas as pd
import pytest
import yaml

from abtest.contracts import AppendPatch
from abtest.loop import controller
from abtest.loop.contracts import (DEFAULT_LOOP_CONFIG, ROOT, BotTestResult, Candidate, Generation, JudgeAnswer,
                                   LiveAbResult, LoopConfig, PersonaCard, Prices, Rubric, RubricScore, SpendRow,
                                   Transcript, Turn)

store = pytest.importorskip("abtest.loop.store_loop")

BASE_TEXT = "You are a buyer-side voice bot.\nAsk at most quantity = 3 questions.\n"
SENTINEL_LINE = "SENTINEL-TRANSCRIPT-LINE call me on my private number"
SENTINEL_RATIONALE = "SENTINEL-RATIONALE quoted from a call"
STEPS_WITH_CALLS = ["observe", "diagnose", "propose", "gate", "bot_tests"]
FULL_SEQUENCE = ["GEN_START", "OBSERVED", "AUDIT_PENDING", "DIAGNOSED", "PROPOSED", "GATED", "BOT_TESTED",
                 "DOGFOOD", "LIVE_AB", "PROMOTED", "GEN_END"]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def append_patch(text: str) -> AppendPatch:
    return AppendPatch(mode="append", text=text)


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.contexts: list[tuple[int, str]] = []

    def set_context(self, generation: int, step: str) -> None:
        self.contexts.append((generation, step))


class World:
    """The fake sibling modules plus the knobs a test turns. Everything they were called with is recorded."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.monkeypatch = monkeypatch
        self.data = tmp_path / "data"
        self.reports = tmp_path / "reports"
        monkeypatch.setattr(controller, "LOOP_DATA_DIR", self.data)
        monkeypatch.setattr(controller, "LOOP_REPORTS_DIR", self.reports)
        monkeypatch.delenv("ABT_LOOP_PAUSE", raising=False)

        self.base_path = tmp_path / "base_prompt.txt"
        self.base_path.write_bytes(BASE_TEXT.encode("utf-8"))
        self.raw_cfg = yaml.safe_load(DEFAULT_LOOP_CONFIG.read_text(encoding="utf-8"))
        self.raw_cfg["caps"]["max_generations"] = 10
        self.provider = FakeProvider()
        self.conn = store.connect_loop(tmp_path / "loop.db")
        self.rubric = Rubric.model_validate(
            yaml.safe_load((ROOT / "configs_loop" / "rubrics" / "vani-call-quality-v1.yaml").read_text(encoding="utf-8")))

        # knobs
        self.terminals = ["PROMOTED"]            # the live A/B's terminal state per generation, cycled
        self.pick_score = 0.10
        self.risk_delta = -0.02
        self.pick_enabled = True
        self.blocked: set[str] = set()
        self.proposals = None                    # callable(generation, k) -> list[Candidate]; default: fresh ones
        self.raise_at: str | None = None
        self.cost = 10.0                         # INR spent by each run_bot_tests call
        self.estimate = 1.0
        self.on_live_ab = None
        # records
        self.states: list[str] = []
        self.bot_calls: list[dict] = []
        self.propose_calls: list[dict] = []
        self.gate_calls: list[str] = []
        self.live_calls: list[dict] = []
        self.excerpt_calls: list[list[str]] = []

        self.ctx = controller.LoopContext(
            cfg=LoopConfig.model_validate(self.raw_cfg), conn=self.conn, provider=self.provider, rubric=self.rubric,
            prices=Prices(currency="INR", llm={}, stt={}), personas=self._cards(), loop_id="test-loop",
            base_prompt_path=str(self.base_path), quiet=True)
        self._install()
        self._spy_on_put_generation()

    # --- setup ------------------------------------------------------------------------------

    @staticmethod
    def _cards() -> list[PersonaCard]:
        return [PersonaCard(card_id=f"p{i}", source_call_id="seed", requirement="a pump", render_vars={},
                            language="hi-IN", tone="calm") for i in range(3)]

    def configure(self, **dotted) -> None:
        """world.configure(**{"caps.max_generations": 2}) rebuilds ctx.cfg from the default config."""
        for key, value in dotted.items():
            node = self.raw_cfg
            *parents, leaf = key.split(".")
            for p in parents:
                node = node[p]
            node[leaf] = value
        self.ctx.cfg = LoopConfig.model_validate(self.raw_cfg)

    def _spy_on_put_generation(self) -> None:
        real = store.put_generation

        def spy(conn, g):
            self.states.append(g.state)
            return real(conn, g)

        self.monkeypatch.setattr(store, "put_generation", spy)

    def _install(self) -> None:
        def module(name: str, **attrs):
            mod = types.ModuleType(f"abtest.loop.{name}")
            for k, v in attrs.items():
                setattr(mod, k, v)
            self.monkeypatch.setitem(sys.modules, mod.__name__, mod)
            self.monkeypatch.setattr(sys.modules["abtest.loop"], name, mod, raising=False)
            return mod

        class SpendCapExceeded(RuntimeError):
            pass

        self.SpendCapExceeded = SpendCapExceeded
        module("sarvam", SpendCapExceeded=SpendCapExceeded)
        module("rubric", rubric_hash=lambda r: store.register_rubric(r))
        module("render", apply_patch=lambda template, patch: template + ("" if template.endswith("\n") else "\n")
               + patch.text + "\n")
        module("diagnose", diagnose=self._diagnose, excerpts=self._excerpts)
        module("ledger", tried_hashes=lambda conn, loop_id: {c.patch_hash for c in store.get_candidates(conn, loop_id)},
               ledger_summary=lambda conn, loop_id, max_items=20: [])
        module("proposer", propose=self._propose)
        module("gate", gate=self._gate)
        module("bot_tests", run_bot_tests=self._run_bot_tests, estimate_cost=self._estimate_cost)
        module("live_ab", run_live_ab=self._run_live_ab)

    def _maybe_raise(self, step: str) -> None:
        if self.raise_at == step:
            raise self.SpendCapExceeded(f"cap hit at {step}")

    # --- the fakes --------------------------------------------------------------------------

    def _diagnose(self, scores, rubric, top_k):
        rows = [{"criterion_id": c.id, "failure_rate": 1 - scores[c.id].mean(), "ci_low": 0.0, "ci_high": 1.0,
                 "weight": c.weight, "weighted_cost": c.weight * (1 - scores[c.id].mean()),
                 "n_leads": scores["lead_id"].nunique()} for c in rubric.criteria]
        df = pd.DataFrame(rows).sort_values("weighted_cost", ascending=False, kind="stable").reset_index(drop=True)
        df["rank"] = range(1, len(df) + 1)
        df["target"] = df["rank"] <= top_k
        return df

    def _excerpts(self, transcripts, scores, criterion_id, n=3, max_chars=600):
        got = [t.turns[0].text for t in transcripts]
        self.excerpt_calls.append(got)
        return got[:n]

    def _propose(self, base_prompt, diagnosis, excerpts, rubric, ledger, tried, k, generation, provider, cfg):
        self.propose_calls.append({"base": base_prompt, "diagnosis": diagnosis, "excerpts": excerpts, "tried": set(tried),
                                   "k": k, "generation": generation})
        self._maybe_raise("propose")
        if self.proposals is not None:
            return self.proposals(generation, k)
        return [self.candidate(generation, i) for i in range(k)]

    def candidate(self, generation: int, i: int, text: str | None = None, status: str = "proposed") -> Candidate:
        text = text or f"Rule {generation}.{i}: never repeat the same question."
        ok = {"applies": True, "jinja_parses": True, "renders": True, "within_size": True}
        tried = sha(text) in self.propose_calls[-1]["tried"] if self.propose_calls else False
        return Candidate(candidate_id=f"g{generation}c{i}", generation=generation, patch=append_patch(text),
                         patch_hash=sha(text), rationale=SENTINEL_RATIONALE, targets=("looping",),
                         prechecks=ok, status="precheck_failed" if tried else status)

    def _gate(self, base_prompt, candidate, provider, cfg):
        self.gate_calls.append(candidate.candidate_id)
        self._maybe_raise("gate")
        if candidate.candidate_id in self.blocked:
            return candidate.model_copy(update={"gate": {"alters_commitments": 0.9}, "status": "gate_blocked"})
        return candidate.model_copy(update={"gate": {"alters_commitments": 0.05}})

    def _estimate_cost(self, n_cards, n_prompts, max_turns, prompt_tokens, prices):
        return self.estimate

    def _run_bot_tests(self, base_prompt, candidates, cards, rubric, provider, cfg, generation, conn=None):
        self.bot_calls.append({"prompt": base_prompt, "candidates": [c.candidate_id for c in candidates],
                               "generation": generation})
        self._maybe_raise("bot_tests")
        spend_id = uuid.uuid4().hex
        store.reserve_spend(conn, SpendRow(spend_id=spend_id, generation=generation, step="bot_tests",
                                           state="reserved", estimate_inr=self.cost))
        store.settle_spend(conn, spend_id, self.cost, 100, 10)
        resolved = [base_prompt] + [base_prompt + ("" if base_prompt.endswith("\n") else "\n") + c.patch.text + "\n"
                                    for c in candidates]
        for prompt in resolved:  # like the real thing: conversations and scores of every prompt are stored
            self._store_scores(conn, prompt, cards, rubric)
        scored = [c.model_copy(update={"bot_test_score": self.pick_score - 0.01 * i,
                                       "bot_test_ci": (self.pick_score - 0.01 * i - 0.05, self.pick_score - 0.01 * i + 0.05),
                                       "status": "bot_tested"}) for i, c in enumerate(candidates)]
        pick = scored[0].candidate_id if scored and self.pick_enabled else None
        return BotTestResult(base_mean_score=0.5, base_mean_risk=0.3, candidates=tuple(scored),
                             risk_delta={c.candidate_id: self.risk_delta for c in scored},
                             hard_fail={c.candidate_id: False for c in scored}, pick=pick,
                             reason=f"fake pick {pick}" if pick else "fake: nobody cleared the gap")

    def _store_scores(self, conn, prompt: str, cards, rubric) -> None:
        h, rh = sha(prompt), store.register_rubric(rubric)
        for card in cards:
            call_id = f"{h[:8]}-{card.card_id}"
            store.put_transcript(conn, Transcript(
                call_id=call_id, source="bot_test", language="hi-IN", role_confidence=1.0, stt_model="text",
                audio_seconds=0.0, turns=(Turn(role="buyer", start_s=0, end_s=1, text=SENTINEL_LINE),)))
            answers = tuple(JudgeAnswer(criterion_id=c.id, value=0.3 if c.kind == "yes_no" else 3.0, provider="fake")
                            for c in rubric.criteria)
            store.put_score(conn, RubricScore(call_id=call_id, rubric_hash=rh, judge_model="fake", answers=answers,
                                              rubric_score=0.6, rubric_risk=0.3, failed=()),
                            lead_id=card.card_id, prompt_hash=h)

    def _run_live_ab(self, conn, cfg, base_prompt_path, candidate, generation, injected_lift, injected_risk_shift,
                     seed, quiet=True):
        self.live_calls.append({"base_prompt_path": base_prompt_path, "candidate": candidate.candidate_id,
                                "generation": generation, "lift": injected_lift, "shift": injected_risk_shift,
                                "seed": seed, "quiet": quiet})
        if self.on_live_ab:
            self.on_live_ab()
        terminal = self.terminals[(generation - 1) % len(self.terminals)]
        return LiveAbResult(experiment_id=f"{cfg.loop.id}-g{generation}", terminal=terminal, looks=3, lift_rel=0.12,
                            lift_ci95=(0.01, 0.2), rubric_delta=-0.01, rubric_rci=(-0.03, 0.02), decided_by="engine",
                            injected_lift=injected_lift, injected_risk_shift=injected_risk_shift, reason="fake A/B")

    # --- helpers for assertions -------------------------------------------------------------

    def prompt_files(self) -> list[Path]:
        return sorted((self.data / "prompts").glob("*.txt")) if (self.data / "prompts").exists() else []

    def rows(self) -> list[Generation]:
        return store.get_generations(self.conn, self.ctx.loop_id)


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    return World(tmp_path, monkeypatch)


# --- one generation ---------------------------------------------------------------------------

def test_a_generation_walks_every_state_and_persists_each(world):
    g = controller.run_generation(world.ctx, 1)

    assert world.states == FULL_SEQUENCE
    assert (g.state, g.terminal, g.generation) == ("GEN_END", "PROMOTED", 1)
    assert g.pick == "g1c0" and g.experiment_id == "vani-loop-g1"
    assert g.base_prompt_hash == sha(BASE_TEXT)
    assert world.rows() == [g]  # what is stored is what was returned
    assert [(x.candidate_id, x.status) for x in store.get_candidates(world.conn, "test-loop", 1)] == [
        ("g1c0", "promoted"), ("g1c1", "not_picked")]
    assert world.provider.contexts == [(1, s) for s in STEPS_WITH_CALLS]


def test_the_steps_get_the_right_inputs(world):
    controller.run_generation(world.ctx, 1)

    # observe: a base-only run on the production prompt, first
    assert world.bot_calls[0] == {"prompt": BASE_TEXT, "candidates": [], "generation": 1}
    p = world.propose_calls[0]
    assert (p["base"], p["k"], p["generation"], p["tried"]) == (BASE_TEXT, world.ctx.cfg.propose.candidates, 1, set())
    assert list(p["excerpts"]) == list(p["diagnosis"].loc[p["diagnosis"]["target"], "criterion_id"])
    assert world.gate_calls == ["g1c0", "g1c1"]
    assert world.bot_calls[1] == {"prompt": BASE_TEXT, "candidates": ["g1c0", "g1c1"], "generation": 1}
    assert world.live_calls == [{"base_prompt_path": str(world.base_path), "candidate": "g1c0", "generation": 1,
                                 "lift": pytest.approx(0.15), "shift": -0.02,
                                 "seed": world.ctx.cfg.sim.seed + 1, "quiet": True}]


def test_precheck_failures_are_ledgered_but_never_gated(world):
    world.proposals = lambda g, k: [world.candidate(g, 0, "bad one", "precheck_failed"), world.candidate(g, 1)]
    controller.run_generation(world.ctx, 1)

    assert world.gate_calls == ["g1c1"]
    saved = {c.candidate_id: c.status for c in store.get_candidates(world.conn, "test-loop")}
    assert saved == {"g1c0": "precheck_failed", "g1c1": "promoted"}


@pytest.mark.parametrize("how, last_state", [("prechecks", "PROPOSED"), ("gate", "GATED"), ("bot_tests", "BOT_TESTED")])
def test_no_candidate_ends_the_generation_early(world, how, last_state):
    if how == "prechecks":
        world.proposals = lambda g, k: [world.candidate(g, i, status="precheck_failed") for i in range(k)]
    elif how == "gate":
        world.blocked = {"g1c0", "g1c1"}
    else:
        world.pick_enabled = False
    g = controller.run_generation(world.ctx, 1)

    after = world.states[world.states.index(last_state):]
    assert after == [last_state, "NO_CANDIDATE", "GEN_END"]
    assert (g.state, g.terminal, g.pick, g.experiment_id) == ("GEN_END", "NO_CANDIDATE", None, None)
    assert world.live_calls == []
    if how != "bot_tests":
        assert not any(c["candidates"] for c in world.bot_calls)  # only the observation ran
    assert world.prompt_files() == []
    assert "NO_CANDIDATE" in (world.reports / "generation_test-loop_1.md").read_text(encoding="utf-8")


def test_a_tried_patch_is_not_proposed_again(world):
    world.terminals = ["INCONCLUSIVE"]
    world.proposals = lambda g, k: [world.candidate(g, i, f"The same rule {i}.") for i in range(k)]
    first, second = controller.run_loop(world.ctx, 2)

    assert first.terminal == "INCONCLUSIVE"
    assert second.terminal == "NO_CANDIDATE"
    assert world.propose_calls[1]["tried"] == {sha("The same rule 0."), sha("The same rule 1.")}


# --- roll-out ---------------------------------------------------------------------------------

def test_a_promotion_writes_the_new_prompt_and_the_next_generation_starts_from_it(world):
    controller.run_generation(world.ctx, 1)

    promoted = BASE_TEXT + "Rule 1.0: never repeat the same question.\n"
    path = world.data / "prompts" / f"{sha(promoted)}.txt"
    assert world.prompt_files() == [path] and path.read_bytes().decode("utf-8") == promoted
    assert controller.production_prompt(world.ctx) == controller.Production(path=str(path), text=promoted,
                                                                            hash=sha(promoted))

    g2 = controller.run_generation(world.ctx, 2)
    assert g2.base_prompt_hash == sha(promoted)
    assert world.live_calls[1]["base_prompt_path"] == str(path)
    assert world.propose_calls[1]["base"] == promoted
    # the promoted prompt's conversations were scored in generation 1: no second observation run
    assert [c for c in world.bot_calls if c["generation"] == 2 and not c["candidates"]] == []


def test_a_harm_stop_leaves_production_on_the_promoted_prompt(world):
    world.terminals = ["PROMOTED", "STOPPED_HARM", "INCONCLUSIVE"]
    g1, g2, g3 = controller.run_loop(world.ctx, 3)

    promoted = BASE_TEXT + "Rule 1.0: never repeat the same question.\n"
    assert [g.terminal for g in (g1, g2, g3)] == ["PROMOTED", "STOPPED_HARM", "INCONCLUSIVE"]
    assert g2.base_prompt_hash == g3.base_prompt_hash == sha(promoted)
    assert "STOPPED_HARM" in world.states  # the generation mirrors the experiment's terminal state
    assert [p.name for p in world.prompt_files()] == [f"{sha(promoted)}.txt"]
    statuses = {c.candidate_id: c.status for c in store.get_candidates(world.conn, "test-loop")}
    assert statuses["g1c0"] == "promoted" and statuses["g2c0"] == "not_promoted"


def test_a_terminal_that_needs_a_human_leaves_the_generation_at_live_ab(world):
    world.terminals = ["PENDING_APPROVAL"]
    g = controller.run_generation(world.ctx, 1)

    assert g.terminal == "PENDING_APPROVAL" and g.state == "GEN_END"
    assert world.states[-2:] == ["LIVE_AB", "GEN_END"] and world.states.count("LIVE_AB") == 2
    assert world.prompt_files() == []


# --- the injected simulated truth ---------------------------------------------------------------

def _result(world, pick_id="g1c0", score=0.1, shift=-0.02):
    c = world.candidate(1, 0).model_copy(update={"bot_test_score": score})
    return c, BotTestResult(base_mean_score=0.5, base_mean_risk=0.3, candidates=(c,), risk_delta={pick_id: shift},
                            hard_fail={pick_id: False}, pick=pick_id, reason="r")


@pytest.mark.parametrize("score, lift", [(0.10, 0.15), (0.50, 0.30), (-0.50, -0.30), (0.0, 0.0)])
def test_from_bot_tests_scales_and_caps_the_lift(world, score, lift):
    pick, result = _result(world, score=score)
    assert controller.injected_truth(world.ctx.cfg, result, pick, 1) == (pytest.approx(lift), -0.02)


def test_scenario_sequence_is_cycled_and_shifts_nothing(world):
    world.terminals = ["INCONCLUSIVE"]
    world.configure(**{"sim.effect_mode": "scenario_sequence", "sim.scenario_sequence": [0.15, -0.20, 0.02]})
    controller.run_loop(world.ctx, 4)

    assert [c["lift"] for c in world.live_calls] == [0.15, -0.20, 0.02, 0.15]
    assert {c["shift"] for c in world.live_calls} == {0.0}


def test_an_empty_scenario_sequence_is_an_error(world):
    world.configure(**{"sim.effect_mode": "scenario_sequence", "sim.scenario_sequence": []})
    with pytest.raises(ValueError, match="scenario_sequence is empty"):
        controller.run_generation(world.ctx, 1)


# --- spend ------------------------------------------------------------------------------------

def test_spend_is_counted_per_generation(world):
    g1 = controller.run_generation(world.ctx, 1)  # observe + bot tests
    g2 = controller.run_generation(world.ctx, 2)  # the promoted prompt is already scored: bot tests only

    assert (g1.spend_inr, g2.spend_inr) == (20.0, 10.0)
    assert store.total_spend(world.conn) == 30.0
    assert "INR 30.00 of the INR" in (world.reports / "generation_test-loop_2.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("step", ["propose", "gate", "bot_tests"])
def test_the_spend_cap_ends_the_generation_as_spend_cap(world, step):
    world.raise_at = step
    g = controller.run_generation(world.ctx, 1)

    assert (g.state, g.terminal) == ("GEN_END", "SPEND_CAP")
    assert world.rows() == [g] and world.states[-1] == "GEN_END"
    assert "SPEND_CAP" in (world.reports / "generation_test-loop_1.md").read_text(encoding="utf-8")
    assert world.live_calls == []


def test_run_loop_stops_on_the_spend_cap(world):
    world.raise_at = "gate"
    gens, reason = controller.run_until_stop(world.ctx, 3)

    assert [g.terminal for g in gens] == ["SPEND_CAP"] and reason == "spend cap reached"


def test_run_loop_does_not_start_over_the_cap(world):
    store.reserve_spend(world.conn, SpendRow(spend_id="x", generation=0, step="s", state="reserved",
                                             estimate_inr=world.ctx.cfg.caps.max_spend_inr))
    gens, reason = controller.run_until_stop(world.ctx, 2)

    assert gens == [] and reason.startswith("spend cap reached") and world.rows() == []


def test_the_preflight_refuses_a_bot_test_run_the_cap_cannot_pay_for(world):
    world.provider.name = "sarvam"
    world.estimate = world.ctx.cfg.caps.max_spend_inr + 1
    g = controller.run_generation(world.ctx, 1)

    assert g.terminal == "SPEND_CAP"
    assert not any(c["candidates"] for c in world.bot_calls)  # only the observation ran
    assert "exceeds the spend left" in (world.reports / "generation_test-loop_1.md").read_text(encoding="utf-8")


def test_the_preflight_lets_an_affordable_run_through_and_stub_skips_it(world):
    world.provider.name = "sarvam"
    world.estimate = 5.0
    assert controller.run_generation(world.ctx, 1).terminal == "PROMOTED"

    world.provider.name = "stub"
    world.estimate = float("inf")  # would refuse if it were consulted
    assert controller.run_generation(world.ctx, 2).terminal == "PROMOTED"


# --- the loop: stop conditions ----------------------------------------------------------------

def test_run_loop_starts_after_the_last_stored_generation(world):
    world.terminals = ["INCONCLUSIVE"]
    assert [g.generation for g in controller.run_loop(world.ctx, 1)] == [1]
    assert [g.generation for g in controller.run_loop(world.ctx, 2)] == [2, 3]
    assert [g.generation for g in world.rows()] == [1, 2, 3]


def test_run_loop_stops_at_max_generations(world):
    world.terminals = ["INCONCLUSIVE"]
    world.configure(**{"caps.max_generations": 2})
    gens, reason = controller.run_until_stop(world.ctx, 5)

    assert [g.generation for g in gens] == [1, 2] and "max_generations" in reason
    assert controller.run_loop(world.ctx, 1) == []


def test_three_no_candidate_generations_in_a_row_stop_the_loop(world):
    world.proposals = lambda g, k: [world.candidate(g, i, status="precheck_failed") for i in range(k)]
    gens, reason = controller.run_until_stop(world.ctx, 6)

    assert [g.terminal for g in gens] == ["NO_CANDIDATE"] * 3
    assert reason == "3 consecutive NO_CANDIDATE generations"
    assert controller.run_loop(world.ctx, 1) == []  # the ledger remembers the streak


def test_a_generation_that_found_a_candidate_resets_the_streak(world):
    world.terminals = ["INCONCLUSIVE"]
    fail = {1, 2, 4, 5}
    world.proposals = lambda g, k: [world.candidate(g, i, status="precheck_failed" if g in fail else "proposed")
                                    for i in range(k)]
    gens, _ = controller.run_until_stop(world.ctx, 5)

    assert [g.terminal for g in gens] == ["NO_CANDIDATE", "NO_CANDIDATE", "INCONCLUSIVE", "NO_CANDIDATE",
                                          "NO_CANDIDATE"]


def test_the_pause_env_stops_the_loop_before_any_generation(world, monkeypatch):
    monkeypatch.setenv("ABT_LOOP_PAUSE", "1")
    gens, reason = controller.run_until_stop(world.ctx, 2)

    assert gens == [] and reason.startswith("paused") and controller.is_paused() and world.rows() == []


def test_the_pause_file_stops_the_loop_before_any_generation(world):
    controller.pause_flag().parent.mkdir(parents=True, exist_ok=True)
    controller.pause_flag().touch()
    gens, reason = controller.run_until_stop(world.ctx, 2)

    assert gens == [] and reason.startswith("paused")
    controller.pause_flag().unlink()
    assert not controller.is_paused() and len(controller.run_loop(world.ctx, 1)) == 1


def test_pausing_during_a_generation_stops_the_loop_before_the_next(world):
    world.terminals = ["INCONCLUSIVE"]

    def pause():
        controller.pause_flag().parent.mkdir(parents=True, exist_ok=True)
        controller.pause_flag().touch()

    world.on_live_ab = pause
    gens, reason = controller.run_until_stop(world.ctx, 3)

    assert [g.generation for g in gens] == [1] and reason.startswith("paused")
    assert gens[0].state == "GEN_END"  # the running generation finished cleanly


def test_a_crashed_generation_is_rerun_not_skipped(world):
    now = pd.Timestamp.now().to_pydatetime()
    store.put_generation(world.conn, Generation(generation=1, loop_id="test-loop", state="OBSERVED",
                                                base_prompt_hash=sha(BASE_TEXT), rubric_hash="x",
                                                started_ts=now, updated_ts=now))
    gens = controller.run_loop(world.ctx, 1)

    assert [g.generation for g in gens] == [1] and gens[0].state == "GEN_END"
    assert [g.generation for g in world.rows()] == [1]


def test_a_promoted_generation_that_never_reached_gen_end_is_not_yet_production(world):
    now = pd.Timestamp.now().to_pydatetime()
    store.put_generation(world.conn, Generation(generation=1, loop_id="test-loop", state="PROMOTED", terminal="PROMOTED",
                                                pick="g1c0", base_prompt_hash=sha(BASE_TEXT), rubric_hash="x",
                                                started_ts=now, updated_ts=now))
    assert controller.production_prompt(world.ctx).hash == sha(BASE_TEXT)


def test_a_promoted_pick_missing_from_the_ledger_is_an_error(world):
    now = pd.Timestamp.now().to_pydatetime()
    store.put_generation(world.conn, Generation(generation=1, loop_id="test-loop", state="GEN_END", terminal="PROMOTED",
                                                pick="ghost", base_prompt_hash=sha(BASE_TEXT), rubric_hash="x",
                                                started_ts=now, updated_ts=now))
    with pytest.raises(ValueError, match="ghost"):
        controller.production_prompt(world.ctx)


def test_live_mode_and_missing_personas_are_refused(world):
    world.configure(**{"loop.mode": "live"})
    with pytest.raises(ValueError, match="not supported in this build"):
        controller.run_generation(world.ctx, 1)
    world.configure(**{"loop.mode": "sim"})
    world.ctx.personas = []
    with pytest.raises(ValueError, match="no persona cards"):
        controller.run_generation(world.ctx, 1)


# --- the report -------------------------------------------------------------------------------

def test_the_report_has_aggregates_and_no_transcript_text(world):
    controller.run_generation(world.ctx, 1)
    text = (world.reports / "generation_test-loop_1.md").read_text(encoding="utf-8")

    for heading in ("# Generation 1 of loop `test-loop`", "## Steps", "## Diagnosis", "## Candidates",
                    "## Live A/B (simulated traffic)", "## Spend"):
        assert heading in text
    assert "outcome **PROMOTED**" in text and "Pick: `g1c0`. fake pick g1c0" in text
    assert "Simulated ground truth, injected and never used by the engine" in text
    assert "+15.0%" in text and "-0.020" in text  # the injected lift and risk shift, labelled
    assert "AUDIT_PENDING" in text and "DOGFOOD" in text and text.count("Skipped") == 2
    assert "decided_by" not in text and "Decided by | engine" in text
    # the fake excerpts really carried transcript text, and the report has none of it, nor the rationale
    assert world.excerpt_calls and SENTINEL_LINE in world.excerpt_calls[0]
    assert SENTINEL_LINE not in text and SENTINEL_RATIONALE not in text
    assert "never repeat the same question" not in text  # the patch text stays in the DB too


def test_the_report_names_each_generation_of_each_loop(world):
    world.terminals = ["INCONCLUSIVE"]
    controller.run_loop(world.ctx, 2)
    assert sorted(p.name for p in world.reports.glob("*.md")) == ["generation_test-loop_1.md",
                                                                  "generation_test-loop_2.md"]


# --- the real modules, end to end ---------------------------------------------------------------

REAL_MODULES = ["sarvam", "store_loop", "rubric", "render", "diagnose", "ledger", "proposer", "gate", "bot_tests",
                "buyer_sim", "live_ab", "guard", "sim_scores", "synthdata"]


def test_two_generations_on_the_stub_provider(tmp_path, monkeypatch):
    for name in REAL_MODULES:  # skip, saying which modules are still missing, until they all exist
        pytest.importorskip(f"abtest.loop.{name}", reason=f"loop/{name}.py not built yet")
    for needed in (ROOT / "tests" / "loop" / "fixtures" / "stub" / "proposals.yaml",
                   ROOT / "configs_loop" / "personas_seed.yaml"):
        if not needed.exists():
            pytest.skip(f"{needed.relative_to(ROOT)} not written yet")
    from abtest.loop import cli, live_ab

    monkeypatch.setattr(controller, "LOOP_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(controller, "LOOP_REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(live_ab, "LOOP_DATA_DIR", tmp_path / "data")  # its synthetic dataset and sim configs
    monkeypatch.setenv("LOOP_PROVIDER", "stub")
    monkeypatch.delenv("ABT_LOOP_PAUSE", raising=False)

    from abtest.loop import sarvam
    cfg = sarvam.load_loop_config(DEFAULT_LOOP_CONFIG)
    ctx = cli.build_context(cfg, provider="stub", db=tmp_path / "loop.db", quiet=True)
    gens = controller.run_loop(ctx, 2)

    assert [g.generation for g in gens] == [1, 2] and all(g.state == "GEN_END" for g in gens)
    assert all(g.terminal for g in gens)
    assert store.total_spend(ctx.conn) == 0.0  # the stub provider never spends
    assert sorted(p.name for p in (tmp_path / "reports").glob("*.md")) == [
        f"generation_{cfg.loop.id}_1.md", f"generation_{cfg.loop.id}_2.md"]
    if gens[0].terminal == "PROMOTED":  # the promoted prompt is on disk and generation 2 starts from it
        files = list((tmp_path / "data" / "prompts").glob("*.txt"))
        # one file per promotion; generation 2 starts from generation 1's promoted prompt
        assert gens[1].base_prompt_hash in {f.stem for f in files}
        assert len(files) == sum(g.terminal == "PROMOTED" for g in gens)


def test_the_gate_fails_closed_when_it_cannot_answer():
    from abtest.loop.contracts import Candidate
    from abtest.contracts import AppendPatch

    class ProviderError(Exception):  # matched by name, like loop.sarvam.ProviderError
        pass

    class BrokenGate:
        @staticmethod
        def gate(prompt, candidate, provider, cfg):
            raise ProviderError("gate: no valid JSON object after one retry")

    patch = AppendPatch(mode="append", text="be kind")
    c = Candidate(candidate_id="g1-c1", generation=1, patch=patch, patch_hash="h", rationale="r", targets=("closure",))
    out = controller._gate_or_block(BrokenGate, "prompt", c, provider=None, cfg=None)
    assert out.status == "gate_blocked" and out.gate == {"gate_unavailable": 1.0}
