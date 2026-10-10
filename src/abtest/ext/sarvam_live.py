"""Live calls: real phone calls through Sarvam's Voice Agents platform as traffic for the A/B engine
(docs/runbook_live_calls.md).

Each allowlisted lead gets A or B from our router (router.assign logs the exposure before the phone
rings), and the call goes out with that arm's committed agent version (Instant Outbound, one POST per
lead; the agent holds the prompt). `start` freezes which agent the experiment calls and which version is
which arm. Sarvam has no idempotency key, so each POST is claimed in the store first (live_attempts, its
key unique): a lead is dialled at most once per experiment, across crashes and parallel runs, unless
Sarvam definitely refused the request. `fetch` polls Sarvam's attempts list: each ended attempt becomes
one calls row for the engine and, when it connected, a Transcript (source "live") for the judge. Live
calls are real call content, so the judge sends them to the model provider only on the user's go-ahead.

Only the numbers in ALLOWLIST are dialled (consenting team members, never real buyers). A phone number
is kept there and nowhere else (not in transcripts, the store, logs or reports), and printed masked.

CLI: python -m abtest.ext.sarvam_live start [--split 0.1|0.5] [--id vani-live-c1]
     python -m abtest.ext.sarvam_live call --yes-place-real-calls [--max 10] [--id ...]
     python -m abtest.ext.sarvam_live fetch | look | status [--id ...]
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import pandas as pd
import yaml

from abtest import analysis, boundaries, config, decision, metrics, router, rules, runner, srm, store
from abtest.contracts import DATA_DIR, ROOT, State
from abtest.ext import listen, loop, models
from abtest.ext.models import Transcript, Turn, save

BASE_URL = "https://apps.sarvam.ai/api"  # /outbounds/... places calls, /analytics/... reads them back
ENV_FILE = ROOT / ".env"
ALLOWLIST = DATA_DIR / "live" / "team_phones.yaml"   # git-ignored: the only place a phone number lives
LIVE_DIR = models.LOOP_DIR / "live"                   # git-ignored: <id>.yaml configs, split reports, raw/
RAW_DIR = LIVE_DIR / "raw"
PROMPTS = {"A": "prompts/live/vani_demo_A.txt", "B": "prompts/live/vani_demo_B.txt"}  # what the agent versions hold
FROZEN = ("SARVAM_ORG_ID", "SARVAM_WORKSPACE_ID", "SARVAM_APP_ID", "SARVAM_VERSION_A", "SARVAM_VERSION_B")  # at start
SETTINGS = FROZEN + ("SARVAM_CONNECTION_ID", "SARVAM_AGENT_NUMBER")
IST = timezone(timedelta(hours=5, minutes=30))        # the store's timestamps are naive IST
CALL_HOURS = (9, 21)                                  # dial only from 09:00 to 21:00 IST
LIVE_ID = re.compile(r"vani-live-[a-z0-9-]+")         # service.py shows these as live, not simulated
LEAD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")         # attempt ids become file names
E164 = re.compile(r"\+[1-9]\d{9,14}")                 # 10+ digits, so listen.PHONE masks every number dialled
SLACK = timedelta(minutes=10)        # widens the attempts window: their clock and ours
FINAL_AFTER = timedelta(minutes=30)  # a connected call without the agent's disposition stays provisional this long
ABRUPT_S = 20.0                      # a connected call the buyer ended this soon is an abrupt disconnect
PAGE = 100
NOT_CONNECTED = {"no_answer", "busy", "failed"}  # the webhook's documented outcomes; the analytics API lists none
AGENT_VALUES = {v.lower(): v for v in (  # the agent's `disposition` enum (docs/sarvam_agent_setup.md step 4)
    "BL Enriched", "BL Approved", "BL Generated", "No Enrichment Scope", "Language Barrier")}
LANGUAGES = {"hindi": "hi", "english": "en", "telugu": "te"}
BOT_ROLES, BUYER_ROLES = {"agent", "assistant", "bot", "ai"}, {"user", "human", "customer", "caller", "buyer"}
ENDED_BY_BUYER = BUYER_ROLES - {"caller"}  # on an outbound call the caller is the agent
TEXT_KEYS = ("content", "text", "message", "en_text", "indic_text")
TURN_LINE = re.compile(r"^\s*(\w+)\s*:\s*(.+)$")

LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_experiments (  -- the agent a live experiment calls, frozen at start
    experiment_id TEXT PRIMARY KEY,
    agent         TEXT NOT NULL                -- JSON {name: value} of FROZEN
);
CREATE TABLE IF NOT EXISTS live_attempts (     -- one row per POST, claimed before it is sent; never deleted
    experiment_id TEXT NOT NULL,
    lead_id       TEXT NOT NULL,
    try_no        INTEGER NOT NULL,            -- above 1 only after Sarvam definitely refused the earlier tries
    variant       TEXT NOT NULL,
    app_version   INTEGER NOT NULL,
    prompt_hash   TEXT NOT NULL,
    ts            TEXT NOT NULL,               -- naive IST, the exposure's time
    status        TEXT NOT NULL,               -- unknown (it may have rung) | placed | refused (no call)
    attempt_id    TEXT UNIQUE,
    note          TEXT,                        -- the HTTP status or error name, never a response body
    outcome       TEXT,                        -- its calls row: NULL (none yet) | provisional | final
    PRIMARY KEY (experiment_id, lead_id, try_no)
);
"""

