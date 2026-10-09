import os
import shutil
import tempfile

# Before anything imports abtest.ext.models: every LOOP_DIR path in these tests is a temp dir.
os.environ["ABTEST_LOOP_DIR"] = tempfile.mkdtemp(prefix="vani-loop-test-")

import httpx  # noqa: E402
import pytest  # noqa: E402

from abtest.ext import llm  # noqa: E402
from abtest.ext.models import LOOP_DIR  # noqa: E402


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Extension tests never touch the network or the real loop data: each test starts with an
    empty LOOP_DIR, and the model is stubbed (tests install their own answers with llm.set_stub)."""
    shutil.rmtree(LOOP_DIR, ignore_errors=True)
    LOOP_DIR.mkdir(parents=True)

    def no_network(*a, **k):
        raise AssertionError("tests must not call the network; use llm.set_stub")
    monkeypatch.setattr(httpx, "post", no_network)
    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    yield
    llm.set_stub(None)
