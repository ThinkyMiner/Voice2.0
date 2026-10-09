"""Alpha-spending boundaries and exact crossing probabilities (PLAN.md §2.3, §2.7).

Model: canonical joint distribution of the look-k z-statistics (Jennison & Turnbull 2000, ch. 3).
Under drift theta, Z_k ~ N(theta * sqrt(t_k), 1) and Cov(Z_j, Z_k) = sqrt(t_j / t_k) for j <= k.
Internally we work with the B-value W_k = sqrt(t_k) * Z_k, which is Brownian motion with drift
theta observed at times t_k: independent increments N(theta * dt, dt).

Crossing probabilities are computed exactly (to quadrature error) by the Armitage-McPherson-Rowe
recursion on a grid (J&T 2000, ch. 19): propagate the density of W_k restricted to "no boundary
crossed so far" from look to look with Simpson weights, and read the first-crossing probability
at each look off the normal tail. Monte Carlo is only a cross-check (it cannot reach the early
win boundary, z ~ 4.9).

Both boundaries are non-binding (§2.7): each is solved ignoring the other, under theta = 0, as an
UPPER boundary. Harm is the mirror image under theta = 0, so it is returned as positive values
(crossing means z <= -harm[k-1]). Boundary b_k solves
    P(no crossing at looks 1..k-1, Z_k >= b_k | theta = 0) = alpha(t_k) - alpha(t_{k-1}).
Spending uses min(t, 1), so a look at t > 1 (observed information overshooting n_max) is valid;
when the spending increment is not positive, b_k = +inf: that look cannot cross.

CLI: python -m abtest.boundaries [exp.yaml]   (make boundaries EXP=...)
"""

import math
import sys
from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import NamedTuple

import numpy as np
from scipy.optimize import brentq
from scipy.special import ndtr
from scipy.stats import norm

from abtest.contracts import CONFIGS_DIR, Boundaries, ExperimentConfig

# --- quadrature -----------------------------------------------------------------------------
GRID_R = 18          # grid points per sd of the smaller adjacent look increment (J&T 2000, §19.3)
TAIL_SD = 8.0        # continuation region starts 8 sd below the mean of W_k: Phi(-8) = 6e-16
BOUND_CAP = 40.0     # a boundary beyond this z is unreachable (Phi-bar(40) underflows to 0)
KERNEL_BLOCK = 1 << 21   # kernel-matrix entries evaluated at once (16 MB), bounds memory
CACHE_SIZE = 4096    # distinct (schedule, alpha, spending function) results kept; each is tiny
MC_CHUNK = 250_000   # Monte Carlo paths simulated at once

# --- CLI checks -----------------------------------------------------------------------------
DEFAULT_EXP = CONFIGS_DIR / "experiments" / "vani-meeting-v1.yaml"
CLI_LOOKS = (5, 7)   # PLAN.md §3 Checkpoint 3: equally spaced looks t = k/K
REFERENCE_ALPHA = 0.025   # the reference values below are for one-sided alpha 0.025
# gsDesign sfLDOF, 5 equally spaced looks, one-sided alpha 0.025 (PLAN.md §2.7).
REFERENCE_WIN_5 = (4.877, 3.357, 2.680, 2.290, 2.031)
REFERENCE_TOL = 0.01      # PLAN.md §2.7 test tolerance
MC_PATHS = 1_000_000      # PLAN.md §2.7: at least 10^6 Brownian paths
MC_SEED = 20261009        # fixed: validation is deterministic (PLAN.md §0)
MC_SE_MULT = 4.0          # accept |MC - exact| <= 4 binomial standard errors (+ 1 path)


# --- spending functions ---------------------------------------------------------------------

def _clipped_t(t: float, alpha: float) -> float:
    if not (t >= 0 and math.isfinite(t)):
        raise ValueError(f"information fraction must be finite and >= 0, got {t}")
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    return min(t, 1.0)  # observed information can overshoot n_max; spending never exceeds alpha


def spend_win(t: float, alpha: float) -> float:
    """Lan-DeMets O'Brien-Fleming-type: 2 * (1 - Phi(z_{1-alpha/2} / sqrt(t))). One-sided alpha."""
    t = _clipped_t(t, alpha)
    if t == 0:
        return 0.0
    return float(2 * norm.sf(norm.isf(alpha / 2) / math.sqrt(t)))


def spend_harm(t: float, alpha: float) -> float:
    """Pocock-type: alpha * ln(1 + (e - 1) * t). One-sided alpha."""
    t = _clipped_t(t, alpha)
    return alpha * math.log1p((math.e - 1) * t)


# --- recursive numerical integration --------------------------------------------------------

