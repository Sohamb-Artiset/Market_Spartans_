# Tomorrow's Setup Checklist

> Complete both tasks below when you have your phone. Order doesn't matter.

---

## 📌 Saved Info
- **Mangesh Kale's phone:** `+919922995956`

---

## Task 1 — Telethon (Send Mangesh his host link via YOUR personal Telegram)

### Step 1 — Create Telegram API credentials (2 min, one-time)
1. Go to https://my.telegram.org
2. Log in with **your** phone number
3. Click **API Development Tools**
4. Fill in any app name (e.g. `MarketSpartans`) and short name (e.g. `mkt`)
5. Submit → you get an **`api_id`** (number) and **`api_hash`** (long string)
6. Save both

### Step 2 — Generate session string
```bash
cd /Users/sohambhutkar/projects/Automation/Market_Spartans_
python generate_session.py
```
- Enter `api_id` and `api_hash` when prompted
- Enter **your** phone number
- Enter the OTP you receive on Telegram
- Copy the printed `SESSION_STRING`

### Step 3 — Add to Railway
| Variable | Value |
|---|---|
| `TELETHON_API_ID` | Your api_id number |
| `TELETHON_API_HASH` | Your api_hash string |
| `TELETHON_SESSION` | The SESSION_STRING from Step 2 |

---

## Task 2 — Green API (Send WhatsApp registration link to WhatsApp group)

### Step 1 — Create a free Green API account
1. Go to https://console.green-api.com
2. Sign up for free (no card needed)
3. Create a new **instance**
4. Scan the QR code with the WhatsApp account that is in the group
5. Instance status should turn **green/authorized**
6. Copy your **Instance ID** and **API Token** from the dashboard

### Step 2 — Find your WhatsApp group ID
```bash
cd /Users/sohambhutkar/projects/Automation/Market_Spartans_
python get_whatsapp_group_id.py
```
- Enter your Instance ID and API Token when prompted
- It will list all your WhatsApp groups with their IDs
- Copy the ID of your target group (it ends in `@g.us`)

### Step 3 — Add to Railway
| Variable | Value |
|---|---|
| `GREENAPI_INSTANCE_ID` | Your Instance ID from Green API dashboard |
| `GREENAPI_TOKEN` | Your API Token from Green API dashboard |
| `WHATSAPP_GROUP_ID` | The group ID ending in `@g.us` from Step 2 |

---

## Final Step — Push & Deploy (do this after BOTH tasks)
```bash
cd /Users/sohambhutkar/projects/Automation/Market_Spartans_
git add .
git commit -m "Add Green API WhatsApp group messaging"
git push origin main
```
Railway will auto-redeploy. Everything is live! 🚀

---

## What will happen after setup

**When a morning/evening session completes:**

1. ✅ You get the full report on Telegram (as always)
2. 📱 **Mangesh** gets a personal Telegram message from **your account** with just his host link
3. 💬 Your **WhatsApp group** automatically gets a message with the registration link

---

## Free Tier Limits (nothing to worry about)
| Service | Free Limit | Your Usage |
|---|---|---|
| Telethon | Unlimited | ~60 msgs/month |
| Green API | 500 msgs/month | ~60 msgs/month |
