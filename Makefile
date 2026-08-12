VENV=backend/.venv
PY=$(VENV)/bin/python

.PHONY: all setup env dev-api dev-ui test build run open

# One-shot for a fresh clone: install everything, build the UI, start the
# server, and open it in the browser.
all: setup env run

setup:
	python3 -m venv $(VENV)
	$(PY) -m pip install -q -U pip
	$(PY) -m pip install -q -e "backend[dev]"
	cd frontend && npm install

# A fresh clone has no .env (it's gitignored, on purpose — it holds your
# secrets). Seed one from the template so the app has something to read on
# first boot; fill in real values by hand or from Settings once it's up.
# Never overwrites an existing .env.
env:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "Created .env from .env.example — fill in your Schwab/Anthropic credentials by hand or from the Settings page once the app is running."; \
	fi

dev-api:
	$(PY) -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8484

dev-ui:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/python -m pytest -q

build:
	cd frontend && npm run build

open:
	( \
		for i in $$(seq 1 120); do \
			curl -sf http://127.0.0.1:8484/api/health >/dev/null 2>&1 && break; \
			sleep 1; \
		done; \
		open http://127.0.0.1:8484 \
	) &

run: build open
	$(PY) -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8484
