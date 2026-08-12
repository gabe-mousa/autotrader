import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { resolveTrail, type Crumb } from '../lib/breadcrumbs'
import type { BacktestListResponse, BacktestDetailResponse, RunsListResponse, RunDetail } from '../lib/api'
import type { StrategyDetailResponse, StrategyListResponse } from '../lib/strategy'

/**
 * Nicer label for a crumb that points at an entity, read from whatever the
 * react-query cache already holds. This is deliberately read-only: the trail
 * never fires a request of its own, so a breadcrumb can't make a page slower
 * or hit the API for a run the user isn't looking at. If the cache is cold the
 * caller's static fallback (e.g. "Run a1b2c3d4…") stands.
 */
function useEntityLabel(crumb: Crumb): string {
  const kind = crumb.entity?.kind
  const id = crumb.entity ? crumb.params[crumb.entity.param] : undefined

  // `enabled: false` means these NEVER fetch — they only subscribe to whatever
  // the page itself already put in the cache. useQuery (rather than
  // getQueryData) so the crumb re-renders when the page's own query resolves;
  // getQueryData is a one-shot read and would leave a deep-linked page stuck
  // showing the raw id forever. The hook list is fixed and unconditional.
  const cached = <T,>(key: unknown[], on: boolean) =>
    // eslint-disable-next-line react-hooks/rules-of-hooks -- fixed call order
    useQuery<T>({ queryKey: on ? key : ['breadcrumb-noop'], queryFn: neverFetch, enabled: false })
      .data

  const strategyDetail = cached<StrategyDetailResponse>(['strategy', id], kind === 'strategy' && !!id)
  const strategyList = cached<StrategyListResponse>(['strategies'], kind === 'strategy')
  const runDetail = cached<RunDetail>(['run', id], kind === 'run' && !!id)
  const runList = cached<RunsListResponse>(['runs'], kind === 'run')
  const backtestList = cached<BacktestListResponse>(['backtests'], kind === 'backtest')
  const backtestDetail = cached<BacktestDetailResponse>(['backtest', id], kind === 'backtest' && !!id)

  if (!kind || !id) return crumb.label

  switch (kind) {
    case 'strategy':
      return (
        strategyDetail?.document?.meta?.name ??
        strategyList?.strategies?.find((s) => s.slug === id)?.name ??
        crumb.label
      )
    case 'run':
      return runDetail?.name ?? runList?.runs?.find((r) => r.id === id)?.name ?? crumb.label
    case 'backtest': {
      // The detail response carries no strategy name, so prefer the list entry
      // (which does) and fall back to the symbol from the detail.
      const hit = backtestList?.backtests?.find((b) => b.id === id)
      if (hit?.strategy_name) return hit.strategy_name
      if (backtestDetail?.symbol) return `Backtest · ${backtestDetail.symbol}`
      return crumb.label
    }
    default:
      return crumb.label
  }
}

/** Guard: these queries are `enabled: false` and must never issue a request. */
function neverFetch(): never {
  throw new Error('breadcrumb queries are read-only')
}

function CrumbLabel({ crumb }: { crumb: Crumb }) {
  const label = useEntityLabel(crumb)
  if (crumb.current) {
    return (
      <span aria-current="page" className="max-w-[22rem] truncate text-gray-200">
        {label}
      </span>
    )
  }
  return (
    <Link
      to={crumb.path}
      className="max-w-[16rem] truncate text-gray-400 transition-colors hover:text-gray-200 hover:underline"
    >
      {label}
    </Link>
  )
}

export default function Breadcrumbs() {
  const location = useLocation()
  const navigate = useNavigate()
  const trail = resolveTrail(location.pathname, location.search)

  // "Up one level", not history back: the parent is the crumb before the
  // current page. On the Dashboard there is nowhere up to go.
  const parent = trail.length > 1 ? trail[trail.length - 2] : null

  return (
    <div className="flex h-9 shrink-0 items-center gap-2 border-b border-white/10 bg-[#191613] px-4 text-xs">
      <button
        type="button"
        onClick={() => parent && navigate(parent.path)}
        disabled={!parent}
        title={parent ? `Back to ${parent.label}` : 'Nothing above the Dashboard'}
        aria-label={parent ? `Back to ${parent.label}` : 'Back'}
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded border border-white/10 text-gray-400 transition-colors hover:bg-white/5 hover:text-gray-200 disabled:cursor-default disabled:opacity-30 disabled:hover:bg-transparent"
      >
        <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.6">
          <path d="M10 3.5 5.5 8l4.5 4.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-2">
        {trail.map((crumb, i) => (
          <span key={`${crumb.path}-${i}`} className="flex min-w-0 items-center gap-2">
            {i > 0 && <span className="select-none text-gray-600">/</span>}
            <CrumbLabel crumb={crumb} />
          </span>
        ))}
      </nav>
    </div>
  )
}
