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

ADMIN_URL    = os.environ.get("ADMIN_URL", "https://t.me/YourAdminUsername")
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "0"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "YourBotUsername")
DB_PATH      = os.environ.get("DB_PATH", "bot_database.db")

# Rates & Limits
DOLLAR_RATE       = 120.0   # 1 USD = 120 BDT
MIN_WITHDRAW_BDT  = 20.00   # Tk
MIN_WITHDRAW_USDT = 0.20    # USDT
WITHDRAW_FEE_BDT  = 2.50    # Tk
WITHDRAW_FEE_USDT = 0.01    # USDT
REFERRAL_COMM_PCT = 0.20    # 20% lifetime commission
WITHDRAW_COOLDOWN = 24      # Hours between withdrawals per user

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

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
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


def _migrate_db() -> None:
    """Add new columns to existing databases without breaking old data."""
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


def ensure_user(message: types.Message, referred_by: int = None) -> None:
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


def get_user(chat_id: int):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()


def is_banned(chat_id: int) -> bool:
    row = get_user(chat_id)
    return bool(row and row["is_banned"])


def get_lang(chat_id: int) -> str:
    row = get_user(chat_id)
    return row["language"] if row else "English"


def get_currency(chat_id: int) -> str:
    row = get_user(chat_id)
    return row["currency"] if row else "BDT"


def is_bn(chat_id: int) -> bool:
    return get_lang(chat_id) == "Bangla"


def set_user_state(chat_id: int, state: str, task_key: str = None) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET current_state=?, current_task_key=? WHERE chat_id=?",
            (state, task_key, chat_id),
        )
        conn.commit()


def set_user_lang(chat_id: int, lang: str) -> None:
    with get_db() as conn:
        conn.execute("UPDATE users SET language=? WHERE chat_id=?", (lang, chat_id))
        conn.commit()


def set_user_currency(chat_id: int, currency: str) -> None:
    with get_db() as conn:
        conn.execute("UPDATE users SET currency=? WHERE chat_id=?", (currency, chat_id))
        conn.commit()


def get_balance_usd(chat_id: int) -> float:
    row = get_user(chat_id)
    return round(row["balance"], 6) if row else 0.0


def fmt_amount(amount_usd: float, currency: str) -> str:
    """Format a USD balance amount for display in user's currency."""
    if currency == "BDT":
        return f"{amount_usd * DOLLAR_RATE:.2f} Tk"
    return f"${amount_usd:.4f} USDT"


def fmt_price(price_bdt: float, currency: str) -> str:
    """
    Format a task price (stored in BDT) for display in user's currency.
    BDT → show as Tk
    USDT → convert BDT price to USDT and show as $
    """
    if currency == "BDT":
        return f"{price_bdt:.2f} Tk"
    usdt_price = price_bdt / DOLLAR_RATE
    return f"${usdt_price:.4f} USDT"


def get_total_users() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]


def get_top_earners(limit: int = 10):
    with get_db() as conn:
        return conn.execute(
            "SELECT first_name, total_earned FROM users ORDER BY total_earned DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_referral_count(chat_id: int) -> int:
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) as c FROM users WHERE referred_by=?", (chat_id,)
        ).fetchone()["c"]


def get_user_submissions(chat_id: int, limit: int = 10):
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


def save_submission(chat_id: int, platform: str, task_type: str, file_id: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO submissions (chat_id, platform, task_type, file_id) VALUES (?,?,?,?)",
            (chat_id, platform, task_type, file_id),
        )
        conn.commit()
        return cur.lastrowid


def has_pending_submission(chat_id: int, task_type: str) -> bool:
    with get_db() as conn:
        return bool(conn.execute(
            "SELECT id FROM submissions WHERE chat_id=? AND task_type=? AND status='pending' LIMIT 1",
            (chat_id, task_type),
        ).fetchone())


