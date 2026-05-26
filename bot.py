import os
import logging
import sqlite3
from datetime import date, datetime, timedelta
import telebot
from telebot import types

# ═══════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    raise ValueError("FATAL: BOT_TOKEN environment variable is missing or invalid!")

ADMIN_URL    = os.environ.get("ADMIN_URL", "https://t.me/nolab420")
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "8036268898"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "YourBotUsername")
DB_PATH      = os.environ.get("DB_PATH", "bot_database.db")

# Rates & Limits
DOLLAR_RATE       = 120.0
MIN_WITHDRAW_BDT  = 20.00
MIN_WITHDRAW_USDT = 0.20
WITHDRAW_FEE_BDT  = 2.50
WITHDRAW_FEE_USDT = 0.01
REFERRAL_COMM_PCT = 0.20
WITHDRAW_COOLDOWN = 24

# States
STATE_IDLE            = "idle"
STATE_WAITING_FILE    = "waiting_file"
STATE_WAITING_ADDRESS = "waiting_address"
STATE_WAITING_AMOUNT  = "waiting_amount"
STATE_BROADCAST       = "broadcast"

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

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id          INTEGER PRIMARY KEY,
                username         TEXT,
                first_name       TEXT,
                joined_at        TEXT DEFAULT (date('now')),
                balance          REAL DEFAULT 0.0,
                total_earned     REAL DEFAULT 0.0,
                referral_code    TEXT UNIQUE,
                referred_by      INTEGER,
                language         TEXT DEFAULT 'English',
                currency         TEXT DEFAULT 'BDT',
                current_state    TEXT DEFAULT 'idle',
                current_task_key TEXT,
                is_banned        INTEGER DEFAULT 0
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
                currency     TEXT DEFAULT 'BDT',
                requested_at TEXT DEFAULT (datetime('now')),
                status       TEXT DEFAULT 'pending',
                FOREIGN KEY (chat_id) REFERENCES users(chat_id)
            )
        """)
        conn.commit()
    _migrate_db()
    logger.info("Database initialised at %s", DB_PATH)


def _migrate_db():
    with get_db() as conn:
        u_cols  = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        wd_cols = {r[1] for r in conn.execute("PRAGMA table_info(withdrawals)").fetchall()}
        if "is_banned" not in u_cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
        if "currency" not in u_cols:
            conn.execute("ALTER TABLE users ADD COLUMN currency TEXT DEFAULT 'BDT'")
        if "currency" not in wd_cols:
            conn.execute("ALTER TABLE withdrawals ADD COLUMN currency TEXT DEFAULT 'BDT'")
        conn.commit()


def ensure_user(message, referred_by=None):
    chat_id = message.chat.id
    with get_db() as conn:
        if not conn.execute("SELECT chat_id FROM users WHERE chat_id=?", (chat_id,)).fetchone():
            conn.execute(
                """INSERT INTO users
                   (chat_id, username, first_name, referral_code, referred_by,
                    current_state, language, currency)
                   VALUES (?, ?, ?, ?, ?, 'idle', 'English', 'BDT')""",
                (chat_id,
                 message.from_user.username or "",
                 message.from_user.first_name or "",
                 f"REF{chat_id}",
                 referred_by),
            )
            conn.commit()


def get_user(chat_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()


def is_banned(chat_id):
    row = get_user(chat_id)
    return bool(row and row["is_banned"])


def get_lang(chat_id):
    row = get_user(chat_id)
    return row["language"] if row else "English"


def get_currency(chat_id):
    row = get_user(chat_id)
    return row["currency"] if row else "BDT"


def is_bn(chat_id):
    return get_lang(chat_id) == "Bangla"


def set_user_state(chat_id, state, task_key=None):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET current_state=?, current_task_key=? WHERE chat_id=?",
            (state, task_key, chat_id),
        )
        conn.commit()


def set_user_lang(chat_id, lang):
    with get_db() as conn:
        conn.execute("UPDATE users SET language=? WHERE chat_id=?", (lang, chat_id))
        conn.commit()


def set_user_currency(chat_id, currency):
    with get_db() as conn:
        conn.execute("UPDATE users SET currency=? WHERE chat_id=?", (currency, chat_id))
        conn.commit()


def get_balance_usd(chat_id):
    row = get_user(chat_id)
    return round(row["balance"], 6) if row else 0.0


def fmt_amount(amount_usd, currency):
    if currency == "BDT":
        return f"{amount_usd * DOLLAR_RATE:.2f} Tk"
    return f"${amount_usd:.4f} USDT"


def fmt_price(price_bdt, currency):
    if currency == "BDT":
        return f"{price_bdt:.2f} Tk"
    return f"${price_bdt / DOLLAR_RATE:.4f} USDT"


def get_total_users():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]


def get_top_earners(limit=10):
    with get_db() as conn:
        return conn.execute(
            "SELECT first_name, total_earned FROM users ORDER BY total_earned DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_referral_count(chat_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) as c FROM users WHERE referred_by=?", (chat_id,)
        ).fetchone()["c"]


def get_user_submissions(chat_id, limit=10):
    with get_db() as conn:
        return conn.execute(
            """SELECT id, platform, task_type, status, submitted_at
               FROM submissions WHERE chat_id=?
               ORDER BY submitted_at DESC LIMIT ?""",
            (chat_id, limit),
        ).fetchall()


def get_all_chat_ids():
    with get_db() as conn:
        return [r["chat_id"] for r in
                conn.execute("SELECT chat_id FROM users WHERE is_banned=0").fetchall()]


def save_submission(chat_id, platform, task_type, file_id):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO submissions (chat_id, platform, task_type, file_id) VALUES (?,?,?,?)",
            (chat_id, platform, task_type, file_id),
        )
        conn.commit()
        return cur.lastrowid


def has_pending_submission(chat_id, task_type):
    with get_db() as conn:
        return bool(conn.execute(
            "SELECT id FROM submissions WHERE chat_id=? AND task_type=? AND status='pending' LIMIT 1",
            (chat_id, task_type),
        ).fetchone())


def get_last_withdrawal_time(chat_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT requested_at FROM withdrawals WHERE chat_id=? ORDER BY requested_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return row["requested_at"] if row else None


def save_withdrawal(chat_id, method, address, amount, currency="BDT"):
    fee = WITHDRAW_FEE_BDT if currency == "BDT" else WITHDRAW_FEE_USDT
    net = round(amount - fee, 6)
    deduct_usd = (amount / DOLLAR_RATE) if currency == "BDT" else amount
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO withdrawals (chat_id, method, address, amount, fee, net_amount, currency)
               VALUES (?,?,?,?,?,?,?)""",
            (chat_id, method, address, amount, fee, net, currency),
        )
        conn.execute(
            "UPDATE users SET balance = MAX(0, balance - ?) WHERE chat_id=?",
            (deduct_usd, chat_id),
        )
        conn.commit()
        return cur.lastrowid

# ═══════════════════════════════════════════════════════════
#  BOT INSTANCE
# ═══════════════════════════════════════════════════════════
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
withdraw_session_cache = {}

