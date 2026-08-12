import type { IndicatorInstance, IndicatorSpec, Operand, PriceField } from '../../lib/strategy'
import {
  isOperandInd,
  isOperandOffset,
  isOperandPrice,
  isOperandValue,
  isScalableOperand,
} from '../../lib/strategy'
import { NumberField, SelectField } from './fields'

const PRICE_FIELDS: PriceField[] = ['open', 'high', 'low', 'close', 'volume']

type Kind = 'ind' | 'price' | 'value' | 'ind_offset'

function kindOf(o: Operand): Kind {
  if (isOperandInd(o)) return 'ind'
  if (isOperandPrice(o)) return 'price'
  if (isOperandOffset(o)) return 'ind_offset'
  return 'value'
}

function defaultForKind(kind: Kind, indicators: IndicatorInstance[]): Operand {
  const firstInd = indicators[0]?.id ?? ''
  switch (kind) {
    case 'ind':
      return { ind: firstInd }
    case 'price':
      return { price: 'close' }
    case 'ind_offset':
      return { ind_offset: { ind: firstInd, bars_ago: 1 } }
    case 'value':
    default:
      return { value: 0 }
  }
}

function outputsFor(indId: string, indicators: IndicatorInstance[], specs: Map<string, IndicatorSpec>): string[] {
  const inst = indicators.find((i) => i.id === indId)
  if (!inst) return []
  const spec = specs.get(inst.type)
  return spec?.outputs ?? []
}

export default function OperandEditor({
  value,
  onChange,
  indicators,
  indicatorSpecs,
  label,
}: {
  value: Operand
  onChange: (o: Operand) => void
  indicators: IndicatorInstance[]
  indicatorSpecs: Map<string, IndicatorSpec>
  label: string
}) {
  const kind = kindOf(value)

  return (
    <div className="flex flex-wrap items-end gap-2">
      <SelectField
        label={label}
        value={kind}
        onChange={(v) => onChange(defaultForKind(v as Kind, indicators))}
        options={[
          { value: 'ind', label: 'Indicator' },
          { value: 'price', label: 'Price' },
          { value: 'value', label: 'Number' },
          { value: 'ind_offset', label: 'Indicator N bars ago' },
        ]}
      />

      {kind === 'ind' && isOperandInd(value) && (
        <>
          <SelectField
            label="indicator"
            value={value.ind}
            onChange={(v) => onChange({ ...value, ind: v, field: undefined })}
            options={indicators.map((i) => ({ value: i.id, label: `${i.id} (${i.type})` }))}
          />
          {outputsFor(value.ind, indicators, indicatorSpecs).length > 1 && (
            <SelectField
              label="field"
              value={value.field ?? outputsFor(value.ind, indicators, indicatorSpecs)[0]}
              onChange={(v) => onChange({ ...value, field: v })}
              options={outputsFor(value.ind, indicators, indicatorSpecs).map((o) => ({
                value: o,
                label: o,
              }))}
            />
          )}
        </>
      )}

      {kind === 'price' && isOperandPrice(value) && (
        <SelectField
          label="field"
          value={value.price}
          onChange={(v) => onChange({ ...value, price: v as PriceField })}
          options={PRICE_FIELDS.map((f) => ({ value: f, label: f }))}
        />
      )}

      {kind === 'value' && isOperandValue(value) && (
        <NumberField label="number" value={value.value} onChange={(v) => onChange({ value: v })} />
      )}

      {kind === 'ind_offset' && isOperandOffset(value) && (
        <>
          <SelectField
            label="indicator"
            value={value.ind_offset.ind}
            onChange={(v) =>
              onChange({ ...value, ind_offset: { ...value.ind_offset, ind: v, field: undefined } })
            }
            options={indicators.map((i) => ({ value: i.id, label: `${i.id} (${i.type})` }))}
          />
          {outputsFor(value.ind_offset.ind, indicators, indicatorSpecs).length > 1 && (
            <SelectField
              label="field"
              value={
                value.ind_offset.field ?? outputsFor(value.ind_offset.ind, indicators, indicatorSpecs)[0]
              }
              onChange={(v) =>
                onChange({ ...value, ind_offset: { ...value.ind_offset, field: v } })
              }
              options={outputsFor(value.ind_offset.ind, indicators, indicatorSpecs).map((o) => ({
                value: o,
                label: o,
              }))}
            />
          )}
          <NumberField
            label="bars ago"
            value={value.ind_offset.bars_ago}
            min={1}
            onChange={(v) =>
              onChange({ ...value, ind_offset: { ...value.ind_offset, bars_ago: v } })
            }
          />
        </>
      )}

      {/* Affine scaling on series operands — this is what makes a threshold
          like "volume > 1.5x its 20-bar average" expressible. Hidden for
          literal numbers (already constants) and left out of the way unless
          actually in use. */}
      {isScalableOperand(value) && (
        <>
          <NumberField
            label="x mul"
            value={value.mul ?? 1}
            step={0.1}
            onChange={(v) => onChange({ ...value, mul: v })}
          />
          <NumberField
            label="+ add"
            value={value.add ?? 0}
            step={0.1}
            onChange={(v) => onChange({ ...value, add: v })}
          />
          {/* Indicator-valued offset. A constant `add` cannot express a moving
              distance like "vwap - 1.5*ATR"; this can. */}
          <SelectField
            label="+ offset ind"
            value={value.off_ind ?? ''}
            onChange={(v) =>
              onChange({
                ...value,
                off_ind: v || null,
                off_field: undefined,
                off_mul: v ? (value.off_mul ?? 1) : undefined,
              })
            }
            options={[
              { value: '', label: '(none)' },
              ...indicators.map((i) => ({ value: i.id, label: i.id })),
            ]}
          />
          {value.off_ind && outputsFor(value.off_ind, indicators, indicatorSpecs).length > 1 && (
            <SelectField
              label="offset field"
              value={value.off_field ?? outputsFor(value.off_ind, indicators, indicatorSpecs)[0]}
              onChange={(v) => onChange({ ...value, off_field: v })}
              options={outputsFor(value.off_ind, indicators, indicatorSpecs).map((o) => ({
                value: o,
                label: o,
              }))}
            />
          )}
          {value.off_ind && (
            <NumberField
              label="offset x"
              value={value.off_mul ?? 1}
              step={0.1}
              onChange={(v) => onChange({ ...value, off_mul: v })}
            />
          )}
        </>
      )}
    </div>
  )
}
