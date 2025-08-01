from datetime import datetime, timedelta, timezone
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.error import Forbidden
from telegram.ext import Application, MessageHandler, ContextTypes, filters, CommandHandler, CallbackQueryHandler
import asyncio
import re
import logging
import sqlite3
from timezonefinder import TimezoneFinder
from zoneinfo import ZoneInfo, available_timezones

logging.basicConfig(level=logging.INFO)
TOKEN = "1014634066:AAGTFzlrmJQ7KSM4Bh98o2050IqiL508w5g"

datetime.now(timezone.utc)
detect_prompt_ids = {}
reminders = {}
reminder_list_message_ids = {}
removal_state = {}
editing_state = {}

DEFAULT_TZ = ZoneInfo("Europe/Kyiv")

DB = sqlite3.connect("reminder_bot_copy.db")
DB.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        tz TEXT NOT NULL
    )
""")
DB.execute("""
CREATE TABLE IF NOT EXISTS user_notes_mode (
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0
)""")
DB.execute("""
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    note       TEXT    NOT NULL,
    message_id INTEGER
)""")
DB.execute("""
CREATE TABLE IF NOT EXISTS reminder_meta (
    chat_id      INTEGER PRIMARY KEY,
    list_msg_id  INTEGER
)
""")
DB.execute("""
CREATE TABLE IF NOT EXISTS daily_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    time TEXT NOT NULL, -- format "HH:MM"
    text TEXT NOT NULL,
    last_done_date TEXT DEFAULT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
