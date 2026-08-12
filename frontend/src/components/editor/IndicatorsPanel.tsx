import type { CatalogResponse, IndicatorInstance, StrategyDocument } from '../../lib/strategy'
import { NumberField, SelectField, SmallButton, TextField } from './fields'

const SOURCE_OPTIONS = ['open', 'high', 'low', 'close', 'hl2', 'hlc3', 'ohlc4', 'volume']

export default function IndicatorsPanel({
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

  function updateIndicators(next: IndicatorInstance[]) {
    setDoc((d) => ({ ...d, indicators: next }))
  }

  function updateAt(i: number, patch: Partial<IndicatorInstance>) {
    const next = doc.indicators.slice()
    next[i] = { ...next[i], ...patch }
    updateIndicators(next)
  }

  function removeAt(i: number) {
    updateIndicators(doc.indicators.filter((_, idx) => idx !== i))
  }

  function add() {
    const firstType = specs[0]?.type ?? 'rsi'
    const spec = specByType.get(firstType)
    const params: Record<string, number> = {}
    for (const p of spec?.params ?? []) params[p.name] = p.default
    updateIndicators([
      ...doc.indicators,
      { id: `ind${doc.indicators.length + 1}`, type: firstType, params },
    ])
  }

  function changeType(i: number, type: string) {
    const spec = specByType.get(type)
    const params: Record<string, number> = {}
    for (const p of spec?.params ?? []) params[p.name] = p.default
    updateAt(i, { type, params, source: spec?.uses_source ? 'close' : undefined })
  }

  return (
    <div className="flex flex-col gap-3">
      {doc.indicators.length === 0 && (
        <p className="text-sm text-gray-600">No indicators yet.</p>
      )}
      {doc.indicators.map((ind, i) => {
        const spec = specByType.get(ind.type)
        return (
          <div key={i} className="rounded border border-white/10 bg-white/[0.02] p-3">
            <div className="mb-2 flex flex-wrap items-end gap-2">
              <TextField label="id" value={ind.id} onChange={(v) => updateAt(i, { id: v })} />
              <SelectField
                label="type"
                value={ind.type}
                onChange={(v) => changeType(i, v)}
                options={specs.map((s) => ({ value: s.type, label: s.type }))}
              />
              {spec?.uses_source && (
                <SelectField
                  label="source"
                  value={ind.source ?? 'close'}
                  onChange={(v) => updateAt(i, { source: v })}
                  options={SOURCE_OPTIONS.map((s) => ({ value: s, label: s }))}
                />
              )}
              <SmallButton onClick={() => removeAt(i)} variant="danger">
                Remove
              </SmallButton>
            </div>
            {spec && spec.params.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {spec.params.map((p) => (
                  <NumberField
                    key={p.name}
                    label={p.name}
                    value={ind.params[p.name] ?? p.default}
                    min={p.min}
                    max={p.max}
                    step={p.step}
                    onChange={(v) =>
                      updateAt(i, { params: { ...ind.params, [p.name]: v } })
                    }
                  />
                ))}
              </div>
            )}
            {spec?.doc && <p className="mt-2 text-xs text-gray-600">{spec.doc}</p>}
          </div>
        )
      })}
      <div>
        <SmallButton onClick={add}>+ Add indicator</SmallButton>
      </div>
    </div>
  )
}
