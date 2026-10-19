// diaktoros: the Hermes Desktop half (#369, #630, #371). A Diaktoros page, a status-bar summary,
// a drawer showing what would move one PR, and the installation's own preflight — all fed by
// this plugin's own backend (dashboard/plugin_api.py) through ctx.rest. Reads never mutate state.
// Writes require confirmation: Retry re-arms an eligible run; Pause/Resume changes webhook
// delivery only. Each shows a verified host result, never an optimistic state.
// Plain ESM, loaded uncompiled: jsx() calls, not JSX syntax, and only the three specifiers the
// loader allows. Theme variables only, never a hardcoded color.
import {
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  host,
  queryClient,
  useQuery
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'diaktoros'
const PATH = '/diaktoros'
const SINCE = ['24h', '7d', '30d']
const SEATS = ['reviewer', 'fixer', 'issue_fixer', 'triage', 'adjudicator']
// Where the chip remembers each loop's last announced "needs you" count, keyed by loop id, so a
// rise in any loop toasts once and the first read after a reload only records the baseline.
const NEEDS_YOU_PREFIX = 'needsYou:'
// The health view is the slow one (it stats profiles, the cron store and the venvs), so it is
// read once and kept, not polled like the run views.
const HEALTH_STALE_MS = 600000

// Set in register(), cleared on dispose: components reach the backend through them.
let rest = null
let storage = null
// The status chip reports the loop it highlights and the page must open that loop, but the SDK
// hands a route's render no location. Both halves live in this one file and share the app's query
// cache, so the chip writes the loop here and the page observes it: the handoff works when the
// page mounts afterwards and when it is already open, and it stays inside the SDK surface.
const FOCUS_KEY = [ID, 'focus']

function useFocus() {
  // Client-side only: nothing to fetch, the chip writes it. Observed so an open page follows.
  return useQuery({ queryKey: FOCUS_KEY, queryFn: () => ({ loop: '' }), staleTime: Infinity,
    refetchOnMount: false, refetchOnWindowFocus: false })
}

function call(path, options) {
  if (!rest) return Promise.reject(new Error('plugin not loaded'))
  return rest(path, options)
}

function useLoops() {
  return useQuery({ queryKey: [ID, 'loops'], queryFn: async () => {
    const data = await call('/loops')
    if (!Array.isArray(data?.loops) || data.loops.some(loop => !loop || typeof loop.id !== 'string' || !loop.id ||
      (loop.unreadable !== undefined && typeof loop.unreadable !== 'boolean'))) throw invalidResponse('loops')
    return data
  }, staleTime: 60000 })
}

function useNow(loop) {
  return useQuery({
    queryKey: [ID, 'now', loop || ''],
    queryFn: () => readView('/now?loop=' + encodeURIComponent(loop), 'now'),
    enabled: !!loop,
    refetchInterval: 30000
  })
}

// One read for every configured loop: the chip and the cross-loop block both answer "what is
// waiting on me" from it, so a chip never fetches once per loop.
function useOverview() {
  return useQuery({
    queryKey: [ID, 'overview'],
    queryFn: () => readView('/overview', 'overview'),
    staleTime: 30000,
    refetchInterval: 30000
  })
}

// Reject malformed successful responses rather than manufacturing a verified zero.
function invalidResponse(kind) {
  const error = new Error('Invalid ' + kind + ' response')
  error.code = 'INVALID_RESPONSE'
  return error
}

function safeError(error) {
  return error?.code === 'INVALID_RESPONSE'
    ? 'Backend response could not be verified. Retry the read.'
    : 'Backend read failed. Check the connection and retry.'
}

async function readView(path, kind) {
  const data = await call(path)
  const object = value => value && typeof value === 'object' && !Array.isArray(value)
  const count = value => Number.isSafeInteger(value) && value >= 0
  const counts = value => object(value) && Object.values(value).every(count)
  const text = value => typeof value === 'string' && value.length > 0
  const nonnegative = value => typeof value === 'number' && Number.isFinite(value) && value >= 0
  const durationStats = value => value == null || (object(value) &&
    nonnegative(value.median) && nonnegative(value.max) && Object.values(value).every(nonnegative))
  const pacing = value => value == null || (object(value) &&
    (value.seats === undefined || (object(value.seats) && Object.values(value.seats).every(entry =>
      object(entry) && count(entry.turns) && count(entry.tokens) &&
      ['turns_cap', 'tokens_cap'].every(key => entry[key] == null || count(entry[key]))))) &&
    (value.holds === undefined || (Array.isArray(value.holds) && value.holds.every(hold =>
      object(hold) && text(hold.provider) && text(hold.profile) && nonnegative(hold.until) && typeof hold.reason === 'string'))))
  // The backend's retry verdict for one row (#371): whether a click would re-arm it, the head a
  // retry would target, and — when it would not — the host's own bounded reason. The page draws
  // the button from this and decides nothing itself.
  const sha = value => typeof value === 'string' && /^[0-9a-f]{7,40}$/.test(value)
  const retry = value => value === undefined || (object(value) &&
    typeof value.eligible === 'boolean' && typeof value.reason === 'string' &&
    (value.eligible ? sha(value.head) : (value.head === '' || sha(value.head))))
  const run = value => object(value) && count(value.pr) && value.pr > 0 &&
    text(value.seat) && text(value.head) && text(value.state) &&
    ['pending', 'claimed', 'launching', 'running', 'waiting', 'uncertain', 'failed',
      'cancelled'].includes(value.state) &&
    typeof value.why === 'string' && retry(value.retry)
  const turn = value => object(value) && count(value.turns) && count(value.retried) &&
    counts(value.states) && durationStats(value.ran) && durationStats(value.waited)
  // One loop of the /overview read: its identity, whether its ledger could be read at all, and —
  // only then — its counts. Unavailable or failed must carry no counts, so a missing ledger can
  // never be read as a verified zero.
  // A loop whose config file did not load (#701) has no repo and no ledger: it is named with
  // ``unreadable`` true and a reason, and can never be available or carry counts.
  const loop = value => object(value) && text(value.id) &&
    (value.unreadable === undefined || typeof value.unreadable === 'boolean') &&
    (value.unreadable === true ? value.repo === '' && typeof value.reason === 'string' &&
      value.available === false && value.error === true : text(value.repo)) &&
    typeof value.available === 'boolean' && typeof value.error === 'boolean' &&
    counts(value.counts) && count(value.needs_you) &&
    (value.available && !value.error
      ? true
      : Object.keys(value.counts).length === 0 && value.needs_you === 0)
  if (!object(data)) throw invalidResponse(kind)
  if (kind === 'now') {
    if (typeof data.available !== 'boolean' || !Array.isArray(data.runs) || !data.runs.every(run) ||
      !counts(data.counts) || !pacing(data.pacing)) throw invalidResponse(kind)
    // A ledger that could not be read must come back empty, never with manufactured counts.
    if (!data.available && (data.runs.length || Object.keys(data.counts).length)) throw invalidResponse(kind)
  } else if (kind === 'overview') {
    if (!Array.isArray(data.loops) || !data.loops.every(loop) || !nonnegative(data.read_at)) throw invalidResponse(kind)
  } else if (data.turns !== null && (!object(data.turns) || !Object.values(data.turns).every(turn))) throw invalidResponse(kind)
  return data
}

// The page's one write (#371): POST /retry {loop, pr, seat, head}. The head is what the operator
// confirmed — not a claim about the run. The backend re-reads the ledger and refuses a confirmation
// whose head has moved, so a stale click can never re-arm a different run.
function postRetry(payload) {
  return call('/retry', { method: 'POST', body: payload })
}

// The Desktop transport rejects across IPC with "<status>: <json body>"; anything else (a dead
// gateway, a shape nobody promised) becomes a safe category. The raw message is never rendered.
function retryFailure(error) {
  const message = typeof error?.message === 'string' ? error.message : ''
  const match = /(?:^|\s)(\d{3}):\s*(\{[\s\S]*\})\s*$/.exec(message)
  if (!match) return { status: 0, detail: '' }
  let detail = ''
  try {
    const body = JSON.parse(match[2])
    if (body && typeof body.detail === 'string') detail = body.detail.slice(0, 400)
  } catch {
    detail = ''
  }
  return { status: Number(match[1]), detail }
}

// A successful answer must carry the host's own line and the state it read back. Anything less is
// not a verified write and is never shown as one.
function verifiedRetry(data) {
  const object = value => value && typeof value === 'object' && !Array.isArray(value)
  return object(data) && typeof data.answer === 'string' && data.answer.length > 0 &&
    typeof data.head === 'string' && Array.isArray(data.rearmed) && data.rearmed.length > 0 &&
    Array.isArray(data.runs) &&
    data.runs.every(run => object(run) && typeof run.seat === 'string' &&
      typeof run.state === 'string')
}


// Query cache timestamps are the last successful fetch, not the latest failed attempt.
function viewState(q, empty, maxAge = 120000) {
  const hasData = q.data !== undefined && q.data !== null
  if (!hasData) return q.isError ? 'unavailable' : 'loading'
  if (q.isError || !q.dataUpdatedAt || Date.now() - q.dataUpdatedAt >= maxAge) return 'stale'
  return empty ? 'verified-empty' : 'verified'
}

// Shared local freshness metadata, never a query-cache/backend write. Weak keys let
// removed cache data be collected; each age keeps only its latest timestamp. Cache
// events anchor elapsed time even if neither page, chip nor palette has rendered.
// Imperative reads need no timers; mounted components own one cancellable wake-up.
let freshness = new WeakMap()

function freshRead(q, maxAge) {
  if (q.data == null || !q.dataUpdatedAt) return null
  let ages = freshness.get(q.data)
  if (!ages) { ages = new Map(); freshness.set(q.data, ages) }
  let read = ages.get(maxAge)
  if (!read || read.updatedAt !== q.dataUpdatedAt) {
    const remaining = Math.max(0, Math.min(maxAge, q.dataUpdatedAt + maxAge - Date.now()))
    read = { updatedAt: q.dataUpdatedAt, deadline: performance.now() + remaining, expired: remaining === 0 }
    ages.set(maxAge, read)
  }
  // A forward wall jump is also sticky; neither a later backwards jump nor a
  // fetch/error/observer notification can make the same successful read current.
  if (performance.now() >= read.deadline || Date.now() - q.dataUpdatedAt >= maxAge) read.expired = true
  return read
}

function trackedViewState(q, empty, maxAge = 120000) {
  return freshRead(q, maxAge)?.expired ? 'stale' : viewState(q, empty, maxAge)
}

function observeFreshness(query) {
  if (query.queryKey[0] !== ID) return
  const kind = query.queryKey[1]
  if (kind === 'now' || kind === 'overview' || kind === 'hooks') freshRead(query.state, 60000)
  else if (kind === 'loops' || kind === 'stats' || kind === 'settings') freshRead(query.state, 120000)
}

function useViewState(q, empty, maxAge = 120000) {
  const [, wake] = useState(0)
  const read = freshRead(q, maxAge)
  useEffect(() => {
    if (!read || read.expired) return
    const timer = setTimeout(() => {
      // This callback owns only this read, never its replacement. Even an already
      // queued old callback cannot mark a newer timestamp expired.
      read.expired = true
      wake(n => n + 1)
    }, Math.max(0, read.deadline - performance.now()))
    return () => clearTimeout(timer)
  }, [read])
  return trackedViewState(q, empty, maxAge)
}

function lastGood(q) {
  return q.dataUpdatedAt ? new Date(q.dataUpdatedAt).toLocaleString() : 'time unavailable'
}

function QueryStatus({ query, state, label, noLedger = false }) {
  return jsxs('div', {
    role: query.isError ? 'alert' : 'status',
    className: 'flex flex-wrap items-center gap-2 rounded-md border border-(--ui-stroke-secondary) p-4',
    children: [
      jsx(Badge, { size: 'xs', variant: state === 'stale' || state === 'unavailable' ? 'warn' : 'muted', children: state }),
      jsxs('div', {
        className: 'min-w-0 flex-1 text-xs text-(--ui-text-secondary)',
        children: [
          label + (state === 'loading' ? ': loading…' : state === 'unavailable' ? ': no verified data available.' : state === 'stale' ? ': showing last good data, not current status.' : ': backend read succeeded.'),
          // ``noLedger`` is the missing-ledger disclosure; a caller may pass its own sentence, else
          // it reads as the seat-turns default. Either way it is a disclosure, never a zero.
          noLedger ? jsx('div', { className: 'mt-1', children: typeof noLedger === 'string' ? noLedger : 'No run ledger available in the last successful backend read. Seat-turn counts are unavailable, not zero.' }) : null,
          query.data != null ? jsx('div', { className: 'mt-1 text-(--ui-text-tertiary)', children: 'Last good read: ' + lastGood(query) + (query.isFetching ? ' · refreshing…' : '') }) : null,
          query.isError ? jsx('div', { className: 'mt-1 break-words', children: safeError(query.error) }) : null
        ]
      }),
      jsx(Button, { size: 'xs', variant: 'ghost', disabled: query.isFetching, onClick: () => query.refetch(), children: query.isFetching ? 'Reading…' : 'Retry' })
    ]
  })
}

function usePr(loop, pr) {
  return useQuery({
    queryKey: [ID, 'pr', loop, pr],
    queryFn: () =>
      call('/pr?pr=' + pr + (loop ? '&loop=' + encodeURIComponent(loop) : '')),
    enabled: !!pr,
    staleTime: 15000
  })
}

function useHealth(loop, probe) {
  // The probe reads the loop's own tokens, so it is a different read, not a refresh of this one:
  // its own cache entry, and it only ever runs because the operator asked for it.
  const params = []
  if (loop) params.push('loop=' + encodeURIComponent(loop))
  if (probe) params.push('probe=1')
  return useQuery({
    queryKey: [ID, 'health', loop, probe ? 'probe' : 'local'],
    queryFn: () => call('/health' + (params.length ? '?' + params.join('&') : '')),
    enabled: !!loop,
    refetchOnWindowFocus: false,
    staleTime: HEALTH_STALE_MS
  })
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 90) return Math.round(seconds) + ' s'
  if (seconds < 5400) return (seconds / 60).toFixed(1) + ' min'
  if (seconds < 172800) return (seconds / 3600).toFixed(1) + ' h'
  return (seconds / 86400).toFixed(1) + ' d'
}

