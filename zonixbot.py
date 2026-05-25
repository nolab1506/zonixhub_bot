import os
import logging
from datetime import date
import telebot
from telebot import types

# ─────────────────────────────────────────────
#  Configuration  (set these as env variables)
# ─────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_URL  = os.environ.get("ADMIN_URL", "https://t.me/YourAdminUsername")   # admin Telegram profile URL
BOT_USERNAME = os.environ.get("BOT_USERNAME", "YourBotUsername")              # bot username WITHOUT @

# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  Bot instance
# ─────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ─────────────────────────────────────────────
#  In-memory state store  {chat_id: state_str}
# ─────────────────────────────────────────────
user_states: dict[int, str] = {}

STATE_IDLE            = "idle"
STATE_WAITING_FILE    = "waiting_file"


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


def task_select_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🌸 Instagram", callback_data="task_instagram"))
    return kb


def instagram_type_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔑 set admin rate", callback_data="insta_admin_rate"),
        types.InlineKeyboardButton("Insta 2FA 🔥 (2.70TK)", callback_data="insta_2fa"),
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
    user_states[chat_id] = STATE_IDLE
    bot.send_message(chat_id, text, reply_markup=main_menu_keyboard())


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


# ══════════════════════════════════════════════
#  /start  command
# ══════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def handle_start(message: types.Message) -> None:
    user_states[message.chat.id] = STATE_IDLE
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
    user_states[message.chat.id] = STATE_IDLE
    bot.send_message(
        message.chat.id,
        "✨ যেকোনো একটি কাজ সিলেক্ট করুন 👇",
        reply_markup=task_select_inline(),
    )


@bot.message_handler(func=lambda m: m.text == "💵 ব্যালেন্স")
def handle_balance(message: types.Message) -> None:
    bot.send_message(message.chat.id, "💰 আপনার বর্তমান ব্যালেন্স: 0.00 TK")


@bot.message_handler(func=lambda m: m.text == "💸 উইথড্র")
def handle_withdraw(message: types.Message) -> None:
    bot.send_message(
        message.chat.id,
        "⚠️ টাকা উইথড্র করার জন্য আপনার বিকাশ/নগদ নম্বর দিন (নূন্যতম ২০ টাকা)।\n"
        "অথবা অ্যাডমিনের সাথে যোগাযোগ করুন।",
    )


@bot.message_handler(func=lambda m: m.text == "🎁 Invite & Earn")
def handle_invite(message: types.Message) -> None:
    referral_link = f"https://t.me/{BOT_USERNAME}?start={message.chat.id}"
    bot.send_message(
        message.chat.id,
        f"🎁 আপনার রেফারেল লিংক:\n\n{referral_link}\n\n"
        "এই লিংকটি বন্ধুদের সাথে শেয়ার করুন এবং প্রতিটি রেফারেলে বোনাস আয় করুন!",
    )


@bot.message_handler(func=lambda m: m.text == "☎️ সাপোর্ট")
def handle_support(message: types.Message) -> None:
    bot.send_message(
        message.chat.id,
        "📞 যেকোনো সমস্যায় অ্যাডমিনের সাথে যোগাযোগ করুন:",
        reply_markup=admin_contact_inline("👤 অ্যাডমিনের সাথে কথা বলুন"),
    )


@bot.message_handler(func=lambda m: m.text == "🆕 আমি নতুন?")
def handle_new_user(message: types.Message) -> None:
    guide = (
        "🆕 বটটি কীভাবে ব্যবহার করবেন:\n\n"
        "1️⃣ 📁 কাজ সাবমিট — এখানে ক্লিক করে আপনার কাজ জমা দিন।\n"
        "2️⃣ কাজের ধরন সিলেক্ট করুন (যেমন Instagram 2FA)।\n"
        "3️⃣ নির্দেশনা অনুযায়ী .xlsx ফাইল তৈরি করুন এবং আপলোড করুন।\n"
        "4️⃣ অ্যাডমিন চেক করার পর আপনার ব্যালেন্সে টাকা যোগ হবে।\n"
        "5️⃣ 💵 ব্যালেন্স বাটনে ক্লিক করে আপনার ব্যালেন্স দেখুন।\n"
        "6️⃣ 💸 উইথড্র করতে বিকাশ/নগদ নম্বর দিন (নূন্যতম ২০ টাকা)।\n"
        "7️⃣ 🎁 Invite & Earn — বন্ধুদের রেফার করে বোনাস আয় করুন।\n\n"
        "যেকোনো সমস্যায় ☎️ সাপোর্ট বাটনে ক্লিক করুন।"
    )
    bot.send_message(message.chat.id, guide)