REFUSAL = f"""refusing: this places real phone calls through Sarvam; each one rings a person and costs money.
Only the user can approve that: once they have, re-run with --yes-place-real-calls. Only the numbers in
{ALLOWLIST} are dialled (consenting team members, never real buyers), from 09:00 to 21:00 IST."""


# --- settings, allowlist, clocks, the store -----------------------------------------------------

def _env(name: str) -> str | None:
    """The environment first, then .env (llm._env_key's pattern)."""
    if os.environ.get(name):
        return os.environ[name].strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                return v.strip().strip("'\"")
    return None


def settings() -> dict[str, str]:
    key = _env("SARVAM_VA_API_KEY") or _env("SARVAM_API_KEY")  # SARVAM_API_KEY may be a Model-API key
    if not key:
        sys.exit("missing SARVAM_VA_API_KEY (a Voice Agents API key; SARVAM_API_KEY is the fallback): "
                 "set it in the environment or .env")
    s = {"key": key}
    for name in SETTINGS:
        if not (value := _env(name)):
            sys.exit(f"missing {name}: set it in the environment or .env (docs/runbook_live_calls.md)")
        s[name] = value
    for name in ("SARVAM_VERSION_A", "SARVAM_VERSION_B"):
        if not s[name].isdigit() or int(s[name]) < 1:
            sys.exit(f"{name} must be a committed agent version number (1, 2, ...), got {s[name]!r}")
        s[name] = str(int(s[name]))
    if s["SARVAM_VERSION_A"] == s["SARVAM_VERSION_B"]:
        sys.exit("SARVAM_VERSION_A and SARVAM_VERSION_B must be two different committed versions "
                 "(the one holding Prompt A and the one holding Prompt B)")
    if not E164.fullmatch(s["SARVAM_AGENT_NUMBER"]):
        sys.exit("SARVAM_AGENT_NUMBER must be the rented number in E.164, like +91XXXXXXXXXX")
    return s


def allowlist() -> list[dict[str, str]]:
    """The team allowlist: [{lead_id, phone, name, product, city}]. No message prints a number."""
    try:
        raw = yaml.safe_load(ALLOWLIST.read_text(encoding="utf-8")) if ALLOWLIST.exists() else None
    except yaml.YAMLError as e:  # its message quotes the broken line, number and all: give the place only
        mark = getattr(e, "problem_mark", None)
        sys.exit(f"{ALLOWLIST.name} is not valid YAML"
                 + (f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""))
    if not isinstance(raw, list) or not raw:
        sys.exit(f"no allowlist: write {ALLOWLIST} as a list of {{lead_id, phone, name, product, city}} "
                 "(consenting team members only; docs/runbook_live_calls.md)")
    leads = []
    for i, r in enumerate(raw, 1):
        r = r if isinstance(r, dict) else {}
        lead = str(r.get("lead_id") or "")
        if not LEAD_ID.fullmatch(lead) or listen.PHONE.search(lead):  # it is stored and printed as is
            sys.exit(f"{ALLOWLIST.name} entry {i}: lead_id must be 1-64 letters, digits, _ or -, "
                     "and not a phone number")
        if not isinstance(r.get("phone"), str) or not E164.fullmatch(r["phone"]):
            sys.exit(f'{ALLOWLIST.name} {lead}: phone must be an E.164 string in quotes, like "+91XXXXXXXXXX"')
        leads.append({k: str(r.get(k) or "") for k in ("lead_id", "phone", "name", "product", "city")})
    for k in ("lead_id", "phone"):
        if len({x[k] for x in leads}) < len(leads):
            sys.exit(f"{ALLOWLIST.name}: the same {k} appears twice")
    return leads


def masked(phone: str) -> str:
    return phone[:3] + "*" * (len(phone) - 7) + phone[-4:]


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _in_hours(placed: int = 0) -> datetime:
    """The IST time now, or exit outside calling hours (checked before anything else, and before each POST)."""
    now = now_ist()
    if not CALL_HOURS[0] <= now.hour < CALL_HOURS[1]:
        sys.exit(f"refusing: it is {now:%H:%M} IST; calls go out only from 09:00 to 21:00 IST"
                 + (f" (stopped; {placed} placed this run)" if placed else ""))
    return now


