import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Limit, WindowTokens } from '../types'

// usage-panel: the three limit windows (5h, weekly, Fable) with their bar and, below it, the tokens
// used in the current window, summed across every project on this machine. The tokens are counted by
// scripts/ledger.py (`window` subcommand) from the local transcripts, with its data in
// ~/.claude/usage-ledger; usage in the claude.ai chat does not show up. The percentage samples are
// recorded by the ledger below. Observe-only: it never blocks or throws.

// Sample ledger: records every reading of the account's usage limits (5-hour window, weekly,
// weekly per model…) so that scripts/ledger.py can estimate how many tokens each window allows.
// Observe-only: never blocks, never throws, no network.
//
// Each session writes its own file samples/<sessionId>.jsonl: $.fs has no "append",
// and a single shared file with read-and-rewrite would lose lines when two sessions
// write at the same time.

type RawLimit = { kind: string; percentUsed: number; resetsAt?: string; [k: string]: unknown }

// Our own labels (the mod API provides no display_name). An unknown kind is still
// recorded, with its name as the label.
const LABELS: Record<string, string> = {
  five_hour: 'Session (5h)',
  seven_day: 'Weekly (all models)',
  seven_day_overage_included: 'Fable limit',
  seven_day_opus: 'Weekly Opus',
  seven_day_sonnet: 'Weekly Sonnet',
  seven_day_cowork: 'Weekly Cowork',
  seven_day_omelette: 'Weekly omelette',
  seven_day_oauth_apps: 'Weekly OAuth apps',
  spend_limit: 'Spend limit',
}
const KNOWN = new Set(['kind', 'percentUsed', 'resetsAt'])
const MAX_BYTES = 3 * 1024 * 1024 // $.fs.read rejects anything above 4 MiB
const MAX_ERROR_LINES = 200

type St = {
  last: Map<string, string> // kind → signature of the last sample written
  base?: string
  file?: string
  content?: string // in-memory copy of this session's file
  cwd: string
  sessionId: string
  queue: Promise<void>
  plan?: string | null
}

async function dir($, st: St): Promise<string> {
  if (!st.base) {
    let home = ''
    try {
      home = (await $.env.get('HOME')) ?? ''
    } catch {}
    if (!home) throw new Error('HOME is not set')
    st.base = `${home}/.claude/usage-ledger`
  }
  return st.base
}

async function logError($, st: St, where: string, err: unknown) {
  try {
    const path = `${await dir($, st)}/errors.log`
    let old = ''
    try {
      old = await $.fs.read(path)
    } catch {}
    const msg = err instanceof Error ? `${err.name}: ${err.message}` : String(err)
    const lines = (old + `${new Date().toISOString()} [${where}] ${msg}\n`).split('\n').filter(Boolean)
    await $.fs.write(path, lines.slice(-MAX_ERROR_LINES).join('\n') + '\n')
  } catch {}
}

// Account plan (Pro, Max x5, Max x20) read from ~/.claude.json once per session; null if unknown.
async function plan($, st: St): Promise<string | null> {
  if (st.plan === undefined) {
    st.plan = null
    try {
      const home = await $.env.get('HOME')
      if (!home) return st.plan
      const acc = JSON.parse(await $.fs.read(`${home}/.claude.json`))?.oauthAccount ?? {}
      const tier = String(acc.organizationRateLimitTier || acc.userRateLimitTier || '')
      st.plan = tier.includes('max_20x') ? 'Max x20' : tier.includes('max_5x') ? 'Max x5' : tier.includes('pro') ? 'Pro' : tier || null
    } catch {}
  }
  return st.plan
}

async function persist($, st: St, rows: string[]) {
  if (!st.file) {
    st.file = `${await dir($, st)}/samples/${st.sessionId || 'no-session'}.jsonl`
    try {
      st.content = await $.fs.read(st.file)
    } catch {
      st.content = ''
    }
  }
  if ((st.content ?? '').length > MAX_BYTES) {
    st.file = st.file.replace(/(\.\d+)?\.jsonl$/, `.${Date.now()}.jsonl`)
    st.content = ''
  }
  const next = (st.content ?? '') + rows.join('')
  await $.fs.write(st.file, next)
  st.content = next
}

