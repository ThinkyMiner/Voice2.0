"""Validation proof pack (PLAN.md §2.11) on a synthetic pool: nothing here needs data/.

The config is made cheap by asking for a large MDE (small n_max), so a few hundred runs take
seconds. Statistical checks are loose on purpose: with a few hundred runs a rate has a binomial
SE of ~1-2 points, so the tests catch gross errors (a broken boundary, a missing look, a naive
path that is not naive), and `make validate` is where the 2,000-run precision lives.
"""

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
import yaml

from abtest import rules, validation
from abtest.contracts import CONFIGS_DIR, ExperimentConfig, State
from abtest.validation import (
    WIN_STATES,
    PoolInfo,
    ValidationResults,
    build_pool,
    checks,
    load_pool,
    rate_ci,
    simulate_runs,
    unit_calls,
    validate,
    write_report,
)

N_RUNS = 300
SEED = 20261009


def _headline() -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        yaml.safe_load((CONFIGS_DIR / "experiments" / "vani-meeting-v1.yaml").read_text()))


def _cheap(cfg: ExperimentConfig, **primary) -> ExperimentConfig:
    """A big MDE keeps n_max in the low thousands; auto promotion so wins are PROMOTED."""
    p = cfg.primary.model_copy(update={"mde_rel": 0.6, **primary})
    return cfg.model_copy(update={"primary": p, "promotion": "auto"})


def _pool(cfg: ExperimentConfig, m: int = 20_000, sigma: float = 0.1, seed: int = 5) -> pd.DataFrame:
    """Exactly baseline_p successes; the secondary is mildly higher on successes. A small sigma
    keeps the guardrail RCI inside the margin, so the guardrail does not hide the primary test."""
    rng = np.random.default_rng(seed)
    primary = np.zeros(m, dtype="int8")
    primary[: round(m * cfg.baseline_p)] = 1
    rng.shuffle(primary)
    secondary = rng.lognormal(np.where(primary == 1, 4.7, 4.5), sigma)
    return pd.DataFrame({"primary": primary, "secondary": secondary})


def _info(pool: pd.DataFrame) -> PoolInfo:
    return PoolInfo(source="synthetic", n_calls=len(pool), n_blocks=3, window_days=28,
                    n_units=len(pool), n_mature=len(pool), n_dropped_no_secondary=0,
                    p=float(pool.primary.mean()))


@pytest.fixture(scope="module")
def cfg():
    return _cheap(_headline())


@pytest.fixture(scope="module")
def pool(cfg):
    return _pool(cfg)


@pytest.fixture(scope="module")
def results(cfg, pool) -> ValidationResults:
    return validate(cfg, pool, _info(pool), N_RUNS, SEED)


# --- effect injection (PLAN §2.5 at lead level) ---------------------------------------------

def _inject(prim, sec, p, rel_lift, better, pool_p, pool_s, seed=1):
    validation._inject(np.random.default_rng(seed), prim, sec, pool_p, pool_s, p, rel_lift, better)


def test_injection_moves_the_rate_from_p_to_p_times_one_plus_l_and_redraws_the_secondary():
    # Secondary 100 on successes and 1 on failures makes a flipped lead's redraw visible.
    n, p, lift = 400_000, 0.2, 0.25
    pool_p = np.array([1] * 20 + [0] * 80, dtype="int8")
    pool_s = np.where(pool_p == 1, 100.0, 1.0)
    rng = np.random.default_rng(3)
    idx = rng.integers(0, 100, n)
    prim, sec = pool_p[idx].copy(), pool_s[idx].copy()
    _inject(prim, sec, p, lift, "increase", pool_p, pool_s)
    assert prim.mean() == pytest.approx(p * (1 + lift), abs=0.003)
    assert (sec == np.where(prim == 1, 100.0, 1.0)).all()  # secondary always matches the primary


