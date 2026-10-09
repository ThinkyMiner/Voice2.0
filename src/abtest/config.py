"""Load, validate and freeze configs; resolve variant prompts (PLAN.md §2.3).

Every path, whether passed in or found inside a config, resolves relative to ROOT (an absolute
path stays as is), never the CWD. Only `load_sim` may read configs/sims/.
"""

import hashlib
import json
import re
from pathlib import Path

import yaml

from abtest.contracts import (
    CONFIGS_DIR,
    ROOT,
    AppendPatch,
    ExperimentConfig,
    FrozenConfig,
    FullPatch,
    ReplaceSectionPatch,
    ReplaceTextPatch,
    Scenario,
    SimConfig,
    Variant,
    VariantSpec,
)


def _yaml(path: str | Path):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _count(text: str, sub: str) -> int:
    """Occurrences of `sub` in `text`, overlapping ones included."""
    return len(re.findall(f"(?={re.escape(sub)})", text))


def _read_prompt(v: Variant, path: str) -> str:
    try:  # bytes -> str: no BOM stripping, no newline translation
        return (ROOT / path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"variant {v}: cannot read prompt {path}: {e}") from e


def _once(v: Variant, base: str, what: str, sub: str) -> None:
    if (n := _count(base, sub)) != 1:
        raise ValueError(f"variant {v}: {what} {sub!r} occurs {n} times in the base, need exactly 1")


def _resolve(v: Variant, spec: VariantSpec) -> str:
    p = spec.patch
    if isinstance(p, FullPatch):
        if (p.path is None) == (p.text is None):
            raise ValueError(f"variant {v}: full patch needs exactly one of `path` or `text`")
        if spec.base is not None:
            raise ValueError(f"variant {v}: full patch is a complete prompt, so it takes no `base`")
        return p.text if p.path is None else _read_prompt(v, p.path)
    if spec.base is None:
        raise ValueError(f"variant {v}: no `base` and not a full patch")
    base = _read_prompt(v, spec.base)
    match p:
        case None:
            return base
        case AppendPatch():
            return base + ("" if base.endswith("\n") else "\n") + p.text + "\n"
        case ReplaceTextPatch():
            _once(v, base, "replace_text find", p.find)
            return base.replace(p.find, p.replace, 1)
        case ReplaceSectionPatch():
            _once(v, base, "replace_section start marker", p.start)
            _once(v, base, "replace_section end marker", p.end)
            s, e = base.index(p.start) + len(p.start), base.index(p.end)
            if e < s:
                raise ValueError(f"variant {v}: replace_section end marker {p.end!r} comes before "
                                 f"the end of start marker {p.start!r}")
            return base[:s] + p.text + base[e:]


def _experiment(raw: dict) -> ExperimentConfig:
    cfg = ExperimentConfig.model_validate(raw)
    resolve_prompts(cfg)  # a bad prompt fails at load time
    return cfg


def load_experiment(path: str | Path) -> ExperimentConfig:
    return _experiment(_yaml(path))


def load_sim(path: str | Path) -> SimConfig:
    return SimConfig.model_validate(_yaml(path))


def load_scenario(name_or_path: str | Path) -> Scenario:
    p = Path(name_or_path)
    if len(p.parts) == 1 and not p.suffix:  # bare name like "win"
        p = CONFIGS_DIR / "scenarios" / f"{p}.yaml"
    scenario = Scenario.model_validate(_yaml(p))
    if scenario.name != p.stem:
        raise ValueError(f"scenario name {scenario.name!r} must equal its file stem {p.stem!r}")
    return scenario


def scenario_experiment(scenario: Scenario) -> ExperimentConfig:
    raw = {**_yaml(scenario.experiment), **scenario.overrides}
    if "id" in raw:  # a missing id still fails validation below
        raw["id"] = f"{raw['id']}-{scenario.name}"
    return _experiment(raw)


def resolve_prompts(cfg: ExperimentConfig) -> dict[Variant, str]:
    return {v: _resolve(v, spec) for v, spec in sorted(cfg.variants.items())}


def freeze(cfg: ExperimentConfig) -> FrozenConfig:
    canonical = json.dumps(cfg.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return FrozenConfig(config=cfg, canonical_json=canonical, config_hash=_sha256(canonical),
                        prompt_hash={v: _sha256(t) for v, t in resolve_prompts(cfg).items()})
