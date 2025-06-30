from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, ContextTypes, filters, CommandHandler, CallbackQueryHandler
import asyncio
import re
import logging

logging.basicConfig(level=logging.INFO)

TOKEN = "8130124634:AAGKiaDIFMVhjO2uC383hjaPwRovZUPOJRE"
reminders = {}  # chat_id: {message: (timestamp, task_or_id)}
reminder_list_message_ids = {}  # chat_id: message_id
removal_state = {}  # chat_id: {'mode': 'normal'|'confirm', 'target': str | None}
reminder_list_pinned = False

LOCAL_TIMEZONE = ZoneInfo("Europe/Kyiv")


from telegram.ext import CommandHandler, MessageHandler, filters

async def unknown_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    async def delete_later(mid):
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except:
            pass

    # Reply and delete both messages
    reply = await update.message.reply_text("I didn’t understand that. Try again")
    asyncio.create_task(delete_later(msg_id))               # delete user’s command
    asyncio.create_task(delete_later(reply.message_id))     # delete bot’s reply

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    start_msg_id = update.message.message_id

    # Send welcome message
    welcome = await update.message.reply_text(
        "👋 Hello, this is a reminder chat bot. Use /help function to see how to use me. You can delete this message when you want"
    )

    # Schedule deletion of user's /start command after 5 seconds
    async def delete_start_command():
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=start_msg_id)
        except:
            pass

    # Schedule deletion of the welcome message after 5 minutes
    async def delete_welcome_message():
        await asyncio.sleep(300)  # 300 seconds = 5 minutes
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=welcome.message_id)
        except:
            pass

    asyncio.create_task(delete_start_command())
    asyncio.create_task(delete_welcome_message())




def get_removal_keyboard(chat_id=None):
    user_reminders = reminders.get(chat_id, {})
    if chat_id in removal_state:
        state = removal_state[chat_id]
        if state.get("mode") == "confirm" and state.get("target"):
            msg_to_remove = state["target"]
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirm_delete|{msg_to_remove}"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_confirm")
                ]
            ])
        elif state.get("mode") == "removal":
            buttons = [
                [InlineKeyboardButton(f"🗑️ {msg}", callback_data=f"remove_reminder|{msg}")]
                for msg in user_reminders.keys()
            ]
            buttons.append([InlineKeyboardButton("✅ Done", callback_data="cancel_removal")])
            return InlineKeyboardMarkup(buttons)
    if user_reminders:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Remove Reminder", callback_data="start_removal")]
        ])
    return None




async def update_reminder_list(context: ContextTypes.DEFAULT_TYPE, chat_id=None):
    if chat_id is None:
        return

    user_reminders = reminders.get(chat_id, {})

    if user_reminders:
        lines = ["📋 <b>Upcoming Reminders:</b>"]
        for msg, (ts, _) in sorted(user_reminders.items(), key=lambda x: x[1][0]):
            time_str = datetime.fromtimestamp(ts, tz=LOCAL_TIMEZONE).strftime("%d %b %H:%M")
            lines.append(f"• <b>{msg}</b> at <i>{time_str}</i>")
        text = "\n".join(lines)
    else:
        text = "📋 <b>No upcoming reminders.</b>"

    try:
        message_id = reminder_list_message_ids.get(chat_id)

        if message_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=get_removal_keyboard(chat_id)
            )
        else:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=get_removal_keyboard(chat_id)
            )
            reminder_list_message_ids[chat_id] = msg.message_id

            if reminder_list_pinned:
                try:
                    await context.bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id, disable_notification=True)
                except:
                    pass
    except Exception as e:
        if "message to edit not found" in str(e).lower() or "message is not modified" not in str(e).lower():
            reminder_list_message_ids.pop(chat_id, None)




async def send_scheduled_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message: str, delay_seconds: int):
    timestamp = datetime.now(LOCAL_TIMEZONE).timestamp() + delay_seconds

    async def task_body():
        await asyncio.sleep(delay_seconds)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Complete", callback_data=f"complete|{message}"),
                InlineKeyboardButton("🔁 Snooze 5m", callback_data=f"snooze|{message}|300")
            ]
        ])
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=f"⏰ Reminder: {message}", reply_markup=keyboard)
        reminders.setdefault(chat_id, {})[message] = (timestamp, sent_msg.message_id)
        await update_reminder_list(context, chat_id)

    task = asyncio.create_task(task_body())
    reminders.setdefault(chat_id, {})[message] = (timestamp, task)
    await update_reminder_list(context, chat_id)



