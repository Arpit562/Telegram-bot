# Telegram Subscription Bot

The bot sells 30- and 60-day subscriptions using UPI, sends uploaded payment
screenshots to an admin for manual approval, and manages private-channel
access using SQLite.

## Run

Install dependencies with `pip install -r requirements.txt`, then start the
service with `python bot.py`.

## Required environment variables

- `BOT_TOKEN`: Telegram bot token.
- `ADMIN_TELEGRAM_ID`: Telegram user ID that reviews payments.
- `CHANNEL_IDS`: comma-separated private channel IDs.
- `UPI_ID`: optional fallback UPI ID (defaults to the configured UPI ID in
  `bot.py`).
- `WEBHOOK_SECRET_TOKEN`: webhook secret containing only letters, numbers,
  underscores, or hyphens.
- `RENDER_EXTERNAL_URL` or `WEBHOOK_URL`: public HTTPS URL of the deployed app.

`PORT` defaults to `10000`; `WEBHOOK_PATH` defaults to `telegram-webhook`;
`DB_PATH` defaults to `bot_users.db`. Configure persistent storage for the
database if the hosting platform's local filesystem is ephemeral. The QR image
must be deployed at `QR_code/QR2.jpeg`; 30-day and 60-day plans cost ₹499 and
₹899 respectively. Admins must verify UPI payments manually before approving
subscriptions.
