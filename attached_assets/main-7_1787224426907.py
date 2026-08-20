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

MIN_WITHDRAW_FIRST = 15.0
MIN_WITHDRAW_STANDARD = 30.0
PULSE_TO_TON_RATE = 0.02  # 1 Pulse = 0.02 TON
REFERRAL_BONUS = 1.0  # new user's own welcome bonus for joining via a referral link
REFERRAL_JOIN_BONUS = 0.10   # paid to the referrer instantly — the hook
REFERRAL_PROOF_BONUS = 0.50  # paid to the referrer once the friend proves they're a real, active user
REFERRAL_PROOF_TASKS = 5     # tasks the referred friend must complete to trigger the proof bonus

# Two ad types in Tasks & Offers:
# - Reward Ad: small direct payout, capped per day, counts toward the withdraw-ads gate.
# - Gate Ad: pays nothing, but must be watched every time before the real offerwall link
#   unlocks. Counts toward the withdraw-ads gate too.
REWARD_AD_REWARD = 0.0050
REWARD_AD_DAILY_LIMIT = 5
GATE_AD_REWARD = 0.0000

# Vaults: idle claiming is a bonus on top of tasks/ads/offers, never a substitute for them.
# Level 1 pays 0/h on purpose — idle users must engage with Tasks & Offers to earn anything.
# "stars" is the Telegram Stars price to ACTIVATE that level's rate for 30 days (see the
# activation model below) — it is separate from unlocking the level via tasks.
VAULT_LEVELS = {
    1: {"name": "Starter", "rate": 0.0000, "cap_hours": 0,  "stars": 0},
    2: {"name": "Grinder", "rate": 0.0400, "cap_hours": 16, "stars": 30},
    3: {"name": "Hunter",  "rate": 0.0800, "cap_hours": 20, "stars": 60},
    4: {"name": "Vaulted", "rate": 0.1200, "cap_hours": 22, "stars": 100},
    5: {"name": "Max",     "rate": 0.1500, "cap_hours": 24, "stars": 150},
}

# Cumulative tasks_completed needed to UNLOCK a level (i.e. become eligible to activate it).
# (L4/L5 thresholds extrapolate the L2/L3 spacing from the handoff doc; tune freely.)
LEVEL_UNLOCK_TASKS = {2: 10, 3: 25, 4: 60, 5: 120}

# --- Activation model ---
# Unlocking a level via tasks only makes it ELIGIBLE — it doesn't earn anything by itself.
# To actually earn at that level's rate, the user pays Stars to "activate" it for 30 days.
# If activation expires, the idle rate drops all the way back to Starter (0/h) until they
# either activate again or claim manually via tasks/ads. This is a harder monetization model
# than a rate-multiplier booster: no activation = no idle income, period.
ACTIVATION_DURATION_DAYS = 30

# Withdrawal gate: required lifetime completions before ANY withdrawal unlocks,
# regardless of vault level or balance.
WITHDRAW_GATE_TASKS = 15
WITHDRAW_GATE_ADS = 5
WITHDRAW_GATE_OFFERS = 1

# Placeholder per-action rewards for the admin test-credit command below.
# Real amounts should come from your offerwall's postback payload once that's wired up.
DEFAULT_TASK_REWARD = 0.3000
DEFAULT_AD_REWARD = 0.0050
DEFAULT_OFFER_REWARD = 0.3000

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
                active_level INTEGER DEFAULT 1,
                level_expires INTEGER DEFAULT 0,
                tasks_completed INTEGER DEFAULT 0,
                ads_completed INTEGER DEFAULT 0,
                offers_completed INTEGER DEFAULT 0,
                reward_ads_today INTEGER DEFAULT 0,
                reward_ads_date TEXT,
                stars_spent INTEGER DEFAULT 0,
                referral_proof_paid INTEGER DEFAULT 0,
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
            "ALTER TABLE users ADD COLUMN active_level INTEGER DEFAULT 1",
            "ALTER TABLE users ADD COLUMN level_expires INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN tasks_completed INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN ads_completed INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN offers_completed INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN reward_ads_today INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN reward_ads_date TEXT",
            "ALTER TABLE users ADD COLUMN stars_spent INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN withdraw_state TEXT",
            "ALTER TABLE users ADD COLUMN pending_withdraw_id INTEGER",
            "ALTER TABLE users ADD COLUMN referral_proof_paid INTEGER DEFAULT 0",
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
                      vault_level, active_level, level_expires, tasks_completed, ads_completed,
                      offers_completed, reward_ads_today, reward_ads_date, stars_spent,
                      withdraw_state, pending_withdraw_id, referral_proof_paid
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
                    "active_level": row[7] or 1,
                    "level_expires": row[8] or 0,
                    "tasks_completed": row[9] or 0,
                    "ads_completed": row[10] or 0,
                    "offers_completed": row[11] or 0,
                    "reward_ads_today": row[12] or 0,
                    "reward_ads_date": row[13],
                    "stars_spent": row[14] or 0,
                    "withdraw_state": row[15],
                    "pending_withdraw_id": row[16],
                    "referral_proof_paid": bool(row[17]),
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


