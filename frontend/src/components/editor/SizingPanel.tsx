import type { StrategyDocument } from '../../lib/strategy'
import { NumberField, SelectField } from './fields'

const TYPES: StrategyDocument['sizing']['type'][] = [
  'fixed_dollars',
  'fixed_shares',
  'percent_of_equity',
  'risk_based',
]

export default function SizingPanel({
  doc,
  setDoc,
}: {
  doc: StrategyDocument
  setDoc: (fn: (d: StrategyDocument) => StrategyDocument) => void
}) {
  const sizing = doc.sizing
  return (
    <div className="flex flex-wrap gap-2">
      <SelectField
        label="type"
        value={sizing.type}
        onChange={(v) =>
          setDoc((d) => ({
            ...d,
            sizing: { ...d.sizing, type: v as StrategyDocument['sizing']['type'] },
          }))
        }
        options={TYPES.map((t) => ({ value: t, label: t }))}
      />
      <NumberField
        label="value"
        value={sizing.value}
        step={0.1}
        onChange={(v) => setDoc((d) => ({ ...d, sizing: { ...d.sizing, value: v } }))}
      />
      <NumberField
        label="max_position_notional"
        value={sizing.max_position_notional ?? 0}
        onChange={(v) =>
          setDoc((d) => ({ ...d, sizing: { ...d.sizing, max_position_notional: v } }))
        }
      />
      <NumberField
        label="round_lot"
        value={sizing.round_lot ?? 1}
        min={1}
        onChange={(v) => setDoc((d) => ({ ...d, sizing: { ...d.sizing, round_lot: v } }))}
      />
    </div>
  )
}
