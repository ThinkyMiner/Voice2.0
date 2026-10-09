from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from abtest import contracts, decision, power, router, srm, store
from abtest.contracts import (ALL_A, ALL_B, Boundaries, Decision, ExperimentConfig, FrozenConfig,
                               LookResult, PrimaryStats, SecondaryStats, SrmResult, State, Verdict)

CFG = ExperimentConfig.model_validate({
    "id": "t", "dataset": "bttc", "unit": "lead_id", "trigger": "first_answered_call",
    "salt": "s", "allocation": {"A": 0.9, "B": 0.1},
    "variants": {"A": {"base": "prompts/base_buyer_vani.txt"}, "B": {"base": "prompts/base_buyer_vani.txt"}},
    "window": {"days": 28, "looks": 7}, "maturation_days": 2, "baseline_p": 0.18,
    "primary": {"name": "m", "type": "binary", "dispositions": ["Meeting Fixed"], "better": "increase", "mde_rel": 0.15},
    "secondary": {"name": "dur", "type": "continuous", "role": "guardrail", "worse_if": "increase", "margin_rel": 0.1},
    "alpha_win": 0.025, "alpha_harm": 0.05, "power": 0.8, "min_info_first_look": 0.2,
    "srm_p_threshold": 0.001, "promotion": "approval",
})
FROZEN = FrozenConfig(config=CFG, canonical_json=CFG.model_dump_json(), config_hash="h",
                      prompt_hash={"A": "a", "B": "b"})
T0 = datetime(2026, 10, 1)


def _look(k: int = 1, *, lift: float = 0.044, z: float = 1.0, final: bool = False,
          rci: tuple[float, float] = (-0.01, 0.03), absolute: bool = False,
          share_b: float = 0.1, srm_p: float = 0.5) -> LookResult:
    """6,000 matured leads, 16 days after start: weekly impact = lift * 6000 / 14 * 7 = lift * 3000."""
    primary = PrimaryStats(rate_A=0.18, rate_B=0.18 + lift, lift_abs=lift, lift_rel=lift / 0.18,
                           ci95=(lift - 0.03, lift + 0.03), se_pooled=0.015, se_unpooled=0.016, z=z,
                           detectable_mde_rel=0.2)
    delta = sum(rci) / 2
    secondary = SecondaryStats(name="dur", type="binary" if absolute else "continuous", role="guardrail",
                               mean_A=100, mean_B=100 * (1 + delta), rel_change=delta, delta=delta,
                               delta_type="absolute" if absolute else "relative", se=0.01,
                               ci95=(delta - 0.02, delta + 0.02), rci=rci, margin=0.02 if absolute else 0.1)
    n_b = round(10_000 * share_b)
    s = SrmResult(n_A=10_000 - n_b, n_B=n_b, configured_share_B=0.1, observed_share_B=share_b,
                  ci95=(share_b - 0.005, share_b + 0.005), chi2=1.0, p=srm_p)
    return LookResult(experiment_id="t", look=k, ts_sim=T0 + timedelta(days=16 + k - 1), is_final=final,
                      info_fraction=k / 7, n_A=5_400, n_B=600, primary=primary, secondary=secondary,
                      srm_assigned=s, srm_triggered=s.model_copy(update={"p": 0.5}))


def _bounds(k: int = 1) -> Boundaries:
    return Boundaries(info_fractions=tuple(i / 7 for i in range(1, k + 1)), win=(3.0,) * k, harm=(2.2,) * k)


def _verdict(d, reason: str = "Rule fired.") -> Verdict:
    return Verdict(decision=d, rule=0 if d == "CONTINUE" else 3, reason=reason)