function ago(seconds) {
  if (!seconds) return '—'
  const delta = Date.now() / 1000 - seconds
  if (delta < 90) return Math.max(0, Math.round(delta)) + ' s ago'
  if (delta < 5400) return Math.round(delta / 60) + ' min ago'
  if (delta < 172800) return Math.round(delta / 3600) + ' h ago'
  return Math.round(delta / 86400) + ' d ago'
}

function clock(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString() : '—'
}

/** One seat's day: turns and tokens against their caps, as the loop counts them. */
function seatLine(seat, entry) {
  const cap = entry.turns_cap ? entry.turns_cap : 'no cap'
  const parts = [entry.turns + '/' + cap + ' today']
  if (entry.tokens || entry.tokens_cap) {
    parts.push((entry.tokens || 0).toLocaleString() + ' tokens')
  }
  return seat.replace('_', '-') + ' ' + parts.join(' · ')
}

/** The live usage-window state. A run's own held line is a snapshot from the moment it was
 *  held — it can name a reset that has already passed — so this is the answer that expires
 *  with the window, and it comes from the same read hermes dk status prints. */
function Pacing({ pacing }) {
  if (!pacing) return null
  const seats = Object.keys(pacing.seats || {})
  const holds = pacing.holds || []
  if (!seats.length && !holds.length) return null
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      holds.map(hold =>
        jsx('div', {
          className: 'text-xs text-(--ui-text-secondary)',
          children: 'held: ' + hold.provider + ' as profile ' + hold.profile +
            ' until ' + clock(hold.until) + ' (' + hold.reason + ')'
        }, hold.provider + hold.profile + hold.until)),
      seats.length
        ? jsx(Muted, { text: seats.map(seat => seatLine(seat, pacing.seats[seat])).join(' · ') })
        : null
    ]
  })
}

function summary(counts) {
  const parts = []
  if (counts.pending) parts.push(counts.pending + ' pending')
  if (counts.waiting) parts.push(counts.waiting + ' retry-waiting')
  if (counts.running) parts.push(counts.running + ' running')
  if (counts.held) parts.push(counts.held + ' held')
  if (counts.uncertain) parts.push(counts.uncertain + ' need you')
  // A fixer run the push policy cancelled waits for an operator: listed, never "idle" (#371).
  if (counts.cancelled) parts.push(counts.cancelled + ' cancelled')
  if (counts.failed) parts.push(counts.failed + ' failed')
  return parts.length ? parts.join(' · ') : 'idle'
}

/** doctor's own status vocabulary, as a badge and a dot. */
const CHECK_TONE = { verified: 'success', mismatch: 'destructive', absent: 'warn' }
const CHECK_DOT = { verified: 'good', mismatch: 'bad', absent: 'warn' }

function CheckRow({ check }) {
  return jsxs('div', {
    className: 'flex items-start gap-2 border-b border-(--ui-stroke-secondary) py-1.5 last:border-b-0',
    children: [
      jsx(StatusDot, { tone: CHECK_DOT[check.status] || 'muted', className: 'mt-1.5' }),
      jsxs('div', {
        className: 'flex min-w-0 flex-1 flex-col gap-0.5',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', { className: 'font-medium', children: check.name }),
              jsx(Badge, { size: 'xs', variant: CHECK_TONE[check.status] || 'muted', children: check.status })
            ]
          }),
          jsx('div', { className: 'text-xs text-(--ui-text-tertiary) break-words', children: check.detail }),
          check.fix
            ? jsx('div', { className: 'text-xs text-(--ui-text-quaternary) break-words', children: 'fix: ' + check.fix })
            : null
        ]
      })
    ]
  })
}

