import math
from datetime import datetime, timedelta

import pandas as pd
import pytest
from scipy.stats import chisquare, norm

from abtest import config, router, srm, store
from abtest.contracts import Allocation, Decision, State

CFG = config.load_experiment("configs/experiments/vani-meeting-v1.yaml")
FROZEN = config.freeze(CFG)
T0 = datetime(2026, 10, 1)
N = 20_000


def _exposures(leads: list[str], ts: datetime = T0, allocation: Allocation = CFG.allocation) -> pd.DataFrame:
    buckets = [router.bucket(CFG.salt, lead) for lead in leads]
    return pd.DataFrame({
        "experiment_id": CFG.id, "lead_id": leads,
        "variant": [router.variant_for(b, allocation) for b in buckets], "bucket": buckets,
        # odd rows carry microseconds, so stored ISO strings mix formats (as real runs do)
        "ts": [ts + timedelta(days=i % 3, seconds=i, microseconds=i % 2) for i in range(len(leads))],
    })


def _store(exposures: pd.DataFrame, answered_leads: list[str] = ()):
    conn = store.connect(":memory:")
    s = store.create_experiment(conn, FROZEN, scenario="unit", seed=0)
    store.put_state(conn, s.model_copy(update={"state": State.RUNNING, "allocation": CFG.allocation}))
    store.insert_df(conn, "exposures", exposures)
    calls = pd.DataFrame({"experiment_id": CFG.id, "lead_id": exposures.lead_id.unique()})
    calls["call_id"] = "c-" + calls.lead_id
    calls["answered"] = calls.lead_id.isin(answered_leads).astype(int)
    calls["ts"], calls["disposition"] = T0, "x"
    store.insert_df(conn, "calls", calls)
    return conn


def _honest(n: int = N) -> pd.DataFrame:
    return _exposures([f"sim-7-{i}" for i in range(n)])


def _dropped(n: int = N) -> pd.DataFrame:
    exp = _honest(n)
    b = exp.index[exp.variant == "B"]
    return exp.drop(b[: round(0.15 * len(b))])  # fault injection: 15% of B exposures never logged


