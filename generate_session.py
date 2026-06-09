"""
One-time script to generate a Telethon session string.
Run this ONCE locally, copy the printed SESSION_STRING,
and paste it into Railway as the TELETHON_SESSION env variable.

Usage:
    python generate_session.py
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    print("=" * 60)
    print("  Telethon Session Generator — Market Spartans Bot")
    print("=" * 60)
    print()
    print("You need your API credentials from https://my.telegram.org")
    print()

    api_id   = int(input("Enter your api_id (number): ").strip())
    api_hash = input("Enter your api_hash (string): ").strip()

    print()
    print("Starting auth — you'll receive an OTP on Telegram...")
    print()

    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        await client.start()
        session_string = client.session.save()

    print()
    print("=" * 60)
    print("  ✅ SUCCESS — Copy the string below into Railway")
    print("     Variable name: TELETHON_SESSION")
    print("=" * 60)
    print()
    print(session_string)
    print()
    print("Also add these to Railway:")
    print(f"  TELETHON_API_ID   = {api_id}")
    print(f"  TELETHON_API_HASH = {api_hash}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
