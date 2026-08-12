"""Options support (docs/plan/16-options-trading.md).

Deliberately self-contained: everything an option needs that equities do not —
OCC symbology, contract identity, Black-Scholes, the volatility surface, the
spread model, chain storage — lives here, so the equity path stays exactly as
it was. The rule engine (indicators, entry/exit expressions, market_filter,
selection) never imports from this package: options are an EXPRESSION of a
signal, not a second signal language (plan/16 decision D1).
"""
