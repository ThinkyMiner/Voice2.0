"""loop/telephony: the fake surface is deterministic, and S1 refuses to place a real call unless the
config and the dogfood file allow it, builds the Instant Outbound request with the webhook metadata,
and parses the post-call webhook (PLAN_LOOP §L2.4, §L9 item 6). httpx.MockTransport only: no network."""

import json

import httpx
import pytest
import yaml

from abtest.loop.contracts import DEFAULT_LOOP_CONFIG, LoopConfig
from abtest.loop.telephony import (CallRef, CallRequest, CallResult, FakeSurface, LiveCallsDisabled, Surface,
                                   get_surface)
from abtest.loop.telephony.s1_sarvam import (ANALYTICS_PATH, CREATE_PATH, DEFAULT_BASE_URL, DOGFOOD_FILE, S1Error,
                                             S1Surface)

KEY = "sk-test-secret-key-123"
PHONE = "+919999900001"


def _cfg(surface="s1", calls=5) -> LoopConfig:
    raw = yaml.safe_load(DEFAULT_LOOP_CONFIG.read_text())
    raw["live"]["surface"] = surface
    raw["caps"]["max_live_calls_per_day"] = calls
    return LoopConfig.model_validate(raw)


def _req(lead="L1", variant="B", phone=PHONE):
    return CallRequest(experiment_id="exp-1", lead_id=lead, variant=variant, prompt_hash="ph1",
                       rendered_prompt="You are a helpful assistant. SECRET PROMPT BODY", phone=phone)


@pytest.fixture
def dogfood(tmp_path):
    path = tmp_path / "dogfood.yaml"
    path.write_text("callers: []\n")
    return path


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", KEY)


def _surface(handler, dogfood, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return S1Surface(_cfg(), "https://sarvam.test", "/create", "/rec/{interaction_id}", client=client,
                     dogfood_path=dogfood, **kw)


# --- models --------------------------------------------------------------------------------------

def test_models_hide_the_phone_and_the_prompt_in_repr():
    r = repr(_req())
    assert PHONE not in r and "SECRET PROMPT BODY" not in r
    with pytest.raises(Exception):
        _req().phone = "x"          # frozen
    with pytest.raises(ValueError):
        CallRequest(experiment_id="e", lead_id="l", variant="C", prompt_hash="h", rendered_prompt="p", phone="1")
    with pytest.raises(ValueError):
        CallResult(call_ref="x", status="exploded")


def test_surfaces_satisfy_the_protocol(dogfood, key):
    assert isinstance(FakeSurface(), Surface)
    assert isinstance(_surface(lambda r: httpx.Response(200), dogfood), Surface)


# --- fake surface --------------------------------------------------------------------------------

def test_fake_results_are_deterministic_and_attempts_get_new_refs():
    a, b = FakeSurface(), FakeSurface()
    ra, rb = a.start_call(_req("L9")), b.start_call(_req("L9"))
    assert ra == rb and ra.surface == "fake" and ra.call_ref.startswith("fake-")
    assert a.fetch_result(ra) == b.fetch_result(rb)
    assert a.fetch_result(ra).call_ref == ra.call_ref
    again = a.start_call(_req("L9"))                    # a retry of the same lead
    assert again.call_ref != ra.call_ref
    assert a.requests == [_req("L9"), _req("L9")]


def test_fake_outcomes_vary_by_lead_and_look_like_a_dialer():
    s = FakeSurface()
    results = [s.fetch_result(s.start_call(_req(f"lead{i}"))) for i in range(200)]
    statuses = {r.status for r in results}
    assert statuses == {"connected", "no_answer", "busy", "failed"}
    connected = [r for r in results if r.status == "connected"]
    assert 0.65 < len(connected) / len(results) < 0.95
    assert all(20 <= r.duration_s <= 180 and r.disposition and r.transcript_text for r in connected)
    assert all(r.disposition is None and r.duration_s is None for r in results if r.status != "connected")
    assert {"BL Enriched", "Abruptly Disconnected Call"} <= {r.disposition for r in connected}


def test_fake_scripted_results_and_recording_path():
    scripted = CallResult(call_ref="ignored", status="connected", duration_s=42.0, disposition="BL Approved")
    s = FakeSurface({"L1": scripted}, recording_path="/tmp/x.mp3")
    ref = s.start_call(_req("L1"))
    got = s.fetch_result(ref)
    assert got.call_ref == ref.call_ref and got.disposition == "BL Approved" and got.duration_s == 42.0
    other = s.fetch_result(s.start_call(_req("L2")))
    assert other.status != "in_progress"
    with pytest.raises(KeyError, match="unknown fake call"):
        s.fetch_result(CallRef(call_ref="nope", surface="fake"))


# --- S1 refusal ----------------------------------------------------------------------------------

def test_s1_refuses_unless_surface_caps_and_dogfood_file_allow(dogfood):
    ok = dict(dogfood_path=dogfood)
    S1Surface(_cfg(), **ok)                                          # all three hold: builds
    with pytest.raises(LiveCallsDisabled, match="not 's1'"):
        S1Surface(_cfg(surface="fake"), **ok)
    with pytest.raises(LiveCallsDisabled, match="max_live_calls_per_day"):
        S1Surface(_cfg(calls=0), **ok)
    with pytest.raises(LiveCallsDisabled, match="dogfood"):
        S1Surface(_cfg(), dogfood_path=dogfood.parent / "missing.yaml")


def test_s1_refuses_by_default_in_the_shipped_config():
    """configs_loop/default.yaml: live.surface is fake and max_live_calls_per_day is 0."""
    shipped = LoopConfig.model_validate(yaml.safe_load(DEFAULT_LOOP_CONFIG.read_text()))
    with pytest.raises(LiveCallsDisabled):
        S1Surface(shipped)
    with pytest.raises(LiveCallsDisabled):
        get_surface(shipped.model_copy(update={"live": shipped.live.model_copy(update={"surface": "s1"})}))
    assert isinstance(get_surface(shipped), FakeSurface)             # fake is what the shipped config picks
    assert not DOGFOOD_FILE.exists()                                 # the ignored phone-number file is not here


def test_s1_checks_again_before_each_call(dogfood, key):
    s = _surface(lambda r: pytest.fail("no request may be sent"), dogfood)
    dogfood.unlink()                                                 # the file vanishes after construction
    with pytest.raises(LiveCallsDisabled):
        s.start_call(_req())


def test_s2_is_not_built():
    with pytest.raises(LiveCallsDisabled, match="s2"):
        get_surface(_cfg(surface="s2"))


# --- S1 request ----------------------------------------------------------------------------------

def test_start_call_sends_the_webhook_metadata_and_never_leaks_the_key(dogfood, key):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"attempt_id": "att-1"})

    s = _surface(handler, dogfood)
    ref = s.start_call(_req())
    assert ref.surface == "s1" and ref.call_ref.startswith("s1-")
    (request,) = seen
    assert str(request.url) == "https://sarvam.test/create" and request.method == "POST"
    assert request.headers["api-subscription-key"] == KEY
    body = json.loads(request.content)
    meta = body["webhook_config"]["metadata"]
    assert meta == {"experiment_id": "exp-1", "lead_id": "L1", "call_id": ref.call_ref, "variant": "B",
                    "prompt_hash": "ph1"}
    assert body["to_number"] == PHONE and body["prompt"].startswith("You are")
    assert KEY not in json.dumps(body) and KEY not in repr(s.__dict__) and KEY not in repr(ref)


