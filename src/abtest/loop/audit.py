"""Human audit of the judge: export a stratified sheet, then measure agreement (PLAN_LOOP.md §L2.2 "Human audit", §L7.2).

The rubric is trusted as a guardrail only when a person has checked it. `export_audit` writes the
sheet the reviewer fills (it holds call ids, so it lives under data/derived/loop/, gitignored).
`agreement` reads it back: Spearman rho between the judge's rubric_score and the human's 1-5 score
with a Fisher 95% CI, and for every hard criterion the percent agreement, Cohen's kappa and the
prevalence. Hard failures are rare, so percent agreement alone flatters a judge that always says
"no"; kappa and prevalence are reported next to it and a warning fires when positives are scarce.
"""

import math
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm, spearmanr

from abtest.loop.contracts import Criterion, PassThresholds, Rubric
from abtest.loop.rubric import known_rubric
from abtest.loop.scorer import RUBRIC_ATTR, criterion_columns

MIN_AUDIT_ROWS = 20          # §L2.2: below this the rho CI is too wide to say anything
MIN_POSITIVES = 3            # fewer reviewer-confirmed positives than this and agreement is mostly chance
Z95 = float(norm.ppf(0.975))
_EPS = 1e-12
_YES = {"y", "yes", "true", "1"}
_NO = {"n", "no", "false", "0"}


# --- export ---------------------------------------------------------------------------------

def _allocate(sizes: Mapping[str, int], n: int) -> dict[str, int]:
    """Proportional allocation of n draws over strata, at least one per stratum, never above a
    stratum's size, largest-remainder rounding. If n is below the number of strata every stratum
    still gets one, so the total can exceed n."""
    pop = sum(sizes.values())
    total = min(n, pop)
    exact = {h: total * k / pop for h, k in sizes.items()}
    alloc = {h: min(sizes[h], max(1, math.floor(exact[h]))) for h in sizes}
    while sum(alloc.values()) < total:
        room = [h for h in sorted(sizes) if alloc[h] < sizes[h]]
        alloc[max(room, key=lambda h: exact[h] - alloc[h])] += 1
    while sum(alloc.values()) > total:
        over = [h for h in sorted(sizes) if alloc[h] > 1]
        if not over:
            break
        alloc[max(over, key=lambda h: alloc[h] - exact[h])] -= 1
    return alloc


def _audit_criteria(scores: pd.DataFrame) -> list[str]:
    """Hard criterion ids when the frame names its rubric and has their columns, else every criterion column."""
    every = criterion_columns(scores)
    rubric = known_rubric(str(scores.attrs.get(RUBRIC_ATTR, "")))
    if rubric is not None:
        hard = [c.id for c in rubric.criteria if c.hard]
        if hard and set(hard) <= set(every):
            return hard
    return every