function ErrorLine({ error, children }) {
  return jsxs('div', {
    className: 'text-xs text-(--ui-text-tertiary)',
    children: [error ? safeError(error) + ' — ' : '', children]
  })
}

function Card({ value, label }) {
  return jsxs('div', {
    className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-2',
    children: [
      jsx('div', { className: 'text-lg font-medium text-(--ui-accent)', children: value }),
      jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: label })
    ]
  })
}

function Table({ head, rows }) {
  const cell = (tag, text, i) =>
    jsx(tag, {
      className: (i === 0 ? 'text-left' : 'text-right') + ' px-2 py-1 whitespace-nowrap',
      children: text
    }, i)
  return jsx('div', {
    className: 'overflow-x-auto rounded-md border border-(--ui-stroke-secondary)',
    children: jsxs('table', {
      className: 'w-full text-xs',
      children: [
        jsx('thead', {
          className: 'text-(--ui-text-tertiary)',
          children: jsx('tr', { children: head.map((h, i) => cell('th', h, i)) })
        }),
        jsx('tbody', {
          children: rows.map((row, r) => jsx('tr', { children: row.map((c, i) => cell('td', c, i)) }, r))
        })
      ]
    })
  })
}

function Section({ title, children }) {
  return jsxs('section', {
    className: 'flex flex-col gap-2',
    children: [jsx('h2', { className: 'text-sm font-medium', children: title }), children]
  })
}

function Muted({ text }) {
  return jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: text })
}

/** One live run: the row opens the drawer that answers "why is this not moving", and a run the
 *  backend says retry would re-arm carries the page's one write control (issue 371). The page decides
 *  nothing about eligibility itself: the verdict, the target head and the reason a row has no
 *  button all come from the backend, and the reason is shown on hover rather than invented. */
function RunRow({ run, onOpen, onRetry }) {
  const retry = run.retry
  const canRetry = !!(retry && retry.eligible)
  return jsxs('div', {
    className:
      'flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-(--ui-stroke-secondary) px-2 py-1.5 ' +
      'last:border-b-0',
    children: [
      jsxs('button', {
        type: 'button',
        className:
          'flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1 text-left text-xs ' +
          'hover:text-(--ui-accent)',
        onClick: () => onOpen(run.pr),
        children: [
          jsx('span', { className: 'w-14 shrink-0 font-mono text-(--ui-accent)', children: '#' + run.pr }),
          jsx('span', { className: 'w-20 shrink-0 text-(--ui-text-tertiary)', children: run.seat }),
          jsx('span', { className: 'w-16 shrink-0 font-mono text-(--ui-text-quaternary)', children: run.head }),
          jsx('span', {
            title: retry && !canRetry && retry.reason ? retry.reason : undefined,
            children: jsx(Badge, { size: 'xs', variant: 'muted', children: run.state })
          }),
          jsx('span', { className: 'min-w-0 basis-full break-words text-(--ui-text-tertiary) sm:basis-auto sm:flex-1 sm:truncate', children: run.why })
        ]
      }),
      canRetry
        ? jsx(Button, {
            size: 'xs',
            variant: 'ghost',
            title: 'Re-arm this run — you will be asked to confirm',
            onClick: () => onRetry(run),
            children: 'Retry'
          })
        : null
    ]
  })
}

/** The drawer: the host's own line for what moves each stuck run on this PR, what the ledger
 *  recorded about it, and — honestly — what this view cannot see. */
function Drawer({ loop, pr, onClose }) {
  const q = usePr(loop, pr)
  const d = q.data
  const stuck = (d && d.stuck) || []
  const turns = (d && d.turns) || []
  const rulings = (d && d.rulings) || []
  // The lists hold the newest rows only; the totals say how many the ledger has.
  const turnsTotal = (d && d.turns_total) || 0
  const rulingsTotal = (d && d.rulings_total) || 0
  const verdicts = (d && d.verdicts) || {}
  const counts = Object.keys(verdicts)
    .sort()
    .map(name => verdicts[name] + ' ' + name)
    .join(' · ')

  const findings = (d && d.findings) || []
  const openFindings = findings.filter(item => item.open)
  const decisions = ((d && d.decisions) || []).slice().reverse()
  // Shadow reviews (#218): a second model's would-be verdicts at heads the reviewer reviewed.
  // Recorded for comparison and never posted; nothing in the loop acts on them.
  const shadows = (d && Array.isArray(d.shadows) && d.shadows) || []
  const shadowsBlock = shadows.map((item, i) =>
    jsxs('div', {
      className: 'flex flex-col gap-0.5 border-b border-(--ui-stroke-secondary) py-1.5 last:border-b-0',
      children: [
        jsxs('div', {
          className: 'flex flex-wrap items-center gap-2',
          children: [
            jsx('span', { className: 'font-mono text-(--ui-text-quaternary)', children: item.head }),
            jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: item.model }),
            jsx(Badge, { size: 'xs', variant: 'muted', children: 'would have ' + item.verdict }),
            jsx(Badge, {
              size: 'xs',
              variant: item.agrees === false ? 'warn' : 'muted',
              children: item.real == null
                ? 'no real verdict yet'
                : 'reviewer ' + item.real + (item.agrees ? ' · agrees' : ' · disagrees')
            })
          ]
        }),
        item.held ? jsx(Muted, { text: 'Approval left to a person: ' + item.held }) : null,
        ...(item.findings || []).map((text, j) =>
          jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: text }, j)),
        item.more ? jsx(Muted, { text: '… ' + item.more + ' more' }) : null
      ]
    }, i))

  const findingsBlock = findings.length
    ? jsx('div', {
        className: 'flex flex-col gap-1',
        children: findings
          .slice()
          .sort((a, b) => (b.open ? 1 : 0) - (a.open ? 1 : 0))
          .map(item =>
            jsxs('div', {
              className: 'flex items-start gap-2 border-b border-(--ui-stroke-secondary) py-1.5 last:border-b-0',
              children: [
                jsx('span', { className: 'w-8 shrink-0 font-mono text-(--ui-text-quaternary)', children: item.id }),
                jsx(Badge, {
                  size: 'xs',
                  variant: item.open ? 'warn' : 'muted',
                  children: item.missed && item.open ? 'open (missed)' : item.state
                }),
                jsx('span', {
                  className: 'min-w-0 flex-1 text-xs text-(--ui-text-secondary)',
                  children: item.text + (item.head ? '  · at ' + item.head : '')
                })
              ]
            }, item.id))
      })
    : jsx(Muted, { text: 'The reviewer has raised no numbered findings on this PR.' })

  const decisionsBlock = decisions.length
    ? decisions.map(decision =>
        jsxs('div', {
          className: 'flex items-start gap-2 border-b border-(--ui-stroke-secondary) py-1.5 last:border-b-0',
          children: [
            jsx('span', { className: 'w-40 shrink-0 font-mono text-(--ui-text-quaternary)', children: decision.gate }),
            jsx(Badge, {
              size: 'xs',
              variant: decision.decision === 'accepted' ? 'success' : 'muted',
              children: decision.event + (decision.action ? '/' + decision.action : '')
            }),
            jsxs('span', {
              className: 'min-w-0 flex-1 text-xs text-(--ui-text-secondary)',
              children: [
                decision.decision + ': ' + decision.reason,
                decision.sender ? ' · by ' + decision.sender : ''
              ]
            })
          ]
        }, decision.gate + decision.event + decision.time))
    : jsx(Muted, { text: 'No gate saw an event for this PR — the loop never heard of it.' })

  const why = stuck.length
    ? jsx('div', {
        className: 'flex flex-col gap-2',
        children: stuck.map(run =>
          jsxs('div', {
            className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-2',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-2 text-xs',
                children: [
                  jsx('span', { className: 'w-20 shrink-0 text-(--ui-text-tertiary)', children: run.seat }),
                  jsx('span', { className: 'w-16 shrink-0 font-mono text-(--ui-text-quaternary)', children: run.head }),
                  jsx(Badge, { size: 'xs', variant: 'muted', children: run.state }),
                  jsx('span', { className: 'text-(--ui-text-quaternary)', children: ago(run.since) })
                ]
              }),
              jsx('div', { className: 'pt-1 text-xs text-(--ui-text-secondary)', children: run.next })
            ]
          }, run.seat + run.head + run.state))
      })
    : jsx(Muted, { text: q.isLoading ? 'Loading…' : 'No run on this PR is waiting on anything.' })

  return jsx(Dialog, {
    open: true,
    onOpenChange: open => {
      if (!open) onClose()
    },
    children: jsxs(DialogContent, {
      className: 'w-[min(58rem,94vw)] max-w-none',
      bodyClassName: 'max-h-[min(80vh,46rem)] overflow-y-auto',
      children: [
        jsx(DialogHeader, {
          children: jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx(DialogTitle, { children: '#' + pr + (d && d.repo ? ' · ' + d.repo : '') }),
              jsx(DialogDescription, {
                children:
                  'What moves it, from this machine’s ledger' +
                  (d && d.cap ? ' · verdict cap ' + d.cap : '') +
                  (d && d.read_at ? ' · read ' + ago(d.read_at) : '')
              })
            ]
          })
        }),
        q.isError
          ? jsx(ErrorLine, { error: q.error, children: 'the loop backend did not answer' })
          : jsxs('div', {
              className: 'flex flex-col gap-5 pt-4',
              children: [
                jsx(Pacing, { pacing: d && d.pacing }),
                jsx(Section, { title: 'Why it is not moving', children: why }),
                jsxs(Section, {
                  title: 'Findings (' + openFindings.length + ' open of ' + findings.length + ')',
                  children: findingsBlock
                }),
                shadows.length
                  ? jsxs(Section, {
                      title: 'Shadow reviews (' + shadows.length + ', recorded, never posted)',
                      children: [
                        ...shadowsBlock,
                        d && d.shadows_older > 0
                          ? jsx(Muted, { text: '… ' + d.shadows_older + ' older shadow review(s) not shown' }, 'older')
                          : null
                      ]
                    })
                  : null,
                jsx(Section, { title: 'Why the loop did or did not react', children: decisionsBlock }),
                d && d.breach
                  ? jsx(Muted, {
                      text:
                        'Cap spent on ' + d.breach.head + ' (' + d.breach.status + ')' +
                        (d.breach.rounds ? ' after ' + d.breach.rounds + ' round(s)' : '')
                    })
                  : null,
                jsxs(Section, {
                  title: 'Recorded here',
                  children: [
                    jsx(Muted, {
                      text: counts
                        ? 'Reviewer verdicts this ledger recorded: ' + counts
                        : 'No reviewer verdict recorded for this PR.'
                    }),
                    rulings.length
                      ? jsx(Muted, {
                          text:
                            (rulingsTotal > rulings.length
                              ? 'Adjudicator rulings (newest ' + rulings.length + ' of ' + rulingsTotal + '): '
                              : 'Adjudicator rulings: ') +
                            rulings.map(r => r.verdict + ' on ' + r.head).join(' · ')
                        })
                      : null,
                    jsx(Muted, {
                      text:
                        'The gates count every verdict on GitHub, a person’s included, so the live ' +
                        'round count can be higher than this.'
                    })
                  ]
                }),
                jsxs(Section, {
                  title:
                    turnsTotal > turns.length
                      ? 'Turns (newest ' + turns.length + ' of ' + turnsTotal + ')'
                      : 'Turns (' + turns.length + ')',
                  children: turns.length
                    ? [
                        turnsTotal > turns.length
                          ? jsx(Muted, {
                              text: '… ' + (turnsTotal - turns.length) + ' older turn(s) not shown'
                            }, 'older')
                          : null,
                        jsx(Table, {
                          head: ['when', 'seat', 'head', 'state'],
                          rows: turns
                            .slice()
                            .reverse()
                            .map(t => [clock(t.created), t.seat, t.head, t.state])
                        }, 'turns')
                      ]
                    : jsx(Muted, { text: 'No turn recorded for this PR.' })
                }),
                jsx(Muted, {
                  text:
                    'This view is the ledger and the loop’s own files. For the GitHub half — CI, ' +
                    'reviews, rulesets — run: hermes dk explain --pr ' + pr +
                    (loop ? ' --loop ' + loop : '') + '.'
                })
              ]
            })
      ]
    })
  })
}

