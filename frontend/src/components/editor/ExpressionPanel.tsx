import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { postOptionSelect } from '../../lib/api'
import type {
  Expression,
  OptionRisk,
  SideExpression,
  StrategyDocument,
} from '../../lib/strategy'
import CollapsiblePanel from './CollapsiblePanel'
import { AssetTypeBadge, RightBadge } from '../AssetTypeBadge'

/**
 * The `expression:` block — how a fired signal becomes an instrument.
 *
 * The framing repeated in the copy here is the one that matters: the RULES do
 * not change. A strategy still decides "go long SPY"; this panel only decides
 * whether that is expressed as shares or as a call. Users who understand that
 * can convert any existing strategy in a minute; users who don't will look for
 * options settings inside their entry rules and not find them.
 *
 * The live preview is the highest-value control on the page. An abstract
 * "35-delta, 30-45 DTE" means little until it resolves to a specific contract
 * at a specific price — and it is also how "why does my strategy take no
 * trades" gets answered in one click.
 */
const DEFAULT_SIDE: SideExpression = {
  structure: 'single',
  right: 'call',
  expiry: { dte_min: 25, dte_target: 35, dte_max: 55, allow: ['S', 'M', 'Q'] },
  strike: { by: 'delta', target: 0.35, tolerance: 0.1 },
}

const DEFAULT_EXPRESSION: Expression = {
  instrument: 'options',
  long: { ...DEFAULT_SIDE, right: 'call' },
  short: null,
  liquidity: {
    min_open_interest: 100,
    min_volume: 0,
    max_spread_pct: 10,
    exclude_non_standard: true,
    exclude_mini: true,
  },
  exit: { stop_basis: 'underlying', close_at_dte: 1, max_loss_pct_of_premium: 60 },
  pricing: { source: 'auto', vrp_mult: 1.15, risk_free_rate: 0.04, fill_aggression: 1.0 },
}

