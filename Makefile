# Human interface (PLAN.md §2.2). Parameters: EXP= SCENARIO= DATASET= SPEED= ACTOR= SEEDS=
.PHONY: setup test profile preflight simulate split-report boundaries validate scenarios live approve api dashboard slide demo

RUN      := uv run
DATASET  ?= bttc
EXP_bttc       := configs/experiments/vani-meeting-v1.yaml
EXP_buyer_logs := configs/experiments/vani-buyer-qty-asks-v1.yaml
EXP      ?= $(EXP_$(DATASET))
SCENARIO ?= win
SPEED    ?= 1
ACTOR    ?= $(USER)
PORT     ?= 8000
SEEDS    ?= 20
# The approval scenario's experiment id (make approve's default EXP).
APPROVAL_EXP := vani-meeting-v1-approval
PROMPT_DOCX := data/Sarvam Prompt - Buyer Side VANI.docx

setup:  ## install deps; (re)extract the base prompt if it is missing
	uv sync
	@test -f prompts/base_buyer_vani.txt || $(RUN) python -c "import docx; d = docx.Document(\"$(PROMPT_DOCX)\"); open(\"prompts/base_buyer_vani.txt\", \"w\", encoding=\"utf-8\", newline=\"\\n\").write(\"\\n\".join(p.text for p in d.paragraphs) + \"\\n\")"

test:
	$(RUN) pytest -q

profile:  ## make profile DATASET=bttc|buyer_logs
	$(RUN) python -m abtest.profile --dataset $(DATASET) --exp $(EXP_$(DATASET))

preflight:  ## make preflight EXP=configs/experiments/<id>.yaml
	$(RUN) python -m abtest.power $(EXP)

simulate:  ## make simulate SCENARIO=win  (batch: start, looks, decision, truth report)
	$(RUN) python -m abtest.runner $(SCENARIO)

live:  ## make live SCENARIO=harm SPEED=3  (SPEED = wall seconds per simulated day)
	$(RUN) python -m abtest.runner $(SCENARIO) --speed $(SPEED)

scenarios:  ## every scenario at seeds 1..SEEDS, then each at its pinned seed -> reports/scenarios.md
	$(RUN) python -m abtest.runner --sweep --seeds $(SEEDS)

approve:  ## make approve EXP=<experiment id> ACTOR=<name> [REJECT=1]; EXP defaults to the approval scenario
	$(RUN) python -m abtest.decision $(if $(filter %.yaml,$(EXP)),$(APPROVAL_EXP),$(EXP)) --actor $(ACTOR) $(if $(REJECT),--reject)

demo:  ## the three required scenarios end to end, then the human approval gate
	@for s in win harm inconclusive; do \
	  $(MAKE) --no-print-directory simulate SCENARIO=$$s && $(MAKE) --no-print-directory split-report SCENARIO=$$s || exit 1; \
	done
	$(MAKE) --no-print-directory simulate SCENARIO=approval
	$(MAKE) --no-print-directory approve EXP=$(APPROVAL_EXP) ACTOR=$(ACTOR)

split-report:  ## make split-report SCENARIO=win
	$(RUN) python -m abtest.srm $(SCENARIO)

api:  ## GET http://localhost:$(PORT)/assign?experiment_id=&lead_id=
	$(RUN) uvicorn abtest.api:app --port $(PORT)

boundaries:  ## make boundaries EXP=...  (5- and 7-look boundaries, reference + Monte Carlo checks)
	$(RUN) python -m abtest.boundaries $(EXP)

validate:  ## make validate EXP=...  (A/A, power and naive-peeking runs -> reports/validation.md)
	$(RUN) python -m abtest.validation $(EXP)

dashboard:  ## http://localhost:8501 (reads the store; auto-refreshes while make live runs)
	$(RUN) streamlit run dashboard/app.py --server.port 8501 --server.headless true

slide:  ## slide/one_slide.pptx, plus its chart PNG in reports/
	$(RUN) python slide/build_slide.py

# --- The loop (docs/loop_api.md). SOURCE = production | audited. ARGS passes flags through, e.g.
# make listen ARGS=--yes-send-real-audio, make loop ARGS="--until judge --yes-send-real-transcripts".
# The --yes-* flags send real customer calls to the model provider: only on the user's go-ahead.
.PHONY: platform listen judge agreement loop loop-status
SOURCE ?= production

platform:  ## the Slow & Steady platform (API + web): http://127.0.0.1:8600
	$(RUN) uvicorn abtest.ext.service:app --host 127.0.0.1 --port 8600

listen:  ## make listen SOURCE=production|audited ARGS=--yes-send-real-audio  (recordings -> transcripts)
	$(RUN) python -m abtest.ext.listen --source $(SOURCE) $(ARGS)

judge:  ## make judge SOURCE=production|audited ARGS=--yes-send-real-transcripts  (grade every call)
	$(RUN) python -m abtest.ext.judge audit --source $(SOURCE) $(ARGS)

agreement:  ## the judge vs IndiaMART's human QA grades -> reports/judge_agreement.md
	$(RUN) python -m abtest.ext.judge agreement $(ARGS)

loop:  ## advance the loop's current cycle: make loop ARGS="--until judge" (stops at approvals and go-aheads)
	$(RUN) python -m abtest.ext.loop advance $(ARGS)

loop-status:  ## the prompt registry, the cycles and the next action
	$(RUN) python -m abtest.ext.loop status
