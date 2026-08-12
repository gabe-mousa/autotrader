import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError, getAiStatus, postAiGenerate } from '../lib/api'
import { inputCls } from '../components/editor/fields'
import AiResultSummary from '../components/ai/AiResultSummary'
import DocumentPreview from '../components/ai/DocumentPreview'

export default function StrategyNew() {
  const navigate = useNavigate()
  const [prompt, setPrompt] = useState('')

  const statusQuery = useQuery({ queryKey: ['ai-status'], queryFn: getAiStatus, retry: false })
  const configured = statusQuery.data?.configured ?? false

  const generateMutation = useMutation({
    mutationFn: () => postAiGenerate(prompt),
  })

  const result = generateMutation.data
  const showPreview = result && result.ok && result.document

  function openInEditor() {
    if (!result || !result.document) return
    navigate('/strategies/new/editor', {
      state: { draft: result.document, conversationId: result.conversation_id, aiResult: result },
    })
  }

  return (
    <div className="max-w-6xl">
      <h1 className="mb-1 text-xl font-semibold text-gray-100">New strategy</h1>
      <p className="mb-6 text-sm text-gray-500">
        Describe what you want in plain English, or start from a blank template.
      </p>

      {/* min-h (not flex-1 off a fixed-height ancestor) so the boxes grow
          with the viewport on load without fighting sibling content for
          space once results/errors render below — that fight is what caused
          the post-generate content to overlap the boxes above it. */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 min-h-[max(280px,calc(100vh-260px))]">
        <div className="flex min-h-[280px] flex-col rounded border border-white/10 p-4">
          <h2 className="mb-1 text-sm font-semibold text-gray-200">Describe it in plain English</h2>
          <p className="mb-3 text-xs text-gray-500">
            Tell the AI what the strategy should do and it will draft it for you.
          </p>
          {!statusQuery.isLoading && !configured && (
            <p className="rounded border border-white/10 bg-white/5 p-3 text-xs text-gray-400">
              AI generation needs an Anthropic API key in .env — see{' '}
              <Link to="/settings" className="text-emerald-400 hover:underline">
                Settings
              </Link>
              .
            </p>
          )}
          {configured && (
            <div className="flex min-h-0 flex-1 flex-col gap-2">
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="e.g. Buy SPY when RSI(14) drops below 30 on the daily chart, exit at a 5% profit or 2% stop loss."
                className={inputCls('w-full flex-1 resize-none')}
                disabled={generateMutation.isPending}
              />
              <button
                onClick={() => generateMutation.mutate()}
                disabled={generateMutation.isPending || prompt.trim().length === 0}
                className="self-start rounded bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
              >
                {generateMutation.isPending ? 'Generating…' : 'Generate'}
              </button>
              {generateMutation.isPending && (
                <p className="text-xs text-gray-500">Thinking… this can take 10-30s</p>
              )}
            </div>
          )}
        </div>

        <div className="flex min-h-[280px] flex-col rounded border border-white/10 p-4">
          <h2 className="mb-1 text-sm font-semibold text-gray-200">Start from a blank template</h2>
          <p className="mb-3 text-xs text-gray-500">
            Build the strategy yourself field by field in the full editor.
          </p>
          <button
            onClick={() => navigate('/strategies/new/blank')}
            className="mt-auto self-start rounded border border-white/10 px-4 py-1.5 text-sm font-medium text-gray-200 hover:bg-white/5"
          >
            Open blank editor
          </button>
        </div>
      </div>

      {generateMutation.isError && (
        <div className="mt-4 rounded border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {generateMutation.error instanceof ApiError
            ? generateMutation.error.message
            : 'Failed to generate a strategy.'}
        </div>
      )}

      {result && !result.ok && (
        <div className="mt-4 rounded border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          <p className="mb-1 font-medium">The AI could not produce a strategy:</p>
          <ul className="list-inside list-disc space-y-0.5">
            {result.errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-red-300/80">
            Edit your description above and try again.
          </p>
        </div>
      )}

      {showPreview && result.document && (
        <div className="mt-6 flex flex-col gap-4">
          <DocumentPreview doc={result.document} />
          <AiResultSummary result={result} />
          <div className="flex gap-3">
            <button
              onClick={openInEditor}
              className="rounded bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-500"
            >
              Open in editor
            </button>
            <button
              onClick={() => generateMutation.reset()}
              className="rounded border border-white/10 px-4 py-1.5 text-sm font-medium text-gray-300 hover:bg-white/5"
            >
              Discard &amp; edit prompt
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
