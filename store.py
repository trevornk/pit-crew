"""SQLite persistence for audit log, alerts, rule state and small key/value state.

Lives in the plugin's data directory (``<HERMES_HOME>/plugin-data/pit-crew/data.db`` via
Hermes' ``plugin_db``) so it survives plugin updates and is shared between the agent process
and the Desktop dashboard backend.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .constants import DB_FILENAME, PLUGIN_ID

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    surface TEXT NOT NULL,
    session TEXT,
    command TEXT NOT NULL,
    args TEXT NOT NULL,
    risk TEXT NOT NULL,
    mode TEXT NOT NULL,            -- dry_run | live | blocked | control
    policy TEXT,                   -- allowed | denied | n/a
    approval TEXT,                 -- not_required | confirmed | missing | denied | unattended
    status TEXT NOT NULL,          -- dry_run | confirmed | sent_unconfirmed | blocked | failed | duplicate | rate_limited | invalid | unsupported
    detail TEXT,
    readback TEXT,                 -- confirmed | not_confirmed | unverifiable | n/a
    readback_ts REAL,
    adapter TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    rule TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    suggestion TEXT,
    state TEXT NOT NULL DEFAULT 'new',   -- new | surfaced | acked | resolved
    delivered_ts REAL,
    acked_ts REAL,
    resolved_ts REAL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS alerts_state ON alerts(state);

CREATE TABLE IF NOT EXISTS rule_state (
    rule TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_fired REAL,
    fire_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (rule, dedupe_key)
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    ts REAL PRIMARY KEY,
    pct REAL, range_mi REAL, charging_state TEXT, plugged INTEGER, zone TEXT,
    odo REAL, outside_f REAL, power_kw REAL, moving INTEGER
);

CREATE TABLE IF NOT EXISTS drives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'recorded',     -- recorded | teslamate
    ext_key TEXT UNIQUE,
    start_ts REAL NOT NULL, end_ts REAL NOT NULL,
    miles REAL NOT NULL DEFAULT 0, dist_src TEXT,
    start_pct REAL, end_pct REAL, kwh REAL,
    temp_sum REAL NOT NULL DEFAULT 0, temp_n INTEGER NOT NULL DEFAULT 0,
    speed_max REAL, start_zone TEXT, end_zone TEXT,
    open INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS drives_start ON drives(start_ts);

CREATE TABLE IF NOT EXISTS charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'recorded',
    ext_key TEXT UNIQUE,
    start_ts REAL NOT NULL, end_ts REAL NOT NULL,
    start_pct REAL, end_pct REAL,
    kwh_added REAL, kwh_used REAL, cost REAL,
    temp_sum REAL NOT NULL DEFAULT 0, temp_n INTEGER NOT NULL DEFAULT 0,
    zone TEXT, open INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS charges_start ON charges(start_ts);

CREATE TABLE IF NOT EXISTS lease (
    name TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    expires REAL NOT NULL
);
"""