@pytest.fixture(autouse=True)
def jsonl(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "logs" / "d.jsonl"  # parent missing: the module creates it
    monkeypatch.setattr(decision, "JSONL", path)  # never touch the repo's logs/decisions.jsonl
    return path


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.create_experiment(c, FROZEN)
    return c


def _pending(conn):
    s = decision.start(conn, FROZEN, T0)
    return decision.apply_verdict(conn, s, _look(z=3.5), _bounds(), _verdict(State.PENDING_APPROVAL))


def test_start(conn):
    with pytest.raises(ValueError, match="does not match"):
        decision.start(conn, FROZEN.model_copy(update={"config_hash": "other"}), T0)
    with pytest.raises(KeyError):
        decision.start(conn, FROZEN.model_copy(update={"config": CFG.model_copy(update={"id": "nope"})}), T0)
    s = decision.start(conn, FROZEN, T0)
    assert s.state == State.RUNNING and s.allocation == CFG.allocation
    assert s.n_b_max == power.n_max(CFG).n_b_max and s.started_ts_sim == s.updated_ts_sim == T0
    assert store.get_state(conn, "t") == s
    (row,) = store.read_decisions(conn, "t")
    assert (row.kind, row.from_state, row.to_state, row.decision, row.actor, row.ts_sim) == (
        "state_change", State.DRAFT, State.RUNNING, "RUNNING", "engine", T0)
    assert row.look is None and row.primary is None
    assert "B gets 10% of traffic" in row.reason and "+15% relative lift in Meeting Fixed" in row.reason
    with pytest.raises(ValueError, match="only a DRAFT"):
        decision.start(conn, FROZEN, T0)


def test_start_row_marks_the_running_period_for_srm(conn):
    decision.start(conn, FROZEN, T0)
    leads = [f"l{i}" for i in range(20)]
    buckets = [router.bucket(CFG.salt, lead) for lead in leads]
    ts = [T0 + timedelta(hours=i - 5) for i in range(20)]  # the first 5 arrive while DRAFT (all A)
    store.insert_df(conn, "exposures", pd.DataFrame({
        "experiment_id": "t", "lead_id": leads, "bucket": buckets, "ts": ts,
        "variant": ["A" if t < T0 else router.variant_for(b, CFG.allocation) for b, t in zip(buckets, ts)]}))
    s = srm.split_summary(conn, "t")
    assert s.assigned.n_A + s.assigned.n_B == 15 and s.rehash_pass


def test_continue_logs_one_look_row(conn, jsonl):
    s = decision.start(conn, FROZEN, T0)
    look, b = _look(), _bounds()
    out = decision.apply_verdict(conn, s, look, b, _verdict("CONTINUE", "No boundary crossed at look 1."))
    assert out.state == State.RUNNING and out.looks_done == 1 and out.info_fractions == b.info_fractions
    assert out.allocation == CFG.allocation and out.updated_ts_sim == look.ts_sim
    assert store.get_state(conn, "t") == out
    rows = store.read_decisions(conn, "t")
    assert [Decision.model_validate_json(line) for line in jsonl.read_text("utf-8").splitlines()] == rows
    row = rows[-1]
    assert (row.kind, row.decision, row.reason, row.actor, row.look, row.ts_sim) == (
        "look", "CONTINUE", "No boundary crossed at look 1.", "engine", 1, look.ts_sim)
    assert row.primary.win_boundary == 3.0 and row.primary.harm_boundary == -2.2 < 0
    assert row.primary.lift_abs == look.primary.lift_abs and row.guardrail.rci == look.secondary.rci
    assert (row.srm.p, row.srm.p_triggered, row.n_A, row.n_B) == (0.5, 0.5, 5_400, 600)


@pytest.mark.parametrize("to, kw, phrase", [
    (State.PROMOTED, {"z": 3.5},
     "B now gets 100% of traffic, worth ≈ +132 Meeting Fixed a week (+44 per 1,000 triggered leads)."),
    (State.PENDING_APPROVAL, {"z": 3.5},
     "Approving B is worth ≈ +132 Meeting Fixed a week; B keeps its 10% share"),
    (State.STOPPED_HARM, {"z": -3.0, "lift": -0.05},
     "All traffic back to A; at full rollout B would have cost ≈ 150 Meeting Fixed a week."),
    (State.STOPPED_HARM, {"rci": (0.12, 0.2)},
     "B moved dur by +16.0% (RCI [+12.0%, +20.0%]), beyond the +10.0% margin."),
    (State.STOPPED_HARM, {"rci": (0.03, 0.07), "absolute": True},
     "B moved dur by +5.0 pp (RCI [+3.0 pp, +7.0 pp]), beyond the +2.0 pp margin."),
    (State.BLOCKED_GUARDRAIL, {"z": 3.5, "rci": (0.05, 0.15), "final": True},
     "A stays; B's ≈ +132 Meeting Fixed a week does not clear the dur guardrail (RCI [+5.0%, +15.0%] vs margin +10.0%)."),
    (State.INCONCLUSIVE, {"lift": 0.01, "final": True},
     "A stays; B's effect is between -60 and +120 Meeting Fixed a week (95% CI)"),
    (State.HALTED_SRM, {"srm_p": 1e-6, "share_b": 0.085},
     "the split is broken (B got 8.50% of leads vs 10% configured)"),
])
def test_state_verdicts(conn, jsonl, to, kw, phrase):
    s = decision.start(conn, FROZEN, T0)
    out = decision.apply_verdict(conn, s, _look(**kw), _bounds(), _verdict(to))
    want = ALL_B if to == State.PROMOTED else CFG.allocation if to == State.PENDING_APPROVAL else ALL_A
    assert out.state == to and out.allocation == want and store.get_state(conn, "t") == out
    look_row, change = store.read_decisions(conn, "t")[1:]
    assert (look_row.kind, look_row.decision) == ("look", to.value)
    assert (change.kind, change.decision, change.from_state, change.to_state) == (
        "state_change", to.value, State.RUNNING, to)
    same = {"kind", "decision", "from_state", "to_state", "ts_wall"}
    assert change.model_dump(exclude=same) == look_row.model_dump(exclude=same)  # same look fields
    assert change.reason.startswith("Rule fired. ") and phrase in change.reason
    assert len(jsonl.read_text("utf-8").splitlines()) == 3


def test_approve(conn):
    pending = _pending(conn)
    assert pending.allocation == CFG.allocation
    out = decision.approve(conn, "t", "kartik", True)
    assert out.state == State.PROMOTED and out.allocation == ALL_B and out.updated_ts_sim == pending.updated_ts_sim
    assert store.get_state(conn, "t") == out
    row = store.read_decisions(conn, "t")[-1]
    assert (row.kind, row.actor, row.from_state, row.to_state, row.decision, row.ts_sim) == (
        "state_change", "kartik", State.PENDING_APPROVAL, State.PROMOTED, "PROMOTED", pending.updated_ts_sim)
    assert row.look is None and row.primary is None
    assert row.reason == "Approved by kartik: B now gets 100% of traffic, worth ≈ +132 Meeting Fixed a week."
    with pytest.raises(ValueError, match="PROMOTED"):
        decision.approve(conn, "t", "kartik", True)


def test_reject(conn):
    _pending(conn)
    out = decision.approve(conn, "t", "kartik", False)
    assert out.state == State.REJECTED and out.allocation == ALL_A
    assert store.read_decisions(conn, "t")[-1].reason == "Rejected by kartik: all traffic back to A."


def test_approve_guards(conn):
    with pytest.raises(KeyError):
        decision.approve(conn, "nope", "kartik", True)
    decision.start(conn, FROZEN, T0)
    with pytest.raises(ValueError, match="RUNNING"):
        decision.approve(conn, "t", "kartik", True)


def test_apply_verdict_guards(conn):
    s = decision.start(conn, FROZEN, T0)
    bad = [
        (s, _look(2), _bounds(2), _verdict("CONTINUE")),                      # skips look 1
        (s, _look(final=True), _bounds(), _verdict("CONTINUE")),              # final look must decide
        (s, _look(), _bounds(), _verdict(State.REJECTED)),                    # not reachable from RUNNING
        (s, _look(), _bounds(), _verdict(State.RUNNING)),
        (s, _look(), _bounds(0), _verdict("CONTINUE")),                       # no boundary for look 1
        (s, _look().model_copy(update={"experiment_id": "x"}), _bounds(), _verdict("CONTINUE")),
    ]
    for args in bad:
        with pytest.raises(ValueError):
            decision.apply_verdict(conn, *args)
    done = decision.apply_verdict(conn, s, _look(srm_p=1e-6), _bounds(), _verdict(State.HALTED_SRM))
    with pytest.raises(ValueError, match="HALTED_SRM"):
        decision.apply_verdict(conn, done, _look(2), _bounds(2), _verdict("CONTINUE"))
    assert len(store.read_decisions(conn, "t")) == 3  # start + look + state change; nothing from the rejects


def test_weekly_impact():
    assert decision.weekly_impact(0.044, 6_000, 16, CFG) == pytest.approx(132)  # 0.044 * 6000 / 14 * 7
    assert decision.weekly_impact(0.044, 6_000, 2.5, CFG) == pytest.approx(0.044 * 6_000 * 7)  # floored at 1 day


def test_jsonl_off_writes_no_file(conn, jsonl, monkeypatch):
    monkeypatch.setattr(decision, "JSONL", None)
    decision.start(conn, FROZEN, T0)
    assert not jsonl.parent.exists() and len(store.read_decisions(conn, "t")) == 1


def test_cli(tmp_path, monkeypatch, capsys):
    db = tmp_path / "abtest.db"
    monkeypatch.setattr(contracts, "DB_PATH", db)
    c = store.connect(db)
    store.create_experiment(c, FROZEN)
    _pending(c)
    decision.main(["t", "--actor", "kartik"])
    out = capsys.readouterr().out
    assert "t: PENDING_APPROVAL -> PROMOTED by kartik" in out and "worth ≈ +132 Meeting Fixed a week" in out
    assert store.get_state(c, "t").state == State.PROMOTED
    for argv in (["t", "--actor", "kartik", "--reject"], ["nope", "--actor", "kartik"]):
        with pytest.raises(SystemExit) as e:
            decision.main(argv)
        assert e.value.code == 1 and capsys.readouterr().err.startswith("error: ")


def test_engine_never_reads_sim_ground_truth():
    src = Path(decision.__file__).read_text(encoding="utf-8")
    for word in ("simulator", "SimConfig", "SIMS_DIR", "configs/sims", "sim_truth"):
        assert word not in src
