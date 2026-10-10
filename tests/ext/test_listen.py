"""Listen: the CSV mappings, transcription against a stubbed model (validation and the retry), the
real-audio refusal, and atomic downloads through a fake httpx. Every fixture here is invented."""

import csv
import json

import httpx
import pytest

from abtest.ext import listen, llm
from abtest.ext.models import RECORDINGS_DIR, TRANSCRIPTS_DIR, Transcript, save

MP3 = b"ID3\x04\x00" + bytes(64)
URL = "https://invented-recs.s3.amazonaws.com/{}.mp3"
GOOD = {"language": "hi", "turns": [
    {"speaker": "bot", "start_s": 0.0, "end_s": 3.1, "text": "Namaste, main Vani bol rahi hoon IndiaMART se."},
    {"speaker": "buyer", "start_s": 3.4, "end_s": 6.0, "text": "Ji, 200 bedsheet chahiye. Number 98765 43210 hai."},
    {"speaker": "bot", "start_s": 6.2, "end_s": 7.5, "text": " Aapko kitni quantity chahiye? "},
]}


def _csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([header, *rows])
    return path


@pytest.fixture
def audio(tmp_path):
    p = tmp_path / "fx.mp3"
    p.write_bytes(MP3)
    return p


@pytest.fixture
def stub():
    """Replies in order (the last one repeats); records every request."""
    seen = []

    def install(*replies):
        def answer(model, messages, schema):
            seen.append((model, messages, schema))
            reply = replies[min(len(seen), len(replies)) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply
        llm.set_stub(answer)
        return seen
    return install


# --- CSV mappings ------------------------------------------------------------------------

def test_production_calls_map_the_sample_and_drop_glid(tmp_path, monkeypatch):
    p = _csv(tmp_path / "calls.csv",
             ["glid", "call_id", "bot", "hotlead_type", "call_status", "duration_sec", "call_start_ist", "recording_url"],
             [["777001", "900001", "personalised_vani", "PIM", "Answered", "42", "2026-10-06 09:00:00", URL.format("AB12CD34E")],
              ["777002", "900002", "base_vani", "UA", "Answered", "", "2026-10-06 09:01:00", " " + URL.format("ZZ99YY88X")]])
    monkeypatch.setattr(listen, "CALLS_CSV", p)
    calls = listen.production_calls()
    assert calls[0] == {"call_id": "900001", "recording_url": URL.format("AB12CD34E"), "duration_s": 42.0,
                        "meta": {"bot": "personalised_vani", "hotlead_type": "PIM", "call_start_ist": "2026-10-06 09:00:00"}}
    assert calls[1]["duration_s"] is None and calls[1]["recording_url"] == URL.format("ZZ99YY88X")
    assert "777001" not in json.dumps(calls)


def test_audited_calls_keep_squadstack_inbound_s3_rows_with_human_grades(tmp_path, monkeypatch):
    head = ["Vendor", "fk\\_gl\\_module\\_id", "Call Recording", "Outcome Accuracy (Intent)",
            "Outcome Accuracy Remarks", "Quantity  Accuracy\n(Numeric)", "Product Change/ No Product Accuracy",
            "Product Change/ No Product Accuracy Reamarks", "Additional Details Accuracy",
            "Additional\\_details remarks", "Overall Quality"]
    rows = [["SQUADSTACK", "INBOUND", f" {URL.format('AAA111BBB')} ", "Pass", "", "Non-Fatal",
             "Fatal- Product Change Case", "invented remark", "NA", "", "Fatal"],
            ["SARVAM", "INBOUND", "https://example.invalid/r?id=1", "Fatal", "", "", "", "", "", "", "Fatal"],
            ["SQUADSTACK", "OUTBOUND", URL.format("CCC222DDD"), "Pass", "", "", "", "", "", "", "Pass"],
            ["SQUADSTACK", "INBOUND", "", "Pass", "", "", "", "", "", "", "Pass"],
            ["SQUADSTACK", "INBOUND", URL.format("EEE333FFF"), "", "", " pass ", "", "", "",
             "another invented remark", "Non-Fatal"],
            ["SQUADSTACK", "INBOUND", URL.format("AAA111BBB"), "Fatal", "", "", "", "", "", "", "Fatal"],
            ["SQUADSTACK", "INBOUND"]]  # a short row, as the real export has
    monkeypatch.setattr(listen, "AUDIT_CSV", _csv(tmp_path / "audit.csv", head, rows))
    calls = listen.audited_calls()
    assert [c["call_id"] for c in calls] == ["AAA111BBB", "EEE333FFF"]   # file order, first row per call
    first, second = calls[0]["human"], calls[1]["human"]
    assert calls[0]["recording_url"] == URL.format("AAA111BBB")
    assert first["overall"] == "fatal" and second["overall"] == "non_fatal"
    assert set(first["grades"]) == set(listen.QA_COLUMNS)              # every checklist column, na included
    assert first["grades"]["Outcome Accuracy (Intent)"] == "pass"
    assert first["grades"]["Quantity Accuracy (Numeric)"] == "non_fatal"
    assert first["grades"]["Product Change/ No Product Accuracy"] == "fatal"
    assert first["grades"]["Additional Details Accuracy"] == "na" and first["grades"]["Looping Behavior"] == "na"
    assert first["remarks"] == {"Product Change/ No Product Accuracy": "invented remark"}
    assert second["grades"]["Outcome Accuracy (Intent)"] == "na"
    assert second["grades"]["Quantity Accuracy (Numeric)"] == "pass"
    assert second["remarks"] == {"Additional Details Accuracy": "another invented remark"}


# --- transcribe --------------------------------------------------------------------------

def test_transcribe_builds_saves_and_then_skips(audio, stub):
    seen = stub(GOOD)
    t = listen.transcribe(audio, "fx001", "fixture", model="test/audio", duration_s=7.5, vendor="SQUADSTACK",
                          meta={"bot": "base_vani", "hotlead_type": "PIM", "call_start_ist": "2026-10-06 09:00:00"})
    assert t.provider == "openrouter:test/audio" and t.language == "hi" and t.duration_s == 7.5
    assert t.meta == {"bot": "base_vani", "hotlead_type": "PIM"}
    assert [x.speaker for x in t.turns] == ["bot", "buyer", "bot"]
    assert t.turns[1].text == "Ji, 200 bedsheet chahiye. Number [number] hai."    # quantity kept, phone masked
    assert t.turns[2].text == "Aapko kitni quantity chahiye?" and t.turns[2].start_s == 6.2
    model, messages, schema = seen[0]
    parts = messages[0]["content"]
    assert model == "test/audio" and schema == listen.SCHEMA
    assert parts[1]["type"] == "input_audio" and "[number]" in parts[0]["text"]
    assert Transcript.model_validate_json((TRANSCRIPTS_DIR / "fx001.json").read_text()) == t

    stub(AssertionError("an already transcribed call is never re-sent"))
    assert listen.transcribe(audio, "fx001", "fixture") == t


def test_transcribe_defaults_to_the_transcribe_role_model(audio, stub, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_TRANSCRIBE", "test/default")
    seen = stub(GOOD)
    assert listen.transcribe(audio, "fx002", "fixture").provider == "openrouter:test/default"
    assert seen[0][0] == "test/default"


@pytest.mark.parametrize("bad", [
    {"language": "hi", "turns": []},
    {"language": "hi", "turns": [{"speaker": "agent", "start_s": 0, "end_s": 1, "text": "hello"}]},
    {"language": "hi", "turns": [{"speaker": "bot", "start_s": 0, "end_s": 1, "text": "  "}]},
    {"language": "hi", "turns": [{"speaker": "bot", "start_s": 4, "end_s": 2, "text": "hello"}]},
    {"language": "hi", "turns": [{"speaker": "bot", "start_s": 59, "end_s": 60, "text": "a"},
                                 {"speaker": "buyer", "start_s": 1.05, "end_s": 1.1, "text": "b"}]},  # "1:05"
    {"language": "hi", "turns": [{"speaker": "bot", "start_s": "0:01", "end_s": 1, "text": "hello"}]},
    {"language": "hi", "turns": [{"speaker": "bot", "start_s": -0.5, "end_s": -0.1, "text": "hello"}]},
    {"language": "hi", "turns": [{"speaker": "bot", "start_s": 0, "end_s": float("nan"), "text": "hello"}]},
    {"language": "hi", "turns": [{"speaker": "bot", "start_s": 0, "end_s": float("inf"), "text": "hello"}]},
    {"language": "hi", "turns": [{"speaker": "bot", "start_s": 0, "end_s": 2, "text": "नमस्ते, मैं वाणी"}]},
    {"language": "te", "turns": [{"speaker": "buyer", "start_s": 0, "end_s": 2, "text": "నమస్కారం"}]},
    "Sorry, I cannot transcribe this audio.",
    ValueError("Expecting value: line 1 column 1"),   # what llm.chat raises on a non-JSON reply
])
def test_a_bad_reply_gets_one_stricter_retry(audio, stub, bad):
    seen = stub(bad, GOOD)
    t = listen.transcribe(audio, "fx003", "fixture", model="test/audio")
    assert len(t.turns) == 3 and len(seen) == 2
    first, retry = (s[1][0]["content"][0]["text"] for s in seen)
    assert "rejected" not in first and "Your previous reply was rejected" in retry


def test_two_bad_replies_raise_and_save_nothing(audio, stub):
    seen = stub({"language": "hi", "turns": []})
    with pytest.raises(ValueError, match="no usable transcript"):
        listen.transcribe(audio, "fx004", "fixture", model="test/audio")
    assert len(seen) == 2 and not (TRANSCRIPTS_DIR / "fx004.json").exists()


@pytest.mark.parametrize("spoken", ["98765 43210", "+91 98765 43210", "+91 (98765) 43210", "98765.43210",
                                    "98765\u00a043210", "9876543210"])
def test_phone_numbers_are_masked_in_any_written_form(spoken):
    assert listen.PHONE.sub("[number]", f"Mera number {spoken} hai, 200 piece, 2.5 kg") == \
        "Mera number [number] hai, 200 piece, 2.5 kg"


def test_an_unknown_language_is_dropped_not_retried(audio, stub):
    seen = stub({**GOOD, "language": "bogus"})
    assert listen.transcribe(audio, "fx005", "fixture", model="m").language is None and len(seen) == 1


def test_crosstalk_and_missing_times_are_accepted():
    assert listen._problem({"turns": [{"speaker": "bot", "start_s": 5.0, "end_s": 9.0, "text": "a"},
                                      {"speaker": "buyer", "start_s": 4.5, "end_s": 6.0, "text": "b"},
                                      {"speaker": "bot", "text": "c"}]}) is None


def test_unsafe_call_ids_are_refused(audio):
    with pytest.raises(ValueError, match="unsafe"):
        listen.transcribe(audio, "../escape", "fixture")
    with pytest.raises(ValueError, match="unsafe"):
        listen.download("a/b", URL.format("x"))


def test_load_transcripts_filters_by_source(audio, stub):
    stub(GOOD)
    listen.transcribe(audio, "p1", "production", model="m")
    listen.transcribe(audio, "a1", "audited", model="m")
    assert [t.call_id for t in listen.load_transcripts()] == ["a1", "p1"]
    assert [t.call_id for t in listen.load_transcripts("audited")] == ["a1"]


# --- download ----------------------------------------------------------------------------

@pytest.fixture
def fake_get(monkeypatch):
    """httpx.get answering from a list (Response or exception per attempt); records each URL."""
    calls = []

    def install(*answers):
        def get(url, **kw):
            calls.append(url)
            a = answers[len(calls) - 1]
            if isinstance(a, Exception):
                raise a
            return a
        monkeypatch.setattr(httpx, "get", get)
        monkeypatch.setattr(listen.time, "sleep", lambda s: None)
        return calls
    return install


def test_download_writes_atomically_and_skips_present_files(fake_get):
    calls = fake_get(httpx.Response(200, headers={"content-type": "binary/octet-stream"}, content=MP3))
    path = listen.download("AB12CD34E", URL.format("AB12CD34E"))
    assert path == RECORDINGS_DIR / "AB12CD34E.mp3" and path.read_bytes() == MP3
    assert sorted(p.name for p in RECORDINGS_DIR.iterdir()) == ["AB12CD34E.mp3"]   # no temp file left
    assert listen.download("AB12CD34E", URL.format("AB12CD34E")) == path and len(calls) == 1


def test_download_retries_transient_errors(fake_get):
    calls = fake_get(httpx.ConnectTimeout("slow"), httpx.Response(503),
                     httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"\xff\xfb\x90\x00"))
    assert listen.download("R1", URL.format("R1")).read_bytes() == b"\xff\xfb\x90\x00"
    assert len(calls) == 3


@pytest.mark.parametrize("answer", [
    httpx.Response(200, headers={"content-type": "application/xml"}, content=b"<Error>AccessDenied</Error>"),
    httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>login</html>"),
    httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b""),
    httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"<html>login</html>"),
    httpx.Response(200, headers={"content-type": "audio/wav"}, content=b"RIFF\x24\x00\x00\x00WAVE"),
    httpx.Response(403, content=b"<Error>Expired</Error>"),
])
def test_download_rejects_what_is_not_audio_and_leaves_nothing(fake_get, answer):
    calls = fake_get(answer)
    with pytest.raises(RuntimeError, match="recording R2"):
        listen.download("R2", URL.format("R2"))
    assert len(calls) == 1 and not (RECORDINGS_DIR.exists() and any(RECORDINGS_DIR.iterdir()))


