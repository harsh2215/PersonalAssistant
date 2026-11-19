"""
history_store.py — Per-user time-series storage for stock prices and calendar
events, backed by the `stock_history` and `calendar_events` SQLite tables.

Public interface
────────────────
Stock history:
    init_db()
    log_stock_price(user_id, ticker, price, prev_close, pct_change)
    get_stock_history(user_id, ticker, days=7)  -> list[dict], oldest first
    cleanup_old_stock_history(retain_days=30)   -> int (rows deleted, all users)

Calendar events:
    replace_calendar_events(user_id, events)
        Atomically deletes all existing rows for this user, then reinserts the
        supplied list. Keeps the table idempotent across polls.
    get_upcoming_events(user_id, days=7) -> list[dict], by start_time ASC
        `days` counts calendar days in IST *including today* (days=1 → the
        rest of today, days=2 → today + tomorrow).
    get_calendar_sync_status(user_id) -> dict | None
    record_calendar_sync_error(user_id, message)
        Sync bookkeeping so readers can tell "no events" from "sync broken".
    get_events_starting_soon(user_id, within_minutes) -> list[dict]
        Only timed events (not all-day) starting within the next N minutes.

Table schemas
─────────────
stock_history:
    id          INTEGER PRIMARY KEY AUTOINCREMENT
    user_id     TEXT NOT NULL
    ticker      TEXT NOT NULL
    price       REAL NOT NULL
    prev_close  REAL NOT NULL
    pct_change  REAL NOT NULL
    recorded_at TEXT NOT NULL  -- UTC ISO-8601
    INDEX on (user_id, ticker, recorded_at)

calendar_events:
    id         INTEGER PRIMARY KEY AUTOINCREMENT
    user_id    TEXT NOT NULL
    event_id   TEXT NOT NULL   -- Google Calendar event ID
    title      TEXT NOT NULL
    start_time TEXT NOT NULL   -- ISO-8601, local or UTC depending on source
    end_time   TEXT NOT NULL
    location   TEXT            -- nullable
    fetched_at TEXT NOT NULL   -- UTC ISO-8601
    UNIQUE (user_id, event_id)
    INDEX on (user_id, start_time)

calendar_sync:
    user_id         TEXT PRIMARY KEY
    last_success_at TEXT           -- UTC ISO-8601 of the last successful sync
    last_error      TEXT           -- message of the most recent failure, cleared on success
    last_error_at   TEXT           -- UTC ISO-8601

No LLM calls. No Discord interaction. Pure SQLite reads and writes.
"""

from datetime import datetime, timedelta, timezone

from personalassistant.core.config import IST
from personalassistant.core.db import get_conn


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """
    Create stock_history, calendar_events and calendar_sync tables (and their
    indexes) if they don't already exist. Safe to call multiple times.
    """
    with get_conn() as conn:
        # ── stock_history ────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                ticker      TEXT NOT NULL,
                price       REAL NOT NULL,
                prev_close  REAL NOT NULL,
                pct_change  REAL NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stock_history_lookup
            ON stock_history (user_id, ticker, recorded_at)
        """)

        # ── calendar_events ──────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calendar_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                event_id   TEXT NOT NULL,
                title      TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time   TEXT NOT NULL,
                location   TEXT,
                fetched_at TEXT NOT NULL,
                UNIQUE (user_id, event_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_calendar_events_lookup
            ON calendar_events (user_id, start_time)
        """)

        # ── calendar_sync ────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calendar_sync (
                user_id         TEXT PRIMARY KEY,
                last_success_at TEXT,
                last_error      TEXT,
                last_error_at   TEXT
            )
        """)


# ── Stock history ─────────────────────────────────────────────────────────────

def log_stock_price(
    user_id: str,
    ticker: str,
    price: float,
    prev_close: float,
    pct_change: float,
) -> None:
    """
    Append one stock-price observation for a user.

    Args:
        user_id:    Discord snowflake of the target user.
        ticker:     Normalised ticker symbol (e.g. "RELIANCE.NS").
        price:      Latest traded price.
        prev_close: Previous session's closing price.
        pct_change: Percentage change vs. prev_close (positive = up).
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO stock_history
                (user_id, ticker, price, prev_close, pct_change, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, ticker, price, prev_close, pct_change, now),
        )


