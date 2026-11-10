// Pit Crew — Desktop page, status-bar chip and sidebar entry.
// SDK only (catalog rule 8): imports are limited to '@hermes/plugin-sdk' and 'react'; layout uses inline
// styles with theme variables (no hard-coded colors); all data comes from this plugin's own backend
// (dashboard/plugin_api.py) through ctx.rest, with polling as the baseline and events as a nudge.
import {
  Badge, Button, Codicon, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
  EmptyState, Input, ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, Skeleton, StatusDot, Switch,
  host, queryClient, useQuery
} from '@hermes/plugin-sdk'
import { Component, createElement as h, Fragment, useRef, useState } from 'react'

const PLUGIN_ID = 'pit-crew'
const ROUTE = '/car'
const QK = [PLUGIN_ID]
let CTX = null

// ----------------------------------------------------------------------------- tiny style kit
const T = {
  primary: 'var(--ui-text-primary)',
  secondary: 'var(--ui-text-secondary)',
  tertiary: 'var(--ui-text-tertiary)',
  quaternary: 'var(--ui-text-quaternary)',
  stroke: 'var(--ui-stroke-secondary)',
  accent: 'var(--ui-accent)',
  tile: 'var(--ui-bg-quaternary)'
}
const S = {
  page: { display: 'flex', flexDirection: 'column', gap: 16, padding: 20, maxWidth: 1040, margin: '0 auto', width: '100%', overflowY: 'auto', height: '100%', boxSizing: 'border-box', color: T.primary },
  row: { display: 'flex', alignItems: 'center', gap: 8 },
  between: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' },
  card: { border: `1px solid ${T.stroke}`, borderRadius: 10, padding: 16, display: 'flex', flexDirection: 'column', gap: 12 },
  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 10 },
  grid2: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 16 },
  tile: { background: T.tile, borderRadius: 8, padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 2, minHeight: 64 },
  label: { fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', color: T.tertiary },
  small: { fontSize: 12, color: T.secondary },
  tiny: { fontSize: 11, color: T.tertiary },
  h2: { fontSize: 13, fontWeight: 600, margin: 0, color: T.primary },
  big: { fontSize: 40, fontWeight: 600, lineHeight: 1, fontVariantNumeric: 'tabular-nums' }
}

