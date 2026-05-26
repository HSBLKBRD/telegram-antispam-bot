import os
import json
import logging
from datetime import datetime, time, timezone, timedelta
import pytz
logger = logging.getLogger(__name__)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ChatMemberHandler,
    CallbackQueryHandler,
    JobQueue,
)
from dotenv import load_dotenv
from config import (
    AUTO_KICK,
    FAKE_SCORE_THRESHOLD,
    CHANNEL_IDS,
    ADMIN_CHAT_ID,
    SCAN_TIME,
)

# ------------------------------------------------------------
# Load bot token from .env
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("🚨 Telegram bot token not found in .env")

# ------------------------------------------------------------
# Persistence helpers
MEMBERS_FILE = "members.json"


def load_known_members() -> set[int]:
    if os.path.exists(MEMBERS_FILE):
        with open(MEMBERS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_known_members(members: set[int]) -> None:
    with open(MEMBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(members), f)


known_members = load_known_members()


# ------------------------------------------------------------
# Heuristic fake‑score
import unicodedata
import re

# ------------------------------------------------------------
# Heuristic fake‑score (enhanced)

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
    """Return True if *name* contains any non‑Latin alphabetic characters."""
    for ch in name:
        if ch.isalpha():
            try:
                if "LATIN" not in unicodedata.name(ch):
                    return True
            except ValueError:
                # Some characters may not have a name; treat as non‑Latin
                return True
    return False

def _is_gibberish(name: str) -> bool:
    """Simple gibberish detection: short alphanumeric strings with low vowel ratio."""
    if not name:
        return False
    # Consider only letters/digits
    cleaned = re.sub(r"[^A-Za-z0-9]", "", name)
    if len(cleaned) < 5 or len(cleaned) > 12:
        return False
    # Vowel proportion
    vowels = sum(1 for c in cleaned.lower() if c in "aeiou")
    if len(cleaned) == 0:
        return False
    return (vowels / len(cleaned)) < 0.3

# Placeholder removed – is_fake implementation provided above.

def is_fake(user) -> float:
    """Return a fake‑score (0‑1). Higher indicates more likely fake.
    Combines base heuristics with character‑script analysis and gibberish detection.
    """
    score = _base_fake_score(user)
    # Additional checks
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    full_name = f"{first} {last}".strip()
    if _has_non_latin(full_name):
        score += 0.2
    if _is_gibberish(full_name):
        score += 0.2
    return min(score, 1.0)






# ------------------------------------------------------------
# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("✅ Bot is alive!")


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered on any ChatMemberUpdated event."""
    # Verify that this update belongs to a managed channel
    chat = update.chat_member.chat
    # Determine channel identifier (username with @ or numeric ID)
    channel_id = None
    if getattr(chat, "username", None):
        channel_id = f"@{chat.username}"
    else:
        channel_id = str(chat.id)
    if channel_id not in CHANNEL_IDS:
        return  # ignore chats that are not managed

    new_status = update.chat_member.new_chat_member.status

    if new_status != "member":
        return

    user = update.chat_member.new_chat_member.user
    uid = user.id

    # Persist the member ID
    if uid not in known_members:
        known_members.add(uid)
        save_known_members(known_members)

    # Evaluate
    score = is_fake(user)
    if AUTO_KICK and score >= FAKE_SCORE_THRESHOLD:
        # Automatic removal (should not happen when AUTO_KICK is False)
        await context.bot.ban_chat_member(chat.id, uid)
        await context.bot.send_message(
            chat.id,
            f"🚫 Auto‑removed suspected fake [{user.full_name}](tg://user?id={uid}) (score {score:.2f}).",
        )
        return
    # Manual review – inline buttons
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


async def scan_fake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Heavy maintenance command – run in the bot's private chat.
    Only admins of managed channels may execute this.
    """
    # 1️⃣ Ensure private chat
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "⚙️ Please run /scan_fake in a private chat with the bot."
        )
        return

    # 2️⃣ Verify admin rights in any managed channel
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

    # 3️⃣ Scan each known user via get_chat (bypasses channel restrictions)
    from telegram.error import BadRequest
    total_processed = 0
    report_users: list[tuple[int, str, float]] = []
    for uid in list(known_members):
        user_obj = await context.bot.get_chat(uid)
        score = is_fake(user_obj)
        if score >= FAKE_SCORE_THRESHOLD:
            report_users.append((uid, user_obj.full_name, score))
            known_members.discard(uid)
            total_processed += 1

    # 4️⃣ Send aggregated report to admin
    if not report_users:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="No suspected fake users found.")
    else:
        lines = ["🔎 Suspected fake users:"]
        for i, (uid, name, score) in enumerate(report_users, start=1):
            lines.append(f"{i}. [{name}](tg://user?id={uid}) (Score: {score:.2f})")
        report = "\n".join(lines)
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=report)

    # Persist updated member set and acknowledge
    save_known_members(known_members)
    await update.effective_message.reply_text(
        f"🔎 Scan complete. Total processed: {total_processed}"
    )



# ------------------------------------------------------------
# Background job: daily automated scan
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
            except Exception as e:
                from telegram.error import BadRequest
                if isinstance(e, BadRequest):
                    logger.warning(f"BadRequest for get_chat_member in scheduled scan ({ch_id}) uid {uid}: {e.message}")
                else:
                    logger.error(f"Error during scheduled scan for uid {uid} in {ch_id}: {e}")
                continue
        if fake_users:
            report_by_channel[ch_id] = fake_users
            total_processed += len(fake_users)

    if not report_by_channel:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="No suspected fake users found.")
    else:
        lines = []
        for ch, users in report_by_channel.items():
            lines.append(f"📢 Channel: {ch}")
            for i, (uid, name, score) in enumerate(users, start=1):
                lines.append(f"{i}. [{name}](tg://user?id={uid}) (Score: {score:.2f})")
            lines.append("")
        report = "\n".join(lines).strip()
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=report)

    save_known_members(known_members)

    summary = (
        f"🕛 Daily Fake‑Subscriber Scan Report\n"
        f"Time: {datetime.now(pytz.timezone('America/Toronto')).strftime('%Y-%m-%d %H:%M')}\n"
        f"Total processed: {total_processed}\n"
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=summary)


def main() -> None:
    app = ApplicationBuilder().token(TOKEN).build()

    # Core handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(CommandHandler("scan_fake", scan_fake))

    # --------------------------------------------------------
    # Schedule the daily scan
    job_queue: JobQueue = app.job_queue

    # Parse SCAN_TIME ("HH:MM") in America/Toronto timezone
    hour, minute = map(int, SCAN_TIME.split(":"))
    tz = pytz.timezone("America/Toronto")

    # Convert the next occurrence of SCAN_TIME to UTC for PTB
    now = datetime.now(tz)
    target_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_today <= now:
        # Time already passed today → schedule for tomorrow
        target_utc = (target_today + timedelta(days=1)).astimezone(timezone.utc)
    else:
        target_utc = target_today.astimezone(timezone.utc)

    # Schedule a daily job at the calculated UTC time
    job_queue.run_daily(
        scheduled_scan,
        time=time(hour=target_utc.hour, minute=target_utc.minute, tzinfo=timezone.utc),
    )

    # --------------------------------------------------------
    # Logging
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logger = logging.getLogger(__name__)
    logger.info("🤖 Bot started – listening for events")
    app.run_polling()


if __name__ == "__main__":
    main()
