PY := .venv/bin/python
ORDERS ?= 500
SEED ?= 20260903
CALIBRATION_ORDERS ?= 6000

.PHONY: help venv demo pitch eval eval-live chaos calibrate results serve verify clean

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | column -t -s "$$(printf '\t')"

venv: ## create the venv and install deps
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt

calibrate: ## learn action priors from a separate calibration batch
	$(PY) scripts/calibrate.py $(CALIBRATION_ORDERS)

eval: ## run all three arms, write report.json + RESULTS.md
	$(PY) cli.py eval --orders $(ORDERS) --seed $(SEED)
	$(PY) scripts/render_results.py

eval-live: ## same, using the real model for diagnosis (needs ANTHROPIC_API_KEY)
	$(PY) cli.py eval --orders $(ORDERS) --seed $(SEED) --live
	$(PY) scripts/render_results.py

ablation: ## clean vs noisy corpus -- does the model actually earn its place?
	$(PY) cli.py eval --orders $(ORDERS) --seed $(SEED) --live --noise 0 --out report_clean.json
	$(PY) cli.py eval --orders $(ORDERS) --seed $(SEED) --live --noise 0.35 --out report.json
	$(PY) scripts/render_results.py
	@echo ""
	@echo "  Clean corpus: both tiers score 100%, the model adds nothing."
	@echo "  Noisy corpus: the model is the difference. See RESULTS.md."

chaos: ## failure-path proof: no double charges under injected gateway failure
	$(PY) cli.py chaos --orders 400 --seed $(SEED)

verify: ## verify the ledger hash chain
	$(PY) cli.py verify artifacts/ledger_C_AGENT.jsonl

results: ## re-render RESULTS.md from the last report
	$(PY) scripts/render_results.py

demo: calibrate eval chaos ## the full run, end to end
	@echo ""
	@echo "  Done. RESULTS.md has the numbers; 'make serve' for the dashboard."

pitch: verify ## verify frozen evidence, then open the judge-ready dashboard on :8000
	@echo ""
	@echo "  Pitch dashboard: http://127.0.0.1:8000"
	@echo "  Featured trace: order_5df8aa203d33da"
	$(PY) cli.py serve

serve: ## dashboard on http://localhost:8000
	$(PY) cli.py serve

clean:
	rm -rf artifacts/*.jsonl artifacts/report.json __pycache__ recoup/__pycache__
