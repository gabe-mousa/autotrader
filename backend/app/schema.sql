-- Full schema per docs/plan/10-data-model.md. Applied idempotently at startup.
PRAGMA journal_mode = WAL;

-- market data
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL, tf TEXT NOT NULL, ts INTEGER NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL,
    PRIMARY KEY (symbol, tf, ts)
);
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY, description TEXT, exchange TEXT, cusip TEXT, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS market_calendar (
    date TEXT PRIMARY KEY, is_open INTEGER NOT NULL,
    session_start INTEGER, session_end INTEGER
);

-- strategies (index of disk; disk wins)
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    current_version INTEGER NOT NULL, state TEXT NOT NULL, file_path TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL, content_hash TEXT NOT NULL, file_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (strategy_id, version)
);

-- backtesting
CREATE TABLE IF NOT EXISTS backtests (
    id TEXT PRIMARY KEY, strategy_version_hash TEXT NOT NULL, sweep_id TEXT,
    config_json TEXT NOT NULL, status TEXT NOT NULL,
    started_at INTEGER, finished_at INTEGER, metrics_json TEXT,
    -- which strategy actually ran. strategy_version_hash alone is ambiguous:
    -- content_hash excludes meta, so copies/renames collide (see db.py's
    -- migration). NULL for rows predating that migration.
    strategy_id TEXT
);
CREATE TABLE IF NOT EXISTS backtest_equity (
    backtest_id TEXT NOT NULL, ts INTEGER NOT NULL, equity REAL NOT NULL, drawdown REAL NOT NULL,
    PRIMARY KEY (backtest_id, ts)
);
CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, backtest_id TEXT NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL, entry_ts INTEGER, entry_px REAL, exit_ts INTEGER, exit_px REAL,
    qty REAL, pnl REAL, pnl_pct REAL, mae REAL, mfe REAL, bars_held INTEGER, exit_reason TEXT,
    -- Options detail as JSON rather than a dozen sparse columns that would be
    -- NULL on every equity row: contract symbol, right, strike, expiry, DTE at
    -- entry/exit, entry IV and delta, the underlying prices, premium paid.
    -- NULL for a shares trade, which is what keeps existing rows meaningful.
    detail_json TEXT
);
CREATE TABLE IF NOT EXISTS sweeps (
    id TEXT PRIMARY KEY, strategy_version_hash TEXT NOT NULL,
    param_grid_json TEXT NOT NULL, created_at INTEGER NOT NULL
);

-- parameter optimization (docs/plan/14-optimizer.md). Separate from `sweeps`:
-- a sweep is a hand-read Cartesian grid, a study is a walk-forward search
-- with a holdout, constraints and per-trial diagnostics.
CREATE TABLE IF NOT EXISTS optimization_studies (
    id TEXT PRIMARY KEY,
    strategy_id TEXT, strategy_version_hash TEXT NOT NULL, slug TEXT NOT NULL,
    space_json TEXT NOT NULL,        -- the search space (list of dimensions)
    objective_json TEXT NOT NULL,    -- metric, lambda, constraints
    plan_json TEXT NOT NULL,         -- resolved folds + holdout + measured coverage
    config_json TEXT NOT NULL,       -- base BacktestConfig (capital, slippage, commission)
    sampler TEXT NOT NULL, seed INTEGER NOT NULL, n_trials INTEGER NOT NULL,
    status TEXT NOT NULL,            -- running|done|cancelled|interrupted|error
    champion_trial_id TEXT, holdout_json TEXT, diagnostics_json TEXT,
    created_at INTEGER NOT NULL, finished_at INTEGER, error TEXT
);
-- NOTE: trials store metrics ONLY -- no per-bar equity, no per-trade rows. A
-- 400-trial x 5-fold study is 4000 backtests; persisting those the way the
-- `backtests` table does would add millions of rows nobody will ever open.
-- The champion becomes a real `backtests` row on promotion.
CREATE TABLE IF NOT EXISTS optimization_trials (
    id TEXT PRIMARY KEY, study_id TEXT NOT NULL, trial_number INTEGER NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL,            -- done|rejected|error
    reject_reason TEXT,
    score REAL, stability REAL, oos_mean REAL, oos_std REAL, is_mean REAL,
    folds_json TEXT, metrics_json TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (study_id, trial_number)
);
CREATE INDEX IF NOT EXISTS idx_opt_trials_study ON optimization_trials (study_id, score DESC);

