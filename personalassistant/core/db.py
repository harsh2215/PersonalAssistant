"""
db.py — Shared SQLite connection factory for all store modules.

Provides:
    DB_PATH  — resolved Path to the database file (read from the DB_PATH
               env var, defaulting to data/personalassistant.db relative
               to the repo root). Centralising this in one place means
               every store module uses the same file without any of them
               knowing the resolution logic.

    get_conn — context manager that yields a WAL-mode sqlite3.Connection
               with row_factory=sqlite3.Row, auto-commits on success and
               rolls back on any exception.

All store modules (user_registry, state_store, history_store) import from
here. Swapping to a different SQLite path (e.g. for testing) means setting
the DB_PATH env var once — nothing else changes.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB = _REPO_ROOT / "data" / "personalassistant.db"

DB_PATH = Path(os.getenv("DB_PATH", str(_DEFAULT_DB)))


@contextmanager
def get_conn():
    """
    Yield a committed, WAL-mode sqlite3 connection.

    Usage::

        from personalassistant.core.db import get_conn

        with get_conn() as conn:
            conn.execute("INSERT INTO ...")

    The parent directory of DB_PATH is created automatically if absent.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