async def add_balance(user_id: int, amount: float, type_: str, note: str = ""):
    """Atomically add (or subtract, if amount is negative) to a user's balance + total_earned,
    and log the transaction. Safer than read-modify-write for concurrent credits."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id = ?",
            (amount, amount, user_id)
        )
        await db.commit()
    await add_transaction(user_id, type_, amount, note=note)


async def mark_referral_proof_paid(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET referral_proof_paid = 1 WHERE user_id = ?", (user_id,))
        await db.commit()


async def check_and_pay_referral_proof(user: dict) -> int | None:
    """If this (referred) user just crossed REFERRAL_PROOF_TASKS tasks and their referrer hasn't
    been paid the proof bonus yet, pay it and return the referrer's id (for notifying them)."""
    if not user["referral_id"] or user["referral_proof_paid"]:
        return None
    if user["tasks_completed"] < REFERRAL_PROOF_TASKS:
        return None

    referrer_id = user["referral_id"]
    await add_balance(
        referrer_id, REFERRAL_PROOF_BONUS, "referral_proof",
        note=f"referred {user['user_id']} completed {REFERRAL_PROOF_TASKS} tasks"
    )
    await mark_referral_proof_paid(user["user_id"])
    return referrer_id


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

def highest_unlocked_level(tasks_completed: int) -> int:
    """Highest vault level ELIGIBLE given lifetime tasks. This only makes a level available to
    activate — it does not by itself earn anything (see get_active_rate / ACTIVATION_DURATION_DAYS)."""
    level = 1
    for lvl in sorted(VAULT_LEVELS):
        if lvl == 1:
            continue
        if tasks_completed >= LEVEL_UNLOCK_TASKS.get(lvl, float("inf")):
            level = lvl
    return level


async def set_vault_level(user_id: int, level: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET vault_level = ? WHERE user_id = ?", (level, user_id))
        await db.commit()


async def apply_level_up(user_id: int, user: dict) -> int | None:
    """Recompute the highest ELIGIBLE (unlocked) level from task progress; persist and return the
    new level if it rose. Eligibility alone doesn't earn Pulse — the user still has to activate it
    with Stars (see activate_level)."""
    eligible = highest_unlocked_level(user["tasks_completed"])
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


def get_active_rate(user: dict) -> float:
    """Idle rate the user is CURRENTLY earning. Unlocking a level via tasks is not enough on its
    own — the level must also be activated with Stars and not yet expired. If activation lapses,
    this falls all the way back to Starter (0/h), not to the unlocked level's rate."""
    if user["level_expires"] > int(time.time()) and user["active_level"] > 1:
        return VAULT_LEVELS[user["active_level"]]["rate"]
    return VAULT_LEVELS[1]["rate"]


def active_level_info(user: dict) -> dict:
    """VAULT_LEVELS entry actually in effect right now (Starter if activation has lapsed)."""
    if user["level_expires"] > int(time.time()) and user["active_level"] > 1:
        return VAULT_LEVELS[user["active_level"]]
    return VAULT_LEVELS[1]


async def activate_level(user_id: int, level: int, stars_paid: int) -> int:
    """Pay Stars to activate `level`'s rate for ACTIVATION_DURATION_DAYS from now. Does NOT stack
    with remaining time on a prior activation — buying a new activation simply resets the clock
    to a fresh 30 days at the newly chosen level. Returns the new expiry timestamp."""
    new_expires = int(time.time()) + ACTIVATION_DURATION_DAYS * 86400
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET active_level = ?, level_expires = ?, stars_spent = stars_spent + ? WHERE user_id = ?",
            (level, new_expires, stars_paid, user_id)
        )
        await db.commit()
    return new_expires


