"""Runner tests (PLAN.md §2.5 Clock, §2.7 information fraction, §2.8, Phase 4).

CI-mode tests swap the Phase 3 stats chain, the simulator and decision.py for small fakes that
honour their contracts, so the runner's own logic (clock, look schedule, stopping, engine-path
purity, summaries, sweep) is tested without data/ or the modules still being written.
"""

import functools
import json
import math
import types
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from abtest import config, contracts, router, runner, srm, store
from abtest.contracts import (DATA_DIR, LIVE_ALLOCATION, Boundaries, Decision, ExperimentConfig,
                              GuardrailLog, LeadOutcome, LookResult, Pool, PrimaryLog, PrimaryStats,
                              Scenario, SecondaryStats, SimConfig, SimDay, SimTruth, State, Verdict)

START = datetime(2026, 10, 1)
EXP = "configs/experiments/vani-meeting-v1.yaml"
WIN, HARM = 3.0, 2.0  # fake constant boundaries
B_RATE, A_RATE = 0.3, 0.2  # fake primary success rates
FOLLOW_UP = pd.Timedelta(days=3)  # fake follow-up call lag
_db = store.connect(":memory:")
STORE_COLUMNS = {t: [r[1] for r in _db.execute(f"PRAGMA table_info({t})")] for t in ("exposures", "calls")}
OUTCOME_COLUMNS = list(LeadOutcome.model_fields)


def scenario(name="inconclusive", seed=1, days=10, looks=5):
    return Scenario(name=name, experiment=EXP, sim="configs/sims/win.yaml", seed=seed,
                    overrides={"window": {"days": days, "looks": looks}, "maturation_days": 1})


# --- fakes ----------------------------------------------------------------------------------

def fake_simulator(leads_per_day, log):
    def build_pool(cfg, sim):
        log.args.append(("build_pool", cfg, sim))
        return Pool(calls=pd.DataFrame(), leads=pd.DataFrame(), start=START, p=A_RATE, secondary_base=60.0)

    def simulate_day(pool, cfg, sim, state, day, seed):
        """leads_per_day new leads, routed on the live allocation, each with one answered call
        that day and an unanswered follow-up FOLLOW_UP later (written up front, as the real one)."""
        log.days.append(day)
        rng = np.random.default_rng((seed, day))
        ids = [f"sim-{seed}-{day * leads_per_day + i}" for i in range(leads_per_day)]
        buckets = [router.bucket(state.salt, i) for i in ids]
        variant = np.array([router.variant_for(b, state.allocation) for b in buckets])
        t0 = pd.Timestamp(pool.start + timedelta(days=day, hours=1))
        exposures = pd.DataFrame({"experiment_id": cfg.id, "lead_id": ids, "variant": variant,
                                  "bucket": buckets, "ts": t0})
        if day == 0:  # a DRAFT-period exposure, before the experiment started
            pre = exposures.iloc[:1].assign(lead_id="pre-start", ts=pd.Timestamp(pool.start - timedelta(hours=1)))
            exposures = pd.concat([pre, exposures], ignore_index=True)
        win = rng.random(leads_per_day) < np.where(variant == "B", B_RATE, A_RATE)
        calls = pd.DataFrame({
            "experiment_id": cfg.id, "call_id": [f"{i}-0" for i in ids], "lead_id": ids,
            "ts": t0 + pd.Timedelta(hours=2), "answered": True,
            "disposition": np.where(win, cfg.primary.dispositions[0], "Not Interested"),
            "duration_s": rng.uniform(30, 90, leads_per_day), "attempt_no": 1})
        later = calls.assign(call_id=calls.lead_id + "-1", ts=calls.ts + FOLLOW_UP, answered=False,
                             disposition="Not Answered", duration_s=np.nan, attempt_no=2)
        calls = pd.concat([calls, later], ignore_index=True)
        return SimDay(exposures=exposures, calls=calls, truth=pd.DataFrame({"lead_id": ids, "variant": variant}))

    def truth_summary(truth, pool, cfg, sim, scenario, seed):
        return types.SimpleNamespace(model_dump_json=lambda: json.dumps({"leads": len(truth)}))

    return types.SimpleNamespace(build_pool=build_pool, simulate_day=simulate_day,
                                 truth_summary=truth_summary, write_truth_report=log.reports.append)


