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
        "meeting_time": os.getenv("MORNING_MEETING_TIME"),
    },
    "evening": {
        "label":        "🌆 Evening",
        "site_url":     os.getenv("EVENING_SITE_URL"),
        "export_url":   os.getenv("EVENING_EXPORT_URL"),
        "meeting_time": os.getenv("EVENING_MEETING_TIME"),
    },
}

telegram_app = None
pending_jobs  = {}

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
    start_time = f"{today}T{meet_time}+05:30"

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
            email = (row.get("Email") or row.get("email") or "").strip()
            first = (row.get("First Name") or row.get("first_name") or row.get("FirstName") or row.get("Name") or "").strip()
            last  = (row.get("Last Name") or row.get("last_name") or row.get("LastName") or "").strip()
            if email:
                registrants.append({"first_name": first, "last_name": last, "email": email})

    if not registrants:
        raise ValueError("CSV has 0 valid registrants — aborting import.")

    async with httpx.AsyncClient() as client:
        for i in range(0, len(registrants), 30):
            batch = registrants[i : i + 30]
            r = await client.post(
                f"https://api.zoom.us/v2/meetings/{meeting_id}/registrants/batch",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"registrants": batch},
            )
            r.raise_for_status()
    return len(registrants)

def count_csv(csv_path):
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        for row in reader:
            rows.append(row)
    return len(rows), headers, rows[:3]

async def delete_zoom_meeting(meeting_id):
    token = await get_zoom_token()
    async with httpx.AsyncClient() as client:
        await client.delete(f"https://api.zoom.us/v2/meetings/{meeting_id}", headers={"Authorization": f"Bearer {token}"})

# ── PLAYWRIGHT (UPDATED) ──────────────────────────────────────────────────────
async def export_csv(session_type):
    session = SESSIONS[session_type]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()
        page.set_default_navigation_timeout(60000)

        try:
            # 1. Login
            await page.goto(f"{session['site_url']}/index.php")
            await page.wait_for_load_state("networkidle")
            await page.fill('input[name="username"]', MS_USERNAME)
            await page.fill('input[name="password"]', MS_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")

            # 2. Go directly to export URL
            await page.goto(session["export_url"])
            await page.wait_for_load_state("networkidle")

            # 3. Capture Download (Using specific button name 'btnsavezoom')
            async with page.expect_download(timeout=60_000) as dl_info:
                # Matches your specific button element
                await page.click('button[name="btnsavezoom"]')

            download  = await dl_info.value
            tmp_path  = tempfile.mktemp(suffix=".csv")
            await download.save_as(tmp_path)
            return tmp_path

        except Exception as e:
            # Take a screenshot on failure to see the page state
            await page.screenshot(path="error_state.png")
            logging.error(f"Playwright error: {e}")
            raise e
        finally:
            await browser.close()

# ── AUTOMATION RUNNER ─────────────────────────────────────────────────────────
async def run_automation(session_type):
    try:
        await telegram_app.bot.send_message(TELEGRAM_CHAT_ID, "⏳ Step 1/3 — Exporting users...")
        csv_path = await export_csv(session_type)
        await telegram_app.bot.send_message(TELEGRAM_CHAT_ID, "⏳ Step 2/3 — Creating Zoom meeting...")
        meeting_id, reg_url = await create_zoom_meeting(session_type)
        await telegram_app.bot.send_message(TELEGRAM_CHAT_ID, "⏳ Step 3/3 — Importing registrants...")
        count = await import_registrants(meeting_id, csv_path)
        await telegram_app.bot.send_message(TELEGRAM_CHAT_ID, f"✅ Done! {count} imported.\n{reg_url}")
    except Exception as e:
        await telegram_app.bot.send_message(TELEGRAM_CHAT_ID, f"❌ Failed: {e}")

# ── TELEGRAM HANDLERS ─────────────────────────────────────────────────────────
async def send_confirmation(session_type):
    session = SESSIONS[session_type]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Run", callback_data=f"yes_{session_type}"),
        InlineKeyboardButton("❌ Skip", callback_data=f"no_{session_type}"),
    ]])
    await telegram_app.bot.send_message(TELEGRAM_CHAT_ID, f"Run {session['label']} session?", reply_markup=keyboard)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, session_type = query.data.split("_", 1)
    if action == "yes":
        asyncio.create_task(run_automation(session_type))
    await query.edit_message_text("Processing...")

async def run_test(session_type, chat_id):
    try:
        await telegram_app.bot.send_message(chat_id, "🧪 Starting Test...")
        csv_path = await export_csv(session_type)
        count, headers, preview = count_csv(csv_path)
        await telegram_app.bot.send_message(chat_id, f"✅ CSV Exported: {count} users found.")
        meeting_id, reg_url = await create_zoom_meeting(session_type)
        await delete_zoom_meeting(meeting_id)
        await telegram_app.bot.send_message(chat_id, "✅ Test Complete!")
    except Exception as e:
        await telegram_app.bot.send_message(chat_id, f"❌ Test Failed: {e}")

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID: return
    if not context.args: return
    asyncio.create_task(run_test(context.args[0].lower(), update.effective_chat.id))

async def main():
    global telegram_app
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(CommandHandler("test", test_command))
    
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(send_confirmation, "cron", hour=8, minute=30, args=["morning"])
    scheduler.add_job(send_confirmation, "cron", hour=16, minute=30, args=["evening"])
    scheduler.start()

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())