"""Every built module matches its stub in contracts.py: names, parameter order, kinds, defaults."""

import importlib
import inspect

import pytest

from abtest.contracts import MODULE_CONTRACTS


def _shape(fn):
    return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]


@pytest.mark.parametrize("module", sorted(MODULE_CONTRACTS))
def test_module_matches_contract(module):
    try:
        mod = importlib.import_module(f"abtest.{module}")
    except ModuleNotFoundError as e:
        if e.name != f"abtest.{module}":
            raise
        pytest.skip(f"{module}.py not built yet")
    stub = MODULE_CONTRACTS[module]
    for name, fn in vars(stub).items():
        if isinstance(fn, staticmethod):
            assert hasattr(mod, name), f"{module}.{name} missing"
            assert _shape(getattr(mod, name)) == _shape(fn.__func__), f"{module}.{name} signature drifted"
