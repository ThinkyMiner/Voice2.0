"""Dashboard (PLAN.md §2.10) on small synthetic stores, through Streamlit's AppTest. No data/ needed."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from abtest import contracts, decision, router, store
from abtest.contracts import (Boundaries, ExperimentConfig, FrozenConfig, LookResult, PrimaryStats,
                               SecondaryStats, SimTruth, SrmResult, State, Verdict)

APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
T0 = datetime(2026, 10, 1)
BASE = ExperimentConfig.model_validate({
    "id": "base", "dataset": "bttc", "unit": "lead_id", "trigger": "first_answered_call",
    "salt": "s", "allocation": {"A": 0.9, "B": 0.1},
    "variants": {"A": {"base": "prompts/base_buyer_vani.txt"},
                 "B": {"base": "prompts/base_buyer_vani.txt",
                       "patch": {"mode": "append", "text": "Offer two concrete meeting slots."}}},
    "window": {"days": 28, "looks": 7}, "maturation_days": 2, "baseline_p": 0.18,
    "primary": {"name": "meeting_fixed", "type": "binary", "dispositions": ["Meeting Fixed"],
                "better": "increase", "mde_rel": 0.15},
    "secondary": {"name": "answered_duration_s", "type": "continuous", "role": "guardrail",
                  "worse_if": "increase", "margin_rel": 0.1},
    "alpha_win": 0.025, "alpha_harm": 0.05, "power": 0.8, "min_info_first_look": 0.2,
    "srm_p_threshold": 0.001, "promotion": "approval",
})


def _look(eid: str, k: int, z: float, lift: float) -> LookResult:
    p = PrimaryStats(rate_A=0.18, rate_B=0.18 + lift, lift_abs=lift, lift_rel=lift / 0.18,
                     ci95=(lift - 0.03, lift + 0.03), se_pooled=0.015, se_unpooled=0.016, z=z,
                     detectable_mde_rel=0.2)
    s = SecondaryStats(name="answered_duration_s", type="continuous", role="guardrail", mean_A=40, mean_B=40.4,
                       rel_change=0.01, delta=0.01, delta_type="relative", se=0.02, ci95=(-0.03, 0.05),
                       rci=(-0.05, 0.07), margin=0.1)
    srm = SrmResult(n_A=900, n_B=100, configured_share_B=0.1, observed_share_B=0.1, ci95=(0.08, 0.12),
                    chi2=0.0, p=0.9)
    return LookResult(experiment_id=eid, look=k, ts_sim=T0 + timedelta(days=6 + 2 * k), is_final=False,
                      info_fraction=0.2 * k, n_A=900, n_B=100, primary=p, secondary=s,
                      srm_assigned=srm, srm_triggered=srm)


def _experiment(conn, eid: str, scenario: str, verdicts: list[tuple[float, float, str]]) -> None:
    """One experiment: synthetic exposures and calls, then a look per (z, lift, decision)."""
    cfg = BASE.model_copy(update={"id": eid})
    frozen = FrozenConfig(config=cfg, canonical_json=cfg.model_dump_json(), config_hash=f"hash-{eid}",
                          prompt_hash={"A": "pa", "B": "pb"})
    store.create_experiment(conn, frozen, scenario, 7)
    state = decision.start(conn, frozen, T0)
    leads = [f"{eid}-{i}" for i in range(300)]
    ts = [T0 + timedelta(hours=i % 96) for i in range(300)]
    store.insert_df(conn, "exposures", pd.DataFrame({
        "experiment_id": eid, "lead_id": leads, "ts": ts, "bucket": [router.bucket("s", x) for x in leads],
        "variant": [router.variant_for(router.bucket("s", x), cfg.allocation) for x in leads]}))
    store.insert_df(conn, "calls", pd.DataFrame({
        "experiment_id": eid, "call_id": leads, "lead_id": leads, "ts": ts, "answered": 1,
        "disposition": ["Meeting Fixed" if i % 5 == 0 else "Not Interested" for i in range(300)],
        "duration_s": [30.0 + i % 20 for i in range(300)], "attempt_no": 1}))
    for k, (z, lift, d) in enumerate(verdicts, 1):
        bounds = Boundaries(info_fractions=tuple(0.2 * i for i in range(1, k + 1)), win=(3.0,) * k, harm=(2.2,) * k)
        state = decision.apply_verdict(conn, state, _look(eid, k, z, lift), bounds,
                                       Verdict(decision=d, rule=0 if d == "CONTINUE" else 2, reason=f"Rule says {d}."))


@pytest.fixture
def db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "abtest.db"
    monkeypatch.setattr(contracts, "DB_PATH", path)
    monkeypatch.setattr(contracts, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(decision, "JSONL", tmp_path / "decisions.jsonl")  # never the repo's log
    conn = store.connect(path)
    _experiment(conn, "t-pending", "approval", [(1.0, 0.02, "CONTINUE"), (3.5, 0.05, State.PENDING_APPROVAL)])
    _experiment(conn, "t-harm", "harm", [(-3.0, -0.05, State.STOPPED_HARM)])
    _experiment(conn, "t-running", "win", [])
    truth = SimTruth(experiment_id="t-harm", scenario="harm", seed=7, injected_primary_rel_lift=-0.3,
                     injected_secondary_change=0.0, secondary_change_type="relative", pool_leads=10,
                     pool_p=0.18, leads={"A": 270, "B": 30}, triggered={"A": 270, "B": 30}, exposures_dropped=0,
                     rate_A=0.18, rate_B_counterfactual=0.18, rate_B=0.13, realised_primary_rel_lift=-0.28,
                     secondary_A=40, secondary_B_counterfactual=40, secondary_B=40, realised_secondary_change=0.0,
                     secondary_from_primary=0.0, window_days=28)
    with conn:
        conn.execute("INSERT INTO sim_truth VALUES (?, ?, ?)", ("t-harm", "harm", truth.model_dump_json()))
    conn.close()
    return path


def _run(exp: str | None = None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    if exp:
        at.query_params["exp"] = exp
    return at.run()


def _text(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def _charts(at: AppTest) -> list[dict]:
    return [json.loads(c.proto.spec) for c in at.get("plotly_chart")]


@pytest.mark.parametrize("exp", ["t-pending", "t-harm", "t-running"])
def test_every_experiment_renders(db, exp):
    at = _run(exp)
    assert not at.exception and not at.error
    assert at.selectbox(key="exp").value == exp
    text = _text(at)
    assert "Simulated traffic from bttc, volume ×1" in text  # banner from the scenario's sim config
    for eid in ("t-pending", "t-harm", "t-running"):  # list rows
        assert f'href="?exp={eid}"' in text
    assert "Ground truth (engine never sees this)" in [e.label for e in at.expander]
    assert not at.expander[-1].proto.expanded  # collapsed


def test_detail_shows_business_impact_and_z_path(db):
    at = _run("t-harm")
    text = _text(at)
    assert "B stopped on day 8, protecting ≈" in text and "Meeting Fixed a week" in text
    z = next(f for f in _charts(at) if any(t.get("name", "").startswith("z: B vs A") for t in f["data"]))
    path = next(t for t in z["data"] if t.get("name", "").startswith("z: B vs A"))
    assert path["y"] == [-3.0] and path["x"] == [0.2]
    names = {t.get("name") for t in z["data"]}
    assert {"Win boundary at each look", "Harm boundary at each look", "Win boundary (planned looks)"} <= names
    assert any("Harm boundary crossed at look 1" in a["text"] for a in z["layout"]["annotations"])
    assert "Leads in both arms: <b>0</b>" in text and "Rule says STOPPED_HARM." in text  # split panel, log
    assert "-28.0%" in text  # ground truth: realised lift next to the engine's estimate


def test_approve_promotes_and_records_actor(db):
    at = _run("t-pending")
    assert [b.label for b in at.button] == ["Approve: promote B", "Reject: keep A"]
    at.text_input(key="actor").input("kartik")
    at.button(key="approve").click().run()
    assert not at.exception and not at.error
    conn = store.connect(db)
    assert store.get_state(conn, "t-pending").state == State.PROMOTED
    last = store.read_decisions(conn, "t-pending")[-1]
    assert (last.from_state, last.to_state, last.actor) == (State.PENDING_APPROVAL, State.PROMOTED, "kartik")
    assert "PENDING_APPROVAL → PROMOTED by kartik" in at.success[0].value
    assert not at.button  # no more buttons once decided


def test_approve_needs_a_name(db):
    at = _run("t-pending")
    at.text_input(key="actor").input(" ")
    at.button(key="reject").click().run()
    assert "approver's name" in at.error[0].value
    assert store.get_state(store.connect(db), "t-pending").state == State.PENDING_APPROVAL


def test_validation_page_without_and_with_report(db, tmp_path):
    at = _run()
    at.switch_page("pages/0_Validation.py").run()
    assert not at.exception and "make validate" in at.info[0].value
    assert "Simulated traffic from" in _text(at)

    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "validation.md").write_text("# Validation proof pack\n\n- PASS: A/A false-win rate 2.30%\n\n"
                                           "![validation_false_win](validation_false_win.png)\n", encoding="utf-8")
    Image.new("RGB", (4, 4), "white").save(reports / "validation_false_win.png")
    at.run()
    assert not at.exception and not at.info
    assert "PASS: A/A false-win rate 2.30%" in _text(at) and "![" not in _text(at)
    assert len(at.image) == 1


@pytest.mark.parametrize("empty", ["missing", "empty file"])
def test_empty_store(tmp_path, monkeypatch, empty):
    path = tmp_path / "abtest.db"
    if empty == "empty file":
        path.touch()
    monkeypatch.setattr(contracts, "DB_PATH", path)
    at = _run()
    assert not at.exception and "No experiments in the store yet" in at.info[0].value
    assert path.exists() == (empty == "empty file")  # read-only: never creates the store