""")
DB.commit()


tf = TimezoneFinder()

def db_get_list_msg_id(chat_id: int) -> int | None:
    row = DB.execute(
        "SELECT list_msg_id FROM reminder_meta WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    return row[0] if row else None


def db_set_list_msg_id(chat_id: int, msg_id: int):
    DB.execute(
        "INSERT OR REPLACE INTO reminder_meta(chat_id, list_msg_id) VALUES (?,?)",
        (chat_id, msg_id)
    )
    DB.commit()

def fetch_daily_reminders(chat_id: int):
    return DB.execute(
        "SELECT id, time, text, last_done_date FROM daily_reminders WHERE chat_id=?",
        (chat_id,)
    ).fetchall()


async def mark_daily_done_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, daily_id = query.data.split("|")
        daily_id = int(daily_id)
        chat_id = query.message.chat_id

        # Get the user's timezone and current local date
        tz = get_chat_tz(chat_id)
        today_str = datetime.now(tz).strftime('%Y-%m-%d')

        # Update the daily reminder as done
        DB.execute(
            "UPDATE daily_reminders SET last_done_date=? WHERE id=? AND chat_id=?",
            (today_str, daily_id, chat_id)
        )
        DB.commit()

        # Delete the daily reminder message (the one with the "Done" button)
        try:
            await context.bot.delete_message(chat_id, query.message.message_id)
        except Exception as e:
            logging.warning(f"Failed to delete message after marking daily done: {e}")

        # Refresh the upcoming reminder list
        await update_reminder_list(context, chat_id)

    except Exception as e:
        logging.error(f"Error handling daily_done: {e}", exc_info=True)


def delete_daily_reminder(chat_id: int, text: str):
    DB.execute("DELETE FROM daily_reminders WHERE chat_id=? AND text=?", (chat_id, text))
    DB.commit()

def delete_daily_reminder_by_id(chat_id: int, daily_id: int):
    DB.execute("DELETE FROM daily_reminders WHERE chat_id=? AND id=?", (chat_id, daily_id))
    DB.commit()

def db_delete_list_msg_id(chat_id: int):
    DB.execute("DELETE FROM reminder_meta WHERE chat_id=?", (chat_id,))
    DB.commit()

def get_chat_tz(chat_id: int) -> ZoneInfo:
    row = DB.execute("SELECT tz FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    return ZoneInfo(row[0]) if row else DEFAULT_TZ

def set_chat_tz(chat_id: int, tz_str: str):
    DB.execute("INSERT OR REPLACE INTO users(chat_id, tz) VALUES(?, ?)", (chat_id, tz_str))
    DB.commit()

def db_add_reminder(chat_id: int, text: str, fire_at: int):
    DB.execute(
        "INSERT INTO reminders(chat_id, text, fire_at) VALUES(?,?,?)",
        (chat_id, text, fire_at)
    )
    DB.commit()

def db_delete_reminder(chat_id: int, text: str):
    DB.execute(
        "DELETE FROM reminders WHERE chat_id=? AND text=?",
        (chat_id, text)
    )
    DB.commit()

def db_update_reminder(chat_id: int, text: str, new_fire_at: int):
    DB.execute(
        "UPDATE reminders SET fire_at=? WHERE chat_id=? AND text=?",
        (new_fire_at, chat_id, text)
    )
    DB.commit()

def db_fetch_future():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    return DB.execute(
        "SELECT chat_id, text, fire_at FROM reminders WHERE fire_at>?",
        (now_ts,)
    ).fetchall()

def db_delete_all_reminders(chat_id: int):
    DB.execute("DELETE FROM reminders WHERE chat_id = ?", (chat_id,))
    DB.commit()
    
def delete_all_notes(chat_id: int):
    DB.execute("DELETE FROM notes WHERE chat_id = ?", (chat_id,))
    DB.commit()

async def refresh_or_exit_edit_mode(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if reminders.get(chat_id) or fetch_notes(chat_id) or fetch_daily_reminders(chat_id):
        await update_reminder_list(context, chat_id)
    else:
        removal_state.pop(chat_id, None)
        await update_reminder_list(context, chat_id)


async def detect_timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cmd_id = update.message.message_id

    async def delayed_delete(chat_id, message_id, delay):
        try:
            await asyncio.sleep(delay)
            await context.bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    asyncio.create_task(delayed_delete(chat_id, cmd_id, 5))

    kb = [
        [KeyboardButton("📍 Share Location", request_location=True)],
        [KeyboardButton("❌ Cancel")]
    ]
    markup = ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True)
    
    prompt = await update.message.reply_text(
        "Please share your location to auto-detect your time-zone or tap Cancel:",
        reply_markup=markup
    )
    detect_prompt_ids[chat_id] = prompt.message_id

    async def delete_prompt_later():
        await asyncio.sleep(60)
        mid = detect_prompt_ids.pop(chat_id, None)
        if mid:
            try:
                await context.bot.delete_message(chat_id, mid)
            except Exception:
                pass
    asyncio.create_task(delete_prompt_later())

def notes_enabled(chat_id: int) -> bool:
    row = DB.execute("SELECT enabled FROM user_notes_mode WHERE chat_id = ?", (chat_id,)).fetchone()
    return bool(row[0]) if row else False

def set_notes_enabled(chat_id: int, enabled: bool):
    DB.execute(
        "INSERT OR REPLACE INTO user_notes_mode(chat_id, enabled) VALUES(?, ?)",
        (chat_id, int(enabled))
    )
    DB.commit()

def add_note(chat_id: int, text: str, message_id: int):
    DB.execute(
        "INSERT INTO notes(chat_id, note, message_id) VALUES(?,?,?)",
        (chat_id, text, message_id)
    )
    DB.commit()

def delete_note(chat_id: int, note_id: int):
    DB.execute("DELETE FROM notes WHERE id=? AND chat_id=?", (note_id, chat_id))
    DB.commit()

def fetch_notes(chat_id: int):
    return DB.execute("SELECT id, note FROM notes WHERE chat_id=?", (chat_id,)).fetchall()

async def notes_toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cmd_mid = update.message.message_id
    new_state = not notes_enabled(chat_id)
    set_notes_enabled(chat_id, new_state)

    reply = await update.message.reply_text(
        f"Notes mode is now <b>{'ON 📝' if new_state else 'OFF'}</b>.",
        parse_mode="HTML",
        reply_markup=delete_keyboard()
    )

    async def _cleanup():
        await asyncio.sleep(5)
        for mid in (cmd_mid, reply.message_id):
            try:
                await context.bot.delete_message(chat_id, mid)
            except:
                pass
    asyncio.create_task(_cleanup())

async def send_note(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    # Check if this exact note already exists for the user
    existing = DB.execute(
        "SELECT id FROM notes WHERE chat_id=? AND note=?",
        (chat_id, text)
    ).fetchone()
    if existing:
        return  # Avoid duplicate note

    tmp_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Complete", callback_data="noop")]]
    )
    sent = await context.bot.send_message(chat_id, text, reply_markup=tmp_kb)

    cur = DB.cursor()
    cur.execute(
        "INSERT INTO notes(chat_id, note, message_id) VALUES (?,?,?)",
        (chat_id, text, sent.message_id)
    )
    note_id = cur.lastrowid
    DB.commit()

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Complete", callback_data=f"complete_note|{note_id}")]]
    )
    await context.bot.edit_message_reply_markup(
        chat_id=chat_id,
        message_id=sent.message_id,
        reply_markup=kb
    )

    await update_reminder_list(context, chat_id)

last_checked_date = None

async def daily_reminder_loop(app: Application):
    global last_checked_date
    logging.info("🕒 Daily reminder loop started")

    while True:
        await asyncio.sleep(60)
        now_utc = datetime.now(timezone.utc)

        for row in DB.execute("SELECT DISTINCT chat_id FROM daily_reminders"):
            chat_id = row[0]
            tz = get_chat_tz(chat_id)
            now_local = now_utc.astimezone(tz)
            now_str = now_local.strftime("%H:%M")
            today_str = now_local.strftime("%Y-%m-%d")

            # ✅ Always perform cleanup once per day
            if last_checked_date != today_str:
                yesterday_str = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")

                DB.execute("""
                    DELETE FROM daily_reminders 
                    WHERE chat_id = ?
                    AND (last_done_date IS NULL OR last_done_date != ?)
                    AND DATE(created_at) < ?
                """, (chat_id, yesterday_str, today_str))

                DB.commit()
                last_checked_date = today_str
                await update_reminder_list(app, chat_id)

            # Reset or trigger today's reminders
            for daily_id, time_str, text, last_done in fetch_daily_reminders(chat_id):
                if last_done and last_done != today_str:
                    DB.execute("UPDATE daily_reminders SET last_done_date=NULL WHERE id=? AND chat_id=?", (daily_id, chat_id))
                    DB.commit()
                    last_done = None

                if time_str == now_str and last_done != today_str:
                    try:
                        keyboard = InlineKeyboardMarkup([
                            [InlineKeyboardButton("✅ Done", callback_data=f"daily_done|{daily_id}")]
                        ])
                        await app.bot.send_message(chat_id=chat_id, text=f"📅 Daily Reminder: {text}", reply_markup=keyboard)
                    except Exception as e:
                        logging.warning(f"Failed to send daily reminder to {chat_id}: {e}")





async def complete_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, note_id = query.data.split("|", 1)
    note_id = int(note_id)
    chat_id = query.message.chat_id

    try:
        await context.bot.delete_message(chat_id, query.message.message_id)
    except:
        pass

    delete_note(chat_id, note_id)
    await update_reminder_list(context, chat_id)

def delete_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard with one Delete button."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗑️ Delete", callback_data="delmsg")]]
    )

async def delete_own_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete the message containing the pressed Delete button."""
    query = update.callback_query
    await query.answer()

    try:
        await context.bot.delete_message(chat_id=query.message.chat_id,
                                         message_id=query.message.message_id)
    except:
        pass

