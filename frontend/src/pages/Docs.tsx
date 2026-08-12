import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getCatalog } from '../lib/api'
import CollapsiblePanel from '../components/editor/CollapsiblePanel'

type SectionId =
  | 'first-time-setup'
  | 'overview'
  | 'primitives'
  | 'options'
  | 'market-data'
  | 'metrics'
  | 'fills'
  | 'promotion'
  | 'safety'
  | 'auth'
  | 'no-trade'

const SECTIONS: { id: SectionId; label: string }[] = [
  { id: 'first-time-setup', label: 'First-time setup' },
  { id: 'overview', label: 'Overview & how to use it' },
  { id: 'primitives', label: 'Primitive reference' },
  { id: 'options', label: 'Options' },
  { id: 'market-data', label: 'Market data' },
  { id: 'metrics', label: 'Metric reference' },
  { id: 'fills', label: 'Fill simulation' },
  { id: 'promotion', label: 'Promotion gate' },
  { id: 'safety', label: 'Safety rails' },
  { id: 'auth', label: 'Auth lifecycle' },
  { id: 'no-trade', label: "Why didn't it trade?" },
]

const CMP_OPS: { op: string; doc: string }[] = [
  { op: 'gt / gte / lt / lte / eq', doc: 'Standard numeric comparisons between two operands.' },
  { op: 'crosses_above', doc: 'Left operand was ≤ right on the previous bar and is > right on this bar.' },
  { op: 'crosses_below', doc: 'Left operand was ≥ right on the previous bar and is < right on this bar.' },
  {
    op: 'rising',
    doc: 'Left operand is strictly greater than it was `bars` bars ago. NOTE this is a NET change over the window, not a run of consecutive up bars — for "N bars up in a row" use the up_streak indicator.',
  },
  {
    op: 'falling',
    doc: 'Left operand is strictly less than it was `bars` bars ago. NOTE this is a NET change over the window, not a run of consecutive down bars — for "N bars down in a row" use the down_streak indicator.',
  },
]

const EXPR_OPS: { op: string; doc: string }[] = [
  { op: 'all', doc: 'True only if every child expression is true (logical AND). 1-10 children.' },
  { op: 'any', doc: 'True if at least one child expression is true (logical OR). 1-10 children.' },
  { op: 'not', doc: 'Negates a single child expression.' },
]

const OPERAND_KINDS: { kind: string; doc: string }[] = [
  { kind: 'ind', doc: 'References an indicator by id (and output `field` for multi-output indicators like macd/bollinger).' },
  { kind: 'price', doc: 'A raw OHLCV field: open, high, low, close, volume.' },
  { kind: 'value', doc: 'A fixed numeric constant.' },
  { kind: 'ind_offset', doc: 'An indicator value looked back `bars_ago` (1-100) bars.' },
]

const ORDER_TYPES: { type: string; doc: string }[] = [
  { type: 'market', doc: 'Default. Fills immediately at (simulated or real) market price ± slippage.' },
  { type: 'limit', doc: 'Rests at a specified price; fills only on a price cross, never on proximity.' },
  { type: 'marketable_limit', doc: 'A limit priced `offset_bps` through the market to behave like a marketable order while capping worst-case slippage.' },
]

const STOP_TYPES: { type: string; doc: string }[] = [
  { type: 'percent', doc: 'Stop/target distance as a percentage of entry price.' },
  { type: 'atr_multiple', doc: 'Stop/target distance as a multiple of a referenced ATR indicator\'s current value (requires atr_indicator).' },
  { type: 'fixed_price_offset', doc: 'Stop/target distance as a fixed dollar offset from entry price.' },
]

const SIZING_TYPES: { type: string; doc: string }[] = [
  { type: 'fixed_dollars', doc: 'A fixed dollar notional per position.' },
  { type: 'fixed_shares', doc: 'A fixed share count per position.' },
  { type: 'percent_of_equity', doc: 'value% of current account equity per position.' },
  {
    type: 'risk_based',
    doc: 'qty = (equity × value%) ÷ |entry − stop|. REQUIRES a stop_loss — with no stop there is no risk distance and the position sizes to zero. The distance is absolute, so it works on both sides (a short\'s stop sits above entry). Paired with an atr_multiple stop this becomes volatility targeting: positions shrink automatically when volatility rises, which is what keeps a mean-reversion book survivable in a crash.',
  },
]

