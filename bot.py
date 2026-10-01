import sqlite3
import time
import re
import asyncio
import os
import json

from telegram import ChatPermissions
from collections import defaultdict
from types import SimpleNamespace
from html import escape
from urllib.parse import quote
from urllib.request import urlopen, Request


from telegram import Update,ChatPermissions
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
# BLACKBERRY BOT - FINAL WORKING GROUP VERSION
# ============================================================
# 1) Put your BotFather token below.
# 2) Add bot to the group as ADMIN.
# 3) For welcome/member events, allow the bot to see group events.
# 4) Install: pip install -U python-telegram-bot
# ============================================================

BOT_TOKEN = os.environ.get("8368224966:AAHV53mCAyuTlNrCZ3vhOvSf71yImnIgDxI", "")
DB_NAME = os.environ.get("DB_NAME", "/tmp/blackberry_bot.db")

SPAM_LIMIT = 6
SPAM_WINDOW = 10
DEFAULT_WARN_LIMIT = 3
HISTORY_LIMIT_DEFAULT = 20
MAX_HISTORY_SAVE = 5000
MAX_TAGS = 50
TRANSLATE_TIMEOUT = 15

LINK_RE = re.compile(
    r"(?:https?://|www\.|t\.me/|telegram\.me/|@[A-Za-z0-9_]{5,})",
    re.I,
)

