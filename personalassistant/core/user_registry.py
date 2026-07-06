"""
user_registry.py — User CRUD and terminal-driven OAuth onboarding.

Responsibilities:
- Own the `users` SQLite table schema and provide a clean interface over it:
    init_db()                           — creates table if absent (idempotent)
    add_user(user_id, display_name, …)  — insert; raises ValueError if exists
    get_user(user_id)                   — returns dict | None
    get_all_users()                     — returns list[dict]
    update_user(user_id, **fields)      — partial update; raises if not found
    remove_user(user_id)               — delete; raises if not found
- Normalise tickers on every write: auto-append ".NS" to any ticker that
  contains neither "." nor "^".
- Provide a __main__ CLI entry point for interactive user registration.
  (Google Calendar onboarding — OAuth consent flow — is a separate step,
  handled later in onboard_calendar().)

DB path is read from the DB_PATH env var, defaulting to
data/personalassistant.db relative to the repo root. This keeps the module
testable against a temp file without touching production data.

No LLM calls. No Discord interaction. Pure SQLite + stdlib.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone

from personalassistant.core.db import get_conn

# Fields callers are allowed to update via update_user()
_UPDATABLE_FIELDS = frozenset({
    "display_name",
    "stock_watchlist",
    "alert_threshold_pct",
    "calendar_lookahead_days",
    "google_token_path",
})


# ── Internal helpers ──────────────────────────────────────────────────────────

def _normalise_ticker(ticker: str) -> str:
    """Auto-append '.NS' to any ticker that contains neither '.' nor '^'."""
    ticker = ticker.strip().upper()
    if ticker and "." not in ticker and "^" not in ticker:
        ticker += ".NS"
    return ticker


def _normalise_watchlist(tickers: list[str]) -> list[str]:
    """Return a deduplicated, normalised list of tickers."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in tickers:
        t = _normalise_ticker(raw)
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a Row to a plain dict, deserialising the JSON watchlist field."""
    d = dict(row)
    d["stock_watchlist"] = json.loads(d.get("stock_watchlist") or "[]")
    return d


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create the users table if it doesn't already exist. Safe to call repeatedly."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id                  TEXT PRIMARY KEY,
                display_name             TEXT NOT NULL,
                stock_watchlist          TEXT NOT NULL DEFAULT '[]',
                alert_threshold_pct      REAL NOT NULL DEFAULT 3.0,
                calendar_lookahead_days  INTEGER NOT NULL DEFAULT 10,
                google_token_path        TEXT,
                created_at               TEXT NOT NULL
            )
        """)


# ── CRUD ──────────────────────────────────────────────────────────────────────

