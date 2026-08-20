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
REFERRAL_BONUS = 1.0  # new user's own welcome bonus for joining via a referral link
REFERRAL_JOIN_BONUS = 0.3   # paid to the referrer instantly — the hook
REFERRAL_PROOF_BONUS = 0.7  # paid to the referrer once the friend proves they're a real, active user
REFERRAL_PROOF_TASKS = 10   # tasks the referred friend must complete to trigger the proof bonus

# Vaults: idle claiming is a bonus on top of tasks/ads/offers, never a substitute for them.
# Level 1 pays 0/h on purpose — idle users must engage with Tasks & Offers to earn anything.
VAULT_LEVELS = {
    1: {"name": "Starter", "rate": 0.00, "cap_hours": 0},
    2: {"name": "Grinder", "rate": 0.04, "cap_hours": 16},
    3: {"name": "Hunter",  "rate": 0.08, "cap_hours": 20},
    4: {"name": "Vaulted", "rate": 0.12, "cap_hours": 22},
    5: {"name": "Max",     "rate": 0.15, "cap_hours": 24},
}

# Cumulative tasks_completed needed to reach a level. Levels are earned by grinding ONLY —
# there is no paid path to skip this anymore. (L4/L5 thresholds extrapolate the L2/L3 spacing
# from the handoff doc; tune freely.)
LEVEL_UNLOCK_TASKS = {2: 10, 3: 25, 4: 60, 5: 120}

# --- Booster subscription ("soft evil" design) ---
# Stars no longer buy levels outright. Instead they buy a monthly idle-rate multiplier that
# stacks on top of whatever level the user actually earned through tasks. When the sub lapses,
# the user simply drops back to their normal (unboosted) rate at their current level — they
# never lose the level itself. Much less rage-inducing than revoking a level -> less churn,
# while still giving payers a reason to keep renewing every month.
STARS_SUB_COST = 50            # Telegram Stars per month
STARS_SUB_MULTIPLIER = 2.0     # idle rate multiplier while active
STARS_SUB_DURATION_DAYS = 30

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
                referral_proof_paid INTEGER DEFAULT 0,
                stars_sub_expires INTEGER DEFAULT 0,
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
            "ALTER TABLE users ADD COLUMN referral_proof_paid INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN stars_sub_expires INTEGER DEFAULT 0",
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
                      withdraw_state, pending_withdraw_id, referral_proof_paid, stars_sub_expires
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
                    "referral_proof_paid": bool(row[13]),
                    "stars_sub_expires": row[14] or 0,
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
    """Highest vault level reachable given lifetime tasks. Tasks are the ONLY unlock path —
    Stars never buy a level, they only buy the booster sub (see is_sub_active)."""
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
    """Recompute vault level from current progress; persist and return the new level if it rose."""
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


def is_sub_active(user: dict) -> bool:
    """Whether the booster sub is currently active (Stars paid, not yet expired)."""
    return user.get("stars_sub_expires", 0) > int(time.time())


def effective_rate(user: dict) -> float:
    """Idle rate for this user's level, doubled while their booster sub is active. Losing the
    sub only drops the multiplier — the vault level itself is never taken away."""
    base = VAULT_LEVELS[user["vault_level"]]["rate"]
    return round(base * STARS_SUB_MULTIPLIER, 4) if is_sub_active(user) else base


async def extend_stars_sub(user_id: int, current_expires: int) -> int:
    """Add STARS_SUB_DURATION_DAYS to the sub. Stacks on top of remaining time if still active,
    otherwise starts fresh from now. Returns the new expiry timestamp."""
    now = int(time.time())
    base = current_expires if current_expires > now else now
    new_expires = base + STARS_SUB_DURATION_DAYS * 86400
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET stars_sub_expires = ? WHERE user_id = ?",
            (new_expires, user_id)
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

    level = VAULT_LEVELS[user["vault_level"]]
    rate = effective_rate(user)

    if level["rate"] <= 0:
        await callback.answer(
            "Starter vault doesn't idle-earn — complete Tasks & Offers to unlock claiming 🎯",
            show_alert=True
        )
        return

    now = int(time.time())
    last = user["last_claim"] or now
    hours = min((now - last) / 3600, level["cap_hours"])
    earned = round(hours * rate, 4)

    if earned < 0.01:
        await callback.answer("Too soon! Come back later ⏳", show_alert=True)
        return

    new_balance = round(user["balance"] + earned, 4)
    new_total = round(user["total_earned"] + earned, 4)

    await update_balance(user_id, new_balance, new_total)
    await update_last_claim(user_id, now)
    await add_transaction(user_id, "claim", earned)

    boost_tag = " ⚡2x" if is_sub_active(user) else ""
    text = (
        f"⚡ <b>Claimed!</b>\n\n"
        f"You earned <b>+{earned:.4f} Pulse</b>\n"
        f"New balance: <b>{new_balance:.4f} Pulse</b>\n\n"
        f"Vault: L{user['vault_level']} {level['name']} • {rate}/h{boost_tag} • {level['cap_hours']}h cap"
    )
    await callback.message.edit_text(text, reply_markup=back_menu(), parse_mode=ParseMode.HTML)
    await callback.answer("Claim successful!")


