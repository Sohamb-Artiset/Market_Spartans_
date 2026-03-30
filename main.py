import os
import asyncio
import httpx
import csv
import tempfile
import logging
import signal
import html  
import re  
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone, timedelta
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
        "zoom_topic":   "Options Learnings with Mangesh Kale", 
        "site_url":     os.getenv("MORNING_SITE_URL"),
        "export_url":   os.getenv("MORNING_EXPORT_URL"),
        "meeting_time": os.getenv("MORNING_MEETING_TIME"),
    },
    "evening": {
        "label":        "🌆 Evening",
        "zoom_topic":   "Forex with Mangesh Kale", 
        "site_url":     os.getenv("EVENING_SITE_URL"),
        "export_url":   os.getenv("EVENING_EXPORT_URL"),
        "meeting_time": os.getenv("EVENING_MEETING_TIME"),
    },
}

MEETING_AGENDA = """Zoom ke इस Social Gathering में, हम कोई ट्रेडिंग सुझाव/Tips/Advice नहीं दे रहे हैं, इस group से कोई भी आपको investment proposals or trading tips देने के लिए authorized SEBI Registered नहीं है. इस gathering का उद्देश्य moves of stock market पर केवल चर्चा है। शेयर बाजार को समझना एक सामूहिक प्रयास है और यहां कोई भी एंकर या मेंटर नहीं है, यहाँ तक की मंगेश काले भी नहीं। शेयर बाजार में कोई भी ट्रेड करना या ना करना आपकी खुद की जिम्मेदारी होगी। आपके ट्रेडों के परिणाम के लिए आपके अलावा कोई और जिम्मेदार नहीं होगा क्योंकि यह आपका अपना निर्णय होगा।

ये सभी बातें मुझे समझ आ गई है, और मै ऊपर लिखी बातो को सहमती दर्शाते हुए अपने स्वयं के निर्णय से इस मीटिंग में जॉइन हो रहा हू |"""

telegram_app = None
pending_jobs = {}   

# ── PRE-APPROVAL STATE ────────────────────────────────────────────────────────
pre_approved = {"morning": False, "evening": False}

async def reset_pre_approvals():
    global pre_approved
    pre_approved = {"morning": False, "evening": False}
    logging.info("Midnight reset: Pre-approvals wiped for the new day.")

