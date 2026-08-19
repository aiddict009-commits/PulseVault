import asyncio
import time
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
import aiosqlite
from dotenv import load_dotenv
import os

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

HOURLY_RATE = 0.15
MAX_OFFLINE_HOURS = 16
MIN_WITHDRAW = 5.0
REFERRAL_BONUS = 1.0

DB_PATH = Path("pulsevault.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance REAL DEFAULT 0,
                last_claim INTEGER,
                referral_id INTEGER,
                total_earned REAL DEFAULT 0,
                created_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT,
                amount REAL,
                status TEXT DEFAULT 'completed',
                note TEXT,
                created_at INTEGER
            )
        """)
        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, username, balance, last_claim, referral_id, total_earned FROM users WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "user_id": row[0],
                    "username": row[1],
                    "balance": row[2],
                    "last_claim": row[3],
                    "referral_id": row[4],
                    "total_earned": row[5],
                }
            return None


async def create_user(user_id: int, username: str | None, referral_id: int | None = None):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, balance, last_claim, referral_id, total_earned, created_at) VALUES (?, ?, 0, ?, ?, 0, ?)",
            (user_id, username, now, referral_id, now)
        )
        await db.commit()


async def update_balance(user_id: int, new_balance: float, total_earned: float | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if total_earned is not None:
            await db.execute(
                "UPDATE users SET balance = ?, total_earned = ? WHERE user_id = ?",
                (new_balance, total_earned, user_id)
            )
        else:
            await db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (new_balance, user_id)
            )
        await db.commit()


async def update_last_claim(user_id: int, ts: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_claim = ? WHERE user_id = ?", (ts, user_id))
        await db.commit()


async def add_transaction(user_id: int, type_: str, amount: float, status: str = "completed", note: str = ""):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO transactions (user_id, type, amount, status, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, type_, amount, status, note, now)
        )
        await db.commit()


OFFERWALL_URL = "https://your-offerwall-link-here.com"  # replace with your MyLead/CPX/ToroX/Monlix link

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Claim Pulse", callback_data="claim")],
        [InlineKeyboardButton(text="🎯 Tasks & Offers", callback_data="tasks")],
        [
            InlineKeyboardButton(text="💰 Balance", callback_data="balance"),
            InlineKeyboardButton(text="👥 Invite", callback_data="invite")
        ],
        [
            InlineKeyboardButton(text="📊 Stats", callback_data="stats"),
            InlineKeyboardButton(text="💸 Withdraw", callback_data="withdraw")
        ],
        [InlineKeyboardButton(text="ℹ️ How it works", callback_data="info")]
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="menu")]
    ])


async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username

    referral_id = None
    if message.text and len(message.text.split()) > 1:
        try:
            ref = int(message.text.split()[1])
            if ref != user_id:
                referral_id = ref
        except ValueError:
            pass

    user = await get_user(user_id)
    if not user:
        await create_user(user_id, username, referral_id)
        if referral_id:
            ref_user = await get_user(referral_id)
            if ref_user:
                await update_balance(user_id, REFERRAL_BONUS, REFERRAL_BONUS)
                await add_transaction(user_id, "referral", REFERRAL_BONUS, note=f"from {referral_id}")

                ref_balance = ref_user["balance"] + REFERRAL_BONUS
                ref_total = ref_user["total_earned"] + REFERRAL_BONUS
                await update_balance(referral_id, ref_balance, ref_total)
                await add_transaction(referral_id, "referral", REFERRAL_BONUS, note=f"invited {user_id}")

                try:
                    await message.bot.send_message(
                        referral_id,
                        f"🎉 Someone joined with your link!\n+{REFERRAL_BONUS:.2f} Pulse added to your balance."
                    )
                except Exception:
                    pass

        text = (
            "🔐 <b>Welcome to PulseVault</b>\n\n"
            "Earn Pulse by claiming hourly, completing quick tasks, and inviting friends.\n\n"
            f"You received a welcome bonus of <b>{REFERRAL_BONUS:.2f} Pulse</b> to get started.\n\n"
            "Tap <b>Tasks &amp; Offers</b> for the fastest way to earn, or use <b>Claim Pulse</b> to collect what's building up automatically."
        )
    else:
        text = (
            "🔐 <b>Welcome back to PulseVault</b>\n\n"
            "Your Pulse is still building up.\n"
            "Claim it, check out Tasks &amp; Offers for bigger rewards, or check your balance below."
        )

    await message.answer(text, reply_markup=main_menu(), parse_mode=ParseMode.HTML)


async def show_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🔐 <b>PulseVault</b>\n\nChoose an action:",
        reply_markup=main_menu(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


async def claim_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await get_user(user_id)
    if not user:
        await create_user(user_id, callback.from_user.username)
        user = await get_user(user_id)

    now = int(time.time())
    last = user["last_claim"] or now
    hours = min((now - last) / 3600, MAX_OFFLINE_HOURS)
    earned = round(hours * HOURLY_RATE, 4)

    if earned < 0.01:
        await callback.answer("Too soon! Come back later ⏳", show_alert=True)
        return

    new_balance = round(user["balance"] + earned, 4)
    new_total = round(user["total_earned"] + earned, 4)

    await update_balance(user_id, new_balance, new_total)
    await update_last_claim(user_id, now)
    await add_transaction(user_id, "claim", earned)

    text = (
        f"⚡ <b>Claimed!</b>\n\n"
        f"You earned <b>+{earned:.4f} Pulse</b>\n"
        f"New balance: <b>{new_balance:.4f} Pulse</b>\n\n"
        f"Max offline accrual: {MAX_OFFLINE_HOURS}h"
    )
    await callback.message.edit_text(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)
    await callback.answer("Claim successful!")


async def balance_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    now = int(time.time())
    last = user["last_claim"] or now
    hours = min((now - last) / 3600, MAX_OFFLINE_HOURS)
    pending = round(hours * HOURLY_RATE, 4)

    text = (
        f"💰 <b>Your Balance</b>\n\n"
        f"Available: <b>{user['balance']:.4f} Pulse</b>\n"
        f"Pending: <b>{pending:.4f} Pulse</b>\n"
        f"Total earned: <b>{user['total_earned']:.4f} Pulse</b>"
    )
    await callback.message.edit_text(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)
    await callback.answer()


async def invite_handler(callback: CallbackQuery):
    bot_info = await callback.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"

    text = (
        "👥 <b>Invite & Earn</b>\n\n"
        f"Your link:\n<code>{link}</code>\n\n"
        f"Both get <b>{REFERRAL_BONUS:.2f} Pulse</b> when someone joins."
    )
    await callback.message.edit_text(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)
    await callback.answer()


async def stats_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    text = (
        f"📊 <b>Stats</b>\n\n"
        f"ID: <code>{user['user_id']}</code>\n"
        f"Balance: {user['balance']:.4f}\n"
        f"Total earned: {user['total_earned']:.4f}"
    )
    await callback.message.edit_text(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)
    await callback.answer()


async def withdraw_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    if user["balance"] < MIN_WITHDRAW:
        await callback.answer(f"Minimum is {MIN_WITHDRAW} Pulse", show_alert=True)
        return

    text = (
        f"💸 <b>Withdraw</b>\n\n"
        f"Balance: <b>{user['balance']:.4f} Pulse</b>\n\n"
        "Withdrawals are manual at launch.\n"
        "Send your TON/USDT (TON) address."
    )
    await callback.message.edit_text(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)
    await callback.answer()


async def tasks_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Open Tasks & Offers", url=f"{OFFERWALL_URL}?uid={callback.from_user.id}")],
        [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="menu")]
    ])

    text = (
        "🎯 <b>Tasks & Offers</b>\n\n"
        "Complete surveys, app trials, or quick offers from our partners "
        "to earn Pulse a lot faster than claiming alone.\n\n"
        "These are sponsored by advertisers — that's literally what "
        "funds real Pulse payouts, so the more you engage here, "
        "the more sustainable withdrawals stay for everyone.\n\n"
        "Rewards post automatically once an offer is verified complete "
        "(usually within a few minutes, sometimes longer for surveys)."
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    await callback.answer()

async def info_handler(callback: CallbackQuery):
    text = (
        "ℹ️ <b>How it works</b>\n\n"
        f"• Pulse builds up at {HOURLY_RATE}/hour, capped at {MAX_OFFLINE_HOURS}h — tap Claim to collect it\n"
        "• Tasks &amp; Offers pay the most: complete a sponsored survey or offer "
        "and earn Pulse once it's verified\n"
        f"• Invite friends — you both get {REFERRAL_BONUS:.2f} Pulse\n"
        f"• Withdraw once you reach {MIN_WITHDRAW} Pulse\n\n"
        "<i>Task and offer rewards are funded by our advertising partners — "
        "that's the actual source of the Pulse you can withdraw.</i>"
    )
    await callback.message.edit_text(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)
    await callback.answer()


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.message.register(cmd_start, CommandStart())
    dp.callback_query.register(show_menu, F.data == "menu")
    dp.callback_query.register(claim_handler, F.data == "claim")
    dp.callback_query.register(tasks_handler, F.data == "tasks")
    dp.callback_query.register(balance_handler, F.data == "balance")
    dp.callback_query.register(invite_handler, F.data == "invite")
    dp.callback_query.register(stats_handler, F.data == "stats")
    dp.callback_query.register(withdraw_handler, F.data == "withdraw")
    dp.callback_query.register(info_handler, F.data == "info")

    logger.info("PulseVault bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
