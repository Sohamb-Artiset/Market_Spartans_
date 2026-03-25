import os
import asyncio
import httpx
import csv
import tempfile
import logging
import signal
from datetime import date
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from playwright.async_api import async_playwright

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ── ENV ───────────────────────────────────────────────────────────────────────
MS_USERNAME        = os.getenv("MS_USERNAME")
MS_PASSWORD        = os.getenv("MS_PASSWORD")
ZOOM_ACCOUNT_ID    = os.getenv("ZOOM_ACCOUNT_ID")
ZOOM_CLIENT_ID     = os.getenv("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = os.getenv("ZOOM_CLIENT_SECRET")
ZOOM_TEMPLATE_ID   = os.getenv("ZOOM_TEMPLATE_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID"))

SESSIONS = {
    "morning": {
        "label":        "🌅 Morning",
        "site_url":     os.getenv("MORNING_SITE_URL"),
        "export_url":   os.getenv("MORNING_EXPORT_URL"),
        "meeting_time": os.getenv("MORNING_MEETING_TIME"),  # 09:00:00
    },
    "evening": {
        "label":        "🌆 Evening",
        "site_url":     os.getenv("EVENING_SITE_URL"),
        "export_url":   os.getenv("EVENING_EXPORT_URL"),
        "meeting_time": os.getenv("EVENING_MEETING_TIME"),  # 17:00:00
    },
}

# Global references
telegram_app = None
pending_jobs  = {}   # session_type -> asyncio.Task (auto-skip timer)


# ── ZOOM API ──────────────────────────────────────────────────────────────────
async def get_zoom_token():
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://zoom.us/oauth/token",
            params={"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID},
            auth=(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET),
        )
        r.raise_for_status()
        return r.json()["access_token"]


async def create_zoom_meeting(session_type):
    token      = await get_zoom_token()
    today      = date.today().strftime("%Y-%m-%d")
    meet_time  = SESSIONS[session_type]["meeting_time"]
    start_time = f"{today}T{meet_time}+05:30"   # IST = UTC+5:30

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.zoom.us/v2/users/me/meetings",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"template_id": ZOOM_TEMPLATE_ID, "start_time": start_time, "type": 2},
        )
        r.raise_for_status()
        data = r.json()
        return data["id"], data.get("registration_url", "")


async def import_registrants(meeting_id, csv_path):
    token       = await get_zoom_token()
    registrants = []

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = (
                row.get("Email") or row.get("email") or ""
            ).strip()
            first = (
                row.get("First Name") or row.get("first_name") or
                row.get("FirstName")  or row.get("Name") or ""
            ).strip()
            last  = (
                row.get("Last Name") or row.get("last_name") or
                row.get("LastName")  or ""
            ).strip()
            if email:
                registrants.append({"first_name": first, "last_name": last, "email": email})

    if not registrants:
        raise ValueError("CSV has 0 valid registrants — aborting import.")

    async with httpx.AsyncClient() as client:
        # Zoom allows max 30 registrants per batch request
        for i in range(0, len(registrants), 30):
            batch = registrants[i : i + 30]
            r = await client.post(
                f"https://api.zoom.us/v2/meetings/{meeting_id}/registrants/batch",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"registrants": batch},
            )
            r.raise_for_status()

    return len(registrants)


# ── CSV COUNTER (test mode — no Zoom import) ──────────────────────────────────
def count_csv(csv_path):
    """Read CSV and return (count, first 3 rows preview) without importing anyone."""
    rows    = []
    headers = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader  = csv.DictReader(f)
        headers = reader.fieldnames or []
        for row in reader:
            rows.append(row)
    preview = rows[:3]
    return len(rows), headers, preview


# ── ZOOM DELETE (test cleanup) ─────────────────────────────────────────────────
async def delete_zoom_meeting(meeting_id):
    token = await get_zoom_token()
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"https://api.zoom.us/v2/meetings/{meeting_id}",
            headers={"Authorization": f"Bearer {token}"},
        )


# ── PLAYWRIGHT ────────────────────────────────────────────────────────────────
async def export_csv(session_type):
    session = SESSIONS[session_type]
    today   = date.today().strftime("%Y-%m-%d")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        # 1. Login
        await page.goto(f"{session['site_url']}/index.php")
        await page.wait_for_load_state("networkidle")
        await page.fill('input[name="username"]', MS_USERNAME)
        await page.fill('input[name="password"]', MS_PASSWORD)
        await page.click('button[type="submit"]')
        await page.wait_for_load_state("networkidle")

        # 2. Go to export page
        await page.goto(session["export_url"])
        await page.wait_for_load_state("networkidle")

        # 3. Set today's date in both date fields
        await page.fill('input[name="fromdate"]', today)
        await page.fill('input[name="todate"]',   today)

        # 4. Click Export and capture the download
        async with page.expect_download(timeout=30_000) as dl_info:
            await page.click('button[name="btnsavezoom"]')

        download  = await dl_info.value
        tmp_path  = tempfile.mktemp(suffix=".csv")
        await download.save_as(tmp_path)
        await browser.close()

    return tmp_path


# ── AUTOMATION RUNNER ─────────────────────────────────────────────────────────
async def run_automation(session_type):
    try:
        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID, "⏳ Step 1/3 — Logging in & exporting users from Market Spartans..."
        )
        csv_path = await export_csv(session_type)

        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID, "⏳ Step 2/3 — Creating Zoom meeting from template..."
        )
        meeting_id, reg_url = await create_zoom_meeting(session_type)

        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID, "⏳ Step 3/3 — Importing registrants into Zoom..."
        )
        count = await import_registrants(meeting_id, csv_path)

        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID,
            f"✅ All done!\n"
            f"👥 {count} registrants imported\n\n"
            f"📋 *Registration Link:*\n{reg_url}\n\n"
            f"👆 Copy & paste this to the WhatsApp group",
            parse_mode="Markdown",
        )

    except Exception as e:
        logging.error(f"Automation error [{session_type}]: {e}")
        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID,
            f"❌ *Automation failed!*\n\nError: `{str(e)}`\n\nPlease run manually today.",
            parse_mode="Markdown",
        )


