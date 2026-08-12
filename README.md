# autotrader

A localhost, single-user web app for designing, backtesting, paper-trading,
and running algorithmic trading strategies (equities and options) against a
personal Charles Schwab brokerage account.

- **Backend:** Python (FastAPI + asyncio) — market data ingestion, a shared
  strategy-evaluation engine used identically in backtest/paper/live,
  order gateway with a kill switch and hard safety limits, SQLite storage.
- **Frontend:** React + TypeScript — strategy editor, backtest results,
  live/paper run dashboards, charting.
- **Strategies** are YAML documents (schema-validated, versioned, content-hashed)
  — no freeform code, so they can be authored by hand or generated via the
  Claude API.
- **Safety model:** paper trading is mandatory before live; a promotion gate
  enforces a minimum amount of paper evidence; a kill switch and layered
  notional/loss limits guard every real order; no naked short options.

See `docs/plan/README.md` for the full design (architecture, schema, backtest
engine, execution safety, paper trading, options design).

## Structure

```
autotrader/
├── backend/          FastAPI app: market data, backtest engine, runtime, broker layer
├── frontend/          React + TypeScript UI
├── docs/plan/         Design docs (architecture, schema, safety, roadmap, ...)
├── strategies/        Your strategy YAML files (gitignored — this is your own data)
└── schwab/            Vendored Schwab API reference (gitignored in this checkout)
```

## Setup

Requires Python 3.13+, Node 24+, and a Schwab Developer Portal app
(client id/secret, callback URL registered as
`https://127.0.0.1:8443/oauth/callback`).

```bash
make all   # installs everything, creates .env if you don't have one yet,
           # builds the UI, starts the server, and opens http://127.0.0.1:8484
```

`make all` is the one-command path for a fresh clone: backend venv + deps,
frontend `npm install`, a `.env` seeded from `.env.example` if none exists
yet, a production frontend build, then a single FastAPI process (on
`127.0.0.1:8484`) serving both the API and the built UI.

Add your Schwab (and, optionally, Anthropic) credentials either by editing
`.env` directly, or from the app itself once it's running — Settings →
Credentials saves straight back into `.env` (restart the app afterward for
a new credential to take effect).

For active development, run the backend and frontend as separate hot-reloading
processes instead:

```bash
make setup    # once
make dev-api  # FastAPI on 127.0.0.1:8484
make dev-ui   # Vite dev server on :5173 (proxies /api and /ws to 8484)
```

Run backend tests with `make test`.

The app has no login/auth of its own — it binds to `127.0.0.1` only. The only
auth in the system is the Schwab OAuth token lifecycle (`tokens.json`, never
committed).

## Safety notes

- Live order placement is opt-in and off by default (`LIVE_PROBE_ENABLED=false`
  in `.env`). Paper trading (simulated fills against real streamed quotes) is
  the default and only path to accumulating evidence for the promotion gate.
- Naked/uncovered short options are blocked at the schema, lint, gateway, and
  broker layers — not just discouraged.
- This is a personal tool, not investment advice. Nothing here is a
  recommendation to trade any particular strategy or instrument.
