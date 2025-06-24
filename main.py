from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, ContextTypes, filters, CommandHandler, CallbackQueryHandler
import asyncio
import re

reminders = {}  # message: (timestamp, asyncio.Task or message_id)
reminder_list_message_id = None
reminder_list_chat_id = None

LOCAL_TIMEZONE = ZoneInfo("Europe/Kyiv")


async def update_reminder_list(context: ContextTypes.DEFAULT_TYPE):
    global reminder_list_message_id, reminder_list_chat_id

    if not reminder_list_chat_id:
        return  # Chat is unknown

    if reminders:
        lines = ["📋 <b>Upcoming Reminders:</b>"]
        for msg, (ts, _) in sorted(reminders.items(), key=lambda x: x[1][0]):
            time_str = datetime.fromtimestamp(ts, tz=LOCAL_TIMEZONE).strftime("%d %b %H:%M")
            lines.append(f"• <b>{msg}</b> at <i>{time_str}</i>")
        text = "\n".join(lines)
    else:
        text = "📋 <b>No upcoming reminders.</b>"

    if reminder_list_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=reminder_list_chat_id,
                message_id=reminder_list_message_id,
                text=text,
                parse_mode="HTML"
            )
            return
        except Exception as e:
            if "Message is not modified" in str(e):
                # This is NOT a failure, just nothing to update
                return
            else:
                print("Could not edit reminder list message:", e)
                # Only reset if it failed for other reasons
                reminder_list_message_id = None
                reminder_list_chat_id = None

    # Only send a new message if no valid message exists
    if reminder_list_message_id is None:
        try:
            msg = await context.bot.send_message(chat_id=reminder_list_chat_id, text=text, parse_mode="HTML")
            reminder_list_message_id = msg.message_id
        except Exception as e:
            print("Failed to send reminder list message:", e)





async def send_scheduled_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message: str, delay_seconds: int):
    timestamp = datetime.now(LOCAL_TIMEZONE).timestamp() + delay_seconds

    async def task_body():
        await asyncio.sleep(delay_seconds)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Complete", callback_data=f"complete|{message}")]
        ])
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=f"⏰ Reminder: {message}", reply_markup=keyboard)
        reminders[message] = (timestamp, sent_msg.message_id)
        await update_reminder_list(context)

    task = asyncio.create_task(task_body())
    reminders[message] = (timestamp, task)
    await update_reminder_list(context)


async def complete_reminder_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("complete|"):
        return

    _, message = query.data.split("|", 1)
    chat_id = query.message.chat_id
    msg_id = query.message.message_id

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except:
        pass

    if message in reminders:
        del reminders[message]
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
    now = datetime.now(LOCAL_TIMEZONE)

    patterns = [
        (r'day after tomorrow\s+(\d{1,2}):(\d{2})\s+(.+)', 2),
        (r'tomorrow\s+(\d{1,2}):(\d{2})\s+(.+)', 1),
        (r'today\s+(\d{1,2}):(\d{2})\s+(.+)', 0)
    ]

    for pattern, days in patterns:
        match = re.match(pattern, text.strip())
        if match:
            hour, minute, message = match.groups()
            dt = (now + timedelta(days=days)).replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
            return max((dt - now).total_seconds(), -1), message

    match_full = re.match(r'(\d{1,2})\s+([a-z]+)\s+(\d{1,2}):(\d{2})\s+(.+)', text.strip(), re.IGNORECASE)
    if match_full:
        day, month_str, hour, minute, message = match_full.groups()
        for fmt in ("%d %B %H:%M", "%d %b %H:%M"):
            try:
                dt = datetime.strptime(f"{day} {month_str} {hour}:{minute}", fmt).replace(year=now.year, tzinfo=LOCAL_TIMEZONE)
                return max((dt - now).total_seconds(), -1), message
            except:
                continue

    match_partial = re.match(r'(\d{1,2})\s+([a-z]+)\s+(\d{1,2})\s+(.+)', text.strip(), re.IGNORECASE)
    if match_partial:
        day, month_str, hour, message = match_partial.groups()
        for fmt in ("%d %B %H:%M", "%d %b %H:%M"):
            try:
                dt = datetime.strptime(f"{day} {month_str} {hour}:00", fmt).replace(year=now.year, tzinfo=LOCAL_TIMEZONE)
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

    async def delete_later(mid):
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except:
            pass

    asyncio.create_task(delete_later(msg_id))

    global reminder_list_chat_id
    if reminder_list_chat_id is None:
        reminder_list_chat_id = chat_id


    if text in ["delete all", "del all"]:
        for _, task in reminders.values():
            if isinstance(task, asyncio.Task):
                task.cancel()
        reminders.clear()
        await update_reminder_list(context)
        msg = await context.bot.send_message(chat_id=chat_id, text="🗑️ All reminders deleted.")
        asyncio.create_task(delete_later(msg.message_id))
        return

    if text.startswith("delete ") or text.startswith("del "):
        to_delete = text.replace("delete ", "", 1).replace("del ", "", 1).strip()
        if to_delete in reminders:
            _, task = reminders[to_delete]
            if isinstance(task, asyncio.Task):
                task.cancel()
            del reminders[to_delete]
            await update_reminder_list(context)
            msg = await context.bot.send_message(chat_id=chat_id, text=f"✅ Reminder \"{to_delete}\" deleted.")
        else:
            msg = await context.bot.send_message(chat_id=chat_id, text=f"⚠️ No reminder found with message: \"{to_delete}\"")
        asyncio.create_task(delete_later(msg.message_id))
        return

    if "time" in text:
        now = datetime.now(LOCAL_TIMEZONE)
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

    msg = await context.bot.send_message(chat_id=chat_id, text="I didn’t understand that. Try again")
    asyncio.create_task(delete_later(msg.message_id))


def get_help_keyboard(state="full"):
    if state == "full":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📉 Collapse", callback_data="collapse_help"),
             InlineKeyboardButton("👝 Delete", callback_data="delete_help")]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 Uncollapse", callback_data="uncollapse_help"),
             InlineKeyboardButton("👝 Delete", callback_data="delete_help")]
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
app.add_handler(CallbackQueryHandler(help_button_handler, pattern=r"^(collapse_help|uncollapse_help|delete_help)$"))
app.add_handler(CallbackQueryHandler(complete_reminder_handler, pattern=r"^complete\|"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Bot is running...")
app.run_polling()
# asdasdasdasd