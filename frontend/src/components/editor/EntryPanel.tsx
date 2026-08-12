import ExprBuilder from '../ExprBuilder'
import type { CatalogResponse, StrategyDocument } from '../../lib/strategy'
import { CheckboxField, NumberField } from './fields'
import StopSpecEditor from './StopSpecEditor'

export default function EntryPanel({
  doc,
  setDoc,
  catalog,
}: {
  doc: StrategyDocument
  setDoc: (fn: (d: StrategyDocument) => StrategyDocument) => void
  catalog: CatalogResponse | undefined
}) {
  const specByType = new Map((catalog?.indicators ?? []).map((s) => [s.type, s]))
  const long = doc.entry.long
  const short = doc.entry.short
  const shortEnabled = !!doc.risk?.short?.enabled
  const hasStop = !!doc.exit?.stop_loss

  return (
    <div className="flex flex-col gap-3">
      <CheckboxField
        label="Enable long entry condition"
        checked={!!long}
        onChange={(v) =>
          setDoc((d) => ({
            ...d,
            entry: {
              ...d.entry,
              long: v
                ? { when: { op: 'gt', left: { price: 'close' }, right: { value: 0 } } }
                : null,
            },
          }))
        }
      />

      {long && (
        <ExprBuilder
          expr={long.when}
          onChange={(when) =>
            setDoc((d) => ({ ...d, entry: { ...d.entry, long: { when } } }))
          }
          indicators={doc.indicators}
          indicatorSpecs={specByType}
          depth={0}
        />
      )}

      <div className="border-t border-neutral-800 pt-3">
        <CheckboxField
          label="Enable short entry condition"
          checked={!!short}
          onChange={(v) =>
            setDoc((d) => ({
              ...d,
              entry: {
                ...d.entry,
                short: v
                  ? { when: { op: 'lt', left: { price: 'close' }, right: { value: 0 } } }
                  : null,
              },
              // turning on a short rule also turns on the explicit risk opt-in;
              // leaving them out of sync is the most common way a short
              // strategy silently never trades
              risk: v ? { ...d.risk, short: { ...d.risk?.short, enabled: true } } : d.risk,
            }))
          }
        />

        {short && (
          <>
            <ExprBuilder
              expr={short.when}
              onChange={(when) =>
                setDoc((d) => ({ ...d, entry: { ...d.entry, short: { when } } }))
              }
              indicators={doc.indicators}
              indicatorSpecs={specByType}
              depth={0}
            />
            {!shortEnabled && (
              <p className="mt-2 text-xs text-amber-400">
                risk.short.enabled is off — this short rule will never trade until you
                switch it on in the Risk panel.
              </p>
            )}
            {!hasStop && (
              <p className="mt-2 text-xs text-red-400">
                A short has no stop set. A short&apos;s loss is unbounded — set
                exit.stop_loss before running this strategy.
              </p>
            )}
            <p className="mt-2 text-xs text-neutral-500">
              Exit distances are direction-relative: stop_loss sits <em>above</em> entry
              for a short and take_profit <em>below</em> it.
            </p>
          </>
        )}
      </div>

      {/* Scale-IN. The mirror image of exit.scale_outs, and previously the only
          entry-side lever with no UI at all. */}
      <div>
        <CheckboxField
          label="Enable pyramiding (scale into a working position)"
          checked={!!doc.entry.pyramid}
          onChange={(v) =>
            setDoc((d) => ({
              ...d,
              entry: {
                ...d.entry,
                pyramid: v
                  ? { max_adds: 1, spacing: { type: 'percent', value: 2 }, size_pct: 100 }
                  : null,
              },
            }))
          }
        />
        {doc.entry.pyramid && (
          <div className="mt-2 flex flex-col gap-2">
            <div className="flex flex-wrap items-end gap-2">
              <NumberField
                label="max_adds"
                value={doc.entry.pyramid.max_adds}
                min={1}
                onChange={(v) =>
                  setDoc((d) => ({
                    ...d,
                    entry: { ...d.entry, pyramid: { ...d.entry.pyramid!, max_adds: v } },
                  }))
                }
              />
              <NumberField
                label="size_pct (of original entry)"
                value={doc.entry.pyramid.size_pct ?? 100}
                step={5}
                onChange={(v) =>
                  setDoc((d) => ({
                    ...d,
                    entry: { ...d.entry, pyramid: { ...d.entry.pyramid!, size_pct: v } },
                  }))
                }
              />
            </div>
            <StopSpecEditor
              label="spacing (favourable move from the LAST fill before each add)"
              value={doc.entry.pyramid.spacing}
              onChange={(v) =>
                setDoc((d) => ({
                  ...d,
                  entry: {
                    ...d.entry,
                    pyramid: {
                      ...d.entry.pyramid!,
                      spacing: v ?? { type: 'percent', value: 2 },
                    },
                  },
                }))
              }
              indicators={doc.indicators}
            />
            <p className="text-xs text-gray-500">
              Adds merge into one position at a volume-weighted average price, and the stop
              and target are recomputed from that new average — so the stop travels up with
              the position rather than leaving the original, now-distant stop protecting a
              much larger size.
            </p>
          </div>
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        <NumberField
          label="cooldown_bars"
          value={doc.entry.cooldown_bars ?? 0}
          min={0}
          onChange={(v) => setDoc((d) => ({ ...d, entry: { ...d.entry, cooldown_bars: v } }))}
        />
        <NumberField
          label="max_entries_per_day"
          value={doc.entry.max_entries_per_day ?? 1}
          min={0}
          onChange={(v) =>
            setDoc((d) => ({ ...d, entry: { ...d.entry, max_entries_per_day: v } }))
          }
        />
      </div>
    </div>
  )
}