def _ist(value) -> datetime | None:
    """Sarvam's ISO timestamp (UTC; a naive one is taken as UTC) -> naive IST. metrics drops every call
    stamped before its lead's exposure, so a UTC stamp, 5.5 h behind, would silently drop them all."""
    try:
        t = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(IST).replace(tzinfo=None)


def _utc(ist: datetime) -> str:
    return ist.replace(tzinfo=IST).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scrub(x):
    """A JSON value with every phone number out (in strings, keys and numbers), phone and contact fields dropped."""
    if isinstance(x, str):
        return listen.PHONE.sub("[number]", x)
    if isinstance(x, list):
        return [_scrub(v) for v in x]
    if isinstance(x, dict):
        return {_scrub(k): _scrub(v) for k, v in x.items()
                if not any(w in str(k).lower() for w in ("phone", "contact"))}
    if isinstance(x, int | float) and not isinstance(x, bool) and listen.PHONE.search(str(x)):
        return "[number]"
    return x


def _clean(value, n: int = 80) -> str:
    """Text from Sarvam, fit to print: numbers masked first, then cut (a cut number would keep its digits)."""
    return listen.PHONE.sub("[number]", str(value))[:n]


def _text(r: httpx.Response) -> str:
    return _clean(r.text, 300)  # a 422 echoes the request, the number included


def connect() -> sqlite3.Connection:
    conn = store.connect(loop.DB_PATH)
    conn.executescript(LIVE_SCHEMA)
    return conn


def _attempts(conn, eid: str) -> list[dict]:
    """eid's POSTs, oldest first."""
    return [dict(r) for r in conn.execute("SELECT * FROM live_attempts WHERE experiment_id = ? ORDER BY rowid", (eid,))]


def _mark(conn, key: tuple, status: str, note: str | None, aid: str | None = None) -> None:
    with conn:
        conn.execute("UPDATE live_attempts SET status = ?, note = ?, attempt_id = ? "
                     "WHERE experiment_id = ? AND lead_id = ? AND try_no = ?", (status, note, aid, *key))


def _state(conn, eid: str):
    if (s := store.get_state(conn, eid)) is None:
        sys.exit(f'no experiment {eid} in {loop.DB_PATH}; first run: make calls ARGS="start --id {eid}"')
    return s


def _agent(conn, eid: str) -> dict[str, str]:
    """The agent and arm versions `start` froze for eid."""
    row = conn.execute("SELECT agent FROM live_experiments WHERE experiment_id = ?", (eid,)).fetchone()
    if row is None:
        sys.exit(f"{eid} has no frozen Sarvam agent, so it is not a live experiment this module started")
    return json.loads(row["agent"])


def client(s: dict[str, str], transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, headers={"X-API-Key": s["key"]}, timeout=30, transport=transport)


# --- start --------------------------------------------------------------------------------------

def start(conn, eid: str, split: float, now: datetime) -> None:
    """The buyer template with the two agent prompts, maturation 0 and its own salt, frozen and RUNNING, with
    the agent and each arm's version frozen beside it (live_experiments)."""
    if (s := store.get_state(conn, eid)) is not None:
        sys.exit(f"{eid} already exists ({s.state.value}); start another with --id vani-live-c<n>")
    agent = {k: v for k, v in settings().items() if k in FROZEN}
    arms = (f"agent {agent['SARVAM_APP_ID']}: A = version {agent['SARVAM_VERSION_A']} ({PROMPTS['A']}), "
            f"B = version {agent['SARVAM_VERSION_B']} ({PROMPTS['B']})")
    raw = yaml.safe_load((ROOT / loop.TEMPLATE).read_text(encoding="utf-8"))
    raw.update(id=eid, salt=hashlib.sha256(eid.encode()).hexdigest()[:6],
               allocation={"A": round(1 - split, 6), "B": split}, maturation_days=0,
               variants={v: {"patch": {"mode": "full", "path": p}} for v, p in PROMPTS.items()})
    why = ("a DEMO split, so a handful of calls lands in both arms (the template runs 90/10)" if split == 0.5
           else "the template's split")
    path = LIVE_DIR / f"{eid}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Vani Lab live calls: real Sarvam calls routed by abtest.router, {arms}.\n"
                    f"# Written by abtest.ext.sarvam_live from {loop.TEMPLATE}. B gets {split:.0%}: {why}.\n"
                    "# maturation_days 0: a live call's outcome is known when it ends; the template's 1 day would "
                    "keep tonight's calls out of every look until tomorrow.\n"
                    + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    try:
        frozen = config.freeze(config.load_experiment(path))
    except ValueError as e:  # a missing prompt file, or a config the engine rejects
        sys.exit(f"cannot start {eid}: {e}")
    store.create_experiment(conn, frozen)
    with conn:
        conn.execute("INSERT INTO live_experiments VALUES (?, ?)", (eid, json.dumps(agent)))
    state = decision.start(conn, frozen, now)
    print(f"{eid}: {state.state.value}, {arms}.\n{store.read_decisions(conn, eid)[-1].reason}")
    if split == 0.5:
        print("DEMO SPLIT: 50/50, so a handful of calls lands in both arms; the production template runs 90/10.")
    print(f"config -> {path}")


