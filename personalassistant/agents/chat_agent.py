"""
chat_agent.py — LLM-driven reasoning component. The only agent in Phase 1
that makes Gemini API calls.

Public interface:
    ask(user_id: str, question: str) -> str

Grounding contract (non-negotiable):
    The model must never answer factual questions about a user's stock prices
    or calendar contents from general training knowledge. Every factual answer
    must come from a tool call against stored data. If a tool returns no data,
    the model says so plainly.

user_id threading:
    Both tool implementations are closed over the user_id that entered ask().
    It is structurally impossible for a tool call to query a different user's
    rows — the dispatch function passes user_id directly to history_store.

Tools exposed to the model:
    get_stock_history(ticker, days=7)  — queries history_store, capped at 30 days
    get_upcoming_events(days=7)        — queries history_store, capped at the
                                         user's calendar_lookahead_days setting

Function-calling loop:
    Send the user's question with tools attached → if the response contains
    function_call parts, execute them, send function_response parts back →
    repeat until the model returns a final text part (or MAX_TOOL_ROUNDS is
    hit as a safety limit).

SDK: google-generativeai (not google-genai).
Model: gemini-2.5-flash.
API key: GEMINI_API_KEY environment variable.
"""

import json
import logging
import os

import google as genai
from dotenv import load_dotenv

from personalassistant.core import history_store, user_registry

load_dotenv()

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
_MODEL_NAME = "gemini-2.5-flash"
_MAX_TOOL_ROUNDS = 10  # safety cap; prevents runaway loops

# Configure the SDK at import time if the key is available.
# ask() will raise clearly if the key is still missing at call time.
if _API_KEY:
    genai.configure(api_key=_API_KEY)

# ── System instruction ────────────────────────────────────────────────────────

_SYSTEM_INSTRUCTION = """
You are PersonalAssistant, a personal data assistant with access to a specific
user's stored stock-price history and calendar events via tools.

GROUNDING RULES — follow without exception:
1. Never answer questions about the user's actual stock prices, portfolio
   performance, or calendar events from your general training knowledge.
   Use the provided tools for every factual data question.
2. If a tool returns no data, tell the user plainly — do not guess, fabricate,
   or "helpfully" fill in numbers from memory.
3. Do not acknowledge any prices, percentages, or event details that you have
   not retrieved via a tool call in this conversation.

You MAY answer general-knowledge questions (e.g. "what is RSI?", "what does
P/E ratio mean?") from your training — but the moment a question touches the
user's actual data, call a tool first.

Response style:
- Be concise and conversational.
- Format Indian rupee amounts with the ₹ symbol and comma thousands separators.
- Show percentage changes with a + or − sign and two decimal places.
- If the user asks about a ticker not in their watchlist, say so and suggest
  they update their watchlist via the admin CLI.
""".strip()

# ── Tool declarations ─────────────────────────────────────────────────────────

_TOOL_DECLARATIONS = genai.protos.Tool(
    function_declarations=[
        genai.protos.FunctionDeclaration(
            name="get_stock_history",
            description=(
                "Retrieve stored stock-price history for a specific NSE ticker from the "
                "user's watchlist. Returns time-ordered records with price, previous close, "
                "and percentage change. Use this to answer any question about current price, "
                "recent moves, or trends. Returns empty if the ticker is not in the user's "
                "watchlist, the market was closed during the window, or no data has been "
                "collected yet."
            ),
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "ticker": genai.protos.Schema(
                        type=genai.protos.Type.STRING,
                        description=(
                            "Exact ticker symbol as stored (e.g. 'RELIANCE.NS', 'INFY.NS', "
                            "'^NSEI'). NSE stocks require the '.NS' suffix; indices use '^'."
                        ),
                    ),
                    "days": genai.protos.Schema(
                        type=genai.protos.Type.INTEGER,
                        description=(
                            "How many calendar days of history to retrieve. Default 7. "
                            "Maximum 30 (older data is purged automatically)."
                        ),
                    ),
                },
                required=["ticker"],
            ),
        ),
        genai.protos.FunctionDeclaration(
            name="get_upcoming_events",
            description=(
                "Retrieve the user's upcoming calendar events from stored data synced from "
                "Google Calendar. Returns events whose start time falls within the next "
                "'days' days, ordered chronologically. Use this for any question about the "
                "user's schedule, meetings, appointments, or upcoming events. Returns empty "
                "if the calendar has not been linked/synced, or no events exist in the window."
            ),
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "days": genai.protos.Schema(
                        type=genai.protos.Type.INTEGER,
                        description=(
                            "How many days ahead to look. Default 7. Capped at the user's "
                            "configured calendar lookahead setting."
                        ),
                    ),
                },
            ),
        ),
    ]
)

