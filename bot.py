#!/usr/bin/env python3
"""
Telegram anti‑spam bot – scans members for fake‑account characteristics.

Key improvements:
* `scan_fake` now treats “profile‑access‑restricted” users (BadRequest) as
  suspected fakes and includes them in the final report.
* Restricted users are still counted in the summary.
* All replies use `update.effective_message.reply_text` (private‑chat safe).
* The file is a complete, syntactically‑correct script ready to run.
"""

# ------------------------------------------------------------
# Standard library
# ------------------------------------------------------------
import os
import json
import logging
from datetime import datetime, time, timezone, timedelta
import unicodedata
import re

# ------------------------------------------------------------
# Third‑party packages
# ------------------------------------------------------------
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ChatMemberHandler,
    CallbackQueryHandler,
    JobQueue,
)
from telegram.error import BadRequest
from dotenv import load_dotenv

# ------------------------------------------------------------
# Project configuration (config.py)
# ------------------------------------------------------------
from config import (
    AUTO_KICK,
    FAKE_SCORE_THRESHOLD,
    CHANNEL_IDS,
    ADMIN_CHAT_ID,
    SCAN_TIME,
)

# ------------------------------------------------------------
# Bot token & logger
# ------------------------------------------------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("🚨 Telegram bot token not found in .env")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Persistence helpers
# ------------------------------------------------------------
MEMBERS_FILE = "members.json"