async def balance_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    level = VAULT_LEVELS[user["vault_level"]]
    rate = effective_rate(user)
    now = int(time.time())
    last = user["last_claim"] or now
    hours = min((now - last) / 3600, level["cap_hours"])
    pending = round(hours * rate, 4)

    boost_tag = " ⚡2x boost active" if is_sub_active(user) else ""
    text = (
        f"💰 <b>Your Balance</b>\n\n"
        f"Available: <b>{user['balance']:.4f} Pulse</b>\n"
        f"Unclaimed: <b>{pending:.4f} Pulse</b>\n"
        f"Total earned: <b>{user['total_earned']:.4f} Pulse</b>\n\n"
        f"Vault: L{user['vault_level']} {level['name']} ({rate}/h, {level['cap_hours']}h cap){boost_tag}"
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

    level = VAULT_LEVELS[user["vault_level"]]
    if is_sub_active(user):
        remaining_days = max(0, (user["stars_sub_expires"] - int(time.time())) // 86400)
        sub_line = f"⚡ Booster sub: active, {remaining_days}d left ({effective_rate(user)}/h)"
    else:
        sub_line = f"Booster sub: inactive ({STARS_SUB_COST} ⭐/month for 2x idle rate)"
    text = (
        f"📊 <b>Stats</b>\n\n"
        f"ID: <code>{user['user_id']}</code>\n"
        f"Balance: {user['balance']:.4f}\n"
        f"Total earned: {user['total_earned']:.4f}\n"
        f"Vault: L{user['vault_level']} {level['name']}\n"
        f"{sub_line}\n\n"
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

    current = user["vault_level"]
    lines = ["🔐 <b>Vault Levels</b>\n", "<i>Idle claiming is a bonus on top of tasks — not a substitute for them.</i>\n"]
    buttons = []

    for lvl in sorted(VAULT_LEVELS):
        info = VAULT_LEVELS[lvl]
        marker = "👉" if lvl == current else ("✅" if lvl < current else "🔒")
        lines.append(f"{marker} <b>L{lvl} {info['name']}</b> — {info['rate']}/h, {info['cap_hours']}h cap")
        if lvl in LEVEL_UNLOCK_TASKS:
            lines.append(f"    Unlock: {LEVEL_UNLOCK_TASKS[lvl]} tasks")

    next_level = current + 1 if current < max(VAULT_LEVELS) else None
    if next_level:
        tasks_needed = max(0, LEVEL_UNLOCK_TASKS[next_level] - user["tasks_completed"])
        if tasks_needed > 0:
            lines.append(f"\n🎯 {tasks_needed} more tasks to reach L{next_level}")

    lines.append("")
    if is_sub_active(user):
        remaining_days = max(0, (user["stars_sub_expires"] - int(time.time())) // 86400)
        lines.append(f"⚡ <b>Booster sub active</b> — {STARS_SUB_MULTIPLIER:.0f}x idle rate, {remaining_days}d left")
        buttons.append([InlineKeyboardButton(
            text=f"⭐ Renew booster ({STARS_SUB_COST} ⭐, +{STARS_SUB_DURATION_DAYS}d)",
            callback_data="vault_sub"
        )])
    else:
        lines.append(
            f"⚡ <b>Booster sub</b> — {STARS_SUB_COST} ⭐/month for a {STARS_SUB_MULTIPLIER:.0f}x idle rate multiplier.\n"
            f"Doesn't touch your level — if it lapses you just lose the boost, not L{current}."
        )
        buttons.append([InlineKeyboardButton(
            text=f"⭐ Buy booster ({STARS_SUB_COST} ⭐/month, {STARS_SUB_MULTIPLIER:.0f}x rate)",
            callback_data="vault_sub"
        )])

    buttons.append([InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="menu")])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


async def vault_sub_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Please /start first", show_alert=True)
        return

    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title="PulseVault Booster",
        description=(
            f"{STARS_SUB_MULTIPLIER:.0f}x idle rate for {STARS_SUB_DURATION_DAYS} days. "
            f"Your vault level is unaffected either way."
        ),
        payload="vault_sub",
        currency="XTR",
        prices=[LabeledPrice(label="Booster (30 days)", amount=STARS_SUB_COST)]
    )
    await callback.answer()


async def precheckout_handler(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


async def successful_payment_handler(message: Message):
    payload = message.successful_payment.invoice_payload
    if payload == "vault_sub":
        user_id = message.from_user.id
        amount = message.successful_payment.total_amount  # actual Stars charged, source of truth

        user = await get_user(user_id)
        new_expires = await extend_stars_sub(user_id, user["stars_sub_expires"])
        await add_stars_spent(user_id, amount)
        await add_transaction(user_id, "vault_sub", amount, note="booster sub payment")

        level = VAULT_LEVELS[user["vault_level"]]
        boosted_rate = round(level["rate"] * STARS_SUB_MULTIPLIER, 4)
        remaining_days = max(0, (new_expires - int(time.time())) // 86400)
        text = (
            f"⚡ <b>Booster active!</b>\n\n"
            f"Your L{user['vault_level']} {level['name']} vault now earns "
            f"<b>{boosted_rate}/h</b> ({STARS_SUB_MULTIPLIER:.0f}x) for the next {remaining_days} days.\n\n"
            f"Your level itself doesn't change — if the booster lapses you just drop back to "
            f"{level['rate']}/h at L{user['vault_level']}, nothing is taken away."
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
        "• L2–L5 unlock passive claiming (0.04–0.15/h) purely by completing tasks — see 🔐 Vault Levels\n"
        f"• Optional {STARS_SUB_COST} ⭐/month booster sub gives a {STARS_SUB_MULTIPLIER:.0f}x idle rate "
        "multiplier on top of your level. If it lapses you just lose the boost — your level stays\n"
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
    dp.callback_query.register(vault_sub_handler, F.data == "vault_sub")
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
