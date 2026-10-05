import sqlite3
import time
import re
import asyncio
import os
import json
import tempfile
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from telegram import ChatPermissions
from collections import defaultdict
from types import SimpleNamespace
from html import escape

from telegram import Update, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    filters,
)

# ============================================================
# BLACKBERRY BOT - UPDATED WITH PROTECTION EXEMPTION / FREE
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8368224966:AAHV53mCAyuTlNrCZ3vhOvSf71yImnIgDxI")
DB_NAME = os.environ.get("DB_NAME", "/tmp/blackberry_bot.db")

SPAM_LIMIT = 3
SPAM_WINDOW = 3

DEFAULT_WARN_LIMIT = 3
BOT_TIMEZONE = os.environ.get("BOT_TIMEZONE", "Asia/Kolkata")

db = sqlite3.connect(DB_NAME, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
cursor = db.cursor()

# ------------------------- DATABASE --------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS approved_users (
    chat_id INTEGER,
    user_id INTEGER,
    name TEXT,
    approved_by INTEGER,
    created_at INTEGER,
    PRIMARY KEY(chat_id, user_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS warnings (
    chat_id INTEGER,
    user_id INTEGER,
    warns INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id, user_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS tracked_members (
    chat_id INTEGER, user_id INTEGER, username TEXT, full_name TEXT,
    joined_at INTEGER, last_seen INTEGER,
    PRIMARY KEY(chat_id,user_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS blocked_users (
    chat_id INTEGER, user_id INTEGER, name TEXT, blocked_by INTEGER, created_at INTEGER,
    PRIMARY KEY(chat_id,user_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS join_requests (
    chat_id INTEGER, user_id INTEGER, name TEXT, username TEXT, created_at INTEGER,
    PRIMARY KEY(chat_id,user_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS admin_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    actor_id INTEGER,
    action TEXT,
    target_id INTEGER,
    target_name TEXT,
    created_at INTEGER
)
""")

spam_tracker = defaultdict(list)


# ------------------------- HELPERS ---------------------------

def commit():
    db.commit()

def log_action(chat_id, actor_id, action, target_id=None, target_name=None):
    cursor.execute(
        """INSERT INTO admin_logs
        (chat_id, actor_id, action, target_id, target_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (chat_id, actor_id, action, target_id, target_name, int(time.time())),
    )
    commit()

async def is_admin(update: Update, user_id=None):
    chat = update.effective_chat
    if not chat:
        return False

    if user_id is None:
        user_id = update.effective_user.id if update.effective_user else None

    if user_id is None:
        return False

    try:
        member = await chat.get_member(user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False

async def admin_required(update: Update):
    if not update.effective_chat or update.effective_chat.type not in ("group", "supergroup"):
        if update.message:
            await update.message.reply_text("❌ This command works in groups only.")
        return False

    if not await is_admin(update):
        if update.message:
            await update.message.reply_text("❌ This command is only for group admins.")
        return False

    return True

def is_approved(chat_id, user_id):
    cursor.execute("SELECT 1 FROM approved_users WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    return cursor.fetchone() is not None

def track_member(user, chat_id):
    if not user or user.is_bot:
        return
    now = int(time.time())
    cursor.execute(
        """INSERT INTO tracked_members(chat_id,user_id,username,full_name,joined_at,last_seen)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,
        full_name=excluded.full_name,last_seen=excluded.last_seen""",
        (chat_id, user.id, user.username, user.full_name, now, now),
    )
    commit()

def resolve_user_identifier(update, args=None):
    args = args or []
    if update.message and update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user

    if not args:
        return None

    raw = args[0].strip()
    if raw.startswith("@"):
        raw = raw[1:]

    if raw.lstrip("-").isdigit():
        uid = int(raw)
        cursor.execute("SELECT user_id,username,full_name FROM tracked_members WHERE chat_id=? AND user_id=?", (update.effective_chat.id, uid))
        row = cursor.fetchone()
        return SimpleNamespace(id=uid, username=row[1] if row else None, full_name=row[2] if row else str(uid), is_bot=False)

    cursor.execute("SELECT user_id,username,full_name FROM tracked_members WHERE chat_id=? AND lower(username)=lower(?) ORDER BY last_seen DESC LIMIT 1", (update.effective_chat.id, raw))
    row = cursor.fetchone()
    if row:
        return SimpleNamespace(id=row[0], username=row[1], full_name=row[2] or raw, is_bot=False)
    return None


# ------------------------- APPROVE & FREE COMMANDS -------------------------

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return
    u = resolve_user_identifier(update, context.args)
    if not u:
        return await update.message.reply_text("Usage: Reply to a user's message with /approve or use /approve @username / ID")

    cid = update.effective_chat.id
    cursor.execute(
        "INSERT OR REPLACE INTO approved_users VALUES(?,?,?,?,?)",
        (cid, u.id, u.full_name, update.effective_user.id, int(time.time()))
    )
    cursor.execute("DELETE FROM blocked_users WHERE chat_id=? AND user_id=?", (cid, u.id))
    commit()

    # Join request agar pending ho toh approve karo
    try:
        await update.effective_chat.approve_chat_join_request(u.id)
    except Exception:
        pass

    log_action(cid, update.effective_user.id, "approve_free", u.id, u.full_name)
    await update.message.reply_text(
        f"✅ **Approved & Freed:** {escape(u.full_name)} (`{u.id}`)\n"
        f"🛡️ Ye user ab Spam, Sticker aur Multi-message Protection se Exempted hai!",
        parse_mode="HTML"
    )

async def free_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Free aur approve same action execute karte hain
    await approve_command(update, context)

async def unapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return
    u = resolve_user_identifier(update, context.args)
    if not u:
        return await update.message.reply_text("Usage: Reply to a user's message with /unapprove or use /unapprove @username / ID")

    cid = update.effective_chat.id
    cursor.execute("DELETE FROM approved_users WHERE chat_id=? AND user_id=?", (cid, u.id))
    commit()

    log_action(cid, update.effective_user.id, "unapprove", u.id, u.full_name)
    await update.message.reply_text(
        f"🔒 **Protection Re-applied:** {escape(u.full_name)} (`{u.id}`)\n"
        f"Ab is user par saare group protection limits lagenge.",
        parse_mode="HTML"
    )


# ------------------------- BASIC COMMANDS -------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Blackberry Bot Active!")


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! Active.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Blackberry Bot Help\n\n"
        "/start - Start bot\n"
        "/help - Show help\n"
        "/ping - Check bot status"
    )

# ------------------------- MESSAGE & SPAM ENGINE -------------------------

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type not in ("group", "supergroup") or not user or user.is_bot:
        return

    track_member(user, chat.id)

    # Checking if user is Admin or Approved/Freed
    user_is_admin = await is_admin(update, user.id)
    user_is_approved = is_approved(chat.id, user.id)

    # Agar user Approved/Freed ya Admin hai, toh koi protection/spam check nahi chalega
    if user_is_admin or user_is_approved:
        return

    # Non-Approved members ke liye Spam Protection
    now = time.time()
    key = (chat.id, user.id)
    
    spam_tracker[key] = [t for t in spam_tracker[key] if now - t <= SPAM_WINDOW]
    spam_tracker[key].append(now)

    if len(spam_tracker[key]) >= SPAM_LIMIT:
        try:
            await message.delete()
        except Exception:
            pass
        spam_tracker[key] = []
        return


# ------------------------- MAIN FUNCTION -------------------------

def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN mandatory.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("free", free_command))
    app.add_handler(CommandHandler("unblock", free_command))
    app.add_handler(CommandHandler("unapprove", unapprove_command))

    app.add_handler(MessageHandler(~filters.COMMAND, message_handler))

    print("🤖 Blackberry Bot running with Protection Exemption system...")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()