"""Self-improving loop (PLAN_LOOP.md §L2.10, §L9): generations, the latest candidates, spend and the guard log.

Reads the loop DB read-only (sqlite3 mode=ro) and never writes. The DB defaults to
data/derived/loop/loop.db; set ABT_LOOP_DB or use the sidebar for another (`make loop-demo` writes demo.db).
The page shows aggregates and the patches themselves, never a transcript.
"""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd
import streamlit as st

import common as c
from abtest.loop import controller, store_loop
from abtest.loop.contracts import DEFAULT_LOOP_CONFIG, LOOP_DATA_DIR

st.set_page_config(page_title="Loop", page_icon=":material/autorenew:", layout="wide")
c.style()
c.html_block('<div class="ab-banner"><b>Self-improving loop: simulated traffic for the A/B; real Sarvam models '
             'for scoring, proposing and bot tests</b>. A demo run uses the offline stub provider instead of '
             'Sarvam. The A/B numbers say nothing about real business impact.</div>')
st.title("Self-improving loop: does each generation beat the last?")
st.markdown("One generation diagnoses the production prompt, proposes patches, gates them, tests them against "
            "simulated buyers, runs the picked one through the real A/B engine on simulated traffic, and promotes "
            "it only if the engine says so. A **rubric** guardrail can stop a win that makes the calls worse.")


def connect_ro(path: Path) -> sqlite3.Connection | None:
    """A read-only connection, or None if the file is missing."""
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def pair(x) -> str:
    return "–" if not x else f"[{x[0]:+.3f}, {x[1]:+.3f}]"


def num(x, spec: str = "+.3f") -> str:
    return "–" if x is None else format(x, spec)


def generations_frame(gens) -> pd.DataFrame:
    return pd.DataFrame([{
        "Gen": g.generation, "State": g.state, "Outcome": g.terminal or "–", "Base prompt": g.base_prompt_hash[:10],
        "Pick": g.pick or "–", "Experiment": g.experiment_id or "–", "Spend (INR)": round(g.spend_inr, 2),
        "Started": g.started_ts.strftime("%Y-%m-%d %H:%M"), "Updated": g.updated_ts.strftime("%Y-%m-%d %H:%M")}
        for g in gens])


def candidates_frame(cands) -> pd.DataFrame:
    return pd.DataFrame([{
        "Candidate": x.candidate_id, "Status": x.status, "Mode": x.patch.mode, "Targets": ", ".join(x.targets) or "–",
        "Pre-checks": "ok" if all(x.prechecks.values()) else "failed: " + ", ".join(
            k for k, ok in x.prechecks.items() if not ok),
        "Gate (max p)": num(max(x.gate.values()) if x.gate else None, ".2f"),
        "Bot-test score": num(x.bot_test_score), "CI low": num(x.bot_test_ci[0] if x.bot_test_ci else None),
        "CI high": num(x.bot_test_ci[1] if x.bot_test_ci else None)} for x in cands])


def patch_text(x) -> str:
    p = x.patch
    if p.mode == "append":
        return f"APPEND\n{p.text}"
    if p.mode == "replace_text":
        return f"FIND\n{p.find}\n\nREPLACE WITH\n{p.replace}"
    if p.mode == "replace_section":
        return f"BETWEEN\n{p.start}\nAND\n{p.end}\n\nPUT\n{p.text}"
    return "FULL PROMPT"


def guard_frame(conn: sqlite3.Connection, experiment_id: str) -> pd.DataFrame:
    """One row per look of the experiment, from loop_guard_log (payload keys per loop/live_ab.py)."""
    try:
        rows = conn.execute("SELECT look, payload FROM loop_guard_log WHERE experiment_id = ? ORDER BY look",
                            (experiment_id,)).fetchall()
    except sqlite3.Error:
        return pd.DataFrame()
    out = []
    for look, payload in rows:
        p = json.loads(payload)
        base, final = p.get("base_verdict") or {}, p.get("final_verdict") or {}
        rub, tpl = p.get("rubric_guardrail") or {}, p.get("template_guardrail") or {}
        out.append({
            "Look": look, "Engine said": base.get("decision", "–"), "Decision": final.get("decision", "–"),
            "Decided by": "rubric guardrail" if p.get("rubric_decided") else "engine",
            "rubric_risk delta (B - A)": num(rub.get("delta")), "rubric_risk RCI": pair(rub.get("rci")),
            "Margin": num(rub.get("margin"), ".3f"), "Template guardrail delta": num(tpl.get("delta")),
            "Template guardrail RCI": pair(tpl.get("rci")), "Reason": final.get("reason", "")})
    return pd.DataFrame(out)


