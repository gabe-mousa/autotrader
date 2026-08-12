"""Which parts of a strategy document the LIVE/paper runtime does not honour
exactly as the backtest engine does — one list, four consumers.

This exists because of audit finding C1, which was the structural finding behind
the whole A-group: for seven schema fields the backtest engine implemented a
lever and `runtime/runner.py` silently ignored it, and NOTHING told the user.
The linter had zero checks referencing live-runtime support, the promotion gate
never inspected the document at all, and the only surface anywhere in the product
was one bullet buried in the Docs page that named three of the seven. A strategy
could be written, backtested, linted clean, paper-traded and promoted to LIVE
while trading a materially different document than the one that was tested.

The six field-level gaps that motivated this (`trailing_stop`, `entry.short`,
`selection`, `market_filter`, `session.entry_window`, `session.flatten_eod`,
`entry.pyramid`) have since been CLOSED, so this module deliberately ships
almost empty of them. That is the point: it is the mechanism, not the list. The
next lever someone adds to the engine and forgets in the runner gets registered
here and becomes visible in four places at once.

DESIGN — gaps are BLOCKING BY DEFAULT (`Gap.blocking`, default True). Adding a
gap without thinking about severity makes it block promotion to live, so the
failure mode of forgetting is over-caution rather than a silent trap. The
non-blocking ones each carry an explicit argument for why the divergence is
tolerable, in the `detail` a user actually reads.

Consumers:
 * `strategy/lint.py` — reports the BLOCKING gaps as lint problems, so they show
   up in the editor and in the AI repair loop. Deliberately not the non-blocking
   ones: those are documented approximations that apply to a large share of all
   strategies (44 set `flatten_eod`), and a permanent advisory on most documents
   would only train the user to ignore lint;
 * `runtime/runner.py` — logs a `live_support_gaps` run event at start, so a run's
   own event log records what it is not honouring;
 * `runtime/manager.py::promotion_status` — reports them, and refuses promotion
   to live while any BLOCKING gap is present unless explicitly overridden;
 * `frontend/src/pages/Running.tsx` — a badge on the run row.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schema import Orders, StrategyDocument

# `Orders.unfilled_timeout_sec`'s schema default — see the comment at its use.
_TIMEOUT_DEFAULT_S = Orders.model_fields["unfilled_timeout_sec"].default


@dataclass(frozen=True)
class Gap:
    """One way this document will behave differently live than in backtest.

    `field` is the dotted schema path, so a UI can link to the panel that owns
    it. `detail` is written for the user, not the developer: it says what will
    actually happen, not which function is missing."""

    field: str
    detail: str
    blocking: bool = True


def live_support_gaps(doc: StrategyDocument) -> list[Gap]:
    """Every live/backtest divergence this specific document is exposed to.

    Empty list = the live runtime honours everything this document asks for, to
    the accuracy noted on each non-blocking gap. Order is stable (schema order)
    so a UI can render it without sorting."""
    gaps: list[Gap] = []

    # ---- honoured by NEITHER engine nor runner --------------------------------
    # Reporting this is the whole reason A7 was not "implemented": the engine
    # fills exits at the bar open with no order-spec handling, so building it
    # live would have opened a NEW divergence rather than closing one.
    if doc.orders.exit.type != "market":
        gaps.append(Gap(
            "orders.exit",
            f"orders.exit is set to {doc.orders.exit.type!r}, but exits are sent as "
            "MARKET orders both live and in backtest — no part of the system reads "
            "this field. A marketable-limit exit configured to control slippage "
            "will not do so."))

    # Compared against the schema DEFAULT (60s), not against the 30s ceiling:
    # every document is clamped from 60 to 30, so reporting the ceiling would put
    # a gap on all ~76 strategies and mean nothing. Above the default the user
    # asked for something specific and is not getting it, which does mean
    # something.
    if doc.orders.unfilled_timeout_sec > _TIMEOUT_DEFAULT_S:
        gaps.append(Gap(
            "orders.unfilled_timeout_sec",
            f"{doc.orders.unfilled_timeout_sec}s is clamped to 30s live. The clamp is "
            "deliberate — the value bounds how long order submission blocks on the "
            "rate limiter, and blocking a run's event loop for minutes would stall "
            "every other symbol it trades.",
            blocking=False))

    # ---- implemented live, with a documented approximation --------------------
    intraday = doc.timeframe.bar != "1d"
    if intraday and doc.session.flatten_eod:
        gaps.append(Gap(
            "session.flatten_eod",
            "Enforced live from the 16:00 ET session close. On an EARLY-CLOSE day "
            "(the 13:00 ET half sessions) the backtest flattens on the 13:00 bar "
            "because that is where its data stops, while live waits for a 16:00 bar "
            "that never comes — there is no market calendar in the app to consult. "
            "The position is instead swept on the next session's first bar with exit "
            "reason 'eod_late', so a missed early close costs one logged overnight "
            "hold rather than an unbounded one.",
            blocking=False))

    if doc.entry.pyramid is not None and doc.entry.pyramid.max_adds > 1:
        gaps.append(Gap(
            "entry.pyramid",
            f"max_adds={doc.entry.pyramid.max_adds}: the backtest can take several "
            "adds within a single bar, live takes at most ONE per bar. Each add's "
            "level is spaced off the previous add's actual fill price, which live "
            "has not happened yet when the bar is evaluated. Live therefore builds "
            "the position no faster than backtest, never faster.",
            blocking=False))

    # ---- options (docs/plan/16-options-trading.md §7.5) -----------------------
    expr = getattr(doc, "expression", None)
    if expr is not None and expr.instrument == "options":
        # Backtest-only by design, and the divergence is EXPECTED rather than a
        # bug — but a user comparing a backtest fill to a live fill deserves to
        # be told why they differ before they conclude something is broken.
        gaps.append(Gap(
            "expression.pricing",
            "Backtest option prices are MODELLED (Black-Scholes over a volatility "
            "surface) because Schwab serves no historical option data. Live and "
            "paper runs use real quotes exclusively and never fall back to the "
            "model, so live fills WILL differ from backtest fills. Check "
            "/api/options/calibration for the measured model error on this "
            "underlying.",
            blocking=False))

        # The synthetic chain has no order book, so these filters are silently
        # unenforceable in backtest. Live enforces them, which means live takes
        # FEWER trades than backtest — the safe direction, but still a
        # divergence a user must know about when trade counts disagree.
        if expr.liquidity.min_open_interest or expr.liquidity.min_volume:
            gaps.append(Gap(
                "expression.liquidity",
                f"min_open_interest={expr.liquidity.min_open_interest} / "
                f"min_volume={expr.liquidity.min_volume} CANNOT be enforced in a "
                "backtest that prices synthetically — a generated chain has no "
                "order book. Live enforces both, so a live run may take fewer "
                "trades than its backtest. Fewer, never more.",
                blocking=False))

        # BLOCKING: rolling is not implemented in the runner yet, and a document
        # asking for it would silently just close instead, changing the strategy.
        if expr.exit.roll:
            gaps.append(Gap(
                "expression.exit.roll",
                "Rolling to the next expiration is NOT implemented in the live "
                "runner. A position reaching close_at_dte would simply be closed, "
                "which is a materially different strategy from the one backtested."))

        # BLOCKING-adjacent safety disclosure, non-blocking because the position
        # is defined-risk regardless: this is the resting-stop gap (plan/16 §9.4).
        if expr.exit.stop_basis == "underlying":
            gaps.append(Gap(
                "expression.exit.stop_basis",
                "An UNDERLYING-based stop cannot rest at the broker: no order type "
                "says 'sell this call if SPY drops below 580'. It is monitored "
                "in-process, so it depends on this app being alive and its data "
                "fresh. The position is still defined-risk — a long option cannot "
                "lose more than its premium — and "
                f"{'max_loss_pct_of_premium rests at the broker as a backstop'
                   if expr.exit.max_loss_pct_of_premium is not None
                   else 'NO broker-side protection is configured at all, because '
                        'max_loss_pct_of_premium is disabled'}.",
                blocking=expr.exit.max_loss_pct_of_premium is None))

    if doc.exit.scale_outs:
        gaps.append(Gap(
            "exit.scale_outs",
            "Placed live as resting partial limit orders. Crash recovery does NOT "
            "re-arm a partially-consumed ladder — which levels already filled is "
            "not journaled — so after a restart the remainder exits via "
            "stop/target/signal instead, and the run logs 'scale_outs_not_restored'.",
            blocking=False))

    return gaps


def blocking_gaps(doc: StrategyDocument) -> list[Gap]:
    """The subset that should stop a promotion to live absent an override."""
    return [gap for gap in live_support_gaps(doc) if gap.blocking]


def gap_payloads(doc: StrategyDocument) -> list[dict]:
    """JSON-ready form for run events, the promotion API and the UI."""
    return [{"field": gap.field, "detail": gap.detail, "blocking": gap.blocking}
            for gap in live_support_gaps(doc)]