# --- call ---------------------------------------------------------------------------------------

def _body(s: dict[str, str], version: int, lead: dict[str, str], variant: str) -> dict:
    return {"app_config": {"app_id": s["SARVAM_APP_ID"], "app_version": version,
                           "connection_config": {"connection_id": s["SARVAM_CONNECTION_ID"],
                                                 "agent_phone_number": s["SARVAM_AGENT_NUMBER"]},
                           "agent_variables": {"lead_id": lead["lead_id"], "ab_variant": variant,
                                               "buyer_name": lead["name"], "product_name": lead["product"],
                                               "city": lead["city"]}},
            "user_config": {"user_phone_number": lead["phone"]}}


def _attempt_id(r: httpx.Response) -> str | None:
    try:
        aid = r.json().get("attempt_id") if r.status_code == 200 else None
    except (ValueError, AttributeError):
        return None
    return aid if isinstance(aid, str) and SAFE_ID.fullmatch(aid) else None


def place_calls(conn, eid: str, limit: int, approved: bool) -> int:
    """At most `limit` calls, one per allowlisted lead not yet called in eid. The only code that dials, so it
    checks everything itself: the user's go-ahead, calling hours (again before each POST), the allowlist (the
    only source of numbers), and the agent, versions and prompts eid froze. Each POST is claimed first, its
    key unique: no crash or parallel run can dial a lead twice. Only a definite refusal (HTTP 422, 401 or 403,
    or no connection) frees the lead for a later run; anything else may have rung, and the run stops."""
    if not approved:
        sys.exit(REFUSAL)
    _in_hours()
    s, leads = settings(), allowlist()
    state, agent = _state(conn, eid), _agent(conn, eid)
    if changed := [k for k in FROZEN if s[k] != agent[k]]:
        sys.exit(f"{', '.join(changed)} changed since {eid} started, which froze {agent['SARVAM_APP_ID']} with A = "
                 f"version {agent['SARVAM_VERSION_A']} and B = version {agent['SARVAM_VERSION_B']}: restore .env, "
                 "or start a new experiment")
    try:
        same = config.freeze(store.get_config(conn, eid)).prompt_hash == state.prompt_hash
    except ValueError as e:  # a prompt file is gone
        sys.exit(f"{eid}: {e}")
    if not same:
        sys.exit(f"a prompt file of {eid} changed after it started, so the agent versions may no longer hold "
                 "the texts it froze; restore the files or start a new experiment")
    rows = _attempts(conn, eid)
    tries = {x["lead_id"]: [r for r in rows if r["lead_id"] == x["lead_id"]] for x in leads}
    todo = [x for x in leads if all(r["status"] == "refused" for r in tries[x["lead_id"]])]
    print(f"{eid} ({state.state.value}, B gets {state.allocation.B:.0%}): calling {min(len(todo), limit)} of "
          f"{len(leads)} allowlisted leads ({len(leads) - len(todo)} called before)")
    url = f"/outbounds/v1/orgs/{agent['SARVAM_ORG_ID']}/workspaces/{agent['SARVAM_WORKSPACE_ID']}/outbounds"
    placed = 0
    with client(s) as http:
        for lead in todo[:limit]:
            now = _in_hours(placed)
            a = router.assign(conn, eid, lead["lead_id"], now)  # the exposure is logged before the phone rings
            version = int(agent[f"SARVAM_VERSION_{a.variant}"])
            key = (eid, lead["lead_id"], len(tries[lead["lead_id"]]) + 1)
            who = f"  {lead['lead_id']} ({masked(lead['phone'])}) -> {a.variant}, agent version {version}"
            try:
                with conn:  # the claim, committed before the phone can ring; "unknown" until Sarvam answers
                    conn.execute("INSERT INTO live_attempts (experiment_id, lead_id, try_no, variant, app_version, "
                                 "prompt_hash, ts, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'unknown')",
                                 (*key, a.variant, version, a.prompt_hash, now.isoformat(timespec="microseconds")))
            except sqlite3.IntegrityError:
                print(f"{who}: another call run claimed it first; skipped")
                continue
            try:
                r = http.post(url, json=_body(s, version, lead, a.variant))
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # no connection: the request never left
                _mark(conn, key, "refused", type(e).__name__)
                sys.exit(f"{who}: {type(e).__name__}, no call was placed; check the network and run call again")
            except httpx.HTTPError as e:
                _mark(conn, key, "unknown", type(e).__name__)
                sys.exit(f"{who}: {type(e).__name__}; it may have rung, so it is never dialled again (fetch takes the "
                         "call if Sarvam lists it). Stopped here, so a Sarvam outage costs one lead.")
            if aid := _attempt_id(r):
                _mark(conn, key, "placed", None, aid)
                placed += 1
                print(f"{who}: attempt {aid}")
            elif r.status_code in (401, 403, 422):  # Sarvam refused the request itself: no phone rang
                _mark(conn, key, "refused", f"HTTP {r.status_code}")
                if r.status_code != 422:
                    sys.exit(f"{who}: HTTP {r.status_code}: {_text(r)}\nthe key was refused: put a Voice Agents API "
                             "key (Settings > API Key) in SARVAM_VA_API_KEY")
                print(f"{who}: refused, HTTP 422: {_text(r)}; no call was placed, the next call run tries it again")
            else:
                _mark(conn, key, "unknown", f"HTTP {r.status_code}")
                sys.exit(f"{who}: HTTP {r.status_code}: {_text(r)}; it may have rung, so it is never dialled again "
                         "(fetch takes the call if Sarvam lists it). Stopped here.")
    return placed


