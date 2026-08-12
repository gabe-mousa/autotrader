import type { AIResult } from '../../lib/strategy'

// Shared read-only summary of an AIResult: assumptions, notes, and
// amber callouts for anything the model could not fully support.
export default function AiResultSummary({ result }: { result: AIResult }) {
  return (
    <div className="flex flex-col gap-3">
      {result.notes && <p className="text-sm text-gray-300">{result.notes}</p>}

      {result.assumptions.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-medium text-gray-400">Assumptions</p>
          <ul className="list-inside list-disc space-y-0.5 text-xs text-gray-400">
            {result.assumptions.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        </div>
      )}

      {result.warnings.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-medium text-amber-400">Warnings</p>
          <ul className="list-inside list-disc space-y-0.5 text-xs text-amber-400">
            {result.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      {result.unsupported.length > 0 && (
        <div className="flex flex-col gap-2">
          <p className="text-xs font-medium text-amber-400">
            Not fully supported ({result.unsupported.length})
          </p>
          {result.unsupported.map((u, i) => (
            <div
              key={i}
              className="rounded border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200"
            >
              <p className="font-medium text-amber-300">{u.requested}</p>
              <p className="mt-1 text-amber-300/80">{u.reason}</p>
              {u.approximation && (
                <p className="mt-1 text-amber-200/80">
                  <span className="font-medium">Approximation:</span> {u.approximation}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