def test_srm_test_matches_scipy_and_hand_wilson():
    n_a, n_b, share = 1830, 170, 0.1
    r = srm.srm_test(n_a, n_b, share)
    want = chisquare([n_a, n_b], f_exp=[2000 * 0.9, 2000 * 0.1])
    assert (r.chi2, r.p) == pytest.approx((want.statistic, want.pvalue))
    n, ph, z = n_a + n_b, n_b / (n_a + n_b), norm.ppf(0.975)
    centre = (ph + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    assert r.ci95 == pytest.approx((centre - half, centre + half))
    assert (r.n_A, r.n_B, r.configured_share_B, r.observed_share_B) == (n_a, n_b, share, 0.085)


def test_exact_split_gives_p_one():
    r = srm.srm_test(9000, 1000, 0.1)
    assert r.p == pytest.approx(1.0) and r.chi2 == pytest.approx(0.0)


def test_honest_routing_passes():
    s = srm.split_summary(_store(_honest()), CFG.id)
    assert s.configured_share_B == 0.1 and s.assigned.n_A + s.assigned.n_B == N
    assert s.assigned.p > CFG.srm_p_threshold
    assert s.assigned.ci95[0] < 0.1 < s.assigned.ci95[1]
    assert s.leads_in_both_arms == 0 and s.rehash_pass and s.rehash_checked == N
    assert [d.date for d in s.per_day] == [(T0 + timedelta(days=i)).date() for i in range(3)]
    assert sum(d.n_A + d.n_B for d in s.per_day) == N


def test_dropped_b_exposures_detected():
    s = srm.split_summary(_store(_dropped()), CFG.id)
    assert s.assigned.p < CFG.srm_p_threshold
    assert s.rehash_pass  # the surviving exposures are still routed correctly


def test_repeated_exposures_counted_once():
    exp = _honest(1000)
    again = exp.assign(ts=exp.ts + timedelta(days=5))
    s = srm.split_summary(_store(pd.concat([exp, again])), CFG.id)
    assert s.assigned.n_A + s.assigned.n_B == 1000 and s.rehash_checked == 2000
    assert sum(d.n_A + d.n_B for d in s.per_day) == 1000 and len(s.per_day) == 3


def test_conflicting_exposure_is_both_arms_and_fails_rehash():
    exp = _honest(1000)
    bad = exp.iloc[[0]].assign(variant="B" if exp.variant.iloc[0] == "A" else "A", ts=T0 + timedelta(days=4))
    s = srm.split_summary(_store(pd.concat([exp, bad])), CFG.id)
    assert s.leads_in_both_arms == 1 and s.rehash_mismatches == 1 and not s.rehash_pass
    assert s.assigned.n_A + s.assigned.n_B == 1000


def test_tampered_bucket_fails_rehash():
    exp = _honest(1000)
    exp.loc[5, "bucket"] = (exp.loc[5, "bucket"] + 1) % 10_000
    s = srm.split_summary(_store(exp), CFG.id)
    assert s.rehash_mismatches == 1 and not s.rehash_pass and s.leads_in_both_arms == 0


def test_exposures_after_running_state_change_excluded():
    exp = _honest(1000)
    promoted = Allocation(A=0.0, B=1.0)
    t1 = T0 + timedelta(days=10)
    after = pd.concat([_exposures(exp.lead_id.tolist(), t1, promoted),            # same leads, now B
                       _exposures([f"late-{i}" for i in range(500)], t1, promoted)])  # new leads, all B
    conn = _store(pd.concat([exp, after]))
    d = Decision(ts_wall=datetime.now(), ts_sim=t1, experiment_id=CFG.id, config_hash=FROZEN.config_hash,
                 prompt_hash_A=FROZEN.prompt_hash["A"], prompt_hash_B=FROZEN.prompt_hash["B"],
                 kind="state_change", look=None, info_fraction=None, n_A=None, n_B=None, primary=None,
                 guardrail=None, srm=None, decision="PROMOTED", reason="promoted", actor="engine",
                 from_state=State.RUNNING, to_state=State.PROMOTED)
    with conn:
        conn.execute("INSERT INTO decisions (experiment_id, ts_wall, ts_sim, kind, decision, reason, actor, "
                     "payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (CFG.id, d.ts_wall.isoformat(), t1.isoformat(), d.kind, d.decision, d.reason,
                      d.actor, d.model_dump_json()))
    s = srm.split_summary(conn, CFG.id)
    assert s.leads_in_both_arms == 0 and s.rehash_pass and s.rehash_checked == 1000
    assert s.assigned.n_A + s.assigned.n_B == 1000 and s.assigned.p > CFG.srm_p_threshold


def test_triggered_counts_only_answered_leads():
    exp = _honest(1000)
    answered = exp.lead_id.iloc[::4].tolist()  # every 4th lead answers
    conn = _store(exp, answered)
    with conn:  # an answered call from a lead with no logged exposure is not an assigned lead
        conn.execute("INSERT INTO calls (experiment_id, call_id, lead_id, ts, answered, disposition) "
                     "VALUES (?, 'orphan', 'orphan-lead', ?, 1, 'x')", (CFG.id, T0.isoformat()))
    s = srm.split_summary(conn, CFG.id)
    t = s.triggered
    assert t.n_A + t.n_B == 250
    assert t.n_B == int((exp.set_index("lead_id").loc[answered].variant == "B").sum())


def test_split_report_writes_md_and_png(tmp_path):
    ok = srm.split_report(_store(_honest(5000)), CFG.id, tmp_path / "ok")
    bad = srm.split_report(_store(_dropped()), CFG.id, tmp_path / "bad")
    assert ok.name == "split_unit.md"
    for md in (ok, bad):
        assert md.with_suffix(".png").read_bytes()[:4] == b"\x89PNG"
    text = ok.read_text()
    assert "configured 10.00%, achieved " in text and text.count(": OK") == 2
    assert "Leads in both arms: 0" in text and "Re-hash determinism: PASS (checked 5,000, mismatches 0)" in text
    assert "| Date | A | B | Share B |" in text and "## Method" in text
    assert "**SRM DETECTED**" in bad.read_text() and "SRM DETECTED" not in text
