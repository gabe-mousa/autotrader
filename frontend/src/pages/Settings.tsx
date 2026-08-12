import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ApiError,
  connectSchwab,
  getAuthStatus,
  getCredentials,
  getLiveTradingEnabled,
  getRiskLimits,
  putCredentials,
  putLiveTradingEnabled,
  putRiskLimits,
  type CredentialField,
  type RiskLimitsResponse,
} from '../lib/api'
import { inputCls } from '../components/editor/fields'

function parseSymbolList(raw: string): string[] {
  return raw
    .split(',')
    .map((s) => s.trim().toUpperCase())
    .filter((s) => s.length > 0)
}

const CREDENTIAL_FIELDS: {
  field: CredentialField
  label: string
  secret: boolean
  placeholder: string
}[] = [
  {
    field: 'schwab_client_id',
    label: 'Schwab Client ID',
    secret: true,
    placeholder: 'from your Schwab developer app',
  },
  {
    field: 'schwab_client_secret',
    label: 'Schwab Client Secret',
    secret: true,
    placeholder: 'from your Schwab developer app',
  },
  {
    field: 'schwab_callback_url',
    label: 'Schwab Callback URL',
    secret: false,
    placeholder: 'https://127.0.0.1:8443/oauth/callback',
  },
  {
    field: 'anthropic_api_key',
    label: 'Anthropic API Key',
    secret: true,
    placeholder: 'sk-ant-… (optional, for AI strategy generation)',
  },
]