# --- fetch --------------------------------------------------------------------------------------

def _list(http: httpx.Client, path: str, window: dict[str, str]) -> list[dict]:
    items, offset = [], 0
    while True:
        r = http.get(path, params={**window, "limit": PAGE, "offset": offset})
        if r.status_code != 200:
            sys.exit(f"Sarvam's attempts list: HTTP {r.status_code}: {_text(r)}")
        page = r.json()
        batch = page.get("items") or []
        items += batch
        offset += len(batch)
        if not batch or offset >= (page.get("total") or 0):
            return items


def _vars(x: dict) -> dict:
    v = x.get("agent_variables")
    return v if isinstance(v, dict) else {}


def agent_disposition(x: dict) -> str | None:
    """The agent's own `disposition` output variable, as it gave it (docs/sarvam_agent_setup.md)."""
    return str(_vars(x).get("disposition") or "").strip() or None


def call_row(x: dict, placed: dict, labels: tuple[str, ...], now: datetime) -> tuple[dict, bool] | None:
    """The calls row of an ended attempt, and whether it is final; None while it rings or talks, or while its
    status is one we don't know. Sarvam documents neither the analytics statuses nor when the agent extracts
    its output variables, so a connected call has ended only once it has an end time (a duration proves
    nothing), and without the agent's disposition its row is provisional for FINAL_AFTER.
    The agent's disposition counts when it is one of the experiment's own labels (`labels`: the primary's
    and the guardrail's). The agent has no "abruptly disconnected" value, so any other value gives way to
    the abrupt rule, else No Enrichment Scope: the guardrail must still see a buyer hanging up early."""
    status = str(x.get("connectivity_status") or "").strip().lower()
    connected, agent = status == "connected", agent_disposition(x)
    start, end = _ist(x.get("start_datetime")), _ist(x.get("end_datetime"))
    if not status or (connected and end is None) or (
            not connected and status not in NOT_CONNECTED and end is None and not x.get("failure_reason")):
        return None
    duration = float(x.get("duration_in_seconds") or 0)
    ours = {d.lower(): d for d in labels}
    if not connected:
        label = "Not Connected"
    elif agent and agent.lower() in ours:
        label = ours[agent.lower()]
    elif ENDED_BY_BUYER & set(re.findall(r"[a-z]+", str(x.get("ended_by") or "").lower())) and duration < ABRUPT_S:
        label = "Abruptly Disconnected Call"  # the template's guardrail label
    else:
        label = "No Enrichment Scope"
    exposed = datetime.fromisoformat(placed["ts"])
    final = not connected or agent is not None or now - min(end, now) >= FINAL_AFTER
    return {"experiment_id": placed["experiment_id"], "call_id": x["attempt_id"], "lead_id": placed["lead_id"],
            "ts": min(max(start or exposed, exposed), now),  # within [exposure, now], whatever the two clocks say
            "answered": int(connected), "disposition": label, "duration_s": duration, "attempt_no": 1}, final


def _adopt(conn, rows: list[dict], listed: list[dict]) -> None:
    """A POST that got no answer may still have placed its call: when exactly one unclaimed attempt in Sarvam's
    list carries that lead and arm in its agent variables, it is that call."""
    claimed = {r[0] for r in conn.execute("SELECT attempt_id FROM live_attempts WHERE attempt_id IS NOT NULL")}
    for r in rows:
        if r["status"] != "unknown":
            continue
        hits = {x["attempt_id"] for x in listed if SAFE_ID.fullmatch(str(x.get("attempt_id") or ""))
                and x["attempt_id"] not in claimed
                and (_vars(x).get("lead_id"), _vars(x).get("ab_variant")) == (r["lead_id"], r["variant"])}
        if len(hits) == 1:
            aid = hits.pop()
            _mark(conn, (r["experiment_id"], r["lead_id"], r["try_no"]), "placed",
                  f"{r['note'] or 'the run stopped during the POST'}; found in Sarvam's attempts list", aid)
            claimed.add(aid)
            print(f"  {r['lead_id']}: its POST got no answer, but Sarvam lists its call: attempt {aid}")
        elif hits:
            print(f"  {r['lead_id']}: {len(hits)} attempts in Sarvam's list could be its call; check Call Logs")


