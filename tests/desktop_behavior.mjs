// Executable component tests with explicitly synthetic query/SDK fixtures, not live data.
// No dependencies or changes to the production import surface. Run with --experimental-vm-modules.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import test from 'node:test'

const source = await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
// Pinned from real stats.collect with a missing disposable ledger; Python checks equality.
const settingsFixture = JSON.parse(await readFile(new URL('./fixtures/desktop_settings.json', import.meta.url), 'utf8'))
const noLedgerStats = JSON.parse(await readFile(new URL('./fixtures/desktop_no_ledger_stats.json', import.meta.url), 'utf8'))

// One loop of an /overview read, for synthetic fixtures. needs_you defaults to its uncertain run
// count, as the host view derives it; an unavailable or failed loop carries no counts.
function overviewLoop(id, { available = true, error = false, counts = {}, repo = 'fixture/repo' } = {}) {
  const usable = available && !error
  return { id, repo, available, error, counts: usable ? counts : {}, needs_you: usable ? counts.uncertain || 0 : 0 }
}

function overview(...loops) {
  return { loops, read_at: 1800000000 }
}

async function harness({ cachedAge = null } = {}) {
  const registrations = []
  const queries = {}
  const options = {}
  const calls = []
  const notices = []
  const navigations = []
  const effects = []
  const states = []
  const refs = []
  let refCursor = 0
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
  const cacheRows = []
  const cacheListeners = new Set()
  const disposers = []
  const cache = {
    findAll: ({ queryKey }) => cacheRows.filter(q => queryKey.every((part, i) => q.queryKey[i] === part)),
    subscribe: fn => { cacheListeners.add(fn); return () => cacheListeners.delete(fn) }
  }
  const emit = event => { for (const fn of cacheListeners) fn(event) }
  const store = new Map()
  const focusWrites = []
  let response = {}
  const good = data => ({ data, dataUpdatedAt: time, isError: false, isFetching: false,
    isLoading: false, refetch: () => calls.push('refetch') })
  queries.loops = good({ loops: [{ id: 'fixture-loop', repo: 'fixture/repo' }] })
  queries.focus = good({ loop: '' })
  queries.now = good({ runs: [], counts: {}, pacing: {} })
  queries.overview = good({ loops: [overviewLoop('fixture-loop')], read_at: time / 1000 })
  queries.stats = good({ turns: {} })
  queries.health = good({ checks: [], counts: {}, offline: true })
  queries.settings = good(structuredClone(settingsFixture))
  if (cachedAge !== null) cacheRows.push({ queryKey: ['diaktoros', 'now', 'seeded'],
    state: { data: queries.now.data, dataUpdatedAt: time - cachedAge, status: 'success' } })
  const element = (type, props, key) => ({ type, props: props || {}, key })
  const ui = name => props => element(name, props)
  const sdk = Object.fromEntries(['Badge', 'Button', 'ConfirmDialog', 'Dialog', 'DialogContent',
    'DialogDescription', 'DialogHeader', 'DialogTitle', 'StatusDot'].map(name => [name, ui(name)]))
  Object.assign(sdk, {
    PALETTE_AREA: 'palette', ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'nav', STATUSBAR_AREAS: { right: 'status' },
    host: { notify: n => notices.push(n), navigate: p => navigations.push(p) },
    queryClient: {
      getQueryCache: () => cache,
      invalidateQueries: args => calls.push(args),
      // The focus handoff the chip writes and the page observes; a real cache write notifies
      // observers, which is what a mounted page re-renders on.
      setQueryData: (key, data) => { queries[key[1]] = good(data); focusWrites.push(data) },
      getQueryData: key => (queries[key[1]] && queries[key[1]].data)
    },
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
    useRef: initial => {
      const index = refCursor++
      if (!(index in refs)) refs[index] = { current: typeof initial === 'function' ? initial() : initial }
      return refs[index]
    },
    useState: initial => {
      const index = cursor++
      // React calls a function initializer; the harness models it so a lazy seed behaves the same.
      if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial
      return [states[index], value => {
        states[index] = typeof value === 'function' ? value(states[index]) : value
        if (mountedId) render(mountedId)
      }]
    }
  }
  const context = vm.createContext({ console, Date: FakeDate, performance: { now: () => elapsed }, Promise, Error, setTimeout, clearTimeout })
  const module = new vm.SourceTextModule(source, { context })
  await module.link(specifier => {
    const values = specifier === '@hermes/plugin-sdk' ? sdk : specifier === 'react' ? react : { jsx: element, jsxs: element }
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value)
    }, { context })
  })
  await module.evaluate()
  // The page's one write (#371) is recorded on its own list, with its options, so a test can
  // assert the exact payload and that a refusal never turns into a visible success.
  const retryCalls = []
  let retryResult = { ok: false, error: new Error('fixture retry not configured') }
  module.namespace.default.register({
    rest: (path, opts) => {
      if (path === '/retry') {
        retryCalls.push(opts || {})
        return retryResult.ok ? Promise.resolve(retryResult.data) : Promise.reject(retryResult.error)
      }
      calls.push(path)
      return Promise.resolve(response)
    },
    storage: { get: key => store.get(key), set: (key, value) => store.set(key, value) },
    onDispose: fn => disposers.push(fn), registerMany: rows => registrations.push(...rows)
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
      refs.length = 0
    }
    mountedId = id
    cursor = 0
    effectCursor = 0
    refCursor = 0
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
    retryCalls, retryAnswer: data => { retryResult = { ok: true, data } },
    retryFails: error => { retryResult = { ok: false, error } },
    render, text, buttons, flatten, respond: value => { response = value },
    tree: () => currentTree, timerCount: () => timers.size,
    detail: () => registrations.find(r => r.id === 'open').data.detail(),
    cacheRead: (data = queries.now.data, row = cacheRows[0]) => {
      if (!row) { row = { queryKey: ['diaktoros', 'now', 'fixture-loop'], state: {} }; cacheRows.push(row); emit({ type: 'added', query: row }) }
      row.state = { data, dataUpdatedAt: time, status: 'success' }
      emit({ type: 'updated', action: { type: 'success' }, query: row })
      return row
    },
    overviewRead: (data = queries.overview.data, row = cacheRows.find(r => r.queryKey[1] === 'overview')) => {
      if (!row) { row = { queryKey: ['diaktoros', 'overview'], state: {} }; cacheRows.push(row); emit({ type: 'added', query: row }) }
      row.state = { data, dataUpdatedAt: time, status: 'success' }
      emit({ type: 'updated', action: { type: 'success' }, query: row })
      return row
    },
    emit, listenerCount: () => cacheListeners.size,
    focusWrites: () => focusWrites,
    pendingTimers: () => [...timers.values()].filter(timer => timer.at - elapsed === 60000).map(timer => timer.fn),
    dispose: () => { effects.forEach(effect => effect.cleanup?.()); mountedId = null; disposers.forEach(fn => fn()) },
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

test('settings: production projection renders every schema field and dynamically added unknown type', async () => {
  const h = await harness()
  let text = h.text(h.render('page'))
  for (const [key, spec] of Object.entries(settingsFixture.plugin.schema)) {
    assert.ok(text.includes(key), key)
    assert.ok(text.includes(spec.label), spec.label)
    assert.ok(text.includes(spec.description), key + ' description')
  }
  assert.match(text, /Read-only.*Capabilities/)
  assert.match(text, /Existing loops · effective settings/)
  assert.match(text, /shadow/)
  assert.match(text, /Set in plugin form/)
  assert.match(text, /Effective plugin value 4 Source Set in plugin form Schema default \(reference\) 3/)
  assert.match(text, /shadow_profile/)
  assert.match(text, /shadow_every/)
  assert.match(text, /Omitted settings, including shadow activation, are not reported as off/)
  for (const seat of Object.values(settingsFixture.loops[0].seats)) {
    assert.ok(text.includes('turn_budget_s ' + seat.turn_budget_s))
    assert.ok(text.includes('max_steps ' + seat.max_steps))
  }
  assert.match(text, /Schema default/)
  const data = structuredClone(settingsFixture)
  data.plugin.schema.future_field = { label: 'Future setting', type: 'future', default: { secret: 'do-not-show' }, description: 'Added without page edits' }
  data.plugin.values.future_field = { secret: 'do-not-show' }
  data.plugin.sources.future_field = 'default'
  h.respond(data)
  const validated = await h.options.settings.queryFn()
  h.queries.settings = h.good(validated)
  text = h.text(h.render('page'))
  assert.match(text, /future_field.*Added without page edits.*Unsupported structured value/)
  assert.doesNotMatch(text, /do-not-show/)
  assert.equal(h.calls.at(-1), '/settings')
})

test('settings: unavailable store is reference only, loop error isolated and reasons are never rendered', async () => {
  const h = await harness()
  const data = structuredClone(settingsFixture)
  data.plugin.available = false
  data.plugin.reason = 'credential-is-private'
  data.loops.push({ id: 'broken-loop', repo: '', base: '', error: true, reason: 'credential-is-private', seats: {}, flags: {} })
  h.respond(data)
  h.render('page')
  h.queries.settings = h.good(await h.options.settings.queryFn())
  const text = h.text(h.render('page'))
  assert.match(text, /Settings store unavailable.*reference only/)
  assert.match(text, /Effective plugin value unavailable Not verified/)
  assert.match(text, /alpha.*review-route/)
  assert.match(text, /broken-loop.*Loop settings unavailable/)
  assert.doesNotMatch(text, /credential-is-private|Set in plugin form/)
})

test('settings: loading, first failure, verified empty, cached failure, retry and elapsed freshness', async () => {
  const h = await harness()
  h.queries.settings = h.loading()
  assert.match(h.text(h.render('page')), /Settings: loading/)
  h.queries.settings = h.fail(undefined)
  h.queries.settings.error = new Error('private transport secret')
  assert.match(h.text(h.render('page')), /Settings: no verified data available/)
  assert.doesNotMatch(h.text(h.tree()), /private transport secret/)
  const data = structuredClone(settingsFixture)
  data.plugin.schema = {}; data.plugin.values = {}; data.plugin.sources = {}; data.loops = []
  h.queries.settings = h.good(data)
  assert.match(h.text(h.render('page')), /verified-empty Settings: backend read succeeded/)
  h.queries.settings = h.fail(structuredClone(settingsFixture))
  assert.match(h.text(h.render('page')), /Settings: showing last good data/)
  h.buttons(h.tree()).filter(button => button.props.children === 'Retry').at(-1).props.onClick()
  assert.equal(h.calls.at(-1), 'refetch')
  h.queries.settings = h.good(structuredClone(settingsFixture))
  h.render('page')
  h.advance(120000, -3600000)
  assert.match(h.text(h.tree()), /Settings: showing last good data/)
  h.queries.settings = h.good(structuredClone(settingsFixture))
  assert.match(h.text(h.render('page')), /Settings: backend read succeeded/)
  h.unmount()
  assert.equal(h.timerCount(), 0)
})

test('settings: malformed nested successes rejected before caching', async () => {
  const h = await harness()
  h.render('page')
  const mutations = [
    d => { d.plugin = null }, d => { d.plugin.seats.reviewer = null },
    d => { delete d.plugin.values.cap }, d => { d.plugin.sources.cap = 'guessed' },
    d => { d.plugin.schema.cap = null }, d => { d.plugin.values.cap = '4' },
    d => { d.loops[0].seats.reviewer.max_steps = -1 }, d => { d.loops[0].flags = [] },
    d => { delete d.loops[0].flags.fix_ci }, d => { d.loops[0].flags.cap = -1 },
    d => { delete d.loops[0].seats.reviewer }, d => { d.loops.push(d.loops[0]) },
    d => { d.loops[0].seats.reviewer = null }, d => { d.read_at = 'now' }
  ]
  for (const mutate of mutations) {
    const data = structuredClone(settingsFixture); mutate(data); h.respond(data)
    await assert.rejects(h.options.settings.queryFn(), error => error.code === 'INVALID_RESPONSE')
  }
  h.respond(structuredClone(settingsFixture))
  assert.equal((await h.options.settings.queryFn()).loops[0].id, 'alpha')
})

test('settings: secrets and invalid token paths withheld, token budgets and valid paths retained', async () => {
  const h = await harness()
  const data = structuredClone(settingsFixture)
  for (const [key, value] of Object.entries({ api_key: 'private-api-value', password: 'private-password', future_token_file: 'private-token-value', future_daily_tokens: '1250', safe_token_file: '~/credentials/seat.pat' })) {
    data.plugin.schema[key] = { label: key, description: '', type: 'str', default: value }
    data.plugin.values[key] = value; data.plugin.sources[key] = 'set'
  }
  data.loops[0].seats.reviewer.token_file = 'ghp_privatePAT'
  data.loops[0].reason = 'private-reason'
  h.queries.settings = h.good(data)
  const text = h.text(h.render('page'))
  assert.doesNotMatch(text, /private-api-value|private-password|private-token-value|ghp_privatePAT|private-reason/)
  assert.match(text, /1250/)
  assert.match(text, /~\/credentials\/seat.pat/)
  assert.match(text, /Invalid token path withheld/)
  assert.equal(h.buttons(h.tree()).some(button => /save|apply|merge/i.test(String(button.props.children))), false)
})

test('palette only: unseen successful cache read expires despite backward wall clock', async () => {
  const h = await harness()
  h.cacheRead()
  h.moveWall(-3600000)
  h.advance(60000, 0)
  assert.match(h.detail(), /^stale · idle/)
  h.advance(60000, -60000)
  assert.match(h.detail(), /^stale/)
  assert.equal(h.timerCount(), 0)
  assert.equal(h.calls.length, 0)
})

test('palette only: exact elapsed boundary, hung fetch/error events never extend freshness', async () => {
  const h = await harness()
  const row = h.cacheRead({ runs: [], counts: { running: 2 } })
  assert.match(h.detail(), /^verified · 2 running/)
  h.moveWall(-3600000)
  h.advance(59999, 0)
  row.state.fetchStatus = 'fetching'
  h.emit({ type: 'updated', action: { type: 'fetch' }, query: row })
  assert.match(h.detail(), /^verified · 2 running/)
  h.advance(1, 0)
  assert.match(h.detail(), /^stale · 2 running/)
  const stamp = row.state.dataUpdatedAt
  row.state.status = 'error'
  h.emit({ type: 'updated', action: { type: 'error' }, query: row })
  assert.match(h.detail(), /^stale/)
  row.state.status = 'success'
  h.emit({ type: 'observerResultsUpdated', query: row })
  assert.match(h.detail(), /^stale/)
  assert.equal(row.state.dataUpdatedAt, stamp)
  assert.equal(h.timerCount(), 0)
  assert.equal(h.calls.length, 0)
})

test('palette only: fresh timestamp resets shared-data expiry; identity resets same timestamp', async () => {
  const h = await harness()
  const row = h.cacheRead()
  const data = row.state.data
  h.advance(60000, -3600000)
  assert.match(h.detail(), /^stale/)
  h.cacheRead(data)
  assert.match(h.detail(), /^verified-empty/)
  h.advance(59999, 0)
  assert.match(h.detail(), /^verified-empty/)
  h.advance(1, 0)
  assert.match(h.detail(), /^stale/)
  h.cacheRead({ runs: [], counts: { pending: 1 } })
  assert.match(h.detail(), /^verified · 1 pending/)
  h.advance(60000, 0)
  assert.match(h.detail(), /^stale · 1 pending/)
  assert.equal(h.timerCount(), 0)
})

for (const cachedAge of [30000, 60000]) {
  test(`palette only: registration anchors preexisting ${cachedAge}ms cache without mounted hooks`, async () => {
    const h = await harness({ cachedAge })
    h.moveWall(-3600000)
    if (cachedAge === 30000) {
      h.advance(29999, 0)
      assert.match(h.detail(), /^verified-empty/)
      h.advance(1, 0)
    }
    assert.match(h.detail(), /^stale/)
    assert.equal(h.timerCount(), 0)
    assert.equal(h.listenerCount(), 1)
    h.dispose()
    assert.equal(h.listenerCount(), 0)
    assert.equal(h.calls.length, 0)
  })
}

test('palette only: forward-clock expiry stays stale after rollback', async () => {
  const h = await harness()
  h.cacheRead()
  h.moveWall(60000)
  assert.match(h.detail(), /^stale/)
  h.moveWall(-3600000)
  assert.match(h.detail(), /^stale/)
})

test('shared freshness: palette and chip agree on the overview read; queued old timer cannot expire newer read', async () => {
  const h = await harness()
  h.overviewRead()
  h.render('status')
  const oldCallbacks = h.pendingTimers()
  assert.equal(oldCallbacks.length, 1)
  h.advance(30000)
  const row = h.overviewRead(h.queries.overview.data)
  h.queries.overview = { ...h.queries.overview, ...row.state }
  h.render('status')
  oldCallbacks.forEach(fn => fn())
  assert.match(h.detail(), /^verified-empty/)
  assert.match(h.text(h.tree()), /verified-empty/)
  h.advance(60000, -3600000)
  assert.match(h.detail(), /^stale/)
  assert.match(h.text(h.tree()), /stale/)
  h.unmount()
  assert.equal(h.timerCount(), 0)
  assert.match(h.detail(), /^stale/)
  h.dispose()
  assert.equal(h.listenerCount(), 0)
})

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
    h.respond(kind === 'now' ? { runs: [], counts: {}, available: true } : { turns: {} })
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
  h.queries.overview = h.loading()
  assert.match(h.text(h.render('status')), /loop: loading/)
  h.queries.overview = h.fail(undefined)
  let tree = h.render('status')
  assert.match(h.text(tree), /loop: unavailable/)
  h.buttons(tree).find(n => h.text([n]) === 'Retry').props.onClick()
  assert.ok(h.calls.includes('refetch'))
  h.queries.overview = h.fail(overview(overviewLoop('fixture-loop', { counts: { pending: 2, waiting: 3, uncertain: 4 } })))
  tree = h.render('status')
  assert.match(h.text(tree), /stale · 2 pending · 3 retry-waiting · 4 need you/)
  assert.match(h.buttons(tree)[0].props.title, /Last good read:.*Backend read failed/)
  assert.doesNotMatch(h.buttons(tree)[0].props.title, /fixture backend offline/)
  assert.equal(h.notices.length, 0)
  assert.equal(h.store.size, 0)
  h.queries.overview = h.good(overview(overviewLoop('fixture-loop')))
  tree = h.render('status')
  assert.match(h.text(tree), /verified-empty · idle/)
  h.buttons(tree)[0].props.onClick()
  assert.deepEqual(h.navigations, ['/diaktoros?loop=fixture-loop'])
})