async function record($, st: St, limits: readonly RawLimit[] | undefined, source: string) {
  try {
    if (!limits?.length) return
    const ts = new Date(await $.clock.now()).toISOString()
    if (!st.sessionId) {
      try {
        st.sessionId = await $.session.id()
      } catch {}
    }
    if (!st.cwd) {
      try {
        st.cwd = await $.session.cwd()
      } catch {}
    }
    const rows: string[] = []
    const sigs: [string, string][] = []
    for (const l of limits) {
      if (!l || typeof l.kind !== 'string') continue
      const sig = `${l.percentUsed}|${l.resetsAt ?? ''}`
      if (st.last.get(l.kind) === sig) continue
      sigs.push([l.kind, sig])
      const extra: Record<string, unknown> = {}
      for (const [k, v] of Object.entries(l)) if (!KNOWN.has(k)) extra[k] = v
      const label = typeof extra.label === 'string' ? extra.label : (LABELS[l.kind] ?? l.kind)
      rows.push(
        JSON.stringify({
          ts,
          sessionId: st.sessionId,
          cwd: st.cwd,
          project: st.cwd.split('/').filter(Boolean).pop() ?? '',
          source,
          kind: l.kind,
          label,
          percentUsed: l.percentUsed,
          resetsAt: l.resetsAt ?? null,
          plan: await plan($, st),
          ...(Object.keys(extra).length ? { extra } : {}),
        }) + '\n',
      )
    }
    if (!rows.length) return
    // Serialized writes: two back-to-back measurements do not clobber the file.
    const run = st.queue.then(() => persist($, st, rows))
    st.queue = run.catch(() => {})
    try {
      await run
      for (const [k, sig] of sigs) st.last.set(k, sig) // only what was written counts as "already seen"
    } catch (err) {
      await logError($, st, 'persist', err)
    }
  } catch (err) {
    await logError($, st, source, err)
  }
}

// Session state; the panel module calls these two functions from its hooks
// (a mod cannot register the same event twice).
const st: St = { last: new Map(), cwd: '', sessionId: '', queue: Promise.resolve() }

// Initial sample, after the engine's session.start.
export async function ledgerStart($: EngineInterface, cwd: string | undefined) {
  try {
    st.cwd = cwd ?? ''
    const usage = await $.session.usage()
    await record($, st, usage?.rateLimits, 'start')
  } catch (err) {
    await logError($, st, 'start', err)
  }
}

export async function ledgerMeasure($: EngineInterface, limits: readonly RawLimit[] | undefined) {
  await record($, st, limits, 'measure')
}

// Panel

const PANE = 'usage-panel'
const limits = atom({ plugin: 'usage-panel', key: 'limits' } as const, [] as Limit[])
const tokens = atom({ plugin: 'usage-panel', key: 'tokens' } as const, {} as Record<string, WindowTokens>)
const updatedAt = atom({ plugin: 'usage-panel', key: 'updatedAt' } as const, 0)
const error = atom({ plugin: 'usage-panel', key: 'error' } as const, '')

const WINDOWS: readonly { kind: string; label: string }[] = [
  { kind: 'five_hour', label: 'Session (5h)' },
  { kind: 'seven_day', label: 'Weekly (7 days)' },
  { kind: 'seven_day_overage_included', label: 'Fable limit' },
]
const MIN_INTERVAL_MS = 60_000 // ingestion takes a few seconds: at most once per minute

let running = false
let lastRun = 0

async function refresh($: EngineInterface, force = false) {
  const now = Date.now()
  if (running || (!force && now - lastRun < MIN_INTERVAL_MS)) return
  running = true
  lastRun = now
  try {
    const ls = await read($, limits)
    const resets = ls.filter((l) => l.resetsAt).map((l) => `${l.kind}=${l.resetsAt}`)
    if (resets.length === 0) return
    const home = await $.env.get('HOME')
    if (!home) throw new Error('HOME is not set')
    const r = await $.process.run(
      ['/usr/bin/env', 'python3', '-I', `${$.plugin.root}/scripts/ledger.py`, 'window', '--ingest', ...resets],
      { cwd: home, timeoutMs: 120_000 },
    )
    if (r.exitCode !== 0) {
      await update($, error, () => `ledger.py exited with ${r.exitCode}`)
      return
    }
    const parsed = JSON.parse(r.stdout) as Record<string, WindowTokens>
    await update($, tokens, () => parsed)
    await update($, updatedAt, () => Date.now())
    await update($, error, () => '')
  } catch (err) {
    try {
      await update($, error, () => (err instanceof Error ? err.message : String(err)).slice(0, 120))
    } catch {}
  } finally {
    running = false
  }
}