def get_last_withdrawal_time(chat_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT requested_at FROM withdrawals WHERE chat_id=? ORDER BY requested_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return row["requested_at"] if row else None


def save_withdrawal(chat_id: int, method: str, address: str,
                    amount: float, currency: str = "BDT") -> int:
    fee = WITHDRAW_FEE_BDT if currency == "BDT" else WITHDRAW_FEE_USDT
    net = round(amount - fee, 6)
    # balance is stored in USD; deduct proportionally
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
#  BOT INSTANCE & META
# ═══════════════════════════════════════════════════════════
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
withdraw_session_cache: dict[int, dict] = {}

TASK_META = {
    # ── Instagram ───────────────────────────────────────────
    "ig_cookies": {
        "platform": "Instagram",
        "label": "Instagram Cookies I'D",
        "price": "4.00 Tk/piece",
        "price_val": 4.00,          # stored in BDT
        "review_min": 45,
        "description": "Submit Instagram accounts generated strictly with accurate session cookies.",
        "columns": "A = Username   |   B = Password  (Only 2 Columns)",
    },
    "ig_2fa": {
        "platform": "Instagram",
        "label": "Instagram 2FA I'D",
        "price": "2.50 Tk/piece",
        "price_val": 2.50,          # stored in BDT
        "review_min": 45,
        "description": "Submit Instagram accounts with 2FA key enabled. Ensure all three columns are filled correctly.",
        "columns": "A = Username   |   B = Password   |   C = 2FA Key",
    },
    # ── Facebook ────────────────────────────────────────────
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
    # ── Gmail ───────────────────────────────────────────────
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
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════

def main_menu_keyboard(chat_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if is_bn(chat_id):
        kb.row("💰 ব্যালেন্স",      "📋 কাজ")
        kb.row("📥 উত্তোলন",       "👤 প্রোফাইল")
        kb.row("🏆 শীর্ষ",         "👥 আমার রেফারেল")
        kb.row("📂 আমার সাবমিশন")
        kb.row("💱 কারেন্সি",      "🌐 ভাষা")
        kb.row("📞 সাপোর্ট")
    else:
        kb.row("💰 Balance",       "📋 Tasks")
        kb.row("📥 Withdraw",      "👤 Profile")
        kb.row("🏆 Top",           "👥 My Referrals")
        kb.row("📂 My Submissions")
        kb.row("💱 Currency",      "🌐 Language")
        kb.row("📞 Support")
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


def _price_label(task_key: str, currency: str) -> str:
    """Return a price label like '4.00 Tk' or '$0.0333 USDT' based on user currency."""
    meta = TASK_META.get(task_key)
    if not meta:
        return ""
    return fmt_price(meta["price_val"], currency)


def instagram_type_inline(currency: str = "BDT") -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔑 Set Admin Rate",
                                   callback_data="adminrate_instagram"),
        types.InlineKeyboardButton(
            f"Instagram Cookies I'D 🔥 ({_price_label('ig_cookies', currency)})",
            callback_data="taskinfo_ig_cookies"),
        types.InlineKeyboardButton(
            f"📸 Instagram 2FA I'D 🔥 ({_price_label('ig_2fa', currency)})",
            callback_data="taskinfo_ig_2fa"),
    )
    return kb


def facebook_type_inline(currency: str = "BDT") -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔑 Set Admin Rate",
                                   callback_data="adminrate_facebook"),
        types.InlineKeyboardButton(
            f"PC Clone 6158x I'D 🔥 ({_price_label('fb_clone_6158x', currency)})",
            callback_data="taskinfo_fb_clone_6158x"),
        types.InlineKeyboardButton(
            f"PC Clone 1000x I'D 🔥 ({_price_label('fb_clone_1000x', currency)})",
            callback_data="taskinfo_fb_clone_1000x"),
    )
    return kb


def gmail_type_inline(currency: str = "BDT") -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔑 Set Admin Rate",
                                   callback_data="adminrate_gmail"),
        types.InlineKeyboardButton(
            f"Fresh Gmail 🔥 ({_price_label('gmail_fresh', currency)})",
            callback_data="taskinfo_gmail_fresh"),
    )
    return kb


def task_action_inline(task_key: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📤 Submit File", callback_data=f"task_{task_key}"),
        types.InlineKeyboardButton("❌ Cancel",      callback_data="task_cancel"),
    )
    return kb


def withdraw_method_inline(currency: str = "BDT") -> types.InlineKeyboardMarkup:
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


def currency_select_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🇧🇩 BDT (Tk)", callback_data="currency_BDT"),
        types.InlineKeyboardButton("💎 USDT",       callback_data="currency_USDT"),
    )
    return kb


def language_select_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🇧🇩 বাংলা",   callback_data="lang_bn"),
        types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
    )
    return kb


def admin_contact_inline(label: str = "👤 Admin") -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(label, url=ADMIN_URL))
    return kb

# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════

