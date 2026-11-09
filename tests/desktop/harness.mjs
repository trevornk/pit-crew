// Renders the Desktop plugin's page, status-bar chip and nav entry against recorded backend JSON.
import { readFileSync } from 'node:fs'
import { register } from 'node:module'

const dataFile = process.argv[2]
globalThis.__CAR_DATA__ = JSON.parse(readFileSync(dataFile, 'utf8'))
register('./loader.mjs', import.meta.url)

const { createElement: h } = await import('react')
const { renderToStaticMarkup } = await import('react-dom/server')
const plugin = (await import(new URL('../../desktop/plugin.js', import.meta.url).href)).default

const contributions = []
const events = []
const ctx = {
  source: 'plugin:pit-crew',
  registerMany: cs => { contributions.push(...cs); return () => {} },
  register: c => { contributions.push(c); return () => {} },
  onEvent: name => { events.push(name); return () => {} },
  rest: async () => ({}),
}
plugin.register(ctx)

const out = { id: plugin.id, areas: contributions.map(c => `${c.id}:${c.area}`), nav: contributions.find(c => c.id === 'nav').data }
out.events = events
out.contributions = contributions.map(c => ({ id: c.id, area: c.area, hasRender: !!c.render, data: c.data && { ...c.data, label: c.data.label ? '[element]' : undefined } }))
for (const c of contributions) {
  if (c.render) out[c.id] = renderToStaticMarkup(c.render())
  // a data-form status-bar item: the host renders the <button>; its label is the element we registered
  else if (c.data && c.data.toggleLabel) out[c.id] = renderToStaticMarkup(c.data.label)
}
console.log('HARNESS_JSON:' + JSON.stringify(out))
