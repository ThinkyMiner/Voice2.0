"""Synthetic per-call rubric_risk for simulated traffic (PLAN_LOOP.md §L2.11, §L9.3, contract §L7).

In sim mode no call has a transcript to judge, so every answered call gets a `rubric_risk` drawn
from its disposition: a base level (`sim.risk_base`, e.g. a successful BL call is low risk, an abrupt
disconnect is high risk), plus Gaussian noise, plus a shift in arm B. The shift is ground truth the
loop injects (`injected_risk_shift`); only this module and live_ab.py read it, never the engine path.

Deterministic per (seed, call_id): the noise is a function of those two values alone, not of row
order or of which other calls are in the frame, so a call keeps its score when the same experiment
is scored again at a later look (the frame then holds more calls).
"""

import hashlib
from collections.abc import Mapping
from functools import lru_cache

import numpy as np
import pandas as pd
from scipy.stats import norm

_U_EDGE = 1e-12  # keep the uniform strictly inside (0, 1) so norm.ppf is finite


@lru_cache(maxsize=262_144)
def _normal(seed: int, call_id: str) -> float:
    """A standard normal that depends only on (seed, call_id): hash -> uniform -> inverse CDF."""
    h = hashlib.blake2b(f"{seed}:{call_id}".encode(), digest_size=8).digest()
    u = (int.from_bytes(h, "big") + 0.5) / 2**64
    return float(norm.ppf(min(max(u, _U_EDGE), 1 - _U_EDGE)))


def score_sim_calls(calls: pd.DataFrame, variant_by_lead: Mapping[str, str], shift_b: float,
                    risk_base: Mapping[str, float], noise_sd: float, seed: int) -> pd.DataFrame:
    """call_id, rubric_risk for the answered calls of `calls`, in their input order.

    rubric_risk = clip(base[disposition] + N(0, noise_sd) + shift_b if the lead is in arm B, 0, 1).
    A disposition missing from `risk_base` uses its "default" entry; a lead missing from
    `variant_by_lead` is treated as arm A (no shift)."""
    if "default" not in risk_base:
        raise ValueError("risk_base needs a 'default' entry for dispositions it does not list")
    missing = [c for c in ("call_id", "lead_id", "answered", "disposition") if c not in calls.columns]
    if missing:
        raise ValueError(f"calls is missing columns {missing}")
    answered = calls[calls["answered"].fillna(False).astype(bool)]
    base = answered["disposition"].map(dict(risk_base)).fillna(risk_base["default"]).to_numpy(dtype=float)
    noise = np.fromiter((_normal(seed, str(c)) for c in answered["call_id"]), dtype=float,
                        count=len(answered)) * noise_sd
    in_b = answered["lead_id"].astype(str).map(dict(variant_by_lead)).eq("B").to_numpy()
    risk = np.clip(base + noise + np.where(in_b, shift_b, 0.0), 0.0, 1.0)
    return pd.DataFrame({"call_id": answered["call_id"].to_numpy(), "rubric_risk": risk})