def send_main_menu(chat_id: int, text: str = "🏠") -> None:
    set_user_state(chat_id, STATE_IDLE)
    withdraw_session_cache.pop(chat_id, None)
    bot.send_message(chat_id, text, reply_markup=main_menu_keyboard(chat_id))


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def notify_admin(text: str) -> None:
    if ADMIN_ID:
        try:
            bot.send_message(ADMIN_ID, text, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Admin notify failed: %s", e)


def divider() -> str:
    return "─" * 28


def check_banned(chat_id: int) -> bool:
    if is_banned(chat_id):
        bot.send_message(chat_id, "🚫 আপনার একাউন্ট ব্যান করা হয়েছে। Support এ যোগাযোগ করুন।")
        return True
    return False

# ═══════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════

@bot.message_handler(commands=["admin"])
def handle_admin_help(message: types.Message) -> None:
    if message.chat.id != ADMIN_ID:
        return
    bot.send_message(ADMIN_ID,
        "👑 **Admin Control Panel**\n"
        f"{divider()}\n"
        "🟢 `/approve [ID] [Total_Accounts]`\n"
        "🔴 `/reject [ID]`\n"
        "💵 `/pay [Withdraw_ID]`\n"
        "📋 `/pending` — pending submissions\n"
        "💸 `/cashouts` — pending cashouts\n"
        "🚫 `/ban [chat_id]`\n"
        "✅ `/unban [chat_id]`\n"
        "📢 `/broadcast`\n"
        "📊 `/stats`"
    )


@bot.message_handler(commands=["stats"])
def handle_admin_stats(message: types.Message) -> None:
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
        f"📊 **Bot Statistics**\n{divider()}\n"
        f"👥 Total Users:      {tu}\n"
        f"🚫 Banned:           {ban}\n{divider()}\n"
        f"📁 Pending Subs:     {ps}\n"
        f"✅ Approved:         {ap}\n"
        f"❌ Rejected:         {rj}\n{divider()}\n"
        f"💸 Pending Cashouts: {pw}\n"
        f"✅ Paid Cashouts:    {paid}\n"
        f"💵 Total Paid (BDT): {tp:.2f} Tk"
    )


@bot.message_handler(commands=["pending"])
def handle_admin_pending(message: types.Message) -> None:
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
    lines = [f"📁 **Pending Submissions ({len(rows)})**", divider()]
    for row in rows:
        uname = f"@{row['username']}" if row["username"] else row["first_name"]
        lines.append(
            f"🆔 #{row['id']} | {row['platform']} — {row['task_type']}\n"
            f"   👤 {uname} | {row['submitted_at'][:16]}\n"
            f"   `/approve {row['id']} [n]`  |  `/reject {row['id']}`"
        )
    bot.send_message(ADMIN_ID, "\n".join(lines))


@bot.message_handler(commands=["cashouts"])
def handle_admin_cashouts(message: types.Message) -> None:
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
    lines = [f"💸 **Pending Cashouts ({len(rows)})**", divider()]
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
def handle_approve(message: types.Message) -> None:
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

        # 20% Referral Commission
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
                    f"🎁 **Referral Commission!**\n"
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
        bot.send_message(sub["chat_id"],
            f"🎉 **Task Approved!**\n{divider()}\n"
            f"🆔 Submission ID:  #{sub_id}\n"
            f"📋 Task:           {sub['task_type']}\n"
            f"✅ Accepted:       {total_items} accounts\n"
            f"💰 Credited:       *{total_payout_bdt:.2f} Tk*\n{divider()}\n"
            "আপনার ব্যালেন্সে টাকা যোগ হয়েছে! ধন্যবাদ। 🙏"
        )
    except Exception:
        pass


@bot.message_handler(commands=["reject"])
def handle_reject(message: types.Message) -> None:
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
            f"❌ **Task Rejected!**\n{divider()}\n"
            f"🆔 Submission ID: #{sub_id}\n"
            "⚠️ ফাইলে সমস্যা থাকায় রিজেক্ট হয়েছে। সাহায্যের জন্য 📞 Support এ যোগাযোগ করুন।"
        )
    except Exception:
        pass


@bot.message_handler(commands=["pay"])
def handle_pay_withdraw(message: types.Message) -> None:
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
            f"🎉 **Withdrawal Successful!**\n{divider()}\n"
            f"🆔 Request ID:  #{wid}\n"
            f"💳 Method:      {withd['method']}\n"
            f"💵 Amount:      *{amt}*\n{divider()}\n"
            "পেমেন্ট পাঠিয়ে দেওয়া হয়েছে। ধন্যবাদ! 🙏"
        )
    except Exception:
        pass


