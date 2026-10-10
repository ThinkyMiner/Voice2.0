"""loop/live_ab.py end to end: simulated traffic through the real engine on the synthetic buyer data,
with the rubric guardrail between rules.evaluate_look and decision.apply_verdict (PLAN_LOOP §L9.3-4).

Each run costs about two seconds, so a module fixture runs one scenario once and the tests read it.
The seeds are pinned: runs are deterministic, and each seed was picked for the outcome it shows.
Every test uses its own generation number, so no two share a sim config file. No network, no model call.
"""

import json

import pytest
import yaml

from abtest import decision, store
from abtest.contracts import ROOT, State
from abtest.loop import live_ab, synthdata
from abtest.loop.contracts import DEFAULT_LOOP_CONFIG, Candidate, LiveAbResult, LoopConfig
from abtest.loop.store_loop import connect_loop

BASE_PROMPT = "prompts/base_buyer_vani.txt"
PATCH = {"mode": "replace_text", "find": "quantity = 3", "replace": "quantity = 2"}


@pytest.fixture(scope="module")
def cfg():
    return LoopConfig.model_validate(yaml.safe_load(DEFAULT_LOOP_CONFIG.read_text()))


@pytest.fixture(scope="module")
def candidate():
    return Candidate(candidate_id="g1-c1", generation=1, patch=PATCH, patch_hash="h", rationale="ask less",
                     targets=("looping",))


@pytest.fixture(scope="module")
def loop_dir(tmp_path_factory):
    """LOOP_DATA_DIR and the JSONL mirror redirected away from the repo for the whole module."""
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("loop")
    mp.setattr(live_ab, "LOOP_DATA_DIR", root)
    mp.setattr(decision, "JSONL", None)
    yield root
    mp.undo()


@pytest.fixture(scope="module")
def runner(cfg, candidate, loop_dir):
    def run(lift, shift, seed, generation=1, conn=None, quiet=True):
        conn = conn or connect_loop(":memory:")
        return live_ab.run_live_ab(conn, cfg, BASE_PROMPT, candidate, generation, lift, shift, seed,
                                   quiet=quiet), conn
    return run


@pytest.fixture(scope="module")
def promoted(runner):
    return runner(0.15, 0.0, 2, generation=1)


@pytest.fixture(scope="module")
def rubric_stop(runner):
    return runner(0.15, 0.20, 2, generation=2)


def _guard_rows(conn, experiment_id):
    rows = conn.execute("SELECT look, payload FROM loop_guard_log WHERE experiment_id = ? ORDER BY look",
                        (experiment_id,)).fetchall()
    return [(r["look"], json.loads(r["payload"])) for r in rows]


# --- a win: the engine promotes, the rubric stays out of the way ---------------------------------

def test_a_lift_is_promoted_by_the_engine(promoted):
    res, conn = promoted
    assert isinstance(res, LiveAbResult)
    assert res.terminal == "PROMOTED" and res.decided_by == "engine"
    assert res.injected_lift == 0.15 and res.injected_risk_shift == 0.0
    assert res.lift_rel is not None and res.lift_rel > 0.05
    assert res.lift_ci95 is not None and res.lift_ci95[0] < res.lift_ci95[1]
    assert res.rubric_delta is not None and res.rubric_rci is not None
    assert res.rubric_rci[1] < 0.05                         # the rubric guardrail cleared
    assert "promoted" in res.reason.lower()


