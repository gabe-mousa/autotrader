import ExprBuilder from '../ExprBuilder'
import type { CatalogResponse, ScaleOut, StopSpec, StrategyDocument } from '../../lib/strategy'
import { CheckboxField, NumberField, SelectField } from './fields'
import StopSpecEditor from './StopSpecEditor'

const STOP_TYPES: StopSpec['type'][] = ['percent', 'atr_multiple', 'fixed_price_offset']

/** The trailing stop is a StopSpec plus an optional activation threshold. The
 *  shared `StopSpec` type in lib/strategy.ts doesn't carry `activate_at`, so
 *  this panel narrows it locally rather than widening the type everywhere. */
type TrailingStop = StopSpec & { activate_at?: StopSpec | null }

export default function ExitPanel({
  doc,
  setDoc,
  catalog,
}: {
  doc: StrategyDocument
  setDoc: (fn: (d: StrategyDocument) => StrategyDocument) => void
  catalog: CatalogResponse | undefined
}) {
  const specByType = new Map((catalog?.indicators ?? []).map((s) => [s.type, s]))
  const exit = doc.exit
  const signal = exit.signal ?? null
  const timeStop = exit.time_stop ?? null
  const scaleOuts = exit.scale_outs ?? []
  const atrIndicators = doc.indicators.filter((i) => i.type.toLowerCase().includes('atr'))
  const trailing = (exit.trailing_stop ?? null) as TrailingStop | null

  const setTrailing = (next: TrailingStop | null) =>
    setDoc((d) => ({ ...d, exit: { ...d.exit, trailing_stop: next } }))

  const updateScaleOut = (idx: number, next: ScaleOut) =>
    setDoc((d) => ({
      ...d,
      exit: {
        ...d.exit,
        scale_outs: (d.exit.scale_outs ?? []).map((o, i) => (i === idx ? next : o)),
      },
    }))

  return (
    <div className="flex flex-col gap-3">
      <div>
        <CheckboxField
          label="Enable exit signal condition"
          checked={!!signal}
          onChange={(v) =>
            setDoc((d) => ({
              ...d,
              exit: {
                ...d.exit,
                signal: v
                  ? { when: { op: 'gt', left: { price: 'close' }, right: { value: 0 } } }
                  : null,
              },
            }))
          }
        />
        {signal && (
          <div className="mt-2">
            <ExprBuilder
              expr={signal.when}
              onChange={(when) =>
                setDoc((d) => ({ ...d, exit: { ...d.exit, signal: { when } } }))
              }
              indicators={doc.indicators}
              indicatorSpecs={specByType}
              depth={0}
            />
          </div>
        )}
      </div>

      {/* Signal exits are the one part of the Exit section that is NOT
          direction-relative, so a long/short document needs a second rule. */}
      <div>
        <CheckboxField
          label="Separate exit signal for shorts (signal_short)"
          checked={!!exit.signal_short}
          onChange={(v) =>
            setDoc((d) => ({
              ...d,
              exit: {
                ...d.exit,
                signal_short: v
                  ? { when: { op: 'lt', left: { price: 'close' }, right: { value: 0 } } }
                  : null,
              },
            }))
          }
        />
        {exit.signal_short ? (
          <div className="mt-2">
            <ExprBuilder
              expr={exit.signal_short.when}
              onChange={(when) =>
                setDoc((d) => ({ ...d, exit: { ...d.exit, signal_short: { when } } }))
              }
              indicators={doc.indicators}
              indicatorSpecs={specByType}
              depth={0}
            />
          </div>
        ) : (
          <p className="mt-1 text-xs text-gray-500">
            Short positions fall back to the exit signal above. Every other exit here (stop,
            target, trail) is already direction-relative and mirrors automatically.
          </p>
        )}
      </div>

      <StopSpecEditor
        label="Stop loss"
        value={exit.stop_loss}
        onChange={(v) => setDoc((d) => ({ ...d, exit: { ...d.exit, stop_loss: v } }))}
        indicators={doc.indicators}
      />
      <StopSpecEditor
        label="Take profit"
        value={exit.take_profit}
        onChange={(v) => setDoc((d) => ({ ...d, exit: { ...d.exit, take_profit: v } }))}
        indicators={doc.indicators}
      />
      <div className="flex flex-col gap-2">
        <StopSpecEditor
          label="Trailing stop"
          value={trailing}
          onChange={(v) =>
            // preserve the activation threshold across edits to the trail
            // itself (StopSpecEditor only knows about the base StopSpec)
            setTrailing(v === null ? null : { ...v, activate_at: trailing?.activate_at ?? null })
          }
          indicators={doc.indicators}
        />
        {trailing && (
          <div className="ml-4 border-l border-white/10 pl-3">
            <StopSpecEditor
              label="Only start trailing after a gain of…"
              value={trailing.activate_at}
              onChange={(v) => setTrailing({ ...trailing, activate_at: v })}
              indicators={doc.indicators}
            />
            <div className="mt-1 text-xs text-white/50">
              Optional activation threshold, measured in the favourable direction from entry
              (up for a long, down for a short). Until price reaches it the trailing stop is{' '}
              <span className="text-white/70">dormant and provides no protection at all</span> —
              only the stop loss protects the position, so a stop loss is required when this is
              set. Once reached, the trail arms and then only ever tightens.
            </div>
          </div>
        )}
      </div>

      <div className="rounded border border-white/10 bg-white/[0.02] p-3">
        <div className="flex items-center justify-between gap-2">
          <div>
            <div className="text-sm font-medium">Scale-outs (partial exits)</div>
            <div className="text-xs text-white/50">
              Sell part of the position at each target, in increasing distance from entry.
              Percentages must total under 100% — the remainder exits via take profit,
              trailing stop, or signal.
            </div>
          </div>
          {scaleOuts.length < 3 && (
            <button
              type="button"
              className="shrink-0 rounded border border-white/15 px-2 py-1 text-xs hover:bg-white/10"
              onClick={() =>
                setDoc((d) => ({
                  ...d,
                  exit: {
                    ...d.exit,
                    scale_outs: [
                      ...(d.exit.scale_outs ?? []),
                      { target: { type: 'percent', value: 1 }, percent: 50 },
                    ],
                  },
                }))
              }
            >
              + Add level
            </button>
          )}
        </div>

        {scaleOuts.map((out, idx) => (
          <div key={idx} className="mt-2 flex flex-wrap items-end gap-2">
            <span className="pb-2 text-xs text-white/40">#{idx + 1}</span>
            <SelectField
              label="type"
              value={out.target.type}
              onChange={(t) =>
                updateScaleOut(idx, {
                  ...out,
                  target: { ...out.target, type: t as StopSpec['type'] },
                })
              }
              options={STOP_TYPES.map((t) => ({ value: t, label: t }))}
            />
            <NumberField
              label="target"
              value={out.target.value}
              step={0.1}
              onChange={(v) =>
                updateScaleOut(idx, { ...out, target: { ...out.target, value: v } })
              }
            />
            {out.target.type === 'atr_multiple' && (
              <SelectField
                label="atr_indicator"
                value={out.target.atr_indicator ?? atrIndicators[0]?.id ?? ''}
                onChange={(v) =>
                  updateScaleOut(idx, { ...out, target: { ...out.target, atr_indicator: v } })
                }
                options={atrIndicators.map((i) => ({ value: i.id, label: i.id }))}
              />
            )}
            <NumberField
              label="% of position"
              value={out.percent}
              step={5}
              min={1}
              max={99}
              onChange={(v) => updateScaleOut(idx, { ...out, percent: v })}
            />
            <button
              type="button"
              className="rounded border border-white/15 px-2 py-1 text-xs hover:bg-white/10"
              onClick={() =>
                setDoc((d) => ({
                  ...d,
                  exit: {
                    ...d.exit,
                    scale_outs: (d.exit.scale_outs ?? []).filter((_, i) => i !== idx),
                  },
                }))
              }
            >
              Remove
            </button>
          </div>
        ))}
      </div>

      <div className="rounded border border-white/10 bg-white/[0.02] p-3">
        <CheckboxField
          label="Time stop"
          checked={!!timeStop}
          onChange={(v) =>
            setDoc((d) => ({
              ...d,
              exit: { ...d.exit, time_stop: v ? { bars: 10 } : null },
            }))
          }
        />
        {timeStop && (
          <div className="mt-2">
            <NumberField
              label="bars"
              value={timeStop.bars}
              min={1}
              onChange={(v) =>
                setDoc((d) => ({ ...d, exit: { ...d.exit, time_stop: { bars: v } } }))
              }
            />
          </div>
        )}
      </div>
    </div>
  )
}