@bot.message_handler(commands=["ban"])
def handle_ban(message: types.Message) -> None:
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
def handle_unban(message: types.Message) -> None:
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
def handle_broadcast_start(message: types.Message) -> None:
    if message.chat.id != ADMIN_ID:
        return
    set_user_state(ADMIN_ID, STATE_BROADCAST)
    bot.send_message(ADMIN_ID,
        "📢 **Broadcast Mode**\n"
        "এখন যা পাঠাবেন তা সব ইউজারকে forward হবে।\n"
        "বাতিল করতে: ❌ Cancel",
        reply_markup=cancel_keyboard()
    )

# ═══════════════════════════════════════════════════════════
#  /start
# ═══════════════════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def handle_start(message: types.Message) -> None:
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
        f"👋 Hello, *{name}*!\n\n"
        "ℹ️ সহজ টাস্ক সম্পন্ন করে আয় করুন।\n"
        "নিচের মেনু ব্যবহার করুন 👇",
        reply_markup=main_menu_keyboard(chat_id)
    )

# ═══════════════════════════════════════════════════════════
#  MENU HANDLERS — all bilingual via is_bn()
# ═══════════════════════════════════════════════════════════

# ── Balance ──────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ("💰 Balance", "💰 ব্যালেন্স"))
def handle_balance(message: types.Message) -> None:
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
    min_str  = f"{MIN_WITHDRAW_BDT:.2f} Tk"    if currency == "BDT" else f"${MIN_WITHDRAW_USDT:.2f} USDT"
    fee_str  = f"{WITHDRAW_FEE_BDT:.2f} Tk"    if currency == "BDT" else f"${WITHDRAW_FEE_USDT:.2f} USDT"

    if is_bn(cid):
        text = (f"💰 **ব্যালেন্স ড্যাশবোর্ড**\n{divider()}\n"
                f"💵 বর্তমান ব্যালেন্স:   *{bal_str}*\n"
                f"📈 মোট আয়:            {earn_str}\n{divider()}\n"
                f"💸 সর্বনিম্ন উত্তোলন:  {min_str}\n"
                f"📋 উত্তোলন ফি:         {fee_str}")
    else:
        text = (f"💰 **Balance Dashboard**\n{divider()}\n"
                f"💵 Current Balance:   *{bal_str}*\n"
                f"📈 Total Earned:      {earn_str}\n{divider()}\n"
                f"💸 Min Withdraw:      {min_str}\n"
                f"📋 Withdraw Fee:      {fee_str}")
    bot.send_message(cid, text)


# ── Tasks ─────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ("📋 Tasks", "📋 কাজ"))
def handle_task_submit(message: types.Message) -> None:
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid = message.chat.id
    set_user_state(cid, STATE_IDLE)
    txt = "✨ প্ল্যাটফর্ম সিলেক্ট করুন 👇" if is_bn(cid) else "✨ Select a platform 👇"
    bot.send_message(cid, txt, reply_markup=platform_select_inline())


# ── Withdraw ──────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ("📥 Withdraw", "📥 উত্তোলন"))
def handle_withdraw_menu(message: types.Message) -> None:
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
                f"⚠️ **অপর্যাপ্ত ব্যালেন্স**\n\n"
                f"💰 আপনার ব্যালেন্স: *{bal_str}*\n"
                f"📋 সর্বনিম্ন: {min_str}\n\nআরো টাস্ক সম্পন্ন করুন।"
            )
        else:
            bot.send_message(cid,
                f"⚠️ **Insufficient Balance**\n\n"
                f"💰 Your Balance: *{bal_str}*\n"
                f"📋 Minimum: {min_str}\n\nPlease complete more tasks."
            )
        return

    # Cooldown check
    last_time = get_last_withdrawal_time(cid)
    if last_time:
        last_dt = datetime.strptime(last_time[:19], "%Y-%m-%d %H:%M:%S")
        elapsed = datetime.now() - last_dt
        if elapsed < timedelta(hours=WITHDRAW_COOLDOWN):
            remaining = timedelta(hours=WITHDRAW_COOLDOWN) - elapsed
            h, rem    = divmod(int(remaining.total_seconds()), 3600)
            mn        = rem // 60
            bot.send_message(cid,
                f"⏳ **Cooldown Active**\n\n"
                f"পরবর্তী উত্তোলনের জন্য আরো *{h}h {mn}m* অপেক্ষা করুন।"
            )
            return

    if is_bn(cid):
        txt = (f"📥 **উত্তোলন পদ্ধতি বেছে নিন:**\n{divider()}\n"
               f"💰 ব্যালেন্স:   *{bal_str}*\n"
               f"💸 ফি:          {fee_str}\n"
               f"📋 সর্বনিম্ন:   {min_str}")
    else:
        txt = (f"📥 **Choose Withdraw Method:**\n{divider()}\n"
               f"💰 Balance:   *{bal_str}*\n"
               f"💸 Fee:       {fee_str}\n"
               f"📋 Minimum:   {min_str}")
    set_user_state(cid, STATE_IDLE)
    bot.send_message(cid, txt, reply_markup=withdraw_method_inline(currency))


