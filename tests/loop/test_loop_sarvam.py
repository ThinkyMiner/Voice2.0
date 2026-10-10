"""The provider client: Sarvam over a mock transport, the offline stub, the validator, spend and the
cap, the cache, back-off and the secret (PLAN_LOOP §L1.5, §L2.3, §L2.9, §L9). Nothing here touches
the network: HTTP goes through httpx.MockTransport and batch STT through a fake SDK client."""

import hashlib
import json
import logging
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml

from abtest.loop import sarvam, store_loop
from abtest.loop.contracts import RUBRICS_DIR, SpendRow, sha256_json
from abtest.loop.sarvam import (CHAT_URL, ProviderError, SarvamProvider, SpendCapExceeded, StubProvider,
                                get_provider, load_loop_config, load_prices, validate_json)

KEY = "sk-test-SECRET-key-123"


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def base_cfg():
    return load_loop_config()


def make_cfg(base, *, rpm=6000, concurrency=4, max_spend=150.0, max_jobs=3, poll=30.0):
    return base.model_copy(update={
        "provider": base.provider.model_copy(update={"rpm": rpm, "concurrency": concurrency}),
        "caps": base.caps.model_copy(update={"max_spend_inr": max_spend}),
        "stt": base.stt.model_copy(update={"max_jobs_in_flight": max_jobs, "poll_interval_s": poll}),
    })


