"""
bootstrap.py — Startup helpers shared by main.py and every module's
__main__ entry point (logging setup + idempotent table creation).
"""

import logging
import sys

from personalassistant.core import history_store, state_store, user_registry


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def init_storage() -> None:
    """Create all SQLite tables if they don't already exist."""
    user_registry.init_db()
    history_store.init_db()
    state_store.init_db()
