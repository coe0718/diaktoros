// Executable component tests with explicitly synthetic query/SDK fixtures, not live data.
// No dependencies or changes to the production import surface. Run with --experimental-vm-modules.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import test from 'node:test'

const source = await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
// Pinned from real stats.collect with a missing disposable ledger; Python checks equality.
const noLedgerStats = JSON.parse(await readFile(new URL('./fixtures/desktop_no_ledger_stats.json', import.meta.url), 'utf8'))

async function harness() {
  const registrations = []
  const queries = {}
  const options = {}
  const calls = []
  const notices = []
  const navigations = []
  const effects = []
  const states = []
  const timers = new Map()
  let time = 1800000000000
  let elapsed = 0
  let timerId = 0
  let cursor = 0
  let effectCursor = 0
  let mountedId = null
  let currentTree = null
  class FakeDate extends Date {
    constructor(...args) { super(...(args.length ? args : [time])) }
    static now() { return time }
  }
  const setTimeout = (fn, delay) => { const id = ++timerId; timers.set(id, { fn, at: elapsed + delay }); return id }
  const clearTimeout = id => timers.delete(id)
  const store = new Map()
  let response = {}
  const good = data => ({ data, dataUpdatedAt: time, isError: false, isFetching: false,
    isLoading: false, refetch: () => calls.push('refetch') })
  queries.loops = good({ loops: [{ id: 'fixture-loop', repo: 'fixture/repo' }] })
  queries.now = good({ runs: [], counts: {}, pacing: {} })
  queries.stats = good({ turns: {} })
  queries.health = good({ checks: [], counts: {}, offline: true })
  const element = (type, props, key) => ({ type, props: props || {}, key })
  const ui = name => props => element(name, props)
  const sdk = Object.fromEntries(['Badge', 'Button', 'Dialog', 'DialogContent', 'DialogDescription',
    'DialogHeader', 'DialogTitle', 'StatusDot'].map(name => [name, ui(name)]))
  Object.assign(sdk, {
    PALETTE_AREA: 'palette', ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'nav', STATUSBAR_AREAS: { right: 'status' },
    host: { notify: n => notices.push(n), navigate: p => navigations.push(p) },
    queryClient: { getQueryCache: () => ({ findAll: () => [] }), invalidateQueries: args => calls.push(args) },
    useQuery: opts => { options[opts.queryKey[1]] = opts; return queries[opts.queryKey[1]] }
  })
  const react = {
    useEffect: (fn, deps) => {
      const index = effectCursor++
      const old = effects[index]
      if (!old || deps.some((value, i) => !Object.is(value, old.deps[i]))) {
        old?.cleanup?.()
        effects[index] = { deps, pending: fn }
      }
    },
    useState: initial => {
      const index = cursor++
      if (!(index in states)) states[index] = initial
      return [states[index], value => {
        states[index] = typeof value === 'function' ? value(states[index]) : value
        if (mountedId) render(mountedId)
      }]
    }
  }
  const context = vm.createContext({ console, Date: FakeDate, Promise, Error, setTimeout, clearTimeout })
  const module = new vm.SourceTextModule(source, { context })
  await module.link(specifier => {
    const values = specifier === '@hermes/plugin-sdk' ? sdk : specifier === 'react' ? react : { jsx: element, jsxs: element }
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value)
    }, { context })
  })
  await module.evaluate()
  module.namespace.default.register({
    rest: path => { calls.push(path); return Promise.resolve(response) },
    storage: { get: key => store.get(key), set: (key, value) => store.set(key, value) },
    onDispose: () => {}, registerMany: rows => registrations.push(...rows)
  })
  const resolve = node => {
    if (Array.isArray(node)) return node.flatMap(resolve)
    if (node == null || node === false) return []
    if (typeof node !== 'object') return [node]
    if (typeof node.type === 'function') return resolve(node.type(node.props))
    return [{ ...node, children: resolve(node.props.children) }]
  }
  const render = id => {
    if (mountedId !== id) {
      effects.forEach(effect => effect.cleanup?.())
      effects.length = 0
      states.length = 0
    }
    mountedId = id
    cursor = 0
    effectCursor = 0
    currentTree = resolve(registrations.find(r => r.id === id).render())
    effects.forEach(effect => {
      if (effect.pending) {
        const fn = effect.pending
        effect.pending = null
        effect.cleanup = fn()
      }
    })
    return currentTree
  }
  const flatten = nodes => nodes.flatMap(n => typeof n === 'object' ? [n, ...flatten(n.children)] : [n])
  const text = tree => flatten(tree).filter(n => typeof n !== 'object').join(' ')
  const buttons = tree => flatten(tree).filter(n => n.type === 'Button' || n.type === 'button')
  const fail = data => ({ ...good(data), isError: true, error: new Error('fixture backend offline') })
  const loading = () => ({ ...good(undefined), isLoading: true, isFetching: true })
  return { queries, options, calls, notices, navigations, store, good, fail, loading,
    render, text, buttons, flatten, respond: value => { response = value },
    tree: () => currentTree, timerCount: () => timers.size,
    unmount: () => { effects.forEach(effect => effect.cleanup?.()); mountedId = null },
    moveWall: ms => { time += ms },
    advance: (ms, wallMs = ms) => {
      elapsed += ms
      time += wallMs
      for (const [id, timer] of [...timers]) {
        if (timer.at <= elapsed) { timers.delete(id); timer.fn() }
      }
    } }
}

