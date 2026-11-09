// Pit Crew — web dashboard page (`hermes dashboard`). A plain IIFE on window.__HERMES_PLUGIN_SDK__ (no build step).
// Read-mostly by design: it shows the car, the readiness check, alerts and the audit log, and offers the same
// low-risk actions as the Desktop page. Approval-class commands (unlock, trunk, frunk, vent, Sentry off, honk, flash)
// are PREVIEW-ONLY here: the page shows the safety checks and gives you the exact sentence to say to Hermes, whose own
// approval prompt is the only live approval (the backend refuses them from this page while armed).
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const useState = SDK.hooks.useState;
  const useEffect = SDK.hooks.useEffect;
  const useCallback = SDK.hooks.useCallback;
  const useRef = SDK.hooks.useRef;
  const C = SDK.components;
  const BASE = "/api/plugins/pit-crew";

  function api(path, body) {
    const init = body === undefined ? undefined : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    };
    return SDK.fetchJSON(BASE + path, init);
  }

  // ------------------------------------------------------------------ small helpers
  const num = (v, d) => (typeof v === "number" && isFinite(v) ? v.toFixed(d || 0) : "—");
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, " ") : "");
  function ageText(s) {
    if (s == null) return "";
    if (s < 60) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " days ago";
  }
  function when(iso) {
    const d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso || "") : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }
  const box = { border: "1px solid rgba(128,128,128,.3)", borderRadius: 10, padding: 16, display: "flex", flexDirection: "column", gap: 10 };
  const row = { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" };
  const between = { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" };
  const dim = { fontSize: 12, opacity: 0.7 };
  const grid = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 10 };
  const tile = { background: "rgba(128,128,128,.12)", borderRadius: 8, padding: "10px 12px", display: "flex", flexDirection: "column", gap: 2 };

  const CHARGE_WORDS = { complete: "Charge complete", stopped: "Plugged in, not charging", disconnected: "Not plugged in", no_power: "Plugged in, no power", charging: "Charging", starting: "Starting to charge" };

  // Commands the page may send itself (the rest go through Hermes). Mirrors the backend's risk classes.
  const EVERYDAY = [
    ["lock", "Lock"], ["close_windows", "Close windows"], ["sentry_on", "Sentry on"], ["climate_on", "Warm up (70°F)", { temp_f: 70 }],
    ["climate_off", "Climate off"], ["charge_start", "Start charging"], ["charge_stop", "Stop charging"],
  ];
  const RISKY = [
    ["unlock", "Unlock"], ["open_trunk", "Open trunk"], ["open_frunk", "Open frunk"], ["vent_windows", "Vent windows"],
    ["sentry_off", "Sentry off"], ["honk", "Honk"], ["flash", "Flash lights"],
  ];

  // Why a command does not apply right now (null = it does), from the car's last report.
  function inapplicable(id, st) {
    const sec = st.security || {}, chg = st.charging || {}, cli = st.climate || {};
    const charging = chg.charging_state === "charging" || chg.charging_state === "starting";
    const map = {
      lock: sec.locked === true && "Already locked", unlock: sec.locked === false && "Already unlocked",
      close_windows: Array.isArray(sec.windows_open) && !sec.windows_open.length && "Windows are already closed",
      open_trunk: sec.trunk_open === true && "Trunk is already open", open_frunk: sec.frunk_open === true && "Frunk is already open",
      sentry_on: sec.sentry_on === true && "Sentry is already on", sentry_off: sec.sentry_on === false && "Sentry is already off",
      climate_on: cli.climate_on === true && "Climate is already on", climate_off: cli.climate_on === false && "Climate is already off",
      charge_start: charging ? "Already charging" : chg.plugged_in === false && "Not plugged in: plug the car in first",
      charge_stop: chg.charging_state !== undefined && !charging && "Not charging right now",
    };
    return map[id] || null;
  }

  // The parts of the car's state a command can change. A result panel is only true while this is unchanged.
  function stateSig(st) {
    st = st || {};
    const sec = st.security || {}, chg = st.charging || {}, cli = st.climate || {};
    return JSON.stringify([sec.locked, sec.windows_open, sec.trunk_open, sec.frunk_open, sec.sentry_on, cli.climate_on, chg.charging_state, chg.plugged_in]);
  }
  const OUTDATED = "This result is out of date: the car’s state has changed since. Close this and look at the current state.";
  // "Nothing new" next to open alerts reads as all clear, so always say how many are still open.
  function checkSummary(raised, stillOpen) {
    const n = Number(raised) || 0, total = stillOpen + n;
    return "Checked the car: " + (n ? n + " new alert" + (n === 1 ? "" : "s") : "No new alerts") + (total ? " · " + total + (n ? " open" : " still open") : " · all clear") + ".";
  }

  function whereText(z) {
    const t = String(z || "").trim();
    if (!t || /^(away|not[_ ]home)$/i.test(t)) return "away from home";
    if (/^(unknown|unavailable)$/i.test(t)) return "location unknown";
    return "at " + t;
  }
  const SEV = { high: 0, medium: 1, low: 2, info: 3 };
  const cleanTitle = function (t) { return String(t || "").replace(/^\[Simulated\]\s*/, ""); };
  function plainSuggestion(a) {
    if (a.action && a.state !== "acked") return a.action.via_hermes ? "Hermes can do this once you approve." : "";
    const t = String(a.suggestion || "").replace(/\s*\([^)]*approval[^)]*\)/gi, "").replace(/^Want me to /i, "").replace(/\?$/, ".").trim();
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : "";
  }

  function copyText(text) {
    try { if (navigator.clipboard) return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return false; }); } catch (e) { /* fall through */ }
    return Promise.resolve(false);
  }

  // ------------------------------------------------------------------ pieces
  function Tile(props) {
    const f = props.fresh || {};
    return h("div", { style: Object.assign({}, tile, f.stale ? { borderLeft: "2px dashed rgba(128,128,128,.6)" } : {}) },
      h("span", { style: { fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em", opacity: 0.6 } }, props.label),
      h("strong", { style: { fontSize: 15 } }, props.value),
      props.sub ? h("span", { style: dim }, props.sub) : null,
      h("span", { style: Object.assign({}, dim, { fontStyle: f.stale ? "italic" : "normal" }) }, f.text || ""));
  }

  function Hero(props) {
    const st = props.st, bat = st.battery || {}, chg = st.charging || {}, sec = st.security || {}, loc = st.location || {};
    const stale = (st.freshness && st.freshness.stale_fields) || {};
    const lockAge = stale.locked != null ? ageText(stale.locked) : null;
    const newest = st.freshness && st.freshness.newest_update_age_s;
    const charging = chg.charging_state === "charging";
    const chargeLine = charging ? "Charging " + num(chg.charge_power_kw, 1) + " kW → " + num(chg.charge_limit_pct) + "%"
      : CHARGE_WORDS[chg.charging_state] || (chg.plugged_in === false ? "Not plugged in" : "");
    return h("section", { style: box },
      h("div", { style: between },
        h("div", null, h("strong", { style: { fontSize: 15 } }, st.vehicle.name),
          h("div", { style: dim }, [loc.location_zone ? whereText(loc.location_zone) : null, newest != null ? "car last reported " + ageText(newest) : null].filter(Boolean).join(" · "))),
        h("div", { style: row },
          sec.locked === undefined ? h(C.Badge, { variant: "outline" }, "Lock: unknown")
            : h(C.Badge, { variant: sec.locked ? "success" : "destructive", title: lockAge ? "Last known, " + lockAge : "Confirmed by the car" },
              (sec.locked ? "Locked" : "Unlocked") + (lockAge ? " · as of " + lockAge : "")),
          chg.plugged_in === undefined ? null : h(C.Badge, { variant: "outline" }, chg.plugged_in ? "Plugged in" : "Not plugged in"))),
      h("div", { style: Object.assign({}, row, { alignItems: "flex-end", gap: 18 }) },
        h("div", { style: { fontSize: 40, fontWeight: 600, lineHeight: 1 } }, bat.battery_pct != null ? num(bat.battery_pct) + "%" : "—"),
        h("div", { style: { flex: 1, minWidth: 220, display: "flex", flexDirection: "column", gap: 6 } },
          h("div", { style: { position: "relative", height: 8, borderRadius: 4, background: "rgba(128,128,128,.25)", overflow: "hidden" } },
            h("div", { style: { width: Math.max(0, Math.min(100, bat.battery_pct || 0)) + "%", height: "100%", background: "currentColor", opacity: 0.8 } }),
            chg.charge_limit_pct != null ? h("div", { title: "charge limit " + chg.charge_limit_pct + "%", style: { position: "absolute", top: 0, left: chg.charge_limit_pct + "%", width: 2, height: "100%", background: "currentColor" } }) : null),
          h("div", { style: between },
            h("span", { style: dim }, bat.range_mi != null ? num(bat.range_mi) + " mi of range" : "range unknown"),
            chg.charge_limit_pct != null ? h("span", { style: dim }, "▏ marks the charge limit (" + num(chg.charge_limit_pct) + "%)") : null,
            h("span", { style: dim }, chargeLine)))));
  }

  function Tiles(props) {
    const st = props.st, sec = st.security || {}, chg = st.charging || {}, cli = st.climate || {}, sw = st.software || {}, tires = st.tires || {};
    const psi = [["FL", "front_left_psi"], ["FR", "front_right_psi"], ["RL", "rear_left_psi"], ["RR", "rear_right_psi"]].filter(function (p) { return tires[p[1]] != null; });
    const lowest = psi.length ? Math.min.apply(null, psi.map(function (p) { return tires[p[1]]; })) : null;
    const fr = st.freshness || {}, stale = fr.stale_fields || {}, unavailable = fr.unavailable_fields || [];
    const warned = [["FL", "front_left_warning", "tire_warn_fl"], ["FR", "front_right_warning", "tire_warn_fr"], ["RL", "rear_left_warning", "tire_warn_rl"], ["RR", "rear_right_warning", "tire_warn_rr"]]
      .filter(function (p) { return tires[p[1]] === true && stale[p[2]] == null; }).map(function (p) { return p[0]; });
    const newest = fr.newest_update_age_s;
    function fresh(fields) {
      const f = fields.filter(function (x) { return unavailable.indexOf(x) < 0; })[0];
      if (!f) return { text: "not reported by this car" };
      if (stale[f] != null) return { text: "last known · " + ageText(stale[f]), stale: true };
      return { text: newest != null ? "confirmed " + ageText(newest) : "confirmed" };
    }
    const openList = (v, none) => (Array.isArray(v) ? (v.length ? v.length + " open" : none) : v === undefined ? "—" : v ? "Open" : none);
    const flag = (v, on, off) => (v === undefined ? "—" : v ? on : off);
    return h("div", { style: grid },
      h(Tile, { label: "Climate", value: cli.cabin_temp_f != null ? num(cli.cabin_temp_f) + "°F inside" : "—", sub: cli.outside_temp_f != null ? num(cli.outside_temp_f) + "°F outside" : null, fresh: fresh(["cabin_temp_f", "climate_on"]) }),
      h(Tile, { label: "Doors", value: openList(sec.doors_open, "Closed"), sub: Array.isArray(sec.doors_open) && sec.doors_open.length ? sec.doors_open.join(", ").replace(/_/g, " ") : null, fresh: fresh(["doors_open"]) }),
      h(Tile, { label: "Windows", value: openList(sec.windows_open, "Closed"), fresh: fresh(["windows_open"]) }),
      h(Tile, { label: "Trunk", value: flag(sec.trunk_open, "Open", "Closed"), fresh: fresh(["trunk_open"]) }),
      h(Tile, { label: "Frunk", value: flag(sec.frunk_open, "Open", "Closed"), fresh: fresh(["frunk_open"]) }),
      h(Tile, { label: "Sentry", value: flag(sec.sentry_on, "On", "Off"), fresh: fresh(["sentry_on"]) }),
      h(Tile, { label: "Charging", value: chg.charging_state ? (CHARGE_WORDS[chg.charging_state] || cap(chg.charging_state)) : "—", fresh: fresh(["charging_state"]) }),
      h(Tile, { label: "Tires", value: warned.length ? "Car warns: " + warned.join(", ") : lowest != null ? num(lowest) + " psi lowest" : "—", sub: psi.length ? psi.map(function (p) { return p[0] + " " + num(tires[p[1]]); }).join(" · ") : null, fresh: fresh(["tire_psi_fl", "tire_psi_fr", "tire_psi_rl", "tire_psi_rr", "tire_warn_fl", "tire_warn_fr", "tire_warn_rl", "tire_warn_rr"]) }),
      h(Tile, { label: "Software", value: sw.software_update_available ? "Update waiting" : "Up to date", sub: sw.software_version || null }));
  }

  function Readiness(props) {
    const defaults = props.settings || {};
    const usual = String(defaults.default_departure || "07:30");
    const m = usual.match(/^(\d{1,2}):(\d{2})$/), nowD = new Date();
    const passed = m && (nowD.getHours() * 60 + nowD.getMinutes() >= Number(m[1]) * 60 + Number(m[2]));
    const dep = useState(m && passed ? "tomorrow " + usual : usual);
    const miles = useState(String(defaults.default_trip_mi || 30));
    const round = useState(false);
    const res = useState(null);
    const check = function () {
      const qs = "?departure=" + encodeURIComponent(dep[0]) + "&trip_miles=" + encodeURIComponent(miles[0]) + "&round_trip=" + round[0];
      api("/readiness" + qs).then(res[1], function (e) { res[1]({ ok: false, error: String(e && e.message || e) }); });
    };
    const r = res[0];
    return h("section", { style: box },
      h("strong", null, "Will I be ready?"),
      h("div", { style: Object.assign({}, row, { alignItems: "flex-end" }) },
        h("label", { style: { display: "flex", flexDirection: "column", gap: 3 } }, h("span", { style: dim }, "Leaving"),
          h(C.Input, { value: dep[0], onChange: function (e) { dep[1](e.target.value); } })),
        h("label", { style: { display: "flex", flexDirection: "column", gap: 3, width: 120 } }, h("span", { style: dim }, "Miles (one way)"),
          h(C.Input, { value: miles[0], onChange: function (e) { miles[1](e.target.value); } })),
        h("label", { style: row }, h("input", { type: "checkbox", checked: round[0], onChange: function (e) { round[1](e.target.checked); } }), h("span", { style: dim }, "Round trip (doubles it)")),
        h(C.Button, { onClick: check, disabled: !!props.down }, "Check")),
      !r ? h("div", { style: dim }, "Say when you're leaving (“tomorrow 7:30 AM”, “Fri 8am”, “Nov 14 6pm”) and how far, then Check. Your usual trip is pre-filled from settings.") : null,
      r && !r.ok ? h("div", { style: { fontSize: 13 } }, (r.error || "Could not work that out.") + (r.hint ? " (" + r.hint + ")" : "")) : null,
      r && r.ok ? h("div", { style: { display: "flex", flexDirection: "column", gap: 6 } },
        h("div", { style: dim }, "Leaving " + when(r.departure) + " (in " + r.hours_until_departure + " h) · " + num(r.trip.miles) + " mi " + (r.trip.round_trip ? "round trip" : "one way")),
        h("div", { style: row }, h(C.Badge, { variant: r.verdict === "ready" ? "success" : r.verdict === "tight" ? "outline" : "destructive" }, cap(r.verdict)),
          h("span", { style: dim }, num(r.battery_now_pct) + "% now → " + num(r.projected_at_departure_pct) + "% at departure · needs ~" + num(r.needed_pct) + "%")),
        h("div", { style: { fontSize: 13 } }, r.summary),
        h("div", { style: dim }, "Assumes " + r.assumptions.efficiency + "; " + r.assumptions.safety_margin + " + " + r.assumptions.reserve_pct + "% reserve."),
        (r.recommendations || []).map(function (x, i) {
          const later = x.at && new Date(x.at).getTime() > Date.now() + 5 * 60000;
          return h("div", { key: i, style: dim }, cap(x.action) + ": " + x.why + (later ? " (say “warm the car at " + new Date(x.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) + "” in chat to schedule it)" : ""));
        })) : null);
  }

  function AskHermes(props) {
    const copied = useState(false);
    return h("div", { style: Object.assign({}, row, { gap: 6 }) },
      h("code", { style: { fontSize: 12, padding: "2px 6px", background: "rgba(128,128,128,.15)", borderRadius: 4 } }, props.text),
      h(C.Button, { variant: "outline", size: "sm", onClick: function () { copyText(props.text).then(copied[1]); } }, copied[0] ? "Copied — paste it into your Hermes chat" : "Copy"));
  }

  function Alerts(props) {
    const all = props.alerts || [];
    const open = all.filter(function (a) { return a.state !== "acked"; }).sort(function (x, y) { return (SEV[x.severity] == null ? 9 : SEV[x.severity]) - (SEV[y.severity] == null ? 9 : SEV[y.severity]) || y.id - x.id; });
    const acked = all.filter(function (a) { return a.state === "acked"; });
    const showAcked = useState(false);
    const shown = showAcked[0] ? open.concat(acked) : open;
    const note = useState("");
    const ack = function (id) { api("/alerts/ack", { id: id }).then(function () { props.reload(false); }, function (e) { note[1]("Acknowledge failed: " + (e && e.message || e)); }); };
    const fix = function (a) {
      if (!a.action || a.state === "acked") return null;
      if (a.action.via_hermes) return h(AskHermes, { text: a.action.ask });
      // The same confirmation panel as the Commands section: nothing is sent by a single click on an alert.
      return h(C.Button, { size: "sm", onClick: function () { props.onFix([a.action.command, a.action.label, {}]); } }, a.action.label);
    };
    return h("section", { style: box },
      h("div", { style: between }, h("strong", null, "Alerts" + (open.length ? " (" + open.length + ")" : "")),
        h("div", { style: row },
          open.length > 1 ? h(C.Button, { size: "sm", variant: "ghost", onClick: function () { ack("all"); } }, "Acknowledge all") : null,
          h(C.Button, { size: "sm", variant: "outline", disabled: !!props.down, onClick: function () {
            api("/alerts/check", {}).then(function (r) { note[1](r.ok ? checkSummary(r.raised, open.length) : (r.error || "Could not check the car.")); props.reload(); },
              function (e) { note[1]("Check failed: " + (e && e.message || e)); });
          } }, "Check now"))),
      note[0] ? h("div", { style: dim }, note[0]) : null,
      shown.length === 0 ? h("div", { style: dim }, props.down ? "Can’t check yet: Pit Crew isn’t connected to your car." : "All clear. Nothing needs your attention right now.")
        : shown.map(function (a) {
          return h("div", { key: a.id, style: Object.assign({}, tile, { gap: 4, opacity: a.state === "acked" ? 0.6 : 1 }) },
            h("div", { style: between }, h("strong", { style: { fontSize: 13 } }, cleanTitle(a.title)), h("span", { style: dim, title: props.simulated ? "The simulator’s clock, which Fast-forward moves. History below uses real time." : "" }, (props.simulated ? "simulated " : "") + when(a.when))),
            h("div", { style: { fontSize: 12 } }, a.body),
            h("div", { style: between }, h("span", { style: { fontSize: 12 } }, plainSuggestion(a)),
              h("div", { style: row }, fix(a), a.state === "acked" ? h("span", { style: dim }, "acknowledged") : h(C.Button, { size: "sm", variant: "ghost", onClick: function () { ack(String(a.id)); } }, "Got it"))));
        }),
      acked.length ? h(C.Button, { size: "sm", variant: "ghost", onClick: function () { showAcked[1](!showAcked[0]); } }, showAcked[0] ? "Hide acknowledged" : "Show " + acked.length + " acknowledged") : null);
  }

  const CHECK_NAMES = { temperature_range: "Temperature", charge_limit_range: "Charge limit", charge_amps_range: "Charging current", car_awake: "Car awake", state_fresh: "Fresh data", parked: "Parked", trusted_zone: "Place", plugged_in: "Plugged in" };
  const plainRefusal = function (m) { return String(m || "").split(/\. (?:If the user|Ask the user|Give Pit Crew)/)[0].replace(/ \(Only if you accept.*$/, "").replace(/[.]+$/, ""); };

  function Commands(props) {
    const st = props.st, canDo = (st.can_do || []);
    const pick = useState(null);       // {id, label, args}
    const pf = useState(null);
    const msg = useState("");
    const armed = props.armed;
    const ext = props.external;
    useEffect(function () {
      if (ext) {
        preview(ext.cmd);
        const el = document.getElementById("pit-crew-controls");
        if (el && el.scrollIntoView) el.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    }, [ext && ext.n]);
    const seq = useRef(0), inflight = useRef(false), busy = useState(false);
    const base = useState(null);                         // the car's state right after the command; a later change outdates the result
    const outdated = !!msg[0] && base[0] !== null && base[0] !== stateSig(st);
    function preview(c) {
      const mine = ++seq.current;                       // a slow answer for an earlier pick must not show under this one
      pick[1](c); pf[1](null); msg[1](""); base[1](null);
      api("/preflight", { command: c[0], args: c[2] || {} }).then(function (r) { if (seq.current === mine) pf[1](r); },
        function (e) { if (seq.current === mine) pf[1]({ ok: false, message: String(e && e.message || e) }); });
    }
    function run() {
      if (inflight.current) return Promise.resolve();   // a double click must not send twice (a ref: state lags a frame)
      inflight.current = true;
      const c = pick[0];
      busy[1](true);
      return api("/command", { command: c[0], args: c[2] || {}, confirmed: true, reason: "Pit Crew web page" })
        .then(function (r) { msg[1](r.message || (r.ok ? "Done." : "Not sent.")); return Promise.resolve(props.reload()).then(function (d) { base[1](d && d.status ? stateSig(d.status) : null); }); },
          function (e) { msg[1]("Failed: " + (e && e.message || e)); })
        .then(function () { inflight.current = false; busy[1](false); });
    }
    function goLiveAnd(next) {
      return api("/arm", { confirmed: true, hours: props.hours }).then(function (r) {
        if (r && r.ok === false) { msg[1](r.error || "Could not go live."); return; }
        props.reload(); return next();
      }, function (e) { msg[1]("Could not go live: " + (e && e.message || e)); });
    }
    function btn(c, risky) {
      const reason = canDo.indexOf(c[0]) < 0 ? "This car can’t do that" : inapplicable(c[0], st);
      return h("div", { key: c[0], title: reason || (risky ? "Previews the safety checks, then asks Hermes" : c[1]), style: { display: "flex", flexDirection: "column", gap: 2 } },
        h(C.Button, { variant: risky ? "outline" : "secondary", size: "sm", disabled: !!reason, onClick: function () { preview(c); } }, c[1]),
        reason ? h("span", { style: dim }, reason) : null);
    }
    const g = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 8, alignItems: "start" };
    const p = pick[0], q = pf[0];
    const risky = p && RISKY.some(function (r) { return r[0] === p[0]; });
    const panel = p ? h("div", { style: Object.assign({}, tile, { gap: 8 }) },
        h("strong", { style: { fontSize: 13 } }, p[1] + "?"),
        !q ? h("span", { style: dim }, "Checking the safety rules…") : h("div", { style: { display: "flex", flexDirection: "column", gap: 4 } },
          h("span", { style: { fontSize: 12 } }, q.summary || q.message),
          ((q.policy && q.policy.checks) || []).map(function (c, i) { return h("span", { key: i, style: { fontSize: 12, opacity: c.ok ? 0.75 : 1 } }, (c.ok ? "✓ " : "✗ ") + (CHECK_NAMES[c.check] || cap(c.check)) + (c.detail ? ": " + c.detail : "")); }),
          !q.ok ? h("strong", { style: { fontSize: 12 } }, "Not allowed right now: " + plainRefusal(q.message)) : null),
        risky && q && q.ok ? h(AskHermes, { text: q.ask_hermes }) : null,
        h("div", { style: row },
          !risky && q && q.ok && armed ? h(C.Button, { size: "sm", disabled: busy[0], onClick: run }, "Send command") : null,
          !risky && q && q.ok && !armed ? h(C.Button, { size: "sm", disabled: busy[0], onClick: function () { goLiveAnd(run); } }, "Go live for " + props.hours + " h & send") : null,
          q && q.ok && !armed ? h(C.Button, { size: "sm", variant: "outline", disabled: busy[0], onClick: run }, "Practice run") : null,
          h(C.Button, { size: "sm", variant: "ghost", onClick: function () { pick[1](null); pf[1](null); msg[1](""); base[1](null); } }, "Close")),
        msg[0] ? h("span", { style: { fontSize: 12 } }, outdated ? OUTDATED : msg[0]) : null) : null;
    return h("section", { id: "pit-crew-controls", style: box },
      h("div", { style: between }, h("strong", null, "Controls"), h(C.Badge, { variant: armed ? "destructive" : "outline" }, armed ? "Live" : "Practice mode · nothing is sent")),
      h("div", { style: dim }, armed ? "Every command is safety-checked, recorded, and confirmed by re-reading the car. A greyed-out button says why, right under its label."
        : "Practice mode: these buttons check the safety rules and log the attempt, but send nothing. Use Go live at the top to send for real. A greyed-out button says why, right under its label."),
      panel,
      h("div", { style: g }, EVERYDAY.map(function (c) { return btn(c, false); })),
      h("div", { style: { borderTop: "1px solid rgba(128,128,128,.3)", paddingTop: 10, display: "flex", flexDirection: "column", gap: 6 } },
        h("strong", { style: { fontSize: 12 } }, "Asked in chat, under your Hermes approval setting"),
        h("div", { style: dim }, "Unlock, trunk, frunk, vent, Sentry off, honk and flash are requested in chat, not sent by these buttons. They follow your Hermes approval setting: with approvals on (manual or smart) Hermes asks you first; with approvals off it sends without asking and its reply says so. Check the rules here, then say it to Hermes."),
        h("div", { style: g }, RISKY.map(function (c) { return btn(c, true); }))));
  }

  const WHO = { tool: "Hermes (chat)", slash: "/car", desktop: "Pit Crew page", web: "Pit Crew page", cron: "Scheduled job" };
  const OUTCOME = { confirmed: "✓ Car confirmed", sent_unconfirmed: "Sent, not confirmed yet", dry_run: "Practice run", blocked: "Refused", failed: "Failed",
    unknown_outcome: "Unclear if it was sent", rate_limited: "Too many commands", duplicate: "Skipped (repeat)" };
  const APPROVAL = { confirmed: "You approved", hermes_auto: "Approved by Hermes (no prompt)", denied: "You declined", not_required: "Not needed", missing: "Not given", unattended: "No one to ask" };

  function AuditLog(props) {
    const rows = props.rows;
    const th = { textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6, whiteSpace: "nowrap" };
    const td = { padding: "5px 8px", fontSize: 12, whiteSpace: "nowrap", borderTop: "1px solid rgba(128,128,128,.25)" };
    return h("section", { style: box },
      h("div", { style: between }, h("strong", null, "History of commands"), h("span", { style: dim }, props.simulated ? "every attempt, newest first · real clock time" : "every attempt, newest first")),
      rows == null ? h("div", { style: dim }, "Loading…") : rows.length === 0 ? h("div", { style: dim }, "Nothing yet. Commands you or Hermes try will be recorded here.")
        : h("div", { style: { overflowX: "auto" } }, h("table", { style: { borderCollapse: "collapse", width: "100%" } },
          h("thead", null, h("tr", null, ["When", "Who", "What", "Outcome", "Approval"].map(function (c) { return h("th", { key: c, style: th }, c); }))),
          h("tbody", null, rows.map(function (r) {
            if (r.command === "arm" || r.command === "disarm") {
              return h("tr", { key: r.id }, h("td", { style: td }, when(r.when)), h("td", { style: td }, WHO[r.surface] || r.surface),
                h("td", { style: Object.assign({}, td, { fontStyle: "italic" }), colSpan: 3 }, r.command === "arm" ? "Went live" : "Back to practice mode"));
            }
            return h("tr", { key: r.id, title: r.detail || "" },
              h("td", { style: td }, when(r.when)), h("td", { style: td }, WHO[r.surface] || r.surface), h("td", { style: td }, cap(r.command)),
              h("td", { style: td }, OUTCOME[r.status] || cap(r.status)), h("td", { style: td }, APPROVAL[r.approval] || "—"));
          })))));
  }

  const SIM = [["Fast-forward 30 min", "advance", { minutes: 30 }], ["Drive 20 mi away", "drive", { miles: 20, to: "away" }], ["Park at home", "park_home"],
    ["Plug in", "plug_in"], ["Unplug", "unplug"], ["Leave it unlocked", "leave_unlocked"], ["Leak a tire (6 psi/h)", "leak", { tire: "rr", psi_per_hour: 6 }], ["Pump up tires", "inflate"], ["Leave a door open", "open_door"], ["Close everything", "close_doors"],
    ["Cold snap (18°F)", "cold", { temp_f: 18 }], ["Reset", "reset"]];

  function Sim(props) {
    const note = useState("");
    const open = useState(true);
    return h("section", { style: box },
      h("div", { style: between }, h("strong", null, "Try it: simulator controls"),
        h("div", { style: row }, h(C.Badge, { variant: "outline" }, "demo only"), h(C.Button, { size: "sm", variant: "ghost", onClick: function () { open[1](!open[0]); } }, open[0] ? "Hide" : "Show"))),
      open[0] ? h("div", { style: dim }, "Make things happen to the simulated car, then watch the alerts and the readiness check react. Try: Drive away, Leave it unlocked, Fast-forward 30 min.") : null,
      open[0] ? h("div", { style: row }, SIM.map(function (s) {
        return h(C.Button, { key: s[0], size: "sm", variant: "outline", onClick: function () {
          api("/sim", { action: s[1], params: s[2] || {} }).then(function (r) { note[1](r.ok ? "Simulator: " + r.note : r.error); props.reload(); },
            function (e) { note[1]("Failed: " + (e && e.message || e)); });
        } }, s[0]);
      })) : null,
      note[0] ? h("div", { style: dim }, note[0]) : null);
  }

  // Practice mode <-> live mode, in the header. Going live asks once, inline (no native pop-up), with Cancel first.
  function ModeSwitch(props) {
    const a = props.armed, ask = useState(false), note = useState("");
    const go = function (path, body) {
      api(path, body).then(function (r) {
        if (r && r.ok === false) { note[1](r.error || "Not done."); return; }       // e.g. a bad hours value: keep the prompt open
        note[1](""); ask[1](false); props.reload();
      }, function (e) { note[1]("Failed: " + (e && e.message || e)); });
    };
    return h("div", { style: row },
      h(C.Badge, { variant: a.armed ? "destructive" : "outline" }, a.armed ? "LIVE" + (a.until_local ? " until " + new Date(a.until_local).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "") : "Practice mode · nothing is sent"),
      a.armed ? h(C.Button, { size: "sm", variant: "ghost", onClick: function () { go("/disarm", {}); } }, "Stop")
        : ask[0] ? h("span", { style: row },
          h("span", { style: { fontSize: 12 } }, props.simulated ? "Go live for " + props.hours + " h? (simulated car)" : "Go live for " + props.hours + " h? Hermes can then send commands; unlock and similar follow your Hermes approval setting."),
          h(C.Button, { size: "sm", variant: "secondary", autoFocus: true, onClick: function () { ask[1](false); } }, "Cancel"),
          h(C.Button, { size: "sm", variant: "destructive", onClick: function () { go("/arm", { confirmed: true, hours: props.hours }); } }, "Go live"))
          : h(C.Button, { size: "sm", variant: "outline", onClick: function () { ask[1](true); } }, "Go live…"),
      note[0] ? h("span", { style: dim }, note[0]) : null);
  }

  function Connect(props) {
    const c = props.connection || {};
    return h("section", { style: box },
      h("strong", null, "Connect your car"),
      h("div", { style: { fontSize: 13 } }, c.message || "Pit Crew isn’t connected to a car yet."),
      c.hint ? h("div", { style: dim }, c.hint) : null,
      h("div", { style: dim }, "Settings live under plugins.entries.pit-crew.settings in config.yaml (run `hermes config edit`); tokens go in the profile .env (`hermes config env-path`). Hermes Desktop has a form under Settings → Plugins → Pit Crew. To try Pit Crew first, set adapter: simulator."));
  }

  // ------------------------------------------------------------------ page
  function PitCrewPage() {
    const sum = useState(null), err = useState(null), log = useState(null), got = useState(null);
    const load = useCallback(function (refresh) {
      api("/log?limit=25").then(function (d) { log[1](d.rows || []); }, function () { /* the table keeps its last rows */ });
      return api("/summary" + (refresh ? "?refresh=true" : "")).then(function (d) { sum[1](d); err[1](null); got[1](Date.now()); return d; }, function (e) { err[1](String(e && e.message || e)); return null; });
    }, []);
    useEffect(function () { load(false); const t = setInterval(function () { if (!document.hidden) load(false); }, 15000);   // no polling while the tab is hidden
       return function () { clearInterval(t); }; }, [load]);
    const s = sum[0];
    const ext = useState(null);
    if (err[0] && !s) {
      return h("div", { style: { padding: 24, display: "flex", flexDirection: "column", gap: 8 } }, h("strong", null, "Pit Crew isn’t running on this Hermes"),
        h("span", { style: dim }, "On the machine Hermes runs on, run `hermes plugins enable pit-crew` and restart Hermes, then reload. Details: " + err[0]));
    }
    if (!s) return h("div", { style: { padding: 24 } }, "Loading Pit Crew…");
    const st = s.status || {}, armed = s.armed || {};
    const hours = armed.default_hours || 1;
    const down = !st.ok;
    return h("div", { style: { display: "flex", flexDirection: "column", gap: 16, maxWidth: 1040, margin: "0 auto", padding: 20 } },
      err[0] ? h("section", { style: Object.assign({}, box, { borderLeft: "3px solid rgba(220,80,60,.9)" }) },
        h("strong", null, "Can’t reach Pit Crew right now"),
        h("span", { style: dim }, "What you see below is the last data it sent" + (got[0] ? " (" + new Date(got[0]).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) + ")" : "") + " and may be out of date. Commands may not work. " + err[0])) : null,
      h("div", { style: between },
        h("div", { style: row }, h("h1", { style: { fontSize: 18, fontWeight: 600, margin: 0 } }, "Pit Crew"),
          s.simulated ? h(C.Badge, { variant: "outline" }, "Simulated car") : null,
          h(ModeSwitch, { armed: armed, hours: hours, simulated: s.simulated, reload: load })),
        h(C.Button, { variant: "ghost", size: "sm", onClick: function () { load(true); } }, "Refresh")),
      s.simulated ? h("section", { style: Object.assign({}, box, { flexDirection: "row", justifyContent: "space-between", alignItems: "center" }) },
        h("span", { style: { fontSize: 13 } }, h("strong", null, "This is a simulated car. "), "The simulator pretends to be a car: it reports state, accepts commands and raises alerts, but no real car is touched. Try “Drive 20 mi away” then “Leave it unlocked” below and watch the alert appear.")) : null,
      down ? h(Connect, { connection: s.connection })
        : h(React.Fragment, null, h(Hero, { st: st }),
          (st.warnings || []).length ? h("section", { style: box }, h("strong", null, "Heads up"), Array.from(new Set(st.warnings)).map(function (w, i) { return h("div", { key: i, style: { fontSize: 13 } }, w); })) : null),
      h(Readiness, { settings: s.settings, down: down }),
      h(Alerts, { alerts: s.alerts, simulated: !!s.simulated, reload: load, down: down, onFix: function (cmd) { ext[1]({ cmd: cmd, n: Date.now() }); } }),
      down ? null : h(Tiles, { st: st }),
      down ? h("section", { style: box }, h("strong", null, "Controls"), h("span", { style: dim }, "Commands are unavailable until Pit Crew can read your car."))
        : h(Commands, { st: st, armed: !!armed.armed, hours: hours, reload: load, external: ext[0] }),
      h(AuditLog, { rows: log[0], simulated: !!s.simulated }),
      s.simulated ? h(Sim, { reload: load }) : null,
      h("div", { style: dim }, "Settings: Hermes Desktop has a form under Settings → Plugins → Pit Crew; otherwise edit plugins.entries.pit-crew.settings in config.yaml (`hermes config edit`). Changes apply immediately."));
  }

  window.__HERMES_PLUGINS__.register("pit-crew", PitCrewPage);
})();
