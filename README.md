# autotrader

A localhost, single-user web app for designing, backtesting, paper-trading,
and running algorithmic trading strategies (equities and derivatives) against a
personal Charles Schwab brokerage account.

This is made for technical people to trade in an easy non-technical way.
It's not the easiest lift to get the application working and setting up strategies,
and I would be surprised if anyone is able to make instant profits (especially outpacing SPY) without much effort or a finance background,
but feel free to give it a shot and if you want to email, or dm me on socials I'm happy
to talk about the project more. 

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
├── frontend/         React + TypeScript UI
└── strategies/       Your strategy YAML files
```

## Setup

If we're being honest, the easiest thing these days is just pointing an ai agent through Claude Code or Codex at this repo and having it get things up and running. But if you want to take a crack at the proper setup without that route, follow below. 

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
  in `.env`, or toggle it from Settings → Live trading once the app is
  running). Paper trading (simulated fills against real streamed quotes) is
  the default and only path to accumulating evidence for the promotion gate.
- Naked/uncovered short options are blocked at the schema, lint, gateway, and
  broker layers — not just discouraged.
- This is a personal tool, not investment advice. Nothing here is a
  recommendation to trade any particular strategy or instrument.

## First-time setup

What to do the first time you run this app, in order. Everything here can be
changed later — nothing is permanent. (The in-app Docs page has this same
walkthrough, plus a lot more reference material, once you're running.)

1. **Get a Schwab developer app.** This app trades through your own Schwab
   account, so it needs its own registration on Schwab's developer portal —
   free, but with a few concrete steps:
   - Create an account at [developer.schwab.com](https://developer.schwab.com)
     using your regular Schwab login.
   - Create a new app. You'll be asked which API products to subscribe to —
     pick both **Trader API - Individual** (for accounts/orders) and
     **Market Data Production** (for quotes/candles/streaming); this app
     needs both.
   - Set the callback URL to exactly `https://127.0.0.1:8443/oauth/callback`
     — it must match character-for-character against what's in `.env`
     (`SCHWAB_CALLBACK_URL`). Schwab requires HTTPS even for a local
     redirect like this one.
   - Submit the app. Schwab has to approve new apps before they're usable —
     this isn't instant approval, so expect to wait anywhere from a few
     hours to a few days before the app shows as active in the portal.
     There's nothing to do on your end in the meantime but check back.
   - Once approved, the app's detail page in the portal shows its
     **Client ID** and **Client Secret** — that's what goes into `.env` (or
     Settings) in the next step.

2. **Add your credentials.** Either paste the client ID/secret into `.env`
   directly, or run the app and enter them on the Settings page — Settings
   saves straight back into `.env` for you. Either way, the backend needs a
   restart afterward to pick up new values.

3. **Connect Schwab.** On the Settings page, click "Connect Schwab" — it
   opens Schwab's own login/consent page in a new tab. Once approved, the
   app holds a token it refreshes automatically; you'll only need to redo
   this when the 7-day refresh token lapses.

4. **Add symbols to start recording data.** On the Data page, add the
   symbols you're interested in and run a backfill. This gets you years of
   daily history instantly; for intraday timeframes, the app only has what
   it has recorded, so the sooner a symbol is added the more history you'll
   have to test against later.

5. **Build and backtest a strategy.** Go to Strategies and create one — by
   hand, or from a plain-English description if you've added an Anthropic
   API key (optional, only needed for AI-assisted generation). Run a
   backtest before doing anything else with it.

6. **Paper trade before anything real.** Start a paper run from Running —
   simulated fills against real live quotes, no real money at risk. This is
   required before a strategy is eligible to go live at all.

A few levers worth knowing about early:

- **Max total notional** (Settings → Global risk limits) — the most dollar
  value you can have deployed across every open position at once, added up
  across all strategies. It's a ceiling on total exposure, not a per-trade
  limit.
- **Max total daily loss** (same section) — once everything running together
  has lost more than this in a day, new entries are blocked for the rest of
  the day. Existing positions can still be closed — this only stops new risk
  from being added on top of a bad day.
- **Live order placement** is off by default everywhere in the app. Turn it
  on from Settings → Live trading — no need to touch `.env` by hand. Turning
  it on doesn't place any orders by itself: a strategy still has to be
  deliberately promoted to live after paper trading, and the standalone Live
  probe page (for placing manual test orders) is gated behind this same
  switch.
- **The kill switch** stops all new order placement everywhere, instantly,
  regardless of what any strategy is doing — good to know it exists before
  you need it.
- **The promotion gate** sets the minimum paper-trading track record — days
  elapsed and round-trip trades — a strategy needs before it can go live.
  The defaults are conservative on purpose.
