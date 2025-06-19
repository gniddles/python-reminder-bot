from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import asyncio
import re

# Store message IDs for deletion (optional enhancement)
message_log = []

async def start_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Timer started for 5 seconds...")
    message_log.append((update.effective_chat.id, msg.message_id))
    await asyncio.sleep(5)
    msg = await update.message.reply_text("⏰ Time's up!")
    message_log.append((update.effective_chat.id, msg.message_id))

async def send_scheduled_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message: str, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    msg = await context.bot.send_message(chat_id=chat_id, text=f"⏰ Reminder: {message}")
    message_log.append((chat_id, msg.message_id))

def parse_time_prefix(text: str):
    match = re.match(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?\s+(.*)', text.strip())
    if match:
        hours = int(match.group(1)) if match.group(1) else 0
        minutes = int(match.group(2)) if match.group(2) else 0
        seconds = int(match.group(3)) if match.group(3) else 0
        message = match.group(4)
        total_seconds = hours * 3600 + minutes * 60 + seconds
        if total_seconds > 0 and message:
            return total_seconds, message
    return None, None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    # Save message ID for later deletion
    message_log.append((chat_id, msg_id))

    if "clear" in text:
        # Attempt to delete all tracked messages in the chat
        for cid, mid in message_log:
            try:
                await context.bot.delete_message(chat_id=cid, message_id=mid)
            except Exception as e:
                print(f"Failed to delete message {mid} in chat {cid}: {e}")
        message_log.clear()
        return

    if "time" in text:
        now = datetime.now()
        msg = await update.message.reply_text(f"Current time: {now.strftime('%H:%M:%S')}")
        message_log.append((chat_id, msg.message_id))

    else:
        delay_seconds, reminder_message = parse_time_prefix(text)
        if delay_seconds:
            msg = await update.message.reply_text(
                f"⏳ Reminder set in {delay_seconds // 60} minutes{' and ' + str(delay_seconds % 60) + ' seconds' if delay_seconds % 60 else ''}!"
            )
            message_log.append((chat_id, msg.message_id))
            asyncio.create_task(send_scheduled_message(context, chat_id, reminder_message, delay_seconds))
        else:
            msg = await update.message.reply_text("I didn’t understand that. Try 'timer', 'time', or '30m do something'.")
            message_log.append((chat_id, msg.message_id))

# Replace your real token here
app = Application.builder().token("8130124634:AAGKiaDIFMVhjO2uC383hjaPwRovZUPOJRE").build()

print("Bot is running...")

app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.run_polling()
