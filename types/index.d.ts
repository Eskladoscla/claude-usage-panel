export type Limit = { kind: string; percentUsed: number; resetsAt?: string }
export type Cap = { raw: number; weighted: number; hit_ts: number; plan: string | null }
export type WindowTokens = { raw: number; weighted: number; cap: Cap | null }

declare module 'claude-code' {
  interface PluginState {
    'usage-panel': { limits: Limit[]; tokens: Record<string, WindowTokens>; updatedAt: number; error: string }
  }
}
