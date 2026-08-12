"""Contract pricers: synthetic, recorded, live — behind one interface.

The backtest engine and the live runner ask the same question ("what is this
contract worth right now?") and must consume the answer identically, so all
three sources return a `ContractQuote` with a `source` field. That field is not
decoration: it propagates into the backtest's assumptions block and into the UI
banner, because a modelled price and an observed one deserve very different
trust and the product must never blur them.

  SyntheticPricer  Black-Scholes over the vol surface. Used for historical
                   backtests, where no option data exists. Everything it
                   returns is a MODEL OUTPUT.
  RecordedPricer   Real quotes from `option_chain_snapshots`. Exact, but only
                   for dates after the recorder started.
  LivePricer       Real-time quotes for live/paper runs. NEVER falls back to
                   synthetic — see the class docstring; this is a safety
                   property, not an optimisation.
  ChainedPricer    Recorded where available, synthetic elsewhere, tracking the
                   mix so a backtest spanning the recorder's start date is
                   honestly labelled "mixed" with a percentage.

WHY THE SYNTHETIC BAR'S HIGH/LOW ARE EXACT (and what they still miss). At fixed
sigma and T, Black-Scholes price is strictly monotone in spot — increasing for
calls, decreasing for puts (asserted in tests/test_blackscholes.py). So a
call's high is BS(underlying high) and its low is BS(underlying low), exactly,
with no approximation. What this DOES miss is intrabar volatility movement: in
reality vol rises as price falls, so real option ranges are wider than these.
The consequence is specific and is documented in the assumptions block — a
PREMIUM-based stop looks less likely to trigger in backtest than in life, which
is one reason underlying-based stops are the default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

from .blackscholes import Greeks as BSGreeks
from .blackscholes import greeks as bs_greeks
from .blackscholes import price as bs_price
from .contracts import Contract, ContractOHLC, ContractQuote, Greeks
from .expiry import days_to_expiry, year_fraction
from .spread import SpreadParams, fill_price, quote_from_mid
from .symbology import is_occ
from .volsurface import VolSurface, standardized_moneyness


class ContractPricer(Protocol):
    source: str

    def quote(self, contract: Contract, underlying_px: float,
              ts: int) -> ContractQuote | None: ...


def _to_greeks(g: BSGreeks) -> Greeks:
    return Greeks(delta=g.delta, gamma=g.gamma, theta=g.theta, vega=g.vega, rho=g.rho)


@dataclass
class SyntheticPricer:
    """Black-Scholes over the vol surface, with the modelled spread applied.

    Everything it returns is a model output. It is the ONLY pricer permitted in
    a historical backtest, because no other data exists for those dates."""

    surface: VolSurface
    spread: SpreadParams
    source: str = "synthetic"
    r: float = 0.04
    q: float = 0.0
    penny_pilot: bool = True

    def sigma_for(self, contract: Contract, underlying_px: float, ts: int) -> float:
        dte = max(days_to_expiry(contract.expiry, ts), 0)
        T = year_fraction(contract.expiry, ts)
        return self.surface.sigma(underlying_px, contract.strike_f, T, dte)

    def quote(self, contract: Contract, underlying_px: float,
              ts: int) -> ContractQuote | None:
        if underlying_px is None or underlying_px <= 0:
            return None
        dte = max(days_to_expiry(contract.expiry, ts), 0)
        T = year_fraction(contract.expiry, ts)
        sigma = self.surface.sigma(underlying_px, contract.strike_f, T, dte)
        mid = bs_price(underlying_px, contract.strike_f, T, self.r, self.q,
                       sigma, contract.right)
        g = bs_greeks(underlying_px, contract.strike_f, T, self.r, self.q,
                      sigma, contract.right)
        m = standardized_moneyness(underlying_px, contract.strike_f, T,
                                   self.r, self.q, sigma)
        bid, ask = quote_from_mid(mid, self.spread, m, dte,
                                  penny_pilot=self.penny_pilot)
        return ContractQuote(
            contract=contract, ts=ts, mid=mid, bid=bid, ask=ask,
            underlying_px=underlying_px, iv=sigma, greeks=_to_greeks(g),
            source=self.source)

    def bar(self, contract: Contract, und_open: float, und_high: float,
            und_low: float, und_close: float, ts: int) -> ContractOHLC | None:
        """The contract's OHLC implied by the underlying's bar.

        Exact under fixed sigma/T thanks to monotonicity in spot: a call's high
        is its price at the underlying's high, a put's high is its price at the
        underlying's LOW. Intrabar vol movement is ignored (see module
        docstring)."""
        if min(und_open, und_high, und_low, und_close) <= 0:
            return None
        q_open = self.quote(contract, und_open, ts)
        q_close = self.quote(contract, und_close, ts)
        q_a = self.quote(contract, und_high, ts)
        q_b = self.quote(contract, und_low, ts)
        if not all((q_open, q_close, q_a, q_b)):
            return None
        # calls rise with spot, puts fall — so which underlying extreme produces
        # the option's high flips with the right
        hi, lo = (q_a.mid, q_b.mid) if contract.is_call else (q_b.mid, q_a.mid)
        return ContractOHLC(ts=ts, open=q_open.mid, high=hi, low=lo,
                            close=q_close.mid, source=self.source)


@dataclass
class RecordedPricer:
    """Real recorded quotes. Returns None when nothing was recorded for the
    (symbol, ts) within tolerance, so a caller can fall back."""

    quotes: dict[tuple[str, int], ContractQuote] = field(default_factory=dict)
    source: str = "recorded"
    tolerance_ms: int = 4 * 3600 * 1000
    #: symbol -> sorted timestamps, for nearest-lookup
    _index: dict[str, list[int]] = field(default_factory=dict, repr=False)

    def add(self, q: ContractQuote) -> None:
        key = (q.contract.symbol, q.ts)
        self.quotes[key] = q
        self._index.setdefault(q.contract.symbol, []).append(q.ts)

    def finalize(self) -> None:
        for sym in self._index:
            self._index[sym].sort()

    def quote(self, contract: Contract, underlying_px: float,
              ts: int) -> ContractQuote | None:
        stamps = self._index.get(contract.symbol)
        if not stamps:
            return None
        # most recent recorded quote at or before ts, within tolerance. Never
        # look FORWARD: using a later snapshot to price an earlier bar is
        # look-ahead, and in a backtest that is indistinguishable from cheating.
        best = None
        for t in stamps:
            if t <= ts:
                best = t
            else:
                break
        if best is None or ts - best > self.tolerance_ms:
            return None
        return self.quotes.get((contract.symbol, best))


@dataclass
class LivePricer:
    """Real-time quotes for live and paper runs.

    NEVER falls back to a modelled price. That is a safety property, not a
    performance choice: a live order priced off Black-Scholes rather than the
    actual book could be sent at a limit nowhere near where the contract
    trades. `None` means "no usable market", and the runner's contract is to
    BLOCK the trade on it (plan/16 §9.2).

    Sources in order: the LEVELONE_OPTIONS stream (fresh, push-based), then a
    REST quote. Both are real; neither is a model."""

    recorder: object | None = None        # ChainRecorder, for streamed quotes
    rest_quotes: dict[str, ContractQuote] = field(default_factory=dict)
    source: str = "live"
    max_age_s: float = 15.0

    def quote(self, contract: Contract, underlying_px: float,
              ts: int) -> ContractQuote | None:
        sym = contract.symbol
        if self.recorder is not None:
            got = self.recorder.latest_quote(sym, max_age_s=self.max_age_s)
            if got is not None:
                return got
        return self.rest_quotes.get(sym)

    def set_rest_quote(self, q: ContractQuote) -> None:
        self.rest_quotes[q.contract.symbol] = q


@dataclass
class ChainedPricer:
    """Recorded where it exists, synthetic elsewhere — and counts the mix.

    The counting is the point. A backtest spanning the date the recorder
    started is genuinely part-observed and part-modelled, and reporting it as
    either one alone would be a lie. `mix()` feeds the assumptions block and the
    UI banner."""

    recorded: RecordedPricer
    synthetic: SyntheticPricer
    source: str = "mixed"
    n_recorded: int = 0
    n_synthetic: int = 0
    n_missing: int = 0

    def quote(self, contract: Contract, underlying_px: float,
              ts: int) -> ContractQuote | None:
        got = self.recorded.quote(contract, underlying_px, ts)
        if got is not None:
            self.n_recorded += 1
            return got
        got = self.synthetic.quote(contract, underlying_px, ts)
        if got is None:
            self.n_missing += 1
        else:
            self.n_synthetic += 1
        return got

    def mix(self) -> dict:
        total = self.n_recorded + self.n_synthetic
        pct = (self.n_recorded / total * 100.0) if total else 0.0
        if total == 0:
            label = "none"
        elif self.n_synthetic == 0:
            label = "recorded"
        elif self.n_recorded == 0:
            label = "synthetic"
        else:
            label = "mixed"
        return {"pricing_source": label,
                "pct_bars_recorded": round(pct, 2),
                "quotes_recorded": self.n_recorded,
                "quotes_synthetic": self.n_synthetic,
                "quotes_missing": self.n_missing}


# ---- helpers shared by the engine and the runner --------------------------

def transact_price(q: ContractQuote, side: str, aggression: float = 1.0) -> float:
    """What a LONG position actually pays/receives. `side` is "open"/"close"."""
    return fill_price(q.bid, q.ask, side, aggression)


def position_value(q: ContractQuote, qty: float, multiplier: float) -> float:
    """Mark-to-market value of `qty` contracts. The multiplier is the single
    easiest thing to forget in this codebase and the most expensive: omitting
    it understates every options position by 100x."""
    return qty * q.mid * multiplier


def premium_at_risk(entry_premium: float, qty: float, multiplier: float) -> float:
    """Maximum loss on a LONG option position — the premium paid, known
    exactly at entry. This is the property that makes plan/16's whole safety
    argument work (D2/D3): a runner crash cannot lose more than this."""
    return abs(entry_premium * qty * multiplier)


def moneyness_of(contract: Contract, underlying_px: float, ts: int,
                 sigma: float, r: float = 0.04, q: float = 0.0) -> float:
    T = year_fraction(contract.expiry, ts)
    return standardized_moneyness(underlying_px, contract.strike_f, T, r, q, sigma)


def is_option_symbol(symbol: str) -> bool:
    return is_occ(symbol)


def annualized_theta_pct(q: ContractQuote) -> float | None:
    """Theta as a percentage of premium per day — the decay rate a user can
    actually reason about ("this loses 1.8% of its value per day")."""
    if q.mid <= 0:
        return None
    return q.greeks.theta / q.mid * 100.0


def breakeven(contract: Contract, entry_premium: float) -> float:
    """Underlying price at which a long position breaks even AT EXPIRY."""
    return (contract.strike_f + entry_premium if contract.is_call
            else contract.strike_f - entry_premium)


def payoff_at_expiry(contract: Contract, underlying_px: float,
                     entry_premium: float, qty: float = 1.0) -> float:
    """P&L per position at expiry. Used by the payoff diagram and by the
    engine's expiry settlement."""
    intr = (max(underlying_px - contract.strike_f, 0.0) if contract.is_call
            else max(contract.strike_f - underlying_px, 0.0))
    return (intr - entry_premium) * qty * contract.multiplier


def implied_move(q: ContractQuote, days: float) -> float | None:
    """The +/- move the option market is pricing over `days`, in percent.
    A useful sanity read in the UI: if a strategy's target is far outside the
    implied move, the option is unlikely to reach it."""
    if q.iv <= 0 or days <= 0 or q.underlying_px <= 0:
        return None
    return q.iv * math.sqrt(days / 365.0) * 100.0
