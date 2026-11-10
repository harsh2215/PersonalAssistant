"""
main.py — Orchestrator: initialises storage, schedules monitors, runs listener.

Start order:
  1. Load environment variables (.env).
  2. Init all SQLite tables (idempotent).
  3. Run both monitors once immediately so data is fresh on first startup.
  4. Register APScheduler jobs and start the scheduler in the background.
  5. Print registered jobs + next run times.
  6. Hand off to discord_listener.client.run() — blocks until Ctrl+C.

Scheduler triggers:
  - stocks_monitor.run   : cron, Mon-Fri, :00 and :30, hours 9-15 IST.
    The monitor itself gates on is_market_open(), so stray scheduler
    ticks (e.g. at exactly 15:30) simply return early.
  - calendar_monitor.run : interval, every 3 hours.
  - calendar_monitor.check_alerts : interval, every 15 minutes.
    Sends near-event DMs from already-stored data; no Google API calls.
  - history cleanup      : cron, daily 03:00 IST.

Both jobs use the Asia/Kolkata timezone so APScheduler's "next run time"
display is meaningful at a glance.
"""

import logging
import sys

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from personalassistant.agents import calendar_monitor, stocks_monitor
from personalassistant.core import bootstrap, discord_listener, history_store
from personalassistant.core.config import DISCORD_BOT_TOKEN, STOCK_HISTORY_RETAIN_DAYS

logger = logging.getLogger(__name__)

_TZ = "Asia/Kolkata"


def _cleanup_history() -> None:
    """Delete stock_history rows older than STOCK_HISTORY_RETAIN_DAYS. Called daily at 03:00 IST."""
    deleted = history_store.cleanup_old_stock_history(STOCK_HISTORY_RETAIN_DAYS)
    if deleted:
        logger.info("Removed %d stale stock history row(s).", deleted)


# ── Startup runs ──────────────────────────────────────────────────────────────

def _run_monitors_now() -> None:
    """
    Run both monitors once immediately so the data is fresh before the bot
    starts accepting questions.  Errors in either monitor are caught and
    logged — they must not abort startup.
    """
    logger.info("Running startup monitor pass …")

    try:
        stocks_monitor.run(snapshot_only=True)   # store latest price even if market is closed
        logger.info("stocks_monitor startup run complete.")
    except Exception:
        logger.exception("stocks_monitor startup run failed — continuing anyway.")

    try:
        calendar_monitor.run()
        logger.info("calendar_monitor startup run complete.")
        calendar_monitor.check_alerts()
        logger.info("calendar_monitor startup alert check complete.")
    except Exception:
        logger.exception("calendar_monitor startup run failed — continuing anyway.")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _build_scheduler() -> BackgroundScheduler:
    """
    Configure and return a started BackgroundScheduler with both monitor jobs.

    stocks_monitor   — cron: Mon-Fri, minutes 0 and 30, hours 9 through 15 IST.
    calendar_monitor — interval: every 3 hours.
    calendar_alerts  — interval: every 15 minutes (no Google API; reads stored events).
    history_cleanup  — cron: daily 03:00 IST.
    """
    scheduler = BackgroundScheduler(timezone=_TZ)

    scheduler.add_job(
        func=stocks_monitor.run,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="0,30",
            timezone=_TZ,
        ),
        id="stocks_monitor",
        name="Stock price monitor",
        misfire_grace_time=300,   # allow up to 5 min late if the process was sleeping
        coalesce=True,            # collapse multiple missed fires into one
    )

    scheduler.add_job(
        func=calendar_monitor.run,
        trigger=IntervalTrigger(hours=3, timezone=_TZ),
        id="calendar_monitor",
        name="Google Calendar monitor",
        misfire_grace_time=600,
        coalesce=True,
    )

    scheduler.add_job(
        func=calendar_monitor.check_alerts,
        trigger=IntervalTrigger(minutes=15, timezone=_TZ),
        id="calendar_alerts",
        name="Calendar event alerts",
        misfire_grace_time=300,
        coalesce=True,
    )

    scheduler.add_job(
        func=_cleanup_history,
        trigger=CronTrigger(hour=3, minute=0, timezone=_TZ),
        id="history_cleanup",
        name="Stock history cleanup",
        misfire_grace_time=3600,
        coalesce=True,
    )

    scheduler.start()
    return scheduler


def _print_jobs(scheduler: BackgroundScheduler) -> None:
    """Print each scheduled job and its next run time."""
    print("\n┌─ Scheduled jobs ──────────────────────────────────────")
    for job in scheduler.get_jobs():
        next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z") if job.next_run_time else "N/A"
        print(f"│  [{job.id}]  {job.name}")
        print(f"│      next run: {next_run}")
    print("└───────────────────────────────────────────────────────\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Application entry point. See module docstring for the full start-up sequence."""
    bootstrap.setup_logging()

    if not DISCORD_BOT_TOKEN:
        logger.error(
            "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
        sys.exit(1)

    logger.info("PersonalAssistant starting up …")

    bootstrap.init_storage()
    logger.info("Storage initialised.")
    _run_monitors_now()

    scheduler = _build_scheduler()
    _print_jobs(scheduler)

    logger.info("Discord listener starting — press Ctrl+C to stop.")
    try:
        discord_listener.client.run(DISCORD_BOT_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Shutting down scheduler …")
        scheduler.shutdown(wait=False)
        logger.info("PersonalAssistant stopped.")


if __name__ == "__main__":
    main()
