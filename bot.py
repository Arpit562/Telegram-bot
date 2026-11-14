import os
import re
import uuid
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)




# =========================================================
# CONFIGURATION
# =========================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "6807427898"))
DB_PATH = os.getenv("DB_PATH", "bot_users.db")

# Comma-separated Telegram channel IDs, e.g.:
# CHANNEL_IDS=-1001234567890,-1009876543210
CHANNEL_IDS = [
    item.strip()
    for item in os.getenv("CHANNEL_IDS", "-1003911622211").split(",")
    if item.strip()
]

UPI_ID = os.getenv("UPI_ID", "abhijeetvaish1@ybl").strip()
QR_IMAGE_PATH = Path(__file__).resolve().parent / "QR_code" / "QR2.jpeg"
PLAN_PRICES_INR = {
    30: 499,
    60: 899,
}

PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram-webhook").strip("/")
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "").strip()
EXTERNAL_URL = (
    os.getenv("RENDER_EXTERNAL_URL")
    or os.getenv("WEBHOOK_URL")
    or ""
).rstrip("/")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

def validate_config():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")
    if ADMIN_TELEGRAM_ID <= 0:
        raise RuntimeError("Set a valid ADMIN_TELEGRAM_ID.")
    if not CHANNEL_IDS:
        raise RuntimeError(
            "Set CHANNEL_IDS with at least one private channel ID."
        )
    if not EXTERNAL_URL.startswith("https://"):
        raise RuntimeError("A valid HTTPS RENDER_EXTERNAL_URL or WEBHOOK_URL is required.")
    if not WEBHOOK_SECRET_TOKEN:
        raise RuntimeError("WEBHOOK_SECRET_TOKEN is missing.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET_TOKEN):
        raise RuntimeError("WEBHOOK_SECRET_TOKEN has an invalid format.")
    if not WEBHOOK_PATH:
        raise RuntimeError("WEBHOOK_PATH cannot be empty.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+", UPI_ID):
        raise RuntimeError("UPI_ID has an invalid format.")
    if not QR_IMAGE_PATH.is_file():
        raise RuntimeError(f"UPI QR image was not found: {QR_IMAGE_PATH}")


@contextmanager
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def utc_now():
    return datetime.now(timezone.utc)


def to_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def init_db():
    with db_connect() as conn:
        # Preserve the original table and its existing records.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                expiry_date TEXT,
                status TEXT,
                plan_days INTEGER
            )
        """)

        user_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(users)")
        }
        if "plan_days" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN plan_days INTEGER")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                plan_days INTEGER NOT NULL CHECK(plan_days IN (30, 60)),
                amount_xtr INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'INR',
                invoice_payload TEXT NOT NULL UNIQUE,
                telegram_payment_charge_id TEXT UNIQUE,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                paid_at TEXT,
                reviewed_at TEXT,
                reviewed_by INTEGER,
                amount_inr INTEGER,
                proof_file_id TEXT,
                username TEXT
            )
        """)

        payment_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(payments)")
        }
        if "amount_inr" not in payment_columns:
            conn.execute("ALTER TABLE payments ADD COLUMN amount_inr INTEGER")
        if "proof_file_id" not in payment_columns:
            conn.execute("ALTER TABLE payments ADD COLUMN proof_file_id TEXT")
        if "username" not in payment_columns:
            conn.execute("ALTER TABLE payments ADD COLUMN username TEXT")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                plan_days INTEGER NOT NULL CHECK(plan_days IN (30, 60)),
                status TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_payment_id TEXT NOT NULL
            )
        """)

        conn.execute("""
            UPDATE users
            SET plan_days = (
                SELECT plan_days
                FROM subscriptions
                WHERE subscriptions.user_id = users.user_id
            )
            WHERE plan_days IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM subscriptions
                  WHERE subscriptions.user_id = users.user_id
              )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id TEXT PRIMARY KEY,
                title TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscription_channels (
                user_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                joined_link_created_at TEXT,
                PRIMARY KEY (user_id, channel_id)
            )
        """)

        # Synchronize configured channel IDs without deleting existing records.
        for channel_id in CHANNEL_IDS:
            conn.execute(
                """
                INSERT INTO channels (channel_id, is_active)
                VALUES (?, 1)
                ON CONFLICT(channel_id) DO UPDATE SET is_active = 1
                """,
                (channel_id,),
            )

    logger.info("Database initialized; legacy users table preserved.")

# USER INTERFACE
# =========================================================

def plans_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("30 Days", callback_data="plan:30")],
        [InlineKeyboardButton("60 Days", callback_data="plan:60")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with db_connect() as conn:
        subscription = conn.execute(
            "SELECT status, expires_at FROM subscriptions WHERE user_id = ?",
            (user.id,),
        ).fetchone()

    if subscription and subscription["status"] == "active":
        expiry = parse_iso(subscription["expires_at"])
        if expiry > utc_now():
            await update.message.reply_text(
                f"Welcome back, {user.first_name}!\n"
                f"Your subscription is active until {expiry:%d %b %Y, %H:%M UTC}.\n\n"
                "You can renew below.",
                reply_markup=plans_keyboard(),
            )
            return

    await update.message.reply_text(
        f"Welcome, {user.first_name}!\n"
        "Choose a subscription plan:",
        reply_markup=plans_keyboard(),
    )


async def plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("plan:"):
        return

    try:
        days = int(query.data.split(":", 1)[1])
    except ValueError:
        await query.message.reply_text("Invalid plan.")
        return

    if days not in PLAN_PRICES_INR:
        await query.message.reply_text("This plan is not available.")
        return

    amount = PLAN_PRICES_INR[days]
    payment_id = str(uuid.uuid4())
    payment_ref = f"upi:{payment_id}"

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO payments (
                payment_id, user_id, plan_days, amount_xtr, currency,
                invoice_payload, status, created_at, amount_inr, username
            ) VALUES (?, ?, ?, ?, 'INR', ?, 'awaiting_proof', ?, ?, ?)
            """,
            (
                payment_id,
                query.from_user.id,
                days,
                amount,
                payment_ref,
                to_iso(utc_now()),
                amount,
                query.from_user.username,
            ),
        )

    caption = (
        f"Payment for {days}-day subscription: ₹{amount}\n\n"
        "Scan this PhonePe QR to pay. If the QR does not work, pay to this UPI ID:\n"
        f"{UPI_ID}\n\n"
        "After paying, send your payment screenshot here. "
        "Access will be activated after admin verifies the payment."
    )
    with QR_IMAGE_PATH.open("rb") as qr_image:
        await query.message.reply_photo(photo=qr_image, caption=caption)


