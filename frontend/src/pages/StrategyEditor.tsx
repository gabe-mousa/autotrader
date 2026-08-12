import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import {
  createStrategy,
  getCatalog,
  getStrategy,
  setStrategyArchived,
  updateStrategy,
  validateStrategy,
} from '../lib/api'
import { ApiError } from '../lib/api'
import {
  newTemplateDocument,
  type CreateStrategyResponse,
  type StrategyDetailResponse,
  type StrategyDocument,
  type ValidateResponse,
} from '../lib/strategy'
import { inputCls } from '../components/editor/fields'
import CollapsiblePanel from '../components/editor/CollapsiblePanel'
import UniversePanel from '../components/editor/UniversePanel'
import TimeframePanel from '../components/editor/TimeframePanel'
import SessionPanel from '../components/editor/SessionPanel'
import IndicatorsPanel from '../components/editor/IndicatorsPanel'
import EntryPanel from '../components/editor/EntryPanel'
import ExitPanel from '../components/editor/ExitPanel'
import MarketFilterPanel from '../components/editor/MarketFilterPanel'
import SelectionPanel from '../components/editor/SelectionPanel'
import ExpressionPanel from '../components/editor/ExpressionPanel'
import SizingPanel from '../components/editor/SizingPanel'
import RiskPanel from '../components/editor/RiskPanel'
import OrdersPanel from '../components/editor/OrdersPanel'
import RefinePanel from '../components/ai/RefinePanel'

interface StrategyNewLocationState {
  draft?: StrategyDocument
  conversationId?: string
}

// Branches off an independent copy of `base` — whatever's currently in the
// editor, including unsaved edits, not just what's on disk — so the two
// strategies never share identity. `meta.id` is dropped (not just cleared)
// so the backend's default_factory mints a fresh uuid; reusing the
// original's id would collide in the strategies DB index (id is its primary
// key — see backend/app/strategy/files.py's reindex()). Retries with an
// incrementing "(copy N)" suffix on a slug collision (POST /strategies 409s
// if `strategies/<slug>/strategy.yaml` already exists), e.g. duplicating an
// already-duplicated strategy.
async function createDuplicate(base: StrategyDocument): Promise<CreateStrategyResponse> {
  const { id: _droppedId, ...restMeta } = base.meta
  for (let n = 1; n <= 20; n++) {
    const suffix = n === 1 ? '(copy)' : `(copy ${n})`
    const candidate: StrategyDocument = {
      ...base,
      meta: { ...restMeta, name: `${base.meta.name} ${suffix}`, version: 1, archived: false },
    }
    try {
      return await createStrategy(candidate)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) continue
      throw e
    }
  }
  throw new Error('Could not find an available name for the duplicate.')
}

