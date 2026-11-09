// Clicks "Ask Hermes" in jsdom with a composer that is NOT mounted at first, to exercise the fallback:
// ctx.runAction('session.new') then a bounded number of insertText('new', ...) retries on host timers (ctx.setTimeout).
// node ask.mjs <data.json> <scenario: mounts_after_2 | never | no_runaction>
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
const sdk = await import(new URL('./live-stub.mjs', import.meta.url).href)
const plugin = (await import(new URL('../../desktop/plugin.js', import.meta.url).href)).default
const data = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const scenario = process.argv[3]
const log = { inserts: [], actions: [], timers: [], navigated: 0, clipboard: [], toasts: [] }
sdk.host.navigate = () => { log.navigated++ }
sdk.host.notify = (...a) => { log.toasts.push(a) }
sdk.host.composer = {
  async insertText(target, text) {
    log.inserts.push(target)
    if (target === null) return false
    return scenario === 'mounts_after_2' ? log.inserts.filter(t => t === 'new').length >= 3 : false
  },
  focus() {}
}
let page
const ctx = {
  registerMany: cs => { page = cs.find(c => c.id === 'page'); return () => {} }, register() { return () => {} }, onEvent: () => () => {},
  rest: async path => { const p = path.split('?')[0]; return p in data ? data[p] : { ok: true } },
  setTimeout: (fn, ms) => { log.timers.push(ms); return setTimeout(fn, 0) },
  os: { writeClipboard: async t => { log.clipboard.push(t); return true } }
}
if (scenario !== 'no_runaction') ctx.runAction = async a => { log.actions.push(a) }
plugin.register(ctx)
const wait = ms => new Promise(r => setTimeout(r, ms))
const root = createRoot(document.getElementById('root'))
await act(async () => { root.render(page.render()); await wait(100) })
const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Ask Hermes')
if (!b) { console.log('HARNESS_JSON:' + JSON.stringify({ missing: true })); process.exit(0) }
await act(async () => { b.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })); await wait(300) })
console.log('HARNESS_JSON:' + JSON.stringify(log))
