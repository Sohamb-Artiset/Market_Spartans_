"""
One-time script to find your WhatsApp Group ID after authenticating Green API.
Run this AFTER you've scanned the QR code in Green API dashboard.

Usage:
    python get_whatsapp_group_id.py
"""

import httpx

def main():
    print("=" * 60)
    print("  WhatsApp Group ID Finder — Green API")
    print("=" * 60)
    print()
    print("Paste your Green API credentials from https://console.green-api.com")
    print()

    instance_id = input("Enter your GREENAPI_INSTANCE_ID: ").strip()
    token       = input("Enter your GREENAPI_TOKEN: ").strip()

    print()
    print("Fetching your WhatsApp chats...")
    print()

    url = f"https://api.green-api.com/waInstance{instance_id}/getChats/{token}"

    r = httpx.get(url, timeout=15)
    r.raise_for_status()
    chats = r.json()

    # Filter to only groups
    groups = [c for c in chats if c.get("id", "").endswith("@g.us")]

    if not groups:
        print("❌ No WhatsApp groups found. Make sure you're in at least one group.")
        return

    print(f"Found {len(groups)} group(s):\n")
    for i, g in enumerate(groups, 1):
        name = g.get("name") or g.get("id", "Unknown")
        gid  = g.get("id", "")
        print(f"  {i}. {name}")
        print(f"     ID: {gid}")
        print()

    print("=" * 60)
    print("Copy the ID of your target group and add it to Railway:")
    print("  Variable name: WHATSAPP_GROUP_ID")
    print("  Value: <the ID ending in @g.us>")
    print("=" * 60)


if __name__ == "__main__":
    main()
