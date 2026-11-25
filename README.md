# decoded-bharat-autopost (private)

Always-on posting for @decodedbharat.ai. GitHub Actions runs `poster.py` every 5 minutes:
reads Telegram approval taps, posts approved + due reels. Your PC can be off.

- `jobs/<id>.json`  one file per reel (status: pending -> scheduled -> posting -> posted / failed)
- `videos/<id>.mp4` the reel (deleted after it is posted)
- `state.json`      Telegram update offset

Secrets required (Settings -> Secrets and variables -> Actions): IG_ACCESS_TOKEN, IG_USER_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
Never commit tokens. Times in jobs are ISO-8601 with offset (IST = +05:30).
