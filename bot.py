import os
import logging
import sqlite3
from datetime import date
import telebot
from telebot import types

# ═══════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_URL    = os.environ.get("ADMIN_URL", "https://t.me/YourAdminUsername")
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "0"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "YourBotUsername")
DB_PATH      = os.environ.get("DB_PATH", "bot_database.db")

# Withdraw settings
MIN_WITHDRAW  = 0.20   # USD
WITHDRAW_FEE  = 0.025  # USD

# ═══════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id       INTEGER PRIMARY KEY,
                username      TEXT,
                first_name    TEXT,
                joined_at     TEXT DEFAULT (date('now')),
                balance       REAL DEFAULT 0.0,
                total_earned  REAL DEFAULT 0.0,
                referral_code TEXT UNIQUE,
                referred_by   INTEGER,
                language      TEXT DEFAULT 'en'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL,
                platform     TEXT NOT NULL,
                task_type    TEXT NOT NULL,
                file_id      TEXT,
                submitted_at TEXT DEFAULT (datetime('now')),
                status       TEXT DEFAULT 'pending',
                FOREIGN KEY (chat_id) REFERENCES users(chat_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL,
                method       TEXT NOT NULL,
                address      TEXT NOT NULL,
                amount       REAL NOT NULL,
                fee          REAL NOT NULL,
                net_amount   REAL NOT NULL,
                requested_at TEXT DEFAULT (datetime('now')),
                status       TEXT DEFAULT 'pending',
                FOREIGN KEY (chat_id) REFERENCES users(chat_id)
            )
        """)
        conn.commit()
    logger.info("✅ Database initialised at %s", DB_PATH)


def ensure_user(message: types.Message) -> None:
    chat_id = message.chat.id
    with get_db() as conn:
        existing = conn.execute(
            "SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if not existing:
            referral_code = f"REF{chat_id}"
            referred_by = user_states.get(chat_id, {}).get("referred_by")
            conn.execute(
                """INSERT INTO users (chat_id, username, first_name, referral_code, referred_by)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    chat_id,
                    message.from_user.username or "",
                    message.from_user.first_name or "",
                    referral_code,
                    referred_by,
                ),
            )
            conn.commit()
            logger.info("New user registered: %s (referred by %s)", chat_id, referred_by)


def get_user(chat_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()


def get_balance(chat_id: int) -> float:
    row = get_user(chat_id)
    return row["balance"] if row else 0.0


def get_total_users() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        return row["cnt"]


def get_top_earners(limit: int = 10):
    with get_db() as conn:
        return conn.execute(
            "SELECT first_name, username, total_earned FROM users ORDER BY total_earned DESC LIMIT ?",
            (limit,)
        ).fetchall()


def get_referral_count(chat_id: int) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM users WHERE referred_by = ?", (chat_id,)
        ).fetchone()
        return row["cnt"]


