"""Download and transcribe: retries, skips, chunking, role labelling, resume (PLAN_LOOP §L2.3, §L4 L1).
No network: HTTP goes through httpx.MockTransport and speech-to-text through the stub provider."""

import json
import logging
import threading
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from abtest.loop import transcribe as tr
from abtest.loop.contracts import Transcript
from abtest.loop.sarvam import ProviderError, SpendCapExceeded, StubProvider
from abtest.loop.transcribe import download, transcribe

SECRET_URL = "https://recordings.example/rec/{}.mp3?Signature=SECRET-SIG"


# ---------------------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------------------

class Server:
    """An httpx handler: `plan[call_id]` is a list of statuses/exceptions served in order (then 200)."""

    def __init__(self, plan=None, body=b"mp3-bytes"):
        self.plan, self.body, self.hits, self._lock = plan or {}, body, [], threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        cid = request.url.path.rsplit("/", 1)[-1].removesuffix(".mp3")
        with self._lock:
            self.hits.append(cid)
            steps = self.plan.get(cid, [])
            step = steps.pop(0) if steps else 200
        if isinstance(step, Exception):
            raise step
        if step == 200:
            return httpx.Response(200, content=self.body)
        return httpx.Response(step, text="error body")

    def count(self, cid):
        return self.hits.count(cid)


@pytest.fixture
def sleeps(monkeypatch):
    seen = []
    monkeypatch.setattr(tr, "_sleep", seen.append)
    return seen


@pytest.fixture
def serve(monkeypatch):
    def install(server):
        monkeypatch.setattr(tr, "_make_client",
                            lambda: httpx.Client(transport=httpx.MockTransport(server), follow_redirects=True))
        return server
    return install


def frame(ids, durations=None, urls=True):
    return pd.DataFrame({"call_id": list(ids), "lead_id": [f"L{i}" for i in ids],
                         "duration_s": durations or [30.0] * len(ids),
                         "recording_url": [SECRET_URL.format(i) if urls else None for i in ids]})


def test_download_writes_call_id_mp3_and_adds_columns(tmp_path, serve, sleeps):
    server = serve(Server())
    out = download(frame(["101", "102"]), tmp_path / "rec")
    assert [Path(p).name for p in out["audio_path"]] == ["101.mp3", "102.mp3"]
    assert out["download_error"].isna().all()
    assert (tmp_path / "rec" / "101.mp3").read_bytes() == b"mp3-bytes"
    assert sorted(server.hits) == ["101", "102"] and not list((tmp_path / "rec").glob("*.part"))


def test_download_does_not_modify_the_input_and_keeps_its_index(tmp_path, serve, sleeps):
    serve(Server())
    calls = frame(["1", "2", "3"]).set_index(pd.Index([10, 20, 30]))
    out = download(calls, tmp_path)
    assert "audio_path" not in calls.columns
    assert list(out.index) == [10, 20, 30] and len(out) == 3


def test_download_skips_files_that_are_already_present(tmp_path, serve, sleeps):
    server = serve(Server())
    (tmp_path / "101.mp3").write_bytes(b"already here")
    out = download(frame(["101", "102"]), tmp_path)
    assert server.hits == ["102"]
    assert (tmp_path / "101.mp3").read_bytes() == b"already here"
    assert out["audio_path"].iloc[0] == str(tmp_path / "101.mp3")


def test_an_empty_leftover_file_is_downloaded_again(tmp_path, serve, sleeps):
    server = serve(Server())
    (tmp_path / "101.mp3").write_bytes(b"")
    download(frame(["101"]), tmp_path)
    assert server.hits == ["101"] and (tmp_path / "101.mp3").read_bytes() == b"mp3-bytes"


def test_download_retries_a_server_error_then_succeeds(tmp_path, serve, sleeps):
    server = serve(Server({"101": [500]}))
    out = download(frame(["101"]), tmp_path)
    assert server.count("101") == 2 and sleeps == [1]
    assert out["audio_path"].iloc[0] is not None and pd.isna(out["download_error"].iloc[0])


