import { Link } from 'react-router-dom'

/** Small "how does this work?" link to a specific Docs section, meant to sit
 * next to a page heading. Exists to lower the barrier for a new user who
 * hits an empty chart or a failed backtest and doesn't know it's a data
 * problem — one click explains it instead of them giving up. */
export default function DocsHelpLink({
  to = '/docs#market-data',
  label = 'How does data work?',
}: {
  to?: string
  label?: string
}) {
  return (
    <Link
      to={to}
      className="inline-flex items-center gap-1 rounded-full border border-white/10 px-2.5 py-1 text-xs text-gray-400 hover:border-white/20 hover:text-gray-200"
    >
      <svg
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.75}
        strokeLinecap="round"
        strokeLinejoin="round"
        className="h-3.5 w-3.5"
      >
        <circle cx="12" cy="12" r="10" />
        <path d="M9.5 9a2.5 2.5 0 0 1 4.9.75c0 1.67-2.4 2-2.4 3.5" />
        <path d="M12 17.25h.01" />
      </svg>
      {label}
    </Link>
  )
}
