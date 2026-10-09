"""metrics.lead_outcomes: trigger, maturation and per-lead aggregation (PLAN.md §2.6).

All data is built inside the tests; nothing needs data/.
"""

import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from abtest import config
from abtest.contracts import CONFIGS_DIR, ExperimentConfig, LeadOutcome
from abtest.metrics import OUTCOME_COLUMNS, lead_outcomes

MEETING = config.load_experiment(CONFIGS_DIR / "experiments" / "vani-meeting-v1.yaml")  # continuous secondary, maturation 2 d
BUYER = config.load_experiment(CONFIGS_DIR / "experiments" / "vani-buyer-qty-asks-v1.yaml")  # binary secondary, maturation 1 d
T0 = datetime(2026, 3, 1)
SUCCESS, OTHER = "Meeting Fixed", "Not Interested"


def at(**delta) -> datetime:
    return T0 + timedelta(**delta)


def calls_frame(rows) -> pd.DataFrame:
    """rows: (lead_id, ts, answered, disposition, duration_s)."""
    return pd.DataFrame(rows, columns=["lead_id", "ts", "answered", "disposition", "duration_s"])


def exposures_frame(rows) -> pd.DataFrame:
    """rows: (lead_id, variant, ts)."""
    return pd.DataFrame(rows, columns=["lead_id", "variant", "ts"])


def one(calls, exposures, cfg=MEETING, look=at(days=30)) -> pd.Series:
    """The single output row."""
    out = lead_outcomes(calls_frame(calls), exposures_frame(exposures), cfg, look)
    assert len(out) == 1
    return out.iloc[0]


# --- trigger -------------------------------------------------------------------------------

def test_trigger_is_the_first_answered_call():
    row = one([("L", at(hours=1), False, "Not Answered", None),
               ("L", at(hours=2), True, OTHER, 40.0),
               ("L", at(hours=30), True, SUCCESS, 90.0)],
              [("L", "B", T0)])
    assert row.triggered_ts == at(hours=2)
    assert row.n_calls == 3 and row.n_answered == 2
    assert row.primary == 1  # a success on a later call still counts for the lead (§2.6)


def test_a_later_pickup_never_re_triggers():
    # First pickup at day 1, second at day 2.5, look at day 3.2. Triggering on the later pickup
    # would make this lead immature (2.5 + 2 > 3.2); the first pickup makes it mature (1 + 2 <= 3.2).
    row = one([("L", at(days=1), True, OTHER, 30.0), ("L", at(days=2.5), True, OTHER, 30.0)],
              [("L", "A", T0)], look=at(days=3.2))
    assert row.triggered_ts == at(days=1)
    assert row.mature


def test_an_unanswered_only_lead_is_untriggered_with_na_primary():
    # The success disposition on the unanswered call is a data oddity; it must not leak.
    row = one([("L", at(hours=1), False, SUCCESS, 55.0), ("L", at(hours=5), False, "Not Answered", None)],
              [("L", "A", T0)])
    assert pd.isna(row.triggered_ts)
    assert not row.mature
    assert row.primary is pd.NA
    assert np.isnan(row.secondary)
    assert row.n_calls == 2 and row.n_answered == 0


def test_a_lead_with_no_calls_is_still_one_untriggered_row():
    out = lead_outcomes(calls_frame([]), exposures_frame([("L", "B", T0)]), MEETING, at(days=3))
    assert out.lead_id.tolist() == ["L"] and out.variant.tolist() == ["B"]
    assert out.n_calls.tolist() == [0] and out.n_answered.tolist() == [0]
    assert pd.isna(out.triggered_ts[0]) and pd.isna(out.primary[0]) and not out.mature[0]


def test_unanswered_success_disposition_does_not_count_for_a_triggered_lead():
    row = one([("L", at(hours=1), True, OTHER, 30.0), ("L", at(hours=2), False, SUCCESS, None)],
              [("L", "A", T0)])
    assert row.primary == 0


# --- maturation ----------------------------------------------------------------------------

def test_maturation_boundary_is_inclusive_to_the_second():
    calls = [("L", at(hours=3), True, SUCCESS, 60.0)]
    expo = [("L", "B", T0)]
    boundary = at(hours=3, days=MEETING.maturation_days)
    assert one(calls, expo, look=boundary).mature
    assert not one(calls, expo, look=boundary - timedelta(seconds=1)).mature
    assert one(calls, expo, look=boundary + timedelta(seconds=1)).mature


