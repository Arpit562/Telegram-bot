import os
import re
import uuid
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
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
    for item in os.getenv("CHANNEL_IDS", "").split(",")
    if item.strip()
]

# Telegram Stars prices must be configured by the bot owner.
# These are Stars (XTR), NOT INR.
PRICE_30_DAYS_XTR = int(os.getenv("PRICE_30_DAYS_XTR", "0"))
PRICE_60_DAYS_XTR = int(os.getenv("PRICE_60_DAYS_XTR", "0"))

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
    if PRICE_30_DAYS_XTR <= 0 or PRICE_60_DAYS_XTR <= 0:
        raise RuntimeError(
            "Set PRICE_30_DAYS_XTR and PRICE_60_DAYS_XTR to positive "
            "Telegram Stars amounts. These values are XTR, not INR."
        )
    if not EXTERNAL_URL.startswith("https://"):
        raise RuntimeError("A valid HTTPS RENDER_EXTERNAL_URL or WEBHOOK_URL is required.")
    if not WEBHOOK_SECRET_TOKEN:
        raise RuntimeError("WEBHOOK_SECRET_TOKEN is missing.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET_TOKEN):
        raise RuntimeError("WEBHOOK_SECRET_TOKEN has an invalid format.")
    if not WEBHOOK_PATH:
        raise RuntimeError("WEBHOOK_PATH cannot be empty.")


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
                status TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                plan_days INTEGER NOT NULL CHECK(plan_days IN (30, 60)),
                amount_xtr INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'XTR',
                invoice_payload TEXT NOT NULL UNIQUE,
                telegram_payment_charge_id TEXT UNIQUE,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                paid_at TEXT,
                reviewed_at TEXT,
                reviewed_by INTEGER
            )
        """)

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

    prices = {
        30: PRICE_30_DAYS_XTR,
        60: PRICE_60_DAYS_XTR,
    }
    if days not in prices:
        await query.message.reply_text("This plan is not available.")
        return

    amount = prices[days]
    payment_id = str(uuid.uuid4())
    payload = f"subscription:{query.from_user.id}:{days}:{payment_id}"

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO payments (
                payment_id, user_id, plan_days, amount_xtr, currency,
                invoice_payload, status, created_at
            ) VALUES (?, ?, ?, ?, 'XTR', ?, 'invoice_created', ?)
            """,
            (
                payment_id,
                query.from_user.id,
                days,
                amount,
                payload,
                to_iso(utc_now()),
            ),
        )

    try:
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=f"{days}-Day Channel Subscription",
            description=f"Access to all configured private channels for {days} days.",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"{days} days", amount=amount)],
        )
    except Exception:
        logger.exception("Could not send Stars invoice.")
        with db_connect() as conn:
            conn.execute(
                "UPDATE payments SET status = 'invoice_error' WHERE payment_id = ?",
                (payment_id,),
            )
        await query.message.reply_text(
            "The payment invoice could not be created. Please try again later."
        )


        return

async def precheckout_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.pre_checkout_query
    if not query.invoice_payload.startswith("subscription:"):
        await query.answer(ok=False, error_message="This invoice is invalid.")
        return

    with db_connect() as conn:
        payment = conn.execute(
            "SELECT * FROM payments WHERE invoice_payload = ? AND status = 'invoice_created'",
            (query.invoice_payload,),
        ).fetchone()

    if payment is None:
        await query.answer(
            ok=False,
            error_message="This invoice is invalid or has expired. Please create a new one.",
        )
        return

    if query.total_amount != payment["amount_xtr"]:
        await query.answer(ok=False, error_message="Payment amount mismatch.")
        return

    await query.answer(ok=True)


