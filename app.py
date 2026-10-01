import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parent
seed_db = ROOT / "blackberry_bot.db"
runtime_db = Path("/tmp/blackberry_bot.db")
runtime_db.parent.mkdir(parents=True, exist_ok=True)
if seed_db.exists() and not runtime_db.exists():
    shutil.copy2(seed_db, runtime_db)

os.environ["DB_NAME"] = str(runtime_db)

import bot
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ChatJoinRequestHandler,
    filters,
)

TOKEN = os.environ.get("BOT_TOKEN") or bot.BOT_TOKEN
if not TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")

tg_app = Application.builder().token(TOKEN).build()
bot._ACTIVE_BOT = tg_app.bot

COMMANDS = [
    ("start", bot.start), ("help", bot.help_command), ("setup", bot.setup_command),
    ("id", bot.id_command), ("info", bot.info_command), ("history", bot.history_command),
    ("ping", bot.ping_command), ("admins", bot.admins_command), ("stats", bot.stats_command),
    ("ban", bot.ban), ("unban", bot.unban), ("kick", bot.kick),
    ("mute", bot.mute), ("tmute", bot.tmute), ("unmute", bot.unmute),
    ("warn", bot.warn), ("warns", bot.warns), ("unwarn", bot.unwarn),
    ("del", bot.delete_command), ("purge", bot.purge), ("pin", bot.pin), ("unpin", bot.unpin),
    ("welcome", bot.welcome), ("setwelcome", bot.setwelcome),
    ("setgoodbye", bot.setgoodbye), ("goodbye", bot.goodbye_toggle),
    ("setrules", bot.setrules), ("rules", bot.rules_command),
    ("filter", bot.add_filter), ("filters", bot.list_filters), ("stop", bot.stop_filter),
    ("adultfilter", bot.adultfilter_command), ("antilink", bot.antilink),
    ("anticaps", bot.anticaps), ("badword", bot.badword_command),
    ("badwords", bot.badwords_command), ("lock", bot.lock), ("unlock", bot.unlock),
    ("rank", bot.rank_command), ("top", bot.rank_command), ("mystats", bot.mystats_command),
    ("resetrank", bot.reset_rank), ("adminlogs", bot.adminlogs),
    ("report", bot.report_command), ("approve", bot.approve_command),
    ("decline", bot.decline_command), ("block", bot.block_command),
    ("unblock", bot.unblock_command), ("blocklist", bot.blocklist_command),
    ("tagall", bot.tagall_command), ("tr", bot.translate_command),
    ("translate", bot.translate_command),
]

for name, func in COMMANDS:
    tg_app.add_handler(CommandHandler(name, func))

tg_app.add_handler(ChatMemberHandler(bot.new_member, ChatMemberHandler.CHAT_MEMBER))
tg_app.add_handler(ChatJoinRequestHandler(bot.join_request_handler))
tg_app.add_handler(MessageHandler(~filters.COMMAND, bot.message_handler))
tg_app.add_error_handler(bot.error_handler)

_initialized = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _initialized
    if not _initialized:
        await tg_app.initialize()
        _initialized = True
        vercel_url = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL")
        if vercel_url:
            if not vercel_url.startswith("http"):
                vercel_url = "https://" + vercel_url
            await tg_app.bot.set_webhook(url=f"{vercel_url}/api/webhook")
            print("Telegram webhook set:", f"{vercel_url}/api/webhook")
    yield

app = FastAPI(title="Blackberry Telegram Bot", lifespan=lifespan)

@app.get("/")
async def health():
    return {"ok": True, "service": "blackberry-bot"}

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    if update is not None:
        await tg_app.process_update(update)
    return JSONResponse({"ok": True})