async def credit_action(user_id: int, action_type: str, reward: float) -> dict:
    """Credit a completed task/ad/offer: bump its counter, pay the reward, check for a vault
    level-up, and (for tasks) check whether this user's referrer just earned the proof bonus.

    action_type is one of 'task', 'ad', 'offer'. In production this should be called from your
    offerwall's postback/webhook handler (not present in this polling-only bot) rather than the
    admin test command below.

    Returns {"new_level": int|None, "referrer_paid": int|None} — referrer_paid is the referrer's
    user_id if the referral proof bonus was just triggered, so the caller can notify them.
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
    new_level = await apply_level_up(user_id, user)

    referrer_paid = None
    if action_type == "task":
        referrer_paid = await check_and_pay_referral_proof(user)

    return {"new_level": new_level, "referrer_paid": referrer_paid}

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
                await add_balance(user_id, REFERRAL_BONUS, "referral_welcome", note=f"from {referral_id}")
                await add_balance(referral_id, REFERRAL_JOIN_BONUS, "referral_join", note=f"invited {user_id}")

                try:
                    await message.bot.send_message(
                        referral_id,
                        f"🎉 Someone joined with your link!\n+{REFERRAL_JOIN_BONUS:.2f} Pulse added.\n"
                        f"Another +{REFERRAL_PROOF_BONUS:.2f} Pulse when they complete {REFERRAL_PROOF_TASKS} tasks."
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

    level = active_level_info(user)
    rate = get_active_rate(user)

    if rate <= 0:
        msg = ("Activate a Vault level with ⭐ Stars to start idle earning 🔐" if user["vault_level"] > 1
               else "Complete Tasks & Offers to unlock your first Vault level 🎯")
        await callback.answer(msg, show_alert=True)
        return

    now = int(time.time())
    last = user["last_claim"] or now
    hours = min((now - last) / 3600, level["cap_hours"])
    earned = round(hours * rate, 4)

    if earned < 0.0001:
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
        f"Active: {level['name']} • {rate}/h • {level['cap_hours']}h cap"
    )
    await callback.message.edit_text(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)
    await callback.answer("Claim successful!")


async def balance_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    level = active_level_info(user)
    rate = get_active_rate(user)
    now = int(time.time())
    last = user["last_claim"] or now
    hours = min((now - last) / 3600, level["cap_hours"])
    pending = round(hours * rate, 4)

    text = (
        f"💰 <b>Your Balance</b>\n\n"
        f"Available: <b>{user['balance']:.4f} Pulse</b>\n"
        f"Unclaimed: <b>{pending:.4f} Pulse</b>\n"
        f"Total earned: <b>{user['total_earned']:.4f} Pulse</b>\n\n"
        f"Active: {level['name']} ({rate}/h, {level['cap_hours']}h cap)"
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
        f"Your friend gets <b>{REFERRAL_BONUS:.2f} Pulse</b> just for joining.\n"
        f"You get <b>{REFERRAL_JOIN_BONUS:.2f} Pulse</b> instantly when they join, "
        f"plus <b>{REFERRAL_PROOF_BONUS:.2f} Pulse</b> more once they complete "
        f"{REFERRAL_PROOF_TASKS} tasks."
    )
    await callback.message.edit_text(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)
    await callback.answer()


async def stats_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    unlocked = VAULT_LEVELS[user["vault_level"]]
    now = int(time.time())
    if user["level_expires"] > now and user["active_level"] > 1:
        remaining_days = max(0, (user["level_expires"] - now) // 86400)
        active_line = f"⚡ Active: L{user['active_level']} {VAULT_LEVELS[user['active_level']]['name']}, {remaining_days}d left"
    else:
        active_line = "Active: L1 Starter (no activation running — 0/h)"
    text = (
        f"📊 <b>Stats</b>\n\n"
        f"ID: <code>{user['user_id']}</code>\n"
        f"Balance: {user['balance']:.4f}\n"
        f"Total earned: {user['total_earned']:.4f}\n"
        f"Highest unlocked: L{user['vault_level']} {unlocked['name']}\n"
        f"{active_line}\n\n"
        f"Tasks completed: {user['tasks_completed']} (withdraw needs {WITHDRAW_GATE_TASKS})\n"
        f"Ads watched: {user['ads_completed']} (withdraw needs {WITHDRAW_GATE_ADS})\n"
        f"Offers completed: {user['offers_completed']} (withdraw needs {WITHDRAW_GATE_OFFERS})\n"
        f"Stars spent total: {user['stars_spent']}"
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

    unlocked = user["vault_level"]
    now = int(time.time())
    is_active = user["level_expires"] > now
    lines = [
        "🔐 <b>Vault Levels</b>\n",
        "<i>Tasks unlock a level. Activating it with ⭐ Stars for 30 days is what actually "
        "makes it earn — without an active level, idle rate is 0/h.</i>\n"
    ]
    buttons = []

    for lvl in sorted(VAULT_LEVELS):
        info = VAULT_LEVELS[lvl]
        is_this_active = is_active and user["active_level"] == lvl
        if is_this_active:
            status = "✅ Active"
        elif unlocked >= lvl:
            status = "🔓 Unlocked"
        else:
            status = "🔒 Locked"
        lines.append(f"{status} <b>L{lvl} {info['name']}</b> — {info['rate']}/h, {info['cap_hours']}h cap")
        if lvl in LEVEL_UNLOCK_TASKS:
            lines.append(f"    Unlock: {LEVEL_UNLOCK_TASKS[lvl]} tasks")

        if unlocked >= lvl and lvl > 1 and not is_this_active:
            buttons.append([InlineKeyboardButton(
                text=f"⭐ Activate L{lvl} — {info['stars']} ⭐ (30 days)",
                callback_data=f"activate:{lvl}"
            )])

    next_level = unlocked + 1 if unlocked < max(VAULT_LEVELS) else None
    if next_level:
        tasks_needed = max(0, LEVEL_UNLOCK_TASKS[next_level] - user["tasks_completed"])
        if tasks_needed > 0:
            lines.append(f"\n🎯 {tasks_needed} more tasks to unlock L{next_level}")

    if is_active:
        remaining_days = max(0, (user["level_expires"] - now) // 86400)
        lines.append(f"\n⏳ Current activation: {remaining_days}d left")
    else:
        lines.append("\n⚠️ Nothing active right now — idle rate is 0/h until you activate a level.")

    buttons.append([InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="menu")])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


async def activate_level_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    lvl = int(callback.data.split(":")[1])
    if lvl not in VAULT_LEVELS or lvl < 2 or user["vault_level"] < lvl:
        await callback.answer("That level isn't unlocked yet", show_alert=True)
        return

    info = VAULT_LEVELS[lvl]
    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Activate L{lvl} {info['name']}",
        description=f"{info['rate']}/h idle rate for {ACTIVATION_DURATION_DAYS} days.",
        payload=f"activate:{lvl}",
        currency="XTR",
        prices=[LabeledPrice(label=f"L{lvl} — {ACTIVATION_DURATION_DAYS} days", amount=info["stars"])]
    )
    await callback.answer()


async def precheckout_handler(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


async def successful_payment_handler(message: Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("activate:"):
        lvl = int(payload.split(":")[1])
        user_id = message.from_user.id
        amount = message.successful_payment.total_amount  # actual Stars charged, source of truth

        new_expires = await activate_level(user_id, lvl, amount)
        await add_transaction(user_id, "activate", amount, note=f"activated L{lvl}")

        info = VAULT_LEVELS[lvl]
        remaining_days = max(0, (new_expires - int(time.time())) // 86400)
        text = (
            f"✅ <b>L{lvl} {info['name']} activated!</b>\n\n"
            f"You're now earning <b>{info['rate']}/h</b> for the next {remaining_days} days.\n\n"
            f"When this expires, idle earning drops back to 0/h (Starter) until you activate "
            f"again — your unlocked level itself is unaffected."
        )
        await message.answer(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)


async def tasks_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    today = time.strftime("%Y-%m-%d")
    ads_today = user["reward_ads_today"] if user["reward_ads_date"] == today else 0
    left = max(0, REWARD_AD_DAILY_LIMIT - ads_today)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📺 Rewarded Ad (+{REWARD_AD_REWARD:.4f}) — {left}/{REWARD_AD_DAILY_LIMIT} left",
            callback_data="reward_ad"
        )],
        [InlineKeyboardButton(
            text="▶️ Gate Ad (0 Pulse) — Unlock Tasks & Offers",
            callback_data="gate_ad"
        )],
        [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="menu")]
    ])

    text = (
        "🎯 <b>Tasks & Offers</b>\n\n"
        f"1️⃣ <b>Rewarded Ad</b> — +{REWARD_AD_REWARD:.4f} Pulse, up to {REWARD_AD_DAILY_LIMIT}/day\n"
        "2️⃣ <b>Gate Ad</b> — watch to unlock the real offerwall (surveys, app trials, quick "
        "offers). Pays 0 Pulse directly, but the offers behind it are where the real money is — "
        "and it's what actually funds withdrawals.\n\n"
        "Both count toward your withdrawal-ads requirement."
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    await callback.answer()


async def reward_ad_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    today = time.strftime("%Y-%m-%d")
    ads_today = user["reward_ads_today"] if user["reward_ads_date"] == today else 0

    if ads_today >= REWARD_AD_DAILY_LIMIT:
        await callback.answer(f"Daily limit reached ({REWARD_AD_DAILY_LIMIT}/{REWARD_AD_DAILY_LIMIT})", show_alert=True)
        return

    await add_balance(user["user_id"], REWARD_AD_REWARD, "reward_ad")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET reward_ads_today = ?, reward_ads_date = ?, ads_completed = ads_completed + 1 WHERE user_id = ?",
            (ads_today + 1, today, user["user_id"])
        )
        await db.commit()

    await callback.answer(f"+{REWARD_AD_REWARD:.4f} Pulse!", show_alert=True)
    await tasks_handler(callback)  # refresh the menu with the updated daily count


async def gate_ad_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET ads_completed = ads_completed + 1 WHERE user_id = ?",
            (user["user_id"],)
        )
        await db.commit()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Open Tasks & Offers", url=f"{OFFERWALL_URL}?uid={callback.from_user.id}")],
        [InlineKeyboardButton(text="⬅️ Back to Tasks", callback_data="tasks")]
    ])
    await callback.message.edit_text(
        "✅ <b>Gate ad watched!</b>\n\nYou can now open the offerwall. "
        "Rewards post automatically once an offer is verified complete "
        "(usually within a few minutes, sometimes longer for surveys).\n\n"
        "You'll need to watch a gate ad again next time you come back to Tasks & Offers.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

async def info_handler(callback: CallbackQuery):
    text = (
        "ℹ️ <b>How it works</b>\n\n"
        "• Vaults have 5 levels (L1–L5). Completing tasks <i>unlocks</i> a level, but "
        "unlocking alone doesn't earn anything\n"
        "• To actually earn idle Pulse, activate your unlocked level with ⭐ Stars for "
        f"{ACTIVATION_DURATION_DAYS} days (0.04–0.15/h) — see 🔐 Vault Levels. If activation "
        "expires, idle rate drops to 0/h until you activate again (your unlocked level is kept)\n"
        f"• Tasks & Offers has two ad types: a Rewarded Ad (+{REWARD_AD_REWARD:.4f} Pulse, up to "
        f"{REWARD_AD_DAILY_LIMIT}/day) and a Gate Ad (0 Pulse) that unlocks the real offerwall each visit\n"
        f"• Invite friends — they get {REFERRAL_BONUS:.2f} Pulse for joining; you get "
        f"{REFERRAL_JOIN_BONUS:.2f} Pulse instantly + {REFERRAL_PROOF_BONUS:.2f} more once "
        f"they complete {REFERRAL_PROOF_TASKS} tasks\n"
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

    result = await credit_action(target_id, action_type, amount)
    new_level = result["new_level"]
    referrer_paid = result["referrer_paid"]

    reply = f"Credited {action_type} (+{amount:.4f}) to {target_id}."
    if new_level:
        reply += f" Leveled up to L{new_level} {VAULT_LEVELS[new_level]['name']}!"
    if referrer_paid:
        reply += f" Referral proof bonus (+{REFERRAL_PROOF_BONUS:.2f}) paid to {referrer_paid}."
        try:
            await message.bot.send_message(
                referrer_paid,
                f"🎉 Your friend proved they're active — "
                f"+{REFERRAL_PROOF_BONUS:.2f} Pulse added to your balance!"
            )
        except Exception:
            pass
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
    dp.callback_query.register(activate_level_handler, F.data.startswith("activate:"))
    dp.callback_query.register(reward_ad_handler, F.data == "reward_ad")
    dp.callback_query.register(gate_ad_handler, F.data == "gate_ad")
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
