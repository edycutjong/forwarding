.PHONY: help setup lint typecheck test test-coverage test-live bench bench-live demo seed base-rate site verify check audit mcp watch ci all

help:  ## show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n",$$1,$$2}'

setup:  ## install dev deps (the product itself needs nothing)
	python3 -m pip install -r requirements-dev.txt

# ── Code quality ──────────────────────────────────────────────────────────────────────────────
lint:  ## ruff check + format check
	ruff check . && ruff format --check .

typecheck:  ## mypy over the product and the proxy (scripts/, api/)
	mypy scripts api --ignore-missing-imports

test:  ## pytest, offline only (no internet)
	pytest -q -m "not live"

test-coverage:  ## offline tests with coverage of the judged path (scripts/forwarding.py, mcp_server.py, api/), gated
	pytest -q -m "not live" --cov --cov-report=term-missing --cov-report=xml --cov-fail-under=90

test-live:  ## the live tests — hit the real CoinMarketCap API, keyless
	pytest -q -m live

# ── The product ───────────────────────────────────────────────────────────────────────────────
demo:  ## the judged capability, live, zero config, no key: follow the wallet behind UNI's largest removal
	python3 scripts/forwarding.py investigate --platform ethereum --address 0x1f9840a85d5af5bf1d1762f925bdaddc4201f984

mcp:  ## run the MCP server on stdio (what `claude mcp add forwarding -- python3 scripts/mcp_server.py` does)
	python3 scripts/mcp_server.py

watch:  ## the autonomous loop: poll the built-in 11-token watchlist (or --watchlist file.json), adjudicate every new qualifying removal
	python3 scripts/forwarding.py watch

# ── Proof ─────────────────────────────────────────────────────────────────────────────────────
bench:  ## deterministic benchmark: adjudicate() replayed over the committed receipts, p50/p95
	python3 scripts/bench.py --replay --iterations 200

bench-live:  ## benchmark a real keyless investigation end to end, p50/p95
	python3 scripts/bench.py --iterations 8

seed:  ## re-capture docs/proof/{hero,exit,rebalance,jit}.json from the live API (published selection rule)
	python3 scripts/seed.py

base-rate:  ## follow every >= $100k removal on the watchlist, one maker at a time (slow, keyless)
	python3 scripts/base_rate.py

site:  ## re-render site/index.html, site/judge.html and JUDGE.md from docs/proof/*.json
	python3 scripts/render_site.py

verify:  ## replay every committed receipt through adjudicate(), assert I1-I6, check the page carries those numbers
	python3 scripts/verify.py

check:  ## refuse to ship a placeholder, a drifted page, or a README test count that is wrong
	python3 scripts/check_submission_readiness.py
	python3 scripts/render_site.py --check

# ── Security ──────────────────────────────────────────────────────────────────────────────────
audit:  ## dependency CVEs (pip-audit, both requirement files) + secrets in the full git history (gitleaks)
	@echo "=== pip-audit (dependency CVEs) ==="
	pip-audit -r requirements.txt -r requirements-dev.txt
	@echo "=== gitleaks (secrets in history) ==="
	gitleaks detect --no-banner --redact || true

# ── Everything ────────────────────────────────────────────────────────────────────────────────
ci: lint typecheck test-coverage verify check audit  ## everything CI runs, offline
all: ci bench  ## ci plus the deterministic benchmark
