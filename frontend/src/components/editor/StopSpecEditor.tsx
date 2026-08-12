import type { IndicatorInstance, StopSpec } from '../../lib/strategy'
import { CheckboxField, NumberField, SelectField } from './fields'

const TYPES: StopSpec['type'][] = ['percent', 'atr_multiple', 'fixed_price_offset']

export default function StopSpecEditor({
  label,
  value,
  onChange,
  indicators,
}: {
  label: string
  value: StopSpec | null | undefined
  onChange: (v: StopSpec | null) => void
  indicators: IndicatorInstance[]
}) {
  const enabled = !!value
  const atrIndicators = indicators.filter((i) => i.type.toLowerCase().includes('atr'))

  return (
    <div className="rounded border border-white/10 bg-white/[0.02] p-3">
      <CheckboxField
        label={label}
        checked={enabled}
        onChange={(v) => onChange(v ? { type: 'percent', value: 1 } : null)}
      />
      {value && (
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <SelectField
            label="type"
            value={value.type}
            onChange={(t) => onChange({ ...value, type: t as StopSpec['type'] })}
            options={TYPES.map((t) => ({ value: t, label: t }))}
          />
          <NumberField
            label="value"
            value={value.value}
            step={0.1}
            onChange={(v) => onChange({ ...value, value: v })}
          />
          {value.type === 'atr_multiple' && (
            <SelectField
              label="atr_indicator"
              value={value.atr_indicator ?? atrIndicators[0]?.id ?? ''}
              onChange={(v) => onChange({ ...value, atr_indicator: v })}
              options={atrIndicators.map((i) => ({ value: i.id, label: i.id }))}
            />
          )}
        </div>
      )}
    </div>
  )
}
