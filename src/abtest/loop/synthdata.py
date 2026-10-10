"""Synthetic buyer call log for the loop's simulated live A/B (PLAN_LOOP.md §L9.3, contract §L7).

The organisers' `Call_Logs_Hackathon.csv` is not on every machine, and the loop's live A/B runs
simulated traffic through the real engine, so this writes a file in the buyer source format that
`data.load("buyer_logs", dir)` reads. It is shaped by reports/data_profile_buyer_logs.md: 5 days
(5-9 Oct 2026), ~825 leads with 1-3 calls each, ~76% of calls answered, and the dispositions that
matter to the template (BL Enriched / BL Approved = success, Abruptly Disconnected Call = the
guardrail).

Quotas, not coin flips: with ~640 triggered leads an iid draw misses a 0.667 target by more than
0.03 in about one seed in ten (sd 0.019). Every lead-level count below (triggered, successful,
abrupt, overlap) is therefore fixed exactly from the target rate and only *which* leads get it is
random, so the realised rates sit on the targets for every seed. Deterministic in `seed`: one
numpy Generator, no hashing of Python objects, no set iteration.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from abtest.data import BUYER_TS_FORMAT, FILES

GENERATOR_VERSION = 1            # bump when the generated data changes (live_ab re-writes a stale cache)
MANIFEST = "synthetic_manifest.json"

N_LEADS = 825
DAYS = pd.date_range("2026-10-05", periods=5, freq="D")
DAY_WEIGHTS = (229, 89, 253, 169, 229)                 # calls per day, data_profile_buyer_logs §5
# Calls per lead (profile §4: 707 / 99 / 19 of 825 for 1 / 2 / 3+); 3+ is capped at 3 here.
CALLS_PER_LEAD = ((1, 707 / 825), (2, 99 / 825), (3, 19 / 825))
TRIGGERED_SHARE = 642 / 824                            # leads with an answered call (profile §2)
P_SUCCESS = 0.6667                                     # template baseline_p, per triggered lead
P_ABRUPT = 0.19                                        # abrupt-disconnect rate, per triggered lead
ABRUPT_MULTI_SHARE = 0.45                              # share of multi-call triggered leads that disconnect once
ABRUPT_THEN_BL = 0.6                                   # of those, how many succeed on a later call
FILLER_UNANSWERED = 0.2                                # a retry call that nobody picked up
HOUR_WEIGHTS = (1, .5, .3, .3, .3, .5, 1, 2, 4, 7, 9, 9, 8, 8, 8, 8, 7, 6, 5, 4, 3, 2, 1.5, 1.2)

PRIMARY = {"BL Enriched": 411, "BL Approved": 59}      # answered successes, profile §9
ABRUPT = "Abruptly Disconnected Call"
# Answered calls that are neither a success nor a disconnect (profile §9, is_call_answered = 1).
OTHER_ANSWERED = {"Technical Issue - Call Not Connected": 70, "Queue Drop": 37, "No Product Requirement": 21,
                  "Particular Company Details": 11, "Selling Related": 8, "Job Related_BL Not Approved": 5,
                  "Manual Call": 4, "BL Generated": 4, "Technical Issue": 3, "Language Barrier": 2,
                  "BL Limit Exhausted": 2, "Enrichment Screen": 1}
# Unanswered calls (profile §9). The first two have a null is_call_answered in the real file.
UNANSWERED = {"No Enrichment Scope": 102, "Foreign or Disabled Users": 9, "Call Forwarded": 1,
              "Call Forwarded - API Timeout": 7, "Call Forwarded - No Product": 9, "Language Barrier": 4,
              "Manual Call": 1, "No Product Requirement": 13, "Particular Company Details": 1,
              "Queue Drop": 7, "Selling Related": 2, "Technical Issue - Call Not Connected": 10}
NULL_ANSWERED = frozenset({"No Enrichment Scope", "Foreign or Disabled Users"})
# A retry before the decisive call that nobody answered (a screen-out would not be retried).
FILLER_UNANSWERED_DISP = {"Call Forwarded - API Timeout": 7, "Call Forwarded - No Product": 9,
                          "Technical Issue - Call Not Connected": 10, "Queue Drop": 7,
                          "No Product Requirement": 13}
REASON_CODE = {"answered": 380, "No Enrichment Scope": 386, "Foreign or Disabled Users": 383, "other": 385}
RECORDING_HOST = "https://recordings.synthetic.invalid"  # .invalid never resolves: no real recording exists


def _pick(rng: np.random.Generator, weights: dict[str, int], size: int | None = None):
    names = list(weights)
    p = np.array([weights[n] for n in names], dtype=float)
    out = rng.choice(len(names), size=size, p=p / p.sum())
    return names[out] if size is None else [names[i] for i in out]


def _filler(rng: np.random.Generator) -> tuple[int, str]:
    """One retry call that is not the decisive one: usually answered, sometimes nobody picks up."""
    if rng.random() < FILLER_UNANSWERED:
        return 0, _pick(rng, FILLER_UNANSWERED_DISP)
    return 1, _pick(rng, OTHER_ANSWERED)


def _lead_calls(kind: str, k: int, rng: np.random.Generator) -> list[tuple[int, str]]:
    """(answered, disposition) for each of a lead's k calls, in call order."""
    if kind == "untriggered":
        return [(0, _pick(rng, UNANSWERED)) for _ in range(k)]
    bl = _pick(rng, PRIMARY)
    if kind == "success":
        return [_filler(rng) for _ in range(k - 1)] + [(1, bl)]
    if kind == "abrupt":
        return [(1, ABRUPT)] + [_filler(rng) for _ in range(k - 1)]
    if kind == "both":                                 # disconnected first, a retry converted
        return [(1, ABRUPT)] + [_filler(rng) for _ in range(k - 2)] + [(1, bl)]
    calls = [_filler(rng) for _ in range(k - 1)]       # "other": answered, neither success nor disconnect
    return calls + [(1, _pick(rng, OTHER_ANSWERED))]


