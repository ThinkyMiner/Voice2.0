# TEAM.md: Two developers, two Claude Code sessions, one repo

This file splits the work in PLAN.md and PLAN_EXTENSIONS.md between two lanes that run in parallel.

- PLAN.md and PLAN_EXTENSIONS.md still define *what* gets built. This file only decides *who* builds it and how the work merges.
- Every Claude Code session reads this file at the start of each phase or wave.

## 1. Lanes

**Why the split falls here:** the line between the lanes is the contract boundary in `contracts.py`. The statistics code is pure functions over contract models, so it is the one part of the build that does not have to wait for Phase 2.

| | Lane A: Pipeline and integration | Lane B: Stats and presentation |
|---|---|---|
| Developer | Kartik | Second developer |
| Role | Integration owner: merges to main, applies all tags | Opens pull requests; never tags |
| Base files owned | `contracts.py`, `store.py`, `config.py`, `data.py`, `profile.py`, `router.py`, `api.py`, `simulator.py`, `srm.py`, `decision.py`, `runner.py`, `configs/**`, `Makefile`, `pyproject.toml`, `uv.lock`, `.gitignore`, CI config | `boundaries.py`, `power.py`, `metrics.py`, `analysis.py`, `rules.py`, `validation.py`, `dashboard/app.py`, `slide/**`, `README.md`, `docs/**` |
| Extension files owned | `ext/service.py`, `hooks.py`, `security.py`, `approvals.py`, `daemon.py`, `outbox.py`, `notify.py`, `registry.py`, `store_ext.py`; `integrations/**`; `scripts/auto_demo.sh`; `dashboard/pages/1_Autonomy.py` | `ext/judge.py`, `patch_gate.py`, `qa_labeler.py`, `cuped.py`; `dashboard/pages/2_Judge.py`, `3_Variance_Reduction.py`; `scripts/make_fixture_db.py` |
| Tests owned | The tests for its own modules | The tests for its own modules |

**Ownership rules:**
- **Your files only.** A session writes only the files its lane owns.
- **New files** belong to the lane that creates them. Put them under that lane's directories so ownership stays obvious.
- **Makefile exception:** Lane B may edit the recipe lines of its own targets (`boundaries`, `preflight`, `validate`, `dashboard`, `slide`, and later `gate-eval`, `label-qa`, `qa-report`, `qa-audit`). Each such edit goes in its own commit. Lane A resolves any Makefile conflict.

## 2. Contract changes go through Lane A only
The following files are touched by Lane A only:
- `contracts.py`
- `store.py`
- `pyproject.toml` and `uv.lock`

**Why:** every module imports these files. Two sessions editing them at once produces merge conflicts in the one file everything depends on, and worse, silent interface drift between the lanes.

**When Lane B needs a change:**
1. Open a PR or issue titled `contract: <change>`. Include the exact fields or signatures, and say why the current contract is insufficient.
2. Lane A applies it on main, merges, and tells Lane B.
3. Lane B rebases. Until then, Lane B keeps working against the current contract, using a local adapter in its own files.

Changing a contract after its phase is tagged still requires asking, exactly as PLAN.md says.

## 3. Git workflow
- **Protected main.** All changes reach main through PRs, and only Lane A merges.
- **Branch naming:** `lane-a/<phase-or-wave>` and `lane-b/<phase-or-wave>`. Keep one branch per phase or wave.
- **PR description** = the PLAN.md checkpoint block, plus `Rebased onto main at <sha>`.
- **Tags:** Lane A tags main (`phase-N`, `ext-N`) after reviewing the checkpoint. Tags are applied in phase order even if a later phase merges first.
- **Rebasing:** Lane B runs `git fetch && git rebase origin/main` at the start of every phase or wave, and whenever Lane A announces a merge. If a conflict lands in a Lane A file, take main's version (`git checkout --theirs` during the rebase). Never hand-merge contracts.
- **CI** (Lane A sets this up first): `uv sync`, then `uv run pytest -m "not data"` on every PR. After X1, add the golden test.
  - **Why CI matters with two sessions:** each session can be green on its own branch and still break the other's work once merged. CI runs the tests on the merged result.

## 4. Test rule
- Data stays out of git (see §7), so CI has no `data/` directory.
- Tests that need real data get `@pytest.mark.data` and run locally only. Lane A registers the marker in `pyproject.toml`.
- Every other test runs on small synthetic fixtures generated inside the test.
- Lane B's tests must not depend on Lane A's work in progress. Lane B builds against contract models, not against the simulator.