export default function StrategyEditor() {
  const { slug: routeSlug } = useParams<{ slug: string }>()
  const isNew = !routeSlug || routeSlug === 'new'
  const navigate = useNavigate()
  const location = useLocation()
  const navState = (location.state as StrategyNewLocationState | null) ?? null
  const queryClient = useQueryClient()

  const [doc, setDoc] = useState<StrategyDocument>(() => navState?.draft ?? newTemplateDocument())
  const [loadedSlug, setLoadedSlug] = useState<string | null>(null)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [conversationId, setConversationId] = useState<string | null>(
    navState?.conversationId ?? null,
  )

  const catalogQuery = useQuery({ queryKey: ['catalog'], queryFn: getCatalog, retry: false })

  const detailQuery = useQuery({
    queryKey: ['strategy', routeSlug],
    queryFn: () => getStrategy(routeSlug as string),
    enabled: !isNew,
    retry: false,
  })

  useEffect(() => {
    if (isNew) {
      setDoc(navState?.draft ?? newTemplateDocument())
      setLoadedSlug(null)
      setSaveMsg(null)
      setConversationId(navState?.conversationId ?? null)
    }
    // Only re-seed when switching routes, not on every render — navState is
    // stable for the lifetime of a given navigation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isNew])

  useEffect(() => {
    if (detailQuery.data && detailQuery.data.slug !== loadedSlug) {
      setDoc(detailQuery.data.document)
      setLoadedSlug(detailQuery.data.slug)
    }
  }, [detailQuery.data, loadedSlug])

  // Debounced validation on every document change.
  const [validateResult, setValidateResult] = useState<ValidateResponse | null>(null)
  const [validating, setValidating] = useState(false)
  useEffect(() => {
    setValidating(true)
    const timer = setTimeout(() => {
      validateStrategy(doc)
        .then((r) => setValidateResult(r))
        .catch(() => setValidateResult(null))
        .finally(() => setValidating(false))
    }, 600)
    return () => clearTimeout(timer)
  }, [doc])

  const createMutation = useMutation({
    mutationFn: createStrategy,
    onSuccess: (r) => {
      setSaveMsg(`Saved (${r.content_hash})`)
      navigate(`/strategies/${r.slug}`)
    },
  })

  const updateMutation = useMutation({
    mutationFn: () => updateStrategy(routeSlug as string, doc),
    onSuccess: (r) => {
      setSaveMsg(
        `Saved v${r.version} (${r.content_hash})${r.new_version ? ' — new version' : ''}`,
      )
      // Keep local state AND the query cache in sync with what the server
      // just persisted. Without this, `doc.meta.version` stays whatever it
      // was when the page loaded (never re-synced), so the NEXT save call
      // resubmits a stale version number — harmless now that the backend is
      // server-authoritative for versioning, but the cache half of this
      // was the actual "my changes aren't there" bug: the `['strategy',
      // slug]` query is only re-synced into `doc` once per mount (see the
      // effect below), so an un-invalidated cache entry would keep serving
      // the PRE-edit document on the next visit to this strategy, even
      // though the file on disk was correctly saved.
      const savedDoc = { ...doc, meta: { ...doc.meta, version: r.version } }
      setDoc(savedDoc)
      queryClient.setQueryData(
        ['strategy', routeSlug],
        (old: StrategyDetailResponse | undefined) =>
          old ? { ...old, document: savedDoc, content_hash: r.content_hash } : old,
      )
    },
  })

  function handleSave() {
    setSaveMsg(null)
    if (isNew) createMutation.mutate(doc)
    else updateMutation.mutate()
  }

  const duplicateMutation = useMutation({
    mutationFn: () => createDuplicate(doc),
    onSuccess: (r) => {
      queryClient.invalidateQueries({ queryKey: ['strategies'] })
      navigate(`/strategies/${r.slug}`)
    },
  })

  const archiveMutation = useMutation({
    mutationFn: (archived: boolean) => setStrategyArchived(routeSlug as string, archived),
    onSuccess: (r) => {
      const nextDoc = { ...doc, meta: { ...doc.meta, archived: r.archived } }
      setDoc(nextDoc)
      queryClient.setQueryData(
        ['strategy', routeSlug],
        (old: StrategyDetailResponse | undefined) =>
          old ? { ...old, document: nextDoc } : old,
      )
      // Active/Archived tabs on the Strategies list read from a different
      // cache key per tab — invalidate all of them so this move is reflected
      // when the user navigates back there.
      queryClient.invalidateQueries({ queryKey: ['strategies'] })
    },
  })

  const saving = createMutation.isPending || updateMutation.isPending
  const saveError = createMutation.error ?? updateMutation.error
  const errors = validateResult?.errors ?? []
  const warnings = validateResult?.warnings ?? []
  const isValid = validateResult?.valid ?? false

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <input
          value={doc.meta.name}
          onChange={(e) => setDoc((d) => ({ ...d, meta: { ...d.meta, name: e.target.value } }))}
          placeholder="Strategy name"
          className={inputCls('w-64 text-lg font-semibold')}
        />
        <input
          value={doc.meta.description ?? ''}
          onChange={(e) =>
            setDoc((d) => ({ ...d, meta: { ...d.meta, description: e.target.value } }))
          }
          placeholder="Description"
          className={inputCls('flex-1 min-w-[12rem]')}
        />
        <button
          onClick={handleSave}
          disabled={saving}
          className="rounded bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        {!isNew && (
          <button
            onClick={() => navigate(`/backtests?slug=${routeSlug}`)}
            className="rounded border border-white/10 px-4 py-1.5 text-sm font-medium text-gray-300 hover:bg-white/5"
          >
            Run backtest
          </button>
        )}
        {!isNew && (
          <button
            onClick={() => navigate(`/strategies/${routeSlug}/promote`)}
            className="rounded border border-red-500/30 px-4 py-1.5 text-sm font-medium text-red-400 hover:bg-red-500/10"
          >
            Promote to live
          </button>
        )}
        {!isNew && (
          <button
            onClick={() => duplicateMutation.mutate()}
            disabled={duplicateMutation.isPending}
            className="rounded border border-white/10 px-4 py-1.5 text-sm font-medium text-gray-300 hover:bg-white/5 disabled:opacity-50"
          >
            {duplicateMutation.isPending ? 'Duplicating…' : 'Duplicate'}
          </button>
        )}
        {!isNew && (
          <button
            onClick={() => archiveMutation.mutate(!doc.meta.archived)}
            disabled={archiveMutation.isPending}
            className="rounded border border-white/10 px-4 py-1.5 text-sm font-medium text-gray-300 hover:bg-white/5 disabled:opacity-50"
          >
            {doc.meta.archived ? 'Restore' : 'Archive'}
          </button>
        )}
        {doc.meta.archived && (
          <span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-3 py-1 text-xs text-amber-300">
            Archived
          </span>
        )}
        <StatusPill validating={validating} isValid={isValid} errorCount={errors.length} />
      </div>

      {saveMsg && <p className="mb-3 text-sm text-teal-400">{saveMsg}</p>}
      {saveError && (
        <p className="mb-3 text-sm text-red-400">
          {saveError instanceof ApiError ? saveError.message : 'Failed to save strategy.'}
        </p>
      )}
      {archiveMutation.isError && (
        <p className="mb-3 text-sm text-red-400">
          {archiveMutation.error instanceof ApiError
            ? archiveMutation.error.message
            : 'Failed to update strategy.'}
        </p>
      )}
      {duplicateMutation.isError && (
        <p className="mb-3 text-sm text-red-400">
          {duplicateMutation.error instanceof ApiError
            ? duplicateMutation.error.message
            : 'Failed to duplicate strategy.'}
        </p>
      )}
      {!isNew && detailQuery.isError && (
        <p className="mb-3 text-sm text-red-400">Could not load strategy.</p>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_320px]">
        <div className="flex flex-col gap-4">
          <CollapsiblePanel title="Universe">
            <UniversePanel doc={doc} setDoc={setDoc} />
          </CollapsiblePanel>
          <CollapsiblePanel title="Timeframe">
            <TimeframePanel doc={doc} setDoc={setDoc} />
          </CollapsiblePanel>
          <CollapsiblePanel title="Session">
            <SessionPanel doc={doc} setDoc={setDoc} />
          </CollapsiblePanel>
          <CollapsiblePanel title="Indicators">
            <IndicatorsPanel doc={doc} setDoc={setDoc} catalog={catalogQuery.data} />
          </CollapsiblePanel>
          <CollapsiblePanel title="Entry">
            <EntryPanel doc={doc} setDoc={setDoc} catalog={catalogQuery.data} />
          </CollapsiblePanel>
          <CollapsiblePanel title="Exit">
            <ExitPanel doc={doc} setDoc={setDoc} catalog={catalogQuery.data} />
          </CollapsiblePanel>
          <CollapsiblePanel
            title={`Market filter${doc.market_filter ? ` — ${doc.market_filter.symbol}` : ''}`}
            defaultOpen={!!doc.market_filter}
          >
            <MarketFilterPanel doc={doc} setDoc={setDoc} catalog={catalogQuery.data} />
          </CollapsiblePanel>
          <CollapsiblePanel title="Selection" defaultOpen={!!doc.selection}>
            <SelectionPanel doc={doc} setDoc={setDoc} catalog={catalogQuery.data} />
          </CollapsiblePanel>
          {/* Between the rules and the sizing, which is where it belongs
              conceptually: the rules decide WHEN, this decides WHAT, sizing
              decides HOW MUCH. */}
          <ExpressionPanel doc={doc} onChange={setDoc} />
          <CollapsiblePanel title="Sizing">
            <SizingPanel doc={doc} setDoc={setDoc} />
          </CollapsiblePanel>
          <CollapsiblePanel title="Risk" defaultOpen={false}>
            <RiskPanel doc={doc} setDoc={setDoc} />
          </CollapsiblePanel>
          <CollapsiblePanel title="Orders" defaultOpen={false}>
            <OrdersPanel doc={doc} setDoc={setDoc} />
          </CollapsiblePanel>
        </div>

        <div className="flex flex-col gap-4 lg:sticky lg:top-6 lg:self-start">
          <RefinePanel
            doc={doc}
            conversationId={conversationId}
            setConversationId={setConversationId}
            onApply={setDoc}
          />
          <section className="rounded border border-white/10 p-4">
            <h2 className="mb-2 text-sm font-semibold text-gray-200">Validation</h2>
            {validating && <p className="text-xs text-gray-500">Validating…</p>}
            {!validating && errors.length === 0 && warnings.length === 0 && (
              <p className="text-xs text-teal-400">No issues.</p>
            )}
            {errors.length > 0 && (
              <div className="mb-3">
                <p className="mb-1 text-xs font-medium text-red-400">
                  Errors ({errors.length})
                </p>
                <ul className="list-inside list-disc space-y-0.5 text-xs text-red-400">
                  {errors.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </div>
            )}
            {warnings.length > 0 && (
              <div>
                <p className="mb-1 text-xs font-medium text-amber-400">
                  Warnings ({warnings.length})
                </p>
                <ul className="list-inside list-disc space-y-0.5 text-xs text-amber-400">
                  {warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  )
}

function StatusPill({
  validating,
  isValid,
  errorCount,
}: {
  validating: boolean
  isValid: boolean
  errorCount: number
}) {
  if (validating) {
    return (
      <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-gray-400">
        Validating…
      </span>
    )
  }
  if (isValid) {
    return (
      <span className="rounded-full border border-teal-500/30 bg-teal-500/10 px-3 py-1 text-xs text-teal-400">
        Valid ✓
      </span>
    )
  }
  return (
    <span className="rounded-full border border-red-500/30 bg-red-500/10 px-3 py-1 text-xs text-red-400">
      {errorCount} error{errorCount === 1 ? '' : 's'}
    </span>
  )
}