def _assign_kinds(rng: np.random.Generator, n_calls: np.ndarray) -> np.ndarray:
    """Lead-level outcome class with exact quotas: untriggered / success / abrupt / both / other."""
    n = len(n_calls)
    order = rng.permutation(n)
    n_trig = round(TRIGGERED_SHARE * n)
    trig = order[:n_trig]
    kinds = np.full(n, "untriggered", dtype=object)
    multi = trig[n_calls[trig] >= 2]
    single = trig[n_calls[trig] == 1]
    n_abrupt, n_success = round(P_ABRUPT * n_trig), round(P_SUCCESS * n_trig)

    a_multi = min(round(ABRUPT_MULTI_SHARE * len(multi)), n_abrupt)
    abrupt_multi = rng.choice(multi, a_multi, replace=False)
    abrupt_single = rng.choice(single, n_abrupt - a_multi, replace=False)
    both = rng.choice(abrupt_multi, round(ABRUPT_THEN_BL * a_multi), replace=False)
    rest = np.setdiff1d(trig, np.concatenate([abrupt_multi, abrupt_single]))
    success = rng.choice(rest, n_success - len(both), replace=False)

    kinds[trig] = "other"
    kinds[abrupt_multi] = "abrupt"
    kinds[abrupt_single] = "abrupt"
    kinds[both] = "both"
    kinds[success] = "success"
    return kinds


def generate(seed: int = 20261010, n_leads: int = N_LEADS) -> pd.DataFrame:
    """The synthetic call log as a DataFrame in the source CSV's columns and order."""
    rng = np.random.default_rng(seed)
    counts = [round(share * n_leads) for _, share in CALLS_PER_LEAD[1:]]
    n_calls = np.repeat([1, 2, 3], [n_leads - sum(counts), *counts])
    rng.shuffle(n_calls)
    kinds = _assign_kinds(rng, n_calls)
    lead_ids = rng.choice(89_999_999, n_leads, replace=False) + 10_000_000   # unique 8-digit ids

    days = rng.choice(len(DAYS), size=n_leads, p=np.array(DAY_WEIGHTS) / sum(DAY_WEIGHTS))
    hours = rng.choice(24, size=n_leads, p=np.array(HOUR_WEIGHTS) / sum(HOUR_WEIGHTS))
    minutes = rng.integers(0, 60, n_leads)
    end = len(DAYS) * 1440 - 1          # minutes from midnight of the first day

    rows = []
    for i in range(n_leads):
        t = float(days[i] * 1440 + hours[i] * 60 + minutes[i])
        for seq, (answered, disp) in enumerate(_lead_calls(kinds[i], int(n_calls[i]), rng)):
            if seq:  # retries come minutes later (profile §8a: p95 25 min), 3% the next day
                t = min(t + (float(rng.uniform(20, 26)) * 60 if rng.random() < 0.03
                             else float(rng.exponential(6.0))), end)
            rows.append((int(t), i, seq, answered, disp))
    df = pd.DataFrame(rows, columns=["minute", "lead", "seq", "answered", "disposition"])
    df = df.sort_values(["minute", "lead", "seq"], kind="stable", ignore_index=True)
    df["ts"] = DAYS[0] + pd.to_timedelta(df.minute, unit="min")

    call_id = 7_000_001 + np.arange(len(df))           # ids grow with time, like a real call-records key
    answered = pd.array(df.answered.to_numpy(), dtype="Int64")
    answered[(df.disposition.isin(NULL_ANSWERED) & df.answered.eq(0)).to_numpy()] = pd.NA  # null in the real file
    code = np.where(df.answered.eq(1), REASON_CODE["answered"],
                    df.disposition.map(REASON_CODE).fillna(REASON_CODE["other"]).astype(int))
    return pd.DataFrame({
        "fk_leap_call_records_id": call_id,
        "fk_glusr_usr_id": lead_ids[df.lead.to_numpy()],
        "entered_on": df.ts.dt.strftime(BUYER_TS_FORMAT),
        "is_call_answered": answered,
        "disposition_remarks": df.disposition,
        "call_disposition_reason": code,
        "call_recording_url": np.where(df.answered.eq(1), [f"{RECORDING_HOST}/{c}.mp3" for c in call_id], ""),
    })


def write_synthetic(out_dir: str | Path, seed: int = 20261010) -> dict[str, Path]:
    """Writes `Call_Logs_Hackathon.csv` (+ a manifest naming the seed and generator version) into
    `out_dir` and returns {"buyer_logs": path}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / FILES["buyer_logs"]
    generate(seed).to_csv(path, index=False)
    (out / MANIFEST).write_text(json.dumps({"seed": seed, "generator_version": GENERATOR_VERSION,
                                            "leads": N_LEADS}, sort_keys=True), encoding="utf-8")
    return {"buyer_logs": path}


def is_current(out_dir: str | Path, seed: int) -> bool:
    """True if `out_dir` already holds this generator version's data for `seed`."""
    out = Path(out_dir)
    try:
        m = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (out / FILES["buyer_logs"]).exists() and m.get("seed") == seed \
        and m.get("generator_version") == GENERATOR_VERSION