test('pending and retry-waiting are not idle or verified-empty in page and chip', async () => {
  const h = await harness()
  h.queries.now = h.good({ runs: [], counts: { pending: 2, waiting: 3 }, available: true })
  h.queries.overview = h.good(overview(overviewLoop('fixture-loop', { counts: { pending: 2, waiting: 3 } })))
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
  h.queries.overview = h.fail(undefined)
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
  h.respond({ runs: [], counts: {}, available: true })
  await h.options.now.queryFn()
  assert.equal(h.calls.at(-1), '/now?loop=fixture%20second')
})

test('last-good timestamp is retained through refresh failure, not replaced by error time', async () => {
  const h = await harness()
  const stamp = h.queries.now.dataUpdatedAt - 10000
  h.queries.now.dataUpdatedAt = stamp
  h.queries.overview.dataUpdatedAt = stamp
  const date = new Date(stamp).toLocaleString()
  assert.ok(h.text(h.render('page')).includes(date))
  h.queries.now.isError = true
  h.queries.now.error = new Error('fixture later failure')
  assert.ok(h.text(h.render('page')).includes(date))
  assert.ok(h.buttons(h.render('status'))[0].props.title.includes(date))
  h.queries.overview.isError = true
  h.queries.overview.error = new Error('fixture later failure')
  assert.ok(h.buttons(h.render('status'))[0].props.title.includes(date))
})