TASK_META = {
    "ig_cookies": {
        "platform": "Instagram",
        "label": "Instagram Cookies I'D",
        "price": "4.00 Tk/piece",
        "price_val": 4.00,
        "review_min": 45,
        "description": "Submit Instagram accounts generated strictly with accurate session cookies.",
        "columns": "A = Username   |   B = Password  (Only 2 Columns)",
    },
    "ig_2fa": {
        "platform": "Instagram",
        "label": "Instagram 2FA I'D",
        "price": "2.50 Tk/piece",
        "price_val": 2.50,
        "review_min": 45,
        "description": "Submit Instagram accounts with 2FA key enabled.",
        "columns": "A = Username   |   B = Password   |   C = 2FA Key",
    },
    "fb_clone_6158x": {
        "platform": "Facebook",
        "label": "PC Clone 6158x I'D",
        "price": "5.00 Tk/piece",
        "price_val": 5.00,
        "review_min": 60,
        "description": "Create Facebook Accounts via Clone Method specified by admin criteria.",
        "columns": "A = UID   |   B = Password   |   C = Cookie",
    },
    "fb_clone_1000x": {
        "platform": "Facebook",
        "label": "PC Clone 1000x I'D",
        "price": "15.00 Tk/piece",
        "price_val": 15.00,
        "review_min": 60,
        "description": "Create High Quality Facebook Accounts via specified custom configurations.",
        "columns": "A = UID   |   B = Password   |   C = Cookie",
    },
    "gmail_fresh": {
        "platform": "Gmail",
        "label": "Fresh Gmail",
        "price": "100.00 Tk/piece",
        "price_val": 100.00,
        "review_min": 45,
        "description": "Create a brand new Gmail account using unique mobile recovery setups.",
        "columns": "A. Email  |  B. Password  |  C. Recovery Email",
    },
}

# ═══════════════════════════════════════════════════════════
#  TEXT HELPER — bilingual
# ═══════════════════════════════════════════════════════════

def t(chat_id, bn_text, en_text):
    """Return Bengali or English text based on user language."""
    return bn_text if is_bn(chat_id) else en_text

# ═══════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════

def main_menu_keyboard(chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if is_bn(chat_id):
        kb.row("💰 ব্যালেন্স",     "📋 কাজ")
        kb.row("📥 উত্তোলন",      "👤 প্রোফাইল")
        kb.row("🏆 শীর্ষ",        "👥 আমার রেফারেল")
        kb.row("📂 আমার সাবমিশন")
        kb.row("💱 কারেন্সি",     "🌐 ভাষা")
        kb.row("📞 সাপোর্ট")
    else:
        kb.row("💰 Balance",      "📋 Tasks")
        kb.row("📥 Withdraw",     "👤 Profile")
        kb.row("🏆 Top",          "👥 My Referrals")
        kb.row("📂 My Submissions")
        kb.row("💱 Currency",     "🌐 Language")
        kb.row("📞 Support")
    return kb


def cancel_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("❌ Cancel")
    return kb


def platform_select_inline():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🌸 Instagram", callback_data="platform_instagram"),
        types.InlineKeyboardButton("🔷 Facebook",  callback_data="platform_facebook"),
        types.InlineKeyboardButton("📧 Gmail",     callback_data="platform_gmail"),
    )
    return kb


def _price_label(task_key, currency):
    meta = TASK_META.get(task_key)
    if not meta:
        return ""
    return fmt_price(meta["price_val"], currency)


def instagram_type_inline(currency="BDT"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔑 Set Admin Rate", callback_data="adminrate_instagram"),
        types.InlineKeyboardButton(
            f"Instagram Cookies I'D 🔥 ({_price_label('ig_cookies', currency)})",
            callback_data="taskinfo_ig_cookies"),
        types.InlineKeyboardButton(
            f"📸 Instagram 2FA I'D 🔥 ({_price_label('ig_2fa', currency)})",
            callback_data="taskinfo_ig_2fa"),
    )
    return kb


def facebook_type_inline(currency="BDT"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔑 Set Admin Rate", callback_data="adminrate_facebook"),
        types.InlineKeyboardButton(
            f"PC Clone 6158x I'D 🔥 ({_price_label('fb_clone_6158x', currency)})",
            callback_data="taskinfo_fb_clone_6158x"),
        types.InlineKeyboardButton(
            f"PC Clone 1000x I'D 🔥 ({_price_label('fb_clone_1000x', currency)})",
            callback_data="taskinfo_fb_clone_1000x"),
    )
    return kb


def gmail_type_inline(currency="BDT"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔑 Set Admin Rate", callback_data="adminrate_gmail"),
        types.InlineKeyboardButton(
            f"Fresh Gmail 🔥 ({_price_label('gmail_fresh', currency)})",
            callback_data="taskinfo_gmail_fresh"),
    )
    return kb


def task_action_inline(task_key):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📤 Submit File", callback_data=f"task_{task_key}"),
        types.InlineKeyboardButton("❌ Cancel",      callback_data="task_cancel"),
    )
    return kb


def withdraw_method_inline(currency="BDT"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if currency == "BDT":
        kb.add(
            types.InlineKeyboardButton("📱 bKash",         callback_data="withdraw_bkash"),
            types.InlineKeyboardButton("📱 Nagad",         callback_data="withdraw_nagad"),
            types.InlineKeyboardButton("💎 USDT (BEP-20)", callback_data="withdraw_usdt_bep"),
        )
    else:
        kb.add(
            types.InlineKeyboardButton("💎 USDT (BEP-20)", callback_data="withdraw_usdt_bep"),
        )
    return kb


def currency_select_inline():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🇧🇩 BDT (Tk)", callback_data="currency_BDT"),
        types.InlineKeyboardButton("💎 USDT",       callback_data="currency_USDT"),
    )
    return kb


def language_select_inline():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🇧🇩 বাংলা",   callback_data="lang_bn"),
        types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
    )
    return kb


def admin_contact_inline(label="👤 Admin"):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(label, url=ADMIN_URL))
    return kb


def admin_file_action_inline(sub_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"adm_approve_{sub_id}"),
        types.InlineKeyboardButton("❌ Reject",  callback_data=f"adm_reject_{sub_id}"),
    )
    return kb

# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════

def send_main_menu(chat_id, text="🏠"):
    set_user_state(chat_id, STATE_IDLE)
    withdraw_session_cache.pop(chat_id, None)
    bot.send_message(chat_id, text, reply_markup=main_menu_keyboard(chat_id))


def today_str():
    return date.today().strftime("%Y-%m-%d")