def load_known_members() -> set[int]:
    """Load the set of member IDs we have already seen."""
    if os.path.exists(MEMBERS_FILE):
        with open(MEMBERS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_known_members(members: set[int]) -> None:
    """Persist the current set of known member IDs."""
    with open(MEMBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(members), f)


known_members = load_known_members()

# ------------------------------------------------------------
# Heuristic fake‑score utilities
# ------------------------------------------------------------
def _base_fake_score(user) -> float:
    """Base score from existing heuristics (age, username, photo, name length, bot)."""
    score = 0.0
    # 1️⃣ Account age (Telegram IDs embed creation time)
    creation_ts = user.id >> 32
    creation_dt = datetime.fromtimestamp(creation_ts, tz=timezone.utc)
    age_hours = (datetime.now(tz=timezone.utc) - creation_dt).total_seconds() / 3600
    if age_hours < 24:
        score += 0.4
    # 2️⃣ No username
    if not getattr(user, "username", None):
        score += 0.2
    # 3️⃣ No profile picture
    if not getattr(user, "photo", None):
        score += 0.2
    # 4️⃣ Very short first name
    if not getattr(user, "first_name", None) or len(user.first_name) < 2:
        score += 0.1
    # 5️⃣ Is a bot
    if getattr(user, "is_bot", False):
        score += 0.1
    return min(score, 1.0)


def _has_non_latin(name: str) -> bool:
    """Return True if the name contains any non‑Latin alphabetic characters."""
    for ch in name:
        if ch.isalpha():
            try:
                if "LATIN" not in unicodedata.name(ch):
                    return True
            except ValueError:
                return True
    return False


def _is_gibberish(name: str) -> bool:
    """Very simple gibberish detection – short alphanumeric strings with few vowels."""
    if not name:
        return False
    cleaned = re.sub(r"[^A-Za-z0-9]", "", name)
    if len(cleaned) < 5 or len(cleaned) > 12:
        return False
    vowels = sum(1 for c in cleaned.lower() if c in "aeiou")
    return (vowels / len(cleaned)) < 0.3


def is_fake(user) -> float:
    """
    Return a fake‑score (0‑1). Higher indicates a higher likelihood of being fake.
    New checks added:
      1️⃣ Symbol/Emoji‑only names – if the full name contains **no alphanumeric**
          characters (only emojis, dots, punctuation, etc.), add a strong penalty.
      2️⃣ Extremely short names – if the stripped name is shorter than 2 characters,
          add a moderate penalty.
    The existing non‑Latin and gibberish checks are kept.
    """
    score = _base_fake_score(user)

    # ---- name preparation -------------------------------------------------
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    full_name = f"{first} {last}".strip()

    # ---- 1️⃣ Symbol/Emoji‑only name ----------------------------------------
    name_no_spaces = full_name.replace(" ", "")
    if name_no_spaces and not any(ch.isalnum() for ch in name_no_spaces):
        # Very strong indication of a fake profile
        score += 0.3

    # ---- 2️⃣ Extremely short name -----------------------------------------
    if len(full_name) < 2:
        score += 0.2   # short names are often dummy accounts

    # ---- existing checks ----------------------------------------------------
    if _has_non_latin(full_name):
        score += 0.2
    if _is_gibberish(full_name):
        score += 0.2

    # ---- final score (capped at 1.0) --------------------------------------
    return min(score, 1.0)


# ------------------------------------------------------------
# Bot command / event handlers
# ------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("✅ Bot is alive!")


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered on any ChatMemberUpdated event."""
    chat = update.chat_member.chat
    channel_id = f"@{chat.username}" if getattr(chat, "username", None) else str(chat.id)
    if channel_id not in CHANNEL_IDS:
        return

    new_status = update.chat_member.new_chat_member.status
    if new_status != "member":
        return

    user = update.chat_member.new_chat_member.user
    uid = user.id
    if uid not in known_members:
        known_members.add(uid)
        save_known_members(known_members)

    score = is_fake(user)
    if AUTO_KICK and score >= FAKE_SCORE_THRESHOLD:
        await context.bot.ban_chat_member(chat.id, uid)
        await context.bot.send_message(
            chat.id,
            f"🚫 Auto‑removed suspected fake [{user.full_name}](tg://user?id={uid}) (score {score:.2f}).",
        )
        return

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Keep", callback_data=f"keep|{chat.id}|{uid}"),
                InlineKeyboardButton("❌ Kick", callback_data=f"kick|{chat.id}|{uid}"),
            ]
        ]
    )
    await context.bot.send_message(
        chat.id,
        f"⚠️ New member [{user.full_name}](tg://user?id={uid}) joined.\n"
        f"Score: {score:.2f} (threshold {FAKE_SCORE_THRESHOLD})",
        reply_markup=kb,
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, chat_id_str, uid_str = query.data.split("|")
    chat_id = int(chat_id_str)
    uid = int(uid_str)

    if action == "kick":
        await context.bot.ban_chat_member(chat_id, uid)
        await query.edit_message_text("✅ User was kicked by admin action.")
    else:
        await query.edit_message_text("👍 User kept in the channel.")


# ------------------------------------------------------------
# /scan_fake – heavy maintenance command
# ------------------------------------------------------------
async def scan_fake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Heavy maintenance command – run in the bot's private chat.
    Only admins of managed channels may execute this."""
    # --------------------------------------------------------------------
    # Must be run in a private chat with the bot
    # --------------------------------------------------------------------
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "⚙️ Please run /scan_fake in a private chat with the bot."
        )
        return

    # --------------------------------------------------------------------
    # Verify the caller is an admin of at least one managed channel
    # --------------------------------------------------------------------
    user = update.effective_user
    is_admin = False
    for ch in CHANNEL_IDS:
        try:
            member = await context.bot.get_chat_member(ch, user.id)
            if member.status in ("administrator", "creator"):
                is_admin = True
                break
        except Exception as e:
            logger.info(f"⚠️ Admin check failed for {ch}: {e}")

    if not is_admin:
        await update.effective_message.reply_text(
            "🚫 You must be an admin of one of the managed channels to run this command."
        )
        return

    # --------------------------------------------------------------------
    # Scan known members
    # --------------------------------------------------------------------
    # Reload known members to ensure we scan the freshest list
    global known_members
    known_members = load_known_members()
    total_processed = 0          # users successfully inspected
    restricted_count = 0          # users whose profile cannot be accessed
    report_users: list[tuple[int, str, float]] = []  # (uid, name, score)
    total_to_scan = len(known_members)

    for idx, uid in enumerate(list(known_members), start=1):
        print(f"Scanning user {idx} of {total_to_scan} (uid: {uid})")
        try:
            # `get_chat` raises BadRequest when the bot cannot read the profile
            user_obj = await context.bot.get_chat(uid)
        except BadRequest as e:                     # profile restricted
            logger.info(
                f"⚠️ Profile access restricted for uid {uid}: {e.message}"
            )
            restricted_count += 1
            # Treat as suspected fake – add to the report with a clear reason
            report_users.append(
                (
                    uid,
                    "Restricted/Hidden Profile (Potential Bot)",
                    FAKE_SCORE_THRESHOLD,
                )
            )
            continue
        except Exception as e:                      # any other unexpected issue
            logger.warning(f"⚠️ Failed to fetch user {uid}: {e}")
            continue

        score = is_fake(user_obj)
        if score >= FAKE_SCORE_THRESHOLD:
            report_users.append((uid, user_obj.full_name, score))
            total_processed += 1

    # --------------------------------------------------------------------
    # Send the report back to the initiator (private chat)
    # --------------------------------------------------------------------
    try:
        if not report_users:
            await update.effective_message.reply_text("No suspected fake users found.")
        else:
            lines = ["🔎 Suspected fake users:"]
            for i, (uid, name, score) in enumerate(report_users, start=1):
                lines.append(f"{i}. [{name}](tg://user?id={uid}) (Score: {score:.2f})")
            await update.effective_message.reply_text(
                "\n".join(lines), parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"❌ Failed to send report: {e}")

    # --------------------------------------------------------------------
    # Persist state and give a concise summary
    # --------------------------------------------------------------------
    # Persist known members for future runs (unchanged set)
    save_known_members(known_members)
    try:
        total_known = len(known_members) + total_processed + restricted_count
        await update.effective_message.reply_text(
            f"🔎 Scan complete. Scanned {total_known} users, "
            f"{restricted_count} restricted, {total_processed} processed."
        )
    except Exception as e:
        logger.error(f"❌ Failed to send reply to user: {e}")