test('refresh keeps verified data and timestamp while in flight; retry disabled', async () => {
  const h = await harness()
  h.queries.now.isFetching = true
  const tree = h.render('page')
  assert.match(h.text(tree), /refreshing…/)
  assert.equal(h.buttons(tree).find(n => h.text([n]) === 'Reading…').props.disabled, true)
})

test('chip notifications only follow successful verified reads, keyed per loop', async () => {
  const h = await harness()
  h.queries.overview = h.good(overview(overviewLoop('fixture-loop', { counts: { uncertain: 1 } })))
  h.render('status')
  assert.equal(h.notices.length, 0)
  assert.equal(h.store.get('needsYou:fixture-loop'), 1)
  h.queries.overview = h.fail(overview(overviewLoop('fixture-loop', { counts: { uncertain: 3 } })))
  h.render('status')
  // A failed read is not news and must not rebaseline the loop either.
  assert.equal(h.store.get('needsYou:fixture-loop'), 1)
  assert.equal(h.notices.length, 0)
  h.queries.overview = h.good(overview(overviewLoop('fixture-loop', { counts: { uncertain: 3 } })))
  h.render('status')
  assert.equal(h.notices.length, 1)
  assert.match(h.notices[0].message, /3 run\(s\) need you \(fixture-loop\)/)
  assert.equal(h.store.get('needsYou:fixture-loop'), 3)
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
    h.queries.now = h.good({ runs: [], counts: {}, available: true })
    h.queries.loops = h.good(h.queries.loops.data)
    h.queries.overview = h.good(h.queries.overview.data)
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
  h.queries.overview = h.good(overview(overviewLoop('fixture-loop')))
  h.render('status')
  assert.equal(h.timerCount(), 2)
  h.advance(30000)
  assert.match(h.text(h.tree()), /verified-empty/)
  h.advance(30000)
  assert.match(h.text(h.tree()), /stale/)
  h.queries.overview = h.fail(undefined)
  h.render('status')
  assert.equal(h.timerCount(), 1)
  h.unmount()
  assert.equal(h.timerCount(), 0)
})

