"""
discord_notifier.py — Outbound Discord alert DMs.

Exposes:
    send_alert(user_id, title, body) -> None
    split_message(text, limit)       -> list[str]

Delivery uses the Discord REST API directly (open DM channel, post
message) rather than spinning up a gateway client per alert. This is
synchronous, thread-safe (monitors run in APScheduler threads), needs no
event loop, and does not open a second gateway session next to the
listener's.

Error handling:
    All delivery failures are caught, logged, and swallowed — the monitor
    job must not crash because a DM couldn't be delivered.
"""

import logging

import requests

from personalassistant.core.config import DISCORD_BOT_TOKEN, DISCORD_MESSAGE_LIMIT

logger = logging.getLogger(__name__)

_API = "https://discord.com/api/v10"
_TIMEOUT = 15  # seconds


def split_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split text into chunks that fit within Discord's per-message limit."""
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def send_alert(user_id: str, title: str, body: str) -> None:
    """
    Send a Discord DM alert formatted as ``**<title>**\\n<body>``.

    Guaranteed not to raise — all failures are logged and swallowed so
    the calling monitor job can continue.
    """
    if not DISCORD_BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN is not set — cannot send DM to user %s", user_id)
        return

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    try:
        resp = requests.post(
            f"{_API}/users/@me/channels",
            json={"recipient_id": str(user_id)},
            headers=headers,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        channel_id = resp.json()["id"]

        for chunk in split_message(f"**{title}**\n{body}"):
            msg = requests.post(
                f"{_API}/channels/{channel_id}/messages",
                json={"content": chunk},
                headers=headers,
                timeout=_TIMEOUT,
            )
            msg.raise_for_status()
        logger.debug("DM delivered to user %s", user_id)
    except requests.HTTPError as exc:
        status = exc.response.status_code
        if status == 404:
            logger.warning("Discord user %s not found — DM not delivered", user_id)
        elif status == 403:
            logger.warning(
                "Cannot DM user %s (DMs disabled or bot blocked) — alert not delivered",
                user_id,
            )
        else:
            logger.error("Discord API HTTP %s sending DM to user %s", status, user_id)
    except Exception:
        logger.exception("send_alert failed for user %s — alert was not delivered", user_id)
