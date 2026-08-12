export default function Placeholder({ title }: { title: string }) {
  return (
    <div>
      <h1 className="mb-2 text-xl font-semibold text-gray-100">{title}</h1>
      <p className="text-sm text-gray-500">Coming soon.</p>
    </div>
  )
}
