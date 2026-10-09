"""Cluster-bootstrap replay with effect and fault injection (PLAN.md §2.5).

Pool unit = lead-window: one historical lead's calls inside one non-overlapping `window.days`
block of the reference period (the blocks of profile.volume_stats), timed relative to that
lead's first call in the block. Each sim day draws pool leads with replacement, renames them
sim-<seed>-<n>, routes them through the real router and injects the sim config's true effect
into arm B. Only this module reads the sim config; the engine never does.
"""

import math
from pathlib import Path

import numpy as np
import pandas as pd

from abtest import router
from abtest.contracts import (DATA_DIR, REPORTS_DIR, ExperimentConfig, ExperimentState, Pool,
                              SimConfig, SimDay, SimTruth)
from abtest.data import load

DAY = np.timedelta64(1, "D")
EXPOSURE_COLUMNS = ("experiment_id", "lead_id", "variant", "bucket", "ts")
CALL_COLUMNS = ("experiment_id", "call_id", "lead_id", "ts", "answered", "disposition",
                "duration_s", "attempt_no")


def reference_calls(df: pd.DataFrame, sim: SimConfig) -> pd.DataFrame:
    """Calls whose calendar date lies in [reference.from, reference.to] (None = unbounded) and,
    if set, whose bot_version is in reference.bot_version."""
    ref, day = sim.reference, df.ts.dt.normalize()
    keep = pd.Series(True, index=df.index)
    if ref.from_ is not None:
        keep &= day >= pd.Timestamp(ref.from_)
    if ref.to is not None:
        keep &= day <= pd.Timestamp(ref.to)
    if ref.bot_version is not None:
        keep &= df.bot_version.isin(ref.bot_version)
    return df[keep]


def _sets(cfg: ExperimentConfig) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(primary dispositions, secondary dispositions); the latter is empty for a continuous one."""
    return cfg.primary.dispositions, cfg.secondary.dispositions or ()


def _outcomes(lead: np.ndarray, m: int, ans: np.ndarray, disp: np.ndarray, val: np.ndarray,
              cfg: ExperimentConfig):
    """Per lead 0..m-1 over its calls: triggered, primary (0/1), secondary (0/1 or the mean of
    `val` over answered calls), both NaN if untriggered, and has_other (an answered call in
    neither disposition set)."""
    prim, sec = _sets(cfg)
    is_p, is_s = np.isin(disp, prim), np.isin(disp, sec)

    def any_(mask):
        return np.bincount(lead[mask], minlength=m) > 0

    trig = any_(ans)
    primary = np.where(trig, any_(ans & is_p), np.nan)
    if cfg.secondary.type == "binary":
        secondary = np.where(trig, any_(ans & is_s), np.nan)
    else:
        ok = ans & ~np.isnan(val)
        with np.errstate(invalid="ignore"):  # 0/0 -> NaN for leads with no answered value
            secondary = np.bincount(lead[ok], val[ok], m) / np.bincount(lead[ok], minlength=m)
    return trig, primary, secondary, any_(ans & ~is_p & ~is_s)


def build_pool(cfg: ExperimentConfig, sim: SimConfig, data_dir: Path = DATA_DIR) -> Pool:
    df = reference_calls(load(cfg.dataset, data_dir, cfg.exclude_leads_with_calls_gt)[0], sim)
    if df.empty:
        raise ValueError("the sim reference period and bot_version filter leave no calls")
    start = (df.ts.max().normalize() + pd.Timedelta(days=1)).to_pydatetime()
    W = cfg.window.days
    day = (df.ts.dt.normalize() - df.ts.min().normalize()).dt.days
    span = int(day.max()) + 1
    length = W if span >= W else span  # as volume_stats: a span shorter than a block is one block
    block = day // length
    keep = (block < span // length).to_numpy()  # drop the partial trailing block
    df, block = df[keep].reset_index(drop=True), block.to_numpy()[keep]

    # data.load sorts by (lead_id, ts, call_id), so each lead-window is a contiguous run of rows
    new = (df.lead_id.ne(df.lead_id.shift()) | (block != np.r_[-1, block[:-1]])).to_numpy()
    row = np.flatnonzero(new)               # first row of each pool lead
    pl = np.cumsum(new) - 1                 # pool lead of each call
    ts = df.ts.to_numpy()
    first = ts[row]
    col = cfg.secondary.column
    calls = df[list(dict.fromkeys(["answered", "disposition", "duration_s", "attempt_no", col]))]
    calls = calls.assign(pl=pl, k=np.arange(len(df)) - row[pl], offset=ts - first[pl])
    trig, prim, sec, other = _outcomes(pl, len(row), df.answered.to_numpy(),
                                       df.disposition.to_numpy(), df[col].to_numpy(float), cfg)
    leads = pd.DataFrame({
        "lead_id": df.lead_id.to_numpy()[row], "block": block[row], "row": row,
        "n_calls": np.diff(np.r_[row, len(df)]), "tod": first - first.astype("datetime64[D]"),
        "triggered": trig, "primary": prim, "secondary": sec, "has_other": other,
    })
    return Pool(calls=calls, leads=leads, start=start, p=float(np.mean(prim[trig])),
                secondary_base=float(np.nanmean(sec[trig])))


def _draw(rng: np.random.Generator, mask: pd.Series, k: int) -> np.ndarray:
    """Positions of k pool calls drawn uniformly, with replacement, from those where mask holds."""
    rows = np.flatnonzero(mask.to_numpy())
    if k and not len(rows):
        raise ValueError("no pool call to resample from")
    return rows[rng.integers(0, max(len(rows), 1), k)]


def _last(lead: np.ndarray, mask: np.ndarray, m: int) -> np.ndarray:
    """Position of each lead's last call where mask holds, -1 if none (calls sorted by lead, ts)."""
    out = np.full(m, -1)
    pos = np.flatnonzero(mask)
    np.maximum.at(out, lead[pos], pos)
    return out


