"""The platform's static site (src/abtest/ext/web): files present, no external scripts, every ES module
import resolvable, and demo.json in the exact API shapes with nothing real in it."""

import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from abtest.contracts import Decision
from abtest.ext.models import (AgreementReport, CallAudit, Cycle, Fix, PromptVersion, RootCause, Rubric,
                               Transcript)

WEB = Path(__file__).resolve().parents[2] / "src" / "abtest" / "ext" / "web"
PAGES = ["home", "audit", "calls", "issues", "fixes", "experiments", "rubrics", "prompts", "settings", "docs"]


@pytest.fixture(scope="module")
def demo() -> dict:
    return json.loads((WEB / "demo.json").read_text(encoding="utf-8"))


def test_files_exist_and_index_references_them():
    for name in ["index.html", "app.css", "app.js", "api.js", "ui.js", "viz.js", "demo.json",
                 *(f"pages/{p}.js" for p in PAGES)]:
        assert (WEB / name).is_file(), name
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<link rel="stylesheet" href="app\.css">', html)
    assert re.search(r'<script type="module" src="app\.js"></script>', html)


def test_no_external_scripts_and_only_google_fonts_styles():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for src in re.findall(r"<script[^>]*\ssrc=\"([^\"]+)\"", html):
        assert "//" not in src and not src.startswith("http"), src
    for href in re.findall(r"<link[^>]*rel=\"stylesheet\"[^>]*href=\"([^\"]+)\"", html):
        assert not re.match(r"(https?:)?//", href) or href.startswith("https://fonts.googleapis.com/"), href
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert "@import" not in css and not re.search(r"url\((['\"]?)(https?:)?//", css)


def test_every_module_import_resolves():
    for js in [*WEB.glob("*.js"), *WEB.glob("pages/*.js")]:
        for spec in re.findall(r"^import [^;]*? from \"([^\"]+)\";", js.read_text(encoding="utf-8"), re.M):
            assert spec.startswith("."), f"{js.name}: bare or remote import {spec}"
            assert (js.parent / spec).resolve().is_file(), f"{js.name}: {spec} is missing"


def test_demo_json_has_the_api_shapes(demo):
    ov = demo["/api/overview"]
    assert ov["simulated_traffic"] is True and set(ov["kpis"]) >= {"calls_total", "calls_audited", "fatal_rate",
                                                                 "fatal_vs_rest_accuracy", "per_week_gained"}
    PromptVersion.model_validate(ov["production"])
    cycles = [Cycle.model_validate(c) for c in ov["cycles"]]
    assert ov["production"]["version"] == demo["/api/prompts"]["production"]
    for p in demo["/api/prompts"]["items"]:
        PromptVersion.model_validate(p)
    for r in demo["/api/rubrics"]["items"]:
        Rubric.model_validate(r)
    for key, a in demo.items():
        if key.startswith("/api/audit?"):
            if a["agreement"]:
                AgreementReport.model_validate(a["agreement"])
        if key.startswith("/api/issues?"):
            for rc in a["items"]:
                RootCause.model_validate(rc)
    for f in demo["/api/fixes"]["items"]:
        Fix.model_validate(f)
        assert demo[f"/api/fixes/{f['id']}"]["root_cause"]["id"] == f["root_cause_id"]
    for x in demo["/api/experiments"]["items"]:
        d = demo[f"/api/experiments/{x['experiment_id']}"]
        assert d["assumed_effect"] and x["simulated"] is True
        for row in d["decisions"]:
            Decision.model_validate(row)
    for c in cycles:
        assert c.experiment_id is None or f"/api/experiments/{c.experiment_id}" in demo
        assert c.fix_id is None or f"/api/fixes/{c.fix_id}" in demo


def test_demo_calls_are_invented_and_self_consistent(demo):
    items = demo["/api/calls"]["items"]
    assert items and demo["/api/calls"]["total"] == len(items)
    for c in items:
        assert c["call_id"].startswith("sample-")
        d = demo[f"/api/calls/{c['call_id']}"]
        t, a = Transcript.model_validate(d["transcript"]), CallAudit.model_validate(d["audit"])
        assert t.provider == "fixture" and t.recording_url is None
        assert a.overall == c["overall"]
        for v in a.verdicts:
            assert v.rubric_id in d["rubrics"]
            for e in v.evidence:
                assert e.quote in t.turns[e.turn].text, (c["call_id"], v.rubric_id)
    blob = json.dumps({k: v for k, v in demo.items() if not k.endswith("/text")}, ensure_ascii=False)
    assert not re.search(r"(?<!\d)(\+91[\s-]?)?[6-9]\d{9}(?!\d)", blob), "a phone-number-like digit run"
    assert "amazonaws" not in blob and "s3://" not in blob