@bot.message_handler(func=lambda m: m.text == "❌ বাতিল")
def handle_cancel(message: types.Message) -> None:
    send_main_menu(message.chat.id, "প্রধান মেনুতে ফিরে যাওয়া হয়েছে।")


# ══════════════════════════════════════════════
#  INLINE CALLBACK  handlers
# ══════════════════════════════════════════════

@bot.callback_query_handler(func=lambda call: call.data == "task_instagram")
def callback_instagram(call: types.CallbackQuery) -> None:
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Instagram এর কাজের ধরণ:",
            reply_markup=instagram_type_inline(),
        )
    except Exception as e:
        logger.warning("edit_message_text failed: %s", e)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "insta_admin_rate")
def callback_admin_rate(call: types.CallbackQuery) -> None:
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "🔑 বিশেষ রেট সেট করার জন্য অ্যাডমিনের সাথে যোগাযোগ করুন:",
        reply_markup=admin_contact_inline("👤 অ্যাডমিনের সাথে কথা বলুন"),
    )


@bot.callback_query_handler(func=lambda call: call.data == "insta_2fa")
def callback_insta_2fa(call: types.CallbackQuery) -> None:
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id

    instructions = (
        "📸 Instagram 2FA সাবমিট\n"
        f"📅 ডেট: {today_str()}\n\n"
        "> অনুগ্রহ করে আপনার .xlsx ফাইলটি আপলোড করুন, যেখানে:\n"
        "   A. Username\n"
        "   B. Password\n"
        "   C. 2FA"
    )

    bot.send_message(chat_id, instructions, reply_markup=cancel_keyboard())
    user_states[chat_id] = STATE_WAITING_FILE


# ══════════════════════════════════════════════
#  DOCUMENT  handler  (file uploads)
# ══════════════════════════════════════════════

@bot.message_handler(
    content_types=["document"],
    func=lambda m: user_states.get(m.chat.id) == STATE_WAITING_FILE,
)
def handle_file_upload(message: types.Message) -> None:
    file_name = (message.document.file_name or "").lower()

    if file_name.endswith(".xlsx") or file_name.endswith(".xls"):
        send_main_menu(
            message.chat.id,
            "✅ আপনার ফাইলটি সফলভাবে রিসিভ করা হয়েছে! "
            "অ্যাডমিন চেক করে আপনার ব্যালেন্স অ্যাড করে দেবে।",
        )
    else:
        bot.send_message(
            message.chat.id,
            "❌ দুঃখিত! এটি .xlsx ফাইল নয়। "
            "দয়া করে সঠিক ফাইলটি আবার আপলোড করুন।",
            reply_markup=cancel_keyboard(),
        )


# Wrong content type while waiting for file
@bot.message_handler(
    content_types=["photo", "video", "audio", "voice", "sticker", "animation"],
    func=lambda m: user_states.get(m.chat.id) == STATE_WAITING_FILE,
)
def handle_wrong_content(message: types.Message) -> None:
    bot.send_message(
        message.chat.id,
        "❌ দুঃখিত! এটি .xlsx ফাইল নয়। "
        "দয়া করে সঠিক ফাইলটি আবার আপলোড করুন।",
        reply_markup=cancel_keyboard(),
    )


# ══════════════════════════════════════════════
#  FALLBACK  handler
# ══════════════════════════════════════════════

@bot.message_handler(func=lambda m: True)
def handle_unknown(message: types.Message) -> None:
    state = user_states.get(message.chat.id, STATE_IDLE)

    if state == STATE_WAITING_FILE:
        bot.send_message(
            message.chat.id,
            "❌ দুঃখিত! এটি .xlsx ফাইল নয়। "
            "দয়া করে সঠিক ফাইলটি আবার আপলোড করুন।",
            reply_markup=cancel_keyboard(),
        )
    else:
        bot.send_message(
            message.chat.id,
            "অনুগ্রহ করে নিচের মেনু থেকে একটি অপশন বেছে নিন।",
            reply_markup=main_menu_keyboard(),
        )


# ══════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("Bot starting… (polling)")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