# ------------------------------------------------------------
# Background job: daily automated scan
# ------------------------------------------------------------
async def scheduled_scan(context: ContextTypes.DEFAULT_TYPE):
    """Runs daily at SCAN_TIME, scanning members per channel and reporting to ADMIN_CHAT_ID."""
    if ADMIN_CHAT_ID == 0:
        return

    report_by_channel: dict[str, list[tuple[int, str, float]]] = {}
    total_processed = 0

    for ch_id in CHANNEL_IDS:
        fake_users: list[tuple[int, str, float]] = []
        for uid in list(known_members):
            try:
                cm = await context.bot.get_chat_member(ch_id, uid)
                user_obj = cm.user
                score = is_fake(user_obj)
                if score >= FAKE_SCORE_THRESHOLD:
                    fake_users.append((uid, user_obj.full_name, score))
                    known_members.discard(uid)
            except BadRequest:
                # profile not accessible – just skip silently for the scheduled job
                continue
            except Exception as e:
                logger.error(
                    f"Error during scheduled scan for uid {uid} in {ch_id}: {e}"
                )
                continue

        if fake_users:
            report_by_channel[ch_id] = fake_users
            total_processed += len(fake_users)

    # Send summary to the admin chat
    if not report_by_channel:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID, text="No suspected fake users found."
        )
    else:
        lines = []
        for ch, users in report_by_channel.items():
            lines.append(f"📢 Channel: {ch}")
            for i, (uid, name, score) in enumerate(users, start=1):
                lines.append(f"{i}. [{name}](tg://user?id={uid}) (Score: {score:.2f})")
            lines.append("")
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID, text="\n".join(lines).strip()
        )

    save_known_members(known_members)

    summary = (
        f"🕛 Daily Fake‑Subscriber Scan Report\n"
        f"Time: {datetime.now(pytz.timezone('America/Toronto')).strftime('%Y-%m-%d %H:%M')}\n"
        f"Total processed: {total_processed}\n"
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=summary)


# ------------------------------------------------------------
# Application entry point
# ------------------------------------------------------------
def main() -> None:
    app = ApplicationBuilder().token(TOKEN).build()

    # Core command / event handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(CommandHandler("scan_fake", scan_fake))

    # Schedule the daily scan
    job_queue: JobQueue = app.job_queue
    hour, minute = map(int, SCAN_TIME.split(":"))
    tz = pytz.timezone("America/Toronto")
    now = datetime.now(tz)
    target_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if target_today <= now:
        target_utc = (target_today + timedelta(days=1)).astimezone(timezone.utc)
    else:
        target_utc = target_today.astimezone(timezone.utc)

    job_queue.run_daily(
        scheduled_scan,
        time=time(hour=target_utc.hour, minute=target_utc.minute, tzinfo=timezone.utc),
    )

    logger.info("🤖 Bot started – listening for events")
    app.run_polling()


if __name__ == "__main__":
    main()