# ── GOOGLE SHEETS DATABASE ────────────────────────────────────────────────────
def get_google_sheet():
    """Connects to Google Sheets using the bot's VIP key."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_file("google_credentials.json", scopes=scopes)
    client = gspread.authorize(creds)
    return client.open("Zoom Verified Users").sheet1

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
    
    ist        = timezone(timedelta(hours=5, minutes=30))
    today      = datetime.now(ist).strftime("%Y-%m-%d")
    
    meet_time  = SESSIONS[session_type]["meeting_time"]
    start_time = f"{today}T{meet_time}+05:30"   
    
    send_email = not is_test

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.zoom.us/v2/users/me/meetings",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "template_id": ZOOM_TEMPLATE_ID, 
                "topic": f"{SESSIONS[session_type]['zoom_topic']} - {today}", 
                "agenda": MEETING_AGENDA,  
                "start_time": start_time, 
                "type": 2,
                "duration": 360, 
                "settings": {
                    "approval_type": 0,  
                    "registration_type": 2,  
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
    token = await get_zoom_token()
    
    # 1. FETCH DATABASE SYNC FUNCTION
    def fetch_sheet_data():
        sheet = get_google_sheet()
        records = sheet.get_all_records()
        db = {}
        for i, row in enumerate(records):
            db[row['Original Email']] = {
                'row_num': i + 2,
                'zoom_email': str(row.get('Zoom Email', '')),
                'name': str(row.get('Name', '')),
                'status': str(row.get('Status', ''))
            }
        return sheet, db
        
    await telegram_app.bot.send_message(TELEGRAM_CHAT_ID, "🔍 Checking Google Sheets Database...")
    sheet, db = await asyncio.to_thread(fetch_sheet_data)

    registrants = []
    updates_to_make = [] 
    new_rows_to_add = []

    # 2. PROCESS CSV AND CROSS-CHECK
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2:
                raw_email = row[0].strip()
                clean_csv_email = re.sub(r'\s+', '', raw_email).lower() 
                name = row[1].strip()
                
                if clean_csv_email:
                    final_email = clean_csv_email
                    
                    db_entry = db.get(raw_email)
                    if db_entry and db_entry['zoom_email'].strip():
                        final_email = db_entry['zoom_email'].strip()
                    
                    name_parts = name.split(" ", 1)
                    first = name_parts[0].strip() if name_parts and name_parts[0].strip() else "User"
                    
                    person = {
                        "first_name": first, 
                        "email": final_email,
                        "original_email": raw_email,
                        "full_name": name
                    }
                    
                    if len(name_parts) > 1 and name_parts[1].strip():
                        person["last_name"] = name_parts[1].strip()
                    else:
                        person["last_name"] = "-"
                        
                    registrants.append(person)

    if not registrants:
        raise ValueError("CSV has 0 valid registrants — aborting import.")

    success_count = 0
    failed_emails = [] 
    
    # 3. REGISTER IN ZOOM
    async with httpx.AsyncClient() as client:
        for person in registrants:
            zoom_payload = {"first_name": person["first_name"], "email": person["email"], "last_name": person["last_name"]}

            r = await client.post(
                f"https://api.zoom.us/v2/meetings/{meeting_id}/registrants", 
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=zoom_payload,
            )
            
            status_to_write = "Failed"
            if r.status_code < 400 or "already registered" in r.text.lower():
                success_count += 1
                status_to_write = "Verified"
            else:
                logging.error(f"Zoom Error for {person['email']}: {r.text}")
                failed_emails.append(person['original_email'])
                
            # 4. PREPARE DATABASE UPDATES
            orig_email = person["original_email"]
            if orig_email in db:
                if db[orig_email]['status'] != status_to_write:
                    updates_to_make.append({
                        'range': f'D{db[orig_email]["row_num"]}',
                        'values': [[status_to_write]]
                    })
            else:
                new_rows_to_add.append([orig_email, "", person["full_name"], status_to_write])

            await asyncio.sleep(0.1)

    # 5. PUSH UPDATES TO GOOGLE SHEETS
    def apply_sheet_updates():
        if updates_to_make:
            sheet.batch_update(updates_to_make)
        if new_rows_to_add:
            sheet.append_rows(new_rows_to_add)

    await asyncio.to_thread(apply_sheet_updates)

    return success_count, failed_emails


async def lock_meeting_registration(meeting_id):
    """Locks the meeting to manual approval after CSV users are safely imported."""
    token = await get_zoom_token()
    async with httpx.AsyncClient() as client:
        r = await client.patch(
            f"https://api.zoom.us/v2/meetings/{meeting_id}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"settings": {"approval_type": 1}} 
        )
        if r.status_code >= 400:
            logging.error(f"Failed to lock meeting: {r.text}")


async def delete_zoom_meeting(meeting_id):
    token = await get_zoom_token()
    async with httpx.AsyncClient() as client:
        r = await client.delete(
            f"https://api.zoom.us/v2/meetings/{meeting_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code >= 400:
            logging.warning(f"Could not delete dummy meeting (Missing Scope). ID: {meeting_id}")
            await telegram_app.bot.send_message(
                TELEGRAM_CHAT_ID, 
                f"⚠️ <b>Note:</b> I couldn't delete the dummy meeting because I lack the Zoom permission. You can ignore this for now, but delete it manually later!",
                parse_mode="HTML"
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
    csv_path = None
    try:
        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID, "⏳ Step 1/4 — Logging in & exporting users from Market Spartans..."
        )
        csv_path = await export_csv(session_type)

        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID, "⏳ Step 2/4 — Creating 6-Hour Zoom meeting from template..."
        )
        meeting_id, reg_url = await create_zoom_meeting(session_type, is_test=False)

        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID, "⏳ Step 3/4 — Importing registrants and updating Database..."
        )
        count, failed_emails = await import_registrants(meeting_id, csv_path)

        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID, "⏳ Step 4/4 — Locking registration for WhatsApp link..."
        )
        await lock_meeting_registration(meeting_id)

        report_text = f"✅ <b>{SESSIONS[session_type]['label']} Session complete!</b>\n\n👥 <b>Success:</b> {count} users added."
        if failed_emails:
            failed_list_formatted = "\n".join([f"• <code>{email}</code>" for email in failed_emails])
            report_text += f"\n❌ <b>Failed:</b> {len(failed_emails)} users rejected.\n\n"
            report_text += f"📋 <b>Rejected emails:</b>\n{failed_list_formatted}\n\n"
            report_text += "👆 Check Google Sheets to fix these!"

        report_text += f"\n\n🔗 <b>WhatsApp Link (Requires Approval):</b>\n{reg_url}"

        await telegram_app.bot.send_message(TELEGRAM_CHAT_ID, report_text, parse_mode="HTML", disable_web_page_preview=True)

    except Exception as e:
        logging.error(f"Automation error [{session_type}]: {e}")
        safe_error = html.escape(str(e)) 
        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID,
            f"❌ <b>Automation failed!</b>\n\nError: <code>{safe_error}</code>\n\nPlease run manually today.",
            parse_mode="HTML", 
        )
    finally:
        if csv_path and os.path.exists(csv_path):
            os.remove(csv_path)


# ── TEST RUNNER ───────────────────────────────────────────────────────────────
async def run_test(session_type, chat_id):
    csv_path = None
    try:
        await telegram_app.bot.send_message(
            chat_id,
            f"🧪 <b>TEST MODE — {SESSIONS[session_type]['label']} Session</b>\n"
            f"<i>Full silent test: Emails are BLOCKED.</i>",
            parse_mode="HTML",
        )

        await telegram_app.bot.send_message(chat_id, "⏳ Step 1/5 — Logging in & exporting CSV...")
        csv_path = await export_csv(session_type)
        count, headers, preview = count_csv(csv_path)

        preview_text = "\n".join(
            [f"  {i+1}. {html.escape(str(list(r.values())[:2]))}" for i, r in enumerate(preview)]
        )
        await telegram_app.bot.send_message(
            chat_id,
            f"✅ <b>CSV Export successful!</b>\n"
            f"👥 Total users found: <b>{count}</b>\n"
            f"📋 Columns: <code>{', '.join(headers)}</code>\n\n"
            f"First 3 rows:\n{preview_text}",
            parse_mode="HTML",
        )

        await telegram_app.bot.send_message(chat_id, "⏳ Step 2/5 — Creating 6-hour Zoom meeting (Silent)...")
        meeting_id, reg_url = await create_zoom_meeting(session_type, is_test=True)
        
        await telegram_app.bot.send_message(chat_id, "⏳ Step 3/5 — Testing Database Import...")
        imported_count, failed_emails = await import_registrants(meeting_id, csv_path)

        await telegram_app.bot.send_message(chat_id, "⏳ Step 4/5 — Testing WhatsApp Link Lock...")
        await lock_meeting_registration(meeting_id)

        report_text = (
            f"✅ <b>Test Import successful!</b>\n"
            f"👥 Imported: {imported_count}\n"
        )
        if failed_emails:
            failed_list_formatted = "\n".join([f"• <code>{email}</code>" for email in failed_emails])
            report_text += f"❌ Failed: {len(failed_emails)}\n📋 Rejected:\n{failed_list_formatted}\n"
        else:
            report_text += f"❌ Failed: 0\n"
            
        report_text += f"\n🔗 {reg_url}"

        await telegram_app.bot.send_message(chat_id, report_text, parse_mode="HTML")

        await telegram_app.bot.send_message(chat_id, "⏳ Step 5/5 — Deleting dummy meeting...")
        await delete_zoom_meeting(meeting_id)

        await telegram_app.bot.send_message(
            chat_id,
            f"✅ <b>TEST COMPLETE — Everything works!</b>\n\n"
            f"✅ Market Spartans login → OK\n"
            f"✅ 6-Hour Meeting Created → OK\n"
            f"✅ Database Cross-Check → OK\n"
            f"✅ Registration Locked → OK\n"
            f"✅ Dummy meeting check → OK\n\n"
            f"<i>Ready for real runs at 8:30 AM & 4:30 PM IST</i>",
            parse_mode="HTML",
        )

    except Exception as e:
        logging.error(f"Test error [{session_type}]: {e}")
        safe_error = html.escape(str(e)) 
        await telegram_app.bot.send_message(
            chat_id,
            f"❌ <b>Test failed!</b>\n\nError: <code>{safe_error}</code>",
            parse_mode="HTML", 
        )
    finally:
        if csv_path and os.path.exists(csv_path):
            os.remove(csv_path)


# ── TELEGRAM HANDLERS ─────────────────────────────────────────────────────────

async def setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /setup command for fast-pass pre-approvals."""
    if update.effective_chat.id != TELEGRAM_CHAT_ID: return

    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    
    time_val = now.hour + now.minute / 60.0

    if time_val < 8.5:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌅 Pre-approve Morning Only", callback_data="pre_morning")],
            [InlineKeyboardButton("🌆 Pre-approve Evening Only", callback_data="pre_evening")],
            [InlineKeyboardButton("✅ Pre-approve BOTH Sessions", callback_data="pre_both")]
        ])
        await update.message.reply_text("⚡ <b>Fast-Pass Setup</b>\nSelect the sessions you want to pre-approve for today:", reply_markup=keyboard, parse_mode="HTML")
    
    elif time_val < 16.5:
        if pre_approved["evening"]:
            await update.message.reply_text("✅ Your Evening session is already pre-approved for today!")
            return
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌆 Pre-approve Evening", callback_data="pre_evening")]
        ])
        await update.message.reply_text("⚡ <b>Fast-Pass Setup</b>\nMorning session has passed. Pre-approve the evening session?", reply_markup=keyboard, parse_mode="HTML")
    
    else:
        await update.message.reply_text("All sessions for today have already passed. I'll reset the board at midnight!")


