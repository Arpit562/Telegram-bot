
import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters
)


# ==========================================
# 1. CONFIGURATION
# ==========================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_TELEGRAM_ID = int(
    os.getenv("ADMIN_TELEGRAM_ID", "8904576835")
)

CLIENT_UPI_ID = "abhijeetvaish1@ybl"
CLIENT_NAME = "abhijeet's records"

QR_CODE_PATH = "QR_code/QR2.jpeg"
DB_PATH = "bot_users.db"

# Render configuration
PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH = os.getenv(
    "WEBHOOK_PATH", "telegram-webhook"
).strip("/")

WEBHOOK_SECRET_TOKEN = os.getenv(
    "WEBHOOK_SECRET_TOKEN", ""
).strip()

# Render automatically provides RENDER_EXTERNAL_URL.
# WEBHOOK_URL is an optional manual fallback.
EXTERNAL_URL = (
    os.getenv("RENDER_EXTERNAL_URL")
    or os.getenv("WEBHOOK_URL")
    or ""
).rstrip("/")


# ==========================================
# 2. VALIDATE CONFIGURATION
# ==========================================

if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. Configure it in Render Environment."
    )

if not EXTERNAL_URL.startswith("https://"):
    raise RuntimeError(
        "A valid HTTPS RENDER_EXTERNAL_URL or WEBHOOK_URL is required."
    )

if not WEBHOOK_SECRET_TOKEN:
    raise RuntimeError(
        "WEBHOOK_SECRET_TOKEN is missing. Configure it in Render."
    )

if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET_TOKEN):
    raise RuntimeError(
        "WEBHOOK_SECRET_TOKEN must contain only letters, numbers, "
        "underscores or hyphens, with a maximum of 256 characters."
    )

if not WEBHOOK_PATH:
    raise RuntimeError("WEBHOOK_PATH cannot be empty.")


# ==========================================
# 3. LOGGING
# ==========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# ==========================================
# 4. DATABASE CONNECTION & SETUP
# ==========================================

def init_db():

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            expiry_date TEXT,
            status TEXT
        )
    """)

    conn.commit()
    conn.close()

    logger.info("Database initialized successfully.")


# ==========================================
# 5. START COMMAND
# ==========================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    user_id = user.id
    username = user.username or user.first_name

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT expiry_date, status FROM users WHERE user_id=?",
        (user_id,)
    )

    row = cursor.fetchone()

    conn.close()

    if row:

        expiry_date_str, status = row

        expiry_date = datetime.strptime(
            expiry_date_str, "%Y-%m-%d"
        )

        if status == "active" and datetime.now() < expiry_date:

            await update.message.reply_text(
                f"Welcome back {username}! 👋\n"
                f"Your subscription is active until "
                f"**{expiry_date_str}**.",
                parse_mode="Markdown"
            )

            return

    keyboard = [
        [
            InlineKeyboardButton(
                "Buy / Renew Subscription",
                callback_data="buy_subscription"
            )
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Welcome {username}!\n"
        "Please choose an option below to access the services:",
        reply_markup=reply_markup
    )


# ==========================================
# 6. BUY SUBSCRIPTION BUTTON HANDLER
# ==========================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    if query.data == "buy_subscription":

        caption = (
            "**Subscription Plan: 1 Month**\n\n"
            "Please pay using the following details:\n"
            f"• UPI ID: `{CLIENT_UPI_ID}`\n"
            f"• Name: {CLIENT_NAME}\n\n"
            "You can scan the QR code to pay.\n\n"
            "After making the payment, please send your "
            "**Payment Screenshot** or **Transaction ID (UTR)** "
            "right here in the chat for admin verification."
        )

        try:

            with open(QR_CODE_PATH, "rb") as photo:

                await query.message.reply_photo(
                    photo=photo,
                    caption=caption,
                    parse_mode="Markdown"
                )

        except FileNotFoundError:

            logger.error("QR code file not found: %s", QR_CODE_PATH)

            await query.message.reply_text(
                caption + "\n\n"
                "Note: QR image not found. Please pay directly "
                "using the UPI ID.",
                parse_mode="Markdown"
            )


# ==========================================
# 7. PAYMENT PROOF HANDLER
# ==========================================

async def handle_payment_proof(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if user.id == ADMIN_TELEGRAM_ID:
        return

    forward_caption = (
        "🔔 **New Payment Request Received!**\n\n"
        f"• User ID: `{user.id}`\n"
        f"• Name: {user.full_name}\n"
        f"• Username: @{user.username or 'None'}\n\n"
        "To approve this user, run this command:\n"
        f"/approve {user.id}"
    )

    try:

        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=forward_caption,
            parse_mode="Markdown"
        )

        if update.message.photo:

            await context.bot.send_photo(
                chat_id=ADMIN_TELEGRAM_ID,
                photo=update.message.photo[-1].file_id
            )

        elif update.message.text:

            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=f"User Text: {update.message.text}"
            )

        await update.message.reply_text(
            "Your payment proof has been sent to the admin. "
            "After verification, your 1-month subscription "
            "will be activated!"
        )

    except Exception:

        logger.exception("Failed to forward payment proof.")

        await update.message.reply_text(
            "Sorry, your payment proof could not be forwarded. "
            "Please try again later."
        )


# ==========================================
# 8. ADMIN APPROVAL COMMAND
# ==========================================

async def approve_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    admin_id = update.effective_user.id

    if admin_id != ADMIN_TELEGRAM_ID:

        await update.message.reply_text(
            "You are not authorized to use this command."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "Please provide a user ID.\n"
            "Example: /approve 123456789"
        )

        return

    try:

        target_user_id = int(context.args[0])

    except ValueError:

        await update.message.reply_text(
            "Invalid User ID format! Please use numbers only."
        )

        return

    expiry_date = datetime.now() + timedelta(days=30)

    expiry_str = expiry_date.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR REPLACE INTO users
        (user_id, username, expiry_date, status)
        VALUES (?, ?, ?, ?)
    """, (
        target_user_id,
        "User",
        expiry_str,
        "active"
    ))

    conn.commit()
    conn.close()

    try:

        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                "🎉 Congratulations! Your payment has been verified.\n"
                f"Your 1-month subscription is now active until "
                f"**{expiry_str}**."
            ),
            parse_mode="Markdown"
        )

        await update.message.reply_text(
            f"Success! User {target_user_id} "
            "ka subscription activate ho gaya hai."
        )

    except Exception as e:

        logger.exception("Could not notify approved user.")

        await update.message.reply_text(
            "Database update ho gaya, par user ko message "
            f"nahi gaya: {e}"
        )


# ==========================================
# 9. MAIN FUNCTION - WEBHOOK MODE
# ==========================================

def main():

    init_db()

    logger.info("Starting Telegram bot in WEBHOOK mode...")

    app = ApplicationBuilder().token(TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start))

    app.add_handler(CommandHandler("approve", approve_user))

    app.add_handler(CallbackQueryHandler(button_handler))

    app.add_handler(
        MessageHandler(
            filters.PHOTO | (filters.TEXT & ~filters.COMMAND),
            handle_payment_proof
        )
    )

    webhook_url = f"{EXTERNAL_URL}/{WEBHOOK_PATH}"

    logger.info("Starting webhook server on port %s", PORT)

    logger.info("Webhook endpoint configured.")

    # Start HTTP server and register Telegram webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET_TOKEN,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":

    main()