for (const kind of ['now', 'stats']) {
  test(`page ${kind}: first load never claims empty or zero`, async () => {
    const h = await harness()
    h.queries[kind] = h.loading()
    const text = h.text(h.render('page'))
    assert.match(text, /loading/)
    assert.doesNotMatch(text, kind === 'now' ? /No active or recent failed/ : /No turns in this window|seat turns/)
  })
  test(`page ${kind}: initial failure is unavailable and retry executes`, async () => {
    const h = await harness()
    let retried = 0
    h.queries[kind] = { ...h.fail(undefined), refetch: () => retried++ }
    const tree = h.render('page')
    const text = h.text(tree)
    assert.match(text, /unavailable/)
    assert.match(text, /Backend read failed/)
    assert.doesNotMatch(text, /fixture backend offline/)
    assert.doesNotMatch(text, kind === 'now' ? /No active or recent failed/ : /No turns in this window|seat turns/)
    // Each section has its own callback; retrying one must not refetch the other.
    h.buttons(tree).filter(n => h.text([n]) === 'Retry')[kind === 'now' ? 0 : 1].props.onClick()
    assert.equal(retried, 1)
  })
  test(`page ${kind}: cached failure keeps data and last good timestamp but not empty claim`, async () => {
    const h = await harness()
    h.queries[kind] = h.fail(h.queries[kind].data)
    const text = h.text(h.render('page'))
    assert.match(text, /stale/)
    assert.match(text, /showing last good data, not current status/)
    assert.match(text, /Last good read:/)
    assert.doesNotMatch(text, kind === 'now' ? /No active or recent failed/ : /No turns in this window/)
    if (kind === 'stats') assert.match(text, /seat turns/)
  })
  test(`page ${kind}: recovered successful empty is explicitly verified`, async () => {
    const h = await harness()
    h.queries[kind] = h.fail(undefined)
    h.render('page')
    h.queries[kind] = h.good(kind === 'now' ? { runs: [], counts: {} } : { turns: {} })
    const text = h.text(h.render('page'))
    assert.match(text, /verified-empty/)
    assert.doesNotMatch(text, /unavailable|fixture backend offline/)
    assert.match(text, kind === 'now' ? /No active or recent failed/ : /No turns in this window/)
  })
  test(`${kind}: expired cached read is stale without a fetch error`, async () => {
    const h = await harness()
    h.queries[kind].dataUpdatedAt -= 180000
    assert.match(h.text(h.render('page')), /stale/)
  })
  test(`${kind}: malformed successful response rejects instead of empty`, async () => {
    const h = await harness()
    h.render('page')
    h.respond({})
    await assert.rejects(h.options[kind].queryFn(), /Invalid/)
    h.respond(kind === 'now' ? { runs: [], counts: {} } : { turns: {} })
    await h.options[kind].queryFn()
    assert.match(h.calls.at(-1), /loop=fixture-loop/)
  })
}