def test_injection_decrease_and_negative_lift_both_lower_the_rate():
    pool_p = np.array([1] * 30 + [0] * 70, dtype="int8")
    pool_s = np.where(pool_p == 1, 100.0, 1.0)
    idx = np.random.default_rng(4).integers(0, 100, 400_000)
    for lift, better in ((0.3, "decrease"), (-0.3, "increase")):  # both: B's rate falls by 30%
        prim, sec = pool_p[idx].copy(), pool_s[idx].copy()
        _inject(prim, sec, 0.3, lift, better, pool_p, pool_s)
        assert prim.mean() == pytest.approx(0.3 * 0.7, abs=0.003), (lift, better)
        assert (sec == np.where(prim == 1, 100.0, 1.0)).all()
    for lift, better in ((0.3, "increase"), (-0.3, "decrease")):  # both: B's rate rises by 30%
        prim, sec = pool_p[idx].copy(), pool_s[idx].copy()
        _inject(prim, sec, 0.3, lift, better, pool_p, pool_s)
        assert prim.mean() == pytest.approx(0.3 * 1.3, abs=0.003), (lift, better)


def test_infeasible_lift_is_an_error():
    pool_p = np.array([1] * 60 + [0] * 40, dtype="int8")
    with pytest.raises(ValueError, match="infeasible"):
        _inject(pool_p.copy(), np.ones(100), 0.6, 0.9, "increase", pool_p, np.ones(100))  # p_B = 1.14
    with pytest.raises(ValueError, match="exceeds 100%"):
        _inject(pool_p.copy(), np.ones(100), 0.6, 1.5, "decrease", pool_p, np.ones(100))


def test_realised_lift_is_reported_near_the_injected_lift(results):
    lift = results.rel_lift
    assert validation.realised_lift(results.power, "increase") == pytest.approx(lift, abs=0.02)
    assert validation.realised_lift(results.power, "decrease") == pytest.approx(-lift, abs=0.02)
    assert validation.realised_lift(results.aa, "increase") == 0.0  # A/A injects nothing


# --- the engine's behaviour under the harness ------------------------------------------------

def test_aa_false_win_rate_is_about_alpha_and_naive_peeking_is_clearly_higher(results, cfg):
    aa = results.aa
    wins = aa.decision.isin(WIN_STATES).sum()
    _, lo, _ = rate_ci(wins, len(aa))
    assert lo <= cfg.alpha_win, f"sequential false-win {wins}/{len(aa)} is significantly above alpha"
    naive = aa.naive_decision.isin(WIN_STATES).sum()
    _, nlo, _ = rate_ci(naive, len(aa))
    assert nlo > cfg.alpha_win                   # the naive CI excludes the nominal rate
    assert naive > 2 * max(wins, 1)              # and it is well above the sequential rate


def test_aa_harm_stops_stay_near_alpha_harm(results, cfg):
    harm = results.aa.decision.eq(State.STOPPED_HARM).sum()
    _, lo, _ = rate_ci(harm, len(results.aa))
    assert lo <= cfg.alpha_harm


def test_power_runs_promote_at_about_the_configured_power(results, cfg):
    rate = results.power.decision.isin(WIN_STATES).mean()
    assert rate == pytest.approx(cfg.power, abs=0.09)   # ~4 binomial SEs at 300 runs
    assert rate > results.aa.decision.isin(WIN_STATES).mean() + 0.5


def test_primary_only_crossing_is_at_least_the_win_rate(results):
    for df in (results.aa, results.power):
        assert (~df.decision.isin(WIN_STATES) | df.crossed_primary_seq).all()
        assert (~df.naive_decision.isin(WIN_STATES) | df.crossed_primary_naive).all()


def test_a_worse_b_is_stopped_for_harm_early(cfg, pool):
    df = simulate_runs(cfg, pool, 40, 11, -0.8)
    assert df.decision.eq(State.STOPPED_HARM).mean() > 0.9
    assert df.stop_look.median() <= 3
    assert not df.decision.isin(WIN_STATES).any()


