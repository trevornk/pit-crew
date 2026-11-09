// Resolve hook for interact.mjs: the live SDK stub, and react from HERMES_NODE_MODULES.
const NM = process.env.HERMES_NODE_MODULES
export async function resolve(spec, ctx, next) {
  if (spec === '@hermes/plugin-sdk') return { url: new URL('./live-stub.mjs', import.meta.url).href, shortCircuit: true }
  if (spec === 'react') return next('file://' + NM + '/react/index.js', ctx)
  return next(spec, ctx)
}
