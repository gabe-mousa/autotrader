import { useState, type ReactNode } from 'react'

export default function CollapsiblePanel({
  title,
  defaultOpen = true,
  children,
}: {
  title: string
  defaultOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  // `defaultOpen` is usually derived from the document ("open the Market filter
  // panel if this strategy has one"), and the editor mounts with a blank
  // template BEFORE the fetched document arrives. Without this, the panel
  // latches to its first-render value and a configured section stays collapsed
  // forever. Re-sync only when the prop actually CHANGES, so a section the user
  // collapsed by hand stays collapsed.
  const [prevDefault, setPrevDefault] = useState(defaultOpen)
  if (prevDefault !== defaultOpen) {
    setPrevDefault(defaultOpen)
    setOpen(defaultOpen)
  }
  return (
    <section className="rounded border border-white/10">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-4 py-2.5 text-left"
      >
        <h2 className="text-sm font-semibold text-gray-200">{title}</h2>
        <span className="text-xs text-gray-500">{open ? '▾' : '▸'}</span>
      </button>
      {open && <div className="border-t border-white/10 p-4">{children}</div>}
    </section>
  )
}