def _turn(d) -> Turn | None:
    if not isinstance(d, dict):
        return None
    role = str(d.get("role") or d.get("speaker") or "").strip().lower()
    speaker = "bot" if role in BOT_ROLES else "buyer" if role in BUYER_ROLES else None
    text = next((d[k].strip() for k in TEXT_KEYS if isinstance(d.get(k), str) and d[k].strip()), None)
    return Turn(speaker=speaker, text=text) if speaker and text else None


def _turns(body) -> tuple[Turn, ...]:
    """Sarvam documents no transcript body: the first list of {role, text}-like turns found anywhere in it,
    else a text of "role: text" lines."""
    queue, fallback = [body], ()
    while queue:
        x = queue.pop(0)
        if isinstance(x, list):
            if turns := tuple(t for t in map(_turn, x) if t):
                return turns
            queue += x
        elif isinstance(x, dict):
            queue += x.values()
        elif isinstance(x, str) and not fallback:
            fallback = tuple(t for m in map(TURN_LINE.match, x.splitlines()) if m
                             and (t := _turn({"role": m[1], "text": m[2]})))
    return fallback


def _transcript(http: httpx.Client, app: str, x: dict, placed: dict) -> Transcript | None:
    aid, iid = x["attempt_id"], str(x.get("interaction_id") or "")
    if not iid or ".." in iid.split("/"):
        print(f"  {aid}: no usable interaction id yet; the transcript comes on a later fetch")
        return None
    for path in dict.fromkeys((quote(iid), quote(iid, safe=""))):  # the docs don't say: "/" raw, else as %2F
        if (r := http.get(f"{app}/transcripts/{path}")).status_code != 404:
            break
    if r.status_code != 200:
        print(f"  {aid}: transcript not ready (HTTP {r.status_code}); fetch again later")
        return None
    try:
        body = _scrub(r.json())
    except ValueError:  # the body is undocumented: a plain text is read as "role: text" lines
        body = _scrub(r.text)
    save(RAW_DIR / f"{aid}.json", body)
    if not (turns := _turns(body)):
        print(f"  {aid}: no turns found in Sarvam's transcript; its raw body is in {RAW_DIR}")
        return None
    # prompt_version "live": the call ran its arm's agent version, not a registry version, so the loop never
    # takes it for one of v1's calls (the judge files its audit under audits/live/).
    t = Transcript(call_id=aid, source="live", turns=turns, vendor="SARVAM", provider="sarvam", prompt_version="live",
                   language=LANGUAGES.get(str(x.get("language_name") or "").lower()),
                   duration_s=float(x.get("duration_in_seconds") or 0), variant=placed["variant"],
                   meta={"experiment_id": placed["experiment_id"], "lead_id": placed["lead_id"],
                         "agent_version": str(placed["app_version"]), "interaction_id": iid})
    save(models.TRANSCRIPTS_DIR / f"{aid}.json", t)  # aid passed SAFE_ID when the call was placed
    return t


def _said(x: dict, label: str) -> str:
    """What the agent's disposition adds to a printed line: only a value of its declared enum is shown."""
    said = agent_disposition(x)
    if said is None or said.lower() == label.lower():
        return ""
    return (f" (the agent said {AGENT_VALUES[said.lower()]})" if said.lower() in AGENT_VALUES
            else " (the agent's disposition is none of its declared values)")


