// Executable component tests with explicitly synthetic query/SDK fixtures, not live data.
// No dependencies or changes to the production import surface. Run with --experimental-vm-modules.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import test from 'node:test'

const source = await readFile(process.env.DIAKTOROS_PLUGIN_SOURCE || new URL('../desktop/plugin.js', import.meta.url), 'utf8')
// Pinned from real stats.collect with a missing disposable ledger; Python checks equality.
const settingsFixture = JSON.parse(await readFile(new URL('./fixtures/desktop_settings.json', import.meta.url), 'utf8'))
const recordsWriterFixture = JSON.parse(await readFile(new URL('./fixtures/desktop_records.json', import.meta.url), 'utf8'))
const holdWriterFixtures = JSON.parse(await readFile(new URL('./fixtures/desktop_hold_explanations.json', import.meta.url), 'utf8'))
const holdWriterFixture = holdWriterFixtures.run_waiting
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

async function harness({ cachedAge = null, queryClient: realQueryClient = null, HealthObserver = null } = {}) {
  const registrations = []
  const queries = {}
  // Opt-in exact keys let selection tests model independent TanStack cache entries.
  // Existing fixtures retain their kind-wide defaults unless that kind is explicitly scoped.
  const keyedQueries = new Map()
  const scopedKinds = new Set()
  const queryReads = []
  const storageWrites = []
  const options = {}
  const calls = []
  const notices = []
  const navigations = []
  const effects = []
  const states = []
  const refs = []
  let refCursor = 0
  const timers = new Map()
  let time = HealthObserver ? Date.now() : 1800000000000
  let elapsed = 0
  let timerId = 0
  let cursor = 0
  let effectCursor = 0
  let mountedId = null
  let currentTree = null
  // Opt-in SDK adapter: the actual mounted Health calls production useHealth;
  // its observer owns fetching/subscription/refetch, rather than extracting options
  // and manually creating an observer after a mock component render.
  let healthObserver = null
  let unsubscribeHealth = null
  let observerRenderQueued = false
  const stopHealthObserver = () => {
    unsubscribeHealth?.()
    unsubscribeHealth = null
    healthObserver = null
  }
  const observerChanged = () => {
    if (observerRenderQueued) return
    observerRenderQueued = true
    queueMicrotask(() => {
      observerRenderQueued = false
      if (mountedId) render(mountedId)
    })
  }
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
  let readError = null
  let readErrorPath = ''
  const good = data => ({ data, dataUpdatedAt: time, isError: false, isFetching: false,
    isLoading: false, refetch: () => calls.push('refetch') })
  queries.loops = good({ loops: [{ id: 'fixture-loop', repo: 'fixture/repo' }] })
  queries.hooks = good({ loop: 'fixture-loop', repo: 'fixture/repo', state: 'armed', eligible: true, confirmation: 'opaque-snapshot', reason: '', read_at: time / 1000 })
  queries.focus = good({ loop: '' })
  queries.now = good({ runs: [], counts: {}, pacing: {} })
  queries.overview = good({ loops: [overviewLoop('fixture-loop')], read_at: time / 1000 })
  queries.stats = good({ turns: {} })
  queries.health = good({ checks: [], counts: {}, offline: true })
  queries.settings = good(structuredClone(settingsFixture))
  queries.records = good(recordsFixture())
  if (cachedAge !== null) cacheRows.push({ queryKey: ['diaktoros', 'now', 'seeded'],
    state: { data: queries.now.data, dataUpdatedAt: time - cachedAge, status: 'success' } })
  const element = (type, props, key) => ({ type, props: props || {}, key })
  const ui = name => props => element(name, props)
  const sdk = Object.fromEntries(['Badge', 'Button', 'ConfirmDialog', 'Dialog', 'DialogContent',
    'DialogDescription', 'DialogHeader', 'DialogTitle', 'StatusDot', 'Input'].map(name => [name, ui(name)]))
  Object.assign(sdk, {
    PALETTE_AREA: 'palette', ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'nav', STATUSBAR_AREAS: { right: 'status' },
    host: { notify: n => notices.push(n), navigate: p => navigations.push(p) },
    queryClient: realQueryClient || {
      getQueryCache: () => cache,
      invalidateQueries: args => calls.push(args),
      // The focus handoff the chip writes and the page observes; a real cache write notifies
      // observers, which is what a mounted page re-renders on.
      setQueryData: (key, data) => { queries[key[1]] = good(data); focusWrites.push(data) },
      getQueryData: key => (queries[key[1]] && queries[key[1]].data)
    },
    useQuery: opts => {
      const kind = opts.queryKey[1]
      options[kind] = opts
      queryReads.push(opts)
      if (HealthObserver && kind === 'health') {
        if (!healthObserver) {
          healthObserver = new HealthObserver(realQueryClient, opts)
          unsubscribeHealth = healthObserver.subscribe(observerChanged)
        } else {
          healthObserver.setOptions(opts)
        }
        return healthObserver.getCurrentResult()
      }
      return scopedKinds.has(kind) ? keyedQueries.get(JSON.stringify(opts.queryKey)) || loading() : queries[kind]
    }
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
  const actionCalls = []
  let actionResult = () => Promise.reject(new Error('fixture action not configured'))
  const hookCalls = []
  let hookResult = () => Promise.reject(new Error('fixture hook result not configured'))
  const retryCalls = []
  let retryResult = { ok: false, error: new Error('fixture retry not configured') }
  const sockets = []
  const pluginContext = {
    socket: (path, callback) => {
      const socket = { path, callback, closed: 0 }
      sockets.push(socket)
      return () => { socket.closed++ }
    },
    rest: (path, opts) => {
      if (path === '/actions' && opts?.method === 'POST') { actionCalls.push(opts); return actionResult() }
      if (path === '/hooks' && opts?.method === 'POST') { hookCalls.push(opts); return hookResult() }
      if (path === '/retry') {
        retryCalls.push(opts || {})
        return retryResult.ok ? Promise.resolve(retryResult.data) : Promise.reject(retryResult.error)
      }
      calls.push(path)
      return readError && path.includes(readErrorPath) ? Promise.reject(readError)
        : Promise.resolve(response)
    },
    storage: { get: key => store.get(key), set: (key, value) => { storageWrites.push({ key, value }); store.set(key, value) } },
    onDispose: fn => disposers.push(fn), registerMany: rows => registrations.push(...rows)
  }
  module.namespace.default.register(pluginContext)
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
    registrations, queryReads, storageWrites, sockets, disposers,
    registerAgain: () => module.namespace.default.register(pluginContext),
    queryFor: (key, query) => { scopedKinds.add(key[1]); keyedQueries.set(JSON.stringify(key), query) },
    actionCalls, actionResult: fn => { actionResult = fn },
    hookCalls, hookResult: fn => { hookResult = fn },
    retryCalls, retryAnswer: data => { retryResult = { ok: true, data } },
    retryFails: error => { retryResult = { ok: false, error } },
    render, text, buttons, flatten, respond: value => { response = value; readError = null },
    readFails: (path, error = new Error('private transport fixture')) => { readErrorPath = path; readError = error },
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
    healthResult: () => healthObserver?.getCurrentResult(),
    dispose: () => { stopHealthObserver(); effects.forEach(effect => effect.cleanup?.()); mountedId = null; disposers.forEach(fn => fn()) },
    unmount: () => { stopHealthObserver(); effects.forEach(effect => effect.cleanup?.()); mountedId = null },
    moveWall: ms => { time += ms },
    advance: (ms, wallMs = ms) => {
      elapsed += ms
      time += wallMs
      for (const [id, timer] of [...timers]) {
        if (timer.at <= elapsed) { timers.delete(id); timer.fn() }
      }
    } }
}


async function holdDrawer() {
  const h = await harness()
  h.queries.now = h.good({ runs: [{ pr: 7, seat: 'reviewer', head: 'aaaaaaa', state: 'waiting', why: 'waiting' }],
    counts: { waiting: 1 }, pacing: {}, available: true })
  const data = { repo: 'fixture/repo', loop: 'fixture-loop', pr: 7, cap: 3,
    stuck: [{ seat: 'reviewer', head: 'aaaaaaa', state: 'waiting', since: 1, next: 'Legacy host guidance.',
      explanation: structuredClone(holdWriterFixture) }], turns: [], verdicts: {}, rulings: [], breach: null,
    findings: [], decisions: [], read_at: 1800000000 }
  h.queries.pr = h.good(data)
  h.render('page')
  h.buttons(h.tree()).find(n => h.text([n]).includes('#7')).props.onClick()
  h.render('page')
  return { h, data }
}

test('hold contract: actual backend fixture renders unchanged without granting an action', async () => {
  const { h, data } = await holdDrawer()
  assert.ok(h.text(h.tree()).includes(holdWriterFixture.text))
  assert.match(h.text(h.tree()), /Ledger only \(partial\)/)
  h.respond(data)
  assert.equal(await h.options.pr.queryFn(), data)
  for (const explanation of Object.values(holdWriterFixtures)) {
    const next = structuredClone(data); next.stuck[0].explanation = explanation
    h.respond(next); assert.equal(await h.options.pr.queryFn(), next)
    h.queries.pr = h.good(next); h.render('page')
    assert.ok(h.text(h.tree()).includes(explanation.text))
  }
  assert.equal(h.calls.at(-1), '/pr?pr=7&loop=fixture-loop')
  assert.equal(h.retryCalls.length, 0)
  const dialog = [h.flatten(h.tree()).find(n => n.type === 'Dialog')]
  assert.equal(h.buttons(dialog).filter(n => h.text([n]) === 'Retry').length, 0)
  h.dispose()
})

test('hold contract: malformed nested provenance identity actions and secret prose are rejected before cache', async () => {
  const { h, data } = await holdDrawer()
  for (const mutate of [e => { e.schema = 2 }, e => { e.reason_code = 'ci_passed' },
    e => { e.run_id = '/home/private ghp_secret' }, e => { e.head = 'b'.repeat(40) },
    e => { e.evidence_source = 'live_github' }, e => { e.completeness = 'complete' },
    e => { e.waiting_for = '/private/token' }, e => { e.automatic_resume = 'yes' },
    e => { e.next_eligible_start = -1 }, e => { e.next_eligible_start = NaN },
    e => { e.next_eligible_start = true }, e => { e.operator_action = 'retry' },
    e => { e.text = '/home/private ghp_secret' }, e => { e.extra = 'secret' }]) {
    const broken = structuredClone(data); mutate(broken.stuck[0].explanation); h.respond(broken)
    await assert.rejects(h.options.pr.queryFn(), /Invalid pr response/)
  }
  h.dispose()
})

test('hold contract: retained stale and legacy unknown stay explicit with no transport text', async () => {
  const { h, data } = await holdDrawer()
  h.queries.pr = { ...h.good(data), dataUpdatedAt: 1 }
  h.render('page')
  assert.match(h.text(h.tree()), /stale.*PR ledger explanation/i)
  assert.ok(h.text(h.tree()).includes(holdWriterFixture.text))
  h.queries.pr = { ...h.fail(data), error: new Error('/home/private ghp_secret') }
  h.render('page')
  assert.match(h.text(h.tree()), /stale/i)
  assert.doesNotMatch(h.text(h.tree()), /home\/private|ghp_secret/)
  const legacy = structuredClone(data); delete legacy.stuck[0].explanation
  h.queries.pr = h.good(legacy); h.respond(legacy); await h.options.pr.queryFn(); h.render('page')
  assert.match(h.text(h.tree()), /Shared explanation unavailable \(older backend\)/)
  h.dispose(); assert.equal(h.timerCount(), 0)
})

function recordsFixture() { return structuredClone(recordsWriterFixture) }

const recordsRegion = h => [h.flatten(h.tree()).find(n => n.props?.['aria-label'] === 'Local loop records')].filter(Boolean)
test('records: selected local history distinguishes failures, audit attempts and remembered CI', async () => {
  const h = await harness(); h.queries.records = h.good(recordsFixture()); h.render('page')
  const region = recordsRegion(h); assert.ok(region)
  const text = h.text(region)
  for (const re of [/fixture-loop/, /Unresolved gate failures/, /not quiet gate declines/, /timeout/, /#651/, /Broker write audit/, /attempt.*unknown/, /not a complete authorization-decision log/, /Last recorded green/, /Last red notice/, /Required checks.*tests/, /not current main CI/]) assert.match(text, re)
  assert.equal(h.options.records.refetchInterval, 30000)
  assert.equal(h.options.records.staleTime, 30000)
  assert.equal(h.options.records.queryKey[2], 'fixture-loop')
  assert.equal(h.buttons(region).length, 1)
  assert.equal(h.buttons(region)[0].props.children, 'Retry')
  h.respond(recordsFixture()); await h.options.records.queryFn()
  assert.equal(h.calls.at(-1), '/records?loop=fixture-loop')
  h.dispose()
})

test('records: nested malformed and wrong-loop responses never enter the cache', async () => {
  const h = await harness(); h.queries.records = h.good(recordsFixture()); h.render('page')
  for (const mutate of [d => { d.loop = 'other' }, d => { d.failures.items = [null] },
    d => { d.broker.items[0].at = -1 }, d => { d.main_ci.items[0].required_checks = [null] },
    d => { d.failures.status = 'missing' }, d => { d.main_ci.items[0].green = '/private/token' },
    d => { d.failures.items[0].attempts = true }, d => { d.broker.items[0].outcome = 'allowed' },
    d => { d.failures.reason = '/home/private ghp_secret' },
    d => { d.main_ci.items[0].green = ''; d.main_ci.items[0].notified = '' }]) {
    const data = recordsFixture(); mutate(data); h.respond(data)
    await assert.rejects(h.options.records.queryFn(), /Invalid records response/)
  }
  h.dispose()
})

test('records: missing unreadable empty and retained stale are distinct without transport details', async () => {
  const h = await harness(); const d = recordsFixture()
  d.failures = { status: 'available', reason: 'recorded', items: [] }
  d.broker = { status: 'missing', reason: 'not_recorded', items: [] }
  d.main_ci = { status: 'unreadable', reason: 'malformed', items: [] }
  h.queries.records = h.good(d); h.render('page')
  let text = h.text(recordsRegion(h))
  for (const re of [/No unresolved failures in the read stores/, /Missing.*not recorded/, /Unreadable.*malformed/]) assert.match(text, re)
  h.advance(60000, -3600000)
  assert.match(h.text(recordsRegion(h)), /stale/i)
  h.queries.records = { ...h.fail(d), error: new Error('/home/private ghp_secret') }; h.render('page')
  assert.doesNotMatch(h.text(recordsRegion(h)), /home\/private|ghp_secret/)
  h.queries.records = { ...h.fail(undefined), error: new Error('/private/token') }; h.render('page')
  assert.match(h.text(recordsRegion(h)), /unavailable/i)
  h.dispose(); assert.equal(h.timerCount(), 0)
})

test('records: first displayed loop survives reorder removal and unreadability', async () => {
  const h = await harness()
  h.queries.loops = h.good({ loops: [{ id: 'fixture-loop' }, { id: 'other-loop' }] })
  h.render('page')
  h.queries.loops = h.good({ loops: [{ id: 'other-loop' }, { id: 'fixture-loop' }] }); h.render('page')
  assert.equal(h.options.records.queryKey[2], 'fixture-loop')
  h.queries.loops = h.good({ loops: [{ id: 'other-loop' }] }); h.render('page')
  assert.equal(h.options.records.queryKey[2], 'fixture-loop')
  assert.equal(h.options.records.enabled, false)
  assert.match(h.text(recordsRegion(h)), /Selected loop is not readable.*records not read/)
  assert.doesNotMatch(h.text(recordsRegion(h)), /timeout/)
  h.queries.loops = h.good({ loops: [{ id: 'fixture-loop', unreadable: true }, { id: 'other-loop' }] }); h.render('page')
  assert.equal(h.options.records.queryKey[2], 'fixture-loop')
  assert.equal(h.options.records.enabled, false)
  h.dispose()
})

test('records: reconciled unchanged is retained as a known audit outcome', async () => {
  const h = await harness(); h.render('page')
  const d = recordsFixture(); d.broker.items[0].phase = 'reconciled'; d.broker.items[0].outcome = 'unchanged'
  h.respond(d); h.queries.records = h.good(await h.options.records.queryFn()); h.render('page')
  assert.match(h.text(recordsRegion(h)), /reconciled.*unchanged/)
  h.dispose()
})

test('records: loop switching binds queries and removal never substitutes history', async () => {
  const h = await harness(); h.queries.records = h.good(recordsFixture())
  h.queries.loops = h.good({ loops: [{ id: 'fixture-loop' }, { id: 'other-loop' }] })
  h.queryFor(['diaktoros', 'records', 'fixture-loop'], h.good(recordsFixture()))
  h.render('page'); assert.match(h.text(recordsRegion(h)), /timeout/)
  h.buttons(h.tree()).find(b => b.props.children === 'other-loop').props.onClick()
  assert.equal(h.options.records.queryKey[2], 'other-loop')
  assert.doesNotMatch(h.text(recordsRegion(h)), /timeout/)
  assert.match(h.text(recordsRegion(h)), /loading/i)
  h.queries.loops = h.good({ loops: [{ id: 'fixture-loop' }] }); h.render('page')
  assert.equal(h.options.records.queryKey[2], 'other-loop')
  assert.doesNotMatch(h.text(recordsRegion(h)), /timeout/)
  h.dispose()
})

test('records: retry is executable only for a readable selected identity', async () => {
  // The Retry button must refetch the records query itself, for the selected loop —
  // not just anything that reports a call. Replace the generic refetch stub with
  // one that records the key it was reached through, so a different query (or a
  // refetch with the wrong loop) fails the assertion instead of passing silently.
  const refetched = []
  const h = await harness()
  h.queries.records = { ...h.good(recordsFixture()), refetch: () => refetched.push([...h.options.records.queryKey]) }
  h.render('page')
  const retry = h.buttons(recordsRegion(h))[0]
  assert.equal(retry.props.children, 'Retry')
  retry.props.onClick()
  assert.deepEqual(refetched, [['diaktoros', 'records', 'fixture-loop']])
  assert.ok(!h.calls.some(call => call && call.queryKey), 'retry refetches the records query, it does not invalidate broadly')
  h.queries.loops = h.good({ loops: [{ id: 'other-loop' }] }); h.render('page')
  const before = h.calls.length
  for (const button of h.buttons(recordsRegion(h))) button.props.onClick?.()
  assert.equal(h.buttons(recordsRegion(h)).length, 0)
  assert.equal(h.calls.length, before)
  h.queries.loops = h.good({ loops: [{ id: 'fixture-loop', unreadable: true }] }); h.render('page')
  assert.equal(h.buttons(recordsRegion(h)).length, 0)
  assert.equal(h.options.records.enabled, false)
  h.dispose()
  const empty = await harness(); empty.queries.loops = empty.good({ loops: [] }); empty.render('page')
  assert.equal(empty.buttons(recordsRegion(empty)).length, 0)
  assert.equal(empty.calls.length, 0)
  empty.dispose()
})

test('records: observer and watchdog nested memory is validated and disclosed', async () => {
  const h = await harness(); h.render('page'); const d = recordsFixture()
  // The shared fixture is asserted equal to production projections of real writers in Python.
  h.respond(d); h.queries.records = h.good(await h.options.records.queryFn()); h.render('page')
  for (const re of [/Observer.*local feed/, /Recorded status: delivered/, /not independent proof of delivery/, /Watchdog.*local memory/, /not live running or health/, /Cooldown stamp/, /head_history/]) assert.match(h.text(recordsRegion(h)), re)
  for (const mutate of [x => { x.observer.items[0].status = 'sent' }, x => { x.observer.items[0].attempts = true }, x => { x.observer.items[0].issue = 'false' }, x => { x.watchdog.items[0].at = -1 }, x => { x.watchdog.items[1].observed_at = 'private' }, x => { x.watchdog.items[2].category = '/home/private' }, x => { x.watchdog.items[2].head7 = 'abcdefg' }, x => { x.observer = null }]) {
    const bad = structuredClone(d); mutate(bad); h.respond(bad)
    await assert.rejects(h.options.records.queryFn(), /Invalid records response/)
  }
  h.dispose()
})

// #646: exercise the registered pane without mounting the page or its action components.
const PANE = 'right-now-pane'
const nowKey = loop => ['diaktoros', 'now', loop]
const paneSelect = h => h.flatten(h.tree()).find(node => node.type === 'select')
const paneRuns = (pr = 646) => stalled([retrying({ pr, why: 'passive fixture row' })])
function assertPassivePane(h) {
  assert.equal(h.buttons(h.tree()).length, 0, 'no refresh, drawer, retry or action buttons')
  assert.equal(h.flatten(h.tree()).some(n => ['Dialog', 'ConfirmDialog', 'a'].includes(n.type)), false)
  assert.deepEqual([...new Set(h.queryReads.map(q => q.queryKey[1]))].sort(), ['loops', 'now'])
  assert.deepEqual(h.calls, [], 'no REST, refetch or invalidation from rendering/selection/expiry')
  for (const list of [h.notices, h.navigations, h.storageWrites, h.focusWrites(), h.actionCalls, h.hookCalls, h.retryCalls]) {
    assert.deepEqual(list, [])
  }
  assert.equal(h.store.size, 0)
}

test('pane: independent registration docks below workspace at a bounded height', async () => {
  const h = await harness()
  const registration = h.registrations.find(r => r.id === PANE)
  assert.ok(registration)
  assert.equal(registration.area, 'panes')
  assert.equal(registration.title, 'Diaktoros · Right now')
  assert.deepEqual(JSON.parse(JSON.stringify(registration.data)), {
    placement: 'bottom', dock: { pane: 'workspace', pos: 'bottom' }, height: '200px'
  })
  const tree = h.render(PANE)
  assert.match(h.text(tree), /Right now.*Read-only · fixture-loop/)
  const region = h.flatten(tree).find(n => n.props?.role === 'region')
  assert.equal(region.props['aria-label'], 'Diaktoros Right now')
  assert.equal(region.props.tabIndex, 0)
  assert.match(region.props.className, /min-h-0.*min-w-0.*overflow-y-auto/)
  assertPassivePane(h)
  h.dispose()
  assert.equal(h.timerCount(), 0)
  assert.equal(h.listenerCount(), 0)
})

test('pane: shares page now key, enabled, staleTime, interval and validated encoded query', async () => {
  const id = 'loop /?&'
  const h = await harness()
  h.queries.loops = h.good({ loops: [{ id }] })
  h.render(PANE)
  const paneQuery = h.options.now
  h.render('page')
  const pageQuery = h.options.now
  for (const query of [paneQuery, pageQuery]) {
    assert.equal(JSON.stringify(query.queryKey), JSON.stringify(nowKey(id)))
    assert.equal(query.enabled, true)
    assert.equal(query.staleTime, 30000)
    assert.equal(query.refetchInterval, 30000)
    h.respond({})
    await assert.rejects(query.queryFn(), /Invalid now response/)
    const data = paneRuns()
    h.respond(data)
    assert.equal(await query.queryFn(), data)
    assert.equal(h.calls.at(-1), '/now?loop=' + encodeURIComponent(id))
    h.respond({ runs: [], counts: { running: 1 }, available: false })
    await assert.rejects(query.queryFn(), /Invalid now response/)
  }
  h.unmount()
})

for (const scenario of ['loading', 'error', 'empty']) {
  test(`pane: ${scenario} discovery gates now and never leaks cached runs`, async () => {
    const h = await harness()
    h.queries.now = h.good(paneRuns())
    h.queries.loops = scenario === 'loading' ? h.loading() : scenario === 'error' ? h.fail(undefined) : h.good({ loops: [] })
    const text = h.text(h.render(PANE))
    assert.equal(h.options.now.enabled, false)
    assert.equal(h.options.now.queryKey[2], '')
    assert.doesNotMatch(text, /#646|No active or recent failed/)
    assert.match(text, scenario === 'loading' ? /Configured loops: loading/ : scenario === 'error' ? /Configured loops: no verified data available/ : /verified-empty.*No loops configured/)
    if (scenario !== 'empty') assert.doesNotMatch(text, /No loops configured|verified-empty/)
    assertPassivePane(h)
    h.unmount()
  })
}

test('pane: first loading/error, recovery, retained rows and timestamp are truthful and bounded', async () => {
  const h = await harness()
  h.queries.now = h.loading()
  assert.match(h.text(h.render(PANE)), /Right now: loading/)
  assert.doesNotMatch(h.text(h.tree()), /verified-empty|No active or recent failed|idle/)
  h.queries.now = { ...h.fail(undefined), error: new Error('/private/token ghp_secret') }
  assert.match(h.text(h.render(PANE)), /unavailable.*Backend read failed/)
  assert.doesNotMatch(h.text(h.tree()), /private\/token|ghp_secret|verified-empty|idle/)
  const data = paneRuns()
  h.queries.now = h.good(data)
  const stamp = new Date(h.queries.now.dataUpdatedAt).toLocaleString()
  assert.match(h.text(h.render(PANE)), /#646.*passive fixture row/)
  h.advance(10000)
  h.queries.now = { ...h.queries.now, isFetching: true }
  assert.match(h.text(h.render(PANE)), /refreshing….*#646/)
  h.queries.now = { ...h.queries.now, isError: true, error: new Error('/private/token ghp_secret') }
  const text = h.text(h.render(PANE))
  assert.match(text, /stale.*showing last good data, not current status.*#646/)
  assert.ok(text.includes('Last good read: ' + stamp))
  assert.doesNotMatch(JSON.stringify(h.tree()), /private\/token|ghp_secret|No active or recent failed/)
  assertPassivePane(h)
  h.unmount()
  assert.equal(h.timerCount(), 0)
})

test('pane: verified empty differs from missing ledger and pending counts without rows', async () => {
  const h = await harness()
  h.queries.now = h.good({ runs: [], counts: {}, available: true })
  assert.match(h.text(h.render(PANE)), /verified-empty.*No active or recent failed/)
  h.queries.now = h.good({ runs: [], counts: {}, available: false })
  let text = h.text(h.render(PANE))
  assert.match(text, /unavailable.*No run ledger available.*unavailable, not zero/)
  assert.doesNotMatch(text, /verified-empty|idle|No active or recent failed/)
  h.queries.now = h.good({ runs: [], counts: { pending: 2, waiting: 3 }, available: true })
  text = h.text(h.render(PANE))
  assert.match(text, /2 pending · 3 retry-waiting/)
  assert.doesNotMatch(text, /verified-empty|idle|No active or recent failed/)
  assertPassivePane(h)
  h.unmount()
})

for (const allUnreadable of [false, true]) {
  test(`pane: ${allUnreadable ? 'all' : 'mixed'} unreadable loops are named and never selected`, async () => {
    const h = await harness()
    h.queries.loops = h.good({ loops: [UNREADABLE, ...(allUnreadable ? [] : [{ id: 'widgets' }])] })
    const text = h.text(h.render(PANE))
    assert.match(text, /typo · config unreadable: bad json/)
    assert.doesNotMatch(text, /No loops configured/)
    assert.equal(h.options.now.enabled, !allUnreadable)
    assert.equal(h.options.now.queryKey[2], allUnreadable ? '' : 'widgets')
    assert.equal(paneSelect(h), undefined, 'unreadable configuration is not an option')
    if (allUnreadable) assert.match(text, /No readable loop selected; run status is not read/)
    assertPassivePane(h)
    h.unmount()
  })
}

for (const disappearance of ['removed', 'unreadable']) test(`pane: switching and ${disappearance} selection never show another loop cache rows`, async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queryFor(nowKey('widgets'), h.good(paneRuns(646)))
  h.render(PANE)
  assert.match(h.text(h.tree()), /#646/)
  paneSelect(h).props.onChange({ target: { value: 'gadgets' } })
  assert.equal(h.options.now.queryKey[2], 'gadgets')
  assert.match(h.text(h.tree()), /Right now: loading/)
  assert.doesNotMatch(h.text(h.tree()), /#646/)
  h.queryFor(nowKey('gadgets'), h.good(paneRuns(647)))
  h.render(PANE)
  assert.match(h.text(h.tree()), /#647/)
  assert.doesNotMatch(h.text(h.tree()), /#646/)
  h.queries.loops = h.good({ loops: [{ id: 'widgets' }, ...(disappearance === 'unreadable' ? [{ ...UNREADABLE, id: 'gadgets' }] : [])] })
  let text = h.text(h.render(PANE))
  assert.match(text, /gadgets · not read: selected loop is no longer readable or configured/)
  assert.doesNotMatch(text, /#646|#647/)
  assert.equal(h.options.now.enabled, false)
  assert.equal(h.options.now.queryKey[2], '')
  assert.equal(paneSelect(h).props.value, 'gadgets')
  paneSelect(h).props.onChange({ target: { value: 'widgets' } })
  text = h.text(h.tree())
  assert.match(text, /#646/)
  assert.doesNotMatch(text, /#647/)
  assertPassivePane(h)
  h.unmount()
})

for (const change of ['removed', 'unreadable', 'reordered']) test('pane: initial selection survives discovery ' + change, async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queryFor(nowKey('widgets'), h.good(paneRuns(646)))
  h.queryFor(nowKey('gadgets'), h.good(paneRuns(647)))
  h.render(PANE)
  assert.match(h.text(h.tree()), /#646/)
  h.queries.loops = h.good({ loops: change === 'reordered' ? [...TWO_LOOPS.loops].reverse() :
    [{ id: 'gadgets' }, ...(change === 'unreadable' ? [{ ...UNREADABLE, id: 'widgets' }] : [])] })
  const text = h.text(h.render(PANE))
  assert.doesNotMatch(text, /#647/)
  if (change === 'reordered') assert.match(text, /#646/)
  else { assert.match(text, /widgets · not read/); assert.equal(h.options.now.enabled, false) }
  h.unmount()
})

for (const replacement of ['timestamp', 'identity']) {
  test(`pane: monotonic expiry and ${replacement} replacement re-arm without stale timer races`, async () => {
    const h = await harness()
    h.render(PANE)
    const oldTimers = h.pendingTimers()
    assert.ok(oldTimers.length > 0)
    h.advance(59999, -3600000)
    assert.match(h.text(h.tree()), /verified-empty.*No active or recent failed/)
    h.advance(1, 0)
    assert.match(h.text(h.tree()), /stale/)
    assert.doesNotMatch(h.text(h.tree()), /No active or recent failed/)
    h.moveWall(-3600000)
    h.render(PANE)
    assert.doesNotMatch(h.text(h.tree()), /verified-empty/)
    h.queries.loops = h.good(h.queries.loops.data)
    h.queries.now = replacement === 'timestamp' ? h.good(h.queries.now.data) : {
      ...h.queries.now, data: { runs: [], counts: {}, available: true }
    }
    h.render(PANE)
    oldTimers.forEach(fn => fn())
    assert.match(h.text(h.tree()), /verified-empty.*No active or recent failed/)
    h.advance(59999, 0)
    assert.match(h.text(h.tree()), /verified-empty/)
    h.advance(1, -60000)
    assert.doesNotMatch(h.text(h.tree()), /verified-empty|No active or recent failed/)
    assertPassivePane(h)
    h.dispose()
    assert.equal(h.timerCount(), 0)
    assert.equal(h.listenerCount(), 0)
  })
}

test('pane: cached discovery failure retains rows but discloses stale discovery and bounded errors', async () => {
  const h = await harness()
  h.queries.now = h.good(paneRuns())
  h.render(PANE)
  h.queries.loops = { ...h.fail(h.queries.loops.data), error: { code: 'INVALID_RESPONSE', message: 'private-secret' } }
  const text = h.text(h.render(PANE))
  assert.match(text, /Configured loops: showing last good data/)
  assert.match(text, /Backend response could not be verified/)
  assert.match(text, /#646/)
  assert.doesNotMatch(text, /private-secret/)
  for (const node of h.flatten(h.tree())) {
    if (typeof node === 'object') assert.doesNotMatch(node.props.title || '', /private-secret/)
  }
  assertPassivePane(h)
  h.unmount()
})

test('pane: pending old expiry cannot expire a newer shared-data read', async () => {
  const h = await harness()
  h.render(PANE)
  const oldTimers = h.pendingTimers()
  h.advance(30000)
  h.queries.now = h.good(h.queries.now.data)
  h.queries.loops = h.good(h.queries.loops.data)
  h.render(PANE)
  oldTimers.forEach(fn => fn())
  h.advance(30000, -3600000)
  assert.match(h.text(h.tree()), /verified-empty.*No active or recent failed/)
  h.advance(29999, 0)
  assert.match(h.text(h.tree()), /verified-empty/)
  h.advance(1, 0)
  assert.doesNotMatch(h.text(h.tree()), /verified-empty|No active or recent failed/)
  assertPassivePane(h)
  h.unmount()
  assert.equal(h.timerCount(), 0)
})

test('pane: unmount clears pending freshness timers without reads or side effects', async () => {
  const h = await harness()
  h.queries.now = h.good(paneRuns())
  h.render(PANE)
  assert.ok(h.timerCount() > 0)
  assertPassivePane(h)
  h.unmount()
  assert.equal(h.timerCount(), 0)
  h.advance(120000)
  assertPassivePane(h)
})

const hookButton = h => h.buttons(h.tree()).find(b => /^(Pause|Resume) webhook delivery$/.test(b.props.children))
const hookDialog = h => h.flatten(h.tree()).find(n => n.type === 'ConfirmDialog' && /webhook delivery/.test(n.props.title))
const hookView = (overrides = {}) => ({ loop: 'fixture-loop', repo: 'fixture/repo', state: 'armed', eligible: true, confirmation: 'opaque-snapshot', reason: '', read_at: 1800000000, ...overrides })

for (const [state, reason] of [
  ['unknown', 'Hook state could not be verified.'],
  ['unknown', 'Required loop hooks are missing.'],
  ['unknown', 'Loop hooks are mixed or not correctly wired; inspect them first.'],
  ['paused', 'Hooks are paused; repair hook wiring before resuming.'],
  ['paused', 'All loop hooks are paused.'],
  ['armed', 'All loop hooks are armed.'],
  ['armed', 'All loop hooks are active; repair hook wiring before resuming.'],
]) {
  test(`hooks: renders bounded ${state} reason: ${reason}`, async () => {
    const h = await harness(); h.render('page')
    h.respond(hookView({ state, reason, eligible: false, confirmation: null }))
    h.queries.hooks = h.good(await h.options.hooks.queryFn())
    h.render('page')
    assert.ok(h.text(h.tree()).includes('Loop fixture-loop · ' + state + ' · ' + reason))
    assert.equal(hookButton(h), undefined)
    h.unmount()
  })
}

for (const reason of ['/private/token.json ghp_SECRET', '<img src=x onerror=alert(1)>', 'Bearer SECRET', 'Unexpected transport failure']) {
  test(`hooks: unrecognized reason is sanitized before cache: ${reason}`, async () => {
    const h = await harness(); h.render('page')
    h.respond(hookView({ state: 'unknown', reason, eligible: false, confirmation: null }))
    const data = await h.options.hooks.queryFn()
    assert.equal(data.reason, 'Hook state could not be verified.')
    h.queries.hooks = h.good(data); h.render('page')
    assert.ok(!h.text(h.tree()).includes(reason))
    assert.ok(h.text(h.tree()).includes('unknown · Hook state could not be verified.'))
    h.unmount()
  })
}

test('hooks: armed miswired reason survives validation, Pause confirms, paused miswired cannot Resume', async () => {
  const h = await harness(); h.render('page')
  const armedReason = 'All loop hooks are active; repair hook wiring before resuming.'
  const pausedReason = 'Hooks are paused; repair hook wiring before resuming.'
  h.respond(hookView({ reason: armedReason }))
  const armed = await h.options.hooks.queryFn()
  assert.equal(armed.reason, armedReason)
  h.queries.hooks = h.good(armed); h.render('page')
  assert.ok(h.text(h.tree()).includes('armed · ' + armedReason))
  assert.equal(hookButton(h).props.children, 'Pause webhook delivery')
  const paused = hookView({ state: 'paused', eligible: false, confirmation: null, reason: pausedReason })
  h.hookResult(() => Promise.resolve({ ...paused, ok: true, answer: 'Paused and verified' }))
  hookButton(h).props.onClick()
  const dialog = hookDialog(h)
  assert.equal(dialog.props.title, 'Pause webhook delivery?')
  assert.equal(dialog.props.description, 'Pause webhook delivery for loop fixture-loop. Pausing stops GitHub webhook delivery, not in-flight turns. Paused hooks deliver no new events to queue. The sweeper may recover events even while paused. Resuming allows new webhook delivery; it does not restart completed turns.')
  await dialog.props.onConfirm()
  assert.equal(JSON.stringify(h.hookCalls), JSON.stringify([{ method: 'POST', body: { loop: 'fixture-loop', action: 'pause', confirmation: 'opaque-snapshot' } }]))
  assert.match(h.text(h.tree()), /Backend verified webhook delivery paused/)
  assert.equal(hookButton(h), undefined)
  h.respond(paused)
  const refreshed = await h.options.hooks.queryFn()
  assert.equal(refreshed.reason, pausedReason)
  h.queries.hooks = h.good(refreshed); h.render('page')
  assert.ok(h.text(h.tree()).includes('paused · ' + pausedReason))
  assert.equal(hookButton(h), undefined, 'repair is required before Resume can be offered')
  await dialog.props.onConfirm()
  assert.equal(h.hookCalls.length, 1, 'retained confirmation cannot resume paused miswired hooks')
  h.unmount()
})

for (const reason of ['All loop hooks are active; repair hook wiring before resuming. ', 'all loop hooks are active; repair hook wiring before resuming.', 'All loop hooks are active; repair hook wiring before resuming. /private/token']) {
  test(`hooks: reason allowlist rejects near-match: ${reason}`, async () => {
    const h = await harness(); h.render('page')
    h.respond(hookView({ reason }))
    assert.equal((await h.options.hooks.queryFn()).reason, 'Hook state could not be verified.')
    h.unmount()
  })
}

for (const state of ['armed', 'paused']) {
  test(`hooks: ${state} confirms exact payload, no optimistic state, duplicate confirm blocked`, async () => {
    const h = await harness()
    h.queries.hooks = h.good(hookView({ state }))
    let finish
    h.hookResult(() => new Promise(resolve => { finish = resolve }))
    h.render('page')
    hookButton(h).props.onClick()
    const dialog = hookDialog(h)
    assert.match(dialog.props.description, /loop fixture-loop/)
    assert.match(dialog.props.description, /not in-flight turns/)
    assert.match(dialog.props.description, /Paused hooks deliver no new events to queue/)
    assert.match(dialog.props.description, /sweeper may recover events/)
    assert.equal(h.hookCalls.length, 0)
    const first = dialog.props.onConfirm()
    await dialog.props.onConfirm()
    assert.equal(h.hookCalls.length, 1)
    const action = state === 'armed' ? 'pause' : 'resume'
    assert.equal(JSON.stringify(h.hookCalls[0]), JSON.stringify({ method: 'POST', body: { loop: 'fixture-loop', action, confirmation: 'opaque-snapshot' } }))
    assert.doesNotMatch(h.text(h.tree()), /Backend verified webhook delivery/)
    finish(hookView({ state: action === 'pause' ? 'paused' : 'armed', ok: true, answer: 'Verified by host' }))
    await first
    assert.match(h.text(h.tree()), /Backend verified webhook delivery/)
    assert.equal(h.queries.hooks.data.state, state, 'does not optimistically change cached status')
    assert.equal(hookButton(h), undefined, 'consumed read cannot authorize another write')
    assert.ok(h.calls.some(c => c?.queryKey?.[1] === 'hooks'))
    h.queries.hooks = h.good(hookView({ state: action === 'pause' ? 'paused' : 'armed', confirmation: 'new-snapshot' }))
    h.render('page')
    assert.ok(hookButton(h))
    assert.equal(h.buttons(h.tree()).some(b => /merge/i.test(String(b.props.children))), false)
  })
  test(`hooks: ${state} cancel and retained confirm handler send nothing`, async () => {
    const h = await harness(); h.queries.hooks = h.good(hookView({ state })); h.render('page')
    hookButton(h).props.onClick(); const d = hookDialog(h)
    d.props.onClose(); await d.props.onConfirm()
    assert.equal(hookDialog(h), undefined); assert.equal(h.hookCalls.length, 0)
  })
}

for (const scenario of ['loading', 'first-error', 'cached-error', 'refreshing', 'stale', 'no-timestamp', 'unknown', 'ineligible', 'discovery-error', 'discovery-refresh']) {
  test(`hooks: ${scenario} never exposes a mutation`, async () => {
    const h = await harness(); const data = hookView()
    if (scenario === 'loading') h.queries.hooks = h.loading()
    if (scenario === 'first-error') h.queries.hooks = h.fail(undefined)
    if (scenario === 'cached-error') h.queries.hooks = h.fail(data)
    if (scenario === 'refreshing') h.queries.hooks = { ...h.good(data), isFetching: true }
    if (scenario === 'stale') h.queries.hooks = { ...h.good(data), dataUpdatedAt: 1799999940000 }
    if (scenario === 'no-timestamp') h.queries.hooks = { ...h.good(data), dataUpdatedAt: 0 }
    if (scenario === 'unknown') h.queries.hooks = h.good(hookView({ state: 'unknown', eligible: false, confirmation: null }))
    if (scenario === 'ineligible') h.queries.hooks = h.good(hookView({ eligible: false, confirmation: null }))
    if (scenario === 'discovery-error') h.queries.loops = h.fail(h.queries.loops.data)
    if (scenario === 'discovery-refresh') h.queries.loops.isFetching = true
    h.render('page'); assert.equal(hookButton(h), undefined)
    assert.equal(h.hookCalls.length, 0)
    if (scenario === 'unknown') assert.match(h.text(h.tree()), /Loop fixture-loop · unknown/)
  })
}

for (const changed of ['time', 'identity', 'timestamp', 'refreshing', 'error', 'loop', 'unmount']) {
  test(`hooks: confirmation is refused after ${changed}`, async () => {
    const h = await harness(); h.queries.loops = h.good({ loops: [{ id: 'fixture-loop' }, { id: 'other-loop' }] }); h.render('page')
    hookButton(h).props.onClick(); const confirm = hookDialog(h).props.onConfirm
    if (changed === 'time') h.advance(60000, -3600000)
    if (changed === 'identity') h.queries.hooks = h.good(hookView({ confirmation: 'replacement' }))
    if (changed === 'timestamp') h.queries.hooks = { ...h.queries.hooks, dataUpdatedAt: 1800000000001 }
    if (changed === 'refreshing') h.queries.hooks.isFetching = true
    if (changed === 'error') h.queries.hooks = h.fail(h.queries.hooks.data)
    if (changed === 'loop') h.buttons(h.tree()).find(b => b.props.children === 'other-loop').props.onClick()
    if (changed === 'unmount') h.unmount(); else h.render('page')
    await confirm(); assert.equal(h.hookCalls.length, 0)
  })
}

for (const scenario of ['409', '422', 'network', 'malformed', 'false', 'unknown', 'wrong-state']) {
  test(`hooks: ${scenario} write failure is bounded and requires a new read`, async () => {
    const h = await harness()
    h.hookResult(() => {
      if (['409', '422', 'network'].includes(scenario)) return Promise.reject(new Error(scenario + ': {"detail":"/private/token ghp_secret"}'))
      return Promise.resolve(scenario === 'malformed' ? {} : hookView({ ok: scenario !== 'false', answer: 'host answer', state: scenario === 'unknown' ? 'unknown' : scenario === 'wrong-state' ? 'armed' : 'paused', eligible: scenario !== 'unknown', confirmation: scenario === 'unknown' ? null : 'opaque' }))
    })
    h.render('page'); hookButton(h).props.onClick(); await hookDialog(h).props.onConfirm()
    assert.doesNotMatch(h.text(h.tree()), /Backend verified webhook delivery|private\/token|ghp_secret/)
    if (scenario === '409' || scenario === '422') assert.match(h.text(h.tree()), new RegExp('HTTP ' + scenario))
    assert.equal(hookButton(h), undefined)
    assert.equal(hookDialog(h), undefined)
  })
}

test('hooks: old dialog cannot confirm or cancel a replacement confirmation', async () => {
  const h = await harness(); h.render('page'); hookButton(h).props.onClick(); const old = hookDialog(h)
  old.props.onClose(); hookButton(h).props.onClick(); const replacement = hookDialog(h)
  await old.props.onConfirm(); old.props.onClose()
  assert.equal(h.hookCalls.length, 0); assert.equal(hookDialog(h).props.description, replacement.props.description)
  replacement.props.onClose()
})

test('hooks: late write response after loop switch never appears in another loop', async () => {
  const h = await harness(); h.queries.loops = h.good({ loops: [{ id: 'fixture-loop' }, { id: 'other-loop' }] })
  let finish; h.hookResult(() => new Promise(resolve => { finish = resolve }))
  h.render('page'); hookButton(h).props.onClick(); const task = hookDialog(h).props.onConfirm()
  h.buttons(h.tree()).find(b => b.props.children === 'other-loop').props.onClick()
  finish(hookView({ ok: true, state: 'paused', answer: 'Verified' })); await task
  assert.doesNotMatch(h.text(h.tree()), /Backend verified webhook delivery/)
})

test('hooks: malformed POST identities, status and answer fields never produce verified success', async () => {
  const mutations = [d => { d.loop = 'other-loop' }, d => { d.ok = 'yes' }, d => { delete d.ok }, d => { d.answer = '' }, d => { d.answer = null }, d => { d.answer = 'x'.repeat(401) }, d => { d.answer = 'bad\nanswer' }, d => { d.state = 'missing' }, d => { d.read_at = Infinity }, d => { d.confirmation = 7 }, d => { d.eligible = 'yes' }, d => { d.reason = null }, d => { d.repo = null }]
  for (const mutate of mutations) {
    const h = await harness(); const data = hookView({ ok: true, answer: 'Verified', state: 'paused' }); mutate(data)
    h.hookResult(() => Promise.resolve(data)); h.render('page'); hookButton(h).props.onClick()
    await hookDialog(h).props.onConfirm()
    assert.match(h.text(h.tree()), /hook answer could not be verified/)
    assert.doesNotMatch(h.text(h.tree()), /Backend verified webhook delivery/)
    assert.equal(hookButton(h), undefined)
  }
})

test('hooks: selected loop is encoded in GET and refresh-error recovery needs verified read', async () => {
  const h = await harness(); const id = 'loop /?&'
  h.queries.loops = h.good({ loops: [{ id }] }); h.queries.hooks = h.fail(undefined)
  h.render('page'); assert.equal(hookButton(h), undefined)
  h.respond(hookView({ loop: id })); const data = await h.options.hooks.queryFn()
  assert.equal(h.calls.at(-1), '/hooks?loop=' + encodeURIComponent(id))
  h.queries.hooks = h.good(data); h.render('page'); assert.ok(hookButton(h))
})

test('hooks: GET validates all fields before cache and scopes encoded loop', async () => {
  const h = await harness(); h.render('page')
  const mutations = [d => { d.loop = 'other' }, d => { d.repo = null }, d => { d.state = 'missing' }, d => { d.eligible = 'true' }, d => { d.reason = null }, d => { d.reason = 'x'.repeat(401) }, d => { d.confirmation = '' }, d => { d.confirmation = 1 }, d => { d.confirmation = null }, d => { d.read_at = NaN }, d => { d.read_at = -1 }, d => { d.state = 'unknown' }, d => { delete d.read_at }]
  for (const mutate of mutations) {
    const data = hookView(); mutate(data); h.respond(data)
    await assert.rejects(h.options.hooks.queryFn(), /Invalid hooks response/)
  }
  for (const data of [null, [], hookView({ state: 'unknown', eligible: true, confirmation: null })]) {
    h.respond(data); await assert.rejects(h.options.hooks.queryFn(), /Invalid hooks response/)
  }
  for (const data of [hookView(), hookView({ state: 'paused' }), hookView({ state: 'unknown', eligible: false, confirmation: null })]) {
    h.respond(data); assert.equal((await h.options.hooks.queryFn()).state, data.state)
  }
  assert.equal(h.calls.at(-1), '/hooks?loop=fixture-loop')
})

test('hooks: expired read stays expired across clock rollback and fresh replacement restores controls', async () => {
  const h = await harness(); h.render('page'); assert.ok(hookButton(h))
  h.advance(59999, -3600000); assert.ok(hookButton(h))
  h.advance(1, 0); assert.equal(hookButton(h), undefined)
  h.moveWall(-3600000); h.render('page'); assert.equal(hookButton(h), undefined)
  h.queries.hooks = h.good(hookView()); h.queries.loops = h.good(h.queries.loops.data)
  h.render('page'); assert.ok(hookButton(h)); h.unmount(); assert.equal(h.timerCount(), 0)
})

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
    // Health legitimately owns one freshness wake-up; advance past its ten-minute read so
    // every mounted expiry timer has fired before asserting none were left behind.
    h.advance(600000, 0)
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

test('page: the drawer says when its turns and rulings are the newest of more, and only then', async () => {
  const h = await harness()
  h.queries.loops = h.good(TWO_LOOPS)
  h.queries.now = h.good({ runs: [{ pr: 42, seat: 'reviewer', head: 'abcdef0', state: 'waiting', why: 'waiting to retry' }],
    counts: { waiting: 1 }, pacing: {}, available: true })
  const turn = n => ({ seat: 'reviewer', head: 'abcdef' + n, state: 'succeeded', created: 1800000000 + n, updated: 1800000000 + n })
  const ruling = n => ({ verdict: 'V' + n, head: 'abcdef' + n, created: 1800000000 + n })
  const pr = { repo: 'acme/widgets', loop: 'widgets', pr: 42, cap: 3, stuck: [],
    turns: [turn(1), turn(2), turn(3)], turns_total: 3, verdicts: {},
    rulings: [ruling(1), ruling(2), ruling(3)], rulings_total: 3,
    breach: null, findings: [], decisions: [], read_at: 1800000000 }
  // Cut: the ledger holds 12 turns and 9 rulings, the view carries the newest three of each.
  h.queries.pr = h.good({ ...pr, turns_total: 12, rulings_total: 9 })
  let tree = h.render('page')
  h.buttons(tree).find(n => h.text([n]).includes('#42')).props.onClick()
  let text = h.text(h.render('page'))
  assert.match(text, /Turns \(newest 3 of 12\)/)
  assert.match(text, /… 9 older turn\(s\) not shown/)
  assert.doesNotMatch(text, /Turns \(3\)/)
  assert.match(text, /Adjudicator rulings \(newest 3 of 9\): V1 on abcdef1 · V2 on abcdef2 · V3 on abcdef3/)
  // Not cut: the plain count and no notice, as before.
  h.queries.pr = h.good(pr)
  text = h.text(h.render('page'))
  assert.match(text, /Turns \(3\)/)
  assert.doesNotMatch(text, /not shown/)
  assert.doesNotMatch(text, /newest/)
  assert.match(text, /Adjudicator rulings: V1 on abcdef1/)
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



test('actions: backend refusal accepts empty opaque identity and invalid PR input never reads', async () => {
  const h = await harness(); selectAction(h)
  await previewAction(h, actionPlan({ eligible: false, confirmation: '', reason: 'no adjudicator configured' }))
  assert.match(h.text(h.tree()), /no adjudicator configured/)
  const input = () => h.flatten(h.tree()).find(n => n.type === 'Input' && n.props['aria-label'] === 'Action PR number')
  for (const value of ['0', '-1', '9.1', '1e3', ' 937 ', '999999999999999999999']) {
    input().props.onChange({ target: { value } }); const before = h.calls.length
    assert.equal(actionButton(h, 'Preview escalation').props.disabled, true)
    await actionButton(h, 'Preview escalation').props.onClick(); assert.equal(h.calls.length, before)
  }
})

test('actions: re-review and drain draw no control at all — only escalation is offered here', async () => {
  const h = await harness(); h.render('page')
  assert.ok(actionButton(h, 'Escalate'))
  // A control that can never be honoured is not drawn; the CLI verbs are named instead.
  for (const label of ['Re-review', 'Drain queue', 'reviewer', 'fixer']) assert.equal(actionButton(h, label), undefined)
  assert.match(h.text(h.tree()), /Re-review · host CLI only/)
  assert.match(h.text(h.tree()), /Drain queue · host CLI only/)
  assert.match(h.text(h.tree()), /hermes dk review/)
  assert.match(h.text(h.tree()), /hermes dk drain/)
})

test('actions: late write outcome after loop switch is discarded', async () => {
  const h = await harness(); h.queries.loops = h.good({ loops: [{ id: 'fixture-loop' }, { id: 'other-loop' }] })
  selectAction(h); await previewAction(h); let finish
  h.actionResult(() => new Promise(resolve => { finish = resolve }))
  actionButton(h, 'Confirm escalation').props.onClick(); const task = actionDialog(h).props.onConfirm()
  actionButton(h, 'other-loop').props.onClick()
  finish({ loop: 'fixture-loop', repo: 'fixture/repo', read_at: 1800000000, head: 'a'.repeat(40), ok: true, status: 'verified', reason: '' }); await task
  assert.doesNotMatch(h.text(h.tree()), /Backend verified escalation/)
})

test('actions: old cancelled dialog cannot cancel replacement and backward-clock expiry is sticky', async () => {
  const h = await harness(); selectAction(h); await previewAction(h)
  actionButton(h, 'Confirm escalation').props.onClick(); const old = actionDialog(h)
  old.props.onClose(); actionButton(h, 'Confirm escalation').props.onClick(); old.props.onClose(); await old.props.onConfirm()
  // An identical preview is the same authorization; cancel then reopen does not retarget it.
  assert.ok(actionDialog(h)); assert.equal(h.actionCalls.length, 0)
  h.advance(60000, -3600000); assert.equal(actionButton(h, 'Confirm escalation'), undefined)
  h.moveWall(-3600000); h.render('page'); assert.equal(actionButton(h, 'Confirm escalation'), undefined)
})

// One-shot actions use independent host previews, never a /now state label.
const actionPlan = (extra = {}) => ({ loop: 'fixture-loop', repo: 'fixture/repo', read_at: 1800000000,
  action: 'escalate', pr: 937, seat: null, eligible: true, head: 'a'.repeat(40), reason: '', confirmation: ' opaque/identity+= ', ...extra })
const actionButton = (h, label) => h.buttons(h.tree()).find(b => b.props.children === label)
const actionDialog = h => h.flatten(h.tree()).find(n => n.type === 'ConfirmDialog' && n.props.title === 'Escalate this PR?')
const selectAction = (h, pr = '937') => {
  h.render('page')
  actionButton(h, 'Escalate').props.onClick()
  h.flatten(h.tree()).find(n => n.type === 'Input' && n.props['aria-label'] === 'Action PR number').props.onChange({ target: { value: pr } })
}
const previewAction = async (h, data = actionPlan()) => { h.respond(data); await actionButton(h, 'Preview escalation').props.onClick() }

test('actions: independent PR preview confirms full target and opaque identity exactly once', async () => {
  const h = await harness(); selectAction(h); await previewAction(h)
  assert.equal(h.calls.at(-1), '/actions?loop=fixture-loop&action=escalate&pr=937')
  actionButton(h, 'Confirm escalation').props.onClick(); const d = actionDialog(h)
  for (const re of [/fixture-loop/, /fixture\/repo/, /#937/, /a{40}/, /adjudicator/, /parks this head against further review and fix/, /spends an adjudicator turn/, /hermes dk escalate --loop fixture-loop --pr 937/]) assert.match(d.props.description, re)
  let finish; h.actionResult(() => new Promise(resolve => { finish = resolve }))
  const task = d.props.onConfirm(); await d.props.onConfirm()
  assert.equal(h.actionCalls.length, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(h.actionCalls[0])), { method: 'POST', body: { loop: 'fixture-loop', action: 'escalate', pr: 937, seat: null, confirmation: ' opaque/identity+= ' } })
  finish({ loop: 'fixture-loop', repo: 'fixture/repo', read_at: 1800000000, head: 'a'.repeat(40), ok: true, status: 'verified', reason: '' }); await task
  assert.match(h.text(h.tree()), /Backend verified escalation/)
  assert.equal(actionButton(h, 'Confirm escalation'), undefined)
  assert.equal(h.buttons(h.tree()).some(b => /merge/i.test(String(b.props.children))), false)
})

test('actions: a backend escalation refusal leaves no execution affordance', async () => {
  const h = await harness(); selectAction(h)
  await previewAction(h, actionPlan({ eligible: false, confirmation: '', reason: 'Core operation is not snapshot safe.' }))
  assert.match(h.text(h.tree()), /not snapshot safe/)
  assert.equal(actionButton(h, 'Confirm escalation'), undefined)
  assert.equal(h.actionCalls.length, 0)
})

for (const changed of ['target', 'loop', 'time', 'unmount', 'cancel', 'discovery']) test(`actions: ${changed} invalidates retained confirmation`, async () => {
  const h = await harness(); h.queries.loops = h.good({ loops: [{ id: 'fixture-loop' }, { id: 'other-loop' }] })
  selectAction(h); await previewAction(h); actionButton(h, 'Confirm escalation').props.onClick(); const d = actionDialog(h)
  if (changed === 'target') h.flatten(h.tree()).find(n => n.type === 'Input' && n.props['aria-label'] === 'Action PR number').props.onChange({ target: { value: '938' } })
  if (changed === 'loop') actionButton(h, 'other-loop').props.onClick()
  if (changed === 'time') h.advance(60000, -3600000)
  if (changed === 'unmount') h.unmount()
  if (changed === 'cancel') d.props.onClose()
  if (changed === 'discovery') { h.queries.loops = h.fail(h.queries.loops.data); h.render('page') }
  await d.props.onConfirm(); assert.equal(h.actionCalls.length, 0)
})

for (const status of ['partial', 'unknown', 'stale', 'refused', 'network', 'malformed', 'wrong-loop', 'wrong-head']) test(`actions: ${status} outcome never offers follow-up until fresh preview`, async () => {
  const h = await harness(); selectAction(h); await previewAction(h)
  h.actionResult(() => status === 'network' ? Promise.reject(new Error('/private/token ghp_secret')) : Promise.resolve(status === 'malformed' ? {} : {
    loop: status === 'wrong-loop' ? 'other' : 'fixture-loop', repo: 'fixture/repo', read_at: 1800000000, head: status === 'wrong-head' ? 'b'.repeat(40) : 'a'.repeat(40), ok: ['wrong-loop', 'wrong-head'].includes(status), status: ['wrong-loop', 'wrong-head'].includes(status) ? 'verified' : status, reason: '/private/token ghp_secret' }))
  actionButton(h, 'Confirm escalation').props.onClick(); await actionDialog(h).props.onConfirm()
  assert.doesNotMatch(h.text(h.tree()), /Backend verified escalation|private\/token|ghp_secret/)
  assert.equal(actionButton(h, 'Confirm escalation'), undefined)
  await previewAction(h); assert.ok(actionButton(h, 'Confirm escalation'))
})

test('actions: malformed GET success is rejected before confirmation; raw reason withheld', async () => {
  const h = await harness(); selectAction(h)
  for (const extra of [{ loop: 'other' }, { pr: 938 }, { action: 'review' }, { seat: 'fixer' }, { head: '' }, { eligible: 'yes' }, { confirmation: null }, { read_at: -1 }, { reason: 'x'.repeat(401) }, { repo: null }]) {
    await previewAction(h, actionPlan(extra)); assert.equal(actionButton(h, 'Confirm escalation'), undefined)
    assert.match(h.text(h.tree()), /could not be verified/)
  }
  await previewAction(h, actionPlan({ eligible: false, confirmation: null, reason: '/private/token ghp_secret' }))
  assert.doesNotMatch(h.text(h.tree()), /private\/token|ghp_secret/)
})

// #647: execute the actual registration/callback; transport fixtures deliver already-parsed SDK frames.
const invalidation = () => ({ event: 'invalidate', version: 1 })
test('socket: one registration validates hints, excludes client focus and cleans up late callbacks', async () => {
  const h = await harness()
  assert.equal(h.sockets.length, 1)
  assert.equal(h.sockets[0].path, '/events')
  h.render(PANE)
  h.render('page')
  h.render('status')
  assert.equal(h.sockets.length, 1, 'mounts never create transports')
  for (const message of [null, [], 'invalidate', {}, { event: 'other', version: 1 },
    { event: 'invalidate', version: 2 }, { event: 'invalidate', version: '1' },
    { ...invalidation(), data: { runs: [] } }]) h.sockets[0].callback(message)
  assert.equal(h.calls.length, 0)
  h.sockets[0].callback(invalidation())
  assert.equal(h.calls.length, 1)
  const filter = h.calls[0]
  assert.equal(JSON.stringify(filter.queryKey), '["diaktoros"]')
  for (const kind of ['now', 'overview', 'loops', 'pr', 'stats', 'settings', 'hooks', 'records']) {
    assert.equal(filter.predicate({ queryKey: ['diaktoros', kind, 'loop', 647] }), true)
  }
  for (const kind of ['focus', 'unknown', 'actions', 'health']) {
    assert.equal(filter.predicate({ queryKey: ['diaktoros', kind] }), false)
  }
  assert.equal(h.options.now.refetchInterval, 30000)
  assert.equal(h.options.overview.refetchInterval, 30000)
  h.dispose()
  h.sockets[0].callback(invalidation())
  h.dispose()
  assert.equal(h.calls.length, 1)
  assert.equal(h.sockets[0].closed, 1)
  assert.equal(h.listenerCount(), 0)
  assert.equal(h.timerCount(), 0)
})

test('socket: re-register closes the previous subscription and old disposal cannot clear the new REST', async () => {
  const h = await harness()
  const oldDispose = h.disposers[0]
  h.registerAgain()
  assert.equal(h.sockets.length, 2)
  assert.equal(h.sockets[0].closed, 1)
  assert.equal(h.listenerCount(), 1)
  h.sockets[0].callback(invalidation())
  assert.equal(h.calls.length, 0)
  oldDispose()
  h.sockets[1].callback(invalidation())
  assert.equal(h.calls.length, 1)
  h.render(PANE)
  h.respond(stalled([]))
  await h.options.now.queryFn()
  assert.equal(h.calls.at(-1), '/now?loop=fixture-loop')
  h.dispose()
  assert.equal(h.sockets[1].closed, 1)
})

function healthFixture(loop = 'fixture-loop') {
  return { loop, repo: 'fixture/repo', offline: false, read_at: 1800000000,
    checks: [{ name: 'tokens', status: 'verified', detail: 'Synthetic verified check', fix: '', paused: false }],
    counts: { verified: 1 } }
}

for (const [failure, reason] of [[new Error('403: {"detail":"private caller"}'), 'caller'],
  [new Error('private transport'), 'unreachable'], [new Error('503: {"detail":"private"}'), 'unreachable']]) {
  test('health fallback retains validated local checks and honest disclosure: ' + failure.message, async () => {
    const h = await harness(); h.render('page')
    const offline = { ...healthFixture(), offline: true, checks: [
      { name: 'local profile', status: 'mismatch', detail: 'Local profile requires attention', fix: '', paused: false },
      { name: 'GitHub', status: 'unknown', detail: 'Not probed offline', fix: '', paused: false }
    ], counts: { mismatch: 1, unknown: 1 } }
    h.respond(offline); h.readFails('&probe=1', failure)
    const data = await h.options.health.queryFn()
    assert.equal(data.offline, true); assert.equal(data.probeFailure, reason)
    assert.deepEqual(h.calls, ['/health?loop=fixture-loop&probe=1', '/health?loop=fixture-loop'])
    h.queries.health = h.good(data); h.render('page')
    const text = h.text(h.tree())
    assert.match(text, /Local profile requires attention/)
    assert.match(text, reason === 'caller' ? /GitHub not probed from this caller/ : /GitHub probe unavailable/)
    assert.doesNotMatch(text, /Automatically read when this loop opens|private caller|private transport/)
    h.dispose()
  })
}

test('health fallback rejects malformed, wrong-loop and non-offline replies without inventing checks', async () => {
  const h = await harness(); h.render('page')
  for (const extra of [{ offline: false }, { loop: 'other' }, { checks: [null] }, { counts: {} }]) {
    h.respond({ ...healthFixture(), offline: true, ...extra }); h.readFails('&probe=1', new Error('403: {}'))
    await assert.rejects(h.options.health.queryFn(), { code: 'INVALID_RESPONSE' })
  }
  h.respond({ ...healthFixture(), offline: true })
  await assert.rejects(h.options.health.queryFn(), { code: 'INVALID_RESPONSE' })
  h.dispose()
})

test('health automatic selection uses stable probe identity and explicit bounded refresh policy', async () => {
  const h = await harness()
  h.render('page')
  const opts = h.options.health
  assert.equal(JSON.stringify(opts.queryKey), '["diaktoros","health","fixture-loop","probe"]')
  assert.equal(opts.enabled, true)
  assert.equal(opts.retry, false)
  assert.equal(opts.retryOnMount, false)
  assert.equal(opts.refetchInterval, false)
  assert.equal(opts.refetchOnReconnect, false)
  assert.equal(opts.staleTime, 600000)
  h.respond(healthFixture())
  await opts.queryFn()
  assert.equal(h.calls.at(-1), '/health?loop=fixture-loop&probe=1')
  assert.equal(h.buttons(h.tree()).some(b => b.props.children === 'Probe GitHub'), false)
  h.dispose()
})

test('health rejects wrong-loop, malformed nested checks and dishonest counts', async () => {
  const h = await harness(); h.render('page')
  for (const extra of [{ loop: 'other' }, { checks: [null] }, { counts: { verified: 2 } },
    { checks: [{ name: 'bad', status: 'verified' }] }, { read_at: -1 }]) {
    h.respond({ ...healthFixture(), ...extra })
    await assert.rejects(h.options.health.queryFn(), { code: 'INVALID_RESPONSE' })
  }
  h.dispose()
})

// Dependency-free proof that every read panel's Retry reaches the query's real REST path.
// (The optional real-cache block below repeats this through a real QueryObserver.)
const READ_LABELS = { health: 'Health', stats: 'Seat turns · 7d', settings: 'Settings',
  records: 'Local records', hooks: 'Webhook delivery', overview: 'Across all loops' }
const READ_ENDPOINTS = { health: '/health?loop=fixture-loop&probe=1',
  stats: '/stats?since=7d&loop=fixture-loop', settings: '/settings',
  records: '/records?loop=fixture-loop', hooks: '/hooks?loop=fixture-loop', overview: '/overview' }
for (const kind of Object.keys(READ_ENDPOINTS)) {
  test('read Retry invokes its own REST read (no click-only panel): ' + kind, async () => {
    const h = await harness()
    if (kind === 'overview') h.queries.loops = h.good({ loops: [{ id: 'fixture-loop' }, { id: 'other-loop' }] })
    h.render('page')
    h.respond(kind === 'health' ? healthFixture() : h.queries[kind].data)
    h.queries[kind] = { ...h.good(undefined), isError: true, isLoading: false,
      data: undefined, error: new Error('fixture backend offline'),
      refetch: () => h.options[kind].queryFn() }
    h.render('page')
    const sentence = READ_LABELS[kind] + ': no verified data available.'
    const regions = h.flatten(h.tree()).filter(n => typeof n === 'object' &&
      h.text([n]).includes(sentence) && h.buttons([n]).some(b => b.props.children === 'Retry'))
    assert.ok(regions.length >= 1, kind + ' renders its own Retry status')
    const retry = h.buttons([regions.at(-1)]).filter(b => b.props.children === 'Retry')
    assert.equal(retry.length, 1)
    const before = h.calls.length
    await retry[0].props.onClick()
    assert.equal(h.calls.length, before + 1)
    assert.equal(h.calls.at(-1), READ_ENDPOINTS[kind])
    h.dispose()
  })
}

// Real-cache acceptance mode: tests/run_desktop_query_core.py supplies the locked
// tests/query-core dependency and requires every named case below (no skips).
// Direct developer opt-in also accepts DIAKTOROS_QUERY_CORE as an explicit ESM entry.
if (process.env.DIAKTOROS_QUERY_CORE) {
  // Model a browser for QueryObserver's built-in polling, not a plugin-owned timer/transport.
  globalThis.window = {}
  const { QueryClient, QueryObserver } = await import(process.env.DIAKTOROS_QUERY_CORE)
  delete globalThis.window
  const settle = async client => {
    for (let i = 0; i < 200 && client.isFetching(); i++) await new Promise(resolve => setTimeout(resolve, 5))
    assert.equal(client.isFetching(), 0, 'REST refresh settled')
  }
  test('health real TanStack mounted page: adapter follows selection, fallback and Retry', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
    const a = await harness({ queryClient: client, HealthObserver: QueryObserver })
    const b = await harness({ queryClient: client, HealthObserver: QueryObserver })
    const calls = () => [...a.calls, ...b.calls]
    try {
      a.respond(healthFixture()); b.respond(healthFixture())
      a.render('page'); b.render('page'); await settle(client)
      assert.deepEqual(calls(), ['/health?loop=fixture-loop&probe=1'], 'mounted pages launch one probe without extracted options')
      assert.equal(a.healthResult().data, b.healthResult().data)
      assert.deepEqual(Array.from(a.options.health.queryKey), ['diaktoros', 'health', 'fixture-loop', 'probe'])
      assert.equal(a.options.health.staleTime, 600000)
      assert.equal(a.options.health.refetchInterval, false)
      assert.match(a.text(a.tree()), /Health/)
      a.unmount(); a.render('page'); await settle(client)
      a.render('page'); b.sockets[0].callback(invalidation()); await settle(client)
      assert.equal(calls().length, 1, 'fresh actual remount, rerender and socket do not repeat probe')

      a.queries.loops = a.good({ loops: [{ id: 'fixture-loop' }, { id: 'other-loop' }] })
      a.render('page')
      a.respond({ ...healthFixture('other-loop'), offline: true })
      a.readFails('&probe=1', new Error('private transport'))
      a.buttons(a.tree()).find(button => button.props.children === 'other-loop').props.onClick()
      await settle(client)
      assert.deepEqual(a.calls.slice(-2), ['/health?loop=other-loop&probe=1', '/health?loop=other-loop'])
      assert.equal(a.healthResult().data.loop, 'other-loop')
      assert.equal(a.healthResult().data.probeFailure, 'unreachable')
      assert.doesNotMatch(a.text(a.tree()), /private transport/)
      const count = calls().length
      a.queries.loops = a.good({ loops: [{ id: 'other-loop' }] })
      a.unmount(); a.render('page'); await settle(client)
      assert.equal(calls().length, count, 'fresh fallback remount repeats neither request')

      a.respond(healthFixture('other-loop'))
      await a.buttons(a.tree()).find(button => button.props.children === 'Refresh GitHub').props.onClick()
      await settle(client)
      assert.equal(a.calls.at(-1), '/health?loop=other-loop&probe=1')
      assert.equal(calls().length, count + 1)
      assert.equal(a.healthResult().data.offline, false)
      a.readFails('/health')
      await a.buttons(a.tree()).find(button => button.props.children === 'Refresh GitHub').props.onClick()
      await settle(client)
      assert.equal(a.healthResult().isError, true)
      const sentence = 'Health: showing last good data, not current status'
      const region = a.flatten(a.tree()).filter(node => typeof node === 'object' &&
        a.text([node]).includes(sentence) && a.buttons([node]).some(button => button.props.children === 'Retry')).at(-1)
      assert.ok(region, 'observer notification rerenders the mounted Health error without injected result')
      a.respond(healthFixture('other-loop'))
      const before = calls().length
      await a.buttons([region]).find(button => button.props.children === 'Retry').props.onClick()
      await settle(client)
      assert.equal(calls().length, before + 1)
      assert.equal(a.calls.at(-1), '/health?loop=other-loop&probe=1')
      assert.equal(a.healthResult().isError, false)
      assert.doesNotMatch(a.text(a.tree()), /Health: showing last good data/)
    } finally { a.dispose(); b.dispose(); client.clear() }
  })

  for (const [failure, reason] of [[new Error('403: {}'), 'caller'], [new Error('private transport'), 'unreachable']]) {
    test('health real TanStack fallback dedupes concurrent observers and remount; manual refresh recovers: ' + reason, async () => {
      const client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } })
      const h = await harness({ queryClient: client }); const cleanups = []
      try {
        h.render('page'); const opts = h.options.health
        h.respond({ ...healthFixture(), offline: true }); h.readFails('&probe=1', failure)
        const a = new QueryObserver(client, opts), b = new QueryObserver(client, opts)
        cleanups.push(a.subscribe(() => {}), b.subscribe(() => {})); await settle(client)
        assert.equal(h.calls.length, 2, 'one probe plus one offline read across concurrent observers')
        assert.equal(a.getCurrentResult().data.probeFailure, reason)
        cleanups.splice(0).forEach(fn => fn())
        const remount = new QueryObserver(client, opts)
        cleanups.push(remount.subscribe(() => {})); await settle(client)
        h.sockets[0].callback(invalidation()); await settle(client)
        assert.equal(h.calls.length, 2, 'fresh remount and socket do not repeat either read')
        h.queries.health = remount.getCurrentResult(); h.render('page')
        h.respond(healthFixture())
        await h.buttons(h.tree()).find(b => b.props.children === 'Refresh GitHub').props.onClick()
        await settle(client)
        assert.equal(h.calls.length, 3); assert.equal(h.calls.at(-1), '/health?loop=fixture-loop&probe=1')
        assert.equal(remount.getCurrentResult().data.offline, false)
        assert.equal(remount.getCurrentResult().data.probeFailure, null)
        h.readFails('/health'); await remount.refetch()
        assert.equal(h.calls.length, 5, 'both failed reads are bounded')
        h.queries.health = remount.getCurrentResult(); h.render('page')
        assert.match(h.text(h.tree()), /Health: showing last good data, not current status/)
        assert.doesNotMatch(h.text(h.tree()), /private transport/)
      } finally { cleanups.forEach(fn => fn()); h.dispose(); client.clear() }
    })
  }

  test('health real TanStack: automatic observers dedupe, switch loop, preserve freshness and recover manually', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } })
    const h = await harness({ queryClient: client })
    const cleanups = []
    try {
      h.render('page'); h.respond(healthFixture())
      const opts = h.options.health
      const a = new QueryObserver(client, opts), b = new QueryObserver(client, opts)
      cleanups.push(a.subscribe(() => {}), b.subscribe(() => {}))
      await settle(client)
      assert.equal(h.calls.filter(c => typeof c === 'string').length, 1)
      assert.equal(h.calls[0], '/health?loop=fixture-loop&probe=1')
      assert.equal(a.getCurrentResult().data, b.getCurrentResult().data)
      const fresh = new QueryObserver(client, opts)
      cleanups.push(fresh.subscribe(() => {})); await settle(client)
      assert.equal(h.calls.length, 1, 'fresh mount shares result')
      h.sockets[0].callback(invalidation()); await settle(client)
      assert.equal(h.calls.length, 1, 'local socket never repeats doctor')
      h.queries.loops = h.good({ loops: [{ id: 'fixture-loop' }, { id: 'other-loop' }] })
      h.render('page'); h.buttons(h.tree()).find(b => b.props.children === 'other-loop').props.onClick()
      const other = h.options.health
      assert.equal(other.queryKey[2], 'other-loop')
      h.respond(healthFixture('other-loop'))
      const switched = new QueryObserver(client, other)
      cleanups.push(switched.subscribe(() => {})); await settle(client)
      assert.equal(h.calls.at(-1), '/health?loop=other-loop&probe=1')
      h.readFails('/health'); await switched.refetch()
      assert.equal(switched.getCurrentResult().isError, true)
      assert.equal(switched.getCurrentResult().data.loop, 'other-loop', 'last good data retained')
      // A failed read stays on screen as last-good + error; re-rendering the page does not
      // launch another doctor sweep (the mounted observer already owns the query).
      const count = h.calls.filter(c => c === '/health?loop=other-loop&probe=1').length
      h.render('page')
      assert.equal(h.calls.filter(c => c === '/health?loop=other-loop&probe=1').length, count)
      h.queries.health = switched.getCurrentResult(); h.render('page')
      const refresh = h.buttons(h.tree()).find(b => b.props.children === 'Refresh GitHub')
      h.respond(healthFixture('other-loop')); await refresh.props.onClick(); await settle(client)
      assert.equal(h.calls.at(-1), '/health?loop=other-loop&probe=1')
      assert.equal(switched.getCurrentResult().isError, false)
    } finally { cleanups.forEach(fn => fn()); h.dispose(); client.clear() }
  })

  const LABELS = { health: 'Health', stats: 'Seat turns · 7d', settings: 'Settings', records: 'Local records', hooks: 'Webhook delivery', overview: 'Across all loops' }
  for (const [kind, endpoint] of Object.entries({ health: '/health?loop=fixture-loop&probe=1',
    stats: '/stats?since=7d&loop=fixture-loop', settings: '/settings',
    records: '/records?loop=fixture-loop', hooks: '/hooks?loop=fixture-loop', overview: '/overview' })) {
    test('read Retry real REST endpoint: ' + kind, async () => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
      const h = await harness({ queryClient: client }); let unsubscribe
      try {
        // The cross-loop block (and its own QueryStatus) only renders for more than one loop.
        if (kind === 'overview') h.queries.loops = h.good({ loops: [{ id: 'fixture-loop' }, { id: 'other-loop' }] })
        h.render('page'); const opts = h.options[kind]
        h.readFails(endpoint.split('?')[0])
        const observer = new QueryObserver(client, opts)
        unsubscribe = observer.subscribe(() => {}); await settle(client)
        assert.equal(observer.getCurrentResult().isError, true)
        h.queries[kind] = observer.getCurrentResult(); h.render('page')
        // Find THIS query's actual rendered Retry handler by result.refetch identity,
        // not another panel's counter. Its handler invokes the real QueryObserver.
        const result = h.queries[kind]
        // Bind to this read's own QueryStatus sentence and its one Retry button; a shared
        // call counter or another mounted panel's button must not satisfy the assertion.
        const sentence = LABELS[kind] + ': no verified data available.'
        // Smallest region that holds both the sentence and one Retry control.
        const regions = h.flatten(h.tree()).filter(n => typeof n === 'object' &&
          h.text([n]).includes(sentence) &&
          h.buttons([n]).some(b => b.props.children === 'Retry'))
        assert.ok(regions.length >= 1, kind + ' renders its own status region')
        // Pre-order flatten: the deepest matching node is the status row itself, not an ancestor.
        const region = regions.at(-1)
        const retry = h.buttons([region]).filter(b => b.props.children === 'Retry')
        assert.equal(retry.length, 1, kind + ' has exactly one Retry')
        assert.equal(retry[0].props.children, 'Retry')
        const before = h.calls.length
        await retry[0].props.onClick(); await settle(client)
        assert.equal(h.calls.length, before + (kind === 'health' ? 2 : 1))
        assert.equal(h.calls[before], endpoint, 'Retry starts with the selected read, never another panel')
        assert.equal(h.calls.at(-1), kind === 'health' ? '/health?loop=fixture-loop' : endpoint)
        if (kind === 'health') assert.equal(h.calls.filter(c => c === '/health?loop=fixture-loop').length, 2)
        assert.equal(h.calls.filter(c => c === endpoint).length, 2)
        assert.equal(client.getQueryState(opts.queryKey).fetchStatus, 'idle')
      } finally { unsubscribe?.(); h.dispose(); client.clear() }
    })
  }

  test('socket real TanStack: shared page and pane refresh once through REST, reconnect repairs missed changes', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
    const h = await harness({ queryClient: client })
    const subscriptions = []
    try {
      h.render(PANE)
      const pane = h.options.now
      h.render('page')
      const page = h.options.now
      assert.equal(JSON.stringify(page.queryKey), JSON.stringify(pane.queryKey))
      assert.equal(page.staleTime, pane.staleTime)
      assert.equal(page.refetchInterval, pane.refetchInterval)
      h.respond({ ...stalled([]), counts: { pending: 1 } })
      const pageObserver = new QueryObserver(client, page)
      const paneObserver = new QueryObserver(client, pane)
      subscriptions.push(pageObserver.subscribe(() => {}), paneObserver.subscribe(() => {}))
      await settle(client)
      const path = '/now?loop=fixture-loop'
      assert.equal(h.calls.filter(call => call === path).length, 1)
      assert.equal(pageObserver.getCurrentResult().data, paneObserver.getCurrentResult().data)
      const focus = { loop: 'keep-selection' }
      client.setQueryData(['diaktoros', 'focus'], focus)
      client.setQueryData(['other-plugin', 'now'], { keep: true })
      client.setQueryData(['diaktoros', 'pr', 'other-loop', 647], { cached: true })
      const first = pageObserver.getCurrentResult().data
      // Server's initial connection event invalidates an already-fresh mounted query.
      h.respond({ ...stalled([]), counts: { pending: 2 } })
      h.sockets[0].callback(invalidation())
      await settle(client)
      assert.equal(h.calls.filter(call => call === path).length, 2)
      assert.equal(pageObserver.getCurrentResult().data.counts.pending, 2)
      assert.notEqual(pageObserver.getCurrentResult().data, first)
      assert.equal(pageObserver.getCurrentResult().data, paneObserver.getCurrentResult().data)
      assert.equal(client.getQueryData(['diaktoros', 'focus']), focus)
      assert.equal(client.getQueryState(['diaktoros', 'focus']).isInvalidated, false)
      assert.equal(client.getQueryState(['other-plugin', 'now']).isInvalidated, false)
      assert.equal(client.getQueryState(['diaktoros', 'pr', 'other-loop', 647]).isInvalidated, true)
      // SDK reconnect is synthetic here: no messages during downtime; same callback on re-open.
      h.respond({ ...stalled([]), counts: { pending: 3 } })
      assert.equal(pageObserver.getCurrentResult().data.counts.pending, 2)
      h.sockets[0].callback(invalidation())
      await settle(client)
      assert.equal(h.calls.filter(call => call === path).length, 3)
      assert.equal(paneObserver.getCurrentResult().data.counts.pending, 3)
      const freshMount = new QueryObserver(client, pane)
      subscriptions.push(freshMount.subscribe(() => {}))
      await settle(client)
      assert.equal(h.calls.filter(call => call === path).length, 3, 'fresh extra mount does not duplicate reads')
      h.dispose()
      h.sockets[0].callback(invalidation())
      await settle(client)
      assert.equal(h.calls.filter(call => call === path).length, 3)
    } finally {
      subscriptions.forEach(unsubscribe => unsubscribe())
      h.dispose()
      client.clear()
    }
  })

  for (const mode of ['OAuth no-op', 'disconnected']) {
    test(`socket real TanStack: ${mode} retains built-in polling`, async t => {
      t.mock.timers.enable({ apis: ['setInterval'] })
      const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
      const h = await harness({ queryClient: client })
      let unsubscribe
      try {
        h.render(PANE)
        const opts = h.options.now
        assert.equal(opts.refetchInterval, 30000)
        h.respond({ ...stalled([]), counts: { pending: 1 } })
        const observer = new QueryObserver(client, opts)
        unsubscribe = observer.subscribe(() => {})
        await settle(client)
        if (mode === 'disconnected') {
          h.sockets[0].callback(invalidation())
          await settle(client)
        }
        const before = h.calls.length
        h.respond({ ...stalled([]), counts: { pending: 4 } })
        t.mock.timers.tick(30000)
        await settle(client)
        assert.equal(h.calls.length, before + 1)
        assert.equal(observer.getCurrentResult().data.counts.pending, 4)
        assert.equal(h.sockets.length, 1, 'no plugin reconnect or polling transport')
      } finally {
        unsubscribe?.()
        h.dispose()
        client.clear()
        t.mock.timers.reset()
      }
    })
  }
}
