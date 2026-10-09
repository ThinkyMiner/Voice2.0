"""Simulator tests (PLAN.md §2.5). Real-data tests skip when the data files are absent.

Sampling tolerances are about 4 standard errors of the tested quantity at the sample size used,
so each check fails by chance with probability below 1e-4 (and runs are seeded, so they are
deterministic anyway).
"""

import math
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from abtest import router, simulator, store
from abtest.config import load_experiment, load_sim
from abtest.contracts import (DATA_DIR, Allocation, ExperimentState, Fault, Reference, SimConfig,
                              State, TrueEffect, Volume, Window)
from abtest.data import FILES, load
from abtest.profile import volume_stats
from abtest.simulator import (build_pool, reference_calls, simulate_day, truth_summary,
                              write_truth_report)

EXP = "configs/experiments/vani-meeting-v1.yaml"
BUYER_EXP = "configs/experiments/vani-buyer-qty-asks-v1.yaml"
WIN_SIM = "configs/sims/win.yaml"


def _need(dataset):
    if not (DATA_DIR / FILES[dataset]).exists():
        pytest.skip(f"{FILES[dataset]} not in {DATA_DIR}")


def _state(cfg, b=0.1):
    return ExperimentState(experiment_id=cfg.id, state=State.RUNNING, allocation=Allocation(A=1 - b, B=b),
                           salt=cfg.salt, config_hash="test", prompt_hash={"A": "a", "B": "b"})


def _sim(sim, L=0.0, c=0.0, scale=None, drop=0.0):
    upd = {"true_effect": TrueEffect(primary_rel_lift=L, secondary_change=c),
           "fault": Fault(drop_b_exposure_rate=drop)}
    if scale is not None:
        upd["volume"] = Volume(daily_leads=sim.volume.daily_leads, scale=scale)
    return sim.model_copy(update=upd)


def _pick(pool, out, seed, day):
    """Pool lead of each new lead: simulate_day's first draw from its rng."""
    return np.random.default_rng((seed, day)).integers(0, len(pool.leads), len(out.truth))


def _trig_b(out):
    t = out.truth
    return t[t.triggered & t.variant.eq("B")]


@pytest.fixture(scope="module")
def bttc():
    _need("bttc")
    cfg, sim = load_experiment(EXP), load_sim(WIN_SIM)
    return cfg, sim, build_pool(cfg, sim)


@pytest.fixture(scope="module")
def buyer():
    _need("buyer_logs")
    cfg = load_experiment(BUYER_EXP)
    df, _ = load(cfg.dataset, DATA_DIR, cfg.exclude_leads_with_calls_gt)
    sim = SimConfig(true_effect=TrueEffect(primary_rel_lift=0.0), reference=Reference(),
                    volume=Volume(daily_leads=volume_stats(df, cfg.window.days).daily_leads, scale=100))
    return cfg, sim, build_pool(cfg, sim)


# --- pool -----------------------------------------------------------------------------------