async def successful_payment_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    successful = update.message.successful_payment

    with db_connect() as conn:
        payment = conn.execute(
            "SELECT * FROM payments WHERE invoice_payload = ?",
            (successful.invoice_payload,),
        ).fetchone()

        if payment is None or payment["user_id"] != user.id:
            logger.error("Unmatched successful payment from user %s", user.id)
            await update.message.reply_text(
                "Payment received, but we could not match the order. Please contact the admin."
            )
            return

        if (
            successful.currency != "XTR"
            or successful.total_amount != payment["amount_xtr"]
        ):
            logger.error("Payment amount/currency mismatch for %s", payment["payment_id"])
            await update.message.reply_text(
                "Payment details need manual investigation. Please contact the admin."
            )
            return

        conn.execute(
            """
            UPDATE payments
            SET status = 'pending_admin',
                telegram_payment_charge_id = ?,
                paid_at = ?
            WHERE payment_id = ? AND status = 'invoice_created'
            """,
            (
                successful.telegram_payment_charge_id,
                to_iso(utc_now()),
                payment["payment_id"],
            ),
        )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Approve",
            callback_data=f"approve:{payment['payment_id']}",
        ),
        InlineKeyboardButton(
            "Reject + Refund",
            callback_data=f"reject:{payment['payment_id']}",
        ),
    ]])

    admin_text = (
        "New paid subscription awaiting review\n\n"
        f"User ID: {user.id}\n"
        f"Name: {user.full_name}\n"
        f"Username: @{user.username or 'None'}\n"
        f"Plan: {payment['plan_days']} days\n"
        f"Amount: {successful.total_amount} XTR\n"
        f"Payment ID: {payment['payment_id']}"
    )

    await context.bot.send_message(
        chat_id=ADMIN_TELEGRAM_ID,
        text=admin_text,
        reply_markup=keyboard,
    )
    await update.message.reply_text(
        "Your payment was received successfully. Your subscription is awaiting admin approval."
    )


async def admin_payment_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
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
        await query.edit_message_text("This payment was not found.")
        return

    if payment["status"] != "pending_admin":
        await query.edit_message_text(
            "This payment is no longer pending admin review."
        )
        return

    if action == "approve":
        expires_at = utc_now() + timedelta(days=payment["plan_days"])
        invite_messages = []

        with db_connect() as conn:
            conn.execute(
                """
                UPDATE payments
                SET status = 'approved', reviewed_at = ?, reviewed_by = ?
                WHERE payment_id = ? AND status = 'pending_admin'
                """,
                (to_iso(utc_now()), query.from_user.id, payment_id),
            )
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
                "UPDATE users SET status = 'active', expiry_date = ? WHERE user_id = ?",
                (to_iso(expires_at), payment["user_id"]),
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
            except Exception:
                logger.exception("Could not create invite for channel %s", channel_id)

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

        await query.edit_message_text(
            f"Approved.\nUser: {payment['user_id']}\n"
            f"Plan: {payment['plan_days']} days\n"
            f"Expiry: {expires_at:%d %b %Y, %H:%M UTC}\n"
            f"Channel links generated: {len(invite_messages)}"
        )

    elif action == "reject":
        charge_id = payment["telegram_payment_charge_id"]
        if not charge_id:
            await query.edit_message_text(
                "Cannot refund: Telegram payment charge ID is missing."
            )
            return

        try:
            await context.bot.refund_star_payment(
                user_id=payment["user_id"],
                telegram_payment_charge_id=charge_id,
            )
        except Exception:
            logger.exception("Stars refund failed for payment %s", payment_id)
            await query.edit_message_text(
                "Refund failed. Payment remains pending for manual investigation."
            )
            return

        with db_connect() as conn:
            conn.execute(
                """
                UPDATE payments
                SET status = 'rejected_refunded', reviewed_at = ?, reviewed_by = ?
                WHERE payment_id = ? AND status = 'pending_admin'
                """,
                (to_iso(utc_now()), query.from_user.id, payment_id),
            )

        try:
            await context.bot.send_message(
                chat_id=payment["user_id"],
                text="Your subscription request was rejected. The Stars payment has been refunded.",
            )
        except Exception:
            logger.exception("Could not notify rejected user.")

        await query.edit_message_text(
            f"Rejected and refunded.\nUser: {payment['user_id']}"
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


def main():
    validate_config()
    init_db()

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
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