def fetch(conn, eid: str, now: datetime) -> None:
    """Outcomes and transcripts of eid's calls, read from the agent `start` froze. An ended attempt becomes
    one calls row. A connected call the agent has given no disposition yet is provisional: its row may
    still change, and `look` waits, until the disposition comes or FINAL_AFTER has passed. Re-running is
    safe: a final row and a saved transcript are never fetched again."""
    state, cfg, agent = _state(conn, eid), store.get_config(conn, eid), _agent(conn, eid)
    if not (rows := _attempts(conn, eid)):
        print(f"{eid}: no calls placed yet")
        return
    s = settings()
    labels = cfg.primary.dispositions + (cfg.secondary.dispositions or ())
    app = f"/analytics/v1/{agent['SARVAM_ORG_ID']}/{agent['SARVAM_WORKSPACE_ID']}/{agent['SARVAM_APP_ID']}"
    window = {"start_datetime": _utc(state.started_ts_sim - SLACK), "end_datetime": _utc(now + SLACK)}
    with client(s) as http:
        listed = _list(http, f"{app}/attempts", window)
        _adopt(conn, rows, listed)
        placed = {r["attempt_id"]: r for r in _attempts(conn, eid) if r["attempt_id"]}
        items = {x["attempt_id"]: x for x in listed if x.get("attempt_id") in placed}  # paging can list one twice
        have = {r[0]: r[1] for r in conn.execute("SELECT call_id, disposition FROM calls WHERE experiment_id = ?",
                                                 (eid,))}
        new = waiting = provisional = saved = 0
        for aid, x in items.items():
            p = placed[aid]
            if p["outcome"] != "final":
                if (got := call_row(x, p, labels, now)) is None:
                    waiting += 1
                    print(f"  {p['lead_id']} ({p['variant']}): {_clean(x.get('connectivity_status') or 'no status')}, "
                          "not ended yet")
                    continue
                row, final = got
                head = f"  {p['lead_id']} ({p['variant']}): "
                if aid not in have:
                    store.insert_df(conn, "calls", pd.DataFrame([row]))
                    new += 1
                    why = f" ({_clean(x['failure_reason'])})" if x.get("failure_reason") else ""
                    by = f", ended by {_clean(x['ended_by'], 20)}" if x.get("ended_by") else ""
                    print(f"{head}{_clean(x['connectivity_status'], 20)}{why}{by}, {row['duration_s']:.0f} s, "
                          f"{row['disposition']}{_said(x, row['disposition'])}"
                          + ("" if final else ", provisional: no disposition from the agent yet"))
                elif row["disposition"] != have[aid]:
                    with conn:
                        conn.execute("UPDATE calls SET disposition = ? WHERE experiment_id = ? AND call_id = ?",
                                     (row["disposition"], eid, aid))
                    print(f"{head}{row['disposition']} now (was {have[aid]}){_said(x, row['disposition'])}")
                elif final and agent_disposition(x) is None:
                    print(f"{head}no disposition from the agent in {FINAL_AFTER.seconds // 60} min: "
                          f"{row['disposition']} is final")
                with conn:
                    conn.execute("UPDATE live_attempts SET outcome = ? WHERE attempt_id = ?",
                                 ("final" if final else "provisional", aid))
                provisional += not final
            if str(x.get("connectivity_status") or "").lower() == "connected" \
                    and not (models.TRANSCRIPTS_DIR / f"{aid}.json").exists():
                saved += _transcript(http, app, x, p) is not None
    print(f"{eid}: {len(placed)} calls placed, {len(items)} listed by Sarvam; {new} outcomes recorded now, "
          f"{provisional} provisional (waiting for the agent's disposition), {waiting} still ringing or talking, "
          f"{saved} transcripts saved")


# --- look and status ----------------------------------------------------------------------------

def look(conn, eid: str, now: datetime) -> None:
    """One step of the runner's look chain (runner._run) at look_ts = now: a look is taken only when the
    plan says so (the next planned information fraction, or the window's end), as in the simulation, and
    never on a provisional outcome."""
    state, cfg = _state(conn, eid), store.get_config(conn, eid)
    exposures = store.read_df(conn, "SELECT * FROM exposures WHERE experiment_id = ?", (eid,), parse_dates=["ts"])
    if exposures.empty:
        print(f"{eid}: no lead assigned yet; place calls first")
        return
    if state.state != State.RUNNING:
        print(f"{eid} is {state.state.value}: {store.read_decisions(conn, eid)[-1].reason}")
    else:
        exposures = exposures[exposures.ts >= state.started_ts_sim]
        calls = store.read_df(conn, "SELECT * FROM calls WHERE experiment_id = ? AND ts <= ?",
                              (eid, now.isoformat(timespec="microseconds")), parse_dates=["ts"])
        calls = calls.astype({"answered": bool})
        outcomes = metrics.lead_outcomes(calls, exposures, cfg, now)
        n_b = int((outcomes.variant.eq("B") & outcomes.mature).sum())
        t, done = n_b / state.n_b_max, state.info_fractions
        next_tau = min((p for p in boundaries.planned_info_fractions(cfg) if p > (done[-1] if done else 0)),
                       default=math.inf)
        is_final = t >= 1 or now - state.started_ts_sim >= timedelta(days=cfg.window.days)
        pending = conn.execute("SELECT COUNT(*) FROM live_attempts WHERE experiment_id = ? "
                               "AND outcome = 'provisional'", (eid,)).fetchone()[0]
        print(f"{eid} at {now:%Y-%m-%d %H:%M} IST: {len(outcomes)} leads assigned, "
              f"{int(outcomes.triggered_ts.notna().sum())} answered; information fraction t = {t:.3f} "
              f"({n_b} matured B leads of n_B,max {state.n_b_max:,.0f})")
        if (is_final or t >= next_tau) and pending:
            print(f"verdict: a look is due, but it waits for {pending} provisional outcome{'s' * (pending > 1)} (the "
                  f"agent's disposition may still come, up to {FINAL_AFTER.seconds // 60} min after each call): "
                  "run fetch, then look again")
        elif is_final or t >= next_tau:
            fractions = done + (runner._look_fraction(t, done),)
            bounds = boundaries.compute_boundaries(fractions, cfg)
            result = analysis.analyze_look(outcomes, cfg, state.looks_done + 1, now, bounds, is_final)
            verdict = rules.evaluate_look(result, bounds, cfg)
            state = decision.apply_verdict(conn, state, result, bounds, verdict)
            print(f"look {result.look} of {cfg.window.looks}: {verdict.decision} -> {state.state.value}. "
                  f"{verdict.reason}")
        else:
            store.put_state(conn, state.model_copy(update={"updated_ts_sim": now}))
            print(f"verdict: no look due. The next planned look is at t = {next_tau:.2f} (about "
                  f"{math.ceil(next_tau * state.n_b_max):,} matured B leads); the engine never looks early, "
                  "so nothing is decided and B keeps its share.")
    md = srm.split_report(conn, eid, LIVE_DIR)  # counts of real calls: git-ignored, not reports/
    print("\n".join(line for line in md.read_text(encoding="utf-8").splitlines() if line.startswith("- ")))
    print(f"split report -> {md}")