export default function Settings() {
  const queryClient = useQueryClient()

  const { data: authStatus, isLoading, isError } = useQuery({
    queryKey: ['auth-status'],
    queryFn: getAuthStatus,
    retry: false,
  })

  const connectMutation = useMutation({
    mutationFn: connectSchwab,
    onSuccess: (data) => {
      window.open(data.authorize_url, '_blank', 'noopener,noreferrer')
      queryClient.invalidateQueries({ queryKey: ['auth-status'] })
    },
  })

  // ---- Credentials (.env) ----
  const credentialsQuery = useQuery({
    queryKey: ['credentials'],
    queryFn: getCredentials,
    retry: false,
  })

  const [credentialInputs, setCredentialInputs] = useState<Partial<Record<CredentialField, string>>>({})
  const [credentialMsg, setCredentialMsg] = useState<string | null>(null)

  const credentialsMutation = useMutation({
    mutationFn: putCredentials,
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['credentials'] })
      queryClient.invalidateQueries({ queryKey: ['health'] })
      setCredentialInputs({})
      setCredentialMsg(data.message)
      setTimeout(() => setCredentialMsg(null), 6000)
    },
  })

  function saveCredentials() {
    const updates = Object.fromEntries(
      Object.entries(credentialInputs).filter(([, v]) => (v ?? '').trim().length > 0),
    )
    if (Object.keys(updates).length === 0) return
    credentialsMutation.mutate(updates)
  }

  // ---- Live trading toggle ----
  const liveTradingQuery = useQuery({
    queryKey: ['live-trading-enabled'],
    queryFn: getLiveTradingEnabled,
    retry: false,
  })

  const [liveTradingMsg, setLiveTradingMsg] = useState<string | null>(null)

  const liveTradingMutation = useMutation({
    mutationFn: putLiveTradingEnabled,
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['live-trading-enabled'] })
      setLiveTradingMsg(data.message)
      setTimeout(() => setLiveTradingMsg(null), 6000)
    },
  })

  // ---- Global risk limits ----
  const riskQuery = useQuery({
    queryKey: ['risk-limits'],
    queryFn: getRiskLimits,
    retry: false,
  })

  const [maxNotional, setMaxNotional] = useState(0)
  const [maxDailyLoss, setMaxDailyLoss] = useState(0)
  const [blocklist, setBlocklist] = useState('')
  const [allowlist, setAllowlist] = useState('')
  const [saveMsg, setSaveMsg] = useState<string | null>(null)

  useEffect(() => {
    if (!riskQuery.data) return
    setMaxNotional(riskQuery.data.max_total_notional)
    setMaxDailyLoss(riskQuery.data.max_total_daily_loss)
    setBlocklist(riskQuery.data.symbol_blocklist.join(', '))
    setAllowlist(riskQuery.data.symbol_allowlist ? riskQuery.data.symbol_allowlist.join(', ') : '')
  }, [riskQuery.data])

  const riskMutation = useMutation({
    mutationFn: (overrides: Partial<RiskLimitsResponse>) => putRiskLimits(overrides),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['risk-limits'] })
      setSaveMsg('Saved.')
      setTimeout(() => setSaveMsg(null), 3000)
    },
  })

  function saveRiskLimits() {
    const trimmedAllowlist = allowlist.trim()
    riskMutation.mutate({
      max_total_notional: maxNotional,
      max_total_daily_loss: maxDailyLoss,
      symbol_blocklist: parseSymbolList(blocklist),
      symbol_allowlist: trimmedAllowlist.length === 0 ? null : parseSymbolList(allowlist),
    })
  }

  return (
    <div>
      <h1 className="mb-4 text-xl font-semibold text-gray-100">Settings</h1>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <section className="rounded border border-white/10 p-4">
          <h2 className="mb-3 text-sm font-semibold text-gray-200">Schwab Connection</h2>

          {isLoading && <p className="text-sm text-gray-500">Loading…</p>}
          {isError && <p className="text-sm text-red-400">Could not load auth status.</p>}

          {authStatus && (
            <dl className="mb-4 grid grid-cols-2 gap-y-1 text-sm">
              <dt className="text-gray-500">Connected</dt>
              <dd className={authStatus.connected ? 'text-emerald-400' : 'text-red-400'}>
                {authStatus.connected ? 'Yes' : 'No'}
              </dd>

              <dt className="text-gray-500">Client configured</dt>
              <dd className="text-gray-300">{authStatus.client_configured ? 'Yes' : 'No'}</dd>

              <dt className="text-gray-500">Access token expires in</dt>
              <dd className="text-gray-300">
                {authStatus.access_token_expires_in ?? '—'}
                {authStatus.access_token_expires_in != null ? 's' : ''}
              </dd>

              <dt className="text-gray-500">Refresh token expires in</dt>
              <dd className="text-gray-300">
                {authStatus.refresh_token_expires_in ?? '—'}
                {authStatus.refresh_token_expires_in != null ? 's' : ''}
              </dd>

              <dt className="text-gray-500">Refresh token expires at</dt>
              <dd className="text-gray-300">{authStatus.refresh_token_expires_at ?? '—'}</dd>

              <dt className="text-gray-500">Last refresh</dt>
              <dd className="text-gray-300">{authStatus.last_refresh_at ?? '—'}</dd>
            </dl>
          )}

          <button
            onClick={() => connectMutation.mutate()}
            disabled={connectMutation.isPending}
            className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {connectMutation.isPending ? 'Connecting…' : 'Connect Schwab'}
          </button>

          {connectMutation.isError && (
            <p className="mt-2 text-sm text-red-400">
              {connectMutation.error instanceof Error
                ? connectMutation.error.message
                : 'Failed to start connection.'}
            </p>
          )}
        </section>

        <section className="rounded border border-white/10 p-4">
          <h2 className="mb-1 text-sm font-semibold text-gray-200">Credentials</h2>
          <p className="mb-3 text-xs text-gray-500">
            Saved to the project's <code className="text-gray-400">.env</code> file — same place
            as if you'd edited it by hand. A field left blank keeps its current value.
          </p>

          {credentialsQuery.isLoading && <p className="text-sm text-gray-500">Loading…</p>}
          {credentialsQuery.isError && (
            <p className="text-sm text-red-400">Could not load credential status.</p>
          )}

          {credentialsQuery.data && (
            <div className="flex flex-col gap-3">
              {CREDENTIAL_FIELDS.map(({ field, label, secret, placeholder }) => {
                const configured = credentialsQuery.data.fields[field]?.configured ?? false
                const existingValue = credentialsQuery.data.fields[field]?.value ?? ''
                return (
                  <label key={field} className="flex flex-col gap-1 text-xs text-gray-400">
                    <span className="flex items-center gap-2">
                      {label}
                      <span
                        className={`rounded-full px-1.5 py-0.5 text-[10px] ${
                          configured
                            ? 'bg-emerald-500/10 text-emerald-400'
                            : 'bg-white/5 text-gray-500'
                        }`}
                      >
                        {configured ? 'configured' : 'not set'}
                      </span>
                    </span>
                    <input
                      type={secret ? 'password' : 'text'}
                      value={credentialInputs[field] ?? (secret ? '' : existingValue)}
                      onChange={(e) =>
                        setCredentialInputs((prev) => ({ ...prev, [field]: e.target.value }))
                      }
                      placeholder={secret && configured ? '•••••••• (unchanged)' : placeholder}
                      className={inputCls('w-full')}
                      autoComplete="off"
                    />
                  </label>
                )
              })}

              <div className="flex items-center gap-3">
                <button
                  onClick={saveCredentials}
                  disabled={credentialsMutation.isPending}
                  className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
                >
                  {credentialsMutation.isPending ? 'Saving…' : 'Save'}
                </button>
                {credentialMsg && <span className="text-sm text-emerald-400">{credentialMsg}</span>}
              </div>

              {credentialsMutation.isError && (
                <p className="text-sm text-red-400">
                  {credentialsMutation.error instanceof ApiError
                    ? credentialsMutation.error.message
                    : 'Failed to save credentials.'}
                </p>
              )}
            </div>
          )}
        </section>

        <section className="rounded border border-amber-500/20 bg-amber-500/5 p-4 xl:col-span-2">
          <div className="mb-1 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-200">Live trading</h2>
            {liveTradingQuery.data && (
              <button
                onClick={() => liveTradingMutation.mutate(!liveTradingQuery.data!.enabled)}
                disabled={liveTradingMutation.isPending}
                className={`flex h-6 w-11 items-center rounded-full border px-0.5 transition-colors disabled:opacity-50 ${
                  liveTradingQuery.data.enabled
                    ? 'border-red-500/40 bg-red-500/30'
                    : 'border-white/10 bg-white/5'
                }`}
                title={liveTradingQuery.data.enabled ? 'Disable live trading' : 'Enable live trading'}
                aria-label={liveTradingQuery.data.enabled ? 'Disable live trading' : 'Enable live trading'}
              >
                <span
                  className={`h-5 w-5 rounded-full bg-white transition-transform ${
                    liveTradingQuery.data.enabled ? 'translate-x-5' : 'translate-x-0'
                  }`}
                />
              </button>
            )}
          </div>
          <p className="mb-2 text-xs text-gray-500">
            Off by default. Turning this on doesn't place any orders by itself — a strategy still
            has to be promoted to live after paper trading, and the Live probe page (manual test
            orders) is gated behind this same switch. This is the one setting in the app that
            makes real order placement possible at all.
          </p>

          {liveTradingQuery.isLoading && <p className="text-sm text-gray-500">Loading…</p>}
          {liveTradingQuery.isError && (
            <p className="text-sm text-red-400">Could not load live-trading status.</p>
          )}
          {liveTradingQuery.data && (
            <p className={`text-sm ${liveTradingQuery.data.enabled ? 'text-red-400' : 'text-gray-400'}`}>
              {liveTradingQuery.data.enabled ? 'Enabled' : 'Disabled'}
            </p>
          )}
          {liveTradingMsg && <p className="mt-1 text-sm text-emerald-400">{liveTradingMsg}</p>}
          {liveTradingMutation.isError && (
            <p className="mt-1 text-sm text-red-400">
              {liveTradingMutation.error instanceof ApiError
                ? liveTradingMutation.error.message
                : 'Failed to save.'}
            </p>
          )}
        </section>

        <section className="rounded border border-white/10 p-4 xl:col-span-2">
          <h2 className="mb-3 text-sm font-semibold text-gray-200">Global risk limits</h2>

          {riskQuery.isLoading && <p className="text-sm text-gray-500">Loading…</p>}
          {riskQuery.isError && <p className="text-sm text-red-400">Could not load risk limits.</p>}

          {riskQuery.data && (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <label className="flex flex-col gap-1 text-xs text-gray-400">
                Max total notional
                <input
                  type="number"
                  value={maxNotional}
                  onChange={(e) => setMaxNotional(Number(e.target.value))}
                  className={inputCls('w-full')}
                />
              </label>

              <label className="flex flex-col gap-1 text-xs text-gray-400">
                Max total daily loss
                <input
                  type="number"
                  value={maxDailyLoss}
                  onChange={(e) => setMaxDailyLoss(Number(e.target.value))}
                  className={inputCls('w-full')}
                />
              </label>

              <label className="flex flex-col gap-1 text-xs text-gray-400">
                Symbol blocklist (comma-separated)
                <input
                  type="text"
                  value={blocklist}
                  onChange={(e) => setBlocklist(e.target.value)}
                  placeholder="e.g. GME, AMC"
                  className={inputCls('w-full')}
                />
              </label>

              <label className="flex flex-col gap-1 text-xs text-gray-400">
                Symbol allowlist (empty = all allowed)
                <input
                  type="text"
                  value={allowlist}
                  onChange={(e) => setAllowlist(e.target.value)}
                  placeholder="e.g. SPY, QQQ"
                  className={inputCls('w-full')}
                />
              </label>

              <div className="flex items-center gap-3 sm:col-span-2 lg:col-span-4">
                <button
                  onClick={saveRiskLimits}
                  disabled={riskMutation.isPending}
                  className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
                >
                  {riskMutation.isPending ? 'Saving…' : 'Save'}
                </button>
                {saveMsg && <span className="text-sm text-emerald-400">{saveMsg}</span>}
              </div>

              {riskMutation.isError && (
                <p className="text-sm text-red-400 sm:col-span-2 lg:col-span-4">
                  {riskMutation.error instanceof ApiError
                    ? riskMutation.error.message
                    : 'Failed to save risk limits.'}
                </p>
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