def test_maturation_days_may_be_fractional():
    cfg = MEETING.model_copy(update={"maturation_days": 0.5})
    calls = [("L", at(hours=1), True, SUCCESS, 60.0)]
    assert one(calls, [("L", "A", T0)], cfg, look=at(hours=13)).mature
    assert not one(calls, [("L", "A", T0)], cfg, look=at(hours=13, seconds=-1)).mature


def test_immature_lead_keeps_its_outcome_but_is_flagged():
    row = one([("L", at(hours=1), True, SUCCESS, 60.0)], [("L", "A", T0)], look=at(days=1))
    assert not row.mature and row.primary == 1  # the analysis filters on `mature`, not on NA


# --- which calls and exposures count -------------------------------------------------------

def test_calls_before_assignment_and_after_the_look_are_excluded():
    look = at(days=10)
    row = one([("L", at(days=1) - timedelta(seconds=1), True, SUCCESS, 99.0),  # before assignment
               ("L", at(days=1), True, OTHER, 10.0),                           # at assignment: counted
               ("L", look, True, OTHER, 30.0),                                 # at the look: counted
               ("L", look + timedelta(seconds=1), True, SUCCESS, 99.0)],       # after the look
              [("L", "B", at(days=1))], look=look)
    assert row.n_calls == 2 and row.n_answered == 2
    assert row.primary == 0
    assert row.secondary == pytest.approx(20.0)
    assert row.triggered_ts == at(days=1)


def test_a_call_after_the_look_never_triggers():
    row = one([("L", at(days=5), True, SUCCESS, 60.0)], [("L", "B", T0)], look=at(days=4))
    assert pd.isna(row.triggered_ts) and row.primary is pd.NA and row.n_calls == 0


def test_exposures_after_the_look_are_excluded():
    expo = [("early", "A", at(days=1)), ("late", "B", at(days=6))]
    calls = [("early", at(days=2), True, OTHER, 10.0), ("late", at(days=7), True, SUCCESS, 10.0)]
    out = lead_outcomes(calls_frame(calls), exposures_frame(expo), MEETING, at(days=5))
    assert out.lead_id.tolist() == ["early"]


def test_repeated_exposures_use_the_first_whatever_the_row_order():
    expo = [("L", "A", at(days=3)), ("L", "B", at(days=1)), ("L", "A", at(days=2))]
    calls = [("L", at(days=0.5), True, SUCCESS, 5.0),  # before the first exposure
             ("L", at(days=1.5), True, OTHER, 100.0),  # between the first and the other exposures
             ("L", at(days=2.5), True, OTHER, 200.0)]
    for order in ([0, 1, 2], [2, 1, 0], [1, 0, 2]):
        out = lead_outcomes(calls_frame(calls), exposures_frame([expo[i] for i in order]), MEETING, at(days=20))
        assert len(out) == 1
        assert out.variant[0] == "B" and out.assigned_ts[0] == at(days=1)
        assert out.n_calls[0] == 2 and out.primary[0] == 0
        assert out.triggered_ts[0] == at(days=1.5)


def test_same_instant_exposures_resolve_by_variant_whatever_the_row_order():
    # _first_exposures breaks a same-instant tie on variant, so "A" sorts first in either order.
    for expo in ([("L", "B", T0), ("L", "A", T0)], [("L", "A", T0), ("L", "B", T0)]):
        row = one([("L", at(hours=1), True, SUCCESS, 60.0)], expo)
        assert row.variant == "A" and row.assigned_ts == T0
        assert row.n_calls == 1 and row.primary == 1


def test_calls_of_unexposed_leads_are_ignored():
    # A dropped B exposure must show up as an SRM, so those leads cannot sneak in through calls.
    out = lead_outcomes(calls_frame([("ghost", at(hours=1), True, SUCCESS, 10.0),
                                     ("L", at(hours=1), True, OTHER, 10.0)]),
                        exposures_frame([("L", "A", T0)]), MEETING, at(days=5))
    assert out.lead_id.tolist() == ["L"] and out.n_calls.tolist() == [1]


def test_extra_columns_are_ignored():
    calls = calls_frame([("L", at(hours=1), True, SUCCESS, 10.0)]).assign(
        experiment_id="x", call_id="c1", attempt_no=1, bot_version="main_vani")
    expo = exposures_frame([("L", "B", T0)]).assign(experiment_id="x", bucket=7)
    out = lead_outcomes(calls, expo, MEETING, at(days=5))
    assert tuple(out.columns) == OUTCOME_COLUMNS