export default function ExpressionPanel({
  doc,
  onChange,
}: {
  doc: StrategyDocument
  onChange: (next: StrategyDocument) => void
}) {
  const expr = doc.expression ?? null
  const isOptions = expr?.instrument === 'options'

  function setExpr(next: Expression | null) {
    onChange({ ...doc, expression: next })
  }

  function patch(part: Partial<Expression>) {
    setExpr({ ...(expr ?? DEFAULT_EXPRESSION), ...part })
  }

  function patchSide(which: 'long' | 'short', part: Partial<SideExpression>) {
    const base = expr?.[which] ?? { ...DEFAULT_SIDE, right: which === 'long' ? 'call' : 'put' }
    patch({ [which]: { ...base, ...part } } as Partial<Expression>)
  }

  function patchRisk(part: Partial<OptionRisk>) {
    onChange({
      ...doc,
      risk: { ...(doc.risk ?? {}), options: { ...(doc.risk?.options ?? {}), ...part } },
    })
  }

  return (
    <CollapsiblePanel title="Instrument — shares or options" defaultOpen={isOptions}>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <ToggleButton
          active={!isOptions}
          accent="equity"
          onClick={() => setExpr(null)}
          label="Shares"
        />
        <ToggleButton
          active={isOptions}
          accent="option"
          onClick={() => setExpr(expr ? { ...expr, instrument: 'options' } : DEFAULT_EXPRESSION)}
          label="Options"
        />
        {isOptions && <AssetTypeBadge type="OPTION" size="xs" className="ml-1" />}
      </div>

      <p className="mb-4 text-xs leading-relaxed text-gray-500">
        Your entry and exit <strong className="text-gray-400">rules do not change</strong>.
        They keep evaluating on the underlying's bars exactly as they do for shares — this
        section only decides what a fired signal BUYS. A long signal buys calls, a short
        signal buys puts; both are long premium, so a "short" options strategy needs no
        margin account and no borrow, and cannot lose more than the debit paid.
      </p>

      {isOptions && expr && (
        <div className="space-y-5">
          {doc.entry.long && (
            <SideEditor
              which="long"
              side={expr.long ?? null}
              enable={() => patchSide('long', { right: 'call' })}
              disable={() => patch({ long: null })}
              onChange={(p) => patchSide('long', p)}
              indicators={doc.indicators.map((i) => i.id)}
              symbol={doc.universe.symbols[0]}
              liquidity={expr.liquidity}
            />
          )}
          {doc.entry.short && (
            <SideEditor
              which="short"
              side={expr.short ?? null}
              enable={() => patchSide('short', { right: 'put' })}
              disable={() => patch({ short: null })}
              onChange={(p) => patchSide('short', p)}
              indicators={doc.indicators.map((i) => i.id)}
              symbol={doc.universe.symbols[0]}
              liquidity={expr.liquidity}
            />
          )}
          {!doc.entry.long && !doc.entry.short && (
            <p className="rounded border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-200">
              This strategy has no entry rules yet. Add an entry rule first — the
              expression maps a signal onto a contract, so it needs a signal to map.
            </p>
          )}

          <Group title="Exit &amp; expiry">
            <Field label="Stop measured against">
              <select
                value={expr.exit?.stop_basis ?? 'underlying'}
                onChange={(e) =>
                  patch({ exit: { ...expr.exit, stop_basis: e.target.value as 'underlying' | 'premium' } })
                }
                className={inputCls}
              >
                <option value="underlying">Underlying price (same as shares)</option>
                <option value="premium">Option premium</option>
              </select>
            </Field>
            <NumField
              label="Close at DTE"
              value={expr.exit?.close_at_dte ?? 1}
              min={0}
              onChange={(v) => patch({ exit: { ...expr.exit, close_at_dte: v } })}
              hint="never hold to expiration"
            />
            <NumField
              label="Max loss (% of premium)"
              value={expr.exit?.max_loss_pct_of_premium ?? 60}
              min={1}
              max={100}
              onChange={(v) => patch({ exit: { ...expr.exit, max_loss_pct_of_premium: v } })}
              hint="rests at the broker"
            />
          </Group>

          {(expr.exit?.stop_basis ?? 'underlying') === 'underlying' && (
            <Note tone={expr.exit?.max_loss_pct_of_premium == null ? 'warn' : 'info'}>
              No broker accepts "sell this call if {doc.universe.symbols[0] ?? 'SPY'} drops
              below X", so an underlying-based stop is monitored by this app rather than
              resting at Schwab — it needs the app alive.{' '}
              {expr.exit?.max_loss_pct_of_premium == null ? (
                <strong className="text-amber-200">
                  With max-loss disabled, nothing rests at the broker at all.
                </strong>
              ) : (
                <>
                  The max-loss stop above <em>does</em> rest at the broker as a backstop, and a
                  long option cannot lose more than its premium regardless.
                </>
              )}
            </Note>
          )}

          <Group title="Liquidity filters">
            <NumField
              label="Min open interest"
              value={expr.liquidity?.min_open_interest ?? 100}
              min={0}
              onChange={(v) => patch({ liquidity: { ...expr.liquidity, min_open_interest: v } })}
            />
            <NumField
              label="Min volume"
              value={expr.liquidity?.min_volume ?? 0}
              min={0}
              onChange={(v) => patch({ liquidity: { ...expr.liquidity, min_volume: v } })}
            />
            <NumField
              label="Max spread (% of mid)"
              value={expr.liquidity?.max_spread_pct ?? 10}
              min={0.1}
              step={0.1}
              onChange={(v) => patch({ liquidity: { ...expr.liquidity, max_spread_pct: v } })}
            />
          </Group>
          <Note tone="info">
            Open interest and volume cannot be checked in a backtest — a modelled chain has
            no order book — so a live run may take <em>fewer</em> trades than its backtest.
            Fewer, never more.
          </Note>

          <Group title="Risk limits">
            <NumField
              label="Max premium at risk ($)"
              value={doc.risk?.options?.max_premium_at_risk ?? 5000}
              min={1}
              onChange={(v) => patchRisk({ max_premium_at_risk: v })}
              hint="total open debit"
            />
            <NumField
              label="Max premium per trade (% equity)"
              value={doc.risk?.options?.max_premium_per_trade_pct ?? 2}
              min={0.1}
              step={0.1}
              onChange={(v) => patchRisk({ max_premium_per_trade_pct: v })}
            />
            <NumField
              label="Max contracts per position"
              value={doc.risk?.options?.max_contracts_per_position ?? 50}
              min={1}
              onChange={(v) => patchRisk({ max_contracts_per_position: v })}
            />
          </Group>

          <Group title="Backtest pricing">
            <NumField
              label="Variance risk premium"
              value={expr.pricing?.vrp_mult ?? 1.15}
              min={0.5}
              max={2}
              step={0.05}
              onChange={(v) => patch({ pricing: { ...expr.pricing, vrp_mult: v } })}
            />
            <NumField
              label="Fill aggression"
              value={expr.pricing?.fill_aggression ?? 1}
              min={0}
              max={1.5}
              step={0.1}
              onChange={(v) => patch({ pricing: { ...expr.pricing, fill_aggression: v } })}
              hint="1.0 = cross the spread"
            />
            <NumField
              label="Risk-free rate"
              value={expr.pricing?.risk_free_rate ?? 0.04}
              min={0}
              max={0.25}
              step={0.005}
              onChange={(v) => patch({ pricing: { ...expr.pricing, risk_free_rate: v } })}
            />
          </Group>
          {(expr.pricing?.vrp_mult ?? 1.15) < 1 && (
            <Note tone="warn">
              A variance risk premium below 1.0 prices options <em>cheaper</em> than they have
              historically traded — implied volatility has exceeded subsequent realized
              volatility in roughly 85% of months since 1990. Backtests run this way will
              show profit that never existed.
            </Note>
          )}
          <Note tone="info">
            Backtest option prices are <strong className="text-gray-300">modelled</strong>,
            because Schwab sells no historical option data. Live and paper runs always use
            real quotes and never fall back to the model.
          </Note>
        </div>
      )}
    </CollapsiblePanel>
  )
}

