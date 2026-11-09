// Runs dashboard/dist/index.js (the web-dashboard IIFE) in jsdom against recorded backend JSON and prints the HTML.
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'

const NM = process.env.HERMES_NODE_MODULES
const req = createRequire(NM + '/')
const React = req('react')
const { createRoot } = req('react-dom/client')
const { JSDOM } = req('jsdom')

const data = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' })
const calls = []
let summaryCalls = 0, seqCalls = 0
const el = tag => props => React.createElement(tag, Object.fromEntries(Object.entries(props).filter(([k]) => !['variant', 'size'].includes(k))))
const SDK = {
  React, hooks: { useState: React.useState, useEffect: React.useEffect, useCallback: React.useCallback, useMemo: React.useMemo, useRef: React.useRef },
  components: { Badge: el('span'), Button: el('button'), Input: el('input'), Card: el('div') },
  fetchJSON: async (url, init) => {
    calls.push([url, init && init.method || 'GET', init && init.body || null])
    const path = url.replace('/api/plugins/pit-crew', '').split('?')[0]
    // data.__fail_summary_after = N: the backend answers N summaries and then stops (a crashed/restarted Hermes)
    if (path === '/summary' && data.__fail_summary_after !== undefined && ++summaryCalls > data.__fail_summary_after) throw new Error('502 Bad Gateway')
    // data.__summary_sequence: successive /summary answers (the last one repeats), to play out the car changing over time
    if (path === '/summary' && data.__summary_sequence) return data.__summary_sequence[Math.min(seqCalls++, data.__summary_sequence.length - 1)]
    if (!(path in data)) throw new Error('404: ' + path)
    return data[path]
  },
  utils: {},
}
let page = null
globalThis.window = dom.window
globalThis.document = dom.window.document
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true })
dom.window.__HERMES_PLUGIN_SDK__ = SDK
dom.window.__HERMES_PLUGINS__ = { register: (name, comp) => { page = [name, comp] } }
;(0, eval)(readFileSync(new URL('../../dashboard/dist/index.js', import.meta.url), 'utf8'))
const root = createRoot(dom.window.document.getElementById('root'))
root.render(React.createElement(page[1]))
await new Promise(r => setTimeout(r, 300))
for (const spec of JSON.parse(process.argv[3] || '[]')) {
  const times = spec.endsWith('×2') ? 2 : 1          // "Send command×2": two clicks back to back (a double click)
  const text = spec.replace(/×2$/, '')
  const btn = [...dom.window.document.querySelectorAll('button')].find(b => b.textContent === text)
  if (!btn) throw new Error('no button: ' + text)
  for (let i = 0; i < times; i++) btn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
  await new Promise(r => setTimeout(r, 200))
}
console.log('HARNESS_JSON:' + JSON.stringify({ name: page[0], html: dom.window.document.getElementById('root').innerHTML, calls }))
process.exit(0)
