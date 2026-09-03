PY := .venv/bin/python
ORDERS ?= 500
SEED ?= 20260903

.PHONY: help venv demo eval eval-live chaos calibrate results serve verify clean

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | column -t -s "$$(printf '\t')"

venv: ## create the venv and install deps
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt

calibrate: ## learn action priors from a separate calibration batch
	$(PY) scripts/calibrate.py 4000

eval: ## run all three arms, write report.json + RESULTS.md
	$(PY) cli.py eval --orders $(ORDERS) --seed $(SEED)
	$(PY) scripts/render_results.py

eval-live: ## same, using the real model for diagnosis (needs ANTHROPIC_API_KEY)
	$(PY) cli.py eval --orders $(ORDERS) --seed $(SEED) --live
	$(PY) scripts/render_results.py

chaos: ## failure-path proof: no double charges under injected gateway failure
	$(PY) cli.py chaos --orders 400 --seed $(SEED)

verify: ## verify the ledger hash chain
	$(PY) cli.py verify artifacts/ledger_C_AGENT.jsonl

results: ## re-render RESULTS.md from the last report
	$(PY) scripts/render_results.py

demo: calibrate eval chaos ## the full run, end to end
	@echo ""
	@echo "  Done. RESULTS.md has the numbers; 'make serve' for the dashboard."

serve: ## dashboard on http://localhost:8000
	$(PY) cli.py serve

clean:
	rm -rf artifacts/*.jsonl artifacts/report.json __pycache__ recoup/__pycache__