// ---------------------------------------------------------------------------

function SideEditor({
  which,
  side,
  enable,
  disable,
  onChange,
  indicators,
  symbol,
  liquidity,
}: {
  which: 'long' | 'short'
  side: SideExpression | null
  enable: () => void
  disable: () => void
  onChange: (part: Partial<SideExpression>) => void
  indicators: string[]
  symbol?: string
  liquidity?: Expression['liquidity']
}) {
  const preview = useMutation({ mutationFn: postOptionSelect })

  if (!side) {
    return (
      <div className="rounded border border-white/10 p-3">
        <div className="flex items-center justify-between">
          <span className="text-sm text-gray-400">
            <code className="text-gray-300">entry.{which}</code> has no expression — those
            signals would be dropped silently.
          </span>
          <button onClick={enable} className="rounded bg-amber-600 px-2 py-1 text-xs text-white hover:bg-amber-500">
            Add
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="rounded border border-amber-500/20 bg-amber-500/[0.03] p-3">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm">
          <span className="text-gray-400">
            when <code className="text-gray-300">entry.{which}</code> fires, buy
          </span>
          <RightBadge right={side.right === 'call' ? 'C' : 'P'} />
        </div>
        <button onClick={disable} className="text-xs text-gray-500 hover:text-gray-300">
          remove
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Field label="Right">
          <select
            value={side.right}
            onChange={(e) => onChange({ right: e.target.value as 'call' | 'put' })}
            className={inputCls}
          >
            <option value="call">Call</option>
            <option value="put">Put</option>
          </select>
        </Field>
        <Field label="Strike by">
          <select
            value={side.strike.by}
            onChange={(e) =>
              onChange({ strike: { ...side.strike, by: e.target.value as never } })
            }
            className={inputCls}
          >
            <option value="delta">Delta</option>
            <option value="moneyness_pct">% from spot</option>
            <option value="atr_multiple">ATR multiple</option>
            <option value="offset_dollars">$ from spot</option>
            <option value="atm">At the money</option>
          </select>
        </Field>
        {side.strike.by !== 'atm' && (
          <NumField
            label="Target"
            value={side.strike.target}
            step={side.strike.by === 'delta' ? 0.05 : 0.5}
            min={0.001}
            onChange={(v) => onChange({ strike: { ...side.strike, target: v } })}
          />
        )}
        {side.strike.by === 'atr_multiple' && (
          <Field label="ATR indicator">
            <select
              value={side.strike.atr_indicator ?? ''}
              onChange={(e) =>
                onChange({ strike: { ...side.strike, atr_indicator: e.target.value || null } })
              }
              className={inputCls}
            >
              <option value="">— pick —</option>
              {indicators.map((id) => (
                <option key={id} value={id}>{id}</option>
              ))}
            </select>
          </Field>
        )}
        <NumField
          label="DTE min"
          value={side.expiry.dte_min}
          min={0}
          onChange={(v) => onChange({ expiry: { ...side.expiry, dte_min: v } })}
        />
        <NumField
          label="DTE target"
          value={side.expiry.dte_target}
          min={0}
          onChange={(v) => onChange({ expiry: { ...side.expiry, dte_target: v } })}
        />
        <NumField
          label="DTE max"
          value={side.expiry.dte_max}
          min={0}
          onChange={(v) => onChange({ expiry: { ...side.expiry, dte_max: v } })}
        />
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-xs text-gray-500">Expirations:</span>
        {(['S', 'M', 'Q', 'W'] as const).map((t) => {
          const on = side.expiry.allow.includes(t)
          return (
            <button
              key={t}
              onClick={() =>
                onChange({
                  expiry: {
                    ...side.expiry,
                    allow: on
                      ? side.expiry.allow.filter((x) => x !== t)
                      : [...side.expiry.allow, t],
                  },
                })
              }
              className={`rounded border px-2 py-0.5 text-xs ${
                on
                  ? 'border-amber-500/50 bg-amber-500/15 text-amber-200'
                  : 'border-white/10 text-gray-500 hover:text-gray-300'
              }`}
              title={{ S: '3rd Friday', M: 'end of month', Q: 'quarterly', W: 'weekly' }[t]}
            >
              {{ S: 'Standard', M: 'Monthly', Q: 'Quarterly', W: 'Weekly' }[t]}
            </button>
          )
        })}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          disabled={!symbol || preview.isPending}
          onClick={() =>
            symbol &&
            preview.mutate({ symbol, expression: side, liquidity })
          }
          className="rounded border border-amber-500/40 px-2.5 py-1 text-xs font-medium text-amber-200 hover:bg-amber-500/10 disabled:opacity-50"
        >
          {preview.isPending ? 'Checking…' : `What would this buy in ${symbol ?? '—'}?`}
        </button>
        {preview.isError && (
          <span className="text-xs text-red-400">
            {preview.error instanceof Error ? preview.error.message : 'preview failed'}
          </span>
        )}
      </div>

      {preview.data && (
        <div className="mt-2 rounded border border-white/10 bg-black/20 p-2.5 text-xs">
          {preview.data.selected ? (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <RightBadge right={preview.data.selected.right} />
                <span className="font-mono text-gray-100">{preview.data.selected.describe}</span>
                <span className="text-gray-500">{preview.data.selected.dte} DTE</span>
              </div>
              <div className="mt-1.5 grid grid-cols-2 gap-x-4 gap-y-0.5 text-gray-400 sm:grid-cols-4">
                <span>
                  Ask <span className="font-mono text-gray-200">
                    {preview.data.selected.ask?.toFixed(2) ?? '—'}
                  </span>
                </span>
                <span>
                  Δ <span className="font-mono text-gray-200">
                    {preview.data.selected.delta?.toFixed(3) ?? '—'}
                  </span>
                </span>
                <span>
                  IV <span className="font-mono text-gray-200">
                    {preview.data.selected.iv != null
                      ? `${(preview.data.selected.iv * 100).toFixed(1)}%`
                      : '—'}
                  </span>
                </span>
                <span>
                  Spread <span className="font-mono text-gray-200">
                    {preview.data.selected.spread_pct?.toFixed(1) ?? '—'}%
                  </span>
                </span>
              </div>
              <div className="mt-1.5 text-gray-500">
                One contract costs{' '}
                <span className="font-mono text-amber-200">
                  ${preview.data.selected.cost_per_contract.toLocaleString()}
                </span>{' '}
                — that is also the most it can lose.
              </div>
            </>
          ) : (
            <div className="text-amber-300">
              Nothing selected: <span className="font-mono">{preview.data.rejection?.reason}</span>
              {preview.data.rejection?.detail && (
                <span className="text-gray-500"> — {preview.data.rejection.detail}</span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ---- small shared controls -------------------------------------------------

const inputCls =
  'w-full rounded border border-white/10 bg-white/5 px-2 py-1 text-sm text-gray-200 focus:border-amber-500 focus:outline-none'

function ToggleButton({
  active,
  accent,
  onClick,
  label,
}: {
  active: boolean
  accent: 'equity' | 'option'
  onClick: () => void
  label: string
}) {
  const on =
    accent === 'option'
      ? 'border-amber-500 bg-amber-500/15 text-amber-200'
      : 'border-emerald-500 bg-emerald-500/15 text-emerald-200'
  return (
    <button
      onClick={onClick}
      className={`rounded border px-3 py-1.5 text-sm font-medium transition-colors ${
        active ? on : 'border-white/10 text-gray-400 hover:bg-white/5'
      }`}
    >
      {label}
    </button>
  )
}

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
        {title}
      </h3>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">{children}</div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-0.5 block text-xs text-gray-500">{label}</span>
      {children}
    </label>
  )
}

function NumField({
  label,
  value,
  onChange,
  min,
  max,
  step = 1,
  hint,
}: {
  label: string
  value: number
  onChange: (v: number) => void
  min?: number
  max?: number
  step?: number
  hint?: string
}) {
  const [text, setText] = useState(String(value))
  const [seen, setSeen] = useState(value)
  if (seen !== value) {
    setSeen(value)
    setText(String(value))
  }
  return (
    <label className="block">
      <span className="mb-0.5 block text-xs text-gray-500">
        {label}
        {hint && <span className="ml-1 text-gray-600">({hint})</span>}
      </span>
      <input
        type="number"
        value={text}
        min={min}
        max={max}
        step={step}
        onChange={(e) => {
          setText(e.target.value)
          const n = Number(e.target.value)
          if (e.target.value !== '' && Number.isFinite(n)) onChange(n)
        }}
        className={inputCls}
      />
    </label>
  )
}

function Note({ tone, children }: { tone: 'info' | 'warn'; children: React.ReactNode }) {
  return (
    <p
      className={`rounded border p-2 text-xs leading-relaxed ${
        tone === 'warn'
          ? 'border-amber-500/40 bg-amber-500/10 text-amber-200'
          : 'border-white/10 bg-white/[0.02] text-gray-500'
      }`}
    >
      {children}
    </p>
  )
}
