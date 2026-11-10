"""
state_store.py — Per-user alert deduplication state, backed by two SQLite tables.

Responsibilities:
- ``alert_state`` — track the last price-move direction alerted for a
  (user_id, ticker) pair so stocks_monitor can suppress duplicate alerts
  for the same sustained move.
- ``calendar_alert_state`` — record which calendar events have already
  triggered a DM so calendar_monitor never re-alerts for the same event.

Table schemas:
    alert_state (
        user_id                TEXT NOT NULL,
        ticker                 TEXT NOT NULL,
        last_alerted_direction TEXT,          -- "up" | "down" | NULL
        updated_at             TEXT NOT NULL,  -- UTC ISO-8601
        PRIMARY KEY (user_id, ticker)
    )

    calendar_alert_state (
        user_id    TEXT NOT NULL,
        event_id   TEXT NOT NULL,   -- Google Calendar event ID
        alerted_at TEXT NOT NULL,   -- UTC ISO-8601
        PRIMARY KEY (user_id, event_id)
    )

No business logic about when to alert — that belongs in the monitor modules.
No LLM calls. No Discord interaction. Pure state persistence.
"""

from datetime import datetime, timezone

from personalassistant.core.db import get_conn


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create alert_state and calendar_alert_state tables if they don't already exist."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_state (
                user_id                TEXT NOT NULL,
                ticker                 TEXT NOT NULL,
                last_alerted_direction TEXT,
                updated_at             TEXT NOT NULL,
                PRIMARY KEY (user_id, ticker)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calendar_alert_state (
                user_id    TEXT NOT NULL,
                event_id   TEXT NOT NULL,
                alerted_at TEXT NOT NULL,
                PRIMARY KEY (user_id, event_id)
            )
        """)


# ── Public interface ──────────────────────────────────────────────────────────

def get_alert_direction(user_id: str, ticker: str) -> str | None:
    """
    Return the last direction ("up" or "down") for which an alert was sent,
    or None if no alert has been sent yet (or the state was cleared).

    Args:
        user_id: Discord snowflake of the target user.
        ticker:  Normalised ticker symbol (e.g. "RELIANCE.NS").

    Returns:
        "up", "down", or None.
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT last_alerted_direction
            FROM   alert_state
            WHERE  user_id = ? AND ticker = ?
            """,
            (user_id, ticker),
        ).fetchone()
    return row["last_alerted_direction"] if row is not None else None


def set_alert_direction(
    user_id: str,
    ticker: str,
    direction: str | None,
) -> None:
    """
    Upsert the last alerted direction for a (user_id, ticker) pair.

    Pass direction=None to clear the state (e.g. price has reverted and
    the monitor wants to allow a fresh alert in either direction).

    Args:
        user_id:   Discord snowflake of the target user.
        ticker:    Normalised ticker symbol.
        direction: "up", "down", or None.

    Raises:
        ValueError: if direction is not one of {"up", "down", None}.
    """
    if direction not in {"up", "down", None}:
        raise ValueError(f"direction must be 'up', 'down', or None — got {direction!r}")

    now = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO alert_state (user_id, ticker, last_alerted_direction, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id, ticker) DO UPDATE SET
                last_alerted_direction = excluded.last_alerted_direction,
                updated_at             = excluded.updated_at
            """,
            (user_id, ticker, direction, now),
        )


# ── Calendar alert state ──────────────────────────────────────────────────────

def has_calendar_alert_been_sent(user_id: str, event_id: str) -> bool:
    """
    Return True if a DM alert has already been sent for this (user, event) pair.

    Args:
        user_id:  Discord snowflake of the target user.
        event_id: Google Calendar event ID.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM calendar_alert_state WHERE user_id = ? AND event_id = ?",
            (user_id, event_id),
        ).fetchone()
    return row is not None


def mark_calendar_alert_sent(user_id: str, event_id: str) -> None:
    """
    Record that a calendar alert DM has been sent for this (user, event) pair.

    Safe to call repeatedly — subsequent calls simply update ``alerted_at``.

    Args:
        user_id:  Discord snowflake of the target user.
        event_id: Google Calendar event ID.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO calendar_alert_state (user_id, event_id, alerted_at)
            VALUES (?, ?, ?)
            ON CONFLICT (user_id, event_id) DO UPDATE SET alerted_at = excluded.alerted_at
            """,
            (user_id, event_id, now),
        )


def clear_stale_calendar_alerts(user_id: str, keep_event_ids: set[str]) -> int:
    """
    Delete calendar_alert_state rows whose event_id is no longer upcoming.

    Call this after replacing a user's stored calendar events so the state
    table doesn't grow unboundedly with past-event IDs.

    Args:
        user_id:        Discord snowflake.
        keep_event_ids: Set of event IDs currently stored for this user.
                        Every other event ID in the state table is deleted.

    Returns:
        Number of rows deleted.
    """
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT event_id FROM calendar_alert_state WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        stale = [r["event_id"] for r in existing if r["event_id"] not in keep_event_ids]
        if not stale:
            return 0

        placeholders = ",".join("?" * len(stale))
        cur = conn.execute(
            f"DELETE FROM calendar_alert_state WHERE user_id = ? AND event_id IN ({placeholders})",
            [user_id, *stale],
        )
        return cur.rowcount