def status(conn, eid: str) -> None:
    state, rows = _state(conn, eid), _attempts(conn, eid)
    calls = {r["call_id"]: r for r in conn.execute(
        "SELECT call_id, answered, disposition FROM calls WHERE experiment_id = ?", (eid,))}
    assigned = {v: n for v, n in conn.execute(
        "SELECT variant, COUNT(DISTINCT lead_id) FROM exposures WHERE experiment_id = ? GROUP BY variant", (eid,))}
    print(f"{eid}: {state.state.value}, live allocation A {state.allocation.A:.0%} / B {state.allocation.B:.0%}")
    for arm in "AB":
        mine = [r for r in rows if r["variant"] == arm and r["attempt_id"]]
        back = [calls[r["attempt_id"]] for r in mine if r["attempt_id"] in calls]
        dispositions = Counter(row["disposition"] for row in back).most_common()
        provisional = sum(r["outcome"] == "provisional" for r in mine)
        print(f"  {arm}: assigned {assigned.get(arm, 0)}, called {len(mine)}, outcomes back {len(back)}"
              + (f" ({provisional} provisional)" if provisional else "")
              + f", connected {sum(row['answered'] for row in back)}" + "".join(f"; {d} {n}" for d, n in dispositions))
    if unknown := [f"{r['lead_id']} ({r['note'] or 'the run stopped during the POST'})"
                   for r in rows if r["status"] == "unknown"]:
        print("  may have rung, no attempt id (never dialled again; fetch takes the call if Sarvam lists it, "
              f"else see Call Logs): {', '.join(unknown)}")
    latest = {r["lead_id"]: r for r in rows}  # oldest first: each lead's last try wins
    if refused := [f"{k} ({r['note']})" for k, r in latest.items() if r["status"] == "refused"]:
        print(f"  refused by Sarvam, no call placed (the next call run tries again): {', '.join(refused)}")


# --- CLI ----------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    # allow_abbrev=False: argparse would take any prefix (--yes, even --y) for --yes-place-real-calls
    ap = argparse.ArgumentParser(prog="python -m abtest.ext.sarvam_live", description=__doc__.split("\n\n")[0],
                                 allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    cmd = {name: sub.add_parser(name, help=h, allow_abbrev=False) for name, h in (
        ("start", "create and start a live experiment from the buyer template"),
        ("call", "call the allowlisted leads not yet called"),
        ("fetch", "bring the outcomes and transcripts of placed calls back from Sarvam"),
        ("look", "one look of the engine now, and the split report"),
        ("status", "per arm: assigned, called, connected, dispositions"))}
    for p in cmd.values():
        p.add_argument("--id", default="vani-live-c1", help="the live experiment (default vani-live-c1)")
    cmd["start"].add_argument("--split", type=float, choices=(0.1, 0.5), default=0.1,
                              help="B's share: 0.1 as the template, 0.5 is a demo split")
    cmd["call"].add_argument("--yes-place-real-calls", dest="yes", action="store_true",
                             help="the user approved dialling the allowlisted numbers")
    cmd["call"].add_argument("--max", type=int, default=10, help="at most this many calls this run")
    a = ap.parse_args(argv)
    if not LIVE_ID.fullmatch(a.id):  # a simulated experiment must never get real calls or their rows
        sys.exit(f"{a.id!r}: a live experiment id is vani-live-<lowercase letters, digits, ->, e.g. vani-live-c1")
    if a.cmd == "call" and a.max < 1:
        ap.error("--max must be at least 1")
    conn = connect()
    try:
        if a.cmd == "start":
            start(conn, a.id, a.split, now_ist())
        elif a.cmd == "call":
            place_calls(conn, a.id, a.max, a.yes)
        elif a.cmd == "fetch":
            fetch(conn, a.id, now_ist())
        elif a.cmd == "look":
            look(conn, a.id, now_ist())
        else:
            status(conn, a.id)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
