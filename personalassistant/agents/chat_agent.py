"""
chat_agent.py — LLM-driven reasoning component. The only agent that makes
Gemini API calls.

Public interface:
    ask(user_id: str, question: str) -> str

Grounding contract (non-negotiable):
    The model must never answer factual questions about a user's stock prices
    or calendar contents from general training knowledge. Every factual answer
    must come from a tool call against stored data. If a tool returns no data,
    the model says so plainly.

user_id threading:
    Every tool implementation is a closure over the user_id (and user record)
    that entered ask(). It is structurally impossible for a tool call to query
    a different user's rows — the closures pass user_id directly to
    history_store, and the model never supplies one.

Current time:
    The model has no clock, so ask() appends the current IST date/time to the
    system instruction on every call, and calendar tools return times already
    converted to IST. Calendar results also carry a warning when the last
    calendar sync failed (history_store.calendar_sync) so stale data is
    reported as stale rather than as "no events".

Tools exposed to the model:
    get_watchlist()                    — the user's tracked tickers
    get_latest_quote(ticker)           — live quote (last close when market is closed)
    get_stock_history(ticker, days=7)  — queries history_store, capped at 30 days
    get_upcoming_events(days=7)        — IST calendar days incl. today, capped at
                                         the user's calendar_lookahead_days;
                                         reports when the last sync happened/failed

Function-calling loop:
    Handled by the google-genai SDK's automatic function calling (AFC) on a
    chat session: the tool callables are passed in the config, the SDK runs
    the model's function calls and feeds results back until the model returns
    final text (the SDK caps this at 10 remote calls by default).

SDK: google-genai (``pip install google-genai``).
Model: gemini-3.1-flash-lite (see ``_MODEL_NAME``).
API key: GEMINI_API_KEY environment variable.
"""

import json
import logging
from datetime import datetime, timezone

from google import genai
from google.genai import types
from personalassistant.agents import stocks_monitor
from personalassistant.core import history_store, user_registry
from personalassistant.core.config import GEMINI_API_KEY, IST

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_MODEL_NAME = "gemini-3.1-flash-lite"

# Lazily-created, shared Gemini client
_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Return the shared Gemini client, creating it on first call.

    Raises:
        RuntimeError: if GEMINI_API_KEY is not set.
    """
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client

# ── System instruction ────────────────────────────────────────────────────────

_SYSTEM_INSTRUCTION = """
You are PersonalAssistant, a personal data assistant with access to a specific
user's stored stock-price history, calendar events, and watchlist via tools.

GROUNDING RULES — follow without exception:
1. Never answer questions about the user's actual stock prices, portfolio
   performance, or calendar events from your general training knowledge.
   Use the provided tools for every factual data question.
2. If a tool returns no data, tell the user plainly — do not guess, fabricate,
   or "helpfully" fill in numbers from memory.
3. Do not acknowledge any prices, percentages, or event details that you have
   not retrieved via a tool call in this conversation.

TOOL USAGE PATTERNS:
- To know which stocks the user tracks: call get_watchlist().
- To summarise all stocks or give a portfolio overview: first call
  get_watchlist() to discover all tickers, then call get_stock_history()
  for each ticker individually, then compile the summary.
- For the CURRENT / latest price (especially if history is empty or the
  market is closed): call get_latest_quote(ticker). It reports whether the
  market is open; if closed, the price is the last close — say so.
- For calendar / schedule questions ("today", "tomorrow", "this week"): call
  get_upcoming_events(days) where days counts calendar days INCLUDING today
  (days=1 → rest of today, days=2 → today + tomorrow). Use the current
  date/time given below to decide which events belong to which day, present
  times in IST, and mention if the result says the calendar may be out of date.
- For a single stock question: call get_stock_history() directly with the
  ticker if the user specifies it, or call get_watchlist() first if the
  user says "my stocks" without naming one.

