"""Central configuration. Everything reads from one .env file (or real env vars,
which take precedence). No other module touches os.environ directly."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env lives at the repo root (one level above backend/)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Schwab developer-portal app
    schwab_client_id: str = ""
    schwab_client_secret: str = ""
    schwab_callback_url: str = "https://127.0.0.1:8443/oauth/callback"

    # Anthropic (optional until Phase 4)
    anthropic_api_key: str = ""

    # App
    autotrader_data_dir: Path = Path("~/.autotrader")
    autotrader_strategies_dir: Path = _REPO_ROOT / "strategies"
    autotrader_port: int = 8484
    autotrader_callback_port: int = 8443

    # Global safety-limit defaults (UI-editable overrides live in the DB later)
    autotrader_max_total_notional: float = 50_000
    autotrader_max_total_daily_loss: float = 1_000
    autotrader_sim_slippage_bps: float = 1.0

    # Phase 5.5 — live plumbing probes (docs/plan/11-roadmap.md). Off by default;
    # a real human must opt in via .env AND confirm every single placement —
    # this is the ONLY path in the app that can place a real order in v1.
    live_probe_enabled: bool = False
    live_probe_max_qty: float = 5
    # Dollar cap on qty * price. The qty cap alone doesn't bound how "tiny" an
    # order actually is — 5 shares of a $9 stock and 5 shares of a $900 stock
    # both pass it. This is checked against limit/stop px when given, or a
    # fresh quote for market orders (see live/probe.py).
    live_probe_max_notional: float = 25

    # Phase 6 slice 2 — promotion gate (docs/plan/07-paper-trading.md):
    # minimum paper-trading evidence required before a strategy can be
    # promoted to live (RunManager.start_live), editable per the plan.
    promotion_min_paper_days: float = 14
    promotion_min_round_trips: int = 10

    # Option chain recording cadence (plan/16 D7).
    #
    # A single 15:50 snapshot per day can calibrate a DAILY strategy and nothing
    # more: IV term structure and spreads at 09:45 look nothing like 15:50, so
    # an intraday backtest calibrated only from closing chains is a model
    # applied outside the hours it was ever measured in. Recording intraday is
    # the only way to know the model's error at the times a strategy actually
    # trades — and unlike equity bars, option history cannot be backfilled, so
    # the cadence chosen today is the finest resolution any future backtest can
    # ever have.
    #
    # Cost, measured on the current 6-symbol watchlist (~9.9k rows/sweep,
    # ~1.5 MB/day): a 30-minute cadence is 13 sweeps/day, ~20 MB/day, ~4 GB/yr.
    # Halving the interval doubles both that and the REST budget consumed.
    options_snapshot_interval_min: int = 30
    options_snapshot_first_et: str = "09:45"   # 15m after the open — the
                                               # opening auction's prints have
                                               # settled by then
    options_snapshot_last_et: str = "15:50"    # unchanged, and always included:
                                               # it is the calibration anchor
                                               # every existing snapshot shares
    #: A quote this old at intraday snapshot time is stale. Much tighter than
    #: the 30m default in chain_service: at a 30-minute cadence, a 30-minute-old
    #: quote is just the previous snapshot wearing a new timestamp, and letting
    #: it through would manufacture intraday "movement" that never happened.
    options_intraday_stale_quote_s: int = 300

    # Streamed option quotes (LEVELONE_OPTIONS -> option_tick_quotes).
    #
    # Chain snapshots and this are complementary, not alternatives. A snapshot
    # is a point sample of the WHOLE chain: fine for fitting a vol surface,
    # blind to everything that happens between two slots. This records a NARROW
    # set of near-the-money contracts at stream resolution, which is what makes
    # intrabar exits (stops, targets) simulable at all — with 30-minute samples
    # a stop that would have triggered at 10:22 simply never triggers, which
    # systematically flatters every strategy with tight exits.
    options_tick_recording: bool = True
    options_tick_strikes: int = 10        # each side of ATM -> 21 strikes
    options_tick_expiries: int = 4        # nearest N expirations
    #: Hard ceiling on subscribed contracts. Schwab does not document the
    #: LEVELONE_OPTIONS symbol limit but does define error 19
    #: (REACHED_SYMBOL_LIMIT), so the universe is capped on our side and the
    #: excess dropped nearest-the-money-first, rather than discovered by having
    #: the whole subscription rejected mid-session.
    options_tick_max_symbols: int = 1200
    #: Minimum gap between persisted rows for ONE contract. Writes are also
    #: change-filtered, so this only bounds the worst case — an ATM 0DTE
    #: contract quoting continuously. At 0 every change is written, which for a
    #: liquid chain is millions of rows a day.
    options_tick_min_interval_ms: int = 1000
    #: Ticks are pruned after this many days. Unlike chain snapshots (which are
    #: irreplaceable and kept forever) tick data is bulky and its value decays:
    #: it exists to validate execution assumptions on RECENT behaviour. Set to 0
    #: to keep everything.
    #:
    #: Sizing, measured: a row costs ~216 bytes on disk, and the default
    #: universe is ~870 contracts. The row RATE cannot be predicted from first
    #: principles — it depends entirely on how many of those contracts are
    #: genuinely re-quoting each second, which is a market property — but the
    #: plausible range is wide and the top of it is alarming: at a sustained
    #: 250 rows/s it is ~6M rows and ~1.3 GB PER DAY. 30 days is therefore the
    #: default rather than a quarter. Check `GET /api/options/coverage/ticks`
    #: after a session, then raise this (or `options_tick_min_interval_ms`) once
    #: the real rate is known instead of guessed.
    options_tick_retention_days: int = 30

    @property
    def data_dir(self) -> Path:
        d = self.autotrader_data_dir.expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autotrader.db"

    @property
    def tokens_path(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def strategies_dir(self) -> Path:
        d = self.autotrader_strategies_dir.expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def schwab_configured(self) -> bool:
        return bool(self.schwab_client_id and self.schwab_client_secret)

    @property
    def ai_configured(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