def get_stock_history(
    user_id: str,
    ticker: str,
    days: int = 7,
) -> list[dict]:
    """
    Return stock-price observations for a user/ticker pair within the last
    `days` days, ordered oldest-to-newest.

    Args:
        user_id: Discord snowflake.
        ticker:  Normalised ticker symbol.
        days:    Look-back window in days (default 7).

    Returns:
        List of dicts with keys: id, user_id, ticker, price, prev_close,
        pct_change, recorded_at.  Empty list if no data.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, ticker, price, prev_close, pct_change, recorded_at
            FROM   stock_history
            WHERE  user_id = ? AND ticker = ? AND recorded_at >= ?
            ORDER  BY recorded_at ASC
            """,
            (user_id, ticker, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def cleanup_old_stock_history(retain_days: int = 30) -> int:
    """
    Delete stock_history rows older than `retain_days` days across ALL users.

    Scheduled as a daily job in main.py.

    Args:
        retain_days: Rows older than this many days are removed (default 30).

    Returns:
        Number of rows deleted.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM stock_history WHERE recorded_at < ?",
            (cutoff,),
        )
        return cur.rowcount


# ── Calendar events ───────────────────────────────────────────────────────────

def replace_calendar_events(user_id: str, events: list[dict]) -> None:
    """
    Atomically replace all calendar events for a user with the supplied list.

    The delete and all inserts run inside a single transaction so there is no
    window during which the user's events appear empty. The same transaction
    marks the sync as successful (clearing any previous error).

    Each dict in `events` must contain:
        event_id   (str)        — Google Calendar event ID
        title      (str)
        start_time (str)        — ISO-8601
        end_time   (str)        — ISO-8601
        location   (str | None) — optional

    Args:
        user_id: Discord snowflake.
        events:  List of event dicts from the calendar adapter.
    """
    now = datetime.now(timezone.utc).isoformat()

    rows_to_insert = [
        {
            "user_id":    user_id,
            "event_id":   e["event_id"],
            "title":      e["title"],
            "start_time": e["start_time"],
            "end_time":   e["end_time"],
            "location":   e.get("location"),
            "fetched_at": now,
        }
        for e in events
    ]

    with get_conn() as conn:
        conn.execute(
            "DELETE FROM calendar_events WHERE user_id = ?",
            (user_id,),
        )
        if rows_to_insert:
            conn.executemany(
                """
                INSERT INTO calendar_events
                    (user_id, event_id, title, start_time, end_time,
                     location, fetched_at)
                VALUES
                    (:user_id, :event_id, :title, :start_time, :end_time,
                     :location, :fetched_at)
                """,
                rows_to_insert,
            )
        conn.execute(
            """
            INSERT INTO calendar_sync (user_id, last_success_at, last_error, last_error_at)
            VALUES (?, ?, NULL, NULL)
            ON CONFLICT (user_id) DO UPDATE SET
                last_success_at = excluded.last_success_at,
                last_error      = NULL,
                last_error_at   = NULL
            """,
            (user_id, now),
        )


def record_calendar_sync_error(user_id: str, message: str) -> None:
    """Remember that the latest calendar sync for user_id failed (keeps last_success_at)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO calendar_sync (user_id, last_success_at, last_error, last_error_at)
            VALUES (?, NULL, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                last_error    = excluded.last_error,
                last_error_at = excluded.last_error_at
            """,
            (user_id, message, now),
        )


def get_calendar_sync_status(user_id: str) -> dict | None:
    """
    Return {"last_success_at", "last_error", "last_error_at"} for the user,
    or None if no sync has ever been attempted.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_success_at, last_error, last_error_at "
            "FROM calendar_sync WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def _parse_event_dt(time_str: str) -> datetime | None:
    """Parse an ISO-8601 string into an aware datetime (UTC fallback)."""
    try:
        s = time_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def get_upcoming_events(user_id: str, days: int = 7) -> list[dict]:
    """
    Return upcoming calendar events, ordered by start_time ascending.

    The window runs from "now" to the end of the IST calendar day that is
    `days` days long counting today — days=1 means "the rest of today",
    days=2 "today and tomorrow". Day boundaries are IST so "tomorrow" means
    what the user means. Events that already ended are excluded; an event in
    progress is kept. All-day events are kept for every day they span
    (Google's all-day end date is exclusive, which is handled here).

    Args:
        user_id: Discord snowflake.
        days:    Number of IST calendar days to cover, including today
                 (default 7; callers typically cap it at the user's
                 calendar_lookahead_days).

    Returns:
        List of dicts with keys: id, user_id, event_id, title, start_time,
        end_time, location, fetched_at.  Empty list if none found.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, event_id, title, start_time, end_time,
                   location, fetched_at
            FROM   calendar_events
            WHERE  user_id = ?
            """,
            (user_id,),
        ).fetchall()

    now = datetime.now(timezone.utc)
    today_ist = now.astimezone(IST).date()
    # Exclusive horizon: midnight IST at the start of day (today + days).
    horizon = datetime.combine(today_ist + timedelta(days=days), datetime.min.time(), tzinfo=IST)
    horizon_date = horizon.date()

    results: list[tuple[datetime, dict]] = []
    for r in rows:
        d = dict(r)

        # All-day event (YYYY-MM-DD, no time component):
        if "T" not in d["start_time"]:
            try:
                start_date = datetime.strptime(d["start_time"], "%Y-%m-%d").date()
                # Google's all-day `end` date is EXCLUSIVE (a one-day event on
                # the 19th has end = the 20th), so the last day it covers is
                # end - 1; fall back to a single day if end is missing.
                last_date = (
                    datetime.strptime(d["end_time"], "%Y-%m-%d").date() - timedelta(days=1)
                    if d.get("end_time") and "T" not in d["end_time"]
                    else start_date
                )
                last_date = max(last_date, start_date)
                if last_date >= today_ist and start_date < horizon_date:
                    event_dt = datetime.combine(
                        start_date, datetime.min.time(), tzinfo=IST
                    )
                    results.append((event_dt, d))
            except ValueError:
                pass
            continue

        st = _parse_event_dt(d["start_time"])
        et = _parse_event_dt(d.get("end_time", "")) if d.get("end_time") else st

        # Timed event: keep if it hasn't ended yet and starts before the horizon
        end_bound = et if et is not None else st
        if end_bound is not None and st is not None and end_bound >= now and st < horizon:
            results.append((st, d))

    results.sort(key=lambda x: x[0])
    return [d for _, d in results]


def get_events_starting_soon(user_id: str, within_minutes: int) -> list[dict]:
    """
    Return calendar events that start within the next ``within_minutes`` minutes.

    Only timed events are returned — all-day events (whose ``start_time``
    contains only a date string, e.g. ``"2026-11-10"``) are excluded because
    minute-level alerting doesn't apply to them.

    Args:
        user_id:        Discord snowflake.
        within_minutes: Look-ahead window in whole minutes.

    Returns:
        List of event dicts with keys: id, user_id, event_id, title,
        start_time, end_time, location, fetched_at — ordered by
        start_time ASC.  Empty list if none found.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, event_id, title, start_time, end_time,
                   location, fetched_at
            FROM   calendar_events
            WHERE  user_id = ?
              AND  start_time LIKE '%T%'
            """,
            (user_id,),
        ).fetchall()

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=within_minutes)

    matched: list[tuple[datetime, dict]] = []
    for r in rows:
        d = dict(r)
        dt = _parse_event_dt(d["start_time"])
        if dt is not None and now <= dt <= horizon:
            matched.append((dt, d))

    matched.sort(key=lambda x: x[0])
    return [d for _, d in matched]
