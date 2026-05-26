import os
import json
import logging
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ChatMemberHandler,
    CallbackQueryHandler,
)
from dotenv import load_dotenv
from config import AUTO_KICK, FAKE_SCORE_THRESHOLD, CHANNEL_ID

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("🚨 Token not found. Check .env file.")

# ----------------------------------------------------------------------
# Helper: load / save known members
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


# ----------------------------------------------------------------------
# Heuristic scoring for a Telegram User object
def fake_score(user) -> float:
    """Return a score between 0 and 1 – higher means more likely fake."""
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

    # 5️⃣ Is a bot (should be False)
    if user.is_bot:
        score += 0.1

    return min(score, 1.0)


def is_fake(user) -> bool:
    return fake_score(user) >= FAKE_SCORE_THRESHOLD


# ----------------------------------------------------------------------
# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("✅ Bot is alive!")


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called on any ChatMemberUpdated event."""
    chat = update.chat_member.chat
    new_status = update.chat_member.new_chat_member.status

    # Only care about a user becoming a member of the channel
    if new_status != "member":
        return

    user = update.chat_member.new_chat_member.user
    user_id = user.id

    # Persist the member ID
    if user_id not in known_members:
        known_members.add(user_id)
        save_known_members(known_members)

    # Evaluate fake‑score
    score = fake_score(user)

    if AUTO_KICK and score >= FAKE_SCORE_THRESHOLD:
        # Automatic removal
        await context.bot.ban_chat_member(chat.id, user_id)
        await context.bot.send_message(
            chat.id,
            f"🚫 Auto‑removed suspected fake account [{user.full_name}](tg://user?id={user_id}) (score {score:.2f}).",
        )
        return

    # Manual review – inline buttons
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Keep", callback_data=f"keep|{chat.id}|{user_id}"),
                InlineKeyboardButton("❌ Kick", callback_data=f"kick|{chat.id}|{user_id}"),
            ]
        ]
    )
    await context.bot.send_message(
        chat.id,
        f"⚠️ New member [{user.full_name}](tg://user?id={user_id}) joined.\n"
        f"Score: {score:.2f} (threshold {FAKE_SCORE_THRESHOLD})",
        reply_markup=keyboard,
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, chat_id, user_id = query.data.split("|")
    chat_id = int(chat_id)
    user_id = int(user_id)

    if action == "kick":
        await context.bot.ban_chat_member(chat_id, user_id)
        await query.edit_message_text("✅ User was kicked by admin action.")
    else:
        await query.edit_message_text("👍 User kept in the channel.")


async def scan_fake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Heavy maintenance command – must be run in the bot's private chat.
    The caller must be an admin/creator of CHANNEL_ID.
    """
    # 1️⃣ Ensure the command comes from a private DM with the bot
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "⚙️ Please run /scan_fake in a private chat with the bot."
        )
        return

    # 2️⃣ Verify that the caller is an admin of the managed channel
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

    # 3️⃣ Perform the scan over stored members
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
                        f"⚠️ Suspected fake: [{user_obj.full_name}](tg://user?id={uid}) (score {fake_score(user_obj):.2f})",
                    )
                    kept += 1
                known_members.discard(uid)  # remove after handling
            else:
                kept += 1
        except Exception:
            # User may have left the channel already
            known_members.discard(uid)

    save_known_members(known_members)
    await update.effective_message.reply_text(
        f"🔎 Scan complete. Total scanned: {total}\\n"
        f"Kicked: {kicked}\\n"
        f"Kept: {kept}"
    )


def main() -> None:
    app = ApplicationBuilder().token(TOKEN).build()

    # Basic /start
    app.add_handler(CommandHandler("start", start))

    # New‑member detection
    app.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(button_callback))

    # Scan command (private‑chat only)
    app.add_handler(CommandHandler("scan_fake", scan_fake))

    # Logging
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    print("🤖 Bot started – listening for events")
    app.run_polling()


if __name__ == "__main__":
    main()
