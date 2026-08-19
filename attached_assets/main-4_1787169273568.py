import asyncio
import time
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
import aiosqlite
from dotenv import load_dotenv
import os

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

MIN_WITHDRAW_FIRST = 5.0
MIN_WITHDRAW_STANDARD = 10.0
PULSE_TO_TON_RATE = 0.1  # 5 Pulse = 0.5 TON, 10 Pulse = 1 TON
REFERRAL_BONUS = 1.0

# Vaults: idle claiming is a bonus on top of tasks/ads/offers, never a substitute for them.
# Level 1 pays 0/h on purpose — idle users must engage with Tasks & Offers to earn anything.
VAULT_LEVELS = {
    1: {"name": "Starter", "rate": 0.00, "cap_hours": 0},
    2: {"name": "Grinder", "rate": 0.04, "cap_hours": 16},
    3: {"name": "Hunter",  "rate": 0.08, "cap_hours": 20},
    4: {"name": "Vaulted", "rate": 0.12, "cap_hours": 22},
    5: {"name": "Max",     "rate": 0.15, "cap_hours": 24},
}

# Cumulative tasks_completed needed to reach a level via the free/task-only path.
# (Only L2 and L3 have a task-only path; L4/L5 are Stars-only.)
LEVEL_UNLOCK_TASKS = {2: 10, 3: 50}

# Cumulative Telegram Stars spent on the vault needed to reach a level via the paid path.
# L3 can be reached by EITHER path (tasks OR stars); L4/L5 require stars.
LEVEL_UNLOCK_STARS = {3: 150, 4: 250, 5: 350}

# Withdrawal gate: required lifetime completions before ANY withdrawal unlocks,
# regardless of vault level or balance.
WITHDRAW_GATE_TASKS = 15
WITHDRAW_GATE_ADS = 5
WITHDRAW_GATE_OFFERS = 1

