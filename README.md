# Blackberry Bot - Vercel Webhook Version

Set `BOT_TOKEN` in Vercel Environment Variables. Do not put the token in GitHub.

This version uses `/tmp/blackberry_bot.db` because Vercel's deployed filesystem is read-only except for `/tmp`.
SQLite data in `/tmp` is temporary and is not a durable database.
