"""
state_store.py — Per-user alert-crossing state, backed by the `alert_state`
SQLite table.

Responsibilities:
- Track the last direction in which an alert was sent for a given
  (user_id, ticker) pair, so that stocks_monitor can suppress duplicate
  alerts for the same sustained move.
- Provide three operations:
    get_alert_direction(user_id, ticker) -> "up" | "down" | None
    set_alert_direction(user_id, ticker, direction)  — upserts; pass None
        to clear the state (e.g. after a price reverses past the threshold
        back toward the previous close).

Table schema:
    alert_state (
        user_id                TEXT NOT NULL,
        ticker                 TEXT NOT NULL,
        last_alerted_direction TEXT,          -- "up" | "down" | NULL
        updated_at             TEXT NOT NULL,  -- UTC ISO-8601
        PRIMARY KEY (user_id, ticker)
    )

No business logic about when to alert — that belongs in stocks_monitor.
No LLM calls. No Discord interaction. Pure state persistence.
"""

from datetime import datetime, timezone

from personalassistant.core.db import get_conn


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create the alert_state table if it doesn't already exist."""
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