def notify_admin(text):
    if ADMIN_ID:
        try:
            bot.send_message(ADMIN_ID, text, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Admin notify failed: %s", e)


def divider():
    return "─" * 28


def check_banned(chat_id):
    if is_banned(chat_id):
        bot.send_message(chat_id, "🚫 আপনার একাউন্ট ব্যান করা হয়েছে। Support এ যোগাযোগ করুন।")
        return True
    return False

# ═══════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════

@bot.message_handler(commands=["admin"])
def handle_admin_help(message):
    if message.chat.id != ADMIN_ID:
        return
    bot.send_message(ADMIN_ID,
        "👑 *Admin Control Panel*\n"
        f"{divider()}\n"
        "🟢 `/approve [ID] [Total_Accounts]`\n"
        "🔴 `/reject [ID]`\n"
        "💵 `/pay [Withdraw_ID]`\n"
        "📋 `/pending` — pending submissions\n"
        "💸 `/cashouts` — pending cashouts\n"
        "👥 `/users` — all users list\n"
        "🚫 `/ban [chat_id]`\n"
        "✅ `/unban [chat_id]`\n"
        "📢 `/broadcast`\n"
        "📊 `/stats`"
    )


@bot.message_handler(commands=["stats"])
def handle_admin_stats(message):
    if message.chat.id != ADMIN_ID:
        return
    with get_db() as conn:
        tu   = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        ban  = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_banned=1").fetchone()["c"]
        ps   = conn.execute("SELECT COUNT(*) as c FROM submissions WHERE status='pending'").fetchone()["c"]
        ap   = conn.execute("SELECT COUNT(*) as c FROM submissions WHERE status='approved'").fetchone()["c"]
        rj   = conn.execute("SELECT COUNT(*) as c FROM submissions WHERE status='rejected'").fetchone()["c"]
        pw   = conn.execute("SELECT COUNT(*) as c FROM withdrawals WHERE status='pending'").fetchone()["c"]
        paid = conn.execute("SELECT COUNT(*) as c FROM withdrawals WHERE status='paid'").fetchone()["c"]
        tp   = conn.execute(
            "SELECT COALESCE(SUM(net_amount),0) as s FROM withdrawals WHERE status='paid' AND currency='BDT'"
        ).fetchone()["s"]
    bot.send_message(ADMIN_ID,
        f"📊 *Bot Statistics*\n{divider()}\n"
        f"👥 Total Users:      {tu}\n"
        f"🚫 Banned:           {ban}\n{divider()}\n"
        f"📁 Pending Subs:     {ps}\n"
        f"✅ Approved:         {ap}\n"
        f"❌ Rejected:         {rj}\n{divider()}\n"
        f"💸 Pending Cashouts: {pw}\n"
        f"✅ Paid Cashouts:    {paid}\n"
        f"💵 Total Paid (BDT): {tp:.2f} Tk"
    )


@bot.message_handler(commands=["users"])
def handle_admin_users(message):
    if message.chat.id != ADMIN_ID:
        return
    with get_db() as conn:
        rows = conn.execute(
            """SELECT chat_id, first_name, username, joined_at, balance, total_earned, is_banned
               FROM users ORDER BY joined_at DESC LIMIT 30"""
        ).fetchall()
    if not rows:
        bot.send_message(ADMIN_ID, "No users yet.")
        return
    lines = [f"👥 *All Users (last 30)*\n{divider()}"]
    for row in rows:
        uname  = f"@{row['username']}" if row["username"] else "—"
        status = "🚫" if row["is_banned"] else "✅"
        earned = f"{row['total_earned'] * DOLLAR_RATE:.2f} Tk"
        lines.append(
            f"{status} *{row['first_name']}* {uname}\n"
            f"   🆔 `{row['chat_id']}` | 💰 {earned} | 📅 {row['joined_at']}"
        )
    bot.send_message(ADMIN_ID, "\n".join(lines))


@bot.message_handler(commands=["pending"])
def handle_admin_pending(message):
    if message.chat.id != ADMIN_ID:
        return
    with get_db() as conn:
        rows = conn.execute(
            """SELECT s.id, s.chat_id, s.platform, s.task_type, s.submitted_at,
                      u.first_name, u.username
               FROM submissions s JOIN users u ON u.chat_id=s.chat_id
               WHERE s.status='pending' ORDER BY s.submitted_at ASC LIMIT 20"""
        ).fetchall()
    if not rows:
        bot.send_message(ADMIN_ID, "✅ No pending submissions.")
        return
    lines = [f"📁 *Pending Submissions ({len(rows)})*", divider()]
    for row in rows:
        uname = f"@{row['username']}" if row["username"] else row["first_name"]
        lines.append(
            f"🆔 #{row['id']} | {row['platform']} — {row['task_type']}\n"
            f"   👤 {uname} (`{row['chat_id']}`) | {row['submitted_at'][:16]}\n"
            f"   `/approve {row['id']} [n]`  |  `/reject {row['id']}`"
        )
    bot.send_message(ADMIN_ID, "\n".join(lines))


@bot.message_handler(commands=["cashouts"])
def handle_admin_cashouts(message):
    if message.chat.id != ADMIN_ID:
        return
    with get_db() as conn:
        rows = conn.execute(
            """SELECT w.id, w.chat_id, w.method, w.address, w.net_amount, w.currency, w.requested_at,
                      u.first_name, u.username
               FROM withdrawals w JOIN users u ON u.chat_id=w.chat_id
               WHERE w.status='pending' ORDER BY w.requested_at ASC LIMIT 20"""
        ).fetchall()
    if not rows:
        bot.send_message(ADMIN_ID, "✅ No pending cashouts.")
        return
    lines = [f"💸 *Pending Cashouts ({len(rows)})*", divider()]
    for row in rows:
        uname = f"@{row['username']}" if row["username"] else row["first_name"]
        amt   = f"{row['net_amount']:.4f} {row['currency']}"
        lines.append(
            f"🆔 #{row['id']} | {row['method']} — {amt}\n"
            f"   📲 {row['address']}\n"
            f"   👤 {uname} | {row['requested_at'][:16]}\n"
            f"   `/pay {row['id']}`"
        )
    bot.send_message(ADMIN_ID, "\n".join(lines))


@bot.message_handler(commands=["approve"])
def handle_approve(message):
    if message.chat.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 3:
        bot.send_message(ADMIN_ID, "⚠️ Format: `/approve [Submission_ID] [Total_Accounts]`")
        return
    try:
        sub_id      = int(args[1])
        total_items = int(args[2])
        if total_items <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(ADMIN_ID, "⚠️ Both ID and count must be positive integers.")
        return

    with get_db() as conn:
        sub = conn.execute(
            "SELECT * FROM submissions WHERE id=? AND status='pending'", (sub_id,)
        ).fetchone()
        if not sub:
            bot.send_message(ADMIN_ID, f"⚠️ Pending submission #{sub_id} not found.")
            return

        task_key = next(
            (k for k, v in TASK_META.items() if v["label"] == sub["task_type"]), None
        )
        if not task_key:
            bot.send_message(ADMIN_ID, "⚠️ Task metadata mismatch.")
            return

        price_bdt        = TASK_META[task_key]["price_val"]
        total_payout_bdt = round(price_bdt * total_items, 4)
        total_payout_usd = round(total_payout_bdt / DOLLAR_RATE, 6)

        conn.execute(
            "UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE chat_id=?",
            (total_payout_usd, total_payout_usd, sub["chat_id"]),
        )

        ref_info = conn.execute(
            "SELECT referred_by FROM users WHERE chat_id=?", (sub["chat_id"],)
        ).fetchone()
        ref_text = ""
        if ref_info and ref_info["referred_by"]:
            referrer_id = ref_info["referred_by"]
            comm_usd    = round(total_payout_usd * REFERRAL_COMM_PCT, 6)
            conn.execute(
                "UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE chat_id=?",
                (comm_usd, comm_usd, referrer_id),
            )
            comm_bdt = round(comm_usd * DOLLAR_RATE, 2)
            ref_text = f"\n🎁 Referrer ({referrer_id}) commission: {comm_bdt:.2f} Tk"
            try:
                bot.send_message(referrer_id,
                    f"🎁 *Referral Commission!*\n"
                    f"আপনার রেফার করা ইউজার টাস্ক সম্পন্ন করায় ২০% কমিশন পেয়েছেন।\n\n"
                    f"💰 কমিশন: *{comm_bdt:.2f} Tk*"
                )
            except Exception:
                pass

        conn.execute("UPDATE submissions SET status='approved' WHERE id=?", (sub_id,))
        conn.commit()

    bot.send_message(ADMIN_ID,
        f"✅ Submission #{sub_id} Approved!\n"
        f"💵 Paid: {total_payout_bdt:.2f} Tk ({total_items} × {price_bdt:.2f} Tk){ref_text}"
    )
    try:
        user_currency = get_currency(sub["chat_id"])
        payout_str = fmt_price(total_payout_bdt, user_currency)
        bot.send_message(sub["chat_id"],
            f"🎉 *Task Approved!*\n{divider()}\n"
            f"🆔 Submission ID:  #{sub_id}\n"
            f"📋 Task:           {sub['task_type']}\n"
            f"✅ Accepted:       {total_items} accounts\n"
            f"💰 Credited:       *{payout_str}*\n{divider()}\n"
            + t(sub["chat_id"],
                "আপনার ব্যালেন্সে টাকা যোগ হয়েছে! ধন্যবাদ। 🙏",
                "Amount has been added to your balance! Thank you. 🙏")
        )
    except Exception:
        pass


@bot.message_handler(commands=["reject"])
def handle_reject(message):
    if message.chat.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(ADMIN_ID, "⚠️ Format: `/reject [Submission_ID]`")
        return
    try:
        sub_id = int(args[1])
    except ValueError:
        return
    with get_db() as conn:
        sub = conn.execute(
            "SELECT chat_id FROM submissions WHERE id=? AND status='pending'", (sub_id,)
        ).fetchone()
        if not sub:
            bot.send_message(ADMIN_ID, f"⚠️ Pending submission #{sub_id} not found.")
            return
        conn.execute("UPDATE submissions SET status='rejected' WHERE id=?", (sub_id,))
        conn.commit()
    bot.send_message(ADMIN_ID, f"🔴 Submission #{sub_id} rejected.")
    try:
        bot.send_message(sub["chat_id"],
            f"❌ *Task Rejected!*\n{divider()}\n"
            f"🆔 Submission ID: #{sub_id}\n"
            + t(sub["chat_id"],
                "⚠️ ফাইলে সমস্যা থাকায় রিজেক্ট হয়েছে। সাহায্যের জন্য 📞 Support এ যোগাযোগ করুন।",
                "⚠️ Your submission was rejected due to file issues. Contact 📞 Support for help.")
        )
    except Exception:
        pass


@bot.message_handler(commands=["pay"])
def handle_pay_withdraw(message):
    if message.chat.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(ADMIN_ID, "⚠️ Format: `/pay [Withdraw_ID]`")
        return
    try:
        wid = int(args[1])
    except ValueError:
        return
    with get_db() as conn:
        withd = conn.execute(
            "SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wid,)
        ).fetchone()
        if not withd:
            bot.send_message(ADMIN_ID, f"⚠️ Pending cashout #{wid} not found.")
            return
        conn.execute("UPDATE withdrawals SET status='paid' WHERE id=?", (wid,))
        conn.commit()
    bot.send_message(ADMIN_ID, f"✅ Cashout #{wid} marked PAID.")
    try:
        curr = withd["currency"]
        amt  = f"{withd['net_amount']:.4f} USDT" if curr == "USDT" else f"{withd['net_amount']:.2f} Tk"
        bot.send_message(withd["chat_id"],
            f"🎉 *Withdrawal Successful!*\n{divider()}\n"
            f"🆔 Request ID:  #{wid}\n"
            f"💳 Method:      {withd['method']}\n"
            f"💵 Amount:      *{amt}*\n{divider()}\n"
            + t(withd["chat_id"],
                "পেমেন্ট পাঠিয়ে দেওয়া হয়েছে। ধন্যবাদ! 🙏",
                "Payment has been sent. Thank you! 🙏")
        )
    except Exception:
        pass


# ── Inline approve/reject from file notification ──────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_approve_") or c.data.startswith("adm_reject_"))
def handle_admin_inline_action(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ Not authorized.")
        return

    parts  = call.data.split("_")
    action = parts[1]   # "approve" or "reject"
    sub_id = int(parts[2])

    bot.answer_callback_query(call.id)

    with get_db() as conn:
        sub = conn.execute(
            "SELECT * FROM submissions WHERE id=? AND status='pending'", (sub_id,)
        ).fetchone()

    if not sub:
        bot.edit_message_reply_markup(ADMIN_ID, call.message.message_id, reply_markup=None)
        bot.send_message(ADMIN_ID, f"⚠️ Submission #{sub_id} already processed or not found.")
        return

    if action == "reject":
        with get_db() as conn:
            conn.execute("UPDATE submissions SET status='rejected' WHERE id=?", (sub_id,))
            conn.commit()
        bot.edit_message_reply_markup(ADMIN_ID, call.message.message_id, reply_markup=None)
        bot.send_message(ADMIN_ID, f"🔴 Submission #{sub_id} rejected.")
        try:
            bot.send_message(sub["chat_id"],
                f"❌ *Task Rejected!*\n{divider()}\n"
                f"🆔 Submission ID: #{sub_id}\n"
                + t(sub["chat_id"],
                    "⚠️ ফাইলে সমস্যা থাকায় রিজেক্ট হয়েছে। Support এ যোগাযোগ করুন।",
                    "⚠️ Rejected due to file issues. Contact Support.")
            )
        except Exception:
            pass
    else:
        # Approve — ask for count
        bot.edit_message_reply_markup(ADMIN_ID, call.message.message_id, reply_markup=None)
        bot.send_message(ADMIN_ID,
            f"✅ Approving #{sub_id} — কতটি account accept করবেন?\n"
            f"Reply করুন: `/approve {sub_id} [count]`"
        )


@bot.message_handler(commands=["ban"])
def handle_ban(message):
    if message.chat.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(ADMIN_ID, "⚠️ Format: `/ban [chat_id]`")
        return
    try:
        target = int(args[1])
    except ValueError:
        return
    with get_db() as conn:
        conn.execute("UPDATE users SET is_banned=1 WHERE chat_id=?", (target,))
        conn.commit()
    bot.send_message(ADMIN_ID, f"🚫 User {target} banned.")


@bot.message_handler(commands=["unban"])
def handle_unban(message):
    if message.chat.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(ADMIN_ID, "⚠️ Format: `/unban [chat_id]`")
        return
    try:
        target = int(args[1])
    except ValueError:
        return
    with get_db() as conn:
        conn.execute("UPDATE users SET is_banned=0 WHERE chat_id=?", (target,))
        conn.commit()
    bot.send_message(ADMIN_ID, f"✅ User {target} unbanned.")


@bot.message_handler(commands=["broadcast"])
def handle_broadcast_start(message):
    if message.chat.id != ADMIN_ID:
        return
    set_user_state(ADMIN_ID, STATE_BROADCAST)
    bot.send_message(ADMIN_ID,
        "📢 *Broadcast Mode*\n"
        "এখন যা পাঠাবেন তা সব ইউজারকে forward হবে।\n"
        "বাতিল করতে: ❌ Cancel",
        reply_markup=cancel_keyboard()
    )

# ═══════════════════════════════════════════════════════════
#  /start
# ═══════════════════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def handle_start(message):
    chat_id     = message.chat.id
    args        = message.text.split()
    referred_by = None

    if len(args) > 1:
        with get_db() as conn:
            ref_row = conn.execute(
                "SELECT chat_id FROM users WHERE referral_code=?", (args[1],)
            ).fetchone()
            if ref_row and ref_row["chat_id"] != chat_id:
                referred_by = ref_row["chat_id"]

    ensure_user(message, referred_by=referred_by)
    set_user_state(chat_id, STATE_IDLE)

    if check_banned(chat_id):
        return

    name = message.from_user.first_name or "Friend"
    bot.send_message(chat_id,
        t(chat_id,
          f"👋 স্বাগতম, *{name}*!\n\nসহজ টাস্ক করে আয় করুন। নিচের মেনু ব্যবহার করুন 👇",
          f"👋 Hello, *{name}*!\n\nComplete tasks and earn money. Use the menu below 👇"),
        reply_markup=main_menu_keyboard(chat_id)
    )

# ═══════════════════════════════════════════════════════════
#  MENU HANDLERS
# ═══════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text in ("💰 Balance", "💰 ব্যালেন্স"))
def handle_balance(message):
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid      = message.chat.id
    user     = get_user(cid)
    bal_usd  = user["balance"]      if user else 0.0
    earn_usd = user["total_earned"] if user else 0.0
    currency = get_currency(cid)

    bal_str  = fmt_amount(bal_usd,  currency)
    earn_str = fmt_amount(earn_usd, currency)
    min_str  = f"{MIN_WITHDRAW_BDT:.2f} Tk"   if currency == "BDT" else f"${MIN_WITHDRAW_USDT:.2f} USDT"
    fee_str  = f"{WITHDRAW_FEE_BDT:.2f} Tk"   if currency == "BDT" else f"${WITHDRAW_FEE_USDT:.2f} USDT"

    if is_bn(cid):
        text = (f"💰 *ব্যালেন্স ড্যাশবোর্ড*\n{divider()}\n"
                f"💵 বর্তমান ব্যালেন্স:   *{bal_str}*\n"
                f"📈 মোট আয়:            {earn_str}\n{divider()}\n"
                f"💸 সর্বনিম্ন উত্তোলন:  {min_str}\n"
                f"📋 উত্তোলন ফি:         {fee_str}")
    else:
        text = (f"💰 *Balance Dashboard*\n{divider()}\n"
                f"💵 Current Balance:   *{bal_str}*\n"
                f"📈 Total Earned:      {earn_str}\n{divider()}\n"
                f"💸 Min Withdraw:      {min_str}\n"
                f"📋 Withdraw Fee:      {fee_str}")
    bot.send_message(cid, text)


@bot.message_handler(func=lambda m: m.text in ("📋 Tasks", "📋 কাজ"))
def handle_task_submit(message):
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid = message.chat.id
    set_user_state(cid, STATE_IDLE)
    txt = t(cid, "✨ প্ল্যাটফর্ম সিলেক্ট করুন 👇", "✨ Select a platform 👇")
    bot.send_message(cid, txt, reply_markup=platform_select_inline())


@bot.message_handler(func=lambda m: m.text in ("📥 Withdraw", "📥 উত্তোলন"))
def handle_withdraw_menu(message):
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid      = message.chat.id
    currency = get_currency(cid)
    bal_usd  = get_balance_usd(cid)

    if currency == "BDT":
        current_val = bal_usd * DOLLAR_RATE
        min_val     = MIN_WITHDRAW_BDT
        fee_val     = WITHDRAW_FEE_BDT
        bal_str     = f"{current_val:.2f} Tk"
        min_str     = f"{min_val:.2f} Tk"
        fee_str     = f"{fee_val:.2f} Tk"
    else:
        current_val = bal_usd
        min_val     = MIN_WITHDRAW_USDT
        fee_val     = WITHDRAW_FEE_USDT
        bal_str     = f"${current_val:.4f} USDT"
        min_str     = f"${min_val:.2f} USDT"
        fee_str     = f"${fee_val:.2f} USDT"

    if current_val < min_val:
        if is_bn(cid):
            bot.send_message(cid,
                f"⚠️ *অপর্যাপ্ত ব্যালেন্স*\n\n"
                f"💰 আপনার ব্যালেন্স: *{bal_str}*\n"
                f"📋 সর্বনিম্ন: {min_str}\n\nআরো টাস্ক সম্পন্ন করুন।"
            )
        else:
            bot.send_message(cid,
                f"⚠️ *Insufficient Balance*\n\n"
                f"💰 Your Balance: *{bal_str}*\n"
                f"📋 Minimum: {min_str}\n\nPlease complete more tasks."
            )
        return

    last_time = get_last_withdrawal_time(cid)
    if last_time:
        last_dt = datetime.strptime(last_time[:19], "%Y-%m-%d %H:%M:%S")
        elapsed = datetime.now() - last_dt
        if elapsed < timedelta(hours=WITHDRAW_COOLDOWN):
            remaining = timedelta(hours=WITHDRAW_COOLDOWN) - elapsed
            h, rem = divmod(int(remaining.total_seconds()), 3600)
            mn     = rem // 60
            bot.send_message(cid,
                f"⏳ *Cooldown Active*\n\n"
                + t(cid,
                    f"পরবর্তী উত্তোলনের জন্য আরো *{h}h {mn}m* অপেক্ষা করুন।",
                    f"Please wait *{h}h {mn}m* more before next withdrawal.")
            )
            return

    if is_bn(cid):
        txt = (f"📥 *উত্তোলন পদ্ধতি বেছে নিন:*\n{divider()}\n"
               f"💰 ব্যালেন্স:   *{bal_str}*\n"
               f"💸 ফি:          {fee_str}\n"
               f"📋 সর্বনিম্ন:   {min_str}")
    else:
        txt = (f"📥 *Choose Withdraw Method:*\n{divider()}\n"
               f"💰 Balance:   *{bal_str}*\n"
               f"💸 Fee:       {fee_str}\n"
               f"📋 Minimum:   {min_str}")
    set_user_state(cid, STATE_IDLE)
    bot.send_message(cid, txt, reply_markup=withdraw_method_inline(currency))


@bot.message_handler(func=lambda m: m.text in ("👤 Profile", "👤 প্রোফাইল"))
def handle_profile(message):
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid  = message.chat.id
    user = get_user(cid)
    if not user:
        bot.send_message(cid, "⚠️ Profile not found.")
        return

    currency  = get_currency(cid)
    ref_count = get_referral_count(cid)
    with get_db() as conn:
        sub_c = conn.execute("SELECT COUNT(*) as c FROM submissions WHERE chat_id=?", (cid,)).fetchone()["c"]
        wd_c  = conn.execute("SELECT COUNT(*) as c FROM withdrawals WHERE chat_id=? AND status='paid'", (cid,)).fetchone()["c"]

    bal_str  = fmt_amount(user["balance"],      currency)
    earn_str = fmt_amount(user["total_earned"], currency)
    name     = user["first_name"] or "N/A"
    username = f"@{user['username']}" if user["username"] else "N/A"
    lang_str = "বাংলা 🇧🇩" if user["language"] == "Bangla" else "English 🇬🇧"
    curr_str = "BDT 🇧🇩" if currency == "BDT" else "USDT 💎"

    bot.send_message(cid,
        f"👤 *My Profile*\n{divider()}\n"
        f"📛 Name:           {name}\n"
        f"🔗 Username:       {username}\n"
        f"🆔 Chat ID:        `{cid}`\n"
        f"📅 Joined:         {user['joined_at']}\n{divider()}\n"
        f"💵 Balance:        *{bal_str}*\n"
        f"📈 Total Earned:   {earn_str}\n{divider()}\n"
        f"📁 Submissions:    {sub_c}\n"
        f"💸 Withdrawals:    {wd_c}\n"
        f"👥 Referrals:      {ref_count}\n"
        f"🎫 Referral Code:  `{user['referral_code']}`\n{divider()}\n"
        f"🌐 Language:       {lang_str}\n"
        f"💱 Currency:       {curr_str}"
    )


@bot.message_handler(func=lambda m: m.text in ("🏆 Top", "🏆 শীর্ষ"))
def handle_top(message):
    cid      = message.chat.id
    earners  = get_top_earners(10)
    total_u  = get_total_users()
    currency = get_currency(cid)
    medals   = {1: "🥇", 2: "🥈", 3: "🥉"}

    if is_bn(cid):
        lines = ["🏆 *দৈনিক সেরা ১০ ইউজার*", "",
                 "ℹ️ ফলাফল প্রতিদিন রাত ০১:০০ টায় ঘোষণা করা হয়।",
                 "🔄 প্রতি ১০ মিনিটে আপডেট হয়।", divider()]
    else:
        lines = ["🏆 *Top 10 Users Today*", "",
                 "ℹ️ Results announced daily at 01:00.",
                 "🔄 Updates every 10 minutes.", divider()]

    if earners:
        for i, row in enumerate(earners, 1):
            name  = row["first_name"] or "Unknown"
            medal = medals.get(i, f"{i}.")
            prize = fmt_amount(row["total_earned"], currency)
            lines.append(f"{medal}  {name}  —  {prize}")
    else:
        lines.append(t(cid,
            "⚠️ এখনো কোনো ডেটা নেই।",
            "⚠️ No data yet."))

    lines += [divider(), f"👥 {t(cid, 'মোট ইউজার', 'Total Users')}: {total_u}"]
    bot.send_message(cid, "\n".join(lines))


@bot.message_handler(func=lambda m: m.text in ("📞 Support", "📞 সাপোর্ট"))
def handle_support(message):
    ensure_user(message)
    cid = message.chat.id
    if is_bn(cid):
        txt = (f"📞 *সাপোর্ট*\n{divider()}\n"
               "কোনো সমস্যা বা পেমেন্ট নিয়ে কথা বলতে নিচের বাটনে ক্লিক করুন।\n\n"
               "💬 এডমিন একটিভ থাকলে দ্রুত রিপ্লাই আসবে।")
    else:
        txt = (f"📞 *Support*\n{divider()}\n"
               "For any issue or payment concern, click the button below.\n\n"
               "💬 Admin will reply as soon as possible.")
    bot.send_message(cid, txt, reply_markup=admin_contact_inline("👤 Contact Admin"))


@bot.message_handler(func=lambda m: m.text in ("👥 My Referrals", "👥 আমার রেফারেল"))
def handle_my_referrals(message):
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid       = message.chat.id
    user      = get_user(cid)
    ref_count = get_referral_count(cid)
    code      = user["referral_code"] if user else f"REF{cid}"
    link      = f"https://t.me/{BOT_USERNAME}?start={code}"

    if is_bn(cid):
        bot.send_message(cid,
            f"👥 *আমার রেফারেল*\n{divider()}\n"
            f"👥 মোট রেফারেল: *{ref_count}*\n{divider()}\n"
            f"🔗 *আপনার রেফারেল লিংক:*\n`{link}`\n\n"
            "বন্ধুদের আমন্ত্রণ জানান — তারা কাজ করলে *২০%* আজীবন কমিশন পাবেন! 🔥"
        )
    else:
        bot.send_message(cid,
            f"👥 *My Referrals*\n{divider()}\n"
            f"👥 Total referrals: *{ref_count}*\n{divider()}\n"
            f"🔗 *Your Referral Link:*\n`{link}`\n\n"
            "Invite friends — earn *20% lifetime commission* every time they work! 🔥"
        )


@bot.message_handler(func=lambda m: m.text in ("📂 My Submissions", "📂 আমার সাবমিশন"))
def handle_my_submissions(message):
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid  = message.chat.id
    rows = get_user_submissions(cid, limit=10)

    if not rows:
        bot.send_message(cid, t(cid,
            "📂 আপনার কোনো সাবমিশন নেই।",
            "📂 You have no submissions yet."))
        return

    icons = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    title = f"📂 *{t(cid, 'সর্বশেষ', 'My Last')} {len(rows)} Submissions*"
    lines = [title, divider()]
    for row in rows:
        lines.append(
            f"#{row['id']} {icons.get(row['status'],'❓')} "
            f"{row['platform']} — {row['task_type']}\n"
            f"   {row['submitted_at'][:16]}"
        )
    bot.send_message(cid, "\n".join(lines))


@bot.message_handler(func=lambda m: m.text in ("💱 Currency", "💱 কারেন্সি"))
def handle_currency_menu(message):
    ensure_user(message)
    cid = message.chat.id
    txt = t(cid, "💱 কারেন্সি বেছে নিন:", "💱 Choose your display currency:")
    bot.send_message(cid, txt, reply_markup=currency_select_inline())


@bot.callback_query_handler(func=lambda c: c.data.startswith("currency_"))
def handle_currency_select(call):
    currency = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    cid = call.message.chat.id
    set_user_currency(cid, currency)
    name = "BDT (Tk) 🇧🇩" if currency == "BDT" else "USDT 💎"
    txt = t(cid, f"✅ কারেন্সি পরিবর্তন: *{name}*", f"✅ Currency set to *{name}*")
    bot.send_message(cid, txt, reply_markup=main_menu_keyboard(cid))


@bot.message_handler(func=lambda m: m.text in ("🌐 Language", "🌐 ভাষা"))
def handle_language(message):
    bot.send_message(message.chat.id,
        "🌐 Choose a language / একটি ভাষা নির্বাচন করুন:",
        reply_markup=language_select_inline()
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def handle_language_select(call):
    lang = "Bangla" if call.data == "lang_bn" else "English"
    bot.answer_callback_query(call.id)
    cid = call.message.chat.id
    set_user_lang(cid, lang)
    # Send confirmation AND updated keyboard immediately
    if lang == "Bangla":
        txt = "✅ ভাষা বাংলায় পরিবর্তন করা হয়েছে।"
    else:
        txt = "✅ Language changed to English."
    bot.send_message(cid, txt, reply_markup=main_menu_keyboard(cid))


@bot.message_handler(func=lambda m: m.text == "❌ Cancel")
def handle_cancel(message):
    cid = message.chat.id
    if cid == ADMIN_ID:
        user = get_user(ADMIN_ID)
        if user and user["current_state"] == STATE_BROADCAST:
            send_main_menu(ADMIN_ID, "✅ Broadcast cancelled.")
            return
    txt = t(cid, "✅ বাতিল করা হয়েছে।", "✅ Operation cancelled.")
    send_main_menu(cid, txt)

# ═══════════════════════════════════════════════════════════
#  INLINE CALLBACKS
# ═══════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("platform_"))
def handle_platform_select(call):
    if check_banned(call.message.chat.id):
        bot.answer_callback_query(call.id)
        return
    platform = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    cid      = call.message.chat.id
    currency = get_currency(cid)

    menus = {
        "instagram": (t(cid, "🌸 Instagram — টাস্ক সিলেক্ট করুন:", "🌸 Instagram — Select a task:"), instagram_type_inline(currency)),
        "facebook":  (t(cid, "🔷 Facebook — টাস্ক সিলেক্ট করুন:", "🔷 Facebook — Select a task:"),  facebook_type_inline(currency)),
        "gmail":     (t(cid, "📧 Gmail — টাস্ক সিলেক্ট করুন:", "📧 Gmail — Select a task:"),         gmail_type_inline(currency)),
    }
    if platform in menus:
        text, kb = menus[platform]
        bot.send_message(cid, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adminrate_"))
def handle_admin_rate(call):
    bot.answer_callback_query(call.id)
    platform = call.data.split("_", 1)[1].capitalize()
    bot.send_message(call.message.chat.id,
        t(call.message.chat.id,
          f"🔑 Custom {platform} rate সেট করতে এডমিনের সাথে যোগাযোগ করুন:",
          f"🔑 Contact admin to set a custom {platform} rate:"),
        reply_markup=admin_contact_inline("👤 Admin")
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("taskinfo_"))
def handle_task_info(call):
    task_key = call.data[9:]
    bot.answer_callback_query(call.id)
    meta     = TASK_META.get(task_key)
    cid      = call.message.chat.id
    currency = get_currency(cid)

    if not meta:
        bot.send_message(cid, "⚠️ Unknown task.")
        return

    price_display = fmt_price(meta["price_val"], currency)
    bot.send_message(cid,
        f"📋 *Task:* {meta['label']}\n{divider()}\n"
        f"💵 Reward:       {price_display}/piece\n"
        f"⏳ Review Time:  {meta['review_min']} minutes\n{divider()}\n"
        f"📄 Instructions:\n{meta['description']}\n{divider()}\n"
        f"📂 Required Columns:\n`{meta['columns']}`",
        reply_markup=task_action_inline(task_key)
    )


@bot.callback_query_handler(func=lambda c: c.data == "task_cancel")
def handle_task_cancel_inline(call):
    bot.answer_callback_query(call.id)
    cid = call.message.chat.id
    send_main_menu(cid, t(cid, "✅ বাতিল করা হয়েছে।", "✅ Operation cancelled."))


@bot.callback_query_handler(func=lambda c: c.data.startswith("task_") and c.data != "task_cancel")
def handle_task_select(call):
    if check_banned(call.message.chat.id):
        bot.answer_callback_query(call.id)
        return
    task_key = call.data[5:]
    bot.answer_callback_query(call.id)
    meta = TASK_META.get(task_key)
    if not meta:
        bot.send_message(call.message.chat.id, "⚠️ Unknown task.")
        return

    cid      = call.message.chat.id
    currency = get_currency(cid)

    if has_pending_submission(cid, meta["label"]):
        bot.send_message(cid,
            t(cid,
              f"⚠️ *{meta['label']}* এর একটি সাবমিশন পেন্ডিং আছে। এডমিন রিভিউর পর আবার সাবমিট করুন।",
              f"⚠️ A submission for *{meta['label']}* is still pending. Wait for admin review.")
        )
        return

    price_display = fmt_price(meta["price_val"], currency)
    set_user_state(cid, STATE_WAITING_FILE, task_key=task_key)
    bot.send_message(cid,
        f"📤 *Submit Task*\n{divider()}\n"
        f"📅 Date:    {today_str()}\n"
        f"📋 Task:    {meta['label']}\n"
        f"💵 Reward:  {price_display}/piece\n{divider()}\n"
        f"📂 Required Columns:\n`{meta['columns']}`\n{divider()}\n"
        + t(cid,
            "আপনার *.xlsx* ফাইলটি আপলোড করুন।\nবাতিল করতে ❌ Cancel চাপুন।",
            "Upload your *.xlsx* file.\nPress ❌ Cancel to abort."),
        reply_markup=cancel_keyboard()
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("withdraw_"))
def handle_withdraw_method(call):
    method_key = call.data[9:]
    bot.answer_callback_query(call.id)
    cid      = call.message.chat.id
    currency = get_currency(cid)
    bal_usd  = get_balance_usd(cid)

    method_label = {
        "bkash":    "bKash",
        "nagad":    "Nagad",
        "usdt_bep": "USDT (BEP-20)",
    }.get(method_key)
    if not method_label:
        return

    if currency == "BDT":
        current_val = bal_usd * DOLLAR_RATE
        fee_val     = WITHDRAW_FEE_BDT
        unit        = "Tk"
        bal_str     = f"{current_val:.2f} Tk"
        fee_str     = f"{fee_val:.2f} Tk"
        min_str     = f"{MIN_WITHDRAW_BDT:.2f} Tk"
    else:
        current_val = bal_usd
        fee_val     = WITHDRAW_FEE_USDT
        unit        = "USDT"
        bal_str     = f"${current_val:.4f} USDT"
        fee_str     = f"${fee_val:.2f} USDT"
        min_str     = f"${MIN_WITHDRAW_USDT:.2f} USDT"

    set_user_state(cid, STATE_WAITING_AMOUNT)
    withdraw_session_cache[cid] = {
        "withdraw_method":   method_key,
        "withdraw_label":    method_label,
        "withdraw_currency": currency,
    }
    bot.send_message(cid,
        f"✅ *{method_label}* selected.\n{divider()}\n"
        f"💸 Fee:        {fee_str}\n"
        f"🔢 Minimum:    {min_str}\n"
        f"💰 Balance:    *{bal_str}*\n{divider()}\n"
        + t(cid,
            f"কত {unit} উত্তোলন করতে চান? (শুধু সংখ্যা লিখুন)",
            f"How much {unit} to withdraw? (numbers only)"),
        reply_markup=cancel_keyboard()
    )

# ═══════════════════════════════════════════════════════════
#  STATE WORKFLOWS
# ═══════════════════════════════════════════════════════════

@bot.message_handler(
    func=lambda m: (
        m.chat.id == ADMIN_ID
        and get_user(m.chat.id) is not None
        and get_user(m.chat.id)["current_state"] == STATE_BROADCAST
        and m.text != "❌ Cancel"
    )
)
def handle_broadcast_send(message):
    all_ids = get_all_chat_ids()
    success, fail = 0, 0
    for uid in all_ids:
        if uid == ADMIN_ID:
            continue
        try:
            bot.forward_message(uid, message.chat.id, message.message_id)
            success += 1
        except Exception:
            fail += 1
    send_main_menu(ADMIN_ID)
    bot.send_message(ADMIN_ID, f"📢 Broadcast complete!\n✅ Sent: {success}\n❌ Failed: {fail}")


@bot.message_handler(
    func=lambda m: get_user(m.chat.id) is not None
    and get_user(m.chat.id)["current_state"] == STATE_WAITING_AMOUNT
)
def handle_withdraw_amount(message):
    cid = message.chat.id
    try:
        amount = float(message.text.strip().replace(",", ""))
    except ValueError:
        bot.send_message(cid, t(cid, "⚠️ সঠিক সংখ্যা দিন।", "⚠️ Enter a valid number."), reply_markup=cancel_keyboard())
        return

    session  = withdraw_session_cache.setdefault(cid, {})
    currency = session.get("withdraw_currency", get_currency(cid))
    bal_usd  = get_balance_usd(cid)

    if currency == "BDT":
        current_val = bal_usd * DOLLAR_RATE
        min_val     = MIN_WITHDRAW_BDT
        fee_val     = WITHDRAW_FEE_BDT
        unit        = "Tk"
    else:
        current_val = bal_usd
        min_val     = MIN_WITHDRAW_USDT
        fee_val     = WITHDRAW_FEE_USDT
        unit        = "USDT"

    if amount < min_val:
        bot.send_message(cid, f"⚠️ Minimum: {min_val:.4f} {unit}", reply_markup=cancel_keyboard())
        return
    if amount > current_val:
        bot.send_message(cid,
            t(cid,
              f"⚠️ আপনার ব্যালেন্স *{current_val:.4f} {unit}* — এত উত্তোলন সম্ভব নয়।",
              f"⚠️ Your balance is *{current_val:.4f} {unit}* — insufficient."),
            reply_markup=cancel_keyboard()
        )
        return

    session["withdraw_amount"] = amount
    net   = round(amount - fee_val, 6)
    label = session.get("withdraw_label", "Wallet")

    set_user_state(cid, STATE_WAITING_ADDRESS)
    bot.send_message(cid,
        f"💵 Amount:      *{amount:.4f} {unit}*\n"
        f"📋 Fee:         {fee_val:.4f} {unit}\n"
        f"✅ You'll get:  *{net:.4f} {unit}*\n{divider()}\n"
        + t(cid,
            f"📲 আপনার {label} নম্বর / ঠিকানা লিখুন:",
            f"📲 Enter your {label} number / address:"),
        reply_markup=cancel_keyboard()
    )


@bot.message_handler(
    func=lambda m: get_user(m.chat.id) is not None
    and get_user(m.chat.id)["current_state"] == STATE_WAITING_ADDRESS
)
def handle_withdraw_address(message):
    cid     = message.chat.id
    address = message.text.strip()
    session = withdraw_session_cache.pop(cid, None)

    if not session or "withdraw_amount" not in session:
        bot.send_message(cid, t(cid, "⚠️ সেশন মেয়াদ শেষ। আবার চেষ্টা করুন।", "⚠️ Session expired. Try again."))
        send_main_menu(cid)
        return

    if len(address) < 5 or len(address) > 100:
        withdraw_session_cache[cid] = session
        bot.send_message(cid,
            t(cid, "⚠️ সঠিক নম্বর বা ঠিকানা লিখুন (৫–১০০ অক্ষর)।", "⚠️ Enter a valid address (5–100 chars)."),
            reply_markup=cancel_keyboard())
        return

    label    = session["withdraw_label"]
    amount   = session["withdraw_amount"]
    currency = session.get("withdraw_currency", "BDT")
    fee_val  = WITHDRAW_FEE_BDT if currency == "BDT" else WITHDRAW_FEE_USDT
    net      = round(amount - fee_val, 6)
    unit     = "Tk" if currency == "BDT" else "USDT"

    wid = save_withdrawal(cid, label, address, amount, currency)

    bot.send_message(cid,
        f"✅ *Withdrawal Request Submitted!*\n{divider()}\n"
        f"🆔 Request ID:  #{wid}\n"
        f"💳 Method:      {label}\n"
        f"📲 Address:     `{address}`\n"
        f"💰 Amount:      {amount:.4f} {unit}\n"
        f"📋 Fee:         {fee_val:.4f} {unit}\n"
        f"💵 You'll get:  *{net:.4f} {unit}*\n{divider()}\n"
        + t(cid,
            "⏳ এডমিন ২৪ ঘণ্টার মধ্যে পেমেন্ট প্রসেস করবেন।",
            "⏳ Admin will process payment within 24 hours.")
    )
    notify_admin(
        f"💸 *New Withdrawal #{wid}*\n"
        f"👤 Chat ID:  `{cid}`\n"
        f"💳 Method:   {label} ({currency})\n"
        f"📲 Address:  `{address}`\n"
        f"💰 Amount:   {amount:.4f} {unit}  |  Net: {net:.4f} {unit}\n\n"
        f"Approve: `/pay {wid}`"
    )
    send_main_menu(cid)

# ═══════════════════════════════════════════════════════════
#  FILE HANDLER — forwards actual file to admin
# ═══════════════════════════════════════════════════════════

@bot.message_handler(content_types=["document"])
def handle_document(message):
    cid      = message.chat.id
    userdata = get_user(cid)

    if not userdata or userdata["current_state"] != STATE_WAITING_FILE:
        bot.send_message(cid, t(cid, "⚠️ আগে একটি টাস্ক সিলেক্ট করুন।", "⚠️ Please select a task first."))
        return

    task_key = userdata["current_task_key"]
    meta     = TASK_META.get(task_key)
    if not meta:
        bot.send_message(cid, "⚠️ Task data not found.")
        send_main_menu(cid)
        return

    file_name = message.document.file_name or ""
    if not file_name.lower().endswith(".xlsx"):
        bot.send_message(cid, t(cid, "❌ শুধুমাত্র *.xlsx* ফাইল গ্রহণযোগ্য।", "❌ Only *.xlsx* files accepted."))
        return

    file_size = message.document.file_size or 0
    if file_size > 50 * 1024 * 1024:
        bot.send_message(cid, t(cid, "❌ ফাইল সাইজ ৫০ MB এর বেশি হওয়া যাবে না।", "❌ File size must be under 50 MB."))
        return

    currency      = get_currency(cid)
    price_display = fmt_price(meta["price_val"], currency)
    submission_id = save_submission(cid, meta["platform"], meta["label"], message.document.file_id)
    logger.info("Submission #%d | chat=%s | %s | %s", submission_id, cid, meta["platform"], file_name)

    user = get_user(cid)
    uname = f"@{user['username']}" if user and user["username"] else user["first_name"] if user else str(cid)

    # ── Forward actual file to admin with caption ──
    if ADMIN_ID:
        try:
            caption = (
                f"📁 *New Submission #{submission_id}*\n"
                f"👤 {uname} (`{cid}`)\n"
                f"📌 {meta['platform']} — {meta['label']}\n"
                f"💵 {meta['price']}\n"
                f"📄 {file_name} ({file_size/1024:.1f} KB)\n"
                f"📅 {today_str()}"
            )
            bot.send_document(
                ADMIN_ID,
                message.document.file_id,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=admin_file_action_inline(submission_id)
            )
        except Exception as e:
            logger.warning("Failed to forward file to admin: %s", e)
            notify_admin(
                f"📁 *New Submission #{submission_id}*\n"
                f"👤 {uname} (`{cid}`)\n"
                f"📌 {meta['platform']} — {meta['label']} ({meta['price']})\n"
                f"📄 {file_name} ({file_size/1024:.1f} KB)\n\n"
                f"✅ `/approve {submission_id} [Total_Accounts]`\n"
                f"❌ `/reject {submission_id}`"
            )

    bot.send_message(cid,
        f"✅ *File Submitted!*\n{divider()}\n"
        f"🆔 Submission ID:  #{submission_id}\n"
        f"📌 Platform:       {meta['platform']}\n"
        f"📋 Task:           {meta['label']}\n"
        f"💵 Reward Rate:    {price_display}/piece\n"
        f"📅 Date:           {today_str()}\n"
        f"⏳ Review Time:    {meta['review_min']} minutes\n{divider()}\n"
        + t(cid,
            "এডমিন রিভিউর পর আপনার ব্যালেন্সে টাকা যোগ হবে।",
            "Balance will be credited after admin review.")
    )
    send_main_menu(cid)

# ═══════════════════════════════════════════════════════════
#  CATCH-ALL
# ═══════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: True)
def handle_fallback(message):
    cid      = message.chat.id
    userdata = get_user(cid)
    state    = userdata["current_state"] if userdata else STATE_IDLE

    if state == STATE_WAITING_FILE:
        bot.send_message(cid,
            t(cid, "⚠️ .xlsx ফাইল আপলোড করুন অথবা ❌ Cancel চাপুন।",
                   "⚠️ Upload your .xlsx file or press ❌ Cancel."),
            reply_markup=cancel_keyboard())
    elif state in (STATE_WAITING_ADDRESS, STATE_WAITING_AMOUNT):
        bot.send_message(cid,
            t(cid, "⚠️ সঠিক তথ্য দিন অথবা ❌ Cancel চাপুন।",
                   "⚠️ Enter valid info or press ❌ Cancel."),
            reply_markup=cancel_keyboard())
    elif state == STATE_BROADCAST and cid == ADMIN_ID:
        bot.send_message(ADMIN_ID,
            "⚠️ Broadcast mode চালু। মেসেজ পাঠান বা ❌ Cancel করুন।",
            reply_markup=cancel_keyboard())
    else:
        send_main_menu(cid, t(cid, "🏠 মেইন মেনু", "🏠 Main Menu"))

# ═══════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    logger.info("Bot starting — polling...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