def test_download_gives_up_after_three_tries_with_a_readable_error(tmp_path, serve, sleeps):
    server = serve(Server({"101": [503, 503, 503, 503]}))
    out = download(frame(["101"]), tmp_path)
    assert server.count("101") == 3 and sleeps == [1, 2]
    assert out["audio_path"].iloc[0] is None
    assert out["download_error"].iloc[0] == "HTTP 503 after 3 tries"
    assert not (tmp_path / "101.mp3").exists()


@pytest.mark.parametrize("status", [403, 404])
def test_a_client_error_is_not_retried(tmp_path, serve, sleeps, status):
    server = serve(Server({"101": [status]}))
    out = download(frame(["101"]), tmp_path)
    assert server.count("101") == 1 and sleeps == []
    assert out["audio_path"].iloc[0] is None and out["download_error"].iloc[0] == f"HTTP {status}"


def test_transport_errors_are_retried(tmp_path, serve, sleeps):
    server = serve(Server({"101": [httpx.ConnectError("boom"), httpx.ReadTimeout("slow")]}))
    out = download(frame(["101"]), tmp_path)
    assert server.count("101") == 3 and sleeps == [1, 2] and out["audio_path"].iloc[0] is not None


def test_an_empty_body_counts_as_a_failure_and_is_retried(tmp_path, serve, sleeps):
    server = serve(Server(body=b""))
    out = download(frame(["101"]), tmp_path)
    assert server.count("101") == 3 and out["download_error"].iloc[0] == "empty body after 3 tries"
    assert not (tmp_path / "101.mp3").exists()


def test_one_failure_does_not_stop_the_others(tmp_path, serve, sleeps):
    serve(Server({"102": [404]}))
    out = download(frame(["101", "102", "103"]), tmp_path)
    assert [p is not None for p in out["audio_path"]] == [True, False, True]


def test_missing_urls_are_reported_without_a_request(tmp_path, serve, sleeps):
    server = serve(Server())
    out = download(frame(["101", "102"], urls=False), tmp_path)
    assert server.hits == [] and list(out["download_error"]) == ["no recording_url"] * 2
    assert all(p is None for p in out["audio_path"])
    assert download(frame(["1"]).drop(columns="recording_url"), tmp_path)["download_error"].iloc[0] == "no recording_url"


def test_errors_never_contain_the_signed_url(tmp_path, serve, sleeps):
    serve(Server({"101": [500, 500, 500], "102": [404], "103": [httpx.ConnectError("boom"), httpx.ConnectError("x"),
                                                              httpx.ConnectError("y")]}))
    out = download(frame(["101", "102", "103"]), tmp_path)
    assert all("SECRET-SIG" not in e and "recordings.example" not in e for e in out["download_error"])


def test_download_creates_the_output_directory(tmp_path, serve, sleeps):
    serve(Server())
    download(frame(["1"]), tmp_path / "a" / "b")
    assert (tmp_path / "a" / "b" / "1.mp3").exists()


