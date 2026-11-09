"""Pit Crew's own history: trips, charging sessions and efficiency, recorded from ANY adapter.

Every fresh state read (the watcher, status, read-back…) is fed to :class:`Recorder`, which keeps the previous
snapshot and classifies the interval between two snapshots as *driving*, *charging* or *idle*:

* driving  — odometer advanced, the car reported moving / a speed, or the range fell faster than 15 mi/h
             (idle drain and pre-conditioning are far slower), or the zone changed with a battery drop;
* charging — the car was charging at either end of the interval, or the battery rose while plugged in.

Contiguous intervals of one kind are merged into an episode (drive / charge session) and stored in the plugin
database, together with distance (odometer > speed × time > range drop), energy (battery % × usable kWh), outside
temperature and the *zone name* (never coordinates). The result needs no TeslaMate. Cars that report an odometer
get exact distances; zone-only cars (e.g. Home Assistant ``tesla_fleet``) get range-based estimates, flagged by
``dist_src``. Records imported from TeslaMate go into the same tables (``source='teslamate'``).
"""

from __future__ import annotations

import json
import logging
import statistics
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adapters.base import BatteryHealth, ChargeRecord, DriveRecord, HistoryProvider
from .brain.models import ACTIVELY_CHARGING, VehicleState
from .store import Store

logger = logging.getLogger(__name__)

MIN_SNAPSHOT_SPACING_S = 60.0
MAX_INTERVAL_S = 6 * 3600.0        # a longer gap is a blackout, not an interval
MERGE_GAP_S = 20 * 60.0
RANGE_DROP_MI_PER_H = 15.0
SNAPSHOT_RETENTION_S = 30 * 86400.0
CHARGE_EFFICIENCY = 0.92
KV_LAST = "rec_last"
KV_FIRST = "rec_first_ts"


def _snap(state: VehicleState, now: float) -> Dict[str, Any]:
    v = state.value
    mv = state.motion() if (state.has("moving") or state.has("speed_mph") or state.has("shift_state")) else None
    return {
        "ts": now, "pct": v("battery_pct"), "range_mi": v("range_mi"), "cs": v("charging_state"),
        "plugged": v("plugged_in"), "zone": v("location_zone"), "odo": v("odometer_mi"),
        "outside_f": v("outside_temp_f"), "power_kw": v("charge_power_kw"), "speed": v("speed_mph"), "moving": mv,
    }