def test_docs_links_land_and_its_copied_facts_match_the_code():
    """pages/docs.js hard-codes the gate checks, the built-in rubrics and the headline's planned boundaries:
    a change in the code must show up here. Every #/docs/<id> link must land on a section."""
    from abtest import boundaries, config, power
    from abtest.ext import improve, rubrics
    js = (WEB / "pages" / "docs.js").read_text(encoding="utf-8")
    ids = re.findall(r'\["([a-z]+)", "[^"]+"\]', re.search(r"SECTIONS = \[(.*?)\n\];", js, re.S)[1])
    assert ids and sorted(re.findall(r'sec\("([a-z]+)"', js)) == sorted(ids)
    assert set(re.findall(r"#/docs/([a-z0-9-]+)", js)) <= set(ids)
    for cid, blocking, question in improve.GATE_CHECKS:
        assert f'["{cid}", {str(blocking).lower()}, "{question}"]' in js, cid
    built = rubrics.builtin()
    assert len(built) == len(re.findall(r'\["[^"]+", "(?:prompt|flow|asr|knowledge|platform)"', js))
    for r in built:
        assert f'["{r.name}", "{r.root_type}"' + (", true]" if r.needs_audio else "]") in js, r.name
    cfg = config.load_experiment("configs/experiments/vani-meeting-v1.yaml")
    t = power.planned_info_fractions(cfg)
    plan = {k: [float(x) for x in re.search(rf"{k}: \[([^\]]+)\]", js)[1].split(",")] for k in ("win", "harm")}
    assert plan["win"] == [round(x, 3) for x in boundaries.win_boundaries(t, cfg.alpha_win)]
    assert plan["harm"] == [-round(x, 3) for x in boundaries.harm_boundaries(t, cfg.alpha_harm)]


CASES = [  # (Prompt A line, Prompt B line): the redline must read as B with deletions hidden, as A with insertions hidden
    ("Use an appropriate unit from options ; never ask without a unit.",
     "Use an appropriate unit from options in every question (“kitne piece?”); never ask without a unit."),
    ("On a valid response, update quantity and move on without confirmation.",
     "On a valid response, update quantity and move on. If the buyer gave a bare number, read it back."),
    ("quantity = 3; each specification = 2", "quantity = 2; each specification = 2"),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_redline_word_diff_reads_as_both_prompts():
    script = ("const { wordDiff } = await import(process.argv[1]);"
              "for (const [a, b] of JSON.parse(process.argv[2])) console.log(JSON.stringify(String(wordDiff(a, b))));")
    out = subprocess.run(["node", "--input-type=module", "-e", script, (WEB / "pages" / "fixes.js").as_uri(), json.dumps(CASES)],
                         capture_output=True, text=True, timeout=30, check=True).stdout.splitlines()
    unescape = lambda s: html.unescape(re.sub(r"</?(del|ins)>", "", s))  # noqa: E731
    squash = lambda s: " ".join(s.split())  # noqa: E731
    for (a, b), line in zip(CASES, out, strict=True):
        red = json.loads(line)
        assert squash(unescape(re.sub(r"<del>.*?</del>", "", red))) == squash(b)
        assert squash(unescape(re.sub(r"<ins>.*?</ins>", "", red))) == squash(a)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_demo_prompt_versions_rebuild_to_their_registry_hash(demo):
    """Demo mode shows v2+ as Prompt A plus the fix's edits (api.applyEdits, mirroring improve._apply)."""
    script = """
const { applyEdits } = await import(process.argv[1]);
const { createHash } = await import("node:crypto");
const demo = JSON.parse((await import("node:fs")).readFileSync(process.argv[2], "utf8"));
const text = (v) => { const p = demo["/api/prompts"].items.find((x) => x.version === v);
  if (!p.fix_id) return demo[`/api/prompts/${v}/text`];
  const f = demo[`/api/fixes/${p.fix_id}`].fix; return applyEdits(text(f.base_version), f.edits); };
for (const p of demo["/api/prompts"].items) console.log(p.version, createHash("sha256").update(text(p.version)).digest("hex") === p.sha256);
"""
    out = subprocess.run(["node", "--input-type=module", "-e", script, (WEB / "api.js").as_uri(), str(WEB / "demo.json")],
                         capture_output=True, text=True, timeout=30, check=True).stdout.split()
    assert out and all(ok == "true" for ok in out[1::2]), out
