/**
 * SMF Cron Night — last night's cron runs, failures, and recorded cost.
 * Disk plugin: jsx/jsxs only. No invented USD.
 */
import {
  Badge,
  Button,
  Codicon,
  EmptyState,
  ErrorState,
  GlyphSpinner,
  ScrollArea,
  SegmentedControl,
  Separator,
  StatusDot,
  cn,
  fmtDateTime,
  haptic,
  host,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  atom,
  relativeTime,
  useQuery,
  useValue,
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'smf-cron-night'
const ROUTE = '/cron-night'
const POLL_MS = 8000
const $filter = atom('all')

function overnightWindow(now) {
  // Same bounds as Python overnight_window: after local 18:00 the night that
  // just started (today 18:00 → tomorrow 08:00); before 18:00 yesterday 18:00 → today 08:00.
  const current = now instanceof Date ? now : new Date()
  const evening = new Date(current)
  evening.setHours(18, 0, 0, 0)
  const morning = new Date(current)
  morning.setHours(8, 0, 0, 0)
  if (current >= evening) {
    const end = new Date(morning)
    end.setDate(end.getDate() + 1)
    return { start: evening, end }
  }
  const start = new Date(evening)
  start.setDate(start.getDate() - 1)
  return { start, end: morning }
}

function desktopTz() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || ''
  } catch {
    return ''
  }
}

function emptySummary() {
  return { runs: 0, failed: 0, running: 0, tokens: null, usd: null, usd_billed: 0, cost_coverage: 'none' }
}

function sessionStatus(sess) {
  const ended = sess.ended_at != null && sess.ended_at !== ''
  if (!ended) return 'running'
  const mapped = normalizeStatus(sess.end_reason)
  if (mapped === 'failed') return 'failed'
  if (mapped === 'completed') return 'completed'
  return 'unknown'
}

function parseTime(value) {
  if (value == null || value === '') return null
  if (typeof value === 'number' && Number.isFinite(value)) {
    const ms = value > 1e12 ? value : value * 1000
    const d = new Date(ms)
    return Number.isNaN(d.getTime()) ? null : d
  }
  const text = String(value).trim()
  if (!text) return null
  const file = /^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})$/.exec(text)
  const iso = file ? `${file[1]}T${file[2].replace(/-/g, ':')}` : text
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

function inWindow(dt, start, end) {
  if (!dt) return false
  return dt >= start && dt < end
}

function normalizeStatus(value) {
  if (value == null || value === '') return null
  const raw = String(value).trim().toLowerCase().replace(/\s+/g, '_')
  if (['completed', 'ok', 'success', 'succeeded', 'ok_silent'].includes(raw)) return 'completed'
  if (['failed', 'fail', 'error', 'delivery_failed', 'delivery-failed'].includes(raw)) return 'failed'
  if (raw === 'running') return 'running'
  if (raw === 'claimed') return 'claimed'
  if (raw === 'unknown') return 'unknown'
  return null
}

function finiteNumber(value) {
  if (value == null || value === true || value === false) return null
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string') {
    const n = Number(value.replace(/[$,]/g, '').trim())
    return Number.isFinite(n) ? n : null
  }
  return null
}

function normalizeCost(payload) {
  if (!payload || typeof payload !== 'object') return { tokens: null, usd: null }
  let tokens = finiteNumber(payload.total_tokens ?? payload.tokens ?? payload.total)
  if (tokens != null) tokens = Math.trunc(tokens)
  if (tokens == null) {
    const prompt = finiteNumber(payload.prompt_tokens)
    const completion = finiteNumber(payload.completion_tokens)
    if (prompt != null || completion != null) tokens = Math.trunc((prompt || 0) + (completion || 0))
    else {
      const inp = finiteNumber(payload.input_tokens)
      const out = finiteNumber(payload.output_tokens)
      const cacheR = finiteNumber(payload.cache_read_tokens)
      const cacheW = finiteNumber(payload.cache_write_tokens)
      if (inp != null || out != null || cacheR != null || cacheW != null) {
        tokens = Math.trunc((inp || 0) + (out || 0) + (cacheR || 0) + (cacheW || 0))
      }
    }
  }
  const status = String(payload.cost_status || payload.costStatus || '').toLowerCase()
  let usd = null
  if (status !== 'unknown' && status !== 'n/a' && status !== 'na' && status !== 'included') {
    usd = finiteNumber(
      payload.actual_cost_usd ?? payload.amount_usd ?? payload.cost_usd ?? payload.usd ?? payload.estimated_cost_usd
    )
  }
  return { tokens, usd }
}

