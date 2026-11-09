// Node ESM resolve hook: map the Desktop SDK to a stub and react to a local node_modules.
const NM = process.env.HERMES_NODE_MODULES
const STUB = new URL('./sdk-stub.mjs', import.meta.url).href

export async function resolve(specifier, context, next) {
  if (specifier === '@hermes/plugin-sdk') return { url: STUB, shortCircuit: true }
  if (specifier === 'react' || specifier === 'react/jsx-runtime' || specifier === 'react-dom/server') {
    return next(new URL(`file://${NM}/${specifier}${specifier === 'react' ? '/index.js' : specifier === 'react/jsx-runtime' ? '.js' : '.node.js'}`).href, context)
  }
  return next(specifier, context)
}
