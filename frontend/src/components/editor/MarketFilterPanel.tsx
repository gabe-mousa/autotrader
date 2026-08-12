import ExprBuilder from '../ExprBuilder'
import type { CatalogResponse, IndicatorInstance, StrategyDocument } from '../../lib/strategy'
import { CheckboxField, NumberField, SelectField, SmallButton, TextField } from './fields'

const SOURCE_OPTIONS = ['open', 'high', 'low', 'close', 'hl2', 'hlc3', 'ohlc4', 'volume']
const APPLIES_TO: Array<'both' | 'long' | 'short'> = ['both', 'long', 'short']

/**
 * The regime gate — evaluated on a REFERENCE symbol (usually SPY/QQQ) and ANDed
 * into every entry.
 *
 * This panel exists because the gate was previously invisible in the UI: a
 * strategy could have `market_filter.exit_when` as its ONLY exit (the Exit
 * panel would correctly show nothing) and nothing on screen explained when it
 * closed a position. Its indicators are declared here separately from the
 * document's because they are computed on the reference symbol's own bars, not
 * the traded symbol's — so the main Indicators panel's ids are NOT in scope
 * inside these expressions, and vice versa.
 */
export default function MarketFilterPanel({
  doc,
  setDoc,
  catalog,
}: {
  doc: StrategyDocument
  setDoc: (fn: (d: StrategyDocument) => StrategyDocument) => void
  catalog: CatalogResponse | undefined
}) {
  const specs = catalog?.indicators ?? []
  const specByType = new Map(specs.map((s) => [s.type, s]))
  const mf = doc.market_filter ?? null

  const setMf = (next: StrategyDocument['market_filter']) =>
    setDoc((d) => ({ ...d, market_filter: next }))

  const patch = (p: Partial<NonNullable<StrategyDocument['market_filter']>>) => {
    if (!mf) return
    setMf({ ...mf, ...p })
  }

  function enable(on: boolean) {
    if (!on) return setMf(null)
    const firstType = specs[0]?.type ?? 'sma'
    const spec = specByType.get(firstType)
    const params: Record<string, number> = {}
    for (const p of spec?.params ?? []) params[p.name] = p.default
    setMf({
      symbol: 'SPY',
      indicators: [{ id: 'mf1', type: firstType, params, source: 'close' }],
      when: { op: 'gt', left: { price: 'close' }, right: { ind: 'mf1' } },
      applies_to: 'both',
      exit_when: null,
    })
  }

  function updateInds(next: IndicatorInstance[]) {
    patch({ indicators: next })
  }

  function addInd() {
    if (!mf) return
    const firstType = specs[0]?.type ?? 'sma'
    const spec = specByType.get(firstType)
    const params: Record<string, number> = {}
    for (const p of spec?.params ?? []) params[p.name] = p.default
    updateInds([
      ...mf.indicators,
      { id: `mf${mf.indicators.length + 1}`, type: firstType, params, source: 'close' },
    ])
  }

  function changeIndType(i: number, type: string) {
    if (!mf) return
    const spec = specByType.get(type)
    const params: Record<string, number> = {}
    for (const p of spec?.params ?? []) params[p.name] = p.default
    const next = mf.indicators.slice()
    next[i] = { ...next[i], type, params }
    updateInds(next)
  }

  function updateIndAt(i: number, p: Partial<IndicatorInstance>) {
    if (!mf) return
    const next = mf.indicators.slice()
    next[i] = { ...next[i], ...p }
    updateInds(next)
  }

  return (
    <div className="flex flex-col gap-3">
      <CheckboxField
        label="Enable market regime filter"
        checked={!!mf}
        onChange={enable}
      />

      {!mf && (
        <p className="text-xs text-gray-500">
          No regime gate. Entry rules see only the symbol being evaluated — they have no
          awareness of the broader tape.
        </p>
      )}

      {mf && (
        <>
          <div className="flex flex-wrap items-end gap-2">
            <TextField
              label="Reference symbol"
              value={mf.symbol}
              onChange={(v) => patch({ symbol: v.toUpperCase() })}
            />
            <SelectField
              label="Applies to"
              value={mf.applies_to ?? 'both'}
              onChange={(v) => patch({ applies_to: v as 'both' | 'long' | 'short' })}
              options={APPLIES_TO.map((a) => ({ value: a, label: a }))}
            />
          </div>

          <div>
            <div className="mb-1 flex items-center gap-2">
              <span className="text-xs uppercase tracking-wide text-gray-400">
                Reference-symbol indicators
              </span>
              <SmallButton onClick={addInd}>+ add</SmallButton>
            </div>
            <p className="mb-2 text-xs text-gray-500">
              Computed on {mf.symbol || 'the reference symbol'}&apos;s bars, not the traded
              symbol&apos;s. Only these ids are addressable in the two conditions below.
            </p>
            {mf.indicators.length === 0 && (
              <p className="text-xs text-amber-300/80">
                No indicators — the conditions below can only reference raw price.
              </p>
            )}
            {mf.indicators.map((ind, i) => {
              const spec = specByType.get(ind.type)
              return (
                <div key={i} className="mb-2 flex flex-wrap items-end gap-2">
                  <TextField
                    label="id"
                    value={ind.id}
                    onChange={(v) => updateIndAt(i, { id: v })}
                  />
                  <SelectField
                    label="type"
                    value={ind.type}
                    onChange={(v) => changeIndType(i, v)}
                    options={specs.map((s) => ({ value: s.type, label: s.type }))}
                  />
                  {(spec?.params ?? []).map((p) => (
                    <NumberField
                      key={p.name}
                      label={p.name}
                      value={ind.params[p.name] ?? p.default}
                      step={p.step}
                      onChange={(v) =>
                        updateIndAt(i, { params: { ...ind.params, [p.name]: v } })
                      }
                    />
                  ))}
                  {spec?.uses_source !== false && (
                    <SelectField
                      label="source"
                      value={ind.source ?? 'close'}
                      onChange={(v) => updateIndAt(i, { source: v })}
                      options={SOURCE_OPTIONS.map((s) => ({ value: s, label: s }))}
                    />
                  )}
                  <SmallButton onClick={() => updateInds(mf.indicators.filter((_, x) => x !== i))}>
                    remove
                  </SmallButton>
                </div>
              )
            })}
          </div>

          <div>
            <div className="mb-1 text-xs uppercase tracking-wide text-gray-400">
              Gate ON (entries allowed when)
            </div>
            <ExprBuilder
              expr={mf.when}
              onChange={(when) => patch({ when })}
              indicators={mf.indicators}
              indicatorSpecs={specByType}
              depth={0}
            />
          </div>

          <div>
            <div className="mb-1 flex items-center gap-2">
              <span className="text-xs uppercase tracking-wide text-gray-400">
                Gate EXIT (close open positions when)
              </span>
            </div>
            <CheckboxField
              label="Close open positions when the gate turns off"
              checked={!!mf.exit_when}
              onChange={(v) =>
                patch({
                  exit_when: v
                    ? { op: 'lt', left: { price: 'close' }, right: { value: 0 } }
                    : null,
                })
              }
            />
            {mf.exit_when ? (
              <>
                <p className="mb-2 mt-1 text-xs text-gray-500">
                  Fires on {mf.symbol || 'the reference symbol'}, not the traded symbol. The
                  position closes at the next bar&apos;s open with exit reason{' '}
                  <code className="text-amber-200/70">market_filter</code>. Keeping this
                  separate from &quot;not gate-on&quot; is what allows hysteresis — enter above
                  one threshold, leave below a lower one.
                </p>
                <ExprBuilder
                  expr={mf.exit_when}
                  onChange={(exit_when) => patch({ exit_when })}
                  indicators={mf.indicators}
                  indicatorSpecs={specByType}
                  depth={0}
                />
              </>
            ) : (
              <p className="mt-1 text-xs text-gray-500">
                Entry-only gate: once open, a position is unaffected by the regime turning
                against it and must be closed by the Exit rules.
              </p>
            )}
            {mf.exit_when && isExitless(doc) && (
              <p className="mt-2 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-xs text-amber-200">
                This gate is the strategy&apos;s <strong>only</strong> exit — there is no stop
                loss, take profit, trailing stop, time stop or exit signal. Between the gate
                firing and the next open, nothing protects the position.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  )
}

/** True when the Exit section would close a position by no other means. */
function isExitless(doc: StrategyDocument): boolean {
  const e = doc.exit ?? {}
  return (
    !e.signal &&
    !e.signal_short &&
    !e.stop_loss &&
    !e.take_profit &&
    !e.trailing_stop &&
    !e.time_stop &&
    (e.scale_outs ?? []).length === 0
  )
}