def test_a_guardrail_that_cannot_clear_blocks_wins_at_the_final_look(cfg, pool):
    # A 0.5% margin is far inside any RCI at these sample sizes: primary wins cannot be cleared.
    tight = cfg.model_copy(update={"secondary": cfg.secondary.model_copy(update={"margin_rel": 0.005})})
    df = simulate_runs(tight, pool, 40, 13, cfg.primary.mde_rel)
    blocked = df[df.decision == State.BLOCKED_GUARDRAIL]
    # (Some runs end in STOPPED_HARM instead: B's secondary is slightly higher in this pool, and
    # an RCI entirely above a 0.5% margin is guardrail harm, rule 2.)
    assert not df.decision.isin(WIN_STATES).any() and len(blocked) / len(df) > 0.3
    assert (blocked.stop_look == len(validation._plan(tight).planned)).all()  # only blocked at the end
    assert (blocked.rule == 4).all()


# --- run bookkeeping -------------------------------------------------------------------------

def test_same_seed_gives_identical_results_and_other_seeds_differ(cfg, pool):
    a = simulate_runs(cfg, pool, 12, 7, 0.0)
    pd.testing.assert_frame_equal(a, simulate_runs(cfg, pool, 12, 7, 0.0))
    other = simulate_runs(cfg, pool, 12, 8, 0.0)
    assert not a[["z1", "z2"]].equals(other[["z1", "z2"]])


def test_run_i_does_not_depend_on_how_many_runs_there_are(cfg, pool):
    pd.testing.assert_frame_equal(simulate_runs(cfg, pool, 5, 7, 0.0),
                                  simulate_runs(cfg, pool, 12, 7, 0.0).head(5))


def test_aa_and_power_use_independent_seed_streams(results):
    assert not results.aa[["z1", "z2"]].equals(results.power[["z1", "z2"]])


def test_stop_at_first_non_continue_and_each_path_runs_to_its_own_decision(cfg, pool, monkeypatch):
    """Record every evaluate_look call: each path is evaluated at looks 1..its stop look and then
    never again, and the loop keeps going for the other path until it too has stopped."""
    plan = validation._plan(cfg)
    calls: list[tuple[str, int, str]] = []
    real = rules.evaluate_look

    def spy(look, bounds, c):
        v = real(look, bounds, c)
        calls.append(("seq" if bounds is plan.bounds or bounds == plan.bounds else "naive",
                      look.look, str(v.decision)))
        return v

    monkeypatch.setattr(validation.rules, "evaluate_look", spy)
    df = simulate_runs(cfg, pool, 25, 21, cfg.primary.mde_rel)
    monkeypatch.undo()

    per_run, current = [], None
    for path, k, decision in calls:      # a new run starts whenever look 1 follows a later look
        if path == "seq" and k == 1:
            current = {"seq": [], "naive": []}
            per_run.append(current)
        current[path].append((k, decision))
    assert len(per_run) == len(df)
    for row, log in zip(df.itertuples(), per_run):
        for path, stop in (("seq", row.stop_look), ("naive", row.naive_stop_look)):
            looks = [k for k, _ in log[path]]
            assert looks == list(range(1, stop + 1)), (row.run, path)
            assert all(d == "CONTINUE" for _, d in log[path][:-1])
            assert log[path][-1][1] != "CONTINUE"
        assert row.n_looks == max(row.stop_look, row.naive_stop_look)
        z = [getattr(row, f"z{k}") for k in range(1, plan.k + 1)]
        assert not any(math.isnan(v) for v in z[:row.n_looks])
        assert all(math.isnan(v) for v in z[row.n_looks:])
    # Both orders occur, so neither path truncated the other.
    assert (df.stop_look > df.naive_stop_look).any() or (df.stop_look < df.naive_stop_look).any()


def test_the_final_look_always_decides_and_decisions_are_states(results, cfg):
    k = len(validation._plan(cfg).planned)
    for df in (results.aa, results.power):
        assert df.stop_look.between(1, k).all() and df.naive_stop_look.between(1, k).all()
        assert set(df.decision) <= {s.value for s in State}
        assert set(df.naive_decision) <= {s.value for s in State}
        assert (df.reason.str.len() > 20).all()
        assert df[df.stop_look == k].decision.isin(
            {State.INCONCLUSIVE, State.BLOCKED_GUARDRAIL, State.PROMOTED, State.STOPPED_HARM,
             State.PENDING_APPROVAL, State.HALTED_SRM}).all()


