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

boundaries validate:
	@echo "not built yet (Phase 3)"; exit 1

dashboard slide:
	@echo "not built yet (Phase 5)"; exit 1