def _q(pool: Pool, L: float) -> float:
    """Per-lead conversion probability of the primary injection (§2.5)."""
    if L <= 0:
        return -L
    q = pool.p * L / (1 - pool.p)  # ponytail: full lead-window p vs in-window injection; truth report measures the realised lift
    if q > 1:
        raise ValueError(f"primary_rel_lift {L} needs conversion probability {q:.3f} > 1 at p = {pool.p:.4f}")
    return q


def _eligible_share(pool: Pool, L: float, q: float) -> float:
    """Expected share of triggered B leads eligible for the binary secondary injection: not
    touched by the primary injection, not already in the secondary set, with an answered call in
    neither set."""
    t = pool.leads[pool.leads.triggered]
    touch = q * (t.primary == (0 if L > 0 else 1)) if L else 0.0
    return float(((1 - touch) * (t.secondary == 0) * t.has_other).mean())


def simulate_day(pool: Pool, cfg: ExperimentConfig, sim: SimConfig, state: ExperimentState,
                 day: int, seed: int) -> SimDay:
    W = cfg.window.days
    if not 0 <= day < W:
        raise ValueError(f"day {day} is outside the {W}-day window")
    rng = np.random.default_rng((seed, day))
    R = sim.volume.daily_leads * sim.volume.scale
    n0 = math.floor(day * R)
    m = math.floor((day + 1) * R) - n0
    P, C = pool.leads, pool.calls
    pick = rng.integers(0, len(P), m)
    u_drop, u_prim, u_sec = rng.random((3, m))

    ids = np.array([f"sim-{seed}-{n}" for n in range(n0, n0 + m)], dtype=object)
    routed = [router.route(state, i) for i in ids]
    is_b = np.array([a.variant == "B" for a in routed], dtype=bool)
    logged = ~(is_b & (u_drop < sim.fault.drop_b_exposure_rate))

    # gather each picked lead-window's calls and re-time them onto this sim day
    n = P.n_calls.to_numpy()[pick]
    lead = np.repeat(np.arange(m), n)
    rows = np.repeat(P.row.to_numpy()[pick], n) + np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
    t0 = np.datetime64(pool.start, "us") + day * DAY + P.tod.to_numpy()[pick]
    ts = t0[lead] + C.offset.to_numpy()[rows]
    keep = ts < np.datetime64(pool.start, "us") + W * DAY  # never generate calls after the window
    lead, rows, ts = lead[keep], rows[keep], ts[keep]
    g = C.take(rows)
    ans = g.answered.to_numpy()
    disp = g.disposition.to_numpy(dtype=object, copy=True)
    col = cfg.secondary.column
    vals = {k: g[k].to_numpy(dtype=float, copy=True) for k in dict.fromkeys(["duration_s", col])}
    dur, val = vals["duration_s"], vals[col]  # the same array when col == "duration_s"
    trig, prim_cf, sec_cf, other = _outcomes(lead, m, ans, disp, val, cfg)
    trig_b = is_b & trig
    prim, sec = _sets(cfg)
    L, c = sim.true_effect.primary_rel_lift, sim.true_effect.secondary_change
    q = _q(pool, L)

    # arm B primary: L > 0 converts failures at their last answered call; L < 0 strips successes
    hit = trig_b & (prim_cf == (0 if L > 0 else 1)) & (u_prim < q)
    if L > 0:
        tgt = _last(lead, ans, m)[hit]
        src = _draw(rng, C.answered & C.disposition.eq(prim[0]), len(tgt))
        disp[tgt], dur[tgt] = prim[0], C.duration_s.to_numpy()[src]
    elif L < 0:
        tgt = np.flatnonzero(hit[lead] & ans & np.isin(disp, prim))
        src = _draw(rng, C.answered & ~C.disposition.isin(prim), len(tgt))
        disp[tgt], dur[tgt] = C.disposition.take(src).to_numpy(dtype=object), C.duration_s.to_numpy()[src]

    # arm B secondary, after the primary
    if cfg.secondary.type == "binary":
        e = _eligible_share(pool, L, q)
        if not 0 <= c <= e:
            raise ValueError(f"binary secondary_change {c} must lie in [0, {e:.4f}], the eligible share")
        hit2 = trig_b & ~hit & (sec_cf == 0) & other & (u_sec < (c / e if c else 0.0))
        tgt = _last(lead, ans & ~np.isin(disp, prim) & ~np.isin(disp, sec), m)[hit2]
        src = _draw(rng, C.answered & C.disposition.eq(sec[0]), len(tgt))
        disp[tgt], dur[tgt] = sec[0], C.duration_s.to_numpy()[src]
    else:
        val[is_b[lead] & ans] *= 1 + c
        hit2 = trig_b & (c != 0)
    _, primary, secondary, _ = _outcomes(lead, m, ans, disp, val, cfg)

    lead_id = pd.Series(ids[lead], dtype="str")
    variant = np.where(is_b, "B", "A")
    exposures = pd.DataFrame({
        "experiment_id": cfg.id, "lead_id": ids, "variant": variant,
        "bucket": np.array([a.bucket for a in routed], dtype=np.int64), "ts": t0,
    })[logged].reset_index(drop=True)
    calls = pd.DataFrame({
        "experiment_id": cfg.id, "call_id": lead_id + "-" + pd.Series(g.k.to_numpy()).astype("str"),
        "lead_id": lead_id, "ts": ts, "answered": ans, "disposition": pd.Series(disp, dtype="str"),
        "duration_s": dur, "attempt_no": g.attempt_no.reset_index(drop=True),
    })
    truth = pd.DataFrame({
        "lead_id": ids, "variant": variant, "exposure_logged": logged, "triggered": trig,
        "primary_cf": prim_cf, "primary": primary, "secondary_cf": sec_cf, "secondary": secondary,
        "injected_primary": hit, "injected_secondary": hit2,
    }).astype({"lead_id": "str", "variant": "str"})
    return SimDay(exposures=exposures, calls=calls, truth=truth)


