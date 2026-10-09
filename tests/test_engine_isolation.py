"""Engine isolation (PLAN.md §2.3, required test): nothing on the engine path can see sim truth.

The engine is metrics -> boundaries -> power -> analysis -> rules (-> decision). It accepts only
events, aggregates and the experiment config. Three layers of evidence:

(a) static   : the source never imports the simulator or names a sim type/path;
(b) signature: no parameter or return annotation is a sim type;
(c) runtime  : in a subprocess whose audit hook raises on any open or listing under configs/sims/,
               the engine modules import and a full lead_outcomes -> compute_boundaries ->
               analyze_look -> evaluate_look pass completes; a deliberate open is the control.
"""

import ast
import importlib
import inspect
import pathlib
import re
import subprocess
import sys
import typing
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
import yaml

from abtest.contracts import CONFIGS_DIR, ROOT, SIMS_DIR, ExperimentConfig

ENGINE_MODULES = ("metrics", "analysis", "rules", "boundaries", "power", "decision")
OPTIONAL_MODULES = {"decision"}  # built in Phase 4; skipped until it exists
FORBIDDEN_NAMES = ("SIMS_DIR", "configs/sims", "SimConfig", "load_sim", "Scenario", "SimTruth")
SIM_TYPES = frozenset({"SimConfig", "Scenario", "SimTruth", "Pool", "SimDay"})
ENGINE_FUNCTIONS = (("metrics", "lead_outcomes"), ("analysis", "analyze_look"),
                    ("rules", "evaluate_look"), ("boundaries", "compute_boundaries"),
                    ("power", "n_max"), ("power", "detectable_lift"), ("power", "preflight"))


def _engine_module(name: str):
    try:
        return importlib.import_module(f"abtest.{name}")
    except ModuleNotFoundError as e:
        if name in OPTIONAL_MODULES and e.name == f"abtest.{name}":
            pytest.skip(f"{name}.py not built yet")
        raise


# --- (a) static ------------------------------------------------------------------------------

def _imports_simulator(tree: ast.AST) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name.split(".")[:2] == ["abtest", "simulator"]]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[:2] == ["abtest", "simulator"] or (node.level and mod == "simulator"):
                hits.append(mod)
            elif mod == "abtest" or (node.level and not mod):
                hits += [a.name for a in node.names if a.name == "simulator"]
    return hits


@pytest.mark.parametrize("name", ENGINE_MODULES)
def test_static_no_simulator_import_and_no_sim_names(name):
    path = pathlib.Path(_engine_module(name).__file__)
    source = path.read_text(encoding="utf-8")
    assert not _imports_simulator(ast.parse(source)), f"{path.name} imports the simulator"
    found = [n for n in FORBIDDEN_NAMES if n in source]
    assert not found, f"{path.name} mentions {found}"


