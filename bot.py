import os
import logging
import sqlite3
from datetime import date
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

# Withdraw settings (In BDT/Tk)
MIN_WITHDRAW  = 20.00   # Tk
WITHDRAW_FEE  = 2.50    # Tk
REFERRAL_COMM_PCT = 0.20 # 20% commission

# States
STATE_IDLE            = "idle"
STATE_WAITING_FILE    = "waiting_file"
STATE_WAITING_ADDRESS = "waiting_address"
STATE_WAITING_AMOUNT  = "waiting_amount"

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
                language      TEXT DEFAULT 'en',
                current_state TEXT DEFAULT 'idle',
                current_task_key TEXT
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
    logger.info("✅ Database initialised safely with WAL at %s", DB_PATH)


def ensure_user(message: types.Message, referred_by: int = None) -> None:
    chat_id = message.chat.id
    with get_db() as conn:
        existing = conn.execute(
            "SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if not existing:
            referral_code = f"REF{chat_id}"
            conn.execute(
                """INSERT INTO users (chat_id, username, first_name, referral_code, referred_by, current_state)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    chat_id,
                    message.from_user.username or "",
                    message.from_user.first_name or "",
                    referral_code,
                    referred_by,
                    STATE_IDLE
                ),
            )
            conn.commit()


def get_user(chat_id: int):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()


def set_user_state(chat_id: int, state: str, task_key: str = None) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET current_state = ?, current_task_key = ? WHERE chat_id = ?",
            (state, task_key, chat_id)
        )
        conn.commit()


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
#  BOT & METADATA
# ═══════════════════════════════════════════════════════════
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

withdraw_session_cache: dict[int, dict] = {}

TASK_META = {
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
    "ig_cookies": {
        "platform": "Instagram",
        "label": "Instagram Cookies I'D",
        "price": "4.00 Tk/piece",
        "price_val": 4.00,
        "review_min": 45,
        "description": "Submit Instagram accounts generated strictly with accurate session cookies.",
        "columns": "A = Username   |   B = Password  (Only 2 Columns)",
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
    kb.row("🏆 Top", "📞 Support")
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
        types.InlineKeyboardButton("🔑 Set Admin Rate",                  callback_data="adminrate_instagram"),
        types.InlineKeyboardButton("Instagram Cookies I'D 🔥 (4.00 Tk)", callback_data="taskinfo_ig_cookies"),
    )
    return kb

def facebook_type_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔑 Set Admin Rate",                  callback_data="adminrate_facebook"),
        types.InlineKeyboardButton("PC Clone 6158x I'D 🔥 (5.00 Tk)",    callback_data="taskinfo_fb_clone_6158x"),
        types.InlineKeyboardButton("PC Clone 1000x I'D 🔥 (15.00 Tk)",   callback_data="taskinfo_fb_clone_1000x"),
    )
    return kb

def gmail_type_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔑 Set Admin Rate",                  callback_data="adminrate_gmail"),
        types.InlineKeyboardButton("Fresh Gmail 🔥 (100.00 Tk)",         callback_data="taskinfo_gmail_fresh"),
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
    set_user_state(chat_id, STATE_IDLE)
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
#  👑 ADMIN CONTROLS COMMANDS 👑
# ═══════════════════════════════════════════════════════════

@bot.message_handler(commands=["admin"])
def handle_admin_help(message: types.Message) -> None:
    if message.chat.id != ADMIN_ID:
        return
    help_text = (
        "👑 **Admin Control Panel**\n"
        f"{divider()}\n"
        "🟢 **Approve File & Add Balance:**\n"
        "`/approve [Submission_ID] [Total_Accounts]`\n"
        "Example: `/approve 5 120` *(ID 5 এর ফাইলে ১২০টি আইডি সঠিক ছিল)*\n\n"
        "🔴 **Reject File:**\n"
        "`/reject [Submission_ID]`\n\n"
        "💵 **Complete Cashout:**\n"
        "`/pay [Withdraw_ID]`\n"
        f"{divider()}\n"
        "📊 `/stats` - চেক টোটাল বটের ইউজার ডাটা।"
    )
    bot.send_message(ADMIN_ID, help_text)

@bot.message_handler(commands=["stats"])
def handle_admin_stats(message: types.Message) -> None:
    if message.chat.id != ADMIN_ID:
        return
    with get_db() as conn:
        total_users = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
        pending_subs = conn.execute("SELECT COUNT(*) as cnt FROM submissions WHERE status='pending'").fetchone()["cnt"]
        pending_with = conn.execute("SELECT COUNT(*) as cnt FROM withdrawals WHERE status='pending'").fetchone()["cnt"]
    
    bot.send_message(
        ADMIN_ID,
        f"📊 **Bot Current Statistics**\n"
        f"{divider()}\n"
        f"👥 Total Users: {total_users}\n"
        f"📁 Pending Submissions: {pending_subs}\n"
        f"💸 Pending Cashouts: {pending_with}"
    )

@bot.message_handler(commands=["approve"])
def handle_approve(message: types.Message) -> None:
    if message.chat.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 3:
        bot.send_message(ADMIN_ID, "⚠️ Format error! Use: `/approve [Submission_ID] [Total_Accounts]`")
        return
    
    try:
        sub_id = int(args[1])
        total_items = int(args[2])
    except ValueError:
        bot.send_message(ADMIN_ID, "⚠️ Please type correct numeric digits.")
        return

    with get_db() as conn:
        sub = conn.execute("SELECT * FROM submissions WHERE id = ? AND status = 'pending'", (sub_id,)).fetchone()
        if not sub:
            bot.send_message(ADMIN_ID, "⚠️ Pending Submission file with this ID not found.")
            return
        
        # Find Task Metadata
        task_key = None
        for k, v in TASK_META.items():
            if v["label"] == sub["task_type"]:
                task_key = k
                break
        
        if not task_key:
            bot.send_message(ADMIN_ID, "⚠️ Meta configurations mismatch.")
            return
            
        price_per_item = TASK_META[task_key]["price_val"]
        total_payout = round(price_per_item * total_items, 4)
        
        # Credit user wallet
        conn.execute(
            "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE chat_id = ?",
            (total_payout, total_payout, sub["chat_id"])
        )
        
        # 20% Lifetime Referral Commission
        user_info = conn.execute("SELECT referred_by FROM users WHERE chat_id = ?", (sub["chat_id"],)).fetchone()
        ref_text = ""
        if user_info and user_info["referred_by"]:
            referrer_id = user_info["referred_by"]
            commission_amount = round(total_payout * REFERRAL_COMM_PCT, 4)
            
            conn.execute(
                "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE chat_id = ?",
                (commission_amount, commission_amount, referrer_id)
            )
            ref_text = f"\n🎁 Referrer ({referrer_id}) received 20% Commission: {commission_amount:.2f} Tk"
            try:
                bot.send_message(
                    referrer_id,
                    f"🎁 **Referral Commission Credit!**\n"
                    f"আপনার রেফার করা ইউজার একটি টাস্ক সম্পন্ন করায় আপনি আজীবন পলিসি অনুযায়ী ২০% কমিশন পেয়েছেন।\n\n"
                    f"💰 অর্জিত কমিশন: {commission_amount:.2f} Tk"
                )
            except Exception:
                pass
                
        conn.execute("UPDATE submissions SET status = 'approved' WHERE id = ?", (sub_id,))
        conn.commit()

    bot.send_message(ADMIN_ID, f"✅ Submission #{sub_id} Approved Successfully!\n💵 User Paid: {total_payout:.2f} Tk (Accounts: {total_items}){ref_text}")
    try:
        bot.send_message(sub["chat_id"], f"🎉 **Task Approved!**\n\n🆔 Submission ID: #{sub_id}\n📋 Task: {sub['task_type']}\n✅ Accepted Accounts: {total_items}\n💰 Credited to Balance: {total_payout:.2f} Tk")
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
        sub = conn.execute("SELECT chat_id FROM submissions WHERE id = ? AND status = 'pending'", (sub_id,)).fetchone()
        if not sub:
            bot.send_message(ADMIN_ID, "⚠️ Pending Submission not found.")
            return
        conn.execute("UPDATE submissions SET status = 'rejected' WHERE id = ?", (sub_id,))
        conn.commit()

    bot.send_message(ADMIN_ID, f"🔴 Submission #{sub_id} marked as Rejected.")
    try:
        bot.send_message(sub["chat_id"], f"❌ **Task Rejected!**\n\n🆔 Submission ID: #{sub_id}\n⚠️ আপনার পাঠানো ফাইলে সমস্যা থাকায় এডমিন এটি রিজেক্ট করেছেন। সাহায্য লাগলে Support এ যোগাযোগ করুন।")
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
        withd = conn.execute("SELECT * FROM withdrawals WHERE id = ? AND status = 'pending'", (wid,)).fetchone()
        if not withd:
            bot.send_message(ADMIN_ID, "⚠️ Pending Cashout Request ID not found.")
            return
        conn.execute("UPDATE withdrawals SET status = 'paid' WHERE id = ?", (wid,))
        conn.commit()

    bot.send_message(ADMIN_ID, f"✅ Cashout Request #{wid} marked as PAID.")
    try:
        bot.send_message(withd["chat_id"], f"🎉 **Withdraw Success!**\n\n🆔 Request ID: #{wid}\n💳 Method: {withd['method']}\n💵 Amount: {withd['net_amount']:.2f} Tk\n\nআপনার একাউন্টে টাকা পাঠিয়ে দেওয়া হয়েছে। আমাদের সাথে থাকার জন্য ধন্যবাদ!")
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def handle_start(message: types.Message) -> None:
    chat_id = message.chat.id
    args = message.text.split()
    referred_by = None

    if len(args) > 1:
        ref_code = args[1]
        with get_db() as conn:
            ref_row = conn.execute(
                "SELECT chat_id FROM users WHERE referral_code = ?", (ref_code,)
            ).fetchone()
            if ref_row and ref_row["chat_id"] != chat_id:
                referred_by = ref_row["chat_id"]

    ensure_user(message, referred_by=referred_by)
    set_user_state(chat_id, STATE_IDLE)

    name = message.from_user.first_name or "Friend"
    welcome = (
        f"👋 Hello, {name}!\n\n"
        f"ℹ️ This bot helps you earn money by completing simple tasks.\n\n"
        f"Use the menu below to get started."
    )
    bot.send_message(chat_id, welcome, reply_markup=main_menu_keyboard())

# ═══════════════════════════════════════════════════════════
#  MAIN MENU BUTTONS
# ═══════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "📋 Tasks")
def handle_task_submit(message: types.Message) -> None:
    ensure_user(message)
    set_user_state(message.chat.id, STATE_IDLE)
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
        f"💰 **Balance Dashboard**\n"
        f"{divider()}\n"
        f"💵 Current Balance:  {balance:.2f} Tk\n"
        f"📈 Total Earned:     {total_earned:.2f} Tk\n"
        f"{divider()}\n"
        f"💸 Min Withdraw:     {MIN_WITHDRAW:.2f} Tk\n"
        f"📋 Withdraw Fee:     {WITHDRAW_FEE:.2f} Tk"
    )
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "📤 Withdraw")
def handle_withdraw_menu(message: types.Message) -> None:
    ensure_user(message)
    balance = get_balance(message.chat.id)
    if balance < MIN_WITHDRAW:
        bot.send_message(
            message.chat.id,
            f"⚠️ **Insufficient balance.**\n\n"
            f"💰 Your Balance:   {balance:.2f} Tk\n"
            f"📋 Minimum:        {MIN_WITHDRAW:.2f} Tk\n\n"
            f"Complete more tasks to reach the withdrawal threshold.",
        )
        return
    set_user_state(message.chat.id, STATE_IDLE)
    bot.send_message(
        message.chat.id,
        f"📤 **Choose Withdraw Method**\n"
        f"{divider()}\n"
        f"💰 Your Balance:  {balance:.2f} Tk\n"
        f"💸 Fee:           {WITHDRAW_FEE:.2f} Tk\n"
        f"📋 Minimum:       {MIN_WITHDRAW:.2f} Tk",
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
        f"👤 **Profile Control**\n"
        f"{divider()}\n"
        f"📛 Name:            {name}\n"
        f"🔗 Username:        {username}\n"
        f"🆔 Chat ID:         {chat_id}\n"
        f"📅 Joined:          {user['joined_at']}\n"
        f"{divider()}\n"
        f"💵 Balance:         {user['balance']:.2f} Tk\n"
        f"📈 Total Earned:    {user['total_earned']:.2f} Tk\n"
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
        f"🏆 **Top 10 Earners**",
        divider(),
    ]
    for i, row in enumerate(earners, 1):
        name  = row["first_name"] or "Unknown"
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"  {i}.")
        lines.append(f"{medal}  {name}  —  {row['total_earned']:.2f} Tk")
    lines.append(divider())
    lines.append(f"👥 Total Users: {total_users}")
    bot.send_message(message.chat.id, "\n".join(lines))


@bot.message_handler(func=lambda m: m.text == "📞 Support")
def handle_support(message: types.Message) -> None:
    ensure_user(message)
    chat_id = message.chat.id
    text = (
        f"📞 **Contact Support**\n"
        f"{divider()}\n"
        f"আপনার যদি কোনো সমস্যা হয় বা পেমেন্ট নিয়ে কথা বলতে চান, "
        f"তাহলে নিচের বাটনে ক্লিক করে সরাসরি আমাদের এডমিন-এর সাথে যোগাযোগ করুন।\n\n"
        f"💬 Admin একটিভ থাকলে দ্রুত রিপ্লাই দেওয়া হবে।"
    )
    bot.send_message(chat_id, text, reply_markup=admin_contact_inline("👤 Contact Admin"))


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
        f"👥 **My Referrals**\n"
        f"{divider()}\n"
        f"✅ Total Referrals:  {ref_count}\n"
        f"🎁 Bonus Policy:    **🔥 20% Lifetime Commission**\n"
        f"{divider()}\n"
        f"🔗 Your Invite Link:\n{invite_link}\n\n"
        "আপনার বন্ধুদের আমন্ত্রণ জানান এবং তারা কাজ সাবমিট করলেই প্রতিবার আয়ের ২০% আপনার একাউন্টে আজীবন লাইভ পেয়ে যান!",
    )

@bot.message_handler(func=lambda m: m.text == "🌐 Language")
def handle_language(message: types.Message) -> None:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
        types.InlineKeyboardButton("🇧🇩 বাংলা",   callback_data="lang_bn"),
    )
    bot.send_message(message.chat.id, "🌐 Select your language:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def handle_language_select(call: types.CallbackQuery) -> None:
    lang = call.data.split("_", 1)[1]
    bot.answer_callback_query(call.id)
    lang_name = "English 🇬🇧" if lang == "en" else "বাংলা 🇧🇩"
    with get_db() as conn:
        conn.execute("UPDATE users SET language = ? WHERE chat_id = ?", (lang, call.message.chat.id))
        conn.commit()
    bot.send_message(call.message.chat.id, f"✅ Language set to {lang_name}", reply_markup=main_menu_keyboard())

@bot.message_handler(func=lambda m: m.text == "❌ Cancel")
def handle_cancel(message: types.Message) -> None:
    send_main_menu(message.chat.id, "✅ Operation cancelled.")

# ═══════════════════════════════════════════════════════════
#  CALLBACK HANDLERS
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
        f"📋 **Task:** {meta['label']}\n"
        f"{divider()}\n"
        f"💵 Reward:       {meta['price']}\n"
        f"⏳ Review Time:  {meta['review_min']} minutes\n"
        f"{divider()}\n"
        f"📄 Instructions:\n{meta['description']}\n"
        f"{divider()}\n"
        f"📂 Required Columns:\n{meta['columns']}"
    )
    bot.send_message(call.message.chat.id, text, reply_markup=task_action_inline(task_key))

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
    set_user_state(chat_id, STATE_WAITING_FILE, task_key=task_key)
    text = (
        f"📤 **Submit Task**\n"
        f"{divider()}\n"
        f"📅 Date:    {today_str()}\n"
        f"📋 Task:    {meta['label']}\n"
        f"💵 Reward:  {meta['price']}\n"
        f"{divider()}\n"
        f"📂 Required Columns:\n{meta['columns']}\n"
        f"{divider()}\n"
        "Please upload your **.xlsx** file now.\n"
        "Press ❌ Cancel to go back."
    )
    bot.send_message(chat_id, text, reply_markup=cancel_keyboard())

@bot.callback_query_handler(func=lambda c: c.data.startswith("withdraw_"))
def handle_withdraw_method(call: types.CallbackQuery) -> None:
    method_key = call.data[9:]
    bot.answer_callback_query(call.id)
    method = WITHDRAW_METHODS.get(method_key)
    if not method:
        return

    chat_id = call.message.chat.id
    balance = get_balance(chat_id)
    
    set_user_state(chat_id, STATE_WAITING_AMOUNT)
    withdraw_session_cache[chat_id] = {
        "withdraw_method": method_key,
        "withdraw_label": method["label"],
    }
    bot.send_message(
        chat_id,
        f"✅ You selected **{method['label']}**.\n"
        f"{divider()}\n"
        f"💸 Fee:      {method['fee']:.2f} Tk\n"
        f"🔢 Minimum:  {method['min']:.2f} Tk\n"
        f"💰 Balance:  {balance:.2f} Tk\n"
        f"{divider()}\n"
        "How much would you like to withdraw?\n(Enter numbers only)",
        reply_markup=cancel_keyboard(),
    )

# ═══════════════════════════════════════════════════════════
#  STATE WORKFLOWS (TEXT)
# ═══════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: get_user(m.chat.id) and get_user(m.chat.id)["current_state"] == STATE_WAITING_AMOUNT)
def handle_withdraw_amount(message: types.Message) -> None:
    chat_id = message.chat.id
    try:
        amount = float(message.text.strip())
    except ValueError:
        bot.send_message(chat_id, "⚠️ Please enter a valid number.", reply_markup=cancel_keyboard())
        return

    balance = get_balance(chat_id)
    if amount < MIN_WITHDRAW:
        bot.send_message(chat_id, f"⚠️ Minimum withdrawal amount is {MIN_WITHDRAW:.2f} Tk.", reply_markup=cancel_keyboard())
        return
    if amount > balance:
        bot.send_message(chat_id, f"⚠️ Insufficient balance. Your balance: {balance:.2f} Tk", reply_markup=cancel_keyboard())
        return

    session = withdraw_session_cache.get(chat_id, {})
    session["withdraw_amount"] = amount
    
    set_user_state(chat_id, STATE_WAITING_ADDRESS)
    label = session.get("withdraw_label", "Wallet")

    bot.send_message(
        chat_id,
        f"💵 Amount:     {amount:.2f} Tk\n"
        f"📋 Fee:        {WITHDRAW_FEE:.2f} Tk\n"
        f"✅ You'll get: {amount - WITHDRAW_FEE:.2f} Tk\n"
        f"{divider()}\n"
        f"📲 Enter your {label} number / address:",
        reply_markup=cancel_keyboard(),
    )

@bot.message_handler(func=lambda m: get_user(m.chat.id) and get_user(m.chat.id)["current_state"] == STATE_WAITING_ADDRESS)
def handle_withdraw_address(message: types.Message) -> None:
    chat_id = message.chat.id
    address = message.text.strip()
    session = withdraw_session_cache.pop(chat_id, None)

    if not session:
        bot.send_message(chat_id, "⚠️ Session expired. Please restart the withdrawal process.")
        send_main_menu(chat_id)
        return

    method_key = session["withdraw_method"]
    amount     = session["withdraw_amount"]
    label      = session["withdraw_label"]

    wid = save_withdrawal(chat_id, label, address, amount)
    net = round(amount - WITHDRAW_FEE, 4)

    bot.send_message(
        chat_id,
        f"✅ **Withdrawal Request Submitted!**\n"
        f"{divider()}\n"
        f"🆔 Request ID:  #{wid}\n"
        f"💳 Method:      {label}\n"
        f"📲 Address:     {address}\n"
        f"💰 Amount:      {amount:.2f} Tk\n"
        f"📋 Fee:         {WITHDRAW_FEE:.2f} Tk\n"
        f"💵 You'll get:  {net:.2f} Tk\n"
        f"{divider()}\n"
        "⏳ Admin will process within 24 hours.",
    )

    notify_admin(
        f"💸 **New Withdrawal Request #{wid}**\n"
        f"👤 Chat ID:  {chat_id}\n"
        f"💳 Method:   {label}\n"
        f"📲 Address:  {address}\n"
        f"💰 Amount:   {amount:.2f} Tk  |  Net: {net:.2f} Tk\n\n"
        f"Approve command: `/pay {wid}`"
    )
    send_main_menu(chat_id)

# ═══════════════════════════════════════════════════════════
#  FILE HANDLER
# ═══════════════════════════════════════════════════════════

@bot.message_handler(content_types=["document"])
def handle_document(message: types.Message) -> None:
    chat_id  = message.chat.id
    userdata = get_user(chat_id)

    if not userdata or userdata["current_state"] != STATE_WAITING_FILE:
        bot.send_message(chat_id, "⚠️ Unexpected file. Please select a task first.")
        return

    task_key = userdata["current_task_key"]
    meta     = TASK_META.get(task_key)
    if not meta:
        bot.send_message(chat_id, "⚠️ Task data not found. Please try again.")
        send_main_menu(chat_id)
        return

    file_name = message.document.file_name or ""
    if not file_name.lower().endswith(".xlsx"):
        bot.send_message(chat_id, "❌ Only **.xlsx** files are accepted. Please send the correct file.")
        return

    submission_id = save_submission(
        chat_id=chat_id,
        platform=meta["platform"],
        task_type=meta["label"],
        file_id=message.document.file_id,
    )

    logger.info("Submission #%d | chat_id=%s | %s | %s", submission_id, chat_id, meta["platform"], file_name)

    notify_admin(
        f"📁 **New Submission #{submission_id}**\n"
        f"👤 Chat ID:   {chat_id}\n"
        f"📌 Platform:  {meta['platform']} — {meta['label']} ({meta['price']})\n"
        f"📄 File:      {file_name}\n\n"
        f"Approve Command: `/approve {submission_id} [Total_Accounts]`\n"
        f"Reject Command: `/reject {submission_id}`"
    )

    bot.send_message(
        chat_id,
        f"✅ **File Submitted Successfully!**\n"
        f"{divider()}\n"
        f"🆔 Submission ID:  #{submission_id}\n"
        f"📌 Platform:       {meta['platform']}\n"
        f"📋 Task:           {meta['label']}\n"
        f"💵 Reward Rate:    {meta['price']}\n"
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
    chat_id  = message.chat.id
    userdata = get_user(chat_id)
    state    = userdata["current_state"] if userdata else STATE_IDLE

    if state == STATE_WAITING_FILE:
        bot.send_message(chat_id, "⚠️ Please upload a .xlsx file or press ❌ Cancel.", reply_markup=cancel_keyboard())
    elif state in (STATE_WAITING_ADDRESS, STATE_WAITING_AMOUNT):
        bot.send_message(chat_id, "⚠️ Please enter valid information or press ❌ Cancel.", reply_markup=cancel_keyboard())
    else:
        send_main_menu(chat_id, "🏠 Main Menu")

# ═══════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    logger.info("🤖 Bot starting — polling...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