/** One loop's live state as a short phrase: its run counts, or why they could not be read. */
function loopLine(entry) {
  // The config file did not load: fix the file. Distinct from ``not read`` (this read did not
  // name a loop that is configured): wait or retry.
  if (entry.unreadable) return 'config unreadable'
  if (entry.missing) return 'not read'
  if (entry.error) return 'read failed'
  if (!entry.available) return 'no ledger'
  return summary(entry.counts)
}

/** How many runs across an /overview read are waiting on the operator, ignoring unreadable loops. */
function needsYouTotal(entries) {
  return entries.reduce((n, entry) => n + (entry.available && !entry.error ? entry.needs_you || 0 : 0), 0)
}

function StatusChip() {
  const loops = useLoops()
  const overview = useOverview()
  const list = (loops.data && loops.data.loops) || []
  const entries = (overview.data && overview.data.loops) || []
  const noLoops = loops.data && !loops.data.loops?.length
  const loopsState = useViewState(loops, !!noLoops)
  // The overview is empty only when every loop was read and every one is idle: a loop whose
  // ledger could not be read is unknown, not empty.
  const overviewEmpty = entries.length > 0 &&
    entries.every(entry => entry.available && !entry.error && !Object.values(entry.counts).some(n => n > 0))
  const overviewState = useViewState(overview, overviewEmpty, 60000)
  const byId = {}
  entries.forEach(entry => { byId[entry.id] = entry })
  // A configured loop the read did not name is unknown, not idle: it keeps its own line rather
  // than disappearing from the chip.
  // An unreadable loop is named by /loops itself, whatever this overview read says about it.
  const live = list.map(loop => loop.unreadable
    ? { id: loop.id, repo: '', available: false, error: true, unreadable: true, reason: loop.reason || '', counts: {}, needs_you: 0 }
    : byId[loop.id] ||
      { id: loop.id, repo: loop.repo, available: false, error: true, missing: true, counts: {}, needs_you: 0 })
  const total = needsYouTotal(entries)
  // Announce a rise in any loop's uncertain count, once, naming the loop. The first read of a
  // session only records each loop's baseline, so a reload is never news; a loop whose ledger
  // could not be read keeps its previous baseline and is left out rather than compared to zero.
  useEffect(() => {
    if (!overview.data || loopsState !== 'verified' || !overviewState.startsWith('verified')) return
    const risen = []
    for (const entry of overview.data.loops) {
      if (!entry.available || entry.error || !storage) continue
      const key = NEEDS_YOU_PREFIX + entry.id
      const seen = storage.get(key)
      if (typeof seen === 'number' && entry.needs_you > seen) risen.push(entry.id)
      storage.set(key, entry.needs_you)
    }
    if (risen.length) host.notify({ kind: 'warn',
      message: 'Diaktoros: ' + total + ' run(s) need you (' + risen.join(', ') + ')' })
  }, [overview.data, overviewState, loopsState])
  const state = !loops.data || noLoops ? loopsState
    : loopsState === 'stale' ? overview.data ? 'stale' : 'unavailable'
    : overviewState
  const named = list.length > 1
  const text = state === 'loading' || state === 'unavailable' ? state
    : noLoops ? state + (state === 'verified-empty' ? ' · no loops configured' : ' · loop discovery not current')
    : named ? state + ' · ' + live.map(entry => entry.id + ' ' + loopLine(entry)).join(' · ')
    : live.length ? state + ' · ' + loopLine(live[0])
    : state
  // The chip reports the loop that needs you first, else the first loop; the page opens it.
  // The page can only open a loop whose config loads, so an unreadable one is never the focus
  // unless nothing else exists.
  const focus = live.find(entry => entry.available && !entry.error && entry.needs_you > 0) ||
    live.find(entry => !entry.unreadable) || live[0] || list[0]
  const retryQuery = loopsState === 'stale' || !loops.data || noLoops ? loops : overview
  const names = named ? list.map(loop => loop.id).join(', ') : (focus?.id || 'no loop selected')
  return jsxs('div', {
    className: 'flex items-center gap-1',
    children: [
      jsx('button', {
        type: 'button',
        className: 'px-2 text-xs text-(--ui-text-secondary)',
        title: 'Diaktoros · ' + names +
          (noLoops ? ' · Last good loop read: ' + lastGood(loops) : overview.data ? ' · Last good read: ' + lastGood(overview) : '') +
          (retryQuery.isError ? ' · ' + safeError(retryQuery.error) : ''),
        onClick: () => {
          // Write the handoff before navigating: an open page follows the cache, a page that
          // mounts next reads it. The route also names the loop, for the URL.
          queryClient.setQueryData(FOCUS_KEY, { loop: focus ? focus.id : '' })
          host.navigate(PATH + (focus ? '?loop=' + encodeURIComponent(focus.id) : ''))
        },
        children: (named ? 'loops: ' : 'loop: ') + text
      }),
      state === 'stale' || state === 'unavailable'
        ? jsx(Button, { size: 'xs', variant: 'ghost', disabled: retryQuery.isFetching,
            onClick: () => retryQuery.refetch(), children: 'Retry' }) : null
    ]
  })
}

/** The cross-loop answer to "what is waiting on me": one entry per loop that needs the operator,
 *  each opening that loop on this page. Only shown when more than one loop is configured. */