def test_validation_fast_path_needs_no_simulator_or_router():
    # Not on the engine path (it reads data/), but TEAM.md §5 promises it runs without Phase 2.
    source = (ROOT / "src" / "abtest" / "validation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not _imports_simulator(tree)
    assert not any(isinstance(n, (ast.Import, ast.ImportFrom)) and "router" in ast.unparse(n)
                   for n in ast.walk(tree))
    assert not [n for n in FORBIDDEN_NAMES if n in source]


def test_static_scan_would_catch_a_violation():
    # Guards the guard: the checker above must actually fire on the patterns it claims to catch.
    for snippet in ("import abtest.simulator", "from abtest.simulator import build_pool",
                    "from abtest import simulator", "from . import simulator",
                    "from .simulator import simulate_day"):
        assert _imports_simulator(ast.parse(snippet)), snippet
    assert not _imports_simulator(ast.parse("from abtest import rules"))


def test_importing_the_engine_never_pulls_in_the_simulator():
    mods = [m for m in ENGINE_MODULES if m not in OPTIONAL_MODULES or
            (ROOT / "src" / "abtest" / f"{m}.py").exists()]
    code = ("import sys, importlib\n"
            f"for m in {mods!r}: importlib.import_module('abtest.' + m)\n"
            "bad = [m for m in sys.modules if m.startswith('abtest.simulator')]\n"
            "sys.exit('simulator imported: %s' % bad if bad else 0)")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr or res.stdout


# --- (b) signatures --------------------------------------------------------------------------

def _names_in(annotation) -> set[str]:
    """Every class or type name mentioned in an annotation, unwrapping unions and generics."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return set()
    if isinstance(annotation, str):
        return set(re.findall(r"[A-Za-z_]\w*", annotation))
    names = {getattr(annotation, "__name__", "")}
    for arg in typing.get_args(annotation):
        names |= _names_in(arg)
    return names


@pytest.mark.parametrize("module,func", ENGINE_FUNCTIONS, ids=lambda x: x)
def test_signature_has_no_sim_types(module, func):
    fn = getattr(_engine_module(module), func)
    sig = inspect.signature(fn)
    for p in sig.parameters.values():
        assert not _names_in(p.annotation) & SIM_TYPES, f"{module}.{func}({p.name}) takes a sim type"
    assert not _names_in(sig.return_annotation) & SIM_TYPES, f"{module}.{func} returns a sim type"
    try:  # resolved hints too, in case a module uses `from __future__ import annotations`
        hints = typing.get_type_hints(fn)
    except Exception:  # noqa: BLE001 - unresolved forward refs are covered by the string scan
        hints = {}
    for name, hint in hints.items():
        assert not _names_in(hint) & SIM_TYPES, f"{module}.{func}: {name} is a sim type"


# --- (c) runtime -----------------------------------------------------------------------------

def _synthetic_world(cfg: ExperimentConfig, n_leads: int = 900, seed: int = 7):
    """Exposures and calls for a tiny experiment: ~half of leads answer, ~20% of those succeed."""
    rng = np.random.default_rng(seed)
    t0 = datetime(2026, 7, 1)
    leads = [f"L{i}" for i in range(n_leads)]
    variant = np.where(rng.random(n_leads) < cfg.allocation.B, "B", "A")
    assigned = [t0 + timedelta(hours=int(h)) for h in rng.integers(0, 24 * 5, n_leads)]
    exposures = pd.DataFrame({"experiment_id": cfg.id, "lead_id": leads, "variant": variant,
                              "bucket": 0, "ts": assigned})
    rows = []
    for i, lead in enumerate(leads):
        for j in range(int(rng.integers(1, 4))):
            answered = bool(rng.random() < 0.6)
            success = answered and rng.random() < 0.2
            rows.append({"call_id": f"{lead}-{j}", "lead_id": lead,
                         "ts": assigned[i] + timedelta(hours=3 * j + 1), "answered": answered,
                         "disposition": cfg.primary.dispositions[0] if success else "General",
                         "duration_s": float(rng.lognormal(4.0, 0.8)) if answered else np.nan,
                         "attempt_no": j + 1, "bot_version": "main_vani"})
    return pd.DataFrame(rows), exposures, t0


def _engine_pass() -> None:
    """lead_outcomes -> compute_boundaries -> analyze_look -> evaluate_look at every look."""
    from abtest import analysis, boundaries, metrics, rules

    base = ExperimentConfig.model_validate(
        yaml.safe_load((CONFIGS_DIR / "experiments" / "vani-meeting-v1.yaml").read_text()))
    cfg = base.model_copy(update={
        "window": base.window.model_copy(update={"days": 14, "looks": 3}),
        "maturation_days": 1.0, "primary": base.primary.model_copy(update={"mde_rel": 0.9})})
    calls, exposures, t0 = _synthetic_world(cfg)
    bounds = boundaries.compute_boundaries(boundaries.planned_info_fractions(cfg), cfg)
    assert len(bounds.win) == cfg.window.looks

    verdicts = []
    for k in range(1, cfg.window.looks + 1):
        look_ts = t0 + timedelta(days=5 + 3 * k)
        outcomes = metrics.lead_outcomes(calls, exposures, cfg, look_ts)
        look = analysis.analyze_look(outcomes, cfg, k, look_ts, bounds, k == cfg.window.looks)
        verdicts.append(rules.evaluate_look(look, bounds, cfg))
    assert len(verdicts) == cfg.window.looks
    assert all(v.reason for v in verdicts)
    assert verdicts[-1].decision != "CONTINUE"  # the final look always decides


# A fresh interpreter whose audit hook fires on every open (builtins, io, os.open, the import
# system's open_code) and listing under SIMS_DIR. It is installed before anything is imported, so
# import-time reads count too. Touches are also recorded, in case engine code swallows the error.
_AUDITED = """
import importlib, os, sys
SIMS = os.path.realpath(sys.argv[1])
touched = []
class SimsTouched(Exception): pass
def hook(event, args):
    if event in ("open", "os.listdir", "os.scandir") and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.path.realpath(os.fsdecode(args[0]))
        if path == SIMS or path.startswith(SIMS + os.sep):
            touched.append(path)
            raise SimsTouched(path)
sys.addaudithook(hook)
sys.path.insert(0, sys.argv[2])
import test_engine_isolation as t
for m in t.ENGINE_MODULES:
    try:
        importlib.import_module("abtest." + m)
    except ModuleNotFoundError as e:
        if m not in t.OPTIONAL_MODULES or e.name != "abtest." + m:
            raise
t._engine_pass()
if touched:
    sys.exit(f"engine path touched {touched}")
win = os.path.join(SIMS, "win.yaml")
for control in (lambda: open(win), lambda: os.open(win, os.O_RDONLY), lambda: os.listdir(SIMS)):
    try:
        control()
        sys.exit("control not caught: the hook does not guard " + SIMS)
    except SimsTouched:
        pass
print("engine pass clean, controls caught")
"""


def test_runtime_full_engine_pass_never_touches_sims():
    tests_dir = str(pathlib.Path(__file__).parent)
    res = subprocess.run([sys.executable, "-c", _AUDITED, str(SIMS_DIR), tests_dir],
                         capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert res.returncode == 0, res.stderr or res.stdout
    assert res.stdout.strip() == "engine pass clean, controls caught"
