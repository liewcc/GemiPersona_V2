import os
import json
import time
import sqlite3
import logging

logger = logging.getLogger('conductor')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "health.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    account TEXT,
    event_type TEXT NOT NULL,
    cycle_index INTEGER,
    duration_sec REAL,
    filename TEXT,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_account ON events(account);
"""

def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _conn() as conn:
        conn.executescript(_SCHEMA)

def record_event(run_id, event_type, account=None, cycle_index=None,
                 duration_sec=None, filename=None, extra=None):
    """Append one event. Never raises — stats must not break automation."""
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO events (ts, run_id, account, event_type, "
                "cycle_index, duration_sec, filename, extra) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(time.time()), run_id or "", account, event_type,
                 cycle_index, duration_sec, filename,
                 json.dumps(extra, ensure_ascii=False) if extra else None),
            )
    except Exception as e:
        logger.warning(f"health_db.record_event failed: {e}")

def _range_clause(date_from, date_to):
    """date_from/date_to are 'YYYY-MM-DD' local-date strings or None."""
    clauses, params = [], []
    if date_from:
        clauses.append("date(ts,'unixepoch','localtime') >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("date(ts,'unixepoch','localtime') <= ?")
        params.append(date_to)
    return clauses, params

def query_events(date_from=None, date_to=None, account=None, run_id=None, limit=2000):
    clauses, params = _range_clause(date_from, date_to)
    if account:
        clauses.append("account = ?")
        params.append(account)
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (f"SELECT id, ts, run_id, account, event_type, cycle_index, "
           f"duration_sec, filename, extra FROM events {where} "
           f"ORDER BY ts DESC, id DESC LIMIT ?")
    params.append(int(limit))
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    for r in rows:
        r["extra"] = json.loads(r["extra"]) if r["extra"] else None
    return rows

def summary(date_from=None, date_to=None, group_by="account"):
    """group_by: 'account' or 'day'. Returns aggregate rows."""
    key = ("account" if group_by == "account"
           else "date(ts,'unixepoch','localtime')")
    clauses, params = _range_clause(date_from, date_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        f"SELECT {key} AS grp, "
        "SUM(event_type='success') AS successes, "
        "SUM(event_type='refused') AS refusals, "
        "SUM(event_type='reset') AS resets, "
        "SUM(event_type='quota') AS quota_hits, "
        "AVG(CASE WHEN event_type='success' THEN duration_sec END) AS avg_duration_sec "
        f"FROM events {where} GROUP BY grp ORDER BY grp"
    )
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, params)]

def list_runs(limit=50):
    """One row per run. end_ts is None while a run is still in progress.

    A run_id is reused across stop/continue cycles, so it can hold several
    run_end events and still be live — only a run_end newer than the last
    run_start/run_resume means the run is actually finished. MAX(ts) is not
    an end marker (it just tracks the newest event, i.e. 'now' for a live
    run), so it must never be shown as an End Time.
    """
    sql = (
        "SELECT run_id, MIN(ts) AS start_ts, "
        "MAX(CASE WHEN event_type='run_end' THEN ts END) AS last_end_ts, "
        "MAX(CASE WHEN event_type IN ('run_start','run_resume') THEN ts END) AS last_active_ts, "
        "COUNT(DISTINCT account) AS accounts, "
        "SUM(event_type='success') AS successes, "
        "SUM(event_type='refused') AS refusals, "
        "SUM(event_type='reset') AS resets, "
        "SUM(event_type='quota') AS quota_hits "
        "FROM events WHERE run_id != '' "
        "GROUP BY run_id ORDER BY start_ts DESC LIMIT ?"
    )
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, (int(limit),))]
    for r in rows:
        end, active = r.pop("last_end_ts"), r.pop("last_active_ts")
        r["end_ts"] = end if end is not None and (active is None or end >= active) else None
    return rows

def delete_run(run_id):
    """Delete all events for one run_id. Returns count."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
        return cur.rowcount

def delete_all_runs():
    """Delete all events belonging to any run. Returns count."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM events WHERE run_id != ''")
        return cur.rowcount
