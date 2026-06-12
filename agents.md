# Agent Guidelines & Recurrent Issues

This document outlines key considerations, past bugs, and workflow rules for any AI agent or developer modifying this repository.

---

## 📌 Critical Workflow Rules
1. **Deployment & Pushing**:
   - Always push to the **`upstream`** remote, **NOT** `origin`.
   - command: `git push upstream main`
   - The Railway deployment auto-deploys from `upstream/main`.

---

## ❌ Recurrent Error: `Locator.click: Timeout 30000ms exceeded`

### Error Description
```
❌ Automation failed!
Error: Locator.click: Timeout 30000ms exceeded.
Call log:
waiting for locator("button[name=\"btnsavezoom\"]:has-text(\"Export for Zoom\")")
```

### Root Cause
This error occurs during the Playwright CSV export process in `export_csv(session_type)`. It happens for three main reasons:
1. **Silent Login Failure**: If credentials change or expire, the site redirects the session back to `/index.php` (login page) instead of remaining on `/export-users.php`. Because `/index.php` does not have the "Export for Zoom" button, Playwright waits and eventually times out.
2. **Cloudflare / WAF Blocks**: When hosted on cloud infrastructure like Railway, the target site's firewall (Cloudflare) occasionally flags the cloud IP address, presenting a captcha or access block page instead of the login page.
3. **Flaky Navigation**: Using `page.expect_navigation` can lead to race conditions if the redirection finishes before or during the block execution.

---

## 🛡️ Resolution & Best Practices
To prevent this error from recurring and to make debugging easier, `export_csv` has been modified with a robust implementation:

1. **Retry Loop**: The script attempts the export process up to **3 times** (with a 10-second delay between attempts) before giving up and failing.
2. **Redirection & Error Checks**:
   - It checks the page title for "Cloudflare", "Attention Required!", or "Just a moment..." and raises a clear block message.
   - If redirected back to `index.php` after logging in, it searches the page for danger/error alerts (`.alert-danger`) and includes the exact error text in the exception.
3. **Failure Screenshots via Telegram**:
   - On the final failure, Playwright takes a full-page screenshot of the browser and saves it to a temp directory.
   - The error handling blocks in `run_automation` and `run_test` detect if this screenshot exists and send it directly to the Telegram bot chat.
   - **Do not remove this screenshot capability**, as it is the only way to diagnose cloud-hosted Cloudflare/IP bans.
