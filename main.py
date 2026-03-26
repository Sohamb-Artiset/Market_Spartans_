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


async def create_zoom_meeting(session_type, is_test=False):
    token      = await get_zoom_token()
    today      = date.today().strftime("%Y-%m-%d")
    meet_time  = SESSIONS[session_type]["meeting_time"]
    start_time = f"{today}T{meet_time}+05:30"   
    
    send_email = not is_test

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.zoom.us/v2/users/me/meetings",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "template_id": ZOOM_TEMPLATE_ID, 
                "topic": f"{SESSIONS[session_type]['label']} Session - {today}",
                "agenda": "Welcome to our daily session! Please find your details below.", 
                "start_time": start_time, 
                "type": 2,
                "duration": 360, 
                "settings": {
                    "approval_type": 0,      
                    "registration_type": 2,  # 👈 CRITICAL FIX: Strictly forces Registration ON
                    "registrants_email_notification": send_email,
                    "meeting_authentication": False,
                    "email_notification": True 
                }
            },
        )
        r.raise_for_status()
        data = r.json()
        return data["id"], data.get("registration_url", "")

async def import_registrants(meeting_id, csv_path):
    token       = await get_zoom_token()
    registrants = []

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2:
                email = row[0].strip()
                name  = row[1].strip()
                
                if email:
                    name_parts = name.split(" ", 1)
                    first = name_parts[0] if name_parts else "User"
                    
                    # 👈 CRITICAL FIX: Create the base object with ONLY required fields
                    person = {"first_name": first, "email": email}
                    
                    # Zoom STRICTLY rejects empty strings, so we only add last_name if it actually exists
                    if len(name_parts) > 1 and name_parts[1].strip():
                        person["last_name"] = name_parts[1].strip()
                        
                    registrants.append(person)

    if not registrants:
        raise ValueError("CSV has 0 valid registrants — aborting import.")

    async with httpx.AsyncClient() as client:
        for i in range(0, len(registrants), 30):
            batch = registrants[i : i + 30]
            r = await client.post(
                f"https://api.zoom.us/v2/meetings/{meeting_id}/batch_registrants", 
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "auto_approve": True,
                    "registrants": batch
                },
            )
            r.raise_for_status()

    return len(registrants)


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

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--ignore-certificate-errors", 
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",             
                "--disable-dev-shm-usage"   
            ]
        )
        context = await browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        try:
            await page.goto(f"{session['site_url']}/index.php", wait_until="domcontentloaded")
            
            await page.fill('input[name="username"]', MS_USERNAME)
            await page.fill('input[name="password"]', MS_PASSWORD)
            
            async with page.expect_navigation(wait_until="domcontentloaded"):
                await page.click('button[type="submit"]')
            
            await page.goto(session["export_url"], wait_until="domcontentloaded")

            async with page.expect_download(timeout=120_000) as dl_info:
                await page.locator('button[name="btnsavezoom"]:has-text("Export for Zoom")').click()

            download = await dl_info.value
            tmp_path = tempfile.mktemp(suffix=".csv")
            await download.save_as(tmp_path)
            return tmp_path
        finally:
            await browser.close()


# ── CSV COUNTER (test mode) ───────────────────────────────────────────────────
def count_csv(csv_path):
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2:
                rows.append({"email": row[0].strip(), "name": row[1].strip()})
                
    headers = ["Email", "Name"] 
    return len(rows), headers, rows[:3]


# ── AUTOMATION RUNNER ─────────────────────────────────────────────────────────
async def run_automation(session_type):
    try:
        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID, "⏳ Step 1/3 — Logging in & exporting users from Market Spartans..."
        )
        csv_path = await export_csv(session_type)

        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID, "⏳ Step 2/3 — Creating 6-Hour Zoom meeting from template..."
        )
        meeting_id, reg_url = await create_zoom_meeting(session_type, is_test=False)

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


# ── TEST RUNNER ───────────────────────────────────────────────────────────────
async def run_test(session_type, chat_id):
    try:
        await telegram_app.bot.send_message(
            chat_id,
            f"🧪 *TEST MODE — {SESSIONS[session_type]['label']} Session*\n"
            f"_Full silent test: Emails are BLOCKED._",
            parse_mode="Markdown",
        )

        await telegram_app.bot.send_message(chat_id, "⏳ Step 1/4 — Logging in & exporting CSV...")
        csv_path = await export_csv(session_type)
        count, headers, preview = count_csv(csv_path)

        preview_text = "\n".join(
            [f"  {i+1}. {list(r.values())[:2]}" for i, r in enumerate(preview)]
        )
        await telegram_app.bot.send_message(
            chat_id,
            f"✅ *CSV Export successful!*\n"
            f"👥 Total users found: *{count}*\n"
            f"📋 Columns: `{', '.join(headers)}`\n\n"
            f"First 3 rows:\n{preview_text}",
            parse_mode="Markdown",
        )

        await telegram_app.bot.send_message(chat_id, "⏳ Step 2/4 — Creating 6-hour Zoom meeting (Silent)...")
        meeting_id, reg_url = await create_zoom_meeting(session_type, is_test=True)
        
        await telegram_app.bot.send_message(chat_id, "⏳ Step 3/4 — Testing Registrant Import...")
        imported_count = await import_registrants(meeting_id, csv_path)

        await telegram_app.bot.send_message(
            chat_id,
            f"✅ *Test Import successful!*\n"
            f"👥 Imported: {imported_count}\n"
            f"🔗 {reg_url}",
            parse_mode="Markdown",
        )

        await telegram_app.bot.send_message(chat_id, "⏳ Step 4/4 — Deleting dummy meeting...")
        await delete_zoom_meeting(meeting_id)

        await telegram_app.bot.send_message(
            chat_id,
            f"✅ *TEST COMPLETE — Everything works!*\n\n"
            f"✅ Market Spartans login → OK\n"
            f"✅ Strict Button Click → OK\n"
            f"✅ 6-Hour Meeting Created → OK\n"
            f"✅ Registrant Import → OK\n"
            f"✅ Dummy meeting deleted → OK\n\n"
            f"_Ready for real runs at 8:30 AM & 4:30 PM IST_",
            parse_mode="Markdown",
        )

    except Exception as e:
        logging.error(f"Test error [{session_type}]: {e}")
        await telegram_app.bot.send_message(
            chat_id,
            f"❌ *Test failed!*\n\nError: `{str(e)}`",
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

    async def auto_skip():
        await asyncio.sleep(15 * 60)
        logging.info(f"Auto-skipping {session_type} — no response in 15 min")
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

    if session_type in pending_jobs:
        pending_jobs[session_type].cancel()
        del pending_jobs[session_type]

    if action == "yes":
        await query.edit_message_text("👍 Got it! Starting automation now...")
        asyncio.create_task(run_automation(session_type))
    else:
        await query.edit_message_text("👍 Skipped for today. See you next session!")


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    if not context.args or context.args[0].lower() not in ("morning", "evening"):
        await update.message.reply_text("Usage:\n/test morning\n/test evening")
        return
    asyncio.create_task(run_test(context.args[0].lower(), update.effective_chat.id))


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

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)

    logging.info("✅ Bot running — scheduled at 8:30 AM & 4:30 PM IST")

    stop_event = asyncio.Event()
    signal.signal(signal.SIGTERM, lambda s, f: stop_event.set())
    await stop_event.wait()


if __name__ == "__main__":
    asyncio.run(main())
