import type { StrategyDocument, Timeframe } from '../../lib/strategy'
import { SelectField } from './fields'

const BARS: Timeframe[] = ['1m', '5m', '15m', '30m', '1h', '1d']

export default function TimeframePanel({
  doc,
  setDoc,
}: {
  doc: StrategyDocument
  setDoc: (fn: (d: StrategyDocument) => StrategyDocument) => void
}) {
  return (
    <div className="max-w-xs">
      <SelectField
        label="Bar"
        value={doc.timeframe.bar}
        onChange={(v) =>
          setDoc((d) => ({ ...d, timeframe: { ...d.timeframe, bar: v as Timeframe } }))
        }
        options={BARS.map((b) => ({ value: b, label: b }))}
      />
    </div>
  )
}