## 5. Sequence and sync points

| Step | Lane A | Lane B | Sync |
|---|---|---|---|
| 1 | Finish **Phase 2** on `lane-a/phase-2`. Add CI and the `data` marker on main first. | **Phase 3** on `lane-b/phase-3`, branched from main at `phase-1`. Validation uses the bootstrap fast path from `data.py`, so no router or simulator is needed. | — |
| 2 | Merge Phase 2 and tag `phase-2`. | Rebase. | — |
| 3 | **Phase 4:** build `decision.py` and `runner.py` against the `Verdict` and `LookResult` contracts. Until `phase-3` lands, call a contract-shaped stub for `rules.evaluate_look`. | Finish Phase 3 and open a PR. | Lane A merges and tags `phase-3`, then replaces its stub with the real rules. |
| 4 | Finish Phase 4: scenarios, then tag `phase-4`. | **Phase 5:** dashboard, slide and docs, developed against `scripts/make_fixture_db.py` (fake rows matching the store schema and PLAN.md §2.9, labelled FIXTURE). Switch to real data once `phase-4` merges. | Lane B rebases after `phase-4`. |
| 5 | Review and merge Phase 5, tag `phase-5`. Run **X0** and **X1** (golden baseline). | Rebase. | — |
| 6 | **Wave 1** (autonomy core). | **Wave 2, standalone parts:** judge client, patch gate, QA labeller, the `extra_guardrails` change in `rules.py`, Judge page. | Lane A wires the gate into proposal-pipeline step 3 with one call, and adds the `qa_risk` injection in `simulator.py` to Lane B's spec. |
| 7 | **Wave 4:** Langfuse registry adapter and `make auto-demo`. | **Wave 3:** CUPED. Then the Wave 4 README and slide changes. | Lane A adds the pre-period attach in `simulator.py` to Lane B's spec. |
| 8 | **Codex review**, areas 2, 3 and 5. | **Codex review**, areas 1 and 4. | Each lane fixes only its own files. Lane A runs the final `make demo`, `make auto-demo` and golden test, then tags `v1.0`. |

**Cut order if time runs short:** same as PLAN_EXTENSIONS.md. Waves 1–2 stay; the base plan through Phase 5 is never cut.

## 6. Cross-lane requests
When a lane needs something in a file the other lane owns:
1. Write the exact spec: function signature, inputs, outputs, and a test the other lane can paste.
2. Put it in a PR or issue titled `request: <file>: <change>`.
3. Keep working against the agreed signature.

Known requests in advance:
- **Wave 2:** Lane B → Lane A. Inject `qa_risk` into `simulator.py`, and call `patch_gate.evaluate(base_prompt, b_prompt, diff) -> GateResult` from `hooks.py`.
- **Wave 3:** Lane B → Lane A. Attach seller pre-period history in `simulator.py` when CUPED is on.
- **Wave 1:** Lane A → Lane B. Any read helpers the Autonomy page needs from the new extension tables. Lane A owns that page, but it reads tables whose schema Lane A defines.

## 7. Repo hygiene (Lane A checks this before the first push)
These must all be ignored by git, and must never have been committed:
- `data/`
- the SQLite store
- `.env`
- `reports/qa_audit_sample.csv`
- any file containing real call rows

Check with `git check-ignore -v`, and with `git log --all -- data/`, which must return nothing.

**Why it matters:** the source data is marked internal-only, and generated reports can carry real rows. Ignoring a file now does not remove it from history if it was committed before.

## 8. Kickoff prompts

**Lane A** (Kartik's existing session):
> Read TEAM.md. You are Lane A and the integration owner. Lane B is doing Phase 3 in parallel. My Phase 2 work in progress is on branch `lane-a/phase-2`. First, on main: add the CI workflow and register the `data` pytest marker, then push. Then rebase `lane-a/phase-2` onto main, finish Phase 2 as PLAN.md says, and stop at its checkpoint. Touch only Lane A files.

**Lane B** (second developer's session):
> Read PLAN.md, PLAN_EXTENSIONS.md and TEAM.md. You are Lane B. Do not write code yet. Audit first: run `git tag` and `uv run pytest -m "not data"`, list `data/`, and confirm that the Phase 1 contracts define everything Phase 3 needs (LookResult, Verdict, LeadOutcome, the boundaries/analysis/rules/validation signatures). List any contract requests, then stop for my go. After that, do Phase 3 on branch `lane-b/phase-3`, end with a PR whose description is the checkpoint block, and do not tag.