# --- sidebar: which DB and config ---------------------------------------------------------

default_db = os.environ.get("ABT_LOOP_DB") or str(LOOP_DATA_DIR / "loop.db")
db_path = Path(st.sidebar.text_input("Loop database", value=default_db, key="db")).expanduser()
cfg_path = Path(st.sidebar.text_input("Loop config (for the spend cap)", value=str(DEFAULT_LOOP_CONFIG),
                                      key="cfg")).expanduser()

conn = connect_ro(db_path)
if conn is None:
    st.info(f"No loop database at {db_path}. Run `make loop-demo` (offline) or `make loop-run`, then reload.")
    st.stop()

with closing(conn):
    try:
        loop_ids = [r[0] for r in conn.execute("SELECT DISTINCT loop_id FROM loop_generations ORDER BY loop_id")]
    except sqlite3.Error as e:
        st.error(f"{db_path} is not a loop database: {e}")
        st.stop()
    if not loop_ids:
        st.info("The database has no generations yet. Run `make loop-demo` or `make loop-run`, then reload.")
        st.stop()
    loop_id = st.sidebar.selectbox("Loop", loop_ids, key="loop_id") if len(loop_ids) > 1 else loop_ids[0]

    gens = store_loop.get_generations(conn, loop_id)
    cands = store_loop.get_candidates(conn, loop_id)
    spent = store_loop.total_spend(conn)
    latest = gens[-1]
    live = [g for g in gens if g.experiment_id]
    guard = guard_frame(conn, live[-1].experiment_id) if live else pd.DataFrame()

try:
    from abtest.loop import sarvam
    cap = sarvam.load_loop_config(cfg_path).caps.max_spend_inr
except Exception:  # noqa: BLE001 - the cap is only a gauge; a bad config path must not hide the data
    cap = None

paused = controller.is_paused()
promoted = sum(g.terminal == "PROMOTED" for g in gens)
kpis = [("Generations", str(len(gens)), f"latest: {latest.state}, outcome {latest.terminal or '–'}"),
        ("Promoted", f"{promoted} of {len(gens)}", "decided by the A/B engine on simulated traffic"),
        ("Spend", f"INR {spent:,.2f}", f"of the INR {cap:,.2f} cap" if cap is not None else "cap unknown"),
        ("Loop", "PAUSED" if paused else "running", "ABT_LOOP_PAUSE or the PAUSE flag" if paused else "no pause flag")]
c.html_block('<div class="ab-kpis">' + "".join(
    f'<div class="ab-kpi"><div class="l">{c.esc(label)}</div><div class="v">{c.esc(value)}</div>'
    f'<div class="s">{c.esc(sub)}</div></div>' for label, value, sub in kpis) + '</div>')
if cap:
    st.progress(min(1.0, spent / cap), text=f"Spend INR {spent:,.2f} of INR {cap:,.2f}")

st.subheader("Generations")
st.dataframe(generations_frame(gens), hide_index=True, width="stretch")
st.caption("State is where the generation stopped; outcome is how it ended (the A/B engine's terminal state, "
           "NO_CANDIDATE, or SPEND_CAP). A PROMOTED generation's prompt is the next generation's base.")

st.subheader(f"Generation {latest.generation}: candidates")
latest_cands = [x for x in cands if x.generation == latest.generation]
if not latest_cands:
    st.info("This generation proposed no candidate yet.")
else:
    st.dataframe(candidates_frame(latest_cands), hide_index=True, width="stretch")
    st.caption("Bot-test score is the paired mean rubric_score gain over the base prompt on the same simulated "
               "buyers, with a bootstrap 95% CI. It ranks candidates; it never promotes one. Gate (max p) is the "
               "highest blocking-question probability.")
    for x in latest_cands:
        with st.expander(f"Patch of {x.candidate_id} ({x.status})"):
            st.code(patch_text(x), language=None)

st.subheader("Guard log of the latest experiment")
if live:
    st.caption(f"Experiment {live[-1].experiment_id} (generation {live[-1].generation}): at every look the "
               f"engine's verdict, then the rubric_risk guardrail's.")
    if guard.empty:
        st.info("No looks were logged for this experiment.")
    else:
        st.dataframe(guard, hide_index=True, width="stretch")
else:
    st.info("No generation has reached the live A/B yet.")
