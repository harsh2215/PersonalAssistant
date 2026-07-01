"""
stocks_monitor.py — Deterministic stock-price monitor. No LLM calls.

Entry point:
    run() — fetches quotes for every registered user's watchlist, logs
    to history_store, fires alerts via discord_notifier when a threshold
    is crossed, and resets alert state when price returns to normal range.

Market-hours gate:
    is_market_open() is called at the top of run(). If the market is
    closed (outside Mon–Fri 09:15–15:30 IST) the function logs and
    returns immediately without touching yfinance.

Adapter pattern:
    yfinance is never called inline inside run(). All fetches go through
    a module-level DirectAPIAdapter wrapping _raw_fetch_quotes(). To
    substitute a mock in tests, patch stocks_monitor._adapter.

Alert state machine (per user per ticker):
    - price crosses threshold (up or down) and direction differs from
      last alerted → send alert, persist new direction in state_store.
    - price still outside threshold in same direction → suppress (already
      alerted).
    - price returns inside ±threshold → clear state so next crossing fires
      a fresh alert.

Deduplication:
    All tickers across all users are collected and fetched once; results
    are then distributed per-user. Avoids hitting yfinance N×M times when
    multiple users watch the same ticker.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

from personalassistant.core import discord_notifier, history_store, state_store, user_registry
from personalassistant.core.data_source_adapter import DirectAPIAdapter

logger = logging.getLogger(__name__)

# ── Market hours ──────────────────────────────────────────────────────────────

_IST = ZoneInfo("Asia/Kolkata")
_OPEN_H,  _OPEN_M  = 9,  15
_CLOSE_H, _CLOSE_M = 15, 30


def is_market_open() -> bool:
    """
    Return True if the Indian stock market is currently open.

    Checks:
    - Weekday: Monday–Friday only (weekday() 0–4).
    - Time: 09:15–15:29 IST (upper bound exclusive so 15:30 is closed).

    Market holidays are not handled in Phase 1 — a holiday poll will
    simply find no price movement, which is acceptable.
    """
    now = datetime.now(_IST)
    if now.weekday() >= 5:          # Saturday = 5, Sunday = 6
        return False
    current_mins = now.hour * 60 + now.minute
    open_mins    = _OPEN_H  * 60 + _OPEN_M
    close_mins   = _CLOSE_H * 60 + _CLOSE_M
    return open_mins <= current_mins < close_mins


# ── yfinance fetch callable (wrapped by adapter) ───────────────────────────────

def _raw_fetch_quotes(params: dict) -> dict:
    """
    Fetch latest price data for a list of tickers via yfinance fast_info.

    params:
        {"tickers": list[str]}  — normalised ticker symbols

    Returns:
        {
          ticker: {
            "price":      float,
            "prev_close": float,
            "pct_change": float,   # positive = up vs. prev close
          },
          ...
        }
    Tickers for which data is unavailable or incomplete are omitted from
    the result (logged as warnings). Individual failures do not abort the
    rest of the batch.
    """
    results: dict = {}
    for ticker in params["tickers"]:
        try:
            fi         = yf.Ticker(ticker).fast_info
            price      = fi.last_price
            prev_close = fi.previous_close

            if price is None or prev_close is None or prev_close == 0:
                logger.warning(
                    "Incomplete data for %s (price=%s, prev_close=%s) — skipped",
                    ticker, price, prev_close,
                )
                continue

            pct_change = (price - prev_close) / prev_close * 100
            results[ticker] = {
                "price":      round(price, 4),
                "prev_close": round(prev_close, 4),
                "pct_change": round(pct_change, 4),
            }
        except Exception:
            logger.exception("Unhandled error fetching quote for %s — skipped", ticker)

    return results


# Module-level adapter. In tests, patch this attribute to inject a mock:
#   stocks_monitor._adapter = DirectAPIAdapter(mock_fn)
_adapter = DirectAPIAdapter(_raw_fetch_quotes)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _normalise_ticker(ticker: str) -> str:
    """Defensive normalisation — registry already normalises on write."""
    ticker = ticker.strip().upper()
    if ticker and "." not in ticker and "^" not in ticker:
        ticker += ".NS"
    return ticker


def _collect_all_tickers(users: list[dict]) -> set[str]:
    """Union of every user's normalised watchlist — used to batch the fetch."""
    return {
        _normalise_ticker(t)
        for user in users
        for t in user.get("stock_watchlist", [])
    }


