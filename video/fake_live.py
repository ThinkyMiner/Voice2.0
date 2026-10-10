"""DRAFT ONLY: a fake live experiment in this worktree's sandbox store, offline, so the video's draft can film
the live page and `make calls ARGS=status`. It drives sarvam_live exactly as tests/ext/test_sarvam_live.py does:
fake settings, an allowlist of invented numbers (+91999000000N, team-01 to team-04) and the test module's fake
Sarvam behind httpx.MockTransport. No phone rings and nothing leaves the machine. Then start --split 0.5,
call --max 4, fetch, status; status's output goes to video/out/terminal/14-live.txt.

    HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 uv run python video/fake_live.py

It refuses to run unless the store is this worktree's, the worktree has no .env and both proxies are the
dead http://127.0.0.1:9 (so any client that is not the mock fails instead of reaching the network).
"""
import contextlib
import importlib.util
import io
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
WT = HERE.parent
FAKE = HERE / "out" / "fake"
DEAD = "http://127.0.0.1:9"

for k in ("HTTPS_PROXY", "HTTP_PROXY"):
    if os.environ.get(k) != DEAD:
        sys.exit(f"refusing: set {k}={DEAD} (the fake driver runs offline only)")
if (WT / ".env").exists():
    sys.exit(f"refusing: {WT / '.env'} exists; the fake driver never runs where real keys live")

import httpx  # noqa: E402
import plotly.graph_objects as go  # noqa: E402

from abtest import contracts  # noqa: E402
from abtest.ext import loop, models  # noqa: E402

if contracts.ROOT != WT or not Path(loop.DB_PATH).resolve().is_relative_to(WT) \
        or not models.LOOP_DIR.resolve().is_relative_to(WT):
    sys.exit(f"refusing: the store must be this worktree's ({WT}), not {contracts.ROOT} / {loop.DB_PATH}")

spec = importlib.util.spec_from_file_location("fake_sarvam", WT / "tests" / "ext" / "test_sarvam_live.py")
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
live = t.live

FAKE.mkdir(parents=True, exist_ok=True)
team = FAKE / "team_phones.yaml"
team.write_text(t.TEAM, encoding="utf-8")
os.environ.update(t.ENV)  # fake keys, agent and versions
live.ENV_FILE = FAKE / "no.env"  # never a real .env
live.ALLOWLIST = team
assert all(re.fullmatch(r"\+91999000000[1-4]", x["phone"]) for x in live.allowlist()), "invented numbers only"
now = [t.NOW]
live.now_ist = lambda: now[0]
fake, real_client = t.FakeSarvam(), live.client
live.client = lambda s: real_client(s, transport=httpx.MockTransport(fake))
go.Figure.write_image = lambda *a, **k: None  # no kaleido

with contextlib.closing(live.connect()) as conn:
    started = live.store.get_state(conn, "vani-live-c1") is not None
if not started:
    live.main(["start", "--split", "0.5"])
    live.main(["call", "--yes-place-real-calls", "--max", "4"])
    now[0] = t.NOW + timedelta(minutes=10)
    live.main(["fetch"])
out = io.StringIO()
with contextlib.redirect_stdout(out):
    live.main(["status"])
(HERE / "out" / "terminal").mkdir(parents=True, exist_ok=True)
(HERE / "out" / "terminal" / "14-live.txt").write_text(f"$ make calls ARGS=status\n{out.getvalue()}", encoding="utf-8")
print(out.getvalue(), end="")
print(f"POSTs to the fake Sarvam: {sum(r.method == 'POST' for r in fake.requests)}")
