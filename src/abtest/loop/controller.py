"""Generation controller: one pass of the self-improving loop, and the loop around it
(PLAN_LOOP.md §L2.9 as narrowed by §L9).

A generation walks GEN_START -> OBSERVED -> AUDIT_PENDING -> DIAGNOSED -> PROPOSED -> GATED ->
BOT_TESTED -> DOGFOOD -> LIVE_AB -> <the experiment's terminal state> -> GEN_END, persisting the
Generation row at every transition and every Candidate as it changes, so the ledger always shows where
a run stopped. The build is sim-only (§L9): the audit gate and the dogfood calls guard real traffic, so
they are recorded as skipped in the generation report, and the live A/B runs on simulated traffic through
the real engine (loop/live_ab.py). Nothing here calls a model: every model call is made by a sibling
module through `ctx.provider`, and SpendCapExceeded is allowed to travel up to this file's one handler.

Sibling modules are imported on use (`loop_module`), so this file loads before they exist and tests can
swap them out. Personal data stays in the loop DB and under data/derived/loop/; the report this file
writes (reports/loop/generation_<loop id>_<n>.md) carries aggregates only, never a transcript line.
"""

import hashlib
import importlib
import os
import sqlite3
import typing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from abtest.loop.contracts import (LOOP_DATA_DIR, LOOP_REPORTS_DIR, ROOT, BotTestResult, Candidate,
                                   GenState, Generation, LiveAbResult, LoopConfig, PersonaCard, Prices,
                                   Rubric)

SPEND_CAP = "SPEND_CAP"            # terminal marker of a generation cut short by caps.max_spend_inr
NO_CANDIDATE_STOP = 3              # consecutive NO_CANDIDATE generations that stop the loop (§L2.9)
CHARS_PER_TOKEN = 4                # rough prompt-size estimate for the pre-flight cost check only
GEN_STATES = frozenset(typing.get_args(GenState))


@dataclass
class LoopContext:
    cfg: LoopConfig
    conn: sqlite3.Connection
    provider: typing.Any               # a loop.sarvam Provider
    rubric: Rubric
    prices: Prices
    personas: list[PersonaCard]
    loop_id: str
    base_prompt_path: str              # relative to ROOT: the template's variant A base (generation 1's production prompt)
    quiet: bool = False


@dataclass(frozen=True)
class Production:
    path: str                          # relative to ROOT (absolute only when outside it)
    text: str
    hash: str


# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------

def loop_module(name: str):
    """A sibling loop module, looked up on use so this file imports before the others exist."""
    return importlib.import_module(f"abtest.loop.{name}")


class _SpendRefused(Exception):
    """The pre-flight cost check refused a paid step before any money was spent."""


def spend_cap_exception() -> type[BaseException]:
    try:
        return loop_module("sarvam").SpendCapExceeded
    except ImportError:  # sarvam.py not built: no provider can raise it either
        class _NeverRaised(Exception):
            pass
        return _NeverRaised


