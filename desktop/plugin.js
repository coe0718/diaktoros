// diaktoros: the Hermes Desktop half (#369, #630). A read-only Diaktoros page, a status-bar
// summary, a drawer showing what would move one PR, and the installation's own preflight —
// all fed by this plugin's own backend (dashboard/plugin_api.py) through ctx.rest. Plain ESM,
// loaded uncompiled: jsx() calls, not JSX syntax, and only the three specifiers the loader
// allows. Theme variables only, never a hardcoded color.
import {
  Badge,
  Button,
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
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'diaktoros'
const PATH = '/diaktoros'
const SINCE = ['24h', '7d', '30d']
const SEATS = ['reviewer', 'fixer', 'issue_fixer', 'triage', 'adjudicator']
// Where the chip remembers the last "needs you" count it announced, so a toast fires on a
// change and never on the first read after a reload.
const NEEDS_YOU_KEY = 'needsYouCount'
// The health view is the slow one (it stats profiles, the cron store and the venvs), so it is
// read once and kept, not polled like the run views.
const HEALTH_STALE_MS = 600000

// Set in register(), cleared on dispose: components reach the backend through them.
let rest = null
let storage = null

function call(path) {
  if (!rest) return Promise.reject(new Error('plugin not loaded'))
  return rest(path)
}

function useLoops() {
  return useQuery({ queryKey: [ID, 'loops'], queryFn: () => call('/loops'), staleTime: 60000 })
}

function useNow(loop) {
  return useQuery({
    queryKey: [ID, 'now', loop || ''],
    queryFn: () => call('/now' + (loop ? '?loop=' + encodeURIComponent(loop) : '')),
    refetchInterval: 30000
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

function summary(counts) {
  const parts = []
  if (counts.running) parts.push(counts.running + ' running')
  if (counts.held) parts.push(counts.held + ' held')
  if (counts.uncertain) parts.push(counts.uncertain + ' need you')
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
    children: [error ? 'Not reachable: ' + String(error.message || error) + ' — ' : '', children]
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

/** One live run, clickable: the drawer answers "why is this not moving". */
function RunRow({ run, onOpen }) {
  return jsxs('button', {
    type: 'button',
    className:
      'flex w-full items-center gap-3 border-b border-(--ui-stroke-secondary) px-2 py-1.5 text-left text-xs ' +
      'last:border-b-0 hover:text-(--ui-accent)',
    onClick: () => onOpen(run.pr),
    children: [
      jsx('span', { className: 'w-14 shrink-0 font-mono text-(--ui-accent)', children: '#' + run.pr }),
      jsx('span', { className: 'w-20 shrink-0 text-(--ui-text-tertiary)', children: run.seat }),
      jsx('span', { className: 'w-16 shrink-0 font-mono text-(--ui-text-quaternary)', children: run.head }),
      jsx(Badge, { size: 'xs', variant: 'muted', children: run.state }),
      jsx('span', { className: 'min-w-0 flex-1 truncate text-(--ui-text-tertiary)', children: run.why })
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
  const verdicts = (d && d.verdicts) || {}
  const counts = Object.keys(verdicts)
    .sort()
    .map(name => verdicts[name] + ' ' + name)
    .join(' · ')

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
                jsx(Section, { title: 'Why it is not moving', children: why }),
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
                            'Adjudicator rulings: ' +
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
                  title: 'Turns (' + turns.length + ')',
                  children: turns.length
                    ? jsx(Table, {
                        head: ['when', 'seat', 'head', 'state'],
                        rows: turns
                          .slice()
                          .reverse()
                          .map(t => [clock(t.created), t.seat, t.head, t.state])
                      })
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

function StatusChip() {
  const loops = useLoops()
  const first = loops.data && loops.data.loops && loops.data.loops[0]
  const now = useNow(first ? first.id : '')
  const counts = (now.data && now.data.counts) || {}
  // Announce a rise in what is waiting on the operator, once. The first read of a session only
  // records the baseline: a reload is not news.
  useEffect(() => {
    if (!now.data) return
    const count = counts.uncertain || 0
    const seen = storage ? storage.get(NEEDS_YOU_KEY) : null
    if (typeof seen === 'number' && count > seen) {
      host.notify({ kind: 'warn', message: 'Diaktoros: ' + count + ' run(s) need you' })
    }
    if (storage) storage.set(NEEDS_YOU_KEY, count)
  }, [now.data])
  const text = now.data ? summary(counts) : now.isError ? '?' : '…'
  return jsx('button', {
    type: 'button',
    className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    title: 'Diaktoros',
    onClick: () => host.navigate(PATH),
    children: 'loop: ' + text
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

function Page() {
  const [since, setSince] = useState('7d')
  const [chosen, setChosen] = useState('')
  const [openPr, setOpenPr] = useState(null)
  const loops = useLoops()
  const list = (loops.data && loops.data.loops) || []
  const loop = chosen || (list[0] && list[0].id) || ''
  const stats = useQuery({
    queryKey: [ID, 'stats', loop, since],
    queryFn: () => call('/stats?since=' + since + (loop ? '&loop=' + encodeURIComponent(loop) : '')),
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

  let body
  if (loops.isError) body = jsx(Muted, { text: 'The loop backend is not reachable. Enable the plugin\'s Python half (plugins.enabled in config.yaml) and restart the gateway.' })
  else if (!loops.isLoading && !list.length) body = jsx(Muted, { text: 'No loops configured. Run: hermes dk setup, in a terminal.' })
  else body = jsxs('div', {
    className: 'flex flex-col gap-5',
    children: [
      jsx('div', { className: 'grid grid-cols-2 gap-2 sm:grid-cols-4', children: cards }),
      jsx(Section, {
        title: 'Right now',
        children: runs.length
          ? jsxs('div', {
              className: 'flex flex-col',
              children: [
                jsx('div', {
                  className: 'rounded-md border border-(--ui-stroke-secondary)',
                  children: runs.map(run => jsx(RunRow, {
                    run,
                    onOpen: pr => setOpenPr(pr)
                  }, run.pr + run.seat + run.head + run.state))
                }),
                jsx(Muted, { text: 'Click a row for what would move it.' })
              ]
            })
          : jsx(Muted, { text: now.isLoading ? 'Loading…' : 'Nothing running, held or waiting on you.' })
      }),
      jsx(Section, {
        title: 'Seat turns (' + since + ')',
        children: seatRows.length
          ? jsx(Table, {
              head: ['seat', 'turns', 'ok', 'failed', 'waiting', 'other', 'retried', 'ran (median)', 'ran (max)', 'waited (median)'],
              rows: seatRows
            })
          : jsx(Muted, { text: stats.isLoading ? 'Loading…' : 'No turns in this window.' })
      }),
      jsx(Health, { loop }),
      jsx(Muted, { text: 'Read-only, from this machine\'s run ledger. For PRs and reviews from GitHub, run: hermes dk stats --github' })
    ]
  })

  return jsxs('div', {
    className: 'flex h-full flex-col gap-4 overflow-y-auto p-4 text-sm',
    children: [
      header,
      body,
      openPr
        ? jsx(Drawer, { loop, pr: openPr, onClose: () => setOpenPr(null) }, 'drawer-' + openPr)
        : null
    ]
  })
}

/** The palette rows read the same cache the page paints from, through the app's one query
 *  client — no second poll of the backend. */
function liveDetail() {
  for (const query of queryClient.getQueryCache().findAll({ queryKey: [ID, 'now'] })) {
    if (query.state.data && query.state.data.counts) return summary(query.state.data.counts)
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
    ctx.onDispose(() => {
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