async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id    = update.effective_chat.id
    loc_msg_id = update.message.message_id

    async def delete_location():
        await asyncio.sleep(2)
        try:
            await context.bot.delete_message(chat_id, loc_msg_id)
        except Exception:
            pass
    asyncio.create_task(delete_location())

    prompt_id = detect_prompt_ids.pop(chat_id, None)
    if prompt_id:
        try:
            await context.bot.delete_message(chat_id, prompt_id)
        except Exception:
            pass

    loc = update.message.location
    if loc:
        tzname = tf.timezone_at(lat=loc.latitude, lng=loc.longitude)
        if tzname and tzname in available_timezones():
            set_chat_tz(chat_id, tzname)
            reply_text = f"✅ Time‑zone detected and set to <b>{tzname}</b>."
        else:
            reply_text = (
                "❌ Couldn't determine your time‑zone. "
                "Set it manually with /timezone."
            )
    else:
        reply_text = "⚠️ No location received."
        
    result_msg = await update.message.reply_text(
        reply_text,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )

    async def delete_result():
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(chat_id, result_msg.message_id)
        except Exception:
            pass
    asyncio.create_task(delete_result())

async def detect_cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cancel_msg_id = update.message.message_id

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=cancel_msg_id)
    except Exception:
        pass

    prompt_id = detect_prompt_ids.pop(chat_id, None)
    if prompt_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prompt_id)
        except Exception:
            pass

    notice = await context.bot.send_message(
        chat_id=chat_id,
        text="❌ Location request cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )

    async def delete_notice():
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=notice.message_id)
        except Exception:
            pass

    asyncio.create_task(delete_notice())

async def timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id
    async def delete_later():
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except:
            pass
    asyncio.create_task(delete_later())

    if not context.args:
        current = get_chat_tz(chat_id).key
        await update.message.reply_text(
            f"Your current time‑zone is <b>{current}</b>.\n"
            "Use /timezone <code>Region/City</code> to change it, e.g.:\n"
            "<code>/timezone Europe/Paris</code>\n"
            "Or type /dtz to detect it automatically.\n"
            "If you are using a desktop version of telegram /dtz command wor't work",
            parse_mode="HTML",
            reply_markup=delete_keyboard()
        )
        return

    tz_candidate = " ".join(context.args)
    if tz_candidate not in available_timezones():
        await update.message.reply_text(
            "⚠️ Unknown time‑zone. Use a valid IANA identifier like "
            "<code>America/New_York</code> or <code>Asia/Tokyo</code>.",
            parse_mode="HTML",
            reply_markup=delete_keyboard()
        )
        return

    set_chat_tz(chat_id, tz_candidate)
    await update.message.reply_text(
        f"✅ Time‑zone set to <b>{tz_candidate}</b>.",
        parse_mode="HTML",
        reply_markup=delete_keyboard()
    )



async def unknown_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    async def delete_later(mid):
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except:
            pass

    reply = await update.message.reply_text("I didn’t understand that. Try again")
    asyncio.create_task(delete_later(msg_id))
    asyncio.create_task(delete_later(reply.message_id))



async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    async def delete_later():
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except:
            pass
    asyncio.create_task(delete_later())

    try:
        welcome = await update.message.reply_text(
            "👋 Hello, this is a reminder chat bot. Use /help to see how to use me. "
            "You can delete this message whenever you want.",
            reply_markup=delete_keyboard()
        )
        async def delete_welcome():
            await asyncio.sleep(300)
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=welcome.message_id)
            except:
                pass
        asyncio.create_task(delete_welcome())

    except Exception as e:
        logging.warning(f"Could not send welcome message: {e}")



async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Forbidden):
        context.application.logger.info("Message blocked – user has blocked the bot.")
    else:
        logging.exception("Unhandled exception", exc_info=context.error)