def truth_summary(truth: pd.DataFrame, pool: Pool, cfg: ExperimentConfig, sim: SimConfig,
                  scenario: str, seed: int) -> SimTruth:
    t = truth[truth.triggered]
    a, b = t[t.variant == "A"], t[t.variant == "B"]
    rate_b, rate_cf = float(b.primary.mean()), float(b.primary_cf.mean())
    sec_b, sec_cf = float(b.secondary.mean()), float(b.secondary_cf.mean())
    relative = cfg.secondary.type == "continuous"
    return SimTruth(
        experiment_id=cfg.id, scenario=scenario, seed=seed,
        injected_primary_rel_lift=sim.true_effect.primary_rel_lift,
        injected_secondary_change=sim.true_effect.secondary_change,
        secondary_change_type="relative" if relative else "absolute",
        pool_leads=len(pool.leads), pool_p=pool.p,
        leads={v: int((truth.variant == v).sum()) for v in ("A", "B")},
        triggered={v: int((t.variant == v).sum()) for v in ("A", "B")},
        exposures_dropped=int((~truth.exposure_logged).sum()),
        rate_A=float(a.primary.mean()), rate_B_counterfactual=rate_cf, rate_B=rate_b,
        realised_primary_rel_lift=rate_b / rate_cf - 1,
        secondary_A=float(a.secondary.mean()), secondary_B_counterfactual=sec_cf, secondary_B=sec_b,
        realised_secondary_change=sec_b / sec_cf - 1 if relative else sec_b - sec_cf,
    )