function errorSnippet(value) {
  if (value == null) return null
  let text = ''
  if (typeof value === 'string') text = value
  else if (typeof value === 'object') text = value.message || value.error || value.detail || ''
  text = String(text).trim().replace(/\s+/g, ' ')
  if (!text) return null
  return text.length > 240 ? text.slice(0, 239) + '…' : text
}

function scheduleOf(job) {
  if (!job) return ''
  if (typeof job.schedule_display === 'string' && job.schedule_display.trim()) return job.schedule_display.trim()
  const sched = job.schedule
  if (typeof sched === 'string') return sched
  if (sched && typeof sched === 'object') return sched.display || sched.expr || sched.kind || ''
  return ''
}

function jobsFromRpc(payload) {
  if (!payload) return []
  if (Array.isArray(payload)) return payload.filter((j) => j && typeof j === 'object')
  if (Array.isArray(payload.jobs)) return payload.jobs.filter((j) => j && typeof j === 'object')
  if (Array.isArray(payload.result)) return payload.result.filter((j) => j && typeof j === 'object')
  if (payload.result && Array.isArray(payload.result.jobs)) return payload.result.jobs
  return []
}

function sessionsFromRpc(payload) {
  if (!payload) return []
  if (Array.isArray(payload)) return payload
  if (Array.isArray(payload.sessions)) return payload.sessions
  if (payload.result && Array.isArray(payload.result.sessions)) return payload.result.sessions
  return []
}

function preferStatus(current, incoming) {
  const rank = { failed: 50, unknown: 40, running: 30, claimed: 20, completed: 10 }
  if (!incoming) return current || null
  if (!current) return incoming
  return (rank[incoming] || 0) >= (rank[current] || 0) ? incoming : current
}

function emptyRun() {
  return {
    id: '',
    job_id: '',
    name: '',
    schedule: '',
    started_at: null,
    finished_at: null,
    status: null,
    tokens: null,
    usd: null,
    error: null,
    session_id: null,
    profile: '',
    home: '',
  }
}

function toRun(raw) {
  const run = emptyRun()
  const jobId = String(raw.job_id || raw.id || '').trim()
  run.job_id = jobId
  run.name = String(raw.name || raw.job_name || raw.title || jobId)
  run.schedule = String(raw.schedule || raw.schedule_display || scheduleOf(raw) || '')
  const started = parseTime(raw.started_at || raw.claimed_at || raw.start_time || raw.ts || raw.last_run_at)
  const finished = parseTime(raw.finished_at || raw.end_time || raw.ended_at)
  run.started_at = started ? started.toISOString() : null
  run.finished_at = finished ? finished.toISOString() : null
  let status = normalizeStatus(raw.status || raw.last_status)
  if (!status && raw.error) status = 'failed'
  run.status = status
  const cost = normalizeCost(raw)
  run.tokens = cost.tokens
  run.usd = cost.usd
  run.error = errorSnippet(raw.error || raw.last_error || raw.last_delivery_error)
  run.session_id = raw.session_id || raw.sessionId || (String(raw.id || '').startsWith('cron_') ? raw.id : null)
  run.profile = String(raw.profile || '')
  run.home = String(raw.home || '')
  run.id = String(raw.execution_id || raw.id || raw.fire_id || raw.session_id || `${jobId}:${run.started_at || ''}`)
  return run
}

