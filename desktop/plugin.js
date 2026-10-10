// diaktoros: the Hermes Desktop half (#369). A read-only Diaktoros page and a
// status-bar summary, fed by this plugin's own backend (dashboard/plugin_api.py) through
// ctx.rest. Plain ESM, loaded uncompiled: jsx() calls, not JSX syntax, and only the three
// specifiers the loader allows. Theme variables only, never a hardcoded color.
import { host, useQuery, ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'diaktoros'
const PATH = '/diaktoros'
const SINCE = ['24h', '7d', '30d']
const SEATS = ['reviewer', 'fixer', 'issue_fixer', 'triage', 'adjudicator']

// Set in register(), cleared on dispose: components reach the backend through it.
let rest = null

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

function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 90) return Math.round(seconds) + ' s'
  if (seconds < 5400) return (seconds / 60).toFixed(1) + ' min'
  if (seconds < 172800) return (seconds / 3600).toFixed(1) + ' h'
  return (seconds / 86400).toFixed(1) + ' d'
}

function summary(counts) {
  const parts = []
  if (counts.running) parts.push(counts.running + ' running')
  if (counts.held) parts.push(counts.held + ' held')
  if (counts.uncertain) parts.push(counts.uncertain + ' need you')
  if (counts.failed) parts.push(counts.failed + ' failed')
  return parts.length ? parts.join(' · ') : 'idle'
}

function StatusChip() {
  const loops = useLoops()
  const first = loops.data && loops.data.loops && loops.data.loops[0]
  const now = useNow(first ? first.id : '')
  const text = now.data ? summary(now.data.counts || {}) : now.isError ? '?' : '…'
  return jsx('button', {
    type: 'button',
    className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    title: 'Diaktoros',
    onClick: () => host.navigate(PATH),
    children: 'loop: ' + text
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

function Page() {
  const [since, setSince] = useState('7d')
  const [chosen, setChosen] = useState('')
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
      String(s.waiting || 0), String(t.retried), duration(r && r.median), duration(r && r.max),
      duration(w && w.median)]
  })
  const runs = (now.data && now.data.runs) || []
  const runRows = runs.map(run => ['#' + run.pr, run.seat, run.head, run.state, run.why])

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
  else if (!loops.isLoading && !list.length) body = jsx(Muted, { text: 'No loops configured. Run `hermes dk setup` in a terminal.' })
  else body = jsxs('div', {
    className: 'flex flex-col gap-5',
    children: [
      jsx('div', { className: 'grid grid-cols-2 gap-2 sm:grid-cols-4', children: cards }),
      jsx(Section, {
        title: 'Right now',
        children: runRows.length
          ? jsx(Table, { head: ['PR', 'seat', 'head', 'state', 'why'], rows: runRows })
          : jsx(Muted, { text: now.isLoading ? 'Loading…' : 'Nothing running, held or waiting on you.' })
      }),
      jsx(Section, {
        title: 'Seat turns (' + since + ')',
        children: seatRows.length
          ? jsx(Table, {
              head: ['seat', 'turns', 'ok', 'failed', 'waiting', 'retried', 'ran (median)', 'ran (max)', 'waited (median)'],
              rows: seatRows
            })
          : jsx(Muted, { text: stats.isLoading ? 'Loading…' : 'No turns in this window.' })
      }),
      jsx(Muted, { text: 'Read-only, from this machine\'s run ledger. For PRs and reviews from GitHub, run `hermes dk stats --github`.' })
    ]
  })

  return jsxs('div', { className: 'flex h-full flex-col gap-4 overflow-y-auto p-4 text-sm', children: [header, body] })
}

export default {
  id: ID,
  name: 'Diaktoros',
  defaultEnabled: false,
  register(ctx) {
    rest = ctx.rest
    ctx.onDispose(() => { rest = null })
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(Page, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label: 'Diaktoros', codicon: 'git-pull-request' } },
      { id: 'status', area: STATUSBAR_AREAS.right, order: 140, render: () => jsx(StatusChip, {}) }
    ])
  }
}
