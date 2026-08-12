import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { activateKill, deactivateKill, getHealth, getKillStatus } from '../lib/api'
import ThemeToggle from './ThemeToggle'

function Dot({ ok }: { ok: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${ok ? 'bg-emerald-500' : 'bg-red-500'}`}
    />
  )
}

export default function StatusBar() {
  const queryClient = useQueryClient()

  const { data, isError } = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    refetchInterval: 5000,
    retry: false,
  })

  const killQuery = useQuery({
    queryKey: ['kill-status'],
    queryFn: getKillStatus,
    refetchInterval: 5000,
    retry: false,
  })

  const activateMutation = useMutation({
    mutationFn: (cancelAll: boolean) => activateKill(cancelAll),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['kill-status'] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  const deactivateMutation = useMutation({
    mutationFn: deactivateKill,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['kill-status'] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  const apiOk = !isError && data?.status === 'ok'
  const authConnected = !!data?.auth?.connected
  const streamerConnected = data?.streamer === 'connected'
  const killActive = killQuery.data?.active ?? false

  function onKillClick() {
    if (killActive) {
      if (window.confirm('Deactivate the kill switch and allow new orders again?')) {
        deactivateMutation.mutate()
      }
      return
    }
    // Two SEPARATE confirms on purpose: the first is the real "are you sure"
    // (Cancel here means "don't activate, abort" as any user would expect
    // from a kill switch) — folding cancel_all into the same dialog made
    // Cancel silently activate anyway, just without mass-cancel, which is
    // exactly the kind of surprise a kill switch must never produce.
    if (!window.confirm('Activate the kill switch? This blocks ALL new order placements across every run.')) {
      return
    }
    const cancelAll = window.confirm(
      'Also mass-cancel every resting order across all active runs?\n\n' +
        'OK: cancel all resting orders too. Cancel: just block new orders (existing resting orders stay open).',
    )
    activateMutation.mutate(cancelAll)
  }

  return (
    <div className="flex h-9 shrink-0 items-center gap-4 border-b border-white/10 bg-[#1d1a17] px-4 text-xs text-gray-400">
      <div className="flex items-center gap-1.5">
        <Dot ok={apiOk} />
        <span>{apiOk ? 'API online' : 'API offline'}</span>
      </div>
      <div className="flex items-center gap-1.5">
        <Dot ok={apiOk && authConnected} />
        <span>Schwab {apiOk && authConnected ? 'connected' : 'disconnected'}</span>
      </div>
      <div className="flex items-center gap-1.5">
        <Dot ok={apiOk && streamerConnected} />
        <span>Streamer {apiOk ? data?.streamer ?? 'unknown' : 'unknown'}</span>
      </div>
      <div className="ml-auto flex items-center gap-3">
        <ThemeToggle />
        <button
          onClick={onKillClick}
          disabled={activateMutation.isPending || deactivateMutation.isPending}
          className={
            killActive
              ? 'rounded-full bg-red-600 px-3 py-1 text-xs font-semibold text-white hover:bg-red-500 disabled:opacity-50'
              : 'rounded-full border border-red-500/40 px-3 py-1 text-xs text-red-400 hover:bg-red-500/10 disabled:opacity-50'
          }
        >
          {killActive ? 'KILL SWITCH ACTIVE' : 'Kill switch'}
        </button>
      </div>
    </div>
  )
}
