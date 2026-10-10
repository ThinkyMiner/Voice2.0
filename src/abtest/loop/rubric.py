"""Rubric loading, hashing and the scoring formulas (PLAN_LOOP.md §L2.2, §L7.2).

A rubric is a YAML file in configs_loop/rubrics/. Every weight is positive and `polarity` says which
way is good, so the whole scoring step reduces to three numbers per call: `rubric_score` (higher
is better), `rubric_risk` (probability that a risk criterion fired) and the criteria that failed.
Metrics and guardrails use the first two; the failed ids are labels for diagnosis and the dogfood
gate only. Pure functions, no model call.
"""

import importlib
import math
from collections.abc import Callable, Mapping
from pathlib import Path

import yaml

from abtest.loop.contracts import Criterion, JudgeAnswer, Rubric, sha256_json

# Content-addressed memo: rubric hash -> the (frozen) rubric it was computed from. A RubricScore
# carries only the hash, but turning its answers into goodness needs each criterion's polarity,
# so scorer.scores_frame resolves the hash here. Same hash means same content, so this is a cache,
# not hidden state.
_KNOWN: dict[str, Rubric] = {}


def load_rubric(path: str | Path) -> Rubric:
    """Parse and validate a rubric YAML (duplicate ids, unknown or non-yes_no risk ids raise)."""
    rubric = Rubric.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    rubric_hash(rubric)  # remember it for scores_frame
    return rubric


def rubric_hash(rubric: Rubric) -> str:
    """sha256 of the canonical JSON of the parsed rubric: a changed rubric never mixes with old scores."""
    h = sha256_json(rubric.model_dump(mode="json"))
    _KNOWN.setdefault(h, rubric)
    return h


def known_rubric(h: str) -> Rubric | None:
    """The rubric a hash was computed from, if this process has loaded or hashed it."""
    return _KNOWN.get(h)


def _clip01(x: float) -> float:
    return min(1.0, max(0.0, x))


def _finite(value: float, criterion_id: str) -> float:
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"judge answer for {criterion_id!r} is not a finite number: {value!r}")
    return v


def goodness(criterion: Criterion, answer: JudgeAnswer) -> float:
    """g in [0, 1], higher is better. yes_no: bad -> 1 - p, good -> p. score_1_5: x = (s - 1) / 4
    clipped to [0, 1], bad -> 1 - x. Out-of-range answers are clipped, never rejected."""
    if answer.criterion_id != criterion.id:
        raise ValueError(f"answer for {answer.criterion_id!r} given to criterion {criterion.id!r}")
    value = _finite(answer.value, criterion.id)
    x = _clip01(value) if criterion.kind == "yes_no" else _clip01((value - 1.0) / 4.0)
    return 1.0 - x if criterion.polarity == "bad" else x


def _fails(criterion: Criterion, answer: JudgeAnswer, g: float, rubric: Rubric) -> bool:
    """A bad yes_no fails at p >= bad; everything else fails at goodness < good (§L2.2)."""
    th = rubric.pass_thresholds
    if criterion.kind == "yes_no" and criterion.polarity == "bad":
        return _clip01(float(answer.value)) >= th.bad
    return g < th.good


def _resolve_aggregate(path: str) -> Callable[[dict[str, JudgeAnswer], Rubric], float]:
    """`pkg.mod:fn` or `pkg.mod.fn` -> the function, with a clear error when it cannot be found."""
    module_name, sep, attr = path.partition(":")
    if not sep:
        module_name, _, attr = path.rpartition(".")
    if not module_name or not attr:
        raise ValueError(f"aggregate {path!r} is neither 'weighted_mean' nor a dotted path 'pkg.mod:fn'")
    try:
        fn = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as e:
        raise ValueError(f"cannot load aggregate {path!r}: {e}") from e
    if not callable(fn):
        raise ValueError(f"aggregate {path!r} is not callable")
    return fn


def score_from_answers(rubric: Rubric, answers: Mapping[str, JudgeAnswer]) -> tuple[float, float, tuple[str, ...]]:
    """(rubric_score, rubric_risk, failed criterion ids). Extra answers are ignored; a missing one
    raises ValueError naming it.

    rubric_score = sum(w * g) / sum(w); the `aggregate` hook replaces only this number.
    rubric_risk  = 1 - prod(1 - p_i) over rubric.risk, p_i the risk criterion's yes probability;
                   independence is an approximation, as §L2.2 documents.
    """
    if missing := [c.id for c in rubric.criteria if c.id not in answers]:
        raise ValueError(f"rubric {rubric.id}: no judge answer for {missing}")
    g = {c.id: goodness(c, answers[c.id]) for c in rubric.criteria}

    if rubric.aggregate == "weighted_mean":
        score = sum(c.weight * g[c.id] for c in rubric.criteria) / sum(c.weight for c in rubric.criteria)
    else:
        score = float(_resolve_aggregate(rubric.aggregate)(dict(answers), rubric))
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError(f"aggregate {rubric.aggregate!r} returned {score!r}; it must be in [0, 1]")

    survive = 1.0
    for cid in rubric.risk:
        survive *= 1.0 - _clip01(_finite(answers[cid].value, cid))
    risk = 1.0 - survive

    failed = tuple(c.id for c in rubric.criteria if _fails(c, answers[c.id], g[c.id], rubric))
    return _clip01(score), _clip01(risk), failed
