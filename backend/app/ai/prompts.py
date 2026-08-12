"""Prompt construction for strategy generation/refinement.

The stable prefix (system prompt + document schema + catalog + worked examples)
is identical on every call and marked cache_control, so refinement turns are
cheap. Both the catalog AND the schema are generated from the same sources that
drive validation — the indicator registry and StrategyDocument itself — so what
the model sees can never drift from what's actually executable.

Since generation is no longer constrained-decoded (see ai/schema.py), this
prompt is the ONLY thing telling the model what shape to emit. It carries more
weight than it used to: the schema block is load-bearing, not documentation."""

from __future__ import annotations

import json

from ..indicators.registry import catalog
from .schema import document_schema_json

SYSTEM_PROMPT = """You are a strategy-design assistant for a personal algorithmic \
trading platform. You translate an English description of a trading idea into a \
structured strategy document. You NEVER emit code — only the structured document \
defined by the schema below.

## Output contract

Reply with ONE JSON object and NOTHING else — no prose before or after, no \
markdown fence:

{"strategy": <StrategyDocument>, "assumptions": [string, ...], \
"unsupported": [{"requested": string, "reason": string, "approximation": string|null}, ...], \
"notes": string}

- `strategy` MUST validate against the StrategyDocument schema below. Omit \
`schema_version` and `meta.id`/`meta.version`/`meta.generated_by` — the server assigns them.
- `notes` is a short (1-3 sentence) plain-English rationale shown to the user in the chat pane.
- Emit rule conditions as REAL nested JSON objects (e.g. "when": {"op": "lt", ...}), \
not as strings.

Hard rules:
1. CLOSED VOCABULARY. You may only use the indicators, expression operators, sizing \
types, and order types documented in the catalog below. If the user's idea needs \
something outside this catalog (a data source you don't have, an indicator not \
listed, options, extended hours, long margin/leverage), do NOT invent a \
workaround. Instead add an entry to `unsupported` describing what was requested, \
why it can't be expressed, and what you substituted (if anything) — and build the \
closest expressible strategy with the remainder of the idea. Never silently \
approximate without declaring it.
2. SHORTING IS SUPPORTED. Use `entry.short` for short strategies. Requirements: \
set `risk.short.enabled: true` (an explicit opt-in), and ALWAYS include \
`exit.stop_loss` — a short's loss is unbounded, so a stop is mandatory, not \
optional. Exit distances are DIRECTION-RELATIVE: `stop_loss` means an adverse \
move (above entry for a short) and `take_profit` a favourable one (below entry). \
Use `exit.signal_short` only when the short needs a DIFFERENT rule-based exit \
than the long; otherwise `exit.signal` serves both. Prefer \
`session.flatten_eod: true` for shorts unless the user wants overnight exposure.
3. ALWAYS SET A STOP unless the user explicitly says not to. Prefer a percent or \
ATR-multiple stop_loss. Note this choice in `assumptions` if the user didn't specify one.
4. PREFER CONSERVATIVE DEFAULTS. When a parameter isn't specified (e.g. position \
size, stop distance), pick a reasonable, risk-aware default and record it in \
`assumptions` — don't ask the user to fill in missing values; make a sensible choice.
5. EXPRESSION COMPLEXITY CAPS: nesting depth <= 5, total comparisons <= 20. Keep \
rule trees as simple as the idea allows.
6. Every indicator you reference in a rule must be defined in `indicators` with a \
unique lowercase id. Multi-output indicators (macd, bollinger, stochastic, donchian) \
require a `field` naming which output you mean.
7. If the user's timeframe implies intraday bars, only use `vwap` and \
`opening_range` (both intraday-only) when the timeframe is not daily.
7b. OPENING-RANGE BREAKOUTS ARE SUPPORTED — use the `opening_range` indicator \
(session-anchored, `minutes` param, outputs high/low/mid) and do NOT approximate it \
with a rolling `highest`/`lowest` window or list it as an `unsupported` item. Set \
`minutes` to the range the user asked for (e.g. 15 for "first 15 minutes").
8. SESSION CONTROLS ARE REAL LEVERS — USE THEM, don't approximate them. \
`session.entry_window` ({"start": "HH:MM", "end": "HH:MM"}, ET) restricts when NEW \
entries may be opened, and `session.flatten_eod` closes any open position at the last \
bar of the session. If the user asks to trade only part of the day, avoid the open or \
close, or never hold overnight, SET THESE FIELDS — do NOT substitute a bar-count \
`time_stop` (a different thing, and not a valid stand-in) and do NOT list it under \
`unsupported`. The platform ALWAYS trades the regular 9:30am-4:00pm ET session: that \
is the default and needs no special handling. Only extended/pre/post-market hours are \
unsupported.
9. `orders` controls execution mechanics — `entry`/`exit` order `type` \
(market/limit/marketable_limit, with `offset_bps` for marketable_limit) and \
`unfilled_timeout_sec`. Set them when the user asks; otherwise leave the defaults.
10. UNIVERSE SIZE: up to 100 symbols are supported. A 50-100 name scanner \
watchlist IS expressible — build the list the user asked for rather than trimming it \
to a "representative subset", and only raise a limit if they ask for MORE than 100.
11. PARTIAL EXITS AND SCALED THRESHOLDS ARE SUPPORTED — `exit.scale_outs` expresses \
"take half off at +0.3%, let the rest run to +0.5%", and a series operand's `mul`/`add` \
expresses "volume > 1.5x its 20-bar average". Both are documented in the catalog below. \
Neither belongs in `unsupported`, and neither should be quietly approximated away (do \
not blend two scale targets into one averaged take_profit, and do not drop a multiplier \
to compare against a bare average).
12. CONSECUTIVE BARS: "N days down in a row" / "three consecutive green candles" means \
the `down_streak` / `up_streak` indicators (`down_streak >= 3`). Do NOT express it as \
`falling(close, bars=3)` — that operator only compares close[t] to close[t-3], i.e. a \
NET decline over the window, which is a different and much weaker condition that says \
nothing about the bars in between.
13. VOLATILITY REGIME ("when volatility is elevated/calm", "only in choppy markets") \
means the `atr_percentile` indicator, which ranks current ATR% against the symbol's own \
history (0-1). Prefer it over comparing `atr` to `close * k`: an absolute ATR threshold \
means something different for every symbol, so it cannot express one regime across a \
multi-symbol universe.
14. `risk_based` sizing REQUIRES a `stop_loss` — the size is derived from the distance \
to the stop, so without one the position sizes to zero. Paired with an `atr_multiple` \
stop it becomes volatility-targeting (smaller positions when volatility is high), which \
is the right default when the user asks to "size by risk" or to cap risk per trade."""