# ── Profile ───────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ("👤 Profile", "👤 প্রোফাইল"))
def handle_profile(message: types.Message) -> None:
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
        f"👤 **My Profile**\n{divider()}\n"
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


# ── Top Leaderboard ───────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ("🏆 Top", "🏆 শীর্ষ"))
def handle_top(message: types.Message) -> None:
    cid      = message.chat.id
    earners  = get_top_earners(10)
    total_u  = get_total_users()
    currency = get_currency(cid)
    medals   = {1: "🥇", 2: "🥈", 3: "🥉"}

    if is_bn(cid):
        lines = [
            "🏆 **দৈনিক সেরা ১০ ইউজার**", "",
            "ℹ️ ফলাফল প্রতিদিন রাত ০১:০০ টায় ঘোষণা করা হয়।",
            "বিজয়ীরা সরাসরি ব্যালেন্সে পুরস্কার পান!",
            "🔄 প্রতি ১০ মিনিটে আপডেট হয়।", divider()
        ]
    else:
        lines = [
            "🏆 **Top 10 Users Today**", "",
            "ℹ️ Results announced daily at 01:00.",
            "Leaders receive real money to their balance!",
            "🔄 Updates every 10 minutes.", divider()
        ]

    if earners:
        for i, row in enumerate(earners, 1):
            name  = row["first_name"] or "Unknown"
            medal = medals.get(i, f"{i}.")
            prize = fmt_amount(row["total_earned"], currency)
            lines.append(f"{medal}  {name}  —  {prize}")
    else:
        no_data = ("⚠️ এখনো কোনো ডেটা নেই। ইউজাররা কাজ শুরু করলে তালিকা আসবে।"
                   if is_bn(cid) else
                   "⚠️ No data yet. List will appear when users start working.")
        lines.append(no_data)

    lines += [divider(),
              f"👥 {'মোট ইউজার' if is_bn(cid) else 'Total Users'}: {total_u}"]
    bot.send_message(cid, "\n".join(lines))


# ── Support ───────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ("📞 Support", "📞 সাপোর্ট"))
def handle_support(message: types.Message) -> None:
    ensure_user(message)
    cid = message.chat.id
    if is_bn(cid):
        txt = (f"📞 **সাপোর্ট**\n{divider()}\n"
               "কোনো সমস্যা বা পেমেন্ট নিয়ে কথা বলতে নিচের বাটনে ক্লিক করুন।\n\n"
               "💬 এডমিন একটিভ থাকলে দ্রুত রিপ্লাই আসবে।")
    else:
        txt = (f"📞 **Support**\n{divider()}\n"
               "For any issue or payment concern, click the button below.\n\n"
               "💬 Admin will reply as soon as possible.")
    bot.send_message(cid, txt, reply_markup=admin_contact_inline("👤 Contact Admin"))


# ── My Referrals ──────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ("👥 My Referrals", "👥 আমার রেফারেল"))
def handle_my_referrals(message: types.Message) -> None:
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
            f"👥 **আমার রেফারেল**\n{divider()}\n"
            f"👥 মোট রেফারেল: *{ref_count}*\n{divider()}\n"
            f"🔗 **আপনার রেফারেল লিংক:**\n`{link}`\n\n"
            "বন্ধুদের আমন্ত্রণ জানান — তারা কাজ করলে প্রতিবার আয়ের *২০%* আজীবন পাবেন! 🔥"
        )
    else:
        bot.send_message(cid,
            f"👥 **My Referrals**\n{divider()}\n"
            f"👥 Total referrals: *{ref_count}*\n{divider()}\n"
            f"🔗 **Your Referral Link:**\n`{link}`\n\n"
            "Invite friends — earn *20% lifetime commission* every time they work! 🔥"
        )