# --- per-lead metrics ----------------------------------------------------------------------

def test_continuous_secondary_is_the_mean_over_answered_calls_only():
    row = one([("L", at(hours=1), True, OTHER, 100.0),
               ("L", at(hours=2), True, OTHER, 200.0),
               ("L", at(hours=3), True, OTHER, np.nan),       # answered, duration missing: skipped
               ("L", at(hours=4), False, "Not Answered", 5000.0)],  # unanswered: never counted
              [("L", "A", T0)])
    assert row.secondary == pytest.approx(150.0)
    assert row.n_calls == 4 and row.n_answered == 3


def test_continuous_secondary_is_nan_when_no_answered_call_has_a_value():
    row = one([("L", at(hours=1), True, OTHER, np.nan)], [("L", "A", T0)])
    assert np.isnan(row.secondary)
    assert row.primary == 0 and row.triggered_ts == at(hours=1)  # still triggered


def test_a_lead_is_one_unit_however_many_calls_it_has():
    calls = [("many", at(minutes=m), True, SUCCESS, 30.0) for m in range(1, 21)]
    calls += [("one", at(minutes=1), True, OTHER, 30.0)]
    out = lead_outcomes(calls_frame(calls), exposures_frame([("many", "B", T0), ("one", "A", T0)]),
                        MEETING, at(days=9))
    assert out.set_index("lead_id").primary.to_dict() == {"many": 1, "one": 0}
    assert out.set_index("lead_id").n_calls.to_dict() == {"many": 20, "one": 1}


def test_binary_secondary_and_primary_use_their_own_disposition_sets():
    abrupt, enriched = "Abruptly Disconnected Call", "BL Enriched"
    out = lead_outcomes(
        calls_frame([("both", at(hours=1), True, abrupt, None), ("both", at(hours=2), True, enriched, None),
                     ("only_primary", at(hours=1), True, "BL Approved", None),
                     ("neither", at(hours=1), True, "Wrong Number", None),
                     ("unanswered_abrupt", at(hours=1), False, abrupt, None)]),
        exposures_frame([(k, "A", T0) for k in ("both", "only_primary", "neither", "unanswered_abrupt")]),
        BUYER, at(days=9)).set_index("lead_id")
    assert out.primary.astype(object).where(out.primary.notna(), None).to_dict() == {
        "both": 1, "only_primary": 1, "neither": 0, "unanswered_abrupt": None}
    assert out.secondary.fillna(-1).to_dict() == {"both": 1.0, "only_primary": 0.0, "neither": 0.0,
                                                  "unanswered_abrupt": -1.0}
    assert out.mature.to_dict() == {"both": True, "only_primary": True, "neither": True,
                                    "unanswered_abrupt": False}


def test_binary_secondary_needs_no_duration_column():
    calls = calls_frame([("L", at(hours=1), True, SUCCESS, 1.0)]).drop(columns="duration_s")
    out = lead_outcomes(calls, exposures_frame([("L", "A", T0)]), BUYER, at(days=9))
    assert out.n_calls.tolist() == [1]


def test_the_continuous_secondary_column_comes_from_config():
    cfg = MEETING.model_copy(update={"secondary": MEETING.secondary.model_copy(update={"column": "wait_s"})})
    calls = calls_frame([("L", at(hours=1), True, OTHER, 999.0)]).assign(wait_s=7.5)
    assert lead_outcomes(calls, exposures_frame([("L", "A", T0)]), cfg, at(days=9)).secondary[0] == 7.5
    with pytest.raises(ValueError, match="wait_s"):
        lead_outcomes(calls.drop(columns="wait_s"), exposures_frame([("L", "A", T0)]), cfg, at(days=9))


# --- input formats and output shape --------------------------------------------------------

def test_iso_string_timestamps_and_0_1_answered_match_native_types():
    native = calls_frame([("L1", at(hours=1), True, OTHER, 10.0), ("L1", at(hours=2, microseconds=250000), True, SUCCESS, 20.0),
                          ("L2", at(hours=1), False, "Not Answered", None)])
    expo = exposures_frame([("L1", "B", T0), ("L2", "A", at(seconds=30))])
    as_store = native.assign(ts=[t.isoformat() for t in native.ts], answered=native.answered.astype(int))
    expo_store = expo.assign(ts=[t.isoformat() for t in expo.ts])
    assert as_store.ts.tolist() == ["2026-03-01T01:00:00", "2026-03-01T02:00:00.250000", "2026-03-01T01:00:00"]  # mixed precision
    look = at(days=9)
    pd.testing.assert_frame_equal(lead_outcomes(as_store, expo_store, MEETING, look),
                                  lead_outcomes(native, expo, MEETING, look))


