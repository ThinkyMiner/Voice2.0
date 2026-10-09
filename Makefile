# Human interface (PLAN.md §2.2). Parameters: EXP= SCENARIO= DATASET= SPEED= ACTOR=
.PHONY: setup test profile preflight simulate split-report boundaries validate scenarios live approve api dashboard slide demo

RUN      := uv run
DATASET  ?= bttc
EXP_bttc       := configs/experiments/vani-meeting-v1.yaml
EXP_buyer_logs := configs/experiments/vani-buyer-qty-asks-v1.yaml
EXP      ?= $(EXP_$(DATASET))
SCENARIO ?= win
SPEED    ?= 1
ACTOR    ?= $(USER)
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

simulate split-report api:
	@echo "not built yet (Phase 2)"; exit 1

boundaries validate:
	@echo "not built yet (Phase 3)"; exit 1

scenarios live approve demo:
	@echo "not built yet (Phase 4)"; exit 1

dashboard slide:
	@echo "not built yet (Phase 5)"; exit 1
