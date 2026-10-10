"""loop/synthdata.py: the synthetic buyer call log hits the profile's rates, loads through the base
adapter, and is deterministic in its seed (PLAN_LOOP §L9.3)."""

import numpy as np
import pandas as pd
import pytest

from abtest import config, data, metrics, simulator
from abtest.loop import synthdata

TEMPLATE = "configs/experiments/vani-buyer-qty-asks-v1.yaml"
SEEDS = [20261010, 1, 2, 3]
TARGET_SUCCESS, TARGET_ABRUPT, TARGET_ANSWER = 0.667, 0.19, 0.76


@pytest.fixture(scope="module")
def cfg():
    return config.load_experiment(TEMPLATE)


@pytest.fixture(scope="module", params=SEEDS)
def synth(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(f"synth{request.param}")
    paths = synthdata.write_synthetic(out, request.param)
    df, report = data.load("buyer_logs", out)
    return request.param, out, paths, df, report


def test_writes_the_named_csv(synth):
    _, out, paths, df, _ = synth
    assert paths == {"buyer_logs": out / "Call_Logs_Hackathon.csv"}
    assert paths["buyer_logs"].exists()
    raw = pd.read_csv(paths["buyer_logs"])
    for col in ("fk_leap_call_records_id", "fk_glusr_usr_id", "entered_on", "is_call_answered",
                "disposition_remarks", "call_recording_url"):
        assert col in raw.columns
    assert raw.entered_on.str.fullmatch(r"\d\d/\d\d/\d\d \d\d:\d\d").all()   # "%d/%m/%y %H:%M" text


def test_shape_matches_the_profile(synth):
    _, _, _, df, report = synth
    assert df.lead_id.nunique() == 825
    assert df.groupby("lead_id").size().between(1, 3).all()
    assert df.ts.dt.normalize().nunique() == 5
    assert df.ts.min() >= pd.Timestamp("2026-10-05") and df.ts.max() < pd.Timestamp("2026-10-10")
    assert 900 <= len(df) <= 1050
    assert report.dropped == {"call_id is null": 0, "lead_id is null": 0, "ts is null": 0}
    # only the two dispositions the real file leaves null go through the adapter's inference
    assert {k.split("'")[1] for k in report.inferred if k.startswith("answered")} == {
        "No Enrichment Scope", "Foreign or Disabled Users"}
    assert df.call_id.is_unique


def test_answer_rate_near_target(synth):
    df = synth[3]
    assert abs(df.answered.mean() - TARGET_ANSWER) < 0.03


def test_pool_rates_match_the_template_baseline(synth, cfg):
    """The rates the simulator bootstraps from: BL success per triggered lead, abrupt disconnects."""
    _, out, _, _, _ = synth
    sim = config.load_sim("configs_loop/sims/buyer_base.yaml")
    pool = simulator.build_pool(cfg, sim, data_dir=out)
    assert abs(pool.p - TARGET_SUCCESS) < 0.03
    assert abs(pool.secondary_base - TARGET_ABRUPT) < 0.03
    assert abs(pool.p - cfg.baseline_p) < 0.03


def test_rates_through_metrics(synth, cfg):
    """The same rates through the engine's own per-lead outcomes (metrics.lead_outcomes)."""
    df = synth[3]
    first = df.groupby("lead_id").ts.min().rename("ts").reset_index().assign(variant="A")
    out = metrics.lead_outcomes(df, first, cfg, df.ts.max() + pd.Timedelta(days=30))
    trig = out[out.triggered_ts.notna()]
    assert abs(len(trig) / len(out) - 0.78) < 0.03                       # triggered share of leads
    assert abs(trig.primary.astype(float).mean() - TARGET_SUCCESS) < 0.03
    assert abs(trig.secondary.mean() - TARGET_ABRUPT) < 0.03


def test_dispositions_are_consistent_with_answered(synth):
    df = synth[3]
    for d in ("BL Enriched", "BL Approved", "Abruptly Disconnected Call"):
        assert df[df.disposition == d].answered.all()
    assert not df[df.disposition.isin(["No Enrichment Scope", "Foreign or Disabled Users"])].answered.any()
    assert set(df[df.answered].disposition) & {"BL Enriched", "BL Approved"}


def test_deterministic_in_seed(tmp_path):
    a = synthdata.write_synthetic(tmp_path / "a", 7)["buyer_logs"].read_bytes()
    b = synthdata.write_synthetic(tmp_path / "b", 7)["buyer_logs"].read_bytes()
    c = synthdata.write_synthetic(tmp_path / "c", 8)["buyer_logs"].read_bytes()
    assert a == b
    assert a != c


def test_default_seed_is_the_contract_seed(tmp_path):
    synthdata.write_synthetic(tmp_path / "d")
    synthdata.write_synthetic(tmp_path / "e", 20261010)
    assert (tmp_path / "d" / "Call_Logs_Hackathon.csv").read_bytes() == \
        (tmp_path / "e" / "Call_Logs_Hackathon.csv").read_bytes()


def test_manifest_marks_current_data(tmp_path):
    assert not synthdata.is_current(tmp_path, 5)
    synthdata.write_synthetic(tmp_path, 5)
    assert synthdata.is_current(tmp_path, 5)
    assert not synthdata.is_current(tmp_path, 6)          # another seed: stale
    (tmp_path / "Call_Logs_Hackathon.csv").unlink()
    assert not synthdata.is_current(tmp_path, 5)


def test_lead_quotas_are_exact_for_any_seed():
    """Quota construction: the triggered, success and abrupt totals do not wobble with the seed
    (only the split between abrupt-only and abrupt-then-success does)."""
    totals = set()
    for seed in range(8):
        n_calls = np.repeat([1, 2, 3], [707, 99, 19])
        rng = np.random.default_rng(seed)
        rng.shuffle(n_calls)
        kinds = synthdata._assign_kinds(rng, n_calls)
        k = {name: int((kinds == name).sum()) for name in ("untriggered", "success", "abrupt", "both", "other")}
        totals.add((825 - k["untriggered"], k["success"] + k["both"], k["abrupt"] + k["both"]))
        assert not (kinds[n_calls == 1] == "both").any()          # a single call cannot be both
    assert len(totals) == 1
    trig, success, abrupt = totals.pop()
    assert success / trig == pytest.approx(0.6667, abs=0.001)
    assert abrupt / trig == pytest.approx(0.19, abs=0.001)
