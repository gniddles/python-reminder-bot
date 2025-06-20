from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, ContextTypes, filters, CommandHandler, CallbackQueryHandler
import asyncio
import re

reminders = []
reminder_list_message_id = None
reminder_list_chat_id = None
message_log = []


async def update_reminder_list(context: ContextTypes.DEFAULT_TYPE):
    global reminder_list_message_id, reminder_list_chat_id

    if reminders:
        lines = ["📋 <b>Upcoming Reminders:</b>"]
        for msg, ts in sorted(reminders, key=lambda x: x[1]):
            time_str = datetime.fromtimestamp(ts).strftime("%d %b %H:%M")
            lines.append(f"• <b>{msg}</b> at <i>{time_str}</i>")
        text = "\n".join(lines)
    else:
        text = "📋 <b>No upcoming reminders.</b>"

    if reminder_list_message_id and reminder_list_chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=reminder_list_chat_id,
                message_id=reminder_list_message_id,
                text=text,
                parse_mode="HTML"
            )
            return
        except:
            pass

    msg = await context.bot.send_message(chat_id=reminder_list_chat_id or context._chat_id,
                                         text=text, parse_mode="HTML")
    reminder_list_message_id = msg.message_id
    reminder_list_chat_id = msg.chat.id


async def send_scheduled_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message: str, delay_seconds: int):
    timestamp = datetime.now().timestamp() + delay_seconds
    reminders.append((message, timestamp))
    await update_reminder_list(context)

    await asyncio.sleep(delay_seconds)

    sent_msg = await context.bot.send_message(chat_id=chat_id, text=f"⏰ Reminder: {message}")

    async def delete_reminder():
        await asyncio.sleep(300)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=sent_msg.message_id)
        except:
            pass

    asyncio.create_task(delete_reminder())

    reminders[:] = [(m, t) for m, t in reminders if not (m == message and abs(t - timestamp) < 1)]
    await update_reminder_list(context)


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


def parse_datetime_message(text: str):
    now = datetime.now()

    match_after = re.match(r'day after tomorrow\s+(\d{1,2}):(\d{2})\s+(.+)', text.strip())
    if match_after:
        hour, minute, message = match_after.groups()
        dt = (now + timedelta(days=2)).replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        return max((dt - now).total_seconds(), -1), message

    match_tomorrow = re.match(r'tomorrow\s+(\d{1,2}):(\d{2})\s+(.+)', text.strip())
    if match_tomorrow:
        hour, minute, message = match_tomorrow.groups()
        dt = (now + timedelta(days=1)).replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        return max((dt - now).total_seconds(), -1), message

    match_today = re.match(r'today\s+(\d{1,2}):(\d{2})\s+(.+)', text.strip())
    if match_today:
        hour, minute, message = match_today.groups()
        dt = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        return max((dt - now).total_seconds(), -1), message

    match_full = re.match(r'(\d{1,2})\s+([a-z]+)\s+(\d{1,2}):(\d{2})\s+(.+)', text.strip(), re.IGNORECASE)
    if match_full:
        day, month_str, hour, minute, message = match_full.groups()
        for fmt in ("%d %B %H:%M", "%d %b %H:%M"):
            try:
                dt = datetime.strptime(f"{day} {month_str} {hour}:{minute}", fmt).replace(year=now.year)
                return max((dt - now).total_seconds(), -1), message
            except:
                continue

    match_partial_time = re.match(r'(\d{1,2})\s+([a-z]+)\s+(\d{1,2})\s+(.+)', text.strip(), re.IGNORECASE)
    if match_partial_time:
        day, month_str, hour, message = match_partial_time.groups()
        for fmt in ("%d %B %H:%M", "%d %b %H:%M"):
            try:
                dt = datetime.strptime(f"{day} {month_str} {hour}:00", fmt).replace(year=now.year)
                return max((dt - now).total_seconds(), -1), message
            except:
                continue

    match_time = re.match(r'(\d{1,2}):(\d{2})\s+(.+)', text.strip())
    if match_time:
        hour, minute, message = match_time.groups()
        dt = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        return max((dt - now).total_seconds(), -1), message

    return None, None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id
    text = update.message.text.lower()

    async def delete_later(message_id):
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except:
            pass

    asyncio.create_task(delete_later(msg_id))

    global reminder_list_chat_id, reminder_list_message_id
    reminder_list_chat_id = chat_id
    if reminder_list_message_id is None:
        await update_reminder_list(context)

    if text in ["delete all", "del all"]:
        reminders.clear()
        await update_reminder_list(context)
        msg = await context.bot.send_message(chat_id=chat_id, text="🗑️ All reminders deleted.")
        asyncio.create_task(delete_later(msg.message_id))
        return

    if text.startswith("delete ") or text.startswith("del "):
        to_delete = text.replace("delete ", "", 1).replace("del ", "", 1).strip()
        before = len(reminders)
        reminders[:] = [(msg, ts) for (msg, ts) in reminders if msg.lower() != to_delete.lower()]

        if len(reminders) < before:
            msg = await context.bot.send_message(chat_id=chat_id, text=f"✅ Reminder \"{to_delete}\" deleted.")
        else:
            msg = await context.bot.send_message(chat_id=chat_id, text=f"⚠️ No reminder found with message: \"{to_delete}\"")
        asyncio.create_task(delete_later(msg.message_id))
        await update_reminder_list(context)
        return

    if "time" in text:
        now = datetime.now()
        msg = await context.bot.send_message(chat_id=chat_id, text=f"Current time: {now.strftime('%H:%M:%S')}")
        asyncio.create_task(delete_later(msg.message_id))
        return

    delay_dt, message_dt = parse_datetime_message(text)
    if delay_dt == -1:
        msg = await context.bot.send_message(chat_id=chat_id, text="⏰ This time has already passed.")
        asyncio.create_task(delete_later(msg.message_id))
        return
    elif delay_dt is not None:
        msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Reminder scheduled!")
        asyncio.create_task(delete_later(msg.message_id))
        asyncio.create_task(send_scheduled_message(context, chat_id, message_dt, delay_dt))
        return

    delay_seconds, reminder_message = parse_time_prefix(text)
    if delay_seconds:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Reminder set in {delay_seconds // 60} minutes"
                 f"{' and ' + str(delay_seconds % 60) + ' seconds' if delay_seconds % 60 else ''}!"
        )
        asyncio.create_task(delete_later(msg.message_id))
        asyncio.create_task(send_scheduled_message(context, chat_id, reminder_message, delay_seconds))
        return

    msg = await context.bot.send_message(chat_id=chat_id, text="I didn’t understand that. Try again'")
    asyncio.create_task(delete_later(msg.message_id))