def test_output_columns_dtypes_sort_and_index():
    expo = exposures_frame([(k, "B" if i % 3 == 0 else "A", at(minutes=i)) for i, k in enumerate(["L10", "L2", "L1", "L33", "L3"])])
    calls = calls_frame([("L10", at(hours=1), True, SUCCESS, 5.0), ("L3", at(hours=1), False, "Not Answered", None)])
    out = lead_outcomes(calls, expo, MEETING, at(days=9))
    assert tuple(out.columns) == OUTCOME_COLUMNS == tuple(LeadOutcome.model_fields)
    assert out.lead_id.tolist() == sorted(["L10", "L2", "L1", "L33", "L3"])
    assert isinstance(out.index, pd.RangeIndex) and out.index.tolist() == list(range(5))
    assert [str(out[c].dtype) for c in OUTCOME_COLUMNS] == [
        "str", "str", "datetime64[ns]", "datetime64[ns]", "bool", "Int64", "float64", "int64", "int64"]


def test_empty_inputs_give_an_empty_frame_with_the_same_dtypes():
    expect = lead_outcomes(calls_frame([("L", at(hours=1), True, SUCCESS, 5.0)]),
                           exposures_frame([("L", "B", T0)]), MEETING, at(days=9)).dtypes
    empty = lead_outcomes(calls_frame([]), exposures_frame([]), MEETING, at(days=9))
    assert len(empty) == 0 and tuple(empty.columns) == OUTCOME_COLUMNS
    assert empty.dtypes.equals(expect)


def test_missing_columns_raise():
    with pytest.raises(ValueError, match="calls"):
        lead_outcomes(calls_frame([]).drop(columns="answered"), exposures_frame([]), MEETING, T0)
    with pytest.raises(ValueError, match="exposures"):
        lead_outcomes(calls_frame([]), exposures_frame([]).drop(columns="variant"), MEETING, T0)
    with pytest.raises(ValueError, match="duration_s"):
        lead_outcomes(calls_frame([]).drop(columns="duration_s"), exposures_frame([]), MEETING, T0)


def test_every_row_validates_as_a_lead_outcome():
    rng = np.random.default_rng(7)
    calls, expo = _random_inputs(rng, n_leads=40, strings=False)
    out = lead_outcomes(calls, expo, MEETING, at(days=9))
    assert out.triggered_ts.isna().any() and out.triggered_ts.notna().any()  # both kinds of row are covered
    as_none = out.astype(object).where(out.notna(), None)
    for rec in as_none.to_dict("records"):
        LeadOutcome.model_validate(rec)


# --- cross-check against a naive per-lead loop ---------------------------------------------

def _random_inputs(rng, n_leads: int, strings: bool):
    """Calls and exposures with repeats, leads without exposure and calls before/after the window."""
    leads = [f"L{i:03d}" for i in range(n_leads)]
    expo = []
    for lead in leads:
        if rng.random() < 0.9:  # 10% of leads are never exposed (their calls must be ignored)
            for _ in range(rng.choice([1, 1, 2, 3])):
                expo.append((lead, rng.choice(["A", "B"]), at(seconds=int(rng.integers(0, 6 * 86400)))))
    calls = []
    for lead in leads:
        for _ in range(rng.integers(0, 9)):
            answered = bool(rng.random() < 0.5)
            dur = np.nan if rng.random() < 0.15 else float(rng.gamma(2, 60)) if answered else float(rng.random())
            calls.append((lead, at(seconds=int(rng.integers(0, 14 * 86400))), answered,
                          str(rng.choice([SUCCESS, OTHER, "General", "Abruptly Disconnected Call", "BL Enriched"])), dur))
    c, e = calls_frame(calls), exposures_frame(expo)
    if strings:  # store form: ISO strings with mixed precision, 0/1 answered
        frac = lambda t, i: t.isoformat(timespec="seconds") if i % 2 else t.isoformat(timespec="microseconds")  # noqa: E731
        c = c.assign(ts=[frac(t, i) for i, t in enumerate(c.ts)], answered=c.answered.astype(int))
        e = e.assign(ts=[frac(t, i) for i, t in enumerate(e.ts)])
    return c, e