You MAY answer general-knowledge questions (e.g. "what is RSI?", "what does
P/E ratio mean?") from your training — but the moment a question touches the
user's actual data, call a tool first.

Response style:
- Be concise and conversational.
- Format Indian rupee amounts with the ₹ symbol and comma thousands separators.
- Show percentage changes with a + or − sign and two decimal places.
- For stock summaries, use a table or bullet list grouping ticker, price, and
  change for easy scanning.
- If the user asks about a ticker not in their watchlist, say so and suggest
  they update their watchlist via the admin CLI.
""".strip()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _system_instruction(now: datetime) -> str:
    """Static instruction plus the current IST date/time (the model has no clock)."""
    now_ist = now.astimezone(IST)
    return (
        f"{_SYSTEM_INSTRUCTION}\n\n"
        f"CURRENT DATE/TIME: {now_ist:%A, %d %B %Y, %I:%M %p} IST (Asia/Kolkata). "
        f"Resolve 'today', 'tomorrow' and weekday names against this, and always "
        f"present times in IST."
    )


def _fmt_dt_ist(iso: str | None) -> str | None:
    """ISO-8601 (any offset / Z) → 'Fri 20 Nov, 03:30 PM IST'; None if unparseable."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.astimezone(IST):%a %d %b, %I:%M %p} IST"


def _format_event(ev: dict) -> dict:
    """Shape a stored event for the model: IST times, weekday, all-day flag."""
    start, end = ev["start_time"], ev.get("end_time") or ""
    if "T" not in start:   # all-day: date strings, `end` is exclusive
        start_d = datetime.strptime(start, "%Y-%m-%d")
        out = {
            "title": ev["title"],
            "all_day": True,
            "date": f"{start_d:%a %d %b %Y}",
        }
        if end and "T" not in end:
            last = datetime.strptime(end, "%Y-%m-%d").date()
            if (last - start_d.date()).days > 1:
                out["through"] = f"{last.fromordinal(last.toordinal() - 1):%a %d %b %Y}"
    else:
        out = {
            "title": ev["title"],
            "all_day": False,
            "start": _fmt_dt_ist(start) or start,
            "end": _fmt_dt_ist(end) or end,
        }
    if ev.get("location"):
        out["location"] = ev["location"]
    return out


def _sync_note(user_id: str) -> str | None:
    """Human-readable warning when the calendar data may be stale or never synced."""
    status = history_store.get_calendar_sync_status(user_id)
    if status is None:
        return "The calendar has never been synced for this user."
    last_ok = _fmt_dt_ist(status.get("last_success_at"))
    if status.get("last_error"):
        return (
            f"The latest calendar sync FAILED ({status['last_error']}). "
            f"Data shown is from the last successful sync: {last_ok or 'never'}. "
            f"It may be out of date — tell the user."
        )
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

def ask(user_id: str, question: str) -> str:
    """
    Answer a natural-language question for user_id using Gemini function calling.

    All tool calls within this invocation are closed over user_id — no call
    can reach another user's data.

    Args:
        user_id:  Discord snowflake (string) of the asking user.
        question: Raw question text from the Discord DM.

    Returns:
        A plain-text answer, grounded in the user's stored data.

    Raises:
        RuntimeError: if GEMINI_API_KEY is not configured.
        Exception:    Gemini API / network errors are not caught here; the
                      caller (discord_listener) handles them.
    """
    client = _get_client()

    user = user_registry.get_user(user_id)
    if user is None:
        # Should not normally happen — listener checks registration first,
        # but handle defensively.
        return (
            "I couldn't find your account in the registry. "
            "Please ask an admin to register you."
        )

    # ── Tool definitions as closures ──────────────────────────────────────────

    def get_stock_history(ticker: str, days: int = 7) -> str:
        """Retrieve stored stock-price history for a specific NSE ticker from the user's watchlist.

        Args:
            ticker: Exact ticker symbol as stored (e.g. 'RELIANCE.NS', 'INFY.NS', '^NSEI').
                    NSE stocks require the '.NS' suffix; indices use '^'.
            days: How many calendar days of history to retrieve. Default 7. Maximum 30.
        """
        ticker_val = ticker.strip()
        if not ticker_val:
            return "Error: 'ticker' is required."
        clamped_days = min(max(int(days), 1), 30)

        rows = history_store.get_stock_history(user_id, ticker_val, clamped_days)
        if not rows:
            return (
                f"No stock history found for '{ticker_val}' over the last {clamped_days} day(s) "
                f"for this user. Possible reasons: ticker is not in the watchlist, "
                f"the market was closed during this window, or the monitor hasn't run yet."
            )
        return json.dumps(rows, default=str)

    def get_latest_quote(ticker: str) -> str:
        """Fetch the latest available live quote for a ticker on the user's watchlist.

        Works even when the market is closed (then the price is the last close).
        Use this for "current price" questions or when stored history is empty.

        Args:
            ticker: Ticker symbol, e.g. 'RELIANCE' or 'RELIANCE.NS'.
        """
        norm = user_registry.normalise_ticker(ticker)
        if norm not in user.get("stock_watchlist", []):
            return f"'{norm}' is not in the user's watchlist."
        quotes = stocks_monitor.fetch_quotes([norm])
        quote = quotes.get(norm)
        if quote is None:
            return f"Could not fetch a live quote for '{norm}' right now."
        return json.dumps({
            "ticker": norm,
            **quote,
            "market_open": stocks_monitor.is_market_open(),
        })

    def get_watchlist() -> str:
        """Return the user's stock watchlist — the list of ticker symbols they are tracking.

        Call this first whenever the user asks about 'my stocks', 'my watchlist',
        a portfolio summary, or any question that requires knowing all tickers before
        fetching individual prices.
        """
        watchlist = user.get("stock_watchlist", [])
        if not watchlist:
            return "The user has no stocks in their watchlist yet."
        return json.dumps({"watchlist": watchlist, "count": len(watchlist)})

    def get_upcoming_events(days: int = 7) -> str:
        """Retrieve the user's upcoming calendar events (synced from Google Calendar), in IST.

        Args:
            days: Number of IST calendar days to cover, INCLUDING today. days=1 is the
                  rest of today, days=2 is today and tomorrow, days=7 is the next week.
                  Default 7. Capped at the user's configured calendar lookahead setting.
        """
        max_days = user.get("calendar_lookahead_days", 10)
        clamped_days = min(max(int(days), 1), max_days)

        events = history_store.get_upcoming_events(user_id, clamped_days)
        note = _sync_note(user_id)
        if not events:
            msg = (
                f"No events found in the next {clamped_days} calendar day(s) (today included) "
                f"in the stored calendar."
            )
            if note:
                msg += f" NOTE: {note}"
            else:
                msg += " The last sync succeeded, so the calendar appears to be empty for this window."
            return msg

        result: dict = {
            "window": f"next {clamped_days} calendar day(s), today included",
            "events": [_format_event(e) for e in events],
        }
        if note:
            result["warning"] = note
        return json.dumps(result)

    config = types.GenerateContentConfig(
        system_instruction=_system_instruction(datetime.now(timezone.utc)),
        tools=[get_watchlist, get_latest_quote, get_stock_history, get_upcoming_events],
        temperature=0.0,
    )

    logger.debug("ask() start — user=%s question=%.100s…", user_id, question)

    chat = client.chats.create(
        model=_MODEL_NAME,
        config=config,
    )

    response = chat.send_message(question)

    # Extract the final text answer
    try:
        text = response.text.strip()
    except Exception:
        logger.exception("Failed to extract text from final Gemini response")
        text = ""

    if not text:
        return (
            "I wasn't able to generate an answer. "
            "Please try rephrasing your question."
        )

    logger.debug("ask() done — user=%s answer_len=%d", user_id, len(text))
    return text


# ── __main__: interactive CLI test loop ───────────────────────────────────────

if __name__ == "__main__":
    import sys

    from personalassistant.core import bootstrap

    bootstrap.setup_logging(logging.DEBUG)
    bootstrap.init_storage()

    if not GEMINI_API_KEY:
        print("Error: GEMINI_API_KEY is not set. Add it to your .env file.")
        sys.exit(1)

    print("\n══════════════════════════════════════════════════")
    print("  PersonalAssistant — Chat Agent CLI Test")
    print("══════════════════════════════════════════════════\n")

    test_user_id = input("Discord user ID to test as (must be registered): ").strip()
    if not test_user_id:
        print("No user ID entered. Exiting.")
        sys.exit(0)

    user = user_registry.get_user(test_user_id)
    if user is None:
        print(f"\nUser '{test_user_id}' not found in the registry.")
        print("Register first:  python -m personalassistant.core.user_registry")
        sys.exit(1)

    print(f"\nChatting as : {user['display_name']} (ID: {test_user_id})")
    print(f"Watchlist   : {user['stock_watchlist'] or '(none)'}")
    print(f"Calendar    : {'linked' if user.get('google_token_path') else 'not linked'}")
    print(f"Model       : {_MODEL_NAME}")
    print("\nType a question and press Enter. Empty line or Ctrl+C to exit.\n")

    while True:
        try:
            question = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break

        if not question:
            print("Goodbye.")
            break

        try:
            answer = ask(test_user_id, question)
            print(f"\nAssistant: {answer}\n")
        except RuntimeError as exc:
            print(f"\nConfiguration error: {exc}\n")
            break
        except Exception as exc:
            print(f"\nError: {exc}\n")