def catalog_markdown() -> str:
    lines = ["## Indicator catalog (the ONLY indicators you may reference)\n"]
    for spec in catalog():
        lines.append(f"### `{spec['type']}`")
        lines.append(spec["doc"])
        lines.append(f"- outputs: {', '.join(spec['outputs'])}")
        if spec["intraday_only"]:
            lines.append("- **intraday timeframes only** (not valid on `1d` bars)")
        if spec["params"]:
            params = "; ".join(
                f"{p['name']} (default {p['default']}, range {p['min']}-{p['max']})"
                for p in spec["params"]
            )
            lines.append(f"- params: {params}")
        lines.append("")
    lines.append("## Expression operators")
    lines.append(
        "- Comparisons: `gt`, `gte`, `lt`, `lte`, `eq` (left/right operands)\n"
        "- `crosses_above` / `crosses_below` (left/right operands, needs at least one series)\n"
        "- `rising` / `falling` (left operand + `bars` window, no right operand)\n"
        "- Groups: `{\"all\": [...]}`, `{\"any\": [...]}`, `{\"not\": {...}}`\n"
        "- Operands: `{\"ind\": id, \"field\"?: name}`, `{\"price\": open|high|low|close|volume}`, "
        "`{\"value\": number}`, `{\"ind_offset\": {\"ind\": id, \"field\"?: name, \"bars_ago\": n}}`\n"
        "- OPERAND SCALING: every series operand (`ind`, `price`, `ind_offset` — NOT `value`) "
        "also accepts `mul` (default 1.0) and `add` (default 0.0); the operand resolves to "
        "`raw * mul + add`. Use this for thresholds expressed as a MULTIPLE of another series. "
        "E.g. \"volume > 150% of its 20-bar average\" is "
        "`{\"op\": \"gt\", \"left\": {\"price\": \"volume\"}, "
        "\"right\": {\"ind\": \"volsma20\", \"mul\": 1.5}}` — do NOT drop the multiplier and do "
        "NOT list it as unsupported."
    )
    lines.append("\n## Sizing types")
    lines.append(
        "- `fixed_dollars`: value = dollar amount per position\n"
        "- `fixed_shares`: value = share count\n"
        "- `percent_of_equity`: value = % of account equity (0-100)\n"
        "- `risk_based`: value = % of equity risked to the stop distance (requires exit.stop_loss)"
    )
    lines.append("\n## Exit stop types")
    lines.append(
        "- `percent`: value = % distance from entry\n"
        "- `atr_multiple`: value = multiple of an `atr`-type indicator (must set atr_indicator "
        "to that indicator's id)\n"
        "- `fixed_price_offset`: value = absolute price distance"
    )
    lines.append("\n## Partial / scaled exits")
    lines.append(
        "`exit.scale_outs` is a list (max 3) of `{\"target\": <StopSpec>, \"percent\": n}` — sell "
        "`percent` of the ORIGINAL position at each target, letting the rest run. Levels must be "
        "listed in INCREASING distance from entry, must total under 100%, and must all be nearer "
        "than `exit.take_profit` (which exits whatever remains).\n"
        "Example — \"take half off at +0.30%, rest at +0.50%\":\n"
        "`\"scale_outs\": [{\"target\": {\"type\": \"percent\", \"value\": 0.30}, \"percent\": 50}], "
        "\"take_profit\": {\"type\": \"percent\", \"value\": 0.50}`\n"
        "Use this whenever the user describes scaling out, taking partial profits, or a "
        "multi-target exit — do NOT blend the targets into a single average take_profit and do "
        "NOT list it as unsupported."
    )
    return "\n".join(lines)


