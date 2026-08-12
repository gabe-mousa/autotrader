"""OCC option symbology — parse and format the 21-character contract symbol.

The lowest level of the options stack: the broker, the streamer, the chain
recorder and every stored row key off this string, so a bug here is a bug
everywhere. Format is documented identically in both vendored Schwab specs
(`schwab/market-data-production/README.md` §Symbol Formats and
`schwab/trader-api--individual/README.md` §Options Symbology):

    RRRRRRYYMMDDsSSSSSsss        (exactly 21 characters)

    RRRRRR   root, LEFT-justified, SPACE-padded to 6
    YYMMDD   expiration date
    s        C or P
    SSSSSsss strike x 1000, zero-padded to 8 (5 whole digits + 3 decimal)

    AAPL  251219C00200000  =  AAPL, 2025-12-19, call, $200.00
    XYZ   210115C00062500  =  XYZ,  2021-01-15, call, $62.50

STRIKES ARE INTEGER THOUSANDTHS, NEVER FLOATS. `int(round(62.55 * 1000))` is
correct for the values that actually exist, but chaining float arithmetic into
a strike is how you produce a valid-LOOKING symbol for a contract that does not
exist — the order is then rejected, or worse, matches a different contract.
Decimal in, Decimal out, integer thousandths in between.

The space padding is real and must survive round-trips: it is what both the
REST API and the streamer expect on the wire.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

OCC_LEN = 21
ROOT_WIDTH = 6
STRIKE_WIDTH = 8
STRIKE_SCALE = 1000  # strike is encoded in thousandths of a dollar

#: Structural pattern. Deliberately permissive about the root's character set
#: (adjusted contracts carry roots like `AAPL1`, and index weeklies `SPXW`) but
#: strict about the fixed-width numeric tail, which is where a malformed symbol
#: actually does damage.
_OCC_RE = re.compile(r"^([A-Z0-9./ ]{6})(\d{6})([CP])(\d{8})$")

Right = str  # "C" | "P" — kept a plain str so callers need not import a Literal

#: Cash-settled index products, which break two assumptions that hold for every
#: listed equity and ETF:
#:
#:   1. The market-data request symbol is NOT the underlying's name. Schwab
#:      wants `$SPX`, and `SPX` is a flat 400 "Check Param Values" — the same
#:      `$`-prefixed convention `volsurface.VOL_INDEX_ANCHORS` already uses for
#:      `$VIX`.
#:   2. The contracts do NOT carry the underlying as their OCC root. SPX lists
#:      under BOTH `SPX` (AM-settled monthlies) and `SPXW` (PM-settled
#:      weeklies/dailies), and on a normal day nearly the whole chain is SPXW.
#:
#: Mapped underlying -> (request symbol, roots that legitimately belong to it).
#: Every entry below was verified against a live chain response; roots are not
#: guessed, because accepting a root that is not really this underlying's is the
#: exact mixing bug `_validate_contract`'s root check exists to prevent.
INDEX_PRODUCTS: dict[str, tuple[str, frozenset[str]]] = {
    "SPX":  ("$SPX",  frozenset({"SPX", "SPXW"})),
    "NDX":  ("$NDX",  frozenset({"NDX", "NDXP"})),
    "RUT":  ("$RUT",  frozenset({"RUT", "RUTW"})),
    "VIX":  ("$VIX",  frozenset({"VIX", "VIXW"})),
    "DJX":  ("$DJX",  frozenset({"DJX", "DJXW"})),
    "XSP":  ("$XSP",  frozenset({"XSP"})),
    "OEX":  ("$OEX",  frozenset({"OEX"})),
    "XEO":  ("$XEO",  frozenset({"XEO"})),
}


#: Root -> the underlying it settles against ("SPXW" -> "SPX"). Derived from
#: INDEX_PRODUCTS so the two can never drift apart.
_ROOT_TO_UNDERLYING: dict[str, str] = {
    root: underlying
    for underlying, (_sym, roots) in INDEX_PRODUCTS.items()
    for root in roots
}


def canonical_underlying(root: str) -> str:
    """An OCC root -> the underlying it settles against.

    `SPXW` -> `SPX`, `AAPL` -> `AAPL`. `parse_occ` deliberately does NOT do this
    — it stays faithful to the symbol so `to_symbol()` round-trips — so anything
    that GROUPS by underlying (position lookup, risk, the chain store, matching
    a quote to its underlying price) must canonicalize explicitly, or SPX
    weeklies end up filed under a ticker that no underlying bar ever matches."""
    return _ROOT_TO_UNDERLYING.get(_canon(root), _canon(root))


def is_index_underlying(underlying: str) -> bool:
    return _canon(underlying) in INDEX_PRODUCTS


def market_data_symbol(underlying: str) -> str:
    """Underlying -> the symbol Schwab's market-data endpoints want.

    `SPX` -> `$SPX`; `AAPL` -> `AAPL`. Already-prefixed input passes through, so
    a caller that stores `$SPX` is not double-prefixed into `$$SPX`."""
    u = _canon(underlying)
    if u.startswith("$"):
        return u
    entry = INDEX_PRODUCTS.get(u)
    return entry[0] if entry else u


def accepted_roots(underlying: str) -> frozenset[str]:
    """The OCC roots whose contracts really do settle against `underlying`.

    For an equity that is just the ticker itself. Note that `AAPL1` is NOT
    accepted for `AAPL`: an adjusted contract does not deliver 100 shares, so
    treating it as ordinary AAPL misprices it."""
    u = _canon(underlying).lstrip("$")
    entry = INDEX_PRODUCTS.get(u)
    return entry[1] if entry else frozenset({u})


def _canon(underlying: str) -> str:
    return (underlying or "").strip().upper()


class InvalidOccSymbol(ValueError):
    """A string that is not a well-formed OCC option symbol."""


@dataclass(frozen=True, order=True)
class OccSymbol:
    """A parsed contract identity.

    `strike` is a Decimal so equality and hashing are exact — two contracts at
    $62.50 must compare equal regardless of how each was constructed.
    """

    underlying: str      # unpadded, upper-case: "AAPL"
    expiry: date
    right: str           # "C" | "P"
    strike: Decimal      # exact, e.g. Decimal("62.500")

    def __post_init__(self) -> None:
        if self.right not in ("C", "P"):
            raise InvalidOccSymbol(f"right must be 'C' or 'P', got {self.right!r}")

    @property
    def is_call(self) -> bool:
        return self.right == "C"

    @property
    def is_put(self) -> bool:
        return self.right == "P"

    def to_symbol(self) -> str:
        return format_occ(self.underlying, self.expiry, self.right, self.strike)

    def describe(self) -> str:
        """Human-readable form for logs, run events and the UI —
        `AAPL 19Dec25 200C`. Never parse this back; it is lossy by design."""
        d = self.expiry.strftime("%d%b%y")
        return f"{self.underlying} {d} {_fmt_strike(self.strike)}{self.right}"


def _fmt_strike(strike: Decimal) -> str:
    """Trim trailing zeros for display: 200.000 -> '200', 62.500 -> '62.5'.

    The `"." in s` guard is load-bearing: a bare `.rstrip("0")` turns "200.000"
    into "2", because it eats the integer part's zeros once the decimal point
    is gone."""
    s = f"{strike:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


#: How far a supplied strike may sit from an exact thousandth before it is
#: treated as a real error rather than float noise. A tenth of a cent is finer
#: than any listed strike, so nothing legitimate is lost.
_STRIKE_EPS = Decimal("0.0000005")


def _to_thousandths(strike: Decimal | float | int | str) -> int:
    """Strike -> integer thousandths.

    Floats are quantized through their shortest repr, which is what makes
    `62.5` land on 62500 rather than 62499. But arithmetic-derived floats do
    not have clean reprs — `0.1 * 3` is `0.30000000000000004`, and callers
    computing a strike as `price * 1.05` produce such values routinely — so the
    value is ROUNDED to thousandths and then checked: within `_STRIKE_EPS` it
    was float noise and the rounding stands; beyond it, the caller genuinely
    asked for a strike finer than $0.001, which no contract has, and that is a
    real error worth raising on rather than silently rounding away."""
    if isinstance(strike, Decimal):
        d = strike
    elif isinstance(strike, int):
        d = Decimal(strike)
    elif isinstance(strike, float):
        d = Decimal(repr(strike))
    else:
        d = Decimal(str(strike).strip())
    scaled = d * STRIKE_SCALE
    as_int = int(scaled.to_integral_value(rounding=ROUND_HALF_UP))
    if abs(scaled - as_int) > _STRIKE_EPS * STRIKE_SCALE:
        raise InvalidOccSymbol(
            f"strike {strike!r} is not a multiple of $0.001 "
            f"(would encode as {scaled}); no listed contract has finer granularity")
    return as_int


def format_occ(underlying: str, expiry: date, right: str,
               strike: Decimal | float | int | str) -> str:
    """Build the 21-character symbol. Raises `InvalidOccSymbol` on anything it
    cannot represent EXACTLY — never truncates a long root and never rounds a
    strike, because both produce a plausible symbol for the wrong contract."""
    root = (underlying or "").strip().upper()
    if not root:
        raise InvalidOccSymbol("empty underlying")
    if len(root) > ROOT_WIDTH:
        raise InvalidOccSymbol(
            f"underlying {root!r} exceeds the {ROOT_WIDTH}-character OCC root field")
    if not re.fullmatch(r"[A-Z0-9./]+", root):
        raise InvalidOccSymbol(f"underlying {root!r} has characters not valid in an OCC root")
    r = (right or "").strip().upper()
    if r in ("CALL",):
        r = "C"
    elif r in ("PUT",):
        r = "P"
    if r not in ("C", "P"):
        raise InvalidOccSymbol(f"right must be C/P (or call/put), got {right!r}")
    if not isinstance(expiry, date):
        raise InvalidOccSymbol(f"expiry must be a date, got {type(expiry).__name__}")

    thousandths = _to_thousandths(strike)
    if thousandths <= 0:
        raise InvalidOccSymbol(f"strike must be positive, got {strike!r}")
    if thousandths > 10 ** STRIKE_WIDTH - 1:
        raise InvalidOccSymbol(
            f"strike {strike!r} exceeds the {STRIKE_WIDTH}-digit OCC strike field")

    return (f"{root:<{ROOT_WIDTH}}"
            f"{expiry:%y%m%d}"
            f"{r}"
            f"{thousandths:0{STRIKE_WIDTH}d}")


def parse_occ(symbol: str) -> OccSymbol:
    """Inverse of `format_occ`. Raises `InvalidOccSymbol` rather than returning
    None: every caller that reaches here has already decided this is an option
    symbol, so a failure is a real error worth a stack trace."""
    if not isinstance(symbol, str):
        raise InvalidOccSymbol(f"expected str, got {type(symbol).__name__}")
    if len(symbol) != OCC_LEN:
        raise InvalidOccSymbol(
            f"OCC symbol must be exactly {OCC_LEN} characters, got {len(symbol)}: {symbol!r}")
    m = _OCC_RE.match(symbol)
    if not m:
        raise InvalidOccSymbol(f"malformed OCC symbol: {symbol!r}")
    root_raw, ymd, right, strike_raw = m.groups()
    root = root_raw.strip()
    if not root:
        raise InvalidOccSymbol(f"OCC symbol has an empty root: {symbol!r}")
    try:
        expiry = date(2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError as e:
        raise InvalidOccSymbol(f"invalid expiration date in {symbol!r}: {e}") from e
    strike = Decimal(int(strike_raw)) / STRIKE_SCALE
    if strike <= 0:
        raise InvalidOccSymbol(f"non-positive strike in {symbol!r}")
    return OccSymbol(underlying=root, expiry=expiry, right=right, strike=strike)


def is_occ(symbol: str) -> bool:
    """Cheap, total discriminator: is this an option symbol or an equity ticker?

    Called on every symbol in the runner, gateway and broker to pick a code
    path, so it must be fast and must NEVER raise — a malformed string is
    simply 'not an option', and the equity path's own validation deals with it.

    It must also agree with `parse_occ`: anything this accepts, `parse_occ` must
    successfully parse. A pure structural regex is NOT enough for that —
    `AAPL  251332C00200000` matches the shape but has month 13, and a caller
    that trusted `is_occ` would then hit an unguarded raise deep in the order
    path. So the date and the root are validated here too; both checks are a
    handful of integer comparisons and do not change the cost meaningfully."""
    if not isinstance(symbol, str) or len(symbol) != OCC_LEN:
        return False
    m = _OCC_RE.match(symbol)
    if m is None:
        return False
    root_raw, ymd, _right, strike_raw = m.groups()
    if not root_raw.strip():
        return False
    if int(strike_raw) <= 0:
        return False
    try:
        date(2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        return False
    return True


def underlying_of(symbol: str) -> str:
    """The underlying ticker for either kind of symbol. An equity symbol is its
    own underlying, which lets aggregation code (risk grouping, UI grouping,
    position lookup) treat both uniformly without branching."""
    if is_occ(symbol):
        return canonical_underlying(parse_occ(symbol).underlying)
    return _canon(symbol).lstrip("$")


def describe(symbol: str) -> str:
    """Display form for any symbol; falls back to the raw string."""
    try:
        return parse_occ(symbol).describe() if is_occ(symbol) else symbol
    except InvalidOccSymbol:
        return symbol