def test_download_follows_redirects(tmp_path, monkeypatch, sleeps):
    def handler(request):
        if request.url.path.endswith("/start.mp3"):
            return httpx.Response(302, headers={"location": "https://cdn.example/real.mp3"})
        return httpx.Response(200, content=b"final")
    monkeypatch.setattr(tr, "_make_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))
    calls = frame(["7"]).assign(recording_url="https://r.example/start.mp3")
    download(calls, tmp_path)
    assert (tmp_path / "7.mp3").read_bytes() == b"final"


# ---------------------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------------------

class Counting:
    """Wraps a provider and records the call_ids of every batch it was asked for."""

    name = "counting"

    def __init__(self, inner=None, fail_on=(), cap_on=()):
        self.inner, self.batches, self.fail_on, self.cap_on = inner or StubProvider(), [], set(fail_on), set(cap_on)

    def transcribe_batch(self, paths, call_ids):
        self.batches.append(list(call_ids))
        if self.fail_on & set(call_ids):
            raise ProviderError("job failed")
        if self.cap_on & set(call_ids):
            raise SpendCapExceeded("cap")
        return self.inner.transcribe_batch(paths, call_ids)

    @property
    def requested(self):
        return [c for b in self.batches for c in b]


def with_audio(tmp_path, ids, durations=None, missing=()):
    d = tmp_path / "rec"
    d.mkdir(exist_ok=True)
    paths = []
    for cid in ids:
        p = d / f"{cid}.mp3"
        if cid not in missing:
            p.write_bytes(b"x" + cid.encode())
        paths.append(str(p))
    df = frame(ids, durations)
    df["audio_path"] = paths
    return df


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_transcribe_labels_roles_and_appends_json_lines(tmp_path):
    out = tmp_path / "t.jsonl"
    result = transcribe(with_audio(tmp_path, ["fx_other_first", "fx_bot_first"]), StubProvider(), out=out)
    assert [t.call_id for t in result] == ["fx_other_first", "fx_bot_first"]
    first = result[0]
    assert first.turns[0].role == "buyer" and first.turns[1].role == "bot" and first.role_confidence == 0.9
    assert first.source == "corpus" and first.stt_model == "stub" and first.language == "hi-IN"
    assert [d["call_id"] for d in read_lines(out)] == ["fx_other_first", "fx_bot_first"]
    assert all(Transcript.model_validate(d) for d in read_lines(out))


def test_calls_shorter_than_min_duration_are_not_transcribed(tmp_path):
    calls = with_audio(tmp_path, ["a", "b", "c", "d"], durations=[5.0, 10.0, 30.0, float("nan")])
    prov = Counting()
    result = transcribe(calls, prov, out=tmp_path / "t.jsonl")
    assert prov.requested == ["b", "c"] and [t.call_id for t in result] == ["b", "c"]      # 10 s is included
    prov2 = Counting()
    transcribe(calls, prov2, min_duration_s=20.0, out=tmp_path / "t2.jsonl")
    assert prov2.requested == ["c"]


def test_calls_without_a_downloaded_file_are_skipped(tmp_path):
    calls = with_audio(tmp_path, ["a", "b", "c"], missing={"b"})
    calls.loc[calls["call_id"] == "c", "audio_path"] = None
    prov = Counting()
    transcribe(calls, prov, out=tmp_path / "t.jsonl")
    assert prov.requested == ["a"]
    prov2 = Counting()
    assert transcribe(calls.drop(columns="audio_path"), prov2, out=tmp_path / "t2.jsonl") == [] and prov2.batches == []


def test_transcribe_sends_chunks_of_twenty(tmp_path):
    ids = [f"c{i:02d}" for i in range(45)]
    prov = Counting()
    result = transcribe(with_audio(tmp_path, ids), prov, out=tmp_path / "t.jsonl")
    assert [len(b) for b in prov.batches] == [20, 20, 5] and len(result) == 45
    assert tr.CHUNK == 20


def test_a_second_run_resumes_and_calls_the_provider_for_nothing(tmp_path):
    out = tmp_path / "t.jsonl"
    calls = with_audio(tmp_path, ["fx_other_first", "fx_bot_first"])
    first = transcribe(calls, StubProvider(), out=out)
    prov = Counting()
    again = transcribe(calls, prov, out=out)
    assert prov.batches == [] and again == first and len(read_lines(out)) == 2


def test_new_calls_are_added_to_an_existing_file(tmp_path):
    out = tmp_path / "t.jsonl"
    transcribe(with_audio(tmp_path, ["a", "b"]), StubProvider(), out=out)
    prov = Counting()
    result = transcribe(with_audio(tmp_path, ["a", "b", "c", "d"]), prov, out=out)
    assert prov.requested == ["c", "d"] and [t.call_id for t in result] == ["a", "b", "c", "d"]
    assert [d["call_id"] for d in read_lines(out)] == ["a", "b", "c", "d"]


def test_the_result_follows_the_order_of_the_calls_frame(tmp_path):
    out = tmp_path / "t.jsonl"
    transcribe(with_audio(tmp_path, ["a", "b"]), StubProvider(), out=out)
    result = transcribe(with_audio(tmp_path, ["b", "a"]), Counting(), out=out)
    assert [t.call_id for t in result] == ["b", "a"]


def test_a_torn_last_line_is_redone_and_does_not_corrupt_the_next_record(tmp_path):
    out = tmp_path / "t.jsonl"
    transcribe(with_audio(tmp_path, ["a"]), StubProvider(), out=out)
    with out.open("a", encoding="utf-8") as f:
        f.write('{"call_id": "b", "source": "corp')                                          # killed mid-write
    prov = Counting()
    result = transcribe(with_audio(tmp_path, ["a", "b"]), prov, out=out)
    assert prov.requested == ["b"] and [t.call_id for t in result] == ["a", "b"]
    good = [line for line in out.read_text(encoding="utf-8").splitlines() if line.strip().endswith("}")]
    assert any('"call_id":"b"' in line and line.startswith('{"call_id":"b"') for line in good)
    prov2 = Counting()
    transcribe(with_audio(tmp_path, ["a", "b"]), prov2, out=out)
    assert prov2.batches == []                                                               # b is now durably there


def test_a_failed_chunk_is_logged_and_skipped_and_the_rest_continue(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    ids = [f"c{i:02d}" for i in range(45)]
    out = tmp_path / "t.jsonl"
    prov = Counting(fail_on={"c25"})                                                         # the middle chunk fails
    result = transcribe(with_audio(tmp_path, ids), prov, out=out)
    assert [len(b) for b in prov.batches] == [20, 20, 5]
    assert len(result) == 25 and "c25" not in [t.call_id for t in result]
    assert "failed" in caplog.text
    healthy = Counting()
    result2 = transcribe(with_audio(tmp_path, ids), healthy, out=out)
    assert healthy.requested == ids[20:40] and len(result2) == 45                            # a rerun retries only those


def test_the_spend_cap_is_never_swallowed_and_earlier_chunks_are_kept(tmp_path):
    ids = [f"c{i:02d}" for i in range(30)]
    out = tmp_path / "t.jsonl"
    with pytest.raises(SpendCapExceeded):
        transcribe(with_audio(tmp_path, ids), Counting(cap_on={"c25"}), out=out)
    assert len(read_lines(out)) == 20


def test_a_call_with_no_speech_is_not_written_so_a_rerun_retries_it(tmp_path):
    class Silent(Counting):
        def transcribe_batch(self, paths, call_ids):
            out = super().transcribe_batch(paths, call_ids)
            return [d.model_copy(update={"segments": ()}) if d.call_id == "b" else d for d in out]

    out = tmp_path / "t.jsonl"
    result = transcribe(with_audio(tmp_path, ["a", "b"]), Silent(), out=out)
    assert [t.call_id for t in result] == ["a"] and [d["call_id"] for d in read_lines(out)] == ["a"]
    prov = Counting()
    transcribe(with_audio(tmp_path, ["a", "b"]), prov, out=out)
    assert prov.requested == ["b"]


def test_duplicate_call_ids_are_transcribed_once(tmp_path):
    calls = with_audio(tmp_path, ["a", "a", "b"])
    prov = Counting()
    result = transcribe(calls, prov, out=tmp_path / "t.jsonl")
    assert prov.requested == ["a", "b"] and [t.call_id for t in result] == ["a", "b"]


def test_the_rubric_argument_is_unused(tmp_path):
    calls = with_audio(tmp_path, ["fx_bot_first"])
    a = transcribe(calls, StubProvider(), rubric=None, out=tmp_path / "a.jsonl")
    b = transcribe(calls, StubProvider(), rubric=object(), out=tmp_path / "b.jsonl")      # type: ignore[arg-type]
    assert a == b


def test_numeric_ids_in_the_frame_become_string_call_ids(tmp_path):
    calls = with_audio(tmp_path, ["596711"])
    calls["call_id"] = np.int64(596711)
    result = transcribe(calls, StubProvider(), out=tmp_path / "t.jsonl")
    assert [t.call_id for t in result] == ["596711"]


def test_output_directory_is_created(tmp_path):
    out = tmp_path / "deep" / "er" / "t.jsonl"
    transcribe(with_audio(tmp_path, ["a"]), StubProvider(), out=out)
    assert out.exists()


def test_download_then_transcribe_end_to_end(tmp_path, serve, sleeps):
    serve(Server())
    calls = frame(["fx_bot_first", "fx_other_first"])
    got = download(calls, tmp_path / "rec")
    result = transcribe(got, StubProvider(), out=tmp_path / "t.jsonl")
    assert [t.role_confidence for t in result] == [0.9, 0.9]