function NeedsYou({ overview, state, onPick }) {
  const entries = (overview.data && overview.data.loops) || []
  const waiting = entries.filter(entry => entry.available && !entry.error && entry.needs_you > 0)
  const broken = entries.filter(entry => entry.error || !entry.available)
  // Without a verified read there is nothing to claim: no "nothing is waiting" line, no entries.
  // "Nothing is waiting" is only honest when every loop was read — a broken loop is not idle.
  const unread = !overview.data || state === 'unavailable'
  const readable = entries.length > 0 && !broken.length
  return jsx(Section, {
    title: 'Needs you',
    children: jsxs('div', {
      className: 'flex flex-col gap-2',
      children: [
        state === 'stale' || state === 'unavailable'
          ? jsx(QueryStatus, { query: overview, state, label: 'Across all loops' }) : null,
        unread ? null : waiting.length
          ? jsx('div', {
              className: 'rounded-md border border-(--ui-stroke-secondary)',
              children: waiting.map(entry => jsxs('button', {
                type: 'button',
                className: 'flex w-full flex-wrap items-center gap-x-3 gap-y-1 border-b border-(--ui-stroke-secondary) px-2 py-1.5 text-left text-xs last:border-b-0 hover:text-(--ui-accent)',
                onClick: () => onPick(entry.id),
                children: [
                  jsx('span', { className: 'w-24 shrink-0 font-mono text-(--ui-accent)', children: entry.id }),
                  jsx('span', { className: 'min-w-0 flex-1 text-(--ui-text-secondary)', children: summary(entry.counts) })
                ]
              }, entry.id))
            })
          : readable ? jsx(Muted, { text: 'No run in any loop is waiting on you.' }) : null,
        unread || !broken.length ? null
          : jsx(Muted, { text: 'Could not read: ' + broken.map(entry => entry.id + ' (' + loopLine(entry) + ')').join(', ') })
      ]
    })
  })
}

/** The one line a clean install gets: nothing wrong, and how many checks could not be read offline. */
function cleanLine(counts, total) {
  const unknown = counts.unknown || 0
  return 'No mismatch or absent check (' + total + ' checked' +
    (unknown ? ', ' + unknown + ' unknown: the offline ones, see Show all' : '') + ').'
}

/** Turn states that have no column of their own, so the row still adds up to its turns. */
function otherStates(states) {
  const own = ['succeeded', 'failed', 'waiting']
  const rest = Object.keys(states || {}).filter(k => !own.includes(k) && states[k] > 0).sort()
  return rest.length ? rest.map(k => states[k] + ' ' + k).join(', ') : '—'
}

function Health({ loop }) {
  const [showAll, setShowAll] = useState(false)
  const [probe, setProbe] = useState(false)
  const health = useHealth(loop, probe)
  const checks = (health.data && health.data.checks) || []
  const counts = (health.data && health.data.counts) || {}

  // The failures first: an install is read for what is wrong with it, not for its 40 verified
  // lines. Then what could not be read at all — an unprobed check is not a passing one, and the
  // offline set is exactly what Probe GitHub is for. "Show all" reaches the rest without hiding
  // that they were checked.
  const attention = checks.filter(c => c.status === 'mismatch' || c.status === 'absent')
  const unprobed = checks.filter(c => c.status === 'unknown')
  const shown = showAll ? checks : attention.length ? attention : unprobed

  const button = (label, onClick, disabled) =>
    jsx(Button, { size: 'xs', variant: 'ghost', onClick, disabled, children: label })

  return jsx(Section, {
    title: 'Health',
    children: jsxs('div', {
      className: 'flex flex-col gap-2',
      children: [
        jsxs('div', {
          className: 'flex flex-wrap items-center gap-2',
          children: [
            Object.keys(counts)
              .sort()
              .map(name =>
                jsx(Badge, { size: 'xs', variant: CHECK_TONE[name] || 'muted', children: counts[name] + ' ' + name }, name)
              ),
            jsx('div', { className: 'flex-1' }),
            health.data && health.data.offline
              ? button('Probe GitHub', () => setProbe(true), health.isFetching)
              : null,
            button(health.isFetching ? 'Checking…' : 'Re-check', () => health.refetch(), health.isFetching),
            checks.length
              ? button(showAll ? 'Only problems' : 'Show all ' + checks.length, () => setShowAll(!showAll), false)
              : null
          ]
        }),
        health.isError
          ? jsx(ErrorLine, { error: health.error, children: 'the health view did not answer' })
          : jsx('div', {
              className: 'rounded-md border border-(--ui-stroke-secondary) px-3',
              children: health.isLoading
                ? jsx('div', { className: 'py-2', children: jsx(Muted, { text: 'Checking…' }) })
                : shown.length
                  ? shown.map(check => jsx(CheckRow, { check }, check.name + check.status))
                  : jsx('div', {
                      className: 'py-2',
                      children: jsx(Muted, { text: cleanLine(counts, checks.length) })
                    })
            }),
        jsx(Muted, {
          text: health.data && health.data.offline
            ? 'The installation’s own preflight, read offline: the gateway, the hooks, token expiry ' +
              'and scopes, and the labels are left unprobed — nothing here fires a route or a ' +
              'GitHub call. Probe GitHub reads them with this loop’s own tokens, from this machine ' +
              'only; hermes dk doctor does the same in a terminal.'
            : 'Read on demand with this loop’s own tokens: the gateway, every hook with its latest ' +
              'delivery and decision, token expiry and scopes, and the labels — the same set ' +
              'hermes dk doctor reads.'
        })
      ]
    })
  })
}

// Settings is a read projection, not another editor. Unknown schema fields are enumerated,
// but structured/secret values are never stringified into the page.
const settingsObject = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const settingsScalar = value => value === null || typeof value === 'string' || typeof value === 'boolean' ||
  (typeof value === 'number' && Number.isFinite(value))

function validateSettings(data) {
  const object = settingsObject
  const strings = value => Array.isArray(value) && value.every(item => typeof item === 'string')
  const fields = (value, keys) => object(value) && keys.every(key => typeof value[key] === 'string')
  const limit = value => Number.isSafeInteger(value) && value >= 0
  const p = data?.plugin
  if (!object(data) || !object(p) || typeof p.available !== 'boolean' || typeof p.reason !== 'string' ||
      !object(p.schema) || !object(p.values) || !object(p.sources) || !object(p.seats) ||
      !strings(p.roles) || !strings(p.routed_roles) || !Array.isArray(data.loops) ||
      typeof data.read_at !== 'number' || !Number.isFinite(data.read_at) || data.read_at < 0) throw invalidResponse('settings')
  const sameKeys = value => Object.keys(value).length === Object.keys(p.schema).length &&
    Object.keys(p.schema).every(key => Object.hasOwn(value, key))
  if (!sameKeys(p.values) || !sameKeys(p.sources)) throw invalidResponse('settings')
  for (const [key, spec] of Object.entries(p.schema)) {
    if (!fields(spec, ['label', 'type', 'description']) || !Object.hasOwn(spec, 'default') ||
        !['set', 'default'].includes(p.sources[key])) throw invalidResponse('settings')
    const valid = value => spec.type === 'int' ? Number.isSafeInteger(value) :
      spec.type === 'bool' ? typeof value === 'boolean' : spec.type === 'str' ? typeof value === 'string' : true
    if (!valid(p.values[key]) || !valid(spec.default)) throw invalidResponse('settings')
  }
  if (!p.roles.every(role => Object.hasOwn(p.seats, role)) ||
      !Object.values(p.seats).every(seat => fields(seat, ['profile', 'login', 'token_file']) && typeof seat.set === 'boolean')) throw invalidResponse('settings')
  const ids = new Set()
  for (const loop of data.loops) {
    if (!fields(loop, ['id', 'repo', 'base', 'reason']) || !loop.id || ids.has(loop.id) ||
        typeof loop.error !== 'boolean' || !object(loop.seats) || !object(loop.flags)) throw invalidResponse('settings')
    ids.add(loop.id)
    if (!loop.error && (!loop.repo || typeof loop.flags.review_after_ci !== 'boolean' ||
        typeof loop.flags.fix_ci !== 'boolean' || typeof loop.flags.fixer_check !== 'string' ||
        !limit(loop.flags.cap) || typeof loop.flags.base !== 'string')) throw invalidResponse('settings')
    if (!Object.values(loop.flags).every(settingsScalar) || !Object.values(loop.seats).every(seat =>
      fields(seat, ['profile', 'login', 'token_file', 'route']) && limit(seat.turn_budget_s) && limit(seat.max_steps)) ||
      (!loop.error && !p.roles.every(role => Object.hasOwn(loop.seats, role)))) throw invalidResponse('settings')
  }
  return data
}

function settingsText(value) {
  // Defense in depth for recognizable credentials accidentally returned in a non-secret field.
  return String(value).replace(/(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|Bearer\s+\S+)/gi, '[redacted]')
}

function settingValue(key, value) {
  if (/(?:password|secret|credential|api[_-]?key|access[_-]?token|^token$)/i.test(key)) return 'Sensitive value withheld'
  if (/(?:^|_)token_file$/.test(key) && value !== '' &&
      (typeof value !== 'string' || !/^(?:\/|~\/)[^\r\n]+$/.test(value))) return 'Invalid token path withheld'
  if (!settingsScalar(value)) return 'Unsupported structured value (withheld)'
  return value === '' || value === null ? 'Not set here' : settingsText(value)
}

