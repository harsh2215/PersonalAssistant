"""
discord_listener.py — Inbound Discord DM handler; routes messages to chat_agent.

This module owns the long-running discord.Client (bot) instance that:
  - Listens for direct messages.
  - Resolves the sender's Discord snowflake ID against user_registry.
  - Delegates the question to chat_agent.ask(user_id, text).
  - Sends the reply back to the same DM channel, chunked if needed.

IMPORTANT — Developer Portal setup required:
    Message Content Intent must be enabled in the Discord Developer Portal:
      Applications → <your bot> → Bot → Privileged Gateway Intents
      → "Message Content Intent" → toggle ON
    Without this, message.content will always be an empty string for
    messages not mentioning the bot, even if intents.message_content = True
    is set here in code.

Threading model:
    The bot runs its asyncio event loop on the main thread (or whichever
    thread calls client.run()).  chat_agent.ask() makes blocking Gemini
    API calls, so it is dispatched via asyncio.to_thread() to avoid
    stalling the event loop while waiting for Gemini.

Error handling:
    - Unknown user → brief "not registered" reply, no further processing.
    - chat_agent raises → log exception, send a generic error reply.
    - Empty messages (bot message, non-DM) → silently ignored.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import aiohttp
import discord

from personalassistant.agents import chat_agent
from personalassistant.core import user_registry
from personalassistant.core.config import DISCORD_BOT_TOKEN
from personalassistant.core.discord_notifier import split_message

logger = logging.getLogger(__name__)

# ── Intent setup ──────────────────────────────────────────────────────────────
# message_content=True is required to read DM text.
# ALSO enable "Message Content Intent" in the Discord Developer Portal —
# see the module docstring above.
intents = discord.Intents.default()
intents.message_content = True  # ← must also be ON in Developer Portal

client = discord.Client(intents=intents)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _reply(channel: discord.DMChannel, text: str) -> None:
    """
    Send a (possibly chunked) reply to a DM channel.

    Delivery failures (Discord unreachable, DMs closed, DNS outage, …) are
    logged as a single warning instead of escaping as an unhandled
    exception from the event handler.
    """
    try:
        for chunk in split_message(text):
            await channel.send(chunk)
    except (discord.DiscordException, aiohttp.ClientError, OSError) as exc:
        logger.warning("Could not deliver reply to channel %s: %s", channel.id, exc)


@asynccontextmanager
async def _typing(channel: discord.DMChannel):
    """
    Best-effort "typing…" indicator. It is purely cosmetic, so if Discord
    can't be reached we log and carry on rather than abort the request.
    """
    cm = channel.typing()
    try:
        await cm.__aenter__()
    except (discord.DiscordException, aiohttp.ClientError, OSError) as exc:
        logger.warning("Typing indicator unavailable: %s", exc)
        yield
        return
    try:
        yield
    finally:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass


# ── Event handlers ────────────────────────────────────────────────────────────

@client.event
async def on_ready() -> None:
    logger.info("Discord listener online as %s (id=%s)", client.user, client.user.id)


@client.event
async def on_message(message: discord.Message) -> None:
    """
    Handle incoming messages.

    Filters:
    1. Ignore any message from a bot (including ourselves).
    2. Ignore any message that is not a direct message.

    Processing:
    3. Resolve the sender's Discord ID via user_registry.
    4. If unregistered, reply with a short "not registered" note and stop.
    5. If registered, call chat_agent.ask() in a thread and send the reply.
    """
    # ── 1. Ignore bots ────────────────────────────────────────────────────────
    if message.author.bot:
        return

    # ── 2. Ignore non-DMs ────────────────────────────────────────────────────
    if not isinstance(message.channel, discord.DMChannel):
        return

    user_id = str(message.author.id)
    text    = message.content.strip()

    if not text:
        return  # empty message (e.g. only an attachment)

    logger.debug("DM received from %s: %.80s…", user_id, text)

    # ── 3. Resolve user in registry ───────────────────────────────────────────
    user = user_registry.get_user(user_id)

    # ── 4. Unregistered sender ────────────────────────────────────────────────
    if user is None:
        logger.info("Unregistered user %s sent a DM — replied with not-registered", user_id)
        await _reply(
            message.channel,
            "You're not registered with PersonalAssistant. "
            "Ask an admin to add you via the CLI:\n"
            "```\npython -m personalassistant.core.user_registry\n```",
        )
        return

    # ── 5. Route to chat_agent ────────────────────────────────────────────────
    async with _typing(message.channel):
        try:
            # chat_agent.ask() makes blocking HTTP calls to the Gemini API;
            # run it in a thread pool to keep the event loop responsive.
            reply: str = await asyncio.to_thread(chat_agent.ask, user_id, text)
        except Exception:
            logger.exception(
                "chat_agent.ask raised for user %s — sending error reply", user_id
            )
            reply = (
                "Sorry, something went wrong while processing your request. "
                "Please try again in a moment."
            )

    await _reply(message.channel, reply)
    logger.debug("Reply sent to user %s (%d chars)", user_id, len(reply))


# ── __main__: run the bot ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    from personalassistant.core import bootstrap

    bootstrap.setup_logging()
    bootstrap.init_storage()

    if not DISCORD_BOT_TOKEN:
        logger.error(
            "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
        sys.exit(1)

    logger.info("Starting PersonalAssistant Discord listener …")
    client.run(DISCORD_BOT_TOKEN, log_handler=None)