def fake_decision(n_b_max, log):
    """decision.py contract semantics, minus the JSONL mirror."""
    ns = types.SimpleNamespace(JSONL="sentinel.jsonl")

    def start(conn, frozen, ts_sim):
        log.args.append(("start", conn, frozen, ts_sim))
        cfg = frozen.config
        s = store.get_state(conn, cfg.id).model_copy(update={
            "state": State.RUNNING, "allocation": cfg.allocation, "n_b_max": n_b_max,
            "started_ts_sim": ts_sim, "updated_ts_sim": ts_sim})
        store.put_state(conn, s)
        return s

    def apply_verdict(conn, state, look, bounds, verdict):
        log.args.append(("apply_verdict", conn, state, look, bounds, verdict))
        assert state.state == State.RUNNING and look.look == state.looks_done + 1
        log.jsonl.append(ns.JSONL)
        new = state.state if verdict.decision == "CONTINUE" else verdict.decision
        s = state.model_copy(update={"state": new, "looks_done": look.look,
                                     "info_fractions": bounds.info_fractions, "updated_ts_sim": look.ts_sim,
                                     "allocation": LIVE_ALLOCATION[new] or state.allocation})
        p, g = look.primary, look.secondary
        row = Decision(
            ts_wall=datetime.now(), ts_sim=look.ts_sim, experiment_id=s.experiment_id,
            config_hash=s.config_hash, prompt_hash_A=s.prompt_hash["A"], prompt_hash_B=s.prompt_hash["B"],
            kind="look", look=look.look, info_fraction=look.info_fraction, n_A=look.n_A, n_B=look.n_B,
            primary=PrimaryLog(rate_A=p.rate_A, rate_B=p.rate_B, lift_abs=p.lift_abs, lift_rel=p.lift_rel,
                               ci95=p.ci95, z=p.z, win_boundary=bounds.win[-1], harm_boundary=-bounds.harm[-1]),
            guardrail=GuardrailLog(mean_A=g.mean_A, mean_B=g.mean_B, rel_change=g.rel_change,
                                   delta=g.delta, rci=g.rci, margin=g.margin),
            srm=None, decision=str(verdict.decision), reason=verdict.reason, actor="engine")
        store.record(conn, s, [row])
        return s

    def weekly_impact(lift_abs, n_matured, elapsed_days, cfg):
        return lift_abs * n_matured / max(elapsed_days - cfg.maturation_days, 1) * 7

    ns.start, ns.apply_verdict, ns.weekly_impact = start, apply_verdict, weekly_impact
    return ns


def until_final(look, bounds, cfg):
    if look.is_final:
        return Verdict(decision=State.INCONCLUSIVE, rule=5, reason="Final look, nothing crossed.")
    return Verdict(decision="CONTINUE", rule=0, reason="No boundary crossed.")


def stop_at(k, state):
    def evaluate(look, bounds, cfg):
        if look.look == k:
            return Verdict(decision=state, rule=2, reason=f"Scripted {state} at look {k}.")
        return until_final(look, bounds, cfg)
    return evaluate