def test_the_base_tables_are_written(promoted):
    res, conn = promoted
    row = conn.execute("SELECT * FROM experiments WHERE experiment_id = ?", (res.experiment_id,)).fetchone()
    assert res.experiment_id == "vani-buyer-qty-asks-v1-vani-loop-g1"
    assert row["scenario"] == "vani-loop-g1" and row["seed"] == 2
    assert row["prompt_hash_A"] != row["prompt_hash_B"]     # B carries the candidate's patch
    cfg = json.loads(row["config_json"])
    assert cfg["promotion"] == "auto" and cfg["allocation"] == {"A": 0.9, "B": 0.1}
    assert cfg["variants"]["B"]["patch"] == PATCH and cfg["variants"]["A"]["base"] == BASE_PROMPT

    state = store.get_state(conn, res.experiment_id)
    assert state.state == State.PROMOTED and state.looks_done == res.looks
    rows = store.read_decisions(conn, res.experiment_id)
    looks = [d for d in rows if d.kind == "look"]
    assert [d.look for d in looks] == list(range(1, res.looks + 1))
    assert rows[0].kind == "state_change" and rows[0].to_state == State.RUNNING   # decision.start
    assert rows[-1].kind == "state_change" and rows[-1].to_state == State.PROMOTED
    assert all(d.decision == "CONTINUE" for d in looks[:-1])
    assert conn.execute("SELECT COUNT(*) FROM sim_truth WHERE experiment_id = ?",
                        (res.experiment_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM exposures WHERE experiment_id = ?",
                        (res.experiment_id,)).fetchone()[0] > 1000
    assert conn.execute("SELECT COUNT(*) FROM calls WHERE experiment_id = ?",
                        (res.experiment_id,)).fetchone()[0] > 1000


def test_every_look_has_a_guard_log_row(promoted):
    res, conn = promoted
    rows = _guard_rows(conn, res.experiment_id)
    assert [k for k, _ in rows] == list(range(1, res.looks + 1))
    for k, p in rows:
        assert set(p) == {"base_verdict", "final_verdict", "rubric_decided", "template_guardrail",
                          "rubric_guardrail"}
        assert p["rubric_guardrail"]["name"] == "rubric_risk"
        assert p["template_guardrail"]["name"] == "abrupt_disconnect"
        assert p["rubric_decided"] is False
        assert p["base_verdict"]["decision"] == p["final_verdict"]["decision"] or k < res.looks
    assert rows[-1][1]["final_verdict"]["decision"] == "PROMOTED"


def test_guard_log_is_strict_json_even_with_unbounded_intervals(promoted):
    """NaN and infinities are written as strings, so the log parses with any strict JSON reader."""
    res, conn = promoted
    raw = conn.execute("SELECT payload FROM loop_guard_log WHERE experiment_id = ? AND look = 1",
                       (res.experiment_id,)).fetchone()["payload"]

    def no_constants(name):
        raise AssertionError(f"non-strict JSON constant {name}")

    payload = json.loads(raw, parse_constant=no_constants)
    assert payload["rubric_guardrail"]["rci"][0] == payload["rubric_guardrail"]["rci"][0]   # parsed, not NaN


def test_the_synthetic_data_and_sim_config_are_written_under_the_loop_dir(promoted, loop_dir, cfg):
    assert synthdata.is_current(loop_dir / "synthetic", cfg.sim.seed)
    sim = yaml.safe_load((loop_dir / "sims" / "vani-loop-g1.yaml").read_text())
    assert sim["true_effect"]["primary_rel_lift"] == 0.15
    assert sim["volume"] == {"daily_leads": 164.8, "scale": 4.0}


def test_the_synthetic_data_is_reused(promoted, runner, loop_dir):
    csv = loop_dir / "synthetic" / "Call_Logs_Hackathon.csv"
    before = csv.stat().st_mtime_ns
    runner(0.0, 0.0, 3, generation=12)
    assert csv.stat().st_mtime_ns == before


# --- the rubric guardrail decides ----------------------------------------------------------------

def test_a_large_risk_shift_stops_the_run_via_the_rubric(rubric_stop):
    res, conn = rubric_stop
    assert res.terminal == "STOPPED_HARM" and res.decided_by == "rubric_guardrail"
    assert res.injected_risk_shift == 0.20
    assert res.rubric_delta > 0.05 and res.rubric_rci[0] > 0.05
    assert "rubric_risk" in res.reason
    last = store.read_decisions(conn, res.experiment_id)[-1]
    assert last.kind == "state_change" and last.to_state == State.STOPPED_HARM
    assert "rubric_risk" in last.reason and "+5.00 pp margin" in last.reason
    # the decision row describes the guardrail that fired: the rubric stats, not the template's
    assert last.guardrail.margin == pytest.approx(0.05)
    assert last.guardrail.delta == pytest.approx(res.rubric_delta)
    assert "rubric_risk" in last.reason.split("Business impact:")[1]
    rows = _guard_rows(conn, res.experiment_id)
    assert rows[-1][1]["rubric_decided"] is True
    assert rows[-1][1]["base_verdict"]["decision"] != "STOPPED_HARM"     # the engine alone would not have


def test_the_rubric_blocks_a_win_it_cannot_clear(runner):
    res, conn = runner(0.15, 0.08, 1, generation=3)       # net rubric rise just under the margin
    assert res.terminal == "BLOCKED_GUARDRAIL" and res.decided_by == "rubric_guardrail"
    assert res.looks == 7
    assert 0.0 < res.rubric_rci[0] < 0.05 < res.rubric_rci[1]
    rows = _guard_rows(conn, res.experiment_id)
    waited = [p for k, p in rows if p["base_verdict"]["decision"] == "PROMOTED"]
    assert waited and all(p["final_verdict"]["decision"] == "CONTINUE" for p in waited[:-1])
    assert "waits for the rubric guardrail" in waited[0]["final_verdict"]["reason"]
    assert rows[-1][1]["final_verdict"]["decision"] == "BLOCKED_GUARDRAIL" and rows[-1][1]["rubric_decided"]
    last = store.read_decisions(conn, res.experiment_id)[-1]
    assert "rubric_risk" in last.reason
    assert last.guardrail.margin == pytest.approx(0.05)    # the rubric stats, not the template's 0.02


def test_an_engine_block_is_not_attributed_to_the_rubric(runner):
    res, _ = runner(0.15, 0.08, 8, generation=4)          # the template's own guardrail does not clear here
    assert res.terminal == "BLOCKED_GUARDRAIL" and res.decided_by == "engine"


# --- harm and no-effect runs -----------------------------------------------------------------------

def test_a_harmful_lift_is_stopped_early_by_the_engine(runner):
    res, conn = runner(-0.20, 0.0, 1, generation=5)
    assert res.terminal == "STOPPED_HARM" and res.decided_by == "engine"
    assert res.looks <= 3 and res.lift_rel < 0
    assert res.injected_lift == -0.20


def test_no_effect_ends_inconclusive_at_the_final_look(runner):
    res, conn = runner(0.0, 0.0, 2, generation=6)
    assert res.terminal == "INCONCLUSIVE" and res.looks == 7 and res.decided_by == "engine"
    last = [d for d in store.read_decisions(conn, res.experiment_id) if d.kind == "look"][-1]
    assert last.decision == "INCONCLUSIVE"


# --- determinism, reruns, output --------------------------------------------------------------------

def test_same_inputs_give_the_same_run(runner, promoted):
    again, _ = runner(0.15, 0.0, 2, generation=1)
    first, _ = promoted
    assert again == first


def test_a_rerun_of_a_generation_replaces_the_previous_run(runner):
    conn = connect_loop(":memory:")
    first, _ = runner(0.15, 0.20, 4, generation=7, conn=conn)       # rubric stop: few looks
    second, _ = runner(0.0, 0.0, 4, generation=7, conn=conn)        # null run: all 7 looks
    assert first.experiment_id == second.experiment_id
    assert [k for k, _ in _guard_rows(conn, second.experiment_id)] == list(range(1, second.looks + 1))
    third, _ = runner(0.15, 0.20, 4, generation=7, conn=conn)       # fewer looks again: no stale rows
    assert [k for k, _ in _guard_rows(conn, third.experiment_id)] == list(range(1, third.looks + 1))
    assert conn.execute("SELECT COUNT(*) FROM experiments WHERE experiment_id = ?",
                        (third.experiment_id,)).fetchone()[0] == 1


def test_quiet_false_prints_one_line_per_look(runner, capsys):
    res, _ = runner(0.15, 0.20, 2, generation=9, quiet=False)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == res.looks
    assert all(line.startswith("day ") and "rubric_risk" in line for line in out)
    assert "by the rubric guardrail" in out[-1]
    _, _ = runner(0.15, 0.20, 2, generation=9, quiet=True)
    assert capsys.readouterr().out == ""


def test_the_candidate_patch_reaches_variant_b(cfg, candidate):
    v = live_ab._variants(BASE_PROMPT, candidate)
    assert v == {"A": {"base": BASE_PROMPT}, "B": {"base": BASE_PROMPT, "patch": PATCH}}
    full = Candidate(candidate_id="c", generation=1, patch={"mode": "full", "text": "a whole prompt"},
                     patch_hash="h", rationale="r", targets=())
    assert live_ab._variants(BASE_PROMPT, full)["B"] == {
        "patch": {"mode": "full", "path": None, "text": "a whole prompt"}}   # a full patch takes no base


def test_a_negative_lift_can_be_injected_at_all(promoted, runner, loop_dir):
    """secondary_change 0 makes the simulator reject a negative lift on this binary guardrail (it has
    no removal path); the sim config carries the change the lift itself implies instead."""
    runner(-0.20, 0.0, 3, generation=13)
    neg = yaml.safe_load((loop_dir / "sims" / "vani-loop-g13.yaml").read_text())["true_effect"]
    assert neg["primary_rel_lift"] == -0.20 and neg["secondary_change"] > 0.0      # more disconnects
    pos = yaml.safe_load((loop_dir / "sims" / "vani-loop-g1.yaml").read_text())["true_effect"]
    assert pos["primary_rel_lift"] == 0.15 and pos["secondary_change"] < 0.0       # fewer disconnects


def test_the_sim_config_path_is_relative_to_root_when_inside_it(tmp_path):
    assert live_ab._ref(ROOT / "data" / "x.yaml") == "data/x.yaml"
    assert live_ab._ref(tmp_path / "x.yaml") == str(tmp_path / "x.yaml")