function Settings() {
  const query = useQuery({ queryKey: [ID, 'settings'], queryFn: async () => validateSettings(await call('/settings')),
    staleTime: 120000, refetchOnWindowFocus: false })
  const state = useViewState(query, !!query.data && !Object.keys(query.data.plugin.schema).length && !query.data.loops.length)
  const p = query.data?.plugin
  const row = (label, value) => jsxs('div', { className: 'flex min-w-0 flex-col gap-1', children: [
    jsx('dt', { className: 'text-xs text-(--ui-text-tertiary)', children: label }),
    jsx('dd', { className: 'm-0 break-words text-xs', children: value })] }, label)
  return jsx(Section, { title: 'Settings', children: jsxs('div', { className: 'flex min-w-0 flex-col gap-4', children: [
    jsx(Muted, { text: 'Read-only. Edit plugin defaults in Settings → Capabilities → Plugins. Existing loops keep their own effective settings until explicitly applied; reading changes nothing.' }),
    jsx(QueryStatus, { query, state: p?.available === false && state !== 'stale' ? 'unavailable' : state, label: 'Settings' }),
    !p ? null : jsxs('div', { className: 'flex min-w-0 flex-col gap-4', children: [
      jsx('h3', { className: 'text-sm font-medium', children: 'Plugin defaults' }),
      !p.available ? jsx('div', { role: 'status', className: 'text-xs text-(--ui-text-secondary)', children: 'Settings store unavailable. Schema defaults below are reference only, not verified effective plugin values.' }) : null,
      !Object.keys(p.schema).length ? jsx(Muted, { text: 'No settings fields returned in this read.' }) : null,
      jsx('div', { className: 'grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-2', children: Object.entries(p.schema).map(([key, spec]) =>
        jsxs('article', { className: 'min-w-0 rounded-md border border-(--ui-stroke-secondary) p-4', children: [
          jsxs('div', { className: 'flex flex-wrap items-start gap-2', children: [
            jsx('h4', { className: 'min-w-0 flex-1 break-words text-sm font-medium', children: settingsText(spec.label) }),
            jsx(Badge, { size: 'xs', variant: 'muted', children: settingsText(spec.type) })] }),
          jsx('div', { className: 'mt-1 break-words font-mono text-xs text-(--ui-text-tertiary)', children: settingsText(key) }),
          jsx('p', { className: 'my-2 break-words text-xs text-(--ui-text-secondary)', children: settingsText(spec.description) }),
          jsx('dl', { className: 'm-0 flex flex-col gap-2', children: [
            row(p.available ? 'Effective plugin value' : 'Effective plugin value unavailable', p.available ? settingValue(key, p.values[key]) : 'Not verified'),
            row('Source', p.available ? p.sources[key] === 'set' ? 'Set in plugin form' : 'Schema default' : 'Unavailable'),
            row('Schema default (reference)', settingValue(key, spec.default))] })
        ] }, key)) }),
      jsx('h3', { className: 'text-sm font-medium', children: 'Existing loops · effective settings' }),
      jsx(Muted, { text: 'These are each loop’s own projected answers, not the plugin defaults above. Omitted settings, including shadow activation, are not reported as off. Token files are paths only; credentials are never read or displayed.' }),
      !query.data.loops.length ? jsx(Muted, { text: 'No loops returned in this settings read.' }) : null,
      query.data.loops.map(loop => jsxs('article', { className: 'min-w-0 rounded-md border border-(--ui-stroke-secondary) p-4', children: [
        jsx('h4', { className: 'break-words text-sm font-medium', children: settingsText(loop.id) + (loop.repo ? ' · ' + settingsText(loop.repo) : '') }),
        loop.error ? jsx('div', { role: 'status', className: 'mt-2 text-xs', children: 'Loop settings unavailable: config or effective settings could not be read. Other loops are unaffected.' }) : jsxs('div', { className: 'mt-4 flex min-w-0 flex-col gap-4', children: [
          jsx('dl', { className: 'm-0 grid grid-cols-1 gap-2 sm:grid-cols-2', children: Object.entries(loop.flags).map(([key, value]) => row(settingsText(key), settingValue(key, value))) }),
          Object.entries(loop.seats).map(([role, seat]) => jsxs('div', { className: 'min-w-0 border-t border-(--ui-stroke-secondary) pt-4', children: [
            jsx('h5', { className: 'mb-2 text-xs font-medium', children: settingsText(role) }),
            jsx('dl', { className: 'm-0 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3', children: Object.entries(seat).map(([key, value]) => row(settingsText(key), settingValue(key, value))) })
          ] }, role))] })
      ] }, loop.id))
    ] })
  ] }) })
}

// Hook status is host-owned; unknown never means paused, and the confirmation is opaque.
function validateHooks(data, loop, write = false) {
  const object = data && typeof data === 'object' && !Array.isArray(data)
  const bounded = value => typeof value === 'string' && value.length <= 400 && !/[\r\n\x00-\x1f]/.test(value)
  if (!object || data.loop !== loop || typeof data.repo !== 'string' ||
      !['armed', 'paused', 'unknown'].includes(data.state) || typeof data.eligible !== 'boolean' ||
      !bounded(data.reason) || typeof data.read_at !== 'number' || !Number.isFinite(data.read_at) || data.read_at < 0 ||
      !(data.confirmation === null || (typeof data.confirmation === 'string' && data.confirmation.length > 0 && data.confirmation.length <= 4096)) ||
      (data.eligible && (data.state === 'unknown' || !data.repo || !data.confirmation)) ||
      (data.state === 'unknown' && data.confirmation !== null) ||
      (write && (typeof data.ok !== 'boolean' || !bounded(data.answer) || !data.answer))) throw invalidResponse('hooks')
  return data
}

function HooksControl({ loop, discoveryCurrent }) {
  const q = useQuery({ queryKey: [ID, 'hooks', loop], enabled: !!loop,
    queryFn: async () => validateHooks(await call('/hooks?loop=' + encodeURIComponent(loop)), loop),
    refetchInterval: 30000 })
  const state = useViewState(q, false, 60000)
  const [pending, setPending] = useState(null)
  const [answer, setAnswer] = useState(null)
  const current = useRef(null)
  const targetRef = useRef(null)
  const busy = useRef(false)
  const consumed = useRef(null)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  // Latest render is authoritative even for callbacks retained by a closing dialog.
  current.current = { loop, q, discoveryCurrent }
  const allowed = () => {
    const latest = current.current
    return mounted.current && latest.loop === loop && latest.discoveryCurrent &&
      !latest.q.isFetching && trackedViewState(latest.q, false, 60000) === 'verified' &&
      latest.q.data?.loop === loop && latest.q.data?.eligible === true &&
      ['armed', 'paused'].includes(latest.q.data.state) &&
      !(consumed.current?.data === latest.q.data && consumed.current?.at === latest.q.dataUpdatedAt)
  }
  const cancel = () => { targetRef.current = null; setPending(null) }
  useEffect(() => { cancel(); setAnswer(null) }, [loop])
  const ask = () => {
    if (busy.current || !allowed()) return
    const target = { loop, action: q.data.state === 'armed' ? 'pause' : 'resume',
      confirmation: q.data.confirmation, data: q.data, at: q.dataUpdatedAt }
    targetRef.current = target
    setAnswer(null)
    setPending(target)
  }
  const confirm = async target => {
    if (busy.current || !target || targetRef.current !== target) return
    const latest = current.current.q
    if (!allowed() || target.loop !== current.current.loop || target.data !== latest.data ||
        target.at !== latest.dataUpdatedAt || target.confirmation !== latest.data?.confirmation) {
      cancel()
      setAnswer('Confirmation expired. Read hook status again before confirming.')
      return
    }
    busy.current = true
    consumed.current = { data: latest.data, at: latest.dataUpdatedAt }
    try {
      const result = validateHooks(await call('/hooks', { method: 'POST', body: {
        loop: target.loop, action: target.action, confirmation: target.confirmation } }), target.loop, true)
      if (mounted.current && current.current.loop === target.loop && targetRef.current === target) {
        setAnswer(result.ok && result.state === (target.action === 'pause' ? 'paused' : 'armed')
          ? 'Backend verified webhook delivery ' + result.state + ' for loop ' + target.loop + '.'
          : 'The backend could not confirm the requested change. Read hook status again.')
      }
    } catch (error) {
      const failure = retryFailure(error)
      const text = error?.code === 'INVALID_RESPONSE' ? 'The hook answer could not be verified. Read hook status again.'
        : failure.status === 409 ? 'HTTP 409 · Confirmation refused: hook state changed. Read hook status and confirm again.'
        : failure.status === 422 ? 'HTTP 422 · Hook change refused. Read hook status before trying again.'
        : 'Hook change could not be verified. Check the connection and read hook status again.'
      if (mounted.current && current.current.loop === target.loop && targetRef.current === target) setAnswer(text)
    } finally {
      busy.current = false
      if (targetRef.current === target) cancel()
      queryClient.invalidateQueries({ queryKey: [ID, 'hooks', target.loop] })
      queryClient.invalidateQueries({ queryKey: [ID, 'health', target.loop] })
    }
  }
  const awaitingRead = consumed.current?.data === q.data && consumed.current?.at === q.dataUpdatedAt
  const verified = state === 'verified' && !q.isFetching && discoveryCurrent && !awaitingRead
  return jsx(Section, { title: 'Webhook delivery', children: jsxs('div', {
    className: 'flex min-w-0 flex-col gap-2', children: [
      jsx(QueryStatus, { query: q, state: awaitingRead ? 'stale' : state, label: 'Webhook delivery' }),
      jsx(Muted, { text: verified ? 'Loop ' + loop + ' · ' + (q.data?.state || 'unknown') : 'Current webhook delivery state unknown. Read status before changing it.' }),
      jsx(Muted, { text: 'Pausing stops webhook delivery, not in-flight turns. The sweeper may recover events while hooks are paused; this is not a full execution stop.' }),
      allowed() && !busy.current ? jsx(Button, { size: 'xs', variant: 'ghost', onClick: ask,
        children: q.data.state === 'armed' ? 'Pause webhook delivery' : 'Resume webhook delivery' }) : null,
      answer ? jsx('div', { role: 'status', className: 'break-words text-xs text-(--ui-text-secondary)', children: answer }) : null,
      pending && pending.loop === loop ? jsx(ConfirmDialog, { open: true,
        title: pending.action === 'pause' ? 'Pause webhook delivery?' : 'Resume webhook delivery?',
        description: (pending.action === 'pause' ? 'Pause' : 'Resume') + ' webhook delivery for loop ' + pending.loop +
          '. Pausing stops GitHub webhook delivery, not in-flight turns. The sweeper may recover events even while paused. Resuming allows new webhook delivery; it does not restart completed turns.',
        confirmLabel: pending.action === 'pause' ? 'Pause delivery' : 'Resume delivery',
        busyLabel: 'Checking hook change…', cancelLabel: 'Cancel', onClose: () => { if (targetRef.current === pending) cancel() }, onConfirm: () => confirm(pending) }) : null
    ] }) })
}

