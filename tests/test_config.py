import hashlib
from datetime import date
import json

import pytest
import yaml
from pydantic import ValidationError

from abtest import config
from abtest.contracts import BASE_PROMPT, CONFIGS_DIR, ROOT, ExperimentConfig, Scenario

MEETING = CONFIGS_DIR / "experiments" / "vani-meeting-v1.yaml"
BUYER = CONFIGS_DIR / "experiments" / "vani-buyer-qty-asks-v1.yaml"
SCENARIOS = ["win", "approval", "harm", "inconclusive", "guardrail_block", "srm_fault"]
LOADERS = {"experiments": config.load_experiment, "sims": config.load_sim,
           "scenarios": config.load_scenario}


def _raw(path=MEETING) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write(tmp_path, raw: dict, name="exp.yaml", text: str | None = None):
    p = tmp_path / name
    p.write_text(text if text is not None else yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return p


def _load_with(tmp_path, **variants):
    """Meeting config with the given variants, loaded through load_experiment from a tmp file."""
    return config.load_experiment(_write(tmp_path, {**_raw(), "variants": variants}))


@pytest.fixture
def base(tmp_path):
    p = tmp_path / "base.txt"
    p.write_bytes("﻿Hello {% if x %}X{% endif %}\n<S>old body</S>\ntail".encode())
    return p


# --- every config loads ---------------------------------------------------------------

@pytest.mark.parametrize("path", sorted(CONFIGS_DIR.rglob("*.yaml")), ids=lambda p: p.stem)
def test_every_yaml_loads(path):
    LOADERS[path.parent.name](path)


def test_scenarios():
    assert set(SCENARIOS) <= {p.stem for p in (CONFIGS_DIR / "scenarios").glob("*.yaml")}
    loaded = {n: config.load_scenario(n) for n in SCENARIOS}
    seeds = {n: s.seed for n, s in loaded.items()}
    assert seeds["approval"] == seeds["win"]  # same traffic: the approval demo is the win run, gated
    others = [v for n, v in seeds.items() if n != "approval"]
    assert len(set(others)) == len(others)
    exps = {n: config.scenario_experiment(s) for n, s in loaded.items()}
    assert {n: e.id for n, e in exps.items()} == {n: f"vani-meeting-v1-{n}" for n in SCENARIOS}
    assert exps["win"].promotion == "auto" and exps["approval"].promotion == "approval"
    for s in loaded.values():
        config.load_sim(s.sim)
    assert config.load_scenario(CONFIGS_DIR / "scenarios" / "win.yaml") == loaded["win"]


def test_paths_resolve_from_root_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = config.load_scenario("win")
    config.load_sim(s.sim)
    assert config.scenario_experiment(s) == config.scenario_experiment(
        config.load_scenario("configs/scenarios/win.yaml"))


def test_scenario_name_must_match_stem(tmp_path):
    p = _write(tmp_path, {"name": "other", "experiment": "x", "sim": "y", "seed": 1}, "win.yaml")
    with pytest.raises(ValueError, match="stem"):
        config.load_scenario(p)


def test_sim_from_alias():
    sim = config.load_sim(CONFIGS_DIR / "sims" / "srm_fault.yaml")
    assert sim.reference.from_ == date(2026, 4, 1) and sim.fault.drop_b_exposure_rate == 0.15


# --- hashing --------------------------------------------------------------------------

def _reversed(x):
    return {k: _reversed(x[k]) for k in reversed(list(x))} if isinstance(x, dict) else x


@pytest.mark.parametrize("path", [MEETING, BUYER], ids=lambda p: p.stem)
def test_hash_ignores_key_order_comments_whitespace(tmp_path, path):
    text = "# a comment\n\n" + yaml.safe_dump(_reversed(_raw(path)), sort_keys=False, indent=4,
                                               default_flow_style=False, width=10**6).replace("\n", "   \n\n")
    other = config.load_experiment(_write(tmp_path, {}, text=text))
    assert config.freeze(other).config_hash == config.freeze(config.load_experiment(path)).config_hash


EDITS = [
    ("id", "vani-meeting-v2"), ("dataset", "buyer_logs"), ("salt", "7f3a92"),
    ("allocation", {"A": 0.8, "B": 0.2}), ("window.days", 21), ("window.looks", 5),
    ("maturation_days", 3), ("baseline_p", 0.3), ("primary.name", "mf"),
    ("primary.dispositions", ["Meeting Fixed", "Callback"]), ("primary.mde_rel", 0.2),
    ("primary.better", "decrease"), ("secondary.margin_rel", 0.05), ("secondary.column", "dur"),
    ("secondary.worse_if", "decrease"), ("variants.B.patch.text", "Offer three slots."),
    ("variants.A.patch", {"mode": "append", "text": "x"}), ("alpha_win", 0.02),
    ("alpha_harm", 0.04), ("power", 0.9), ("min_info_first_look", 0.3),
    ("srm_p_threshold", 0.01), ("promotion", "auto"), ("exclude_leads_with_calls_gt", 10),
]


@pytest.mark.parametrize("key,value", EDITS, ids=[k for k, _ in EDITS])
def test_any_single_value_change_changes_hash(tmp_path, key, value):
    raw = _raw()
    *parents, leaf = key.split(".")
    node = raw
    for k in parents:
        node = node[k]
    assert node.get(leaf) != value
    node[leaf] = value
    assert (config.freeze(config.load_experiment(_write(tmp_path, raw))).config_hash
            != config.freeze(config.load_experiment(MEETING)).config_hash)


@pytest.mark.parametrize("path", [MEETING, BUYER], ids=lambda p: p.stem)
def test_freeze(path):
    cfg = config.load_experiment(path)
    fr = config.freeze(cfg)
    assert fr == config.freeze(cfg) == config.freeze(config.load_experiment(path))
    assert fr.config_hash == hashlib.sha256(fr.canonical_json.encode("utf-8")).hexdigest()
    assert fr.prompt_hash == {v: hashlib.sha256(t.encode("utf-8")).hexdigest()
                              for v, t in config.resolve_prompts(cfg).items()}
    assert fr.prompt_hash["A"] != fr.prompt_hash["B"]
    assert ExperimentConfig.model_validate(json.loads(fr.canonical_json)) == cfg


def test_frozen_and_strict(tmp_path):
    cfg = config.load_experiment(MEETING)
    with pytest.raises(ValidationError):
        cfg.salt = "x"
    raw = _raw()
    raw["alpah_win"] = raw.pop("alpha_win")
    with pytest.raises(ValueError, match="alpah_win"):
        config.load_experiment(_write(tmp_path, raw))
    bad = Scenario(name="bad", experiment=str(MEETING.relative_to(ROOT)), sim="configs/sims/win.yaml",
                   seed=1, overrides={"alpah_win": 0.01})
    with pytest.raises(ValueError, match="alpah_win"):
        config.scenario_experiment(bad)


# --- patch modes ----------------------------------------------------------------------

def test_real_prompts_byte_exact():
    raw = BASE_PROMPT.read_bytes().decode("utf-8")
    for path in (MEETING, BUYER):
        assert config.resolve_prompts(config.load_experiment(path))["A"] == raw
    b = config.resolve_prompts(config.load_experiment(BUYER))["B"]
    assert "quantity = 2" in b and "quantity = 3" not in b
    assert b == raw.replace("quantity = 3", "quantity = 2")
    m = config.resolve_prompts(config.load_experiment(MEETING))["B"]
    assert m.startswith(raw) and m.count("﻿") == raw.count("﻿") > 0


def test_append(tmp_path, base):
    a = {"base": str(base)}
    b = {"base": str(base), "patch": {"mode": "append", "text": "MORE"}}
    raw = base.read_bytes().decode()
    assert config.resolve_prompts(_load_with(tmp_path, A=a, B=b))["B"] == raw + "\nMORE\n"
    base.write_bytes(raw.encode() + b"\n")
    assert config.resolve_prompts(_load_with(tmp_path, A=a, B=b))["B"] == raw + "\nMORE\n"


@pytest.mark.parametrize("find,count", [("absent", 0), ("{%", 2), ("{% if x %}", 1)])
def test_replace_text(tmp_path, base, find, count):
    b = {"base": str(base), "patch": {"mode": "replace_text", "find": find, "replace": "R"}}
    if count != 1:
        with pytest.raises(ValueError, match=f"variant B.*occurs {count} times"):
            _load_with(tmp_path, A={"base": str(base)}, B=b)
    else:
        out = config.resolve_prompts(_load_with(tmp_path, A={"base": str(base)}, B=b))["B"]
        assert out == base.read_bytes().decode().replace(find, "R")


def test_replace_section(tmp_path, base):
    def b(start, end):
        return {"base": str(base), "patch": {"mode": "replace_section", "start": start, "end": end,
                                             "text": "new"}}
    out = config.resolve_prompts(_load_with(tmp_path, A={"base": str(base)}, B=b("<S>", "</S>")))["B"]
    assert out == "﻿Hello {% if x %}X{% endif %}\n<S>new</S>\ntail"
    for start, end, msg in [("<Q>", "</S>", "occurs 0"), ("<S>", "{%", "occurs 2"),
                            ("</S>", "<S>", "before")]:
        with pytest.raises(ValueError, match=f"variant B.*{msg}"):
            _load_with(tmp_path, A={"base": str(base)}, B=b(start, end))


def test_full(tmp_path, base):
    a = {"base": str(base)}
    full = tmp_path / "full.txt"
    full.write_bytes("﻿Whole prompt\r\n".encode())
    by_path = _load_with(tmp_path, A=a, B={"patch": {"mode": "full", "path": str(full)}})
    assert config.resolve_prompts(by_path)["B"] == "﻿Whole prompt\r\n"
    by_text = _load_with(tmp_path, A=a, B={"patch": {"mode": "full", "text": "T"}})
    assert config.resolve_prompts(by_text)["B"] == "T"
    for patch in ({"mode": "full", "path": str(full), "text": "T"}, {"mode": "full"}):
        with pytest.raises(ValueError, match="variant B.*exactly one"):
            _load_with(tmp_path, A=a, B={"patch": patch})


def test_bad_variants(tmp_path, base):
    a = {"base": str(base)}
    with pytest.raises(ValueError, match="variant B.*cannot read"):
        _load_with(tmp_path, A=a, B={"base": str(tmp_path / "missing.txt")})
    with pytest.raises(ValueError, match="variant B.*no `base`"):
        _load_with(tmp_path, A=a, B={"patch": {"mode": "append", "text": "x"}})
    with pytest.raises(ValueError, match="variant B.*takes no `base`"):
        _load_with(tmp_path, A=a, B={"base": str(base), "patch": {"mode": "full", "text": "x"}})