test('sticky expiry resets on a timestamp-only success with structurally shared data', async () => {
  const h = await harness()
  h.render('status')
  const data = h.queries.overview.data
  h.advance(60000, -3600000)
  assert.match(h.text(h.tree()), /stale/)
  h.queries.overview = { ...h.queries.overview, isError: true, error: new Error('offline') }
  h.render('status')
  h.queries.overview = { ...h.queries.overview, isError: false }
  h.render('status')
  assert.match(h.text(h.tree()), /stale/)
  h.queries.overview = h.good(data)
  h.render('status')
  assert.match(h.text(h.tree()), /verified-empty/)
  h.advance(60000, 0)
  assert.match(h.text(h.tree()), /stale/)
  // A new identity at the same timestamp is a distinct successful read as well.
  h.queries.loops = h.good(h.queries.loops.data)
  h.queries.overview = { ...h.queries.overview, data: overview(overviewLoop('fixture-loop')) }
  h.render('status')
  assert.match(h.text(h.tree()), /verified-empty/)
  h.advance(60000, 0)
  assert.match(h.text(h.tree()), /stale/)
  h.unmount()
  assert.equal(h.timerCount(), 0)
})

test('nested now responses reject invalid counts, run rows and a ledger-absent payload with counts', async () => {
  const h = await harness()
  h.render('page')
  for (const value of [-1, Infinity, NaN, 1.5, '2', null]) {
    h.respond({ runs: [], counts: { running: value }, available: true })
    await assert.rejects(h.options.now.queryFn(), /Invalid now/)
  }
  const row = { pr: 42, seat: 'reviewer', head: 'abcdef0', state: 'waiting', why: 'waiting to retry' }
  for (const run of [null, {}, { ...row, pr: -1 }, { ...row, pr: 1.5 },
    { ...row, seat: null }, { ...row, head: {} }, { ...row, state: 'not-a-state' }, { ...row, why: {} }]) {
    h.respond({ runs: [run], counts: {}, available: true })
    await assert.rejects(h.options.now.queryFn(), /Invalid now/)
  }
  // A ledger that could not be read must not arrive with counts: that would be a manufactured zero.
  h.respond({ runs: [], counts: { uncertain: 1 }, available: false })
  await assert.rejects(h.options.now.queryFn(), /Invalid now/)
  h.respond({ runs: [], counts: {}, available: 'yes' })
  await assert.rejects(h.options.now.queryFn(), /Invalid now/)
  h.respond({ runs: [row], counts: { waiting: 1 }, available: true })
  await h.options.now.queryFn()
  for (const pacing of [{ seats: { reviewer: null } }, { seats: [] }, { holds: [null] }, { holds: {} }]) {
    h.respond({ runs: [], counts: {}, pacing, available: true })
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

// ---- Desktop slice 2: multi-loop visibility -------------------------------------------------

const TWO_LOOPS = { loops: [{ id: 'widgets', repo: 'acme/widgets' }, { id: 'gadgets', repo: 'acme/gadgets' }] }

test('chip: aggregate across all loops, naming each and showing a per-loop read failure', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.overview = h.good(overview(
    overviewLoop('widgets', { counts: { uncertain: 2, running: 1 } }),
    overviewLoop('gadgets', { error: true })))
  const tree = h.render('status')
  assert.equal(h.text(tree), 'loops: verified · widgets 1 running · 2 need you · gadgets read failed')
  assert.match(h.buttons(tree)[0].props.title, /Diaktoros · widgets, gadgets/)
})

// #701: a loop whose config file did not load is named by /loops and /overview, and reads as
// "config unreadable" (fix the file), never as "not read" (wait or retry) and never absent.
const UNREADABLE = { id: 'typo', repo: '', unreadable: true, reason: 'bad json' }
const unreadableOverview = id => ({ id, repo: '', available: false, error: true, unreadable: true,
  reason: 'bad json', counts: {}, needs_you: 0 })

test('chip: an unreadable loop is named as config unreadable beside the loop that loads', async () => {
  const h = await harness()
  h.queries.loops = h.good({ loops: [{ id: 'widgets', repo: 'acme/widgets' }, UNREADABLE] })
  h.queries.overview = h.good(overview(overviewLoop('widgets', { counts: { running: 1 } }), unreadableOverview('typo')))
  const text = h.text(h.render('status'))
  assert.equal(text, 'loops: verified · widgets 1 running · typo config unreadable')
  assert.doesNotMatch(text, /not read/)
})

test('chip: an unreadable loop is named even when the overview read omits it', async () => {
  const h = await harness()
  h.queries.loops = h.good({ loops: [{ id: 'widgets', repo: 'acme/widgets' }, UNREADABLE] })
  h.queries.overview = h.good(overview(overviewLoop('widgets')))
  const text = h.text(h.render('status'))
  assert.match(text, /typo config unreadable/)
})

test('chip: a configured loop the overview did not name still reads not read', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.overview = h.good(overview(overviewLoop('widgets')))
  assert.match(h.text(h.render('status')), /gadgets not read/)
})

test('page: an unreadable loop is listed with its reason, is not selectable, and the other loop still shows', async () => {
  const h = await harness()
  h.queries.loops = h.good({ loops: [{ id: 'widgets', repo: 'acme/widgets' }, UNREADABLE] })
  h.queries.overview = h.good(overview(overviewLoop('widgets'), unreadableOverview('typo')))
  const tree = h.render('page')
  const text = h.text(tree)
  assert.match(text, /Config unreadable/)
  assert.match(text, /typo \(bad json\)/)
  assert.doesNotMatch(text, /No run in any loop is waiting on you/)
  assert.equal(h.options.now.enabled, true)
  assert.match(h.options.now.queryKey.join('/'), /widgets/)
})

test('page: only unreadable loops configured still names them rather than "No loops configured"', async () => {
  const h = await harness()
  h.queries.loops = h.good({ loops: [UNREADABLE] })
  const text = h.text(h.render('page'))
  assert.match(text, /typo \(bad json\)/)
  assert.doesNotMatch(text, /No loops configured/)
})

test('overview responses accept an unreadable loop and reject one that claims counts or a repo', async () => {
  const h = await harness()
  h.render('status')
  h.respond({ loops: [unreadableOverview('typo')], read_at: 1 })
  await h.options.overview.queryFn()
  for (const bad of [{ ...unreadableOverview('typo'), repo: 'a/b' }, { ...unreadableOverview('typo'), available: true },
    { ...unreadableOverview('typo'), reason: 5 }, { ...unreadableOverview('typo'), unreadable: 'yes' }]) {
    h.respond({ loops: [bad], read_at: 1 })
    await assert.rejects(h.options.overview.queryFn(), /Invalid overview/)
  }
})

test('chip: a loop with no ledger reads as no ledger, not verified idle', async () => {
  const h = await harness()
  h.queries.overview = h.good(overview(overviewLoop('fixture-loop', { available: false })))
  const text = h.text(h.render('status'))
  assert.match(text, /loop: verified · no ledger/)
  assert.doesNotMatch(text, /idle|verified-empty/)
})

test('chip: a rise in any loop announces once, named, and a reload never announces', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.overview = h.good(overview(
    overviewLoop('widgets', { counts: { uncertain: 1 } }), overviewLoop('gadgets')))
  h.render('status')
  assert.equal(h.notices.length, 0)                       // the first read records the baseline only
  assert.equal(h.store.get('needsYou:widgets'), 1)
  assert.equal(h.store.get('needsYou:gadgets'), 0)
  h.queries.overview = h.good(overview(
    overviewLoop('widgets', { counts: { uncertain: 1 } }), overviewLoop('gadgets')))
  h.render('status')
  assert.equal(h.notices.length, 0)                       // a reload of the same read is not news
  h.queries.overview = h.good(overview(
    overviewLoop('widgets', { counts: { uncertain: 1 } }), overviewLoop('gadgets', { counts: { uncertain: 4 } })))
  h.render('status')
  assert.equal(h.notices.length, 1)                       // one toast for one rise
  assert.match(h.notices[0].message, /5 run\(s\) need you \(gadgets\)/)
  assert.equal(h.store.get('needsYou:gadgets'), 4)
  assert.equal(h.store.get('needsYou:widgets'), 1)
})

