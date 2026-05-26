import os
import time
import logging
import sqlite3
import threading
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
DB_PATH      = os.environ.get("DB_PATH", "/app/data/bot_database.db")

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
#  DATABASE — thread-safe
# ═══════════════════════════════════════════════════════════
_db_lock = threading.Lock()

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    with _db_lock:
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS referral_earnings (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    referrer_id INTEGER NOT NULL,
                    amount_usd  REAL NOT NULL,
                    earned_at   TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (referrer_id) REFERENCES users(chat_id)
                )
            """)
            conn.commit()
    _migrate_db()
    logger.info("Database initialised at %s", DB_PATH)


def _migrate_db():
    with _db_lock:
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
    with _db_lock:
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


def check_state(chat_id, state):
    """Thread-safe single DB call for state check."""
    user = get_user(chat_id)
    return user is not None and user["current_state"] == state


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
    with _db_lock:
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET current_state=?, current_task_key=? WHERE chat_id=?",
                (state, task_key, chat_id),
            )
            conn.commit()


def set_user_lang(chat_id, lang):
    with _db_lock:
        with get_db() as conn:
            conn.execute("UPDATE users SET language=? WHERE chat_id=?", (lang, chat_id))
            conn.commit()


def set_user_currency(chat_id, currency):
    with _db_lock:
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


def get_new_referrals_24h(chat_id):
    with get_db() as conn:
        since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        return conn.execute(
            "SELECT COUNT(*) as c FROM users WHERE referred_by=? AND joined_at>=?",
            (chat_id, since)
        ).fetchone()["c"]


def get_referral_earned_30d(chat_id):
    with get_db() as conn:
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_usd),0) as s FROM referral_earnings WHERE referrer_id=? AND earned_at>=?",
            (chat_id, since)
        ).fetchone()
        return row["s"] if row else 0.0


def get_referral_earned_24h(chat_id):
    with get_db() as conn:
        since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_usd),0) as s FROM referral_earnings WHERE referrer_id=? AND earned_at>=?",
            (chat_id, since)
        ).fetchone()
        return row["s"] if row else 0.0


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
    with _db_lock:
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
    with _db_lock:
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

# Thread-safe session cache
_session_lock = threading.Lock()
_withdraw_sessions = {}

def session_get(chat_id):
    with _session_lock:
        return _withdraw_sessions.get(chat_id, {}).copy()

def session_set(chat_id, data):
    with _session_lock:
        _withdraw_sessions[chat_id] = data

def session_update(chat_id, key, value):
    with _session_lock:
        if chat_id not in _withdraw_sessions:
            _withdraw_sessions[chat_id] = {}
        _withdraw_sessions[chat_id][key] = value

def session_pop(chat_id):
    with _session_lock:
        return _withdraw_sessions.pop(chat_id, None)

def session_clear(chat_id):
    with _session_lock:
        _withdraw_sessions.pop(chat_id, None)


TASK_META = {
    # Instagram
    "ig_cookies": {
        "platform": "Instagram",
        "label": "Instagram Cookies I'D",
        "price_val": 3.10,
        "review_min": 1500,
        "description": "Submit Instagram accounts generated strictly with accurate session cookies.",
        "columns": "A = Username   |   B = Password",
    },
    "ig_2fa": {
        "platform": "Instagram",
        "label": "Instagram 2FA I'D",
        "price_val": 2.10,
        "review_min": 1500,
        "description": "Submit Instagram accounts with 2FA key enabled.",
        "columns": "A = Username   |   B = Password   |   C = 2FA Key",
    },
    # Facebook
    "fb_clone_6155x": {
        "platform": "Facebook",
        "label": "PC Clone 6155x/56x/57x I'D",
        "price_val": 3.00,
        "review_min": 1500,
        "description": "Create Facebook Accounts via Clone Method (6155x/56x/57x).",
        "columns": "A = UID   |   B = Password   |   C = Cookie",
    },
    "fb_clone_6158x": {
        "platform": "Facebook",
        "label": "PC Clone 6158x I'D",
        "price_val": 3.50,
        "review_min": 1500,
        "description": "Create Facebook Accounts via Clone Method (6158x).",
        "columns": "A = UID   |   B = Password   |   C = Cookie",
    },
    "fb_clone_1000x": {
        "platform": "Facebook",
        "label": "PC Clone 1000x I'D",
        "price_val": 11.00,
        "review_min": 1500,
        "description": "Create High Quality Facebook Accounts via specified custom configurations.",
        "columns": "A = UID   |   B = Password   |   C = Cookie",
    },
    "fb_num00_2fa": {
        "platform": "Facebook",
        "label": "Number 00 Friend 2FA I'D",
        "price_val": 3.00,
        "review_min": 1500,
        "description": "Submit Facebook accounts with Number 00 and Friend 2FA enabled.",
        "columns": "A = UID   |   B = Password   |   C = 2FA Key",
    },
    "fb_hotmail_30": {
        "platform": "Facebook",
        "label": "Hotmail 30+ Friend I'D",
        "price_val": 8.50,
        "review_min": 1500,
        "description": "Submit Facebook accounts with Hotmail 30+ Friends.",
        "columns": "A = UID   |   B = Password   |   C = 2FA Key   |   D = Full Hotmail",
    },
    "fb_2fa_cookies": {
        "platform": "Facebook",
        "label": "2FA + Cookies",
        "price_val": 4.80,
        "review_min": 1500,
        "description": "Submit Facebook accounts with 2FA and Cookies.",
        "columns": "A = UID   |   B = Password   |   C = Cookie   |   D = 2FA Key",
    },
    "fb_2fa_cookies_30": {
        "platform": "Facebook",
        "label": "2FA + Cookies (30+)",
        "price_val": 7.00,
        "review_min": 1500,
        "description": "Submit Facebook accounts with 2FA, Cookies and 30+ Friends.",
        "columns": "A = UID   |   B = Password   |   C = Cookie   |   D = 2FA Key",
    },
    # Gmail
    "gmail_fresh": {
        "platform": "Gmail",
        "label": "Fresh Gmail",
        "price_val": 11.00,
        "review_min": 1500,
        "description": "Create a brand new Gmail account using unique mobile recovery setups.",
        "columns": "A = Email   |   B = Password   |   C = Recovery Email",
    },
    "gmail_cookies": {
        "platform": "Gmail",
        "label": "Gmail Cookies",
        "price_val": 13.00,
        "review_min": 1500,
        "description": "Submit Gmail accounts with accurate session cookies.",
        "columns": "A = Email   |   B = Password   |   C = Cookie",
    },
}

# ═══════════════════════════════════════════════════════════
#  TEXT HELPER — bilingual
# ═══════════════════════════════════════════════════════════

def t(chat_id, bn_text, en_text):
    return bn_text if is_bn(chat_id) else en_text

# ═══════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════

def main_menu_keyboard(chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if is_bn(chat_id):
        kb.row("📋 কাজ",        "💰 ব্যালেন্স")
        kb.row("📥 উত্তোলন",   "👥 রেফারেল")
        kb.row("📞 সাপোর্ট",   "📂 সাবমিশন")
        kb.row("🌐 ভাষা",       "💱 কারেন্সি")
        kb.row("👤 প্রোফাইল")
    else:
        kb.row("📋 Tasks",      "💰 Balance")
        kb.row("📥 Withdraw",   "👥 Referrals")
        kb.row("📞 Support",    "📂 Submissions")
        kb.row("🌐 Language",   "💱 Currency")
        kb.row("👤 Profile")
    return kb


def cancel_keyboard(chat_id=None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if chat_id and is_bn(chat_id):
        kb.add("❌ বাতিল")
    else:
        kb.add("❌ Cancel")
    return kb


def platform_select_inline(chat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("Instagram", callback_data="platform_instagram"),
        types.InlineKeyboardButton("Facebook",  callback_data="platform_facebook"),
        types.InlineKeyboardButton("Gmail",     callback_data="platform_gmail"),
        types.InlineKeyboardButton(
            "❌ বাতিল" if is_bn(chat_id) else "❌ Cancel",
            callback_data="task_cancel"
        ),
    )
    return kb


def _price_label(task_key, currency):
    meta = TASK_META.get(task_key)
    if not meta:
        return ""
    return fmt_price(meta["price_val"], currency)


def instagram_type_inline(currency="BDT", chat_id=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("Set Admin Rate", callback_data="adminrate_instagram"),
        types.InlineKeyboardButton(
            f"Instagram Cookies I'D ({_price_label('ig_cookies', currency)})",
            callback_data="taskinfo_ig_cookies"),
        types.InlineKeyboardButton(
            f"Instagram 2FA I'D ({_price_label('ig_2fa', currency)})",
            callback_data="taskinfo_ig_2fa"),
        types.InlineKeyboardButton(
            "❌ বাতিল" if (chat_id and is_bn(chat_id)) else "❌ Cancel",
            callback_data="task_cancel"
        ),
    )
    return kb


def facebook_type_inline(currency="BDT", chat_id=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("Set Admin Rate", callback_data="adminrate_facebook"),
        types.InlineKeyboardButton(
            f"PC Clone 6155x/56x/57x I'D ({_price_label('fb_clone_6155x', currency)})",
            callback_data="taskinfo_fb_clone_6155x"),
        types.InlineKeyboardButton(
            f"PC Clone 6158x I'D ({_price_label('fb_clone_6158x', currency)})",
            callback_data="taskinfo_fb_clone_6158x"),
        types.InlineKeyboardButton(
            f"PC Clone 1000x I'D ({_price_label('fb_clone_1000x', currency)})",
            callback_data="taskinfo_fb_clone_1000x"),
        types.InlineKeyboardButton(
            f"Number 00 Friend 2FA I'D ({_price_label('fb_num00_2fa', currency)})",
            callback_data="taskinfo_fb_num00_2fa"),
        types.InlineKeyboardButton(
            f"Hotmail 30+ Friend I'D ({_price_label('fb_hotmail_30', currency)})",
            callback_data="taskinfo_fb_hotmail_30"),
        types.InlineKeyboardButton(
            f"2FA + Cookies ({_price_label('fb_2fa_cookies', currency)})",
            callback_data="taskinfo_fb_2fa_cookies"),
        types.InlineKeyboardButton(
            f"2FA + Cookies 30+ ({_price_label('fb_2fa_cookies_30', currency)})",
            callback_data="taskinfo_fb_2fa_cookies_30"),
        types.InlineKeyboardButton(
            "❌ বাতিল" if (chat_id and is_bn(chat_id)) else "❌ Cancel",
            callback_data="task_cancel"
        ),
    )
    return kb


def gmail_type_inline(currency="BDT", chat_id=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("Set Admin Rate", callback_data="adminrate_gmail"),
        types.InlineKeyboardButton(
            f"Fresh Gmail ({_price_label('gmail_fresh', currency)})",
            callback_data="taskinfo_gmail_fresh"),
        types.InlineKeyboardButton(
            f"Gmail Cookies ({_price_label('gmail_cookies', currency)})",
            callback_data="taskinfo_gmail_cookies"),
        types.InlineKeyboardButton(
            "❌ বাতিল" if (chat_id and is_bn(chat_id)) else "❌ Cancel",
            callback_data="task_cancel"
        ),
    )
    return kb


def task_action_inline(task_key, chat_id=None):
    kb = types.InlineKeyboardMarkup(row_width=2)
    submit_txt = "📤 ফাইল সাবমিট" if (chat_id and is_bn(chat_id)) else "📤 Submit File"
    cancel_txt = "❌ বাতিল" if (chat_id and is_bn(chat_id)) else "❌ Cancel"
    kb.add(
        types.InlineKeyboardButton(submit_txt, callback_data=f"task_{task_key}"),
        types.InlineKeyboardButton(cancel_txt, callback_data="task_cancel"),
    )
    return kb


def withdraw_method_inline(currency="BDT"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if currency == "BDT":
        kb.add(
            types.InlineKeyboardButton("bKash",  callback_data="withdraw_bkash"),
            types.InlineKeyboardButton("Nagad",  callback_data="withdraw_nagad"),
        )
    else:
        kb.add(
            types.InlineKeyboardButton("USDT (BEP-20)", callback_data="withdraw_usdt_bep"),
        )
    return kb


def currency_select_inline():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("BDT (Tk)", callback_data="currency_BDT"),
        types.InlineKeyboardButton("USDT",     callback_data="currency_USDT"),
    )
    return kb


def language_select_inline():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("বাংলা",   callback_data="lang_bn"),
        types.InlineKeyboardButton("English", callback_data="lang_en"),
    )
    return kb


def admin_contact_inline(label="Admin"):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(label, url=ADMIN_URL))
    return kb


def admin_file_action_inline(sub_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("Approve", callback_data=f"adm_approve_{sub_id}"),
        types.InlineKeyboardButton("Reject",  callback_data=f"adm_reject_{sub_id}"),
    )
    return kb

# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════

def send_main_menu(chat_id, text="🏠"):
    set_user_state(chat_id, STATE_IDLE)
    session_clear(chat_id)
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
    return "━━━━━━━━━━━━━━━"


def check_banned(chat_id):
    if is_banned(chat_id):
        bot.send_message(chat_id,
            t(chat_id,
              "🚫 আপনার একাউন্ট ব্যান করা হয়েছে। সাপোর্টে যোগাযোগ করুন।",
              "🚫 Your account has been banned. Contact support.")
        )
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

    with _db_lock:
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
                conn.execute(
                    "INSERT INTO referral_earnings (referrer_id, amount_usd) VALUES (?,?)",
                    (referrer_id, comm_usd),
                )
                comm_bdt = round(comm_usd * DOLLAR_RATE, 2)
                ref_text = f"\n🎁 Referrer ({referrer_id}) commission: {comm_bdt:.2f} Tk"
                try:
                    ref_currency = get_currency(referrer_id)
                    comm_str = fmt_amount(comm_usd, ref_currency)
                    bot.send_message(referrer_id,
                        t(referrer_id,
                          f"🎁 *রেফারেল কমিশন!*\nআপনার রেফার করা ইউজার টাস্ক সম্পন্ন করায় ২০% কমিশন পেয়েছেন।\n\n💰 কমিশন: *{comm_str}*",
                          f"🎁 *Referral Commission!*\nYour referred user completed a task. You earned 20% commission.\n\n💰 Commission: *{comm_str}*")
                    )
                except Exception:
                    pass

            conn.execute("UPDATE submissions SET status='approved' WHERE id=?", (sub_id,))
            conn.commit()

    bot.send_message(ADMIN_ID,
        f"✅ Submission #{sub_id} Approved!\n"
        f"💵 Paid: {total_payout_bdt:.2f} Tk ({total_items} x {price_bdt:.2f} Tk){ref_text}"
    )
    try:
        user_currency = get_currency(sub["chat_id"])
        payout_str = fmt_price(total_payout_bdt, user_currency)
        bot.send_message(sub["chat_id"],
            t(sub["chat_id"],
              f"✅ *টাস্ক অনুমোদিত!*\n{divider()}\n🆔 সাবমিশন: #{sub_id}\n📋 টাস্ক: {sub['task_type']}\n✅ গৃহীত: {total_items} accounts\n💰 জমা: *{payout_str}*\n{divider()}\nআপনার ব্যালেন্সে টাকা যোগ হয়েছে! ধন্যবাদ। 🙏",
              f"✅ *Task Approved!*\n{divider()}\n🆔 Submission: #{sub_id}\n📋 Task: {sub['task_type']}\n✅ Accepted: {total_items} accounts\n💰 Credited: *{payout_str}*\n{divider()}\nAmount added to your balance! Thank you. 🙏")
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
    with _db_lock:
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
            t(sub["chat_id"],
              f"❌ *টাস্ক বাতিল!*\n{divider()}\n🆔 সাবমিশন: #{sub_id}\n⚠️ ফাইলে সমস্যা থাকায় বাতিল হয়েছে। সাহায্যের জন্য সাপোর্টে যোগাযোগ করুন।",
              f"❌ *Task Rejected!*\n{divider()}\n🆔 Submission: #{sub_id}\n⚠️ Your submission was rejected due to file issues. Contact Support for help.")
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
    with _db_lock:
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
            t(withd["chat_id"],
              f"🎉 *উত্তোলন সফল!*\n{divider()}\n🆔 রিকোয়েস্ট: #{wid}\n💳 মাধ্যম: {withd['method']}\n💵 পরিমাণ: *{amt}*\n{divider()}\nপেমেন্ট পাঠিয়ে দেওয়া হয়েছে। ধন্যবাদ! 🙏",
              f"🎉 *Withdrawal Successful!*\n{divider()}\n🆔 Request: #{wid}\n💳 Method: {withd['method']}\n💵 Amount: *{amt}*\n{divider()}\nPayment has been sent. Thank you! 🙏")
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_approve_") or c.data.startswith("adm_reject_"))
def handle_admin_inline_action(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Not authorized.")
        return

    parts  = call.data.split("_")
    action = parts[1]
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
        with _db_lock:
            with get_db() as conn:
                conn.execute("UPDATE submissions SET status='rejected' WHERE id=?", (sub_id,))
                conn.commit()
        bot.edit_message_reply_markup(ADMIN_ID, call.message.message_id, reply_markup=None)
        bot.send_message(ADMIN_ID, f"🔴 Submission #{sub_id} rejected.")
        try:
            bot.send_message(sub["chat_id"],
                t(sub["chat_id"],
                  f"❌ *টাস্ক বাতিল!*\n{divider()}\n🆔 সাবমিশন: #{sub_id}\n⚠️ ফাইলে সমস্যা। সাপোর্টে যোগাযোগ করুন।",
                  f"❌ *Task Rejected!*\n{divider()}\n🆔 Submission: #{sub_id}\n⚠️ Rejected due to file issues. Contact Support.")
            )
        except Exception:
            pass
    else:
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
    with _db_lock:
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
    with _db_lock:
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
    bal_usd  = user["balance"] if user else 0.0
    currency = get_currency(cid)
    bal_str  = fmt_amount(bal_usd, currency)
    bot.send_message(cid, f"💰 {t(cid, 'আপনার ব্যালেন্স', 'Your balance')}: *{bal_str}*")


@bot.message_handler(func=lambda m: m.text in ("📋 Tasks", "📋 কাজ"))
def handle_task_submit(message):
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid = message.chat.id
    set_user_state(cid, STATE_IDLE)
    txt = t(cid, "যেকোনো একটি কাজ সিলেক্ট করুন:", "Select a task:")
    bot.send_message(cid, txt, reply_markup=platform_select_inline(cid))


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
        fee_val     = WITHDRAW_FEE_BDT
        bal_str     = f"{current_val:.2f} Tk"
        fee_str     = f"{fee_val:.2f} Tk"
        min_str     = f"{MIN_WITHDRAW_BDT:.2f} Tk"
    else:
        current_val = bal_usd
        fee_val     = WITHDRAW_FEE_USDT
        bal_str     = f"${current_val:.4f} USDT"
        fee_str     = f"${fee_val:.2f} USDT"
        min_str     = f"${MIN_WITHDRAW_USDT:.2f} USDT"

    last_time = get_last_withdrawal_time(cid)
    if last_time:
        last_dt = datetime.strptime(last_time[:19], "%Y-%m-%d %H:%M:%S")
        elapsed = datetime.now() - last_dt
        if elapsed < timedelta(hours=WITHDRAW_COOLDOWN):
            remaining = timedelta(hours=WITHDRAW_COOLDOWN) - elapsed
            h, rem = divmod(int(remaining.total_seconds()), 3600)
            mn     = rem // 60
            bot.send_message(cid,
                t(cid,
                  f"⏳ পরবর্তী উত্তোলনের জন্য আরো *{h}h {mn}m* অপেক্ষা করুন।",
                  f"⏳ Please wait *{h}h {mn}m* more before next withdrawal.")
            )
            return

    set_user_state(cid, STATE_IDLE)
    bot.send_message(cid,
        t(cid,
          f"📥 *উত্তোলন পদ্ধতি বেছে নিন:*\n{divider()}\n💰 ব্যালেন্স: *{bal_str}*\n💸 ফি: {fee_str}\n📋 সর্বনিম্ন: {min_str}",
          f"📥 *Choose Withdraw Method:*\n{divider()}\n💰 Balance: *{bal_str}*\n💸 Fee: {fee_str}\n📋 Minimum: {min_str}"),
        reply_markup=withdraw_method_inline(currency)
    )


@bot.message_handler(func=lambda m: m.text in ("👤 Profile", "👤 প্রোফাইল"))
def handle_profile(message):
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid = message.chat.id
    bot.send_message(cid,
        f"👤 {t(cid, 'আপনার প্রোফাইল', 'Your profile')}:\n"
        f"ℹ️ ID: `{cid}`"
    )


@bot.message_handler(func=lambda m: m.text in ("📞 Support", "📞 সাপোর্ট"))
def handle_support(message):
    ensure_user(message)
    cid = message.chat.id
    if is_bn(cid):
        txt = (
            "╔════════════════╗\n"
            "║  🎯 সাপোর্ট   ║\n"
            "╚════════════════╝\n\n"
            "🟢 Admin Online\n"
            f"{divider()}\n"
            "❓ কোনো সমস্যা?\n"
            "⚡ সাথে সাথে সমাধান পাবেন!\n\n"
            "📲 @nolab420"
        )
    else:
        txt = (
            "╔════════════════╗\n"
            "║  🎯 SUPPORT    ║\n"
            "╚════════════════╝\n\n"
            "🟢 Admin Online\n"
            f"{divider()}\n"
            "❓ Any problem?\n"
            "⚡ Get instant solution!\n\n"
            "📲 @nolab420"
        )
    label = "💬 যোগাযোগ করুন →" if is_bn(cid) else "💬 Contact Now →"
    bot.send_message(cid, txt, reply_markup=admin_contact_inline(label))


@bot.message_handler(func=lambda m: m.text in ("👥 Referrals", "👥 রেফারেল"))
def handle_my_referrals(message):
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid         = message.chat.id
    user        = get_user(cid)
    currency    = get_currency(cid)
    ref_count   = get_referral_count(cid)
    new_24h     = get_new_referrals_24h(cid)
    earned_30d  = get_referral_earned_30d(cid)
    earned_24h  = get_referral_earned_24h(cid)
    code        = user["referral_code"] if user else f"REF{cid}"
    link        = f"https://t.me/{BOT_USERNAME}?start={code}"

    earn_30d_str = fmt_amount(earned_30d, currency)
    earn_24h_str = fmt_amount(earned_24h, currency)

    if is_bn(cid):
        bot.send_message(cid,
            f"👥 মোট রেফারেল: {ref_count}\n"
            f"🆕 গত ২৪ ঘণ্টায় নতুন: {new_24h}\n\n"
            f"💰 গত ৩০ দিনে আয়: {earn_30d_str}\n"
            f"💵 গত ২৪ ঘণ্টায় আয়: {earn_24h_str}\n\n"
            f"🔗 আপনার রেফারেল লিংক:\n{link}\n\n"
            f"ℹ️ আপনি আপনার প্রতিটি রেফারেলের আয়ের ২০% পাবেন।"
        )
    else:
        bot.send_message(cid,
            f"👥 Total referrals: {ref_count}\n"
            f"🆕 New in last 24 hours: {new_24h}\n\n"
            f"💰 Earned in last 30 days: {earn_30d_str}\n"
            f"💵 Earned in last 24 hours: {earn_24h_str}\n\n"
            f"🔗 Your referral link:\n{link}\n\n"
            f"ℹ️ You receive 20% of the earnings of each of your referrals from the tasks they complete."
        )


@bot.message_handler(func=lambda m: m.text in ("📂 Submissions", "📂 সাবমিশন"))
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
    title = f"📂 *{t(cid, 'সর্বশেষ', 'Last')} {len(rows)} {t(cid, 'সাবমিশন', 'Submissions')}*"
    lines = [title, divider()]
    for row in rows:
        lines.append(
            f"#{row['id']} {icons.get(row['status'], '❓')} "
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
    name = "BDT (Tk)" if currency == "BDT" else "USDT"
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
    if lang == "Bangla":
        txt = "✅ ভাষা বাংলায় পরিবর্তন করা হয়েছে।"
    else:
        txt = "✅ Language changed to English."
    bot.send_message(cid, txt, reply_markup=main_menu_keyboard(cid))


@bot.message_handler(func=lambda m: m.text in ("❌ Cancel", "❌ বাতিল"))
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

    if platform == "instagram":
        txt = t(cid, "Instagram — টাস্ক সিলেক্ট করুন:", "Instagram — Select a task:")
        bot.send_message(cid, txt, reply_markup=instagram_type_inline(currency, cid))
    elif platform == "facebook":
        txt = t(cid, "Facebook — টাস্ক সিলেক্ট করুন:", "Facebook — Select a task:")
        bot.send_message(cid, txt, reply_markup=facebook_type_inline(currency, cid))
    elif platform == "gmail":
        txt = t(cid, "Gmail — টাস্ক সিলেক্ট করুন:", "Gmail — Select a task:")
        bot.send_message(cid, txt, reply_markup=gmail_type_inline(currency, cid))


@bot.callback_query_handler(func=lambda c: c.data.startswith("adminrate_"))
def handle_admin_rate(call):
    bot.answer_callback_query(call.id)
    cid      = call.message.chat.id
    platform = call.data.split("_", 1)[1].capitalize()
    bot.send_message(cid,
        t(cid,
          f"কাস্টম {platform} রেট সেট করতে এডমিনের সাথে যোগাযোগ করুন:",
          f"Contact admin to set a custom {platform} rate:"),
        reply_markup=admin_contact_inline("💬 @nolab420")
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
        f"📋 *{meta['label']}*\n{divider()}\n"
        f"💵 {t(cid, 'রিওয়ার্ড', 'Reward')}:        {price_display}/piece\n"
        f"⏳ {t(cid, 'রিভিউ সময়', 'Review Time')}:  {meta['review_min']} minutes\n{divider()}\n"
        f"📄 {t(cid, 'নির্দেশনা', 'Instructions')}:\n{meta['description']}\n{divider()}\n"
        f"📂 {t(cid, 'প্রয়োজনীয় কলাম', 'Required Columns')}:\n`{meta['columns']}`",
        reply_markup=task_action_inline(task_key, cid)
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
        f"📤 *{t(cid, 'টাস্ক সাবমিট', 'Submit Task')}*\n{divider()}\n"
        f"📅 {t(cid, 'তারিখ', 'Date')}:    {today_str()}\n"
        f"📋 {t(cid, 'টাস্ক', 'Task')}:    {meta['label']}\n"
        f"💵 {t(cid, 'রিওয়ার্ড', 'Reward')}:  {price_display}/piece\n{divider()}\n"
        f"📂 {t(cid, 'প্রয়োজনীয় কলাম', 'Required Columns')}:\n`{meta['columns']}`\n{divider()}\n"
        + t(cid,
            "আপনার *.xlsx* ফাইলটি আপলোড করুন।\nবাতিল করতে ❌ বাতিল চাপুন।",
            "Upload your *.xlsx* file.\nPress ❌ Cancel to abort."),
        reply_markup=cancel_keyboard(cid)
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
        min_val     = MIN_WITHDRAW_BDT
    else:
        current_val = bal_usd
        fee_val     = WITHDRAW_FEE_USDT
        unit        = "USDT"
        bal_str     = f"${current_val:.4f} USDT"
        fee_str     = f"${fee_val:.2f} USDT"
        min_str     = f"${MIN_WITHDRAW_USDT:.2f} USDT"
        min_val     = MIN_WITHDRAW_USDT

    if current_val < min_val:
        bot.send_message(cid,
            t(cid,
              f"⚠️ *অপর্যাপ্ত ব্যালেন্স*\n\n💰 আপনার ব্যালেন্স: *{bal_str}*\n📋 সর্বনিম্ন: {min_str}\n\nআরো টাস্ক সম্পন্ন করুন।",
              f"⚠️ *Insufficient Balance*\n\n💰 Your Balance: *{bal_str}*\n📋 Minimum: {min_str}\n\nPlease complete more tasks.")
        )
        return

    set_user_state(cid, STATE_WAITING_AMOUNT)
    session_set(cid, {
        "withdraw_method":   method_key,
        "withdraw_label":    method_label,
        "withdraw_currency": currency,
    })
    bot.send_message(cid,
        f"✅ *{method_label}* {t(cid, 'সিলেক্ট করা হয়েছে।', 'selected.')}\n{divider()}\n"
        f"💸 {t(cid, 'ফি', 'Fee')}:        {fee_str}\n"
        f"🔢 {t(cid, 'সর্বনিম্ন', 'Minimum')}:    {min_str}\n"
        f"💰 {t(cid, 'ব্যালেন্স', 'Balance')}:    *{bal_str}*\n{divider()}\n"
        + t(cid,
            f"কত {unit} উত্তোলন করতে চান? (শুধু সংখ্যা লিখুন)",
            f"How much {unit} to withdraw? (numbers only)"),
        reply_markup=cancel_keyboard(cid)
    )

# ═══════════════════════════════════════════════════════════
#  STATE WORKFLOWS
# ═══════════════════════════════════════════════════════════

@bot.message_handler(
    func=lambda m: (
        m.chat.id == ADMIN_ID
        and check_state(m.chat.id, STATE_BROADCAST)
        and m.text not in ("❌ Cancel", "❌ বাতিল")
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
    func=lambda m: check_state(m.chat.id, STATE_WAITING_AMOUNT)
)
def handle_withdraw_amount(message):
    cid = message.chat.id
    try:
        amount = float(message.text.strip().replace(",", ""))
    except ValueError:
        bot.send_message(cid,
            t(cid, "⚠️ সঠিক সংখ্যা দিন।", "⚠️ Enter a valid number."),
            reply_markup=cancel_keyboard(cid))
        return

    session  = session_get(cid)
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
        bot.send_message(cid, f"⚠️ Minimum: {min_val:.4f} {unit}", reply_markup=cancel_keyboard(cid))
        return
    if amount > current_val:
        bot.send_message(cid,
            t(cid,
              f"⚠️ আপনার ব্যালেন্স *{current_val:.4f} {unit}* — এত উত্তোলন সম্ভব নয়।",
              f"⚠️ Your balance is *{current_val:.4f} {unit}* — insufficient."),
            reply_markup=cancel_keyboard(cid)
        )
        return

    session["withdraw_amount"] = amount
    session_set(cid, session)
    net   = round(amount - fee_val, 6)
    label = session.get("withdraw_label", "Wallet")

    set_user_state(cid, STATE_WAITING_ADDRESS)
    bot.send_message(cid,
        f"💵 {t(cid, 'পরিমাণ', 'Amount')}:      *{amount:.4f} {unit}*\n"
        f"📋 {t(cid, 'ফি', 'Fee')}:         {fee_val:.4f} {unit}\n"
        f"✅ {t(cid, 'পাবেন', 'You will get')}:  *{net:.4f} {unit}*\n{divider()}\n"
        + t(cid,
            f"📲 আপনার {label} নম্বর / ঠিকানা লিখুন:",
            f"📲 Enter your {label} number / address:"),
        reply_markup=cancel_keyboard(cid)
    )


@bot.message_handler(
    func=lambda m: check_state(m.chat.id, STATE_WAITING_ADDRESS)
)
def handle_withdraw_address(message):
    cid     = message.chat.id
    address = message.text.strip()
    session = session_pop(cid)

    if not session or "withdraw_amount" not in session:
        bot.send_message(cid, t(cid, "⚠️ সেশন মেয়াদ শেষ। আবার চেষ্টা করুন।", "⚠️ Session expired. Try again."))
        send_main_menu(cid)
        return

    if len(address) < 5 or len(address) > 100:
        session_set(cid, session)
        bot.send_message(cid,
            t(cid, "⚠️ সঠিক নম্বর বা ঠিকানা লিখুন (৫–১০০ অক্ষর)।", "⚠️ Enter a valid address (5–100 chars)."),
            reply_markup=cancel_keyboard(cid))
        return

    label    = session["withdraw_label"]
    amount   = session["withdraw_amount"]
    currency = session.get("withdraw_currency", "BDT")
    fee_val  = WITHDRAW_FEE_BDT if currency == "BDT" else WITHDRAW_FEE_USDT
    net      = round(amount - fee_val, 6)
    unit     = "Tk" if currency == "BDT" else "USDT"

    wid = save_withdrawal(cid, label, address, amount, currency)

    bot.send_message(cid,
        t(cid,
          f"✅ *উত্তোলন রিকোয়েস্ট সাবমিট হয়েছে!*\n{divider()}\n🆔 রিকোয়েস্ট: #{wid}\n💳 মাধ্যম: {label}\n📲 ঠিকানা: `{address}`\n💰 পরিমাণ: {amount:.4f} {unit}\n📋 ফি: {fee_val:.4f} {unit}\n💵 পাবেন: *{net:.4f} {unit}*\n{divider()}\n⏳ এডমিন ২৪ ঘণ্টার মধ্যে পেমেন্ট প্রসেস করবেন।",
          f"✅ *Withdrawal Request Submitted!*\n{divider()}\n🆔 Request: #{wid}\n💳 Method: {label}\n📲 Address: `{address}`\n💰 Amount: {amount:.4f} {unit}\n📋 Fee: {fee_val:.4f} {unit}\n💵 You will get: *{net:.4f} {unit}*\n{divider()}\n⏳ Admin will process payment within 24 hours.")
    )
    notify_admin(
        f"💸 *New Withdrawal #{wid}*\n"
        f"👤 Chat ID: `{cid}`\n"
        f"💳 Method: {label} ({currency})\n"
        f"📲 Address: `{address}`\n"
        f"💰 Amount: {amount:.4f} {unit}  |  Net: {net:.4f} {unit}\n\n"
        f"Approve: `/pay {wid}`"
    )
    send_main_menu(cid)

# ═══════════════════════════════════════════════════════════
#  FILE HANDLER
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

    user  = get_user(cid)
    uname = f"@{user['username']}" if user and user["username"] else user["first_name"] if user else str(cid)

    if ADMIN_ID:
        try:
            caption = (
                f"📁 *New Submission #{submission_id}*\n"
                f"👤 {uname} (`{cid}`)\n"
                f"📌 {meta['platform']} — {meta['label']}\n"
                f"💵 {meta['price_val']:.2f} Tk/piece\n"
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
                f"📌 {meta['platform']} — {meta['label']} ({meta['price_val']:.2f} Tk)\n"
                f"📄 {file_name} ({file_size/1024:.1f} KB)\n\n"
                f"✅ `/approve {submission_id} [Total_Accounts]`\n"
                f"❌ `/reject {submission_id}`"
            )

    bot.send_message(cid,
        t(cid,
          f"✅ *ফাইল সাবমিট হয়েছে!*\n{divider()}\n🆔 সাবমিশন: #{submission_id}\n📌 প্ল্যাটফর্ম: {meta['platform']}\n📋 টাস্ক: {meta['label']}\n💵 রিওয়ার্ড: {price_display}/piece\n📅 তারিখ: {today_str()}\n⏳ রিভিউ সময়: {meta['review_min']} minutes\n{divider()}\nএডমিন রিভিউর পর আপনার ব্যালেন্সে টাকা যোগ হবে।",
          f"✅ *File Submitted!*\n{divider()}\n🆔 Submission: #{submission_id}\n📌 Platform: {meta['platform']}\n📋 Task: {meta['label']}\n💵 Reward: {price_display}/piece\n📅 Date: {today_str()}\n⏳ Review Time: {meta['review_min']} minutes\n{divider()}\nBalance will be credited after admin review.")
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
            t(cid, "⚠️ .xlsx ফাইল আপলোড করুন অথবা ❌ বাতিল চাপুন।",
                   "⚠️ Upload your .xlsx file or press ❌ Cancel."),
            reply_markup=cancel_keyboard(cid))
    elif state in (STATE_WAITING_ADDRESS, STATE_WAITING_AMOUNT):
        bot.send_message(cid,
            t(cid, "⚠️ সঠিক তথ্য দিন অথবা ❌ বাতিল চাপুন।",
                   "⚠️ Enter valid info or press ❌ Cancel."),
            reply_markup=cancel_keyboard(cid))
    elif state == STATE_BROADCAST and cid == ADMIN_ID:
        bot.send_message(ADMIN_ID,
            "⚠️ Broadcast mode চালু। মেসেজ পাঠান বা ❌ Cancel করুন।",
            reply_markup=cancel_keyboard())
    else:
        send_main_menu(cid, t(cid, "🏠 মেইন মেনু", "🏠 Main Menu"))

# ═══════════════════════════════════════════════════════════
#  ENTRY POINT — auto-restart on crash
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    logger.info("Bot starting — polling...")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=20)
        except Exception as e:
            logger.error("Polling crashed: %s", e)
            time.sleep(5)
            logger.info("Restarting polling...")
