"""Rule engine: evaluates the rules against a state, handles hold times, de-duplication, cooldowns,
acknowledgement and auto-resolution, and stores alerts."""

from __future__ import annotations

import logging
import math
import threading
from typing import Any, Dict, List, Optional

from ..brain import tires
from ..brain.models import VehicleState
from .rules import RULE_LABELS, RULE_MINIMUMS, RULES, RULES_BY_ID, Finding, Rule, RuleContext

logger = logging.getLogger(__name__)

OPEN_STATES = ("new", "surfaced", "acked")
#: a charge_stop / charge_limit Pit Crew sent within this long explains a "charging stopped" reading
RECENT_COMMAND_S = 1800.0
OPENING_FIELDS = ("doors_open", "windows_open", "trunk_open", "frunk_open")


class RuleEngine:
    def __init__(self, service: Any) -> None:
        self._svc = service
        self._eval_lock = threading.Lock()      # a forced poll and the watcher's poll must not both raise the same alert

    # -- configuration --------------------------------------------------------------------
    def rule_config(self, rule: Rule) -> Dict[str, Any]:
        """Defaults < the plugin's ``rules`` setting (the one source of truth: Settings, the page and the car_rules tool all
        read and write it). Edits made by older versions sat in the plugin DB (``rule_overrides``); they are moved into the
        setting once, and until that succeeds they keep applying so nothing is lost."""
        self._migrate_overrides()
        cfg = self._svc.settings.rule_config(rule.id, rule.defaults)
        over = (self._svc.store.kv_get("rule_overrides", {}) or {}).get(rule.id) or {}
        for k, v in over.items():
            if k in rule.defaults:
                cfg[k] = v
        for k, floor in RULE_MINIMUMS.items():     # also guards values typed into the config file, not just set_rule
            if k in cfg and isinstance(cfg[k], (int, float)) and not isinstance(cfg[k], bool) and cfg[k] < floor:
                cfg[k] = floor
        return cfg

    def set_rule(self, rule_id: str, **changes: Any) -> Dict[str, Any]:
        rule = RULES_BY_ID.get(rule_id)
        if rule is None:
            raise KeyError(rule_id)
        clean: Dict[str, Any] = {}
        for k, v in changes.items():
            if k not in rule.defaults:
                raise ValueError(f"'{k}' is not a setting of rule '{rule_id}' (options: {', '.join(rule.defaults)})")
            default = rule.defaults[k]
            if isinstance(default, bool):
                clean[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            else:
                try:
                    num = float(v)
                except (TypeError, ValueError):
                    raise ValueError(f"'{k}' must be a number") from None
                if not math.isfinite(num) or num < 0 or num > 1_000_000:
                    raise ValueError(f"'{k}' must be a sensible positive number")
                floor = RULE_MINIMUMS.get(k)
                if floor is not None and num < floor:
                    raise ValueError(f"'{k}' must be at least {floor:g}")
                if k == "below_pct" and num > 100:
                    raise ValueError("'below_pct' must be at most 100")
                if k == "min_psi" and num != 0 and not 5 <= num <= 120:
                    raise ValueError("'min_psi' must be 0 (automatic) or between 5 and 120")
                clean[k] = int(num) if float(num).is_integer() else num
        self._migrate_overrides()
        self._svc.settings.save_rule(rule_id, clean, ctx=self._svc.ctx)
        return self.rule_config(rule)

    def _migrate_overrides(self) -> None:
        """One-time move of the old DB-held ``rule_overrides`` into the ``rules`` setting, then drop them."""
        store = self._svc.store
        old = store.kv_get("rule_overrides", None)
        if not old:
            if old is not None:
                store.kv_delete("rule_overrides")
            return
        try:
            for rule_id, values in old.items():
                rule = RULES_BY_ID.get(rule_id)
                if rule is None or not isinstance(values, dict):
                    continue
                keep = {k: v for k, v in values.items() if k in rule.defaults}
                if keep:
                    self._svc.settings.save_rule(rule_id, keep, ctx=self._svc.ctx)
            store.kv_delete("rule_overrides")
        except Exception:      # noqa: BLE001 - e.g. a managed install: keep honouring the old overrides, try again later
            logger.warning("could not move old alert-rule edits into the plugin setting; keeping them for now", exc_info=True)

    def describe_rules(self, state: Optional[VehicleState] = None) -> List[Dict[str, Any]]:
        out = []
        for rule in RULES:
            cfg = self.rule_config(rule)
            item: Dict[str, Any] = {"id": rule.id, "title": rule.title, "description": rule.description,
                                    "enabled": bool(cfg["enabled"]),
                                    "settings": {k: v for k, v in cfg.items() if k != "enabled"},
                                    "labels": {k: RULE_LABELS.get(k, k.replace("_", " ")) for k in cfg if k != "enabled"}}
            active = [k for k in self._svc.store.rule_state_keys(rule.id)]
            item["active_conditions"] = len(active)
            if state is not None:
                missing = [f for f in rule.requires if not state.is_available(f)]
                if missing:
                    item["note"] = f"cannot run: this car doesn't report {', '.join(missing)}"
                elif rule.requires_any and not any(state.has(f) for f in rule.requires_any):
                    what = "tire warnings" if rule.id == "tire_car_warning" else "tire pressure"
                    item["note"] = f"cannot run: this car doesn't report {what}" + self._tire_enable_hint(rule.requires_any)
            out.append(item)
        return out

    def _tire_enable_hint(self, fields: Any = ("tire_psi_fl",)) -> str:
        try:
            fn = getattr(self._svc.adapter, "disabled_entity_hint", None)
            text = fn(tuple(fields)) if callable(fn) else ""
        except Exception:  # noqa: BLE001
            text = ""
        return f" ({text})" if text else ""

    # -- evaluation -----------------------------------------------------------------------
    def evaluate(self, state: VehicleState, now: float) -> List[Dict[str, Any]]:
        """Run all enabled rules; return the alerts newly raised by this evaluation."""
        with self._eval_lock:
            return self._evaluate(state, now)

    def _evaluate(self, state: VehicleState, now: float) -> List[Dict[str, Any]]:
        svc = self._svc
        store = svc.store
        settings = svc.settings.snapshot()
        can_do = svc.capabilities().commands
        local = svc.local_dt(now)
        raised: List[Dict[str, Any]] = []
        last_moving = store.kv_get("last_moving_ts")
        if state.motion() is True:
            last_moving = now
            store.kv_set("last_moving_ts", now)
        for rule in RULES:
            cfg = self.rule_config(rule)
            if not cfg["enabled"]:
                self._clear_all(rule)
                continue
            if any(not state.is_available(f) for f in rule.requires):
                continue  # honest skip: the car doesn't report what this rule needs
            if rule.requires_any and not any(state.has(f) for f in rule.requires_any):
                continue
            # R16: a day-old reading must not raise a fresh alert. Skip (keep existing state) until it is refreshed.
            inputs = rule.requires or tuple(f for f in rule.requires_any if state.has(f)) or OPENING_FIELDS
            stale_after = float(settings.get("stale_after_s") or 900)
            stale = [f for f in inputs if (a := state.age_s(f, now)) is not None and a > stale_after]
            if rule.requires_any and not rule.requires:
                # per-input freshness (one sleeping tire sensor must not silence the other three); the rule itself ignores
                # stale inputs, and alerts that name a stale input are kept as they are (see _apply)
                if len(stale) == len(inputs):
                    continue
            elif stale:
                continue
            memo = store.kv_get(f"rule_memo:{rule.id}", {}) or {}
            ctx = RuleContext(state=state, now=now, local=local, cfg=cfg, settings=settings, can_do=can_do, memo=memo,
                              last_moving_ts=last_moving,
                              recent_commands=(store.live_commands_since(svc._wall() - RECENT_COMMAND_S)
                                               if rule.id == "charging_stopped_unexpectedly" else None),
                              readiness=(lambda dep, mi, _s=svc: _s.readiness(dep, mi)) if rule.id == "soc_below_tomorrow" else None)
            try:
                findings = rule.check(ctx)
            except Exception:
                logger.exception("rule %s crashed; skipping it this round", rule.id)
                continue
            store.kv_set(f"rule_memo:{rule.id}", memo)
            raised.extend(self._apply(rule, cfg, findings, now, int(settings["alert_cooldown_min"]),
                                      frozen=set(stale) if rule.requires_any else frozenset()))
        return raised

    def _apply(self, rule: Rule, cfg: Dict[str, Any], findings: List[Finding], now: float,
               default_cooldown_min: int, frozen: Any = frozenset()) -> List[Dict[str, Any]]:
        store = self._svc.store
        raised: List[Dict[str, Any]] = []
        active = {f.key for f in findings}
        cooldown_s = float(cfg.get("cooldown_min", default_cooldown_min)) * 60.0
        for f in findings:
            row = store.rule_state_begin(rule.id, f.key, now)
            if now - row["first_seen"] < f.hold_s:
                continue
            open_alerts = [a for a in store.alerts_list(OPEN_STATES, limit=200)
                           if a["rule"] == rule.id and a["dedupe_key"] == f.key]
            if any(a["state"] == "acked" for a in open_alerts):
                continue  # the human acknowledged it; stay quiet until the condition clears
            if row["last_fired"] is not None and now - row["last_fired"] < cooldown_s:
                continue
            title = ("[Simulated] " if getattr(self._svc.adapter, "simulated", False) else "") + f.title
            aid = store.alert_add(ts=now, rule=rule.id, dedupe_key=f.key, severity=f.severity, title=title,
                                  body=f.body, suggestion=f.suggestion, data=f.data)
            store.rule_state_fired(rule.id, f.key, now)
            raised.append({"id": aid, "rule": rule.id, "key": f.key, "severity": f.severity, "title": title,
                           "body": f.body, "suggestion": f.suggestion})
        for key in store.rule_state_keys(rule.id):
            if key not in active:
                if frozen and frozen.intersection(tires.key_tires(key)):
                    continue        # R16: a tire that went quiet neither confirms nor clears its alert
                store.rule_state_clear(rule.id, key)
                store.alerts_resolve_key(rule.id, key)
        return raised

    def _clear_all(self, rule: Rule) -> None:
        store = self._svc.store
        for key in store.rule_state_keys(rule.id):
            store.rule_state_clear(rule.id, key)
            store.alerts_resolve_key(rule.id, key)
