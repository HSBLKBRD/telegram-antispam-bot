# extractor.py – one‑time Member Extraction using Telethon
"""
Utility script to fetch all current members from the Telegram channels listed in `config.py`
and merge them into the bot's `members.json` database.

Run:
    python3 extractor.py
You will be prompted for your Telethon credentials (API_ID, API_HASH, phone number).
"""

import asyncio
import json
import os
from typing import Set

from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

# Import channel list from the bot configuration
try:
    from config import CHANNEL_IDS
except ImportError:
    raise SystemExit("❌ Could not import CHANNEL_IDS from config.py")

# Path used by the main bot to store member IDs
MEMBERS_FILE = "members.json"

async def fetch_members(client: TelegramClient, channel_identifier: str) -> Set[int]:
    """Return a set of user IDs present in *channel_identifier*.
    The identifier can be a username ("@mychannel") or a numeric chat ID.
    """
    try:
        entity = await client.get_entity(channel_identifier)
    except Exception as e:
        print(f"⚠️ Unable to resolve channel {channel_identifier}: {e}")
        return set()

    # Telethon's iter_participants works for both super‑groups and channels.
    ids: Set[int] = set()
    async for participant in client.iter_participants(entity):
        ids.add(participant.id)
    return ids

async def main():
    # ------------------------------------------------------------
    # 1️⃣ Gather Telethon credentials from the user.
    # ------------------------------------------------------------
    api_id_input = input("Enter your Telegram API_ID (integer): ").strip()
    api_hash = input("Enter your Telegram API_HASH (string): ").strip()
    phone = input("Enter the phone number associated with the account (e.g. +1234567890): ").strip()

    try:
        api_id = int(api_id_input)
    except ValueError:
        raise SystemExit("❌ API_ID must be an integer")

    # ------------------------------------------------------------
    # 2️⃣ Initialise the MTProto client.
    # ------------------------------------------------------------
    client = TelegramClient("extractor_session", api_id, api_hash)
    print("🔐 Connecting to Telegram…")
    await client.start(phone=phone)  # will trigger code request if needed
    print("✅ Authenticated.")

    # ------------------------------------------------------------
    # 3️⃣ Load existing members (if any) to avoid duplicates.
    # ------------------------------------------------------------
    existing_ids: Set[int] = set()
    if os.path.exists(MEMBERS_FILE):
        try:
            with open(MEMBERS_FILE, "r", encoding="utf-8") as f:
                existing_ids = set(json.load(f))
        except Exception:
            print("⚠️ Could not read existing members.json – starting with an empty set.")

    # ------------------------------------------------------------
    # 4️⃣ Iterate over every channel defined in config.py and collect members.
    # ------------------------------------------------------------
    new_ids: Set[int] = set()
    for ch in CHANNEL_IDS:
        print(f"📡 Fetching participants from {ch} …")
        ids = await fetch_members(client, ch)
        print(f"   → Retrieved {len(ids)} users.")
        new_ids.update(ids)

    # ------------------------------------------------------------
    # 5️⃣ Merge and write back to members.json.
    # ------------------------------------------------------------
    combined = existing_ids.union(new_ids)
    with open(MEMBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(combined), f, ensure_ascii=False, indent=2)
    print(f"✅ Finished. members.json now contains {len(combined)} unique user IDs.")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
