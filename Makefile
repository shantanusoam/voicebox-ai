# Convenience wrappers. scripts/verify.py is the real entry point.
.PHONY: help install verify verify-browser test evals live-smoke schema run clean

help:
	@echo "make install         install Python dependencies"
	@echo "make verify          run every layer gate (L0-L7)"
	@echo "make verify-browser  also run the Playwright gate (L8)"
	@echo "make test            run the pytest suite only"
	@echo "make evals           run the evaluation set and write a scorecard"
	@echo "make live-smoke      OPT-IN: real billed provider calls, budget capped"
	@echo "make schema          regenerate docs/openapi.json from the code"
	@echo "make run             start the local server"

install:
	python -m pip install -r requirements-dev.txt

verify:
	python scripts/verify.py

verify-browser:
	python scripts/verify.py --with-browser

test:
	python -m pytest -q

evals:
	python evals/runner.py --report

live-smoke:
	python evals/live_smoke.py --budget 0.25

schema:
	python scripts/verify.py --only static --update-schema

run:
	python -m callbox

clean:
	rm -rf build evals/out qa/verify-report.json qa/verify-loopback.json
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
