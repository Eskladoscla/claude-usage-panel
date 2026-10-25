import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Limit, WindowTokens } from '../types'

// usage-panel: las tres ventanas de límite (5 h, semanal, Fable) con su barra y, debajo, los tokens
// gastados en la ventana en curso, sumando todos los proyectos de esta máquina. Los tokens los cuenta
// scripts/ledger.py (subcomando `window`) desde las transcripciones locales, con sus datos en
// ~/.claude/usage-ledger; lo gastado en el chat de claude.ai no aparece. Las muestras de porcentaje
// las registra ./ledger. Solo observa: nunca bloquea ni lanza.

// Registro de muestras: deja constancia de cada lectura de los límites de uso de la cuenta
// (ventana de 5 h, semanal, semanal por modelo…) para que scripts/ledger.py estime cuántos
// tokens admite cada ventana. Solo observa: nunca bloquea, nunca lanza, sin red.
//
// Cada sesión escribe su propio fichero samples/<sessionId>.jsonl: $.fs no tiene
// «append», y un único fichero compartido con leer-y-reescribir perdería líneas
// cuando dos sesiones escriben a la vez.

type RawLimit = { kind: string; percentUsed: number; resetsAt?: string; [k: string]: unknown }

// Etiquetas propias (la API de mods no trae display_name). Un kind desconocido se
// guarda igual, con su nombre como etiqueta.
const LABELS: Record<string, string> = {
  five_hour: 'Sesión (5 h)',
  seven_day: 'Semanal (todos los modelos)',
  seven_day_overage_included: 'Fable limit',
  seven_day_opus: 'Semanal Opus',
  seven_day_sonnet: 'Semanal Sonnet',
  seven_day_cowork: 'Semanal Cowork',
  seven_day_omelette: 'Semanal omelette',
  seven_day_oauth_apps: 'Semanal apps OAuth',
  spend_limit: 'Límite de gasto',
}
const KNOWN = new Set(['kind', 'percentUsed', 'resetsAt'])
const MAX_BYTES = 3 * 1024 * 1024 // $.fs.read rechaza por encima de 4 MiB
const MAX_ERROR_LINES = 200

type St = {
  last: Map<string, string> // kind → firma de la última muestra escrita
  base?: string
  file?: string
  content?: string // copia en memoria del fichero de esta sesión
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
    if (!home) throw new Error('HOME sin definir')
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

// Plan de la cuenta (Pro, Max x5, Max x20) leído de ~/.claude.json una vez por sesión; null si no se sabe.
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
    st.file = `${await dir($, st)}/samples/${st.sessionId || 'sin-sesion'}.jsonl`
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
    // Escrituras en serie: dos medidas seguidas no se pisan el fichero.
    const run = st.queue.then(() => persist($, st, rows))
    st.queue = run.catch(() => {})
    try {
      await run
      for (const [k, sig] of sigs) st.last.set(k, sig) // solo lo escrito cuenta como «ya visto»
    } catch (err) {
      await logError($, st, 'persist', err)
    }
  } catch (err) {
    await logError($, st, source, err)
  }
}

// Estado de la sesión; el módulo del panel llama a estas dos funciones desde sus hooks
// (un mod no puede registrar el mismo evento dos veces).
const st: St = { last: new Map(), cwd: '', sessionId: '', queue: Promise.resolve() }

// Muestra inicial, tras el session.start del motor.
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
  { kind: 'five_hour', label: 'Sesión (5 h)' },
  { kind: 'seven_day', label: 'Semanal (7 días)' },
  { kind: 'seven_day_overage_included', label: 'Fable limit' },
]
const MIN_INTERVAL_MS = 60_000 // la ingesta tarda unos segundos: como mucho una por minuto

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
    if (!home) throw new Error('HOME sin definir')
    const r = await $.process.run(
      ['/usr/bin/env', 'python3', '-I', `${$.plugin.root}/scripts/ledger.py`, 'window', '--ingest', ...resets],
      { cwd: home, timeoutMs: 120_000 },
    )
    if (r.exitCode !== 0) {
      await update($, error, () => `ledger.py salió con ${r.exitCode}`)
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
  if (n >= 1e9) return `${(n / 1e9).toFixed(2).replace('.', ',')} B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace('.', ',')} M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1).replace('.', ',')} k`
  return String(n)
}

function fmtReset(iso: string | undefined, now: number): string {
  if (!iso) return ''
  const ms = Date.parse(iso) - now
  if (!Number.isFinite(ms)) return ''
  if (ms <= 0) return 'Se reinicia ya'
  const h = ms / 3_600_000
  return h >= 24 ? `Se reinicia en ${Math.round(h / 24)} d` : h >= 1 ? `Se reinicia en ${Math.round(h)} h` : `Se reinicia en ${Math.max(1, Math.round(ms / 60_000))} min`
}

// Casillas llenas y vacías de la barra; la parte llena se pinta en naranja.
function bar(pct: number, width: number): { full: string; empty: string } {
  const full = Math.max(0, Math.min(width, Math.round((pct / 100) * width)))
  return { full: '█'.repeat(full), empty: '░'.repeat(width - full) }
}

const ORANGE = '#f97316'

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'usage-panel', description: 'Abre el panel de límites de uso con los tokens gastados' })
    // El pane solo se abre a petición, con /usage-panel.
    try {
      await setLimits($, (await $.session.usage())?.rateLimits)
    } catch {}
    void refresh($, true)
    const result = await next(e)
    await ledgerStart($, e.cwd)
    return result
  })

  on('command.run', { command: 'usage-panel' }, async ($) => {
    await $.ui.open({ id: PANE, title: 'Uso y tokens' })
    void refresh($, true)
    return { text: 'Panel de uso abierto.' }
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
        {ls.length === 0 && <Text dimColor>Sin datos de límites todavía (llegan tras el primer turno).</Text>}
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
                  Último tope: {new Date(t.cap.hit_ts * 1000).toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit' })}
                  {t.cap.plan ? ` · ${t.cap.plan}` : ''}
                </Text>
              )}
              <Text dimColor>
                {t ? `Gastados/Previstos: ${fmtTokens(t.raw)} / ${t.cap ? fmtTokens(t.cap.raw) : '—'}` : 'Gastados/Previstos: calculando…'}
              </Text>
            </Box>
          )
        })}
        {at > 0 && <Text dimColor>Tokens a las {new Date(at).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })}</Text>}
        {err && <Text color="red">{err}</Text>}
      </Box>
    )
  })
}