test('real missing-ledger stats payload is unavailable, not malformed or verified zero', async () => {
  const h = await harness()
  h.render('page')
  h.respond(noLedgerStats)
  const data = await h.options.stats.queryFn()
  h.queries.stats = h.good(data)
  h.render('page')
  const section = () => h.flatten(h.tree()).find(n => n.type === 'section' &&
    n.children.some(c => c.type === 'h2' && h.text([c]) === 'Seat turns (7d)'))
  // Restrict assertions to stats: Right now may independently be verified-empty.
  const statsText = () => h.text([section()])
  assert.match(statsText(), /unavailable.*No run ledger available/)
  assert.doesNotMatch(statsText(), /verified-empty|No turns in this window|0 seat turns|could not be verified/)
  h.advance(120000)
  assert.match(statsText(), /unavailable.*No run ledger available/)
  h.queries.stats = { ...h.queries.stats, isError: true, error: new Error('offline') }
  h.render('page')
  assert.match(statsText(), /Backend read failed/)
  assert.doesNotMatch(statsText(), /0 seat turns|No turns in this window/)
  h.queries.stats = h.good({ turns: {} })
  h.render('page')
  assert.match(statsText(), /verified-empty.*0 seat turns.*No turns in this window/)
  assert.doesNotMatch(statsText(), /No run ledger available/)
})

test('chip: loading, unavailable, cached failure, recovery, timestamps and retry', async () => {
  const h = await harness()
  h.queries.now = h.loading()
  assert.match(h.text(h.render('status')), /loop: loading/)
  h.queries.now = h.fail(undefined)
  let tree = h.render('status')
  assert.match(h.text(tree), /loop: unavailable/)
  h.buttons(tree).find(n => h.text([n]) === 'Retry').props.onClick()
  assert.ok(h.calls.includes('refetch'))
  h.queries.now = h.fail({ runs: [], counts: { pending: 2, waiting: 3, uncertain: 4 } })
  tree = h.render('status')
  assert.match(h.text(tree), /stale · 2 pending · 3 retry-waiting · 4 need you/)
  assert.match(h.buttons(tree)[0].props.title, /Last good read:.*Backend read failed/)
  assert.doesNotMatch(h.buttons(tree)[0].props.title, /fixture backend offline/)
  assert.equal(h.notices.length, 0)
  assert.equal(h.store.size, 0)
  h.queries.now = h.good({ runs: [], counts: {} })
  tree = h.render('status')
  assert.match(h.text(tree), /verified-empty · idle/)
  h.buttons(tree)[0].props.onClick()
  assert.deepEqual(h.navigations, ['/diaktoros'])
})

test('pending and retry-waiting are not idle or verified-empty in page and chip', async () => {
  const h = await harness()
  h.queries.now = h.good({ runs: [], counts: { pending: 2, waiting: 3 } })
  for (const id of ['page', 'status']) {
    const text = h.text(h.render(id))
    assert.match(text, /2 pending · 3 retry-waiting/)
    assert.doesNotMatch(text, /No active or recent failed|idle/)
  }
})

test('loop discovery gates requests; failure and successful no-loops are distinct', async () => {
  const h = await harness()
  for (const id of ['page', 'status']) {
    h.queries.loops = h.loading()
    assert.match(h.text(h.render(id)), /loading/)
    assert.equal(h.options.now.enabled, false)
    h.queries.loops = h.fail(undefined)
    const tree = h.render(id)
    assert.match(h.text(tree), /unavailable/)
    assert.doesNotMatch(h.text(tree), /No loops configured|idle/)
    h.buttons(tree).find(n => h.text([n]) === 'Retry').props.onClick()
    h.queries.loops = h.good({ loops: [] })
    assert.match(h.text(h.render(id)), /verified-empty/)
    assert.equal(h.options.now.enabled, false)
  }
  assert.equal(h.options.stats.enabled, false)
  h.respond({})
  await assert.rejects(h.options.loops.queryFn(), /Invalid loops/)
})