test('chip reports the loop that needs you and the page opens it', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.overview = h.good(overview(
    overviewLoop('widgets', { counts: { running: 1 } }), overviewLoop('gadgets', { counts: { uncertain: 2 } })))
  const tree = h.render('status')
  h.buttons(tree)[0].props.onClick()
  assert.deepEqual(h.navigations, ['/diaktoros?loop=gadgets'])
  // The page honours the handoff: it selects the reported loop and reads that loop's runs.
  h.render('page')
  assert.equal(h.options.now.queryKey[2], 'gadgets')
})

test('page: the cross-loop needs-you block names each loop and selecting one opens its runs', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.overview = h.good(overview(
    overviewLoop('widgets', { counts: { uncertain: 1 } }), overviewLoop('gadgets', { counts: { uncertain: 2 } })))
  let tree = h.render('page')
  assert.match(h.text(tree), /Needs you/)
  assert.equal(h.options.now.queryKey[2], 'widgets')      // the first loop by default
  h.buttons(tree).find(n => h.text([n]) === 'gadgets 2 need you').props.onClick()
  tree = h.render('page')
  assert.equal(h.options.now.queryKey[2], 'gadgets')      // the block opened that loop's runs
})

test('page: a handoff to an already-open page moves it to the loop the chip reported', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.overview = h.good(overview(
    overviewLoop('widgets', { counts: { running: 1 } }), overviewLoop('gadgets')))
  h.render('page')
  assert.equal(h.options.now.queryKey[2], 'widgets')      // the page is already open on this loop
  // The chip writes the handoff and navigates; the open page observes the same cache key, which a
  // real query client notifies its observers about. This is the case the route's query string
  // alone cannot cover.
  h.render('status')
  h.buttons(h.tree())[0].props.onClick()
  h.queries.focus = h.good({ loop: 'gadgets' })
  h.render('page')
  assert.equal(h.options.now.queryKey[2], 'gadgets')
})

