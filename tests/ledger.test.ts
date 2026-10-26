import { expect, mock, test } from 'claude-code/testing'

const START = { surface: 'terminal', isInteractive: true, cwd: '/work/my-project' } as const
const R5 = '2026-10-25T15:00:00Z'
const R7 = '2026-10-30T08:00:00Z'

function stubs(on, opts: { initial?: unknown[]; failWrite?: boolean } = {}) {
  mock.clock(on, { now: Date.parse('2026-10-25T12:00:00Z') })
  const files = new Map<string, string>()
  on('env.get', ($, e) => ({ value: e.name === 'HOME' || e.key === 'HOME' ? '/home/u' : undefined }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('command.register', () => ({ value: undefined }))
  on('session.cwd', () => ({ value: '/work/my-project' }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 1_000_000 }, rateLimits: opts.initial ?? [] } }))
  on('fs.read', ($, e) => {
    if (!files.has(e.path)) throw new Error('ENOENT')
    return { value: files.get(e.path) }
  })
  on('fs.write', ($, e) => {
    if (opts.failWrite && e.path.endsWith('.jsonl')) throw new Error('disk full')
    files.set(e.path, e.text)
    return { value: undefined }
  })
  on('session.start', () => ({ cwd: '/work/my-project' }))
  on('session.measure', ($, e) => ({ changed: e.changed }))
  return files
}

const rows = (files: Map<string, string>) =>
  (files.get('/home/u/.claude/usage-ledger/samples/sess-1.jsonl') ?? '').split('\n').filter(Boolean).map((l) => JSON.parse(l))

const measure = (rateLimits: unknown[]) => ({ context: { window: 1_000_000 }, rateLimits, changed: ['rateLimits'] }) as never

test('initial read and measurements: one line per kind only when it changes, with label and extra fields', async ($, on) => {
  const files = stubs(on, { initial: [{ kind: 'five_hour', percentUsed: 12, resetsAt: R5 }] })
  await $.session.start(START)
  expect(rows(files)).toHaveLength(1)
  expect(rows(files)[0]).toMatchObject({ sessionId: 'sess-1', source: 'start', kind: 'five_hour', label: 'Session (5h)', percentUsed: 12, resetsAt: R5, project: 'my-project' })

  // same figure: nothing new; Fable changes and an unknown kind arrives with an extra field
  const r = await $.session.measure(
    measure([
      { kind: 'five_hour', percentUsed: 12, resetsAt: R5 },
      { kind: 'seven_day_overage_included', percentUsed: 40.5, resetsAt: R7 },
      { kind: 'seven_day_new', percentUsed: 3, resetsAt: R7, display_name: 'New' },
    ]),
  )
  expect(r).toEqual({ changed: ['rateLimits'] })
  const all = rows(files)
  expect(all.map((x) => x.kind)).toEqual(['five_hour', 'seven_day_overage_included', 'seven_day_new'])
  expect(all[1].label).toBe('Fable limit')
  expect(all[2]).toMatchObject({ label: 'seven_day_new', extra: { display_name: 'New' } })

  // up one point: one more line; a new reset with the same figure also counts
  await $.session.measure(measure([{ kind: 'five_hour', percentUsed: 13, resetsAt: R5 }]))
  await $.session.measure(measure([{ kind: 'five_hour', percentUsed: 13, resetsAt: '2026-10-25T20:00:00Z' }]))
  expect(rows(files)).toHaveLength(5)
})

test('a write failure never breaks the session: it goes to errors.log and the measurement goes on', async ($, on) => {
  const files = stubs(on, { failWrite: true })
  await $.session.start(START)
  const r = await $.session.measure(measure([{ kind: 'seven_day', percentUsed: 50, resetsAt: R7 }]))
  expect(r).toEqual({ changed: ['rateLimits'] })
  expect(files.get('/home/u/.claude/usage-ledger/errors.log')).toMatch(/\[persist\]/)
  // the sample does not count as written: the next identical measurement is retried
  expect(files.has("/home/u/.claude/usage-ledger/samples/sess-1.jsonl")).toBe(false)
})
