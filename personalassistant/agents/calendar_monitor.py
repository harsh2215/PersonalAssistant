"""
calendar_monitor.py — Deterministic Google Calendar monitor. No LLM calls.

Entry points:
    run()          — fetches upcoming events for every linked user, atomically
                     replaces their calendar_events rows in history_store, and
                     cleans up stale alert state. Called every 3 hours.
    check_alerts() — queries stored events (no Google API calls) and sends a
                     Discord DM for any event starting within
                     CALENDAR_ALERT_LOOKAHEAD_MINUTES. Called every 15 minutes.

Credentials:
    Shared OAuth client secrets live at config/google_credentials.json
    (downloaded from Google Cloud Console, gitignored).
    Per-user token files are cached at data/google_token_<user_id>.json
    (also gitignored).

    _get_credentials(user) implements a three-step credential resolution:
      1. Load the cached token file if it exists.
      2. Silently refresh if expired and a refresh_token is present.
      3. Launch InstalledAppFlow.run_local_server() for first-time or
         revoked consent — this opens a browser and is the expected
         Phase 1 onboarding path. The resulting token is cached and the
         user registry is updated with its path.

Adapter pattern:
    The Google Calendar API client is never called inline in run(). All
    API calls go through a module-level DirectAPIAdapter wrapping
    _raw_fetch_calendar(). To substitute a mock in tests, patch
    calendar_monitor._adapter.

Alert deduplication:
    check_alerts() calls state_store.has_calendar_alert_been_sent() before
    sending and state_store.mark_calendar_alert_sent() after. Each event
    triggers at most one DM regardless of how many 15-minute ticks pass.
    All-day events (no time component in start_time) are skipped.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from personalassistant.core import discord_notifier, history_store, state_store, user_registry
from personalassistant.core.config import CALENDAR_ALERT_LOOKAHEAD_MINUTES, IST as _IST
from personalassistant.core.data_source_adapter import DirectAPIAdapter

logger = logging.getLogger(__name__)

# ── Paths & constants ─────────────────────────────────────────────────────────

_REPO_ROOT        = Path(__file__).resolve().parents[2]
_CREDENTIALS_FILE = _REPO_ROOT / "config" / "google_credentials.json"
_TOKEN_DIR        = _REPO_ROOT / "data"
_SCOPES           = ["https://www.googleapis.com/auth/calendar.readonly"]


def _token_path_for(user_id: str) -> Path:
    return _TOKEN_DIR / f"google_token_{user_id}.json"


# ── Credential management ─────────────────────────────────────────────────────

def _get_credentials(user: dict) -> Credentials:
    """
    Return valid OAuth credentials for a user, refreshing or re-authorising
    as necessary.

    Resolution order:
    1. If user["google_token_path"] points to an existing file, load it.
    2. If the loaded credentials are expired but carry a refresh_token,
       refresh them silently via the Google token endpoint.
    3. If no valid credentials are available, launch
       InstalledAppFlow.run_local_server() — this opens a browser tab and
       blocks until the user grants consent (Phase 1 manual onboarding).
    4. Persist any updated credentials back to the token file.
    5. If a new token file was created and the user registry didn't know
       its path yet, update the registry.

    Args:
        user: Full user dict from user_registry.get_user().

    Raises:
        FileNotFoundError: if _CREDENTIALS_FILE is absent (downloaded from
            Google Cloud Console → APIs & Services → Credentials).
    """
    user_id        = user["user_id"]
    token_path_str = user.get("google_token_path")
    token_path     = Path(token_path_str) if token_path_str else _token_path_for(user_id)

    creds: Credentials | None = None

    # ── Step 1: load cached token ─────────────────────────────────────────────
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), _SCOPES)
        except Exception:
            logger.warning(
                "Could not load token from %s — will re-authenticate", token_path
            )

    # ── Step 2: silent refresh ────────────────────────────────────────────────
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            logger.debug("Token refreshed silently for user %s", user_id)
        except Exception:
            logger.warning(
                "Token refresh failed for user %s — will re-authenticate", user_id
            )
            creds = None

    # ── Step 3: full consent flow ─────────────────────────────────────────────
    if not creds or not creds.valid:
        if not _CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                f"Google client credentials not found: {_CREDENTIALS_FILE}\n"
                "Download OAuth 2.0 client credentials from:\n"
                "  Google Cloud Console → APIs & Services → Credentials\n"
                "and save the file to config/google_credentials.json"
            )
        logger.info(
            "Starting OAuth consent flow for user %s — a browser will open", user_id
        )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(_CREDENTIALS_FILE), _SCOPES
        )
        creds = flow.run_local_server(port=0)
        logger.info("OAuth consent completed for user %s", user_id)

    # ── Step 4: persist credentials ───────────────────────────────────────────
    _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())

    # ── Step 5: update registry if token path was new ─────────────────────────
    if not token_path_str:
        try:
            user_registry.update_user(user_id, google_token_path=str(token_path))
            logger.info(
                "Registered token path for user %s: %s", user_id, token_path
            )
        except ValueError:
            logger.warning(
                "Could not update registry for user %s (user may have been removed)",
                user_id,
            )

    return creds


# ── Calendar API fetch (wrapped by adapter) ───────────────────────────────────

def _raw_fetch_calendar(params: dict) -> dict:
    """
    Underlying callable for the DirectAPIAdapter.

    params:
        {
          "credentials":    google.oauth2.credentials.Credentials,
          "lookahead_days": int,
        }

    Returns:
        {
          "events": [
            {
              "event_id":   str,   # Google Calendar event ID
              "title":      str,   # event summary (or "(No title)")
              "start_time": str,   # ISO-8601 dateTime or date string
              "end_time":   str,
              "location":   str | None,
            },
            ...
          ]
        }

    All-day events use "date" strings; timed events use "dateTime" strings.
    singleEvents=True expands recurring events into individual instances.
    """
    creds          = params["credentials"]
    lookahead_days = params["lookahead_days"]

    now      = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=lookahead_days)).isoformat()

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
        )
        .execute()
    )

    raw_events = events_result.get("items", [])
    events: list[dict] = []

    for ev in raw_events:
        start = ev.get("start", {})
        end   = ev.get("end",   {})
        events.append({
            "event_id":   ev["id"],
            "title":      ev.get("summary", "(No title)"),
            "start_time": start.get("dateTime") or start.get("date", ""),
            "end_time":   end.get("dateTime")   or end.get("date",   ""),
            "location":   ev.get("location"),
        })

    return {"events": events}


# Module-level adapter. In tests, patch this attribute to inject a mock:
#   calendar_monitor._adapter = DirectAPIAdapter(mock_fn)
_adapter = DirectAPIAdapter(_raw_fetch_calendar)


def _fetch_events(user: dict, lookahead_days: int) -> list[dict]:
    """
    Resolve credentials for the user, then fetch and return their upcoming
    calendar events via the adapter.

    Args:
        user:          Full user dict from user_registry.
        lookahead_days: Number of days ahead to query.

    Returns:
        List of normalised event dicts (event_id, title, start_time,
        end_time, location).  Empty list if no events found.
    """
    creds  = _get_credentials(user)
    result = _adapter.fetch({"credentials": creds, "lookahead_days": lookahead_days})
    return result["events"]


# ── Main entry point ──────────────────────────────────────────────────────────

def run() -> None:
    """
    One calendar monitoring cycle. Called by APScheduler every 3 hours.

    For each registered user:
    - If google_token_path is None or empty → log at DEBUG and skip.
      (User hasn't completed onboarding yet; not an error.)
    - Otherwise fetch their events using their calendar_lookahead_days and
      atomically replace their calendar_events rows in history_store.
    - Any per-user error (API, auth, network) is caught and logged so a
      single failure doesn't abort the whole cycle for other users.
    """
    users = user_registry.get_all_users()
    if not users:
        logger.info("No registered users — nothing to fetch.")
        return

    for user in users:
        user_id = user["user_id"]

        # Skip users who haven't linked their calendar yet
        if not user.get("google_token_path"):
            logger.debug(
                "User %s (%s) has no Google Calendar linked — skipping",
                user_id, user["display_name"],
            )
            continue

        lookahead = user["calendar_lookahead_days"]

        try:
            events = _fetch_events(user, lookahead)
            history_store.replace_calendar_events(user_id, events)
            # Keep alert-state table tidy: drop rows for events no longer upcoming.
            stale = state_store.clear_stale_calendar_alerts(
                user_id, {e["event_id"] for e in events}
            )
            if stale:
                logger.debug(
                    "Cleared %d stale calendar alert state row(s) for user %s",
                    stale, user_id,
                )
            logger.info(
                "Calendar synced for user %s (%s): %d event(s) over next %d day(s)",
                user_id, user["display_name"], len(events), lookahead,
            )
        except FileNotFoundError as exc:
            logger.error("Missing Google credentials file: %s", exc)
        except HttpError as exc:
            logger.error(
                "Google Calendar API error for user %s: HTTP %s — %s",
                user_id, exc.resp.status, exc.error_details,
            )
        except Exception:
            logger.exception(
                "Unexpected error syncing calendar for user %s", user_id
            )


# ── Alert helpers ─────────────────────────────────────────────────────────────

def _parse_event_dt(start_time_str: str) -> datetime | None:
    """
    Parse an ISO-8601 dateTime string into an aware datetime.

    Handles both ``+HH:MM`` offset notation and the ``Z`` suffix (UTC).
    Returns None if the string cannot be parsed (e.g. a bare date string
    without a time component, which should be pre-filtered by the caller).
    """
    try:
        # Python 3.10 fromisoformat() doesn't understand the trailing 'Z'
        s = start_time_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _minutes_until(start_time_str: str) -> int | None:
    """Return whole minutes from now until start_time_str, or None if unparseable."""
    dt = _parse_event_dt(start_time_str)
    if dt is None:
        return None
    delta = dt - datetime.now(timezone.utc)
    return int(delta.total_seconds() // 60)


def _build_calendar_alert_body(event: dict, minutes_away: int) -> str:
    """Format the DM body for a calendar event alert.

    Args:
        event:        Event dict from history_store (must have start_time, optionally location).
        minutes_away: Whole minutes until the event starts (may be 0 or negative).

    Returns:
        A short multi-line string ready for discord_notifier.send_alert().
    """
    if minutes_away <= 0:
        time_str = "starting now"
    elif minutes_away == 1:
        time_str = "in 1 minute"
    else:
        time_str = f"in {minutes_away} minutes"

    dt = _parse_event_dt(event["start_time"])
    if dt is not None:
        start_str = dt.astimezone(_IST).strftime("%I:%M %p IST").lstrip("0")
    else:
        start_str = event["start_time"]

    lines = [f"Starts {time_str} — at {start_str}"]
    if event.get("location"):
        lines.append(f"📍 {event['location']}")
    return "\n".join(lines)


def _check_user_alerts(user: dict) -> None:
    """Check and fire pending calendar alerts for a single user.

    Queries stored events starting within CALENDAR_ALERT_LOOKAHEAD_MINUTES,
    then for each one not yet alerted sends a Discord DM and marks it as sent.
    """
    user_id = user["user_id"]
    upcoming = history_store.get_events_starting_soon(
        user_id, CALENDAR_ALERT_LOOKAHEAD_MINUTES
    )
    if not upcoming:
        return

    for event in upcoming:
        event_id = event["event_id"]

        if state_store.has_calendar_alert_been_sent(user_id, event_id):
            logger.debug(
                "Calendar alert suppressed (already sent) → user=%s event=%s",
                user_id, event_id,
            )
            continue

        minutes_away = _minutes_until(event["start_time"])
        if minutes_away is None:
            # Unparseable start_time — skip silently (should not happen after LIKE filter)
            logger.warning(
                "Could not parse start_time %r for event %s — skipping alert",
                event["start_time"], event_id,
            )
            continue

        discord_notifier.send_alert(
            user_id,
            title=f"📅 {event['title']}",
            body=_build_calendar_alert_body(event, minutes_away),
        )
        state_store.mark_calendar_alert_sent(user_id, event_id)
        logger.info(
            "Calendar alert sent → user=%s event=%s starts_in=%dm",
            user_id, event_id, minutes_away,
        )


# ── Alert entry point ─────────────────────────────────────────────────────────

def check_alerts() -> None:
    """
    Calendar alert cycle — no Google API calls.

    Called by APScheduler every 15 minutes. For every registered user who
    has linked their Google Calendar, queries stored events starting within
    CALENDAR_ALERT_LOOKAHEAD_MINUTES and sends a Discord DM for any event
    not yet alerted. Each event generates at most one DM (tracked via
    state_store.calendar_alert_state).

    All-day events (bare date strings without a time component) are skipped.
    Per-user errors are caught and logged without aborting the cycle.
    """
    users = user_registry.get_all_users()
    if not users:
        return

    for user in users:
        user_id = user["user_id"]

        if not user.get("google_token_path"):
            continue   # calendar not linked

        try:
            _check_user_alerts(user)
        except Exception:
            logger.exception(
                "Calendar alert check failed for user %s — continuing", user_id
            )


# ── __main__: manual onboarding / test run ────────────────────────────────────

if __name__ == "__main__":
    import sys

    from personalassistant.core import bootstrap, history_store as hs, user_registry as ur

    bootstrap.setup_logging(logging.DEBUG)
    bootstrap.init_storage()

    print("\n=== Calendar Monitor — Manual Test / Onboarding ===\n")
    user_id = input("Discord user ID to test (must be registered): ").strip()
    if not user_id:
        print("No user ID entered. Exiting.")
        sys.exit(0)

    user = ur.get_user(user_id)
    if user is None:
        print(f"User '{user_id}' not found in registry. Register first with:")
        print("  python -m personalassistant.core.user_registry")
        sys.exit(1)

    print(f"\nUser    : {user['display_name']}")
    print(f"Lookahead: {user['calendar_lookahead_days']} days")

    if user.get("google_token_path"):
        print(f"Token   : {user['google_token_path']}")
    else:
        print("Token   : (none — OAuth consent flow will open in your browser)")

    print()

    try:
        events = _fetch_events(user, user["calendar_lookahead_days"])
        if not events:
            print("No upcoming events found in this window.")
        else:
            print(f"Found {len(events)} event(s):\n")
            for ev in events:
                print(f"  [{ev['start_time']}]  {ev['title']}")
                if ev.get("location"):
                    print(f"    📍 {ev['location']}")

        hs.replace_calendar_events(user_id, events)
        print(f"\n✓  Stored {len(events)} event(s) in history_store.")

    except FileNotFoundError as exc:
        print(f"\nError: {exc}")
        sys.exit(1)
    except Exception:
        logger.exception("Test run failed")
        sys.exit(1)
