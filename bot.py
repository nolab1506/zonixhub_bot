import os
import logging
import sqlite3
from datetime import date
import telebot
from telebot import types

# ─────────────────────────────────────────────
#  Configuration  (set these as env variables)
# ─────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_URL    = os.environ.get("ADMIN_URL", "https://t.me/YourAdminUsername")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "YourBotUsername")
DB_PATH      = os.environ.get("DB_PATH", "bot_database.db")

# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  Database Setup
# ─────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                joined_at   TEXT DEFAULT (date('now')),
                balance     REAL DEFAULT 0.0,
                total_earned REAL DEFAULT 0.0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                platform    TEXT NOT NULL,
                task_type   TEXT NOT NULL,
                file_id     TEXT,
                submitted_at TEXT DEFAULT (datetime('now')),
                status      TEXT DEFAULT 'pending',
                FOREIGN KEY (chat_id) REFERENCES users(chat_id)
            )
        """)
        conn.commit()
    logger.info("Database initialised at %s", DB_PATH)


def ensure_user(message: types.Message) -> None:
    """Insert user if not already in DB."""
    chat_id = message.chat.id
    with get_db() as conn:
        existing = conn.execute(
            "SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if not existing:
            referral_code = f"REF{chat_id}"
            conn.execute(
                """INSERT INTO users (chat_id, username, first_name, referral_code)
                   VALUES (?, ?, ?, ?)""",
                (
                    chat_id,
                    message.from_user.username or "",
                    message.from_user.first_name or "",
                    referral_code,
                ),
            )
            conn.commit()
            logger.info("New user registered: %s", chat_id)


def get_balance(chat_id: int) -> float:
    with get_db() as conn:
        row = conn.execute(
            "SELECT balance FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row["balance"] if row else 0.0


def save_submission(chat_id: int, platform: str, task_type: str, file_id: str) -> int:
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO submissions (chat_id, platform, task_type, file_id)
               VALUES (?, ?, ?, ?)""",
            (chat_id, platform, task_type, file_id),
        )
        conn.commit()
        return cursor.lastrowid


# ─────────────────────────────────────────────
#  Bot instance
# ─────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ─────────────────────────────────────────────
#  In-memory state store  {chat_id: state_dict}
# ─────────────────────────────────────────────
user_states: dict[int, dict] = {}

STATE_IDLE         = "idle"
STATE_WAITING_FILE = "waiting_file"

# ─────────────────────────────────────────────
#  Task metadata
# ─────────────────────────────────────────────
TASK_META = {
    "insta_2fa": {
        "platform": "Instagram",
        "label": "Insta 2FA",
        "price": "2.10TK",
        "columns": "A. Username  |  B. Password  |  C. 2FA Code",
    },
    "fb_account": {
        "platform": "Facebook",
        "label": "FB Account",
        "price": "4.50TK",
        "columns": "A. Email/Phone  |  B. Password  |  C. Profile Link",
    },
    "gmail_fresh": {
        "platform": "Gmail",
        "label": "Fresh Gmail",
        "price": "10.00TK",
        "columns": "A. Email  |  B. Password  |  C. Recovery Email",
    },
}


# ══════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════

def main_menu_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📁 কাজ সাবমিট", "💵 ব্যালেন্স")
    kb.row("💸 উইথড্র", "🎁 Invite & Earn")
    kb.row("☎️ সাপোর্ট", "🆕 আমি নতুন?")
    return kb


def cancel_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("❌ বাতিল")
    return kb


