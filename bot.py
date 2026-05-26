import os
import json
import logging
from datetime import datetime, time, timezone, timedelta
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
from dotenv import load_dotenv
from config import (
    AUTO_KICK,
    FAKE_SCORE_THRESHOLD,
    CHANNEL_ID,
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
def fake_score(user) -> float:
    """Return a score 0‑1 – higher means more likely fake."""
    score = 0.0

    # 1️⃣ Account age (Telegram IDs embed creation time)
    creation_ts = user.id >> 32
    creation_dt = datetime.fromtimestamp(creation_ts, tz=timezone.utc)
    age_hours = (datetime.now(tz=timezone.utc) - creation_dt).total_seconds() / 3600
    if age_hours < 24:
        score += 0.4

    # 2️⃣ No username
    if not user.username:
        score += 0.2

    # 3️⃣ No profile picture
    if not user.photo:
        score += 0.2

    # 4️⃣ Very short first name
    if not user.first_name or len(user.first_name) < 2:
        score += 0.1

    # 5️⃣ Is a bot
    if user.is_bot:
        score += 0.1

    return min(score, 1.0)


def is_fake(user) -> bool:
    return fake_score(user) >= FAKE_SCORE_THRESHOLD


# ------------------------------------------------------------
# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("✅ Bot is alive!")


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered on any ChatMemberUpdated event."""
    chat = update.chat_member.chat
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
    score = fake_score(user)

    if AUTO_KICK and score >= FAKE_SCORE_THRESHOLD:
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
    """
    Heavy maintenance command – must be run in the bot's private chat.
    The caller must be an admin/creator of CHANNEL_ID.
    """
    # 1️⃣ Private‑chat only
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "⚙️ Please run /scan_fake in a private chat with the bot."
        )
        return

    # 2️⃣ Verify admin of the managed channel
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user.id)
        if member.status not in ("administrator", "creator"):
            await update.effective_message.reply_text(
                "🚫 You must be an admin of the managed channel to run this command."
            )
            return
    except Exception:
        await update.effective_message.reply_text(
            "❌ Could not verify your admin status on the channel."
        )
        return

    # 3️⃣ Perform the scan
    total = len(known_members)
    kicked = 0
    kept = 0

    for uid in list(known_members):
        try:
            cm = await context.bot.get_chat_member(CHANNEL_ID, uid)
            user_obj = cm.user
            if is_fake(user_obj):
                if AUTO_KICK:
                    await context.bot.ban_chat_member(CHANNEL_ID, uid)
                    kicked += 1
                else:
                    await context.bot.send_message(
                        CHANNEL_ID,
                        f"⚠️ Suspected fake: [{user_obj.full_name}](tg://user?id={uid}) "
                        f"(score {fake_score(user_obj):.2f})",
                    )
                    kept += 1
                known_members.discard(uid)
            else:
                kept += 1
        except Exception:
            # User might have left already
            known_members.discard(uid)

    save_known_members(known_members)
    await update.effective_message.reply_text(
        f"🔎 Scan complete. Total scanned: {total}\n"
        f"Kicked: {kicked}\n"
        f"Kept: {kept}"
    )


# ------------------------------------------------------------
# Background job: daily automated scan
async def scheduled_scan(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs automatically each day at SCAN_TIME (America/Toronto timezone).
    Sends a concise summary to ADMIN_CHAT_ID (private DM).
    """
    # If ADMIN_CHAT_ID is still the placeholder, skip the job silently.
    if ADMIN_CHAT_ID == 0:
        return

    # Perform the same scan logic but **without** sending per‑user messages to the channel.
    total = len(known_members)
    kicked = 0
    kept = 0

    for uid in list(known_members):
        try:
            cm = await context.bot.get_chat_member(CHANNEL_ID, uid)
            user_obj = cm.user
            if is_fake(user_obj):
                if AUTO_KICK:
                    await context.bot.ban_chat_member(CHANNEL_ID, uid)
                    kicked += 1
                else:
                    # silent – just count
                    kept += 1
                known_members.discard(uid)
            else:
                kept += 1
        except Exception:
            known_members.discard(uid)

    save_known_members(known_members)

    # Prepare the DM summary
    summary = (
        f"🕛 Daily Fake‑Subscriber Scan Report\n"
        f"Channel: {CHANNEL_ID}\n"
        f"Time: {datetime.now(pytz.timezone('America/Toronto')).strftime('%Y-%m-%d %H:%M')}\n"
        f"Total processed: {total}\n"
        f"Kicked: {kicked}\n"
        f"Kept: {kept}"
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
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    print("🤖 Bot started – listening for events")
    app.run_polling()


if __name__ == "__main__":
    main()