function Page() {
  const [since, setSince] = useState('7d')
  const focus = useFocus()
  // The loop the chip reported: read once at mount, then followed through the same observed key,
  // so a handoff to an already-open page moves it too.
  const [chosen, setChosen] = useState(() => (focus.data && focus.data.loop) || '')
  // The drawer remembers which loop it was opened on, so switching loops closes it rather than
  // showing another loop's record for the same PR number.
  const [openPr, setOpenPr] = useState(null)
  // The run the operator asked to re-arm, holding the exact target the confirmation names; the
  // backend's answer to the last attempt; and whether one is in flight (no double submission).
  const [pendingRetry, setPendingRetry] = useState(null)
  const [retryAnswer, setRetryAnswer] = useState(null)
  // A ref, not state: two clicks in the same tick both read the render's state, and only a ref
  // that is set synchronously stops the second request (#371: no double submission).
  const retryInFlight = useRef(false)
  const loops = useLoops()
  const overview = useOverview()
  const configured = (loops.data && loops.data.loops) || []
  // A loop whose config file did not load (#701) is shown by name and reason, but it is never
  // selectable: it has no repo, so every per-loop read for it would only fail.
  const list = configured.filter(l => !l.unreadable)
  const unreadable = configured.filter(l => l.unreadable)
  useEffect(() => {
    const id = focus.data && focus.data.loop
    if (id) setChosen(id)
  }, [focus.data])
  const loop = chosen || (list[0] && list[0].id) || ''
  const stats = useQuery({
    queryKey: [ID, 'stats', loop, since],
    queryFn: () => readView('/stats?since=' + since + '&loop=' + encodeURIComponent(loop), 'stats'),
    refetchInterval: 60000,
    enabled: !!loop
  })
  const now = useNow(loop)

  const turns = (stats.data && stats.data.turns) || {}
  const seats = Object.keys(turns).sort((a, b) => {
    const ia = SEATS.indexOf(a), ib = SEATS.indexOf(b)
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)
  })
  const ran = seat => (turns[seat] && turns[seat].ran) || null
  const cards = []
  ;['reviewer', 'issue_fixer', 'fixer'].forEach(seat => {
    const r = ran(seat)
    if (r) cards.push(jsx(Card, { value: duration(r.median), label: 'median ' + seat.replace('_', ' ') + ' turn' }, seat))
  })
  const total = seats.reduce((n, seat) => n + turns[seat].turns, 0)
  cards.unshift(jsx(Card, { value: String(total), label: 'seat turns' }, 'total'))

  const seatRows = seats.map(seat => {
    const t = turns[seat], s = t.states || {}, r = t.ran, w = t.waited
    return [seat, String(t.turns), String(s.succeeded || 0), String(s.failed || 0),
      String(s.waiting || 0), otherStates(s), String(t.retried), duration(r && r.median), duration(r && r.max),
      duration(w && w.median)]
  })
  const runs = (now.data && now.data.runs) || []
  const loopsState = useViewState(loops, !configured.length)
  const overviewState = useViewState(overview, false, 60000)
  const nowState = useViewState(now, !runs.length && !Object.values(now.data?.counts || {}).some(n => n > 0), 60000)
  // /now carries whether its own ledger could be read; a missing ledger renders unavailable,
  // never a verified-empty "nothing running".
  const nowNoLedger = now.data?.available === false
  const nowDisplayState = nowNoLedger ? 'unavailable' : nowState
  const statsReadState = useViewState(stats, total === 0)
  const noLedger = stats.data?.turns === null
  const statsState = noLedger ? 'unavailable' : statsReadState

  // Every Retry click goes through the SDK's ConfirmDialog, which names the real target: the loop,
  // PR, seat and head the backend would re-arm. The handler throws the backend's own refusal so the
  // dialog keeps it in view; on success the page shows the host's answer and the state read back,
  // and invalidates the reads that are now out of date.
  const askRetry = run => {
    if (!run.retry || !run.retry.eligible) return
    setRetryAnswer(null)
    setPendingRetry({ loop, pr: run.pr, seat: run.seat, head: run.retry.head, state: run.state })
  }
  const confirmRetry = async () => {
    const target = pendingRetry
    if (!target || retryInFlight.current) return
    retryInFlight.current = true
    try {
      const answer = await postRetry({ loop: target.loop, pr: target.pr, seat: target.seat,
        head: target.head })
      if (!verifiedRetry(answer)) throw invalidResponse('retry')
      setRetryAnswer({ ok: true, text: answer.answer,
        runs: (answer.runs || []).map(run => run.seat + ' ' + run.state).join(' · ') })
      queryClient.invalidateQueries({ queryKey: [ID, 'now', target.loop] })
      queryClient.invalidateQueries({ queryKey: [ID, 'pr', target.loop, target.pr] })
      queryClient.invalidateQueries({ queryKey: [ID, 'overview'] })
    } catch (error) {
      const failure = retryFailure(error)
      const text = error?.code === 'INVALID_RESPONSE'
        ? 'The retry answer could not be verified. Read the run again.'
        : failure.detail || 'Retry failed: the backend refused or did not answer.'
      setRetryAnswer({ ok: false, runs: '',
        text: (failure.status ? 'HTTP ' + failure.status + ' · ' : '') + text })
      throw new Error(text)
    } finally {
      retryInFlight.current = false
    }
  }
  const retryTarget = pendingRetry
  // Mounted only while a target exists: a closed dialog in the tree would put its text into the
  // page for a reader (and a test) to trip over.
  const retryDialog = retryTarget ? jsx(ConfirmDialog, {
    open: true,
    title: 'Re-arm this run?',
    // The target first, then what it means; the equivalent command on its own line (the dialog
    // keeps paragraph breaks) so a long command is never broken mid-flag by wrapping.
    description: 'Retry the ' + retryTarget.seat + ' on #' + retryTarget.pr + ' @ ' +
        String(retryTarget.head).slice(0, 7) + ' in loop ' + retryTarget.loop +
        '. It re-runs that seat at that head, under the loop budget in force now, and asks the ' +
        'host to start a worker. A run that may have written is refused, and the host says ' +
        'why.\nSame as: hermes dk retry --loop ' + retryTarget.loop + ' --pr ' +
        retryTarget.pr + ' --seat ' + retryTarget.seat,
    confirmLabel: 'Retry run',
    busyLabel: 'Re-arming…',
    cancelLabel: 'Cancel',
    onClose: () => setPendingRetry(null),
    onConfirm: confirmRetry
  }) : null
  const retryAnswerLine = retryAnswer
    ? jsxs('div', {
        role: 'status',
        className:
          'flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded-md border border-(--ui-stroke-secondary) ' +
          'px-2 py-1.5 text-xs',
        children: [
          jsx('span', {
            className: 'font-medium ' + (retryAnswer.ok ? 'text-(--ui-accent)' : 'text-(--ui-red)'),
            children: retryAnswer.ok ? 'Re-armed' : 'Retry refused'
          }),
          jsx('span', {
            className: 'min-w-0 flex-1 break-words text-(--ui-text-secondary)',
            children: retryAnswer.text
          }),
          retryAnswer.runs
            ? jsx('span', {
                className: 'font-mono text-(--ui-text-quaternary)',
                children: 'now: ' + retryAnswer.runs
              })
            : null
        ]
      })
    : null

  const header = jsxs('div', {
    className: 'flex flex-wrap items-center gap-2',
    children: [
      jsx('h1', { className: 'text-base font-medium', children: 'Diaktoros' }),
      list.length > 1 && jsx('div', {
        className: 'flex gap-1',
        children: list.map(l => jsx('button', {
          type: 'button',
          className: 'rounded px-2 py-0.5 text-xs ' + (l.id === loop ? 'text-(--ui-accent)' : 'text-(--ui-text-tertiary)'),
          onClick: () => setChosen(l.id),
          children: l.id
        }, l.id))
      }),
      jsx('div', { className: 'flex-1' }),
      jsx('div', {
        className: 'flex gap-1',
        children: SINCE.map(s => jsx('button', {
          type: 'button',
          className: 'rounded px-2 py-0.5 text-xs ' + (s === since ? 'text-(--ui-accent)' : 'text-(--ui-text-tertiary)'),
          onClick: () => setSince(s),
          children: s
        }, s))
      })
    ]
  })

  // Names every loop whose config did not load, with the host's bounded reason: the operator's
  // next action is to fix the file, which is not what ``not read`` (wait/retry) asks for.
  const unreadableBlock = unreadable.length
    ? jsx(Section, {
        title: 'Config unreadable',
        children: jsx(Muted, { text: 'Could not load the config of: ' +
          unreadable.map(l => l.id + (l.reason ? ' (' + l.reason + ')' : '')).join(', ') +
          '. Fix the file, then run: hermes dk list.' })
      })
    : null
  let body
  if (!list.length) body = jsxs('div', {
    className: 'flex flex-col gap-4',
    children: [jsx(QueryStatus, { query: loops, state: loopsState, label: 'Configured loops' }),
      unreadableBlock,
      loopsState === 'verified-empty' ? jsx(Muted, { text: 'No loops configured. Run: hermes dk setup, in a terminal.' }) : null]
  })
  else body = jsxs('div', {
    className: 'flex flex-col gap-5',
    children: [
      loopsState === 'stale' ? jsx(QueryStatus, { query: loops, state: loopsState, label: 'Configured loops' }) : null,
      unreadableBlock,
      configured.length > 1
        ? jsx(NeedsYou, { overview, state: overviewState, onPick: setChosen })
        : null,
      jsx(Section, {
        title: 'Right now',
        children: jsxs('div', {
          className: 'flex flex-col gap-2',
          children: [
            jsx(QueryStatus, { query: now, state: nowDisplayState, label: 'Right now',
              noLedger: nowNoLedger ? 'No run ledger available in the last successful backend read. Run counts are unavailable, not zero.' : false }),
            now.data && !nowNoLedger ? jsx(Muted, { text: summary(now.data.counts) }) : null,
            jsx(Pacing, { pacing: now.data && now.data.pacing }),
            retryAnswerLine, runs.length
          ? jsxs('div', {
              className: 'flex flex-col',
              children: [
                jsx('div', {
                  className: 'rounded-md border border-(--ui-stroke-secondary)',
                  children: runs.map(run => jsx(RunRow, {
                    run,
                    onOpen: pr => setOpenPr({ loop, pr }),
                    onRetry: askRetry
                  }, run.pr + run.seat + run.head + run.state))
                }),
                jsx(Muted, { text: 'Click a row for what would move it. Retry re-arms a failed run that never wrote; it asks first.' })
              ]
            })
            : nowDisplayState === 'verified-empty'
              ? jsx(Muted, { text: 'No active or recent failed runs recorded in this backend read.' }) : null]
        })
      }),
      jsx(Section, {
        title: 'Seat turns (' + since + ')',
        children: jsxs('div', {
          className: 'flex flex-col gap-2',
          children: [
            jsx(QueryStatus, { query: stats, state: statsState, label: 'Seat turns · ' + since, noLedger }),
            stats.data && !noLedger ? jsx('div', { className: 'grid grid-cols-2 gap-2 sm:grid-cols-4', children: cards }) : null,
            seatRows.length
            ? jsx(Table, {
                head: ['seat', 'turns', 'ok', 'failed', 'waiting', 'other', 'retried', 'ran (median)', 'ran (max)', 'waited (median)'],
                rows: seatRows
              })
            : statsState === 'verified-empty' ? jsx(Muted, { text: 'No turns in this window.' }) : null,
            jsx(Muted, {
              text: 'The table is the window above; today’s counts and any closed usage window are in Right now.'
            })]
        })
      }),
      jsx(HooksControl, { loop, discoveryCurrent: loopsState === 'verified' && !loops.isFetching && list.some(item => item.id === loop) }),
      jsx(Health, { loop }),
      jsx(Muted, { text: 'Writes require confirmation: Retry re-arms a run that never wrote; Pause/Resume changes webhook delivery only. Data is this machine\'s run ledger; for PRs and reviews from GitHub, run: hermes dk stats --github' })
    ]
  })

  return jsxs('div', {
    className: 'flex h-full flex-col gap-4 overflow-y-auto p-4 text-sm',
    children: [
      header,
      body,
      jsx(Settings, {}),
      retryDialog,
      openPr && openPr.loop === loop
        ? jsx(Drawer, { loop, pr: openPr.pr, onClose: () => setOpenPr(null) }, 'drawer-' + openPr.loop + '-' + openPr.pr)
        : null
    ]
  })
}

