import type { ReactNode } from 'react'

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-gray-400">
      <span>{label}</span>
      {children}
    </label>
  )
}

export function inputCls(extra = ''): string {
  return `rounded border border-white/10 bg-white/5 px-2 py-1.5 text-sm text-gray-200 placeholder:text-gray-600 focus:border-emerald-500 focus:outline-none ${extra}`
}

export function selectCls(extra = ''): string {
  return `rounded border border-white/10 bg-white/5 px-2 py-1.5 text-sm text-gray-200 focus:border-emerald-500 focus:outline-none ${extra}`
}

export function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  step,
}: {
  label: string
  value: number
  onChange: (v: number) => void
  min?: number
  max?: number
  step?: number
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        value={Number.isFinite(value) ? value : 0}
        min={min}
        max={max}
        step={step ?? 1}
        onChange={(e) => onChange(e.target.valueAsNumber)}
        className={inputCls('w-full')}
      />
    </Field>
  )
}

export function TextField({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
}) {
  return (
    <Field label={label}>
      <input
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={inputCls('w-full')}
      />
    </Field>
  )
}

export function SelectField({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
}) {
  return (
    <Field label={label}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={selectCls('w-full')}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </Field>
  )
}

export function CheckboxField({
  label,
  checked,
  onChange,
}: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <label className="flex items-center gap-2 text-xs text-gray-400">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-3.5 w-3.5 rounded border-white/20 bg-white/5"
      />
      <span>{label}</span>
    </label>
  )
}

export function SmallButton({
  onClick,
  children,
  variant = 'default',
  disabled,
}: {
  onClick: () => void
  children: ReactNode
  variant?: 'default' | 'danger'
  disabled?: boolean
}) {
  const base = 'rounded px-2 py-1 text-xs font-medium disabled:opacity-50'
  const style =
    variant === 'danger'
      ? 'border border-red-500/30 text-red-400 hover:bg-red-500/10'
      : 'border border-white/10 text-gray-300 hover:bg-white/10'
  return (
    <button onClick={onClick} disabled={disabled} className={`${base} ${style}`}>
      {children}
    </button>
  )
}