class _Stage(NamedTuple):
    """Density of W_k on the continuation region (no boundary crossed at looks 1..k).

    `mass` is Simpson weight * density at each grid point `w`, so for smooth g,
    sum(mass * g(w)) approximates the integral of density * g and sum(mass) is P(no crossing).
    """
    w: np.ndarray
    mass: np.ndarray


_START = _Stage(np.zeros(1), np.ones(1))  # W_0 = 0 with probability 1


def _check_fractions(info_fractions: Sequence[float]) -> tuple[float, ...]:
    t = tuple(float(x) for x in info_fractions)
    if not t:
        raise ValueError("info_fractions must not be empty")
    if not all(math.isfinite(x) and x > 0 for x in t):
        raise ValueError(f"info_fractions must be finite and > 0, got {t}")
    if any(b <= a for a, b in zip(t, t[1:])):
        raise ValueError(f"info_fractions must be strictly increasing, got {t}")
    return t


def _simpson_grid(lo: float, hi: float, h: float) -> tuple[np.ndarray, np.ndarray]:
    n = max(2, 2 * math.ceil((hi - lo) / (2 * h)))  # Simpson needs an even number of intervals
    coef = np.full(n + 1, 2.0)
    coef[1::2] = 4.0
    coef[0] = coef[-1] = 1.0
    return np.linspace(lo, hi, n + 1), coef * ((hi - lo) / n / 3)


def _cross(stage: _Stage, t_prev: float, t: float, bound: float, drift: float) -> float:
    """P(no crossing before t_prev and W_t >= bound * sqrt(t))."""
    dt = t - t_prev
    x = (bound * math.sqrt(t) - stage.w - drift * dt) / math.sqrt(dt)
    return float(stage.mass @ ndtr(-x))


def _advance(stage: _Stage, t_prev: float, t: float, bound: float, drift: float,
             dt_next: float | None) -> _Stage | None:
    """Density of W_t restricted to W_t < bound * sqrt(t), on a fresh Simpson grid.

    Spacing is sd / GRID_R for the smaller of this and the next increment, so both the kernel
    that built this density and the kernel that will consume it are resolved. None when the
    continuation region is empty (all mass has crossed, to within the truncation error).
    """
    dt = t - t_prev
    sd = math.sqrt(dt)
    centre, half = drift * t, TAIL_SD * math.sqrt(t)
    lo, hi = centre - half, min(bound * math.sqrt(t), centre + half)
    if hi <= lo:
        return None
    h = min(sd, math.sqrt(dt_next) if dt_next is not None else sd) / GRID_R
    w, simpson = _simpson_grid(lo, hi, h)
    # The kernel is a Gaussian of sd `sd`: drop previous grid points further than TAIL_SD sd from
    # a row (mass < 1e-15). For close looks the band is narrow, so the cost stays O(n * band)
    # instead of O(n^2); for ordinary spacing it spans most of the grid.
    shift, reach = drift * dt, TAIL_SD * sd
    first = np.searchsorted(stage.w, w - shift - reach, side="left")
    last = np.searchsorted(stage.w, w - shift + reach, side="right")
    dens = np.empty_like(w)
    s = 0
    while s < len(w):
        e = len(w)
        while e - s > 1 and (e - s) * (last[e - 1] - first[s]) > KERNEL_BLOCK:
            e = s + (e - s) // 2  # bound the kernel block's memory
        cols = slice(first[s], last[e - 1])
        z = (w[s:e, None] - stage.w[None, cols] - shift) / sd
        dens[s:e] = np.exp(-0.5 * z * z) @ stage.mass[cols]
        s = e
    dens /= sd * math.sqrt(2 * math.pi)
    return _Stage(w, simpson * dens)


def _first_crossing_probs(bounds: Sequence[float], t: tuple[float, ...], drift: float) -> list[float]:
    """P(first crossing of bounds[k] happens at look k+1), for every look."""
    probs: list[float] = []
    stage: _Stage | None = _START
    t_prev = 0.0
    for k, tk in enumerate(t):
        if stage is None:  # everything crossed earlier
            probs.append(0.0)
            continue
        probs.append(_cross(stage, t_prev, tk, bounds[k], drift))
        if k + 1 < len(t):
            stage = _advance(stage, t_prev, tk, bounds[k], drift, t[k + 1] - tk)
        t_prev = tk
    return probs


def crossing_probability(bounds: Sequence[float], info_fractions: Sequence[float],
                         drift: float) -> float:
    """P(exists k: Z_k >= bounds[k-1]) under drift theta (+inf bounds never cross)."""
    t = _check_fractions(info_fractions)
    if len(bounds) != len(t):
        raise ValueError(f"need one bound per look: {len(bounds)} bounds, {len(t)} looks")
    if any(math.isnan(b) for b in bounds):
        raise ValueError("bounds must not be NaN")
    return math.fsum(_first_crossing_probs(bounds, t, float(drift)))