-- running (paper + live)
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, strategy_version_hash TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('paper','live')), state TEXT NOT NULL,
    resume_policy TEXT NOT NULL DEFAULT 'require_confirm',
    started_at INTEGER, stopped_at INTEGER, starting_equity REAL, promotion_id TEXT,
    account_hash TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts INTEGER NOT NULL,
    level TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_run_events_run_ts ON run_events (run_id, ts);
CREATE TABLE IF NOT EXISTS positions (
    run_id TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL, avg_price REAL NOT NULL,
    opened_at INTEGER NOT NULL, stop_order_id TEXT, target_order_id TEXT,
    -- 'long' | 'short'. Direction used to survive a restart only as the SIGN of
    -- qty; this column is now the authoritative record (runner.py's
    -- _recover_position falls back to the sign for pre-migration rows). qty is
    -- STILL written signed, because runtime/recovery.py reconstructs paper cash
    -- from `qty * avg_price` and SimBroker.seed_position takes a signed qty.
    side TEXT NOT NULL DEFAULT 'long',
    -- options (docs/plan/16 §9.1). `symbol` holds the OCC contract symbol for an
    -- option position; `underlying` is the ticker the strategy actually trades,
    -- and is what the runner keys its in-memory position map by.
    asset_type TEXT NOT NULL DEFAULT 'EQUITY',
    underlying TEXT,
    multiplier REAL NOT NULL DEFAULT 1,
    expiry INTEGER,               -- epoch ms
    premium_paid REAL,            -- per contract; also the max loss on a long
    -- the UNDERLYING's price at entry. `avg_price` holds the option PREMIUM
    -- for an option row, so without this the entry reference every
    -- underlying-based stop is measured from would be lost on a restart —
    -- and the stop would silently re-anchor to the recovered price.
    underlying_entry_px REAL,
    PRIMARY KEY (run_id, symbol)
);
CREATE TABLE IF NOT EXISTS order_intents (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ts INTEGER NOT NULL,
    symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, order_type TEXT NOT NULL,
    limit_px REAL, stop_px REAL, purpose TEXT NOT NULL, status TEXT NOT NULL,
    gateway_decision_json TEXT,
    -- options (docs/plan/16). `symbol` holds the full OCC contract symbol for an
    -- option intent; `underlying` is what the Orders page groups and filters by,
    -- since a user thinks in terms of "my SPY trades", not in OCC strings.
    asset_type TEXT NOT NULL DEFAULT 'EQUITY',
    underlying TEXT,
    multiplier REAL NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, run_id TEXT NOT NULL, mode TEXT NOT NULL,
    schwab_order_id TEXT, tag TEXT, status TEXT NOT NULL,
    submitted_at INTEGER, closed_at INTEGER, raw_last_json TEXT
);
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, ts INTEGER NOT NULL,
    qty REAL NOT NULL, price REAL NOT NULL, fees REAL NOT NULL DEFAULT 0, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_snapshots (
    ts INTEGER PRIMARY KEY, equity REAL, cash REAL, buying_power REAL, positions_json TEXT
);

-- Phase 5.5: raw ACCT_ACTIVITY capture. Schwab documents the envelope (seq,
-- key, account, message type, message data) but NOT the message-type-
-- specific JSON shapes -- store the whole raw item verbatim so real examples
-- can be inspected/used to build the parser once probe orders are placed.
-- See app/streamer/acct_activity.py and app/broker/schwab.py's module docstring.
CREATE TABLE IF NOT EXISTS broker_activity_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT, received_at INTEGER NOT NULL,
    seq INTEGER, raw_json TEXT NOT NULL
);

