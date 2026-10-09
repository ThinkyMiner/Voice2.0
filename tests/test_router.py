"""Router (PLAN.md §2.4): stickiness, cross-process determinism, uniformity, salt independence,
threshold edges, live allocation after a decision, and exposure logging."""

import json
import os
import subprocess
import sys
from datetime import datetime

import numpy as np
import pytest
from hypothesis import given, strategies as st
from scipy import stats

from abtest import config, router, store
from abtest.contracts import Allocation, ExperimentState, State
from abtest.router import BUCKETS

CFG_PATH = "configs/experiments/vani-meeting-v1.yaml"
P_MIN = 0.001             # test significance floor
N_IDS = 100_000           # sequential lead ids for the distribution tests
N_CROSS_PROCESS = 3_000
DECILES = 10
SHARE_B = 0.10
CONFIDENCE = 0.999
TS = datetime(2026, 10, 9, 12, 0, 0, 123456)

shares = st.integers(0, BUCKETS).map(lambda k: k / BUCKETS)


def _alloc(b: float) -> Allocation:
    return Allocation(A=1 - b, B=b)


def _state(salt: str = "s", b: float = SHARE_B) -> ExperimentState:
    return ExperimentState(experiment_id="e", state=State.RUNNING, allocation=_alloc(b), salt=salt,
                           config_hash="c", prompt_hash={"A": "pa", "B": "pb"})


def _running_store(path) -> tuple:
    cfg = config.load_experiment(CFG_PATH)
    conn = store.connect(path)
    s = store.create_experiment(conn, config.freeze(cfg))
    store.put_state(conn, s.model_copy(update={"state": State.RUNNING}))
    return conn, cfg.id


@pytest.fixture
def db(tmp_path):
    conn, exp_id = _running_store(tmp_path / "t.db")
    yield conn, exp_id
    conn.close()


@pytest.fixture(scope="module")
def shared_db(tmp_path_factory):
    conn, exp_id = _running_store(tmp_path_factory.mktemp("router") / "t.db")
    yield conn, exp_id
    conn.close()


def _buckets(salt: str) -> np.ndarray:
    return np.array([router.bucket(salt, str(i)) for i in range(N_IDS)])


def _is_b(salt: str) -> np.ndarray:
    alloc = _alloc(SHARE_B)
    return np.array([router.variant_for(int(x), alloc) == "B" for x in _buckets(salt)])


# --- stickiness ----------------------------------------------------------------------------

@given(salt=st.text(min_size=1), lead=st.text(), others=st.lists(st.text()), b=shares)
def test_route_sticky_under_interleaving(salt, lead, others, b):
    s = _state(salt, b)
    first = router.route(s, lead)
    for other in others:
        assert router.route(s, other) == router.route(_state(salt, b), other)
        assert router.route(s, lead) == first
    assert first.variant == router.variant_for(router.bucket(salt, lead), s.allocation)
    assert first.prompt_hash == s.prompt_hash[first.variant] and first.state == State.RUNNING


@given(lead=st.text())
def test_assign_agrees_with_route_while_running(shared_db, lead):
    conn, exp_id = shared_db
    assert router.assign(conn, exp_id, lead, TS) == router.route(store.get_state(conn, exp_id), lead)


# --- determinism, uniformity, independence -------------------------------------------------

def test_cross_process_determinism():
    ids = [f"lead-{i}" for i in range(N_CROSS_PROCESS)] + ["", "ünïcødé-लीड", "a:b"]
    here = [router.bucket("7f3a91", i) for i in ids]
    code = ("import json, sys; from abtest.router import bucket; "
            "print(json.dumps([bucket('7f3a91', i) for i in json.load(sys.stdin)]))")
    for seed in ("1", "2"):
        out = subprocess.run([sys.executable, "-c", code], input=json.dumps(ids), text=True,
                             capture_output=True, check=True, env={**os.environ, "PYTHONHASHSEED": seed})
        assert json.loads(out.stdout) == here


@given(salt=st.text(), lead=st.text())
def test_bucket_in_range(salt, lead):
    assert 0 <= router.bucket(salt, lead) <= BUCKETS - 1


def test_bucket_deciles_uniform():
    b = _buckets("7f3a91")
    assert b.min() >= 0 and b.max() <= BUCKETS - 1
    counts = np.bincount(b * DECILES // BUCKETS, minlength=DECILES)
    assert stats.chisquare(counts).pvalue > P_MIN


def test_achieved_share_within_binomial_interval():
    n_b = _is_b("7f3a91").sum()
    lo, hi = stats.binom.interval(CONFIDENCE, N_IDS, SHARE_B)
    assert lo <= n_b <= hi


def test_salt_independence():
    x, y = _is_b("7f3a91"), _is_b("another-salt")
    table = [[np.sum(~x & ~y), np.sum(~x & y)], [np.sum(x & ~y), np.sum(x & y)]]
    assert stats.chi2_contingency(table, correction=False).pvalue > P_MIN
    assert not np.array_equal(x, y)


# --- edges ---------------------------------------------------------------------------------

@given(b=st.integers(0, BUCKETS - 1))
def test_zero_and_full_share(b):
    assert router.variant_for(b, _alloc(0.0)) == "A"
    assert router.variant_for(b, _alloc(1.0)) == "B"


@given(k=st.integers(0, BUCKETS))
def test_threshold_bucket_goes_to_a(k):
    alloc = _alloc(k / BUCKETS)
    if k < BUCKETS:
        assert router.variant_for(k, alloc) == "A"
    if k > 0:
        assert router.variant_for(k - 1, alloc) == "B"


def test_known_threshold():
    alloc = Allocation(A=0.9, B=0.1)
    assert router.variant_for(999, alloc) == "B"
    assert router.variant_for(1000, alloc) == "A"


# --- live allocation after a decision ------------------------------------------------------

@pytest.mark.parametrize("decided, live, want", [
    (State.PROMOTED, Allocation(A=0.0, B=1.0), "B"),
    (State.STOPPED_HARM, Allocation(A=1.0, B=0.0), "A"),
])
def test_live_allocation_after_decision(db, decided, live, want):
    conn, exp_id = db
    leads = [f"lead-{i}" for i in range(N_CROSS_PROCESS)]
    running = store.get_state(conn, exp_id)
    assert {router.route(running, lead).variant for lead in leads} == {"A", "B"}
    store.put_state(conn, running.model_copy(update={"state": decided, "allocation": live}))
    for lead in leads:
        a = router.assign(conn, exp_id, lead, TS)
        assert (a.variant, a.state) == (want, decided)


# --- exposure logging ----------------------------------------------------------------------

def test_assign_logs_one_row_per_call(db):
    conn, exp_id = db
    a = [router.assign(conn, exp_id, "lead-1", TS) for _ in range(3)]
    rows = [tuple(r) for r in conn.execute("SELECT * FROM exposures")]
    assert rows == [(exp_id, "lead-1", a[0].variant, a[0].bucket, TS.isoformat())] * 3
    assert a[0].bucket == router.bucket(store.get_state(conn, exp_id).salt, "lead-1")


def test_assign_unknown_experiment(db):
    conn, _ = db
    with pytest.raises(KeyError, match="nope"):
        router.assign(conn, "nope", "lead-1", TS)
    assert conn.execute("SELECT COUNT(*) FROM exposures").fetchone()[0] == 0