def test_pool_lead_windows_synthetic(tmp_path):
    rows = [  # call_id, lead, bot, attempt, ts, status, duration, disposition
        (1, 11, "main_vani", 1, "2026-04-01 10:00:00", "NotAnswered", 0, "Not Answered"),
        (2, 11, "main_vani", 2, "2026-04-02 11:00:00", "Answered", 60, "Meeting Fixed"),
        (3, 11, "main_vani", 1, "2026-04-03 09:00:00", "Answered", 10, "Not Interested"),
        (4, 22, "main_vani", 1, "2026-04-02 23:30:00", "Answered", 20, "General (talked)"),
        (5, 22, "main_vani", 2, "2026-04-05 08:00:00", "Answered", 70, "Meeting Fixed"),  # partial block
        (6, 33, "arrowhead", 1, "2026-04-04 12:00:00", "NotAnswered", 0, "Not Answered"),
    ]
    cols = ["data_hotlead_disposition_dtlid", "fk_glusr_usr_id", "lead_bot_version", "call_attempt_count",
            "call_start_time", "lead_call_status", "lead_call_duration", "disposition_label"]
    pd.DataFrame(rows, columns=cols).to_csv(tmp_path / FILES["bttc"], index=False)
    cfg = load_experiment(EXP).model_copy(update={"window": Window(days=2, looks=1)})
    sim = load_sim(WIN_SIM).model_copy(update={"reference": Reference()})

    pool = build_pool(cfg, sim, tmp_path)
    # blocks Apr 1-2 and Apr 3-4; Apr 5 is the dropped partial block
    assert list(zip(pool.leads.lead_id, pool.leads.block)) == [("11", 0), ("11", 1), ("22", 0), ("33", 1)]
    assert pool.leads.n_calls.tolist() == [2, 1, 1, 1]
    assert pool.leads.tod.tolist() == [pd.Timedelta(hours=10), pd.Timedelta(hours=9),
                                       pd.Timedelta(hours=23, minutes=30), pd.Timedelta(hours=12)]
    assert pool.calls.offset.tolist() == [pd.Timedelta(0), pd.Timedelta(hours=25), pd.Timedelta(0), pd.Timedelta(0), pd.Timedelta(0)]
    assert pool.leads.triggered.tolist() == [True, True, True, False]
    assert pool.p == pytest.approx(1 / 3) and pool.secondary_base == pytest.approx((60 + 10 + 20) / 3)
    assert pool.start == datetime(2026, 4, 6)  # midnight after the last filtered day
    df = reference_calls(load("bttc", tmp_path)[0], sim)
    assert len(pool.leads) / (2 * 2) == volume_stats(df, 2).daily_leads

    only_main = sim.model_copy(update={"reference": Reference(bot_version=("main_vani",), to=datetime(2026, 4, 4).date())})
    pool = build_pool(cfg, only_main, tmp_path)  # filtered span Apr 1-3: one block, Apr 3 dropped
    assert pool.leads.lead_id.tolist() == ["11", "22"] and pool.start == datetime(2026, 4, 4)


@pytest.mark.data
def test_volume_matches_profile(bttc):
    cfg, sim, pool = bttc
    W = cfg.window.days
    daily = volume_stats(reference_calls(load(cfg.dataset)[0], sim), W).daily_leads
    assert pool.leads.groupby("block").size().mean() / W == pytest.approx(daily, rel=1e-12)
    assert sim.volume.daily_leads == pytest.approx(daily, abs=0.05)  # the config was measured this way
    assert pool.start == datetime(2026, 10, 1) and 0 < pool.p < 1 and pool.secondary_base > 0


# --- generation and routing -------------------------------------------------------------------

