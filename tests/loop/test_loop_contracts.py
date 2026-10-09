"""Every built loop module matches its stub in loop/contracts.py, and the shipped configs load."""

import importlib
import inspect

import pytest
import yaml

from abtest.loop.contracts import (DEFAULT_LOOP_CONFIG, LOOP_MODULE_CONTRACTS, PRICES, RUBRICS_DIR,
                                   LoopConfig, Prices, Rubric)


def _shape(fn):
    return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]


@pytest.mark.parametrize("module", sorted(LOOP_MODULE_CONTRACTS))
def test_loop_module_matches_contract(module):
    try:
        mod = importlib.import_module(f"abtest.loop.{module}")
    except ModuleNotFoundError as e:
        if e.name != f"abtest.loop.{module}":
            raise
        pytest.skip(f"loop/{module}.py not built yet")
    for name, fn in vars(LOOP_MODULE_CONTRACTS[module]).items():
        if isinstance(fn, staticmethod):
            assert hasattr(mod, name), f"loop.{module}.{name} missing"
            assert _shape(getattr(mod, name)) == _shape(fn.__func__), f"loop.{module}.{name} signature drifted"


def test_default_loop_config_validates():
    cfg = LoopConfig.model_validate(yaml.safe_load(DEFAULT_LOOP_CONFIG.read_text()))
    assert cfg.caps.max_live_calls_per_day == 0          # no live calls until L0 sets the caps
    assert cfg.provider.roles.keys() == {"judge", "proposer", "buyer", "bot"}


def test_prices_validate():
    Prices.model_validate(yaml.safe_load(PRICES.read_text()))


@pytest.mark.parametrize("path", sorted(RUBRICS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_shipped_rubrics_validate(path):
    r = Rubric.model_validate(yaml.safe_load(path.read_text()))
    assert all(c.weight > 0 for c in r.criteria)
    assert any(c.hard for c in r.criteria)