def fake_engine(log, evaluate):
    def lead_outcomes(calls, exposures, cfg, look_ts):
        log.args.append(("lead_outcomes", calls, exposures, cfg, look_ts))
        calls = calls[calls.ts <= look_ts]
        ans = calls[calls.answered]
        first = exposures.sort_values("ts").drop_duplicates("lead_id")
        out = pd.DataFrame({"lead_id": first.lead_id, "variant": first.variant, "assigned_ts": first.ts})
        out["triggered_ts"] = out.lead_id.map(ans.groupby("lead_id").ts.min())
        out["mature"] = out.triggered_ts + pd.Timedelta(days=cfg.maturation_days) <= look_ts  # NaT -> False
        prim = ans.disposition.isin(cfg.primary.dispositions).astype(int).groupby(ans.lead_id).max()
        out["primary"] = out.lead_id.map(prim).astype("Int64")
        out["secondary"] = out.lead_id.map(ans.groupby("lead_id").duration_s.mean())
        out["n_calls"] = out.lead_id.map(calls.groupby("lead_id").size()).fillna(0).astype(int)
        out["n_answered"] = out.lead_id.map(ans.groupby("lead_id").size()).fillna(0).astype(int)
        log.matured_b.append(int((out.variant.eq("B") & out.mature).sum()))
        return out[OUTCOME_COLUMNS].reset_index(drop=True)

    def planned(cfg):  # boundaries.planned_info_fractions, written independently
        k = cfg.window.looks
        return tuple(float(x) for x in np.linspace(max(cfg.min_info_first_look, 1 / k), 1.0, k))

    def compute_boundaries(info_fractions, cfg):
        log.args.append(("compute_boundaries", info_fractions, cfg))
        log.fractions.append(tuple(info_fractions))
        k = len(info_fractions)
        return Boundaries(info_fractions=tuple(info_fractions), win=(WIN,) * k, harm=(HARM,) * k)

    def analyze_look(outcomes, cfg, look, ts_sim, bounds, is_final):
        log.args.append(("analyze_look", outcomes, cfg, look, ts_sim, bounds, is_final))
        m = outcomes[outcomes.mature]
        a, b = m[m.variant == "A"], m[m.variant == "B"]
        ra, rb = float(a.primary.mean()), float(b.primary.mean())
        se = math.sqrt(ra * (1 - ra) / len(a) + rb * (1 - rb) / len(b))
        d, sa, sb = rb - ra, float(a.secondary.mean()), float(b.secondary.mean())
        rel = sb / sa - 1
        sr = srm.srm_test(len(a), len(b), cfg.allocation.B)
        return LookResult(
            experiment_id=cfg.id, look=look, ts_sim=ts_sim, is_final=is_final,
            info_fraction=bounds.info_fractions[-1], n_A=len(a), n_B=len(b),
            primary=PrimaryStats(rate_A=ra, rate_B=rb, lift_abs=d, lift_rel=d / ra,
                                 ci95=(d - 1.96 * se, d + 1.96 * se), se_pooled=se, se_unpooled=se,
                                 z=d / se, detectable_mde_rel=None),
            secondary=SecondaryStats(name=cfg.secondary.name, type=cfg.secondary.type, role=cfg.secondary.role,
                                     mean_A=sa, mean_B=sb, rel_change=rel, delta=rel, delta_type="relative",
                                     se=0.01, ci95=(rel - 0.0196, rel + 0.0196),
                                     rci=(rel - 0.01 * WIN, rel + 0.01 * WIN), margin=cfg.secondary.margin),
            srm_assigned=sr, srm_triggered=sr)

    def evaluate_look(look, bounds, cfg):
        log.args.append(("evaluate_look", look, bounds, cfg))
        log.looks.append(look)
        return evaluate(look, bounds, cfg)

    return (types.SimpleNamespace(lead_outcomes=lead_outcomes), types.SimpleNamespace(analyze_look=analyze_look),
            types.SimpleNamespace(compute_boundaries=compute_boundaries, planned_info_fractions=planned),
            types.SimpleNamespace(evaluate_look=evaluate_look))


@pytest.fixture
def fakes(monkeypatch):
    log = types.SimpleNamespace(args=[], days=[], reports=[], fractions=[], looks=[], matured_b=[], jsonl=[])

    def install(leads_per_day=200, n_b_max=95.0, evaluate=until_final):
        monkeypatch.setattr(runner, "simulator", fake_simulator(leads_per_day, log))
        monkeypatch.setattr(runner, "decision", fake_decision(n_b_max, log))
        monkeypatch.setattr(runner, "_engine", lambda: fake_engine(log, evaluate))
        monkeypatch.setattr(runner, "_POOLS", {})
        log.n_b_max = n_b_max
        return log
    return install


def _day(ts_sim):
    return (ts_sim - START).days - 1  # 0-based sim day of a look


# --- scheduling ---------------------------------------------------------------------------