const MERGE_WINDOW_MS = 120 * 1000

function runTimes(run) {
  const times = []
  const started = parseTime(run.started_at)
  const finished = parseTime(run.finished_at)
  if (started) times.push(started)
  if (finished) times.push(finished)
  return times
}

function mergeRuns(list) {
  const merged = []
  for (const raw of list) {
    const run = toRun(raw)
    const times = runTimes(run)
    let partner = null
    let partnerDelta = MERGE_WINDOW_MS + 1
    for (const other of merged) {
      if (other.job_id !== run.job_id) continue
      const otherTimes = runTimes(other)
      if (!times.length && !otherTimes.length) {
        partner = other
        partnerDelta = 0
        break
      }
      for (const left of times) {
        for (const right of otherTimes) {
          const delta = Math.abs(left.getTime() - right.getTime())
          if (delta <= MERGE_WINDOW_MS && delta < partnerDelta) {
            partner = other
            partnerDelta = delta
          }
        }
      }
    }
    if (partner) {
      for (const field of ['id', 'job_id', 'name', 'schedule', 'started_at', 'finished_at', 'session_id', 'profile', 'home']) {
        if (!partner[field] && run[field]) partner[field] = run[field]
      }
      partner.status = preferStatus(partner.status, run.status)
      if (run.error && !partner.error) partner.error = run.error
      if (partner.tokens == null && run.tokens != null) partner.tokens = run.tokens
      if (partner.usd == null && run.usd != null) partner.usd = run.usd
    } else {
      merged.push(run)
    }
  }
  return merged
}

function summarize(runs) {
  let failed = 0
  let running = 0
  let tokenSum = 0
  let tokenAny = false
  let usdSum = 0
  let usdBilled = 0
  for (const run of runs) {
    if (run.status === 'failed' || run.status === 'unknown' || (!run.status && run.error)) failed += 1
    if (run.status === 'running' || run.status === 'claimed') running += 1
    if (typeof run.tokens === 'number') {
      tokenSum += run.tokens
      tokenAny = true
    }
    if (typeof run.usd === 'number' && Number.isFinite(run.usd)) {
      usdSum += run.usd
      usdBilled += 1
    }
  }
  const n = runs.length
  let coverage = 'none'
  let usd = null
  if (usdBilled === 0) {
    coverage = 'none'
    usd = null
  } else if (usdBilled === n) {
    coverage = 'complete'
    usd = usdSum
  } else {
    coverage = 'partial'
    usd = null
  }
  return {
    runs: n,
    failed,
    running,
    tokens: tokenAny ? tokenSum : null,
    usd,
    usd_billed: usdBilled,
    cost_coverage: coverage,
  }
}

function hungIntoWindow(run, start, end) {
  if (run.status !== 'running' && run.status !== 'claimed') return false
  const finished = parseTime(run.finished_at)
  if (finished) return false
  const started = parseTime(run.started_at)
  if (!started) return true
  return started < end
}

function filterWindow(runs, start, end) {
  return runs
    .filter((run) => {
      if (inWindow(parseTime(run.started_at), start, end) || inWindow(parseTime(run.finished_at), start, end)) {
        return true
      }
      return hungIntoWindow(run, start, end)
    })
    .sort((a, b) => {
      const ta = parseTime(a.started_at)
      const tb = parseTime(b.started_at)
      return (tb ? tb.getTime() : 0) - (ta ? ta.getTime() : 0)
    })
}

function cronSessionJobId(session) {
  const id = String(session.id || session.session_id || '')
  const m = /^cron_([A-Za-z0-9]+)_(.+)$/.exec(id)
  return m ? m[1] : ''
}

function windowLabel(now) {
  const current = now instanceof Date ? now : new Date()
  const evening = new Date(current)
  evening.setHours(18, 0, 0, 0)
  return current >= evening ? 'Tonight' : 'Last night'
}