def get_removal_keyboard(chat_id=None):
    user_reminders = reminders.get(chat_id, {})
    user_notes = fetch_notes(chat_id)
    user_dailies = fetch_daily_reminders(chat_id)

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
        elif state.get("mode") in {"removal", "edit"}:
            buttons = []

            for daily_id, _, msg, _ in user_dailies:
                label = f"🗑️ {msg}" if state["mode"] == "removal" else f"✏️ {msg}"
                cb = f"remove_daily|{daily_id}" if state["mode"] == "removal" else f"edit_daily|{daily_id}"
                buttons.append([InlineKeyboardButton(label, callback_data=cb)])
            
            for msg in user_reminders.keys():
                label = f"🗑️ {msg}" if state["mode"] == "removal" else f"✏️ {msg}"
                cb = f"remove_reminder|{msg}" if state["mode"] == "removal" else f"edit_reminder|{msg}"
                buttons.append([InlineKeyboardButton(label, callback_data=cb)])

            for note_id, note in user_notes:
                label = f"🗑️ {note}" if state["mode"] == "removal" else f"✏️ {note}"
                cb = f"remove_note|{note_id}" if state["mode"] == "removal" else f"edit_note|{note_id}"
                buttons.append([InlineKeyboardButton(label, callback_data=cb)])

            buttons.append([
                InlineKeyboardButton("✅ Done", callback_data="cancel_removal")
            ])

            return InlineKeyboardMarkup(buttons)

    # ✅ Show the main edit/delete buttons if any reminders, notes, OR daily exist
    if user_reminders or user_notes or user_dailies:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✏️ Edit", callback_data="start_edit"),
                InlineKeyboardButton("🗑️ Delete Reminder", callback_data="start_removal")
            ]
        ])

    return None



async def update_reminder_list(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    tz = get_chat_tz(chat_id)
    user_reminders = reminders.get(chat_id, {})
    user_notes = fetch_notes(chat_id)
    daily_reminders = fetch_daily_reminders(chat_id)

    today_str = datetime.now(tz).strftime('%Y-%m-%d')

    lines = []
    if daily_reminders:
        lines.append("🗓️ <b>Daily Reminders:</b>")
        for daily_id, time_str, msg, last_done in daily_reminders:
            status = "✅ Done" if last_done == today_str else ""
            lines.append(f"• <b>{msg}</b> at <i>{time_str}</i> {status}")

    if user_reminders:
        lines.append("\n⏰ <b>Timed Reminders:</b>")
        for msg, (ts, _) in sorted(user_reminders.items(), key=lambda x: x[1][0]):
            tstr = datetime.fromtimestamp(ts, tz=tz).strftime("%d %b %H:%M")
            lines.append(f"• <b>{msg}</b> at <i>{tstr}</i>")

    if user_notes:
        lines.append("\n📝 <b>Notes:</b>")
        for _, note_txt in user_notes:
            lines.append(f"• <b>{note_txt}</b>")

    if not lines:
        text = "<b>REMINDER BOT</B>\n" + "\n📋 <b>No upcoming reminders.</b>"
    else:
        text = "<b>REMINDER BOT</b>\n" + "\n".join(lines)



    # 🧠 Fix: If we are in edit/remove mode but no content remains, clear mode
    if not (user_reminders or user_notes or daily_reminders):
        removal_state.pop(chat_id, None)
        keyboard = None
    else:
        # 🧠 Fix: Ensure we return to main menu after edit
        if chat_id in removal_state and removal_state[chat_id]["mode"] in {"edit", "removal"}:
            keyboard = get_removal_keyboard(chat_id)
        elif chat_id not in removal_state:
            keyboard = get_removal_keyboard(chat_id)
        else:
            keyboard = get_removal_keyboard(chat_id)

    mid = reminder_list_message_ids.get(chat_id)
    if mid is None:
        mid = db_get_list_msg_id(chat_id)
        if mid:
            reminder_list_message_ids[chat_id] = mid

    try:
        if mid:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=mid,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            reminder_list_message_ids[chat_id] = msg.message_id
            db_set_list_msg_id(chat_id, msg.message_id)
    except Exception as e:
        if "message to edit not found" in str(e).lower():
            reminder_list_message_ids.pop(chat_id, None)
            db_delete_list_msg_id(chat_id)
            await update_reminder_list(context, chat_id)



async def send_scheduled_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message: str,
    delay_seconds: int,
    *,
    store_in_db: bool = True
):
    fire_at = int(datetime.now(timezone.utc).timestamp() + delay_seconds)

    if store_in_db:
        db_add_reminder(chat_id, message, fire_at)

    async def task_body():
        try:
            await asyncio.sleep(delay_seconds)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Complete", callback_data=f"complete|{message}"),
                 InlineKeyboardButton("🔁 Snooze 5m", callback_data=f"snooze|{message}|300")]
            ])

            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ Reminder: {message}",
                reply_markup=keyboard,
            )

            reminders.setdefault(chat_id, {})[message] = (fire_at, sent.message_id)
            db_delete_reminder(chat_id, message)
            await update_reminder_list(context, chat_id)
        except Exception as e:
            logging.error(f"❌ Failed to send scheduled reminder: {e}", exc_info=True)

    task = asyncio.create_task(task_body())
    reminders.setdefault(chat_id, {})[message] = (fire_at, task)
    await update_reminder_list(context, chat_id)


    
    