test('nonempty cached rows and stats remain visible after failed refresh', async () => {
  const h = await harness()
  h.queries.now = h.fail({ runs: [{ pr: 42, seat: 'reviewer', head: 'fixture', state: 'waiting', why: 'waiting to retry' }], counts: { waiting: 1 } })
  h.queries.stats = h.fail({ turns: { reviewer: { turns: 7, states: { waiting: 2 }, retried: 1 } } })
  const tree = h.render('page')
  assert.match(h.text(tree), /#42/)
  assert.match(h.text(tree), /7 seat turns/)
  assert.equal(h.flatten(tree).filter(n => n.type === 'div' && n.props.role === 'alert').length, 2)
})

test('cached loop discovery failure is visible in page and chip', async () => {
  const h = await harness()
  h.queries.loops = h.fail(h.queries.loops.data)
  for (const id of ['page', 'status']) assert.match(h.text(h.render(id)), /stale/)
  h.queries.now = h.fail(undefined)
  assert.match(h.text(h.render('status')), /unavailable/)
  assert.doesNotMatch(h.text(h.render('status')), /idle/)
})

test('changing loop and window selects isolated query keys without ambiguous now calls', async () => {
  const h = await harness()
  h.queries.loops = h.good({ loops: [{ id: 'fixture-loop' }, { id: 'fixture second' }] })
  let tree = h.render('page')
  h.buttons(tree).find(n => h.text([n]) === 'fixture second').props.onClick()
  tree = h.render('page')
  assert.equal(h.options.now.queryKey[2], 'fixture second')
  assert.equal(h.options.stats.queryKey[2], 'fixture second')
  h.buttons(tree).find(n => h.text([n]) === '30d').props.onClick()
  h.render('page')
  assert.equal(h.options.stats.queryKey[3], '30d')
  h.respond({ runs: [], counts: {} })
  await h.options.now.queryFn()
  assert.equal(h.calls.at(-1), '/now?loop=fixture%20second')
})

test('last-good timestamp is retained through refresh failure, not replaced by error time', async () => {
  const h = await harness()
  const stamp = h.queries.now.dataUpdatedAt - 10000
  h.queries.now.dataUpdatedAt = stamp
  const date = new Date(stamp).toLocaleString()
  assert.ok(h.text(h.render('page')).includes(date))
  h.queries.now.isError = true
  h.queries.now.error = new Error('fixture later failure')
  assert.ok(h.text(h.render('page')).includes(date))
  assert.ok(h.buttons(h.render('status'))[0].props.title.includes(date))
})

test('refresh keeps verified data and timestamp while in flight; retry disabled', async () => {
  const h = await harness()
  h.queries.now.isFetching = true
  const tree = h.render('page')
  assert.match(h.text(tree), /refreshing…/)
  assert.equal(h.buttons(tree).find(n => h.text([n]) === 'Reading…').props.disabled, true)
})

test('chip notifications only follow successful verified reads', async () => {
  const h = await harness()
  h.queries.now = h.good({ runs: [], counts: { uncertain: 1 } })
  h.render('status')
  assert.equal(h.notices.length, 0)
  h.queries.now = h.fail({ runs: [], counts: { uncertain: 3 } })
  h.render('status')
  assert.equal(h.store.get('needsYouCount'), 1)
  h.queries.now = h.good({ runs: [], counts: { uncertain: 3 } })
  h.render('status')
  assert.equal(h.notices.length, 1)
})

for (const id of ['page', 'status']) {
  test(`${id}: elapsed expiry is sticky when wall clock moves backwards`, async () => {
    const h = await harness()
    h.queries.now.isFetching = true
    h.render(id)
    h.moveWall(-3600000)
    h.advance(60000, 0)
    assert.match(h.text(h.tree()), /stale/)
    assert.doesNotMatch(h.text(h.tree()), /No active or recent failed/)
    if (id === 'status') assert.doesNotMatch(h.text(h.tree()), /verified/)
    h.advance(120000, 0)
    assert.doesNotMatch(h.text(h.tree()), /verified-empty|No turns in this window/)
    assert.equal(h.timerCount(), 0)
    h.render(id)
    assert.doesNotMatch(h.text(h.tree()), /verified/)
    h.queries.now = h.good({ runs: [], counts: {} })
    h.queries.loops = h.good(h.queries.loops.data)
    h.queries.stats = h.good({ turns: {} })
    h.render(id)
    assert.match(h.text(h.tree()), /verified-empty/)
    h.advance(60000, -60000)
    assert.match(h.text(h.tree()), /stale/)
    h.unmount()
    assert.equal(h.timerCount(), 0)
  })
  test(`${id}: empty now expires at boundary while refresh hangs, without manual render`, async () => {
    const h = await harness()
    h.queries.now.isFetching = true
    h.render(id)
    assert.match(h.text(h.tree()), /verified-empty/)
    h.advance(59999)
    assert.doesNotMatch(h.text(h.tree()), /stale/)
    h.advance(1)
    assert.match(h.text(h.tree()), /stale/)
    assert.doesNotMatch(h.text(h.tree()), /No active or recent failed/)
    if (id === 'status') assert.doesNotMatch(h.text(h.tree()), /verified/)
    h.unmount()
    assert.equal(h.timerCount(), 0)
  })
  test(`${id}: verified-empty loop discovery expires with setup claim`, async () => {
    const h = await harness()
    h.queries.loops = h.good({ loops: [] })
    h.render(id)
    h.advance(120000)
    assert.match(h.text(h.tree()), /stale/)
    assert.doesNotMatch(h.text(h.tree()), /No loops configured|no loops configured|hermes dk setup/)
  })
}

test('page: stats empty claim expires without polling or manual render', async () => {
  const h = await harness()
  h.render('page')
  assert.match(h.text(h.tree()), /No turns in this window/)
  h.advance(120000)
  assert.doesNotMatch(h.text(h.tree()), /No turns in this window|verified-empty/)
})

test('expiry timer re-arms for a new successful timestamp and cleans up on failure', async () => {
  const h = await harness()
  h.render('status')
  h.advance(30000)
  h.queries.now = h.good({ runs: [], counts: {} })
  h.render('status')
  assert.equal(h.timerCount(), 2)
  h.advance(30000)
  assert.match(h.text(h.tree()), /verified-empty/)
  h.advance(30000)
  assert.match(h.text(h.tree()), /stale/)
  h.queries.now = h.fail(undefined)
  h.render('status')
  assert.equal(h.timerCount(), 1)
  h.unmount()
  assert.equal(h.timerCount(), 0)
})

test('sticky expiry resets on a timestamp-only success with structurally shared data', async () => {
  const h = await harness()
  h.render('status')
  const data = h.queries.now.data
  h.advance(60000, -3600000)
  assert.match(h.text(h.tree()), /stale/)
  h.queries.now = { ...h.queries.now, isError: true, error: new Error('offline') }
  h.render('status')
  h.queries.now = { ...h.queries.now, isError: false }
  h.render('status')
  assert.match(h.text(h.tree()), /stale/)
  h.queries.now = h.good(data)
  h.render('status')
  assert.match(h.text(h.tree()), /verified-empty/)
  h.advance(60000, 0)
  assert.match(h.text(h.tree()), /stale/)
  // A new identity at the same timestamp is a distinct successful read as well.
  h.queries.loops = h.good(h.queries.loops.data)
  h.queries.now = { ...h.queries.now, data: { runs: [], counts: {} } }
  h.render('status')
  assert.match(h.text(h.tree()), /verified-empty/)
  h.advance(60000, 0)
  assert.match(h.text(h.tree()), /stale/)
  h.unmount()
  assert.equal(h.timerCount(), 0)
})

test('nested now responses reject invalid counts and run rows before caching', async () => {
  const h = await harness()
  h.render('page')
  for (const value of [-1, Infinity, NaN, 1.5, '2', null]) {
    h.respond({ runs: [], counts: { running: value } })
    await assert.rejects(h.options.now.queryFn(), /Invalid now/)
  }
  const row = { pr: 42, seat: 'reviewer', head: 'abcdef0', state: 'waiting', why: 'waiting to retry' }
  for (const run of [null, {}, { ...row, pr: -1 }, { ...row, pr: 1.5 },
    { ...row, seat: null }, { ...row, head: {} }, { ...row, state: 'not-a-state' }, { ...row, why: {} }]) {
    h.respond({ runs: [run], counts: {} })
    await assert.rejects(h.options.now.queryFn(), /Invalid now/)
  }
  h.respond({ runs: [row], counts: { waiting: 1 } })
  await h.options.now.queryFn()
  for (const pacing of [{ seats: { reviewer: null } }, { seats: [] }, { holds: [null] }, { holds: {} }]) {
    h.respond({ runs: [], counts: {}, pacing })
    await assert.rejects(h.options.now.queryFn(), /Invalid now/)
  }
})

test('nested stats responses reject null seats and invalid numeric fields before caching', async () => {
  const h = await harness()
  h.render('page')
  const row = { turns: 2, retried: 1, states: { succeeded: 2 }, ran: { median: 1, max: 2 }, waited: null }
  for (const seat of [null, [], {}, { ...row, turns: -1 }, { ...row, retried: Infinity },
    { ...row, states: { failed: NaN } }, { ...row, ran: { median: -1 } }, { ...row, waited: [] }]) {
    h.respond({ turns: { reviewer: seat } })
    await assert.rejects(h.options.stats.queryFn(), /Invalid stats/)
  }
  h.respond({ turns: { reviewer: row } })
  await h.options.stats.queryFn()
})

test('arbitrary transport errors never appear in page, health, drawer, or chip tooltips', async () => {
  const h = await harness()
  const secret = '/home/private/tokens/pat?token=secret-token ' + 'X'.repeat(5000)
  for (const kind of ['loops', 'now', 'stats', 'health']) {
    h.queries[kind].isError = true
    h.queries[kind].error = new Error(secret)
  }
  h.queries.pr = { ...h.fail(undefined), error: new Error(secret) }
  for (const id of ['page', 'status']) {
    const tree = h.render(id)
    assert.doesNotMatch(h.text(tree), /secret-token|private\/tokens|XXXXX/)
    for (const node of h.flatten(tree)) {
      if (typeof node === 'object') assert.doesNotMatch(node.props.title || '', /secret-token|private\/tokens|XXXXX/)
    }
  }
  h.queries.now.data = { runs: [{ pr: 42, seat: 'reviewer', head: 'fixture', state: 'failed', why: 'failed: see explain' }], counts: { failed: 1 } }
  const tree = h.render('page')
  h.buttons(tree).find(n => h.text([n]).includes('#42')).props.onClick()
  assert.doesNotMatch(h.text(h.tree()), /secret-token|private\/tokens|XXXXX/)
  assert.match(h.text(h.tree()), /Backend read failed/)
})

test('invalid response category is bounded and does not reveal error message', async () => {
  const h = await harness()
  h.queries.now = { ...h.fail(undefined), error: { code: 'INVALID_RESPONSE', message: 'secret-token' } }
  assert.match(h.text(h.render('page')), /Backend response could not be verified/)
  assert.doesNotMatch(h.text(h.tree()), /secret-token/)
})

test('already stale empty loop cache never offers setup instruction', async () => {
  const h = await harness()
  h.queries.loops = h.good({ loops: [] })
  h.queries.loops.dataUpdatedAt -= 180000
  assert.match(h.text(h.render('page')), /stale/)
  assert.doesNotMatch(h.text(h.tree()), /No loops configured|hermes dk setup/)
})

test('RunRow retains responsive wrapping and full-width narrow reason', async () => {
  const h = await harness()
  h.queries.now = h.good({ runs: [{ pr: 42, seat: 'reviewer', head: 'abcdef0', state: 'waiting', why: 'long fixture reason' }], counts: { waiting: 1 } })
  const tree = h.render('page')
  const row = h.buttons(tree).find(node => h.text([node]).includes('#42'))
  assert.match(row.props.className, /flex-wrap/)
  const reason = h.flatten([row]).find(node => node.type === 'span' && node.props.children === 'long fixture reason')
  assert.match(reason.props.className, /basis-full.*break-words.*sm:basis-auto.*sm:flex-1.*sm:truncate/)
})
