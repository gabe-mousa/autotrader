// Breadcrumb / back-navigation model.
//
// The trail is HIERARCHICAL, not history-based: a crumb always points at the
// logical parent of the current page, so the same page shows the same trail no
// matter how you got there (sidebar, a table row, a redirect after a mutation).
// Browser back still does history; the back button here does "up one level",
// which is the thing that is otherwise unreachable in one click.
//
// Every route in App.tsx must appear in ROUTES — resolveTrail falls back to a
// bare path-segment trail if one is missing, but the label will be ugly.

export type RouteParams = Record<string, string>

export interface RouteNode {
  /** Path pattern, matching the App.tsx <Route path>. */
  pattern: string
  /** Crumb text. A function gets the matched params + the query string. */
  label: string | ((params: RouteParams, search: URLSearchParams) => string)
  /**
   * Pattern of the logical parent. Params are substituted from the current
   * match, so '/strategies/:slug' as a parent of '/strategies/:slug/promote'
   * resolves to the concrete strategy the user is on. Omit for top-level
   * pages (the ones reachable from the sidebar) — those get no back button.
   */
  parent?: string
  /**
   * Which cached entity supplies a nicer label than the raw id. Resolved by
   * useBreadcrumbs against the react-query cache; the static `label` above is
   * the fallback while the query is still loading or has failed.
   */
  entity?: { kind: 'strategy' | 'backtest' | 'run'; param: string }
}

/** Short, readable form of an opaque id for a crumb. */
export function shortId(id: string | undefined): string {
  if (!id) return '?'
  return id.length > 10 ? `${id.slice(0, 8)}…` : id
}

export const ROUTES: RouteNode[] = [
  { pattern: '/', label: 'Dashboard' },

  { pattern: '/strategies', label: 'Strategies' },
  { pattern: '/strategies/new', label: 'New strategy', parent: '/strategies' },
  { pattern: '/strategies/new/blank', label: 'Blank editor', parent: '/strategies/new' },
  { pattern: '/strategies/new/editor', label: 'AI draft', parent: '/strategies/new' },
  {
    pattern: '/strategies/:slug',
    label: (p) => p.slug ?? 'Strategy',
    parent: '/strategies',
    entity: { kind: 'strategy', param: 'slug' },
  },
  {
    pattern: '/strategies/:slug/promote',
    label: 'Promote to live',
    parent: '/strategies/:slug',
  },

  { pattern: '/running', label: 'Running' },
  {
    pattern: '/running/:runId',
    label: (p) => `Run ${shortId(p.runId)}`,
    parent: '/running',
    entity: { kind: 'run', param: 'runId' },
  },

  {
    pattern: '/backtests',
    label: (_p, search) => {
      const slug = search.get('slug')
      return slug ? `Backtests · ${slug}` : 'Backtests'
    },
  },
  {
    pattern: '/backtests/compare',
    label: (_p, search) => {
      const n = (search.get('ids') ?? '').split(',').filter(Boolean).length
      return n ? `Compare (${n})` : 'Compare'
    },
    parent: '/backtests',
  },
  {
    pattern: '/backtests/:id',
    label: (p) => `Backtest ${shortId(p.id)}`,
    parent: '/backtests',
    entity: { kind: 'backtest', param: 'id' },
  },

  { pattern: '/charts', label: 'Charts' },
  { pattern: '/orders', label: 'Orders' },
  { pattern: '/data', label: 'Data' },
  { pattern: '/docs', label: 'Docs' },
  { pattern: '/settings', label: 'Settings' },
  { pattern: '/live-probe', label: 'Live probe' },
]

const BY_PATTERN = new Map(ROUTES.map((r) => [r.pattern, r]))

/** Split a path into segments, ignoring leading/trailing slashes. */
function segments(path: string): string[] {
  return path.split('/').filter(Boolean)
}

/**
 * Match a concrete pathname against a pattern, returning the extracted params
 * or null. Static segments beat nothing fancy — no splats, no optionals, which
 * is all App.tsx uses.
 */
export function matchPattern(pattern: string, pathname: string): RouteParams | null {
  const pat = segments(pattern)
  const path = segments(pathname)
  if (pat.length !== path.length) return null
  const params: RouteParams = {}
  for (let i = 0; i < pat.length; i++) {
    const p = pat[i]
    if (p.startsWith(':')) {
      params[p.slice(1)] = decodeURIComponent(path[i])
    } else if (p !== path[i]) {
      return null
    }
  }
  return params
}

/**
 * Find the route node for a pathname. Static patterns win over dynamic ones so
 * '/strategies/new' resolves to the New-strategy page rather than to a strategy
 * whose slug happens to be "new".
 */
export function matchRoute(pathname: string): { node: RouteNode; params: RouteParams } | null {
  const candidates: { node: RouteNode; params: RouteParams; dynamic: number }[] = []
  for (const node of ROUTES) {
    const params = matchPattern(node.pattern, pathname)
    if (params) {
      candidates.push({
        node,
        params,
        dynamic: segments(node.pattern).filter((s) => s.startsWith(':')).length,
      })
    }
  }
  if (candidates.length === 0) return null
  candidates.sort((a, b) => a.dynamic - b.dynamic)
  return candidates[0]
}

/** Substitute matched params back into a parent pattern. */
export function fillPattern(pattern: string, params: RouteParams): string {
  const filled = segments(pattern).map((s) =>
    s.startsWith(':') ? encodeURIComponent(params[s.slice(1)] ?? '') : s,
  )
  return `/${filled.join('/')}`
}

export interface Crumb {
  /** Concrete path to navigate to. */
  path: string
  /** Pattern this crumb came from, or '' for a fallback crumb. */
  pattern: string
  label: string
  params: RouteParams
  entity?: RouteNode['entity']
  /** True for the page currently being viewed (rendered as plain text). */
  current: boolean
}

/**
 * Build the root-first crumb trail for a location. The last crumb is always
 * the current page; its parent (if any) is the back-button target.
 *
 * Query strings are dropped from ancestor crumbs on purpose: '/backtests' as
 * the parent of a compare view should land on the unfiltered list.
 */
export function resolveTrail(pathname: string, searchString = ''): Crumb[] {
  const search = new URLSearchParams(searchString)
  const matched = matchRoute(pathname)

  if (!matched) {
    // Unregistered route: degrade to raw segments rather than showing nothing.
    const segs = segments(pathname)
    return segs.map((s, i) => ({
      path: `/${segs.slice(0, i + 1).join('/')}`,
      pattern: '',
      label: s,
      params: {},
      current: i === segs.length - 1,
    }))
  }

  const trail: Crumb[] = []
  let node: RouteNode | undefined = matched.node
  let path = pathname
  const params = matched.params
  const seen = new Set<string>()

  while (node) {
    if (seen.has(node.pattern)) break // guard against a mis-typed parent cycle
    seen.add(node.pattern)
    const label =
      typeof node.label === 'function'
        ? node.label(params, trail.length === 0 ? search : new URLSearchParams())
        : node.label
    trail.unshift({
      path,
      pattern: node.pattern,
      label,
      params,
      entity: node.entity,
      current: trail.length === 0,
    })
    if (!node.parent) break
    path = fillPattern(node.parent, params)
    node = BY_PATTERN.get(node.parent)
  }

  // Every trail starts at the Dashboard so there is always a way home.
  if (trail[0]?.pattern !== '/') {
    trail.unshift({ path: '/', pattern: '/', label: 'Dashboard', params: {}, current: false })
  }
  return trail
}