def add_user(
    user_id: str,
    display_name: str,
    stock_watchlist: list[str] | None = None,
    alert_threshold_pct: float = 3.0,
    calendar_lookahead_days: int = 10,
    google_token_path: str | None = None,
) -> None:
    """
    Insert a new user record.

    Raises:
        ValueError: if user_id is already registered.
    """
    watchlist = _normalise_watchlist(stock_watchlist or [])
    now = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        try:
            conn.execute(
                """
                INSERT INTO users
                    (user_id, display_name, stock_watchlist,
                     alert_threshold_pct, calendar_lookahead_days,
                     google_token_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    display_name,
                    json.dumps(watchlist),
                    alert_threshold_pct,
                    calendar_lookahead_days,
                    google_token_path,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"User '{user_id}' is already registered.")


def get_user(user_id: str) -> dict | None:
    """
    Return the user record as a dict, or None if not found.

    The 'stock_watchlist' key is a Python list (deserialised from JSON).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def get_all_users() -> list[dict]:
    """Return all registered users ordered by creation time, oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at ASC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_user(user_id: str, **fields) -> None:
    """
    Partially update one or more fields for an existing user.

    Accepted keyword arguments:
        display_name             (str)
        stock_watchlist          (list[str])  — re-normalised on write
        alert_threshold_pct      (float)
        calendar_lookahead_days  (int)
        google_token_path        (str | None)

    Raises:
        ValueError: if user_id is not found, no fields are given, or an
                    unsupported field name is supplied.
    """
    if not fields:
        return  # nothing to do

    unsupported = set(fields) - _UPDATABLE_FIELDS
    if unsupported:
        raise ValueError(f"Unsupported field(s): {unsupported}")

    # Serialise watchlist if provided
    if "stock_watchlist" in fields:
        fields["stock_watchlist"] = json.dumps(
            _normalise_watchlist(fields["stock_watchlist"])
        )

    set_clause = ", ".join(f"{col} = ?" for col in fields)
    values = [*fields.values(), user_id]

    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE users SET {set_clause} WHERE user_id = ?", values
        )
        if cur.rowcount == 0:
            raise ValueError(f"User '{user_id}' not found.")


def remove_user(user_id: str) -> None:
    """
    Delete a user record.

    Raises:
        ValueError: if user_id is not found.
    """
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        if cur.rowcount == 0:
            raise ValueError(f"User '{user_id}' not found.")


# ── __main__ interactive CLI ───────────────────────────────────────────────────

def _prompt(label: str, default: str = "") -> str:
    """Show a prompt with an optional default; return stripped input."""
    suffix = f" [{default}]" if default else ""
    raw = input(f"{label}{suffix}: ").strip()
    return raw if raw else default


def _cli_register() -> None:
    """Interactively register a new user, then print a summary."""
    print("\n══════════════════════════════════════════")
    print("  PersonalAssistant — Register New User")
    print("══════════════════════════════════════════\n")

    user_id = _prompt("Discord user ID (snowflake)").strip()
    if not user_id:
        print("✗  Discord ID is required. Aborting.")
        return

    display_name = _prompt("Display name").strip()
    if not display_name:
        print("✗  Display name is required. Aborting.")
        return

    raw_tickers = _prompt(
        "Stock watchlist (comma-separated tickers, blank for none)", ""
    )
    watchlist = [t.strip() for t in raw_tickers.split(",") if t.strip()]

    threshold_raw = _prompt("Alert threshold %", "3.0")
    try:
        threshold = float(threshold_raw)
    except ValueError:
        print("  ⚠ Invalid threshold — using default 3.0.")
        threshold = 3.0

    lookahead_raw = _prompt("Calendar lookahead days", "10")
    try:
        lookahead = int(lookahead_raw)
    except ValueError:
        print("  ⚠ Invalid lookahead — using default 10.")
        lookahead = 10

    # Ensure table exists before inserting
    init_db()

    try:
        add_user(
            user_id=user_id,
            display_name=display_name,
            stock_watchlist=watchlist,
            alert_threshold_pct=threshold,
            calendar_lookahead_days=lookahead,
        )
    except ValueError as exc:
        print(f"\n✗  {exc}")
        return

    user = get_user(user_id)
    print("\n✓  User registered successfully!")
    print(f"   ID         : {user['user_id']}")
    print(f"   Name       : {user['display_name']}")
    print(f"   Watchlist  : {user['stock_watchlist'] or '(none)'}")
    print(f"   Threshold  : ±{user['alert_threshold_pct']}%")
    print(f"   Lookahead  : {user['calendar_lookahead_days']} days")
    print(f"   Calendar   : not linked (run onboard_calendar to link)")
    print()


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    init_db()

    def _main_menu() -> None:
        while True:
            print("\n══════════════════════════════════════════")
            print("  PersonalAssistant — User Registry")
            print("══════════════════════════════════════════")
            print("  1. Register new user")
            print("  2. View registered users")
            print("  3. Edit watchlist")
            print("  4. Exit")
            choice = input("\nChoice: ").strip()

            if choice == "1":
                _cli_register()
            elif choice == "2":
                _cli_view_users()
            elif choice == "3":
                _cli_edit_watchlist()
            elif choice == "4":
                print("Goodbye.")
                sys.exit(0)
            else:
                print("  ⚠ Invalid choice — enter 1, 2, 3, or 4.")

    def _cli_view_users() -> None:
        users = get_all_users()
        if not users:
            print("\n  (no registered users)")
            return
        print(f"\n{'─'*60}")
        for u in users:
            wl = ", ".join(u["stock_watchlist"]) or "(none)"
            print(f"  ID        : {u['user_id']}")
            print(f"  Name      : {u['display_name']}")
            print(f"  Watchlist : {wl}")
            print(f"  Threshold : ±{u['alert_threshold_pct']}%")
            print(f"  Lookahead : {u['calendar_lookahead_days']} days")
            print(f"  Calendar  : {u.get('google_token_path') or '(not linked)'}")
            print(f"{'─'*60}")

    def _cli_edit_watchlist() -> None:
        print("\n══════════════════════════════════════════")
        print("  Edit Stock Watchlist")
        print("══════════════════════════════════════════\n")

        user_id = _prompt("Discord user ID").strip()
        if not user_id:
            print("✗  Discord ID is required. Aborting.")
            return

        user = get_user(user_id)
        if user is None:
            print(f"✗  User '{user_id}' not found. Register first (option 1).")
            return

        current = user["stock_watchlist"]
        current_str = ", ".join(current) if current else "(none)"
        print(f"\n  User     : {user['display_name']}")
        print(f"  Watchlist: {current_str}\n")

        print("  a. Add tickers")
        print("  b. Remove tickers")
        print("  c. Replace entire watchlist")
        print("  d. Cancel")
        sub = input("\n  Choice: ").strip().lower()

        if sub == "a":
            raw = _prompt("Tickers to add (comma-separated)").strip()
            if not raw:
                print("  (nothing entered — no changes made)")
                return
            new_tickers = [t.strip() for t in raw.split(",") if t.strip()]
            merged = list(current) + new_tickers
            update_user(user_id, stock_watchlist=merged)
            updated = get_user(user_id)["stock_watchlist"]
            print(f"\n✓  Watchlist updated: {', '.join(updated) or '(empty)'}")

        elif sub == "b":
            raw = _prompt("Tickers to remove (comma-separated)").strip()
            if not raw:
                print("  (nothing entered — no changes made)")
                return
            to_remove = {_normalise_ticker(t) for t in raw.split(",") if t.strip()}
            remaining = [t for t in current if t not in to_remove]
            update_user(user_id, stock_watchlist=remaining)
            updated = get_user(user_id)["stock_watchlist"]
            removed = to_remove & set(current)
            not_found = to_remove - set(current)
            if removed:
                print(f"\n✓  Removed: {', '.join(sorted(removed))}")
            if not_found:
                print(f"  ⚠ Not in watchlist (skipped): {', '.join(sorted(not_found))}")
            print(f"  Watchlist now: {', '.join(updated) or '(empty)'}")

        elif sub == "c":
            raw = _prompt("New watchlist (comma-separated, blank to clear)").strip()
            new_tickers = [t.strip() for t in raw.split(",") if t.strip()]
            update_user(user_id, stock_watchlist=new_tickers)
            updated = get_user(user_id)["stock_watchlist"]
            print(f"\n✓  Watchlist replaced: {', '.join(updated) or '(empty)'}")

        elif sub == "d":
            print("  Cancelled — no changes made.")
        else:
            print("  ⚠ Invalid choice — no changes made.")

    _main_menu()