def save_submission(chat_id: int, platform: str, task_type: str, file_id: str) -> int:
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO submissions (chat_id, platform, task_type, file_id)
               VALUES (?, ?, ?, ?)""",
            (chat_id, platform, task_type, file_id),
        )
        conn.commit()
        return cursor.lastrowid


def save_withdrawal(chat_id: int, method: str, address: str, amount: float) -> int:
    fee = WITHDRAW_FEE
    net = round(amount - fee, 4)
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO withdrawals (chat_id, method, address, amount, fee, net_amount)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (chat_id, method, address, amount, fee, net),
        )
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE chat_id = ?",
            (amount, chat_id)
        )
        conn.commit()
        return cursor.lastrowid


# ═══════════════════════════════════════════════════════════
#  BOT & STATE
# ═══════════════════════════════════════════════════════════
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

user_states: dict[int, dict] = {}

STATE_IDLE            = "idle"
STATE_WAITING_FILE    = "waiting_file"
STATE_WAITING_ADDRESS = "waiting_address"
STATE_WAITING_AMOUNT  = "waiting_amount"

# ═══════════════════════════════════════════════════════════
#  TASK METADATA
# ═══════════════════════════════════════════════════════════
TASK_META = {
    "insta_2fa": {
        "platform": "Instagram",
        "label": "Instagram 2FA",
        "price": "$0.21",
        "price_val": 0.21,
        "review_min": 60,
        "description": (
            "Create a brand new Instagram account using only a real mobile device.\n\n"
            "🔒 IMPORTANT\n"
            "Use only the credentials provided by the bot to register.\n\n"
            "❗ Using your own personal information will result in REJECTION."
        ),
        "columns": "A. Username  |  B. Password  |  C. 2FA Code",
    },
    "fb_account": {
        "platform": "Facebook",
        "label": "Facebook Account",
        "price": "$0.45",
        "price_val": 0.45,
        "review_min": 90,
        "description": (
            "Create a brand new Facebook account using only a real mobile device.\n\n"
            "🔒 IMPORTANT\n"
            "Use only the credentials provided by the bot to register.\n\n"
            "❗ Using your own personal information will result in REJECTION."
        ),
        "columns": "A. Email/Phone  |  B. Password  |  C. Profile Link",
    },
    "gmail_fresh": {
        "platform": "Gmail",
        "label": "Fresh Gmail",
        "price": "$1.00",
        "price_val": 1.00,
        "review_min": 45,
        "description": (
            "Create a brand new Gmail account using only a real mobile device.\n\n"
            "🔒 IMPORTANT\n"
            "Use only the credentials provided by the bot to register.\n\n"
            "❗ Using your own personal information will result in REJECTION."
        ),
        "columns": "A. Email  |  B. Password  |  C. Recovery Email",
    },
}

WITHDRAW_METHODS = {
    "bkash":    {"label": "bKash",         "fee": WITHDRAW_FEE, "min": MIN_WITHDRAW},
    "nagad":    {"label": "Nagad",         "fee": WITHDRAW_FEE, "min": MIN_WITHDRAW},
    "usdt_bep": {"label": "USDT (BEP-20)", "fee": WITHDRAW_FEE, "min": MIN_WITHDRAW},
}

# ═══════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════

def main_menu_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("💰 Balance", "📋 Tasks")
    kb.row("📤 Withdraw", "👤 Profile")
    kb.row("🏆 Top")
    kb.row("👥 My Referrals", "🌐 Language")
    return kb


def cancel_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("❌ Cancel")
    return kb


def platform_select_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🌸 Instagram", callback_data="platform_instagram"),
        types.InlineKeyboardButton("🔷 Facebook",  callback_data="platform_facebook"),
        types.InlineKeyboardButton("📧 Gmail",     callback_data="platform_gmail"),
    )
    return kb


def instagram_type_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔑 Set Admin Rate",                callback_data="adminrate_instagram"),
        types.InlineKeyboardButton("Instagram 2FA 🔥  ($0.21)",        callback_data="taskinfo_insta_2fa"),
    )
    return kb


def facebook_type_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔑 Set Admin Rate",                callback_data="adminrate_facebook"),
        types.InlineKeyboardButton("Facebook Account 🔥  ($0.45)",     callback_data="taskinfo_fb_account"),
    )
    return kb


def gmail_type_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔑 Set Admin Rate",                callback_data="adminrate_gmail"),
        types.InlineKeyboardButton("Fresh Gmail 🔥  ($1.00)",          callback_data="taskinfo_gmail_fresh"),
    )
    return kb


def task_action_inline(task_key: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📤 Submit File", callback_data=f"task_{task_key}"),
        types.InlineKeyboardButton("❌ Cancel",      callback_data="task_cancel"),
    )
    return kb


def withdraw_method_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📱 bKash",         callback_data="withdraw_bkash"),
        types.InlineKeyboardButton("📱 Nagad",         callback_data="withdraw_nagad"),
        types.InlineKeyboardButton("💎 USDT (BEP-20)", callback_data="withdraw_usdt_bep"),
    )
    return kb


def admin_contact_inline(label: str = "👤 Admin") -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(label, url=ADMIN_URL))
    return kb


# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════

def send_main_menu(chat_id: int, text: str = "🏠 Main Menu") -> None:
    user_states[chat_id] = {"state": STATE_IDLE}
    bot.send_message(chat_id, text, reply_markup=main_menu_keyboard())


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def notify_admin(text: str) -> None:
    if ADMIN_ID:
        try:
            bot.send_message(ADMIN_ID, text)
        except Exception as e:
            logger.warning("Admin notify failed: %s", e)


def divider() -> str:
    return "─" * 28


# ═══════════════════════════════════════════════════════════
#  /start  — handles referral too
# ═══════════════════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def handle_start(message: types.Message) -> None:
    chat_id = message.chat.id
    args = message.text.split()

    if len(args) > 1:
        ref_code = args[1]
        with get_db() as conn:
            ref_row = conn.execute(
                "SELECT chat_id FROM users WHERE referral_code = ?", (ref_code,)
            ).fetchone()
            if ref_row and ref_row["chat_id"] != chat_id:
                user_states[chat_id] = {"state": STATE_IDLE, "referred_by": ref_row["chat_id"]}

    ensure_user(message)
    user_states[chat_id] = {"state": STATE_IDLE}

    name = message.from_user.first_name or "Friend"
    welcome = (
        f"👋 Hello, {name}!\n\n"
        f"ℹ️ This bot helps you earn money by completing simple tasks.\n\n"
        f"By using this bot, you automatically agree to the Terms of Use and Privacy Policy.\n\n"
        f"Use the menu below to get started."
    )
    bot.send_message(chat_id, welcome, reply_markup=main_menu_keyboard())


# ═══════════════════════════════════════════════════════════
#  MAIN MENU BUTTONS
# ═══════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "📋 Tasks")
def handle_task_submit(message: types.Message) -> None:
    ensure_user(message)
    user_states[message.chat.id] = {"state": STATE_IDLE}
    bot.send_message(
        message.chat.id,
        "✨ Select a platform to get started 👇",
        reply_markup=platform_select_inline(),
    )


@bot.message_handler(func=lambda m: m.text == "💰 Balance")
def handle_balance(message: types.Message) -> None:
    ensure_user(message)
    user = get_user(message.chat.id)
    balance      = user["balance"]      if user else 0.0
    total_earned = user["total_earned"] if user else 0.0
    text = (
        f"💰 Balance\n"
        f"{divider()}\n"
        f"💵 Current Balance:  ${balance:.4f}\n"
        f"📈 Total Earned:     ${total_earned:.4f}\n"
        f"{divider()}\n"
        f"💸 Min Withdraw:     ${MIN_WITHDRAW:.2f}\n"
        f"📋 Withdraw Fee:     ${WITHDRAW_FEE:.3f}"
    )
    bot.send_message(message.chat.id, text)


@bot.message_handler(func=lambda m: m.text == "📤 Withdraw")
def handle_withdraw_menu(message: types.Message) -> None:
    ensure_user(message)
    balance = get_balance(message.chat.id)
    if balance < MIN_WITHDRAW:
        bot.send_message(
            message.chat.id,
            f"⚠️ Insufficient balance.\n\n"
            f"💰 Your Balance:   ${balance:.4f}\n"
            f"📋 Minimum:        ${MIN_WITHDRAW:.2f}\n\n"
            f"Complete more tasks to reach the withdrawal threshold.",
        )
        return
    user_states[message.chat.id] = {"state": STATE_IDLE}
    bot.send_message(
        message.chat.id,
        f"📤 Choose Withdraw Method\n"
        f"{divider()}\n"
        f"💰 Your Balance:  ${balance:.4f}\n"
        f"💸 Fee:           ${WITHDRAW_FEE:.3f}\n"
        f"📋 Minimum:       ${MIN_WITHDRAW:.2f}",
        reply_markup=withdraw_method_inline(),
    )


@bot.message_handler(func=lambda m: m.text == "👤 Profile")
def handle_profile(message: types.Message) -> None:
    ensure_user(message)
    chat_id = message.chat.id
    user = get_user(chat_id)
    if not user:
        bot.send_message(chat_id, "⚠️ Profile not found.")
        return

    ref_count = get_referral_count(chat_id)
    with get_db() as conn:
        sub_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM submissions WHERE chat_id = ?", (chat_id,)
        ).fetchone()["cnt"]
        withdraw_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM withdrawals WHERE chat_id = ? AND status = 'paid'", (chat_id,)
        ).fetchone()["cnt"]

    name     = user["first_name"] or "N/A"
    username = f"@{user['username']}" if user["username"] else "N/A"
    text = (
        f"👤 Profile\n"
        f"{divider()}\n"
        f"📛 Name:            {name}\n"
        f"🔗 Username:        {username}\n"
        f"🆔 Chat ID:         {chat_id}\n"
        f"📅 Joined:          {user['joined_at']}\n"
        f"{divider()}\n"
        f"💵 Balance:         ${user['balance']:.4f}\n"
        f"📈 Total Earned:    ${user['total_earned']:.4f}\n"
        f"{divider()}\n"
        f"📁 Submissions:     {sub_count}\n"
        f"💸 Withdrawals:     {withdraw_count}\n"
        f"👥 Referrals:       {ref_count}\n"
        f"🎫 Referral Code:   {user['referral_code']}"
    )
    bot.send_message(chat_id, text)


@bot.message_handler(func=lambda m: m.text == "🏆 Top")
def handle_top(message: types.Message) -> None:
    earners = get_top_earners(10)
    total_users = get_total_users()
    lines = [
        f"🏆 Top 10 Earners",
        divider(),
    ]
    for i, row in enumerate(earners, 1):
        name  = row["first_name"] or "Unknown"
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"  {i}.")
        lines.append(f"{medal}  {name}  —  ${row['total_earned']:.4f}")
    lines.append(divider())
    lines.append(f"👥 Total Users: {total_users}")
    bot.send_message(message.chat.id, "\n".join(lines))


@bot.message_handler(func=lambda m: m.text == "👥 My Referrals")
def handle_my_referrals(message: types.Message) -> None:
    ensure_user(message)
    chat_id = message.chat.id
    user = get_user(chat_id)
    ref_count = get_referral_count(chat_id)
    code = user["referral_code"] if user else f"REF{chat_id}"
    invite_link = f"https://t.me/{BOT_USERNAME}?start={code}"
    bot.send_message(
        chat_id,
        f"👥 My Referrals\n"
        f"{divider()}\n"
        f"✅ Total Referrals:  {ref_count}\n"
        f"🎁 Bonus per Refer:  Coming soon\n"
        f"{divider()}\n"
        f"🔗 Your Invite Link:\n{invite_link}\n\n"
        "Share with friends and earn bonuses!",
    )


@bot.message_handler(func=lambda m: m.text == "🌐 Language")
def handle_language(message: types.Message) -> None:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
        types.InlineKeyboardButton("🇧🇩 বাংলা",   callback_data="lang_bn"),
    )
    bot.send_message(
        message.chat.id,
        "🌐 Select your language:",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def handle_language_select(call: types.CallbackQuery) -> None:
    lang = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    lang_name = "English 🇬🇧" if lang == "en" else "বাংলা 🇧🇩"
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET language = ? WHERE chat_id = ?",
            (lang, call.message.chat.id)
        )
        conn.commit()
    bot.send_message(
        call.message.chat.id,
        f"✅ Language set to {lang_name}",
        reply_markup=main_menu_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "❌ Cancel")
def handle_cancel(message: types.Message) -> None:
    send_main_menu(message.chat.id, "✅ Operation cancelled.")


# ═══════════════════════════════════════════════════════════
#  INLINE CALLBACKS — Platform & Task
# ═══════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("platform_"))
def handle_platform_select(call: types.CallbackQuery) -> None:
    platform = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    menus = {
        "instagram": ("🌸 Instagram — Select a task:", instagram_type_inline()),
        "facebook":  ("🔷 Facebook — Select a task:",  facebook_type_inline()),
        "gmail":     ("📧 Gmail — Select a task:",     gmail_type_inline()),
    }
    if platform in menus:
        text, kb = menus[platform]
        bot.send_message(call.message.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adminrate_"))
def handle_admin_rate(call: types.CallbackQuery) -> None:
    bot.answer_callback_query(call.id)
    platform = call.data.split("_", 1)[1].capitalize()
    bot.send_message(
        call.message.chat.id,
        f"🔑 Contact admin to set a custom {platform} rate:",
        reply_markup=admin_contact_inline("👤 Admin"),
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("taskinfo_"))
def handle_task_info(call: types.CallbackQuery) -> None:
    task_key = call.data[9:]
    bot.answer_callback_query(call.id)
    meta = TASK_META.get(task_key)
    if not meta:
        bot.send_message(call.message.chat.id, "⚠️ Unknown task.")
        return

    text = (
        f"📋 Task: {meta['label']}\n"
        f"{divider()}\n"
        f"💵 Reward:       {meta['price']}\n"
        f"⏳ Review Time:  {meta['review_min']} minutes\n"
        f"{divider()}\n"
        f"📄 Instructions:\n{meta['description']}\n"
        f"{divider()}\n"
        f"📂 Required Columns:\n{meta['columns']}"
    )
    bot.send_message(
        call.message.chat.id,
        text,
        reply_markup=task_action_inline(task_key),
    )


@bot.callback_query_handler(func=lambda c: c.data == "task_cancel")
def handle_task_cancel_inline(call: types.CallbackQuery) -> None:
    bot.answer_callback_query(call.id)
    send_main_menu(call.message.chat.id, "✅ Operation cancelled.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("task_") and c.data != "task_cancel")
def handle_task_select(call: types.CallbackQuery) -> None:
    task_key = call.data[5:]
    bot.answer_callback_query(call.id)
    meta = TASK_META.get(task_key)
    if not meta:
        bot.send_message(call.message.chat.id, "⚠️ Unknown task.")
        return

    chat_id = call.message.chat.id
    user_states[chat_id] = {"state": STATE_WAITING_FILE, "task_key": task_key}
    text = (
        f"📤 Submit Task\n"
        f"{divider()}\n"
        f"📅 Date:    {today_str()}\n"
        f"📋 Task:    {meta['label']}\n"
        f"💵 Reward:  {meta['price']}\n"
        f"{divider()}\n"
        f"📂 Required Columns:\n{meta['columns']}\n"
        f"{divider()}\n"
        "Please upload your .xlsx file now.\n"
        "Press ❌ Cancel to go back."
    )
    bot.send_message(chat_id, text, reply_markup=cancel_keyboard())


# ═══════════════════════════════════════════════════════════
#  INLINE CALLBACKS — Withdraw
# ═══════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("withdraw_"))
def handle_withdraw_method(call: types.CallbackQuery) -> None:
    method_key = call.data[9:]
    bot.answer_callback_query(call.id)
    method = WITHDRAW_METHODS.get(method_key)
    if not method:
        return

    chat_id = call.message.chat.id
    balance = get_balance(chat_id)
    user_states[chat_id] = {
        "state": STATE_WAITING_AMOUNT,
        "withdraw_method": method_key,
        "withdraw_label": method["label"],
    }
    bot.send_message(
        chat_id,
        f"✅ You selected {method['label']}.\n"
        f"{divider()}\n"
        f"💸 Fee:      ${method['fee']:.3f}\n"
        f"🔢 Minimum:  ${method['min']:.2f}\n"
        f"💰 Balance:  ${balance:.4f}\n"
        f"{divider()}\n"
        "How much would you like to withdraw?\n(Enter numbers only)",
        reply_markup=cancel_keyboard(),
    )


# ═══════════════════════════════════════════════════════════
#  TEXT — Withdraw amount & address
# ═══════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("state") == STATE_WAITING_AMOUNT)
def handle_withdraw_amount(message: types.Message) -> None:
    chat_id = message.chat.id
    try:
        amount = float(message.text.strip())
    except ValueError:
        bot.send_message(chat_id, "⚠️ Please enter a valid number.", reply_markup=cancel_keyboard())
        return

    balance = get_balance(chat_id)
    if amount < MIN_WITHDRAW:
        bot.send_message(chat_id, f"⚠️ Minimum withdrawal amount is ${MIN_WITHDRAW:.2f}.", reply_markup=cancel_keyboard())
        return
    if amount > balance:
        bot.send_message(chat_id, f"⚠️ Insufficient balance. Your balance: ${balance:.4f}", reply_markup=cancel_keyboard())
        return

    state = user_states[chat_id]
    state["state"]           = STATE_WAITING_ADDRESS
    state["withdraw_amount"] = amount
    label = state["withdraw_label"]

    bot.send_message(
        chat_id,
        f"💵 Amount:     ${amount:.4f}\n"
        f"📋 Fee:        ${WITHDRAW_FEE:.3f}\n"
        f"✅ You'll get: ${amount - WITHDRAW_FEE:.4f}\n"
        f"{divider()}\n"
        f"📲 Enter your {label} number / address:",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("state") == STATE_WAITING_ADDRESS)
def handle_withdraw_address(message: types.Message) -> None:
    chat_id = message.chat.id
    address = message.text.strip()
    state   = user_states[chat_id]

    method_key = state["withdraw_method"]
    amount     = state["withdraw_amount"]
    label      = state["withdraw_label"]

    wid = save_withdrawal(chat_id, label, address, amount)
    net = round(amount - WITHDRAW_FEE, 4)

    bot.send_message(
        chat_id,
        f"✅ Withdrawal Request Submitted!\n"
        f"{divider()}\n"
        f"🆔 Request ID:  #{wid}\n"
        f"💳 Method:      {label}\n"
        f"📲 Address:     {address}\n"
        f"💰 Amount:      ${amount:.4f}\n"
        f"📋 Fee:         ${WITHDRAW_FEE:.3f}\n"
        f"💵 You'll get:  ${net:.4f}\n"
        f"{divider()}\n"
        "⏳ Admin will process within 24 hours.",
    )

    notify_admin(
        f"💸 New Withdrawal Request #{wid}\n"
        f"👤 Chat ID:  {chat_id}\n"
        f"💳 Method:   {label}\n"
        f"📲 Address:  {address}\n"
        f"💰 Amount:   ${amount:.4f}  |  Net: ${net:.4f}"
    )
    send_main_menu(chat_id)


# ═══════════════════════════════════════════════════════════
#  FILE HANDLER
# ═══════════════════════════════════════════════════════════

@bot.message_handler(content_types=["document"])
def handle_document(message: types.Message) -> None:
    chat_id    = message.chat.id
    state_info = user_states.get(chat_id, {})

    if state_info.get("state") != STATE_WAITING_FILE:
        bot.send_message(chat_id, "⚠️ Unexpected file. Please select a task first.")
        return

    task_key = state_info.get("task_key", "")
    meta     = TASK_META.get(task_key)
    if not meta:
        bot.send_message(chat_id, "⚠️ Task data not found. Please try again.")
        send_main_menu(chat_id)
        return

    file_name = message.document.file_name or ""
    if not file_name.lower().endswith(".xlsx"):
        bot.send_message(chat_id, "❌ Only .xlsx files are accepted. Please send the correct file.")
        return

    submission_id = save_submission(
        chat_id=chat_id,
        platform=meta["platform"],
        task_type=meta["label"],
        file_id=message.document.file_id,
    )

    logger.info("Submission #%d | chat_id=%s | %s | %s", submission_id, chat_id, meta["platform"], file_name)

    notify_admin(
        f"📁 New Submission #{submission_id}\n"
        f"👤 Chat ID:   {chat_id}\n"
        f"📌 Platform:  {meta['platform']} — {meta['label']} ({meta['price']})\n"
        f"📄 File:      {file_name}"
    )

    bot.send_message(
        chat_id,
        f"✅ File Submitted Successfully!\n"
        f"{divider()}\n"
        f"🆔 Submission ID:  #{submission_id}\n"
        f"📌 Platform:       {meta['platform']}\n"
        f"📋 Task:           {meta['label']}\n"
        f"💵 Reward:         {meta['price']}\n"
        f"📅 Date:           {today_str()}\n"
        f"⏳ Review Time:    {meta['review_min']} minutes\n"
        f"{divider()}\n"
        "Once reviewed by admin, the reward will be added to your balance.",
    )
    send_main_menu(chat_id)


# ═══════════════════════════════════════════════════════════
#  CATCH-ALL
# ═══════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: True)
def handle_fallback(message: types.Message) -> None:
    chat_id = message.chat.id
    state   = user_states.get(chat_id, {}).get("state")

    if state == STATE_WAITING_FILE:
        bot.send_message(
            chat_id,
            "⚠️ Please upload a .xlsx file or press ❌ Cancel.",
            reply_markup=cancel_keyboard(),
        )
    elif state in (STATE_WAITING_ADDRESS, STATE_WAITING_AMOUNT):
        bot.send_message(
            chat_id,
            "⚠️ Please enter valid information or press ❌ Cancel.",
            reply_markup=cancel_keyboard(),
        )
    else:
        send_main_menu(chat_id, "🏠 Main Menu")


# ═══════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    logger.info("🤖 Bot starting — polling...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