def test_nested_looks_use_prefixes_of_one_sequence(cfg):
    plan = validation._plan(cfg)
    assert all(a <= b for a, b in zip(plan.take_a, plan.take_a[1:]))
    assert all(a <= b for a, b in zip(plan.take_b, plan.take_b[1:]))
    assert plan.take_a[-1] == plan.n_a and plan.take_b[-1] == plan.n_b
    assert plan.naive.win == (plan.naive.win[0],) * plan.k
    assert plan.naive.harm == (plan.naive.harm[0],) * plan.k
    from scipy.stats import norm
    assert plan.naive.win[0] == pytest.approx(norm.ppf(1 - cfg.alpha_win))
    assert plan.naive.harm[0] == pytest.approx(norm.ppf(1 - cfg.alpha_harm))
    assert plan.bounds.win[0] > plan.naive.win[0] and plan.bounds.win[-1] < plan.bounds.win[0]


# --- checks and report -----------------------------------------------------------------------

def _frames(cfg, aa_wins, aa_harm, naive_wins, power_wins, n=2000):
    def frame(wins, harm, naive):
        d = np.full(n, State.INCONCLUSIVE.value, dtype=object)
        d[:wins] = State.PROMOTED.value
        d[wins:wins + harm] = State.STOPPED_HARM.value
        nd = np.full(n, State.INCONCLUSIVE.value, dtype=object)
        nd[:naive] = State.PROMOTED.value
        return pd.DataFrame({"decision": d, "naive_decision": nd, "stop_look": 7, "naive_stop_look": 7,
                             "rule": 5, "naive_rule": 5, "crossed_primary_seq": d == "PROMOTED",
                             "crossed_primary_naive": nd == "PROMOTED", "rate_B_cf": 0.2,
                             "rate_B": 0.2 * (1 + cfg.primary.mde_rel),
                             "reason": "x" * 30, **{f"z{k}": 0.0 for k in range(1, 8)}})
    return frame(aa_wins, aa_harm, naive_wins), frame(power_wins, 0, power_wins)


def _hand_results(cfg, pool, **counts) -> ValidationResults:
    aa, pw = _frames(cfg, **counts)
    return ValidationResults(cfg=cfg, seed=1, n_runs=2000, pool=_info(pool), plan=validation._plan(cfg),
                             rel_lift=cfg.primary.mde_rel, aa=aa, power=pw,
                             seconds={"aa": 1.0, "power": 1.0, "total": 2.0})


def test_checks_pass_on_nominal_numbers(cfg, pool):
    res = _hand_results(cfg, pool, aa_wins=50, aa_harm=90, naive_wins=300, power_wins=1600)
    assert [c.passed for c in checks(res)] == [True, True, True, True]


def test_checks_fail_each_target_when_violated(cfg, pool):
    ok = dict(aa_wins=50, aa_harm=90, naive_wins=300, power_wins=1600)
    cases = {"aa_wins": 120, "naive_wins": 40, "aa_harm": 200, "power_wins": 1000}
    for name, bad in cases.items():
        res = _hand_results(cfg, pool, **{**ok, name: bad})
        failed = [c.name for c in checks(res) if not c.passed]
        assert len(failed) == 1, (name, failed)
    # A false-win rate BELOW alpha passes (the CI need not contain it); power 5 pp off is the edge.
    assert checks(_hand_results(cfg, pool, **{**ok, "aa_wins": 5}))[0].passed
    assert not checks(_hand_results(cfg, pool, **{**ok, "power_wins": 1380}))[3].passed  # 69%
    assert checks(_hand_results(cfg, pool, **{**ok, "power_wins": 1700}))[3].passed       # 85%