def _spending_bounds(t: tuple[float, ...], alpha: float,
                     spend: Callable[[float, float], float]) -> tuple[float, ...]:
    """Upper boundaries (z scale) whose cumulative crossing probability under theta = 0 is
    spend(t_k, alpha) at every look; +inf where the spending increment is not positive."""
    bounds: list[float] = []
    stage = _START
    t_prev = spent_prev = 0.0
    for k, tk in enumerate(t):
        spent = spend(tk, alpha)
        inc = spent - spent_prev
        if inc <= 0:
            b = math.inf
        elif k == 0:
            b = float(norm.isf(inc))  # one look: closed form
        else:
            def gap(b: float, stage=stage, t_prev=t_prev, tk=tk, inc=inc) -> float:
                return _cross(stage, t_prev, tk, b, 0.0) - inc
            b = math.inf if gap(BOUND_CAP) >= 0 else float(brentq(gap, -BOUND_CAP, BOUND_CAP))
        bounds.append(b)
        if k + 1 < len(t):
            nxt = _advance(stage, t_prev, tk, b, 0.0, t[k + 1] - tk)
            assert nxt is not None  # at theta = 0 the region below any boundary is non-empty
            stage = nxt
        t_prev, spent_prev = tk, spent
    return tuple(bounds)


@lru_cache(maxsize=CACHE_SIZE)
def _cached_bounds(t: tuple[float, ...], alpha: float,
                   spend: Callable[[float, float], float]) -> tuple[float, ...]:
    """Memoised: ExperimentConfig is unhashable, but the boundaries depend only on these three.
    The validation harness asks for the same schedule thousands of times; the runner re-asks for
    every prefix of its schedule (boundaries have the prefix property)."""
    return _spending_bounds(t, alpha, spend)


def win_boundaries(info_fractions: Sequence[float], alpha_win: float) -> tuple[float, ...]:
    """Win boundaries only (non-binding), for callers that need no harm boundary (power.py)."""
    return _cached_bounds(_check_fractions(info_fractions), float(alpha_win), spend_win)


def harm_boundaries(info_fractions: Sequence[float], alpha_harm: float) -> tuple[float, ...]:
    """Harm boundaries (positive values; crossing means z <= -harm), non-binding."""
    return _cached_bounds(_check_fractions(info_fractions), float(alpha_harm), spend_harm)


def compute_boundaries(info_fractions: Sequence[float], cfg: ExperimentConfig) -> Boundaries:
    t = _check_fractions(info_fractions)
    return Boundaries(info_fractions=t, win=win_boundaries(t, cfg.alpha_win),
                      harm=harm_boundaries(t, cfg.alpha_harm))


def planned_info_fractions(cfg: ExperimentConfig) -> tuple[float, ...]:
    """The planned look schedule: window.looks equally spaced fractions from t1 to 1 inclusive,
    t1 = max(min_info_first_look, 1/K) (§2.7: no look before min_info_first_look)."""
    k = cfg.window.looks
    t1 = max(cfg.min_info_first_look, 1 / k)
    return tuple(float(x) for x in np.linspace(t1, 1.0, k))


# --- Monte Carlo cross-check ----------------------------------------------------------------

def _mc_cumulative(bounds: Sequence[float], t: tuple[float, ...], drift: float, n_paths: int,
                   seed: int) -> np.ndarray:
    """Fraction of simulated paths that have crossed by look k, for every k."""
    if n_paths < 1:
        raise ValueError(f"n_paths must be >= 1, got {n_paths}")
    rng = np.random.default_rng(seed)
    dt = np.diff(t, prepend=0.0)
    c = np.asarray(bounds, dtype=float) * np.sqrt(t)  # boundaries on the B-value scale
    counts = np.zeros(len(t), dtype=np.int64)
    left = n_paths
    while left > 0:
        m = min(MC_CHUNK, left)
        w = np.cumsum(rng.normal(drift * dt, np.sqrt(dt), size=(m, len(t))), axis=1)
        counts += np.logical_or.accumulate(w >= c, axis=1).sum(axis=0)
        left -= m
    return counts / n_paths


def monte_carlo_crossing(bounds: Sequence[float], info_fractions: Sequence[float], drift: float,
                         n_paths: int, seed: int) -> float:
    t = _check_fractions(info_fractions)
    if len(bounds) != len(t):
        raise ValueError(f"need one bound per look: {len(bounds)} bounds, {len(t)} looks")
    return float(_mc_cumulative(bounds, t, float(drift), n_paths, seed)[-1])


# --- CLI ------------------------------------------------------------------------------------