@pytest.mark.parametrize("leads_per_day, n_b_max, ends", [
    (200, 95.0, "t>=1"),     # t grows ~0.2 a day: reaches t >= 1 mid-window
    (200, 400.0, "window"),  # ~0.05 a day: the window ends at t < 1
    (1000, 230.0, "skip"),   # ~0.4 a day: skips planned points, reaches t >= 1
])
def test_look_schedule(fakes, leads_per_day, n_b_max, ends):
    log = fakes(leads_per_day, n_b_max)
    days = 10
    sc = scenario(days=days)
    cfg = config.scenario_experiment(sc)
    K = cfg.window.looks
    planned = np.linspace(max(cfg.min_info_first_look, 1 / K), 1.0, K)
    first_tau = planned[0]
    state = runner.run_scenario(sc, store.connect(":memory:"))

    t = [m / n_b_max for m in log.matured_b]  # observed fraction after each simulated day
    look_days = [_day(look.ts_sim) for look in log.looks]
    fracs = log.fractions[-1]
    assert [look.look for look in log.looks] == list(range(1, len(log.looks) + 1)) and len(fracs) == len(log.looks)
    assert all(f[:-1] == prev for prev, f in zip(log.fractions, log.fractions[1:]))  # one new t per look
    assert all(a < b for a, b in zip(fracs, fracs[1:]))                              # strictly increasing
    assert len(set(look_days)) == len(look_days)                                     # <= one look per day
    assert t[look_days[0]] >= first_tau and max(t[:look_days[0]], default=0) < first_tau
    assert state.state == State.INCONCLUSIVE and log.looks[-1].is_final
    assert not any(look.is_final for look in log.looks[:-1])
    assert log.days == list(range(look_days[-1] + 1))  # nothing simulated after the final look
    # no planned point is skipped on a day that reached it; passed points are not looked at again
    taus = [first_tau] + [min((p for p in planned if p > f), default=math.inf) for f in fracs[:-1]]
    for d in range(look_days[-1]):
        due = taus[sum(ld < d for ld in look_days)]
        assert (d in look_days) == (t[d] >= due)
    if ends == "window":
        assert look_days[-1] == days - 1 and fracs[-1] == t[-1] < 1
    else:
        # the observed fraction, uncapped: boundaries.py clips the spending at t = 1 itself
        assert fracs[-1] == t[look_days[-1]] > 1 and look_days[-1] < days - 1
    if ends == "skip":
        assert len(log.looks) < K


@pytest.mark.parametrize("decided", [State.STOPPED_HARM, State.PENDING_APPROVAL])
def test_stops_right_after_a_decision(fakes, decided):
    log = fakes(evaluate=stop_at(2, decided))
    conn = store.connect(":memory:")
    state = runner.run_scenario(scenario(), conn)
    assert state.state == decided and state.looks_done == 2
    assert log.days[-1] == _day(log.looks[-1].ts_sim)  # no further day simulated
    assert store.get_state(conn, state.experiment_id) == state
    assert conn.execute("SELECT COUNT(*) FROM sim_truth").fetchone()[0] == 1 and len(log.reports) == 1


def test_clock_advances_on_days_without_a_look(fakes):
    fakes(n_b_max=1000.0)
    conn = store.connect(":memory:")
    state = runner.run_scenario(scenario(days=6), conn)
    assert state.updated_ts_sim == START + timedelta(days=6)
    assert store.get_state(conn, state.experiment_id).updated_ts_sim == state.updated_ts_sim


# --- engine-path purity (PLAN.md §2.3) ----------------------------------------------------

def test_engine_sees_only_events_aggregates_and_the_experiment_config(fakes):
    log = fakes()
    conn = store.connect(":memory:")
    runner.run_scenario(scenario(), conn)
    engine = [a for a in log.args if a[0] != "build_pool"]
    assert {a[0] for a in engine} == {"start", "lead_outcomes", "compute_boundaries", "analyze_look",
                                      "evaluate_look", "apply_verdict"}
    for name, *args in engine:
        for arg in args:
            assert not isinstance(arg, (SimConfig, Pool, SimTruth, types.SimpleNamespace)), name
        cfgs = [a for a in args if isinstance(a, ExperimentConfig)]
        assert all(c.id == "vani-meeting-v1-inconclusive" for c in cfgs)
    for _, calls, exposures, cfg, look_ts in (a for a in engine if a[0] == "lead_outcomes"):
        assert list(calls.columns) == STORE_COLUMNS["calls"] and calls.answered.dtype == bool
        assert list(exposures.columns) == STORE_COLUMNS["exposures"]
        assert calls.ts.max() <= look_ts  # no call from the future reaches the engine
    for a in engine:
        if a[0] == "analyze_look":
            assert list(a[1].columns) == OUTCOME_COLUMNS
    # the simulator, and only it, got the sim config
    assert isinstance(next(a for a in log.args if a[0] == "build_pool")[2], SimConfig)