async def payment_proof_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.message
    proof_file_id = message.photo[-1].file_id

    with db_connect() as conn:
        payment = conn.execute(
            """
            SELECT * FROM payments
            WHERE user_id = ? AND status = 'awaiting_proof'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user.id,),
        ).fetchone()

        if payment is None:
            await message.reply_text(
                "Pehle /start se plan choose karo, payment karo, phir screenshot bhejo."
            )
            return

        updated = conn.execute(
            """
            UPDATE payments
            SET status = 'pending_admin', proof_file_id = ?, paid_at = ?
            WHERE payment_id = ? AND status = 'awaiting_proof'
            """,
            (proof_file_id, to_iso(utc_now()), payment["payment_id"]),
        ).rowcount
        if not updated:
            await message.reply_text(
                "Is payment ka screenshot pehle hi submit ho chuka hai."
            )
            return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Approve",
            callback_data=f"approve:{payment['payment_id']}",
        ),
        InlineKeyboardButton(
            "Reject",
            callback_data=f"reject:{payment['payment_id']}",
        ),
    ]])

    admin_text = (
        "UPI payment proof awaiting review\n\n"
        f"User ID: {user.id}\n"
        f"Name: {user.full_name}\n"
        f"Username: @{user.username or 'None'}\n"
        f"Plan: {payment['plan_days']} days\n"
        f"Amount: ₹{payment['amount_inr']}\n"
        f"Payment ID: {payment['payment_id']}"
    )

    try:
        await context.bot.send_photo(
            chat_id=ADMIN_TELEGRAM_ID,
            photo=proof_file_id,
            caption=admin_text,
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception("Could not forward payment proof %s to admin", payment["payment_id"])
        with db_connect() as conn:
            conn.execute(
                """
                UPDATE payments
                SET status = 'awaiting_proof', proof_file_id = NULL, paid_at = NULL
                WHERE payment_id = ? AND status = 'pending_admin'
                """,
                (payment["payment_id"],),
            )
        await message.reply_text(
            "Screenshot admin tak nahi pahunch saka. Thodi der baad screenshot dobara bhejo."
        )
        return

    await message.reply_text(
        "Payment screenshot admin ko bhej diya hai. Verification ke baad subscription activate hoga."
    )


async def admin_payment_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    if query.from_user.id != ADMIN_TELEGRAM_ID:
        await query.answer("Only the admin can review payments.", show_alert=True)
        return
    await query.answer()

    if not query.data or ":" not in query.data:
        return

    action, payment_id = query.data.split(":", 1)
    if action not in {"approve", "reject"}:
        return

    with db_connect() as conn:
        payment = conn.execute(
            "SELECT * FROM payments WHERE payment_id = ?",
            (payment_id,),
        ).fetchone()

    if payment is None:
        await query.edit_message_caption(caption="This payment was not found.")
        return

    if payment["status"] != "pending_admin":
        await query.edit_message_caption(
            caption="This payment is no longer pending admin review."
        )
        return

    if action == "approve":
        expires_at = utc_now() + timedelta(days=payment["plan_days"])
        invite_messages = []
        invite_failures = []

        with db_connect() as conn:
            updated = conn.execute(
                """
                UPDATE payments
                SET status = 'approved', reviewed_at = ?, reviewed_by = ?
                WHERE payment_id = ? AND status = 'pending_admin'
                """,
                (to_iso(utc_now()), query.from_user.id, payment_id),
            ).rowcount
            if not updated:
                await query.edit_message_caption(
                    caption="This payment was already reviewed."
                )
                return
            conn.execute(
                """
                INSERT INTO subscriptions (
                    user_id, plan_days, status, approved_at, expires_at, last_payment_id
                ) VALUES (?, ?, 'active', ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    plan_days = excluded.plan_days,
                    status = 'active',
                    approved_at = excluded.approved_at,
                    expires_at = excluded.expires_at,
                    last_payment_id = excluded.last_payment_id
                """,
                (
                    payment["user_id"],
                    payment["plan_days"],
                    to_iso(utc_now()),
                    to_iso(expires_at),
                    payment_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO users (
                    user_id, username, expiry_date, status, plan_days
                ) VALUES (?, ?, ?, 'active', ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username),
                    expiry_date = excluded.expiry_date,
                    status = 'active',
                    plan_days = excluded.plan_days
                """,
                (
                    payment["user_id"],
                    payment["username"],
                    to_iso(expires_at),
                    payment["plan_days"],
                ),
            )

        with db_connect() as conn:
            channels = conn.execute(
                "SELECT channel_id FROM channels WHERE is_active = 1"
            ).fetchall()

        for channel in channels:
            channel_id = channel["channel_id"]
            try:
                try:
                    await context.bot.ban_chat_member(
                        chat_id=channel_id,
                        user_id=payment["user_id"],
                    )
                    await context.bot.unban_chat_member(
                        chat_id=channel_id,
                        user_id=payment["user_id"],
                        only_if_banned=True,
                    )
                except Exception:
                    logger.info("User was not banned in channel %s", channel_id)

                chat = await context.bot.get_chat(channel_id)
                if chat.username:
                    invite_messages.append(f"https://t.me/{chat.username}")
                else:
                    invite = await context.bot.create_chat_invite_link(
                        chat_id=channel_id,
                        member_limit=1,
                        expire_date=utc_now() + timedelta(hours=24),
                    )
                    invite_messages.append(invite.invite_link)

                with db_connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO subscription_channels (
                            user_id, channel_id, joined_link_created_at
                        ) VALUES (?, ?, ?)
                        ON CONFLICT(user_id, channel_id) DO UPDATE SET
                            joined_link_created_at = excluded.joined_link_created_at
                        """,
                        (payment["user_id"], channel_id, to_iso(utc_now())),
                    )
                    print(f"--> NEW PAYMENT DATA: User={payment['user_id']}, Days={payment['plan_days']}, Amount={payment['amount_inr']}")
            except Exception as e:
                # Yeh line terminal me exact error dikha degi bhai
                logger.exception("Could not create invite for channel %s", channel_id)
                print(f"--> ASLI PYTHON ERROR: {e}") 
                invite_failures.append(channel_id)

        user_text = (
            "Payment approved! Your subscription is active.\n"
            f"Plan: {payment['plan_days']} days\n"
            f"Expires: {expires_at:%d %b %Y, %H:%M UTC}\n\n"
        )
        if invite_messages:
            user_text += "Join your private channels using these links:\n"
            user_text += "\n".join(invite_messages)
        else:
            user_text += (
                "We could not generate your channel links automatically. "
                "Please contact the admin."
            )

        try:
            await context.bot.send_message(
                chat_id=payment["user_id"],
                text=user_text,
            )
        except Exception:
            logger.exception("Could not notify approved user %s", payment["user_id"])

        try:
            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=f"You approved {payment['user_id']}",
            )
        except Exception:
            logger.exception(
                "Could not notify admin about approval for user %s",
                payment["user_id"],
            )

        await query.edit_message_text(
            f"Approved.\nUser: {payment['user_id']}\n"
            f"Plan: {payment['plan_days']} days\n"
            f"Expiry: {expires_at:%d %b %Y, %H:%M UTC}\n"
            f"Channel links generated: {len(invite_messages)}"
            + (
                f"\nCould not generate links for: {', '.join(invite_failures)}. "
                "For private channels, make the bot an admin with invite-link permission."
                if invite_failures else ""
            )
        )

    elif action == "reject":
        with db_connect() as conn:
            updated = conn.execute(
                """
                UPDATE payments
                SET status = 'rejected', reviewed_at = ?, reviewed_by = ?
                WHERE payment_id = ? AND status = 'pending_admin'
                """,
                (to_iso(utc_now()), query.from_user.id, payment_id),
            ).rowcount
        if not updated:
            await query.edit_message_caption(
                caption="This payment was already reviewed."
            )
            return

        try:
            await context.bot.send_message(
                chat_id=payment["user_id"],
                text=(
                    "Your payment proof was rejected. Please contact the admin "
                    "if you believe this is a mistake."
                ),
            )
        except Exception:
            logger.exception("Could not notify rejected user.")

        await query.edit_message_caption(
            caption=f"Payment rejected.\nUser: {payment['user_id']}"
        )


# =========================================================
# AUTOMATIC SUBSCRIPTION EXPIRY
# =========================================================

async def expire_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    now = to_iso(utc_now())

    with db_connect() as conn:
        expired = conn.execute(
            """
            SELECT user_id FROM subscriptions
            WHERE status = 'active' AND expires_at <= ?
            """,
            (now,),
        ).fetchall()
        channels = conn.execute(
            "SELECT channel_id FROM channels WHERE is_active = 1"
        ).fetchall()

    for subscription in expired:
        user_id = subscription["user_id"]

        for channel in channels:
            channel_id = channel["channel_id"]
            try:
                # Ban followed immediately by unban removes the member
                # without permanently preventing a future renewal.
                await context.bot.ban_chat_member(
                    chat_id=channel_id,
                    user_id=user_id,
                )
                await context.bot.unban_chat_member(
                    chat_id=channel_id,
                    user_id=user_id,
                    only_if_banned=True,
                )
            except Exception:
                logger.exception(
                    "Could not remove user %s from channel %s",
                    user_id,
                    channel_id,
                )

        with db_connect() as conn:
            conn.execute(
                "UPDATE subscriptions SET status = 'expired' WHERE user_id = ?",
                (user_id,),
            )
            conn.execute(
                "UPDATE users SET status = 'expired' WHERE user_id = ?",
                (user_id,),
            )

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "Your subscription has expired and your channel access has been removed. "
                    "You can renew anytime using /start."
                ),
            )
        except Exception:
            logger.info("Could not send expiry message to user %s", user_id)


# =========================================================
# COMMANDS AND WEBHOOK APP
# =========================================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "To cancel a pending request, please contact the admin."
    )


async def check_db_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if user is None or user.id != ADMIN_TELEGRAM_ID or message is None:
        return

    with db_connect() as conn:
        # Ab hum users ke sath-sath payments table bhi check karenge
        users = conn.execute("SELECT user_id, status, expiry_date FROM users").fetchall()
        payments = conn.execute("SELECT payment_id, user_id, status, amount_inr FROM payments").fetchall()

    msg = f"📊 **DB Report:**\n\n👤 **Users ({len(users)}):**\n"
    for u in users:
        msg += f"ID: `{u[0]}` | Status: {u[1]}\n"
        
    msg += f"\n💳 **Payments ({len(payments)}):**\n"
    for p in payments:
        msg += f"PayID: `{p[0]}` | User: `{p[1]}` | Status: {p[2]} | Amt: ₹{p[3]}\n"

    await message.reply_text(msg, parse_mode="Markdown")
    
def main():
    validate_config()
    init_db()

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("checkdb", check_db_users))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("checkusers", check_db_users))
    app.add_handler(MessageHandler(filters.PHOTO, payment_proof_handler))
    app.add_handler(
        CallbackQueryHandler(
            admin_payment_callback,
            pattern=r"^(approve|reject):",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            plan_callback,
            pattern=r"^plan:",
        )
    )

    if app.job_queue is None:
        raise RuntimeError(
            "JobQueue is unavailable. Install python-telegram-bot[job-queue,webhooks]."
        )

    app.job_queue.run_repeating(
        expire_subscriptions,
        interval=300,
        first=30,
        name="subscription-expiry-check",
    )

    webhook_url = f"{EXTERNAL_URL}/{WEBHOOK_PATH}"
    logger.info("Starting bot webhook at %s", webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET_TOKEN,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