class FakeTime:
    """A clock that only moves when something sleeps, so back-off and rate limits cost no wall time."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


@pytest.fixture
def fake_time(monkeypatch):
    ft = FakeTime()
    monkeypatch.setattr(sarvam, "_sleep", ft.sleep)
    monkeypatch.setattr(sarvam, "_monotonic", ft.monotonic)
    return ft


def reply(content, finish="stop", prompt_tokens=100, completion_tokens=50):
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens}}


class FakeApi:
    """An httpx handler that replays `items` in order (the last one repeats) and records every request."""

    def __init__(self, *items):
        self.items, self.requests, self._lock = list(items), [], threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append({"url": str(request.url), "headers": dict(request.headers),
                                  "body": json.loads(request.content)})
            item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    @property
    def n(self):
        return len(self.requests)


def make(tmp_path, api, cfg, **kw):
    return SarvamProvider(cfg, api_key=KEY, transport=httpx.MockTransport(api), cache_dir=tmp_path / "cache", **kw)


SCHEMA = {"type": "object", "required": ["looping", "closure"], "properties": {
    "looping": {"type": "number", "minimum": 0, "maximum": 1},
    "closure": {"type": "integer", "minimum": 1, "maximum": 5}}}
GOOD = json.dumps({"looping": 0.2, "closure": 4})


def judge(p, user="BOT: hi"):
    return p.complete_json("judge", "You judge calls.", user, SCHEMA)


# ---------------------------------------------------------------------------------------
# config and factory
# ---------------------------------------------------------------------------------------

def test_default_config_and_prices_load(base_cfg):
    assert base_cfg.provider.rpm == 40 and base_cfg.stt.model == "saaras:v4"
    assert load_prices().llm["sarvam-105b"]["input_per_million_tokens"] == pytest.approx(29.28)


def test_get_provider_name_precedence(base_cfg, monkeypatch):
    monkeypatch.delenv("LOOP_PROVIDER", raising=False)
    assert get_provider(base_cfg).name == "sarvam"                       # cfg.provider.name
    assert get_provider(base_cfg, "stub").name == "stub"                 # name overrides cfg
    monkeypatch.setenv("LOOP_PROVIDER", "stub")
    assert get_provider(base_cfg, "sarvam").name == "stub"               # env overrides both
    monkeypatch.setenv("LOOP_PROVIDER", "nope")
    with pytest.raises(ValueError, match="unknown loop provider"):
        get_provider(base_cfg)


def test_only_low_high_max_reasoning_effort_is_accepted(base_cfg):
    roles = dict(base_cfg.provider.roles)
    roles["judge"] = roles["judge"].model_copy(update={"reasoning_effort": "medium"})
    cfg = base_cfg.model_copy(update={"provider": base_cfg.provider.model_copy(update={"roles": roles})})
    with pytest.raises(ProviderError, match="low\\|high\\|max"):
        SarvamProvider(cfg, api_key=KEY)


def test_spend_cap_is_not_a_provider_error():
    assert not issubclass(SpendCapExceeded, ProviderError)   # `except ProviderError` must never swallow the cap


# ---------------------------------------------------------------------------------------
# request shape
# ---------------------------------------------------------------------------------------

def test_complete_json_request_shape(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD))
    out = judge(make(tmp_path, api, make_cfg(base_cfg)))
    assert out == {"looping": 0.2, "closure": 4}
    req = api.requests[0]
    assert req["url"] == CHAT_URL == "https://api.sarvam.ai/v1/chat/completions"
    assert req["headers"]["api-subscription-key"] == KEY
    body = req["body"]
    assert body["model"] == "sarvam-105b" and body["temperature"] == 0
    assert body["reasoning_effort"] == "low" and body["max_tokens"] == 4096        # judge role in default.yaml
    assert body["response_format"] == {"type": "json_object"}
    system = body["messages"][0]
    assert system["role"] == "system" and system["content"].startswith("You judge calls.")
    assert "Return only a JSON object matching this schema: " + json.dumps(SCHEMA, sort_keys=True) in system["content"]
    assert body["messages"][1] == {"role": "user", "content": "BOT: hi"}


def test_chat_request_shape_and_stripped_reply(tmp_path, base_cfg):
    api = FakeApi(reply("  Namaste ji.\n"))
    out = make(tmp_path, api, make_cfg(base_cfg)).chat("bot", [{"role": "system", "content": "S"},
                                                                  {"role": "user", "content": "<call connected>"}])
    assert out == "Namaste ji."
    body = api.requests[0]["body"]
    assert "response_format" not in body
    assert body["reasoning_effort"] == "low" and body["max_tokens"] == 2048 and body["temperature"] == 0.5
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_each_role_takes_its_settings_from_the_config(tmp_path, base_cfg):
    api = FakeApi(reply("x"))
    p = make(tmp_path, api, make_cfg(base_cfg))
    p.chat("buyer", [{"role": "system", "content": "persona"}])
    p.chat("proposer", [{"role": "system", "content": "p"}])
    buyer, proposer = (r["body"] for r in api.requests)
    assert (buyer["reasoning_effort"], buyer["max_tokens"]) == ("low", 1024)
    assert (proposer["reasoning_effort"], proposer["max_tokens"]) == ("low", 8192)


def test_the_gate_role_borrows_the_judge_settings_and_is_deterministic(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD))
    make(tmp_path, api, make_cfg(base_cfg)).complete_json("gate", "s", "u", SCHEMA)
    body = api.requests[0]["body"]
    assert (body["reasoning_effort"], body["max_tokens"], body["temperature"]) == ("low", 4096, 0)


# ---------------------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------------------

def test_a_cache_hit_makes_no_request_and_costs_nothing(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD, prompt_tokens=1000, completion_tokens=500))
    conn = store_loop.connect_loop(":memory:")
    p = make(tmp_path, api, make_cfg(base_cfg))
    p.attach(conn)
    p.set_context(1, "judge")
    first = judge(p)
    spent = store_loop.total_spend(conn)
    assert api.n == 1 and spent > 0
    assert judge(p) == first
    assert api.n == 1 and store_loop.total_spend(conn) == spent                        # no request, no spend
    assert conn.execute("SELECT COUNT(*) FROM loop_spend").fetchone()[0] == 1
    # a fresh provider on the same cache directory is a hit too (the disk cache is what replays a generation)
    q = make(tmp_path, api, make_cfg(base_cfg))
    assert judge(q) == first and api.n == 1


def test_cache_file_is_named_by_the_sha256_of_provider_model_endpoint_and_body(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD))
    judge(make(tmp_path, api, make_cfg(base_cfg)))
    key = sha256_json({"provider": "sarvam", "model": "sarvam-105b", "endpoint": CHAT_URL, "body": api.requests[0]["body"]})
    assert [f.name for f in (tmp_path / "cache").glob("*.json")] == [f"{key}.json"]


def test_a_changed_request_is_a_different_cache_entry(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD))
    p = make(tmp_path, api, make_cfg(base_cfg))
    judge(p, "BOT: one")
    judge(p, "BOT: two")
    assert api.n == 2 and len(list((tmp_path / "cache").glob("*.json"))) == 2


def test_cache_is_written_atomically_and_a_torn_entry_is_ignored(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD))
    p = make(tmp_path, api, make_cfg(base_cfg))
    judge(p)
    assert not list((tmp_path / "cache").glob("*.tmp"))
    entry = next((tmp_path / "cache").glob("*.json"))
    entry.write_text("{not json", encoding="utf-8")                                       # a crash mid-write
    assert judge(p) == {"looping": 0.2, "closure": 4} and api.n == 2                      # re-fetched, not crashed


def test_the_cache_holds_the_reply_not_the_key(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD))
    judge(make(tmp_path, api, make_cfg(base_cfg)))
    assert all(KEY not in f.read_text() for f in (tmp_path / "cache").glob("*.json"))


# ---------------------------------------------------------------------------------------
# JSON validation and the one retry
# ---------------------------------------------------------------------------------------

def test_invalid_reply_is_retried_once_with_the_error_appended(tmp_path, base_cfg):
    bad = json.dumps({"looping": 0.2})                                                    # `closure` missing
    api = FakeApi(reply(bad), reply(GOOD))
    out = judge(make(tmp_path, api, make_cfg(base_cfg)))
    assert out == {"looping": 0.2, "closure": 4} and api.n == 2
    retry = api.requests[1]["body"]["messages"]
    assert retry[-2] == {"role": "assistant", "content": bad}
    assert retry[-1]["role"] == "user" and "closure" in retry[-1]["content"] and "invalid" in retry[-1]["content"]
    assert api.requests[1]["body"]["max_tokens"] == 4096                                  # a parse error is not a length error


def test_two_invalid_replies_raise_after_exactly_two_requests(tmp_path, base_cfg):
    api = FakeApi(reply("not json at all"), reply(json.dumps({"looping": "high", "closure": 4})))
    with pytest.raises(ProviderError, match="no valid JSON object after one retry"):
        judge(make(tmp_path, api, make_cfg(base_cfg)))
    assert api.n == 2


@pytest.mark.parametrize("bad", ["[1, 2]", '{"looping": 2.0, "closure": 4}', '{"looping": 0.1, "closure": 9}',
                                 '{"looping": 0.1, "closure": 2.5}', '{"looping": true, "closure": 4}', "hello"])
def test_each_kind_of_invalid_reply_triggers_the_retry(tmp_path, base_cfg, bad):
    api = FakeApi(reply(bad), reply(GOOD))
    assert judge(make(tmp_path, api, make_cfg(base_cfg))) == {"looping": 0.2, "closure": 4}
    assert api.n == 2


def test_a_code_fence_or_a_sentence_around_the_json_is_tolerated(tmp_path, base_cfg):
    api = FakeApi(reply("```json\n" + GOOD + "\n```"))
    assert judge(make(tmp_path, api, make_cfg(base_cfg)), "BOT: a") == {"looping": 0.2, "closure": 4}
    api2 = FakeApi(reply("Here is the result: " + GOOD + " Hope that helps."))
    assert judge(make(tmp_path, api2, make_cfg(base_cfg)), "BOT: b") == {"looping": 0.2, "closure": 4}
    assert api.n == 1 and api2.n == 1


def test_empty_content_is_retried(tmp_path, base_cfg):
    api = FakeApi(reply(None), reply(GOOD))
    assert judge(make(tmp_path, api, make_cfg(base_cfg))) == {"looping": 0.2, "closure": 4}
    assert api.n == 2 and "empty" in api.requests[1]["body"]["messages"][-1]["content"]


def test_a_whole_integer_float_is_a_valid_integer(tmp_path, base_cfg):
    api = FakeApi(reply(json.dumps({"looping": 0.2, "closure": 4.0})))
    assert judge(make(tmp_path, api, make_cfg(base_cfg)))["closure"] == 4 and api.n == 1


# ---------------------------------------------------------------------------------------
# the length retry
# ---------------------------------------------------------------------------------------

def test_finish_reason_length_retries_once_with_double_max_tokens(tmp_path, base_cfg):
    api = FakeApi(reply(None, finish="length"), reply(GOOD))
    assert judge(make(tmp_path, api, make_cfg(base_cfg))) == {"looping": 0.2, "closure": 4}
    assert [r["body"]["max_tokens"] for r in api.requests] == [4096, 8192]


def test_a_truncated_reply_with_content_also_counts_as_length(tmp_path, base_cfg):
    api = FakeApi(reply('{"looping": 0.', finish="length"), reply(GOOD))
    assert judge(make(tmp_path, api, make_cfg(base_cfg))) == {"looping": 0.2, "closure": 4}
    assert api.n == 2


def test_length_twice_raises(tmp_path, base_cfg):
    api = FakeApi(reply(None, finish="length"))
    with pytest.raises(ProviderError, match="no valid JSON object"):
        judge(make(tmp_path, api, make_cfg(base_cfg)))
    assert [r["body"]["max_tokens"] for r in api.requests] == [4096, 8192]


def test_length_retry_grows_to_the_ceiling_then_gives_up(tmp_path, base_cfg):
    api = FakeApi(reply(None, finish="length"), reply(None, finish="length"))
    with pytest.raises(ProviderError, match="max_tokens=16384"):
        make(tmp_path, api, make_cfg(base_cfg)).complete_json("proposer", "s", "u", SCHEMA)   # proposer is 8192
    assert [r["body"]["max_tokens"] for r in api.requests] == [8192, 16384]


def test_chat_length_retry_doubles_max_tokens_and_returns_the_text(tmp_path, base_cfg):
    api = FakeApi(reply(None, finish="length"), reply("Theek hai."))
    out = make(tmp_path, api, make_cfg(base_cfg)).chat("bot", [{"role": "system", "content": "S"}])
    assert out == "Theek hai." and [r["body"]["max_tokens"] for r in api.requests] == [2048, 4096]


def test_chat_returns_a_cut_off_reply_rather_than_failing_and_raises_on_none(tmp_path, base_cfg):
    api = FakeApi(reply("half a sent", finish="length"))
    assert make(tmp_path, api, make_cfg(base_cfg)).chat("bot", [{"role": "system", "content": "S"}]) == "half a sent"
    assert api.n == 2
    api2 = FakeApi(reply(None, finish="length"))
    with pytest.raises(ProviderError, match="empty after the length retry"):
        make(tmp_path, api2, make_cfg(base_cfg)).chat("bot", [{"role": "system", "content": "S2"}])


# ---------------------------------------------------------------------------------------
# rate limiting and back-off
# ---------------------------------------------------------------------------------------

def test_429_backs_off_one_then_two_seconds(tmp_path, base_cfg, fake_time):
    api = FakeApi(httpx.Response(429, text="slow down"), httpx.Response(429), reply(GOOD))
    assert judge(make(tmp_path, api, make_cfg(base_cfg))) == {"looping": 0.2, "closure": 4}
    assert api.n == 3 and fake_time.sleeps == [1, 2]


def test_503_and_transport_errors_back_off_too(tmp_path, base_cfg, fake_time):
    api = FakeApi(httpx.Response(503), httpx.ConnectError("boom"), reply(GOOD))
    assert judge(make(tmp_path, api, make_cfg(base_cfg))) == {"looping": 0.2, "closure": 4}
    assert fake_time.sleeps == [1, 2]


def test_five_failures_in_a_row_raise_with_the_documented_delays(tmp_path, base_cfg, fake_time):
    api = FakeApi(httpx.Response(429))
    with pytest.raises(ProviderError, match="after 5 tries"):
        judge(make(tmp_path, api, make_cfg(base_cfg)))
    assert api.n == 5 and fake_time.sleeps == [1, 2, 4, 8]


def test_other_http_errors_are_not_retried(tmp_path, base_cfg, fake_time):
    api = FakeApi(httpx.Response(400, text="reasoning_effort must be low, high or max"))
    with pytest.raises(ProviderError, match="HTTP 400.*reasoning_effort"):
        judge(make(tmp_path, api, make_cfg(base_cfg)))
    assert api.n == 1 and fake_time.sleeps == []


def test_a_200_with_an_unexpected_shape_is_a_provider_error(tmp_path, base_cfg):
    with pytest.raises(ProviderError, match="unexpected response shape"):
        judge(make(tmp_path, FakeApi({"not": "chat"}), make_cfg(base_cfg)))


def test_token_bucket_spaces_requests_after_the_burst(fake_time):
    bucket = sarvam._TokenBucket(60)                       # 1 per second, burst capacity 6
    for _ in range(6):
        bucket.acquire()
    assert fake_time.sleeps == []
    bucket.acquire()
    assert fake_time.sleeps == [pytest.approx(1.0)]
    fake_time.now += 10                                    # idle time refills, up to the capacity only
    n = len(fake_time.sleeps)
    for _ in range(6):
        bucket.acquire()
    assert len(fake_time.sleeps) == n
    bucket.acquire()
    assert len(fake_time.sleeps) == n + 1


def test_token_bucket_is_thread_safe(fake_time):
    bucket = sarvam._TokenBucket(600)                      # capacity 60
    threads = [threading.Thread(target=bucket.acquire) for _ in range(60)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert fake_time.sleeps == []                          # exactly the burst: no over-issue, no spurious wait
    bucket.acquire()
    assert len(fake_time.sleeps) == 1


def test_the_semaphore_caps_concurrent_requests(tmp_path, base_cfg):
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def handler(request):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.03)
        with lock:
            state["active"] -= 1
        return httpx.Response(200, json=reply("ok"))

    p = SarvamProvider(make_cfg(base_cfg, concurrency=2), api_key=KEY, transport=httpx.MockTransport(handler),
                       cache_dir=tmp_path / "cache")
    threads = [threading.Thread(target=p.chat, args=("bot", [{"role": "system", "content": f"s{i}"}])) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert state["max"] == 2


# ---------------------------------------------------------------------------------------
# spend
# ---------------------------------------------------------------------------------------

JUDGE_COST = 1000 / 1e6 * 29.28 + 500 / 1e6 * 73.2


def test_spend_is_reserved_then_settled_with_the_actual_cost(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD, prompt_tokens=1000, completion_tokens=500))
    conn = store_loop.connect_loop(":memory:")
    p = make(tmp_path, api, make_cfg(base_cfg))
    p.attach(conn)
    p.set_context(2, "judge")
    judge(p, "BOT: hi")
    [row] = conn.execute("SELECT spend_id, generation, step, state, estimate_inr, actual_inr, input_tokens, "
                         "output_tokens FROM loop_spend").fetchall()
    body = api.requests[0]["body"]
    key = sha256_json({"provider": "sarvam", "model": "sarvam-105b", "endpoint": CHAT_URL, "body": body})
    assert row[0] == sha256_json([2, "judge", key])
    assert tuple(row[1:4]) == (2, "judge", "settled") and tuple(row[6:]) == (1000, 500)
    assert row[5] == pytest.approx(JUDGE_COST)
    chars = sum(len(m["content"]) for m in body["messages"])
    assert row[4] == pytest.approx(chars / 3.5 / 1e6 * 29.28 + 4096 / 2 / 1e6 * 73.2)   # the estimate stays on the row
    assert store_loop.total_spend(conn) == pytest.approx(JUDGE_COST)


def test_the_cap_refuses_before_any_request(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD))
    conn = store_loop.connect_loop(":memory:")
    p = make(tmp_path, api, make_cfg(base_cfg, max_spend=0.0001))
    p.attach(conn)
    with pytest.raises(SpendCapExceeded, match="spend cap"):
        judge(p)
    assert api.n == 0 and conn.execute("SELECT COUNT(*) FROM loop_spend").fetchone()[0] == 0


@pytest.mark.parametrize("use_conn", [True, False], ids=["store", "memory"])
def test_the_cap_counts_what_is_already_spent(tmp_path, base_cfg, use_conn):
    # estimate ~ INR 0.15 (half of 4,096 output tokens); one settled call is INR 0.066
    api = FakeApi(reply(GOOD, prompt_tokens=1000, completion_tokens=500))
    p = make(tmp_path, api, make_cfg(base_cfg, max_spend=0.2))
    conn = store_loop.connect_loop(":memory:")
    if use_conn:
        p.attach(conn)
    judge(p, "BOT: first")                                                                # 0 + 0.15 <= 0.2
    with pytest.raises(SpendCapExceeded):
        judge(p, "BOT: second")                                                           # 0.066 + 0.15 > 0.2
    assert api.n == 1
    assert judge(p, "BOT: first") == {"looping": 0.2, "closure": 4}                       # a cache hit is free, cap or not


def test_a_failed_request_is_settled_at_zero(tmp_path, base_cfg):
    conn = store_loop.connect_loop(":memory:")
    p = make(tmp_path, FakeApi(httpx.Response(400, text="bad")), make_cfg(base_cfg))
    p.attach(conn)
    with pytest.raises(ProviderError):
        judge(p)
    assert store_loop.total_spend(conn) == 0.0
    assert conn.execute("SELECT state FROM loop_spend").fetchone()[0] == "settled"


def test_a_crash_leaves_the_reservation_counted_at_its_estimate(tmp_path, base_cfg):
    conn = store_loop.connect_loop(":memory:")
    p = make(tmp_path, FakeApi(RuntimeError("process died")), make_cfg(base_cfg))
    p.attach(conn)
    with pytest.raises(RuntimeError):
        judge(p)
    [state, estimate] = conn.execute("SELECT state, estimate_inr FROM loop_spend").fetchone()
    assert state == "reserved" and store_loop.total_spend(conn) == pytest.approx(estimate) and estimate > 0


def test_a_retry_after_a_lost_cache_entry_reuses_the_spend_row(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD, prompt_tokens=1000, completion_tokens=500))
    conn = store_loop.connect_loop(":memory:")
    p = make(tmp_path, api, make_cfg(base_cfg))
    p.attach(conn)
    p.set_context(1, "judge")
    judge(p)
    for f in (tmp_path / "cache").glob("*.json"):
        f.unlink()
    judge(p)                                                                              # same spend_id: reserve is refused
    assert api.n == 2 and conn.execute("SELECT COUNT(*) FROM loop_spend").fetchone()[0] == 1
    assert store_loop.total_spend(conn) == pytest.approx(JUDGE_COST)                      # not doubled


def test_context_labels_the_rows_and_a_new_context_makes_new_rows(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD))
    conn = store_loop.connect_loop(":memory:")
    p = make(tmp_path, api, make_cfg(base_cfg))
    p.attach(conn)
    p.set_context(1, "judge")
    judge(p)
    p.set_context(2, "judge")
    for f in (tmp_path / "cache").glob("*.json"):
        f.unlink()
    judge(p)
    assert [tuple(r) for r in conn.execute("SELECT generation, step FROM loop_spend ORDER BY generation")] == \
        [(1, "judge"), (2, "judge")]


def test_spend_made_before_attach_moves_into_the_store(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD, prompt_tokens=1000, completion_tokens=500))
    p = make(tmp_path, api, make_cfg(base_cfg))
    p.set_context(3, "observe")
    judge(p)
    conn = store_loop.connect_loop(":memory:")
    p.attach(conn)
    assert store_loop.total_spend(conn) == pytest.approx(JUDGE_COST)
    assert conn.execute("SELECT generation, step, state FROM loop_spend").fetchone()[:] == (3, "observe", "settled")


def test_worker_threads_record_spend_through_their_own_connection(tmp_path, base_cfg):
    api = FakeApi(reply(GOOD, prompt_tokens=1000, completion_tokens=500))
    conn = store_loop.connect_loop(tmp_path / "loop.db")
    p = make(tmp_path, api, make_cfg(base_cfg))
    p.attach(conn)
    errors = []

    def work(i):
        try:
            judge(p, f"BOT: call {i}")
        except Exception as e:            # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert store_loop.total_spend(conn) == pytest.approx(8 * JUDGE_COST)
    assert conn.execute("SELECT COUNT(*) FROM loop_spend WHERE state = 'settled'").fetchone()[0] == 8


def test_an_in_memory_store_cannot_be_shared_across_threads_and_says_so(tmp_path, base_cfg):
    conn = store_loop.connect_loop(":memory:")
    p = make(tmp_path, FakeApi(reply(GOOD)), make_cfg(base_cfg))
    p.attach(conn)
    errors = []
    t = threading.Thread(target=lambda: errors.append(_raises(lambda: judge(p))))
    t.start()
    t.join()
    assert isinstance(errors[0], ProviderError) and "file-backed" in str(errors[0])


def _raises(fn):
    try:
        fn()
    except Exception as e:                # noqa: BLE001
        return e
    return None


# ---------------------------------------------------------------------------------------
# the secret
# ---------------------------------------------------------------------------------------

def test_the_key_never_appears_in_logs_errors_or_cache(tmp_path, base_cfg, caplog, fake_time):
    caplog.set_level(logging.DEBUG)
    echo = httpx.Response(400, text=f"invalid api-subscription-key {KEY}")                # a server that echoes the key
    p = make(tmp_path, FakeApi(echo), make_cfg(base_cfg))
    with pytest.raises(ProviderError) as e1:
        judge(p)
    assert KEY not in str(e1.value) and "***" in str(e1.value)
    p2 = make(tmp_path, FakeApi(httpx.Response(429)), make_cfg(base_cfg))
    with pytest.raises(ProviderError) as e2:
        judge(p2)
    p3 = make(tmp_path, FakeApi(reply("junk")), make_cfg(base_cfg))
    with pytest.raises(ProviderError) as e3:
        judge(p3)
    p4 = make(tmp_path, FakeApi(reply(GOOD)), make_cfg(base_cfg))
    judge(p4, "BOT: ok")
    assert all(KEY not in str(e.value) for e in (e1, e2, e3)) and KEY not in repr(p4)
    assert KEY not in caplog.text
    assert all(KEY not in f.read_text() for f in (tmp_path / "cache").glob("*.json"))


def test_the_key_comes_from_the_environment(tmp_path, base_cfg, monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", KEY)
    api = FakeApi(reply(GOOD))
    judge(SarvamProvider(make_cfg(base_cfg), transport=httpx.MockTransport(api), cache_dir=tmp_path / "c"))
    assert api.requests[0]["headers"]["api-subscription-key"] == KEY


@pytest.mark.parametrize("line", ['SARVAM_API_KEY=' + KEY, 'export SARVAM_API_KEY="' + KEY + '"',
                                  "SARVAM_API_KEY='" + KEY + "'  ", "SARVAM_API_KEY=" + KEY + " # comment"])
def test_the_key_falls_back_to_the_env_file(tmp_path, base_cfg, monkeypatch, line):
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    monkeypatch.setattr(sarvam, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("OTHER=1\n" + line + "\n", encoding="utf-8")
    api = FakeApi(reply(GOOD))
    judge(SarvamProvider(make_cfg(base_cfg), transport=httpx.MockTransport(api), cache_dir=tmp_path / "c"))
    assert api.requests[0]["headers"]["api-subscription-key"] == KEY


def test_a_missing_key_is_an_error_that_names_the_variable(tmp_path, base_cfg, monkeypatch):
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    monkeypatch.setattr(sarvam, "ROOT", tmp_path)                                         # no .env here
    api = FakeApi(reply(GOOD))
    p = SarvamProvider(make_cfg(base_cfg), transport=httpx.MockTransport(api), cache_dir=tmp_path / "c")
    with pytest.raises(ProviderError, match="SARVAM_API_KEY"):
        judge(p)
    assert api.n == 0


# ---------------------------------------------------------------------------------------
# validate_json
# ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, schema, ok", [
    ({"a": 1}, {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}, True),
    ({}, {"type": "object", "required": ["a"]}, False),
    (True, {"type": "number"}, False),
    (True, {"type": "integer"}, False),
    (True, {"type": "boolean"}, True),
    (2.0, {"type": "integer"}, True),
    (2.5, {"type": "integer"}, False),
    (float("nan"), {"type": "number"}, False),
    ("x", {"type": ["string", "null"]}, True),
    (None, {"type": ["string", "null"]}, True),
    (3, {"type": "number", "minimum": 0, "maximum": 1}, False),
    (-1, {"type": "number", "minimum": 0}, False),
    (0.5, {"type": "number", "minimum": 0, "maximum": 1}, True),
    ("c", {"enum": ["a", "b"]}, False),
    ("a", {"type": "string", "enum": ["a", "b"]}, True),
    ([1, 2], {"type": "array", "items": {"type": "integer"}, "minItems": 2}, True),
    ([1, "x"], {"type": "array", "items": {"type": "integer"}}, False),
    ([1], {"type": "array", "minItems": 2}, False),
    ({"a": 1, "b": 2}, {"type": "object", "properties": {"a": {}}, "additionalProperties": False}, False),
    ("ab", {"type": "string", "minLength": 3}, False),
    ({"x": {"y": [{"z": "q"}]}},
     {"type": "object", "properties": {"x": {"type": "object", "properties": {"y": {"type": "array",
      "items": {"type": "object", "properties": {"z": {"type": "string"}}, "required": ["z"]}}}}}}, True),
    ({"x": {"y": [{"z": 5}]}},
     {"type": "object", "properties": {"x": {"type": "object", "properties": {"y": {"type": "array",
      "items": {"type": "object", "properties": {"z": {"type": "string"}}}}}}}}, False),
])
def test_validate_json(value, schema, ok):
    assert (validate_json(value, schema) == []) is ok


def test_validate_json_names_the_path():
    errors = validate_json({"x": [{"z": 5}]}, {"type": "object", "properties": {"x": {"type": "array", "items": {
        "type": "object", "properties": {"z": {"type": "string"}}}}}})
    assert errors == ["$.x[0].z: expected string, got int"]


# ---------------------------------------------------------------------------------------
# batch speech-to-text (a fake SDK client)
# ---------------------------------------------------------------------------------------

def stt_output(name, seconds=10.0):
    return {"request_id": "r", "transcript": f"t {name}", "language_code": "hi-IN", "diarized_transcript": {"entries": [
        {"transcript": "नमस्ते, क्या मेरी बात", "start_time_seconds": 0.5, "end_time_seconds": 3.0, "speaker_id": "1"},
        {"transcript": "हाँ", "start_time_seconds": 3.0, "end_time_seconds": seconds, "speaker_id": "0"}]}}


class FakeStt:
    """Stands in for sarvamai.SarvamAI: records every call, writes `<input name>.json` outputs."""

    def __init__(self, fail_job=None, skip=(), output=stt_output):
        self.speech_to_text_job = self
        self.jobs, self.fail_job, self.skip, self.output = [], fail_job, set(skip), output
        self.active = self.max_active = 0
        self._lock = threading.Lock()

    def create_job(self, **kwargs):
        job = FakeJob(self, kwargs, len(self.jobs))
        self.jobs.append(job)
        return job


class FakeJob:
    def __init__(self, client, kwargs, index):
        self.client, self.kwargs, self.index, self.paths, self.waited = client, kwargs, index, [], None

    def upload_files(self, paths):
        self.paths = list(paths)

    def start(self):
        pass

    def wait_until_complete(self, poll_interval, timeout):
        self.waited = (poll_interval, timeout)
        with self.client._lock:
            self.client.active += 1
            self.client.max_active = max(self.client.max_active, self.client.active)
        time.sleep(0.05)
        with self.client._lock:
            self.client.active -= 1
        if self.index == self.client.fail_job:
            raise RuntimeError("job failed on the server")

    def download_outputs(self, out_dir):
        for p in self.paths:
            name = Path(p).name
            if name not in self.client.skip:
                (Path(out_dir) / f"{name}.json").write_text(json.dumps(self.client.output(name)), encoding="utf-8")


def audio(tmp_path, n, prefix="a"):
    d = tmp_path / "audio"
    d.mkdir(exist_ok=True)
    paths = []
    for i in range(n):
        p = d / f"{prefix}{i:03d}.mp3"
        p.write_bytes(f"audio-{prefix}-{i}".encode() * 10)
        paths.append(p)
    return paths, [f"{prefix}{i:03d}" for i in range(n)]


def stt_provider(tmp_path, base_cfg, fake, **cfg_kw):
    return SarvamProvider(make_cfg(base_cfg, **cfg_kw), api_key=KEY, cache_dir=tmp_path / "cache",
                          stt_client_factory=lambda: fake)


def test_batch_stt_runs_jobs_of_twenty_with_the_documented_settings(tmp_path, base_cfg):
    fake = FakeStt()
    paths, ids = audio(tmp_path, 45)
    out = stt_provider(tmp_path, base_cfg, fake).transcribe_batch(paths, ids)
    assert [len(j.paths) for j in fake.jobs] == [20, 20, 5]
    assert fake.jobs[0].kwargs == {"model": "saaras:v4", "mode": "transcribe", "language_code": "unknown",
                                   "with_diarization": True, "num_speakers": 2}
    assert fake.jobs[0].waited == (30.0, 1800)
    assert [t.call_id for t in out] == ids
    t = out[0]
    assert t.language == "hi-IN" and t.stt_model == "saaras:v4" and t.audio_seconds == 10.0
    assert [(s.speaker_id, s.start_s, s.end_s) for s in t.segments] == [("1", 0.5, 3.0), ("0", 3.0, 10.0)]
    assert t.segments[0].text == "नमस्ते, क्या मेरी बात"


def test_at_most_max_jobs_in_flight_run_at_once(tmp_path, base_cfg):
    paths, ids = audio(tmp_path, 100)                                                     # 5 jobs
    fake = FakeStt()
    stt_provider(tmp_path, base_cfg, fake, max_jobs=2).transcribe_batch(paths, ids)
    assert len(fake.jobs) == 5 and fake.max_active == 2
    fake3 = FakeStt()
    paths2, ids2 = audio(tmp_path, 100, prefix="b")
    stt_provider(tmp_path, base_cfg, fake3, max_jobs=3).transcribe_batch(paths2, ids2)
    assert fake3.max_active == 3


def test_stt_is_cached_per_file_by_audio_sha256(tmp_path, base_cfg):
    fake = FakeStt()
    paths, ids = audio(tmp_path, 3)
    p = stt_provider(tmp_path, base_cfg, fake)
    first = p.transcribe_batch(paths, ids)
    assert len(fake.jobs) == 1
    again = p.transcribe_batch(paths, ids)
    assert len(fake.jobs) == 1 and again == first                                          # no job, no cost
    renamed = tmp_path / "audio" / "same-bytes-other-name.mp3"
    renamed.write_bytes(paths[0].read_bytes())
    third = p.transcribe_batch([renamed], ["other-call"])
    assert len(fake.jobs) == 1 and third[0].call_id == "other-call"
    assert third[0].segments == first[0].segments                                          # the key is the audio, not the name
    assert len(list((tmp_path / "cache").glob("*.json"))) == 3


def test_only_the_uncached_files_are_sent(tmp_path, base_cfg):
    fake = FakeStt()
    paths, ids = audio(tmp_path, 4)
    p = stt_provider(tmp_path, base_cfg, fake)
    p.transcribe_batch(paths[:2], ids[:2])
    p.transcribe_batch(paths, ids)
    assert [Path(x).name for x in fake.jobs[1].paths] == ["a002.mp3", "a003.mp3"]


def test_identical_audio_in_one_call_is_uploaded_once(tmp_path, base_cfg):
    fake = FakeStt()
    (tmp_path / "x").mkdir()
    a, b = tmp_path / "x" / "one.mp3", tmp_path / "x" / "two.mp3"
    a.write_bytes(b"same"), b.write_bytes(b"same")
    out = stt_provider(tmp_path, base_cfg, fake).transcribe_batch([a, b], ["c1", "c2"])
    assert len(fake.jobs[0].paths) == 1 and [t.call_id for t in out] == ["c1", "c2"]
    assert out[0].segments == out[1].segments


def test_stt_cost_is_audio_hours_times_the_diarized_price_one_row_per_job(tmp_path, base_cfg):
    fake = FakeStt(output=lambda name: stt_output(name, seconds=36.0))
    conn = store_loop.connect_loop(":memory:")
    p = stt_provider(tmp_path, base_cfg, fake)
    p.attach(conn)
    p.set_context(0, "transcribe")
    paths, ids = audio(tmp_path, 25)                                                       # jobs of 20 and 5
    p.transcribe_batch(paths, ids)
    rows = conn.execute("SELECT generation, step, state, actual_inr FROM loop_spend ORDER BY rowid").fetchall()
    assert len(rows) == 2 and all(tuple(r[:3]) == (0, "transcribe", "settled") for r in rows)
    assert rows[0][3] == pytest.approx(20 * 36.0 / 3600 * 45.0) and rows[1][3] == pytest.approx(5 * 36.0 / 3600 * 45.0)
    assert store_loop.total_spend(conn) == pytest.approx(25 * 36.0 / 3600 * 45.0)
    p.transcribe_batch(paths, ids)                                                         # all cached
    assert store_loop.total_spend(conn) == pytest.approx(25 * 36.0 / 3600 * 45.0)


def test_stt_cap_refusal_happens_before_any_job(tmp_path, base_cfg):
    fake = FakeStt()
    conn = store_loop.connect_loop(":memory:")
    big = tmp_path / "big.mp3"
    big.write_bytes(b"x" * (16_000 * 3600))                                                # ~1 hour by size: ~INR 45
    p = stt_provider(tmp_path, base_cfg, fake, max_spend=10.0)
    p.attach(conn)
    with pytest.raises(SpendCapExceeded):
        p.transcribe_batch([big], ["c1"])
    assert fake.jobs == [] and store_loop.total_spend(conn) == 0.0


def test_a_failed_job_raises_after_the_others_finish_and_are_cached(tmp_path, base_cfg):
    fake = FakeStt(fail_job=1)
    conn = store_loop.connect_loop(":memory:")
    paths, ids = audio(tmp_path, 45)
    p = stt_provider(tmp_path, base_cfg, fake)
    p.attach(conn)
    with pytest.raises(ProviderError, match="1 of 3 speech-to-text jobs failed"):
        p.transcribe_batch(paths, ids)
    states = [tuple(r) for r in conn.execute("SELECT state, actual_inr FROM loop_spend ORDER BY rowid")]
    assert all(s == "settled" for s, _ in states) and sorted(round(a, 6) for _, a in states) == \
        sorted([0.0, round(20 * 10 / 3600 * 45, 6), round(5 * 10 / 3600 * 45, 6)])
    healthy = FakeStt()
    p2 = stt_provider(tmp_path, base_cfg, healthy)
    out = p2.transcribe_batch(paths, ids)
    assert [len(j.paths) for j in healthy.jobs] == [20] and all(t.segments for t in out)   # only the failed chunk reruns


def test_a_missing_output_file_comes_back_empty_and_is_not_cached(tmp_path, base_cfg):
    fake = FakeStt(skip={"a001.mp3"})
    paths, ids = audio(tmp_path, 3)
    p = stt_provider(tmp_path, base_cfg, fake)
    out = p.transcribe_batch(paths, ids)
    assert [bool(t.segments) for t in out] == [True, False, True]
    assert out[1].call_id == "a001" and out[1].audio_seconds > 0                           # the size-based estimate
    healthy = FakeStt()
    stt_provider(tmp_path, base_cfg, healthy).transcribe_batch(paths, ids)
    assert [Path(x).name for x in healthy.jobs[0].paths] == ["a001.mp3"]


def test_output_without_diarization_becomes_one_segment(tmp_path, base_cfg):
    fake = FakeStt(output=lambda name: {"transcript": "sirf text", "language_code": "hi-IN"})
    paths, ids = audio(tmp_path, 1)
    [t] = stt_provider(tmp_path, base_cfg, fake).transcribe_batch(paths, ids)
    assert [(s.speaker_id, s.text) for s in t.segments] == [("0", "sirf text")]


def test_the_sdk_is_imported_lazily_and_its_absence_is_explained(tmp_path, base_cfg, monkeypatch):
    monkeypatch.setitem(sys.modules, "sarvamai", None)                                     # makes `import sarvamai` fail
    p = SarvamProvider(make_cfg(base_cfg), api_key=KEY, cache_dir=tmp_path / "cache")      # constructing needs no SDK
    paths, ids = audio(tmp_path, 1)
    conn = store_loop.connect_loop(":memory:")
    p.attach(conn)
    with pytest.raises(ProviderError, match=r"uv run --with sarvamai"):
        p.transcribe_batch(paths, ids)
    assert store_loop.total_spend(conn) == 0.0                                             # failed before reserving money


def test_cached_audio_needs_no_sdk(tmp_path, base_cfg, monkeypatch):
    paths, ids = audio(tmp_path, 1)
    stt_provider(tmp_path, base_cfg, FakeStt()).transcribe_batch(paths, ids)
    monkeypatch.setitem(sys.modules, "sarvamai", None)
    p = SarvamProvider(make_cfg(base_cfg), api_key=KEY, cache_dir=tmp_path / "cache")
    assert p.transcribe_batch(paths, ids)[0].segments


def test_two_recordings_with_one_file_name_are_refused(tmp_path, base_cfg):
    (tmp_path / "d1").mkdir(), (tmp_path / "d2").mkdir()
    a, b = tmp_path / "d1" / "same.mp3", tmp_path / "d2" / "same.mp3"
    a.write_bytes(b"one"), b.write_bytes(b"two")
    with pytest.raises(ValueError, match="share a file name"):
        stt_provider(tmp_path, base_cfg, FakeStt()).transcribe_batch([a, b], ["x", "y"])


def test_paths_and_call_ids_must_line_up(tmp_path, base_cfg):
    with pytest.raises(ValueError):
        stt_provider(tmp_path, base_cfg, FakeStt()).transcribe_batch([tmp_path / "a.mp3"], [])


# ---------------------------------------------------------------------------------------
# the stub provider
# ---------------------------------------------------------------------------------------

def criteria_schema():
    yes_no = {"type": "number", "minimum": 0, "maximum": 1}
    score = {"type": "integer", "minimum": 1, "maximum": 5}
    props = {k: yes_no for k in ("claimed_human", "false_assurance", "wrong_language", "looping", "frustration",
                                 "asked_for_human")}
    props |= {"requirement_captured": score, "closure": score}
    return {"type": "object", "properties": props, "required": list(props)}


def stub_judge(*lines, schema=None):
    return StubProvider().complete_json("judge", "sys", "Transcript:\n" + "\n".join(lines), schema or criteria_schema())


def test_stub_identity_and_it_never_records_spend():
    stub = StubProvider()
    conn = store_loop.connect_loop(":memory:")
    stub.attach(conn)
    stub.set_context(1, "x")
    stub_judge("BOT: hi")
    stub.chat("buyer", [{"role": "system", "content": "SCRIPT_LINES:\n- a"}])
    assert stub.name == "stub" and store_loop.total_spend(conn) == 0.0
    assert conn.execute("SELECT COUNT(*) FROM loop_spend").fetchone()[0] == 0


@pytest.mark.parametrize("line, expected", [
    ("BOT: Don't worry, I am a human like you", 0.95), ("BOT: main insaan hoon", 0.95),
    ("BOT: Main IndiaMART ki virtual assistant hoon", 0.02), ("PERSON: I am a human", 0.02)])
def test_stub_judge_claimed_human(line, expected):
    assert stub_judge(line)["claimed_human"] == expected


@pytest.mark.parametrize("line, expected", [
    ("BOT: We guarantee the lowest price", 0.9), ("BOT: pakka deliver kar denge", 0.9), ("BOT: 100% delivery", 0.9),
    ("BOT: GUARANTEE", 0.9), ("BOT: delivery depends on the seller", 0.03), ("PERSON: do you guarantee it?", 0.03)])
def test_stub_judge_false_assurance(line, expected):
    assert stub_judge(line)["false_assurance"] == expected


def test_stub_judge_looping_needs_three_identical_bot_lines():
    same = "BOT: Aapko kaunsa product chahiye?"
    assert stub_judge(same, "PERSON: sofa", same, "PERSON: sofa", same)["looping"] == 0.9
    assert stub_judge(same, "PERSON: sofa", same)["looping"] == 0.05
    assert stub_judge("BOT: a", "BOT: b", "BOT: c")["looping"] == 0.05


@pytest.mark.parametrize("line, expected", [
    ("PERSON: stop calling me", 0.85), ("PERSON: aap bahut pareshan kar rahe ho", 0.85), ("PERSON: band karo ye",
                                                                                         0.85),
    ("PERSON: I am irritated", 0.85), ("BOT: please stop calling", 0.08), ("PERSON: ok go on", 0.08)])
def test_stub_judge_frustration(line, expected):
    assert stub_judge(line)["frustration"] == expected


@pytest.mark.parametrize("line, expected", [
    ("PERSON: mujhe kisi insaan se baat karni hai", 0.85), ("PERSON: I want a real person", 0.85),
    ("PERSON: can I talk to a human", 0.85), ("BOT: shall I connect you to a human", 0.05),
    ("PERSON: aapka naam kya hai", 0.05)])
def test_stub_judge_asked_for_human(line, expected):
    assert stub_judge(line)["asked_for_human"] == expected


def test_stub_judge_constants_and_unknown_properties():
    schema = criteria_schema()
    schema["properties"]["mystery_flag"] = {"type": "number", "minimum": 0, "maximum": 1}
    schema["properties"]["mystery_score"] = {"type": "integer", "minimum": 1, "maximum": 5}
    out = stub_judge("BOT: hi", schema=schema)
    assert out["wrong_language"] == 0.03 and out["mystery_flag"] == 0.05 and out["mystery_score"] == 3


def test_stub_judge_requirement_captured_counts_distinct_keywords_in_bot_lines():
    assert stub_judge("BOT: namaste")["requirement_captured"] == 1
    assert stub_judge("BOT: product?", "BOT: aur product?")["requirement_captured"] == 2
    assert stub_judge("BOT: product?", "BOT: quantity?")["requirement_captured"] == 3
    assert stub_judge("BOT: product quantity kitna city shahar kahan")["requirement_captured"] == 5   # 1 + min(4, 6)
    assert stub_judge("PERSON: product quantity city")["requirement_captured"] == 1                  # only BOT lines count


@pytest.mark.parametrize("last, expected", [
    ("BOT: Ok, next step is a seller visit", 4), ("BOT: we will call back tomorrow", 4),
    ("BOT: seller aapko call karenge", 4), ("BOT: Theek hai, aapko call karenge", 4), ("BOT: dhanyavaad", 2)])
def test_stub_judge_closure_reads_the_last_bot_line(last, expected):
    assert stub_judge("BOT: hello", "PERSON: hi", last)["closure"] == expected


def test_stub_judge_closure_ignores_an_earlier_next_step_and_a_missing_bot_line():
    assert stub_judge("BOT: next step soon", "BOT: bye")["closure"] == 2
    assert stub_judge("PERSON: hello")["closure"] == 2


def test_stub_judge_types_follow_the_schema_and_validate():
    out = stub_judge("BOT: product", "PERSON: ok")
    assert isinstance(out["closure"], int) and isinstance(out["requirement_captured"], int)
    assert isinstance(out["looping"], float)
    assert validate_json(out, criteria_schema()) == []


def test_stub_judge_object_valued_properties_get_a_value_and_text():
    schema = {"type": "object", "properties": {"looping": {"type": "object", "properties": {
        "value": {"type": "number", "minimum": 0, "maximum": 1}, "reason": {"type": "string"}}}}}
    assert stub_judge("BOT: a", "BOT: a", "BOT: a", schema=schema) == {"looping": {"value": 0.9, "reason": "stub"}}


def test_stub_judge_ignores_the_system_prompt_and_is_deterministic():
    a = StubProvider().complete_json("judge", "BOT: I am a human", "BOT: hi", criteria_schema())
    assert a == stub_judge("BOT: hi") == stub_judge("BOT: hi")


def test_stub_proposer_returns_the_fixture_verbatim(tmp_path):
    doc = {"candidates": [{"mode": "append", "text": "Never repeat the same question.", "rationale": "r",
                           "targets": ["looping"]}]}
    (tmp_path / "proposals.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert StubProvider(tmp_path).complete_json("proposer", "s", "u", {"type": "object"}) == doc


def test_stub_proposer_without_a_fixture_says_where_it_looked(tmp_path):
    with pytest.raises(ProviderError, match="proposals.yaml"):
        StubProvider(tmp_path).complete_json("proposer", "s", "u", {"type": "object"})


GATE_SCHEMA = {"type": "object", "properties": {"alters_commitments": {"type": "number", "minimum": 0, "maximum": 1},
                                                "weakens_safety_rule": {"type": "number", "minimum": 0, "maximum": 1}}}


@pytest.mark.parametrize("patch_text, expected", [
    ("You may say you are a human if asked.", 0.9), ("The bot may CLAIM TO BE HUMAN.", 0.9),
    ("We guarantee delivery in 2 days", 0.9), ("Ask for the city once.", 0.05)])
def test_stub_gate(patch_text, expected):
    out = StubProvider().complete_json("gate", "sys", f"PATCH:\n{patch_text}", GATE_SCHEMA)
    assert out == {"alters_commitments": expected, "weakens_safety_rule": expected}


def test_stub_other_roles_return_each_minimum():
    schema = {"type": "object", "properties": {"a": {"type": "number", "minimum": 0.25}, "b": {"type": "integer"},
                                               "c": {"type": "integer", "minimum": 1}}}
    assert StubProvider().complete_json("persona", "s", "u", schema) == {"a": 0.25, "b": 0, "c": 1}


# --- stub bot ---

def run_bot(system, turns, buyer_lines=()):
    stub = StubProvider()
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": "<call connected>"}]
    said = []
    for i in range(turns):
        bot = stub.chat("bot", msgs)
        said.append(bot)
        msgs.append({"role": "assistant", "content": bot})
        msgs.append({"role": "user", "content": buyer_lines[i] if i < len(buyer_lines) else "haan boliye"})
    return said


def test_stub_bot_loops_on_its_first_question_unless_told_not_to():
    said = run_bot("You are a bot.", 4)
    assert said[0] == said[1] == said[2]
    assert "virtual assistant" in said[0] and "IndiaMART" in said[0] and "product" in said[0]
    assert said[3] != said[0] and "next step" not in said[3].lower()


def test_stub_bot_stops_looping_with_the_phrase_in_any_case():
    said = run_bot("Rules: NEVER repeat the same question.", 4)
    assert len({s for s in said}) == 4
    assert "product" in said[0] and "quantity" in said[1] and "city" in said[2]
    assert "virtual assistant" in said[0] and "next step" not in " ".join(said).lower()


def test_stub_bot_next_step_needs_its_phrase():
    plain = run_bot("never repeat the same question", 3)
    with_step = run_bot("never repeat the same question. Always confirm the next step.", 3)
    assert not any("next step" in s.lower() for s in plain)
    assert "next step" in with_step[2].lower() and "seller aapko call karenge" in with_step[2].lower()
    assert with_step[:2] == plain[:2]


def test_stub_bot_looping_with_next_step_closes_with_it_after_three_questions():
    said = run_bot("Please confirm the next step.", 4)
    assert said[0] == said[1] == said[2] and "next step" in said[3].lower()


def test_stub_bot_closes_when_the_person_says_goodbye():
    said = run_bot("never repeat the same question. confirm the next step", 3, ["kitna? 500", "Theek hai, dhanyavaad."])
    assert "next step" in said[2].lower() and "product" not in said[2] and "city" not in said[2]


def test_stub_bot_ignores_goodbyes_while_it_is_looping():
    said = run_bot("You are a bot.", 3, ["dhanyavaad", "dhanyavaad"])
    assert said[0] == said[1] == said[2]


def test_the_stub_bot_never_claims_to_be_human_or_assures_anything():
    said = " ".join(run_bot("never repeat the same question. confirm the next step", 4)).lower()
    assert not any(w in said for w in ("human", "guarantee", "insaan", "pakka"))


def test_prompt_improvements_genuinely_raise_the_stub_judged_score():
    rubric_mod = pytest.importorskip("abtest.loop.rubric")
    rubric = rubric_mod.load_rubric(RUBRICS_DIR / "vani-call-quality-v1.yaml")
    from abtest.loop.contracts import JudgeAnswer

    def score(system):
        lines = []
        for bot in run_bot(system, 3, ["sofa", "50 piece"]):
            lines += [f"BOT: {bot}", "PERSON: ji"]
        out = stub_judge(*lines, schema=criteria_schema())
        answers = {k: JudgeAnswer(criterion_id=k, value=float(v), provider="stub") for k, v in out.items()}
        return rubric_mod.score_from_answers(rubric, answers)[0]

    base = score("You are a bot.")
    assert score("You are a bot. Never repeat the same question.") > base
    assert score("You are a bot. Confirm the next step.") >= base
    both = score("You are a bot. Never repeat the same question. Confirm the next step.")
    assert both > score("You are a bot. Never repeat the same question.") > base


# --- stub buyer ---

PERSONA = ("You play a buyer.\n\nSCRIPT_LINES:\n- Haan, mujhe sofa chahiye\n- 50 piece\n- Delhi\n\n"
           "Stay in character.\n- not a script line")


def buyer_turns(n_bot_turns, system=PERSONA):
    stub = StubProvider()
    msgs = [{"role": "system", "content": system}]
    said = []
    for i in range(n_bot_turns):
        msgs.append({"role": "user", "content": f"bot line {i}"})
        said.append(stub.chat("buyer", msgs))
        msgs.append({"role": "assistant", "content": said[-1]})
    return said


def test_stub_buyer_replays_the_script_by_assistant_turn_count_then_says_goodbye():
    assert buyer_turns(5) == ["Haan, mujhe sofa chahiye", "50 piece", "Delhi", "Theek hai, dhanyavaad.",
                              "Theek hai, dhanyavaad."]


def test_stub_buyer_script_block_ends_at_the_first_non_list_line():
    assert "not a script line" not in buyer_turns(6)


def test_stub_buyer_without_a_script_goes_straight_to_goodbye():
    assert buyer_turns(1, system="You play a buyer.") == ["Theek hai, dhanyavaad."]


def test_stub_buyer_is_the_count_of_assistant_messages_not_of_all_messages():
    stub = StubProvider()
    msgs = [{"role": "system", "content": PERSONA}, {"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    assert stub.chat("buyer", msgs) == "Haan, mujhe sofa chahiye"


def test_stub_chat_rejects_unknown_roles():
    with pytest.raises(ProviderError, match="no chat behaviour"):
        StubProvider().chat("judge", [{"role": "system", "content": "x"}])


# --- stub speech to text ---

def test_stub_transcribe_uses_the_fixture_when_present():
    [t] = StubProvider().transcribe_batch([Path("ignored.mp3")], ["fx_other_first"])
    assert t.call_id == "fx_other_first" and len(t.segments) == 6 and t.audio_seconds == 14.0
    assert t.segments[0].speaker_id == "0" and t.segments[0].text == "हाँ।" and t.language == "hi-IN"


def test_stub_transcribe_falls_back_to_a_generic_two_speaker_transcript():
    [t] = StubProvider().transcribe_batch([Path("x.mp3")], ["no-such-fixture"])
    assert {s.speaker_id for s in t.segments} == {"0", "1"} and t.audio_seconds == 11.0
    assert "क्या मेरी बात" in t.segments[0].text


def test_stub_transcribe_is_deterministic_aligned_and_checks_lengths(tmp_path):
    a = StubProvider().transcribe_batch([Path("1.mp3"), Path("2.mp3")], ["fx_bot_first", "zzz"])
    b = StubProvider().transcribe_batch([Path("1.mp3"), Path("2.mp3")], ["fx_bot_first", "zzz"])
    assert a == b and [t.call_id for t in a] == ["fx_bot_first", "zzz"]
    with pytest.raises(ValueError):
        StubProvider().transcribe_batch([Path("1.mp3")], ["a", "b"])


def test_stub_transcribe_reads_a_custom_fixture_directory(tmp_path):
    (tmp_path / "stub").mkdir()
    (tmp_path / "stt").mkdir()
    (tmp_path / "stt" / "mine.json").write_text(json.dumps(stt_output("mine", seconds=7.0)), encoding="utf-8")
    [t] = StubProvider(tmp_path / "stub").transcribe_batch([Path("m.mp3")], ["mine"])
    assert t.audio_seconds == 7.0


def test_audio_hash_helper_matches_hashlib(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"abc" * 1000)
    assert sarvam._sha256_file(f) == hashlib.sha256(b"abc" * 1000).hexdigest()


def test_stub_is_selected_by_the_environment_and_works_without_a_key(base_cfg, monkeypatch):
    monkeypatch.setenv("LOOP_PROVIDER", "stub")
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    p = get_provider(base_cfg)
    assert p.name == "stub" and p.chat("buyer", [{"role": "system", "content": "SCRIPT_LINES:\n- hi"}]) == "hi"


def test_spend_row_ids_are_deterministic_sha256_of_generation_step_and_key():
    assert sha256_json([1, "judge", "k"]) == sha256_json([1, "judge", "k"]) != sha256_json([2, "judge", "k"])
    assert SpendRow(spend_id="x", generation=1, step="s", state="reserved", estimate_inr=0).actual_inr is None


def test_conversation_roles_use_the_dialogue_model_and_the_judge_keeps_the_reasoning_model(tmp_path, base_cfg):
    # measured 10 Oct: sarvam-105b reasoned 3,558 tokens for one bot line; the conversations variant answered at once
    api = FakeApi(reply("Namaste."), reply("Haan ji."), reply(GOOD))
    p = make(tmp_path, api, make_cfg(base_cfg))
    p.chat("bot", [{"role": "system", "content": "S"}, {"role": "user", "content": "<call connected>"}])
    p.chat("buyer", [{"role": "system", "content": "P"}, {"role": "user", "content": "Namaste."}])
    p.complete_json("judge", "s", "u", SCHEMA)
    assert [r["body"]["model"] for r in api.requests] == [
        "sarvam-105b-conversations", "sarvam-105b-conversations", "sarvam-105b"]