def _build_alert_message(
    display_name: str,
    ticker: str,
    price: float,
    prev_close: float,
    pct_change: float,
    threshold: float,
) -> str:
    sign = "+" if pct_change >= 0 else ""
    return (
        f"Price: ₹{price:,.2f}  •  Prev close: ₹{prev_close:,.2f}\n"
        f"Change: **{sign}{pct_change:.2f}%** (threshold ±{threshold}%)\n"
        f"User: {display_name}"
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def run() -> None:
    """
    One monitoring cycle. Called by APScheduler every 30 minutes.

    Steps:
    1. Gate on market hours — return immediately if closed.
    2. Load all registered users.
    3. Collect and deduplicate tickers across all watchlists.
    4. Fetch all quotes in one adapter call.
    5. For each user × ticker:
       a. Log price to history_store.
       b. Compare pct_change to the user's alert_threshold_pct.
       c. Fire a Discord alert (and update state_store) on a new crossing.
       d. Reset state_store when price returns inside the threshold band.
    6. Run history_store.cleanup_old_stock_history() once for all users.
    """
    if not is_market_open():
        logger.info("Market is closed — skipping stock monitor cycle.")
        return

    users = user_registry.get_all_users()
    if not users:
        logger.info("No registered users — nothing to fetch.")
        return

    all_tickers = _collect_all_tickers(users)
    if not all_tickers:
        logger.info("No tickers in any watchlist — nothing to fetch.")
        return

    logger.info(
        "Fetching %d ticker(s): %s",
        len(all_tickers),
        ", ".join(sorted(all_tickers)),
    )
    quotes: dict = _adapter.fetch({"tickers": sorted(all_tickers)})

    for user in users:
        user_id      = user["user_id"]
        display_name = user["display_name"]
        threshold    = user["alert_threshold_pct"]
        watchlist    = [_normalise_ticker(t) for t in user.get("stock_watchlist", [])]

        for ticker in watchlist:
            quote = quotes.get(ticker)
            if quote is None:
                logger.warning(
                    "No quote for %s (user %s) — skipping this ticker",
                    ticker, user_id,
                )
                continue

            price      = quote["price"]
            prev_close = quote["prev_close"]
            pct_change = quote["pct_change"]

            # ── 5a. Persist observation ───────────────────────────────────
            history_store.log_stock_price(
                user_id, ticker, price, prev_close, pct_change
            )

            # ── 5b. Determine crossing direction ──────────────────────────
            if pct_change >= threshold:
                current_direction: str | None = "up"
            elif pct_change <= -threshold:
                current_direction = "down"
            else:
                current_direction = None

            last_alerted = state_store.get_alert_direction(user_id, ticker)

            # ── 5c. Alert on new crossing ─────────────────────────────────
            if current_direction is not None:
                if current_direction != last_alerted:
                    body = _build_alert_message(
                        display_name, ticker, price, prev_close,
                        pct_change, threshold,
                    )
                    discord_notifier.send_alert(
                        user_id,
                        title=f"{'📈' if current_direction == 'up' else '📉'} {ticker} price alert",
                        body=body,
                    )
                    state_store.set_alert_direction(user_id, ticker, current_direction)
                    logger.info(
                        "Alert sent → user=%s ticker=%s pct=%.2f%% direction=%s",
                        user_id, ticker, pct_change, current_direction,
                    )
                else:
                    logger.debug(
                        "Alert suppressed (already in %s state) → user=%s ticker=%s",
                        last_alerted, user_id, ticker,
                    )

            # ── 5d. Reset state when back in normal range ─────────────────
            else:
                if last_alerted is not None:
                    state_store.set_alert_direction(user_id, ticker, None)
                    logger.debug(
                        "Alert state cleared (back in range) → user=%s ticker=%s pct=%.2f%%",
                        user_id, ticker, pct_change,
                    )

    # ── 6. Cleanup across all users ───────────────────────────────────────────
    deleted = history_store.cleanup_old_stock_history(retain_days=30)
    if deleted:
        logger.info("Removed %d stale stock history row(s).", deleted)


# ── __main__: manual test run ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    from personalassistant.core import history_store as hs, state_store as ss, user_registry as ur

    hs.init_db()
    ss.init_db()
    ur.init_db()

    open_status = is_market_open()
    now_ist = datetime.now(_IST)
    print(f"\nCurrent IST time : {now_ist.strftime('%A %Y-%m-%d %H:%M:%S')}")
    print(f"Market open      : {open_status}\n")

    if not open_status:
        print("Market is closed — run() will return early.")
        print("To force a fetch regardless of hours, call _raw_fetch_quotes() directly:")
        tickers = input("Enter tickers to test (comma-separated, blank to skip): ").strip()
        if tickers:
            test_tickers = [_normalise_ticker(t) for t in tickers.split(",") if t.strip()]
            print(f"\nFetching: {test_tickers}")
            result = _raw_fetch_quotes({"tickers": test_tickers})
            for t, q in result.items():
                print(f"  {t}: ₹{q['price']:,.2f}  ({q['pct_change']:+.2f}%  prev ₹{q['prev_close']:,.2f})")
    else:
        print("Running full monitor cycle …")
        run()
        print("Done.")