// ----------------------------------------------------------------------------- helpers
const api = (path, opts) => CTX.rest(path, opts)
const refresh = () => queryClient.invalidateQueries({ queryKey: QK })
// The parts of the car's state a command can change. A result panel is only true while this is unchanged.
const stateSig = status => {
  const st = status || {}, sec = st.security || {}, chg = st.charging || {}, cli = st.climate || {}
  return JSON.stringify([sec.locked, sec.windows_open, sec.trunk_open, sec.frunk_open, sec.sentry_on, cli.climate_on, chg.charging_state, chg.plugged_in])
}
const OUTDATED = 'This result is out of date: the car’s state has changed since. Close this and look at the current state.'
const num = (v, d = 0) => (typeof v === 'number' && Number.isFinite(v) ? v.toFixed(d) : '—')
// Humane ages: "just now", "7 min ago", "5 h ago", "3 days ago" (never "4.8 h").
const ageText = s => {
  if (s == null) return ''
  if (s < 60) return 'just now'
  if (s < 5400) return `${Math.round(s / 60)} min ago`
  if (s < 129600) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} days ago`
}
const when = iso => {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}
const clock = iso => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}
const cap = s => (s ? s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, ' ') : '')
// Which live/practice state applies (the same words as `/car`): live mode "always" vs on-demand timed windows.
const isAlways = a => !!(a && a.live_mode === 'always')
const liveText = a => !a ? '' : a.state === 'always' ? 'Live: always on'
  : a.state === 'paused' ? 'Paused (practice) — /car live to resume'
  : a.armed ? `Live${a.until_local ? ' until ' + clock(a.until_local) : ''}`
  : 'Practice mode · nothing is sent'
const goLiveLabel = (a, hours) => (isAlways(a) ? 'Go live' : `Go live for ${hours} h`)
// Where the car is, in words ("away" is not a place you can be "at").
const whereText = z => {
  const t = String(z || '').trim()
  if (!t || /^(away|not[_ ]home)$/i.test(t)) return 'away from home'
  if (/^(unknown|unavailable)$/i.test(t)) return 'location unknown'
  return `at ${t}`
}
const CHARGE_WORDS = { complete: 'Charge complete', stopped: 'Plugged in, not charging', disconnected: 'Not plugged in', no_power: 'Plugged in, no power', charging: 'Charging', starting: 'Starting to charge' }
const openSettings = () => { try { host.navigate('/settings?tab=plugins&agent=pit-crew') } catch (e) { toast('info', 'Open Settings → Plugins → Pit Crew.') } }
const toast = (kind, message) => host.notify({ kind, message })
// Run a backend action; on failure toast the reason instead of leaving the UI silently unchanged.
async function attempt(what, fn) {
  try {
    return await fn()
  } catch (err) {
    toast('error', `${what} failed: ${String((err && err.message) || err)}`)
    return null
  }
}

function useSummary() {
  return useQuery({ queryKey: [...QK, 'summary'], queryFn: () => api('/summary'), refetchInterval: 12000, staleTime: 4000 })
}

// ----------------------------------------------------------------------------- derived view of the car
function view(summary) {
  const st = (summary && summary.status) || {}
  const ok = !!(summary && summary.status && st.ok)
  const fresh = st.freshness || {}
  const stale = fresh.stale_fields || {}
  const unavailable = new Set(fresh.unavailable_fields || [])
  const sec = st.security || {}
  const chg = st.charging || {}
  const bat = st.battery || {}
  const cli = st.climate || {}
  const loc = st.location || {}
  const sw = st.software || {}
  const tires = st.tires || {}
  const newest = fresh.newest_update_age_s
  const isStale = ok && ((newest != null && newest > (fresh.stale_after_s || 900)) || (st.status && st.status.asleep))
  // Per-reading freshness. A reading older than the stale limit is shown as "last known" (still readable, never greyed
  // out like a disabled control); a recent one says when the car last confirmed it.
  const tileFresh = fields => {
    const f = fields.find(x => !unavailable.has(x))
    if (!f) return { text: 'not reported by this car', dim: true, missing: true }
    if (stale[f] != null) return { text: `last known · ${ageText(stale[f])}`, dim: true, stale: true }
    return { text: newest != null ? `confirmed ${ageText(newest)}` : 'confirmed', dim: false }
  }
  // Age of a single field when it is stale (so badges can say "as of 5 h ago"), else null.
  const staleAge = f => (stale[f] != null ? ageText(stale[f]) : null)
  const armed = !!(summary && summary.armed && summary.armed.armed)
  return { st, ok, sec, chg, bat, cli, loc, sw, tires, isStale, tileFresh, staleAge, unavailable, newest, armed }
}

// ----------------------------------------------------------------------------- building blocks
function Section({ title, right, children }) {
  return h('section', { style: S.card },
    h('div', { style: S.between }, h('h2', { style: S.h2 }, title), right || null),
    children)
}

function Tile({ label, value, sub, fresh, icon }) {
  return h('div', { style: { ...S.tile, opacity: fresh && fresh.missing ? 0.6 : 1, borderLeft: fresh && fresh.stale ? `2px dashed ${T.quaternary}` : 'none' } },
    h('div', { style: { ...S.row, justifyContent: 'space-between' } },
      h('span', { style: S.label }, label),
      icon ? h(Codicon, { name: icon, style: { color: T.tertiary } }) : null),
    h('div', { style: { fontSize: 15, fontWeight: 600 } }, value),
    sub ? h('div', { style: S.small }, sub) : null,
    fresh ? h('div', { style: { ...S.tiny, fontStyle: fresh.stale ? 'italic' : 'normal' } }, fresh.text) : null)
}

function Bar({ pct, marker, markerLabel = 'limit' }) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0))
  return h('div', { role: 'progressbar', 'aria-valuenow': Math.round(p), 'aria-valuemin': 0, 'aria-valuemax': 100,
    style: { position: 'relative', height: 8, borderRadius: 4, background: T.tile, overflow: 'hidden' } },
    h('div', { style: { width: `${p}%`, height: '100%', background: T.accent, borderRadius: 4, transition: 'width 300ms' } }),
    marker != null ? h('div', { title: `${markerLabel} ${marker}%`, style: { position: 'absolute', top: 0, left: `${Math.max(0, Math.min(100, marker))}%`, width: 2, height: '100%', background: T.secondary } }) : null)
}

// Legend that names the unlabeled marker on a bar ("| charge limit 80%").
function BarLegend({ marker, label }) {
  if (marker == null) return null
  return h('span', { style: { ...S.tiny, display: 'inline-flex', alignItems: 'center', gap: 4 } },
    h('span', { style: { display: 'inline-block', width: 2, height: 10, background: T.secondary } }), `${label} ${num(marker)}%`)
}

// ----------------------------------------------------------------------------- header + hero
function Header({ summary, onRefresh, loading, onGoLive }) {
  const armed = summary && summary.armed
  const stop = async () => {
    const r = await attempt('Stopping live mode', () => api('/disarm', { method: 'POST', body: {} }))
    if (r) toast('info', r.state === 'paused' ? 'Paused (practice). Nothing will be sent to the car until you go live again.' : 'Back to practice mode. Nothing will be sent to the car.')
    refresh()
  }
  return h('div', { style: S.between },
    h('div', { style: S.row },
      h(Codicon, { name: 'zap', size: 18 }),
      h('h1', { style: { fontSize: 18, fontWeight: 600, margin: 0 } }, 'Pit Crew'),
      summary && summary.simulated ? h(Badge, { variant: 'outline' }, 'Simulated car') : null,
      armed ? h(Fragment, null,
        h(Badge, { variant: armed.armed ? 'warn' : 'muted', title: armed.armed ? `Hermes can send commands to the car${isAlways(armed) ? ' until you press Stop' : ''}. Unlock, trunk and similar follow your Hermes approval setting.` : 'Commands are checked and logged but never sent to the car.' },
          liveText(armed)),
        armed.armed ? h(Button, { variant: 'ghost', size: 'xs', onClick: stop, title: isAlways(armed) ? 'Pause: practice mode until you go live again' : 'Back to practice mode' }, 'Stop')
          : h(Button, { variant: 'outline', size: 'xs', onClick: onGoLive }, 'Go live…')) : null),
    h(Button, { variant: 'ghost', size: 'xs', onClick: onRefresh, disabled: loading }, h(Codicon, { name: 'refresh' }), 'Refresh'))
}

function Hero({ v, summary }) {
  const { st, ok, sec, chg, bat, loc, isStale, staleAge } = v
  if (!ok) return h(ConnectCard, { summary })
  const charging = chg.charging_state === 'charging'
  const chargeLine = charging
    ? `Charging ${num(chg.charge_power_kw, 1)} kW → ${num(chg.charge_limit_pct)}%${chg.done_at ? ' · done ~' + clock(chg.done_at) : ''}`
    : CHARGE_WORDS[chg.charging_state] || (chg.plugged_in ? 'Plugged in' : chg.plugged_in === false ? 'Not plugged in' : '')
  const lockAge = staleAge('locked')
  const plugAge = staleAge('plugged_in')
  return h('section', { style: { ...S.card, opacity: isStale ? 0.7 : 1 } },
    h('div', { style: S.between },
      h('div', { style: { display: 'flex', flexDirection: 'column', gap: 4 } },
        h('div', { style: S.row }, h('span', { style: { fontSize: 15, fontWeight: 600 } }, st.vehicle.name),
          isStale ? h(Badge, { variant: 'warn' }, st.status && st.status.asleep ? 'Asleep' : 'Stale data') : null),
        h('div', { style: S.small }, [loc.location_zone ? whereText(loc.location_zone) : null, st.freshness && st.freshness.newest_update_age_s != null ? `car last reported ${ageText(st.freshness.newest_update_age_s)}` : null].filter(Boolean).join(' · '))),
      h('div', { style: S.row },
        sec.locked === undefined ? h(Badge, { variant: 'muted' }, 'Lock: unknown')
          : h(Badge, { variant: sec.locked ? 'success' : 'warn', title: lockAge ? `Last known state, ${lockAge}` : 'Confirmed by the car' },
            h(Codicon, { name: sec.locked ? 'lock' : 'unlock' }), sec.locked ? 'Locked' : 'Unlocked', lockAge ? ` · as of ${lockAge}` : null),
        chg.plugged_in === undefined ? null
          : h(Badge, { variant: 'outline', title: plugAge ? `Last known state, ${plugAge}` : 'Confirmed by the car' },
            h(Codicon, { name: 'plug' }), chg.plugged_in ? 'Plugged in' : 'Not plugged in', plugAge ? ` · as of ${plugAge}` : null))),
    h('div', { style: { display: 'flex', alignItems: 'flex-end', gap: 18, flexWrap: 'wrap' } },
      h('div', { style: S.big }, bat.battery_pct != null ? `${num(bat.battery_pct)}%` : '—'),
      h('div', { style: { flex: 1, minWidth: 220, display: 'flex', flexDirection: 'column', gap: 6 } },
        h(Bar, { pct: bat.battery_pct, marker: chg.charge_limit_pct, markerLabel: 'charge limit' }),
        h('div', { style: S.between },
          h('span', { style: S.small }, [bat.range_mi != null ? `${num(bat.range_mi)} mi of range` : 'range unknown'].join('')),
          h(BarLegend, { marker: chg.charge_limit_pct, label: 'charge limit' }),
          h('span', { style: { ...S.small, color: charging ? T.accent : T.secondary } }, h(Fragment, null, charging ? h(Codicon, { name: 'zap' }) : null, ' ', chargeLine))))))
}


// ----------------------------------------------------------------------------- connect your car
function ConnectCard({ summary }) {
  const conn = (summary && summary.connection) || {}
  const discover = async () => {
    const r = await attempt('Looking in Home Assistant', () => api('/discover', { method: 'POST', body: {} }))
    if (!r) return
    if (!r.ok) { toast('error', r.error || 'Could not look at Home Assistant.'); return }
    const rec = r.recommended
    toast('info', rec ? `Found “${rec.ha_slug}” (${rec.ha_profile}). Set Home Assistant · Car name to ${rec.ha_slug} in settings.` : 'No matching car found in Home Assistant.')
  }
  const useSim = () => toast('info', 'In settings, set Connection · Which car to “simulator”. Changes apply immediately.')
  return h(Section, { title: 'Connect your car' },
    h('div', { style: { fontSize: 13 } }, conn.message || 'Pit Crew isn’t connected to a car yet.'),
    conn.hint ? h('div', { style: S.small }, conn.hint) : null,
    h('div', { style: S.row },
      h(Button, { variant: 'default', size: 'sm', onClick: openSettings }, h(Codicon, { name: 'settings-gear' }), 'Open Pit Crew settings'),
      conn.kind === 'homeassistant' ? h(Button, { variant: 'outline', size: 'sm', onClick: discover }, 'Find my car in Home Assistant') : null,
      h(Button, { variant: 'ghost', size: 'sm', onClick: useSim }, 'Try the simulated car instead')))
}

// ----------------------------------------------------------------------------- go live (turns on real commands for a while)
function GoLiveDialog({ summary, onClose, onDone }) {
  const cancelRef = useRef(null)
  const [busy, setBusy] = useState(false)
  const hours = (summary.armed && summary.armed.default_hours) || summary.settings.live_default_hours || 1
  const always = isAlways(summary.armed)
  const go = async () => {
    setBusy(true)
    const r = await attempt('Going live', () => api('/arm', { method: 'POST', body: always ? { confirmed: true } : { confirmed: true, hours } }))
    setBusy(false)
    if (!r || r.ok === false) { if (r) toast('error', r.error || 'Could not go live.'); return }
    toast('info', always ? 'Live: always on (until you press Stop).' : `Live for ${r.hours || hours} h.`)
    refresh()
    onDone ? onDone() : onClose()
  }
  return h(Dialog, { open: true, onOpenChange: o => { if (!o && !busy) onClose() } },
    h(DialogContent, { className: 'max-w-md', onOpenAutoFocus: e => { if (cancelRef.current) { e.preventDefault(); cancelRef.current.focus() } } },
      h(DialogHeader, null, h(DialogTitle, null, always ? 'Resume live mode?' : `Go live for ${hours} hours?`),
        h(DialogDescription, null, summary.simulated
          ? 'This is the simulated car, so nothing real can happen.'
          : `Hermes will be able to send real commands to your car. Unlock, trunk, frunk, window vent, Sentry-off, honk and flash follow your Hermes approval setting (Hermes asks you when its approvals are on). ${always ? 'Live mode is set to always on, so it stays live until you press Stop.' : 'It switches back to practice mode by itself.'}`)),
      h(DialogFooter, null, h(Button, { ref: cancelRef, variant: 'secondary', autoFocus: true, disabled: busy, onClick: onClose }, 'Cancel'),
        h(Button, { variant: 'destructive', disabled: busy, onClick: go }, busy ? 'Going live…' : 'Go live'))))
}

// ----------------------------------------------------------------------------- quick actions (the three things people do)
function QuickActions({ summary, v, onPick }) {
  const canDo = new Set((summary && summary.status && summary.status.can_do) || [])
  const [temp, setTemp] = useState('70')
  if (!v.ok) return null
  const one = (id, text, icon, args) => {
    const reason = !canDo.has(id) ? 'This car can’t do that' : inapplicable(id, v)
    return h('div', { key: id, title: reason || text, style: { display: 'flex', flexDirection: 'column', gap: 2 } },
      h(Button, { variant: 'secondary', size: 'sm', disabled: !!reason, onClick: () => onPick({ command: id, args: args || {} }) }, h(Codicon, { name: icon }), text),
      reason ? h('span', { style: { ...S.tiny, paddingLeft: 4 } }, reason) : null)
  }
  return h('section', { style: { ...S.card, flexDirection: 'row', flexWrap: 'wrap', alignItems: 'flex-start', gap: 10, padding: 12 } },
    one('lock', 'Lock', 'lock'),
    h('div', { style: S.row },
      one('climate_on', 'Warm up', 'flame', { temp_f: Number(temp) }),
      h('select', { 'aria-label': 'Warm-up temperature', value: temp, onChange: e => setTemp(e.target.value),
        style: { height: 28, borderRadius: 6, border: `1px solid ${T.stroke}`, background: 'transparent', color: T.primary, fontSize: 12, padding: '0 4px' } },
        ...['66', '68', '70', '72', '74'].map(t => h('option', { key: t, value: t }, `${t}°F`)))),
    one('charge_start', 'Charge', 'zap'),
    h('div', { style: { display: 'flex', flexDirection: 'column', gap: 2 } },
      h(Button, { variant: 'outline', size: 'sm', onClick: () => askHermes('Give me a quick update on my car and anything I should do.') }, h(Codicon, { name: 'comment-discussion' }), 'Ask Hermes')))
}

// ----------------------------------------------------------------------------- setup warnings
function Warnings({ v, summary }) {
  if (!v.ok) return null   // the Connect card already explains it; don't say it twice
  const all = [...new Set((v.st && v.st.warnings) || [])]
  if (!all.length) return null
  return h('section', { style: { ...S.card, gap: 6 } },
    h('div', { style: S.row }, h(Badge, { variant: 'warn' }, h(Codicon, { name: 'warning' }), 'Needs attention'),
      h('span', { style: S.small }, 'Worth a look')),
    ...all.map((w, i) => h('div', { key: i, style: { fontSize: 13 } }, w)))
}

// ----------------------------------------------------------------------------- tiles
function Tiles({ v }) {
  const { ok, sec, chg, cli, sw, tires, tileFresh, staleAge, unavailable } = v
  if (!ok) return null
  const psi = [['FL', 'front_left_psi'], ['FR', 'front_right_psi'], ['RL', 'rear_left_psi'], ['RR', 'rear_right_psi']].filter(([, k]) => tires[k] != null)
  const lowest = psi.length ? Math.min(...psi.map(([, k]) => tires[k])) : null
  // the car's own TPMS warning, shown only while that reading is fresh
  const warned = [['FL', 'front_left_warning', 'tire_warn_fl'], ['FR', 'front_right_warning', 'tire_warn_fr'], ['RL', 'rear_left_warning', 'tire_warn_rl'], ['RR', 'rear_right_warning', 'tire_warn_rr']]
    .filter(([, k, f]) => tires[k] === true && staleAge(f) == null).map(([n]) => n)
  const openList = (val, none) => (Array.isArray(val) ? (val.length ? `${val.length} open` : none) : val === undefined ? '—' : val ? 'Open' : none)
  const names = val => (Array.isArray(val) && val.length ? val.map(x => x.replace(/_/g, ' ')).join(', ') : null)
  const flag = (val, on, off) => (val === undefined ? '—' : val ? on : off)
  return h('div', { style: S.grid },
    h(Tile, { label: 'Climate', icon: 'flame', value: cli.cabin_temp_f != null ? `${num(cli.cabin_temp_f)}°F inside` : '—',
      sub: [cli.outside_temp_f != null ? `${num(cli.outside_temp_f)}°F outside` : null, cli.climate_on === undefined ? null : cli.climate_on ? `on → ${num(cli.target_temp_f)}°F` : 'off'].filter(Boolean).join(' · '),
      fresh: tileFresh(['cabin_temp_f', 'climate_on']) }),
    h(Tile, { label: 'Doors', icon: 'sign-in', value: openList(sec.doors_open, 'Closed'), sub: names(sec.doors_open), fresh: tileFresh(['doors_open']) }),
    h(Tile, { label: 'Windows', icon: 'layout', value: openList(sec.windows_open, 'Closed'), sub: names(sec.windows_open), fresh: tileFresh(['windows_open']) }),
    h(Tile, { label: 'Trunk', icon: 'package', value: flag(sec.trunk_open, 'Open', 'Closed'), fresh: tileFresh(['trunk_open']) }),
    h(Tile, { label: 'Frunk', icon: 'package', value: flag(sec.frunk_open, 'Open', 'Closed'), fresh: tileFresh(['frunk_open']) }),
    h(Tile, { label: 'Sentry', icon: 'eye', value: flag(sec.sentry_on, 'On', 'Off'), fresh: tileFresh(['sentry_on']) }),
    h(Tile, { label: 'Charging', icon: 'plug', value: chg.charging_state ? (CHARGE_WORDS[chg.charging_state] || cap(chg.charging_state)) : '—',
      sub: chg.charge_current_a ? `${num(chg.charge_current_a)} A · ${num(chg.charger_voltage_v)} V` : null, fresh: tileFresh(['charging_state']) }),
    h(Tile, { label: 'Tires', icon: 'circle-large', value: warned.length ? `Car warns: ${warned.join(', ')}` : lowest != null ? `${num(lowest)} psi lowest` : '—',
      sub: psi.length ? psi.map(([n, k]) => `${n} ${num(tires[k])}`).join(' · ') : null,
      fresh: tileFresh(['tire_psi_fl', 'tire_psi_fr', 'tire_psi_rl', 'tire_psi_rr', 'tire_warn_fl', 'tire_warn_fr', 'tire_warn_rl', 'tire_warn_rr']) }),
    h(Tile, { label: 'Software', icon: 'versions', value: sw.software_update_available ? 'Update waiting' : 'Up to date',
      sub: [sw.software_version, sw.odometer_mi != null ? `${num(sw.odometer_mi)} mi` : null].filter(Boolean).join(' · '),
      fresh: unavailable.has('software_update_available') ? tileFresh(['software_update_available']) : null }))
}

// ----------------------------------------------------------------------------- commands + safety dialog (Cancel is the default)
// `hermes: true` = approval-class. Those are NEVER sent from this page: it previews the safety checks and hands the
// request to Hermes, whose own approval prompt is the only live approval (catalog rule 12).
const COMMANDS = [
  { id: 'lock', label: 'Lock', icon: 'lock', group: 'everyday' },
  { id: 'close_windows', label: 'Close windows', icon: 'layout', group: 'everyday' },
  { id: 'sentry_on', label: 'Sentry on', icon: 'eye', group: 'everyday' },
  { id: 'climate_on', label: 'Warm up (70°F)', icon: 'flame', group: 'everyday', args: { temp_f: 70 } },
  { id: 'climate_off', label: 'Climate off', icon: 'circle-slash', group: 'everyday' },
  { id: 'charge_start', label: 'Start charging', icon: 'zap', group: 'everyday' },
  { id: 'charge_stop', label: 'Stop charging', icon: 'debug-stop', group: 'everyday' },
  { id: 'unlock', label: 'Unlock', icon: 'unlock', group: 'risky', hermes: true },
  { id: 'open_trunk', label: 'Open trunk', icon: 'package', group: 'risky', hermes: true },
  { id: 'open_frunk', label: 'Open frunk', icon: 'package', group: 'risky', hermes: true },
  { id: 'vent_windows', label: 'Vent windows', icon: 'layout', group: 'risky', hermes: true },
  { id: 'sentry_off', label: 'Sentry off', icon: 'eye-closed', group: 'risky', hermes: true },
  { id: 'honk', label: 'Honk', icon: 'unmute', group: 'risky', hermes: true },
  { id: 'flash', label: 'Flash lights', icon: 'lightbulb', group: 'risky', hermes: true }
]
const CHECK_NAMES = { temperature_range: 'Temperature', charge_limit_range: 'Charge limit', charge_amps_range: 'Charging current', car_awake: 'Car awake', state_fresh: 'Fresh data', parked: 'Parked', trusted_zone: 'Place', plugged_in: 'Plugged in' }
// The policy's messages are written for the agent too ("retry with override_zone=true…"); people get the first sentence.
const plainRefusal = m => String(m || '').split(/\. (?:If the user|Ask the user|Give Pit Crew)/)[0].replace(/ \(Only if you accept.*$/, '').replace(/[.]+$/, '')
const label = id => (COMMANDS.find(c => c.id === id) || { label: cap(id) }).label

const INAPPLICABLE_FIELDS = {
  lock: ['locked'], unlock: ['locked'], close_windows: ['windows_open'], vent_windows: ['windows_open'], open_trunk: ['trunk_open'],
  open_frunk: ['frunk_open'], sentry_on: ['sentry_on'], sentry_off: ['sentry_on'], climate_on: ['climate_on'], climate_off: ['climate_on'],
  charge_start: ['charging_state', 'plugged_in'], charge_stop: ['charging_state']
}
// Why a command makes no sense right now (null = applicable). Based only on what the car last reported.
function inapplicable(id, v) {
  const { sec, chg, cli } = v
  // A stale reading is not a reason to grey a button out ("Already locked" from a reading taken before the car slept).
  if ((INAPPLICABLE_FIELDS[id] || []).some(f => v.staleAge(f))) return null
  const none = a => Array.isArray(a) && a.length === 0
  const charging = chg.charging_state === 'charging' || chg.charging_state === 'starting'
  switch (id) {
    case 'lock': return sec.locked === true ? 'Already locked' : null
    case 'unlock': return sec.locked === false ? 'Already unlocked' : null
    case 'close_windows': return none(sec.windows_open) ? 'Windows are already closed' : null
    case 'vent_windows': return Array.isArray(sec.windows_open) && sec.windows_open.length ? 'A window is already open' : null
    case 'open_trunk': return sec.trunk_open === true ? 'Trunk is already open' : null
    case 'open_frunk': return sec.frunk_open === true ? 'Frunk is already open' : null
    case 'sentry_on': return sec.sentry_on === true ? 'Sentry is already on' : null
    case 'sentry_off': return sec.sentry_on === false ? 'Sentry is already off' : null
    case 'climate_on': return cli.climate_on === true ? 'Climate is already on' : null
    case 'climate_off': return cli.climate_on === false ? 'Climate is already off' : null
    case 'charge_start': return charging ? 'Already charging' : chg.plugged_in === false ? 'Not plugged in: plug the car in first' : null
    case 'charge_stop': return chg.charging_state !== undefined && !charging ? 'Not charging right now' : null
    default: return null
  }
}

// Hand a request to Hermes through the SDK's supported composer API (host.composer). If no chat composer is mounted
// (this page replaced the chat view) start a new chat with the host's own 'session.new' action and retry a few times
// (bounded, host timers) while the composer mounts; as a last resort copy the text for pasting.
const COMPOSER_RETRIES = 5
const COMPOSER_RETRY_MS = 250
const wait = ms => new Promise(r => (CTX && CTX.setTimeout ? CTX.setTimeout(r, ms) : setTimeout(r, ms)))

async function askHermes(text) {
  let ok = false
  try { ok = !!(await host.composer.insertText(null, text, { mode: 'block' })) } catch (e) { ok = false }
  if (!ok) {
    try {
      if (CTX && CTX.runAction) await CTX.runAction('session.new')
      else host.navigate('/')
      for (let i = 0; i < COMPOSER_RETRIES && !ok; i++) {
        await wait(COMPOSER_RETRY_MS)
        try { ok = !!(await host.composer.insertText('new', text, { mode: 'block' })) } catch (e) { ok = false }
      }
    } catch (e) { ok = false }
  }
  if (ok) {
    try { host.composer.focus(null) } catch (e) { /* the text is in the draft either way */ }
    toast('info', 'Drafted in the chat. Press Enter, then answer Hermes’ approval prompt if it asks.')
    return true
  }
  let copied = false
  try { copied = !!(CTX && CTX.os && (await CTX.os.writeClipboard(text))) } catch (e) { copied = false }
  toast('info', copied ? 'Copied. Paste it into a chat with Hermes.' : `Open a chat and say: ${text}`)
  return false
}

function SafetyDialog({ request, summary, onClose }) {
  const sigNow = stateSig(summary && summary.status)
  const cancelRef = useRef(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [resultSig, setResultSig] = useState(null)
  const outdated = !!result && resultSig !== null && resultSig !== sigNow
  const q = useQuery({
    queryKey: [...QK, 'preflight', request && request.command, JSON.stringify((request && request.args) || {})],
    queryFn: () => api('/preflight', { method: 'POST', body: { command: request.command, args: request.args || {} } }),
    enabled: !!request, staleTime: 0, gcTime: 0, retry: false
  })
  if (!request) return null
  const pf = q.data
  const live = pf && pf.mode === 'live'
  const viaHermes = !!(pf && pf.via_hermes)
  const run = async () => {
    setBusy(true)
    try {
      const res = await api('/command', { method: 'POST', body: { command: request.command, args: request.args || {}, confirmed: true, reason: 'Pit Crew page' } })
      setResult(res)
      toast(res.ok ? 'info' : 'error', res.message || 'Done')
      // Remember the state right after this command, so a later, different state marks this result outdated.
      try { await refresh() } catch (e) { /* the next poll will do */ }
      const fresh = queryClient.getQueryData([...QK, 'summary'])
      setResultSig(stateSig(fresh && fresh.status))
    } catch (err) {
      setResult({ ok: false, message: String((err && err.message) || err) })
    } finally {
      setBusy(false)
    }
  }
  const ask = async () => { setBusy(true); try { await askHermes(pf.ask_hermes); onClose() } finally { setBusy(false) } }
  // Practice mode: one click goes live (still behind this dialog) and then sends, or drafts the request for Hermes.
  const hours = (summary && summary.armed && summary.armed.default_hours) || 1
  const goLiveAnd = async next => {
    setBusy(true)
    try {
      const r = await api('/arm', { method: 'POST', body: isAlways(summary && summary.armed) ? { confirmed: true } : { confirmed: true, hours } })
      if (r && r.ok === false) { toast('error', r.error || 'Could not go live.'); return }
      refresh()
    } catch (err) {
      toast('error', `Could not go live: ${String((err && err.message) || err)}`)
      return
    } finally { setBusy(false) }
    await next()
  }
  return h(Dialog, { open: true, onOpenChange: o => { if (!o && !busy) onClose() } },
    h(DialogContent, {
      className: 'max-w-md',
      // Cancel — not Confirm — gets focus, and Enter never confirms a car command.
      onOpenAutoFocus: e => { if (cancelRef.current) { e.preventDefault(); cancelRef.current.focus() } }
    },
      h(DialogHeader, null,
        h(DialogTitle, null, result ? 'Result' : viaHermes ? `${label(request.command)}: Hermes will ask you` : `${label(request.command)}?`),
        h(DialogDescription, null, result ? '' : q.isError ? 'Could not check the safety rules.' : (pf && pf.summary) || 'Checking the safety rules…')),
      result ? h('div', { style: { display: 'flex', flexDirection: 'column', gap: 8 } },
        !outdated ? h(Badge, { variant: result.ok ? (result.status === 'confirmed' ? 'success' : 'warn') : 'destructive' }, cap(result.status || (result.ok ? 'done' : 'failed'))) : null,
        outdated ? h(Badge, { variant: 'muted' }, 'Outdated') : null,
        h('div', { style: { fontSize: 13 } }, outdated ? OUTDATED : result.message),
        !outdated && result.readback ? h('div', { style: S.tiny }, `Read-back: ${result.readback.status} — ${result.readback.detail || ''}`) : null)
        : h('div', { style: { display: 'flex', flexDirection: 'column', gap: 10 } },
          q.isLoading ? h(Skeleton, { style: { height: 60 } }) : null,
          q.isError ? h('div', { style: { fontSize: 13 } }, `${String((q.error && q.error.message) || 'The backend did not answer')}. Nothing was sent. Close this and try again.`) : null,
          pf ? h(Fragment, null,
            h('div', { style: S.row },
              h(Badge, { variant: live ? 'warn' : 'muted' }, live ? 'LIVE: this will be sent' : 'Practice mode: nothing is sent'),
              h(Badge, { variant: 'outline' }, cap(pf.risk || 'unknown'))),
            viaHermes ? h('div', { style: S.small }, 'This kind of command is requested in chat, not sent by the button. It follows your Hermes approval setting: with approvals on (manual or smart) Hermes asks you; with approvals off it sends without asking and its reply says so. The checks below are what it will enforce.') : null,
            !live && !viaHermes ? h('div', { style: S.small }, 'Practice mode is on, so nothing is sent unless you go live.') : null,
            pf.policy ? h('ul', { style: { margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 4 } },
              ...pf.policy.checks.map((c, i) => h('li', { key: i, style: { ...S.small, display: 'flex', gap: 6, color: c.ok ? T.secondary : T.primary } },
                h(Codicon, { name: c.ok ? 'check' : 'error' }), h('span', null, `${CHECK_NAMES[c.check] || cap(c.check)}${c.detail ? ': ' + c.detail : ''}`)))) : null,
            ...(pf.policy && pf.policy.warnings ? pf.policy.warnings.map((w, i) => h('div', { key: 'w' + i, style: S.small }, `⚠ ${w}`)) : []),
            !pf.ok ? h('div', { style: { fontSize: 13, fontWeight: 600 } }, `Not allowed right now: ${plainRefusal(pf.message)}`) : null) : null),
      h(DialogFooter, null,
        h(Button, { ref: cancelRef, variant: 'secondary', onClick: onClose, disabled: busy, autoFocus: true }, result || q.isError ? 'Close' : 'Cancel'),
        result || q.isError ? null : viaHermes
          ? h(Fragment, null,
            !live ? h(Button, { variant: 'outline', disabled: busy || !pf || !pf.ok, onClick: run, title: 'Logs the attempt, sends nothing' }, 'Practice run') : null,
            !live ? h(Button, { variant: 'default', disabled: busy || !pf || !pf.ok, onClick: () => goLiveAnd(ask) }, h(Codicon, { name: 'comment-discussion' }), `${goLiveLabel(summary && summary.armed, hours)} & ask Hermes`)
              : h(Button, { variant: 'default', disabled: busy || !pf || !pf.ok, onClick: ask }, h(Codicon, { name: 'comment-discussion' }), 'Ask Hermes'))
          : live
            ? h(Button, { variant: 'default', disabled: busy || !pf || !pf.ok, onClick: run }, busy ? 'Sending…' : 'Send command')
            : h(Fragment, null,
              h(Button, { variant: 'outline', disabled: busy || !pf || !pf.ok, onClick: run, title: 'Logs the attempt, sends nothing' }, 'Practice run'),
              h(Button, { variant: 'default', disabled: busy || !pf || !pf.ok, onClick: () => goLiveAnd(run) }, busy ? 'Working…' : `${goLiveLabel(summary && summary.armed, hours)} & send`)))))
}

function CommandButton({ c, v, canDo, onPick }) {
  const unsupported = !canDo.has(c.id)
  const reason = unsupported ? 'This car can’t do that' : inapplicable(c.id, v)
  const btn = h(Button, {
    variant: c.hermes ? 'outline' : 'secondary', size: 'sm', disabled: !!reason || !v.ok,
    style: { width: '100%', justifyContent: 'flex-start' },
    onClick: () => onPick({ command: c.id, args: c.args || {} })
  }, h(Codicon, { name: c.icon }), c.label)
  // A disabled <button> swallows hover, so the reason lives on a wrapper and is also printed under the label.
  return h('div', { title: reason || (c.hermes ? 'Previews the safety checks, then asks Hermes' : c.label), style: { display: 'flex', flexDirection: 'column', gap: 2 } },
    btn, reason ? h('span', { style: { ...S.tiny, paddingLeft: 4 } }, reason) : null)
}

function Commands({ summary, v, onPick }) {
  if (!v.ok) {
    return h(Section, { title: 'More controls' }, h('div', { style: S.small }, 'Commands are unavailable until Pit Crew can read your car.'))
  }
  const canDo = new Set((summary && summary.status && summary.status.can_do) || [])
  const grid = { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 8, alignItems: 'start' }
  const armed = !!(summary && summary.armed && summary.armed.armed)
  const paused = !!(summary && summary.armed && summary.armed.state === 'paused')
  const group = g => COMMANDS.filter(c => c.group === g).map(c => h(CommandButton, { key: c.id, c, v, canDo, onPick }))
  return h(Section, { title: 'More controls',
    right: h(Badge, { variant: armed ? 'warn' : 'muted' }, liveText(summary && summary.armed)) },
    h('div', { style: S.tiny }, armed
      ? 'Every command is safety-checked, recorded, and confirmed by re-reading the car. A greyed-out button says why, right under its label.'
      : paused ? 'Paused (practice): these buttons check the safety rules and log the attempt, but send nothing. Use Go live at the top to resume (live mode is always on until you press Stop). A greyed-out button says why, right under its label.'
      : 'Practice mode: these buttons check the safety rules and log the attempt, but send nothing. Use Go live at the top to send for real. A greyed-out button says why, right under its label.'),
    h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6 } },
      h('span', { style: S.label }, 'Everyday'), h('div', { style: grid }, ...group('everyday'))),
    h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6, borderTop: `1px solid ${T.stroke}`, paddingTop: 10 } },
      h('div', { style: S.row }, h(Codicon, { name: 'shield', style: { color: T.tertiary } }), h('span', { style: S.label }, 'Asked in chat, under your Hermes approval setting')),
      h('div', { style: S.tiny }, 'Unlock, trunk, frunk, vent, Sentry off, honk and flash are requested in chat, not sent by these buttons. They follow your Hermes approval setting: with approvals on (manual or smart) Hermes asks you first; with approvals off it sends without asking and its reply says so.'),
      h('div', { style: grid }, ...group('risky'))))
}

// ----------------------------------------------------------------------------- readiness
function Readiness({ summary, onPick }) {
  const defaults = (summary && summary.settings) || {}
  const usual = String(defaults.default_departure || '07:30')
  const passedToday = (() => { const m = usual.match(/^(\d{1,2}):(\d{2})$/); if (!m) return false; const n = new Date(); return n.getHours() * 60 + n.getMinutes() >= Number(m[1]) * 60 + Number(m[2]) })()
  const [departure, setDeparture] = useState(/^\d{1,2}:\d{2}$/.test(usual) ? (passedToday ? `tomorrow ${usual}` : usual) : usual)
  const [miles, setMiles] = useState(String(defaults.default_trip_mi || 30))
  const [round, setRound] = useState(false)
  const [res, setRes] = useState(null)
  const [busy, setBusy] = useState(false)
  const check = async () => {
    setBusy(true)
    try {
      const qs = new URLSearchParams({ departure, trip_miles: miles, round_trip: String(round) })
      setRes(await api(`/readiness?${qs.toString()}`))
    } catch (err) {
      setRes({ ok: false, error: String((err && err.message) || err) })
    } finally {
      setBusy(false)
    }
  }
  const verdictVariant = { ready: 'success', tight: 'warn', not_ready: 'destructive' }
  const down = !!(summary.status && !summary.status.ok)
  return h(Section, { title: 'Will I be ready?' },
    h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' } },
      h('label', { style: { display: 'flex', flexDirection: 'column', gap: 3, flex: 1, minWidth: 150 } }, h('span', { style: S.tiny }, 'Leaving'),
        h(Input, { value: departure, onChange: e => setDeparture(e.target.value), placeholder: 'tomorrow 7:30 AM' })),
      h('label', { style: { display: 'flex', flexDirection: 'column', gap: 3, width: 110 } }, h('span', { style: S.tiny }, 'Miles (one way)'),
        h(Input, { value: miles, onChange: e => setMiles(e.target.value), inputMode: 'decimal' })),
      h('label', { style: { ...S.row, paddingBottom: 6 } }, h(Switch, { checked: round, onCheckedChange: setRound }), h('span', { style: S.small }, 'Round trip (doubles it)')),
      h(Button, { onClick: check, disabled: busy || down, title: down ? 'Pit Crew can’t read your car yet' : '' }, busy ? 'Checking…' : 'Check')),
    !res ? h('div', { style: S.tiny }, `Say when you're leaving (“tomorrow 7:30 AM”, “Fri 8am”, “Nov 14 6pm”) and how far you're going, then Check. Your usual trip (${defaults.default_trip_mi || 30} mi) is pre-filled from settings.`) : null,
    res && !res.ok ? h('div', { style: S.small }, `${res.error || 'Could not work that out.'}${res.hint ? ' (' + res.hint + ')' : ''}`) : null,
    res && res.ok ? h('div', { style: { display: 'flex', flexDirection: 'column', gap: 8 } },
      h('div', { style: S.tiny }, `Leaving ${when(res.departure)} (in ${res.hours_until_departure} h) · ${num(res.trip.miles, 0)} mi ${res.trip.round_trip ? 'round trip' : 'one way'}`),
      h('div', { style: S.row }, h(Badge, { variant: verdictVariant[res.verdict] || 'muted' }, cap(res.verdict)),
        h('span', { style: S.small }, `${num(res.battery_now_pct)}% now → ${num(res.projected_at_departure_pct)}% at departure · needs ~${num(res.needed_pct)}%`)),
      h(Bar, { pct: res.projected_at_departure_pct, marker: res.needed_pct, markerLabel: 'needed' }),
      h('div', { style: { fontSize: 13 } }, res.summary),
      h('div', { style: S.tiny }, `Assumes ${res.assumptions.efficiency}; ${num(res.trip.wh_per_mi)} Wh/mi; ${res.assumptions.safety_margin} + ${res.assumptions.reserve_pct}% reserve.`),
      ...(res.recommendations || []).map((r, i) => {
        // A time-based tip ("warm it at 7:20") must not become "do it now" at 11 PM.
        const later = r.at && new Date(r.at).getTime() > Date.now() + 5 * 60000
        return h('div', { key: i, style: { ...S.row, justifyContent: 'space-between', gap: 10 } },
          h('span', { style: S.small }, `${cap(r.action)}: ${r.why}`),
          r.command ? h('div', { style: S.row },
            later ? h(Button, { size: 'xs', variant: 'default', onClick: () => askHermes(`Warm the car at ${clock(r.at)} (${when(r.at)}) before I leave.`) }, `Schedule for ${clock(r.at)}`) : null,
            h(Button, { size: 'xs', variant: later ? 'ghost' : 'outline', onClick: () => onPick({ command: r.command, args: r.args || {} }) }, later ? 'Warm it now' : 'Do it…')) : null)
      })) : null)
}

// ----------------------------------------------------------------------------- alerts
// "Nothing new" next to open alerts reads as all clear, so always say how many are still open.
const checkSummary = (raised, stillOpen) => {
  const n = Number(raised) || 0, total = stillOpen + n
  const head = n ? `${n} new alert${n === 1 ? '' : 's'}` : 'No new alerts'
  return `Checked the car: ${head}${total ? ` · ${total} ${n ? 'open' : 'still open'}` : ' · all clear'}.`
}
const SEV_ORDER = { high: 0, medium: 1, low: 2, info: 3 }
const cleanTitle = t => String(t || '').replace(/^\[Simulated\]\s*/, '')
function Alerts({ summary, onPick, v }) {
  const [showAcked, setShowAcked] = useState(false)
  const all = (summary && summary.alerts) || []
  const open = all.filter(a => a.state !== 'acked').sort((a, b) => (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9) || Number(b.id) - Number(a.id))
  const acked = all.filter(a => a.state === 'acked')
  const shown = showAcked ? [...open, ...acked] : open
  const ack = async id => { await attempt('Acknowledge', () => api('/alerts/ack', { method: 'POST', body: { id } })); refresh() }
  const check = async () => {
    const r = await attempt('Check', () => api('/alerts/check', { method: 'POST', body: {} }))
    if (r) toast(r.ok ? 'info' : 'error', r.ok ? checkSummary(r.raised, open.length) : (r.error || 'Could not check the car.'))
    refresh()
  }
  const sev = { high: 'destructive', medium: 'warn', low: 'muted', info: 'muted' }
  // The alert's own one-click fix. Every fix goes through the same confirm dialog; approval-class fixes go through Hermes.
  const fix = a => {
    if (!a.action || a.state === 'acked') return null
    if (a.action.via_hermes) return h(Button, { size: 'xs', variant: 'default', onClick: () => askHermes(a.action.ask) }, h(Codicon, { name: 'comment-discussion' }), 'Ask Hermes')
    return h(Button, { size: 'xs', variant: 'default', onClick: () => onPick({ command: a.action.command, args: {} }) }, a.action.label)
  }
  return h(Section, { title: `Alerts${open.length ? ` (${open.length})` : ''}`,
    right: h('div', { style: S.row },
      open.length > 1 ? h(Button, { size: 'xs', variant: 'ghost', onClick: () => ack('all') }, 'Acknowledge all') : null,
      h(Button, { size: 'xs', variant: 'outline', onClick: check, disabled: !v.ok }, h(Codicon, { name: 'pulse' }), 'Check now'))
  },
    !v.ok && shown.length === 0 ? h(EmptyState, { title: 'Can’t check yet', description: 'Pit Crew can’t watch a car it isn’t connected to.' })
      : shown.length === 0 ? h(EmptyState, { title: 'All clear', description: 'Nothing needs your attention right now.' })
      : h('div', { style: { display: 'flex', flexDirection: 'column', gap: 10 } },
        ...shown.map(a => h('div', { key: a.id, style: { ...S.tile, gap: 4, opacity: a.state === 'acked' ? 0.6 : 1 } },
          h('div', { style: S.between },
            h('div', { style: S.row }, h(Badge, { variant: sev[a.severity] || 'muted' }, cap(a.severity)), h('strong', { style: { fontSize: 13 } }, cleanTitle(a.title))),
            h('span', { style: S.tiny, title: summary.simulated ? 'The simulator’s clock, which Fast-forward moves. History below uses real time.' : '' }, (summary.simulated ? 'simulated ' : '') + when(a.when))),
          h('div', { style: S.small }, a.body),
          h('div', { style: S.between }, h('span', { style: { ...S.small, color: T.primary } }, userSuggestion(a)),
            h('div', { style: S.row }, fix(a),
              a.state === 'acked' ? h('span', { style: S.tiny }, 'acknowledged') : h(Button, { size: 'xs', variant: 'ghost', onClick: () => ack(String(a.id)) }, 'Got it')))))),
    acked.length ? h(Button, { size: 'xs', variant: 'ghost', onClick: () => setShowAcked(!showAcked) }, showAcked ? 'Hide acknowledged' : `Show ${acked.length} acknowledged`) : null)
}
// Suggestions are written for the agent ("Want me to lock it? (lock needs no approval)"); people get a short verb.
function userSuggestion(a) {
  if (a.action && a.state !== 'acked') return a.action.via_hermes ? 'Hermes can do this once you approve.' : ''
  const t = String(a.suggestion || '').replace(/\s*\([^)]*approval[^)]*\)/gi, '').replace(/^Want me to /i, '').replace(/\?$/, '.').trim()
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : ''
}

// ----------------------------------------------------------------------------- audit log
const WHO = { tool: 'Hermes (chat)', slash: '/car', desktop: 'Pit Crew page', web: 'Pit Crew page', cron: 'Scheduled job' }
const OUTCOME = { confirmed: ['✓ Car confirmed', 'success'], sent_unconfirmed: ['Sent, not confirmed yet', 'warn'], dry_run: ['Practice run', 'muted'],
  blocked: ['Refused', 'destructive'], failed: ['Failed', 'destructive'], unknown_outcome: ['Unclear if it was sent', 'warn'], rate_limited: ['Too many commands', 'warn'], duplicate: ['Skipped (repeat)', 'muted'] }
const APPROVAL = { confirmed: 'You approved', hermes_auto: 'Approved by Hermes (no prompt)', denied: 'You declined', not_required: 'Not needed', missing: 'Not given', unattended: 'No one to ask' }
function AuditLog({ simulated }) {
  const q = useQuery({ queryKey: [...QK, 'log'], queryFn: () => api('/log?limit=25'), refetchInterval: 15000 })
  const rows = (q.data && q.data.rows) || []
  const th = { textAlign: 'left', padding: '4px 8px', fontSize: 11, color: T.tertiary, fontWeight: 500, whiteSpace: 'nowrap' }
  const td = { padding: '5px 8px', fontSize: 12, color: T.secondary, whiteSpace: 'nowrap', borderTop: `1px solid ${T.stroke}` }
  return h(Section, { title: 'History of commands', right: h('span', { style: S.tiny }, simulated ? 'every attempt, newest first · real clock time' : 'every attempt, newest first') },
    q.isLoading ? h(Skeleton, { style: { height: 80 } })
      : q.isError ? h(EmptyState, { title: 'Could not load the history', description: String((q.error && q.error.message) || 'The backend did not answer.') })
      : rows.length === 0 ? h(EmptyState, { title: 'Nothing yet', description: 'Commands you or Hermes try will be recorded here.' })
      : h('div', { style: { overflowX: 'auto' } }, h('table', { style: { borderCollapse: 'collapse', width: '100%' } },
        h('thead', null, h('tr', null, ...['When', 'Who', 'What', 'Outcome', 'Approval'].map(c => h('th', { key: c, style: th }, c)))),
        h('tbody', null, ...rows.map(r => {
          if (r.command === 'arm' || r.command === 'disarm') {
            return h('tr', { key: r.id }, h('td', { style: td }, when(r.when)), h('td', { style: td }, WHO[r.surface] || r.surface),
              h('td', { style: { ...td, fontStyle: 'italic' }, colSpan: 3 }, r.command === 'arm' ? 'Went live' : 'Back to practice mode'))
          }
          const args = r.args && typeof r.args === 'object' ? Object.entries(r.args).filter(([k]) => k !== 'override_zone' && k !== 'wake_ok').map(([k, v]) => `${k}=${v}`).join(', ') : ''
          const [text, variant] = OUTCOME[r.status] || [cap(r.status), 'muted']
          return h('tr', { key: r.id, title: r.detail || '' },
            h('td', { style: td }, when(r.when)), h('td', { style: td }, WHO[r.surface] || r.surface),
            h('td', { style: { ...td, color: T.primary } }, cap(r.command) + (args ? ` (${args})` : '')),
            h('td', { style: td }, h(Badge, { variant, size: 'xs' }, text)),
            h('td', { style: td }, APPROVAL[r.approval] || '—'))
        })))))
}

// ----------------------------------------------------------------------------- settings
function RuleRow({ rule }) {
  const save = async changes => {
    const r = await attempt(`Saving “${rule.title}”`, () => api(`/rules/${rule.id}`, { method: 'POST', body: changes }))
    if (r && r.ok === false) toast('error', r.error || 'That value was not accepted.')
    refresh()
  }
  const settings = Object.entries(rule.settings || {})
  return h('div', { style: { ...S.tile, minHeight: 0, gap: 6 } },
    h('div', { style: S.between },
      h('div', { style: { display: 'flex', flexDirection: 'column' } }, h('strong', { style: { fontSize: 13 } }, rule.title), h('span', { style: S.tiny }, rule.note || rule.description)),
      h(Switch, { checked: rule.enabled, onCheckedChange: v => save({ enabled: v }) })),
    settings.length ? h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
      ...settings.map(([k, v]) => h('label', { key: k, style: { display: 'flex', alignItems: 'center', gap: 4 } },
        h('span', { style: S.tiny }, (rule.labels && rule.labels[k]) || k.replace(/_/g, ' ')),
        h(Input, { key: String(v), defaultValue: String(v), style: { width: 70, height: 24 }, inputMode: 'decimal',
          onBlur: e => { const n = Number(e.target.value); if (Number.isFinite(n) && n !== v) save({ settings: { [k]: n } }) } })))) : null)
}

function Settings({ summary }) {
  const rq = useQuery({ queryKey: [...QK, 'rules'], queryFn: () => api('/rules'), refetchInterval: 30000 })
  const s = summary.settings
  const mins = sec => (sec % 60 === 0 ? `${sec / 60} min` : `${sec} s`)
  const facts = [
    `Connection: ${(summary.connection && summary.connection.label) || summary.adapter.label || s.adapter}`,
    `Unlock is only allowed ${s.trusted_zones && s.trusted_zones.length ? 'at ' + s.trusted_zones.join(', ') : 'nowhere (no trusted places set)'}`,
    `Risky commands are refused if the car’s data is older than ${mins(s.max_state_age_s)}`,
    `Checks the car every ${mins(s.poll_interval_s)}`
  ]
  return h(Section, { title: 'Safety & settings', right: h(Button, { size: 'xs', variant: 'outline', onClick: openSettings }, h(Codicon, { name: 'settings-gear' }), 'Open settings') },
    h('ul', { style: { margin: 0, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 2 } }, ...facts.map((f, i) => h('li', { key: i, style: S.small }, f))),
    h('div', { style: S.tiny }, 'Connection details, trusted places and thresholds live in Settings → Plugins → Pit Crew. Changes apply immediately.'),
    h('div', { style: { ...S.between, marginTop: 4 } }, h('strong', { style: { fontSize: 13 } }, 'Alert rules'),
      h('span', { style: S.tiny }, summary.notify.bound ? `alerts are sent to ${summary.notify.target}` : 'No alert destination: type /car notify here in a chat (or /car notify telegram) to get alerts sent there; until then they show up here and in your next chat')),
    rq.isLoading ? h(Skeleton, { style: { height: 60 } })
      : rq.isError ? h('div', { style: S.small }, 'Couldn’t load the alert rules.')
      : h('div', { style: S.grid2 }, ...((rq.data && rq.data.rules) || []).map(r => h(RuleRow, { key: r.id, rule: r }))))
}

function SimControls({ firstRun }) {
  const act = async (action, params, text) => {
    const r = await attempt('Simulator', () => api('/sim', { method: 'POST', body: { action, params: params || {} } }))
    if (r) toast(r.ok ? 'info' : 'error', r.ok ? `Simulator: ${r.note}` : r.error)
    refresh()
  }
  const [open, setOpen] = useState(!!firstRun)
  const buttons = [
    ['Fast-forward 30 min', 'advance', { minutes: 30 }], ['Fast-forward 3 h', 'advance', { minutes: 180 }],
    ['Drive 20 mi away', 'drive', { miles: 20, to: 'away' }], ['Park at home', 'park_home'], ['Plug in', 'plug_in'], ['Unplug', 'unplug'],
    ['Leave it unlocked', 'leave_unlocked'], ['Leave a door open', 'open_door'], ['Close everything', 'close_doors'],
    ['Leak a tire (6 psi/h)', 'leak', { tire: 'rr', psi_per_hour: 6 }], ['Pump up tires', 'inflate'], ['Cold snap (18°F)', 'cold', { temp_f: 18 }], ['Normal weather', 'normal_weather'], ['Reset', 'reset']]
  return h(Section, { title: 'Try it: simulator controls', right: h('div', { style: S.row }, h(Badge, { variant: 'outline' }, 'demo only'),
    h(Button, { size: 'xs', variant: 'ghost', onClick: () => setOpen(!open) }, open ? 'Hide' : 'Show')) },
    open ? h(Fragment, null,
      h('div', { style: S.tiny }, 'Make things happen to the simulated car, then watch the alerts and the readiness check react. Try: Drive away, Leave it unlocked, Fast-forward 30 min.'),
      h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
        ...buttons.map(([text, action, params]) => h(Button, { key: text, size: 'sm', variant: 'outline', onClick: () => act(action, params) }, text)))) : null)
}

function SimBanner() {
  return h('section', { style: { ...S.card, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', padding: 12 } },
    h('div', { style: { fontSize: 13 } }, h('strong', null, 'This is a simulated car. '), 'The simulator pretends to be a car: it reports state, accepts commands and raises alerts, but no real car is touched. Try “Drive 20 mi away” then “Leave it unlocked” below and watch the alert appear.'),
    h(Button, { size: 'sm', variant: 'default', onClick: openSettings }, 'Connect my real car'))
}

// ----------------------------------------------------------------------------- page
function Page() {
  const q = useSummary()
  const [request, setRequest] = useState(null)
  const [goLive, setGoLive] = useState(false)
  const summary = q.data
  const v = view(summary)
  if (q.isLoading) {
    return h('div', { style: S.page }, h(Skeleton, { style: { height: 40 } }), h(Skeleton, { style: { height: 140 } }), h(Skeleton, { style: { height: 200 } }))
  }
  if (q.isError || !summary) {
    return h('div', { style: S.page }, h(EmptyState, { title: 'Pit Crew isn’t running on this Hermes', description: 'On the machine Hermes runs on, run “hermes plugins enable pit-crew” and restart Hermes, then reload this page. (Desktop on another computer? Type /car desktop-install in a chat.)' }))
  }
  return h('div', { style: S.page },
    h(Header, { summary, onRefresh: () => { attempt('Refresh', () => api('/summary?refresh=true')).then(refresh) }, loading: q.isFetching, onGoLive: () => setGoLive(true) }),
    summary.simulated ? h(SimBanner, null) : null,
    h(Hero, { v, summary }),
    h(QuickActions, { summary, v, onPick: setRequest }),
    h(Warnings, { v, summary }),
    h('div', { style: S.grid2 }, h(Readiness, { summary, onPick: setRequest }), h(Alerts, { summary, onPick: setRequest, v })),
    h(Tiles, { v }),
    h(Commands, { summary, v, onPick: setRequest }),
    h(AuditLog, { simulated: summary.simulated }),
    h(Settings, { summary }),
    summary.simulated ? h(SimControls, { firstRun: true }) : null,
    h(SafetyDialog, { key: request ? `${request.command}:${JSON.stringify(request.args || {})}:${request.n || 0}` : 'closed', request, summary, onClose: () => setRequest(null) }),
    goLive ? h(GoLiveDialog, { summary, onClose: () => setGoLive(false) }) : null)
}

// ----------------------------------------------------------------------------- status-bar chip
// Registered ONCE as a data-form StatusbarItem with a toggleLabel, so it shows up in the status bar's right-click
// show/hide menu (a `render` contribution never can). The host renders the <button>; the label is a stable element that
// refreshes itself from the summary query (never re-register on state change: each registration adds a disposer).
// Data items get no host error boundary, so the label has its own.
class ChipGuard extends Component {
  constructor(props) { super(props); this.state = { failed: false } }
  static getDerivedStateFromError() { return { failed: true } }
  render() { return this.state.failed ? 'Car' : this.props.children }
}

function ChipLabel() {
  const q = useSummary()
  const summary = q.data
  const v = view(summary)
  if (!summary || !v.ok) {
    return h('span', { title: 'Pit Crew can’t read your car. Click to fix.', style: { opacity: 0.6 } },
      h(Codicon, { name: 'zap' }), ' Car ?')
  }
  const { bat, sec, chg, isStale } = v
  const charging = chg.charging_state === 'charging'
  const tip = `${v.st.vehicle.name}: ${num(bat.battery_pct)}%${sec.locked === false ? (v.staleAge('locked') ? `, last reported unlocked ${v.staleAge('locked')}` : ', UNLOCKED') : ''}${charging ? ', charging' : ''}${isStale ? ' (stale)' : ''} — ${liveText(summary.armed)}`
  return h('span', { title: tip, style: { display: 'inline-flex', alignItems: 'center', gap: 4, opacity: isStale ? 0.5 : 1 } },
    charging ? h(Codicon, { name: 'zap' }) : null,
    `${num(bat.battery_pct)}%`,
    sec.locked === undefined ? null : h(Codicon, { name: sec.locked ? 'lock' : 'unlock' }),
    v.st.vehicle.simulated ? h(StatusDot, { tone: 'muted' }) : null,
    summary.alerts.some(a => a.state !== 'acked' && a.severity !== 'info') ? h(StatusDot, { tone: 'warn' }) : null)
}

const CHIP_LABEL = h(ChipGuard, null, h(ChipLabel, null))   // created once: stable identity

// ----------------------------------------------------------------------------- plugin
export default {
  id: PLUGIN_ID,
  name: 'Pit Crew',
  defaultEnabled: true,
  register(ctx) {
    CTX = ctx
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => h(Page, null) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: ROUTE, label: 'Pit Crew', codicon: 'zap' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 110,
        data: { id: `${PLUGIN_ID}:chip`, toggleLabel: 'Pit Crew', title: 'Pit Crew: open the car page', to: ROUTE, variant: 'action', label: CHIP_LABEL } }
    ])
    // Nudge from the backend (state changed / alert raised); polling remains the baseline.
    ctx.onEvent(`plugin.${PLUGIN_ID}.state.changed`, () => refresh())
    // A change in Settings -> Plugins -> Pit Crew shows at once (the poll is only the baseline).
    ctx.onEvent(`plugin.${PLUGIN_ID}.settings.changed`, () => refresh())
  }
}
