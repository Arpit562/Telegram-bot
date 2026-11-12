# Telegram Subscription Bot

The bot sells 30- and 60-day subscriptions for Telegram Stars (XTR), sends
payments to an admin for approval, and manages private-channel access using
SQLite.

## Run

Install dependencies with `pip install -r requirements.txt`, then start the
service with `python bot.py`.

## Required environment variables

- `BOT_TOKEN`: Telegram bot token.
- `ADMIN_TELEGRAM_ID`: Telegram user ID that reviews payments.
- `CHANNEL_IDS`: comma-separated private channel IDs.
- `PRICE_30_DAYS_XTR` and `PRICE_60_DAYS_XTR`: positive prices in Stars.
- `WEBHOOK_SECRET_TOKEN`: webhook secret containing only letters, numbers,
  underscores, or hyphens.
- `RENDER_EXTERNAL_URL` or `WEBHOOK_URL`: public HTTPS URL of the deployed app.

`PORT` defaults to `10000`; `WEBHOOK_PATH` defaults to `telegram-webhook`;
`DB_PATH` defaults to `bot_users.db`. Configure persistent storage for the
database if the hosting platform's local filesystem is ephemeral.