def test_exposures_before_start_are_excluded(fakes):
    log = fakes()
    conn = store.connect(":memory:")
    state = runner.run_scenario(scenario(), conn)
    seen = [a[2] for a in log.args if a[0] == "lead_outcomes"]
    assert all(e.ts.min() >= state.started_ts_sim and "pre-start" not in set(e.lead_id) for e in seen)
    assert conn.execute("SELECT COUNT(*) FROM exposures WHERE lead_id = 'pre-start'").fetchone()[0] == 1


# --- live mode, determinism ---------------------------------------------------------------

def test_speed_sleeps_once_per_sim_day(fakes, monkeypatch):
    log = fakes()
    sleeps = []
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    runner.run_scenario(scenario(), store.connect(":memory:"), speed=0.25)
    assert sleeps == [0.25] * len(log.days)
    runner.run_scenario(scenario(), store.connect(":memory:"))
    assert len(sleeps) < len(log.days)  # no speed, no sleep


def test_same_scenario_and_seed_give_identical_decisions(fakes):
    fakes(evaluate=stop_at(3, State.STOPPED_HARM))
    runs = []
    for _ in range(2):
        conn = store.connect(":memory:")
        state = runner.run_scenario(scenario(seed=5), conn)
        runs.append(([d.model_dump(exclude={"ts_wall"}) for d in store.read_decisions(conn, state.experiment_id)],
                     store.read_df(conn, "SELECT * FROM calls ORDER BY call_id")))
    (d1, c1), (d2, c2) = runs
    assert d1 == d2 and len(d1) == 3 and c1.equals(c2)


# --- summarize, sweep ---------------------------------------------------------------------

@pytest.mark.parametrize("name, evaluate, hit", [
    ("harm", stop_at(2, State.STOPPED_HARM), True),
    ("harm", stop_at(4, State.STOPPED_HARM), False),   # too late: harm must stop by look 3
    ("win", until_final, False),                        # wrong state
    ("inconclusive", until_final, True),
])
def test_summarize(fakes, name, evaluate, hit):
    fakes(evaluate=evaluate)
    conn = store.connect(":memory:")
    sc = scenario(name)
    state = runner.run_scenario(sc, conn)
    r = runner.summarize(conn, sc)
    last = [d for d in store.read_decisions(conn, state.experiment_id) if d.kind == "look"][-1]
    p = last.primary
    assert (r.state, r.expected, r.hit) == (state.state, runner.EXPECTED[name][0], hit)
    assert r.stop_look == state.looks_done and r.info_fraction == last.info_fraction
    assert r.lift_rel_ci95 == (p.ci95[0] / p.rate_A, p.ci95[1] / p.rate_A) and r.lift_rel == p.lift_rel
    assert (r.guardrail_delta, r.guardrail_rci) == (last.guardrail.delta, last.guardrail.rci)
    elapsed = (last.ts_sim - START).days
    assert r.weekly_impact == pytest.approx(p.lift_abs * (last.n_A + last.n_B) / max(elapsed - 1, 1) * 7)
    assert r.sim_days == elapsed


def test_summarize_without_a_look(fakes):
    fakes()
    conn = store.connect(":memory:")
    sc = scenario("harm")
    frozen = config.freeze(config.scenario_experiment(sc))
    store.create_experiment(conn, frozen, sc.name, sc.seed)
    runner.decision.start(conn, frozen, START)
    r = runner.summarize(conn, sc)
    assert r.state == State.RUNNING and not r.hit and r.sim_days == 0
    assert all(getattr(r, f) is None for f in runner.LOOK_FIELDS)


