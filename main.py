from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio


async def timer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Timer started for 5 seconds...")
    await asyncio.sleep(5)
    await update.message.reply_text("⏰ Time's up!")



async def time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    await update.message.reply_text(f"Current time: {now.strftime('%H:%M:%S')}")

app = Application.builder().token("8130124634:AAGKiaDIFMVhjO2uC383hjaPwRovZUPOJRE").build()

print("Bot is running...")
app.add_handler(CommandHandler("timer", timer_command))
app.add_handler(CommandHandler("time", time_command))
app.run_polling()