def write_truth_report(truth: SimTruth, out_dir: Path = REPORTS_DIR) -> Path:
    t = truth
    rel = t.secondary_change_type == "relative"
    sc = (lambda x: f"{x:+.2%}") if rel else (lambda x: f"{x:+.4f}")
    sv = (lambda x: f"{x:.2f}") if rel else (lambda x: f"{x:.4f}")
    md = f"""# Simulation ground truth: {t.scenario}

The engine never sees this file or the sim config.

Experiment `{t.experiment_id}`, seed {t.seed}.

| effect | Injected (sim config) | Realised (this run) |
|---|---|---|
| primary relative lift | {t.injected_primary_rel_lift:+.2%} | {t.realised_primary_rel_lift:+.2%} |
| secondary change ({t.secondary_change_type}) | {sc(t.injected_secondary_change)} | {sc(t.realised_secondary_change)} |

| arm | leads | triggered leads | primary rate | secondary |
|---|---|---|---|---|
| A | {t.leads['A']:,} | {t.triggered['A']:,} | {t.rate_A:.4f} | {sv(t.secondary_A)} |
| B, counterfactual (before injection) | {t.leads['B']:,} | {t.triggered['B']:,} | {t.rate_B_counterfactual:.4f} | {sv(t.secondary_B_counterfactual)} |
| B, realised | {t.leads['B']:,} | {t.triggered['B']:,} | {t.rate_B:.4f} | {sv(t.secondary_B)} |

Exposures dropped (fault injection): {t.exposures_dropped:,} B leads have calls but no logged exposure.

Bootstrap pool: {t.pool_leads:,} lead-windows; pool p = {t.pool_p:.4f}.

## Definitions

- **Pool lead (lead-window):** one historical lead's calls inside one non-overlapping
  window-length block of the reference period, with their timing relative to that lead's first
  call in the block. Sim leads are drawn from the pool uniformly with replacement, renamed
  sim-<seed>-<n>, and routed through the real router; calls at or after the window end are not
  generated.
- **Triggered lead:** at least one answered call (§2.6). Rates and secondary values are over
  triggered leads only.
- **Primary per lead:** 1 if any answered call has a primary disposition, else 0.
- **Secondary per lead:** the mean over answered calls (continuous) or 1 if any answered call
  has a secondary disposition (binary).
- **Counterfactual:** the same B leads' replayed historical outcome before injection. Arm A is
  never changed.
- **Realised primary lift** = rate_B / rate_B_counterfactual − 1. **Realised secondary change**
  = secondary_B / secondary_B_counterfactual − 1 (relative) or secondary_B −
  secondary_B_counterfactual (absolute).
- **Pool p:** the primary rate per triggered pool lead over full lead-windows. The injection
  probability uses it, while the realised lift uses in-window calls, so the two can differ
  slightly.
"""
    path = Path(out_dir) / f"sim_truth_{t.scenario}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    return path
