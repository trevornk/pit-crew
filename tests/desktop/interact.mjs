// Clicks through the Desktop page in jsdom against recorded backend JSON: node interact.mjs <data.json> '<["Lock","Close"]>'
// Prints HARNESS_JSON with the dialog text after each click and every REST call the page made.
import { readFileSync } from 'node:fs'
import { createRequire, register } from 'node:module'

const NM = process.env.HERMES_NODE_MODULES
register('./live-loader.mjs', import.meta.url)
const req = createRequire(NM + '/')
const { JSDOM } = req('jsdom')
const { createRoot } = req('react-dom/client')
const { act } = req('react')
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' })
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const plugin = (await import(new URL('../../desktop/plugin.js', import.meta.url).href)).default
const { queryClient } = await import(new URL('./live-stub.mjs', import.meta.url).href)
const data = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const calls = []
let summaryCalls = 0
let page
plugin.register({
  registerMany: cs => { page = cs.find(c => c.id === 'page'); return () => {} }, register() { return () => {} }, onEvent: () => () => {},
  rest: async (path, opts) => {
    calls.push([path, (opts && opts.method) || 'GET', JSON.stringify((opts && opts.body) || null)])
    const p = path.split('?')[0]
    if (p === '/command') return data['/command'] || { ok: true, status: 'dry_run', message: 'PRACTICE RUN done' }
    // data.__summary_sequence: successive /summary answers (the last one repeats), to play out the car changing over time
    if (p === '/summary' && data.__summary_sequence) return data.__summary_sequence[Math.min(summaryCalls++, data.__summary_sequence.length - 1)]
    return p in data ? data[p] : { ok: true }
  }
})
const wait = ms => new Promise(r => setTimeout(r, ms))
const root = createRoot(document.getElementById('root'))
await act(async () => { root.render(page.render()); await wait(100) })
const dialog = () => (document.querySelector('[role=dialog]') ? document.querySelector('[role=dialog]').textContent : '')
const shots = []
for (const text of JSON.parse(process.argv[3] || '[]')) {
  if (text === '!poll') {   // the page's own refetch (what its 12 s timer does), so a test need not wait for it
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['pit-crew'] }); await wait(50) })
    shots.push({ click: text, dialog: dialog().slice(0, 400), buttons: [...document.querySelectorAll('[role=dialog] button')].map(x => x.textContent.trim()) })
    continue
  }
  const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === text)
  if (!b) { shots.push({ click: text, missing: true }); continue }
  await act(async () => { b.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })); await wait(100) })
  shots.push({ click: text, dialog: dialog().slice(0, 400), buttons: [...document.querySelectorAll('[role=dialog] button')].map(x => x.textContent.trim()) })
}
console.log('HARNESS_JSON:' + JSON.stringify({ shots, calls }))
process.exit(0)
