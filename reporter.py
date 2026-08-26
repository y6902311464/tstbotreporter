# -*- coding: utf-8 -*-
"""
coding by amirwebcode : telegram = @saeqehe
pip install python-telegram-bot==22.7 rubpy pycryptodome
"""

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15 as pkcs1_15_sig

from rubpy import Client as RubikaClient
from rubpy.crypto import Crypto
from rubpy.enums import ReportType

from telegram import (
    ChatMember,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "8179859382:AAHnhHIa5-DXV923UfRdbEgOUnLO5P79qIs"
ADMIN_IDS: set[int] = {8503523539}
ADMIN_USERNAME = "@Saeqehe"

REQUIRED_CHANNELS = ["mrvpn294", "amirwebcode1"]

FREE_LIMIT    = 100
PREMIUM_LIMIT = 1000
PREMIUM_PRICE = "350,000 تومان"
REPORT_DELAY  = 3

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DB_PATH      = os.path.join(BASE_DIR, "users.db")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

(
    ST_PHONE,
    ST_PASSWORD,
    ST_CODE,
    ST_MAIN_MENU,
    ST_REPORT_GUID,
    ST_REPORT_TYPES,
    ST_REPORT_OTHER_TEXT,
    ST_REPORT_COUNT,
) = range(8)

REPORT_TYPES_MAP: dict[str, tuple[str, ReportType]] = {
    "1": ("🔞 محتوای مستهجن", ReportType.PORNOGRAPHY),
    "2": ("⚔️ خشونت",         ReportType.VIOLENCE),
    "3": ("📛 اسپم",          ReportType.SPAM),
    "4": ("👶 کودک‌آزاری",    ReportType.CHILD_ABUSE),
    "5": ("©️ نقض حق‌نشر",    ReportType.COPYRIGHT),
    "6": ("🎣 فیشینگ",        ReportType.FISHING),
    "7": ("📝 سایر",          ReportType.OTHER),
}

# ────────────────────────────────────────────────────────────────────────
#  DB
# ────────────────────────────────────────────────────────────────────────
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id        INTEGER PRIMARY KEY,
                phone              TEXT,
                rubika_session     TEXT,
                rubika_auth        TEXT,
                rubika_private_key TEXT,
                is_premium         INTEGER DEFAULT 0,
                premium_until      TEXT,
                coins              INTEGER DEFAULT 0,
                total_reports      INTEGER DEFAULT 0,
                total_invites      INTEGER DEFAULT 0,
                created_at         TEXT DEFAULT (datetime('now'))
            )
        """)
        for col, ctype in [("rubika_auth", "TEXT"), ("rubika_private_key", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ctype}")
            except sqlite3.OperationalError:
                pass
        conn.commit()

def get_user(telegram_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return dict(row) if row else None

def upsert_user(telegram_id: int, **kwargs) -> None:
    existing = get_user(telegram_id)
    with sqlite3.connect(DB_PATH) as conn:
        if existing:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            conn.execute(
                f"UPDATE users SET {sets} WHERE telegram_id = ?",
                (*kwargs.values(), telegram_id),
            )
        else:
            kwargs["telegram_id"] = telegram_id
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" * len(kwargs))
            conn.execute(
                f"INSERT INTO users ({cols}) VALUES ({placeholders})",
                tuple(kwargs.values()),
            )
        conn.commit()

def add_stats(telegram_id: int, sent: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET coins = coins + ?, total_reports = total_reports + ? WHERE telegram_id = ?",
            (sent * 10, sent, telegram_id),
        )
        conn.commit()

def is_premium_active(user: dict) -> bool:
    if not user.get("is_premium"):
        return False
    until = user.get("premium_until")
    if not until:
        return False
    return datetime.now() < datetime.fromisoformat(until)

def set_premium(telegram_id: int, months: int = 1) -> None:
    until = (datetime.now() + timedelta(days=30 * months)).isoformat()
    upsert_user(telegram_id, is_premium=1, premium_until=until)

# ────────────────────────────────────────────────────────────────────────
#  Helpers
# ────────────────────────────────────────────────────────────────────────
def _normalize_phone(phone: str) -> Optional[str]:
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    phone = phone.translate(trans)
    phone = re.sub(r"[^\d+]", "", phone)
    if phone.startswith("+98"):
        phone = "98" + phone[3:]
    elif phone.startswith("0098"):
        phone = "98" + phone[4:]
    elif phone.startswith("0"):
        phone = "98" + phone[1:]
    elif not phone.startswith("98") and len(phone) == 10:
        phone = "98" + phone
    if re.match(r"^98\d{10}$", phone):
        return phone
    return None

SESSION_EXTENSIONS = (".rp", ".rubika", ".session")

def session_exists(session_path: str | None) -> bool:
    if not session_path:
        return False
    return any(os.path.isfile(session_path + ext) for ext in SESSION_EXTENSIONS) or os.path.isfile(session_path)

def cleanup_session_files(session_path: str | None) -> None:
    if not session_path:
        return
    for f in [session_path] + [session_path + ext for ext in SESSION_EXTENSIONS]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

# ────────────────────────────────────────────────────────────────────────
#  Force Join
# ────────────────────────────────────────────────────────────────────────
async def check_user_membership(bot, user_id: int) -> bool:
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
            if member.status in (ChatMember.LEFT, ChatMember.BANNED):
                return False
        except Exception as exc:
            logger.warning("check_member @%s user=%d: %s (بات ادمین کانال نیست؟)", channel, user_id, exc)
            return False
    return True

def kb_force_join() -> InlineKeyboardMarkup:
    buttons = []
    for ch in REQUIRED_CHANNELS:
        buttons.append([InlineKeyboardButton(f"📢 @{ch}", url=f"https://t.me/{ch}")])
    buttons.append([InlineKeyboardButton("✅ بررسی عضویت", style="success", callback_data="check_join")])
    return InlineKeyboardMarkup(buttons)

# ────────────────────────────────────────────────────────────────────────
#  Rubika auth
# ────────────────────────────────────────────────────────────────────────
async def rubika_send_code(phone: str, pass_key: str = None) -> dict:
    tmp_path = os.path.join(SESSIONS_DIR, f"tmp_{phone}")
    client = RubikaClient(name=tmp_path)
    await client.connect()
    try:
        kwargs = {"phone_number": phone, "send_type": "SMS"}
        if pass_key is not None:
            kwargs["pass_key"] = pass_key
        result = await client.send_code(**kwargs)
        return {
            "status": result.status,
            "phone_code_hash": result.phone_code_hash,
            "hint_pass_key": getattr(result, "hint_pass_key", None),
        }
    except Exception as e:
        raise RuntimeError(f"خطا در ارسال کد: {e}")
    finally:
        await client.disconnect()
        cleanup_session_files(tmp_path)

async def rubika_sign_in(phone: str, code: str, phone_code_hash: str) -> dict:
    session_name = f"user_{phone}"
    session_path = os.path.join(SESSIONS_DIR, session_name)
    client = RubikaClient(name=session_path)
    await client.connect()
    try:
        public_key, private_key = Crypto.create_keys()
        result = await client.sign_in(
            phone_code=code,
            phone_number=phone,
            phone_code_hash=phone_code_hash,
            public_key=public_key,
        )

        if result.status != "OK":
            raise RuntimeError(f"خطا در ورود: {result.status}")

        decrypted_auth = Crypto.decrypt_RSA_OAEP(private_key, result.auth)

        client.auth = decrypted_auth
        client.key = Crypto.passphrase(decrypted_auth)
        client.decode_auth = Crypto.decode_auth(decrypted_auth)
        client.import_key = pkcs1_15_sig.new(RSA.import_key(private_key.encode()))
        client.private_key = private_key
        client.guid = result.user.user_guid

        client.session.insert(
            auth=decrypted_auth,
            guid=result.user.user_guid,
            user_agent=client.user_agent,
            phone_number=phone,
            private_key=private_key,
        )

        await client.register_device(device_model=session_name)

        await client.disconnect()

        return {
            "session_path": session_path,
            "auth": decrypted_auth,
            "private_key": private_key,
        }
    except Exception as e:
        await client.disconnect()
        cleanup_session_files(session_path)
        raise RuntimeError(f"خطا در sign_in: {e}")

# ────────────────────────────────────────────────────────────────────────
#  Keyboards
# ────────────────────────────────────────────────────────────────────────
def kb_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 ارسال شماره تماس", request_contact=True, style="primary")]],
        resize_keyboard=True,
    )

def kb_main(premium: bool) -> InlineKeyboardMarkup:
    plan_label = "⭐ ویژه" if premium else "🔹 رایگان"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚨 گزارش تخلف",    style="danger",  callback_data="start_report"),
            InlineKeyboardButton("📊 وضعیت حساب",     style="primary", callback_data="account_status"),
        ],
        [
            InlineKeyboardButton(f"💎 {plan_label}",  style="success", callback_data="subscription"),
            InlineKeyboardButton("🎁 جایزه روزانه",   style="success", callback_data="daily_reward"),
        ],
        [
            InlineKeyboardButton("👥 دعوت دوستان",    style="primary", callback_data="invite"),
            InlineKeyboardButton("❓ راهنما",          style="primary", callback_data="help"),
        ],
    ])

def kb_menu_return() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 بازگشت", style="primary", callback_data="back_menu")],
    ])

def kb_report_types(selected: set[str]) -> InlineKeyboardMarkup:
    styles = {
        "1": "danger", "2": "danger", "3": "success",
        "4": "danger", "5": "primary", "6": "danger", "7": "primary",
    }
    buttons = []
    for k, (label, _) in REPORT_TYPES_MAP.items():
        mark = "✅" if k in selected else "⬜"
        buttons.append([InlineKeyboardButton(f"{mark} {label}", style=styles.get(k, "primary"), callback_data=f"rt_{k}")])
    buttons.append([InlineKeyboardButton("▶️ شروع گزارش", style="success", callback_data="rt_confirm")])
    return InlineKeyboardMarkup(buttons)

# ────────────────────────────────────────────────────────────────────────
#  Conversation handlers
# ────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    tg_id = update.effective_user.id
    bot = ctx.bot
    user  = get_user(tg_id)
    ctx.user_data.clear()

    is_member = await check_user_membership(bot, tg_id)
    if not is_member:
        await update.message.reply_text(
            "⛔ برای استفاده از ربات ابتدا باید در کانال‌های زیر عضو شوید:\n\n"
            "بعد از عضویت، دکمه «بررسی عضویت» را بزنید:",
            reply_markup=kb_force_join(),
        )
        return ConversationHandler.END

    if user and user.get("rubika_session") and session_exists(user["rubika_session"]):
        premium = is_premium_active(user)
        await update.message.reply_text("👋 خوش برگشتی!", reply_markup=kb_main(premium))
        return ST_MAIN_MENU

    await update.message.reply_text(
        "📱 شماره روبیکا (یا دکمه ارسال تماس) را بفرست:\nمثال: 09123456789",
        reply_markup=kb_phone(),
    )
    return ST_PHONE

async def receive_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.contact:
        phone_raw = update.message.contact.phone_number
    else:
        phone_raw = update.message.text.strip()

    normalized = _normalize_phone(phone_raw)
    if not normalized:
        await update.message.reply_text(
            "❌ شماره نامعتبر.\nیک شماره معتبر ایرانی وارد کنید.",
            reply_markup=kb_phone(),
        )
        return ST_PHONE

    ctx.user_data["phone"] = normalized
    await update.message.reply_text("⏳ ارسال کد تایید...", reply_markup=ReplyKeyboardRemove())

    try:
        result = await rubika_send_code(normalized)
        ctx.user_data["phone_code_hash"] = result["phone_code_hash"]

        if result.get("status") == "SendPassKey":
            hint = result.get("hint_pass_key", "ندارد")
            ctx.user_data["needs_password"] = True
            await update.message.reply_text(
                "🔑 این حساب رمز دو مرحله‌ای دارد.\n"
                f"راهنمایی: {hint}\n\n"
                "لطفاً رمز عبور را وارد کنید:"
            )
            return ST_PASSWORD

        ctx.user_data["needs_password"] = False
        await update.message.reply_text(
            "✅ کد تایید ارسال شد!\n"
            "کد ۶ رقمی را از پیامک روبیکا بخوانید و اینجا بفرستید:"
        )
        return ST_CODE

    except Exception as exc:
        logger.error("send_code: %s", exc)
        await update.message.reply_text(f"❌ {exc}\nبا /start دوباره تلاش کن.")
        return ConversationHandler.END

async def receive_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text.strip()
    phone    = ctx.user_data.get("phone", "")

    if not password:
        await update.message.reply_text("❌ رمز نمی‌تواند خالی باشد. دوباره:")
        return ST_PASSWORD

    await update.message.reply_text("⏳ بررسی رمز عبور...")

    try:
        result = await rubika_send_code(phone, pass_key=password)
        ctx.user_data["phone_code_hash"] = result["phone_code_hash"]

        if result.get("status") == "SendPassKey":
            hint = result.get("hint_pass_key", "ندارد")
            await update.message.reply_text(
                f"❌ رمز اشتباه است. راهنمایی: {hint}\nدوباره:"
            )
            return ST_PASSWORD

        await update.message.reply_text("✅ رمز تایید شد!\nکد ۶ رقمی را از پیامک بفرستید:")
        return ST_CODE

    except Exception as exc:
        logger.error("send_code passkey: %s", exc)
        await update.message.reply_text(f"❌ {exc}")
        return ST_PASSWORD

async def receive_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    code  = update.message.text.strip()
    phone = ctx.user_data.get("phone", "")
    phone_code_hash = ctx.user_data.get("phone_code_hash", "")
    tg_id = update.effective_user.id

    code_digits = re.sub(r"\D", "", code)
    if len(code_digits) < 4 or len(code_digits) > 8:
        await update.message.reply_text("❌ کد ۴ تا ۸ رقم باید باشد. دوباره:")
        return ST_CODE

    await update.message.reply_text("⏳ در حال ورود...")

    try:
        login_data = await rubika_sign_in(phone, code_digits, phone_code_hash)

        upsert_user(
            tg_id,
            phone=phone,
            rubika_session=login_data["session_path"],
            rubika_auth=login_data["auth"],
            rubika_private_key=login_data["private_key"],
        )

        await asyncio.sleep(0.5)

        await update.message.reply_text("✅ ورود موفق!", reply_markup=kb_main(False))
        return ST_MAIN_MENU

    except Exception as exc:
        logger.error("sign_in: %s", exc)
        await update.message.reply_text(f"❌ {exc}")
        return ST_CODE

# ────────────────────────────────────────────────────────────────────────
#  Main menu / Callback handler
# ────────────────────────────────────────────────────────────────────────
async def check_join_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tg_id = update.effective_user.id
    bot = ctx.bot

    is_member = await check_user_membership(bot, tg_id)
    if not is_member:
        await query.message.reply_text(
            "❌ هنوز در کانال‌ها عضو نشدید!\n\n"
            "لطفاً ابتدا در هر دو کانال عضو شوید و سپس «بررسی عضویت» را بزنید:",
            reply_markup=kb_force_join(),
        )
        return ConversationHandler.END

    user = get_user(tg_id)
    if user and user.get("rubika_session") and session_exists(user["rubika_session"]):
        premium = is_premium_active(user)
        await query.message.reply_text("✅ عضویت تایید شد! 👋", reply_markup=kb_main(premium))
        return ST_MAIN_MENU

    await query.message.reply_text(
        "✅ عضویت تایید شد!\n\n📱 شماره روبیکا را بفرست:",
        reply_markup=kb_phone(),
    )
    return ST_PHONE

async def back_to_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        tg_id = update.effective_user.id
        user  = get_user(tg_id)
        premium = is_premium_active(user) if user else False
        await query.message.reply_text("🏠 منو:", reply_markup=kb_main(premium))
    return ST_MAIN_MENU

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data  = query.data
    tg_id = update.effective_user.id
    user  = get_user(tg_id)

    if data == "back_menu":
        premium = is_premium_active(user) if user else False
        await query.message.reply_text("🏠 منو:", reply_markup=kb_main(premium))
        return ST_MAIN_MENU

    if data == "start_report":
        if not user or not user.get("rubika_session"):
            await query.message.reply_text("❌ ابتدا /start بزن و وارد حساب روبیکا شو.")
            return ST_MAIN_MENU

        if not session_exists(user["rubika_session"]):
            await query.message.reply_text(
                "❌ سشن منقضی شده. لطفاً /start بزن و دوباره لاگین کن."
            )
            upsert_user(tg_id, rubika_session=None, rubika_auth=None, rubika_private_key=None)
            return ST_MAIN_MENU

        await query.message.reply_text(
            "🎯 شناسه (object_guid) کاربر روبیکا را بفرست:\n\n"
            "مثال: u0A1bC2dE3fG4hI5jK6lM7nO8pQ9rS0T",
            reply_markup=kb_menu_return(),
        )
        return ST_REPORT_GUID

    if data == "account_status":
        if not user:
            await query.message.reply_text("ابتدا /start بزن.")
            return ST_MAIN_MENU
        premium = is_premium_active(user)
        limit   = PREMIUM_LIMIT if premium else FREE_LIMIT
        plan    = "👑 اشتراکی" if premium else "🆓 رایگان"
        await query.message.reply_text(
            f"📊 وضعیت حساب\n\n"
            f"پلن: {plan}\n"
            f"محدودیت: {limit} از هر نوع\n"
            f"🪙 سکه: {user.get('coins', 0)}\n"
            f"📊 گزارشات: {user.get('total_reports', 0)}\n"
            f"📱 شماره: {user.get('phone', '-')}",
            reply_markup=kb_menu_return(),
        )
        return ST_MAIN_MENU

    if data == "subscription":
        premium = is_premium_active(user) if user else False
        if premium:
            await query.message.reply_text(
                f"👑 اشتراک تا {user['premium_until'][:10]} فعال است.",
                reply_markup=kb_menu_return(),
            )
        else:
            await query.message.reply_text(
                f"💎 پلن اشتراکی\n• {PREMIUM_LIMIT} گزارش\n• {PREMIUM_PRICE} در ماه\n\n"
                f"خرید: {ADMIN_USERNAME}",
                reply_markup=kb_menu_return(),
            )
        return ST_MAIN_MENU

    if data == "invite":
        bot_info = await ctx.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{tg_id}"
        await query.message.reply_text(
            f"🎁 لینک دعوت:\n{link}\n\nهر دعوت = ۵۰ سکه",
            reply_markup=kb_menu_return(),
        )
        return ST_MAIN_MENU

    if data == "help":
        await query.message.reply_text(
            "❓ راهنما\n\n"
            "1. شناسه کاربر روبیکا را بفرست\n"
            "2. نوع گزارش را انتخاب کن\n"
            "3. تعداد را بگو\n"
            "4. گزارش‌ها خودکار ارسال می‌شوند\n\n"
            f"🆓 رایگان: {FREE_LIMIT} گزارش\n"
            f"💎 اشتراکی: {PREMIUM_LIMIT} گزارش\n\n"
            "برای توقف /stop",
            reply_markup=kb_menu_return(),
        )
        return ST_MAIN_MENU

    if data == "daily_reward":
        await query.message.reply_text("🎯 به زودی 🔜", reply_markup=kb_menu_return())
        return ST_MAIN_MENU

    if data.startswith("rt_"):
        key = data[3:]

        if key == "confirm":
            selected: set = ctx.user_data.get("selected_types", set())
            if not selected:
                await query.answer("حداقل یک نوع انتخاب کن.", show_alert=True)
                return ST_REPORT_TYPES

            if "7" in selected:
                await query.message.reply_text(
                    "متن گزارش «سایر» را بنویس:",
                    reply_markup=kb_menu_return(),
                )
                return ST_REPORT_OTHER_TEXT

            user_l = get_user(tg_id)
            premium = is_premium_active(user_l) if user_l else False
            limit   = PREMIUM_LIMIT if premium else FREE_LIMIT
            labels  = [REPORT_TYPES_MAP[k][0] for k in selected]
            await query.message.reply_text(
                f"انتخاب: {', '.join(labels)}\n"
                f"چند گزارش از هر نوع؟ (حداکثر {limit})",
                reply_markup=kb_menu_return(),
            )
            return ST_REPORT_COUNT

        selected: set = ctx.user_data.setdefault("selected_types", set())
        if key in selected:
            selected.discard(key)
        else:
            selected.add(key)

        await query.message.edit_reply_markup(reply_markup=kb_report_types(selected))
        return ST_REPORT_TYPES

    return ST_MAIN_MENU

async def receive_guid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    guid  = update.message.text.strip()
    tg_id = update.effective_user.id

    if not re.match(r"^[a-zA-Z0-9_\-]{10,64}$", guid):
        await update.message.reply_text(
            "❌ شناسه نامعتبر. یک object_guid معتبر بفرست.",
            reply_markup=kb_menu_return(),
        )
        return ST_REPORT_GUID

    ctx.user_data["object_guid"]    = guid
    ctx.user_data["selected_types"] = set()

    await update.message.reply_text("نوع گزارش را انتخاب کن:", reply_markup=kb_report_types(set()))
    return ST_REPORT_TYPES

async def receive_other_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text  = update.message.text.strip()
    tg_id = update.effective_user.id
    user  = get_user(tg_id)

    if not text or len(text) < 3:
        await update.message.reply_text("❌ حداقل ۳ کاراکتر بنویس.", reply_markup=kb_menu_return())
        return ST_REPORT_OTHER_TEXT

    ctx.user_data["other_report_text"] = text
    premium = is_premium_active(user) if user else False
    limit   = PREMIUM_LIMIT if premium else FREE_LIMIT
    selected: set = ctx.user_data.get("selected_types", set())
    labels = [REPORT_TYPES_MAP[k][0] for k in selected]

    await update.message.reply_text(
        f"انتخاب: {', '.join(labels)}\nچند گزارش از هر نوع؟ (حداکثر {limit})",
        reply_markup=kb_menu_return(),
    )
    return ST_REPORT_COUNT

async def receive_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text  = update.message.text.strip()
    tg_id = update.effective_user.id
    user  = get_user(tg_id)

    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ عدد مثبت وارد کن.", reply_markup=kb_menu_return())
        return ST_REPORT_COUNT

    count   = int(text)
    premium = is_premium_active(user) if user else False
    limit   = PREMIUM_LIMIT if premium else FREE_LIMIT

    if count > limit:
        msg = f"❌ محدودیت پلن: {limit} گزارش."
        if not premium:
            msg += f"\n💎 اشتراک: {ADMIN_USERNAME}"
        await update.message.reply_text(msg + "\nعدد کمتر:", reply_markup=kb_menu_return())
        return ST_REPORT_COUNT

    object_guid = ctx.user_data.get("object_guid", "")
    selected    = ctx.user_data.get("selected_types", set())
    other_text  = ctx.user_data.get("other_report_text", "")

    if not selected:
        await update.message.reply_text("❌ حداقل یک نوع گزارش انتخاب کن.", reply_markup=kb_menu_return())
        return ST_MAIN_MENU

    if not object_guid:
        await update.message.reply_text("❌ شناسه کاربر یافت نشد. /start بزن.", reply_markup=kb_menu_return())
        return ST_MAIN_MENU

    if not user or not user.get("rubika_session"):
        await update.message.reply_text("❌ سشن معتبر نیست. /start بزن.", reply_markup=kb_menu_return())
        return ST_MAIN_MENU

    session_path = user["rubika_session"]

    if not session_exists(session_path):
        await update.message.reply_text("❌ سشن منقضی شده. /start بزن.", reply_markup=kb_menu_return())
        upsert_user(tg_id, rubika_session=None, rubika_auth=None, rubika_private_key=None)
        return ST_MAIN_MENU

    selected_types = [
        (REPORT_TYPES_MAP[k][0], REPORT_TYPES_MAP[k][1], other_text if k == "7" else "")
        for k in selected
    ]

    ctx.user_data.pop("object_guid", None)
    ctx.user_data.pop("selected_types", None)
    ctx.user_data.pop("other_report_text", None)
    ctx.user_data["stop_flag"] = False

    await update.message.reply_text(
        f"🚀 ارسال {count} گزارش برای {len(selected_types)} نوع...\n⛔ /stop برای توقف"
    )

    asyncio.create_task(
        _pipeline(
            tg_id=tg_id,
            session_path=session_path,
            object_guid=object_guid,
            selected_types=selected_types,
            count=count,
            status_message=update.message,
            ctx=ctx,
        )
    )
    return ST_MAIN_MENU

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["stop_flag"] = True
    await update.message.reply_text("⛔ توقف")

# ────────────────────────────────────────────────────────────────────────
#  Pipeline
# ────────────────────────────────────────────────────────────────────────
async def _pipeline(
    tg_id: int,
    session_path: str,
    object_guid: str,
    selected_types: list[tuple[str, ReportType, str]],
    count: int,
    status_message,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    async def reply(msg: str) -> None:
        try:
            await status_message.reply_text(msg)
        except Exception:
            pass

    try:
        client = RubikaClient(name=session_path)
        async with client:
            tasks = [
                asyncio.create_task(
                    _single_loop(client, object_guid, rt_enum, other_text, count, label, ctx)
                )
                for label, rt_enum, other_text in selected_types
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:
        logger.error("pipeline error: %s", exc)
        await reply(f"❌ خطا: {exc}")
        return

    total_sent = 0
    lines = []
    for r in results:
        if isinstance(r, Exception):
            lines.append(f"❌ خطا: {r}")
        else:
            label, sent, failed = r
            total_sent += sent
            lines.append(f"✅ {label}: {sent} ارسال | {failed} ناموفق")

    add_stats(tg_id, total_sent)

    user    = get_user(tg_id)
    premium = is_premium_active(user) if user else False

    await reply("📊 گزارش:\n" + "\n".join(lines))
    await reply(f"🪙 +{total_sent * 10} سکه")

    try:
        await status_message.reply_text("🔵 منو:", reply_markup=kb_main(premium))
    except Exception:
        pass

async def _single_loop(
    client: RubikaClient,
    object_guid: str,
    report_type_enum: ReportType,
    other_text: str,
    count: int,
    label: str,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> tuple[str, int, int]:
    sent   = 0
    failed = 0
    for i in range(1, count + 1):
        if ctx.user_data.get("stop_flag"):
            break
        try:
            if report_type_enum == ReportType.OTHER:
                await client.report_object(object_guid, report_type_enum, description=other_text)
            else:
                await client.report_object(object_guid, report_type_enum)
            sent += 1
        except Exception as exc:
            failed += 1
            logger.error("[%s][%d/%d] %s", label, i, count, exc)
        if i < count:
            await asyncio.sleep(REPORT_DELAY)
    return label, sent, failed

# ────────────────────────────────────────────────────────────────────────
#  Admin
# ────────────────────────────────────────────────────────────────────────
async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = ctx.args or []
    if not args:
        await update.message.reply_text("/grant <telegram_id> [months]")
        return
    try:
        target_id = int(args[0])
        months    = int(args[1]) if len(args) > 1 else 1
        set_premium(target_id, months)
        await update.message.reply_text(f"✅ اشتراک {months} ماهه برای {target_id}")
    except (ValueError, IndexError):
        await update.message.reply_text("خطا در آرگومان‌ها.")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        prem  = conn.execute("SELECT COUNT(*) FROM users WHERE is_premium=1").fetchone()[0]
        reps  = conn.execute("SELECT SUM(total_reports) FROM users").fetchone()[0] or 0
    await update.message.reply_text(f"📊 آمار\n👥 {total}\n👑 {prem}\n📢 {reps}")

# ────────────────────────────────────────────────────────────────────────
#  Main
# ────────────────────────────────────────────────────────────────────────
def build_app() -> Application:
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ST_PHONE: [
                MessageHandler(filters.CONTACT, receive_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone),
            ],
            ST_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
            ST_CODE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code)],
            ST_MAIN_MENU: [
                CallbackQueryHandler(callback_handler),
                CommandHandler("stop", cmd_stop),
            ],
            ST_REPORT_GUID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_guid),
                CallbackQueryHandler(back_to_menu, pattern="^back_menu$"),
            ],
            ST_REPORT_TYPES: [CallbackQueryHandler(callback_handler)],
            ST_REPORT_OTHER_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_other_text),
                CallbackQueryHandler(back_to_menu, pattern="^back_menu$"),
            ],
            ST_REPORT_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_count),
                CallbackQueryHandler(back_to_menu, pattern="^back_menu$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CommandHandler("stop", cmd_stop),
            CallbackQueryHandler(check_join_callback, pattern="^check_join$"),
        ],
        per_user=True,
        per_chat=True,
        name="rubika_reporter",
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("stats", cmd_stats))
    return app

def main() -> None:
    app = build_app()
    logger.warning("[POLLING] starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
