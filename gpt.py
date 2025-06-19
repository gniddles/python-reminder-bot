from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import asyncio


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()

    if "timer" in text:
        await update.message.reply_text("Timer started for 5 seconds...")
        await asyncio.sleep(5)
        await update.message.reply_text("⏰ Time's up!")

    elif "time" in text:
        now = datetime.now()
        await update.message.reply_text(f"Current time: {now.strftime('%H:%M:%S')}")

    else:
        await update.message.reply_text("I didn’t understand that. Try typing 'timer' or 'time'.")


app = Application.builder().token("8130124634:AAGKiaDIFMVhjO2uC383hjaPwRovZUPOJRE").build()

print("Bot is running...")

# This handler responds to any text message (except commands like /start)
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

app.run_polling()