async def snooze_reminder_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, message, snooze_sec = query.data.split("|")
    snooze_seconds = int(snooze_sec)
    chat_id = query.message.chat_id

    try:
        await context.bot.delete_message(chat_id=chat_id,
                                         message_id=query.message.message_id)
    except Exception:
        pass
    db_delete_reminder(chat_id, message)
    await send_scheduled_message(context, chat_id, message, snooze_seconds)



async def pin_reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global reminder_list_pinned
    reminder_list_pinned = not reminder_list_pinned
    status = "enabled 📌" if reminder_list_pinned else "disabled ❌"
    await update.message.reply_text(f"Pinned reminders are now {status}.")
    await update_reminder_list(context)



async def complete_reminder_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logging.warning(f"Failed to answer callback query: {e}")
    
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
    
    if query.data == "start_edit":
        if reminders.get(chat_id) or fetch_notes(chat_id) or fetch_daily_reminders(chat_id):
            removal_state[chat_id] = {"mode": "edit", "target": None}
        await update_reminder_list(context, chat_id)
        return

    if query.data == "start_removal":
        if reminders.get(chat_id) or fetch_notes(chat_id) or fetch_daily_reminders(chat_id):
            removal_state[chat_id] = {"mode": "removal", "target": None}
        await update_reminder_list(context, chat_id)
        return

    elif query.data.startswith("remove_reminder|"):
        _, msg_to_remove = query.data.split("|", 1)
        removal_state[chat_id] = {"mode": "confirm", "target": msg_to_remove, "type": "reminder"}
        await update_reminder_list(context, chat_id)

    elif query.data.startswith("remove_note|"):
        _, note_id = query.data.split("|", 1)
        removal_state[chat_id] = {"mode": "confirm", "target": int(note_id), "type": "note"}
        await update_reminder_list(context, chat_id)
    
    elif query.data.startswith("remove_daily|"):
        _, daily_id = query.data.split("|", 1)
        removal_state[chat_id] = {
            "mode": "confirm",
            "target": int(daily_id),
            "type": "daily"
        }
        await update_reminder_list(context, chat_id)

    elif query.data.startswith("edit_daily|"):
        _, daily_id = query.data.split("|", 1)
        editing_state[chat_id] = {"type": "daily", "daily_id": int(daily_id)}
        msg = await context.bot.send_message(
            chat_id,
            "✏️ Send me the edited version of your daily reminder.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")]])
        )
        editing_state[chat_id]["prompt_msg_id"] = msg.message_id

    elif query.data.startswith("edit_reminder|"):
        _, original_text = query.data.split("|", 1)
        editing_state[chat_id] = {"type": "reminder", "original": original_text}
        msg = await context.bot.send_message(
            chat_id, 
            "✏️ Send me the edited version of your reminder.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")]])
        )
        editing_state[chat_id]["prompt_msg_id"] = msg.message_id

    elif query.data.startswith("edit_note|"):
        _, note_id = query.data.split("|", 1)
        note_id = int(note_id)
        editing_state[chat_id] = {"type": "note", "note_id": note_id}
        msg = await context.bot.send_message(
            chat_id, 
            "✏️ Send me the edited version of your note.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")]])
        )
        editing_state[chat_id]["prompt_msg_id"] = msg.message_id

    elif query.data == "cancel_edit":
        state = editing_state.pop(chat_id, None)
        if state and "prompt_msg_id" in state:
            try:
                await context.bot.delete_message(chat_id, state["prompt_msg_id"])
            except Exception:
                pass
        msg = await context.bot.send_message(chat_id, "❌ Edit cancelled.")
        async def delete_later():
            await asyncio.sleep(5)
            try:
                await context.bot.delete_message(chat_id, msg.message_id)
            except:
                pass
        asyncio.create_task(delete_later())

    elif query.data.startswith("confirm_delete|"):
        _, target = query.data.split("|", 1)
        state = removal_state.get(chat_id, {})
        if state.get("type") == "reminder":
            if target in reminders.get(chat_id, {}):
                _, handle = reminders[chat_id][target]
                if isinstance(handle, asyncio.Task):
                    handle.cancel()
                del reminders[chat_id][target]
            db_delete_reminder(chat_id, target)
        elif state.get("type") == "note":
            note_id = int(target)
            row = DB.execute(
                "SELECT message_id FROM notes WHERE id=? AND chat_id=?",
                (note_id, chat_id)
            ).fetchone()
            if row:
                try:
                    await context.bot.delete_message(chat_id, row[0])
                except Exception:
                    pass
            delete_note(chat_id, note_id)
        elif state.get("type") == "daily":
            daily_id = int(target)
            delete_daily_reminder_by_id(chat_id, daily_id)

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