# Placeholder per-action rewards for the admin test-credit command below.
# Real amounts should come from your offerwall's postback payload once that's wired up.
DEFAULT_TASK_REWARD = 0.05
DEFAULT_AD_REWARD = 0.02
DEFAULT_OFFER_REWARD = 0.20

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
                vault_level INTEGER DEFAULT 1,
                tasks_completed INTEGER DEFAULT 0,
                ads_completed INTEGER DEFAULT 0,
                offers_completed INTEGER DEFAULT 0,
                stars_spent INTEGER DEFAULT 0,
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


        for column_sql in (
            "ALTER TABLE users ADD COLUMN vault_level INTEGER DEFAULT 1",
            "ALTER TABLE users ADD COLUMN tasks_completed INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN ads_completed INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN offers_completed INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN stars_spent INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN withdraw_state TEXT",
            "ALTER TABLE users ADD COLUMN pending_withdraw_id INTEGER",
        ):
            try:
                await db.execute(column_sql)
                await db.commit()
            except Exception:
                pass  # column already exists

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT user_id, username, balance, last_claim, referral_id, total_earned,
                      vault_level, tasks_completed, ads_completed, offers_completed, stars_spent,
                      withdraw_state, pending_withdraw_id
               FROM users WHERE user_id = ?""",
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
                    "vault_level": row[6] or 1,
                    "tasks_completed": row[7] or 0,
                    "ads_completed": row[8] or 0,
                    "offers_completed": row[9] or 0,
                    "stars_spent": row[10] or 0,
                    "withdraw_state": row[11],
                    "pending_withdraw_id": row[12],
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

async def count_withdrawals(user_id: int) -> int:
    """Count of COMPLETED withdrawals only — used to decide first- vs standard-minimum."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM transactions WHERE user_id = ? AND type = 'withdraw' AND status = 'completed'",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def create_pending_withdrawal(user_id: int, amount: float) -> int | None:
    """Reserve the withdrawal amount immediately (deduct from balance) and open a tracking
    transaction awaiting the user's payout address. Returns the new transaction id, or None
    if the balance check failed (e.g. a concurrent request already reserved it)."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ? AND withdraw_state IS NULL",
            (amount, user_id, amount)
        )
        if cursor.rowcount == 0:
            await db.rollback()
            return None
        tx_cursor = await db.execute(
            "INSERT INTO transactions (user_id, type, amount, status, note, created_at) VALUES (?, 'withdraw', ?, 'awaiting_address', '', ?)",
            (user_id, amount, now)
        )
        tx_id = tx_cursor.lastrowid
        await db.execute(
            "UPDATE users SET withdraw_state = 'awaiting_address', pending_withdraw_id = ? WHERE user_id = ?",
            (tx_id, user_id)
        )
        await db.commit()
        return tx_id


async def get_transaction(tx_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_id, type, amount, status, note FROM transactions WHERE id = ?",
            (tx_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"id": row[0], "user_id": row[1], "type": row[2], "amount": row[3], "status": row[4], "note": row[5]}
            return None


async def finalize_withdrawal_address(user_id: int, tx_id: int, address: str):
    """Attach the submitted address and move the withdrawal to 'pending' (awaiting admin review)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE transactions SET status = 'pending', note = ? WHERE id = ? AND user_id = ?",
            (address, tx_id, user_id)
        )
        await db.execute(
            "UPDATE users SET withdraw_state = NULL, pending_withdraw_id = NULL WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()


async def cancel_pending_withdrawal(user_id: int, tx_id: int):
    """Refund a withdrawal that never got an address (or that the user cancelled)."""
    tx = await get_transaction(tx_id)
    if not tx or tx["user_id"] != user_id or tx["status"] not in ("awaiting_address",):
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE transactions SET status = 'rejected', note = 'cancelled by user' WHERE id = ?", (tx_id,))
        await db.execute(
            "UPDATE users SET balance = balance + ?, withdraw_state = NULL, pending_withdraw_id = NULL WHERE user_id = ?",
            (tx["amount"], user_id)
        )
        await db.commit()
    return True


async def list_pending_withdrawals(limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT t.id, t.user_id, u.username, t.amount, t.note, t.created_at
               FROM transactions t LEFT JOIN users u ON u.user_id = t.user_id
               WHERE t.type = 'withdraw' AND t.status = 'pending'
               ORDER BY t.created_at ASC LIMIT ?""",
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {"id": r[0], "user_id": r[1], "username": r[2], "amount": r[3], "address": r[4], "created_at": r[5]}
                for r in rows
            ]


async def approve_withdrawal(tx_id: int):
    tx = await get_transaction(tx_id)
    if not tx or tx["type"] != "withdraw" or tx["status"] != "pending":
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE transactions SET status = 'completed' WHERE id = ?", (tx_id,))
        await db.commit()
    return tx


async def reject_withdrawal(tx_id: int, reason: str = ""):
    tx = await get_transaction(tx_id)
    if not tx or tx["type"] != "withdraw" or tx["status"] != "pending":
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE transactions SET status = 'rejected', note = ? WHERE id = ?",
            (f"{tx['note']} | rejected: {reason}" if reason else tx["note"], tx_id)
        )
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (tx["amount"], tx["user_id"])
        )
        await db.commit()
    return tx

def highest_unlocked_level(tasks_completed: int, stars_spent: int) -> int:
    """Highest vault level reachable given lifetime tasks and Stars spent on the vault."""
    level = 1
    for lvl in sorted(VAULT_LEVELS):
        if lvl == 1:
            continue
        task_ok = tasks_completed >= LEVEL_UNLOCK_TASKS.get(lvl, float("inf"))
        stars_ok = stars_spent >= LEVEL_UNLOCK_STARS.get(lvl, float("inf"))
        if task_ok or stars_ok:
            level = lvl
    return level


async def set_vault_level(user_id: int, level: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET vault_level = ? WHERE user_id = ?", (level, user_id))
        await db.commit()


async def apply_level_up(user_id: int, user: dict) -> int | None:
    """Recompute vault level from current progress; persist and return the new level if it rose."""
    eligible = highest_unlocked_level(user["tasks_completed"], user["stars_spent"])
    if eligible > user["vault_level"]:
        await set_vault_level(user_id, eligible)
        return eligible
    return None


async def add_stars_spent(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET stars_spent = stars_spent + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()


async def credit_action(user_id: int, action_type: str, reward: float):
    """Credit a completed task/ad/offer: bump its counter, pay the reward, and check for a level-up.

    action_type is one of 'task', 'ad', 'offer'. In production this should be called from your
    offerwall's postback/webhook handler (not present in this polling-only bot) rather than the
    admin test command below.
    """
    column = {"task": "tasks_completed", "ad": "ads_completed", "offer": "offers_completed"}.get(action_type)
    if column is None:
        raise ValueError(f"Unknown action_type: {action_type}")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE users SET {column} = {column} + 1, balance = balance + ?, total_earned = total_earned + ? "
            f"WHERE user_id = ?",
            (reward, reward, user_id)
        )
        await db.commit()

    await add_transaction(user_id, action_type, reward, note="credited")

    user = await get_user(user_id)
    return await apply_level_up(user_id, user)

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
        [InlineKeyboardButton(text="🔐 Vault Levels", callback_data="vault_menu")],
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

    level = VAULT_LEVELS[user["vault_level"]]

    if level["rate"] <= 0:
        await callback.answer(
            "Starter vault doesn't idle-earn — complete Tasks & Offers to unlock claiming 🎯",
            show_alert=True
        )
        return

    now = int(time.time())
    last = user["last_claim"] or now
    hours = min((now - last) / 3600, level["cap_hours"])
    earned = round(hours * level["rate"], 4)

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
        f"Vault: L{user['vault_level']} {level['name']} • {level['rate']}/h • {level['cap_hours']}h cap"
    )
    await callback.message.edit_text(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)
    await callback.answer("Claim successful!")


async def balance_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    level = VAULT_LEVELS[user["vault_level"]]
    now = int(time.time())
    last = user["last_claim"] or now
    hours = min((now - last) / 3600, level["cap_hours"])
    pending = round(hours * level["rate"], 4)

    text = (
        f"💰 <b>Your Balance</b>\n\n"
        f"Available: <b>{user['balance']:.4f} Pulse</b>\n"
        f"Unclaimed: <b>{pending:.4f} Pulse</b>\n"
        f"Total earned: <b>{user['total_earned']:.4f} Pulse</b>\n\n"
        f"Vault: L{user['vault_level']} {level['name']} ({level['rate']}/h, {level['cap_hours']}h cap)"
    )
    if user["withdraw_state"] == "awaiting_address":
        text += "\n\n⏳ You have a withdrawal reserved and awaiting your payout address."
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

    level = VAULT_LEVELS[user["vault_level"]]
    text = (
        f"📊 <b>Stats</b>\n\n"
        f"ID: <code>{user['user_id']}</code>\n"
        f"Balance: {user['balance']:.4f}\n"
        f"Total earned: {user['total_earned']:.4f}\n"
        f"Vault: L{user['vault_level']} {level['name']}\n\n"
        f"Tasks completed: {user['tasks_completed']} (withdraw needs {WITHDRAW_GATE_TASKS})\n"
        f"Ads watched: {user['ads_completed']} (withdraw needs {WITHDRAW_GATE_ADS})\n"
        f"Offers completed: {user['offers_completed']} (withdraw needs {WITHDRAW_GATE_OFFERS})\n"
        f"Stars spent on vault: {user['stars_spent']}"
    )
    await callback.message.edit_text(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)
    await callback.answer()


async def withdraw_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    # Withdrawal gate: must complete a minimum spread of tasks/ads/offers before ANY withdraw unlocks.
    tasks_left = max(0, WITHDRAW_GATE_TASKS - user["tasks_completed"])
    ads_left = max(0, WITHDRAW_GATE_ADS - user["ads_completed"])
    offers_left = max(0, WITHDRAW_GATE_OFFERS - user["offers_completed"])

    if tasks_left or ads_left or offers_left:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Open Tasks & Offers", callback_data="tasks")],
            [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="menu")]
        ])
        lines = ["💸 <b>Withdraw locked</b>\n", "Complete these first to unlock withdrawals:\n"]
        lines.append(f"{'✅' if not tasks_left else '❌'} Tasks: {user['tasks_completed']}/{WITHDRAW_GATE_TASKS}")
        lines.append(f"{'✅' if not ads_left else '❌'} Ads: {user['ads_completed']}/{WITHDRAW_GATE_ADS}")
        lines.append(f"{'✅' if not offers_left else '❌'} Offers: {user['offers_completed']}/{WITHDRAW_GATE_OFFERS}")
        text = "\n".join(lines)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        await callback.answer()
        return

    if user["withdraw_state"] == "awaiting_address":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel request", callback_data="withdraw_cancel")],
            [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="menu")]
        ])
        await callback.message.edit_text(
            "💸 <b>Withdraw</b>\n\nYou already have a withdrawal awaiting an address. "
            "Reply with your TON wallet address, or cancel below.",
            reply_markup=keyboard, parse_mode=ParseMode.HTML
        )
        await callback.answer()
        return

    withdrawals_done = await count_withdrawals(user["user_id"])
    is_first = withdrawals_done == 0
    min_required = MIN_WITHDRAW_FIRST if is_first else MIN_WITHDRAW_STANDARD

    if user["balance"] < min_required:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Claim Pulse", callback_data="claim")],
            [InlineKeyboardButton(text="🎯 Tasks & Offers", callback_data="tasks")],
            [InlineKeyboardButton(text="🔓 Vault Levels", callback_data="vault_menu")],
            [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="menu")]
        ])
        text = (
            f"💸 <b>Withdraw</b>\n\n"
            f"Balance: <b>{user['balance']:.4f} Pulse</b>\n\n"
            f"The minimum {'first ' if is_first else ''}withdrawal is "
            f"{min_required:.0f} Pulse (≈{min_required * PULSE_TO_TON_RATE:.2f} TON).\n"
            f"Keep claiming, complete Tasks & Offers, or raise your Vault level to earn faster."
        )
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        await callback.answer()
        return

    amount = round(user["balance"], 4)
    tx_id = await create_pending_withdrawal(user["user_id"], amount)
    if tx_id is None:
        await callback.answer("Balance changed — please try again", show_alert=True)
        return

    ton_amount = round(amount * PULSE_TO_TON_RATE, 4)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel request", callback_data="withdraw_cancel")]
    ])
    text = (
        f"💸 <b>Withdraw</b>\n\n"
        f"Reserved: <b>{amount:.4f} Pulse</b> (≈{ton_amount:.4f} TON)\n\n"
        "Withdrawals are manual at launch.\n"
        "Reply to this chat with your TON wallet address to submit the request for review."
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    await callback.answer()


async def withdraw_cancel_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user["pending_withdraw_id"]:
        await callback.answer("Nothing to cancel", show_alert=True)
        return

    ok = await cancel_pending_withdrawal(user["user_id"], user["pending_withdraw_id"])
    if ok:
        await callback.message.edit_text(
            "💸 Withdrawal request cancelled — the balance has been returned.",
            reply_markup=back_menu(), parse_mode=ParseMode.HTML
        )
    else:
        await callback.answer("Could not cancel (may already be submitted)", show_alert=True)
        return
    await callback.answer()


async def withdraw_address_handler(message: Message):
    """Catches a plain-text reply from a user with an open withdrawal awaiting an address.
    No-ops for anyone not in that state, so it's safe to register as a catch-all."""
    user = await get_user(message.from_user.id)
    if not user or user["withdraw_state"] != "awaiting_address" or not user["pending_withdraw_id"]:
        return

    address = (message.text or "").strip()
    if len(address) < 10 or len(address) > 128:
        await message.answer("That doesn't look like a valid TON address — please send it again.")
        return

    tx_id = user["pending_withdraw_id"]
    await finalize_withdrawal_address(user["user_id"], tx_id, address)

    await message.answer(
        f"✅ <b>Withdrawal request submitted</b>\n\n"
        f"Request #{tx_id} is now pending manual review. You'll be notified once it's processed.",
        reply_markup=back_menu(), parse_mode=ParseMode.HTML
    )

    if ADMIN_ID:
        try:
            await message.bot.send_message(
                ADMIN_ID,
                f"💸 New withdrawal request #{tx_id}\n"
                f"User: {user['user_id']} (@{user['username']})\n"
                f"Address: <code>{address}</code>\n\n"
                f"Approve: /approve {tx_id}\nReject: /reject {tx_id} [reason]",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

async def vault_menu_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    current = user["vault_level"]
    lines = ["🔐 <b>Vault Levels</b>\n", "<i>Idle claiming is a bonus on top of tasks — not a substitute for them.</i>\n"]
    buttons = []

    for lvl in sorted(VAULT_LEVELS):
        info = VAULT_LEVELS[lvl]
        marker = "👉" if lvl == current else ("✅" if lvl < current else "🔒")
        lines.append(f"{marker} <b>L{lvl} {info['name']}</b> — {info['rate']}/h, {info['cap_hours']}h cap")
        if lvl in LEVEL_UNLOCK_TASKS or lvl in LEVEL_UNLOCK_STARS:
            reqs = []
            if lvl in LEVEL_UNLOCK_TASKS:
                reqs.append(f"{LEVEL_UNLOCK_TASKS[lvl]} tasks")
            if lvl in LEVEL_UNLOCK_STARS:
                reqs.append(f"{LEVEL_UNLOCK_STARS[lvl]} ⭐ total")
            lines.append(f"    Unlock: {' OR '.join(reqs)}")

    next_level = current + 1 if current < max(VAULT_LEVELS) else None
    if next_level and next_level in LEVEL_UNLOCK_STARS:
        stars_needed = max(0, LEVEL_UNLOCK_STARS[next_level] - user["stars_spent"])
        if stars_needed > 0:
            buttons.append([InlineKeyboardButton(
                text=f"⭐ Buy {stars_needed} Stars → unlock L{next_level}",
                callback_data=f"vault_buy:{next_level}"
            )])

    buttons.append([InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="menu")])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


async def vault_buy_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    target_level = int(callback.data.split(":")[1])
    threshold = LEVEL_UNLOCK_STARS.get(target_level)
    if threshold is None or target_level <= user["vault_level"]:
        await callback.answer("Not available", show_alert=True)
        return

    stars_needed = max(0, threshold - user["stars_spent"])
    if stars_needed <= 0:
        await callback.answer("Already eligible — reopen Vault Levels", show_alert=True)
        return

    level_info = VAULT_LEVELS[target_level]
    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"PulseVault LVL {target_level} - {level_info['name']}",
        description=f"{level_info['rate']}/h, {level_info['cap_hours']}h cap. Adds {stars_needed} Stars to your vault total.",
        payload=f"vault_stars:{target_level}",
        currency="XTR",
        prices=[LabeledPrice(label=f"Vault LVL {target_level}", amount=stars_needed)]
    )
    await callback.answer()


async def precheckout_handler(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


async def successful_payment_handler(message: Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("vault_stars:"):
        user_id = message.from_user.id
        amount = message.successful_payment.total_amount  # actual Stars charged, source of truth

        await add_stars_spent(user_id, amount)
        await add_transaction(user_id, "vault_stars", amount, note="stars payment")

        user = await get_user(user_id)
        new_level = await apply_level_up(user_id, user)

        if new_level:
            info = VAULT_LEVELS[new_level]
            text = (
                f"🔓 <b>Vault Level Up!</b>\n\n"
                f"You're now <b>L{new_level} {info['name']}</b> — {info['rate']}/h, {info['cap_hours']}h cap."
            )
        else:
            text = (
                f"⭐ <b>{amount} Stars added</b> to your vault total "
                f"({user['stars_spent'] + amount} spent so far)."
            )
        await message.answer(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)


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
        "• Vaults have 5 levels (L1–L5). L1 earns nothing idle — Tasks &amp; Offers "
        "are how you level up and how everyone earns fastest\n"
        "• L2–L5 unlock passive claiming (0.04–0.15/h) via task counts or Stars — see 🔐 Vault Levels\n"
        f"• Invite friends — you both get {REFERRAL_BONUS:.2f} Pulse\n"
        f"• Withdrawals unlock after {WITHDRAW_GATE_TASKS} tasks, {WITHDRAW_GATE_ADS} ads, "
        f"and {WITHDRAW_GATE_OFFERS} offer completed\n"
        f"• First withdrawal minimum: {MIN_WITHDRAW_FIRST:.0f} Pulse (≈{MIN_WITHDRAW_FIRST * PULSE_TO_TON_RATE:.2f} TON). "
        f"Every withdrawal after: {MIN_WITHDRAW_STANDARD:.0f} Pulse (≈{MIN_WITHDRAW_STANDARD * PULSE_TO_TON_RATE:.2f} TON)\n\n"
        "<i>Task and offer rewards are funded by our advertising partners — "
        "that's the actual source of the Pulse you can withdraw.</i>"
    )
    await callback.message.edit_text(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)
    await callback.answer()


async def cmd_pending(message: Message):
    """Admin-only: list withdrawal requests awaiting review."""
    if message.from_user.id != ADMIN_ID:
        return

    pending = await list_pending_withdrawals()
    if not pending:
        await message.answer("No pending withdrawals.")
        return

    lines = ["<b>Pending withdrawals</b>\n"]
    for p in pending:
        ton = round(p["amount"] * PULSE_TO_TON_RATE, 4)
        lines.append(
            f"#{p['id']} — {p['amount']:.4f} Pulse (≈{ton:.4f} TON)\n"
            f"  User: {p['user_id']} (@{p['username']})\n"
            f"  Address: <code>{p['address']}</code>\n"
            f"  /approve {p['id']} • /reject {p['id']}"
        )
    await message.answer("\n\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_approve(message: Message):
    """Admin-only: mark a pending withdrawal as paid. Usage: /approve <tx_id>"""
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Usage: /approve <tx_id>")
        return
    try:
        tx_id = int(parts[1])
    except ValueError:
        await message.answer("Usage: /approve <tx_id>")
        return

    tx = await approve_withdrawal(tx_id)
    if not tx:
        await message.answer(f"No pending withdrawal #{tx_id} found.")
        return

    await message.answer(f"✅ Approved #{tx_id} ({tx['amount']:.4f} Pulse). Send the TON payout now if you haven't.")
    try:
        await message.bot.send_message(
            tx["user_id"],
            f"✅ <b>Withdrawal #{tx_id} approved!</b>\n\n{tx['amount']:.4f} Pulse has been paid out.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


async def cmd_reject(message: Message):
    """Admin-only: reject a pending withdrawal and refund the balance. Usage: /reject <tx_id> [reason]"""
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Usage: /reject <tx_id> [reason]")
        return
    try:
        tx_id = int(parts[1])
    except ValueError:
        await message.answer("Usage: /reject <tx_id> [reason]")
        return
    reason = parts[2] if len(parts) > 2 else ""

    tx = await reject_withdrawal(tx_id, reason)
    if not tx:
        await message.answer(f"No pending withdrawal #{tx_id} found.")
        return

    await message.answer(f"❌ Rejected #{tx_id}. {tx['amount']:.4f} Pulse refunded to the user.")
    try:
        await message.bot.send_message(
            tx["user_id"],
            f"❌ <b>Withdrawal #{tx_id} rejected.</b>\n\n"
            f"{tx['amount']:.4f} Pulse has been returned to your balance."
            + (f"\nReason: {reason}" if reason else ""),
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


async def cmd_credit(message: Message):
    """Admin-only: simulate an offerwall postback crediting a task/ad/offer.

    Usage: /credit <user_id> <task|ad|offer> [amount]
    NOTE: this is a manual test hook, not a real integration. Wire your offerwall's actual
    postback/webhook to call credit_action() directly once you stand up an HTTP endpoint
    (this bot only long-polls Telegram, it doesn't run a web server).
    """
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Usage: /credit <user_id> <task|ad|offer> [amount]")
        return

    try:
        target_id = int(parts[1])
        action_type = parts[2].lower()
        if action_type not in ("task", "ad", "offer"):
            raise ValueError
        default_reward = {"task": DEFAULT_TASK_REWARD, "ad": DEFAULT_AD_REWARD, "offer": DEFAULT_OFFER_REWARD}[action_type]
        amount = float(parts[3]) if len(parts) > 3 else default_reward
    except (ValueError, IndexError):
        await message.answer("Usage: /credit <user_id> <task|ad|offer> [amount]")
        return

    user = await get_user(target_id)
    if not user:
        await message.answer("User not found")
        return

    new_level = await credit_action(target_id, action_type, amount)
    reply = f"Credited {action_type} (+{amount:.4f}) to {target_id}."
    if new_level:
        reply += f" Leveled up to L{new_level} {VAULT_LEVELS[new_level]['name']}!"
    await message.answer(reply)


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
    dp.callback_query.register(withdraw_cancel_handler, F.data == "withdraw_cancel")
    dp.callback_query.register(vault_menu_handler, F.data == "vault_menu")
    dp.callback_query.register(vault_buy_handler, F.data.startswith("vault_buy:"))
    dp.callback_query.register(info_handler, F.data == "info")
    dp.pre_checkout_query.register(precheckout_handler)
    dp.message.register(successful_payment_handler, F.successful_payment)
    dp.message.register(cmd_credit, F.text.startswith("/credit"))
    dp.message.register(cmd_pending, F.text.startswith("/pending"))
    dp.message.register(cmd_approve, F.text.startswith("/approve"))
    dp.message.register(cmd_reject, F.text.startswith("/reject"))
    # Catch-all MUST be registered last: it only acts if the user has an open withdrawal
    # awaiting an address, and no-ops otherwise, so it's safe to sit behind the commands above.
    dp.message.register(withdraw_address_handler, F.text, ~F.text.startswith("/"))

    logger.info("PulseVault bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
