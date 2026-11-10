"""
stocks_monitor.py — Deterministic stock-price monitor. No LLM calls.

Entry point:
    run() — fetches quotes for every registered user's watchlist, logs
    to history_store, fires alerts via discord_notifier when a threshold
    is crossed, and resets alert state when price returns to normal range.

Market-hours gate:
    is_market_open() is called at the top of run(). If the market is
    closed (outside Mon–Fri 09:15–15:30 IST) the function logs and
    returns immediately without making any HTTP requests.

Adapter pattern:
    The Yahoo Finance chart API is never called inline inside run(). All
    fetches go through a module-level DirectAPIAdapter wrapping
    _raw_fetch_quotes(). To substitute a mock in tests, patch
    stocks_monitor._adapter.

Alert state machine (per user per ticker):
    - price crosses threshold (up or down) and direction differs from
      last alerted → send alert, persist new direction in state_store.
    - price still outside threshold in same direction → suppress (already
      alerted).
    - price returns inside ±threshold → clear state so next crossing fires
      a fresh alert.

Deduplication:
    All tickers across all users are collected and fetched once; results
    are then distributed per-user. Avoids hitting the quote API N×M times when
    multiple users watch the same ticker.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from personalassistant.core import discord_notifier, history_store, state_store, user_registry
from personalassistant.core.config import IST as _IST
from personalassistant.core.data_source_adapter import DirectAPIAdapter
from personalassistant.core.user_registry import normalise_ticker

logger = logging.getLogger(__name__)

# ── Market hours ──────────────────────────────────────────────────────────────

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


# ── Quote fetch callable (wrapped by adapter) ─────────────────────────────────

# Yahoo Finance chart API — direct HTTP (no yfinance dependency for this call)
_YF_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
_YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
_YF_PARAMS = {"range": "1d", "interval": "1m"}
_REQUEST_TIMEOUT = 15   # seconds per ticker
_MAX_WORKERS     = 8    # concurrent quote requests
_RETRIES         = 2    # extra attempts on timeout / network error / 5xx / 429


def _fetch_one(session: requests.Session, ticker: str) -> dict | None:
    """
    Fetch one ticker's quote. Returns {"price", "prev_close", "pct_change"}
    or None if unavailable. Never raises; transient failures are retried
    with a short backoff.
    """
    url = _YF_CHART_URL.format(ticker=ticker)
    for attempt in range(_RETRIES + 1):
        try:
            resp = session.get(url, params=_YF_PARAMS, timeout=_REQUEST_TIMEOUT)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < _RETRIES:
                time.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            chart = resp.json().get("chart", {})
            result = chart.get("result") or []
            if not result:
                logger.warning(
                    "No chart result for %s — %s — skipped",
                    ticker, chart.get("error") or "no result",
                )
                return None

            meta       = result[0]["meta"]
            price      = meta.get("regularMarketPrice")
            prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is None or not prev_close:
                logger.warning(
                    "Incomplete data for %s (price=%s, prev_close=%s) — skipped",
                    ticker, price, prev_close,
                )
                return None

            pct_change = (price - prev_close) / prev_close * 100
            logger.debug(
                "Quote OK  %s  price=%.2f  prev_close=%.2f  pct=%.2f%%",
                ticker, price, prev_close, pct_change,
            )
            return {
                "price":      round(price, 4),
                "prev_close": round(prev_close, 4),
                "pct_change": round(pct_change, 4),
            }
        except (requests.ConnectionError, requests.Timeout):
            if attempt < _RETRIES:
                time.sleep(1.5 * (attempt + 1))
                continue
            logger.warning("Network error/timeout fetching %s — skipped (will retry next cycle)", ticker)
        except requests.HTTPError as exc:
            logger.warning("HTTP %s fetching %s — skipped", exc.response.status_code, ticker)
            return None
        except Exception:
            logger.exception("Unhandled error fetching quote for %s — skipped", ticker)
            return None
    return None


def _raw_fetch_quotes(params: dict) -> dict:
    """
    Fetch latest prices for a list of tickers from the Yahoo Finance chart
    API (v8), concurrently over a shared HTTP session.

    params:  {"tickers": list[str]}  — normalised ticker symbols
    Returns: {ticker: {"price", "prev_close", "pct_change"}, ...}
             Tickers with unavailable/incomplete data are omitted; one
             failure never aborts the rest of the batch.
    """
    tickers = list(params["tickers"])
    if not tickers:
        return {}

    with requests.Session() as session:
        session.headers.update(_YF_HEADERS)
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(tickers))) as pool:
            quotes = list(pool.map(lambda t: _fetch_one(session, t), tickers))

    return {t: q for t, q in zip(tickers, quotes) if q is not None}


# Module-level adapter. In tests, patch this attribute to inject a mock:
#   stocks_monitor._adapter = DirectAPIAdapter(mock_fn)
_adapter = DirectAPIAdapter(_raw_fetch_quotes)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _collect_all_tickers(users: list[dict]) -> set[str]:
    """Union of every user's normalised watchlist — used to batch the fetch."""
    return {
        normalise_ticker(t)
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
    """Format the body text for a price-threshold alert Discord DM.

    Args:
        display_name: Human-readable name of the user (shown at the bottom).
        ticker:       Normalised ticker symbol (e.g. ``RELIANCE.NS``).
        price:        Current market price.
        prev_close:   Previous session's closing price.
        pct_change:   Percentage change relative to prev_close (signed).
        threshold:    The user's configured alert threshold (unsigned %).

    Returns:
        A multi-line string formatted for Discord, including price, prev close,
        percentage change, threshold, and the user's display name.
    """
    sign = "+" if pct_change >= 0 else ""
    return (
        f"Price: ₹{price:,.2f}  •  Prev close: ₹{prev_close:,.2f}\n"
        f"Change: **{sign}{pct_change:.2f}%** (threshold ±{threshold}%)\n"
        f"User: {display_name}"
    )


def _process_quote(user: dict, ticker: str, quote: dict, alert: bool = True) -> None:
    """
    Log one observation and, when alert=True, run the alert state machine
    for (user, ticker). With alert=False (off-hours snapshot) the price is
    stored but no alert is sent and no alert state is touched.
    """
    user_id    = user["user_id"]
    threshold  = user["alert_threshold_pct"]
    price      = quote["price"]
    prev_close = quote["prev_close"]
    pct_change = quote["pct_change"]

    history_store.log_stock_price(user_id, ticker, price, prev_close, pct_change)
    if not alert:
        return

    if pct_change >= threshold:
        direction: str | None = "up"
    elif pct_change <= -threshold:
        direction = "down"
    else:
        direction = None

    last_alerted = state_store.get_alert_direction(user_id, ticker)

    if direction is None:
        # Back in normal range — allow a fresh alert on the next crossing.
        if last_alerted is not None:
            state_store.set_alert_direction(user_id, ticker, None)
            logger.debug(
                "Alert state cleared (back in range) → user=%s ticker=%s pct=%.2f%%",
                user_id, ticker, pct_change,
            )
        return

    if direction == last_alerted:
        logger.debug(
            "Alert suppressed (already in %s state) → user=%s ticker=%s",
            last_alerted, user_id, ticker,
        )
        return

    discord_notifier.send_alert(
        user_id,
        title=f"{'📈' if direction == 'up' else '📉'} {ticker} price alert",
        body=_build_alert_message(
            user["display_name"], ticker, price, prev_close, pct_change, threshold
        ),
    )
    state_store.set_alert_direction(user_id, ticker, direction)
    logger.info(
        "Alert sent → user=%s ticker=%s pct=%.2f%% direction=%s",
        user_id, ticker, pct_change, direction,
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_quotes(tickers: list[str]) -> dict:
    """Public helper: latest quotes for normalised tickers (via the adapter)."""
    return _adapter.fetch({"tickers": sorted(set(tickers))})


def run(snapshot_only: bool = False) -> None:
    """
    One monitoring cycle. Called by APScheduler every 30 minutes.

    snapshot_only=True lets the cycle run while the market is closed: it
    stores the latest available price (e.g. the last close) so the chat
    agent has data, but sends no alerts. Used for the startup pass.

    Steps:
    1. Gate on market hours — return immediately if closed (unless snapshot_only).
    2. Load all registered users.
    3. Collect and deduplicate tickers across all watchlists.
    4. Fetch all quotes in one adapter call.
    5. For each user × ticker:
       a. Log price to history_store.
       b. Compare pct_change to the user's alert_threshold_pct.
       c. Fire a Discord alert (and update state_store) on a new crossing.
       d. Reset state_store when price returns inside the threshold band.

    Stale-history cleanup is a separate daily job scheduled in main.py.
    """
    market_open = is_market_open()
    if not market_open and not snapshot_only:
        logger.info("Market is closed — skipping stock monitor cycle.")
        return
    if not market_open:
        logger.info("Market is closed — taking a price snapshot (no alerts).")

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
    quotes: dict = fetch_quotes(sorted(all_tickers))

    for user in users:
        user_id = user["user_id"]

        for ticker in (normalise_ticker(t) for t in user.get("stock_watchlist", [])):
            quote = quotes.get(ticker)
            if quote is None:
                logger.warning(
                    "No quote for %s (user %s) — skipping this ticker",
                    ticker, user_id,
                )
                continue

            try:
                _process_quote(user, ticker, quote, alert=market_open)
            except Exception:
                logger.exception(
                    "Failed processing %s for user %s — continuing", ticker, user_id
                )


# ── __main__: manual test run ─────────────────────────────────────────────────

if __name__ == "__main__":
    from personalassistant.core import bootstrap

    bootstrap.setup_logging(logging.DEBUG)
    bootstrap.init_storage()

    open_status = is_market_open()
    now_ist = datetime.now(_IST)
    print(f"\nCurrent IST time : {now_ist.strftime('%A %Y-%m-%d %H:%M:%S')}")
    print(f"Market open      : {open_status}\n")

    if not open_status:
        print("Market is closed — run() will return early.")
        print("To force a fetch regardless of hours, call _raw_fetch_quotes() directly:")
        tickers = input("Enter tickers to test (comma-separated, blank to skip): ").strip()
        if tickers:
            test_tickers = [normalise_ticker(t) for t in tickers.split(",") if t.strip()]
            print(f"\nFetching: {test_tickers}")
            result = _raw_fetch_quotes({"tickers": test_tickers})
            for t, q in result.items():
                print(f"  {t}: ₹{q['price']:,.2f}  ({q['pct_change']:+.2f}%  prev ₹{q['prev_close']:,.2f})")
    else:
        print("Running full monitor cycle …")
        run()
        print("Done.")
