"""The options half of the backtest engine, factored out.

`backtest/engine.py` is already 800 lines of carefully-reasoned equity logic
that several running strategies depend on. Threading options through it inline
would have doubled its size and buried the equity path. Instead everything
options-specific lives here, behind one object the engine holds as `opt`, which
is **None for every shares strategy** — so when it is None the engine's
behaviour is bit-for-bit what it was before.

What this module owns:
  * building the per-underlying vol surface and pricer for a backtest;
  * selecting a contract when a signal fires;
  * pricing a contract at a given underlying price (entry, exit, marking);
  * expiry/DTE lifecycle decisions;
  * the honest `assumptions` block that every options result must carry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..options.contracts import Contract, ContractQuote, Greeks
from ..options.expiry import days_to_expiry, ms_to_expiry
from ..options.pricing import SyntheticPricer, transact_price
from ..options.spread import SpreadParams, tier_for
from ..options.volsurface import (VolSurface, anchor_for, atm_vol, realized_vol_ewma)
from ..strategy.expression import (Rejection, Selection, SyntheticChainView,
                                   right_for_side, select_contract)
from ..strategy.schema import StrategyDocument
from .tick_source import TickQuoteSource

#: Short-rate history (3-month T-bill, annual averages). A flat 4% applied to
#: 2008 or 2021 is materially wrong at longer tenors and this costs nothing.
#: Source: FRED DTB3 annual means, rounded to 2dp.
DEFAULT_RATE_CURVE: dict[int, float] = {
    1990: 0.078, 1991: 0.056, 1992: 0.035, 1993: 0.030, 1994: 0.042, 1995: 0.055,
    1996: 0.051, 1997: 0.052, 1998: 0.048, 1999: 0.046, 2000: 0.058, 2001: 0.034,
    2002: 0.016, 2003: 0.010, 2004: 0.014, 2005: 0.032, 2006: 0.048, 2007: 0.044,
    2008: 0.014, 2009: 0.002, 2010: 0.001, 2011: 0.001, 2012: 0.001, 2013: 0.001,
    2014: 0.000, 2015: 0.001, 2016: 0.003, 2017: 0.009, 2018: 0.019, 2019: 0.021,
    2020: 0.004, 2021: 0.000, 2022: 0.020, 2023: 0.051, 2024: 0.050, 2025: 0.043,
    2026: 0.040,
}

#: Dividend yields for common underlyings. Ignoring these biases calls cheap and
#: puts rich; SPY's ~1.2% is not negligible at longer tenors.
DEFAULT_DIVIDEND_YIELD: dict[str, float] = {
    "SPY": 0.013, "VOO": 0.013, "IVV": 0.013, "QQQ": 0.005, "IWM": 0.012,
    "DIA": 0.017, "EEM": 0.024, "EFA": 0.028, "TLT": 0.038, "HYG": 0.058,
    "XLF": 0.017, "XLE": 0.032, "XLU": 0.030, "XLP": 0.026,
}


def is_options(doc: StrategyDocument) -> bool:
    expr = getattr(doc, "expression", None)
    return expr is not None and expr.instrument == "options"


@dataclass
class ContractPosition:
    """Bookkeeping the engine needs that a bare `Contract` does not carry."""
    contract: Contract
    entry_quote: ContractQuote
    dte_at_entry: int


@dataclass
class OptionsRuntime:
    doc: StrategyDocument
    cfg: object                                  # BacktestConfig
    frames: dict[str, pd.DataFrame]
    surfaces: dict[str, VolSurface] = field(default_factory=dict)
    pricers: dict[str, SyntheticPricer] = field(default_factory=dict)
    anchor_series: dict[str, pd.Series] = field(default_factory=dict)
    #: rejection histogram, reported in the result so a thin trade count is
    #: explainable rather than mysterious
    rejections: dict[str, int] = field(default_factory=dict)
    missing_quotes: int = 0
    expired_unclosed: int = 0
    #: Recorded quotes, when the streamer captured this contract. None means
    #: every price below is modelled, which is the pre-existing behaviour.
    ticks: "TickQuoteSource | None" = None
    quotes_from_ticks: int = 0
    quotes_from_model: int = 0
    #: Bars where the intrabar premium range was answered from recordings, and
    #: bars where it could not be. The second number is the honest measure of
    #: how much of a premium-stop result is still bar-close approximation.
    premium_ranges_used: int = 0
    premium_ranges_missing: int = 0
    #: cfg.options.pricing_source == "recorded". Refuses to fall back to the
    #: model, so an uncovered bar yields no quote at all — the caller already
    #: treats None as "cannot trade", which turns a coverage gap into missing
    #: trades instead of quietly modelled ones.
    require_recorded: bool = False
    vol_sources: dict[str, int] = field(default_factory=dict)

    # ---- construction ----------------------------------------------------
    @classmethod
    def build(cls, doc: StrategyDocument, cfg, frames: dict[str, pd.DataFrame],
              anchor_frames: dict[str, pd.DataFrame] | None = None) -> "OptionsRuntime":
        rt = cls(doc=doc, cfg=cfg, frames=frames)
        for sym, sym_df in frames.items():
            anchor = anchor_for(sym)
            if not (anchor and anchor_frames and anchor in anchor_frames):
                continue
            df = anchor_frames[anchor]
            if df.empty:
                continue
            # Volatility indices quote in PERCENTAGE POINTS (VIX 18.4 means
            # 18.4% vol). Storing 18.4 as sigma would price every option as if
            # the market expected 1,840% volatility.
            series = df["close"] / 100.0
            # REINDEX ONTO THE TRADED SYMBOL'S BARS. The engine looks the anchor
            # up by positional index `i` into the traded frame, so a raw anchor
            # series — which has its own length and its own holidays — would
            # silently read the wrong DATE's volatility, drifting further out of
            # alignment the longer the backtest ran. Forward-fill only, so a
            # missing anchor bar uses the last KNOWN value and never a future
            # one.
            rt.anchor_series[sym] = series.reindex(sym_df.index, method="ffill")
        return rt

    # ---- rates and dividends --------------------------------------------
    def rate_at(self, ts: int) -> float:
        curve = getattr(self.cfg.options, "rate_curve", None) or {}
        year = ms_to_expiry(ts).year
        if str(year) in curve:
            return float(curve[str(year)])
        if year in DEFAULT_RATE_CURVE:
            return DEFAULT_RATE_CURVE[year]
        return float(self.cfg.options.risk_free_rate)

    def dividend_yield(self, underlying: str) -> float:
        cfg_map = getattr(self.cfg.options, "dividend_yield", None) or {}
        u = underlying.upper()
        if u in cfg_map:
            return float(cfg_map[u])
        return DEFAULT_DIVIDEND_YIELD.get(u, 0.0)

    # ---- surface / pricer ------------------------------------------------
    def pricer_for(self, underlying: str, i: int, ts: int) -> SyntheticPricer:
        """A pricer refreshed for THIS bar: the volatility level moves with the
        market, so a surface built once at the start of a backtest would price
        2020 with 2019's volatility."""
        level, source = self._atm_level(underlying, i)
        self.vol_sources[source] = self.vol_sources.get(source, 0) + 1
        r = self.rate_at(ts)
        q = self.dividend_yield(underlying)
        params = self.surfaces.get(underlying)
        if params is None:
            from ..options.volsurface import SurfaceParams
            sp = SurfaceParams.default_for(
                underlying, vrp_mult=self.doc.expression.pricing.vrp_mult)
        else:
            sp = params.params
        surface = VolSurface(underlying=underlying, atm_30=level, params=sp,
                             source=source, r=r, q=q)
        self.surfaces[underlying] = surface
        tier = self.doc.expression.pricing.liquidity_tier or tier_for(underlying)
        return SyntheticPricer(surface=surface,
                               spread=SpreadParams.default_for(underlying, tier),
                               r=r, q=q)

    def _atm_level(self, underlying: str, i: int) -> tuple[float, str]:
        anchor = self.anchor_series.get(underlying)
        anchor_vol = None
        if anchor is not None and 0 <= i < len(anchor):
            v = float(anchor.iloc[i])
            if v == v and v > 0:
                anchor_vol = v
        realized = None
        if anchor_vol is None:
            df = self.frames.get(underlying)
            if df is not None and i >= 5:
                window = df["close"].iloc[max(0, i - 120):i + 1].tolist()
                realized = realized_vol_ewma(window)
        params = self.surfaces.get(underlying)
        sp = params.params if params is not None else None
        if sp is None:
            from ..options.volsurface import SurfaceParams
            sp = SurfaceParams.default_for(
                underlying, vrp_mult=self.doc.expression.pricing.vrp_mult)
        level, source = atm_vol(anchor_vol=anchor_vol, realized=realized, params=sp)
        if level is None:
            # Nothing to go on. 20% is a neutral placeholder and the source
            # string records that it was a fallback, so the assumptions block
            # can report how often this happened.
            return 0.20, "fallback_flat"
        return level, source

    # ---- selection -------------------------------------------------------
    def select(self, underlying: str, side: str, i: int, ts: int, *,
               atr: float | None = None) -> Selection | Rejection:
        expr = right_for_side(self.doc.expression, side)
        if expr is None:
            return self._reject(Rejection("no_expression_for_side", side))
        pricer = self.pricer_for(underlying, i, ts)
        self.pricers[underlying] = pricer
        view = SyntheticChainView(underlying=underlying, pricer=pricer)
        got = select_contract(expr, self.doc.expression.liquidity, view,
                              underlying, self._spot(underlying, i), ts,
                              atr=atr, sigma=pricer.surface.atm_30,
                              r=pricer.r, q=pricer.q)
        if isinstance(got, Rejection):
            return self._reject(got)
        return got

    def _reject(self, r: Rejection) -> Rejection:
        self.rejections[r.reason] = self.rejections.get(r.reason, 0) + 1
        return r

    def _spot(self, underlying: str, i: int) -> float:
        df = self.frames.get(underlying)
        if df is None or i < 0 or i >= len(df):
            return 0.0
        return float(df["close"].iloc[i])

    # ---- pricing ---------------------------------------------------------
    def quote(self, contract: Contract, underlying_px: float, ts: int,
              i: int = 0) -> ContractQuote | None:
        """Recorded quote if one exists at/before `ts`, else the model.

        The order is not negotiable: a recorded bid/ask is what the market
        actually showed, spread and all, while the model is an estimate of it.
        Falling back is fine — silently failing to SAY that a price was modelled
        is not, which is why both branches are counted and surfaced in
        `assumptions()`."""
        recorded = self._recorded_quote(contract, underlying_px, ts)
        if recorded is not None:
            self.quotes_from_ticks += 1
            return recorded
        if self.require_recorded:
            self.missing_quotes += 1
            return None

        pricer = self.pricers.get(contract.underlying)
        if pricer is None:
            pricer = self.pricer_for(contract.underlying, i, ts)
            self.pricers[contract.underlying] = pricer
        q = pricer.quote(contract, underlying_px, ts)
        if q is None:
            self.missing_quotes += 1
        else:
            self.quotes_from_model += 1
        return q

    def _recorded_quote(self, contract: Contract, underlying_px: float,
                        ts: int) -> ContractQuote | None:
        if self.ticks is None:
            return None
        got = self.ticks.quote(contract.symbol, ts)
        if got is None:
            return None
        bid, ask, mark = got
        if ask <= 0 or bid < 0 or bid > ask:
            return None
        # Greeks and IV are deliberately left at zero rather than back-solved
        # here: nothing on the pricing path reads them, and a fabricated delta
        # on a row labelled "recorded" would be worse than an absent one.
        return ContractQuote(contract=contract, ts=ts, mid=mark, bid=bid, ask=ask,
                             underlying_px=underlying_px, iv=0.0,
                             greeks=Greeks(), source="recorded")

    def premium_range(self, contract: Contract, start_ms: int,
                      end_ms: int) -> tuple[float, float] | None:
        """(low, high) recorded premium strictly inside a bar, or None.

        This is what makes an intrabar PREMIUM stop simulable. Without it the
        engine can only see the premium at the bar's close, so a contract that
        spiked and came back looks like it never moved — and the stop that
        would have fired in life never fires here. None means "not recorded",
        which the caller must not read as "did not move"."""
        if self.ticks is None:
            return None
        got = self.ticks.extremes(contract.symbol, start_ms, end_ms)
        if got is None:
            self.premium_ranges_missing += 1
        else:
            self.premium_ranges_used += 1
        return got

    def transact(self, contract: Contract, underlying_px: float, ts: int,
                 side: str, i: int = 0) -> float | None:
        """Price actually paid ("open") or received ("close") per contract."""
        q = self.quote(contract, underlying_px, ts, i)
        if q is None:
            return None
        return transact_price(q, side, self.doc.expression.pricing.fill_aggression)

    # ---- lifecycle -------------------------------------------------------
    def should_close_for_dte(self, contract: Contract, ts: int) -> bool:
        return days_to_expiry(contract.expiry, ts) <= self.doc.expression.exit.close_at_dte

    def is_expired(self, contract: Contract, ts: int) -> bool:
        return days_to_expiry(contract.expiry, ts) < 0

    def settle_at_expiry(self, contract: Contract, underlying_px: float) -> float:
        """Intrinsic value per contract. Reached only if a position somehow
        survived `close_at_dte`; the engine counts it, because a nonzero count
        is a bug signal rather than a normal outcome."""
        self.expired_unclosed += 1
        return (max(underlying_px - contract.strike_f, 0.0) if contract.is_call
                else max(contract.strike_f - underlying_px, 0.0))

    # ---- reporting -------------------------------------------------------
    def assumptions(self) -> dict:
        """The honesty block (plan/16 D5). Every options result carries this,
        and the UI turns it into a non-dismissible banner."""
        expr = self.doc.expression
        surface = next(iter(self.surfaces.values()), None)
        total_vol = sum(self.vol_sources.values()) or 1
        priced = self.quotes_from_ticks + self.quotes_from_model
        pct_recorded = (round(self.quotes_from_ticks / priced * 100, 2)
                        if priced else 0.0)
        if not self.quotes_from_ticks:
            source = "synthetic"
        elif not self.quotes_from_model:
            source = "recorded"
        else:
            source = "mixed"
        return {
            "instrument": "options",
            "pricing_source": source,
            "pct_bars_recorded": pct_recorded,
            "quotes_recorded": self.quotes_from_ticks,
            "quotes_modelled": self.quotes_from_model,
            "recorded_note": (
                "prices came from streamed quotes recorded at the time; the "
                "spread is the real one" if source == "recorded" else
                f"{pct_recorded}% of prices are recorded quotes, the rest are "
                "modelled — a result is only as defensible as this number"
                if source == "mixed" else
                "no recorded quotes covered this window; every price is "
                "modelled"),
            "tick_source": self.ticks.stats() if self.ticks is not None else None,
            "model": "generalized Black-Scholes-Merton (European). American "
                     "early exercise is NOT modelled; it makes long puts "
                     "marginally cheap, i.e. optimistic for a buyer.",
            "vol_source_mix": {k: round(v / total_vol * 100, 1)
                               for k, v in sorted(self.vol_sources.items())},
            "vrp_mult": expr.pricing.vrp_mult,
            "vrp_note": "applied to REALIZED vol only; a volatility-index anchor "
                        "already contains the premium and is not re-multiplied",
            "surface": surface.to_dict() if surface else None,
            "skew_note": "standardised moneyness is tanh-saturated at 3 sigma so "
                         "the smile flattens in the wings; an unsaturated "
                         "quadratic admits butterfly arbitrage the backtest "
                         "would trade as fake edge",
            "risk_free": "built-in annual short-rate curve unless overridden",
            "fill_aggression": expr.pricing.fill_aggression,
            "fill_note": "opening pays the ask, closing receives the bid",
            "commission_per_contract": self.cfg.commission.per_contract,
            "intrabar_iv": (
                "RECORDED — premium stops and targets are checked against the "
                "real quoted range inside each bar, not against a range derived "
                "from the underlying at fixed sigma."
                if self.ticks is not None and self.premium_ranges_used else
                "IGNORED — option high/low are derived from the underlying's "
                "high/low at fixed sigma. Real option ranges are wider, so a "
                "PREMIUM-based stop looks less likely to trigger here than in "
                "life."),
            "intrabar_premium_bars": self.premium_ranges_used,
            "intrabar_premium_bars_unrecorded": self.premium_ranges_missing,
            "liquidity_enforced": ["max_spread_pct", "max_spread_abs"],
            "liquidity_NOT_enforced": ["min_open_interest", "min_volume"],
            "liquidity_note": "a synthetic chain has no order book, so open "
                              "interest and volume cannot be checked. Live "
                              "enforces both and may take FEWER trades.",
            "assignment": "not modelled — v1 is long-premium only, so there is "
                          "no short leg to be assigned",
            "expiry_policy": f"closed at {expr.exit.close_at_dte} DTE",
            "expired_unclosed_count": self.expired_unclosed,
            "missing_quote_count": self.missing_quotes,
            "selection_rejections": dict(sorted(self.rejections.items())),
        }