const METRIC_DOCS: { key: string; label: string; text: string }[] = [
  {
    key: 'total_return_pct',
    label: 'Total return %',
    text: 'Percent change from starting to ending equity over the whole window, including any still-open position marked to market. The single top-line number, but it says nothing about the ride getting there — pair with max drawdown.',
  },
  {
    key: 'cagr_pct',
    label: 'CAGR %',
    text: 'Total return annualized (compounded) over the actual elapsed calendar time of the window. On short windows (weeks) this annualizes noise into a huge or deeply negative-looking number — treat CAGR from short backtests as meaningless, it only becomes informative over many months.',
  },
  {
    key: 'sharpe',
    label: 'Sharpe',
    text: 'Risk-adjusted return: mean per-bar return over its volatility, annualized by bar frequency (252 trading days/year for daily bars). Above ~1 is decent, above ~2 is strong for a single strategy — but short backtest windows make this extremely noisy, don\'t over-trust a high Sharpe from a 2-week test with a handful of trades.',
  },
  {
    key: 'sortino',
    label: 'Sortino',
    text: 'Like Sharpe, but only penalizes downside volatility (losing bars), not all volatility. Null if there are no losing bars to compute a downside deviation from — a symptom of too little data, not a good sign.',
  },
  {
    key: 'max_drawdown_pct',
    label: 'Max drawdown %',
    text: 'The largest peak-to-trough decline in the equity curve during the window. The most direct "how bad did it get" number; a strategy with great returns but a brutal drawdown may be undeployable psychologically even if the math works out.',
  },
  {
    key: 'calmar',
    label: 'Calmar',
    text: 'CAGR divided by the absolute max drawdown — return per unit of worst pain endured. Inherits CAGR\'s short-window noise problem, so treat it the same way: informative over months, not over a couple of weeks.',
  },
  {
    key: 'win_rate_pct',
    label: 'Win rate %',
    text: 'Percent of closed trades that were profitable. A high win rate with a low profit factor usually means many small wins funding a few large losses — always read it alongside profit factor and expectancy, never alone.',
  },
  {
    key: 'profit_factor',
    label: 'Profit factor',
    text: 'Gross profit from winning trades divided by gross loss from losing trades. Above 1 means the strategy made more than it lost in total; below 1 means it\'s a net loser regardless of win rate. Null if there were no losing trades to divide by.',
  },
  {
    key: 'expectancy',
    label: 'Expectancy',
    text: 'Average P&L per closed trade in dollars. The number that tells you what a "typical" trade is worth — multiply by expected trade frequency to get a rough sense of P&L per week/month.',
  },
  {
    key: 'trade_count',
    label: 'Trades',
    text: 'Number of closed round trips (entry + exit) in the window. Low trade counts make every other statistic on this page unreliable — a handful of trades cannot establish an edge.',
  },
  {
    key: 'exposure_pct',
    label: 'Exposure %',
    text: 'Percent of bars in the window during which the strategy held a position. Low exposure means most of the return story is about the entries you took, not market beta; high exposure means the strategy behaves more like buy-and-hold.',
  },
  {
    key: 'monthly_returns',
    label: 'Monthly / yearly returns',
    text: 'Percent change in equity over each calendar month, or each calendar year when you switch the table to "by year". The first period is measured from opening equity rather than dropped, so partial first months count and the yearly figures compound to the same total return shown above. Yearly returns compound the months, they do not sum them.',
  },
  {
    key: 'avg_bars_held',
    label: 'Avg bars held',
    text: 'Average number of bars between entry and exit across closed trades, in the strategy\'s own timeframe (e.g. daily bars vs 5-minute bars mean very different holding periods for the same number).',
  },
  {
    key: 'open_positions_at_end',
    label: 'Open at end',
    text: 'Count of positions still open when the backtest window ended. These are marked-to-market into the equity curve (so they DO affect Sharpe/return/drawdown above) but are not closed round trips, so they will not appear in the Trades table — see "Why didn\'t it trade?" below if this makes the numbers look mysterious.',
  },
]

const PAGE_GUIDE: { name: string; path: string; doc: string }[] = [
  {
    name: 'Dashboard',
    path: '/',
    doc: "Home screen: today's paper P&L across all active runs, how many strategies are running/paused, open positions and orders placed today, a live account-balances table (with a per-row privacy toggle), and per-strategy status tiles you can click into.",
  },
  {
    name: 'Strategies',
    path: '/strategies',
    doc: 'The library of every strategy document on disk. Create new ones by hand or via AI generation, open any existing one in the editor, and see its lint/live-support status at a glance.',
  },
  {
    name: 'Strategy editor',
    path: '/strategies/:slug',
    doc: 'Build or edit a strategy: indicators, entry/exit rules, sizing, risk limits, and (optionally) an options expression block. Lints live as you edit and previews against a chart before you ever run a backtest.',
  },
  {
    name: 'Running',
    path: '/running',
    doc: 'Every paper and live run, tabbed, with live state, positions, and controls (pause/resume/stop). This is where a backtested strategy actually starts trading.',
  },
  {
    name: 'Run detail',
    path: '/running/:runId',
    doc: 'Deep view of one run: live positions, order/fill history, the gateway decision journal, and any live-support gaps flagged for that specific strategy.',
  },
  {
    name: 'Backtests',
    path: '/backtests',
    doc: 'History of every backtest that has been run, with quick access to results, comparisons between runs, and parameter sweeps.',
  },
  {
    name: 'Backtest result',
    path: '/backtests/:id',
    doc: "One backtest's full detail: equity curve, trade-by-trade table, and the full metric set (see Metric reference below).",
  },
  {
    name: 'Optimize',
    path: '/optimize',
    doc: 'Walk-forward parameter sweeps with a holdout period, to search a strategy\'s lever space without simply curve-fitting to one window.',
  },
  {
    name: 'Charts',
    path: '/charts',
    doc: "Standalone charting over the app's own recorded candle history, independent of any strategy.",
  },
  {
    name: 'Chains',
    path: '/chains',
    doc: 'Recorded option chain snapshots, plus the calibration view comparing the synthetic pricing model against what was actually observed.',
  },
  {
    name: 'Orders',
    path: '/orders',
    doc: 'Flat, filterable log of every order this app has placed (paper and live) across all runs.',
  },
  {
    name: 'Data',
    path: '/data',
    doc: 'Market-data recorder controls and status: backfill progress, streamer connection health, snapshot density, and tick-recording settings.',
  },
  {
    name: 'Docs',
    path: '/docs',
    doc: "This page — the reference for every lever, metric, and behavior in the app, generated from the same schema/catalog the app runs on wherever possible.",
  },
  {
    name: 'Settings',
    path: '/settings',
    doc: 'Schwab OAuth connection status and re-auth, plus global safety-limit defaults (max notional, max daily loss, slippage assumptions).',
  },
]

function SectionHeading({ id, children }: { id: string; children: string }) {
  return (
    <h2 id={id} className="mb-3 scroll-mt-4 text-lg font-semibold text-gray-100">
      {children}
    </h2>
  )
}