test('chip: a configured loop the read did not name is not read, never idle', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.overview = h.good(overview(overviewLoop('widgets', { counts: { running: 1 } })))
  const text = h.text(h.render('status'))
  assert.match(text, /widgets 1 running · gadgets not read/)
  assert.doesNotMatch(text, /gadgets idle|gadgets verified-empty/)
})

test('page: a single-loop install gets no cross-loop block', async () => {
  const h = await harness()
  h.queries.overview = h.good(overview(overviewLoop('fixture-loop', { counts: { uncertain: 1 } })))
  assert.doesNotMatch(h.text(h.render('page')), /Needs you/)
})

test('page: the drawer is scoped to its loop and closes when the loop changes', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.now = h.good({ runs: [{ pr: 42, seat: 'reviewer', head: 'abcdef0', state: 'waiting', why: 'waiting to retry' }],
    counts: { waiting: 1 }, pacing: {}, available: true })
  h.queries.pr = h.good({ repo: 'acme/widgets', loop: 'widgets', pr: 42, cap: 3, stuck: [], turns: [],
    verdicts: {}, rulings: [], breach: null, findings: [], decisions: [], read_at: 1800000000 })
  let tree = h.render('page')
  h.buttons(tree).find(n => h.text([n]).includes('#42')).props.onClick()
  tree = h.render('page')
  assert.match(h.text(tree), /hermes dk explain --pr 42 --loop widgets/)
  h.buttons(tree).find(n => h.text([n]) === 'gadgets').props.onClick()
  tree = h.render('page')
  assert.doesNotMatch(h.text(tree), /hermes dk explain --pr 42/)
})

test('page: the drawer shows shadow reviews beside the real verdict, and none when there are none', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.now = h.good({ runs: [{ pr: 42, seat: 'reviewer', head: 'abcdef0', state: 'waiting', why: 'waiting to retry' }],
    counts: { waiting: 1 }, pacing: {}, available: true })
  const pr = { repo: 'acme/widgets', loop: 'widgets', pr: 42, cap: 3, stuck: [], turns: [],
    verdicts: {}, rulings: [], breach: null, findings: [], decisions: [], read_at: 1800000000 }
  h.queries.pr = h.good({ ...pr, shadows: [
    { model: 'grok-4.7', head: 'abcdef0', verdict: 'changes', real: 'approved', agrees: false,
      findings: ['a.py:3: leaks the token'], more: 1, held: '', created: 1 },
    { model: 'grok-4.7', head: '1234567', verdict: 'approved', real: null, agrees: null,
      findings: [], more: 0, held: 'it touches .github/ci.yml', created: 0 }] })
  let tree = h.render('page')
  h.buttons(tree).find(n => h.text([n]).includes('#42')).props.onClick()
  let text = h.text(h.render('page'))
  assert.match(text, /Shadow reviews \(2, recorded, never posted\)/)
  assert.match(text, /would have changes/)
  assert.match(text, /reviewer approved · disagrees/)
  assert.match(text, /a\.py:3: leaks the token/)
  assert.match(text, /… 1 more/)
  assert.match(text, /no real verdict yet/)
  assert.match(text, /Approval left to a person: it touches \.github\/ci\.yml/)
  h.queries.pr = h.good(pr)
  h.render('page')
  text = h.text(h.flatten(h.tree()).filter(node => node.type === 'Dialog'))
  assert.doesNotMatch(text, /Shadow reviews/)
})