function recordRpcError(errors, kind, err) {
  errors.push({
    kind,
    path: null,
    error: err && err.message ? String(err.message) : String(err || 'request failed'),
  })
}

async function loadFromHost() {
  const now = new Date()
  const win = overnightWindow(now)
  const errors = []
  let jobs = []
  let sessions = []
  try {
    jobs = jobsFromRpc(await host.request('cron.manage', { action: 'list' }))
  } catch (err) {
    jobs = []
    recordRpcError(errors, 'cron.manage', err)
  }
  try {
    sessions = sessionsFromRpc(await host.request('session.list', { include_hidden: true, limit: 200 }))
  } catch (err) {
    sessions = []
    recordRpcError(errors, 'session.list', err)
  }
  const cronSessions = sessions.filter((s) => {
    const src = String(s.source || '').toLowerCase()
    const id = String(s.id || '')
    return src === 'cron' || id.startsWith('cron_')
  })
  const byId = new Map(jobs.map((j) => [String(j.id || ''), j]))
  const raw = []
  const jobsWithHistory = new Set()
  for (const sess of cronSessions) {
    const jobId = cronSessionJobId(sess)
    const job = byId.get(jobId) || {}
    if (jobId) jobsWithHistory.add(jobId)
    raw.push({
      ...sess,
      job_id: jobId || sess.id,
      name: job.name || sess.title || jobId || sess.id,
      schedule: scheduleOf(job),
      started_at: sess.started_at,
      finished_at: sess.ended_at,
      status: sessionStatus(sess),
      session_id: sess.id,
      profile: 'gateway',
      home: 'gateway',
    })
  }
  for (const job of jobs) {
    if (!job.last_run_at) continue
    const jobId = String(job.id || '')
    if (jobId && jobsWithHistory.has(jobId)) continue
    raw.push({
      id: `last:${job.id}:${job.last_run_at}`,
      job_id: job.id,
      name: job.name || job.id,
      schedule: scheduleOf(job),
      last_run_at: job.last_run_at,
      started_at: job.last_run_at,
      status: job.last_status,
      error: job.last_error || job.last_delivery_error,
      profile: 'gateway',
      home: 'gateway',
    })
  }
  const runs = filterWindow(mergeRuns(raw), win.start, win.end)
  const unread = errors.length > 0 && runs.length === 0
  const partial = errors.length > 0 && runs.length > 0
  return {
    ok: errors.length === 0,
    read_status: unread ? 'unread' : partial ? 'partial' : 'ok',
    window: {
      start: win.start.toISOString(),
      end: win.end.toISOString(),
      tz: desktopTz(),
      label: windowLabel(now),
    },
    summary: summarize(runs),
    runs,
    homes: ['gateway'],
    errors,
    source: 'rpc',
    degraded: true,
  }
}

function isUnreadPayload(payload) {
  if (!payload) return true
  if (payload.read_status === 'unread') return true
  if (payload.ok === false && (!payload.runs || payload.runs.length === 0)) return true
  return false
}

function hasReadProblems(payload) {
  if (!payload) return false
  if (payload.ok === false) return true
  if (payload.read_status === 'unread' || payload.read_status === 'partial') return true
  return Array.isArray(payload.errors) && payload.errors.length > 0
}