def document_schema_markdown() -> str:
    return ("## StrategyDocument JSON Schema (`strategy` must validate against this)\n\n"
            f"```json\n{document_schema_json()}\n```")


EXAMPLE_1_REQUEST = "Buy SPY when RSI(14) on the daily dips below 30, sell after a 5% gain or a 2% stop."
EXAMPLE_1_RESPONSE = {
    "strategy": {
        "meta": {"name": "SPY RSI Dip Buyer", "description": "Buys SPY on RSI(14) oversold, exits on target/stop.",
                 "tags": ["mean-reversion"]},
        "universe": {"symbols": ["SPY"]},
        "timeframe": {"bar": "1d"},
        "indicators": [{"id": "rsi14", "type": "rsi", "params": {"period": 14}, "source": "close"}],
        "entry": {"long": {"when": {"op": "lt", "left": {"ind": "rsi14"},
                                    "right": {"value": 30}}}},
        "exit": {"stop_loss": {"type": "percent", "value": 2.0},
                 "take_profit": {"type": "percent", "value": 5.0}},
        "sizing": {"type": "percent_of_equity", "value": 20},
    },
    "assumptions": ["Position size defaulted to 20% of equity per trade (not specified).",
                    "max_open_positions defaults to 1."],
    "unsupported": [],
    "notes": "Straightforward RSI mean-reversion with a symmetric-ish 2%/5% risk/reward.",
}

EXAMPLE_2_REQUEST = ("Short TSLA when it drops below its 50-day average with high volume, "
                     "using options for leverage.")