# ── My Submissions ────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ("📂 My Submissions", "📂 আমার সাবমিশন"))
def handle_my_submissions(message: types.Message) -> None:
    if check_banned(message.chat.id):
        return
    ensure_user(message)
    cid  = message.chat.id
    rows = get_user_submissions(cid, limit=10)

    if not rows:
        bot.send_message(cid,
            "📂 আপনার কোনো সাবমিশন নেই।" if is_bn(cid)
            else "📂 You have no submissions yet."
        )
        return

    icons  = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    title  = f"📂 **{'সর্বশেষ' if is_bn(cid) else 'My Last'} {len(rows)} Submissions**"
    lines  = [title, divider()]
    for row in rows:
        lines.append(
            f"#{row['id']} {icons.get(row['status'],'❓')} "
            f"{row['platform']} — {row['task_type']}\n"
            f"   {row['submitted_at'][:16]}"
        )
    bot.send_message(cid, "\n".join(lines))


# ── Currency ──────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ("💱 Currency", "💱 কারেন্সি"))
def handle_currency_menu(message: types.Message) -> None:
    ensure_user(message)
    cid = message.chat.id
    txt = "💱 কারেন্সি বেছে নিন:" if is_bn(cid) else "💱 Choose your display currency:"
    bot.send_message(cid, txt, reply_markup=currency_select_inline())


@bot.callback_query_handler(func=lambda c: c.data.startswith("currency_"))
def handle_currency_select(call: types.CallbackQuery) -> None:
    currency = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    cid = call.message.chat.id
    set_user_currency(cid, currency)
    name = "BDT (Tk) 🇧🇩" if currency == "BDT" else "USDT 💎"
    txt = (f"✅ কারেন্সি পরিবর্তন: *{name}*" if is_bn(cid)
           else f"✅ Currency set to *{name}*")
    bot.send_message(cid, txt, reply_markup=main_menu_keyboard(cid))