def test_report_is_written_with_the_key_lines_and_charts(results, cfg, tmp_path):
    path = write_report(results, tmp_path)
    assert path == tmp_path / "validation.md"
    text = path.read_text()
    for needle in ("# Validation proof pack: vani-meeting-v1", "## Verdict", "PASS: A/A false-win rate",
                   "Naive peeking inflates", "A/A false-harm-stop rate", "Power at the MDE",
                   "## Bootstrap pool", "pool p (primary rate)", "baseline p in config",
                   "## Planned looks and boundaries", "naive win z", "## A/A runs",
                   "primary-only win crossing", "## Power runs", "realised lift",
                   "## Stop look by terminal state", "runtime", "95% CI"):
        assert needle in text or needle.lower() in text.lower(), needle
    verdicts = [line for line in text.splitlines() if line.startswith(("- PASS", "- FAIL"))]
    assert len(verdicts) == 4
    n_b, n_a = results.plan.n_b, results.plan.n_a
    assert f"{n_b:,} / {n_a:,}" in text
    for png in ("validation_false_win.png", "validation_stop_looks.png"):
        if (tmp_path / png).exists():
            assert (tmp_path / png).stat().st_size > 5_000 and png in text
        else:
            assert "PNG export failed" in text   # kaleido without Chrome: noted, not a crash


def test_a_failed_png_export_is_noted_in_the_report_not_raised(results, tmp_path, monkeypatch):
    import plotly.graph_objects as go

    def boom(self, *a, **k):
        raise RuntimeError("Kaleido requires Google Chrome to be installed.")

    monkeypatch.setattr(go.Figure, "write_image", boom)
    path = write_report(results, tmp_path)
    text = path.read_text()
    assert "PNG export failed" in text and "Chrome" in text
    assert not list(tmp_path.glob("*.png"))


# --- pool from real-shaped calls -------------------------------------------------------------

def _calls(rows) -> pd.DataFrame:
    t0 = datetime(2026, 4, 1)
    df = pd.DataFrame(
        [{"call_id": f"c{i}", "lead_id": lead, "ts": t0 + timedelta(days=day, hours=hour),
          "answered": ans, "disposition": disp, "duration_s": dur, "attempt_no": 1, "bot_version": "x"}
         for i, (lead, day, hour, ans, disp, dur) in enumerate(rows)])
    return df.sort_values(["lead_id", "ts", "call_id"], ignore_index=True)


def _window_cfg(cfg) -> ExperimentConfig:
    """7-day blocks and a 1-day maturation so a 15-day synthetic dataset has 2 full blocks."""
    return cfg.model_copy(update={"window": cfg.window.model_copy(update={"days": 7}),
                                  "maturation_days": 1.0})


MF, GEN = "Meeting Fixed", "General"
POOL_CALLS = [
    ("1", 0, 10, True, MF, 100.0), ("1", 8, 10, True, GEN, 50.0),          # 1|0 success, 1|1 not
    ("2", 2, 9, False, "Not Answered", np.nan), ("2", 3, 9, True, GEN, 60.0),
    ("2", 4, 9, True, MF, 80.0),                                           # 2|0 success, mean 70
    ("3", 6, 23, True, GEN, 40.0), ("3", 7, 1, True, MF, 90.0),            # 3|0 not, 3|1 success
    ("4", 14, 9, True, MF, 70.0),                                          # trailing partial block
    ("5", 13, 18, True, GEN, 30.0),                                        # trigger < 1 d before end
    ("6", 1, 9, False, "Not Answered", np.nan),                            # never answers
]


def test_unit_calls_make_one_unit_per_lead_per_window_block(cfg):
    calls, exposures = unit_calls(_calls(POOL_CALLS), _window_cfg(cfg))
    # Lead 4 is only in the partial trailing block, which is dropped.
    assert set(calls.lead_id) == {"1|0", "1|1", "2|0", "3|0", "3|1", "5|1", "6|0"}
    first = {u: ts for u, ts in zip(exposures.lead_id, exposures.ts)}
    assert set(exposures.variant) == {"A"} and len(exposures) == 7
    assert first["2|0"] == datetime(2026, 4, 3, 9)          # the unit's FIRST call, answered or not
    assert first["3|1"] == datetime(2026, 4, 8, 1)
    assert (exposures.experiment_id == cfg.id).all()


