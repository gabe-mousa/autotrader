import type { StrategyDocument } from '../../lib/strategy'
import { NumberField, SelectField } from './fields'

type OrderType = 'market' | 'limit' | 'marketable_limit'
const ORDER_TYPES: OrderType[] = ['market', 'limit', 'marketable_limit']

export default function OrdersPanel({
  doc,
  setDoc,
}: {
  doc: StrategyDocument
  setDoc: (fn: (d: StrategyDocument) => StrategyDocument) => void
}) {
  const orders = doc.orders ?? {}
  const entryOrder = orders.entry ?? { type: 'market' as OrderType }
  const exitOrder = orders.exit ?? { type: 'market' as OrderType }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end gap-2">
        <SelectField
          label="entry order type"
          value={entryOrder.type}
          onChange={(v) =>
            setDoc((d) => ({
              ...d,
              orders: { ...d.orders, entry: { ...entryOrder, type: v as OrderType } },
            }))
          }
          options={ORDER_TYPES.map((t) => ({ value: t, label: t }))}
        />
        {entryOrder.type === 'marketable_limit' && (
          <NumberField
            label="entry offset_bps"
            value={entryOrder.offset_bps ?? 0}
            onChange={(v) =>
              setDoc((d) => ({
                ...d,
                orders: { ...d.orders, entry: { ...entryOrder, offset_bps: v } },
              }))
            }
          />
        )}
      </div>

      <div className="flex flex-wrap items-end gap-2">
        <SelectField
          label="exit order type"
          value={exitOrder.type}
          onChange={(v) =>
            setDoc((d) => ({
              ...d,
              orders: { ...d.orders, exit: { ...exitOrder, type: v as OrderType } },
            }))
          }
          options={ORDER_TYPES.map((t) => ({ value: t, label: t }))}
        />
        {exitOrder.type === 'marketable_limit' && (
          <NumberField
            label="exit offset_bps"
            value={exitOrder.offset_bps ?? 0}
            onChange={(v) =>
              setDoc((d) => ({
                ...d,
                orders: { ...d.orders, exit: { ...exitOrder, offset_bps: v } },
              }))
            }
          />
        )}
      </div>

      <NumberField
        label="unfilled_timeout_sec"
        value={orders.unfilled_timeout_sec ?? 0}
        min={0}
        onChange={(v) => setDoc((d) => ({ ...d, orders: { ...d.orders, unfilled_timeout_sec: v } }))}
      />
    </div>
  )
}