# ── Tool dispatch ─────────────────────────────────────────────────────────────

def _dispatch(name: str, args: dict, user_id: str, user: dict) -> str:
    """
    Execute a tool call on behalf of user_id.

    Results are returned as JSON strings (or a plain-language "no data" message)
    so Gemini can reason over structured output in its next turn.

    This function is the only place that touches history_store — all access
    is scoped to user_id.
    """
    if name == "get_stock_history":
        ticker = args.get("ticker", "").strip()
        if not ticker:
            return "Error: 'ticker' is required."
        days = min(max(int(args.get("days", 7)), 1), 30)

        rows = history_store.get_stock_history(user_id, ticker, days)
        if not rows:
            return (
                f"No stock history found for '{ticker}' over the last {days} day(s) "
                f"for this user. Possible reasons: ticker is not in the watchlist, "
                f"the market was closed during this window, or the monitor hasn't run yet."
            )
        return json.dumps(rows, default=str)

    elif name == "get_upcoming_events":
        max_days = user.get("calendar_lookahead_days", 10)
        days = min(max(int(args.get("days", 7)), 1), max_days)

        events = history_store.get_upcoming_events(user_id, days)
        if not events:
            return (
                f"No upcoming events found in the next {days} day(s) for this user. "
                f"Possible reasons: Google Calendar hasn't been linked yet, "
                f"the calendar monitor hasn't run yet, or there are no events in this window."
            )
        return json.dumps(events, default=str)

    else:
        logger.warning("Model requested unknown function: %s", name)
        return (
            f"Unknown function '{name}'. "
            "Available functions: get_stock_history, get_upcoming_events."
        )


# ── Main entry point ──────────────────────────────────────────────────────────

def ask(user_id: str, question: str) -> str:
    """
    Answer a natural-language question for user_id using Gemini function calling.

    The function-calling loop runs until the model returns a final text answer
    or MAX_TOOL_ROUNDS is exhausted.  All tool calls within this invocation are
    closed over user_id — no call can reach another user's data.

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
    if not _API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key."
        )

    user = user_registry.get_user(user_id)
    if user is None:
        # Should not normally happen — listener checks registration first,
        # but handle defensively.
        return (
            "I couldn't find your account in the registry. "
            "Please ask an admin to register you."
        )

    model = genai.GenerativeModel(
        model_name=_MODEL_NAME,
        tools=[_TOOL_DECLARATIONS],
        system_instruction=_SYSTEM_INSTRUCTION,
    )

    chat = model.start_chat()
    logger.debug("ask() start — user=%s question=%.100s…", user_id, question)

    response = chat.send_message(question)

    for round_num in range(_MAX_TOOL_ROUNDS):
        # Collect function_call parts (a single turn may contain several)
        function_calls = []
        try:
            for part in response.parts:
                if hasattr(part, "function_call") and part.function_call.name:
                    function_calls.append(part.function_call)
        except Exception:
            # response.parts may be unavailable on a blocked/empty response
            logger.warning("Could not read response parts on round %d", round_num)
            break

        if not function_calls:
            break  # final text answer is ready

        # Execute every tool call and package the results
        result_parts: list[genai.protos.Part] = []
        for fc in function_calls:
            fn_name = fc.name
            fn_args = dict(fc.args)
            logger.debug(
                "Tool call [round %d/%d]: %s(%s)",
                round_num + 1, _MAX_TOOL_ROUNDS, fn_name, fn_args,
            )
            result_str = _dispatch(fn_name, fn_args, user_id, user)
            logger.debug("Tool result: %.200s…", result_str)

            result_parts.append(
                genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=fn_name,
                        response={"result": result_str},
                    )
                )
            )

        response = chat.send_message(result_parts)

    else:
        logger.warning(
            "MAX_TOOL_ROUNDS (%d) reached for user %s — returning whatever text is available",
            _MAX_TOOL_ROUNDS, user_id,
        )

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
    from personalassistant.core import history_store as hs, state_store as ss

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    hs.init_db()
    ss.init_db()
    user_registry.init_db()

    if not _API_KEY:
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