def provider_error() -> type[BaseException]:
    try:
        return loop_module("sarvam").ProviderError
    except ImportError:
        class _NeverRaised(Exception):
            pass
        return _NeverRaised


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rel_path(path: Path) -> str:
    """The path as the base config wants it: relative to ROOT, or as is when outside it."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _read_prompt(path: str) -> str:
    # bytes -> str with no newline translation, so the hash equals the base config's prompt_hash
    return (ROOT / path).read_bytes().decode("utf-8")


def _store_prompt(text: str) -> Path:
    """data/derived/loop/prompts/<sha256>.txt; idempotent, content-addressed."""
    path = LOOP_DATA_DIR / "prompts" / f"{_sha(text)}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(text.encode("utf-8"))
    return path


def pause_flag() -> Path:
    return LOOP_DATA_DIR / "PAUSE"


def is_paused() -> bool:
    return os.environ.get("ABT_LOOP_PAUSE") == "1" or pause_flag().exists()


def _say(ctx: LoopContext, msg: str) -> None:
    if not ctx.quiet:
        print(msg, flush=True)


def _set_context(ctx: LoopContext, generation: int, step: str) -> None:
    """Tell the provider which generation and step its next calls belong to (spend rows), if it cares."""
    fn = getattr(ctx.provider, "set_context", None)
    if callable(fn):
        fn(generation, step)


def production_prompt(ctx: LoopContext) -> Production:
    """The prompt the next generation starts from: the base prompt with every PROMOTED generation's
    pick applied in order (replayed from the ledger; each result is cached under prompts/<sha256>.txt)."""
    store, render = loop_module("store_loop"), loop_module("render")
    path = ctx.base_prompt_path
    text = _read_prompt(path)
    for g in sorted(store.get_generations(ctx.conn, ctx.loop_id), key=lambda g: g.generation):
        if g.state != "GEN_END" or g.terminal != "PROMOTED" or g.pick is None:
            continue
        pick = next((c for c in store.get_candidates(ctx.conn, ctx.loop_id, g.generation)
                     if c.candidate_id == g.pick), None)
        if pick is None:
            raise ValueError(f"generation {g.generation} was PROMOTED but its pick {g.pick!r} is not in the ledger")
        text = render.apply_patch(text, pick.patch)
        path = rel_path(_store_prompt(text))
    return Production(path=path, text=text, hash=_sha(text))


def injected_truth(cfg: LoopConfig, result: BotTestResult, pick: Candidate, generation: int) -> tuple[float, float]:
    """(primary lift, arm-B rubric_risk shift) injected into the simulated A/B (§L9.4)."""
    sim = cfg.sim
    if sim.effect_mode == "scenario_sequence":
        if not sim.scenario_sequence:
            raise ValueError("sim.effect_mode is scenario_sequence but sim.scenario_sequence is empty")
        return sim.scenario_sequence[(generation - 1) % len(sim.scenario_sequence)], 0.0
    lift = sim.lift_per_score_point * (pick.bot_test_score or 0.0)
    return max(-sim.max_abs_lift, min(sim.max_abs_lift, lift)), result.risk_delta.get(pick.candidate_id, 0.0)


# ---------------------------------------------------------------------------------------
# One generation
# ---------------------------------------------------------------------------------------

@dataclass
class _Run:
    """Everything one generation accumulates: the persisted row, plus what its report needs."""
    ctx: LoopContext
    gen: Generation
    prod: Production
    spend0: float
    notes: list[tuple[str, str]] = field(default_factory=list)       # (step, one sentence) for the report
    diagnosis: pd.DataFrame | None = None
    candidates: dict[str, Candidate] = field(default_factory=dict)   # latest version of each, in proposal order
    bot: BotTestResult | None = None
    live: LiveAbResult | None = None
    truth: tuple[float, float] | None = None

    def advance(self, state: str, **update) -> Generation:
        """Move to `state` and persist. Validated, so a state outside GenState fails here, not in the DB."""
        store = loop_module("store_loop")
        spend = max(0.0, store.total_spend(self.ctx.conn) - self.spend0)
        self.gen = Generation.model_validate({**self.gen.model_dump(), "state": state, "spend_inr": spend,
                                              "updated_ts": datetime.now(), **update})
        store.put_generation(self.ctx.conn, self.gen)
        _say(self.ctx, f"  gen {self.gen.generation}: {state}")
        return self.gen

    def save(self, c: Candidate, status: str | None = None) -> Candidate:
        if status is not None:
            c = c.model_copy(update={"status": status})
        self.candidates[c.candidate_id] = c
        loop_module("store_loop").put_candidate(self.ctx.conn, c, self.ctx.loop_id)
        return c

    def note(self, step: str, text: str) -> None:
        self.notes.append((step, text))


def _observe(run: _Run) -> pd.DataFrame:
    """Scores of the production prompt: reuse stored ones when there are at least as many leads as
    seed personas, else observe it with base-only bot tests (a crashed run can leave a single stored
    conversation, too few to diagnose from; the base conversations are cached, so topping up is cheap)."""
    ctx, store = run.ctx, loop_module("store_loop")
    scores = store.get_scores(ctx.conn, run.gen.rubric_hash, prompt_hash=run.prod.hash)
    if scores.empty or scores["lead_id"].nunique() < len(ctx.personas):
        _set_context(ctx, run.gen.generation, "observe")
        bot_tests = loop_module("bot_tests")
        bot_tests.run_bot_tests(run.prod.text, [], ctx.personas, ctx.rubric, ctx.provider, ctx.cfg,
                                run.gen.generation, ctx.conn)
        scores = store.get_scores(ctx.conn, run.gen.rubric_hash, prompt_hash=run.prod.hash)
        run.note("OBSERVED", f"Observed the production prompt with base-only bot-test conversations "
                             f"({len(scores)} scored).")
    else:
        run.note("OBSERVED", f"Reused {len(scores)} stored scores of the production prompt.")
    if scores.empty:
        raise RuntimeError("observing the production prompt produced no scores")
    return scores


def _diagnose(run: _Run, scores: pd.DataFrame) -> dict[str, list[str]]:
    ctx, store, dx = run.ctx, loop_module("store_loop"), loop_module("diagnose")
    run.diagnosis = dx.diagnose(scores, ctx.rubric, ctx.cfg.diagnose.top_k)
    targets = [str(t) for t in run.diagnosis.loc[run.diagnosis["target"].astype(bool), "criterion_id"]]
    transcripts = store.get_transcripts(ctx.conn, call_ids=scores["call_id"].tolist())
    run.note("DIAGNOSED", f"Targets: {', '.join(targets) or 'none'}.")
    return {cid: dx.excerpts(transcripts, scores, cid) for cid in targets}


def _eligible(c: Candidate) -> bool:
    return c.status == "proposed" and all(c.prechecks.values())


def _estimate_ok(run: _Run, n_prompts: int) -> bool:
    """The §L2.6 pre-flight: refuse a bot-test run whose estimate exceeds what the cap has left."""
    ctx = run.ctx
    if getattr(ctx.provider, "name", "") == "stub":
        return True
    prompt_tokens = len(run.prod.text) // CHARS_PER_TOKEN
    est = loop_module("bot_tests").estimate_cost(len(ctx.personas), n_prompts, ctx.cfg.bot_tests.max_turns,
                                                 prompt_tokens, ctx.prices)
    left = ctx.cfg.caps.max_spend_inr - loop_module("store_loop").total_spend(ctx.conn)
    run.note("BOT_TESTED", f"Estimated bot-test cost INR {est:.2f}; INR {left:.2f} left under the cap.")
    _say(ctx, f"  gen {run.gen.generation}: bot tests will cost about INR {est:.2f} (INR {left:.2f} left)")
    return est <= left


def _no_candidate(run: _Run, why: str) -> None:
    run.note("NO_CANDIDATE", why)
    run.advance("NO_CANDIDATE", terminal="NO_CANDIDATE")


def _generation_steps(run: _Run) -> None:
    ctx = run.ctx
    cfg, g = ctx.cfg, run.gen.generation

    scores = _observe(run)
    run.advance("OBSERVED")

    run.note("AUDIT_PENDING", "Skipped: the audit gate applies to going live on real traffic (PLAN_LOOP §L2.2, "
                              "§L9); this build only simulates the live A/B, so an untrusted rubric may rank "
                              "candidates here.")
    run.advance("AUDIT_PENDING")

    _set_context(ctx, g, "diagnose")
    excerpts = _diagnose(run, scores)
    run.advance("DIAGNOSED")

    _set_context(ctx, g, "propose")
    ledger = loop_module("ledger")
    proposed = loop_module("proposer").propose(
        run.prod.text, run.diagnosis, excerpts, ctx.rubric, ledger.ledger_summary(ctx.conn, ctx.loop_id),
        ledger.tried_hashes(ctx.conn, ctx.loop_id), cfg.propose.candidates, g, ctx.provider, cfg)
    for c in proposed:
        run.save(c)  # failed pre-checks too: the ledger records them
    run.note("PROPOSED", f"{len(proposed)} proposed, {sum(map(_eligible, proposed))} passed the pre-checks.")
    run.advance("PROPOSED")
    survivors = [c for c in proposed if _eligible(c)]
    if not survivors:
        return _no_candidate(run, "No proposed candidate passed the pre-checks.")

    _set_context(ctx, g, "gate")
    gate = loop_module("gate")
    gated = [run.save(gate.gate(run.prod.text, c, ctx.provider, cfg)) for c in survivors]
    survivors = [c for c in gated if c.status != "gate_blocked"]
    run.note("GATED", f"{len(survivors)} of {len(gated)} passed the patch gate.")
    run.advance("GATED")
    if not survivors:
        return _no_candidate(run, "Every candidate was blocked by the patch gate.")

    if not _estimate_ok(run, len(survivors) + 1):
        raise _SpendRefused("the estimated bot-test cost exceeds the spend left under the cap")
    _set_context(ctx, g, "bot_tests")
    bot_tests = loop_module("bot_tests")
    result = bot_tests.run_bot_tests(run.prod.text, survivors, ctx.personas, ctx.rubric, ctx.provider, cfg, g,
                                     ctx.conn)
    run.bot = result
    for c in result.candidates:
        run.save(c, "picked" if c.candidate_id == result.pick else "not_picked")
    for c in survivors:  # gated but not scored (cannot happen with a sound bot_tests; kept honest)
        if c.candidate_id not in {x.candidate_id for x in result.candidates}:
            run.save(c, "not_picked")
    run.note("BOT_TESTED", result.reason)
    run.advance("BOT_TESTED", pick=result.pick)
    if result.pick is None:
        return _no_candidate(run, "Bot tests picked no candidate, so nothing goes to the A/B.")
    pick = run.candidates[result.pick]

    run.note("DOGFOOD", "Skipped in sim mode: there is no live surface, and real calls need consent and "
                        "numbers (PLAN_LOOP §L2.7, §L9).")
    run.advance("DOGFOOD")

    lift, shift = injected_truth(cfg, result, pick, g)
    run.truth = (lift, shift)
    run.save(pick, "live")
    run.advance("LIVE_AB")
    live = loop_module("live_ab").run_live_ab(ctx.conn, cfg, run.prod.path, pick, g, lift, shift,
                                              cfg.sim.seed + g, quiet=ctx.quiet)
    run.live = live
    terminal = str(live.terminal)
    # The generation mirrors the experiment's terminal state; one that is not a generation state
    # (e.g. PENDING_APPROVAL, which needs a human) leaves the generation at LIVE_AB with that terminal.
    run.advance(terminal if terminal in GEN_STATES and terminal != "GEN_END" else "LIVE_AB",
                experiment_id=live.experiment_id, terminal=terminal)

    if terminal == "PROMOTED":
        new = _store_prompt(loop_module("render").apply_patch(run.prod.text, pick.patch))
        run.save(pick, "promoted")
        run.note("ROLL OUT", f"Promoted: the resolved B prompt is {rel_path(new)}; the next generation starts from it.")
    else:
        run.save(pick, "not_promoted")
        run.note("ROLL OUT", f"Not promoted ({terminal}); production stays on this generation's prompt.")


def _finish(run: _Run, terminal: str | None = None) -> Generation:
    run.advance("GEN_END", terminal=terminal if terminal is not None else run.gen.terminal)
    path = write_report(run)
    _say(run.ctx, f"  gen {run.gen.generation}: report {rel_path(path)}")
    return run.gen


def run_generation(ctx: LoopContext, generation: int) -> Generation:
    """Run one generation to GEN_END. A spend-cap stop ends it with terminal SPEND_CAP instead of raising."""
    if ctx.cfg.loop.mode != "sim":
        raise ValueError("loop.mode: live is not supported in this build: the dogfood and dialer steps "
                         "(PLAN_LOOP §L2.4, §L2.7) place real calls and are not built; use mode: sim")
    if not ctx.personas:
        raise ValueError("no persona cards: the bot tests need at least one (configs_loop/personas_seed.yaml)")
    store = loop_module("store_loop")
    prod = production_prompt(ctx)
    now = datetime.now()
    gen = Generation(generation=generation, loop_id=ctx.loop_id, state="GEN_START", base_prompt_hash=prod.hash,
                     rubric_hash=loop_module("rubric").rubric_hash(ctx.rubric), started_ts=now, updated_ts=now)
    store.put_generation(ctx.conn, gen)
    run = _Run(ctx=ctx, gen=gen, prod=prod, spend0=store.total_spend(ctx.conn))
    _say(ctx, f"generation {generation}: production prompt {prod.hash[:12]}")
    try:
        _generation_steps(run)
    except (spend_cap_exception(), _SpendRefused) as e:
        run.note("SPEND_CAP", f"Stopped at the spend cap: {e}")
        return _finish(run, SPEND_CAP)
    return _finish(run)


# ---------------------------------------------------------------------------------------
# Report (aggregates only)
# ---------------------------------------------------------------------------------------

def _num(x, spec: str = ".3f") -> str:
    return "n/a" if x is None or (isinstance(x, float) and x != x) else format(x, spec)


def _ci(ci, spec: str = "+.3f") -> str:
    return "n/a" if ci is None else f"[{format(ci[0], spec)}, {format(ci[1], spec)}]"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|",
            *("| " + " | ".join(r) + " |" for r in rows)]


def _patch_chars(c: Candidate) -> int:
    p = c.patch
    return len(getattr(p, "replace", None) or getattr(p, "text", None) or "")


def _pre(c: Candidate) -> str:
    failed = sorted(k for k, ok in c.prechecks.items() if not ok)
    return "ok" if not failed else "failed: " + ", ".join(failed)


def write_report(run: _Run) -> Path:
    """reports/loop/generation_<loop id>_<n>.md: tables and numbers, no transcript text, no rationale."""
    ctx, g, cfg = run.ctx, run.gen, run.ctx.cfg
    total = loop_module("store_loop").total_spend(ctx.conn)
    out = [f"# Generation {g.generation} of loop `{g.loop_id}`", "",
           f"Final state {g.state}, outcome **{g.terminal or 'none'}**. Mode `{cfg.loop.mode}`: the A/B runs on "
           f"simulated traffic. Provider `{getattr(ctx.provider, 'name', '?')}`. "
           f"Production prompt `{g.base_prompt_hash[:12]}`, rubric `{g.rubric_hash[:12]}`.", "",
           "## Steps", ""]
    out += _table(["Step", "What happened"], [[s, t.replace("|", "/")] for s, t in run.notes])

    out += ["", "## Diagnosis", ""]
    if run.diagnosis is None:
        out.append("Not reached.")
    else:
        out += _table(["Rank", "Criterion", "Failure rate", "95% CI", "Weight", "Weighted cost", "Leads", "Target"],
                      [[str(int(r.rank)), str(r.criterion_id), _num(r.failure_rate),
                        f"[{_num(r.ci_low)}, {_num(r.ci_high)}]", _num(r.weight, ".1f"),
                        _num(r.weighted_cost), str(int(r.n_leads)), "yes" if r.target else ""]
                       for r in run.diagnosis.itertuples()])

    out += ["", "## Candidates", ""]
    if not run.candidates:
        out.append("None proposed.")
    else:
        out += _table(["Candidate", "Mode", "Chars", "Targets", "Pre-checks", "Gate (p per question)",
                       "Bot-test score", "95% CI", "Status"],
                      [[c.candidate_id, c.patch.mode, str(_patch_chars(c)), ", ".join(c.targets) or "-", _pre(c),
                        ", ".join(f"{k}={v:.2f}" for k, v in (c.gate or {}).items()) or "-",
                        _num(c.bot_test_score, "+.3f"), _ci(c.bot_test_ci), c.status]
                       for c in run.candidates.values()])
        out += ["", f"Pick: {f'`{g.pick}`' if g.pick else 'none'}." + (f" {run.bot.reason}" if run.bot else "")]

    out += ["", "## Live A/B (simulated traffic)", ""]
    if run.live is None:
        out.append("Not run.")
    else:
        v = run.live
        out += _table(["Item", "Value"], [
            ["Experiment", f"`{v.experiment_id}`"], ["Terminal state", v.terminal], ["Looks", str(v.looks)],
            ["Primary lift (relative)", _num(v.lift_rel, "+.1%")], ["Lift 95% CI (absolute)", _ci(v.lift_ci95)],
            ["rubric_risk delta (B - A)", _num(v.rubric_delta, "+.3f")], ["rubric_risk RCI", _ci(v.rubric_rci)],
            ["Decided by", v.decided_by], ["Reason", v.reason.replace("|", "/")]])
    if run.truth is not None:
        out += ["", f"Simulated ground truth, injected and never used by the engine: primary lift "
                    f"{run.truth[0]:+.1%}, arm-B rubric_risk shift {run.truth[1]:+.3f} "
                    f"(effect mode `{cfg.sim.effect_mode}`). The A/B is simulated traffic: it makes no claim "
                    f"about real business impact."]

    out += ["", "## Spend", "",
            f"This generation INR {g.spend_inr:.2f}; loop total INR {total:.2f} of the INR "
            f"{cfg.caps.max_spend_inr:.2f} cap.", ""]
    # Demo runs regenerate their reports every time, so they go to the ignored data folder.
    base = LOOP_DATA_DIR / "reports" if g.loop_id.endswith("-demo") else LOOP_REPORTS_DIR
    path = base / f"generation_{g.loop_id}_{g.generation}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------------------

def _stop_reason(ctx: LoopContext, finished: list[Generation], n: int) -> str | None:
    if is_paused():
        return f"paused (ABT_LOOP_PAUSE=1 or the {rel_path(pause_flag())} file)"
    if n > ctx.cfg.caps.max_generations:
        return f"caps.max_generations ({ctx.cfg.caps.max_generations}) reached"
    tail = 0
    for g in reversed(finished):
        if g.terminal != "NO_CANDIDATE":
            break
        tail += 1
    if tail >= NO_CANDIDATE_STOP:
        return f"{tail} consecutive NO_CANDIDATE generations"
    spent = loop_module("store_loop").total_spend(ctx.conn)
    if spent >= ctx.cfg.caps.max_spend_inr:
        return f"spend cap reached (INR {spent:.2f} of {ctx.cfg.caps.max_spend_inr:.2f})"
    return None


def run_until_stop(ctx: LoopContext, generations: int) -> tuple[list[Generation], str]:
    """run_loop plus the reason it stopped. A generation left unfinished by a crash is re-run, not skipped."""
    store, ran = loop_module("store_loop"), []
    for _ in range(generations):
        stored = sorted(store.get_generations(ctx.conn, ctx.loop_id), key=lambda g: g.generation)
        resume = bool(stored) and stored[-1].state != "GEN_END"
        n = stored[-1].generation if resume else (stored[-1].generation if stored else 0) + 1
        if reason := _stop_reason(ctx, [g for g in stored if g.generation < n], n):
            return ran, reason
        ran.append(run_generation(ctx, n))
        if ran[-1].terminal == SPEND_CAP:
            return ran, "spend cap reached"
    return ran, f"ran the {generations} requested generation(s)"


def run_loop(ctx: LoopContext, generations: int) -> list[Generation]:
    """Run up to `generations` generations after the last stored one. Stops early on the spend cap,
    three consecutive NO_CANDIDATE, caps.max_generations or the pause flag."""
    return run_until_stop(ctx, generations)[0]
