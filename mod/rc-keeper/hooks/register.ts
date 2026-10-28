import type { EngineInterface, Register } from 'claude-code'

// claude-switcher moves this machine between Claude accounts. A Remote Control
// bridge may survive the switch still bound to the old account, or drop and
// stay off, so after each switch the daemon lists the sessions that had it on
// (rc-signal.json) and each one listed turns Remote Control off and on again.
//
// Off: `/remote-control` on a connected session opens a menu that waits for a
// key, so off goes through the "Enable Remote Control for all sessions" row,
// whose change applies to this session alone, at once. The row is put back to
// the person's own value straight away (the daemon read it before the switch).
// On: `/remote-control` on a session that is off, through the plugin API, run
// while the main loop is idle (a plugin's run would otherwise wait for the
// turn's end, by which time the state it was judged on may have changed).
// Where the person's value is `true`, putting it back is itself the "on".

type Signal = { epoch: number; sessions: string[]; expiresAt: number; restore: string }
type Phase = 'off' | 'on'
type Beat = {
  at: number
  v: number
  handled: number
  phase?: Phase
  phaseAt?: number
  restore?: string
  result?: string
}

// Read by the daemon: a session still running an older rc-keeper is told to reload.
const VERSION = 2
const CHECK_MS = 5_000
const CHECKS_PER_BEAT = 6
// The session picks up the new login on its next request and the new
// organization from its watch of ~/.claude.json, within seconds of the switch.
const SETTLE_MS = 15_000
const OFF_TIMEOUT_MS = 30_000
const ROW = 'remoteControl'

// `/remote-control`'s description names the way it would go.
async function isConnected($: EngineInterface) {
  const command = (await $.command.list()).find(c => c.name === 'remote-control')

  return command !== undefined && /^Disconnect/.test(command.description)
}

async function setRow($: EngineInterface, value: string) {
  const set = await $.config.set({ key: ROW, value })

  return set.deny === undefined
}

async function readJson<T>($: EngineInterface, path: string): Promise<T | undefined> {
  try {
    return JSON.parse(await $.fs.read(path)) as T
  } catch {
    return undefined
  }
}

export const register: Register = on => {
  let runningTurn: string | undefined

  on('turn.start', ($, e, next) => {
    if (e.agentId === undefined) runningTurn = e.turnId
    return next(e)
  })

  on('turn.complete', ($, e, next) => {
    if (e.agentId === undefined && e.turnId === runningTurn) runningTurn = undefined
    return next(e)
  })

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (!e.isInteractive) return started

    const dir = `${await $.env.get('HOME')}/.local/share/claude-switcher`
    const id = await $.session.id()
    const beatPath = `${dir}/rc-mod/${id}.json`
    // Kept in the heartbeat file, so a reload resumes a cycle instead of repeating it.
    let beat: Beat = { at: 0, v: VERSION, handled: 0, ...(await readJson<Beat>($, beatPath)) }
    let checks = 0

    const save = async (now: number, change: Partial<Beat> = {}) => {
      beat = { ...beat, ...change, at: now, v: VERSION }
      await $.fs.write(beatPath, JSON.stringify(beat))
    }
    const finish = (now: number, result: string) =>
      save(now, { phase: undefined, phaseAt: undefined, restore: undefined, result })

    const start = async (now: number) => {
      if (runningTurn !== undefined) return
      const signal = await readJson<Signal>($, `${dir}/rc-signal.json`)
      if (!signal || signal.epoch <= beat.handled || now > signal.expiresAt) return
      if (!signal.sessions.includes(id) || now - signal.epoch < SETTLE_MS) return

      const restore = signal.restore ?? 'default'
      await save(now, { handled: signal.epoch, restore, result: undefined })
      $.ui.toast('rc-keeper: reconnecting Remote Control under the new account')
      if (!(await isConnected($))) {
        await save(now, { phase: 'on', phaseAt: now })
        return
      }
      if (!(await setRow($, 'false'))) {
        await finish(now, 'off-denied')
        return
      }
      if (restore !== 'true') await setRow($, restore)
      await save(now, { phase: 'off', phaseAt: now })
    }

    const advance = async (now: number) => {
      const since = now - (beat.phaseAt ?? now)
      if (beat.phase === 'off') {
        if (await isConnected($)) {
          if (since < OFF_TIMEOUT_MS) return
          if (beat.restore === 'true') await setRow($, 'true')
          await finish(now, 'off-timeout')
          return
        }
        if (beat.restore === 'true') {
          await setRow($, 'true')
          await finish(now, 'cycled')
          return
        }
        await save(now, { phase: 'on', phaseAt: now })
        return
      }
      // Phase 'on': one check after the bridge read as off, so its teardown
      // has finished and /remote-control takes its enable path.
      if (await isConnected($)) {
        await finish(now, 'cycled')
        return
      }
      if (since < CHECK_MS || runningTurn !== undefined) return
      await finish(now, 'cycled')
      await $.command.run({ command: 'remote-control' })
    }

    const check = async () => {
      const now = await $.clock.now()
      if (checks++ % CHECKS_PER_BEAT === 0) await save(now)
      if (beat.phase === undefined) await start(now)
      else await advance(now)
    }

    $.clock.every(CHECK_MS, () => {
      check().catch(err => $.ui.log(`rc-keeper: ${err}`, { to: 'debug' }))
    })

    return started
  })
}