async function fetchNight(ctx) {
  let rpc = null
  let rpcErr = null
  try {
    rpc = await loadFromHost()
  } catch (err) {
    rpcErr = err
  }
  const tz = desktopTz()
  let rest = null
  let restErr = null
  try {
    const qs = tz ? `?tz=${encodeURIComponent(tz)}` : ''
    rest = await ctx.rest(`/night${qs}`)
  } catch (err) {
    restErr = err
  }

  const restRuns = rest && Array.isArray(rest.runs) ? rest.runs : null
  const rpcRuns = rpc && Array.isArray(rpc.runs) ? rpc.runs : null
  const restHasJobs = restRuns && restRuns.length > 0
  const rpcHasJobs = rpcRuns && rpcRuns.length > 0

  if (restRuns && !isUnreadPayload(rest)) {
    return rest
  }
  // Unread/empty REST must not hide RPC that listed real jobs.
  if (restRuns && isUnreadPayload(rest) && !restHasJobs && rpcHasJobs) {
    return {
      ...rpc,
      errors: [...(rest.errors || []), ...(rpc.errors || [])],
    }
  }
  if (restRuns) return rest
  if (rpcRuns) return rpc
  if (restErr) throw restErr
  if (rpcErr) throw rpcErr
  return {
    ok: false,
    error: 'empty',
    read_status: 'unread',
    runs: [],
    errors: [{ kind: 'night', path: null, error: 'empty' }],
    summary: emptySummary(),
  }
}

function fmtUsd(value) {
  if (value == null || typeof value !== 'number' || !Number.isFinite(value)) return '—'
  if (Math.abs(value) < 0.01 && value !== 0) return `$${value.toFixed(4)}`
  return `$${value.toFixed(2)}`
}

function fmtTokens(value) {
  if (value == null || typeof value !== 'number') return '—'
  return `${value.toLocaleString()} tok`
}

function statusMeta(status) {
  if (status === 'failed') return { label: 'failed', tone: 'bad' }
  if (status === 'unknown') return { label: 'unknown', tone: 'warn' }
  if (status === 'running' || status === 'claimed') return { label: status, tone: 'muted' }
  if (status === 'completed') return { label: 'ok', tone: 'good' }
  return { label: status || 'n/a', tone: 'muted' }
}

function fmtWhen(iso) {
  const dt = parseTime(iso)
  if (!dt) return '—'
  try {
    if (typeof fmtDateTime === 'function') return fmtDateTime(dt)
  } catch {
    /* fall through */
  }
  try {
    if (typeof relativeTime === 'function') return relativeTime(dt)
  } catch {
    /* fall through */
  }
  return dt.toLocaleString()
}

function problemRun(run) {
  return (
    run.status === 'failed' ||
    run.status === 'unknown' ||
    run.status === 'running' ||
    run.status === 'claimed' ||
    Boolean(run.error)
  )
}

function FailChip({ failed }) {
  if (!failed) return null
  return jsx('button', {
    type: 'button',
    className: 'px-1.5 text-[0.6875rem] text-(--ui-text-secondary)',
    onClick: () => {
      haptic('tap')
      host.navigate(ROUTE)
    },
    children: jsxs('span', {
      className: 'inline-flex items-center gap-1',
      children: [
        jsx(StatusDot, { tone: 'bad' }),
        `${failed} fail`,
      ],
    }),
  })
}

function RunningChip({ running }) {
  if (!running) return null
  return jsx('button', {
    type: 'button',
    className: 'px-1.5 text-[0.6875rem] text-(--ui-text-secondary)',
    onClick: () => {
      haptic('tap')
      host.navigate(ROUTE)
    },
    children: jsxs('span', {
      className: 'inline-flex items-center gap-1',
      children: [
        jsx(StatusDot, { tone: 'muted' }),
        `${running} running`,
      ],
    }),
  })
}

function costSummaryText(summary) {
  const n = summary.runs || 0
  const billed = typeof summary.usd_billed === 'number' ? summary.usd_billed : null
  const coverage = summary.cost_coverage
  if (coverage === 'partial' || (billed != null && n > 0 && billed > 0 && billed < n)) {
    return `partial (${billed}/${n} billed)`
  }
  if (summary.usd != null) return fmtUsd(summary.usd)
  if (summary.tokens != null) return fmtTokens(summary.tokens)
  return 'cost unknown'
}

function SummaryLine({ summary }) {
  const parts = [
    `${summary.runs} run${summary.runs === 1 ? '' : 's'}`,
    `${summary.failed} failed`,
  ]
  if (summary.running) parts.push(`${summary.running} running`)
  parts.push(costSummaryText(summary))
  return jsx('div', {
    className: 'text-xs text-(--ui-text-tertiary)',
    children: parts.join(' · '),
  })
}