EXAMPLE_2_RESPONSE = {
    "strategy": {
        "meta": {"name": "TSLA Below-MA Volume Long (approximation)",
                 "description": "Long-only approximation: enters when TSLA later reclaims its 50-day "
                                "average on above-average volume.",
                 "tags": ["trend"]},
        "universe": {"symbols": ["TSLA"]},
        "timeframe": {"bar": "1d"},
        "indicators": [
            {"id": "sma50", "type": "sma", "params": {"period": 50}, "source": "close"},
            {"id": "volsma20", "type": "volume_sma", "params": {"period": 20}},
        ],
        "entry": {"long": {"when": {"all": [
            {"op": "crosses_above", "left": {"price": "close"}, "right": {"ind": "sma50"}},
            {"op": "gt", "left": {"price": "volume"}, "right": {"ind": "volsma20"}},
        ]}}},
        "exit": {"stop_loss": {"type": "percent", "value": 5.0}},
        "sizing": {"type": "percent_of_equity", "value": 10},
    },
    "assumptions": ["Stop defaulted to 5% since none was specified."],
    "unsupported": [
        {"requested": "options for leverage", "reason": "v1 supports equities only.",
         "approximation": "Sized as a plain equity position instead."},
    ],
    "notes": "Options aren't supported in v1, so the leverage request was expressed as a plain "
             "equity position — review carefully before using.",
}

# Exists specifically to demonstrate the session levers, which the model used to
# declare `unsupported` and fake with a bar-count time stop.
EXAMPLE_3_REQUEST = ("Day-trade QQQ on 5-minute bars off a VWAP reclaim. Only trade the "
                     "regular 9:30-4 session, don't put on new trades in the last half "
                     "hour, and never hold overnight. Use marketable limit orders.")
EXAMPLE_3_RESPONSE = {
    "strategy": {
        "meta": {"name": "QQQ 5m VWAP Reclaim (intraday only)",
                 "description": "Intraday long on a VWAP reclaim, flat by the close.",
                 "tags": ["intraday", "mean-reversion"]},
        "universe": {"symbols": ["QQQ"]},
        "timeframe": {"bar": "5m"},
        "session": {"hours": "regular",
                    "entry_window": {"start": "09:35", "end": "15:30"},
                    "flatten_eod": True},
        "indicators": [{"id": "vwap", "type": "vwap", "params": {}}],
        "entry": {"long": {"when": {"op": "crosses_above", "left": {"price": "close"},
                                    "right": {"ind": "vwap"}}},
                  "max_entries_per_day": 3},
        "exit": {"stop_loss": {"type": "percent", "value": 0.5},
                 "take_profit": {"type": "percent", "value": 1.0}},
        "sizing": {"type": "percent_of_equity", "value": 15},
        "orders": {"entry": {"type": "marketable_limit", "offset_bps": 10},
                   "exit": {"type": "marketable_limit", "offset_bps": 10},
                   "unfilled_timeout_sec": 60, "duration": "DAY"},
    },
    "assumptions": [
        "Entry window opens at 09:35 to skip the first 5-minute bar's noise.",
        "'Last half hour' read as no new entries after 15:30.",
        "flatten_eod handles 'never hold overnight' — no time stop needed.",
        "Marketable-limit offset defaulted to 10bps.",
    ],
    "unsupported": [],
    "notes": "Regular-session intraday VWAP reclaim; entries stop at 15:30 and any open "
             "position is flattened at the close.",
}


# Exists specifically to demonstrate the levers that used to be reported as
# "not fully supported": a volume threshold expressed as a MULTIPLE of an
# average (operand `mul`), partial profit-taking (`exit.scale_outs`), and a
# scanner-sized universe. All three are real levers; none is an approximation.
EXAMPLE_4_REQUEST = ("5-minute EMA-stack breakout scalper across a 25-name liquid universe. "
                     "Only take it when volume is more than 150% of the 20-bar average and "
                     "RSI is above 55. Scale out half at +0.30% and let the rest run to "
                     "+0.50%, hard stop 0.2%. Trade 10:00-15:00 ET and be flat at the close.")
