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
  const current = now instanceof Date ? now : new Date()
  const morning = new Date(current)
  morning.setHours(8, 0, 0, 0)
  const start = new Date(current)
  start.setHours(18, 0, 0, 0)
  start.setDate(start.getDate() - 1)
  const end = new Date(morning)
  if (current < morning) {
    return { start, end }
  }
  return { start, end }
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
      if (inp != null || out != null) tokens = Math.trunc((inp || 0) + (out || 0))
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
  run.id = String(raw.execution_id || raw.id || raw.fire_id || raw.session_id || `${jobId}:${run.started_at || ''}`)
  return run
}

const MERGE_WINDOW_MS = 120 * 1000

function mergeRuns(list) {
  const merged = []
  for (const raw of list) {
    const run = toRun(raw)
    const started = parseTime(run.started_at)
    let partner = null
    let partnerDelta = MERGE_WINDOW_MS + 1
    for (const other of merged) {
      if (other.job_id !== run.job_id) continue
      const otherStarted = parseTime(other.started_at)
      if (!started && !otherStarted) {
        partner = other
        partnerDelta = 0
        break
      }
      if (!started || !otherStarted) continue
      const delta = Math.abs(started.getTime() - otherStarted.getTime())
      if (delta <= MERGE_WINDOW_MS && delta < partnerDelta) {
        partner = other
        partnerDelta = delta
      }
    }
    if (partner) {
      for (const field of ['id', 'job_id', 'name', 'schedule', 'started_at', 'finished_at', 'session_id', 'profile']) {
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
  let tokenSum = 0
  let tokenAny = false
  let usdSum = 0
  let usdAny = false
  for (const run of runs) {
    if (run.status === 'failed' || run.status === 'unknown' || (!run.status && run.error)) failed += 1
    if (typeof run.tokens === 'number') {
      tokenSum += run.tokens
      tokenAny = true
    }
    if (typeof run.usd === 'number' && Number.isFinite(run.usd)) {
      usdSum += run.usd
      usdAny = true
    }
  }
  return {
    runs: runs.length,
    failed,
    tokens: tokenAny ? tokenSum : null,
    usd: usdAny ? usdSum : null,
  }
}

function filterWindow(runs, start, end) {
  return runs
    .filter((run) => inWindow(parseTime(run.started_at), start, end) || inWindow(parseTime(run.finished_at), start, end))
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

async function loadFromHost() {
  const win = overnightWindow(new Date())
  let jobs = []
  let sessions = []
  try {
    jobs = jobsFromRpc(await host.request('cron.manage', { action: 'list' }))
  } catch {
    jobs = []
  }
  try {
    sessions = sessionsFromRpc(await host.request('session.list', { include_hidden: true, limit: 200 }))
  } catch {
    sessions = []
  }
  const cronSessions = sessions.filter((s) => {
    const src = String(s.source || '').toLowerCase()
    const id = String(s.id || '')
    return src === 'cron' || id.startsWith('cron_')
  })
  const byId = new Map(jobs.map((j) => [String(j.id || ''), j]))
  const raw = []
  for (const sess of cronSessions) {
    const jobId = cronSessionJobId(sess)
    const job = byId.get(jobId) || {}
    const ended = sess.ended_at != null
    raw.push({
      ...sess,
      job_id: jobId || sess.id,
      name: job.name || sess.title || jobId || sess.id,
      schedule: scheduleOf(job),
      started_at: sess.started_at,
      finished_at: sess.ended_at,
      status: ended ? (String(sess.end_reason || '').toLowerCase().includes('fail') || String(sess.end_reason || '').toLowerCase() === 'error' ? 'failed' : 'completed') : 'running',
      session_id: sess.id,
    })
  }
  for (const job of jobs) {
    if (!job.last_run_at) continue
    raw.push({
      id: `last:${job.id}:${job.last_run_at}`,
      job_id: job.id,
      name: job.name || job.id,
      schedule: scheduleOf(job),
      last_run_at: job.last_run_at,
      started_at: job.last_run_at,
      status: job.last_status,
      error: job.last_error || job.last_delivery_error,
    })
  }
  const runs = filterWindow(mergeRuns(raw), win.start, win.end)
  return {
    ok: true,
    window: { start: win.start.toISOString(), end: win.end.toISOString(), tz: Intl.DateTimeFormat().resolvedOptions().timeZone, label: 'Last night' },
    summary: summarize(runs),
    runs,
    source: 'rpc',
    degraded: true,
  }
}

async function fetchNight(ctx) {
  let rpc = null
  let rpcErr = null
  try {
    rpc = await loadFromHost()
  } catch (err) {
    rpcErr = err
  }
  try {
    const rest = await ctx.rest('/night')
    if (rest && rest.ok !== false && Array.isArray(rest.runs)) {
      return rest
    }
  } catch (restErr) {
    if (rpc && Array.isArray(rpc.runs)) return rpc
    throw restErr
  }
  if (rpc && Array.isArray(rpc.runs)) return rpc
  if (rpcErr) throw rpcErr
  return { ok: false, error: 'empty', runs: [], summary: { runs: 0, failed: 0, tokens: null, usd: null } }
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

function SummaryLine({ summary }) {
  const parts = [
    `${summary.runs} run${summary.runs === 1 ? '' : 's'}`,
    `${summary.failed} failed`,
  ]
  if (summary.usd != null) parts.push(fmtUsd(summary.usd))
  else if (summary.tokens != null) parts.push(fmtTokens(summary.tokens))
  else parts.push('cost unknown')
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
  const summary = (data && data.summary) || { runs: 0, failed: 0, tokens: null, usd: null }
  const windowInfo = data && data.window
  const visible = filter === 'failed'
    ? runs.filter((r) => r.status === 'failed' || r.status === 'unknown' || r.error)
    : runs

  if (isLoading) {
    return jsxs('div', {
      className: 'flex h-full flex-col items-center justify-center gap-3',
      children: [
        jsx(GlyphSpinner, { size: 24 }),
        jsx('div', { className: 'text-sm text-(--ui-text-secondary)', children: 'Loading last night…' }),
      ],
    })
  }

  if (error && !data) {
    return jsxs('div', {
      className: 'flex h-full flex-col items-center justify-center gap-3 p-8',
      children: [
        jsx(ErrorState, {
          title: 'Backend not reachable',
          description:
            'Enable Cron Night in Settings → Plugins, then quit Hermes Desktop and relaunch from the menu. Reload desktop plugins is JS only. Gateway cron/session RPCs were also unavailable.',
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
            title: filter === 'failed' ? 'No failed runs' : 'Nothing ran last night',
            description:
              filter === 'failed'
                ? 'Last night’s window has no failed or unknown attempts.'
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
  if (!failed) return null
  return jsx(FailChip, { failed })
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