async def snooze_reminder_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, message, snooze_sec = query.data.split("|")
    try:
        await context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
    except:
        pass
    snooze_seconds = int(snooze_sec)
    chat_id = query.message.chat_id
    await send_scheduled_message(context, chat_id, message, snooze_seconds)

async def pin_reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global reminder_list_pinned
    reminder_list_pinned = not reminder_list_pinned
    status = "enabled 📌" if reminder_list_pinned else "disabled ❌"
    await update.message.reply_text(f"Pinned reminders are now {status}.")
    await update_reminder_list(context)


async def complete_reminder_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    _, message = query.data.split("|", 1)

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
    except:
        pass

    if message in reminders.get(chat_id, {}):
        _, task = reminders[chat_id][message]
        if isinstance(task, asyncio.Task):
            task.cancel()
        del reminders[chat_id][message]
        await update_reminder_list(context, chat_id)



async def handle_removal_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "start_removal":
        if reminders.get(chat_id):
            removal_state[chat_id] = {"mode": "removal", "target": None}
        await update_reminder_list(context, chat_id)

    elif query.data.startswith("remove_reminder|"):
        _, msg_to_remove = query.data.split("|", 1)
        removal_state[chat_id] = {"mode": "confirm", "target": msg_to_remove}
        await update_reminder_list(context, chat_id)

    elif query.data.startswith("confirm_delete|"):
        _, msg_to_remove = query.data.split("|", 1)
        if msg_to_remove in reminders.get(chat_id, {}):
            _, task = reminders[chat_id][msg_to_remove]
            if isinstance(task, asyncio.Task):
                task.cancel()
            del reminders[chat_id][msg_to_remove]
        removal_state.pop(chat_id, None)
        await update_reminder_list(context, chat_id)

    elif query.data == "cancel_confirm":
        if chat_id in removal_state and removal_state[chat_id]["mode"] == "confirm":
            removal_state[chat_id] = {"mode": "removal", "target": None}
            await update_reminder_list(context, chat_id)

    elif query.data == "cancel_removal":
        removal_state.pop(chat_id, None)
        await update_reminder_list(context, chat_id)


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

    if text in ["delete all", "del all"]:
        for _, task in reminders.get(chat_id, {}).values():
            if isinstance(task, asyncio.Task):
                task.cancel()
        reminders[chat_id] = {}
        await update_reminder_list(context, chat_id)
        msg = await context.bot.send_message(chat_id=chat_id, text="🗑️ All reminders deleted.")
        asyncio.create_task(delete_later(msg.message_id))
        return

    if text.startswith("delete ") or text.startswith("del "):
        to_delete = text.replace("delete ", "", 1).replace("del ", "", 1).strip()
        if to_delete in reminders.get(chat_id, {}):
            _, task = reminders[chat_id][to_delete]
            if isinstance(task, asyncio.Task):
                task.cancel()
            del reminders[chat_id][to_delete]
            await update_reminder_list(context, chat_id)
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


app = Application.builder().token(TOKEN).build()


app.add_handler(CommandHandler("start", start_command))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("pin_reminders", pin_reminders_command))
app.add_handler(CallbackQueryHandler(help_button_handler, pattern=r"^(collapse_help|uncollapse_help|delete_help)$"))
app.add_handler(CallbackQueryHandler(complete_reminder_handler, pattern=r"^complete\|"))
app.add_handler(CallbackQueryHandler(handle_removal_button, pattern=r"^(start_removal|remove_reminder\|.*|confirm_delete\|.*|cancel_confirm|cancel_removal)$"))
app.add_handler(CallbackQueryHandler(snooze_reminder_handler, pattern=r"^snooze\|"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.COMMAND, unknown_command_handler))



print("Bot is running...")
app.run_polling()