# ── Language ──────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text in ("🌐 Language", "🌐 ভাষা"))
def handle_language(message: types.Message) -> None:
    bot.send_message(message.chat.id,
        "🌐 Choose a language / একটি ভাষা নির্বাচন করুন:",
        reply_markup=language_select_inline()
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def handle_language_select(call: types.CallbackQuery) -> None:
    lang = "Bangla" if call.data == "lang_bn" else "English"
    bot.answer_callback_query(call.id)
    cid = call.message.chat.id
    set_user_lang(cid, lang)
    txt = "✅ ভাষা পরিবর্তন করা হয়েছে।" if lang == "Bangla" else "✅ Language has been changed."
    bot.send_message(cid, txt, reply_markup=main_menu_keyboard(cid))


# ── Cancel ────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "❌ Cancel")
def handle_cancel(message: types.Message) -> None:
    cid = message.chat.id
    if cid == ADMIN_ID:
        user = get_user(ADMIN_ID)
        if user and user["current_state"] == STATE_BROADCAST:
            send_main_menu(ADMIN_ID, "✅ Broadcast cancelled.")
            return
    txt = "✅ বাতিল করা হয়েছে।" if is_bn(cid) else "✅ Operation cancelled."
    send_main_menu(cid, txt)

# ═══════════════════════════════════════════════════════════
#  INLINE CALLBACK HANDLERS
# ═══════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("platform_"))
def handle_platform_select(call: types.CallbackQuery) -> None:
    if check_banned(call.message.chat.id):
        bot.answer_callback_query(call.id)
        return
    platform = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    cid      = call.message.chat.id
    currency = get_currency(cid)

    menus = {
        "instagram": ("🌸 Instagram — টাস্ক সিলেক্ট করুন:", instagram_type_inline(currency)),
        "facebook":  ("🔷 Facebook — টাস্ক সিলেক্ট করুন:",  facebook_type_inline(currency)),
        "gmail":     ("📧 Gmail — টাস্ক সিলেক্ট করুন:",     gmail_type_inline(currency)),
    }
    if platform in menus:
        text, kb = menus[platform]
        bot.send_message(cid, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adminrate_"))
def handle_admin_rate(call: types.CallbackQuery) -> None:
    bot.answer_callback_query(call.id)
    platform = call.data.split("_", 1)[1].capitalize()
    bot.send_message(call.message.chat.id,
        f"🔑 Custom {platform} rate সেট করতে এডমিনের সাথে যোগাযোগ করুন:",
        reply_markup=admin_contact_inline("👤 Admin")
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("taskinfo_"))
def handle_task_info(call: types.CallbackQuery) -> None:
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
        f"📋 **Task:** {meta['label']}\n{divider()}\n"
        f"💵 Reward:       {price_display}/piece\n"
        f"⏳ Review Time:  {meta['review_min']} minutes\n{divider()}\n"
        f"📄 Instructions:\n{meta['description']}\n{divider()}\n"
        f"📂 Required Columns:\n`{meta['columns']}`",
        reply_markup=task_action_inline(task_key)
    )


@bot.callback_query_handler(func=lambda c: c.data == "task_cancel")
def handle_task_cancel_inline(call: types.CallbackQuery) -> None:
    bot.answer_callback_query(call.id)
    cid = call.message.chat.id
    txt = "✅ বাতিল করা হয়েছে।" if is_bn(cid) else "✅ Operation cancelled."
    send_main_menu(cid, txt)


@bot.callback_query_handler(func=lambda c: c.data.startswith("task_") and c.data != "task_cancel")
def handle_task_select(call: types.CallbackQuery) -> None:
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
            f"⚠️ **Duplicate Blocked**\n\n"
            f"*{meta['label']}* এর একটি সাবমিশন এখনো পেন্ডিং আছে।\n"
            "এডমিন রিভিউর পর আবার সাবমিট করুন।"
        )
        return

    price_display = fmt_price(meta["price_val"], currency)

    set_user_state(cid, STATE_WAITING_FILE, task_key=task_key)
    bot.send_message(cid,
        f"📤 **Submit Task**\n{divider()}\n"
        f"📅 Date:    {today_str()}\n"
        f"📋 Task:    {meta['label']}\n"
        f"💵 Reward:  {price_display}/piece\n{divider()}\n"
        f"📂 Required Columns:\n`{meta['columns']}`\n{divider()}\n"
        "আপনার **.xlsx** ফাইলটি আপলোড করুন।\n"
        "বাতিল করতে ❌ Cancel চাপুন।",
        reply_markup=cancel_keyboard()
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("withdraw_"))
def handle_withdraw_method(call: types.CallbackQuery) -> None:
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
        f"✅ **{method_label}** সিলেক্ট করা হয়েছে।\n{divider()}\n"
        f"💸 ফি:          {fee_str}\n"
        f"🔢 সর্বনিম্ন:   {min_str}\n"
        f"💰 ব্যালেন্স:   *{bal_str}*\n{divider()}\n"
        f"কত {unit} উত্তোলন করতে চান? (শুধু সংখ্যা লিখুন)",
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
def handle_broadcast_send(message: types.Message) -> None:
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
def handle_withdraw_amount(message: types.Message) -> None:
    cid = message.chat.id
    try:
        amount = float(message.text.strip().replace(",", ""))
    except ValueError:
        bot.send_message(cid, "⚠️ সঠিক সংখ্যা দিন।", reply_markup=cancel_keyboard())
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
            f"⚠️ আপনার ব্যালেন্স *{current_val:.4f} {unit}* — এত উত্তোলন সম্ভব নয়।",
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
        f"📲 আপনার {label} নম্বর / ঠিকানা লিখুন:",
        reply_markup=cancel_keyboard()
    )


@bot.message_handler(
    func=lambda m: get_user(m.chat.id) is not None
    and get_user(m.chat.id)["current_state"] == STATE_WAITING_ADDRESS
)
def handle_withdraw_address(message: types.Message) -> None:
    cid     = message.chat.id
    address = message.text.strip()
    session = withdraw_session_cache.pop(cid, None)

    if not session or "withdraw_amount" not in session:
        bot.send_message(cid, "⚠️ সেশন মেয়াদ শেষ। আবার চেষ্টা করুন।")
        send_main_menu(cid)
        return

    if len(address) < 5 or len(address) > 100:
        withdraw_session_cache[cid] = session
        bot.send_message(cid, "⚠️ সঠিক নম্বর বা ঠিকানা লিখুন (৫–১০০ অক্ষর)।", reply_markup=cancel_keyboard())
        return

    label    = session["withdraw_label"]
    amount   = session["withdraw_amount"]
    currency = session.get("withdraw_currency", "BDT")
    fee_val  = WITHDRAW_FEE_BDT if currency == "BDT" else WITHDRAW_FEE_USDT
    net      = round(amount - fee_val, 6)
    unit     = "Tk" if currency == "BDT" else "USDT"

    wid = save_withdrawal(cid, label, address, amount, currency)

    bot.send_message(cid,
        f"✅ **Withdrawal Request Submitted!**\n{divider()}\n"
        f"🆔 Request ID:  #{wid}\n"
        f"💳 Method:      {label}\n"
        f"📲 Address:     `{address}`\n"
        f"💰 Amount:      {amount:.4f} {unit}\n"
        f"📋 Fee:         {fee_val:.4f} {unit}\n"
        f"💵 You'll get:  *{net:.4f} {unit}*\n{divider()}\n"
        "⏳ এডমিন ২৪ ঘণ্টার মধ্যে পেমেন্ট প্রসেস করবেন।"
    )
    notify_admin(
        f"💸 **New Withdrawal #{wid}**\n"
        f"👤 Chat ID:  `{cid}`\n"
        f"💳 Method:   {label} ({currency})\n"
        f"📲 Address:  `{address}`\n"
        f"💰 Amount:   {amount:.4f} {unit}  |  Net: {net:.4f} {unit}\n\n"
        f"Approve: `/pay {wid}`"
    )
    send_main_menu(cid)