function RunRow({ run }) {
  const meta = statusMeta(run.status)
  const cost =
    run.usd != null ? fmtUsd(run.usd) : run.tokens != null ? fmtTokens(run.tokens) : '—'
  return jsxs('div', {
    className: cn(
      'flex flex-col gap-1 rounded-lg border border-(--ui-stroke-secondary)',
      'px-3 py-2'
    ),
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(StatusDot, { tone: meta.tone }),
          jsx('div', {
            className: 'min-w-0 flex-1 truncate text-sm font-medium',
            children: run.name || run.job_id || 'cron',
          }),
          run.profile
            ? jsx(Badge, { className: 'shrink-0 text-[0.625rem]', children: run.profile })
            : null,
          jsx(Badge, { className: 'shrink-0 text-[0.625rem]', children: meta.label }),
        ],
      }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-(--ui-text-secondary)',
        children: [
          run.schedule
            ? jsx('span', { className: 'font-mono text-(--ui-text-tertiary)', children: run.schedule })
            : null,
          jsx('span', { children: fmtWhen(run.started_at) }),
          run.finished_at
            ? jsx('span', { className: 'text-(--ui-text-tertiary)', children: '→ ' + fmtWhen(run.finished_at) })
            : null,
          jsx('span', { className: 'ml-auto tabular-nums', children: cost }),
        ],
      }),
      run.error
        ? jsx('div', {
            className: 'line-clamp-2 font-mono text-[0.6875rem] text-(--ui-text-secondary)',
            children: run.error,
          })
        : null,
    ],
  })
}

function formatReadErrors(data) {
  const errs = (data && data.errors) || []
  const lines = errs
    .map((e) => {
      if (!e) return ''
      if (e.path) return `${e.path}: ${e.error || e.kind || 'read failed'}`
      if (e.kind && e.error) return `${e.kind}: ${e.error}`
      return e.error || e.kind || ''
    })
    .filter(Boolean)
  if (data && data.error && !lines.length) lines.push(String(data.error))
  if (!lines.length) {
    return 'Could not read local cron storage. This is not a quiet night — the ledger was unread.'
  }
  return lines.slice(0, 4).join(' · ')
}