def test_sweep_in_process_is_sorted_and_restores_jsonl(fakes):
    log = fakes()
    runs = runner.sweep(["win", "harm"], 2, workers=1)
    assert [(r.scenario, r.seed) for r in runs] == [("harm", 1), ("harm", 2), ("win", 1), ("win", 2)]
    assert all(r.experiment_id == f"vani-meeting-v1-{r.scenario}" for r in runs)
    assert log.jsonl and set(log.jsonl) == {None}  # JSONL mirror off during the sweep
    assert runner.decision.JSONL == "sentinel.jsonl" and log.reports == []  # restored; no truth report
    assert sum(a[0] == "build_pool" for a in log.args) == 2  # one pool per (experiment, sim, overrides)


def test_sweep_restores_jsonl_on_error(fakes, monkeypatch):
    fakes()
    monkeypatch.setattr(runner.simulator, "build_pool", lambda cfg, sim: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        runner.sweep(["win"], 1, workers=1)
    assert runner.decision.JSONL == "sentinel.jsonl"


def test_scenario_table_formats_business_columns(fakes):
    fakes(evaluate=stop_at(2, State.STOPPED_HARM))
    conn = store.connect(":memory:")
    sc = config.load_scenario("harm")
    runner.run_scenario(sc, conn)
    r = runner.summarize(conn, sc)
    row = runner.scenario_table([r], [r, r.model_copy(update={"hit": False})], 2).splitlines()[-1]
    assert row.startswith(f"| harm (seed {sc.seed}) | STOPPED_HARM | 2 (day {r.sim_days}) | {r.lift_rel:+.1%} (")
    assert row.endswith(f"| {r.weekly_impact:+,.0f} | 1/2 |")
    miss = runner.scenario_table([r.model_copy(update={"hit": False})], [], 2)
    assert "MISS: expected STOPPED_HARM" in miss


def test_cli_simulate_and_sweep(fakes, monkeypatch, tmp_path, capsys):
    fakes(evaluate=stop_at(2, State.STOPPED_HARM))
    monkeypatch.setattr(contracts, "DB_PATH", tmp_path / "abtest.db")
    monkeypatch.setattr(runner, "REPORTS_DIR", tmp_path)
    runner.main(["harm"])
    out = capsys.readouterr().out
    assert "look 2" in out and "final state STOPPED_HARM" in out and "Scripted STOPPED_HARM" in out
    assert "a week if B served 100% of traffic" in out

    runner.main(["--sweep", "--seeds", "2", "--workers", "1", "inconclusive", "harm"])
    md = (tmp_path / "scenarios.md").read_text()
    harm, inconclusive = (config.load_scenario(n).seed for n in ("harm", "inconclusive"))
    assert f"| harm (seed {harm}) | STOPPED_HARM | 2 (day " in md and md.count("| 2/2 |") == 1
    assert f"| inconclusive (seed {inconclusive}) | **STOPPED_HARM (MISS: expected INCONCLUSIVE)** |" in md
    assert "| 0/2 |" in md and md in capsys.readouterr().out
    conn = store.connect(tmp_path / "abtest.db")  # the pinned demo runs are in the main store
    assert conn.execute("SELECT COUNT(*) FROM experiment_state").fetchone()[0] == 2


# --- end to end on the real data ----------------------------------------------------------

@pytest.mark.data
@pytest.mark.skipif(not (DATA_DIR / "Best-Time-to-Call - Call Attempts Apr-Sep 2026.csv").exists(),
                    reason="data not present")
def test_win_end_to_end(tmp_path, monkeypatch):
    for m in ("metrics", "analysis", "boundaries", "rules", "decision"):
        pytest.importorskip(f"abtest.{m}")
    from abtest import decision, simulator
    monkeypatch.setattr(decision, "JSONL", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(runner, "decision", decision)
    monkeypatch.setattr(simulator, "write_truth_report",
                        functools.partial(simulator.write_truth_report, out_dir=tmp_path))
    conn = store.connect(tmp_path / "e2e.db")
    state = runner.run_scenario(config.load_scenario("win").model_copy(update={"seed": 7}), conn)
    assert state.state != State.RUNNING
    looks = [d.look for d in store.read_decisions(conn, state.experiment_id) if d.kind == "look"]
    assert looks == list(range(1, state.looks_done + 1))
