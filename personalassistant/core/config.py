"""
config.py — Single place for environment loading and shared constants.

Importing this module loads `.env` exactly once; every other module reads
settings from here instead of calling load_dotenv() itself.
"""

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN: str = os.environ.get("DISCORD_BOT_TOKEN", "")
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")

IST = ZoneInfo("Asia/Kolkata")

# Discord hard cap on a single message
DISCORD_MESSAGE_LIMIT = 2000

# Stock history retention (days)
STOCK_HISTORY_RETAIN_DAYS = 30

# How many minutes before a calendar event starts to send a DM alert.
# The alert check job runs every 15 minutes, so the actual lead time is
# between (CALENDAR_ALERT_LOOKAHEAD_MINUTES - 15) and
# CALENDAR_ALERT_LOOKAHEAD_MINUTES minutes.
CALENDAR_ALERT_LOOKAHEAD_MINUTES: int = int(
    os.environ.get("CALENDAR_ALERT_LOOKAHEAD_MINUTES", "30")
)