db = sqlite3.connect(DB_NAME, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
cursor = db.cursor()

# ------------------------- DATABASE --------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS warnings (
    chat_id INTEGER,
    user_id INTEGER,
    warns INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id, user_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS filters (
    chat_id INTEGER,
    keyword TEXT,
    response TEXT,
    PRIMARY KEY(chat_id, keyword)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS media_filters (
    chat_id INTEGER,
    keyword TEXT,
    media_type TEXT,
    file_id TEXT,
    caption TEXT,
    PRIMARY KEY(chat_id, keyword)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS settings (
    chat_id INTEGER PRIMARY KEY,
    welcome TEXT DEFAULT 'Welcome {mention} to {chat}! 👋'
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS goodbye_settings (
    chat_id INTEGER PRIMARY KEY,
    text TEXT DEFAULT 'Goodbye {name}! 👋'
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS welcome_state (
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER DEFAULT 1,
    goodbye INTEGER DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS rules (
    chat_id INTEGER PRIMARY KEY,
    text TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS badwords (
    chat_id INTEGER,
    word TEXT,
    PRIMARY KEY(chat_id, word)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS mod_settings (
    chat_id INTEGER PRIMARY KEY,
    anti_link INTEGER DEFAULT 0,
    anti_caps INTEGER DEFAULT 0,
    warn_limit INTEGER DEFAULT 3
)
""")

# Optional adult-filter setting for existing databases.
try:
    cursor.execute("ALTER TABLE mod_settings ADD COLUMN adult_filter INTEGER DEFAULT 1")
    db.commit()
except sqlite3.OperationalError:
    pass

cursor.execute("""
CREATE TABLE IF NOT EXISTS ranks (
    chat_id INTEGER,
    user_id INTEGER,
    name TEXT,
    messages INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id, user_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS user_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    full_name TEXT,
    message_id INTEGER,
    message_type TEXT,
    content TEXT,
    media_file_id TEXT,
    reply_to_message_id INTEGER,
    created_at INTEGER
)
""")

cursor.execute("""
CREATE INDEX IF NOT EXISTS idx_history_chat_user
ON user_history(chat_id, user_id, id DESC)
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

cursor.execute("""CREATE TABLE IF NOT EXISTS tracked_members (
    chat_id INTEGER, user_id INTEGER, username TEXT, full_name TEXT,
    joined_at INTEGER, last_seen INTEGER,
    PRIMARY KEY(chat_id,user_id)
)
""")
cursor.execute("""CREATE TABLE IF NOT EXISTS user_identity_history (
    chat_id INTEGER, user_id INTEGER, username TEXT, full_name TEXT,
    first_seen INTEGER, last_seen INTEGER,
    PRIMARY KEY(chat_id,user_id,username,full_name)
)
""")

cursor.execute("""CREATE TABLE IF NOT EXISTS blocked_users (
    chat_id INTEGER, user_id INTEGER, name TEXT, blocked_by INTEGER, created_at INTEGER,
    PRIMARY KEY(chat_id,user_id)
)
""")
cursor.execute("""CREATE TABLE IF NOT EXISTS join_requests (
    chat_id INTEGER, user_id INTEGER, name TEXT, username TEXT, created_at INTEGER,
    PRIMARY KEY(chat_id,user_id)
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
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def admin_required(update: Update):
    if not update.effective_chat or update.effective_chat.type not in (
        "group", "supergroup"
    ):
        if update.message:
            await update.message.reply_text("❌ This command works in groups only.")
        return False

    if not await is_admin(update):
        if update.message:
            await update.message.reply_text(
                "❌ This command is only for group admins."
            )
        return False

    return True


def target(update: Update):
    if update.message and update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    return None


def resolve_user_identifier(update, args=None):
    """Resolve reply target, numeric ID, or tracked @username."""
    args = args or []
    u = target(update)
    if u:
        return u
    if not args:
        return None
    raw = args[0].strip()
    if raw.startswith("@"):
        raw = raw[1:]
    try:
        uid = int(raw)
        cursor.execute("SELECT user_id,username,full_name FROM tracked_members WHERE chat_id=? AND user_id=?", (update.effective_chat.id, uid))
        row = cursor.fetchone()
        return SimpleNamespace(id=uid, username=row[1] if row else None, full_name=row[2] if row else str(uid), is_bot=False)
    except ValueError:
        pass
    cursor.execute(
        "SELECT user_id,username,full_name FROM tracked_members WHERE chat_id=? AND lower(username)=lower(?) ORDER BY last_seen DESC LIMIT 1",
        (update.effective_chat.id, raw),
    )
    row = cursor.fetchone()
    if row:
        return SimpleNamespace(id=row[0], username=row[1], full_name=row[2] or raw, is_bot=False)
    return None


def user_label(user):
    if not user:
        return "Unknown"
    return user.full_name or user.username or str(user.id)


def format_template(template, user, chat):
    mention = (
        f'<a href="tg://user?id={user.id}">'
        f'{user.full_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")}'
        f'</a>'
    )
    return (
        template
        .replace("{name}", user.full_name)
        .replace("{mention}", mention)
        .replace("{username}", f"@{user.username}" if user.username else "")
        .replace("{id}", str(user.id))
        .replace("{chat}", chat.title or "the group")
    )


def get_message_type(message):
    if message.text:
        return "text"
    if message.photo:
        return "photo"
    if message.sticker:
        return "sticker"
    if message.video:
        return "video"
    if message.document:
        return "document"
    if message.audio:
        return "audio"
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.animation:
        return "animation"
    if message.contact:
        return "contact"
    if message.location:
        return "location"
    if message.poll:
        return "poll"
    return "other"


def get_message_content(message):
    if message.text:
        return message.text[:4000]
    if message.caption:
        return message.caption[:4000]
    if message.sticker:
        return f"sticker: {message.sticker.emoji or ''}"
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.document:
        return f"document: {message.document.file_name or ''}"
    if message.audio:
        return f"audio: {message.audio.title or ''}"
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.animation:
        return "animation"
    if message.contact:
        return f"contact: {message.contact.phone_number}"
    if message.location:
        return "location"
    if message.poll:
        return f"poll: {message.poll.question}"
    return "other"


def get_media_file_id(message):
    if message.photo:
        return message.photo[-1].file_id
    if message.sticker:
        return message.sticker.file_id
    if message.video:
        return message.video.file_id
    if message.document:
        return message.document.file_id
    if message.audio:
        return message.audio.file_id
    if message.voice:
        return message.voice.file_id
    if message.video_note:
        return message.video_note.file_id
    if message.animation:
        return message.animation.file_id
    return None


def save_history(message):
    user = message.from_user
    chat = message.chat

    if not user or user.is_bot or not chat:
        return

    content = get_message_content(message)
    mtype = get_message_type(message)
    media_file_id = get_media_file_id(message)
    reply_to_id = message.reply_to_message.message_id if message.reply_to_message else None

    cursor.execute(
        """INSERT INTO user_history
        (chat_id, user_id, username, full_name, message_id,
         message_type, content, media_file_id, reply_to_message_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            chat.id, user.id, user.username, user.full_name, message.message_id,
            mtype, content, media_file_id, reply_to_id, int(time.time()),
        ),
    )

    # Keep the database from growing forever.
    cursor.execute(
        """DELETE FROM user_history
        WHERE chat_id=? AND user_id=?
        AND id NOT IN (
            SELECT id FROM user_history
            WHERE chat_id=? AND user_id=?
            ORDER BY id DESC LIMIT ?
        )""",
        (chat.id, user.id, chat.id, user.id, MAX_HISTORY_SAVE),
    )
    commit()


def get_warn_limit(chat_id):
    cursor.execute(
        "SELECT warn_limit FROM mod_settings WHERE chat_id=?",
        (chat_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else DEFAULT_WARN_LIMIT


def media_from_message(message):
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.sticker:
        return "sticker", message.sticker.file_id
    if message.video:
        return "video", message.video.file_id
    if message.document:
        return "document", message.document.file_id
    if message.audio:
        return "audio", message.audio.file_id
    if message.voice:
        return "voice", message.voice.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    if message.animation:
        return "animation", message.animation.file_id
    return None, None


async def send_media_filter(message, media_type, file_id, caption=""):
    try:
        if media_type == "photo":
            await message.reply_photo(file_id, caption=caption or None)
        elif media_type == "sticker":
            await message.reply_sticker(file_id)
        elif media_type == "video":
            await message.reply_video(file_id, caption=caption or None)
        elif media_type == "document":
            await message.reply_document(file_id, caption=caption or None)
        elif media_type == "audio":
            await message.reply_audio(file_id, caption=caption or None)
        elif media_type == "voice":
            await message.reply_voice(file_id, caption=caption or None)
        elif media_type == "video_note":
            await message.reply_video_note(file_id)
        elif media_type == "animation":
            await message.reply_animation(file_id, caption=caption or None)
    except Exception as e:
        print("MEDIA FILTER ERROR:", e)



# ------------------------- MEMBER / SAFETY HELPERS -------------------------

ADULT_WORDS = {
    "porn", "xxx", "pornhub", "xvideos", "sexcam", "nudes", "nude",
    "onlyfans", "hentai", "nsfw", "sex", "blowjob", "cum", "anal",
    "18+", "adult", "explicit", "xxx18", "pornographic", "sexual",
}


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
    # Every distinct identity seen for this ID is retained.
    cursor.execute(
        """INSERT OR IGNORE INTO user_identity_history
        (chat_id,user_id,username,full_name,first_seen,last_seen)
        VALUES(?,?,?,?,?,?)""",
        (chat_id, user.id, user.username, user.full_name, now, now),
    )
    cursor.execute(
        """UPDATE user_identity_history SET last_seen=?
        WHERE chat_id=? AND user_id=? AND username IS ? AND full_name IS ?""",
        (now, chat_id, user.id, user.username, user.full_name),
    )
    commit()


def is_blocked(chat_id, user_id):
    cursor.execute("SELECT 1 FROM blocked_users WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    return cursor.fetchone() is not None


def looks_adult_text(text):
    low=(text or "").lower()
    return any(re.search(r"(?<![a-z])"+re.escape(w)+r"(?![a-z])", low) for w in ADULT_WORDS)


def media_looks_adult(message):
    text = (message.caption or "")
    if looks_adult_text(text):
        return True
    st = message.sticker
    if st:
        if st.set_name and looks_adult_text(st.set_name.replace("_", " ")):
            return True
        if st.emoji and st.emoji in {"🔞", "🔞️"}:
            return True
    for obj in (message.document, message.animation, message.video):
        if obj:
            name = getattr(obj, "file_name", None) or getattr(obj, "file_unique_id", "")
            if looks_adult_text(name):
                return True
    return False


async def notify_admins(chat, text):
    try:
        admins = await chat.get_administrators()
        for a in admins:
            if not a.user.is_bot:
                try:
                    await context_bot_send(chat, a.user.id, text)
                except Exception:
                    pass
    except Exception:
        pass


async def context_bot_send(chat, user_id, text):
    # Kept as a small helper so admin notifications use the bot's chat context.
    # Telegram Bot API cannot DM a user who has never started the bot; in that case it fails safely.
    return await _ACTIVE_BOT.send_message(chat_id=user_id, text=text, parse_mode="HTML")


_ACTIVE_BOT = None


# ------------------------- TRANSLATION ----------------------

# Common language names/aliases -> ISO-639-1 codes accepted by MyMemory.
LANGUAGE_CODES = {
    "english":"en", "en":"en", "hindi":"hi", "hi":"hi", "हिंदी":"hi",
    "urdu":"ur", "ur":"ur", "bengali":"bn", "bangla":"bn", "bn":"bn", "বাংলা":"bn",
    "tamil":"ta", "ta":"ta", "தமிழ்":"ta", "telugu":"te", "te":"te", "తెలుగు":"te",
    "marathi":"mr", "mr":"mr", "मराठी":"mr", "gujarati":"gu", "gu":"gu", "ગુજરાતી":"gu",
    "punjabi":"pa", "pa":"pa", "ਪੰਜਾਬੀ":"pa", "kannada":"kn", "kn":"kn", "ಕನ್ನಡ":"kn",
    "malayalam":"ml", "ml":"ml", "മലയാളം":"ml", "nepali":"ne", "ne":"ne", "नेपाली":"ne",
    "sinhala":"si", "si":"si", "arabic":"ar", "ar":"ar", "persian":"fa", "farsi":"fa", "fa":"fa",
    "turkish":"tr", "tr":"tr", "french":"fr", "fr":"fr", "german":"de", "de":"de",
    "spanish":"es", "es":"es", "italian":"it", "it":"it", "portuguese":"pt", "pt":"pt",
    "russian":"ru", "ru":"ru", "ukrainian":"uk", "uk":"uk", "dutch":"nl", "nl":"nl",
    "polish":"pl", "pl":"pl", "romanian":"ro", "ro":"ro", "greek":"el", "el":"el",
    "hebrew":"he", "he":"he", "thai":"th", "th":"th", "vietnamese":"vi", "vi":"vi",
    "indonesian":"id", "id":"id", "malay":"ms", "ms":"ms", "chinese":"zh-CN", "zh":"zh-CN",
    "zh-cn":"zh-CN", "japanese":"ja", "ja":"ja", "korean":"ko", "ko":"ko",
}


def normalize_language(value):
    """Return a real MyMemory language code, never AUTO."""
    value = (value or "").strip().lower()
    return LANGUAGE_CODES.get(value, value)


def detect_language(text):
    """Detect the source language. The translator must receive a real ISO code."""
    text = (text or "").strip()
    if not text:
        return "en"
    try:
        from langdetect import detect
        detected = detect(text)
        # langdetect may return zh-cn/zh-tw; MyMemory accepts these regional codes.
        if detected == "zh-cn":
            return "zh-CN"
        if detected == "zh-tw":
            return "zh-TW"
        if re.fullmatch(r"[a-z]{2}", detected or ""):
            return detected
    except Exception:
        pass

    # Script fallback when langdetect is unavailable.
    scripts = [
        (r"[\u0900-\u097F]", "hi"), (r"[\u0980-\u09FF]", "bn"),
        (r"[\u0B80-\u0BFF]", "ta"), (r"[\u0C00-\u0C7F]", "te"),
        (r"[\u0C80-\u0CFF]", "kn"), (r"[\u0D00-\u0D7F]", "ml"),
        (r"[\u0A80-\u0AFF]", "gu"), (r"[\u0A00-\u0A7F]", "pa"),
        (r"[\u0600-\u06FF]", "ar"), (r"[\u0750-\u077F]", "ar"),
        (r"[\u0590-\u05FF]", "he"), (r"[\u0E00-\u0E7F]", "th"),
        (r"[\u3040-\u30FF]", "ja"), (r"[\uAC00-\uD7AF]", "ko"),
        (r"[\u4E00-\u9FFF]", "zh-CN"), (r"[\u0D80-\u0DFF]", "si"),
    ]
    for pattern, code in scripts:
        if re.search(pattern, text):
            return code
    return "en"


async def translate_text(text, target_lang, source_lang=None):
    target_lang = normalize_language(target_lang)
    if not re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", target_lang or ""):
        raise ValueError("Unsupported target language code")

    source_lang = normalize_language(source_lang) if source_lang else detect_language(text)
    # IMPORTANT: MyMemory rejects AUTO. Always send the detected real source code.
    if not re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", source_lang or ""):
        source_lang = "en"

    # Translating to the same language is unnecessary and avoids odd API responses.
    if source_lang.lower() == target_lang.lower():
        return text

    q = quote((text or "")[:4000])
    pair = quote(f"{source_lang}|{target_lang}")
    url = f"https://api.mymemory.translated.net/get?q={q}&langpair={pair}"

    def fetch():
        req = Request(url, headers={"User-Agent": "BlackberryBot/1.0"})
        with urlopen(req, timeout=TRANSLATE_TIMEOUT) as r:
            return r.read().decode("utf-8")

    data = json.loads(await asyncio.to_thread(fetch))
    response = data.get("responseData") or {}
    translated = response.get("translatedText")
    if translated:
        return translated
    details = data.get("responseDetails") or data.get("responseStatus") or "Unknown translation error"
    raise RuntimeError(str(details))


async def translate_command(update, context):
    if not update.message:
        return

    reply = update.message.reply_to_message
    args = list(context.args or [])
    if not args and not reply:
        return await update.message.reply_text(
            "🌐 Usage:\n/tr hi <text>\n\nOr reply to ANY message and use:\n/tr hi\n/tr hindi\n/tr english\n\nSupported examples: en, hi, ur, bn, ta, te, mr, gu, pa, kn, ml, ar, fr, de, es, ru, zh, ja, ko."
        )

    target_raw = args[0] if args else "en"
    target_lang = normalize_language(target_raw)
    if not re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", target_lang or ""):
        return await update.message.reply_text(
            "❌ Invalid language. Use a code/name like: en, hi, hindi, ur, bn, ta, te, fr, de, es, ru, zh, ja, ko."
        )

    # Reply mode: /tr hi translates the replied message itself.
    # /tr hi some replacement text keeps the explicit text instead.
    if reply and len(args) == 1:
        source = (reply.text or reply.caption or "").strip()
    else:
        source = " ".join(args[1:]).strip()
        if not source and reply:
            source = (reply.text or reply.caption or "").strip()

    if not source:
        return await update.message.reply_text(
            "❌ Reply to a text/caption message, or write: /tr hi Hello"
        )

    try:
        detected = detect_language(source)
        out = await translate_text(source, target_lang, detected)
        await update.message.reply_text(
            f"🌐 <b>{escape(detected)}</b> → <b>{escape(target_lang)}</b>\n\n{escape(out)}",
            parse_mode="HTML",
            allow_sending_without_reply=True,
        )
    except Exception as e:
        print("TRANSLATION ERROR:", repr(e))
        await update.message.reply_text(
            "❌ Translation failed. Check the internet connection/API and try again."
        )


async def report_command(update, context):
    if not update.message or not update.message.reply_to_message:
        return await update.message.reply_text(
            "❌ Reply to the member's message and use /report [reason]."
        )

    target_user = update.message.reply_to_message.from_user
    reason = " ".join(context.args).strip() or "No reason provided"
    cid = update.effective_chat.id
    reporter = update.effective_user
    log_action(cid, reporter.id, "report", target_user.id, target_user.full_name)

    report_text = (
        "🚨 <b>MEMBER REPORT</b>\n\n"
        f"👤 Reporter: <a href=\"tg://user?id={reporter.id}\">{escape(reporter.full_name)}</a> "
        f"(<code>{reporter.id}</code>)\n"
        f"🎯 Target: <a href=\"tg://user?id={target_user.id}\">{escape(target_user.full_name)}</a> "
        f"(<code>{target_user.id}</code>)\n"
        f"📝 Reason: {escape(reason)}\n"
        f"💬 Message ID: <code>{update.message.reply_to_message.message_id}</code>"
    )

    # Reports are posted in the group so every admin can see them; private
    # admin DMs are attempted only when Telegram permits them.
    try:
        await update.effective_chat.send_message(
            report_text, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception:
        pass

    try:
        admins = await update.effective_chat.get_administrators()
        for admin in admins:
            if admin.user.is_bot:
                continue
            try:
                await _ACTIVE_BOT.send_message(
                    chat_id=admin.user.id, text=report_text, parse_mode="HTML"
                )
            except Exception:
                # Telegram blocks unsolicited bot DMs unless the admin has started the bot.
                pass
    except Exception:
        pass

    await update.message.reply_text("✅ Report logged and sent to the group admin channel/message flow.")


async def block_command(update, context):
    if not await admin_required(update): return
    u=target(update)
    uid=u.id if u else (int(context.args[0]) if context.args and context.args[0].lstrip('-').isdigit() else None)
    if uid is None: return await update.message.reply_text("Usage: reply /block or /block USER_ID")
    name=u.full_name if u else str(uid)
    cursor.execute("INSERT OR REPLACE INTO blocked_users VALUES(?,?,?,?,?)", (update.effective_chat.id,uid,name,update.effective_user.id,int(time.time())))
    commit(); log_action(update.effective_chat.id,update.effective_user.id,"block",uid,name)
    try: await update.effective_chat.ban_member(uid)
    except Exception: pass
    await update.message.reply_text(f"🚫 Blocklisted: {name} ({uid})")


async def unblock_command(update, context):
    if not await admin_required(update): return
    u=target(update); uid=u.id if u else (int(context.args[0]) if context.args and context.args[0].isdigit() else None)
    if uid is None: return await update.message.reply_text("Usage: reply /unblock or /unblock USER_ID")
    cursor.execute("DELETE FROM blocked_users WHERE chat_id=? AND user_id=?",(update.effective_chat.id,uid)); commit()
    try: await update.effective_chat.unban_member(uid, only_if_banned=True)
    except Exception: pass
    await update.message.reply_text(f"✅ Removed from blocklist: {uid}")


async def blocklist_command(update, context):
    if not await admin_required(update): return
    cursor.execute("SELECT user_id,name FROM blocked_users WHERE chat_id=? ORDER BY name",(update.effective_chat.id,))
    rows=cursor.fetchall()
    if not rows: return await update.message.reply_text("📭 Blocklist is empty.")
    await update.message.reply_text("🚫 <b>Blocklist</b>\n\n"+"\n".join(f"• {escape(n)} — <code>{u}</code>" for u,n in rows)[:3900],parse_mode="HTML")


async def tagall_command(update, context):
    if not await admin_required(update):
        return
    cid = update.effective_chat.id
    cursor.execute(
        "SELECT user_id,full_name FROM tracked_members WHERE chat_id=? ORDER BY last_seen DESC LIMIT ?",
        (cid, MAX_TAGS),
    )
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text(
            "📭 No tracked members yet. Members must send a message or join while the bot is active."
        )

    # Telegram message text has a length limit, so split mentions safely.
    chunks, current = [], "📢 "
    for uid, name in rows:
        mention = f'<a href="tg://user?id={uid}">{escape(name or str(uid))}</a>'
        if len(current) + len(mention) + 1 > 3500:
            chunks.append(current)
            current = "📢 " + mention
        else:
            current += (" " if current != "📢 " else "") + mention
    if current.strip() != "📢":
        chunks.append(current)

    for chunk in chunks:
        await update.message.reply_text(
            chunk, parse_mode="HTML", disable_web_page_preview=True
        )


async def approve_command(update, context):
    if not await admin_required(update): return
    uid=None
    if context.args and context.args[0].lstrip('-').isdigit(): uid=int(context.args[0])
    elif update.message.reply_to_message: uid=update.message.reply_to_message.from_user.id
    if uid is None: return await update.message.reply_text("Usage: /approve USER_ID")
    try:
        await update.effective_chat.approve_chat_join_request(uid)
        cursor.execute("DELETE FROM join_requests WHERE chat_id=? AND user_id=?",(update.effective_chat.id,uid)); commit()
        log_action(update.effective_chat.id,update.effective_user.id,"approve",uid)
        await update.message.reply_text(f"✅ Approved <code>{uid}</code>.",parse_mode="HTML")
    except Exception as e: await update.message.reply_text(f"❌ Approval failed: {e}")


async def decline_command(update, context):
    if not await admin_required(update): return
    if not context.args or not context.args[0].lstrip('-').isdigit(): return await update.message.reply_text("Usage: /decline USER_ID")
    uid=int(context.args[0])
    try:
        await update.effective_chat.decline_chat_join_request(uid)
        cursor.execute("DELETE FROM join_requests WHERE chat_id=? AND user_id=?",(update.effective_chat.id,uid)); commit()
        log_action(update.effective_chat.id,update.effective_user.id,"decline",uid)
        await update.message.reply_text(f"✅ Declined <code>{uid}</code>.",parse_mode="HTML")
    except Exception as e: await update.message.reply_text(f"❌ Decline failed: {e}")


async def join_request_handler(update, context):
    r=update.chat_join_request
    if not r: return
    u=r.from_user; cid=r.chat.id
    track_member(u,cid)
    cursor.execute("INSERT OR REPLACE INTO join_requests VALUES(?,?,?,?,?)",(cid,u.id,u.full_name,u.username,int(time.time()))); commit()
    log_action(cid,0,"join_request",u.id,u.full_name)
    try:
        await r.chat.send_message(f"🔔 <b>Join request</b>\n{escape(u.full_name)} — <code>{u.id}</code>\n\nAdmin: /approve {u.id}\nDecline: /decline {u.id}",parse_mode="HTML")
    except Exception: pass


# ------------------------- BASIC -----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Blackberry Bot is online!\n\n"
        "Use /help to see all commands.\n"
        "Use /ping to test the bot."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """🤖 <b>BLACKBERRY BOT — SIMPLE GUIDE</b>

<b>🆔 USER / HISTORY</b>
/id — your ID + chat ID
/info — user details (reply to a message)
/history 123456 20 — saved history for user ID
/history 20 — history of replied user
/stats — group statistics

<b>🏆 RANKING</b>
/rank — your rank, name, ID and messages
/top — top 10 users with names + message count
/mystats — same as /rank
/resetrank — reset replied user's ranking

<b>🔎 FILTERS</b>
/filter hello Hello 👋 — text reply
Reply to media + /filter hello — save photo/sticker/video/etc.
/filters — list filters
/stop hello — remove filter

<b>🛡 MODERATION</b>
/ban, /unban, /kick, /mute, /tmute, /unmute
/warn, /warns, /unwarn, /del, /purge
/pin, /unpin

<b>🔐 PROTECTION</b>
/antilink on|off
/anticaps on|off
/adultfilter on|off
/badword add WORD
/badword remove WORD
/lock /unlock
/block USER_ID /unblock USER_ID /blocklist

<b>🚨 ADMIN TOOLS</b>
/report reason — reply to a member
/adminlogs — moderation history
/approve USER_ID — approve join request
/decline USER_ID — decline join request
/tagall — tag tracked members

<b>👋 WELCOME</b>
/setwelcome Welcome {mention} to {chat}!
/welcome on|off
/setgoodbye Goodbye {name}!
/goodbye on|off

<b>🌐 TRANSLATION</b>
/tr hi Hello everyone
Reply to ANY message/word + /tr hi
/tr en hello — English
/tr hi hello — Hindi
/tr ur hello — Urdu
/tr bn hello — Bengali


<b>📌 IMPORTANT SETUP</b>
• Bot should be ADMIN for moderation, deleting, approvals and welcome events.
• Disable BotFather Privacy Mode if you want the bot to read normal group messages.
• History/ranking/tagall only contain users/messages the bot has observed.
• Telegram does not give bots a complete old chat archive by user ID.
• Visual nude-image detection needs a separate image-classification service; this bot removes obvious NSFW keywords/sticker-set labels."""
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        "⚙️ <b>BLACKBERRY QUICK SETUP</b>\n\n"
        "1️⃣ BotFather → /setprivacy → <b>Disable</b>\n"
        "2️⃣ Add bot to group as <b>Administrator</b>.\n"
        "3️⃣ Give it permission to delete messages, restrict members, invite/approve users and pin messages as needed.\n"
        "4️⃣ Start the bot.\n"
        "5️⃣ Try /help.\n\n"
        "🧪 Test filters: /filter hi Hello\n"
        "🧪 Test media: reply to a sticker/photo → /filter wow\n"
        "🧪 Test ranking: send messages → /top\n"
        "🧪 Test history: reply to a user's message → /history 20\n"
        "🧪 Test protection: /antilink on and /adultfilter on",
        parse_mode="HTML",
    )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! Blackberry is online.")


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = target(update) or update.effective_user
    if not u:
        return

    text = (
        f"👤 <b>{u.full_name}</b>\n"
        f"🆔 <code>{u.id}</code>\n"
        f"🔗 Username: @{u.username}" if u.username else
        f"👤 <b>{u.full_name}</b>\n🆔 <code>{u.id}</code>\n🔗 Username: —"
    )

    if update.effective_chat:
        text += f"\n💬 Chat ID: <code>{update.effective_chat.id}</code>"

    await update.message.reply_text(text, parse_mode="HTML")


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = target(update) or update.effective_user
    if not u:
        return

    try:
        member = await update.effective_chat.get_member(u.id)
        status = member.status
    except Exception:
        status = "unknown"

    cursor.execute(
        "SELECT warns FROM warnings WHERE chat_id=? AND user_id=?",
        (update.effective_chat.id, u.id),
    )
    row = cursor.fetchone()

    cursor.execute(
        "SELECT COUNT(*) FROM user_history WHERE chat_id=? AND user_id=?",
        (update.effective_chat.id, u.id),
    )
    history_count = cursor.fetchone()[0]

    await update.message.reply_text(
        f"👤 <b>{u.full_name}</b>\n"
        f"🆔 <code>{u.id}</code>\n"
        f"🔗 Username: @{u.username if u.username else '—'}\n"
        f"📌 Status: {status}\n"
        f"⚠️ Warnings: {row[0] if row else 0}/{get_warn_limit(update.effective_chat.id)}\n"
        f"📝 Saved messages: {history_count}",
        parse_mode="HTML",
    )


async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        admins = await update.effective_chat.get_administrators()
        text = "👮 <b>Group Admins</b>\n\n" + "\n".join(
            f"• {a.user.full_name} — <code>{a.user.id}</code>"
            for a in admins
        )
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Could not get admins: {e}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id

    cursor.execute("SELECT COUNT(*) FROM warnings WHERE chat_id=?", (cid,))
    warnings_users = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM filters WHERE chat_id=?", (cid,))
    filter_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM media_filters WHERE chat_id=?", (cid,))
    media_filter_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM badwords WHERE chat_id=?", (cid,))
    badword_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM user_history WHERE chat_id=?", (cid,))
    history_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM ranks WHERE chat_id=?", (cid,))
    users_tracked = cursor.fetchone()[0]

    await update.message.reply_text(
        f"📊 <b>Blackberry Stats</b>\n\n"
        f"⚠️ Users with warnings: {warnings_users}\n"
        f"🔎 Text filters: {filter_count}\n"
        f"🖼 Media filters: {media_filter_count}\n"
        f"🚫 Custom bad words: {badword_count}\n"
        f"📝 Saved messages: {history_count}\n"
        f"🏆 Users ranked: {users_tracked}",
        parse_mode="HTML",
    )


# ------------------------- HISTORY ---------------------------

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return
    u = resolve_user_identifier(update, context.args)
    if not u:
        return await update.message.reply_text(
            "Usage: reply to a member + /history [N]\nOR /history USER_ID [N]\nOR /history @username [N]"
        )
    # If replying, first arg may be the count.
    count_arg = context.args[0] if context.args and target(update) else (context.args[1] if len(context.args)>1 else None)
    try:
        limit = max(1, min(int(count_arg), 100)) if count_arg else HISTORY_LIMIT_DEFAULT
    except ValueError:
        return await update.message.reply_text("❌ Count must be a number (1-100).")
    cid = update.effective_chat.id
    cursor.execute("SELECT full_name,username FROM tracked_members WHERE chat_id=? AND user_id=?", (cid, u.id))
    profile = cursor.fetchone()
    current_name = profile[0] if profile else u.full_name
    current_username = profile[1] if profile else u.username
    cursor.execute("SELECT username,full_name,first_seen,last_seen FROM user_identity_history WHERE chat_id=? AND user_id=? ORDER BY last_seen ASC", (cid, u.id))
    identities = cursor.fetchall()
    cursor.execute("""SELECT full_name, username, message_id, message_type, content,
               media_file_id, reply_to_message_id, created_at FROM user_history
        WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?""", (cid, u.id, limit))
    rows = cursor.fetchall()
    lines = [f"👤 <b>User history</b>", f"🆔 ID: <code>{u.id}</code>", f"📛 Current name: {escape(current_name or '—')}", f"🔗 Current username: @{escape(current_username) if current_username else '—'}"]
    if identities:
        lines.append("🕘 <b>Names/usernames seen before:</b>")
        seen=set()
        for un, fn, first, last in identities:
            key=(un,fn)
            if key in seen: continue
            seen.add(key)
            when=time.strftime('%Y-%m-%d %H:%M', time.localtime(last))
            lines.append(f"• {escape(fn or '—')} | @{escape(un) if un else '—'} | last seen {when}")
    lines.append(f"📚 Saved messages: {len(rows)}")
    if not rows:
        lines.append("\nNo saved messages found. History starts when the bot observes the member.")
    else:
        lines.append("")
        for fn, un, mid, mtype, content, media_file_id, reply_to_id, created_at in reversed(rows):
            when=time.strftime('%Y-%m-%d %H:%M', time.localtime(created_at))
            safe=escape(content or '')
            if len(safe)>220: safe=safe[:220]+'…'
            lines.append(f"• {when} | msg #{mid} | {mtype}" + (" 📎" if media_file_id else "") + (f" ↩️#{reply_to_id}" if reply_to_id else "") + f"\n  {safe}")
    await update.message.reply_text("\n".join(lines)[:3900], parse_mode="HTML")


# ------------------------- MODERATION ------------------------

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return
    u = resolve_user_identifier(update, context.args)
    if not u:
        return await update.message.reply_text("❌ Reply to a user's message and use /ban.")
    if await is_admin(update, u.id):
        return await update.message.reply_text("❌ You cannot ban an admin.")

    try:
        await update.effective_chat.ban_member(u.id)
        log_action(update.effective_chat.id, update.effective_user.id, "ban", u.id, u.full_name)
        await update.message.reply_text(f"🔨 {u.full_name} has been banned.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ban failed: {e}")


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return
    u = resolve_user_identifier(update, context.args)
    if not u:
        return await update.message.reply_text("Usage: /unban USER_ID or /unban @username")

    try:
        uid = u.id
        await update.effective_chat.unban_member(uid, only_if_banned=True)
        log_action(update.effective_chat.id, update.effective_user.id, "unban", uid)
        await update.message.reply_text(f"✅ User {uid} has been unbanned.")
    except Exception as e:
        await update.message.reply_text(f"❌ Unban failed: {e}")


async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return
    u = resolve_user_identifier(update, context.args)
    if not u:
        return await update.message.reply_text("❌ Reply to a user's message and use /kick.")
    if await is_admin(update, u.id):
        return await update.message.reply_text("❌ You cannot kick an admin.")

    try:
        await update.effective_chat.ban_member(u.id)
        await update.effective_chat.unban_member(u.id)
        log_action(update.effective_chat.id, update.effective_user.id, "kick", u.id, u.full_name)
        await update.message.reply_text(f"👢 {u.full_name} has been kicked.")
    except Exception as e:
        await update.message.reply_text(f"❌ Kick failed: {e}")


MUTE_PERMISSIONS = ChatPermissions(can_send_messages=False)

UNMUTE_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return
    u = resolve_user_identifier(update, context.args)
    if not u:
        return await update.message.reply_text("❌ Reply to a user's message and use /mute.")
    if await is_admin(update, u.id):
        return await update.message.reply_text("❌ You cannot mute an admin.")

    try:
        await update.effective_chat.restrict_member(
            u.id, permissions=MUTE_PERMISSIONS
        )
        log_action(update.effective_chat.id, update.effective_user.id, "mute", u.id, u.full_name)
        await update.message.reply_text(f"🔇 {u.full_name} has been muted.")
    except Exception as e:
        await update.message.reply_text(f"❌ Mute failed: {e}")


async def tmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return
    u = resolve_user_identifier(update, context.args)
    minutes_arg = context.args[0] if target(update) else (context.args[1] if len(context.args) > 1 else None)
    if not u or not minutes_arg:
        return await update.message.reply_text("Usage: reply + /tmute MINUTES OR /tmute USER_ID MINUTES OR /tmute @username MINUTES")
    if await is_admin(update, u.id):
        return await update.message.reply_text("❌ You cannot mute an admin.")

    try:
        minutes = int(minutes_arg)
        if minutes < 1 or minutes > 10080:
            return await update.message.reply_text(
                "❌ Minutes must be between 1 and 10080."
            )

        until = int(time.time()) + minutes * 60
        await update.effective_chat.restrict_member(
            u.id,
            permissions=MUTE_PERMISSIONS,
            until_date=until,
        )
        log_action(update.effective_chat.id, update.effective_user.id, f"tmute {minutes}m", u.id, u.full_name)
        await update.message.reply_text(
            f"🔇 {u.full_name} muted for {minutes} minutes."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Temporary mute failed: {e}")


async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return
    u = resolve_user_identifier(update, context.args)
    if not u:
        return await update.message.reply_text("❌ Reply to the muted user's message.")

    try:
        await update.effective_chat.restrict_member(
            u.id, permissions=UNMUTE_PERMISSIONS
        )
        log_action(update.effective_chat.id, update.effective_user.id, "unmute", u.id, u.full_name)
        await update.message.reply_text(f"🔊 {u.full_name} has been unmuted.")
    except Exception as e:
        await update.message.reply_text(f"❌ Unmute failed: {e}")


async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    u = resolve_user_identifier(update, context.args)
    if not u:
        return await update.message.reply_text("❌ Reply to a user's message and use /warn.")
    if await is_admin(update, u.id):
        return await update.message.reply_text("❌ You cannot warn an admin.")

    cid = update.effective_chat.id
    cursor.execute(
        "SELECT warns FROM warnings WHERE chat_id=? AND user_id=?",
        (cid, u.id),
    )
    row = cursor.fetchone()
    n = row[0] + 1 if row else 1

    cursor.execute(
        "INSERT OR REPLACE INTO warnings VALUES(?,?,?)",
        (cid, u.id, n),
    )
    commit()

    limit = get_warn_limit(cid)

    if n >= limit:
        try:
            await update.effective_chat.restrict_member(
                u.id, permissions=MUTE_PERMISSIONS
            )
            cursor.execute(
                "DELETE FROM warnings WHERE chat_id=? AND user_id=?",
                (cid, u.id),
            )
            commit()
            await update.message.reply_text(
                f"⚠️ {u.full_name} reached {limit} warnings.\n"
                "🔇 User has been muted."
            )
        except Exception as e:
            await update.message.reply_text(
                f"⚠️ Warning saved, but mute failed: {e}"
            )
    else:
        await update.message.reply_text(
            f"⚠️ Warning given to {u.full_name}.\n"
            f"Warnings: {n}/{limit}"
        )


async def warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = target(update) or update.effective_user
    cursor.execute(
        "SELECT warns FROM warnings WHERE chat_id=? AND user_id=?",
        (update.effective_chat.id, u.id),
    )
    row = cursor.fetchone()
    await update.message.reply_text(
        f"⚠️ {u.full_name} has {row[0] if row else 0}/"
        f"{get_warn_limit(update.effective_chat.id)} warnings."
    )


async def unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    u = resolve_user_identifier(update, context.args)
    if not u:
        return await update.message.reply_text("❌ Reply to a user and use /unwarn.")

    cursor.execute(
        "DELETE FROM warnings WHERE chat_id=? AND user_id=?",
        (update.effective_chat.id, u.id),
    )
    commit()
    await update.message.reply_text(f"✅ Warnings reset for {u.full_name}.")


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("❌ Reply to a message and use /del.")

    try:
        await update.message.reply_to_message.delete()
        await update.message.delete()
    except Exception as e:
        await update.message.reply_text(f"❌ Delete failed: {e}")


async def purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    if not context.args:
        return await update.message.reply_text("Usage: /purge N")

    try:
        n = int(context.args[0])
        if n < 1 or n > 100:
            return await update.message.reply_text("❌ N must be between 1 and 100.")

        if not update.message.reply_to_message:
            return await update.message.reply_text(
                "❌ Reply to the first message to delete, then use /purge N."
            )

        start_id = update.message.reply_to_message.message_id
        ids = list(range(start_id, start_id + n + 1))

        deleted = 0
        for i in range(0, len(ids), 100):
            try:
                await update.effective_chat.delete_messages(ids[i:i + 100])
                deleted += len(ids[i:i + 100])
            except Exception:
                for mid in ids[i:i + 100]:
                    try:
                        await update.effective_chat.delete_message(mid)
                        deleted += 1
                    except Exception:
                        pass

        await update.message.reply_text(f"🧹 Deleted approximately {deleted} messages.")
    except Exception as e:
        await update.message.reply_text(f"❌ Purge failed: {e}")


async def pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("❌ Reply to a message and use /pin.")

    try:
        await update.message.reply_to_message.pin()
        await update.message.reply_text("📌 Message pinned.")
    except Exception as e:
        await update.message.reply_text(f"❌ Pin failed: {e}")


async def unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    try:
        if update.message.reply_to_message:
            await update.message.reply_to_message.unpin()
        else:
            await update.effective_chat.unpin_all_forum_topic_messages()
        await update.message.reply_text("📌 Message unpinned.")
    except Exception as e:
        try:
            await update.effective_chat.unpin_message()
            await update.message.reply_text("📌 Message unpinned.")
        except Exception:
            await update.message.reply_text(f"❌ Unpin failed: {e}")


# ------------------------- WELCOME ---------------------------

async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id

    if context.args and context.args[0].lower() in ("on", "off"):
        if not await admin_required(update):
            return

        val = 1 if context.args[0].lower() == "on" else 0
        cursor.execute(
            "INSERT OR IGNORE INTO welcome_state(chat_id,enabled,goodbye) VALUES(?,1,0)",
            (cid,),
        )
        cursor.execute(
            "UPDATE welcome_state SET enabled=? WHERE chat_id=?",
            (val, cid),
        )
        commit()

        await update.message.reply_text(
            "👋 Welcome is " + ("ON." if val else "OFF.")
        )
        return

    cursor.execute("SELECT welcome FROM settings WHERE chat_id=?", (cid,))
    row = cursor.fetchone()

    cursor.execute("SELECT enabled FROM welcome_state WHERE chat_id=?", (cid,))
    state = cursor.fetchone()
    enabled = state[0] if state else 1

    await update.message.reply_text(
        "👋 Current welcome:\n"
        + (row[0] if row else "Welcome {mention} to {chat}! 👋")
        + f"\n\nStatus: {'ON' if enabled else 'OFF'}"
    )


async def setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    if not context.args:
        return await update.message.reply_text(
            "Usage: /setwelcome Welcome {mention} to {chat}! 👋"
        )

    msg = " ".join(context.args)

    cursor.execute(
        "INSERT OR REPLACE INTO settings(chat_id,welcome) VALUES(?,?)",
        (update.effective_chat.id, msg),
    )
    cursor.execute(
        """INSERT OR IGNORE INTO welcome_state
        (chat_id,enabled,goodbye) VALUES(?,1,0)""",
        (update.effective_chat.id,),
    )
    commit()

    await update.message.reply_text(
        "✅ Welcome message saved and enabled.\n"
        "Available: {name} {mention} {username} {id} {chat}"
    )


async def setgoodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    if not context.args:
        return await update.message.reply_text(
            "Usage: /setgoodbye Goodbye {name}! 👋"
        )

    msg = " ".join(context.args)

    cursor.execute(
        "INSERT OR REPLACE INTO goodbye_settings(chat_id,text) VALUES(?,?)",
        (update.effective_chat.id, msg),
    )
    cursor.execute(
        """INSERT OR IGNORE INTO welcome_state
        (chat_id,enabled,goodbye) VALUES(?,1,0)""",
        (update.effective_chat.id,),
    )
    commit()

    await update.message.reply_text("✅ Goodbye message saved.")


async def goodbye_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        return await update.message.reply_text("Usage: /goodbye on OR /goodbye off")

    val = 1 if context.args[0].lower() == "on" else 0
    cid = update.effective_chat.id

    cursor.execute(
        "INSERT OR IGNORE INTO welcome_state(chat_id,enabled,goodbye) VALUES(?,1,0)",
        (cid,),
    )
    cursor.execute(
        "UPDATE welcome_state SET goodbye=? WHERE chat_id=?",
        (val, cid),
    )
    commit()

    await update.message.reply_text(
        "👋 Goodbye is " + ("ON." if val else "OFF.")
    )


async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = update.chat_member
    if not r:
        return

    cid = update.effective_chat.id
    old = r.old_chat_member.status
    new = r.new_chat_member.status
    user = r.new_chat_member.user
    track_member(user, cid)

    # New member
    if old in ("left", "kicked") and new in ("member", "administrator"):
        cursor.execute(
            "SELECT enabled FROM welcome_state WHERE chat_id=?",
            (cid,),
        )
        state = cursor.fetchone()

        # Default is ON even before /setwelcome.
        if state and not state[0]:
            return

        cursor.execute(
            "SELECT welcome FROM settings WHERE chat_id=?",
            (cid,),
        )
        row = cursor.fetchone()

        template = row[0] if row else "Welcome {mention} to {chat}! 👋"
        msg = format_template(template, user, update.effective_chat)

        try:
            await update.effective_chat.send_message(
                msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            print("WELCOME ERROR:", e)

    # Member left / kicked
    elif old in ("member", "administrator") and new in ("left", "kicked"):
        cursor.execute(
            "SELECT goodbye FROM welcome_state WHERE chat_id=?",
            (cid,),
        )
        state = cursor.fetchone()

        if not state or not state[0]:
            return

        cursor.execute(
            "SELECT text FROM goodbye_settings WHERE chat_id=?",
            (cid,),
        )
        row = cursor.fetchone()

        template = row[0] if row else "Goodbye {name}! 👋"
        msg = format_template(template, user, update.effective_chat)

        try:
            await update.effective_chat.send_message(
                msg,
                parse_mode="HTML",
            )
        except Exception as e:
            print("GOODBYE ERROR:", e)


# ------------------------- RULES -----------------------------

async def setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    if not context.args:
        return await update.message.reply_text(
            "Usage: /setrules Be respectful and no spam."
        )

    text = " ".join(context.args)
    cursor.execute(
        "INSERT OR REPLACE INTO rules(chat_id,text) VALUES(?,?)",
        (update.effective_chat.id, text),
    )
    commit()

    await update.message.reply_text("✅ Group rules saved.")


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute(
        "SELECT text FROM rules WHERE chat_id=?",
        (update.effective_chat.id,),
    )
    row = cursor.fetchone()

    await update.message.reply_text(
        "📜 Rules:\n\n"
        + (row[0] if row else "No rules have been set yet.")
    )


# ------------------------- FILTERS ---------------------------

async def add_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    if not context.args:
        return await update.message.reply_text(
            "Usage:\n"
            "/filter word reply\n\n"
            "For media: reply to the photo/sticker/video/etc. and use:\n"
            "/filter word"
        )

    cid = update.effective_chat.id
    word = context.args[0].lower().strip()
    replied = update.message.reply_to_message

    if not word:
        return await update.message.reply_text("❌ Keyword cannot be empty.")

    # Save replied media as a filter.
    if replied:
        typ, fid = media_from_message(replied)

        if typ and fid:
            cursor.execute(
                """INSERT OR REPLACE INTO media_filters
                (chat_id,keyword,media_type,file_id,caption)
                VALUES(?,?,?,?,?)""",
                (
                    cid,
                    word,
                    typ,
                    fid,
                    replied.caption or "",
                ),
            )

            # If an old text filter exists for the same keyword, remove it.
            cursor.execute(
                "DELETE FROM filters WHERE chat_id=? AND keyword=?",
                (cid, word),
            )
            commit()

            await update.message.reply_text(
                f"✅ Media filter added:\n"
                f"Keyword: {word}\n"
                f"Reply type: {typ}\n\n"
                f"Now sending '{word}' in the group will reply with this media."
            )
            return

    if len(context.args) < 2:
        return await update.message.reply_text(
            "❌ For text filter use: /filter word reply\n"
            "For media filter, reply to the media and use: /filter word"
        )

    response = " ".join(context.args[1:])

    cursor.execute(
        "INSERT OR REPLACE INTO filters VALUES(?,?,?)",
        (cid, word, response),
    )
    cursor.execute(
        "DELETE FROM media_filters WHERE chat_id=? AND keyword=?",
        (cid, word),
    )
    commit()

    await update.message.reply_text(f"✅ Text filter added: {word}")


async def list_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id

    cursor.execute(
        "SELECT keyword FROM filters WHERE chat_id=? ORDER BY keyword",
        (cid,),
    )
    text_filters = [r[0] for r in cursor.fetchall()]

    cursor.execute(
        "SELECT keyword,media_type FROM media_filters WHERE chat_id=? ORDER BY keyword",
        (cid,),
    )
    media_filters = [f"{r[0]} → {r[1]}" for r in cursor.fetchall()]

    items = [f"• {x}" for x in text_filters + media_filters]

    await update.message.reply_text(
        "📭 No filters." if not items else "🔎 Filters:\n\n" + "\n".join(items)
    )


async def stop_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    if not context.args:
        return await update.message.reply_text("Usage: /stop word")

    word = context.args[0].lower()

    cursor.execute(
        "DELETE FROM filters WHERE chat_id=? AND keyword=?",
        (update.effective_chat.id, word),
    )
    cursor.execute(
        "DELETE FROM media_filters WHERE chat_id=? AND keyword=?",
        (update.effective_chat.id, word),
    )
    commit()

    await update.message.reply_text(f"✅ Filter removed: {word}")


# ------------------------- PROTECTION ------------------------

async def adultfilter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        return await update.message.reply_text("Usage: /adultfilter on OR /adultfilter off")
    value = 1 if context.args[0].lower() == "on" else 0
    cid = update.effective_chat.id
    cursor.execute("INSERT OR IGNORE INTO mod_settings(chat_id) VALUES(?)", (cid,))
    cursor.execute("UPDATE mod_settings SET adult_filter=? WHERE chat_id=?", (value, cid))
    commit()
    await update.message.reply_text(
        f"🔞 Adult/NSFW keyword & sticker-set filter is {'ON' if value else 'OFF'}.\n"
        "Note: this is not a visual nude-image detector."
    )


async def antilink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        return await update.message.reply_text("Usage: /antilink on OR /antilink off")

    value = 1 if context.args[0].lower() == "on" else 0
    cid = update.effective_chat.id

    cursor.execute(
        "INSERT OR IGNORE INTO mod_settings(chat_id) VALUES(?)",
        (cid,),
    )
    cursor.execute(
        "UPDATE mod_settings SET anti_link=? WHERE chat_id=?",
        (value, cid),
    )
    commit()

    await update.message.reply_text(
        f"🔗 Anti-link is {'ON' if value else 'OFF'}."
    )


async def anticaps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    if not context.args or context.args[0].lower() not in ("on", "off"):
        return await update.message.reply_text("Usage: /anticaps on OR /anticaps off")

    value = 1 if context.args[0].lower() == "on" else 0
    cid = update.effective_chat.id

    cursor.execute(
        "INSERT OR IGNORE INTO mod_settings(chat_id) VALUES(?)",
        (cid,),
    )
    cursor.execute(
        "UPDATE mod_settings SET anti_caps=? WHERE chat_id=?",
        (value, cid),
    )
    commit()

    await update.message.reply_text(
        f"🔠 Anti-caps is {'ON' if value else 'OFF'}."
    )


async def badword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    if len(context.args) < 2 or context.args[0].lower() not in ("add", "remove"):
        return await update.message.reply_text(
            "Usage:\n/badword add WORD\n/badword remove WORD"
        )

    action = context.args[0].lower()
    word = context.args[1].lower()

    if action == "add":
        cursor.execute(
            "INSERT OR IGNORE INTO badwords(chat_id,word) VALUES(?,?)",
            (update.effective_chat.id, word),
        )
        commit()
        await update.message.reply_text(f"✅ Bad-word filter added: {word}")
    else:
        cursor.execute(
            "DELETE FROM badwords WHERE chat_id=? AND word=?",
            (update.effective_chat.id, word),
        )
        commit()
        await update.message.reply_text(f"✅ Bad-word filter removed: {word}")


async def badwords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute(
        "SELECT word FROM badwords WHERE chat_id=? ORDER BY word",
        (update.effective_chat.id,),
    )
    rows = cursor.fetchall()

    await update.message.reply_text(
        "📭 No custom bad words."
        if not rows
        else "🚫 Bad words:\n\n" + "\n".join("• " + r[0] for r in rows)
    )


# ------------------------- LOCK ------------------------------

async def lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    try:
        await update.effective_chat.set_permissions(
            ChatPermissions(can_send_messages=False)
        )
        await update.message.reply_text(
            "🔒 Group locked. Only admins can send messages."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Lock failed: {e}")


async def unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    try:
        await update.effective_chat.set_permissions(UNMUTE_PERMISSIONS)
        await update.message.reply_text("🔓 Group unlocked.")
    except Exception as e:
        await update.message.reply_text(f"❌ Unlock failed: {e}")


# ------------------------- RANKING ---------------------------

async def rank_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id

    cursor.execute(
        "SELECT name,messages FROM ranks WHERE chat_id=? AND user_id=?",
        (cid, update.effective_user.id),
    )
    row = cursor.fetchone()

    if (update.message.text or "").split()[0].split("@")[0].lower() == "/top":
        cursor.execute(
            """SELECT name,user_id,messages FROM ranks
            WHERE chat_id=? ORDER BY messages DESC LIMIT 10""",
            (cid,),
        )
        rows = cursor.fetchall()

        if not rows:
            return await update.message.reply_text("🏆 No ranking data yet.")

        text = "🏆 <b>Top Chatters</b>\n\n"
        for i, (name, uid, messages) in enumerate(rows, 1):
            text += f"{i}. {name} — {messages} messages\n"
        return await update.message.reply_text(text, parse_mode="HTML")

    messages = row[1] if row else 0
    cursor.execute("SELECT COUNT(*)+1 FROM ranks WHERE chat_id=? AND messages>?", (cid, messages))
    position = cursor.fetchone()[0]
    await update.message.reply_text(
        f"🏆 <b>{escape(update.effective_user.full_name)}</b>\n"
        f"🆔 <code>{update.effective_user.id}</code>\n"
        f"📊 Rank: #{position}\n"
        f"💬 Messages: {messages}", parse_mode="HTML")


async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rank_command(update, context)


async def reset_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    u = target(update)
    uid = u.id if u else update.effective_user.id

    cursor.execute(
        "DELETE FROM ranks WHERE chat_id=? AND user_id=?",
        (update.effective_chat.id, uid),
    )
    commit()

    await update.message.reply_text("✅ Ranking data reset.")


# ------------------------- ADMIN LOGS ------------------------

async def adminlogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_required(update):
        return

    limit = 20
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 50))
        except ValueError:
            pass

    cursor.execute(
        """SELECT actor_id,action,target_id,target_name,created_at
        FROM admin_logs WHERE chat_id=? ORDER BY id DESC LIMIT ?""",
        (update.effective_chat.id, limit),
    )
    rows = cursor.fetchall()

    if not rows:
        return await update.message.reply_text("📭 No admin logs yet.")

    text = "📋 <b>Admin Logs</b>\n\n"
    for actor, action, target_id, target_name, ts in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        text += (
            f"• {when} | admin <code>{actor}</code>\n"
            f"  {action}"
            + (f" → {target_name} ({target_id})" if target_id else "")
            + "\n"
        )

    await update.message.reply_text(text[:3900], parse_mode="HTML")


# ------------------------- MESSAGE ENGINE --------------------

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message:
        return

    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type not in ("group", "supergroup"):
        return

    if not user or user.is_bot:
        return

    track_member(user, chat.id)

    # Blocklist is enforced before normal processing.
    if is_blocked(chat.id, user.id):
        try: await message.delete()
        except Exception: pass
        return

    # Remove obvious adult/NSFW text and media labels when enabled.
    cursor.execute("SELECT adult_filter FROM mod_settings WHERE chat_id=?", (chat.id,))
    _adult_row = cursor.fetchone()
    adult_filter_on = bool(_adult_row[0]) if _adult_row else True
    if adult_filter_on and media_looks_adult(message) and not await is_admin(update, user.id):
        try:
            await message.delete()
        except Exception:
            pass
        log_action(chat.id, 0, "adult_media_removed", user.id, user.full_name)
        return

    # Always track observed messages first.
    save_history(message)

    # Ranking also counts media.
    cid = chat.id
    cursor.execute(
        """INSERT INTO ranks(chat_id,user_id,name,messages)
        VALUES(?,?,?,1)
        ON CONFLICT(chat_id,user_id)
        DO UPDATE SET name=excluded.name,
        messages=ranks.messages+1""",
        (cid, user.id, user.full_name),
    )
    commit()

    # Admins are still allowed to trigger filters. They are only skipped
    # from automatic moderation (antilink, anticaps, badwords, spam actions).
    user_is_admin = await is_admin(update, user.id)

    text = (message.text or message.caption or "").strip()
    lower = text.lower()

    # Spam protection.
    now = time.time()
    key = (cid, user.id)
    spam_tracker[key] = [
        t for t in spam_tracker[key]
        if now - t < SPAM_WINDOW
    ]
    spam_tracker[key].append(now)

    if len(spam_tracker[key]) > SPAM_LIMIT:
        try:
            await message.delete()
            await chat.restrict_member(
                user.id,
                permissions=MUTE_PERMISSIONS,
                until_date=int(now) + 60,
            )
            await chat.send_message(
                f"🚫 {user.full_name} muted for 60 seconds due to spam."
            )
        except Exception as e:
            print("SPAM ERROR:", e)

        spam_tracker[key] = []
        return

    if not user_is_admin:
        # Protection settings.
        cursor.execute(
            "SELECT anti_link,anti_caps FROM mod_settings WHERE chat_id=?",
            (cid,),
        )
        st = cursor.fetchone()

        anti_link = bool(st[0]) if st else False
        anti_caps = bool(st[1]) if st else False

        if anti_link and text and LINK_RE.search(text):
            try:
                await message.delete()
                await chat.send_message(
                    f"🔗 {user.full_name}, links are not allowed in this group."
                )
            except Exception:
                pass
            return

        letters = [c for c in text if c.isalpha()]
        if (
            anti_caps
            and len(letters) >= 10
            and sum(c.isupper() for c in letters) / len(letters) >= 0.75
        ):
            try:
                await message.delete()
                await chat.send_message(
                    f"🔠 {user.full_name}, please avoid excessive CAPITAL letters."
                )
            except Exception:
                pass
            return

        # Bad words.
        cursor.execute(
            "SELECT word FROM badwords WHERE chat_id=?",
            (cid,),
        )
        words = [r[0] for r in cursor.fetchall()]

        if lower and any(w and w in lower for w in words):
            try:
                await message.delete()
                await chat.send_message(
                    f"🚫 {user.full_name}, that word is not allowed here."
                )
            except Exception:
                pass
            return

    # Text filters: keyword -> text reply.
    if lower:
        cursor.execute(
            "SELECT keyword,response FROM filters WHERE chat_id=?",
            (cid,),
        )
        for word, reply in cursor.fetchall():
            word = (word or "").strip().lower()
            if word and word in lower:
                try:
                    await message.reply_text(reply, allow_sending_without_reply=True)
                except Exception as e:
                    print("TEXT FILTER ERROR:", e)
                return

    # Media filters: keyword in text/caption -> saved media.
    if lower:
        cursor.execute(
            """SELECT keyword,media_type,file_id,caption
            FROM media_filters WHERE chat_id=?""",
            (cid,),
        )
        for word, typ, fid, cap in cursor.fetchall():
            word = (word or "").strip().lower()
            if word and word in lower:
                await send_media_filter(message, typ, fid, cap)
                return



# ------------------------- ERROR -----------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("ERROR:", context.error)


# ------------------------- MAIN ------------------------------

def main():
    if not BOT_TOKEN:
        print("ERROR: Put your BotFather token in BOT_TOKEN.")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    global _ACTIVE_BOT
    _ACTIVE_BOT = app.bot

    handlers = [
        ("start", start),
        ("help", help_command),
        ("setup", setup_command),
        ("id", id_command),
        ("info", info_command),
        ("history", history_command),
        ("ping", ping_command),
        ("admins", admins_command),
        ("stats", stats_command),

        ("ban", ban),
        ("unban", unban),
        ("kick", kick),
        ("mute", mute),
        ("tmute", tmute),
        ("unmute", unmute),
        ("warn", warn),
        ("warns", warns),
        ("unwarn", unwarn),
        ("del", delete_command),
        ("purge", purge),
        ("pin", pin),
        ("unpin", unpin),

        ("welcome", welcome),
        ("setwelcome", setwelcome),
        ("setgoodbye", setgoodbye),
        ("goodbye", goodbye_toggle),

        ("setrules", setrules),
        ("rules", rules_command),

        ("filter", add_filter),
        ("filters", list_filters),
        ("stop", stop_filter),

        ("adultfilter", adultfilter_command),
        ("antilink", antilink),
        ("anticaps", anticaps),
        ("badword", badword_command),
        ("badwords", badwords_command),

        ("lock", lock),
        ("unlock", unlock),

        ("rank", rank_command),
        ("top", rank_command),
        ("mystats", mystats_command),
        ("resetrank", reset_rank),

        ("adminlogs", adminlogs),
        ("report", report_command),
        ("approve", approve_command),
        ("decline", decline_command),
        ("block", block_command),
        ("unblock", unblock_command),
        ("blocklist", blocklist_command),
        ("tagall", tagall_command),
        ("tr", translate_command),
        ("translate", translate_command),

    ]

    for name, func in handlers:
        app.add_handler(CommandHandler(name, func))

    # Member join/leave events.
    app.add_handler(
        ChatMemberHandler(new_member, ChatMemberHandler.CHAT_MEMBER)
    )
    app.add_handler(ChatJoinRequestHandler(join_request_handler))

    # All non-command group messages, including photo/sticker/media.
    app.add_handler(
        MessageHandler(~filters.COMMAND, message_handler)
    )

    app.add_error_handler(error_handler)

    print("🤖 Blackberry Bot is starting...")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
