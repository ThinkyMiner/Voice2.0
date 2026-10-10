"""Loop test isolation: no loop test writes the engine's JSONL decision mirror (logs/decisions.jsonl)."""

import pytest


@pytest.fixture(autouse=True)
def _no_decisions_jsonl(monkeypatch):
    from abtest import decision
    monkeypatch.setattr(decision, "JSONL", None)