async function setLimits($: EngineInterface, rl: readonly Limit[] | undefined) {
  if (!rl || rl.length === 0) return
  await update($, limits, () => rl.map((l) => ({ kind: l.kind, percentUsed: l.percentUsed, resetsAt: l.resetsAt })))
}

function fmtTokens(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} k`
  return String(n)
}

function fmtReset(iso: string | undefined, now: number): string {
  if (!iso) return ''
  const ms = Date.parse(iso) - now
  if (!Number.isFinite(ms)) return ''
  if (ms <= 0) return 'Resets now'
  const h = ms / 3_600_000
  return h >= 24 ? `Resets in ${Math.round(h / 24)} d` : h >= 1 ? `Resets in ${Math.round(h)} h` : `Resets in ${Math.max(1, Math.round(ms / 60_000))} min`
}

// Full and empty cells of the bar; the full part is drawn in orange.
function bar(pct: number, width: number): { full: string; empty: string } {
  const full = Math.max(0, Math.min(width, Math.round((pct / 100) * width)))
  return { full: '█'.repeat(full), empty: '░'.repeat(width - full) }
}

const ORANGE = '#f97316'

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'usage-panel', description: 'Opens the usage limits panel with the tokens used' })
    // The pane only opens on request, with /usage-panel.
    try {
      await setLimits($, (await $.session.usage())?.rateLimits)
    } catch {}
    void refresh($, true)
    const result = await next(e)
    await ledgerStart($, e.cwd)
    return result
  })

  on('command.run', { command: 'usage-panel' }, async ($) => {
    await $.ui.open({ id: PANE, title: 'Usage and tokens' })
    void refresh($, true)
    return { text: 'Usage panel opened.' }
  })

  on('session.measure', async ($, e, next) => {
    await ledgerMeasure($, e.rateLimits)
    try {
      await setLimits($, e.rateLimits)
      void refresh($)
    } catch {}
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const ls = await read($, limits)
    const tk = await read($, tokens)
    const at = await read($, updatedAt)
    const err = await read($, error)
    const now = Date.now()
    const width = Math.max(10, Math.min(40, (e.viewport?.columns ?? 40) - 4))

    return (
      <Box flexDirection="column">
        {ls.length === 0 && <Text dimColor>No limit data yet (it arrives after the first turn).</Text>}
        {WINDOWS.map((w) => {
          const l = ls.find((x) => x.kind === w.kind)
          const t = tk[w.kind]
          return (
            <Box flexDirection="column" marginBottom={1}>
              <Text>
                {w.label}  {l ? `${l.percentUsed}%` : '—'}
              </Text>
              <Text>
                <Text color={ORANGE}>{bar(l?.percentUsed ?? 0, width).full}</Text>
                {bar(l?.percentUsed ?? 0, width).empty}
              </Text>
              {l?.resetsAt && <Text dimColor>{fmtReset(l.resetsAt, now)}</Text>}
              {t?.cap && (
                <Text dimColor>
                  Last cap: {new Date(t.cap.hit_ts * 1000).toLocaleDateString('en-US', { month: '2-digit', day: '2-digit' })}
                  {t.cap.plan ? ` · ${t.cap.plan}` : ''}
                </Text>
              )}
              <Text dimColor>
                {t ? `Used/Expected: ${fmtTokens(t.raw)} / ${t.cap ? fmtTokens(t.cap.raw) : '—'}` : 'Used/Expected: calculating…'}
              </Text>
            </Box>
          )
        })}
        {at > 0 && <Text dimColor>Tokens as of {new Date(at).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}</Text>}
        {err && <Text color="red">{err}</Text>}
      </Box>
    )
  })
}