def _mc_check(label: str, bounds: Sequence[float], t: tuple[float, ...], target: Sequence[float]) -> bool:
    """MC cumulative crossing at every look vs the spending function, under theta = 0."""
    mc = _mc_cumulative(bounds, t, 0.0, MC_PATHS, MC_SEED)
    exact = [math.fsum(_first_crossing_probs(bounds, t, 0.0)[:k + 1]) for k in range(len(t))]
    ok = True
    print(f"    {label}: look  alpha(t_k)   exact      MC         |MC - alpha|  tol")
    for k, (a, e, m) in enumerate(zip(target, exact, mc), 1):
        # binomial SE, plus one path so that a single MC hit on a ~1e-7 probability is not a FAIL
        tol = MC_SE_MULT * math.sqrt(a * (1 - a) / MC_PATHS) + 1 / MC_PATHS
        good = abs(m - a) <= tol and abs(e - a) <= 1e-6
        ok &= good
        print(f"    {'':{len(label)}}  {k:>4}  {a:.3e}  {e:.3e}  {m:.3e}  {abs(m - a):.2e}      "
              f"{tol:.1e}  {'ok' if good else 'FAIL'}")
    return ok


def main(argv: list[str] | None = None) -> int:
    from abtest import config
    from abtest.power import inflation_factor

    args = argv if argv is not None else sys.argv[1:]
    if len(args) > 1:
        raise SystemExit("usage: python -m abtest.boundaries [exp.yaml]")
    cfg = config.load_experiment(args[0] if args else DEFAULT_EXP)
    print(f"Boundaries: {cfg.id}  alpha_win {cfg.alpha_win} (O'Brien-Fleming-type), "
          f"alpha_harm {cfg.alpha_harm} (Pocock-type), both one-sided and non-binding")

    failed = False
    tables: dict[int, Boundaries] = {}
    for k in CLI_LOOKS:
        t = tuple(i / k for i in range(1, k + 1))
        tables[k] = b = compute_boundaries(t, cfg)
        print(f"\n{k} equally spaced looks (t = k/{k}); win: promote if z >= win; "
              f"harm: stop if z <= -harm")
        print("  look  t       win z    harm z")
        for i, (tk, w, h) in enumerate(zip(t, b.win, b.harm), 1):
            print(f"  {i:>4}  {tk:.4f}  {w:>7.3f}  {h:>7.3f}")

    print(f"\nReference check (gsDesign sfLDOF, 5 looks, alpha_win {REFERENCE_ALPHA}, "
          f"tolerance +/-{REFERENCE_TOL})")
    if math.isclose(cfg.alpha_win, REFERENCE_ALPHA):
        diffs = [abs(a - r) for a, r in zip(tables[5].win, REFERENCE_WIN_5)]
        ok = max(diffs) <= REFERENCE_TOL
        failed |= not ok
        print("  look  expected  computed  |diff|")
        for i, (r, a, d) in enumerate(zip(REFERENCE_WIN_5, tables[5].win, diffs), 1):
            print(f"  {i:>4}  {r:>8.3f}  {a:>8.3f}  {d:.4f}")
        print(f"  {'PASS' if ok else 'FAIL'} (max |diff| {max(diffs):.4f})")
    else:
        print(f"  skipped: alpha_win is {cfg.alpha_win}, the reference values are for "
              f"{REFERENCE_ALPHA}")

    print(f"\nMonte Carlo cross-check ({MC_PATHS:,} paths, seed {MC_SEED}, theta = 0): "
          f"cumulative crossing probability at each look vs the spending function,\n"
          f"  tolerance {MC_SE_MULT:.0f} binomial SE + 1 path. Cumulative, because MC cannot pin the "
          f"earliest win boundary itself (z ~ 4.9 is too far in the tail).")
    mc_ok = True
    for k in CLI_LOOKS:
        t, b = tuple(i / k for i in range(1, k + 1)), tables[k]
        print(f"  {k} looks")
        mc_ok &= _mc_check("win ", b.win, t, [spend_win(x, cfg.alpha_win) for x in t])
        mc_ok &= _mc_check("harm", b.harm, t, [spend_harm(x, cfg.alpha_harm) for x in t])
    failed |= not mc_ok
    print(f"  {'PASS' if mc_ok else 'FAIL'}")

    planned = planned_info_fractions(cfg)
    r = inflation_factor(cfg)
    b = compute_boundaries(planned, cfg)
    print(f"\nPlanned schedule for {cfg.id}: {cfg.window.looks} looks at t = "
          + ", ".join(f"{x:.3f}" for x in planned))
    print("  win z   " + "  ".join(f"{x:.3f}" for x in b.win))
    print("  harm z  " + "  ".join(f"{x:.3f}" for x in b.harm))
    print(f"  inflation factor R = {r:.4f}  (n_B,max = R * fixed-design n_B; power {cfg.power}, "
          f"win boundaries only)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