function CronNightPage({ ctx }) {
  const filter = useValue($filter)
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: [ID, 'night'],
    queryFn: () => fetchNight(ctx),
    refetchInterval: POLL_MS,
    staleTime: POLL_MS,
    retry: 1,
  })
  const runs = (data && data.runs) || []
  const summary = (data && data.summary) || emptySummary()
  const windowInfo = data && data.window
  const homes = (data && Array.isArray(data.homes) ? data.homes : []).filter(Boolean)
  const visible = filter === 'failed'
    ? runs.filter(problemRun)
    : runs
  const readProblems = hasReadProblems(data)
  const unread = Boolean(data) && isUnreadPayload(data) && runs.length === 0

  if (isLoading) {
    return jsxs('div', {
      className: 'flex h-full flex-col items-center justify-center gap-3',
      children: [
        jsx(GlyphSpinner, { size: 24 }),
        jsx('div', { className: 'text-sm text-(--ui-text-secondary)', children: 'Loading last night…' }),
      ],
    })
  }

  if ((error && !data) || unread || (readProblems && runs.length === 0)) {
    return jsxs('div', {
      className: 'flex h-full flex-col items-center justify-center gap-3 p-8',
      children: [
        jsx(ErrorState, {
          title: error && !data ? 'Backend not reachable' : 'Could not read last night',
          description:
            error && !data
              ? 'Enable Cron Night in Settings → Plugins, then quit Hermes Desktop and relaunch from the menu. Reload desktop plugins is JS only. Gateway cron/session RPCs were also unavailable.'
              : formatReadErrors(data),
        }),
        jsx(Button, { variant: 'ghost', size: 'sm', onClick: () => refetch(), children: 'Retry' }),
      ],
    })
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-3 p-4',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Codicon, { name: 'history', size: 16 }),
          jsx('div', { className: 'text-sm font-medium', children: 'Cron Night' }),
          data && data.degraded
            ? jsx(Badge, { className: 'text-[0.625rem]', children: 'RPC fallback' })
            : null,
          isFetching
            ? jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: 'updating' })
            : null,
        ],
      }),
      readProblems
        ? jsx('div', {
            className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-2 text-xs text-(--ui-text-secondary)',
            children: formatReadErrors(data),
          })
        : null,
      windowInfo
        ? jsx('div', {
            className: 'text-xs text-(--ui-text-tertiary)',
            children:
              (windowInfo.label || 'Last night') +
              ' · ' +
              fmtWhen(windowInfo.start) +
              ' → ' +
              fmtWhen(windowInfo.end) +
              (windowInfo.tz ? ' · ' + windowInfo.tz : ''),
          })
        : null,
      homes.length
        ? jsx('div', {
            className: 'text-xs text-(--ui-text-tertiary)',
            children:
              (data && data.source === 'rpc'
                ? 'RPC · connected gateway only · '
                : homes.length > 1
                  ? 'Union of homes · '
                  : 'Home · ') + homes.join(' · '),
          })
        : null,
      jsx(SummaryLine, { summary }),
      jsx(SegmentedControl, {
        value: filter,
        onChange: (v) => $filter.set(v),
        options: [
          { id: 'all', label: 'All' },
          { id: 'failed', label: 'Failed' },
        ],
      }),
      jsx(Separator, {}),
      visible.length === 0
        ? jsx(EmptyState, {
            title: filter === 'failed' ? 'No failed or hung runs' : 'Nothing ran last night',
            description:
              filter === 'failed'
                ? 'Last night’s window has no failed, unknown, or still-running attempts.'
                : 'No cron attempts in the overnight window. Jobs still scheduled will show up after they fire.',
          })
        : jsx(ScrollArea, {
            className: 'min-h-0 flex-1',
            children: jsx('div', {
              className: 'flex flex-col gap-2 pb-4',
              children: visible.map((run) => jsx(RunRow, { run }, run.id || run.job_id + run.started_at)),
            }),
          }),
    ],
  })
}

function FailChipHost({ ctx }) {
  const { data } = useQuery({
    queryKey: [ID, 'night'],
    queryFn: () => fetchNight(ctx),
    refetchInterval: POLL_MS,
    staleTime: POLL_MS,
    retry: 0,
  })
  const failed = (data && data.summary && data.summary.failed) || 0
  const running = (data && data.summary && data.summary.running) || 0
  if (!failed && !running) return null
  return jsxs('span', {
    className: 'inline-flex items-center',
    children: [
      jsx(FailChip, { failed }),
      jsx(RunningChip, { running }),
    ],
  })
}

export default {
  id: ID,
  name: 'Cron Night',
  defaultEnabled: true,
  register(ctx) {
    ctx.registerMany([
      {
        id: `${ID}-nav`,
        area: SIDEBAR_NAV_AREA,
        data: { path: ROUTE, label: 'Cron Night', codicon: 'history' },
      },
      {
        id: `${ID}-route`,
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => jsx(CronNightPage, { ctx }),
      },
      {
        id: `${ID}-palette`,
        area: PALETTE_AREA,
        data: {
          id: `${ID}-open`,
          label: 'Open Cron Night',
          keywords: ['cron', 'night', 'failed', 'cost', 'overnight'],
          run: () => host.navigate(ROUTE),
        },
      },
      {
        id: `${ID}-fail-chip`,
        area: STATUSBAR_AREAS.right,
        order: 140,
        render: () => jsx(FailChipHost, { ctx }),
      },
    ])
  },
}
