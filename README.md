# PersonalAssistant

A multi-tenant background service that:

- **Monitors stock prices** (NSE/Yahoo Finance) and DMs you on Discord when a stock moves past your threshold.
- **Syncs Google Calendar** events per user and sends Discord DM alerts for upcoming events.
- **Answers questions** about your stored data in Discord DMs, using a Gemini-backed chat agent grounded in tool calls (it never invents prices or events).

## Architecture

```
main.py ── APScheduler ──┬─ stocks_monitor     Mon–Fri, :00/:30, 9–15 IST (gated on 09:15–15:30)
   │                     ├─ calendar_monitor   every 3 hours (sync from Google Calendar)
   │                     ├─ calendar_alerts    every 15 minutes (near-term event alerts)
   │                     └─ history cleanup    daily 03:00 IST
   └─ Discord listener (DMs) ──► chat_agent (Gemini + tools) ──► history_store / user_registry
```

| Path | Role |
|---|---|
| `main.py` | Orchestrator: init storage, run monitors once, schedule jobs, run the bot |
| `agents/stocks_monitor.py` | No LLM. Concurrent quote fetch (shared session, retries), per-user threshold alerts with de-duplication |
| `agents/calendar_monitor.py` | No LLM. Per-user Google OAuth, replaces each user's stored events, sends Discord DM alerts for events starting soon |
| `agents/chat_agent.py` | Gemini function-calling over `get_watchlist`, `get_latest_quote` (live; last close when market is closed), `get_stock_history`, `get_upcoming_events` (all scoped to the asking user) |
| `core/config.py` | Loads `.env` once; shared constants |
| `core/bootstrap.py` | Logging setup + idempotent table creation |
| `core/db.py` | SQLite (WAL) connection context manager |
| `core/user_registry.py` | Users + watchlists; interactive admin CLI |
| `core/history_store.py` | Stock price history and calendar events |
| `core/state_store.py` | Last alert direction per (user, ticker) and calendar alert deduplication |
| `core/discord_listener.py` / `discord_notifier.py` | Inbound DM handling / outbound alerts via the Discord REST API |
| `core/data_source_adapter.py` | Adapter seam so data backends can be swapped (e.g. MCP) without touching monitors |

### Alert behaviour

**Stocks:** For each user and ticker, the change vs. previous close is compared with the user's `alert_threshold_pct`. An alert is sent on the first crossing in a direction; further polls in the same direction are suppressed; when the price returns inside the band the state resets so the next crossing alerts again. On startup the monitor takes one price snapshot even if the market is closed (stored, no alerts), so the chat agent always has data. Market holidays are not handled (a holiday poll just sees no movement).

**Calendar:** Every 15 minutes, the service checks stored events for any timed event starting within `CALENDAR_ALERT_LOOKAHEAD_MINUTES` (default 30 min). Alerts are deduplicated in SQLite (`calendar_alert_state`), ensuring each event triggers at most one Discord DM.

## Setup

**Requirements:** Python 3.10+ (uses `X | None` types and `zoneinfo`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then fill in the values below
```

| Variable | Purpose |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token from the Discord Developer Portal |
| `GEMINI_API_KEY` | Google AI Studio key for the chat agent |
| `DB_PATH` *(optional)* | SQLite file path; defaults to `data/personalassistant.db` |
| `CALENDAR_ALERT_LOOKAHEAD_MINUTES` *(optional)* | Minutes before calendar event to send alert; defaults to `30` |

**Discord:** in the Developer Portal → your bot → Bot → *Privileged Gateway Intents*, enable **Message Content Intent**. Users must share a server with the bot (or have DMs open) to receive DMs.

**Google Calendar (optional):** create an OAuth *Desktop* client in Google Cloud Console (Calendar API enabled), download it to `config/google_credentials.json`. The first calendar run for each user opens a browser for consent; the token is cached in `data/` (both are gitignored).

## Usage

1. **Register users** (Discord user ID, display name, watchlist, alert threshold, calendar lookahead):
   ```bash
   python -m personalassistant.core.user_registry
   ```
   Tickers without a suffix get `.NS` automatically; indices use `^` (e.g. `^NSEI`).
2. **Run the service:**
   ```bash
   python main.py
   ```
3. **DM the bot** from a registered account: *"How is my watchlist doing?"*, *"What's on my calendar this week?"*

Each component can also be exercised on its own:

```bash
python -m personalassistant.agents.stocks_monitor
python -m personalassistant.agents.calendar_monitor
python -m personalassistant.agents.chat_agent      # terminal chat, no Discord
```

## Notes

- Stock history is kept for 30 days (`STOCK_HISTORY_RETAIN_DAYS` in `core/config.py`).
- Secrets and local data (`.env`, `data/*.db`, Google credentials/tokens) are gitignored.
