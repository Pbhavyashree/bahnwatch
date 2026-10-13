.PHONY: help install run live probe test cov lint clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-9s\033[0m %s\n", $$1, $$2}'

install: ## Install with dev extras
	pip install -e ".[dev]"

run: ## Start on the simulator (offline, always works)
	BAHNWATCH_SOURCE=simulator uvicorn bahnwatch.api:app --reload --port 8000

live: ## Start against the live VBB feed (needs VBB_ACCESS_ID)
	BAHNWATCH_SOURCE=vbb uvicorn bahnwatch.api:app --port 8000

probe: ## Check the VBB key and endpoint answer before relying on them
	python scripts/probe_vbb.py $(or $(STOP),900130002)

test: ## Run the tests (no network needed)
	pytest

cov: ## Tests with coverage
	pytest --cov=bahnwatch --cov-report=term-missing

lint: ## Lint
	ruff check src tests

clean:
	rm -rf .pytest_cache .ruff_cache .coverage bahnwatch.db src/*.egg-info
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