def export_audit(scores: pd.DataFrame, calls: pd.DataFrame, n: int, seed: int, out: Path) -> Path:
    """Write the audit sheet: a stratified (by bot_version) random sample of n scored calls.

    Columns: call_id, bot_version, rubric_score, a `judge_<id>` per criterion, then the empty columns
    the reviewer fills: human_score (1-5), human_role_ok (y/n) and a `human_<id>` (y/n: is the
    criterion's question answered yes) per criterion. The criteria are the hard ones when `scores`
    came from scorer.scores_frame (it records which rubric produced it); otherwise, since this
    function is not given the rubric, every criterion column of `scores` (the extras are optional
    for the reviewer; `agreement` reads only the hard ones).

    The `judge_<id>` values are the frame's own unit, goodness (1 = good); `agreement` converts them
    to the judge's yes/no with the rubric's polarity. Strata are sampled in proportion to their size
    with at least one call each (so the sheet can exceed n when n is below the number of bot
    versions); rows are shuffled so the reviewer cannot read the arm off the order. Deterministic
    for a given seed and input.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    crit = _audit_criteria(scores)
    s = scores.drop_duplicates("call_id", keep="last")[["call_id", "rubric_score", *crit]]
    c = calls.drop_duplicates("call_id", keep="first")
    c = c[["call_id", "bot_version"]] if "bot_version" in c.columns else c[["call_id"]].assign(bot_version="unknown")
    merged = s.merge(c, on="call_id", how="inner")
    merged["bot_version"] = merged["bot_version"].fillna("unknown").astype(str)
    if merged.empty:
        raise ValueError("no scored call is present in the calls frame; nothing to audit")
    merged = merged.sort_values("call_id").reset_index(drop=True)

    sizes = merged["bot_version"].value_counts().to_dict()
    alloc = _allocate(sizes, n)
    rng = np.random.default_rng(seed)
    keep: list[int] = []
    for version in sorted(sizes):
        rows = np.flatnonzero((merged["bot_version"] == version).to_numpy())
        keep.extend(int(i) for i in sorted(rng.choice(rows, size=alloc[version], replace=False)))
    sheet = merged.iloc[sorted(keep)].reset_index(drop=True)
    sheet = sheet.iloc[rng.permutation(len(sheet))].reset_index(drop=True)

    sheet["rubric_score"] = sheet["rubric_score"].round(6)
    sheet = sheet.rename(columns={k: f"judge_{k}" for k in crit})
    sheet = sheet[["call_id", "bot_version", "rubric_score", *(f"judge_{k}" for k in crit)]]
    sheet["human_score"] = ""
    sheet["human_role_ok"] = ""
    for k in crit:
        sheet[f"human_{k}"] = ""

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.to_csv(out, index=False)
    return out


# --- agreement ------------------------------------------------------------------------------

def _yes_no(column: pd.Series, name: str) -> pd.Series:
    """y/n cells -> True/False, blank -> NaN. Anything else is a data-entry error, not a missing value."""
    def parse(cell):
        if pd.isna(cell):
            return np.nan
        word = str(cell).strip().lower()
        if word in _YES:
            return True
        if word in _NO:
            return False
        raise ValueError(f"column {name}: expected y or n, got {cell!r}")
    return column.map(parse).astype(object)


def _judge_yes(c: Criterion, g: float, th: PassThresholds) -> bool:
    """Would the judge answer the criterion's question 'yes'? yes_no: p (from goodness, via polarity)
    against the threshold that criterion fails at; a 1-5 criterion counts as 'yes' when satisfied."""
    if c.kind == "yes_no":
        p = round(1.0 - g if c.polarity == "bad" else g, 9)
        return p >= (th.bad if c.polarity == "bad" else th.good) - _EPS
    return g >= th.good - _EPS


def _kappa(a: int, b: int, c: int, d: int) -> float | None:
    """Cohen's kappa of a 2x2 table (a: both yes, b: human yes only, c: judge yes only, d: both no),
    in exact integer arithmetic so an always-'no' judge gives 0.0, not 1e-17. None when both
    raters are constant (chance agreement is 1: kappa is undefined)."""
    denom = (a + b) * (b + d) + (a + c) * (c + d)
    return None if denom == 0 else 2.0 * (a * d - b * c) / denom


def _spearman(judge: pd.Series, human: pd.Series) -> tuple[float | None, tuple[float, float] | None]:
    """(rho, Fisher 95% CI). rho needs 3+ points and some spread on both sides; the CI needs n > 3."""
    n = len(judge)
    if n < 3 or judge.nunique() < 2 or human.nunique() < 2:
        return None, None
    rho = float(spearmanr(judge, human).statistic)
    if n <= 3:
        return rho, None
    if abs(rho) >= 1.0:  # z is infinite: the interval collapses onto the point
        return rho, (rho, rho)
    z, se = math.atanh(rho), 1.0 / math.sqrt(n - 3)
    return rho, (math.tanh(z - Z95 * se), math.tanh(z + Z95 * se))


def agreement(sheet: Path, rubric: Rubric, min_rho: float, min_hard_agreement: float) -> dict:
    """Read the filled sheet and decide whether the judge can be trusted.

    Keys (undefined numbers are None, so the dict is JSON-safe):
      rows        rows in the sheet;  n  rows with a human_score
      rho, rho_ci95   Spearman between rubric_score and human_score, Fisher z interval (lo, hi)
      criteria    per hard criterion: n (rows with both labels), agreement (share of rows where the
                  human's y/n equals the judge's), kappa, prevalence (share of human 'y'),
                  judge_rate (share of judge 'yes'); a 'yes' is the criterion's question answered yes
      role_accuracy, role_n   share of 'y' in human_role_ok, over the rows filled
      trusted     n >= 20 and rho >= min_rho and every hard criterion's agreement >= min_hard_agreement
      reason      one sentence saying why
      warnings    hard criteria where reviewer-confirmed positives are too scarce for percent
                  agreement to mean much (a judge that always says 'no' would score high)
    """
    df = pd.read_csv(sheet, dtype=str, keep_default_na=False, na_values=[""])
    hard = [c for c in rubric.criteria if c.hard]
    need = ["rubric_score", "human_score", *(f"{p}_{c.id}" for c in hard for p in ("judge", "human"))]
    if missing := [k for k in need if k not in df.columns]:
        raise ValueError(f"audit sheet lacks columns {missing}; was it exported for rubric {rubric.id}?")

    human = pd.to_numeric(df["human_score"], errors="coerce")
    if (bad := df["human_score"].notna() & human.isna()).any():
        raise ValueError(f"human_score must be a number from 1 to 5; {int(bad.sum())} row(s) are not")
    if ((human < 1) | (human > 5)).any():
        raise ValueError("human_score must be between 1 and 5")
    judge = pd.to_numeric(df["rubric_score"], errors="coerce")
    filled = human.notna() & judge.notna()
    n = int(filled.sum())
    rho, ci = _spearman(judge[filled], human[filled]) if n else (None, None)

    criteria: dict[str, dict] = {}
    warnings: list[str] = []
    for c in hard:
        h = _yes_no(df[f"human_{c.id}"], f"human_{c.id}")
        g = pd.to_numeric(df[f"judge_{c.id}"], errors="coerce")
        both = h.notna() & g.notna()
        k = int(both.sum())
        if k == 0:
            criteria[c.id] = {"n": 0, "agreement": None, "kappa": None, "prevalence": None, "judge_rate": None}
            continue
        hy = h[both].astype(bool).to_numpy()
        jy = np.array([_judge_yes(c, float(v), rubric.pass_thresholds) for v in g[both]])
        a, b = int((hy & jy).sum()), int((hy & ~jy).sum())
        cc, d = int((~hy & jy).sum()), int((~hy & ~jy).sum())
        criteria[c.id] = {"n": k, "agreement": (a + d) / k, "kappa": _kappa(a, b, cc, d),
                          "prevalence": int(hy.sum()) / k, "judge_rate": int(jy.sum()) / k}
        if hy.sum() < MIN_POSITIVES:
            warnings.append(f"{c.id}: the reviewer marked only {int(hy.sum())} of {k} calls 'y', so percent "
                            f"agreement mostly reflects the 'n' answers; read its kappa and label more calls")

    role = _yes_no(df["human_role_ok"], "human_role_ok").dropna() if "human_role_ok" in df.columns else pd.Series(dtype=object)
    role_accuracy = float(role.astype(bool).mean()) if len(role) else None

    problems = []
    if n == 0:
        problems.append("no row has a human_score yet")
    else:
        if n < MIN_AUDIT_ROWS:
            problems.append(f"only {n} rows have a human_score (need {MIN_AUDIT_ROWS})")
        if rho is None:
            problems.append("rho is undefined (too few rows, or the judge or the reviewer gave one value throughout)")
        elif rho < min_rho - _EPS:
            problems.append(f"rho {rho:.2f} is below {min_rho:g}")
    for c in hard:
        agree = criteria[c.id]["agreement"]
        if agree is None:
            problems.append(f"{c.id} has no human labels")
        elif agree < min_hard_agreement - _EPS:
            problems.append(f"{c.id} agreement {agree:.2f} is below {min_hard_agreement:g}")

    trusted = not problems
    if trusted:
        lo, hi = ci if ci else (float("nan"), float("nan"))
        reason = (f"Trusted: rho {rho:.2f} (95% CI {lo:.2f} to {hi:.2f}) over {n} calls, and every hard "
                  f"criterion agrees at {min_hard_agreement:g} or better.")
    else:
        reason = "Not trusted: " + "; ".join(problems) + "."
    return {"rows": len(df), "n": n, "rho": rho, "rho_ci95": ci, "criteria": criteria,
            "role_accuracy": role_accuracy, "role_n": int(len(role)), "trusted": trusted,
            "reason": reason, "warnings": warnings}