def platform_select_inline() -> types.InlineKeyboardMarkup:
    """Step 1 – choose platform."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🌸 Instagram", callback_data="platform_instagram"),
        types.InlineKeyboardButton("🔷 Facebook",  callback_data="platform_facebook"),
        types.InlineKeyboardButton("📧 Gmail",     callback_data="platform_gmail"),
    )
    return kb


def instagram_type_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔑 set admin rate", callback_data="adminrate_instagram"),
        types.InlineKeyboardButton("Insta 2FA 🔥 (2.10TK)", callback_data="task_insta_2fa"),
    )
    return kb


def facebook_type_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔑 set admin rate", callback_data="adminrate_facebook"),
        types.InlineKeyboardButton("FB Account 🔥 (4.50TK)", callback_data="task_fb_account"),
    )
    return kb


def gmail_type_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔑 set admin rate", callback_data="adminrate_gmail"),
        types.InlineKeyboardButton("Fresh Gmail 🔥 (10.00TK)", callback_data="task_gmail_fresh"),
    )
    return kb


def admin_contact_inline(label: str = "👤 অ্যাডমিন") -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(label, url=ADMIN_URL))
    return kb


# ══════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════

def send_main_menu(chat_id: int, text: str = "প্রধান মেনুতে ফিরে যাওয়া হয়েছে।") -> None:
    user_states[chat_id] = {"state": STATE_IDLE}
    bot.send_message(chat_id, text, reply_markup=main_menu_keyboard())


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def prompt_file_upload(chat_id: int, task_key: str) -> None:
    """Ask user to upload .xlsx for the chosen task."""
    meta = TASK_META[task_key]
    user_states[chat_id] = {"state": STATE_WAITING_FILE, "task_key": task_key}
    text = (
        f"📅 তারিখ: {today_str()}\n\n"
        f"✅ আপনি সিলেক্ট করেছেন: {meta['label']} ({meta['price']})\n\n"
        f"📂 অনুগ্রহ করে আপনার .xlsx ফাইল আপলোড করুন।\n\n"
        f"📋 প্রয়োজনীয় কলাম:\n{meta['columns']}\n\n"
        "❌ বাতিল করতে নিচের বাটন চাপুন।"
    )
    bot.send_message(chat_id, text, reply_markup=cancel_keyboard())


# ══════════════════════════════════════════════
#  /start  command
# ══════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def handle_start(message: types.Message) -> None:
    ensure_user(message)
    user_states[message.chat.id] = {"state": STATE_IDLE}
    name = message.from_user.first_name or "বন্ধু"
    welcome = (
        f"আসসালামু আলাইকুম, {name}! 👋\n\n"
        "🤖 আমাদের Earning Bot-এ স্বাগতম!\n"
        "নিচের মেনু থেকে আপনার পছন্দের অপশনটি বেছে নিন।"
    )
    bot.send_message(message.chat.id, welcome, reply_markup=main_menu_keyboard())


# ══════════════════════════════════════════════
#  REPLY KEYBOARD  handlers
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "📁 কাজ সাবমিট")
def handle_task_submit(message: types.Message) -> None:
    ensure_user(message)
    user_states[message.chat.id] = {"state": STATE_IDLE}
    bot.send_message(
        message.chat.id,
        "✨ যেকোনো একটি প্ল্যাটফর্ম সিলেক্ট করুন 👇",
        reply_markup=platform_select_inline(),
    )


@bot.message_handler(func=lambda m: m.text == "💵 ব্যালেন্স")
def handle_balance(message: types.Message) -> None:
    ensure_user(message)
    balance = get_balance(message.chat.id)
    bot.send_message(
        message.chat.id,
        f"💰 আপনার বর্তমান ব্যালেন্স: {balance:.2f} TK",
    )


@bot.message_handler(func=lambda m: m.text == "💸 উইথড্র")
def handle_withdraw(message: types.Message) -> None:
    ensure_user(message)
    balance = get_balance(message.chat.id)
    bot.send_message(
        message.chat.id,
        f"💸 উইথড্র করতে অ্যাডমিনের সাথে যোগাযোগ করুন।\nআপনার ব্যালেন্স: {balance:.2f} TK",
        reply_markup=admin_contact_inline("👤 অ্যাডমিনকে মেসেজ করুন"),
    )


@bot.message_handler(func=lambda m: m.text == "🎁 Invite & Earn")
def handle_invite(message: types.Message) -> None:
    ensure_user(message)
    with get_db() as conn:
        row = conn.execute(
            "SELECT referral_code FROM users WHERE chat_id = ?", (message.chat.id,)
        ).fetchone()
    code = row["referral_code"] if row else f"REF{message.chat.id}"
    invite_link = f"https://t.me/{BOT_USERNAME}?start={code}"
    bot.send_message(
        message.chat.id,
        f"🎁 আপনার ইনভাইট লিংক:\n{invite_link}\n\nবন্ধুদের শেয়ার করুন এবং বোনাস আয় করুন!",
    )


@bot.message_handler(func=lambda m: m.text == "☎️ সাপোর্ট")
def handle_support(message: types.Message) -> None:
    bot.send_message(
        message.chat.id,
        "☎️ সাপোর্টের জন্য অ্যাডমিনের সাথে যোগাযোগ করুন:",
        reply_markup=admin_contact_inline("👤 সাপোর্ট"),
    )


@bot.message_handler(func=lambda m: m.text == "🆕 আমি নতুন?")
def handle_new_user(message: types.Message) -> None:
    ensure_user(message)
    bot.send_message(
        message.chat.id,
        (
            "🆕 নতুন ব্যবহারকারী গাইড:\n\n"
            "1️⃣ '📁 কাজ সাবমিট' চাপুন\n"
            "2️⃣ প্ল্যাটফর্ম বেছে নিন (Instagram / Facebook / Gmail)\n"
            "3️⃣ কাজের ধরন সিলেক্ট করুন\n"
            "4️⃣ নির্দিষ্ট কলাম সহ .xlsx ফাইল আপলোড করুন\n"
            "5️⃣ কাজ যাচাই হলে ব্যালেন্স যোগ হবে\n\n"
            "❓ সমস্যা হলে '☎️ সাপোর্ট' বাটন চাপুন।"
        ),
        reply_markup=main_menu_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "❌ বাতিল")
def handle_cancel(message: types.Message) -> None:
    send_main_menu(message.chat.id, "❌ অপারেশন বাতিল করা হয়েছে।")


# ══════════════════════════════════════════════
#  INLINE KEYBOARD  callbacks
# ══════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("platform_"))
def handle_platform_select(call: types.CallbackQuery) -> None:
    platform = call.data.split("_", 1)[1]  # instagram / facebook / gmail
    bot.answer_callback_query(call.id)

    if platform == "instagram":
        bot.send_message(
            call.message.chat.id,
            "🌸 Instagram – টাস্ক সিলেক্ট করুন:",
            reply_markup=instagram_type_inline(),
        )
    elif platform == "facebook":
        bot.send_message(
            call.message.chat.id,
            "🔷 Facebook – টাস্ক সিলেক্ট করুন:",
            reply_markup=facebook_type_inline(),
        )
    elif platform == "gmail":
        bot.send_message(
            call.message.chat.id,
            "📧 Gmail – টাস্ক সিলেক্ট করুন:",
            reply_markup=gmail_type_inline(),
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("adminrate_"))
def handle_admin_rate(call: types.CallbackQuery) -> None:
    bot.answer_callback_query(call.id)
    platform = call.data.split("_", 1)[1].capitalize()
    bot.send_message(
        call.message.chat.id,
        f"🔑 {platform} অ্যাডমিন রেট সেট করতে অ্যাডমিনের সাথে যোগাযোগ করুন:",
        reply_markup=admin_contact_inline("👤 অ্যাডমিন"),
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("task_"))
def handle_task_select(call: types.CallbackQuery) -> None:
    task_key = call.data[5:]   # e.g. "insta_2fa", "fb_account", "gmail_fresh"
    bot.answer_callback_query(call.id)

    if task_key not in TASK_META:
        bot.send_message(call.message.chat.id, "⚠️ অজানা টাস্ক।")
        return

    prompt_file_upload(call.message.chat.id, task_key)


# ══════════════════════════════════════════════
#  FILE / DOCUMENT  handler
# ══════════════════════════════════════════════

@bot.message_handler(content_types=["document"])
def handle_document(message: types.Message) -> None:
    chat_id = message.chat.id
    state_info = user_states.get(chat_id, {})

    if state_info.get("state") != STATE_WAITING_FILE:
        bot.send_message(chat_id, "⚠️ অপ্রত্যাশিত ফাইল। প্রথমে একটি টাস্ক সিলেক্ট করুন।")
        return

    task_key = state_info.get("task_key", "")
    meta = TASK_META.get(task_key)
    if not meta:
        bot.send_message(chat_id, "⚠️ টাস্ক তথ্য পাওয়া যায়নি। আবার চেষ্টা করুন।")
        send_main_menu(chat_id)
        return

    # Validate .xlsx extension
    file_name = message.document.file_name or ""
    if not file_name.lower().endswith(".xlsx"):
        bot.send_message(
            chat_id,
            "❌ শুধুমাত্র .xlsx ফাইল গ্রহণযোগ্য। সঠিক ফাইল পাঠান।",
        )
        return

    # Save submission to DB
    submission_id = save_submission(
        chat_id=chat_id,
        platform=meta["platform"],
        task_type=meta["label"],
        file_id=message.document.file_id,
    )

    logger.info(
        "Submission #%d | chat_id=%s | platform=%s | task=%s | file=%s",
        submission_id, chat_id, meta["platform"], meta["label"], file_name,
    )

    bot.send_message(
        chat_id,
        (
            f"✅ ফাইল সফলভাবে জমা হয়েছে!\n\n"
            f"🆔 সাবমিশন ID: #{submission_id}\n"
            f"📌 প্ল্যাটফর্ম: {meta['platform']}\n"
            f"📋 টাস্ক: {meta['label']} ({meta['price']})\n"
            f"📅 তারিখ: {today_str()}\n\n"
            "⏳ অ্যাডমিন যাচাই করার পর আপনার ব্যালেন্সে যোগ হবে।"
        ),
    )
    send_main_menu(chat_id)


# ══════════════════════════════════════════════
#  CATCH-ALL  (unexpected text while waiting for file)
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: True)
def handle_fallback(message: types.Message) -> None:
    chat_id = message.chat.id
    state_info = user_states.get(chat_id, {})

    if state_info.get("state") == STATE_WAITING_FILE:
        bot.send_message(
            chat_id,
            "⚠️ অনুগ্রহ করে একটি .xlsx ফাইল পাঠান অথবা বাতিল করতে ❌ বাতিল চাপুন।",
            reply_markup=cancel_keyboard(),
        )
    else:
        send_main_menu(chat_id, "🔄 প্রধান মেনু:")


# ══════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    logger.info("Bot starting — polling...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
