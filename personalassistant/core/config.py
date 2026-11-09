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