# ═══════════════════════════════════════════════════════════
#  FILE HANDLER
# ═══════════════════════════════════════════════════════════

@bot.message_handler(content_types=["document"])
def handle_document(message: types.Message) -> None:
    cid      = message.chat.id
    userdata = get_user(cid)

    if not userdata or userdata["current_state"] != STATE_WAITING_FILE:
        bot.send_message(cid, "⚠️ আগে একটি টাস্ক সিলেক্ট করুন।")
        return

    task_key = userdata["current_task_key"]
    meta     = TASK_META.get(task_key)
    if not meta:
        bot.send_message(cid, "⚠️ Task data not found.")
        send_main_menu(cid)
        return

    file_name = message.document.file_name or ""
    if not file_name.lower().endswith(".xlsx"):
        bot.send_message(cid, "❌ শুধুমাত্র **.xlsx** ফাইল গ্রহণযোগ্য।")
        return

    file_size = message.document.file_size or 0
    if file_size > 50 * 1024 * 1024:
        bot.send_message(cid, "❌ ফাইল সাইজ ৫০ MB এর বেশি হওয়া যাবে না।")
        return

    currency      = get_currency(cid)
    price_display = fmt_price(meta["price_val"], currency)

    submission_id = save_submission(cid, meta["platform"], meta["label"], message.document.file_id)
    logger.info("Submission #%d | chat=%s | %s | %s", submission_id, cid, meta["platform"], file_name)

    notify_admin(
        f"📁 **New Submission #{submission_id}**\n"
        f"👤 Chat ID:   `{cid}`\n"
        f"📌 Platform:  {meta['platform']} — {meta['label']} ({meta['price']})\n"
        f"📄 File:      {file_name}  ({file_size/1024:.1f} KB)\n\n"
        f"✅ `/approve {submission_id} [Total_Accounts]`\n"
        f"❌ `/reject {submission_id}`"
    )
    bot.send_message(cid,
        f"✅ **File Submitted!**\n{divider()}\n"
        f"🆔 Submission ID:  #{submission_id}\n"
        f"📌 Platform:       {meta['platform']}\n"
        f"📋 Task:           {meta['label']}\n"
        f"💵 Reward Rate:    {price_display}/piece\n"
        f"📅 Date:           {today_str()}\n"
        f"⏳ Review Time:    {meta['review_min']} minutes\n{divider()}\n"
        "এডমিন রিভিউর পর আপনার ব্যালেন্সে টাকা যোগ হবে।"
    )
    send_main_menu(cid)

# ═══════════════════════════════════════════════════════════
#  CATCH-ALL
# ═══════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: True)
def handle_fallback(message: types.Message) -> None:
    cid      = message.chat.id
    userdata = get_user(cid)
    state    = userdata["current_state"] if userdata else STATE_IDLE

    if state == STATE_WAITING_FILE:
        bot.send_message(cid, "⚠️ .xlsx ফাইল আপলোড করুন অথবা ❌ Cancel চাপুন।", reply_markup=cancel_keyboard())
    elif state in (STATE_WAITING_ADDRESS, STATE_WAITING_AMOUNT):
        bot.send_message(cid, "⚠️ সঠিক তথ্য দিন অথবা ❌ Cancel চাপুন।", reply_markup=cancel_keyboard())
    elif state == STATE_BROADCAST and cid == ADMIN_ID:
        bot.send_message(ADMIN_ID, "⚠️ Broadcast mode চালু। মেসেজ পাঠান বা ❌ Cancel করুন।", reply_markup=cancel_keyboard())
    else:
        send_main_menu(cid, "🏠 মেইন মেনু" if is_bn(cid) else "🏠 Main Menu")

# ═══════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    logger.info("Bot starting — polling...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
