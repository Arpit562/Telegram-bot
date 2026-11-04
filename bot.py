import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters


# --- CONFIGURATION ---
TOKEN = ''
ADMIN_TELEGRAM_ID = 8904576835      # Admin ki numeric Telegram ID
CLIENT_UPI_ID = "abhijeetvaish1@ybl"
CLIENT_NAME = "abhijeet's records"
QR_CODE_PATH = "QR_code/QR2.jpeg"
DB_PATH = "bot_users.db"

# --- DATABASE CONNECTION & SETUP ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            expiry_date TEXT,
            status TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- START COMMAND ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT expiry_date, status FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        expiry_date_str, status = row
        expiry_date = datetime.strptime(expiry_date_str, "%Y-%m-%d")

        if status == "active" and datetime.now() < expiry_date:
            await update.message.reply_text(
                f"Welcome back {username}! 👋\n"
                f"Your subscription is active until **{expiry_date_str}**.",
                parse_mode="Markdown"
            )
            return

    keyboard = [[InlineKeyboardButton("Buy / Renew Subscription", callback_data="buy_subscription")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Welcome {username}! \n"
        "Please choose an option below to access the services:",
        reply_markup=reply_markup
    )

# --- BUY BUTTON CLICK HANDLER ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "buy_subscription":
        caption = (
            f" **Subscription Plan: 1 Month**\n\n"
            f"Please pay using the following details:\n"
            f"• UPI ID: `{CLIENT_UPI_ID}`\n"
            f"• Name: {CLIENT_NAME}\n\n"
            f"You can scan the QR code above/below to pay.\n"
            f"After making the payment, please send your **Payment Screenshot** or **Transaction ID (UTR)** right here in the chat for admin verification."
        )

        try:
            with open(QR_CODE_PATH, "rb") as photo:
                await query.message.reply_photo(photo=photo, caption=caption, parse_mode="Markdown")
        except FileNotFoundError:
            await query.message.reply_text(
                f"{caption}\n\n*(Note: QR image file not found, please pay directly to the UPI ID)*",
                parse_mode="Markdown"
            )

# --- PAYMENT PROOF HANDLER ---
async def handle_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Agar admin khud kuch type karega, toh usko payment proof mat mano
    if user.id == ADMIN_TELEGRAM_ID:
        return

    forward_caption = (
        f"🔔 **New Payment Request Received!**\n\n"
        f"• User ID: `{user.id}`\n"
        f"• Name: {user.full_name}\n"
        f"• Username: @{user.username or 'None'}\n\n"
        f"To approve this user, run this command:\n"
        f"/approve {user.id}"
    )

    # Send notification and proof to Admin
    await context.bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=forward_caption, parse_mode="Markdown")
    
    if update.message.photo:
        await context.bot.send_photo(chat_id=ADMIN_TELEGRAM_ID, photo=update.message.photo[-1].file_id)
    elif update.message.text:
        await context.bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=f"User Text: {update.message.text}")

    await update.message.reply_text("Your payment proof has been sent to the admin. After verification, your 1-month subscription will be activated!")

# --- ADMIN APPROVAL COMMAND ---
async def approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id

    if admin_id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("You are not authorized to use this command.")
        return

    if not context.args:
        await update.message.reply_text("Please provide a user ID to approve. Example: /approve 123456789")
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid User ID format! Please use numbers only.")
        return

    expiry_date = datetime.now() + timedelta(days=30)
    expiry_str = expiry_date.strftime("%Y-%m-%d")

    

    # Database me save karo
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users (user_id, username, expiry_date, status)
        VALUES (?, ?, ?, ?)
    ''', (target_user_id, "User", expiry_str, "active"))
    conn.commit()
    conn.close()

    # User ko message bhejo
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"🎉 Congratulations! Your payment has been verified.\n"
                 f"Your 1-month subscription is now active until **{expiry_str}**.",
            parse_mode="Markdown"
        )
        await update.message.reply_text(f"Success! User {target_user_id} ka subscription activate ho gaya hai.")
    except Exception as e:
        await update.message.reply_text(f"Database update ho gaya, par user ko message nahi gaya. (Ho sakta hai user ne bot ko start na kiya ho): {e}")

        print("Admin ID:", admin_id)
        print("Target User ID:", target_user_id)
# --- MAIN FUNCTION ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    # Handlers (Commands sabse upar)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("approve", approve_user))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Message Handler (Strictly ignoring commands and admin's own chats)
    app.add_handler(MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), handle_payment_proof))

    print("Bot backend successfully started bhai!")
    app.run_polling()

