import OperandEditor from './OperandEditor'
import type { CatalogResponse, StrategyDocument } from '../../lib/strategy'
import { CheckboxField, NumberField, SelectField } from './fields'

/**
 * Cross-sectional selection: on each bar, rank the universe by `metric` and
 * only allow entries in the top `top_n`.
 *
 * Without it a multi-symbol strategy takes whichever signal fires first in
 * timestamp order, which is an arbitrary tiebreak, not a choice.
 */
export default function SelectionPanel({
  doc,
  setDoc,
  catalog,
}: {
  doc: StrategyDocument
  setDoc: (fn: (d: StrategyDocument) => StrategyDocument) => void
  catalog: CatalogResponse | undefined
}) {
  const specByType = new Map((catalog?.indicators ?? []).map((s) => [s.type, s]))
  const sel = doc.selection ?? null
  const symbolCount = doc.universe.symbols.length

  const setSel = (next: StrategyDocument['selection']) =>
    setDoc((d) => ({ ...d, selection: next }))

  return (
    <div className="flex flex-col gap-3">
      <CheckboxField
        label="Enable cross-sectional selection"
        checked={!!sel}
        onChange={(v) =>
          setSel(
            v
              ? {
                  metric: { ind: doc.indicators[0]?.id ?? '' },
                  top_n: Math.min(3, Math.max(1, symbolCount)),
                  order: 'desc',
                }
              : null,
          )
        }
      />

      {!sel && (
        <p className="text-xs text-gray-500">
          No ranking. Every symbol whose entry condition fires is eligible, and which one
          actually gets filled depends on bar timestamp order and the open-position limit.
        </p>
      )}

      {sel && (
        <>
          <OperandEditor
            label="Rank by"
            value={sel.metric}
            onChange={(metric) => setSel({ ...sel, metric })}
            indicators={doc.indicators}
            indicatorSpecs={specByType}
          />
          <div className="flex flex-wrap items-end gap-2">
            <NumberField
              label="top_n"
              value={sel.top_n ?? 3}
              step={1}
              onChange={(v) => setSel({ ...sel, top_n: v })}
            />
            <SelectField
              label="order"
              value={sel.order ?? 'desc'}
              onChange={(v) => setSel({ ...sel, order: v as 'desc' | 'asc' })}
              options={[
                { value: 'desc', label: 'desc — highest metric first (strength)' },
                { value: 'asc', label: 'asc — lowest metric first (weakness)' },
              ]}
            />
          </div>
          <p className="text-xs text-gray-500">
            Ties break on symbol name, so a run is deterministic. Selection ranks the whole
            universe ({symbolCount} symbol{symbolCount === 1 ? '' : 's'}); it does not itself
            cap how many positions you hold — that is risk.max_open_positions.
          </p>
          {(sel.top_n ?? 3) >= symbolCount && symbolCount > 0 && (
            <p className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-xs text-amber-200">
              top_n ({sel.top_n ?? 3}) covers the entire {symbolCount}-symbol universe, so the
              ranking never excludes anything and has no effect.
            </p>
          )}
        </>
      )}
    </div>
  )
}