async def send_confirmation(session_type):
    session = SESSIONS[session_type]
    
    if pre_approved[session_type]:
        await telegram_app.bot.send_message(
            TELEGRAM_CHAT_ID,
            f"⚡ <b>Kicking off your pre-approved {session['label']} session...</b>\nI've got it from here!",
            parse_mode="HTML"
        )
        asyncio.create_task(run_automation(session_type))
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, run it",  callback_data=f"yes_{session_type}"),
        InlineKeyboardButton("⏭️ Skip for today",   callback_data=f"no_{session_type}"),
    ]])

    await telegram_app.bot.send_message(
        TELEGRAM_CHAT_ID,
        f"⚙️ <b>{session['label']} session is queued up!</b>\n\n" 
        f"Should I go ahead and run the automation now?\n\n"
        f"<i>(This will auto-skip in 15 minutes if I don't hear from you.)</i>",
        reply_markup=keyboard,
        parse_mode="HTML", 
    )

    async def auto_skip():
        try:
            await asyncio.sleep(15 * 60)
            logging.info(f"Auto-skipping {session_type} — no response in 15 min")
            await telegram_app.bot.send_message(
                TELEGRAM_CHAT_ID,
                f"⏳ <b>Auto-skipped</b> — I didn't get a response, so I skipped the {session['label']} session for today.",
                parse_mode="HTML"
            )
        except asyncio.CancelledError:
            pass 

    task = asyncio.create_task(auto_skip())
    pending_jobs[session_type] = task


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("pre_"):
        target = query.data.split("_")[1]
        
        if target == "both":
            pre_approved["morning"] = True
            pre_approved["evening"] = True
            await query.edit_message_text("✅ <b>Both Morning & Evening</b> sessions are pre-approved for today. I'll handle them automatically!", parse_mode="HTML")
        elif target == "morning":
            pre_approved["morning"] = True
            await query.edit_message_text("✅ <b>Morning</b> session pre-approved for today!", parse_mode="HTML")
        elif target == "evening":
            pre_approved["evening"] = True
            await query.edit_message_text("✅ <b>Evening</b> session pre-approved for today!", parse_mode="HTML")
        return

    action, session_type = query.data.split("_", 1)

    if session_type in pending_jobs:
        pending_jobs[session_type].cancel()
        del pending_jobs[session_type]

    if action == "yes":
        await query.edit_message_text("🚀 <b>Got it!</b> Starting the automation right now...", parse_mode="HTML")
        asyncio.create_task(run_automation(session_type))
    else:
        await query.edit_message_text("⏭️ <b>Skipped.</b> I'll leave this session alone for today.", parse_mode="HTML")


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
    
    telegram_app.add_handler(CommandHandler("setup", setup_command)) 
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(CommandHandler("test", test_command))

    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    
    scheduler.add_job(reset_pre_approvals, "cron", hour=0, minute=0)
    
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
