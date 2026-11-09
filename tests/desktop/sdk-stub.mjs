// Minimal stand-in for @hermes/plugin-sdk: plain elements, data-driven useQuery. Enough to render the page
// server-side and catch runtime errors / missing exports; it is not a visual test.
import { createElement as h } from 'react'

export const DATA = globalThis.__CAR_DATA__ || {}
const el = tag => ({ children, ...p }) => h(tag, p, children)
const strip = p => {
  const { variant, size, asChild, tone, loading, name, spinning, ...rest } = p
  return rest
}

export const Badge = ({ children, variant, ...p }) => h('span', { 'data-badge': variant || 'default', ...strip(p) }, children)
export const Button = ({ children, variant, ...p }) => h('button', { 'data-variant': variant, ...strip(p) }, children)
export const Codicon = ({ name }) => h('i', { className: `codicon codicon-${name}` })
export const Dialog = ({ children, open }) => (open ? h('div', { role: 'dialog' }, children) : null)
export const DialogContent = ({ children }) => h('div', null, children)
export const DialogDescription = el('p')
export const DialogFooter = el('div')
export const DialogHeader = el('div')
export const DialogTitle = el('h3')
export const EmptyState = ({ title, description }) => h('div', { 'data-empty': true }, title, ' ', description)
export const Input = p => h('input', strip(p))
export const Skeleton = p => h('div', { 'data-skeleton': true })
export const StatusDot = ({ tone }) => h('span', { 'data-dot': tone })
export const Switch = ({ checked }) => h('input', { type: 'checkbox', defaultChecked: !!checked, readOnly: true })
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebarNav'
export const STATUSBAR_AREAS = { left: 'statusbar.left', right: 'statusbar.right' }
export const host = { notify() {}, navigate() {}, onEvent() { return () => {} }, composer: { async insertText() { return true }, focus() {} } }
export const queryClient = { invalidateQueries() {} }
export function useQuery({ queryKey }) {
  const key = String(queryKey[1])
  const data = DATA[key]
  return { data, isLoading: false, isError: false, isFetching: false }
}