def test_blocks_start_at_midnight_of_the_first_day(cfg):
    # Day 6 23:59 is in block 0 and day 7 00:00 in block 1, whatever the time of day of the first call.
    rows = [("a", 0, 15, True, GEN, 1.0), ("b", 6, 23, True, GEN, 1.0), ("c", 7, 0, True, GEN, 1.0),
            ("d", 13, 23, True, GEN, 1.0), ("e", 14, 1, True, GEN, 1.0)]
    calls, _ = unit_calls(_calls(rows), _window_cfg(cfg))
    assert set(calls.lead_id) == {"a|0", "b|0", "c|1", "d|1"}


def test_data_shorter_than_one_block_is_one_block(cfg):
    calls, _ = unit_calls(_calls([("a", 0, 9, True, GEN, 5.0), ("a", 3, 9, True, MF, 7.0)]), cfg)
    assert set(calls.lead_id) == {"a|0"}


def test_build_pool_reuses_lead_outcomes_and_keeps_mature_triggered_units(cfg, monkeypatch):
    seen = []
    real = validation.metrics.lead_outcomes

    def spy(calls, exposures, c, look_ts):
        seen.append((len(calls), len(exposures), look_ts))
        return real(calls, exposures, c, look_ts)

    monkeypatch.setattr(validation.metrics, "lead_outcomes", spy)
    pool, info = build_pool(_calls(POOL_CALLS), _window_cfg(cfg))
    assert len(seen) == 1 and seen[0][1] == 7 and seen[0][2] == datetime(2026, 4, 14, 18)
    got = sorted(zip(pool.primary.tolist(), pool.secondary.round(6).tolist()))
    assert got == sorted([(1, 100.0), (0, 50.0), (1, 70.0), (0, 40.0), (1, 90.0)])
    assert info.n_units == 7 and info.n_mature == 5 and info.p == pytest.approx(3 / 5)
    assert info.n_dropped_no_secondary == 0


def test_units_without_a_defined_secondary_are_dropped_and_counted(cfg):
    rows = [("a", 0, 9, True, MF, np.nan), ("b", 0, 9, True, GEN, 10.0), ("c", 0, 9, True, MF, 20.0),
            ("z", 5, 9, False, "Not Answered", np.nan)]   # a late call so the day-0 leads are mature
    pool, info = build_pool(_calls(rows), _window_cfg(cfg))
    assert len(pool) == 2 and info.n_dropped_no_secondary == 1 and info.n_mature == 3
    assert info.n_units == 4 and sorted(pool.primary) == [0, 1]


def test_missing_data_raises_naming_the_expected_path(cfg, tmp_path):
    with pytest.raises(FileNotFoundError) as e:
        load_pool(cfg, tmp_path)
    assert str(tmp_path / "Best-Time-to-Call - Call Attempts Apr-Sep 2026.csv") in str(e.value)


# --- CLI -------------------------------------------------------------------------------------

def test_cli_prints_the_summary_and_writes_the_report(cfg, pool, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(validation, "load_pool", lambda c: (pool, _info(pool)))
    code = validation.main([str(CONFIGS_DIR / "experiments" / "vani-meeting-v1.yaml"), "--runs", "8",
                            "--seed", "3", "--out-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code in (0, 1)
    assert str(tmp_path / "validation.md") in out and "runtime" in out
    assert sum(line.startswith(("  PASS", "  FAIL")) for line in out.splitlines()) == 4
    assert (tmp_path / "validation.md").exists()


def test_cli_without_data_exits_2_naming_the_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(validation.data.FILES, "bttc", "definitely-missing.csv")  # even if data/ exists
    exp = str(CONFIGS_DIR / "experiments" / "vani-meeting-v1.yaml")
    code = validation.main([exp, "--out-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2 and "definitely-missing.csv" in err and "ABTEST_DATA_DIR" in err
    assert not (tmp_path / "validation.md").exists()


def test_defaults_match_the_plan():
    assert validation.DEFAULT_RUNS == 2000
    assert validation.DEFAULT_EXP.name == "vani-meeting-v1.yaml" and validation.DEFAULT_EXP.exists()
