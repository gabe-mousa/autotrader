import type { StrategyDocument } from '../../lib/strategy'

// Compact read-only preview of the key shape of a strategy document —
// used to preview an AI-generated or AI-refined draft before it's applied.
export default function DocumentPreview({ doc }: { doc: StrategyDocument }) {
  return (
    <div className="flex flex-col gap-3 rounded border border-white/10 p-4">
      <div>
        <h3 className="text-base font-semibold text-gray-100">{doc.meta.name || 'Untitled strategy'}</h3>
        {doc.meta.description && (
          <p className="mt-1 text-sm text-gray-400">{doc.meta.description}</p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 text-xs text-gray-400 sm:grid-cols-4">
        <div>
          <p className="text-gray-500">Symbols</p>
          <p className="text-gray-200">{doc.universe.symbols.join(', ') || '—'}</p>
        </div>
        <div>
          <p className="text-gray-500">Bar</p>
          <p className="text-gray-200">{doc.timeframe.bar}</p>
        </div>
        <div>
          <p className="text-gray-500">Sizing</p>
          <p className="text-gray-200">
            {doc.sizing.type} ({doc.sizing.value})
          </p>
        </div>
        <div>
          <p className="text-gray-500">Indicators</p>
          <p className="text-gray-200">{doc.indicators.length}</p>
        </div>
      </div>

      {doc.indicators.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-medium text-gray-400">Indicators</p>
          <div className="flex flex-wrap gap-1">
            {doc.indicators.map((ind) => (
              <span
                key={ind.id}
                className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-xs text-gray-300"
              >
                {ind.id} ({ind.type})
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