def parse_datetime_message(text: str, tz: ZoneInfo):
    """
    Interpret phrases like ‘today 14:00 meeting’ or
    ‘22 June 19:30 party’ relative to the supplied time‑zone.

    Returns (delay_seconds, message) or (None, None) if no match.
    If the given time is in the past, returns (-1, message) so the
    caller can tell the user.
    """
    now = datetime.now(tz)
    patterns = [
        (r'day after tomorrow\s+(\d{1,2}):(\d{2})\s+(.+)', 2),
        (r'tomorrow\s+(\d{1,2}):(\d{2})\s+(.+)', 1),
        (r'today\s+(\d{1,2}):(\d{2})\s+(.+)', 0)
    ]
    for pattern, days in patterns:
        m = re.match(pattern, text.strip())
        if m:
            hour, minute, message = m.groups()
            dt = (now + timedelta(days=days)).replace(
                hour=int(hour), minute=int(minute),
                second=0, microsecond=0
            )
            return max((dt - now).total_seconds(), -1), message

    m = re.match(r'(\d{1,2})\s+([a-z]+)\s+(\d{1,2}):(\d{2})\s+(.+)',
                 text.strip(), re.IGNORECASE)
    if m:
        day, mon, hour, minute, message = m.groups()
        for fmt in ("%d %B %H:%M", "%d %b %H:%M"):
            try:
                dt = datetime.strptime(
                    f"{day} {mon} {hour}:{minute}", fmt
                ).replace(year=now.year, tzinfo=tz)
                return max((dt - now).total_seconds(), -1), message
            except ValueError:
                pass
            
    m = re.match(r'(\d{1,2})\s+([a-z]+)\s+(\d{1,2})\s+(.+)',
                 text.strip(), re.IGNORECASE)
    if m:
        day, mon, hour, message = m.groups()
        for fmt in ("%d %B %H:%M", "%d %b %H:%M"):
            try:
                dt = datetime.strptime(
                    f"{day} {mon} {hour}:00", fmt
                ).replace(year=now.year, tzinfo=tz)
                return max((dt - now).total_seconds(), -1), message
            except ValueError:
                pass

    m = re.match(r'(\d{1,2}):(\d{2})\s+(.+)', text.strip())
    if m:
        hour, minute, message = m.groups()
        dt = now.replace(hour=int(hour), minute=int(minute),
                         second=0, microsecond=0)
        return max((dt - now).total_seconds(), -1), message
    return None, None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz = get_chat_tz(chat_id)
    msg_id = update.message.message_id
    text = update.message.text.strip()

    async def delete_later(mid, delay=5):
        await asyncio.sleep(delay)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except:
            pass

    asyncio.create_task(delete_later(msg_id))

    # Handle editing state first
    if chat_id in editing_state:
        state = editing_state.pop(chat_id)
        prompt_id = state.get("prompt_msg_id")
        if prompt_id:
            try:
                await context.bot.delete_message(chat_id, prompt_id)
            except:
                pass

        if state["type"] == "reminder":
            original = state["original"]
            if original in reminders.get(chat_id, {}):
                fire_at, handle = reminders[chat_id].pop(original)
                if isinstance(handle, asyncio.Task):
                    handle.cancel()
                    db_delete_reminder(chat_id, original)
                    await send_scheduled_message(
                        context,
                        chat_id,
                        text,
                        fire_at - int(datetime.now(timezone.utc).timestamp())
                    )
                elif isinstance(handle, int):
                    try:
                        keyboard = InlineKeyboardMarkup([
                            [InlineKeyboardButton("✅ Complete", callback_data=f"complete|{text}"),
                             InlineKeyboardButton("🔁 Snooze 5m", callback_data=f"snooze|{text}|300")]
                        ])
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=handle,
                            text=f"⏰ Reminder: {text}",
                            reply_markup=keyboard
                        )
                        reminders[chat_id][text] = (fire_at, handle)
                        db_delete_reminder(chat_id, original)
                    except:
                        await send_scheduled_message(
                            context,
                            chat_id,
                            text,
                            fire_at - int(datetime.now(timezone.utc).timestamp())
                        )
                removal_state.pop(chat_id, None)
                await update_reminder_list(context, chat_id)
            return

        elif state["type"] == "note":
            note_id = state["note_id"]
            row = DB.execute("SELECT message_id FROM notes WHERE id=? AND chat_id=?", (note_id, chat_id)).fetchone()
            if row:
                msg_id_old = row[0]
                try:
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Complete", callback_data=f"complete_note|{note_id}")]
                    ])
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id_old,
                        text=text,
                        reply_markup=kb
                    )
                except:
                    pass
                DB.execute("UPDATE notes SET note=? WHERE id=? AND chat_id=?", (text, note_id, chat_id))
                DB.commit()
                removal_state.pop(chat_id, None)
                await update_reminder_list(context, chat_id)
            return

        elif state["type"] == "daily":
            daily_id = state["daily_id"]
            DB.execute("UPDATE daily_reminders SET text=? WHERE id=? AND chat_id=?", (text, daily_id, chat_id))
            DB.commit()
            removal_state.pop(chat_id, None)
            await update_reminder_list(context, chat_id)
            return

    # Handle creation of daily reminders
    m = re.match(r'^daily\s+(\d{1,2}):(\d{2})\s+(.+)', text, re.IGNORECASE)
    if m:
        hour, minute, message = m.groups()
        DB.execute(
            "INSERT INTO daily_reminders(chat_id, time, text) VALUES (?,?,?)",
            (chat_id, f"{int(hour):02d}:{int(minute):02d}", message)
        )
        DB.commit()
        await update_reminder_list(context, chat_id)
        return

    # Handle full deletion of all types
    if text.lower() in {"delete all", "del all"}:
        for message, (ts, handle_or_msgid) in reminders.get(chat_id, {}).items():
            if isinstance(handle_or_msgid, asyncio.Task):
                handle_or_msgid.cancel()
            elif isinstance(handle_or_msgid, int):
                try:
                    await context.bot.delete_message(chat_id, handle_or_msgid)
                except:
                    pass
        reminders[chat_id] = {}
        db_delete_all_reminders(chat_id)

        for note_id, note_text, msg_id in DB.execute(
            "SELECT id, note, message_id FROM notes WHERE chat_id=?", (chat_id,)
        ).fetchall():
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except:
                pass
        delete_all_notes(chat_id)

        DB.execute("DELETE FROM daily_reminders WHERE chat_id=?", (chat_id,))
        DB.commit()

        removal_state.pop(chat_id, None)
        await update_reminder_list(context, chat_id)
        return

    # Handle single deletions
    if text.lower().startswith(("delete ", "del ")):
        target = text.split(maxsplit=1)[1]
        if target in reminders.get(chat_id, {}):
            _, handle = reminders[chat_id][target]
            if isinstance(handle, asyncio.Task):
                handle.cancel()
            elif isinstance(handle, int):
                try:
                    await context.bot.delete_message(chat_id, handle)
                except:
                    pass
            del reminders[chat_id][target]
            db_delete_reminder(chat_id, target)
            removal_state.pop(chat_id, None)
            await update_reminder_list(context, chat_id)
            return

        row = DB.execute("SELECT id, message_id FROM notes WHERE note=? AND chat_id=?", (target, chat_id)).fetchone()
        if row:
            note_id, message_id = row
            delete_note(chat_id, note_id)
            try:
                await context.bot.delete_message(chat_id, message_id)
            except:
                pass
            removal_state.pop(chat_id, None)
            await update_reminder_list(context, chat_id)
            return

        row = DB.execute("SELECT id FROM daily_reminders WHERE chat_id=? AND text=?", (chat_id, target)).fetchone()
        if row:
            delete_daily_reminder(chat_id, target)
            removal_state.pop(chat_id, None)
            await update_reminder_list(context, chat_id)
            return

    # Handle time query
    if "time" in text.lower():
        now = datetime.now(tz)
        m = await context.bot.send_message(chat_id, f"Current time: {now:%H:%M:%S}")
        asyncio.create_task(delete_later(m.message_id))
        return

    # Handle natural language datetime reminder
    delay_dt, msg_dt = parse_datetime_message(text, tz)
    if delay_dt == -1:
        m = await context.bot.send_message(chat_id, "⏰ This time has already passed.")
        asyncio.create_task(delete_later(m.message_id))
        return
    if delay_dt is not None:
        asyncio.create_task(
            send_scheduled_message(context, chat_id, msg_dt, int(delay_dt))
        )
        return

    # Handle relative time reminder
    delay_s, msg_rel = parse_time_prefix(text)
    if delay_s:
        asyncio.create_task(
            send_scheduled_message(context, chat_id, msg_rel, delay_s)
        )
        return

    # Notes
    if notes_enabled(chat_id):
        lines = [line.strip() for line in update.message.text.splitlines() if line.strip()]
        for line in lines:
            await send_note(context, chat_id, line)
    else:
        m = await context.bot.send_message(chat_id, "I didn’t understand that. Try again.")
        asyncio.create_task(delete_later(m.message_id))