function MiniTable({
  head,
  rows,
}: {
  head: string[]
  rows: (string | number)[][]
}) {
  return (
    <div className="overflow-x-auto rounded border border-white/10">
      <table className="w-full text-left text-sm">
        <thead className="bg-white/5 text-gray-400">
          <tr>
            {head.map((h) => (
              <th key={h} className="px-3 py-2 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-white/10">
              {r.map((c, j) => (
                <td key={j} className="px-3 py-2 align-top text-gray-300">
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function Docs() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['catalog'],
    queryFn: getCatalog,
    retry: false,
  })

  const [active, setActive] = useState<SectionId>('overview')

  useEffect(() => {
    const hash = window.location.hash.replace('#', '')
    if (hash && SECTIONS.some((s) => s.id === hash)) {
      setActive(hash as SectionId)
      requestAnimationFrame(() => {
        document.getElementById(hash)?.scrollIntoView({ block: 'start' })
      })
    }
  }, [])

  function goTo(id: SectionId) {
    setActive(id)
    window.location.hash = id
    document.getElementById(id)?.scrollIntoView({ block: 'start' })
  }

  return (
    <div className="flex gap-8">
      <nav className="sticky top-4 h-fit w-48 shrink-0 space-y-1 text-sm">
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            onClick={() => goTo(s.id)}
            className={`block w-full rounded px-2 py-1.5 text-left ${
              active === s.id
                ? 'bg-white/10 text-gray-100'
                : 'text-gray-400 hover:bg-white/5 hover:text-gray-200'
            }`}
          >
            {s.label}
          </button>
        ))}
      </nav>

      <div className="min-w-0 flex-1 space-y-10 pb-16">
        <div>
          <h1 className="mb-1 text-xl font-semibold text-gray-100">Docs</h1>
          <p className="text-sm text-gray-400">
            Reference for every lever, metric, and behavior in this app — generated from the
            same schema/metadata the app itself runs on where possible, and describing only
            what's actually implemented today.
          </p>
        </div>

        {/* ---- Section -1: first-time setup ---- */}
        <section>
          <SectionHeading id="first-time-setup">First-time setup</SectionHeading>
          <p className="mb-4 text-sm leading-relaxed text-gray-400">
            What to do the first time you run this app, in order. Everything here can be
            changed later — nothing is permanent.
          </p>

          <ol className="mb-6 space-y-4 text-sm text-gray-400">
            <li className="rounded border border-white/10 p-3">
              <p className="mb-1 font-semibold text-gray-200">1. Get a Schwab developer app</p>
              <p>
                This app trades through your own Schwab account, so it needs its own Schwab
                developer-portal app registration (free, a few minutes) to get a client ID and
                secret. Register the callback URL exactly as{' '}
                <code className="rounded bg-white/5 px-1">
                  https://127.0.0.1:8443/oauth/callback
                </code>{' '}
                — it has to match character-for-character.
              </p>
            </li>
            <li className="rounded border border-white/10 p-3">
              <p className="mb-1 font-semibold text-gray-200">2. Add your credentials</p>
              <p>
                Either paste the client ID/secret into the project's{' '}
                <code className="rounded bg-white/5 px-1">.env</code> file directly, or run the
                app and enter them on the{' '}
                <span className="text-gray-200">Settings</span> page — Settings saves straight
                back into <code className="rounded bg-white/5 px-1">.env</code> for you.
                Whichever way you go, the backend needs a restart afterward to pick the new
                values up.
              </p>
            </li>
            <li className="rounded border border-white/10 p-3">
              <p className="mb-1 font-semibold text-gray-200">3. Connect Schwab</p>
              <p>
                On the Settings page, click <span className="text-gray-200">Connect Schwab</span>{' '}
                — it opens Schwab's own login/consent page in a new tab. Once approved, the app
                holds a token it refreshes automatically; you'll only need to redo this when the
                7-day refresh token lapses (see "Auth lifecycle" below).
              </p>
            </li>
            <li className="rounded border border-white/10 p-3">
              <p className="mb-1 font-semibold text-gray-200">4. Add symbols to start recording data</p>
              <p>
                On the <span className="text-gray-200">Data</span> page, add the symbols you're
                interested in and run a backfill. This gets you years of daily history instantly;
                for intraday timeframes, the app only has what it has recorded, so the sooner a
                symbol is added the more history you'll have to test against later — see "Market
                data" below.
              </p>
            </li>
            <li className="rounded border border-white/10 p-3">
              <p className="mb-1 font-semibold text-gray-200">5. Build and backtest a strategy</p>
              <p>
                Go to <span className="text-gray-200">Strategies</span> and create one — by hand,
                or from a plain-English description if you've added an Anthropic API key (optional,
                only needed for AI-assisted generation). Run a backtest before doing anything else
                with it.
              </p>
            </li>
            <li className="rounded border border-white/10 p-3">
              <p className="mb-1 font-semibold text-gray-200">6. Paper trade before anything real</p>
              <p>
                Start a paper run from <span className="text-gray-200">Running</span> — simulated
                fills against real live quotes, no real money at risk. This is required before a
                strategy is eligible to go live at all.
              </p>
            </li>
          </ol>

          <div className="rounded border border-amber-500/20 bg-amber-500/5 p-4">
            <p className="mb-2 text-sm font-semibold text-gray-200">
              A few levers worth knowing about early
            </p>
            <ul className="list-disc space-y-1.5 pl-5 text-sm text-gray-400">
              <li>
                <span className="text-gray-200">Max total notional</span> (Settings → Global risk
                limits) — the most dollar value you can have deployed across every open position
                at once, added up across all strategies. It's a ceiling on total exposure, not a
                per-trade limit.
              </li>
              <li>
                <span className="text-gray-200">Max total daily loss</span> (same section) — once
                everything running together has lost more than this in a day, new entries are
                blocked for the rest of the day. Existing positions can still be closed — this
                only stops new risk from being added on top of a bad day.
              </li>
              <li>
                <span className="text-gray-200">Live order placement</span> is off by default
                everywhere in the app. You can turn it on from{' '}
                <span className="text-gray-200">Settings → Live trading</span> — no need to touch{' '}
                <code className="rounded bg-white/5 px-1">.env</code> by hand. Turning it on
                doesn't place any orders by itself: a strategy still has to be deliberately
                promoted to live after paper trading, and the standalone Live probe page (for
                placing manual test orders) is gated behind this same switch.
              </li>
              <li>
                <span className="text-gray-200">The kill switch</span> stops all new order
                placement everywhere, instantly, regardless of what any strategy is doing — good
                to know it exists before you need it.
              </li>
              <li>
                <span className="text-gray-200">The promotion gate</span> (see below) sets the
                minimum paper-trading track record — days elapsed and round-trip trades — a
                strategy needs before it can go live. The defaults are conservative on purpose.
              </li>
            </ul>
          </div>
        </section>

        {/* ---- Section 0: overview / how to use the app ---- */}
        <section>
          <SectionHeading id="overview">Overview & how to use it</SectionHeading>

          <h3 className="mb-2 text-sm font-semibold text-gray-200">What this app is</h3>
          <p className="mb-4 text-sm leading-relaxed text-gray-400">
            A single-user, localhost-only platform for designing, backtesting, paper-trading,
            and (optionally) live-trading rule-based strategies against a personal Schwab
            account — equities and long options (calls/puts). There's no freeform strategy
            code: every strategy is a YAML document built from a fixed set of indicators,
            comparison rules, sizing, and risk levers, which is what makes the exact same
            evaluation logic reusable across backtest, paper, and live.
          </p>

          <h3 className="mb-2 text-sm font-semibold text-gray-200">The strategy lifecycle</h3>
          <ol className="mb-4 list-decimal space-y-1.5 pl-5 text-sm text-gray-400">
            <li>
              <span className="text-gray-200">Build.</span> Author a strategy by hand in the
              editor (Strategies → New) or generate a draft from a plain-English description via
              the AI integration, then refine it lever by lever.
            </li>
            <li>
              <span className="text-gray-200">Backtest.</span> Run it against recorded historical
              bars (Backtests) to see hypothetical performance, trade-by-trade detail, and the
              full metric set — see "Metric reference" below.
            </li>
            <li>
              <span className="text-gray-200">Paper trade.</span> Start a paper run (Running) to
              trade it against real, live streamed quotes with simulated fills — same runtime
              code path as live, just a different broker object underneath. This is required
              before a strategy can go live.
            </li>
            <li>
              <span className="text-gray-200">Promote to live.</span> Once a paper run clears the
              promotion gate's minimum evidence bar (see "Promotion gate" below), it can be
              promoted to place real orders — always paused-first, always a manual confirmation.
            </li>
          </ol>

          <h3 className="mb-2 text-sm font-semibold text-gray-200">How trades actually execute</h3>
          <p className="mb-2 text-sm leading-relaxed text-gray-400">
            Every order — paper or live — funnels through one safety chokepoint (the
            OrderGateway, see "Safety rails" below) before it can be placed. In backtest, fills
            are simulated against historical bars with a pessimistic same-bar assumption. In
            paper, fills are simulated against the real live quote stream with simulated latency
            and slippage. In live, orders go to Schwab's real API and are real fills at real
            prices. Stops and targets referencing the underlying's price are monitored inside
            this app (Schwab has no order type for "sell this call if SPY drops below X") — see
            "Fill simulation" and "Options" below for exactly what that means for risk.
          </p>
          <p className="mb-4 text-sm leading-relaxed text-gray-400">
            Nothing places a real order without: the strategy being promoted to live, the kill
            switch being off, and every check in the safety-rails chain passing. Live order
            placement is off by default for the whole app until deliberately enabled.
          </p>

          <h3 className="mb-2 text-sm font-semibold text-gray-200">What data is available</h3>
          <ul className="mb-4 list-disc space-y-1.5 pl-5 text-sm text-gray-400">
            <li>
              <span className="text-gray-200">Candles.</span> Daily and intraday (1-min and
              aggregated) equity bars, backfilled from Schwab's REST history and then grown
              forward by a continuously-running recorder off the live streamer — the app's own
              local store is the source of truth for anything older than Schwab's REST window.
            </li>
            <li>
              <span className="text-gray-200">Option chains.</span> Recorded snapshots (strikes,
              greeks, IV) at a configurable cadence, used to calibrate the options pricing model
              — Schwab sells no historical option prices, so backtests before a chain was
              recorded price contracts synthetically (see "Options" below).
            </li>
            <li>
              <span className="text-gray-200">Account & order state.</span> Live account balances,
              positions, and order/activity history pulled from Schwab for real accounts, plus
              this app's own journal of every paper/live order and gateway decision.
            </li>
          </ul>

          <h3 className="mb-2 text-sm font-semibold text-gray-200">Page-by-page guide</h3>
          <div className="mb-2 grid grid-cols-1 gap-3 md:grid-cols-2">
            {PAGE_GUIDE.map((p) => (
              <div key={p.path} className="rounded border border-white/10 p-3">
                <div className="mb-1 flex items-baseline gap-2">
                  <span className="text-sm font-semibold text-gray-200">{p.name}</span>
                  <span className="font-mono text-xs text-gray-600">{p.path}</span>
                </div>
                <p className="text-sm text-gray-400">{p.doc}</p>
              </div>
            ))}
          </div>
        </section>

        {/* ---- Section 1: primitives ---- */}
        <section>
          <SectionHeading id="primitives">Primitive reference</SectionHeading>

          <h3 className="mb-2 text-sm font-semibold text-gray-200">Indicators</h3>
          <p className="mb-3 text-xs text-gray-500">
            Auto-generated from <code className="rounded bg-white/5 px-1">GET /api/catalog</code> —
            this list can never drift from what's actually implemented.
          </p>
          {isLoading && <p className="text-sm text-gray-500">Loading catalog…</p>}
          {isError && <p className="text-sm text-red-400">Could not load /api/catalog.</p>}
          {data && (
            <div className="mb-6 grid grid-cols-1 gap-3 md:grid-cols-2">
              {data.indicators.map((ind) => (
                <div key={ind.type} className="rounded border border-white/10 p-3">
                  <div className="mb-1 flex items-center gap-2">
                    <span className="font-mono text-sm text-teal-400">{ind.type}</span>
                    {ind.intraday_only && (
                      <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-[10px] text-gray-500">
                        intraday only
                      </span>
                    )}
                    {!ind.uses_source && (
                      <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-[10px] text-gray-500">
                        no source
                      </span>
                    )}
                  </div>
                  <p className="mb-2 text-sm text-gray-300">{ind.doc}</p>
                  <p className="mb-2 text-xs text-gray-500">
                    Outputs: {ind.outputs.join(', ')}
                  </p>
                  {ind.params.length > 0 && (
                    <table className="w-full text-left text-xs">
                      <thead className="text-gray-500">
                        <tr>
                          <th className="pr-2 font-medium">Param</th>
                          <th className="pr-2 font-medium">Default</th>
                          <th className="pr-2 font-medium">Range</th>
                        </tr>
                      </thead>
                      <tbody>
                        {ind.params.map((p) => (
                          <tr key={p.name} className="border-t border-white/5">
                            <td className="pr-2 py-1 font-mono text-gray-400">{p.name}</td>
                            <td className="pr-2 py-1 text-gray-300">{p.default}</td>
                            <td className="pr-2 py-1 text-gray-300">
                              {p.min}–{p.max}
                              {p.step !== 1 ? ` (step ${p.step})` : ''}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              ))}
            </div>
          )}

          <h3 className="mb-2 text-sm font-semibold text-gray-200">Expression operators</h3>
          <p className="mb-3 text-xs text-gray-500">Combinators (from the strategy schema):</p>
          <div className="mb-4">
            <MiniTable head={['Op', 'Meaning']} rows={EXPR_OPS.map((o) => [o.op, o.doc])} />
          </div>

          <h3 className="mb-2 text-sm font-semibold text-gray-200">Comparison operators</h3>
          <div className="mb-4">
            <MiniTable head={['Op', 'Meaning']} rows={CMP_OPS.map((o) => [o.op, o.doc])} />
          </div>

          <h3 className="mb-2 text-sm font-semibold text-gray-200">Operand kinds</h3>
          <div className="mb-4">
            <MiniTable head={['Kind', 'Meaning']} rows={OPERAND_KINDS.map((o) => [o.kind, o.doc])} />
          </div>

          <h3 className="mb-2 text-sm font-semibold text-gray-200">Order types</h3>
          <div className="mb-4">
            <MiniTable head={['Type', 'Meaning']} rows={ORDER_TYPES.map((o) => [o.type, o.doc])} />
          </div>

          <h3 className="mb-2 text-sm font-semibold text-gray-200">Stop / target types</h3>
          <p className="mb-3 text-xs text-gray-500">
            Used identically for both <code className="rounded bg-white/5 px-1">stop_loss</code>,{' '}
            <code className="rounded bg-white/5 px-1">take_profit</code>, and{' '}
            <code className="rounded bg-white/5 px-1">trailing_stop</code>.
          </p>
          <div className="mb-4">
            <MiniTable head={['Type', 'Meaning']} rows={STOP_TYPES.map((o) => [o.type, o.doc])} />
          </div>

          <h3 className="mb-2 text-sm font-semibold text-gray-200">Sizing types</h3>
          <div className="mb-2">
            <MiniTable head={['Type', 'Meaning']} rows={SIZING_TYPES.map((o) => [o.type, o.doc])} />
          </div>
          <p className="text-sm text-gray-300">Plus a per-trade{' '}
            <code className="rounded bg-white/5 px-1">max_position_notional</code> cap and{' '}
            <code className="rounded bg-white/5 px-1">round_lot</code> share rounding.</p>
          <p className="mt-2 text-sm text-amber-300/90">
            <strong>max_position_notional is absolute dollars, not a percent of equity.</strong>{' '}
            It encodes an assumed account size — 25,000 means "25% of a position" only on a
            100k account. As equity compounds the cap becomes a smaller share, so a strategy
            silently de-levers as it grows. Re-set it when funding a strategy with a different
            amount.
          </p>
        </section>

        {/* ---- Section 2: metrics ---- */}
        <section>
          <SectionHeading id="options">Options</SectionHeading>
          <div className="mb-8 space-y-4 text-sm leading-relaxed text-gray-400">
            <p>
              Options are an <strong className="text-gray-300">expression of a signal</strong>,
              not a second kind of strategy. Your indicators and entry/exit rules keep
              evaluating on the underlying's bars exactly as they do for shares; the{' '}
              <code className="text-gray-300">expression</code> block only decides what a
              fired signal buys. That is why any existing strategy can be converted by
              adding one section, with no rule rewriting.
            </p>
            <p>
              A <strong className="text-gray-300">long</strong> signal buys calls and a{' '}
              <strong className="text-gray-300">short</strong> signal buys puts. Both are
              long premium, so a "short" options strategy needs no margin account and no
              stock borrow, and cannot lose more than the debit paid. Selling options to
              open (naked shorts) is not supported and is refused at four separate layers —
              that cap on losses is what the rest of the safety design rests on.
            </p>

            <h3 className="pt-2 text-sm font-semibold text-amber-200">
              Backtests use modelled prices
            </h3>
            <p>
              Schwab sells <strong className="text-gray-300">no historical option data</strong>.
              Its price-history endpoint is equities-only, so a backtest over any period
              before this app started recording chains prices contracts with Black-Scholes
              over a modelled volatility surface. Those results are model output, not
              observed outcomes, and every options backtest says so at the top of the page.
            </p>
            <p>
              The volatility level comes from a real observed source wherever possible —
              VIX for SPY-like products, VXN for QQQ, RVX for IWM — because those{' '}
              <em>are</em> quoted 30-day implied volatility and reach back to 1990. Only
              when no such anchor exists does the model fall back to the underlying's
              realized volatility, multiplied by a variance risk premium. That multiplier
              is not optional: implied volatility has exceeded subsequent realized
              volatility in roughly 85% of months since 1990, so pricing options off
              realized vol alone makes every one of them too cheap and manufactures profit
              that never existed.
            </p>
            <p>
              Once chains have been recorded (Data → Options), the model is replayed
              against them and the measured error is reported per underlying at{' '}
              <code className="text-gray-300">/api/options/calibration</code>, bucketed by
              days-to-expiry and delta. That turns "approximate" into a number you can
              apply as a haircut.
            </p>

            <h3 className="pt-2 text-sm font-semibold text-amber-200">
              Costs are the first thing to check
            </h3>
            <p>
              A one-lot round trip costs two commissions ($0.65 each) plus the bid/ask
              spread twice. On a liquid $3.00 SPY call that is roughly 1–2% of the premium;
              on an illiquid name it is over 10%. Any strategy whose edge per trade is
              thinner than that is a cost generator, not an edge — the same arithmetic that
              killed the fast-scalping families in the equity research campaigns, an order
              of magnitude deeper. Backtests cross the full spread by default for exactly
              this reason.
            </p>

            <h3 className="pt-2 text-sm font-semibold text-amber-200">
              Stops, and what actually rests at the broker
            </h3>
            <p>
              With <code className="text-gray-300">stop_basis: underlying</code> (the
              default, and what every converted strategy uses), your stop and target are
              measured against the UNDERLYING's price exactly as they were for shares. But
              no broker offers an order that says "sell this call if SPY drops below 580",
              so that stop is monitored <strong className="text-gray-300">in this app</strong>{' '}
              rather than resting at Schwab — it depends on the app being alive and its data
              fresh.
            </p>
            <p>
              Two things make that acceptable. First, a long option's maximum loss is the
              premium paid, known exactly at entry and capped by your per-trade limit — a
              crash cannot lose more than that. Second,{' '}
              <code className="text-gray-300">max_loss_pct_of_premium</code> places a REAL
              stop order at the broker on the contract's own price, which fires whether or
              not this app is running. Disabling it removes the only broker-side protection
              an options position has, and both lint and the promotion gate say so.
            </p>

            <h3 className="pt-2 text-sm font-semibold text-amber-200">Expiry</h3>
            <p>
              Positions are never held through expiration. Every position is force-closed
              at <code className="text-gray-300">close_at_dte</code> (default 1 day),
              because in the final days gamma explodes, spreads widen and assignment
              mechanics take over — none of which this system models. A position that
              somehow reaches expiration is settled at intrinsic value and counted; a
              non-zero count is a bug signal, not a normal outcome.
            </p>

            <h3 className="pt-2 text-sm font-semibold text-amber-200">
              What is deliberately not built
            </h3>
            <p>
              Naked/uncovered short options, credit spreads, iron condors, straddles,
              strangles, calendars, ratios, covered calls against existing shares, futures
              options, cash-settled index options (SPX/NDX), exercise or assignment
              initiated by this app, and automatic rolling. Debit verticals are planned but
              not yet enabled.
            </p>
          </div>

          <SectionHeading id="market-data">Market data — what you can see, and how to get more</SectionHeading>
          <div className="mb-10 space-y-5 text-sm leading-relaxed text-gray-400">
            <p>
              Backtests and charts only use data this app has already saved. If a backtest
              looks empty or short, it's almost always a data problem, not a strategy
              problem — and it's fixable in a couple of clicks.
            </p>

            <div className="rounded border border-white/10 p-4">
              <p className="mb-2 text-sm font-semibold text-gray-200">
                Daily bars — years of history, instantly
              </p>
              <p className="mb-3">
                Add any symbol on the Data page and you'll get its full price history back
                to the 1980s–90s right away. Great for daily/swing strategies — nothing to
                wait for.
              </p>
              <CollapsiblePanel title="Details" defaultOpen={false}>
                <p className="text-sm text-gray-400">
                  Schwab's price-history endpoint returns a symbol's complete listed daily
                  history in a single request — SPY back to its 1993 listing, most large
                  caps back to Schwab's own 1985 data floor. There's effectively no limit
                  on daily history: it comes back in full the moment a symbol is added, and
                  there's nothing to record over time for this timeframe.
                </p>
              </CollapsiblePanel>
            </div>

            <div className="rounded border border-white/10 p-4">
              <p className="mb-2 text-sm font-semibold text-gray-200">
                Intraday bars (1-minute, 5-minute, etc.) — only what's been recorded
              </p>
              <p className="mb-3">
                For fast strategies, you can only get about the last month or two of
                history right away. Beyond that, the app has to have been running and
                watching that symbol to have saved the bars itself — there's no way to buy
                or fetch older intraday history from anywhere. The earlier you add a
                symbol, the more history you'll have to test against later.
              </p>
              <CollapsiblePanel title="Details" defaultOpen={false}>
                <div className="space-y-2 text-sm text-gray-400">
                  <p>
                    Schwab enforces a rolling date wall on intraday history that gets
                    tighter the finer the timeframe: roughly 46 days back for 1-minute
                    bars, 203 days for 5-minute bars, and 260 days for 10/15/30-minute
                    bars. A request for a window entirely before that wall simply comes
                    back empty — it isn't an error, there's just nothing there.
                  </p>
                  <p>
                    Past that wall, the only source of intraday history is this app's own
                    recorder, which appends every bar as it streams in from Schwab while
                    the app is running. It only accumulates while running — a day the app
                    is closed during market hours is a day of intraday history that can't
                    be recovered afterward, since Schwab's own wall moves forward too.
                    Coarser intraday timeframes (5m/15m/30m) are built locally from
                    recorded 1-minute bars rather than fetched separately.
                  </p>
                </div>
              </CollapsiblePanel>
            </div>

            <div className="rounded border border-white/10 p-4">
              <p className="mb-2 text-sm font-semibold text-gray-200">
                Options — priced with a model until real data builds up
              </p>
              <p className="mb-3">
                There's no source for historical options prices, so options backtests use
                an estimate based on the stock's price and volatility. Once you've recorded
                real option chains for a while, you can check how accurate that estimate
                has been on the Chains page.
              </p>
              <CollapsiblePanel title="Details" defaultOpen={false}>
                <p className="text-sm text-gray-400">
                  Schwab's price-history endpoint is equities-only — there's no vendor of
                  record for historical option prices anywhere. Any options backtest
                  predating this app's own chain recordings prices contracts with a
                  volatility model instead of observed prices (see the "Options" section
                  above for how). Recorded chain snapshots (Data page) are what let the
                  model's accuracy actually be measured, on the Chains page's calibration
                  view.
                </p>
              </CollapsiblePanel>
            </div>

            <div className="rounded border border-teal-500/20 bg-teal-500/5 p-4">
              <p className="mb-2 text-sm font-semibold text-gray-200">How to get more data</p>
              <ul className="list-disc space-y-1.5 pl-5">
                <li>
                  Go to the <span className="text-gray-200">Data</span> page and add any
                  symbols you want to trade — do this as soon as you think of them.
                </li>
                <li>
                  Leave the app running during market hours so it can keep recording — the
                  longer it's been running, the more intraday history you'll have.
                </li>
                <li>
                  Check the Data page any time to see how much history each symbol already
                  has.
                </li>
              </ul>
            </div>
          </div>

          <SectionHeading id="metrics">Metric reference</SectionHeading>
          <p className="mb-4 text-sm text-gray-400">
            Every metric shown on a backtest result page, in plain English.
          </p>
          <div className="space-y-3">
            {METRIC_DOCS.map((m) => (
              <div key={m.key} className="rounded border border-white/10 p-3">
                <p className="mb-1 text-sm font-semibold text-gray-200">{m.label}</p>
                <p className="text-sm text-gray-400">{m.text}</p>
              </div>
            ))}
          </div>
        </section>

        {/* ---- Section 3a: fills ---- */}
        <section>
          <SectionHeading id="fills">Fill simulation: backtest vs paper</SectionHeading>
          <div className="space-y-4">
            <div className="rounded border border-white/10 p-4">
              <p className="mb-2 text-sm font-semibold text-gray-200">Backtest engine</p>
              <p className="text-sm text-gray-400">
                Works off historical OHLCV bars, not tick-level order books. Signals evaluated at
                bar close act on the <em>next</em> bar: market orders fill at next bar's open ±
                slippage; stop orders that gap through their trigger fill at that bar's open
                (never at a better price); if a stop and a target would both trigger on the same
                bar, the stop is assumed to fill first (the pessimistic assumption). No margin
                modeling — sizing is cash-behavior only.
              </p>
            </div>
            <div className="rounded border border-white/10 p-4">
              <p className="mb-2 text-sm font-semibold text-gray-200">Paper engine (SimBroker)</p>
              <p className="text-sm text-gray-400">
                Runs on the SAME runtime code path as live trading — only the injected broker
                differs — driven by real streaming Schwab quotes (no Schwab-supported paper API
                exists). No order-book depth: fills always execute in full at the quoted price ±
                slippage, never partial (conservative vs reality, never optimistic). Market
                orders self-schedule their fill after a configurable simulated latency and
                re-query the quote at fill time, not at placement time. Limit/stop orders are
                passive — they sit working and only fire on a strict price cross once a live tick
                is processed, never on mere proximity. OCO linkage (canceling the sibling on a
                fill) is handled one layer up in the runtime, not inside SimBroker itself.
              </p>
            </div>
            <p className="text-xs text-gray-500">
              In short: backtest fills are bar-level and can look ahead only to "next bar,"
              while paper fills are tick-driven and latency-aware but still fully simulated —
              neither ever talks to Schwab's real order book.
            </p>
          </div>
        </section>

        {/* ---- Section 3b: promotion gate ---- */}
        <section>
          <SectionHeading id="promotion">Promotion gate (backtest → paper → live)</SectionHeading>
          <div className="space-y-3 text-sm text-gray-400">
            <p>
              <span className="font-semibold text-gray-200">The rules:</span> a strategy version
              needs at least one completed backtest before paper can start; to go from paper to
              live it should additionally have a paper run meeting minimums (default: ≥ 2 weeks
              elapsed and ≥ 10 round-trip trades, both editable, with a journaled override for
              exceptions). Any lever edit changes the strategy's content hash, which resets the
              gate — new version, new evidence required.
            </p>
            <p>
              <span className="font-semibold text-gray-200">What's actually enforced today:</span>{' '}
              only the backtest-required check for starting a <em>paper</em> run — a strategy
              version with no completed backtest can't start paper trading. The full
              promotion-gate UI wizard (side-by-side backtest-vs-paper metric review, live
              risk-limit confirmation, type-to-confirm, paused-first-activation) is{' '}
              <span className="text-amber-400">not built yet</span>.
            </p>
          </div>
        </section>

        {/* ---- Section 3c: safety rails ---- */}
        <section>
          <SectionHeading id="safety">Safety rails (OrderGateway)</SectionHeading>
          <p className="mb-3 text-sm text-gray-400">
            Every order intent funnels through one chokepoint that runs 8 checks, in order,
            before anything reaches the broker:
          </p>
          <ol className="list-decimal space-y-2 pl-5 text-sm text-gray-400">
            <li><span className="text-gray-200">Kill switch</span> — if active, blocks all new order placements (cancels still work separately).</li>
            <li><span className="text-gray-200">Auth gate</span> — live orders require a valid Schwab access token.</li>
            <li><span className="text-gray-200">Mode gate</span> — the order's broker mode must match the run's mode (defense in depth; paper strategies use a different broker object entirely).</li>
            <li><span className="text-gray-200">Per-strategy limits</span> — max open positions, max daily orders, no duplicate working entry, and a daily-loss breach blocks new entries (exits are never blocked by it).</li>
            <li><span className="text-gray-200">Global limits</span> — symbol allowlist/blocklist, global max notional, global daily-loss breach, market-hours guard for entries.</li>
            <li><span className="text-gray-200">Duplicate suppression</span> — rejects an identical intent resubmitted within a short cooldown window.</li>
            <li><span className="text-gray-200">Price sanity</span> — market orders need a fresh quote; discretionary limit/stop prices must sit within a band of the reference price (protective stops are exempted, since being far from market is the point).</li>
            <li><span className="text-gray-200">Rate limiter</span> — a token-bucket order-rate cap with priority classes so protective exits are never starved by entries.</li>
          </ol>
          <p className="mt-3 text-xs text-gray-500">
            Every decision is journaled before any broker call, and failures degrade toward
            "flat and stopped," never "unknown position."
          </p>
        </section>

        {/* ---- Section 3d: auth lifecycle ---- */}
        <section>
          <SectionHeading id="auth">Auth lifecycle</SectionHeading>
          <p className="mb-3 text-sm text-gray-400">
            The app itself has no login — it's a single-user, localhost-only tool. The only
            "auth" that exists is the Schwab OAuth token lifecycle needed to call their APIs:
          </p>
          <ul className="list-disc space-y-2 pl-5 text-sm text-gray-400">
            <li>Connecting Schwab opens a browser to Schwab's consent page; the app catches the redirect on a local HTTPS listener and exchanges the code for tokens.</li>
            <li>A background task refreshes the access token roughly every 25 minutes. Each refresh rotates the refresh token (Schwab invalidates the old one), so the token file is written atomically to never lose the only valid refresh token.</li>
            <li>The refresh token itself has a 7-day lifetime — the app tracks its age with a countdown and shows a persistent banner starting 24 hours before it would lapse.</li>
            <li>If the refresh token lapses without re-auth, strategies enter a defined degraded state: no new orders can be placed (the auth-gate check in the safety rails above), though already-resting protective stops at the broker are unaffected.</li>
          </ul>
        </section>

        {/* ---- Section 3e: why didn't it trade ---- */}
        <section>
          <SectionHeading id="no-trade">Why didn't my strategy trade?</SectionHeading>
          <p className="mb-3 text-sm text-gray-400">Checklist, roughly in order of likelihood:</p>
          <ul className="list-disc space-y-2 pl-5 text-sm text-gray-400">
            <li>
              <span className="text-gray-200">Warm-up period.</span> Indicators need enough
              history to stop being NaN before any rule can fire — a strategy using a 200-period
              SMA needs ~200 bars of history before its first bar of possible signals. Short
              backtest windows without extra lookback history can silently produce zero trades.
            </li>
            <li>
              <span className="text-gray-200">Entry limits already hit.</span>{' '}
              <code className="rounded bg-white/5 px-1">max_entries_per_day</code> and{' '}
              <code className="rounded bg-white/5 px-1">cooldown_bars</code> can both suppress a
              signal that would otherwise fire, if the strategy already entered recently.
            </li>
            <li>
              <span className="text-gray-200">There IS a trade — it just hasn't closed.</span>{' '}
              If the backtest window ends while a position is still open, it shows up under
              "Open positions at end of window" (marked-to-market into equity/Sharpe/drawdown)
              rather than in the Trades table, which only holds closed round trips. A result with
              0 trades and non-zero P&L usually means this.
            </li>
            <li>
              <span className="text-gray-200">Live-runtime gaps:</span> the live/paper runtime
              enforces almost every strategy lever the backtest engine does, but if a specific
              lever isn't honoured live the way it was backtested, that's surfaced right where
              you're already looking — as a lint problem in the editor, a badge on its row in
              Running, an event in the run log, and a blocker on the promotion gate — rather than
              a list here that could quietly go stale. The one known gap: exits are always sent as
              market orders in both backtest and live, so a marketable-limit exit doesn't actually
              control slippage.
            </li>
          </ul>
        </section>
      </div>
    </div>
  )
}