test('page: a /now read with no ledger renders unavailable, never nothing-running', async () => {
  const h = await harness()
  h.queries.now = h.good({ repo: 'fixture/repo', runs: [], counts: {}, pacing: {}, available: false })
  const page = h.text(h.render('page'))
  assert.match(page, /unavailable/)
  assert.match(page, /No run ledger available in the last successful backend read. Run counts are unavailable, not zero./)
  assert.doesNotMatch(page, /No active or recent failed runs recorded|idle/)
})

test('overview responses reject malformed loops and a bad read_at before caching', async () => {
  const h = await harness()
  h.render('status')
  const bad = [
    [{ id: '' }],
    [{ id: 'a', repo: 'r' }],
    [{ id: 'a', repo: 'r', available: 'yes', error: false, counts: {}, needs_you: 0 }],
    [{ id: 'a', repo: 'r', available: false, error: false, counts: { uncertain: 1 }, needs_you: 1 }],
    [{ id: 'a', repo: 'r', available: true, error: false, counts: { x: -1 }, needs_you: 0 }]
  ]
  for (const loops of bad) {
    h.respond({ loops, read_at: 1 })
    await assert.rejects(h.options.overview.queryFn(), /Invalid overview/)
  }
  h.respond({ loops: [overviewLoop('a')], read_at: 'x' })
  await assert.rejects(h.options.overview.queryFn(), /Invalid overview/)
  h.respond({ loops: [overviewLoop('a')], read_at: 1 })
  await h.options.overview.queryFn()
})

test('page: the cross-loop block keeps last-good entries on a stale read', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.overview = h.fail(overview(overviewLoop('widgets'), overviewLoop('gadgets', { counts: { uncertain: 2 } })))
  const text = h.text(h.render('page'))
  assert.match(text, /Needs you/)
  assert.match(text, /gadgets 2 need you/)                       // last-good data kept, not hidden
  assert.match(text, /Across all loops: showing last good data/)
  assert.doesNotMatch(text, /No run in any loop is waiting on you/)
})

test('page: the cross-loop block names broken loops and never claims nothing is waiting', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.overview = h.good(overview(
    overviewLoop('widgets', { available: false }), overviewLoop('gadgets', { error: true })))
  const text = h.text(h.render('page'))
  assert.match(text, /Could not read: widgets \(no ledger\), gadgets \(read failed\)/)
  assert.doesNotMatch(text, /No run in any loop is waiting on you/)
})

test('page: an unread overview never claims nothing is waiting', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.overview = h.fail(undefined)
  const text = h.text(h.render('page'))
  assert.match(text, /Across all loops: no verified data available/)
  assert.doesNotMatch(text, /No run in any loop is waiting on you/)
})

// ---- Desktop slice (#371): a checked, confirmed Retry ------------------------------------------
// The backend owns eligibility and the retry itself; the page draws the button from the verdict
// it is given, always confirms, sends the exact target, and shows the backend's own answer.

const HEX = 'a'.repeat(40)
const RETRY_TITLE = 'Re-arm this run — you will be asked to confirm'

function retrying(extra = {}) {
  return { pr: 356, seat: 'reviewer', head: HEX.slice(0, 7), state: 'failed',
    why: 'failed: see explain', retry: { eligible: true, head: HEX, reason: '' }, ...extra }
}

// The counts line is the host's own derivation (stats.now): one per run state, with a held
// waiting run counted as held. Built from the rows so a fixture can never claim a count the
// rows contradict — the page renders "idle" from an empty counts map, not from an empty list.
function stalled(states) {
  const counts = {}
  for (const run of states) {
    const key = run.state === 'waiting' && run.why.startsWith('held:') ? 'held' : run.state
    counts[key] = (counts[key] || 0) + 1
  }
  return { runs: states, counts, pacing: null, available: true }
}

const dialogOf = h => h.flatten(h.tree()).find(node => node.type === 'ConfirmDialog')
const retryButtons = h => h.buttons(h.tree())
  .filter(node => node.type === 'Button' && node.props.title === RETRY_TITLE)

test('page: Retry is drawn only for a row the backend says a retry would re-arm', async () => {
  const h = await harness()
  h.queries.now = h.good(stalled([
    retrying(),
    retrying({ seat: 'fixer', state: 'uncertain',
      retry: { eligible: false, head: HEX, reason: 'run is uncertain (a worker may have written)' } }),
    retrying({ seat: 'fixer', state: 'cancelled', why: 'cancelled before any write — retry re-arms it',
      retry: { eligible: false, head: '', reason: 'unattended fixer pushes are off for fixture/repo' } })
  ]))
  h.render('page')
  const buttons = retryButtons(h)
  assert.equal(buttons.length, 1, 'exactly the retryable row carries the control')
  // The reason a row has no button is the backend's own, never a state the page matched on.
  const badges = h.flatten(h.tree()).filter(node => node.type === 'span' && node.props.title)
  assert.ok(badges.some(node => node.props.title === 'run is uncertain (a worker may have written)'))
  assert.ok(badges.some(node => /unattended fixer pushes are off/.test(node.props.title)))
  // Cancelled rows are listed (a push-policy cancellation is an operator's to recover), not hidden,
  // and the counts line names them instead of calling the loop idle.
  assert.match(h.text(h.tree()), /cancelled before any write — retry re-arms it/)
  assert.match(h.text(h.tree()), /1 cancelled/)
  // The loop never merges, and no control on the page may imply it might: no button does.
  assert.ok(!h.buttons(h.tree()).some(node => /merge/i.test(h.text([node]))), 'no merge affordance')
})

