import os
import time
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta
import telebot
from telebot import types

# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    raise ValueError("FATAL: BOT_TOKEN environment variable is missing or invalid!")

ADMIN_URL = os.environ.get("ADMIN_URL", "https://t.me/nolab420")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8036268898"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "YourBotUsername")
DB_PATH = os.environ.get("DB_PATH", "/app/data/bot_database.db")

# Rates & Limits
DOLLAR_RATE = 120.0
MIN_WITHDRAW_BDT = 20.00
MIN_WITHDRAW_USDT = 0.20
WITHDRAW_FEE_BDT = 2.50
WITHDRAW_FEE_USDT = 0.025
REFERRAL_COMM_PCT = 0.15
WITHDRAW_COOLDOWN = 24

# Helsinki timezone offset (UTC+3 in summer, UTC+2 in winter — using UTC+2 as base)
HELSINKI_UTC_OFFSET = 3  # EET/EEST

# Daily prize pool
DAILY_PRIZES = {1: 4.0, 2: 2.0, 3: 1.0}
DAILY_PRIZE_DEFAULT = 0.5  # 4th-10th place

# States
STATE_IDLE = "idle"
STATE_WAITING_FILE = "waiting_file"
STATE_WAITING_ADDRESS = "waiting_address"
STATE_WAITING_AMOUNT = "waiting_amount"
STATE_BROADCAST = "broadcast"

# ═══════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# DATABASE — thread-safe
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
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    joined_at TEXT DEFAULT (datetime('now')),
                    balance REAL DEFAULT 0.0,
                    total_earned REAL DEFAULT 0.0,
                    referral_code TEXT UNIQUE,
                    referred_by INTEGER,
                    language TEXT DEFAULT 'English',
                    currency TEXT DEFAULT 'BDT',
                    current_state TEXT DEFAULT 'idle',
                    current_task_key TEXT,
                    is_banned INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    file_id TEXT,
                    submitted_at TEXT DEFAULT (datetime('now')),
                    status TEXT DEFAULT 'pending',
                    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    method TEXT NOT NULL,
                    address TEXT NOT NULL,
                    amount REAL NOT NULL,
                    fee REAL NOT NULL,
                    net_amount REAL NOT NULL,
                    currency TEXT DEFAULT 'BDT',
                    requested_at TEXT DEFAULT (datetime('now')),
                    status TEXT DEFAULT 'pending',
                    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS referral_earnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    referrer_id INTEGER NOT NULL,
                    amount_usd REAL NOT NULL,
                    earned_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (referrer_id) REFERENCES users(chat_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_prizes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    prize_date TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    prize_usd REAL NOT NULL,
                    awarded_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
                )
            """)
            conn.commit()
    _migrate_db()
    logger.info("Database initialised at %s", DB_PATH)

def _migrate_db():
    with _db_lock:
        with get_db() as conn:
            u_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
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
            existing = conn.execute("SELECT chat_id, referred_by FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            if not existing:
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
            "SELECT id FROM submissions WHERE chat_id=? AND task_type=? AND status='pending'",
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
# DAILY LEADERBOARD
# ═══════════════════════════════════════════════════════════
def get_daily_top10():
    """Get top 10 users by approved submissions today."""
    with get_db() as conn:
        today = date.today().strftime("%Y-%m-%d")
        rows = conn.execute(
            """SELECT u.chat_id, u.first_name,
               COUNT(s.id) as completed
               FROM submissions s
               JOIN users u ON u.chat_id = s.chat_id
               WHERE s.status='approved' AND DATE(s.submitted_at)=?
               GROUP BY s.chat_id
               ORDER BY completed DESC
               LIMIT 10""",
            (today,)
        ).fetchall()
        return rows

def get_user_daily_rank(chat_id):
    """Get user's rank and completed count for today."""
    with get_db() as conn:
        today = date.today().strftime("%Y-%m-%d")
        rows = conn.execute(
            """SELECT u.chat_id,
               COUNT(s.id) as completed
               FROM submissions s
               JOIN users u ON u.chat_id = s.chat_id
               WHERE s.status='approved' AND DATE(s.submitted_at)=?
               GROUP BY s.chat_id
               ORDER BY completed DESC""",
            (today,)
        ).fetchall()
        for i, row in enumerate(rows):
            if row["chat_id"] == chat_id:
                return i + 1, row["completed"]
        return None, 0

def mask_id(chat_id):
    s = str(chat_id)
    if len(s) <= 4:
        return s
    return s[:4] + "***" + s[-2:]

def award_daily_prizes():
    """Award prizes to top 10 users. Called at 01:00 Helsinki time."""
    top10 = get_daily_top10()
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    with _db_lock:
        with get_db() as conn:
            for i, row in enumerate(top10):
                rank = i + 1
                prize_usd = DAILY_PRIZES.get(rank, DAILY_PRIZE_DEFAULT if rank <= 10 else 0)
                if prize_usd <= 0:
                    continue
                already = conn.execute(
                    "SELECT id FROM daily_prizes WHERE chat_id=? AND prize_date=?",
                    (row["chat_id"], yesterday)
                ).fetchone()
                if already:
                    continue
                conn.execute(
                    "INSERT INTO daily_prizes (chat_id, prize_date, rank, prize_usd) VALUES (?,?,?,?)",
                    (row["chat_id"], yesterday, rank, prize_usd)
                )
                conn.execute(
                    "UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE chat_id=?",
                    (prize_usd, prize_usd, row["chat_id"])
                )
                conn.commit()
                try:
                    currency = get_currency(row["chat_id"])
                    prize_str = fmt_amount(prize_usd, currency)
                    bot.send_message(row["chat_id"],
                        t(row["chat_id"],
                          f"🏆 *অভিনন্দন!* আজকের লিডারবোর্ডে আপনি #{rank} স্থানে ছিলেন!\n💰 পুরস্কার: {prize_str} আপনার ব্যালেন্সে যোগ হয়েছে!",
                          f"🏆 *Congratulations!* You ranked #{rank} in today's leaderboard!\n💰 Prize: {prize_str} has been added to your balance!")
                    )
                except Exception:
                    pass

def daily_prize_scheduler():
    """Background thread that awards prizes at 01:00 Helsinki time."""
    while True:
        now_utc = datetime.utcnow()
        helsinki_now = now_utc + timedelta(hours=HELSINKI_UTC_OFFSET)
        # Next 01:00 Helsinki
        target = helsinki_now.replace(hour=1, minute=0, second=0, microsecond=0)
        if helsinki_now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - helsinki_now).total_seconds()
        time.sleep(wait_seconds)
        try:
            award_daily_prizes()
        except Exception as e:
            logger.error("Daily prize error: %s", e)

