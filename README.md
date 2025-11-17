# Multi-Account Login Manager (Telegram) — Safe System

**What this does (safe & allowed):**
- Lets you add multiple Telegram *user* accounts via a Bot UI (enter API_ID, API_HASH, phone & OTP).
- Saves user session files (`sessions/<name>.session`) on the server.
- Maintains a SQLite link DB (`links.db`) with link classification (group/channel/bot/folder).
- Provides per-account cooldown estimator (EMA) that *learns* from your manual reports (success / floodwait).
- Provides “join assist” (manual) lists for you to click/open on your phone per-account with recommended delays.
- DOES NOT automatically join groups with user accounts — manual action required.

**Stack**
- Bot: `aiogram` (Telegram Bot API) — provides UI to add accounts, list them, request OTP, and report join results.
- User session login: `Telethon` — programmatically handles phone/OTP and saves session files.
- DB: `sqlite3`
- Deploy: Docker + Render (or any VPS)

**Security note**
- **Do not** commit `sessions/` or `.env` to GitHub. `.gitignore` includes them.
- Use HTTPS/Render secrets for BOT_TOKEN, etc.

---

## Quick start (local)

1. Create venv & install:
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
