// A STATEFUL stand-in for @hermes/plugin-sdk (real React hooks, fetching useQuery) so the Desktop page can be clicked through
// in jsdom. tests/desktop/sdk-stub.mjs is the static one used for server rendering.
import { createElement as h, useEffect, useState } from 'react'
const strip = p => { const { variant, size, asChild, tone, loading, name, spinning, ...rest } = p; return rest }
export const Badge = ({ children, ...p }) => h('span', strip(p), children)
export const Button = ({ children, ...p }) => h('button', strip(p), children)
export const Codicon = () => h('i')
export const Dialog = ({ children, open }) => (open ? h('div', { role: 'dialog' }, children) : null)
const el = t => ({ children, ...p }) => h(t, p, children)
export const DialogContent = ({ children }) => h('div', null, children)
export const DialogDescription = el('p'); export const DialogFooter = el('div'); export const DialogHeader = el('div'); export const DialogTitle = el('h3')
export const EmptyState = ({ title }) => h('div', null, title)
export const Input = p => h('input', strip(p))
export const Skeleton = () => h('div')
export const StatusDot = () => h('span')
export const Switch = () => h('input')
export const ROUTES_AREA = 'routes', SIDEBAR_NAV_AREA = 'nav', STATUSBAR_AREAS = { right: 'r' }
export const host = { notify() {}, navigate() {}, composer: { async insertText() { return true }, focus() {} } }
// A small TanStack-like cache: queries register themselves, invalidateQueries({queryKey}) refetches every query under that
// prefix and resolves when they are done, getQueryData reads what the last fetch stored.
const cache = new Map(), live = new Map()
const under = (key, prefix) => prefix.every((p, i) => key[i] === p)
export const queryClient = {
  invalidateQueries({ queryKey } = {}) {
    return Promise.all([...live.values()].filter(q => under(q.key, queryKey || [])).map(q => q.refetch()))
  },
  getQueryData(queryKey) { return cache.get(JSON.stringify(queryKey)) }
}
export function useQuery({ queryKey, queryFn, enabled = true }) {
  const k = JSON.stringify(queryKey)
  const [s, set] = useState({ data: undefined, isLoading: true, isError: false })
  useEffect(() => {
    if (!enabled) return
    let on = true
    const refetch = () => queryFn().then(d => { cache.set(k, d); if (on) set({ data: d, isLoading: false, isError: false }) },
      e => { if (on) set({ isError: true, isLoading: false, error: e }) })
    live.set(k, { key: queryKey, refetch })
    refetch()
    return () => { on = false; live.delete(k) }
  }, [k, enabled])
  return { ...s, isFetching: false }
}