# ── TELEGRAM ──────────────────────────────────────────────────────────────────
async def send_confirmation(session_type):
    session  = SESSIONS[session_type]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, Run It",  callback_data=f"yes_{session_type}"),
        InlineKeyboardButton("❌ Skip Today",   callback_data=f"no_{session_type}"),
    ]])

    await telegram_app.bot.send_message(
        TELEGRAM_CHAT_ID,
        f"{session['label']} session automation is ready!\n"
        f"Should I run it now?\n\n"
        f"_(Auto-skips in 15 minutes if no response)_",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )

    # Auto-skip timer — cancels if Dad responds
    async def auto_skip():
        await asyncio.sleep(15 * 60)
        logging.info(f"Auto-skipping {session_type} session (no response in 15 min)")
        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID,
            f"⏰ No response — {session['label']} session auto-skipped for today.",
        )

    task = asyncio.create_task(auto_skip())
    pending_jobs[session_type] = task


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, session_type = query.data.split("_", 1)

    # Cancel the auto-skip timer
    if session_type in pending_jobs:
        pending_jobs[session_type].cancel()
        del pending_jobs[session_type]

    if action == "yes":
        await query.edit_message_text("👍 Got it! Starting automation now...")
        asyncio.create_task(run_automation(session_type))
    else:
        await query.edit_message_text("👍 Skipped for today. See you next session!")


# ── TEST RUNNER ───────────────────────────────────────────────────────────────
async def run_test(session_type, chat_id):
    """
    Runs the full automation EXCEPT importing registrants.
    Creates a real Zoom meeting then immediately deletes it.
    Zero emails sent to real users.
    """
    try:
        await telegram_app.bot.send_message(
            chat_id,
            f"🧪 *TEST MODE — {SESSIONS[session_type]['label']} Session*\n"
            f"_Registrants will NOT be imported. No emails sent to users._",
            parse_mode="Markdown",
        )

        # Step 1 — Export CSV
        await telegram_app.bot.send_message(chat_id, "⏳ Step 1/3 — Logging in & exporting CSV from Market Spartans...")
        csv_path = await export_csv(session_type)
        count, headers, preview = count_csv(csv_path)

        preview_text = "\n".join(
            [f"  {i+1}. {list(r.values())[:3]}" for i, r in enumerate(preview)]
        )
        await telegram_app.bot.send_message(
            chat_id,
            f"✅ *CSV Export successful!*\n"
            f"👥 Total users found: *{count}*\n"
            f"📋 Columns: `{', '.join(headers)}`\n\n"
            f"First 3 rows preview:\n{preview_text}",
            parse_mode="Markdown",
        )

        # Step 2 — Create Zoom meeting
        await telegram_app.bot.send_message(chat_id, "⏳ Step 2/3 — Creating Zoom meeting from template...")
        meeting_id, reg_url = await create_zoom_meeting(session_type)
        await telegram_app.bot.send_message(
            chat_id,
            f"✅ *Zoom meeting created!*\n"
            f"🆔 Meeting ID: `{meeting_id}`\n"
            f"🔗 Registration Link: {reg_url}",
            parse_mode="Markdown",
        )

        # Step 3 — Skip import, delete dummy meeting
        await telegram_app.bot.send_message(chat_id, "⏳ Step 3/3 — Skipping import (test mode) & deleting dummy meeting...")
        await delete_zoom_meeting(meeting_id)

        await telegram_app.bot.send_message(
            chat_id,
            f"✅ *TEST COMPLETE — Everything works!*\n\n"
            f"✅ Market Spartans login → OK\n"
            f"✅ CSV export → OK ({count} users)\n"
            f"✅ Zoom meeting creation → OK\n"
            f"✅ Dummy meeting deleted → OK\n"
            f"⏭️ Registrant import → Skipped (test mode)\n\n"
            f"_When ready for real run, use the scheduled 8:30 AM / 4:30 PM triggers._",
            parse_mode="Markdown",
        )

    except Exception as e:
        logging.error(f"Test error [{session_type}]: {e}")
        await telegram_app.bot.send_message(
            chat_id,
            f"❌ *Test failed at this step!*\n\nError: `{str(e)}`",
            parse_mode="Markdown",
        )


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage:
      /test morning
      /test evening
    """
    # Only respond to Dad
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    args = context.args
    if not args or args[0].lower() not in ("morning", "evening"):
        await update.message.reply_text(
            "Usage:\n/test morning\n/test evening"
        )
        return

    session_type = args[0].lower()
    asyncio.create_task(run_test(session_type, update.effective_chat.id))


# ── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    global telegram_app

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(CommandHandler("test", test_command))

    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(send_confirmation, "cron", hour=8,  minute=30, args=["morning"])
    scheduler.add_job(send_confirmation, "cron", hour=16, minute=30, args=["evening"])
    scheduler.start()

    stop_event = asyncio.Event()

    def _handle_stop(signum, frame):
        logging.info(f"Received signal {signum}, shutting down...")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)

    logging.info("✅ Bot running — scheduled at 8:30 AM & 4:30 PM IST")
    await stop_event.wait()

    logging.info("Stopping bot...")
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
