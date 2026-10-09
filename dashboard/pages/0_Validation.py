"""Validation proof pack (PLAN.md §2.10, §2.11): renders what `make validate` wrote to reports/."""

import re
from datetime import datetime

import streamlit as st

import common as c
from abtest import contracts

st.set_page_config(page_title="Validation", page_icon=":material/fact_check:", layout="wide")
c.style()
c.banner([(e.cfg.dataset, c.scale(e.scenario)) for e in c.load_experiments()])
st.title("Validation: can the engine's calls be trusted?")
st.markdown("A false win ships a prompt that does nothing (or harm) to every call; a missed win leaves meetings "
            "on the table. These runs measure both on replayed data with a known true effect: **A/A runs** "
            "(B identical to A) give the false-win rate with sequential looks versus naive peeking at "
            "z = 1.96, **power runs** (true lift = the MDE) give how often a real win is caught, and the "
            "**stop-look** distribution shows how early decisions arrive.")

reports = contracts.REPORTS_DIR
md = reports / "validation.md"
charts = [p for p in (reports / "validation_false_win.png", reports / "validation_stop_looks.png") if p.exists()]
if not md.exists() and not charts:
    st.info("No validation report yet. Run `make validate` to write reports/validation.md with "
            "reports/validation_false_win.png and reports/validation_stop_looks.png, then reload this page.")
    st.stop()
if md.exists():
    st.caption(f"reports/validation.md, written {datetime.fromtimestamp(md.stat().st_mtime):%d %b %Y %H:%M}.")
for col, png in zip(st.columns([2, 3] if len(charts) == 2 else 1) if charts else [], charts):  # stop looks is wide
    col.image(str(png), width="stretch")
if md.exists():
    # As written, minus its image links: the charts are shown above (relative links do not resolve here).
    st.markdown(re.sub(r"^!\[[^\]]*\]\([^)]*\)\s*$", "", md.read_text(encoding="utf-8"), flags=re.M))

