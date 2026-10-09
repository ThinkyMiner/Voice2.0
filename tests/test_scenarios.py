"""Scenario terminal states (PLAN.md Phase 4) and engine-path purity (PLAN.md §2.3).

The scenario tests run each scenario end to end at its pinned seed. They need the real data and
the whole engine (the Phase 3 stats chain plus decision.py and runner.EXPECTED), and skip with
the reason until both are present. The purity tests need neither and run in CI.
"""

import importlib
import importlib.util
import inspect
import typing
from functools import partial
from pathlib import Path

import pytest

from abtest import config, simulator, srm, store
from abtest.contracts import CONFIGS_DIR, DATA_DIR, Pool, SimConfig, SimTruth, State
from abtest.data import FILES

# --- engine-path purity (CI) ------------------------------------------------------------

ENGINE = ("metrics", "analysis", "boundaries", "rules", "decision", "power")
SIM_TOKENS = ("simulator", "SimConfig", "SimTruth", "SIMS_DIR", "configs/sims", "sim_truth", "load_sim")
SIM_TYPES = {SimConfig, SimTruth, Pool}


def _source(name: str) -> str:
    spec = importlib.util.find_spec(f"abtest.{name}")
    if spec is None:
        pytest.skip(f"abtest.{name} is not on this branch yet")
    return Path(spec.origin).read_text(encoding="utf-8")


def _types(t) -> typing.Iterator:
    """t and every type nested in it (X | None, list[X], ...)."""
    yield t
    for arg in typing.get_args(t):
        yield from _types(arg)


@pytest.mark.parametrize("name", ENGINE)
def test_engine_source_never_mentions_sim_ground_truth(name):
    found = [tok for tok in SIM_TOKENS if tok in _source(name)]
    assert not found, f"abtest.{name} mentions {found}; the engine path must never see sim truth (§2.3)"


@pytest.mark.parametrize("name", ENGINE)
def test_engine_functions_never_take_sim_types(name):
    _source(name)  # skip if absent
    mod = importlib.import_module(f"abtest.{name}")
    bad = [f"{fn_name}({p.name}: {p.annotation})"
           for fn_name, fn in inspect.getmembers(mod, inspect.isfunction)
           if fn.__module__ == mod.__name__ and not fn_name.startswith("_")
           for p in inspect.signature(fn, eval_str=True).parameters.values()
           if SIM_TYPES & set(_types(p.annotation))]
    assert not bad, f"abtest.{name} public functions take sim types: {bad}"


# --- scenario terminal states (local, data) ---------------------------------------------

def _missing() -> list[str]:
    out = []
    for name in ("metrics", "analysis", "boundaries", "rules", "decision", "runner"):
        try:
            mod = importlib.import_module(f"abtest.{name}")
        except ImportError as e:
            out.append(f"abtest.{name} ({e})")
            continue
        if name == "runner" and not hasattr(mod, "EXPECTED"):
            out.append("runner.EXPECTED")
    return out


_why = [f"engine not on this branch yet: {', '.join(m)}" for m in [_missing()] if m]
_why += [f"data files not in {DATA_DIR}"] if not all((DATA_DIR / f).exists() for f in FILES.values()) else []
needs_engine = pytest.mark.skipif(bool(_why), reason="; ".join(_why))
NAMES = sorted(p.stem for p in (CONFIGS_DIR / "scenarios").glob("*.yaml"))


def _run(name, tmp_path, monkeypatch):
    """Run a scenario into a tmp store; the JSONL mirror and truth report go to tmp_path."""
    from abtest import decision, runner
    monkeypatch.setattr(decision, "JSONL", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(simulator, "write_truth_report",
                        partial(simulator.write_truth_report, out_dir=tmp_path))
    conn = store.connect(tmp_path / "store.db")
    sc = config.load_scenario(name)
    runner.run_scenario(sc, conn)
    return sc, conn, store.get_state(conn, config.scenario_experiment(sc).id)


@pytest.mark.data
@needs_engine
@pytest.mark.parametrize("name", NAMES)
def test_scenario_reaches_expected_state(name, tmp_path, monkeypatch):
    from abtest import runner
    expected, by_look = runner.EXPECTED[name]  # every scenario config needs an expectation
    sc, conn, state = _run(name, tmp_path, monkeypatch)
    eid = state.experiment_id

    run = runner.summarize(conn, sc)
    assert run.expected == expected
    assert run.hit, (f"{name} seed {sc.seed}: {run.state} at look {run.stop_look}, expected {expected}"
                     + (f" by look {by_look}" if by_look else ""))

    rows = store.read_decisions(conn, eid)
    looks = [d for d in rows if d.kind == "look"]
    assert [d.look for d in looks] == list(range(1, state.looks_done + 1))  # one row per look
    assert all(d.reason.strip() for d in looks)
    changes = [d for d in rows if d.kind == "state_change"]
    assert changes and all(d.reason.strip() and d.actor.strip() for d in changes)

    if expected == State.HALTED_SRM:
        assert state.state == State.HALTED_SRM
    else:  # PLAN §4: SRM p above the threshold and no lead in both arms
        split = srm.split_summary(conn, eid)
        assert split.assigned.p > store.get_config(conn, eid).srm_p_threshold
        assert split.leads_in_both_arms == 0


@pytest.mark.data
@needs_engine
def test_approval_promotes_after_human_approval(tmp_path, monkeypatch):
    from abtest import decision
    _, conn, state = _run("approval", tmp_path, monkeypatch)
    assert state.state == State.PENDING_APPROVAL

    after = decision.approve(conn, state.experiment_id, "kartik", True)
    assert after.state == State.PROMOTED and after.allocation.B == 1.0
    assert store.get_state(conn, state.experiment_id).allocation.B == 1.0
    assert store.read_decisions(conn, state.experiment_id)[-1].actor == "kartik"