-- Raw CHART_EQUITY capture. bar_aggregator.py's and recorder.py's _on_chart
-- guards independently found live (2026-07-23, 2026-07-24) that field "1"
-- ("open" per Schwab's documented field map) sometimes carries a value
-- wildly inconsistent with high/low/close -- e.g. a per-message counter
-- that increments by 1/minute and is IDENTICAL across unrelated symbols in
-- the same push. That's not explainable from parsed log output alone; store
-- every raw CHART_EQUITY content item verbatim, before any field-mapping is
-- applied, so the actual wire shape can be inspected. See
-- app/streamer/chart_raw.py.
CREATE TABLE IF NOT EXISTS chart_equity_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT, received_at INTEGER NOT NULL,
    symbol TEXT, raw_json TEXT NOT NULL
);

-- ===================== OPTIONS (docs/plan/16-options-trading.md) =====================
-- Everything here is ADDITIVE. No existing table's meaning changes, so a
-- backend that has not restarted yet keeps behaving exactly as before and the
-- running paper/live equity strategies are untouched.

-- One row per contract per snapshot. THE ONLY source of real historical option
-- prices this system will ever have: Schwab's /pricehistory is equity-only
-- (verified against schwab/market-data-production/spec.json), so any option
-- price not recorded here is gone permanently. Written by
-- app/streamer/option_recorder.py; read by options/store.py, the RecordedPricer
-- and options/calibrate.py.
CREATE TABLE IF NOT EXISTS option_chain_snapshots (
    underlying   TEXT NOT NULL,
    ts           INTEGER NOT NULL,   -- epoch ms, SNAPSHOT time (not quote time)
    symbol       TEXT NOT NULL,      -- full 21-char OCC symbol
    expiry       INTEGER NOT NULL,   -- epoch ms, 16:00 ET on the expiration date
    strike       REAL NOT NULL,
    -- `opt_right` not `right`: RIGHT is a SQLite keyword (RIGHT JOIN, 3.39+) and
    -- an unquoted column of that name is a parse hazard waiting to happen.
    opt_right    TEXT NOT NULL CHECK (opt_right IN ('C','P')),
    bid REAL, ask REAL, last REAL, mark REAL,
    bid_size INTEGER, ask_size INTEGER,
    volume INTEGER, open_interest INTEGER,
    iv REAL,                          -- Schwab's own implied volatility
    delta REAL, gamma REAL, theta REAL, vega REAL, rho REAL,
    -- underlying mark AT SNAPSHOT TIME. Required, not optional: without it the
    -- row cannot be used to calibrate anything, because moneyness is unknown.
    underlying_px REAL,
    dte INTEGER,
    multiplier REAL NOT NULL DEFAULT 100,
    is_non_standard INTEGER NOT NULL DEFAULT 0,
    -- Schwab's quoteTimeInLong. A stale quote on an illiquid strike is the main
    -- data-quality hazard in this table, and this column is how it is detected.
    quote_ts INTEGER,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS ix_ocs_underlying_ts ON option_chain_snapshots (underlying, ts);
CREATE INDEX IF NOT EXISTS ix_ocs_expiry ON option_chain_snapshots (underlying, expiry, ts);

-- Expiration calendar per underlying, from GET /expirationchain. Small, cheap,
-- and needed by the SYNTHETIC pricer too: a backtest must only ever select
-- expirations that really listed, or it trades contracts that never existed.
CREATE TABLE IF NOT EXISTS option_expirations (
    underlying TEXT NOT NULL,
    expiry INTEGER NOT NULL,          -- epoch ms
    expiration_type TEXT,             -- M | Q | S | W
    settlement_type TEXT,             -- A | P
    standard INTEGER NOT NULL DEFAULT 1,
    option_roots TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (underlying, expiry)
);

-- Fitted vol-surface / spread parameters per underlying, produced by
-- options/calibrate.py from option_chain_snapshots. The SyntheticPricer reads
-- the newest row at or before the bar being priced, so a backtest over a period
-- with no calibration data falls back to documented defaults rather than
-- silently applying today's parameters to 2008.
CREATE TABLE IF NOT EXISTS option_surface_params (
    underlying TEXT NOT NULL,
    as_of INTEGER NOT NULL,           -- epoch ms
    params_json TEXT NOT NULL,        -- {atm_iv_beta, term_slope, skew_b, skew_c,
                                      --  spread_a, spread_b, spread_c, n_obs, rmse}
    PRIMARY KEY (underlying, as_of)
);

-- Raw LEVELONE_OPTIONS capture, same rationale as chart_equity_raw: the
-- CHART_EQUITY field map published by Schwab turned out to be WRONG on the wire
-- (see app/streamer/recorder.py's field-map comment), so the options field map
-- is verified against real payloads before it is trusted rather than after.
CREATE TABLE IF NOT EXISTS option_levelone_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT, received_at INTEGER NOT NULL,
    symbol TEXT, raw_json TEXT NOT NULL
);

-- Streamed option quotes, from LEVELONE_OPTIONS. The high-resolution
-- counterpart to option_chain_snapshots, and the answer to what that table
-- structurally cannot capture: a 30-minute snapshot is a POINT SAMPLE, so a
-- contract that traded 6.40 -> 9.80 -> 6.50 between two slots records as a
-- flat 6.40 -> 6.50 and the move is not merely missing, it is invisible —
-- the series looks calm. Every stop that would have triggered intrabar, and
-- every bit of realized variance inside the interval, is lost that way.
--
-- Deliberately NARROW where the snapshot table is wide: snapshots cover the
-- whole chain coarsely for surface calibration, this covers a few hundred
-- near-the-money contracts finely for execution realism. Writes are
-- change-filtered and conflated (see streamer/option_recorder.py) because an
-- unfiltered ATM 0DTE feed is thousands of rows per second.
CREATE TABLE IF NOT EXISTS option_tick_quotes (
    symbol TEXT NOT NULL,             -- full 21-char OCC symbol
    -- Schwab's quote time, NOT arrival time: conflation means arrival order is
    -- not quote order, and pricing a bar off a receipt timestamp would
    -- reintroduce the smear this table exists to remove.
    ts INTEGER NOT NULL,
    underlying TEXT NOT NULL,
    bid REAL, ask REAL, mark REAL, last REAL,
    bid_size INTEGER, ask_size INTEGER,
    volume INTEGER, open_interest INTEGER,
    iv REAL,
    delta REAL, gamma REAL, theta REAL, vega REAL, rho REAL,
    underlying_px REAL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS ix_otq_underlying_ts ON option_tick_quotes (underlying, ts);

-- Per-snapshot recorder diagnostics. A silent 40% rejection rate must be
-- visible on the Data page, not buried in a log line nobody reads.
CREATE TABLE IF NOT EXISTS option_snapshot_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    underlying TEXT NOT NULL, ts INTEGER NOT NULL,
    requested INTEGER NOT NULL DEFAULT 0,
    written INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    reject_reasons_json TEXT,
    underlying_px REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_osr_underlying_ts ON option_snapshot_runs (underlying, ts DESC);

-- promotion & audit
CREATE TABLE IF NOT EXISTS promotions (
    id TEXT PRIMARY KEY, strategy_version_hash TEXT NOT NULL,
    -- nullable: override_gate=True can promote with no backtest and/or no
    -- prior paper-trading evidence at all (explicit user choice)
    backtest_run_id TEXT, paper_run_id TEXT,
    gate_snapshot_json TEXT NOT NULL, confirmed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
    actor TEXT NOT NULL, category TEXT NOT NULL, payload_json TEXT NOT NULL
);

-- AI
CREATE TABLE IF NOT EXISTS ai_conversations (
    id TEXT PRIMARY KEY, strategy_id TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
    content_json TEXT NOT NULL, model TEXT, created_at INTEGER NOT NULL
);

-- key-value app state (UI-edited settings overrides, kill switch flag, etc.)
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at INTEGER NOT NULL
);
