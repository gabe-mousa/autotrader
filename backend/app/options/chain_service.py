"""Live option chain access: fetch, flatten, validate, cache.

Schwab's `GET /chains` response is nested three levels deep —
`callExpDateMap["2026-09-18:45"]["680.0"] -> [OptionContract]` — and carries
both calls and puts in separate maps. Everything downstream wants a flat list,
so the flattening (and, critically, the VALIDATION) happens here once.

The validation is not optional politeness. `streamer/recorder.py` already
found, twice, that Schwab's documented wire format did not match reality
(the CHART_EQUITY field shift, the crossed-market quotes), and each time the
guard that rejected the bad data is what stopped it poisoning the store. Option
chains have more ways to be wrong than equity bars — stale strikes that have not
quoted in hours, zero-bid wings, adjusted contracts with the wrong root — so
every row is checked and every rejection is COUNTED and surfaced, never silently
dropped.
"""

from __future__ import annotations

import datetime as dt
import time
from collections import Counter
from dataclasses import dataclass, field

from ..logging import get_logger
from .contracts import Contract, ContractQuote, Greeks
from .expiry import expiry_to_ms
from .store import SnapshotRow
from .symbology import (InvalidOccSymbol, accepted_roots, canonical_underlying,
                        is_occ, market_data_symbol, parse_occ)

log = get_logger("options.chain")

#: A quote older than this at snapshot time is stale — the strike has not been
#: updated recently and its "price" is a memory, not a market.
STALE_QUOTE_MS = 30 * 60 * 1000

#: Deep wings carry no information and bloat the store; |delta| outside this
#: band is dropped at write time.
MIN_ABS_DELTA = 0.02
MAX_ABS_DELTA = 0.98


@dataclass
class ChainFetchResult:
    underlying: str
    ts: int
    underlying_px: float | None
    rows: list[SnapshotRow] = field(default_factory=list)
    requested: int = 0
    rejected: int = 0
    reject_reasons: Counter = field(default_factory=Counter)
    error: str | None = None

    @property
    def written(self) -> int:
        return len(self.rows)


