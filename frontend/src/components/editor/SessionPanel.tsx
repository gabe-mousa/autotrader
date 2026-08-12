import type { StrategyDocument } from '../../lib/strategy'
import { CheckboxField, TextField } from './fields'

export default function SessionPanel({
  doc,
  setDoc,
}: {
  doc: StrategyDocument
  setDoc: (fn: (d: StrategyDocument) => StrategyDocument) => void
}) {
  const session = doc.session ?? {}
  const windowEnabled = !!session.entry_window
  const win = session.entry_window ?? { start: '09:30', end: '16:00' }

  return (
    <div className="flex flex-col gap-3">
      <CheckboxField
        label="Flatten positions at end of day"
        checked={!!session.flatten_eod}
        onChange={(v) => setDoc((d) => ({ ...d, session: { ...d.session, flatten_eod: v } }))}
      />

      <CheckboxField
        label="Restrict entries to a time window"
        checked={windowEnabled}
        onChange={(v) =>
          setDoc((d) => ({
            ...d,
            session: { ...d.session, entry_window: v ? win : null },
          }))
        }
      />

      {windowEnabled && (
        <div className="ml-5 flex gap-2">
          <TextField
            label="Start (HH:MM)"
            value={win.start}
            onChange={(v) =>
              setDoc((d) => ({
                ...d,
                session: { ...d.session, entry_window: { ...win, start: v } },
              }))
            }
          />
          <TextField
            label="End (HH:MM)"
            value={win.end}
            onChange={(v) =>
              setDoc((d) => ({
                ...d,
                session: { ...d.session, entry_window: { ...win, end: v } },
              }))
            }
          />
        </div>
      )}
    </div>
  )
}