class Recorder:
    def __init__(self, store: Store, battery_kwh: Callable[[], float], rate_usd_kwh: Callable[[], float]) -> None:
        self._store = store
        self._kwh = battery_kwh
        self._rate = rate_usd_kwh

    # -- entry point ----------------------------------------------------------------------------
    def record(self, state: VehicleState, now: float) -> None:
        """Feed one fresh state. Never raises into the caller."""
        try:
            self._record(state, now)
        except Exception:
            logger.exception("history recorder failed; continuing without recording this snapshot")

    def _record(self, state: VehicleState, now: float) -> None:
        store = self._store
        cur = _snap(state, now)
        prev = store.kv_get(KV_LAST)
        if prev is not None and now - prev["ts"] < MIN_SNAPSHOT_SPACING_S:
            return
        # R8: remember since when the range reading has held its current value, so a sleep-drain step is spread over the
        # whole time it took instead of looking like a fast drop between two polls.
        cur["rts"] = prev.get("rts", prev["ts"]) if (prev is not None and prev.get("range_mi") == cur["range_mi"]) else now
        if store.kv_get(KV_FIRST) is None:
            store.kv_set(KV_FIRST, now)
        store.execute(
            "INSERT OR REPLACE INTO snapshots(ts, pct, range_mi, charging_state, plugged, zone, odo, outside_f, power_kw, moving)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (now, cur["pct"], cur["range_mi"], cur["cs"], None if cur["plugged"] is None else int(bool(cur["plugged"])),
             cur["zone"], cur["odo"], cur["outside_f"], cur["power_kw"], None if cur["moving"] is None else int(bool(cur["moving"]))))
        store.execute("DELETE FROM snapshots WHERE ts < ?", (now - SNAPSHOT_RETENTION_S,))
        store.kv_set(KV_LAST, cur)
        if prev is None:
            return
        dt = now - prev["ts"]
        if dt <= 0:
            return
        if dt > MAX_INTERVAL_S:
            self._close_open(prev["ts"])
            return
        kind = self._classify(prev, cur, dt)
        if kind == "charge":
            self._close_open(None, only="drives")
            self._extend_charge(prev, cur, dt)
        elif kind == "drive":
            self._close_open(None, only="charges")
            self._extend_drive(prev, cur, dt)
        else:
            self._close_open(None)

    # -- classification ---------------------------------------------------------------------------
    @staticmethod
    def _classify(prev: Dict[str, Any], cur: Dict[str, Any], dt: float) -> str:
        # A car that was charging and is no longer, with the odometer moved, was unplugged and driven in this interval.
        # (Checked first: otherwise "was charging" swallowed the whole interval and the first leg of the trip was lost;
        # for a car that jumps straight to its destination, e.g. the simulator, the entire trip.)
        if (prev["cs"] in ACTIVELY_CHARGING and cur["cs"] not in ACTIVELY_CHARGING
                and prev["odo"] is not None and cur["odo"] is not None and cur["odo"] - prev["odo"] > 0.05):
            return "drive"
        if (prev["cs"] in ACTIVELY_CHARGING or cur["cs"] in ACTIVELY_CHARGING
                or (prev["pct"] is not None and cur["pct"] is not None and cur["pct"] - prev["pct"] >= 1
                    and cur["plugged"] and prev["plugged"])):
            return "charge"
        if prev["odo"] is not None and cur["odo"] is not None and cur["odo"] - prev["odo"] > 0.05:
            return "drive"
        if cur["moving"] or prev["moving"] or (cur["speed"] or 0) > 1.0:
            return "drive"
        if prev["range_mi"] is not None and cur["range_mi"] is not None:
            drop = prev["range_mi"] - cur["range_mi"]
            window = max(dt, cur["ts"] - prev.get("rts", prev["ts"]))   # the drop may have built up since the last change
            away = any(z and z != "home" for z in (prev["zone"], cur["zone"]))
            corroborated = away or (prev["zone"] != cur["zone"])      # range alone never makes a trip at home
            if drop >= 1.0 and drop / (window / 3600.0) >= RANGE_DROP_MI_PER_H and corroborated:
                return "drive"
        if (prev["zone"] and cur["zone"] and prev["zone"] != cur["zone"] and not cur["plugged"]
                and prev["pct"] is not None and cur["pct"] is not None and cur["pct"] < prev["pct"]):
            return "drive"
        return "idle"

    # -- drives ----------------------------------------------------------------------------------------
    @staticmethod
    def _drive_miles(prev: Dict[str, Any], cur: Dict[str, Any], dt: float) -> Tuple[float, str]:
        if prev["odo"] is not None and cur["odo"] is not None and cur["odo"] >= prev["odo"]:
            return cur["odo"] - prev["odo"], "odometer"
        speeds = [x for x in (prev["speed"], cur["speed"]) if x is not None]
        if speeds and max(speeds) > 1.0:
            return sum(speeds) / len(speeds) * dt / 3600.0, "speed"
        if prev["range_mi"] is not None and cur["range_mi"] is not None and prev["range_mi"] > cur["range_mi"]:
            return prev["range_mi"] - cur["range_mi"], "range"
        return 0.0, "unknown"

    def _open_row(self, table: str) -> Optional[Dict[str, Any]]:
        rows = self._store.query(f"SELECT * FROM {table} WHERE open = 1 AND source = 'recorded' ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def _extend_drive(self, prev: Dict[str, Any], cur: Dict[str, Any], dt: float) -> None:
        miles, src = self._drive_miles(prev, cur, dt)
        # Long gaps: the car cannot have driven the whole time; assume ~35 mph.
        start = prev["ts"] if dt <= 600 or miles <= 0 else max(prev["ts"], cur["ts"] - miles / 35.0 * 3600.0)
        row = self._open_row("drives")
        if row is not None and start - row["end_ts"] > MERGE_GAP_S:
            self._close_open(None, only="drives")
            row = None
        t = cur["outside_f"]
        if row is None:
            self._store.execute(
                "INSERT INTO drives(source, start_ts, end_ts, miles, dist_src, start_pct, end_pct, temp_sum, temp_n, speed_max,"
                " start_zone, end_zone, open) VALUES('recorded',?,?,?,?,?,?,?,?,?,?,?,1)",
                (start, cur["ts"], miles, src, prev["pct"], cur["pct"], t or 0.0, 1 if t is not None else 0,
                 cur["speed"], prev["zone"], cur["zone"]))
        else:
            self._store.execute(
                "UPDATE drives SET end_ts=?, miles=miles+?, dist_src=?, end_pct=?, temp_sum=temp_sum+?, temp_n=temp_n+?,"
                " speed_max=MAX(COALESCE(speed_max,0), COALESCE(?,0)), end_zone=? WHERE id=?",
                (cur["ts"], miles, row["dist_src"] if row["dist_src"] == src or src == "unknown" else "mixed", cur["pct"],
                 t or 0.0, 1 if t is not None else 0, cur["speed"], cur["zone"], row["id"]))
        self._finalize("drives")

    # -- charging -------------------------------------------------------------------------------------------
    def _extend_charge(self, prev: Dict[str, Any], cur: Dict[str, Any], dt: float) -> None:
        powers = [x for x in (prev["power_kw"], cur["power_kw"]) if isinstance(x, (int, float))]
        used = (sum(powers) / len(powers) * dt / 3600.0) if powers else 0.0
        t = cur["outside_f"]
        row = self._open_row("charges")
        if row is not None and prev["ts"] - row["end_ts"] > MERGE_GAP_S:
            self._close_open(None, only="charges")
            row = None
        if row is None:
            self._store.execute(
                "INSERT INTO charges(source, start_ts, end_ts, start_pct, end_pct, kwh_used, temp_sum, temp_n, zone, open)"
                " VALUES('recorded',?,?,?,?,?,?,?,?,1)",
                (prev["ts"], cur["ts"], prev["pct"], cur["pct"], used or None, t or 0.0, 1 if t is not None else 0,
                 cur["zone"] or prev["zone"]))
        else:
            self._store.execute(
                "UPDATE charges SET end_ts=?, end_pct=?, kwh_used=COALESCE(kwh_used,0)+?, temp_sum=temp_sum+?, temp_n=temp_n+?"
                " WHERE id=?", (cur["ts"], cur["pct"], used, t or 0.0, 1 if t is not None else 0, row["id"]))
        self._finalize("charges")

    def _finalize(self, table: str) -> None:
        """Recompute derived energy/cost fields of the open row (cheap; keeps rows readable mid-session)."""
        row = self._open_row(table)
        if row is None:
            return
        cap = float(self._kwh())
        if table == "charges":
            added = None
            if row["start_pct"] is not None and row["end_pct"] is not None and row["end_pct"] - row["start_pct"] >= 1:
                added = (row["end_pct"] - row["start_pct"]) / 100.0 * cap
            elif row["kwh_used"]:
                added = row["kwh_used"] * CHARGE_EFFICIENCY
            rate = float(self._rate() or 0.0)
            basis = row["kwh_used"] or (added / CHARGE_EFFICIENCY if added else None)
            cost = round(basis * rate, 2) if rate > 0 and basis else None
            self._store.execute("UPDATE charges SET kwh_added=?, cost=? WHERE id=?", (added, cost, row["id"]))
        else:
            kwh = None
            if row["start_pct"] is not None and row["end_pct"] is not None and row["start_pct"] - row["end_pct"] >= 2:
                kwh = (row["start_pct"] - row["end_pct"]) / 100.0 * cap
            self._store.execute("UPDATE drives SET kwh=? WHERE id=?", (kwh, row["id"]))

    def _close_open(self, end_ts: Optional[float], only: Optional[str] = None) -> None:
        for table in ("drives", "charges"):
            if only and only != table:
                continue
            if self._open_row(table) is None:
                continue
            self._finalize(table)
            self._store.execute(f"UPDATE {table} SET open = 0 WHERE open = 1 AND source = 'recorded'")


# --------------------------------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------------------------------
class OwnHistory(HistoryProvider):
    kind = "own"
    label = "Pit Crew recorded history"

    def __init__(self, store: Store, battery_kwh: Callable[[], float], now: Callable[[], float]) -> None:
        self._store = store
        self._kwh = battery_kwh
        self._now = now

    def coverage_start(self) -> Optional[float]:
        first = self._store.kv_get(KV_FIRST)
        rows = self._store.query("SELECT MIN(start_ts) AS t FROM drives UNION ALL SELECT MIN(start_ts) FROM charges")
        stamps = [r["t"] for r in rows if r["t"]] + ([first] if first else [])
        return min(stamps) if stamps else None

    def _live_rows(self, table: str, start: float, end: float, limit: int) -> List[Dict[str, Any]]:
        # an "open" episode that has been quiet for a while is finished; show it too
        return self._store.query(
            f"SELECT * FROM {table} WHERE start_ts >= ? AND start_ts <= ? ORDER BY start_ts DESC LIMIT ?",
            (start, end, int(limit)))[::-1]

    def drives(self, start: float, end: float, limit: int = 500) -> List[DriveRecord]:
        out = []
        for r in self._live_rows("drives", start, end, limit):
            if r["open"] and self._now() - r["end_ts"] < MERGE_GAP_S:
                continue  # still in progress
            temp = r["temp_sum"] / r["temp_n"] if r["temp_n"] else None
            dur = max(0.0, (r["end_ts"] - r["start_ts"]) / 60.0)
            miles = float(r["miles"] or 0.0)
            kwh = r["kwh"]
            out.append(DriveRecord(
                start=r["start_ts"], end=r["end_ts"], miles=round(miles, 2), duration_min=round(dur, 1),
                start_pct=r["start_pct"], end_pct=r["end_pct"], kwh=kwh,
                # R9: miles estimated from the rated-range drop make Wh/mi circular (pack ÷ rated range): leave it unknown
                wh_per_mi=(round(kwh * 1000.0 / miles, 1)
                           if kwh and miles >= 1.0 and r["dist_src"] not in ("range", "mixed", "unknown") else None),
                outside_temp_f=round(temp, 1) if temp is not None else None,
                speed_avg_mph=round(miles / (dur / 60.0), 1) if dur > 0 and miles > 0 else None,
                speed_max_mph=r["speed_max"]))
        return out

    def charges(self, start: float, end: float, limit: int = 500) -> List[ChargeRecord]:
        out = []
        for r in self._live_rows("charges", start, end, limit):
            if r["open"] and self._now() - r["end_ts"] < MERGE_GAP_S:
                continue
            temp = r["temp_sum"] / r["temp_n"] if r["temp_n"] else None
            added = r["kwh_added"]
            if added is None and r["start_pct"] is not None and r["end_pct"] is not None and r["end_pct"] > r["start_pct"]:
                added = (r["end_pct"] - r["start_pct"]) / 100.0 * float(self._kwh())
            if added is None:
                continue
            out.append(ChargeRecord(
                start=r["start_ts"], end=r["end_ts"], kwh_added=round(float(added), 2),
                kwh_used=round(r["kwh_used"], 2) if r["kwh_used"] else None, cost=r["cost"],
                start_pct=r["start_pct"], end_pct=r["end_pct"],
                duration_min=round((r["end_ts"] - r["start_ts"]) / 60.0, 1),
                outside_temp_f=round(temp, 1) if temp is not None else None))
        return out

    def battery_health(self) -> Optional[BatteryHealth]:
        """Usable capacity estimated from long charge sessions (kWh added ÷ fraction of battery gained)."""
        rows = self._store.query(
            "SELECT start_pct, end_pct, kwh_used FROM charges WHERE source='recorded' AND open=0 "
            "AND end_pct - start_pct >= 25 AND kwh_used > 0 ORDER BY start_ts DESC LIMIT 12")
        est = [r["kwh_used"] * CHARGE_EFFICIENCY / ((r["end_pct"] - r["start_pct"]) / 100.0) for r in rows]
        if len(est) < 3:
            return None
        return BatteryHealth(current_capacity_kwh=round(statistics.median(est), 1))


class CombinedHistory(HistoryProvider):
    """``primary`` records, plus ``backfill`` records that END before the primary's first record
    (used so the simulated car has a past, and so an imported TeslaMate history is never duplicated)."""

    def __init__(self, primary: OwnHistory, backfill: HistoryProvider) -> None:
        self._p, self._b = primary, backfill
        self.kind = "own+" + backfill.kind
        self.label = f"{primary.label} + {backfill.label}"

    def coverage_start(self) -> Optional[float]:
        return self._p.coverage_start()

    def _cut(self) -> float:
        return self._p.coverage_start() or float("inf")

    def drives(self, start: float, end: float, limit: int = 500) -> List[DriveRecord]:
        cut = self._cut()
        old = [d for d in self._b.drives(start, min(end, cut), limit) if d.end < cut]
        return (old + self._p.drives(start, end, limit))[-limit:]

    def charges(self, start: float, end: float, limit: int = 500) -> List[ChargeRecord]:
        cut = self._cut()
        old = [c for c in self._b.charges(start, min(end, cut), limit) if c.end < cut]
        return (old + self._p.charges(start, end, limit))[-limit:]

    def battery_health(self) -> Optional[BatteryHealth]:
        return self._p.battery_health() or self._b.battery_health()


# --------------------------------------------------------------------------------------------------
# TeslaMate (or any HistoryProvider) -> own store backfill
# --------------------------------------------------------------------------------------------------
def import_history(store: Store, provider: HistoryProvider, start: float, end: float, source: str = "teslamate") -> Dict[str, int]:
    """Copy drives/charges from ``provider`` into the own store. Idempotent (unique ext_key) and never
    overwrites or duplicates something Pit Crew recorded itself (time overlap)."""
    counts = {"drives": 0, "charges": 0, "skipped_overlap": 0, "already_imported": 0}

    def overlaps(table: str, s: float, e: float) -> bool:
        rows = store.query(f"SELECT 1 FROM {table} WHERE source='recorded' AND start_ts <= ? AND end_ts >= ? LIMIT 1", (e, s))
        return bool(rows)

    for d in provider.drives(start, end, 5000):
        key = f"{source}:d:{int(d.start)}:{int(d.end)}"
        if store.query("SELECT 1 FROM drives WHERE ext_key=?", (key,)):
            counts["already_imported"] += 1
            continue
        if overlaps("drives", d.start, d.end):
            counts["skipped_overlap"] += 1
            continue
        store.execute(
            "INSERT INTO drives(source, ext_key, start_ts, end_ts, miles, dist_src, start_pct, end_pct, kwh, temp_sum, temp_n,"
            " speed_max, open) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (source, key, d.start, d.end, d.miles, source, d.start_pct, d.end_pct, d.kwh,
             d.outside_temp_f or 0.0, 1 if d.outside_temp_f is not None else 0, d.speed_max_mph))
        counts["drives"] += 1
    for c in provider.charges(start, end, 5000):
        key = f"{source}:c:{int(c.start)}:{int(c.end)}"
        if store.query("SELECT 1 FROM charges WHERE ext_key=?", (key,)):
            counts["already_imported"] += 1
            continue
        if overlaps("charges", c.start, c.end):
            counts["skipped_overlap"] += 1
            continue
        store.execute(
            "INSERT INTO charges(source, ext_key, start_ts, end_ts, start_pct, end_pct, kwh_added, kwh_used, cost, temp_sum,"
            " temp_n, open) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
            (source, key, c.start, c.end, c.start_pct, c.end_pct, c.kwh_added, c.kwh_used, c.cost,
             c.outside_temp_f or 0.0, 1 if c.outside_temp_f is not None else 0))
        counts["charges"] += 1
    return counts