def _num(v, default=None):
    """Schwab sends -999.0 for 'not available' on greeks and NaN in places."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:                      # NaN
        return default
    if f <= -999.0:                 # documented sentinel
        return default
    return f


def flatten_chain(payload: dict, *, underlying: str, ts: int | None = None,
                  stale_ms: int = STALE_QUOTE_MS,
                  drop_deep_wings: bool = True) -> ChainFetchResult:
    """Schwab OptionChain -> validated flat rows + rejection counts.

    Pure function (no I/O) so it can be tested against a captured payload
    fixture, which is exactly how the CHART_EQUITY field-map bug was finally
    pinned down."""
    ts = ts or int(time.time() * 1000)
    # Rows are always stored under the canonical name ("SPX"), never the
    # request symbol ("$SPX"), so the store has one key per underlying.
    underlying = (underlying or "").strip().upper().lstrip("$")
    res = ChainFetchResult(underlying=underlying, ts=ts, underlying_px=None)

    if not isinstance(payload, dict):
        res.error = "chain payload was not a JSON object"
        return res

    status = payload.get("status")
    if status and str(status).upper() not in ("SUCCESS", "OK"):
        res.error = f"chain status={status!r}"

    und = payload.get("underlying") or {}
    res.underlying_px = (_num(payload.get("underlyingPrice"))
                         or _num(und.get("mark"))
                         or _num(und.get("last")))

    # SPX contracts carry the roots SPX and SPXW, never "SPX" alone; for an
    # ordinary ticker this is just {ticker} and the check is unchanged.
    roots = accepted_roots(underlying)

    for map_key, expected_right in (("callExpDateMap", "C"), ("putExpDateMap", "P")):
        exp_map = payload.get(map_key) or {}
        if not isinstance(exp_map, dict):
            continue
        for _exp_key, strikes in exp_map.items():
            if not isinstance(strikes, dict):
                continue
            for _strike_key, contracts in strikes.items():
                if not isinstance(contracts, list):
                    continue
                for c in contracts:
                    res.requested += 1
                    row = _validate_contract(
                        c, underlying=underlying, roots=roots,
                        ts=ts, expected_right=expected_right,
                        underlying_px=res.underlying_px, stale_ms=stale_ms,
                        drop_deep_wings=drop_deep_wings, reasons=res.reject_reasons)
                    if row is None:
                        res.rejected += 1
                    else:
                        res.rows.append(row)
    return res


def _validate_contract(c: dict, *, underlying: str, roots: frozenset[str],
                       ts: int, expected_right: str,
                       underlying_px: float | None, stale_ms: int,
                       drop_deep_wings: bool, reasons: Counter) -> SnapshotRow | None:
    if not isinstance(c, dict):
        reasons["not_an_object"] += 1
        return None
    sym = (c.get("symbol") or "").strip()
    if not sym:
        reasons["missing_symbol"] += 1
        return None
    if not is_occ(sym):
        reasons["bad_occ_symbol"] += 1
        return None
    try:
        occ = parse_occ(sym)
    except InvalidOccSymbol:
        reasons["bad_occ_symbol"] += 1
        return None

    # An adjusted or mini contract's root leaks in as e.g. "AAPL1". Reject
    # rather than store it under the wrong underlying: a chain that silently
    # mixes AAPL and AAPL1 will misprice everything downstream, because the
    # adjusted contract does not deliver 100 shares.
    if occ.underlying not in roots:
        reasons["root_mismatch"] += 1
        return None

    put_call = (c.get("putCall") or "").strip().upper()
    right = "C" if put_call.startswith("C") else "P" if put_call.startswith("P") else ""
    if right and right != occ.right:
        reasons["right_disagrees_with_symbol"] += 1
        return None
    if occ.right != expected_right:
        reasons["right_in_wrong_map"] += 1
        return None

    # /chains contracts name these `bid`/`ask`; /quotes OptionResponse names the
    # same fields `bidPrice`/`askPrice`. Accept both so a fixture captured from
    # either endpoint parses.
    bid = _num(c.get("bid"), _num(c.get("bidPrice")))
    ask = _num(c.get("ask"), _num(c.get("askPrice")))
    if bid is None or ask is None:
        reasons["missing_quote"] += 1
        return None
    if bid < 0 or ask <= 0:
        reasons["non_positive_quote"] += 1
        return None
    if bid > ask:
        reasons["crossed_market"] += 1
        return None

    strike = _num(c.get("strikePrice"))
    if strike is None or strike <= 0:
        reasons["bad_strike"] += 1
        return None
    if abs(strike - float(occ.strike)) > 1e-6:
        reasons["strike_disagrees_with_symbol"] += 1
        return None

    quote_ts = c.get("quoteTimeInLong")
    quote_ts = int(quote_ts) if isinstance(quote_ts, (int, float)) and quote_ts else None
    if quote_ts and ts - quote_ts > stale_ms:
        reasons["stale_quote"] += 1
        return None

    if c.get("nonStandard") or c.get("isNonStandard"):
        reasons["non_standard"] += 1
        return None
    if c.get("mini") or c.get("isMini"):
        reasons["mini"] += 1
        return None

    delta = _num(c.get("delta"))
    if drop_deep_wings and delta is not None:
        ad = abs(delta)
        if ad < MIN_ABS_DELTA or ad > MAX_ABS_DELTA:
            reasons["deep_wing"] += 1
            return None

    mark = _num(c.get("mark")) or _num(c.get("markPrice")) or (bid + ask) / 2
    multiplier = _num(c.get("multiplier"), 100.0) or 100.0

    return SnapshotRow(
        underlying=underlying, ts=ts, symbol=sym,
        expiry=expiry_to_ms(occ.expiry), strike=float(occ.strike), opt_right=occ.right,
        bid=bid, ask=ask, last=_num(c.get("last")), mark=mark,
        bid_size=_int(c.get("bidSize")), ask_size=_int(c.get("askSize")),
        volume=_int(c.get("totalVolume")), open_interest=_int(c.get("openInterest")),
        iv=_normalize_iv(_num(c.get("volatility"))),
        delta=delta, gamma=_num(c.get("gamma")), theta=_num(c.get("theta")),
        vega=_num(c.get("vega")), rho=_num(c.get("rho")),
        underlying_px=underlying_px, dte=_int(c.get("daysToExpiration")),
        multiplier=multiplier, is_non_standard=0, quote_ts=quote_ts,
    )


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _normalize_iv(v: float | None) -> float | None:
    """Schwab reports chain `volatility` in PERCENTAGE POINTS (e.g. 18.5 for
    18.5% vol) while every pricing formula wants a decimal (0.185). Storing the
    raw number would make every calibration off by 100x — and it would look
    'nearly right' on a 1%-vol instrument, which is the worst kind of wrong.
    Values above 5.0 are therefore read as percent."""
    if v is None or v <= 0:
        return None
    return v / 100.0 if v > 5.0 else v


def parse_expiration_chain(payload: dict) -> list[dict]:
    """GET /expirationchain -> rows for `option_expirations`."""
    out: list[dict] = []
    for e in (payload or {}).get("expirationList") or []:
        # `expirationDate` is what the wire actually sends; `expiration` is what
        # the spec's field table calls it. Reading only the documented name
        # returned an EMPTY list for every underlying — and because an empty
        # list is a legitimate answer for a symbol with no options, nothing
        # downstream could tell the difference between "none listed" and "we
        # parsed the wrong key". Same failure mode as bid/ask in `/chains`.
        raw = (e.get("expirationDate") or e.get("expiration") or "").strip()
        if not raw:
            continue
        try:
            d = dt.date.fromisoformat(raw)
        except ValueError:
            continue
        out.append({
            "expiry": expiry_to_ms(d),
            "expiration_type": e.get("expirationType"),
            "settlement_type": e.get("settlementType"),
            "standard": 1 if e.get("standard", True) else 0,
            "option_roots": e.get("optionRoots"),
        })
    return out


def quote_from_schwab_quote(symbol: str, payload: dict) -> ContractQuote | None:
    """One `GET /quotes` OptionResponse entry -> ContractQuote (source="live").

    Used by LivePricer. Returns None rather than raising for anything
    unusable — the caller's contract is that a missing quote BLOCKS the trade
    (plan/16 §9.2), so None is a meaningful, safe answer."""
    if not is_occ(symbol):
        return None
    q = (payload or {}).get("quote") or {}
    ref = (payload or {}).get("reference") or {}
    bid, ask = _num(q.get("bidPrice")), _num(q.get("askPrice"))
    if bid is None or ask is None or ask <= 0 or bid < 0 or bid > ask:
        return None
    try:
        occ = parse_occ(symbol)
    except InvalidOccSymbol:
        return None
    mark = _num(q.get("mark")) or (bid + ask) / 2
    contract = Contract(
        underlying=canonical_underlying(occ.underlying), expiry=occ.expiry, right=occ.right,
        strike=occ.strike, multiplier=_num(ref.get("multiplier"), 100.0) or 100.0)
    return ContractQuote(
        contract=contract,
        ts=int(_num(q.get("quoteTime"), time.time() * 1000) or time.time() * 1000),
        mid=mark, bid=bid, ask=ask,
        underlying_px=_num(q.get("underlyingPrice"), 0.0) or 0.0,
        iv=_normalize_iv(_num(q.get("volatility"))) or 0.0,
        greeks=Greeks(delta=_num(q.get("delta"), 0.0) or 0.0,
                      gamma=_num(q.get("gamma"), 0.0) or 0.0,
                      theta=_num(q.get("theta"), 0.0) or 0.0,
                      vega=_num(q.get("vega"), 0.0) or 0.0,
                      rho=_num(q.get("rho"), 0.0) or 0.0),
        volume=_int(q.get("totalVolume")), open_interest=_int(q.get("openInterest")),
        source="live")


class ChainService:
    """Fetches and caches live chains. One instance per app.

    The cache matters: the runner may evaluate many symbols on one bar close and
    a chain request is not cheap. TTL is short (30s) because a stale chain used
    for ENTRY sizing would produce an order at a price that no longer exists."""

    def __init__(self, schwab, store, *, cache_ttl_s: float = 30.0):
        self._schwab = schwab
        self._store = store
        self._ttl = cache_ttl_s
        self._cache: dict[tuple[str, int], tuple[float, ChainFetchResult]] = {}

    async def fetch(self, underlying: str, *, max_dte: int = 180,
                    strike_count: int = 60, contract_type: str = "ALL",
                    use_cache: bool = True,
                    stale_ms: int = STALE_QUOTE_MS) -> ChainFetchResult:
        underlying = (underlying or "").strip().upper().lstrip("$")
        now = time.time()
        if use_cache:
            # Keyed by the staleness bound too: an intraday snapshot asks for a
            # much tighter bound than a live selection call, and serving one
            # from the other's cached result would apply the wrong filter.
            hit = self._cache.get((underlying, stale_ms))
            if hit and now - hit[0] < self._ttl:
                return hit[1]
        today = dt.date.today()
        try:
            payload = await self._schwab.get_option_chain(
                market_data_symbol(underlying),
                contract_type=contract_type, strike_count=strike_count,
                from_date=today.isoformat(),
                to_date=(today + dt.timedelta(days=max_dte)).isoformat(),
                include_underlying_quote=True)
        except Exception as e:  # noqa: BLE001 — surfaced, never raised at the caller
            log.warning("chain_fetch_failed", underlying=underlying, error=str(e))
            res = ChainFetchResult(underlying=underlying, ts=int(now * 1000),
                                   underlying_px=None, error=str(e))
            return res
        res = flatten_chain(payload, underlying=underlying, ts=int(now * 1000),
                            stale_ms=stale_ms)
        self._cache[(underlying, stale_ms)] = (now, res)
        return res

    async def fetch_expirations(self, underlying: str) -> list[dict]:
        underlying = (underlying or "").strip().upper().lstrip("$")
        payload = await self._schwab.get_expiration_chain(
            market_data_symbol(underlying))
        return parse_expiration_chain(payload)

    def invalidate(self, underlying: str | None = None) -> None:
        if underlying is None:
            self._cache.clear()
            return
        u = (underlying or "").strip().upper().lstrip("$")
        # One underlying may be cached under several staleness bounds; drop all
        # of them, or an invalidate would leave a stale entry serving requests.
        for key in [k for k in self._cache if k[0] == u]:
            self._cache.pop(key, None)