test('page: every click confirms first, and cancel sends nothing', async () => {
  const h = await harness()
  h.queries.now = h.good(stalled([retrying()]))
  h.render('page')
  assert.equal(dialogOf(h), undefined, 'no dialog before a click')
  retryButtons(h)[0].props.onClick()
  const dialog = dialogOf(h)
  assert.ok(dialog, 'the click opens the confirmation')
  // The preview names the real target: loop, PR, seat and head, and the command it equals.
  assert.match(dialog.props.description, /Retry the reviewer on #356 @ aaaaaaa in loop fixture-loop/)
  assert.match(dialog.props.description, /hermes dk retry --loop fixture-loop --pr 356 --seat reviewer/)
  assert.equal(dialog.props.confirmLabel, 'Retry run')
  assert.equal(dialog.props.open, true)
  assert.equal(h.retryCalls.length, 0, 'nothing is sent before the operator confirms')
  dialog.props.onClose()
  assert.equal(dialogOf(h), undefined, 'cancel closes it')
  assert.equal(h.retryCalls.length, 0)
  assert.doesNotMatch(h.text(h.tree()), /Re-armed/)
})

test('page: confirming posts the exact target, then shows the answer and the state read back', async () => {
  const h = await harness()
  h.queries.now = h.good(stalled([retrying()]))
  h.retryAnswer({ ok: true, loop: 'fixture-loop', pr: 356, seat: 'reviewer', head: HEX, head7: 'aaaaaaa',
    answer: 'reviewer #356 @ aaaaaaa re-armed (was failed) · no private runtime file — the run stays pending until one exists',
    rearmed: [{ seat: 'reviewer', head7: 'aaaaaaa', state: 'failed' }], refused: [],
    worker: 'no private runtime file — the run stays pending until one exists',
    runs: [{ seat: 'reviewer', head7: 'aaaaaaa', state: 'pending' }], read_at: 1800000000 })
  h.render('page')
  retryButtons(h)[0].props.onClick()
  await dialogOf(h).props.onConfirm()
  assert.equal(h.retryCalls.length, 1)
  // The full head the backend would re-arm is what it is asked to confirm — not the 7-char preview.
  assert.deepEqual(JSON.parse(JSON.stringify(h.retryCalls[0])), { method: 'POST',
    body: { loop: 'fixture-loop', pr: 356, seat: 'reviewer', head: HEX } })
  const text = h.text(h.tree())
  assert.match(text, /Re-armed/)
  assert.match(text, /reviewer #356 @ aaaaaaa re-armed \(was failed\)/)
  assert.match(text, /no private runtime file — the run stays pending until one exists/)
  assert.match(text, /now: reviewer pending/, 'the ledger state is read back after the write')
  // The reads that are now out of date are invalidated: the live list, the PR record and the chip.
  const keys = h.calls.filter(call => call && call.queryKey).map(call => call.queryKey.join('/'))
  assert.deepEqual(keys.sort(), ['diaktoros/now/fixture-loop', 'diaktoros/overview',
    'diaktoros/pr/fixture-loop/356'])
})

test('page: a refusal shows the backend reason, keeps the dialog open and invalidates nothing', async () => {
  const h = await harness()
  h.queries.now = h.good(stalled([retrying()]))
  const detail = 'refused: run is uncertain (a worker may have written). A run that may have written ' +
    'is never replayed. Inspect the PR for its external writes'
  h.retryFails(new Error('Error invoking remote method hermes:api: Error: 409: ' +
    JSON.stringify({ detail })))
  h.render('page')
  retryButtons(h)[0].props.onClick()
  await assert.rejects(dialogOf(h).props.onConfirm(), error => error.message === detail)
  const text = h.text(h.tree())
  assert.match(text, /Retry refused/)
  assert.match(text, /HTTP 409 · refused: run is uncertain \(a worker may have written\)/)
  assert.doesNotMatch(text, /Error invoking remote method|hermes:api|PRIVATE_TRANSPORT_SECRET/)
  assert.equal(dialogOf(h).props.open, true, 'the dialog stays open on the host own refusal')
  assert.deepEqual(h.calls.filter(call => call && call.queryKey), [], 'a refusal changes nothing to re-read')
})

test('page: one confirm is one request even when the button is hit twice', async () => {
  const h = await harness()
  h.queries.now = h.good(stalled([retrying()]))
  h.retryAnswer({ ok: true, answer: 'reviewer #356 @ aaaaaaa re-armed (was failed)', head: HEX,
    rearmed: [{ seat: 'reviewer', head7: 'aaaaaaa', state: 'failed' }],
    runs: [{ seat: 'reviewer', head7: 'aaaaaaa', state: 'pending' }] })
  h.render('page')
  retryButtons(h)[0].props.onClick()
  const dialog = dialogOf(h)
  await Promise.all([dialog.props.onConfirm(), dialog.props.onConfirm()])
  assert.equal(h.retryCalls.length, 1, 'the in-flight guard prevents a double submission')
})

test('page: an unverified answer is never shown as a re-arm', async () => {
  const h = await harness()
  h.queries.now = h.good(stalled([retrying()]))
  h.retryAnswer({ ok: true, answer: '', rearmed: [], runs: [] })
  h.render('page')
  retryButtons(h)[0].props.onClick()
  await assert.rejects(dialogOf(h).props.onConfirm())
  const text = h.text(h.tree())
  assert.match(text, /Retry refused/)
  assert.match(text, /The retry answer could not be verified/)
  assert.doesNotMatch(text, /Re-armed\b/)
  assert.deepEqual(h.calls.filter(call => call && call.queryKey), [])
})

test('page: /now accepts a retry verdict and rejects a malformed one before caching', async () => {
  const h = await harness()
  h.render('page')
  h.respond(stalled([retrying()]))
  const validated = await h.options.now.queryFn()
  assert.equal(validated.runs[0].retry.head, HEX)
  const malformed = [
    stalled([retrying({ retry: { eligible: 'yes', head: HEX, reason: '' } })]),
    stalled([retrying({ retry: { eligible: true, head: '', reason: '' } })]),
    stalled([retrying({ retry: { eligible: false, head: HEX } })]),
    stalled([retrying({ retry: { eligible: false, head: 'not-a-sha', reason: '' } })]),
    stalled([retrying({ state: 'quarantined' })])
  ]
  for (const payload of malformed) {
    h.respond(payload)
    await assert.rejects(h.options.now.queryFn(), /Invalid now response/)
  }
})
