import { expect, mock, test } from 'claude-code/testing'

const HOME = '/home/tester'
const DIR = `${HOME}/.local/share/claude-switcher`
const ID = 'session-1'
const START = 1_000_000

type On = Parameters<Parameters<typeof test>[1]>[1]
type Dollar = Parameters<Parameters<typeof test>[1]>[0]

// The engine beneath the mod: files in memory, and a Remote Control bridge
// that the "Enable Remote Control for all sessions" row and /remote-control
// drive the way Claude Code's own do. `calls` records what reached it.
function world(on: On, { connected = true, offWorks = true } = {}) {
  const files: Record<string, string> = {}
  const calls: string[] = []
  const bridge = { connected }
  const clock = mock.clock(on, { now: START })
  mock.env(on, { HOME })

  on('session.id', () => ({ value: ID }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('fs.read', (_, e) => {
    if (!(e.path in files)) throw new Error(`ENOENT: ${e.path}`)
    return { value: files[e.path] }
  })
  on('fs.write', (_, e) => {
    files[e.path] = e.text
    return { value: undefined }
  })
  on('command.list', () => ({
    value: [{
      name: 'remote-control',
      description: bridge.connected ? 'Disconnect Remote Control' : 'Control this session from your phone or claude.ai/code',
      source: 'builtin',
    }],
  }))
  on('config.set', (_, e) => {
    calls.push(`set:${e.value}`)
    if (e.value === 'false' && offWorks) bridge.connected = false
    if (e.value === 'true') bridge.connected = true
    return { value: e.value }
  })
  on('command.run', (_, e) => {
    // Run while connected, the real command opens a menu that waits for a key.
    calls.push(bridge.connected ? `run:${e.command} (menu!)` : `run:${e.command}`)
    bridge.connected = true
    return { text: '' }
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', () => ({ text: '' }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))

  const signal = (sessions = [ID], { epoch = clock.now(), restore = 'default', ttl = 600_000 } = {}) => {
    files[`${DIR}/rc-signal.json`] = JSON.stringify({ epoch, sessions, expiresAt: epoch + ttl, restore })
  }
  const beat = () => JSON.parse(files[`${DIR}/rc-mod/${ID}.json`])
  return { files, calls, bridge, clock, signal, beat }
}

const start = ($: Dollar) => $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

test('a connected session is turned off, its setting put back, then turned on', async ($, on) => {
  const w = world(on)
  await start($)
  w.signal()

  await w.clock.advance(10_000)
  expect(w.calls).toEqual([])  // giving the session time to take in the new login

  await w.clock.advance(30_000)
  expect(w.calls).toEqual(['set:false', 'set:default', 'run:remote-control'])
  expect(w.bridge.connected).toBe(true)
  expect(w.beat().result).toBe('cycled')
  expect(w.beat().v).toBe(2)

  await w.clock.advance(60_000)
  expect(w.calls).toHaveLength(3)
})

test('a session whose bridge already dropped is only turned on', async ($, on) => {
  const w = world(on, { connected: false })
  await start($)
  w.signal()

  await w.clock.advance(40_000)
  expect(w.calls).toEqual(['run:remote-control'])
})

test('where the setting is true, putting it back is what turns it on', async ($, on) => {
  const w = world(on)
  await start($)
  w.signal([ID], { restore: 'true' })

  await w.clock.advance(40_000)
  expect(w.calls).toEqual(['set:false', 'set:true'])
  expect(w.bridge.connected).toBe(true)
})

test('nothing starts while a main-loop turn runs', async ($, on) => {
  const w = world(on)
  await start($)
  w.signal()

  await $.turn.start({ text: 'work', turnId: 'main-1' })
  await $.turn.start({ text: 'sub', turnId: 'sub-1', agentId: 'agent-1' })
  await $.turn.complete({ answer: '', durationMs: 1, isAborted: false, turnId: 'sub-1', agentId: 'agent-1', reason: 'answer' })
  await w.clock.advance(60_000)
  expect(w.calls).toEqual([])

  await $.turn.complete({ answer: '', durationMs: 1, isAborted: false, turnId: 'main-1', reason: 'answer' })
  await w.clock.advance(30_000)
  expect(w.calls).toEqual(['set:false', 'set:default', 'run:remote-control'])
})

test('a bridge that will not go off is never toggled from the command', async ($, on) => {
  const w = world(on, { offWorks: false })
  await start($)
  w.signal()

  await w.clock.advance(120_000)
  expect(w.calls).toEqual(['set:false', 'set:default'])
  expect(w.beat().result).toBe('off-timeout')
})

test('sessions not listed, and expired signals, are left alone', async ($, on) => {
  const w = world(on)
  await start($)

  w.signal(['another-session'])
  await w.clock.advance(30_000)
  w.signal([ID], { epoch: w.clock.now() - 700_000 })
  await w.clock.advance(30_000)

  expect(w.calls).toEqual([])
})

test('a reload mid-cycle resumes it rather than starting over', async ($, on) => {
  const w = world(on, { connected: false })
  w.files[`${DIR}/rc-mod/${ID}.json`] = JSON.stringify({
    at: START, handled: START - 20_000, phase: 'on', phaseAt: START - 10_000, restore: 'default',
  })
  w.signal([ID], { epoch: START - 20_000 })
  await start($)

  await w.clock.advance(10_000)
  expect(w.calls).toEqual(['run:remote-control'])
})
