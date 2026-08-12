/**
 * The stocks-vs-options visual language, in one place.
 *
 * The app trades two instrument classes with very different risk shapes, and a
 * user must never have to guess which one they are looking at. So the
 * distinction is carried by COLOUR and by an explicit label, everywhere, and
 * both come from this file rather than being re-invented per page:
 *
 *   equities / shares -> emerald+teal (the app's pre-existing accent)
 *   options / contracts -> amber
 *
 * Colour alone is never the only signal — every badge also carries text, so the
 * distinction survives greyscale and colour-blindness.
 */

export type AssetType = 'EQUITY' | 'OPTION'

/** Tailwind class fragments per asset type, for surfaces that are not badges
 *  (panel borders, table row accents, section headers). */
export const ASSET_STYLES: Record<
  AssetType,
  { text: string; border: string; bg: string; ring: string; accent: string; label: string }
> = {
  EQUITY: {
    text: 'text-emerald-300',
    border: 'border-emerald-500/30',
    bg: 'bg-emerald-500/10',
    ring: 'ring-emerald-500/30',
    accent: 'bg-emerald-600 hover:bg-emerald-500',
    label: 'Stock',
  },
  OPTION: {
    text: 'text-amber-300',
    border: 'border-amber-500/30',
    bg: 'bg-amber-500/10',
    ring: 'ring-amber-500/30',
    accent: 'bg-amber-600 hover:bg-amber-500',
    label: 'Option',
  },
}

export function AssetTypeBadge({
  type,
  size = 'sm',
  className = '',
}: {
  type: AssetType
  size?: 'xs' | 'sm'
  className?: string
}) {
  const s = ASSET_STYLES[type]
  const pad = size === 'xs' ? 'px-1.5 py-0 text-[10px]' : 'px-2 py-0.5 text-xs'
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded-full border font-medium uppercase tracking-wide ${pad} ${s.text} ${s.border} ${s.bg} ${className}`}
    >
      {s.label}
    </span>
  )
}

/** Call/put pill. Calls read green, puts read red — the near-universal
 *  convention on every options screen, so contradicting it would be its own
 *  source of error. This is DIRECTIONAL colour and is independent of the
 *  amber "this is an option" signal above; both are shown together. */
export function RightBadge({
  right,
  className = '',
}: {
  right: 'C' | 'P'
  className?: string
}) {
  const isCall = right === 'C'
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded px-1.5 py-0 text-[10px] font-bold uppercase ${
        isCall
          ? 'bg-green-500/15 text-green-300 ring-1 ring-green-500/30'
          : 'bg-rose-500/15 text-rose-300 ring-1 ring-rose-500/30'
      } ${className}`}
    >
      {isCall ? 'Call' : 'Put'}
    </span>
  )
}

/**
 * A contract's identity, rendered readably.
 *
 * The raw OCC symbol (`SPY   260918C00680000`) is unreadable at a glance and
 * its embedded spaces render badly in HTML, so it is shown decomposed. The raw
 * form is kept in the `title` for copy/paste and for cross-referencing against
 * a Schwab statement.
 */
export function ContractLabel({
  symbol,
  describe,
  right,
  strike,
  expiry,
  dte,
  className = '',
}: {
  symbol: string
  describe?: string
  right: 'C' | 'P'
  strike: number
  expiry: string
  dte?: number | null
  className?: string
}) {
  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`} title={symbol}>
      <RightBadge right={right} />
      <span className="font-mono text-gray-200">{formatStrike(strike)}</span>
      <span className="text-xs text-gray-500">
        {formatExpiry(expiry)}
        {dte != null && ` · ${dte}d`}
      </span>
      {describe && <span className="sr-only">{describe}</span>}
    </span>
  )
}

export function formatStrike(strike: number): string {
  return strike % 1 === 0 ? String(strike) : String(Number(strike.toFixed(3)))
}

export function formatExpiry(iso: string): string {
  const [y, m, d] = iso.split('-')
  if (!y || !m || !d) return iso
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  return `${d}${months[Number(m) - 1] ?? m}${y.slice(2)}`
}
