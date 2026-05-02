#!/usr/bin/env python3
"""
Telegram bot for posty.

Commands:
  /sync — trigger a sync immediately

Auto-sync runs every SYNC_INTERVAL_HOURS (default 6). Scheduled runs are
silent when there is no new mail; errors and new letters always produce a
notification.
"""

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from sync import run_sync  # noqa: E402 — must load .env first

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
SYNC_INTERVAL_HOURS = int(os.environ.get("SYNC_INTERVAL_HOURS", "6"))

_sync_lock = asyncio.Lock()


# ── Formatting ────────────────────────────────────────────────────────────────

def _format_result(new_letters: list[dict], errors: list[str], *, manual: bool) -> str | None:
    """Return a Telegram message string, or None if nothing worth reporting."""
    parts = []

    if new_letters:
        lines = [f"📬 {len(new_letters)} new letter(s):"]
        for letter in new_letters:
            title = letter.get("title") or letter["id"][:8]
            sender = f" — {letter['sender']}" if letter.get("sender") else ""
            date = f" ({letter['date']})" if letter.get("date") else ""
            lines.append(f"  • {title}{sender}{date}")
        parts.append("\n".join(lines))

    if errors:
        lines = [f"⚠️ {len(errors)} error(s):"]
        for e in errors:
            lines.append(f"  • {e}")
        parts.append("\n".join(lines))

    if not parts:
        return "✅ No new mail." if manual else None

    return "\n\n".join(parts)


# ── Sync runner ───────────────────────────────────────────────────────────────

async def _do_sync(*, manual: bool) -> str | None:
    """Run sync in a thread. Returns formatted message or None (scheduled, no news)."""
    loop = asyncio.get_running_loop()
    try:
        new_letters, errors = await loop.run_in_executor(None, run_sync)
    except FileNotFoundError:
        return "❌ No session file. Run `python sync.py login` on a machine with a display."
    except RuntimeError as e:
        if "expired" in str(e).lower():
            return "⚠️ ePost session expired. Run `python sync.py login` to re-authenticate."
        return f"❌ Sync failed: {e}"
    except Exception as e:
        return f"❌ Unexpected error: {type(e).__name__}: {e}"

    return _format_result(new_letters, errors, manual=manual)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def handle_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return

    if _sync_lock.locked():
        await update.message.reply_text("⏳ Sync already in progress…")
        return

    async with _sync_lock:
        await update.message.reply_text("🔄 Syncing…")
        msg = await _do_sync(manual=True)
        await update.message.reply_text(msg)


async def scheduled_sync(context: ContextTypes.DEFAULT_TYPE):
    if _sync_lock.locked():
        return

    async with _sync_lock:
        msg = await _do_sync(manual=False)
        if msg:
            await context.bot.send_message(chat_id=CHAT_ID, text=msg)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("sync", handle_sync))

    if SYNC_INTERVAL_HOURS > 0:
        app.job_queue.run_repeating(
            scheduled_sync,
            interval=SYNC_INTERVAL_HOURS * 3600,
            first=30,  # short delay on startup so the bot is ready before first run
        )
        print(f"Auto-sync every {SYNC_INTERVAL_HOURS}h.")
    else:
        print("Auto-sync disabled (SYNC_INTERVAL_HOURS=0).")

    print(f"Bot running. Listening for /sync from chat {CHAT_ID}.")
    app.run_polling()


if __name__ == "__main__":
    main()