@pytest.mark.data
def test_store_columns_and_insert(bttc):
    cfg, sim, pool = bttc
    out = simulate_day(pool, cfg, sim, _state(cfg), 0, 1)
    conn = store.connect(":memory:")
    for table, df in (("exposures", out.exposures), ("calls", out.calls)):
        assert list(df.columns) == [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert store.insert_df(conn, table, df) == len(df)
    assert list(out.truth.columns) == ["lead_id", "variant", "exposure_logged", "triggered", "primary_cf",
                                       "primary", "secondary_cf", "secondary", "injected_primary",
                                       "injected_secondary"]
    assert out.calls.call_id.is_unique


@pytest.mark.data
def test_deterministic(bttc):
    cfg, sim, pool = bttc
    a, b = (simulate_day(pool, cfg, sim, _state(cfg), 3, 1) for _ in range(2))
    for f in ("exposures", "calls", "truth"):
        pd.testing.assert_frame_equal(getattr(a, f), getattr(b, f))
    c = simulate_day(pool, cfg, sim, _state(cfg), 3, 2)
    assert not a.calls[["ts", "disposition"]].equals(c.calls[["ts", "disposition"]])


@pytest.mark.data
def test_daily_counts_and_ids(bttc):
    cfg, sim, pool = bttc
    W, R, seed = cfg.window.days, 3.65 * 2.0, 5
    small = sim.model_copy(update={"volume": Volume(daily_leads=3.65, scale=2.0)})
    ids = []
    for d in range(W):
        t = simulate_day(pool, cfg, small, _state(cfg), d, seed).truth
        assert len(t) == math.floor((d + 1) * R) - math.floor(d * R)
        ids += t.lead_id.tolist()
    assert ids == [f"sim-{seed}-{n}" for n in range(math.floor(W * R))]
    t = simulate_day(pool, cfg, sim, _state(cfg), 1, seed).truth
    R = sim.volume.daily_leads * sim.volume.scale
    assert t.lead_id.iat[0] == f"sim-{seed}-{math.floor(R)}" and len(t) == math.floor(2 * R) - math.floor(R)


@pytest.mark.data
def test_routing_uses_router(bttc):
    cfg, sim, pool = bttc
    st = _state(cfg)
    days = [simulate_day(pool, cfg, sim, st, d, 9) for d in range(3)]
    t = pd.concat([o.truth for o in days], ignore_index=True)
    e = pd.concat([o.exposures for o in days], ignore_index=True)
    routed = [router.route(st, i) for i in t.lead_id]
    assert t.variant.tolist() == [a.variant for a in routed]
    assert e.lead_id.tolist() == t.lead_id.tolist()  # no fault: every exposure logged
    assert e.variant.tolist() == t.variant.tolist() and e.bucket.tolist() == [a.bucket for a in routed]
    share, se = t.variant.eq("B").mean(), math.sqrt(0.1 * 0.9 / len(t))
    assert abs(share - 0.1) < 4 * se


@pytest.mark.data
def test_arm_a_replays_pool_lead_window(bttc):
    cfg, sim, pool = bttc
    seed, day = 4, 0  # day 0: a lead-window (< W days from its first call) is never truncated
    out = simulate_day(pool, cfg, sim, _state(cfg), day, seed)
    pick = _pick(pool, out, seed, day)
    day_start = pd.Timestamp(pool.start) + pd.Timedelta(days=day)
    cols = ["answered", "disposition", "duration_s", "attempt_no"]
    for j in np.flatnonzero(out.truth.variant.eq("A").to_numpy())[:200]:
        pl = pool.leads.iloc[pick[j]]
        hist = pool.calls.iloc[pl.row: pl.row + pl.n_calls].reset_index(drop=True)
        got = out.calls[out.calls.lead_id == out.truth.lead_id.iat[j]].reset_index(drop=True)
        assert got.ts.iat[0] == day_start + pl.tod
        assert (got.ts - got.ts.iat[0]).tolist() == hist.offset.tolist()
        pd.testing.assert_frame_equal(got[cols], hist[cols])


@pytest.mark.data
def test_window_end(bttc):
    cfg, sim, pool = bttc
    W, seed = cfg.window.days, 6
    end = pd.Timestamp(pool.start) + pd.Timedelta(days=W)
    for day in (W - 3, W - 1):
        out = simulate_day(pool, cfg, sim, _state(cfg), day, seed)
        assert (out.calls.ts < end).all()
        first = out.calls.groupby("lead_id").ts.min().reindex(out.exposures.lead_id)
        lo = pd.Timestamp(pool.start) + pd.Timedelta(days=day)
        assert first.between(lo, lo + pd.Timedelta(days=1), inclusive="left").all()
        assert (first.to_numpy() == out.exposures.ts.to_numpy()).all()
        assert len(out.calls) < pool.leads.n_calls.to_numpy()[_pick(pool, out, seed, day)].sum()  # truncated
    with pytest.raises(ValueError):
        simulate_day(pool, cfg, sim, _state(cfg), W, seed)


# --- injection ----------------------------------------------------------------------------
# bttc runs use B = 100% and scale 10: about 8,900 triggered B leads on day 0, cf rate p ~ 0.18.

@pytest.mark.data
def test_positive_lift(bttc):
    cfg, sim, pool = bttc
    out = simulate_day(pool, cfg, _sim(sim, L=0.25, scale=10), _state(cfg, 1.0), 0, 11)
    t = _trig_b(out)
    # SD of the ratio ~0.013 (conversion draws ~0.011, cf-rate noise ~0.007); 0.05 is ~4 SD
    assert t.primary.mean() / t.primary_cf.mean() == pytest.approx(1.25, abs=0.05)
    inj = t[t.injected_primary]
    assert (inj.primary_cf == 0).all() and (inj.primary == 1).all()
    assert (t.primary[~t.injected_primary] == t.primary_cf[~t.injected_primary]).all()
    mf = cfg.primary.dispositions[0]
    conv = out.calls[out.calls.lead_id.isin(inj.lead_id) & out.calls.disposition.eq(mf)]
    pool_mf = np.unique(pool.calls.duration_s[pool.calls.answered & pool.calls.disposition.eq(mf)].dropna())
    # B durations are also scaled so the total secondary change stays at c = 0 (Checkpoint 2)
    mult = 1 / (1 + simulator._from_primary(pool, cfg, 0.25, simulator._q(pool, 0.25)))
    raw = conv.duration_s.dropna().to_numpy() / mult
    assert len(conv) == len(inj) and np.abs(raw[:, None] - pool_mf[None, :]).min(axis=1).max() < 1e-6


@pytest.mark.data
def test_negative_lift(bttc):
    cfg, sim, pool = bttc
    out = simulate_day(pool, cfg, _sim(sim, L=-0.30, scale=10), _state(cfg, 1.0), 0, 12)
    t = _trig_b(out)
    # ~1,600 success leads each lose success w.p. 0.3: SD of the ratio ~0.011; 0.05 is ~4.5 SD
    assert t.primary.mean() / t.primary_cf.mean() == pytest.approx(0.70, abs=0.05)
    inj = t[t.injected_primary]
    assert (inj.primary_cf == 1).all() and (inj.primary == 0).all()
    c = out.calls[out.calls.lead_id.isin(inj.lead_id)]
    assert not (c.answered & c.disposition.isin(cfg.primary.dispositions)).any()


@pytest.mark.data
def test_null_effect_and_untriggered_unchanged(bttc):
    cfg, sim, pool = bttc
    st = _state(cfg, 1.0)
    null = simulate_day(pool, cfg, _sim(sim, scale=10), st, 0, 13)
    t = null.truth
    pd.testing.assert_series_equal(t.primary, t.primary_cf, check_names=False)
    pd.testing.assert_series_equal(t.secondary, t.secondary_cf, check_names=False)
    assert not (t.injected_primary | t.injected_secondary).any()

    eff = simulate_day(pool, cfg, _sim(sim, L=0.25, c=0.03, scale=10), st, 0, 13)
    untrig = eff.truth.lead_id[~eff.truth.triggered]
    assert len(untrig) and not eff.truth[~eff.truth.triggered][["injected_primary", "injected_secondary"]].any().any()
    pd.testing.assert_frame_equal(eff.calls[eff.calls.lead_id.isin(untrig)], null.calls[null.calls.lead_id.isin(untrig)])


@pytest.mark.data
def test_continuous_secondary_multiplies_answered_b(bttc):
    cfg, sim, pool = bttc
    st = _state(cfg, 0.5)
    base = simulate_day(pool, cfg, _sim(sim, scale=3), st, 0, 14)
    eff = simulate_day(pool, cfg, _sim(sim, c=0.03, scale=3), st, 0, 14)
    b = base.calls.lead_id.isin(base.truth.lead_id[base.truth.variant.eq("B")]) & base.calls.answered
    assert b.any() and (~b).any()
    assert (eff.calls.duration_s[b] == base.calls.duration_s[b] * 1.03).all()
    pd.testing.assert_frame_equal(eff.calls[~b], base.calls[~b])


@pytest.mark.data
def test_binary_secondary(buyer):
    cfg, sim, pool = buyer
    st = _state(cfg, 1.0)  # scale 100: ~12,900 triggered B leads
    with_c = simulate_day(pool, cfg, _sim(sim, L=0.10, c=0.05), st, 0, 15)
    no_c = simulate_day(pool, cfg, _sim(sim, L=0.10), st, 0, 15)
    t, t0 = _trig_b(with_c), _trig_b(no_c)
    assert not (t.injected_primary & t.injected_secondary).any()  # only untouched leads
    pd.testing.assert_series_equal(t.primary, t0.primary)        # same primary injection
    inj = t[t.injected_secondary]
    assert (inj.secondary_cf == 0).all() and (inj.secondary == 1).all()
    assert (t.secondary[~t.injected_secondary] == t0.secondary[~t.injected_secondary]).all()
    # q2 = c / e with e ~ 0.19: SD of the rise ~0.0018 (conversion) + ~0.0008 (eligible share); 0.01 > 4 SD
    assert (t.secondary - t0.secondary).mean() == pytest.approx(0.05, abs=0.01)

    alone = _trig_b(simulate_day(pool, cfg, _sim(sim, c=0.05), st, 0, 16))
    assert (alone.secondary - alone.secondary_cf).mean() == pytest.approx(0.05, abs=0.01)
    with pytest.raises(ValueError):
        simulate_day(pool, cfg, _sim(sim, c=0.5), st, 0, 15)  # above the eligible share


# --- fault injection and truth ------------------------------------------------------------

@pytest.mark.data
def test_fault_drops_b_exposures_only(bttc):
    cfg, sim, pool = bttc
    out = simulate_day(pool, cfg, _sim(sim, scale=3, drop=0.15), _state(cfg, 0.5), 0, 17)
    t = out.truth
    b = t[t.variant.eq("B")]
    # ~1,570 B leads: SD of the dropped share ~0.009; 0.04 is ~4.4 SD
    assert (~b.exposure_logged).mean() == pytest.approx(0.15, abs=0.04)
    assert t.exposure_logged[t.variant.eq("A")].all()
    assert out.exposures.lead_id.tolist() == t.lead_id[t.exposure_logged].tolist()
    assert b.lead_id[~b.exposure_logged].isin(out.calls.lead_id).all()


@pytest.mark.data
def test_truth_summary_and_report(bttc, buyer, tmp_path):
    cfg, sim, pool = bttc
    truth = pd.concat([simulate_day(pool, cfg, sim, _state(cfg), d, 21).truth for d in range(3)],
                      ignore_index=True)
    s = truth_summary(truth, pool, cfg, sim, "test", 21)
    assert s.experiment_id == cfg.id and s.seed == 21 and s.secondary_change_type == "relative"
    assert (s.injected_primary_rel_lift, s.injected_secondary_change) == (0.25, 0.03)
    assert s.leads["A"] + s.leads["B"] == len(truth) and s.exposures_dropped == 0
    assert all(0 < s.triggered[v] < s.leads[v] for v in "AB")
    assert s.pool_leads == len(pool.leads) and s.pool_p == pool.p
    assert s.realised_primary_rel_lift == pytest.approx(s.rate_B / s.rate_B_counterfactual - 1)
    assert s.realised_primary_rel_lift > 0 and s.realised_secondary_change > 0

    path = write_truth_report(s, tmp_path)
    md = path.read_text()
    assert path == tmp_path / "sim_truth_test.md"
    for text in ("Injected (sim config)", "Realised (this run)", "+25.00%", "seed 21", cfg.id,
                 "The engine never sees this file or the sim config.", f"{len(pool.leads):,} lead-windows"):
        assert text in md

    bcfg, bsim, bpool = buyer
    bs = _sim(bsim, L=0.1, c=0.05)
    bt = truth_summary(simulate_day(bpool, bcfg, bs, _state(bcfg), 0, 3).truth, bpool, bcfg, bs, "b", 3)
    assert bt.secondary_change_type == "absolute"
    assert bt.realised_secondary_change == pytest.approx(bt.secondary_B - bt.secondary_B_counterfactual)


@pytest.mark.data
def test_secondary_change_is_the_total_true_change(bttc, buyer):
    """Checkpoint 2: the extra meetings lengthen calls / overwrite dispositions; the direct
    injection nets that out so the realised total lands on secondary_change."""
    cfg, sim, pool = bttc
    t = _trig_b(simulate_day(pool, cfg, _sim(sim, L=0.25, c=0.03, scale=10), _state(cfg, 1.0), 0, 21))
    assert t.secondary.mean() / t.secondary_cf.mean() - 1 == pytest.approx(0.03, abs=0.01)
    t = _trig_b(simulate_day(pool, cfg, _sim(sim, L=0.25, scale=10), _state(cfg, 1.0), 0, 22))
    assert t.secondary.mean() / t.secondary_cf.mean() - 1 == pytest.approx(0.0, abs=0.01)
    cfg, sim, pool = buyer
    t = _trig_b(simulate_day(pool, cfg, _sim(sim, L=0.10, c=0.05), _state(cfg, 1.0), 0, 23))
    assert (t.secondary - t.secondary_cf).mean() == pytest.approx(0.05, abs=0.01)