def test_download_gives_up_after_the_last_try(fake_get):
    calls = fake_get(*[httpx.ReadTimeout("slow")] * 3)
    with pytest.raises(httpx.ReadTimeout):
        listen.download("R3", URL.format("R3"))
    assert len(calls) == 3


# --- CLI ---------------------------------------------------------------------------------

def test_cli_refuses_without_the_real_audio_flag(monkeypatch, capsys):
    def never(*a, **k):
        raise AssertionError("nothing may be read, downloaded or sent without the flag")
    for name in ("production_calls", "audited_calls", "download", "transcribe"):
        monkeypatch.setattr(listen, name, never)
    assert listen.main(["--source", "production"]) == 2
    assert "--yes-send-real-audio" in capsys.readouterr().err


def test_cli_downloads_transcribes_and_reports(monkeypatch, capsys, audio, stub):
    calls = [{"call_id": f"9000{i}", "recording_url": URL.format(f"REC{i}"), "duration_s": 30.0,
              "meta": {"bot": "base_vani", "hotlead_type": "PIM", "call_start_ist": "2026-10-06 09:00:00"}}
             for i in range(4)]
    monkeypatch.setattr(listen, "production_calls", lambda: calls)

    def download(call_id, url):
        if call_id == "90003":
            raise RuntimeError("recording 90003: HTTP 403")
        return audio
    monkeypatch.setattr(listen, "download", download)
    stub(GOOD)
    save(TRANSCRIPTS_DIR / "90000.json", Transcript(call_id="90000", source="production", turns=()))

    rc = listen.main(["--source", "production", "--ids", "90000, 90001,90003", "--workers", "2",
                      "--model", "test/audio", "--yes-send-real-audio"])
    out = capsys.readouterr().out
    assert rc == 1 and "3 calls, 1 already transcribed, 2 to do" in out
    assert "90003: FAILED RuntimeError" in out and "1 transcribed, 1 failed" in out
    t = listen.load_transcripts("production")[1]
    assert t.call_id == "90001" and t.vendor == "SQUADSTACK" and t.recording_url == URL.format("REC1")
    assert t.meta == {"bot": "base_vani", "hotlead_type": "PIM"} and t.duration_s == 30.0