def get_help_keyboard(state="full"):
    if state == "full":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📉 Collapse", callback_data="collapse_help"),
             InlineKeyboardButton("🗑️ Delete", callback_data="delete_help")]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 Uncollapse", callback_data="uncollapse_help"),
             InlineKeyboardButton("🗑️ Delete", callback_data="delete_help")]
        ])


def get_full_help_text():
    return (
        "<b>🤖 Reminder Bot - Help Menu</b>\n\n"
        "You can use the following formats to set reminders:\n"
        "• <code>10m feed the cat</code>\n"
        "• <code>1h30m water the plants</code>\n"
        "• <code>today 14:00 meeting</code>\n"
        "• <code>tomorrow 09:15 dentist</code>\n"
        "• <code>22 June 19:30 birthday party</code>\n\n"
        "🗑️ To delete reminders:\n"
        "• <b>delete all</b> or <b>del all</b>\n"
        "• <b>delete [name]</b> or <b>del [name]</b>\n"
        "• <b>time</b> — show current time\n\n"
        "Use the buttons below to collapse or delete this message."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(get_full_help_text(), parse_mode="HTML", reply_markup=get_help_keyboard("full"))
    await asyncio.sleep(5)
    try:
        await context.bot.delete_message(chat_id=msg.chat.id, message_id=update.message.message_id)
    except:
        pass


async def help_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "collapse_help":
        await query.edit_message_text("📖 <b>Help function</b>", parse_mode="HTML", reply_markup=get_help_keyboard("collapsed"))
    elif query.data == "uncollapse_help":
        await query.edit_message_text(get_full_help_text(), parse_mode="HTML", reply_markup=get_help_keyboard("full"))
    elif query.data == "delete_help":
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
        except:
            pass


app = Application.builder().token("8130124634:AAGKiaDIFMVhjO2uC383hjaPwRovZUPOJRE").build()

app.add_handler(CommandHandler("help", help_command))
app.add_handler(CallbackQueryHandler(help_button_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Bot is running...")
app.run_polling()
