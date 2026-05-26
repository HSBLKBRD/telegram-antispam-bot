# extractor.py – one‑time Member Extraction using Telethon
"""
Utility script to fetch **all** current members from the Telegram channels listed in
`config.py` and merge them into the bot's `members.json` database.
"""

import asyncio
import json
import os
import tempfile
import shutil
from typing import Set

from telethon import TelegramClient
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsSearch, User

# Import the list of channel identifiers from the bot configuration
try:
    from config import CHANNEL_IDS
except ImportError:
    raise SystemExit("❌ Could not import CHANNEL_IDS from config.py")

# Path used by the main bot to store member IDs
MEMBERS_FILE = "members.json"


def atomic_save_members(ids: set[int], path: str = MEMBERS_FILE) -> None:
    """
    Write the sorted list of IDs to *path* atomically.
    A temporary file is created in the same directory and then renamed.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_file:
        json.dump(sorted(ids), tmp_file, ensure_ascii=False, indent=2)
    shutil.move(tmp_path, path)


async def fetch_members(client: TelegramClient, channel_identifier: str) -> Set[int]:
    """
    Return a set containing **all** real‑user IDs present in *channel_identifier*.

    Public channels often limit a plain ``GetParticipantsRequest`` with an empty
    search query to 200 results.  To work around this, we perform **search‑based
    pagination**: we query the channel repeatedly with short strings (letters,
    digits, and a few common Persian characters).  For each query we page
    through the results (200 users per request) until no more participants are
    returned.  All unique user IDs are collected in a set; bot accounts are
    filtered out automatically.

    A short ``await asyncio.sleep(1)`` pause is kept between each request to stay
    safely within Telegram’s rate‑limit thresholds.
    """
    try:
        entity = await client.get_entity(channel_identifier)
    except Exception as e:
        print(f"⚠️ Unable to resolve channel {channel_identifier}: {e}")
        return set()

    participants: Set[int] = set()

    # Characters to search for – latin letters, digits and a few Persian letters
    search_chars = (
        list("abcdefghijklmnopqrstuvwxyz") +
        list("0123456789") +
        ["آ", "ا", "ب", "پ", "ت", "ث", "ج", "چ", "ح", "خ",
         "د", "ذ", "ر", "ز", "ژ", "س", "ش", "ص", "ض",
         "ط", "ظ", "ع", "غ", "ف", "ق", "ک", "گ",
         "ل", "م", "ن", "و", "ه", "ی"]
    )

    limit = 200  # maximum allowed per GetParticipantsRequest

    for char in search_chars:
        offset = 0
        while True:
            result = await client(
                GetParticipantsRequest(
                    channel=entity,
                    filter=ChannelParticipantsSearch(char),
                    offset=offset,
                    limit=limit,
                    hash=0,
                )
            )

            # ``result.users`` is a list of ``User`` objects
            if not result.users:
                break

            for user in result.users:               # type: User
                if getattr(user, "bot", False):
                    continue
                participants.add(user.id)

            # Prepare next slice for this character
            offset += len(result.users)

            # Respect rate limits
            await asyncio.sleep(1)

    return participants


async def main():
    # ------------------------------------------------------------
    # 1️⃣ Gather Telethon credentials from the user.
    # ------------------------------------------------------------
    api_id_input = input("Enter your Telegram API_ID (integer): ").strip()
    api_hash = input("Enter your Telegram API_HASH (string): ").strip()
    phone = input(
        "Enter the phone number associated with the account (e.g. +1234567890): "
    ).strip()

    try:
        api_id = int(api_id_input)
    except ValueError:
        raise SystemExit("❌ API_ID must be an integer")

    # ------------------------------------------------------------
    # 2️⃣ Initialise the MTProto client.
    # ------------------------------------------------------------
    client = TelegramClient("extractor_session", api_id, api_hash)
    print("🔐 Connecting to Telegram…")
    await client.start(phone=phone)          # may trigger a login code request
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
            print(
                "⚠️ Could not read existing members.json – starting with an empty set."
            )

    # ------------------------------------------------------------
    # 4️⃣ Iterate over every channel in config.py and collect members.
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
    atomic_save_members(combined)
    print(
        f"✅ Finished. members.json now contains {len(combined)} unique user IDs."
    )

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
