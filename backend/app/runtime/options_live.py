"""The options half of the live/paper runner.

Same structure as `backtest/options_runtime.py`, and for the same reason:
`runtime/runner.py` is 2,000 lines of carefully-reasoned equity logic with real
money running through it right now, so options live behind ONE object the
runner holds as `self._opt`. That object is **None for every shares strategy**,
and every options branch in the runner is guarded by a null check, so a run
that is not an options run takes exactly the code path it always did.

THE DEFINING DIFFERENCE FROM BACKTEST: prices are never modelled here. The
backtest has no choice — Schwab sells no option history — but live and paper
runs have real quotes, and a live order priced off Black-Scholes rather than
the actual book could be sent at a limit nowhere near where the contract
trades. So `quote()` returns None when there is no usable market, and the
runner's contract is to BLOCK the trade on it (plan/16 §9.2). None is a
meaningful answer, never a reason to fall back.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..logging import get_logger
from ..options.contracts import Contract, ContractQuote
from ..options.expiry import Expiration, days_to_expiry, ms_to_expiry
from ..options.pricing import LivePricer, transact_price
from ..options.store import SnapshotRow
from ..options.symbology import canonical_underlying, is_occ, parse_occ
from ..strategy.expression import (RealChainView, Rejection, Selection,
                                   right_for_side, select_contract)
from ..strategy.schema import StrategyDocument

log = get_logger("options-live")

#: How long a cached chain may be reused. Short, because a stale chain used to
#: SIZE an entry produces an order at a price that no longer exists.
CHAIN_TTL_S = 30.0


def is_options_run(doc: StrategyDocument) -> bool:
    expr = getattr(doc, "expression", None)
    return expr is not None and expr.instrument == "options"


@dataclass
class OptionsLive:
    doc: StrategyDocument
    chain_service: object | None = None      # ChainService
    recorder: object | None = None           # ChainRecorder, for streamed quotes
    pricer: LivePricer = field(default_factory=LivePricer)
    _chains: dict[str, tuple[float, RealChainView, float]] = field(
        default_factory=dict, repr=False)
    #: rejection counters, surfaced as run events so a run that stops trading
    #: can always explain itself
    rejections: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.recorder is not None and self.pricer.recorder is None:
            self.pricer.recorder = self.recorder

    # ---- chain access ----------------------------------------------------
    async def chain_for(self, underlying: str) -> tuple[RealChainView | None, float]:
        """(view, underlying_price). Cached briefly — the runner may evaluate
        many symbols on one bar close and a chain request is not cheap."""
        import time
        now = time.time()
        hit = self._chains.get(underlying)
        if hit and now - hit[0] < CHAIN_TTL_S:
            return hit[1], hit[2]
        if self.chain_service is None:
            return None, 0.0
        res = await self.chain_service.fetch(underlying)
        if res.error and not res.rows:
            log.warning("chain_unavailable", underlying=underlying, error=res.error)
            return None, 0.0
        quotes = {}
        for row in res.rows:
            q = _row_to_quote(row, res.underlying_px or 0.0)
            if q is not None:
                quotes[row.symbol] = q
        view = RealChainView(underlying=underlying, rows=res.rows, quotes=quotes)
        px = res.underlying_px or 0.0
        self._chains[underlying] = (now, view, px)
        return view, px

    # ---- selection -------------------------------------------------------
    async def select(self, underlying: str, side: str, ts_ms: int, *,
                     atr: float | None = None) -> Selection | Rejection:
        expr = right_for_side(self.doc.expression, side)
        if expr is None:
            return self._reject(Rejection("no_expression_for_side", side))
        view, spot = await self.chain_for(underlying)
        if view is None or spot <= 0:
            return self._reject(Rejection("no_chain_data", underlying))
        got = select_contract(expr, self.doc.expression.liquidity, view,
                              underlying, spot, ts_ms, atr=atr)
        if isinstance(got, Rejection):
            return self._reject(got)
        return got

    def _reject(self, r: Rejection) -> Rejection:
        self.rejections[r.reason] = self.rejections.get(r.reason, 0) + 1
        return r

    # ---- pricing ---------------------------------------------------------
    def quote(self, contract_symbol: str) -> ContractQuote | None:
        """The freshest REAL quote for one contract, or None.

        Two sources, tried in order, and BOTH are real market data:

          1. the LEVELONE_OPTIONS stream — push-based and freshest;
          2. the cached chain, whose prices came from a REST chain fetch.

        Losing the stream is therefore not a reason to stop trading. What never
        happens is a fall back to a MODEL: a live order priced off
        Black-Scholes rather than the actual book could sit nowhere near where
        the contract trades. None means 'no usable market', and the caller's
        contract is to block on it (plan/16 §9.2)."""
        if not is_occ(contract_symbol):
            return None
        contract = Contract.from_symbol(contract_symbol)
        streamed = self.pricer.quote(contract, 0.0, 0)
        if streamed is not None:
            return streamed
        underlying = contract.underlying
        hit = self._chains.get(underlying)
        if hit is not None:
            return hit[1].quotes.get(contract_symbol) if hit[1].quotes else None
        return None

    def quote_dict(self, contract_symbol: str) -> dict | None:
        """`quote()` in the {"bid","ask","last"} shape SimBroker speaks.

        This is what makes a PAPER options run price off the real book: the
        sim's quote provider is the equity market-data cache, which has never
        heard of an OCC symbol and returns None for every one of them —
        rejecting the order at placement with "no quote available". Returning
        None here still means "no usable market", and the sim's contract on
        None is unchanged: reject rather than invent a price."""
        q = self.quote(contract_symbol)
        if q is None:
            return None
        return {"bid": q.bid, "ask": q.ask, "last": q.mid, "mid": q.mid}

    def transact_price(self, contract_symbol: str, side: str) -> float | None:
        """Price to send on a limit order: the ask to open, the bid to close.

        Full-spread by default, matching the backtest's fill convention, so a
        live fill is comparable to the backtested one rather than flattered."""
        q = self.quote(contract_symbol)
        if q is None:
            return None
        return transact_price(q, side, self.doc.expression.pricing.fill_aggression)

    def quote_is_tradeable(self, q: ContractQuote | None) -> tuple[bool, str]:
        """Would we accept this market? Returns (ok, reason)."""
        if q is None:
            return False, "no quote"
        if q.bid <= 0 or q.ask <= 0:
            return False, "no two-sided market"
        if q.bid > q.ask:
            return False, "crossed market"
        limit = self.doc.expression.liquidity.max_spread_pct
        if q.spread_pct > limit:
            return False, f"spread {q.spread_pct:.1f}% > {limit}%"
        return True, ""

    # ---- lifecycle -------------------------------------------------------
    def should_close_for_dte(self, expiry: dt.date, ts_ms: int) -> bool:
        return days_to_expiry(expiry, ts_ms) <= self.doc.expression.exit.close_at_dte

    def dte(self, expiry: dt.date, ts_ms: int) -> int:
        return days_to_expiry(expiry, ts_ms)

    def max_loss_stop_px(self, premium_paid: float) -> float | None:
        """The premium level at which the broker-side backstop rests.

        This is the ONLY protective order that can rest at the broker under an
        underlying-based stop basis (plan/16 §9.4), so it is what keeps a
        crashed runner from leaving a position completely unmanaged."""
        pct = self.doc.expression.exit.max_loss_pct_of_premium
        if pct is None or premium_paid <= 0:
            return None
        return round(max(premium_paid * (1 - pct / 100.0), 0.01), 2)

    def take_profit_px(self, premium_paid: float) -> float | None:
        pct = self.doc.expression.exit.take_profit_pct_of_premium
        if pct is None or premium_paid <= 0:
            return None
        return round(premium_paid * (1 + pct / 100.0), 2)

    # ---- portfolio risk --------------------------------------------------
    def portfolio_greeks(self, positions: dict) -> dict:
        """Delta-shares, vega dollars and premium at risk across open option
        positions. Marked from live quotes; a position whose quote is missing
        contributes its entry values rather than zero, because dropping it
        would understate exposure — the dangerous direction."""
        delta = vega = premium = 0.0
        for pos in positions.values():
            if not getattr(pos, "is_option", False):
                continue
            q = self.quote(pos.contract_symbol)
            d = q.greeks.delta if q else (pos.entry_delta or 0.0)
            v = q.greeks.vega if q else 0.0
            delta += d * pos.qty * pos.multiplier
            vega += v * pos.qty * pos.multiplier
            premium += pos.premium_paid * pos.qty * pos.multiplier
        return {"delta": delta, "vega": vega, "premium_at_risk": premium}

    def breaches(self, positions: dict) -> list[str]:
        g = self.portfolio_greeks(positions)
        opt_risk = self.doc.risk.options
        out = []
        if opt_risk.max_portfolio_delta is not None and \
                abs(g["delta"]) > opt_risk.max_portfolio_delta:
            out.append(f"portfolio delta {g['delta']:.0f} exceeds "
                       f"{opt_risk.max_portfolio_delta:.0f}")
        if opt_risk.max_portfolio_vega is not None and \
                abs(g["vega"]) > opt_risk.max_portfolio_vega:
            out.append(f"portfolio vega {g['vega']:.0f} exceeds "
                       f"{opt_risk.max_portfolio_vega:.0f}")
        return out

    def premium_at_risk(self, positions: dict) -> float:
        return self.portfolio_greeks(positions)["premium_at_risk"]

    # ---- subscriptions ---------------------------------------------------
    async def subscribe(self, contract_symbols: list[str]) -> None:
        if self.recorder is not None:
            await self.recorder.subscribe([s for s in contract_symbols if s])

    async def unsubscribe(self, contract_symbols: list[str]) -> None:
        if self.recorder is not None:
            await self.recorder.unsubscribe([s for s in contract_symbols if s])


def _row_to_quote(row: SnapshotRow, underlying_px: float) -> ContractQuote | None:
    from ..options.contracts import Greeks
    if not is_occ(row.symbol):
        return None
    occ = parse_occ(row.symbol)
    bid = row.bid or 0.0
    ask = row.ask or 0.0
    if ask <= 0 or bid < 0 or bid > ask:
        return None
    contract = Contract(underlying=canonical_underlying(occ.underlying), expiry=occ.expiry,
                        right=occ.right, strike=occ.strike,
                        multiplier=row.multiplier or 100.0)
    return ContractQuote(
        contract=contract, ts=row.ts, mid=row.mark or (bid + ask) / 2,
        bid=bid, ask=ask, underlying_px=underlying_px, iv=row.iv or 0.0,
        greeks=Greeks(delta=row.delta or 0.0, gamma=row.gamma or 0.0,
                      theta=row.theta or 0.0, vega=row.vega or 0.0,
                      rho=row.rho or 0.0),
        volume=row.volume, open_interest=row.open_interest, source="live")


def expiration_from_ms(ms: int | None) -> dt.date | None:
    return ms_to_expiry(ms) if ms else None


__all__ = ["OptionsLive", "is_options_run", "Expiration", "expiration_from_ms",
           "CHAIN_TTL_S"]
