import type { StrategyDocument } from '../../lib/strategy'
import { CheckboxField, NumberField } from './fields'

export default function RiskPanel({
  doc,
  setDoc,
}: {
  doc: StrategyDocument
  setDoc: (fn: (d: StrategyDocument) => StrategyDocument) => void
}) {
  const risk = doc.risk ?? {}
  const short = risk.short ?? {}
  const hasShortRule = !!doc.entry.short
  const setShort = (patch: Record<string, unknown>) =>
    setDoc((d) => ({ ...d, risk: { ...d.risk, short: { ...d.risk?.short, ...patch } } }))

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        <NumberField
          label="max_open_positions"
          value={risk.max_open_positions ?? 0}
          min={0}
          onChange={(v) => setDoc((d) => ({ ...d, risk: { ...d.risk, max_open_positions: v } }))}
        />
        <NumberField
          label="max_daily_loss"
          value={risk.max_daily_loss ?? 0}
          onChange={(v) => setDoc((d) => ({ ...d, risk: { ...d.risk, max_daily_loss: v } }))}
        />
        <NumberField
          label="max_daily_orders"
          value={risk.max_daily_orders ?? 0}
          min={0}
          onChange={(v) => setDoc((d) => ({ ...d, risk: { ...d.risk, max_daily_orders: v } }))}
        />
      </div>

      <div className="border-t border-neutral-800 pt-3">
        <div className="mb-2 text-xs font-medium uppercase tracking-wide text-neutral-400">
          Short selling
        </div>

        <CheckboxField
          label="risk.short.enabled — allow short entries"
          checked={!!short.enabled}
          onChange={(v) => setShort({ enabled: v })}
        />

        {hasShortRule && !short.enabled && (
          <p className="mt-2 text-xs text-amber-400">
            This strategy has a short rule but shorting is off — it will never take a short
            trade until you enable it here.
          </p>
        )}

        {short.enabled && (
          <>
            <div className="mt-3 flex flex-wrap gap-2">
              <NumberField
                label="initial_margin_pct"
                value={short.initial_margin_pct ?? 50}
                min={0}
                onChange={(v) => setShort({ initial_margin_pct: v })}
              />
              <NumberField
                label="maintenance_margin_pct"
                value={short.maintenance_margin_pct ?? 30}
                min={0}
                onChange={(v) => setShort({ maintenance_margin_pct: v })}
              />
              <NumberField
                label="max_short_notional"
                value={short.max_short_notional ?? 25000}
                min={0}
                onChange={(v) => setShort({ max_short_notional: v })}
              />
            </div>

            <label className="mt-3 block text-xs text-neutral-400">
              hard_to_borrow (comma-separated symbols — never shorted)
              <input
                className="mt-1 w-full rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm text-neutral-200"
                value={(short.hard_to_borrow ?? []).join(', ')}
                placeholder="e.g. LCID, NIO"
                onChange={(e) =>
                  setShort({
                    hard_to_borrow: e.target.value
                      .split(',')
                      .map((s) => s.trim().toUpperCase())
                      .filter(Boolean),
                  })
                }
              />
            </label>

            <p className="mt-3 text-xs text-neutral-500">
              A short&apos;s loss is unbounded, so <code>exit.stop_loss</code> is required
              (not just recommended). Live shorting also requires a margin account — a cash
              account is rejected before any order is sent.
            </p>
          </>
        )}
      </div>
    </div>
  )
}