async def on_startup(app: Application):
    await restore_tasks_on_startup(app)
    asyncio.create_task(daily_reminder_loop(app))  # ← Add this line


from types import SimpleNamespace

async def restore_tasks_on_startup(app: Application):
    """
    Restore future reminders from DB and re-schedule them as asyncio tasks.
    Also refresh reminder list messages.
    """
    # Load list message IDs into memory
    for chat_id, mid in DB.execute("SELECT chat_id, list_msg_id FROM reminder_meta"):
        reminder_list_message_ids[chat_id] = mid

    now_ts = int(datetime.now(timezone.utc).timestamp())
    chats_touched: set[int] = set()

    for chat_id, text, fire_at in db_fetch_future():
        delay = fire_at - now_ts
        if delay <= 0:
            continue

        # Fake context object with bot reference
        context = SimpleNamespace()
        context.bot = app.bot
        context.application = app

        await send_scheduled_message(context, chat_id, text, delay, store_in_db=False)
        chats_touched.add(chat_id)

    # Update reminder list for each chat
    for chat_id in chats_touched:
        await update_reminder_list(context, chat_id)


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
async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send (or refresh) the single 📋 Upcoming Reminders message.

    /reminders           → edit existing list or create one if missing
    /reminders new|fresh → always create a brand‑new list message
    """
    chat_id = update.effective_chat.id
    cmd_mid = update.message.message_id
    if context.args and context.args[0].lower() in {"new", "fresh"}:
        reminder_list_message_ids.pop(chat_id, None)
        db_delete_list_msg_id(chat_id)
        
    await update_reminder_list(context, chat_id)
    async def _cleanup():
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(chat_id, cmd_mid)
        except Exception:
            pass
    asyncio.create_task(_cleanup())

def get_full_help_text():
    return (
        "<b>🧠 Welcome to the Reminder Bot!</b>\n\n"
        "This bot helps you manage your time, tasks, and routines efficiently through three main features:\n\n"
        "📌 <b>1. Reminders (Timed)</b>\n"
        "Send a message to set reminders like:\n"
        "• <code>10m take a break</code>\n"
        "• <code>1h30m attend meeting</code>\n"
        "• <code>today 14:00 call John</code>\n"
        "• <code>tomorrow 08:15 dentist appointment</code>\n"
        "• <code>22 June 19:30 mom’s birthday</code>\n"
        "⏰ You will get a notification with 'Complete' and 'Snooze' buttons.\n\n"

        "📅 <b>2. Daily Reminders</b>\n"
        "Send a message like:\n"
        "• <code>daily 07:00 morning workout</code>\n"
        "Daily reminders repeat at the same time every day.\n"
        "They come with a ✅ Done button to track completion.\n\n"

        "📝 <b>3. Notes</b>\n"
        "Toggle note-taking mode with:\n"
        "• <code>/notes</code>\n"
        "Then just send any message — it will be stored as a note.\n"
        "Notes are shown in the upcoming reminders list and can be marked complete.\n\n"

        "📋 <b>Viewing Your Tasks</b>\n"
        "Use the command:\n"
        "• <code>/reminders</code>\n"
        "To see your upcoming reminders, notes, and daily tasks.\n\n"

        "⚙️ <b>Managing Reminders</b>\n"
        "In the reminder list, tap:\n"
        "• ✏️ Edit — to change text or time\n"
        "• 🗑️ Delete Reminder — to remove tasks or notes\n\n"

        "🌍 <b>Time‑zone Support</b>\n"
        "To set or detect your time-zone:\n"
        "• <code>/timezone Europe/Bratislava</code>\n"
        "• <code>/dtz</code> – auto-detect by location (mobile only)\n\n"

        "🧽 <b>Extra Features</b>\n"
        "• <code>delete all</code> — remove everything\n"
        "• <code>delete [task]</code> — remove one by name\n"
        "• <code>time</code> — view current time\n\n"

        "📎 <b>Pinning</b>\n"
        "Use <code>/reminders</code> to pin or refresh the task list message in chat.\n\n"

        "Use the buttons below to hide or remove this help message."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        get_full_help_text(),
        parse_mode="HTML",
        reply_markup=get_help_keyboard("full")
    )
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

app = (
    Application.builder()
    .token(TOKEN)
    .post_init(restore_tasks_on_startup)
    .post_init(on_startup)
    .build()
)

app.add_handler(CommandHandler("start", start_command))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("timezone", timezone_command))
app.add_handler(CommandHandler("dtz", detect_timezone_command))
app.add_handler(CommandHandler("notes", notes_toggle_command))
app.add_handler(
    CommandHandler(["reminders", "list", "upcoming"], reminders_command)
)

app.add_handler(CallbackQueryHandler(mark_daily_done_handler, pattern=r"^daily_done\|"))
app.add_handler(CallbackQueryHandler(handle_removal_button, pattern=r"^remove_daily\|"))
app.add_handler(CallbackQueryHandler(handle_removal_button, pattern=r"^edit_daily\|"))
app.add_handler(CallbackQueryHandler(complete_note_handler, pattern=r"^complete_note\|"))
app.add_handler(CallbackQueryHandler(help_button_handler, pattern=r"^(collapse_help|uncollapse_help|delete_help)$"))
app.add_handler(CallbackQueryHandler(complete_reminder_handler, pattern=r"^complete\|"))
app.add_handler(CallbackQueryHandler(handle_removal_button, pattern=r"^(start_edit|edit_reminder\|.*|edit_note\|.*|cancel_edit)$"))
app.add_handler(CallbackQueryHandler(handle_removal_button, pattern=r"^(start_removal|remove_reminder\|.*|remove_note\|.*|confirm_delete\|.*|cancel_confirm|cancel_removal)$"))
app.add_handler(CallbackQueryHandler(snooze_reminder_handler, pattern=r"^snooze\|"))
app.add_handler(CallbackQueryHandler(delete_own_message_handler, pattern=r"^delmsg$"))
app.add_handler(MessageHandler(filters.Regex(r'^❌\s*Cancel$'), detect_cancel_handler))


app.add_handler(MessageHandler(filters.LOCATION, location_handler))
app.add_error_handler(error_handler)
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.COMMAND, unknown_command_handler))


print("Bot is running...")
app.run_polling()