class Store:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_alerts()
            self._conn.commit()

    #: push bookkeeping, kept apart from ``state`` (new/surfaced/acked/resolved): showing an alert in some chat's next-turn
    #: context ("surfaced") must not cancel the push to the bound chat
    _ALERT_PUSH_COLUMNS = (("pushed_ts", "REAL"), ("push_attempts", "INTEGER NOT NULL DEFAULT 0"),
                           ("next_push_ts", "REAL"), ("push_claim_ts", "REAL"), ("push_error", "TEXT"))

    def _migrate_alerts(self) -> None:
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(alerts)").fetchall()}
        for col, decl in self._ALERT_PUSH_COLUMNS:
            if col not in have:
                self._conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {decl}")
        # the old claim state: an alert claimed by a pusher that never finished is simply unpushed again
        self._conn.execute("UPDATE alerts SET state='new', delivered_ts=NULL WHERE state='pushing'")

    # -- plumbing -------------------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- key/value ------------------------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv WHERE key = ?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except ValueError:
            logger.warning("kv value for %r is not valid JSON; ignoring", key)
            return default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key, value, updated) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, json.dumps(value, default=str), time.time()),
        )

    def kv_delete(self, key: str) -> None:
        self.execute("DELETE FROM kv WHERE key = ?", (key,))

    # -- audit ----------------------------------------------------------------------------
    def audit_add(self, *, ts: float, surface: str, session: str, command: str, args: Dict[str, Any],
                  risk: str, mode: str, policy: str, approval: str, status: str, detail: str,
                  readback: str, readback_ts: Optional[float], adapter: str) -> int:
        cur = self.execute(
            "INSERT INTO audit(ts, surface, session, command, args, risk, mode, policy, approval, status,"
            " detail, readback, readback_ts, adapter) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, surface, session or "", command, json.dumps(args, sort_keys=True, default=str), risk, mode,
             policy, approval, status, detail, readback, readback_ts, adapter),
        )
        return int(cur.lastrowid or 0)

    def audit_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),))
        for r in rows:
            try:
                r["args"] = json.loads(r["args"])
            except ValueError:
                logger.warning("audit row %s has unreadable args; returning them raw", r.get("id"))
        return rows

    def reserve_live(self, *, ts: float, surface: str, session: str, command: str, args: Dict[str, Any], risk: str,
                     approval: str, adapter: str, rate_window_s: float, rate_max: int, dedupe_window_s: float,
                     canon: Any) -> Dict[str, Any]:
        """Atomically check the rate limit + duplicate window and, if clear, insert a ``sending`` audit row.

        ``BEGIN IMMEDIATE`` takes the write lock first, so the dashboard process and the gateway (separate processes
        on one DB) cannot both pass the checks. Returns ``{"verdict": "ok", "id": n}``, ``{"verdict": "rate"}`` or
        ``{"verdict": "dup", "age_s": n}``. The caller finishes the row with :meth:`audit_finish`."""
        with self._lock:
            if self._conn.in_transaction:
                self._conn.commit()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = [dict(r) for r in self._conn.execute(
                    "SELECT * FROM audit WHERE ts >= ? AND mode = 'live' AND status IN "
                    "('confirmed','sent_unconfirmed','failed','unknown_outcome','sending') ORDER BY ts DESC",
                    (ts - rate_window_s,)).fetchall()]
                if len(rows) >= rate_max:
                    self._conn.rollback()
                    return {"verdict": "rate"}
                want = canon(args)
                for r in rows:
                    try:
                        ra = json.loads(r["args"])
                    except ValueError:
                        ra = {}
                    if r["command"] == command and canon(ra) == want and ts - r["ts"] <= dedupe_window_s:
                        self._conn.rollback()
                        return {"verdict": "dup", "age_s": int(ts - r["ts"])}
                cur = self._conn.execute(
                    "INSERT INTO audit(ts, surface, session, command, args, risk, mode, policy, approval, status,"
                    " detail, readback, readback_ts, adapter) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, surface, session or "", command, json.dumps(args, sort_keys=True, default=str), risk, "live",
                     "allowed", approval, "sending", "", "n/a", None, adapter))
                self._conn.commit()
                return {"verdict": "ok", "id": int(cur.lastrowid or 0)}
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def audit_finish(self, audit_id: int, *, status: str, detail: str, readback: str = "n/a",
                     readback_ts: Optional[float] = None) -> None:
        self.execute("UPDATE audit SET status=?, detail=?, readback=?, readback_ts=? WHERE id=?",
                     (status, detail, readback, readback_ts, int(audit_id)))

    def live_commands_since(self, since: float) -> List[Dict[str, Any]]:
        """Live commands that were actually attempted (sent, confirmed or failed) since ``since``."""
        rows = self.query(
            "SELECT * FROM audit WHERE ts >= ? AND mode = 'live' AND status IN "
            "('confirmed','sent_unconfirmed','failed','unknown_outcome','sending') ORDER BY ts DESC", (since,))
        for r in rows:
            try:
                r["args"] = json.loads(r["args"])
            except ValueError:
                r["args"] = {}
        return rows

    # -- alerts ---------------------------------------------------------------------------
    def alert_add(self, *, ts: float, rule: str, dedupe_key: str, severity: str, title: str, body: str,
                  suggestion: str, data: Optional[Dict[str, Any]] = None) -> int:
        cur = self.execute(
            "INSERT INTO alerts(ts, rule, dedupe_key, severity, title, body, suggestion, data)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ts, rule, dedupe_key, severity, title, body, suggestion, json.dumps(data or {}, default=str)),
        )
        return int(cur.lastrowid or 0)

    def alerts_list(self, states: Optional[Iterable[str]] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if states:
            states = list(states)
            marks = ",".join("?" for _ in states)
            rows = self.query(f"SELECT * FROM alerts WHERE state IN ({marks}) ORDER BY id DESC LIMIT ?",
                              [*states, int(limit)])
        else:
            rows = self.query("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (int(limit),))
        for r in rows:
            try:
                r["data"] = json.loads(r["data"] or "{}")
            except ValueError:
                r["data"] = {}
        return rows

    def alert_set_state(self, alert_id: int, state: str) -> bool:
        col = {"surfaced": "delivered_ts", "acked": "acked_ts", "resolved": "resolved_ts"}.get(state)
        if col:
            cur = self.execute(f"UPDATE alerts SET state = ?, {col} = ? WHERE id = ?", (state, time.time(), alert_id))
        else:
            cur = self.execute("UPDATE alerts SET state = ? WHERE id = ?", (state, alert_id))
        return cur.rowcount > 0

    def alert_transition(self, alert_id: int, new_state: str, only_from: Tuple[str, ...]) -> bool:
        """Move an alert to ``new_state`` only if it is still in one of ``only_from`` (so a slow push can't resurrect
        an alert that was resolved, or acknowledged by the human, while it was being delivered)."""
        col = {"surfaced": "delivered_ts", "acked": "acked_ts", "resolved": "resolved_ts"}.get(new_state)
        marks = ",".join("?" for _ in only_from)
        sets = f"state = ?, {col} = ?" if col else "state = ?"
        params: list = [new_state] + ([time.time()] if col else []) + [alert_id, *only_from]
        cur = self.execute(f"UPDATE alerts SET {sets} WHERE id = ? AND state IN ({marks})", tuple(params))
        return cur.rowcount > 0

    def alerts_for_push(self, now: float, giveup_s: float, limit: int = 20) -> List[Dict[str, Any]]:
        """Alerts still owed a push: not pushed yet, not acknowledged or resolved (``new`` or ``surfaced``), not ``info``,
        newer than ``giveup_s``, past their retry time and not claimed by another pusher right now."""
        rows = self.query(
            "SELECT * FROM alerts WHERE pushed_ts IS NULL AND state IN ('new','surfaced') AND severity != 'info' "
            "AND ts >= ? AND COALESCE(next_push_ts,0) <= ? AND COALESCE(push_claim_ts,0) < ? ORDER BY id DESC LIMIT ?",
            (now - giveup_s, now, now - 300.0, int(limit)))
        for r in rows:
            try:
                r["data"] = json.loads(r["data"] or "{}")
            except ValueError:
                r["data"] = {}
        return rows

    def alerts_push_summary(self, now: float, giveup_s: float) -> Dict[str, int]:
        """Counts for status: waiting for a push (with retries pending) and given up (older than the window, never pushed)."""
        rows = self.query(
            "SELECT SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS waiting, SUM(CASE WHEN ts < ? THEN 1 ELSE 0 END) AS gave_up "
            "FROM alerts WHERE pushed_ts IS NULL AND state IN ('new','surfaced') AND severity != 'info'",
            (now - giveup_s, now - giveup_s))
        r = rows[0] if rows else {}
        return {"waiting": int(r.get("waiting") or 0), "gave_up": int(r.get("gave_up") or 0)}

    def alert_claim_push(self, alert_id: int, now: float, stale_s: float = 300.0) -> bool:
        """Atomically take an alert for pushing. Exactly one caller wins; a claim older than ``stale_s`` (a crashed pusher)
        can be taken over. The alert's ``state`` is not touched."""
        cur = self.execute(
            "UPDATE alerts SET push_claim_ts=? WHERE id=? AND pushed_ts IS NULL AND state IN ('new','surfaced') "
            "AND COALESCE(push_claim_ts,0) < ?", (now, alert_id, now - stale_s))
        return cur.rowcount == 1

    def alert_push_done(self, alert_id: int, now: float) -> None:
        """Delivered to the bound chat. A still-``new`` alert becomes ``surfaced``; acked/resolved ones stay as they are."""
        self.execute("UPDATE alerts SET pushed_ts=?, push_claim_ts=NULL, push_error=NULL, push_attempts=push_attempts+1 "
                     "WHERE id=?", (now, alert_id))
        self.alert_transition(alert_id, "surfaced", ("new",))

    def alert_push_failed(self, alert_id: int, next_ts: float, error: str = "") -> None:
        """The push did not go through: release the claim and try again at ``next_ts``. State stays as it was."""
        self.execute("UPDATE alerts SET push_claim_ts=NULL, next_push_ts=?, push_error=?, push_attempts=push_attempts+1 "
                     "WHERE id=?", (next_ts, (error or "")[:300], alert_id))

    def alerts_resolve_key(self, rule: str, dedupe_key: str) -> int:
        cur = self.execute(
            "UPDATE alerts SET state='resolved', resolved_ts=? WHERE rule=? AND dedupe_key=? "
            "AND state IN ('new','pushing','surfaced','acked')", (time.time(), rule, dedupe_key))
        return cur.rowcount

    # -- rule state -----------------------------------------------------------------------
    def rule_state_get(self, rule: str, key: str) -> Optional[Dict[str, Any]]:
        rows = self.query("SELECT * FROM rule_state WHERE rule=? AND dedupe_key=?", (rule, key))
        return rows[0] if rows else None

    def rule_state_begin(self, rule: str, key: str, now: float) -> Dict[str, Any]:
        """Record that a rule condition is currently true (idempotent). Returns the row."""
        self.execute("INSERT OR IGNORE INTO rule_state(rule, dedupe_key, first_seen) VALUES(?,?,?)",
                     (rule, key, now))
        row = self.rule_state_get(rule, key)
        assert row is not None
        return row

    def rule_state_fired(self, rule: str, key: str, now: float) -> None:
        self.execute("UPDATE rule_state SET last_fired=?, fire_count=fire_count+1 WHERE rule=? AND dedupe_key=?",
                     (now, rule, key))

    def rule_state_clear(self, rule: str, key: Optional[str] = None) -> None:
        if key is None:
            self.execute("DELETE FROM rule_state WHERE rule=?", (rule,))
        else:
            self.execute("DELETE FROM rule_state WHERE rule=? AND dedupe_key=?", (rule, key))

    def rule_state_keys(self, rule: str) -> List[str]:
        return [r["dedupe_key"] for r in self.query("SELECT dedupe_key FROM rule_state WHERE rule=?", (rule,))]

    # -- leases (one watcher per data dir) -------------------------------------------------
    def lease_acquire(self, name: str, holder: str, ttl_s: float, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            # R6: BEGIN IMMEDIATE takes the write lock before the read, so two processes cannot both see "free".
            if self._conn.in_transaction:
                self._conn.commit()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT holder, expires FROM lease WHERE name=?", (name,)).fetchone()
                if row is not None and row["holder"] != holder and row["expires"] > now:
                    self._conn.rollback()
                    return False
                self._conn.execute(
                    "INSERT INTO lease(name, holder, expires) VALUES(?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET holder=excluded.holder, expires=excluded.expires",
                    (name, holder, now + ttl_s))
                self._conn.commit()
                return True
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def lease_release(self, name: str, holder: str) -> None:
        self.execute("DELETE FROM lease WHERE name=? AND holder=?", (name, holder))


def _harden_permissions(conn: sqlite3.Connection) -> None:
    """Best effort: make the DB (and its -wal/-shm files) owner-only; it holds the audit log and location history."""
    try:
        import os

        row = conn.execute("PRAGMA database_list").fetchone()
        base = row[2] if row else ""
        if not base:
            return
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(base + suffix):
                os.chmod(base + suffix, 0o600)
    except Exception:  # noqa: BLE001 - never block start-up on this
        logger.debug("could not tighten database file permissions", exc_info=True)


def open_store(path: Optional[Path] = None) -> Store:
    """Open the store at ``path``; with no path use Hermes' per-plugin data dir."""
    if path is not None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:  # network filesystems etc.: fall back silently to default journal
            logger.debug("WAL unavailable for %s", path)
        store = Store(conn)
        _harden_permissions(conn)
        return store
    from plugins.plugin_storage import plugin_db  # documented public Hermes helper

    conn = plugin_db(PLUGIN_ID, DB_FILENAME)
    store = Store(conn)
    _harden_permissions(conn)
    return store