def _naive(calls, exposures, cfg: ExperimentConfig, look: datetime) -> pd.DataFrame:
    """The §2.6 definition, one lead at a time, no vectorisation."""
    sec, rows = cfg.secondary, []
    calls = calls.assign(ts=pd.to_datetime(calls.ts, format="ISO8601"), answered=calls.answered.astype(bool))
    expo = exposures.assign(ts=pd.to_datetime(exposures.ts, format="ISO8601"))
    for lead, grp in sorted(expo[expo.ts <= look].groupby("lead_id")):
        first = grp.sort_values(["ts", "variant"]).iloc[0]
        mine = calls[(calls.lead_id == lead) & (calls.ts >= first.ts) & (calls.ts <= look)]
        ans = mine[mine.answered]
        trig = ans.ts.min() if len(ans) else pd.NaT
        hit = lambda d: int(ans.disposition.isin(d).any())  # noqa: E731
        if pd.isna(trig):
            primary, secondary = pd.NA, np.nan
        else:
            primary = hit(cfg.primary.dispositions)
            if sec.type == "binary":
                secondary = float(hit(sec.dispositions))
            else:
                vals = ans[sec.column].dropna()
                secondary = float(vals.mean()) if len(vals) else np.nan
        rows.append(dict(lead_id=lead, variant=first.variant, assigned_ts=first.ts, triggered_ts=trig,
                         mature=bool(pd.notna(trig) and trig + pd.Timedelta(days=cfg.maturation_days) <= look),
                         primary=primary, secondary=secondary, n_calls=len(mine), n_answered=len(ans)))
    return pd.DataFrame(rows, columns=list(OUTCOME_COLUMNS))


@pytest.mark.parametrize("seed", range(10))
@pytest.mark.parametrize("cfg", [MEETING, BUYER], ids=["continuous", "binary"])
def test_matches_a_naive_per_lead_loop_and_ignores_row_order(seed, cfg):
    rng = np.random.default_rng(seed)
    calls, expo = _random_inputs(rng, n_leads=40, strings=bool(seed % 2))
    look = at(seconds=int(rng.integers(3 * 86400, 13 * 86400)))
    out = lead_outcomes(calls, expo, cfg, look)
    want = _naive(calls, expo, cfg, look)
    assert out.mature.any() and (~out.mature).any()  # the sample exercises both sides of maturation
    pd.testing.assert_frame_equal(out, want.astype(out.dtypes.to_dict()), check_exact=False)
    shuffled = lead_outcomes(calls.sample(frac=1, random_state=seed), expo.sample(frac=1, random_state=seed), cfg, look)
    pd.testing.assert_frame_equal(out, shuffled)


# --- speed ---------------------------------------------------------------------------------

def test_200k_calls_in_under_a_second():
    rng = np.random.default_rng(20261009)
    n_leads, n_calls = 60_000, 200_000
    ids = np.array([f"L{i}" for i in range(n_leads)])

    def iso(seconds: np.ndarray) -> pd.Series:  # the store keeps ISO strings, the slow input form
        return (pd.Series(np.datetime64(T0, "s") + seconds.astype("timedelta64[s]"))).dt.strftime("%Y-%m-%dT%H:%M:%S")

    expo = pd.DataFrame({"experiment_id": "x", "lead_id": ids, "variant": rng.choice(["A", "B"], n_leads, p=[0.9, 0.1]),
                         "bucket": 0, "ts": iso(rng.integers(0, 10 * 86400, n_leads))})
    calls = pd.DataFrame({"experiment_id": "x", "call_id": np.arange(n_calls).astype(str),
                          "lead_id": ids[rng.integers(0, n_leads, n_calls)],
                          "ts": iso(rng.integers(0, 14 * 86400, n_calls)),
                          "answered": (rng.random(n_calls) < 0.5).astype(int),
                          "disposition": rng.choice([SUCCESS, OTHER, "General"], n_calls),
                          "duration_s": rng.gamma(2, 60, n_calls), "attempt_no": 1})
    best = min(_timed(lambda: lead_outcomes(calls, expo, MEETING, at(days=12))) for _ in range(3))
    assert best < 1.0, f"lead_outcomes took {best:.2f}s on {n_calls:,} calls"


def _timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t