/** The palette rows read the same cache the page paints from, through the app's one query
 *  client — no second poll of the backend. */
function liveDetail() {
  // The chip answers for the whole install from its own read; the palette shares it first, then
  // falls back to a page's single-loop read. Still clock-safe: freshness goes through the same
  // tracked read, never a wall-clock comparison here.
  for (const query of queryClient.getQueryCache().findAll({ queryKey: [ID, 'overview'] })) {
    if (query.state.data && Array.isArray(query.state.data.loops)) {
      const q = { ...query.state, isError: query.state.status === 'error' }
      const total = needsYouTotal(q.data.loops)
      return trackedViewState(q, total === 0, 60000) + ' · ' + total + ' need you · Last good read: ' + lastGood(q)
    }
  }
  for (const query of queryClient.getQueryCache().findAll({ queryKey: [ID, 'now'] })) {
    if (query.state.data && query.state.data.counts) {
      const q = { ...query.state, isError: query.state.status === 'error' }
      return trackedViewState(q, !Object.values(q.data.counts).some(n => n > 0), 60000) + ' · ' + summary(q.data.counts) + ' · Last good read: ' + lastGood(q)
    }
  }
  return 'not read yet'
}

export default {
  id: ID,
  name: 'Diaktoros',
  defaultEnabled: false,
  register(ctx) {
    rest = ctx.rest
    storage = ctx.storage
    const cache = queryClient.getQueryCache()
    // TanStack emits added (including initial/hydrated data) and updated after
    // replacing query.state. Subscribe before seeding already-cached reads.
    const unsubscribe = cache.subscribe(event => {
      if (event.type === 'added' || event.type === 'updated') observeFreshness(event.query)
    })
    cache.findAll({ queryKey: [ID] }).forEach(observeFreshness)
    ctx.onDispose(() => {
      unsubscribe()
      freshness = new WeakMap()
      rest = null
      storage = null
    })
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(Page, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label: 'Diaktoros', codicon: 'git-pull-request' } },
      { id: 'status', area: STATUSBAR_AREAS.right, order: 140, render: () => jsx(StatusChip, {}) },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'diaktoros.open',
          label: 'Diaktoros: open the loop page',
          keywords: ['diaktoros', 'loop', 'review', 'diaktoros page'],
          detail: liveDetail,
          detailVariant: 'state',
          run: () => host.navigate(PATH)
        }
      },
      {
        id: 'refresh',
        area: PALETTE_AREA,
        data: {
          id: 'diaktoros.refresh',
          label: 'Diaktoros: refresh now',
          keywords: ['diaktoros', 'refresh', 'reload', 'loop'],
          detail: liveDetail,
          detailVariant: 'state',
          keepOpen: true,
          run: () => queryClient.invalidateQueries({ queryKey: [ID] })
        }
      },
      {
        id: 'health',
        area: PALETTE_AREA,
        data: {
          id: 'diaktoros.health',
          label: 'Diaktoros: re-run the health checks',
          keywords: ['diaktoros', 'health', 'doctor', 'preflight'],
          run: () => {
            queryClient.invalidateQueries({ queryKey: [ID, 'health'] })
            host.navigate(PATH)
          }
        }
      }
    ])
  }
}
