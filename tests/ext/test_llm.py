import json

import pytest

from abtest.ext import llm
from abtest.ext.models import LLM_CACHE_DIR


def test_stub_answers_and_never_hits_network():
    llm.set_stub(lambda model, messages, schema: {"ok": True} if schema else "plain")
    assert llm.chat("m", [{"role": "user", "content": "x"}], schema={"type": "object"}).data == {"ok": True}
    assert llm.chat("m", [{"role": "user", "content": "x"}]).text == "plain"
    assert not LLM_CACHE_DIR.exists()          # stubbed calls are never cached


def test_stub_mode_without_stub_fails_loudly():
    with pytest.raises(RuntimeError, match="no stub"):
        llm.chat("m", [{"role": "user", "content": "x"}])


def test_parse_json_tolerates_fences_and_prose():
    assert llm._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm._parse_json('Here you go: {"a": [1, 2]} hope it helps') == {"a": [1, 2]}


def test_spend_is_tracked_across_calls():
    assert llm.spent_usd() == 0.0
    llm._add_spend(0.25)
    llm._add_spend(0.5)
    assert llm.spent_usd() == pytest.approx(0.75)
    assert json.loads(llm.SPEND.read_text())["calls"] == 2


def test_model_roles_can_be_overridden(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_JUDGE", "x/y")
    assert llm.model_for("judge") == "x/y" and llm.model_for("fixer") == llm.MODELS["fixer"]
