import { useState } from 'react'
import type { StrategyDocument } from '../../lib/strategy'
import { SmallButton, inputCls } from './fields'

const MAX_SYMBOLS = 100

export default function UniversePanel({
  doc,
  setDoc,
}: {
  doc: StrategyDocument
  setDoc: (fn: (d: StrategyDocument) => StrategyDocument) => void
}) {
  const [next, setNext] = useState('')
  const symbols = doc.universe.symbols

  // Accepts a whole list at once — with a 100-symbol ceiling, a scanner
  // watchlist is normally pasted in from somewhere else, and adding it one
  // ticker at a time would be unusable. Splits on commas/whitespace, drops
  // duplicates (against both the existing list and the pasted batch), and
  // fills up to the cap rather than rejecting the whole paste.
  function add() {
    const incoming = next
      .toUpperCase()
      .split(/[\s,;]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    if (incoming.length === 0) {
      setNext('')
      return
    }
    setDoc((d) => {
      const merged = [...d.universe.symbols]
      for (const sym of incoming) {
        if (merged.length >= MAX_SYMBOLS) break
        if (!merged.includes(sym)) merged.push(sym)
      }
      return { ...d, universe: { symbols: merged } }
    })
    setNext('')
  }

  function remove(sym: string) {
    setDoc((d) => ({ ...d, universe: { symbols: d.universe.symbols.filter((s) => s !== sym) } }))
  }

  return (
    <div>
      <div className="mb-3 flex flex-wrap gap-2">
        {symbols.length === 0 && <span className="text-sm text-gray-600">No symbols yet.</span>}
        {symbols.map((s) => (
          <span
            key={s}
            className="flex items-center gap-1.5 rounded-full border border-white/10 bg-white/5 py-1 pl-3 pr-1.5 text-xs text-gray-300"
          >
            {s}
            <button
              onClick={() => remove(s)}
              aria-label={`Remove ${s}`}
              className="rounded-full px-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200"
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <input
          value={next}
          onChange={(e) => setNext(e.target.value.toUpperCase())}
          onKeyDown={(e) => {
            if (e.key === 'Enter') add()
          }}
          placeholder="Add symbols (paste a list)"
          className={inputCls('w-64')}
        />
        <SmallButton onClick={add} disabled={symbols.length >= MAX_SYMBOLS}>
          Add
        </SmallButton>
        <span className="text-xs text-gray-500">
          {symbols.length >= MAX_SYMBOLS
            ? `Max ${MAX_SYMBOLS} symbols.`
            : `${symbols.length} / ${MAX_SYMBOLS}`}
        </span>
      </div>
    </div>
  )
}