def test_call_ids_are_unique_per_attempt_and_deterministic(dogfood, key):
    s1 = _surface(lambda r: httpx.Response(200), dogfood)
    s2 = _surface(lambda r: httpx.Response(200), dogfood)
    first, retry = s1.start_call(_req()), s1.start_call(_req())
    assert first.call_ref != retry.call_ref
    assert s2.start_call(_req()) == first
    assert s1.start_call(_req(variant="A")).call_ref != first.call_ref


def test_defaults_are_the_documented_placeholders(dogfood):
    s = S1Surface(_cfg(), dogfood_path=dogfood)
    assert (s._base_url, s._create_path, s._analytics_path) == (DEFAULT_BASE_URL, CREATE_PATH, ANALYTICS_PATH)
    assert "{interaction_id}" in ANALYTICS_PATH


def test_start_call_errors_do_not_echo_the_phone_or_the_key(dogfood, key):
    s = _surface(lambda r: httpx.Response(422, json={"detail": f"bad number {PHONE} key {KEY}"}), dogfood)
    with pytest.raises(S1Error) as e:
        s.start_call(_req())
    assert "422" in str(e.value) and PHONE not in str(e.value) and KEY not in str(e.value)


def test_start_call_needs_a_key_and_a_plausible_number(dogfood, monkeypatch):
    s = _surface(lambda r: pytest.fail("no request may be sent"), dogfood)
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SARVAM_API_KEY is not set"):
        s.start_call(_req())
    monkeypatch.setenv("SARVAM_API_KEY", KEY)
    with pytest.raises(ValueError) as e:
        s.start_call(_req(phone="call me maybe"))
    assert "call me maybe" not in str(e.value)


# --- S1 webhook ----------------------------------------------------------------------------------

