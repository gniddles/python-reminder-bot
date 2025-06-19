from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import asyncio
import re


async def start_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Timer started for 5 seconds...")
    await asyncio.sleep(5)
    await update.message.reply_text("⏰ Time's up!")


async def send_scheduled_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message: str, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    await context.bot.send_message(chat_id=chat_id, text=f"⏰ Reminder: {message}")


def parse_time_prefix(text: str):
    """
    Parses formats like '1h20m10s reminder', '30s stretch', etc.
    Returns (total_seconds, message) or (None, None) if no match.
    """
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

    if "timer" in text:
        asyncio.create_task(start_timer(update, context))

    elif "time" in text:
        now = datetime.now()
        await update.message.reply_text(f"Current time: {now.strftime('%H:%M:%S')}")

    else:
        delay_seconds, reminder_message = parse_time_prefix(text)
        if delay_seconds:
            await update.message.reply_text(
                f"⏳ Reminder set in {delay_seconds // 60} minutes{' and ' + str(delay_seconds % 60) + ' seconds' if delay_seconds % 60 else ''}!"
            )
            asyncio.create_task(send_scheduled_message(context, chat_id, reminder_message, delay_seconds))
        else:
            await update.message.reply_text("I didn’t understand that. Try 'timer', 'time', or '30m do something'.")


# Replace your real token here
app = Application.builder().token("8130124634:AAGKiaDIFMVhjO2uC383hjaPwRovZUPOJRE").build()

print("Bot is running...")

app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

app.run_polling()