# ═══════════════════════════════════════════════════════════
# TEAM LEADERBOARD (referral)
# ═══════════════════════════════════════════════════════════
def get_team_leaderboard(referrer_id, limit=10):
    """Get top users referred by this user, sorted by total_earned."""
    with get_db() as conn:
        return conn.execute(
            """SELECT chat_id, first_name, total_earned,
               (SELECT COUNT(*) FROM submissions WHERE chat_id=u.chat_id AND status='approved') as completed
               FROM users u
               WHERE referred_by=?
               ORDER BY completed DESC
               LIMIT ?""",
            (referrer_id, limit)
        ).fetchall()

def get_team_user_rank(referrer_id, chat_id):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT chat_id,
               (SELECT COUNT(*) FROM submissions WHERE chat_id=u.chat_id AND status='approved') as completed
               FROM users u
               WHERE referred_by=?
               ORDER BY completed DESC""",
            (referrer_id,)
        ).fetchall()
        for i, row in enumerate(rows):
            if row["chat_id"] == chat_id:
                return i + 1, row["completed"]
        return None, 0

# ═══════════════════════════════════════════════════════════
# BOT INSTANCE
# ═══════════════════════════════════════════════════════════
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

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
    "ig_cookies": {
        "platform": "Instagram",
        "label": "Instagram Cookies I'D",
        "price_val": 3.10,
        "review_min": 1500,
        "description": "Submit Instagram accounts generated strictly with accurate session cookies.",
        "description_bn": "সঠিক সেশন কুকিজ সহ Instagram অ্যাকাউন্ট জমা দিন।",
        "columns": "A = Username | B = Password",
    },
    "ig_2fa": {
        "platform": "Instagram",
        "label": "Instagram 2FA I'D",
        "price_val": 2.10,
        "review_min": 1500,
        "description": "Submit Instagram accounts with 2FA key enabled.",
        "description_bn": "2FA কি সহ Instagram অ্যাকাউন্ট জমা দিন।",
        "columns": "A = Username | B = Password | C = 2FA Key",
    },
    "fb_clone_6155x": {
        "platform": "Facebook",
        "label": "PC Clone 6155x/56x/57x I'D",
        "price_val": 3.00,
        "review_min": 1500,
        "description": "Create Facebook Accounts via Clone Method (6155x/56x/57x).",
        "description_bn": "ক্লোন পদ্ধতিতে (6155x/56x/57x) Facebook অ্যাকাউন্ট তৈরি করুন।",
        "columns": "A = UID | B = Password | C = Cookie",
    },
    "fb_clone_6158x": {
        "platform": "Facebook",
        "label": "PC Clone 6158x I'D",
        "price_val": 3.50,
        "review_min": 1500,
        "description": "Create Facebook Accounts via Clone Method (6158x).",
        "description_bn": "ক্লোন পদ্ধতিতে (6158x) Facebook অ্যাকাউন্ট তৈরি করুন।",
        "columns": "A = UID | B = Password | C = Cookie",
    },
    "fb_clone_1000x": {
        "platform": "Facebook",
        "label": "PC Clone 1000x I'D",
        "price_val": 11.00,
        "review_min": 1500,
        "description": "Create High Quality Facebook Accounts via specified custom configuration.",
        "description_bn": "কাস্টম কনফিগারেশনে উচ্চমানের Facebook অ্যাকাউন্ট তৈরি করুন।",
        "columns": "A = UID | B = Password | C = Cookie",
    },
    "fb_num00_2fa": {
        "platform": "Facebook",
        "label": "Number 00 Friend 2FA I'D",
        "price_val": 3.00,
        "review_min": 1500,
        "description": "Submit Facebook accounts with Number 00 and Friend 2FA enabled.",
        "description_bn": "Number 00 এবং Friend 2FA সহ Facebook অ্যাকাউন্ট জমা দিন।",
        "columns": "A = UID | B = Password | C = 2FA Key",
    },
    "fb_hotmail_30": {
        "platform": "Facebook",
        "label": "Hotmail 30+ Friend I'D",
        "price_val": 8.50,
        "review_min": 1500,
        "description": "Submit Facebook accounts with Hotmail 30+ Friends.",
        "description_bn": "Hotmail ও ৩০+ বন্ধুসহ Facebook অ্যাকাউন্ট জমা দিন।",
        "columns": "A = UID | B = Password | C = 2FA Key | D = Full Hotmail",
    },
    "fb_2fa_cookies": {
        "platform": "Facebook",
        "label": "2FA + Cookies",
        "price_val": 4.80,
        "review_min": 1500,
        "description": "Submit Facebook accounts with 2FA and Cookies.",
        "description_bn": "2FA ও কুকিজ সহ Facebook অ্যাকাউন্ট জমা দিন।",
        "columns": "A = UID | B = Password | C = Cookie | D = 2FA Key",
    },
    "fb_2fa_cookies_30": {
        "platform": "Facebook",
        "label": "2FA + Cookies (30+)",
        "price_val": 7.00,
        "review_min": 1500,
        "description": "Submit Facebook accounts with 2FA, Cookies and 30+ Friends.",
        "description_bn": "2FA, কুকিজ ও ৩০+ বন্ধুসহ Facebook অ্যাকাউন্ট জমা দিন।",
        "columns": "A = UID | B = Password | C = Cookie | D = 2FA Key",
    },
    "gmail_fresh": {
        "platform": "Gmail",
        "label": "Fresh Gmail",
        "price_val": 11.00,
        "review_min": 1500,
        "description": "Create a brand new Gmail account using unique mobile recovery setups.",
        "description_bn": "ইউনিক মোবাইল রিকভারি দিয়ে নতুন Gmail অ্যাকাউন্ট তৈরি করুন।",
        "columns": "A = Email | B = Password | C = Recovery Email",
    },
    "gmail_cookies": {
        "platform": "Gmail",
        "label": "Gmail Cookies",
        "price_val": 13.00,
        "review_min": 1500,
        "description": "Submit Gmail accounts with accurate session cookies.",
        "description_bn": "সঠিক সেশন কুকিজ সহ Gmail অ্যাকাউন্ট জমা দিন।",
        "columns": "A = Email | B = Password | C = Cookie",
    },
}

# ═══════════════════════════════════════════════════════════
# TEXT HELPER — bilingual
# ═══════════════════════════════════════════════════════════
def t(chat_id, bn_text, en_text):
    return bn_text if is_bn(chat_id) else en_text

# ═══════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════
def main_menu_keyboard(chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if is_bn(chat_id):
        kb.row("📋 কাজ", "💰 ব্যালেন্স")
        kb.row("📤 উত্তোলন", "👥 রেফারেল")
        kb.row("📞 সাপোর্ট", "📁 সাবমিশন")
        kb.row("🌐 ভাষা", "💲 কারেন্সি")
        kb.row("👤 প্রোফাইল", "🏆 টপ")
    else:
        kb.row("📋 Tasks", "💰 Balance")
        kb.row("📤 Withdraw", "👥 Referrals")
        kb.row("📞 Support", "📁 Submissions")
        kb.row("🌐 Language", "💲 Currency")
        kb.row("👤 Profile", "🏆 Top")
    return kb

def cancel_keyboard(chat_id=None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if chat_id and is_bn(chat_id):
        kb.add("❌ বাতিল")
    else:
        kb.add("❌ Cancel")
    return kb

def platform_select_keyboard(chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("Instagram")
    kb.row("Facebook")
    kb.row("Gmail")
    if is_bn(chat_id):
        kb.row("❌ বাতিল")
    else:
        kb.row("❌ Cancel")
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
            types.InlineKeyboardButton("bKash", callback_data="withdraw_bkash"),
            types.InlineKeyboardButton("Nagad", callback_data="withdraw_nagad"),
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
        types.InlineKeyboardButton("USDT", callback_data="currency_USDT"),
    )
    return kb

def language_select_inline():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("বাংলা", callback_data="lang_bn"),
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
        types.InlineKeyboardButton("Reject", callback_data=f"adm_reject_{sub_id}"),
    )
    return kb

def instagram_task_keyboard(chat_id, currency="BDT"):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(f"Instagram Cookies I'D ({fmt_price(TASK_META['ig_cookies']['price_val'], currency)})")
    kb.add(f"Instagram 2FA I'D ({fmt_price(TASK_META['ig_2fa']['price_val'], currency)})")
    kb.add("⚙️ Set Admin Rate (Instagram)")
    kb.add("❌ বাতিল" if is_bn(chat_id) else "❌ Cancel")
    return kb

def facebook_task_keyboard(chat_id, currency="BDT"):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(f"PC Clone 6155x/56x/57x I'D ({fmt_price(TASK_META['fb_clone_6155x']['price_val'], currency)})")
    kb.add(f"PC Clone 6158x I'D ({fmt_price(TASK_META['fb_clone_6158x']['price_val'], currency)})")
    kb.add(f"PC Clone 1000x I'D ({fmt_price(TASK_META['fb_clone_1000x']['price_val'], currency)})")
    kb.add(f"Number 00 Friend 2FA I'D ({fmt_price(TASK_META['fb_num00_2fa']['price_val'], currency)})")
    kb.add(f"Hotmail 30+ Friend I'D ({fmt_price(TASK_META['fb_hotmail_30']['price_val'], currency)})")
    kb.add(f"2FA + Cookies ({fmt_price(TASK_META['fb_2fa_cookies']['price_val'], currency)})")
    kb.add(f"2FA + Cookies 30+ ({fmt_price(TASK_META['fb_2fa_cookies_30']['price_val'], currency)})")
    kb.add("⚙️ Set Admin Rate (Facebook)")
    kb.add("❌ বাতিল" if is_bn(chat_id) else "❌ Cancel")
    return kb

def gmail_task_keyboard(chat_id, currency="BDT"):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(f"Fresh Gmail ({fmt_price(TASK_META['gmail_fresh']['price_val'], currency)})")
    kb.add(f"Gmail Cookies ({fmt_price(TASK_META['gmail_cookies']['price_val'], currency)})")
    kb.add("⚙️ Set Admin Rate (Gmail)")
    kb.add("❌ বাতিল" if is_bn(chat_id) else "❌ Cancel")
    return kb

def submit_cancel_keyboard(chat_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(
        "📤 ফাইল সাবমিট" if is_bn(chat_id) else "📤 Submit File",
        "❌ বাতিল" if is_bn(chat_id) else "❌ Cancel"
    )
    return kb


    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📜 Rules" if not is_bn(chat_id) else "📜 নিয়মাবলী",
                                   callback_data="ref_rules"),
        types.InlineKeyboardButton("🏆 Team Leaderboard" if not is_bn(chat_id) else "🏆 টিম লিডারবোর্ড",
                                   callback_data="ref_leaderboard"),
    )
    return kb

def team_leaderboard_findme_inline(chat_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "🔍 Find my place" if not is_bn(chat_id) else "🔍 আমার অবস্থান",
        callback_data="team_findme"
    ))
    return kb

def top_leaderboard_findme_inline(chat_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "🔍 Find my place" if not is_bn(chat_id) else "🔍 আমার অবস্থান",
        callback_data="top_findme"
    ))
    return kb

# ═══════════════════════════════════════════════════════════
# HELPERS
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
# ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════
@bot.message_handler(commands=["admin"])
def handle_admin_help(message):
    if message.chat.id != ADMIN_ID:
        return
    bot.send_message(ADMIN_ID,
        "🔧 *Admin Control Panel*\n"
        f"{divider()}\n"
        "✅ `/approve [ID] [Total_Accounts] [comment]`\n"
        "❌ `/reject [ID]`\n"
        "💸 `/pay [Withdraw_ID]` or `/pay [Withdraw_ID] [txn_hash]`\n"
        "📋 `/pending` — pending submissions\n"
        "💰 `/cashouts` — pending cashouts\n"
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
        f"👥 Total Users: {tu}\n"
        f"🚫 Banned: {ban}\n{divider()}\n"
        f"📋 Pending Subs: {ps}\n"
        f"✅ Approved: {ap}\n"
        f"❌ Rejected: {rj}\n{divider()}\n"
        f"💰 Pending Cashouts: {pw}\n"
        f"✅ Paid Cashouts: {paid}\n"
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
            f"  🆔 `{row['chat_id']}` | 💰 {earned} | 📅 {row['joined_at']}"
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
    lines = [f"📋 *Pending Submissions ({len(rows)})*", divider()]
    for row in rows:
        uname = f"@{row['username']}" if row["username"] else row["first_name"]
        lines.append(
            f"🆔 #{row['id']} | {row['platform']} — {row['task_type']}\n"
            f"  👤 {uname} (`{row['chat_id']}`) | {row['submitted_at'][:16]}\n"
            f"  `/approve {row['id']} [n] [comment]` | `/reject {row['id']}`"
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
    lines = [f"💰 *Pending Cashouts ({len(rows)})*", divider()]
    for row in rows:
        uname = f"@{row['username']}" if row["username"] else row["first_name"]
        amt   = f"{row['net_amount']:.4f} {row['currency']}"
        lines.append(
            f"🆔 #{row['id']} | {row['method']} — {amt}\n"
            f"  📲 {row['address']}\n"
            f"  👤 {uname} | {row['requested_at'][:16]}\n"
            f"  `/pay {row['id']}` or `/pay {row['id']} [txn_hash]`"
        )
    bot.send_message(ADMIN_ID, "\n".join(lines))

@bot.message_handler(commands=["approve"])
def handle_approve(message):
    if message.chat.id != ADMIN_ID:
        return
    args = message.text.split(None, 3)
    if len(args) < 3:
        bot.send_message(ADMIN_ID, "⚠️ Format: `/approve [Submission_ID] [Total_Accounts] [optional comment]`")
        return
    try:
        sub_id      = int(args[1])
        total_items = int(args[2])
        comment     = args[3] if len(args) > 3 else ""
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

            # Referral commission
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
                    comm_str     = fmt_amount(comm_usd, ref_currency)
                    earn_str     = fmt_amount(total_payout_usd, ref_currency)
                    bot.send_message(referrer_id,
                        t(referrer_id,
                          f"🎉 *রেফারেল কমিশন পেয়েছেন!*\n{divider()}\n💼 আপনার রেফারেল {earn_str} আয় করেছে!\n💰 আপনি পেয়েছেন: *{comm_str}* (১৫%)",
                          f"🎉 *Referral Commission Received!*\n{divider()}\n💼 Your referral earned {earn_str}!\n💰 You received: *{comm_str}* (15%)")
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
        if user_currency == "BDT":
            payout_str = f"+৳{total_payout_bdt:.2f}"
        else:
            payout_str = f"+${total_payout_usd:.4f}"

        comment_line = f"\n✉️ Comment: {comment}" if comment else ""

        bot.send_message(sub["chat_id"],
            t(sub["chat_id"],
              f"✅ রিপোর্ট অনুমোদিত, {payout_str}{comment_line}",
              f"✅ Report approved, {payout_str}{comment_line}")
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

    bot.send_message(ADMIN_ID, f"❌ Submission #{sub_id} rejected.")
    try:
        bot.send_message(sub["chat_id"],
            t(sub["chat_id"],
              f"❌ *টাস্ক বাতিল!*\n{divider()}\n🆔 সাবমিশন: #{sub_id}\n⚠️ ফাইলে সমস্যা থাকায় বাতিল হয়েছে।",
              f"❌ *Task Rejected!*\n{divider()}\n🆔 Submission: #{sub_id}\n⚠️ Your submission was rejected due to file issues.")
        )
    except Exception:
        pass

@bot.message_handler(commands=["pay"])
def handle_pay_withdraw(message):
    if message.chat.id != ADMIN_ID:
        return
    args = message.text.split(None, 2)
    if len(args) < 2:
        bot.send_message(ADMIN_ID, "⚠️ Format: `/pay [Withdraw_ID]` or `/pay [Withdraw_ID] [txn_hash]`")
        return
    try:
        wid = int(args[1])
    except ValueError:
        return
    txn_hash = args[2].strip() if len(args) > 2 else ""

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
        curr       = withd["currency"]
        net        = withd["net_amount"]
        method     = withd["method"]
        address    = withd["address"]

        if curr == "USDT":
            amt_str = f"${net:.4f}"
            if txn_hash:
                pay_msg = (
                    f"✅ Withdrawal successful!\n"
                    f"Amount: {amt_str}\n"
                    f"Transaction: {txn_hash}"
                )
            else:
                pay_msg = (
                    f"✅ Withdrawal successful!\n"
                    f"Amount: {amt_str}"
                )
        else:
            amt_str = f"{net:.2f} Tk"
            pay_msg = (
                f"✅ Withdrawal successful!\n"
                f"Amount: {amt_str}\n"
                f"{method}: {address}"
            )

        bot.send_message(withd["chat_id"], pay_msg)
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
        bot.send_message(ADMIN_ID, f"❌ Submission #{sub_id} rejected.")
        try:
            bot.send_message(sub["chat_id"],
                t(sub["chat_id"],
                  f"❌ *টাস্ক বাতিল!*\n{divider()}\n🆔 সাবমিশন: #{sub_id}\n⚠️ ফাইলে সমস্যা। সাপোর্টে যোগাযোগ করুন।",
                  f"❌ *Task Rejected!*\n{divider()}\n🆔 Submission: #{sub_id}\nRejected due to file issues.")
            )
        except Exception:
            pass
    else:
        bot.edit_message_reply_markup(ADMIN_ID, call.message.message_id, reply_markup=None)
        bot.send_message(ADMIN_ID,
            f"✅ Approving #{sub_id} — কতটি account accept করবেন?\n"
            f"Reply করুন: `/approve {sub_id} [count] [optional comment]`"
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
# /start
# ═══════════════════════════════════════════════════════════
@bot.message_handler(commands=["start"])
def handle_start(message):
    chat_id    = message.chat.id
    args       = message.text.split()
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
# MENU HANDLERS
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
    bot.send_message(cid,
        t(cid, "যেকোনো একটি কাজ সিলেক্ট করুন:", "Select a task:"),
        reply_markup=platform_select_keyboard(cid)
    )

@bot.message_handler(func=lambda m: m.text == "Instagram")
def handle_platform_instagram(message):
    if check_banned(message.chat.id):
        return
    cid = message.chat.id
    currency = get_currency(cid)
    bot.send_message(cid,
        t(cid, "Instagram টাস্ক সিলেক্ট করুন:", "Select Instagram task:"),
        reply_markup=instagram_task_keyboard(cid, currency)
    )

@bot.message_handler(func=lambda m: m.text == "Facebook")
def handle_platform_facebook(message):
    if check_banned(message.chat.id):
        return
    cid = message.chat.id
    currency = get_currency(cid)
    bot.send_message(cid,
        t(cid, "Facebook টাস্ক সিলেক্ট করুন:", "Select Facebook task:"),
        reply_markup=facebook_task_keyboard(cid, currency)
    )

@bot.message_handler(func=lambda m: m.text == "Gmail")
def handle_platform_gmail(message):
    if check_banned(message.chat.id):
        return
    cid = message.chat.id
    currency = get_currency(cid)
    bot.send_message(cid,
        t(cid, "Gmail টাস্ক সিলেক্ট করুন:", "Select Gmail task:"),
        reply_markup=gmail_task_keyboard(cid, currency)
    )

@bot.message_handler(func=lambda m: m.text in ("📤 Withdraw", "📤 উত্তোলন"))
def handle_withdraw_menu(message):
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid      = message.chat.id
    currency = get_currency(cid)

    last_time = get_last_withdrawal_time(cid)
    if last_time:
        last_dt = datetime.strptime(last_time[:19], "%Y-%m-%d %H:%M:%S")
        elapsed = datetime.now() - last_dt
        if elapsed < timedelta(hours=WITHDRAW_COOLDOWN):
            remaining = timedelta(hours=WITHDRAW_COOLDOWN) - elapsed
            h, rem    = divmod(int(remaining.total_seconds()), 3600)
            mn        = rem // 60
            bot.send_message(cid,
                t(cid,
                  f"⏳ পরবর্তী উত্তোলনের জন্য আরো *{h}h {mn}m* অপেক্ষা করুন।",
                  f"⏳ Please wait *{h}h {mn}m* more before next withdrawal.")
            )
            return

    set_user_state(cid, STATE_IDLE)
    bot.send_message(cid,
        t(cid, "📤 উত্তোলন পদ্ধতি বেছে নিন:", "📤 Choose Withdraw Method:"),
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
        f"🆔 ID: `{cid}`"
    )

@bot.message_handler(func=lambda m: m.text in ("📞 Support", "📞 সাপোর্ট"))
def handle_support(message):
    ensure_user(message)
    cid = message.chat.id
    if is_bn(cid):
        txt = (
            "╔════════════════╗\n"
            "║   📞 সাপোর্ট    ║\n"
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
            "║    📞 SUPPORT   ║\n"
            "╚════════════════╝\n\n"
            "🟢 Admin Online\n"
            f"{divider()}\n"
            "❓ Any problem?\n"
            "⚡ Get instant solution!\n\n"
            "📲 @nolab420"
        )
    label = "📲 যোগাযোগ করুন →" if is_bn(cid) else "📲 Contact Now →"
    bot.send_message(cid, txt, reply_markup=admin_contact_inline(label))

@bot.message_handler(func=lambda m: m.text in ("👥 Referrals", "👥 রেফারেল"))
def handle_my_referrals(message):
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid        = message.chat.id
    user       = get_user(cid)
    currency   = get_currency(cid)
    ref_count  = get_referral_count(cid)
    new_24h    = get_new_referrals_24h(cid)
    earned_30d = get_referral_earned_30d(cid)
    earned_24h = get_referral_earned_24h(cid)
    code       = user["referral_code"] if user else f"REF{cid}"
    link       = f"https://t.me/{BOT_USERNAME}?start={code}"
    earn_30d_str = fmt_amount(earned_30d, currency)
    earn_24h_str = fmt_amount(earned_24h, currency)

    if is_bn(cid):
        bot.send_message(cid,
            f"👥 মোট রেফারেল: {ref_count}\n"
            f"🆕 গত ২৪ ঘণ্টায় নতুন: {new_24h}\n\n"
            f"💰 গত ৩০ দিনে আয়: {earn_30d_str}\n"
            f"📊 গত ২৪ ঘণ্টায় আয়: {earn_24h_str}\n\n"
            f"🔗 আপনার রেফারেল লিংক:\n{link}\n\n"
            f"ℹ️ আপনি প্রতিটি রেফারেলের আয়ের ১৫% পাবেন।",
            reply_markup=referral_menu_inline(cid)
        )
    else:
        bot.send_message(cid,
            f"👥 Total referrals: {ref_count}\n"
            f"🆕 New in last 24 hours: {new_24h}\n\n"
            f"💰 Earned in last 30 days: {earn_30d_str}\n"
            f"📊 Earned in last 24 hours: {earn_24h_str}\n\n"
            f"🔗 Your referral link:\n{link}\n\n"
            f"ℹ️ You receive 15% of each referral's earnings.",
            reply_markup=referral_menu_inline(cid)
        )

@bot.callback_query_handler(func=lambda c: c.data == "ref_rules")
def handle_ref_rules(call):
    bot.answer_callback_query(call.id)
    cid = call.message.chat.id
    if is_bn(cid):
        txt = (
            "📜 *নিয়মাবলী*\n"
            f"{divider()}\n"
            "১. শুধুমাত্র .xlsx ফাইল গ্রহণযোগ্য\n"
            "২. প্রতি সাবমিশনে সর্বনিম্ন ১৫০০ রো থাকতে হবে\n"
            "৩. ডুপ্লিকেট একাউন্ট গ্রহণযোগ্য নয়\n"
            "৪. ভুয়া/অবৈধ একাউন্ট বাতিল হবে\n"
            "৫. উত্তোলনের মধ্যে ২৪ ঘণ্টা বিরতি\n"
            "৬. রেফারেল কমিশন: ১৫%"
        )
    else:
        txt = (
            "📜 *Rules*\n"
            f"{divider()}\n"
            "1. Only .xlsx files accepted\n"
            "2. Minimum 1500 rows per submission\n"
            "3. No duplicate accounts\n"
            "4. Fake/invalid accounts will be rejected\n"
            "5. Withdrawal cooldown: 24 hours\n"
            "6. Referral commission: 15%"
        )
    bot.send_message(cid, txt)

@bot.callback_query_handler(func=lambda c: c.data == "ref_leaderboard")
def handle_ref_leaderboard(call):
    bot.answer_callback_query(call.id)
    cid  = call.message.chat.id
    rows = get_team_leaderboard(cid)
    if not rows:
        bot.send_message(cid,
            t(cid,
              "আপনার টিমে এখনো কোনো সদস্য নেই।",
              "Your team has no members yet.")
        )
        return
    lines = [f"🏆 *{'আপনার টিম লিডারবোর্ড' if is_bn(cid) else 'Your Team Leaderboard'}*\n{divider()}"]
    for i, row in enumerate(rows):
        rank    = i + 1
        uid_str = mask_id(row["chat_id"])
        lines.append(f"{rank}. {uid_str} — {row['completed']} {'সাবমিশন' if is_bn(cid) else 'submissions'} | {fmt_amount(row['total_earned'], get_currency(cid))}")
    bot.send_message(cid, "\n".join(lines), reply_markup=team_leaderboard_findme_inline(cid))

@bot.callback_query_handler(func=lambda c: c.data == "team_findme")
def handle_team_findme(call):
    bot.answer_callback_query(call.id)
    cid            = call.message.chat.id
    user           = get_user(cid)
    referrer_id    = user["referred_by"] if user else None
    if not referrer_id:
        bot.send_message(cid, t(cid, "আপনি কোনো টিমে নেই।", "You are not in any team."))
        return
    rank, completed = get_team_user_rank(referrer_id, cid)
    if rank:
        bot.send_message(cid,
            t(cid,
              f"📊 আপনার অবস্থান: #{rank}\n✅ সম্পন্ন: {completed} সাবমিশন",
              f"📊 Your position: #{rank}\n✅ Completed: {completed} submissions")
        )
    else:
        bot.send_message(cid, t(cid, "আপনি এখনো লিডারবোর্ডে নেই।", "You are not on the leaderboard yet."))

# Overall Top Leaderboard
@bot.message_handler(func=lambda m: m.text in ("🏆 Top", "🏆 টপ"))
def handle_top_leaderboard(message):
    if check_banned(message.chat.id):
        return
    cid  = message.chat.id
    rows = get_daily_top10()

    prize_map = {1: "$4", 2: "$2", 3: "$1"}

    lines = [
        f"🏆 *{'শীর্ষ ১০ জন (আজকের)' if is_bn(cid) else 'Top 10 users per day'}*\n",
        f"ℹ️ {'ফলাফল প্রতিদিন ০১:০০ (Helsinki) তে ঘোষণা করা হয়। শীর্ষরা তাদের ব্যালেন্সে পুরস্কার পান!' if is_bn(cid) else 'Results are announced every day at 01:00 (Helsinki). Leaders receive real money to their balance!'}\n",
        f"🔄 {'পরিসংখ্যান প্রতি ১০ মিনিটে আপডেট হয়।' if is_bn(cid) else 'Statistics update every 10 minutes.'}\n",
        f"{divider()}"
    ]

    if not rows:
        lines.append(t(cid, "এখনো কোনো ডেটা নেই।", "No data yet."))
    else:
        for i, row in enumerate(rows):
            rank      = i + 1
            uid_str   = mask_id(row["chat_id"])
            prize_str = prize_map.get(rank, "$0.5")
            lines.append(f"{rank}. {uid_str} — {row['completed']} {'executions' if not is_bn(cid) else 'সাবমিশন'} | {prize_str}")

    bot.send_message(cid, "\n".join(lines), reply_markup=top_leaderboard_findme_inline(cid))

@bot.callback_query_handler(func=lambda c: c.data == "top_findme")
def handle_top_findme(call):
    bot.answer_callback_query(call.id)
    cid            = call.message.chat.id
    rank, completed = get_user_daily_rank(cid)
    if rank:
        bot.send_message(cid,
            t(cid,
              f"📊 আপনার অবস্থান: #{rank}\n✅ সম্পন্ন: {completed} সাবমিশন",
              f"📊 Your position: #{rank}\n✅ Completed: {completed} submissions")
        )
    else:
        bot.send_message(cid, t(cid, "আপনি এখনো লিডারবোর্ডে নেই।", "You are not on the leaderboard yet."))

@bot.message_handler(func=lambda m: m.text in ("📁 Submissions", "📁 সাবমিশন"))
def handle_my_submissions(message):
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid  = message.chat.id
    rows = get_user_submissions(cid, limit=10)
    if not rows:
        bot.send_message(cid, t(cid,
            "📁 আপনার কোনো সাবমিশন নেই।",
            "📁 You have no submissions yet."))
        return
    icons = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    title = f"📁 *{t(cid, 'সর্বশেষ', 'Last')} {len(rows)} {t(cid, 'সাবমিশন', 'Submissions')}*"
    lines = [title, divider()]
    for row in rows:
        lines.append(
            f"#{row['id']} {icons.get(row['status'], '❓')} "
            f"{row['platform']} — {row['task_type']}\n"
            f"  {row['submitted_at'][:16]}"
        )
    bot.send_message(cid, "\n".join(lines))

@bot.message_handler(func=lambda m: m.text in ("💲 Currency", "💲 কারেন্সি"))
def handle_currency_menu(message):
    ensure_user(message)
    cid = message.chat.id
    txt = t(cid, "💲 কারেন্সি বেছে নিন:", "💲 Choose your display currency:")
    bot.send_message(cid, txt, reply_markup=currency_select_inline())

@bot.callback_query_handler(func=lambda c: c.data.startswith("currency_"))
def handle_currency_select(call):
    currency = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    cid  = call.message.chat.id
    set_user_currency(cid, currency)
    name = "BDT (Tk)" if currency == "BDT" else "USDT"
    txt  = t(cid, f"✅ কারেন্সি পরিবর্তন: *{name}*", f"✅ Currency set to *{name}*")
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
    cid  = call.message.chat.id
    set_user_lang(cid, lang)
    txt  = "✅ ভাষা বাংলায় পরিবর্তন করা হয়েছে।" if lang == "Bangla" else "✅ Language changed to English."
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
# INLINE CALLBACKS — Tasks
# ═══════════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: c.data.startswith("adminrate_"))
def handle_admin_rate(call):
    bot.answer_callback_query(call.id)
    cid      = call.message.chat.id
    platform = call.data.split("_", 1)[1].capitalize()
    bot.send_message(cid,
        t(cid,
          f"কাস্টম {platform} রেট সেট করতে এডমিনের সাথে যোগাযোগ করুন:",
          f"Contact admin to set a custom {platform} rate:"),
        reply_markup=admin_contact_inline("📲 @nolab420")
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
        f"💰 {t(cid, 'রিওয়ার্ড', 'Reward')}: {price_display}/piece\n"
        f"⏱ {t(cid, 'রিভিউ সময়', 'Review Time')}: {meta['review_min']} minutes\n{divider()}\n"
        f"📝 {t(cid, 'নির্দেশনা', 'Instructions')}:\n{meta['description']}\n{divider()}\n"
        f"📊 {t(cid, 'প্রয়োজনীয় কলাম', 'Required Columns')}:\n`{meta['columns']}`",
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
    meta     = TASK_META.get(task_key)
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
        f"📅 {t(cid, 'তারিখ', 'Date')}: {today_str()}\n"
        f"📋 {t(cid, 'টাস্ক', 'Task')}: {meta['label']}\n"
        f"💰 {t(cid, 'রিওয়ার্ড', 'Reward')}: {price_display}/piece\n{divider()}\n"
        f"📊 {t(cid, 'প্রয়োজনীয় কলাম', 'Required Columns')}:\n`{meta['columns']}`\n{divider()}\n"
        + t(cid,
            "আপনার *.xlsx* ফাইলটি আপলোড করুন।\nবাতিল করতে ❌ বাতিল চাপুন।",
            "Upload your *.xlsx* file.\nPress ❌ Cancel to abort."),
        reply_markup=cancel_keyboard(cid)
    )

# ═══════════════════════════════════════════════════════════
# REPLY KEYBOARD TASK HANDLERS
# ═══════════════════════════════════════════════════════════
# Map reply keyboard button text to task keys
REPLY_TASK_MAP = {
    "ig_cookies": "Instagram Cookies I'D",
    "ig_2fa": "Instagram 2FA I'D",
    "fb_clone_6155x": "PC Clone 6155x/56x/57x I'D",
    "fb_clone_6158x": "PC Clone 6158x I'D",
    "fb_clone_1000x": "PC Clone 1000x I'D",
    "fb_num00_2fa": "Number 00 Friend 2FA I'D",
    "fb_hotmail_30": "Hotmail 30+ Friend I'D",
    "fb_2fa_cookies": "2FA + Cookies",
    "fb_2fa_cookies_30": "2FA + Cookies 30+",
    "gmail_fresh": "Fresh Gmail",
    "gmail_cookies": "Gmail Cookies",
}

def _strip_price(text):
    """Remove price suffix like ' (3.10 Tk)' or ' ($0.0258 USDT)' from button text."""
    import re
    return re.sub(r'\s*\(.*?\)\s*$', '', text).strip()

def _find_task_key_by_label(label):
    for key, base_label in REPLY_TASK_MAP.items():
        if label == base_label:
            return key
    return None

@bot.message_handler(func=lambda m: _find_task_key_by_label(_strip_price(m.text)) is not None)
def handle_reply_task_select(message):
    if check_banned(message.chat.id):
        return
    cid = message.chat.id
    label = _strip_price(message.text)
    task_key = _find_task_key_by_label(label)
    if not task_key:
        return
    meta = TASK_META.get(task_key)
    if not meta:
        return
    currency = get_currency(cid)
    price_display = fmt_price(meta["price_val"], currency)

    if has_pending_submission(cid, meta["label"]):
        bot.send_message(cid,
            t(cid,
              f"⚠️ *{meta['label']}* এর একটি সাবমিশন পেন্ডিং আছে। এডমিন রিভিউর পর আবার সাবমিট করুন।",
              f"⚠️ A submission for *{meta['label']}* is still pending. Wait for admin review.")
        )
        return

    if is_bn(cid):
        info_text = (
            f"📋 *{meta['label']}*\n{divider()}\n"
            f"💰 পুরস্কার: {price_display}/piece\n"
            f"⏰ রিভিউ সময়: {meta['review_min']} মিনিট\n{divider()}\n"
            f"📝 নির্দেশনা:\n{meta.get('description_bn', meta['description'])}\n{divider()}\n"
            f"📊 প্রয়োজনীয় কলাম:\n`{meta['columns']}`"
        )
    else:
        info_text = (
            f"📋 *{meta['label']}*\n{divider()}\n"
            f"💰 Reward: {price_display}/piece\n"
            f"⏰ Review Time: {meta['review_min']} minutes\n{divider()}\n"
            f"📝 Instructions:\n{meta['description']}\n{divider()}\n"
            f"📊 Required Columns:\n`{meta['columns']}`"
        )

    set_user_state(cid, STATE_WAITING_FILE, task_key=task_key)
    bot.send_message(cid, info_text, reply_markup=submit_cancel_keyboard(cid))

@bot.message_handler(func=lambda m: m.text in (
    "⚙️ Set Admin Rate (Instagram)",
    "⚙️ Set Admin Rate (Facebook)",
    "⚙️ Set Admin Rate (Gmail)",
))
def handle_reply_admin_rate(message):
    cid = message.chat.id
    if "Instagram" in message.text:
        platform = "Instagram"
    elif "Facebook" in message.text:
        platform = "Facebook"
    else:
        platform = "Gmail"
    bot.send_message(cid,
        t(cid,
          f"কাস্টম {platform} রেট সেট করতে এডমিনের সাথে যোগাযোগ করুন:",
          f"Contact admin to set a custom {platform} rate:"),
        reply_markup=admin_contact_inline("📲 @nolab420")
    )

@bot.message_handler(func=lambda m: m.text in ("📤 Submit File", "📤 ফাইল সাবমিট"))
def handle_submit_file_button(message):
    cid = message.chat.id
    userdata = get_user(cid)
    if not userdata or userdata["current_state"] != STATE_WAITING_FILE:
        bot.send_message(cid,
            t(cid, "⚠️ আগে একটি টাস্ক সিলেক্ট করুন।", "⚠️ Please select a task first."),
            reply_markup=main_menu_keyboard(cid)
        )
        return
    bot.send_message(cid,
        t(cid, "📎 এখন আপনার *.xlsx* ফাইলটি পাঠান।", "📎 Now send your *.xlsx* file."),
        reply_markup=submit_cancel_keyboard(cid)
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("withdraw_"))
def handle_withdraw_method(call):
    method_key = call.data[9:]
    bot.answer_callback_query(call.id)
    cid        = call.message.chat.id
    currency   = get_currency(cid)

    method_label = {
        "bkash":    "bKash",
        "nagad":    "Nagad",
        "usdt_bep": "USDT (BEP-20)",
    }.get(method_key)
    if not method_label:
        return

    if currency == "BDT":
        fee_str = f"{WITHDRAW_FEE_BDT:.2f} Tk"
        min_str = f"{MIN_WITHDRAW_BDT:.2f} Tk"
    else:
        fee_str = f"${WITHDRAW_FEE_USDT:.3f}"
        min_str = f"${MIN_WITHDRAW_USDT:.2f}"

    # Info message
    bot.send_message(cid,
        f"You selected {method_label}.\n"
        f"📉 Fee: {fee_str}\n"
        f"🔢 Minimum withdrawal amount: {min_str}"
    )

    # Address prompt
    if method_key == "usdt_bep":
        address_prompt = f"📥 Enter your USDT (BEP-20) address:"
    elif method_key == "bkash":
        address_prompt = f"📥 Enter your bKash number:"
    else:
        address_prompt = f"📥 Enter your Nagad number:"

    session_set(cid, {
        "withdraw_method":   method_key,
        "withdraw_label":    method_label,
        "withdraw_currency": currency,
        "withdraw_fee_str":  fee_str,
        "withdraw_min_str":  min_str,
    })
    set_user_state(cid, STATE_WAITING_ADDRESS)
    bot.send_message(cid, address_prompt, reply_markup=cancel_keyboard(cid))

# ═══════════════════════════════════════════════════════════
# STATE WORKFLOWS
# ═══════════════════════════════════════════════════════════
@bot.message_handler(
    func=lambda m: (
        m.chat.id == ADMIN_ID
        and check_state(m.chat.id, STATE_BROADCAST)
        and m.text not in ("❌ Cancel", "❌ বাতিল")
    )
)
def handle_broadcast_send(message):
    all_ids        = get_all_chat_ids()
    success, fail  = 0, 0
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

@bot.message_handler(func=lambda m: check_state(m.chat.id, STATE_WAITING_ADDRESS))
def handle_withdraw_address(message):
    cid     = message.chat.id
    address = message.text.strip()
    session = session_get(cid)

    if not session or "withdraw_method" not in session:
        bot.send_message(cid, t(cid, "⚠️ সেশন মেয়াদ শেষ। আবার চেষ্টা করুন।", "⚠️ Session expired. Try again."))
        send_main_menu(cid)
        return

    if len(address) < 5 or len(address) > 150:
        bot.send_message(cid,
            t(cid, "⚠️ সঠিক নম্বর বা ঠিকানা লিখুন (৫–১৫০ অক্ষর)।", "⚠️ Enter a valid address (5–150 chars)."),
            reply_markup=cancel_keyboard(cid))
        return

    session["withdraw_address"] = address
    session_set(cid, session)
    set_user_state(cid, STATE_WAITING_AMOUNT)

    method_label = session.get("withdraw_label", "Wallet")
    fee_str      = session.get("withdraw_fee_str", "")
    min_str      = session.get("withdraw_min_str", "")

    # Repeat info + ask amount
    bot.send_message(cid,
        f"You selected {method_label}.\n"
        f"📉 Fee: {fee_str}\n"
        f"🔢 Minimum withdrawal amount: {min_str}\n"
        f"Enter amount:",
        reply_markup=cancel_keyboard(cid)
    )

@bot.message_handler(func=lambda m: check_state(m.chat.id, STATE_WAITING_AMOUNT))
def handle_withdraw_amount(message):
    cid = message.chat.id
    try:
        amount = float(message.text.strip().replace(",", ""))
    except ValueError:
        bot.send_message(cid,
            t(cid, "⚠️ সঠিক সংখ্যা দিন।", "⚠️ Enter a valid number."),
            reply_markup=cancel_keyboard(cid))
        return

    session  = session_pop(cid)
    if not session or "withdraw_address" not in session:
        bot.send_message(cid, t(cid, "⚠️ সেশন মেয়াদ শেষ। আবার চেষ্টা করুন।", "⚠️ Session expired. Try again."))
        send_main_menu(cid)
        return

    currency = session.get("withdraw_currency", get_currency(cid))
    bal_usd  = get_balance_usd(cid)

    if currency == "BDT":
        current_val = bal_usd * DOLLAR_RATE
        min_val     = MIN_WITHDRAW_BDT
        fee_val     = WITHDRAW_FEE_BDT
    else:
        current_val = bal_usd
        min_val     = MIN_WITHDRAW_USDT
        fee_val     = WITHDRAW_FEE_USDT

    if amount < min_val or amount > current_val:
        bot.send_message(cid, "❌ Insufficient balance.")
        send_main_menu(cid, "👍 Action cancelled.")
        return

    label    = session["withdraw_label"]
    address  = session["withdraw_address"]
    net      = round(amount - fee_val, 6)
    wid      = save_withdrawal(cid, label, address, amount, currency)

    if currency == "BDT":
        confirm_msg = (
            f"✅ Withdrawal request created!\n\n"
            f"💳 Method: {label}\n"
            f"📲 Number: {address}\n"
            f"💵 Debit amount: {amount:.2f} Tk\n"
            f"📉 Fee: {fee_val:.2f} Tk\n"
            f"💰 You will receive: {net:.2f} Tk"
        )
    else:
        confirm_msg = (
            f"✅ Withdrawal request created!\n\n"
            f"💳 Method: {label}\n"
            f"👛 Wallet: {address}\n"
            f"💵 Debit amount: ${amount:.4f}\n"
            f"📉 Fee: ${fee_val:.4f}\n"
            f"💰 You will receive: ${net:.4f}"
        )

    bot.send_message(cid, confirm_msg)

    unit = "Tk" if currency == "BDT" else "USDT"
    notify_admin(
        f"💰 *New Withdrawal #{wid}*\n"
        f"👤 Chat ID: `{cid}`\n"
        f"💳 Method: {label} ({currency})\n"
        f"📲 Address: `{address}`\n"
        f"💵 Amount: {amount:.4f} {unit} | Net: {net:.4f} {unit}\n\n"
        f"Approve: `/pay {wid}` or `/pay {wid} [txn_hash]`"
    )
    send_main_menu(cid)

# ═══════════════════════════════════════════════════════════
# FILE HANDLER
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
    uname = f"@{user['username']}" if user and user["username"] else (user["first_name"] if user else str(cid))

    if ADMIN_ID:
        try:
            caption = (
                f"📋 *New Submission #{submission_id}*\n"
                f"👤 {uname} (`{cid}`)\n"
                f"🚀 {meta['platform']} — {meta['label']}\n"
                f"💰 {meta['price_val']:.2f} Tk/piece\n"
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
                f"📋 *New Submission #{submission_id}*\n"
                f"👤 {uname} (`{cid}`)\n"
                f"🚀 {meta['platform']} — {meta['label']} ({meta['price_val']:.2f} Tk)\n"
                f"📄 {file_name} ({file_size/1024:.1f} KB)\n\n"
                f"✅ `/approve {submission_id} [Total_Accounts] [comment]`\n"
                f"❌ `/reject {submission_id}`"
            )

    bot.send_message(cid,
        t(cid,
          f"✅ *ফাইল সাবমিট হয়েছে!*\n{divider()}\n🆔 সাবমিশন: #{submission_id}\n🚀 প্ল্যাটফর্ম: {meta['platform']}\n📋 টাস্ক: {meta['label']}",
          f"✅ *File Submitted!*\n{divider()}\n🆔 Submission: #{submission_id}\n🚀 Platform: {meta['platform']}\n📋 Task: {meta['label']}")
    )
    send_main_menu(cid)

# ═══════════════════════════════════════════════════════════
# CATCH-ALL
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
# ENTRY POINT
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    # Start daily prize scheduler in background
    prize_thread = threading.Thread(target=daily_prize_scheduler, daemon=True)
    prize_thread.start()
    logger.info("Bot starting — polling...")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=20)
        except Exception as e:
            logger.error("Polling crashed: %s", e)
            time.sleep(5)
            logger.info("Restarting polling...")