WEBHOOK = {
    "attempt_id": "att-9", "status": "connected", "duration": 73.5, "interaction_id": "int-77",
    "metadata": {"experiment_id": "exp-1", "lead_id": "L1", "call_id": "s1-abc", "variant": "B",
                 "prompt_hash": "ph1"},
    "final_agent_variables": {"disposition": "BL Enriched", "other": "x"},
    "interaction_transcript": [
        {"role": "agent", "en_text": "Hello, which product?"}, {"role": "user", "en_text": "500 pieces, Pune"},
        {"role": "system", "en_text": "ignored"}, {"role": "user"}],
}


def test_parse_webhook_maps_the_payload(dogfood):
    s = S1Surface(_cfg(), dogfood_path=dogfood)
    r = s.parse_webhook(WEBHOOK)
    assert r == CallResult(call_ref="s1-abc", status="connected", duration_s=73.5, disposition="BL Enriched",
                           transcript_text="BOT: Hello, which product?\nPERSON: 500 pieces, Pune")
    assert s.fetch_result(CallRef(call_ref="s1-abc", surface="s1")) == r


def test_the_disposition_field_comes_from_the_config(dogfood):
    raw = yaml.safe_load(DEFAULT_LOOP_CONFIG.read_text())
    raw["live"].update(surface="s1", disposition_field="end_outcome")
    raw["caps"]["max_live_calls_per_day"] = 1
    s = S1Surface(LoopConfig.model_validate(raw), dogfood_path=dogfood)
    payload = {**WEBHOOK, "final_agent_variables": {"disposition": "wrong", "end_outcome": "BL Approved"}}
    assert s.parse_webhook(payload).disposition == "BL Approved"
    assert s.parse_webhook({**WEBHOOK, "final_agent_variables": {"disposition": "only this"}}).disposition is None


@pytest.mark.parametrize("raw,want", [("connected", "connected"), ("Answered", "connected"),
                                      ("no_answer", "no_answer"), ("no-answer", "no_answer"),
                                      ("BUSY", "busy"), ("failed", "failed"), ("ringing", "in_progress")])
def test_parse_webhook_normalises_statuses(dogfood, raw, want):
    s = S1Surface(_cfg(), dogfood_path=dogfood)
    assert s.parse_webhook({**WEBHOOK, "status": raw}).status == want


def test_parse_webhook_rejects_what_it_cannot_trust(dogfood):
    s = S1Surface(_cfg(), dogfood_path=dogfood)
    with pytest.raises(ValueError, match="unrecognised S1 call status"):
        s.parse_webhook({**WEBHOOK, "status": "exploded"})
    with pytest.raises(ValueError, match="neither metadata.call_id nor attempt_id"):
        s.parse_webhook({"status": "connected"})
    with pytest.raises(ValueError, match="negative"):
        s.parse_webhook({**WEBHOOK, "duration": -1})


def test_parse_webhook_tolerates_a_bare_failed_call(dogfood):
    s = S1Surface(_cfg(), dogfood_path=dogfood)
    r = s.parse_webhook({"attempt_id": "att-2", "status": "no_answer"})
    assert r == CallResult(call_ref="att-2", status="no_answer")      # no duration, disposition or transcript


def test_fetch_result_is_in_progress_until_the_webhook_arrives(dogfood):
    s = S1Surface(_cfg(), dogfood_path=dogfood)
    ref = CallRef(call_ref="s1-later", surface="s1")
    assert s.fetch_result(ref) == CallResult(call_ref="s1-later", status="in_progress")


# --- S1 recording --------------------------------------------------------------------------------

def test_fetch_recording_downloads_by_interaction_id(dogfood, key, tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"ID3-fake-mp3-bytes")

    s = _surface(handler, dogfood)
    ref = CallRef(call_ref="s1-abc", surface="s1")
    assert s.fetch_recording(ref, tmp_path) is None                  # no webhook yet: nothing to fetch
    s.parse_webhook(WEBHOOK)
    path = s.fetch_recording(ref, tmp_path / "rec")
    assert path.read_bytes() == b"ID3-fake-mp3-bytes" and path.name == "s1-abc.mp3"
    assert str(seen[0].url) == "https://sarvam.test/rec/int-77" and seen[0].headers["api-subscription-key"] == KEY
    assert s.fetch_result(ref).recording_path == str(path)


def test_fetch_recording_errors_carry_only_the_status(dogfood, key, tmp_path):
    s = _surface(lambda r: httpx.Response(404, text="no such recording for +919999900001"), dogfood)
    s.parse_webhook(WEBHOOK)
    with pytest.raises(S1Error) as e:
        s.fetch_recording(CallRef(call_ref="s1-abc", surface="s1"), tmp_path)
    assert "404" in str(e.value) and "+91" not in str(e.value)