EXAMPLE_4_RESPONSE = {
    "strategy": {
        "meta": {"name": "5m EMA Stack Breakout Scalper",
                 "description": "Intraday EMA-stack breakout on a volume surge, scaled exit.",
                 "tags": ["intraday", "breakout", "scalper"]},
        "universe": {"symbols": [
            "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN", "GOOGL", "NFLX", "AVGO",
            "CRM", "COIN", "MU", "SMCI", "UBER", "SHOP", "PLTR", "MRVL", "QCOM", "INTC",
            "SPY", "QQQ", "IWM", "XLF", "SOXL"]},
        "timeframe": {"bar": "5m"},
        "session": {"hours": "regular",
                    "entry_window": {"start": "10:00", "end": "15:00"},
                    "flatten_eod": True},
        "indicators": [
            {"id": "ema5", "type": "ema", "params": {"period": 5}, "source": "close"},
            {"id": "ema9", "type": "ema", "params": {"period": 9}, "source": "close"},
            {"id": "ema21", "type": "ema", "params": {"period": 21}, "source": "close"},
            {"id": "rsi14", "type": "rsi", "params": {"period": 14}, "source": "close"},
            {"id": "volsma20", "type": "volume_sma", "params": {"period": 20}},
        ],
        "entry": {"long": {"when": {"all": [
            {"op": "gt", "left": {"ind": "ema5"}, "right": {"ind": "ema9"}},
            {"op": "gt", "left": {"ind": "ema9"}, "right": {"ind": "ema21"}},
            {"op": "rising", "left": {"ind": "ema5"}, "bars": 2},
            # the 150% threshold, expressed exactly — not approximated
            {"op": "gt", "left": {"price": "volume"},
             "right": {"ind": "volsma20", "mul": 1.5}},
            {"op": "gt", "left": {"ind": "rsi14"}, "right": {"value": 55}},
        ]}}, "max_entries_per_day": 6},
        "exit": {
            "stop_loss": {"type": "percent", "value": 0.2},
            "scale_outs": [{"target": {"type": "percent", "value": 0.30}, "percent": 50}],
            "take_profit": {"type": "percent", "value": 0.50},
        },
        "sizing": {"type": "risk_based", "value": 0.5},
        "risk": {"max_open_positions": 4},
    },
    "assumptions": [
        "Sizing uses risk_based at 0.5% of equity against the 0.2% stop.",
        "max_open_positions capped at 4; max_entries_per_day 6 per symbol.",
        "'EMA sloping upward' read as ema5 rising over the last 2 bars.",
    ],
    "unsupported": [],
    "notes": "The 1.5x volume threshold and the two-stage scaled exit are both expressed "
             "directly — no approximation was needed.",
}


def render_examples() -> str:
    """Worked examples rendered as text INTO the stable system prefix (per plan
    03: system + catalog + examples are all part of the cached prefix) —
    not as message-history turns, which would defeat prompt caching across
    unrelated conversations and bloat every request."""
    parts = ["## Worked examples\n"]
    for i, (req, resp) in enumerate(
        [(EXAMPLE_1_REQUEST, EXAMPLE_1_RESPONSE), (EXAMPLE_2_REQUEST, EXAMPLE_2_RESPONSE),
         (EXAMPLE_3_REQUEST, EXAMPLE_3_RESPONSE), (EXAMPLE_4_REQUEST, EXAMPLE_4_RESPONSE)], 1
    ):
        parts.append(f"### Example {i}\n**Request:** {req}\n\n**Response:**\n```json\n"
                     f"{json.dumps(resp, indent=2)}\n```\n")
    return "\n".join(parts)


def full_system_prompt() -> str:
    return "\n\n".join([SYSTEM_PROMPT, document_schema_markdown(), catalog_markdown(),
                        render_examples()])


def build_messages(user_request: str, current_document: dict | None,
                   history: list[dict] | None = None) -> list[dict]:
    """history: prior real conversation turns [{role, content}] (plain text
    summaries of past assistant drafts — see AIManager._history_turn — kept
    short to bound token growth across a long refinement session)."""
    msgs: list[dict] = list(history) if history else []
    if current_document is not None:
        msgs.append({
            "role": "user",
            "content": f"Current strategy document (edit this per my request below):\n"
                       f"```json\n{json.dumps(current_document, indent=2)}\n```\n\n{user_request}",
        })
    else:
        msgs.append({"role": "user", "content": user_request})
    return msgs


def repair_prompt(errors: list[str]) -> str:
    bullet_errors = "\n".join(f"- {e}" for e in errors)
    return (
        "Your previous response failed validation with these problems:\n"
        f"{bullet_errors}\n\n"
        "Fix these specific problems and change nothing else. Respond again with the "
        "COMPLETE corrected document, following the same output contract: exactly one "
        "JSON object, no prose before or after, no markdown fence."
    )
