"""
discord_notifier.py — Outbound Discord alert DMs.

Exposes one public function:
    send_alert(user_id, title, body) -> None

Each call spins up a lightweight, ephemeral discord.Client, fetches the
target user, sends the DM, and immediately closes the connection.  No
persistent gateway connection is kept open.

Threading model:
    Monitors run in APScheduler background threads.  send_alert() is
    therefore called from a non-async context and uses asyncio.run() to
    create a fresh event loop for each send.  This is safe from any thread
    that doesn't already own a running event loop.

    Alternative (lower latency): once discord_listener is running, its
    bot.loop can be passed to a module-level `_bot_loop` variable and
    asyncio.run_coroutine_threadsafe used instead.  That avoids the second
    gateway connection at the cost of coupling the two modules.  Left as
    a future optimisation if alert volume grows.

Error handling:
    All delivery failures are caught, logged, and swallowed — the monitor
    job must not crash because a DM couldn't be delivered.
"""

import asyncio
import logging
import os

import discord
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_TOKEN: str = os.environ.get("DISCORD_BOT_TOKEN", "")

# Discord hard cap on a single message
_MESSAGE_LIMIT = 2000


def _format_message(title: str, body: str) -> str:
    return f"**{title}**\n{body}"


def _split(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    """Split text into chunks that fit within Discord's per-message limit."""
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


async def _send_dm_async(user_id: str, content: str) -> None:
    """
    Open an ephemeral gateway connection, send a DM to user_id, then close.

    Uses Intents.none() — the notifier does not need to receive any events.
    """
    if not _TOKEN:
        logger.error(
            "DISCORD_BOT_TOKEN is not set — cannot send DM to user %s", user_id
        )
        return

    # Intents.none() — we are only making outbound API calls, no event listening.
    intents = discord.Intents.none()
    client  = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            target = await client.fetch_user(int(user_id))
            dm     = await target.create_dm()
            chunks = _split(content)
            for chunk in chunks:
                await dm.send(chunk)
            logger.debug("DM delivered to user %s (%d chunk(s))", user_id, len(chunks))
        except discord.NotFound:
            logger.warning("Discord user %s not found — DM not delivered", user_id)
        except discord.Forbidden:
            logger.warning(
                "Cannot DM user %s (DMs disabled or bot blocked) — alert not delivered",
                user_id,
            )
        except Exception:
            logger.exception("Unexpected error delivering DM to user %s", user_id)
        finally:
            await client.close()

    await client.start(_TOKEN)


def send_alert(user_id: str, title: str, body: str) -> None:
    """
    Send a Discord DM alert to a registered user.

    Formats the message as:
        **<title>**
        <body>

    Guaranteed not to raise — all failures are logged and swallowed so
    the calling monitor job can continue.

    Args:
        user_id: The user's Discord snowflake ID (string).
        title:   Short alert headline (e.g. "📈 RELIANCE.NS alert").
        body:    Alert details.
    """
    content = _format_message(title, body)
    try:
        asyncio.run(_send_dm_async(user_id, content))
    except Exception:
        logger.exception(
            "send_alert failed for user %s — alert was not delivered", user_id
        )
