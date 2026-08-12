import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ApiError, getAiStatus, postAiRefine } from '../../lib/api'
import type { AIResult, StrategyDocument } from '../../lib/strategy'
import { inputCls } from '../editor/fields'
import AiResultSummary from './AiResultSummary'
import DocumentPreview from './DocumentPreview'

// Collapsible AI refinement chat, shown in the editor's sidebar. Lets the
// user describe a change in plain English and review/apply the result
// before it touches the (unsaved) editor state.
export default function RefinePanel({
  doc,
  conversationId,
  setConversationId,
  onApply,
}: {
  doc: StrategyDocument
  conversationId: string | null
  setConversationId: (id: string) => void
  onApply: (doc: StrategyDocument) => void
}) {
  const [message, setMessage] = useState('')
  const [pending, setPending] = useState<AIResult | null>(null)

  const statusQuery = useQuery({ queryKey: ['ai-status'], queryFn: getAiStatus, retry: false })
  const configured = statusQuery.data?.configured ?? false

  const refineMutation = useMutation({
    mutationFn: () => postAiRefine(conversationId ?? '', message, doc),
    onSuccess: (r) => {
      setConversationId(r.conversation_id)
      setPending(r)
    },
  })

  function send() {
    if (message.trim().length === 0) return
    refineMutation.mutate()
  }

  function apply() {
    if (pending?.document) onApply(pending.document)
    setPending(null)
    setMessage('')
  }

  function discard() {
    setPending(null)
  }

  if (!statusQuery.isLoading && !configured) {
    return (
      <section className="rounded border border-white/10 p-4">
        <h2 className="mb-2 text-sm font-semibold text-gray-200">AI refine</h2>
        <p className="text-xs text-gray-500">
          AI refinement needs an Anthropic API key in .env — see Settings.
        </p>
      </section>
    )
  }

  return (
    <section className="rounded border border-white/10 p-4">
      <h2 className="mb-2 text-sm font-semibold text-gray-200">AI refine</h2>

      {!pending && (
        <div className="flex flex-col gap-2">
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="e.g. Add a trailing stop at 3% and only trade during regular hours."
            rows={3}
            className={inputCls('w-full resize-y')}
            disabled={refineMutation.isPending}
          />
          <button
            onClick={send}
            disabled={refineMutation.isPending || message.trim().length === 0}
            className="self-start rounded bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {refineMutation.isPending ? 'Thinking…' : 'Send'}
          </button>
          {refineMutation.isPending && (
            <p className="text-xs text-gray-500">Thinking… this can take 10-30s</p>
          )}
          {refineMutation.isError && (
            <p className="text-xs text-red-400">
              {refineMutation.error instanceof ApiError
                ? refineMutation.error.message
                : 'Failed to refine strategy.'}
            </p>
          )}
        </div>
      )}

      {pending && !pending.ok && (
        <div className="flex flex-col gap-2">
          <div className="rounded border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-400">
            <p className="mb-1 font-medium">The AI could not apply that change:</p>
            <ul className="list-inside list-disc space-y-0.5">
              {pending.errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          </div>
          <button
            onClick={discard}
            className="self-start rounded border border-white/10 px-3 py-1.5 text-xs font-medium text-gray-300 hover:bg-white/5"
          >
            Back
          </button>
        </div>
      )}

      {pending && pending.ok && pending.document && (
        <div className="flex flex-col gap-3">
          <p className="text-xs font-medium text-gray-400">Review AI changes</p>
          <DocumentPreview doc={pending.document} />
          <AiResultSummary result={pending} />
          <div className="flex gap-2">
            <button
              onClick={apply}
              className="rounded bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500"
            >
              Apply
            </button>
            <button
              onClick={discard}
              className="rounded border border-white/10 px-3 py-1.5 text-xs font-medium text-gray-300 hover:bg-white/5"
            >
              Discard
            </button>
          </div>
        </div>
      )}
    </section>
  )
}
