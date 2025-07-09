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
detect_prompt_ids = {}   # chat_id → message_id of location‑prompt
reminders = {}  # chat_id: {message: (timestamp, task_or_id)}
reminder_list_message_ids = {}  # chat_id: message_id
removal_state = {}  # chat_id: {'mode': 'normal'|'confirm', 'target': str | None}

DEFAULT_TZ = ZoneInfo("Europe/Kyiv")

DB = sqlite3.connect("reminder_bot.db")
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
    now_ts = int(datetime.now(timezone.utc).timestamp())           # changed
    return DB.execute(
        "SELECT chat_id, text, fire_at FROM reminders WHERE fire_at>?",
        (now_ts,)
    ).fetchall()

def db_delete_all_reminders(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM reminders WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

async def detect_timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cmd_id = update.message.message_id

    # delete the command message after 5 seconds
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

    # auto-delete the prompt after 60 seconds if still there
    async def delete_prompt_later():
        await asyncio.sleep(60)
        mid = detect_prompt_ids.pop(chat_id, None)
        if mid:
            try:
                await context.bot.delete_message(chat_id, mid)
            except Exception:
                pass
    asyncio.create_task(delete_prompt_later())

# ─── 2️⃣  Helper DB‑wrapper functions (put near get_chat_tz / set_chat_tz) ───
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

# ─── 3️⃣  /notes toggle command ───
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

# ── UPDATED send_note helper  ───────────────────────────────────
async def send_note(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    # 1. Send temporary message with placeholder callback data
    tmp_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Complete", callback_data="noop")]]
    )
    sent = await context.bot.send_message(chat_id, text, reply_markup=tmp_kb)

    # 2. Store note and get its DB id
    cur = DB.cursor()
    cur.execute(
        "INSERT INTO notes(chat_id, note, message_id) VALUES (?,?,?)",
        (chat_id, text, sent.message_id)
    )
    note_id = cur.lastrowid
    DB.commit()

    # 3. Update the button with correct callback containing note_id
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Complete", callback_data=f"complete_note|{note_id}")]]
    )
    await context.bot.edit_message_reply_markup(
        chat_id=chat_id,
        message_id=sent.message_id,
        reply_markup=kb
    )

    # 4. Refresh the 📋 list
    await update_reminder_list(context, chat_id)


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
    await query.answer()  # acknowledge

    try:
        await context.bot.delete_message(chat_id=query.message.chat_id,
                                         message_id=query.message.message_id)
    except:
        pass  # message may already be gone


async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id    = update.effective_chat.id
    loc_msg_id = update.message.message_id

    # delete the location message after 2 s
    async def delete_location():
        await asyncio.sleep(2)
        try:
            await context.bot.delete_message(chat_id, loc_msg_id)
        except Exception:
            pass
    asyncio.create_task(delete_location())

    # delete the prompt, if it is still on screen
    prompt_id = detect_prompt_ids.pop(chat_id, None)
    if prompt_id:
        try:
            await context.bot.delete_message(chat_id, prompt_id)
        except Exception:
            pass

    # determine the time‑zone
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

    # send result and auto‑delete it after 5 s
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

    # 1️⃣  Delete the user’s “❌ Cancel” message immediately
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=cancel_msg_id)
    except Exception:
        pass  # it might already be gone

    # 2️⃣  Remove the location‑prompt, if it’s still on screen
    prompt_id = detect_prompt_ids.pop(chat_id, None)
    if prompt_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prompt_id)
        except Exception:
            pass

    # 3️⃣  (Optional) brief notice – auto‑deletes after 5 s
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

    # ⏳ Auto-delete user command after 5s
    async def delete_later():
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except:
            pass
    asyncio.create_task(delete_later())

    # 🧠 Main logic
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

    # Reply and delete both messages
    reply = await update.message.reply_text("I didn’t understand that. Try again")
    asyncio.create_task(delete_later(msg_id))               # delete user’s command
    asyncio.create_task(delete_later(reply.message_id))     # delete bot’s reply



async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    # ⏳ Auto-delete the user's /start message after 5 seconds
    async def delete_later():
        await asyncio.sleep(5)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except:
            pass
    asyncio.create_task(delete_later())

    # 👋 Send welcome message
    try:
        welcome = await update.message.reply_text(
            "👋 Hello, this is a reminder chat bot. Use /help to see how to use me. "
            "You can delete this message whenever you want.",
            reply_markup=delete_keyboard()
        )

        # Optional: auto-delete welcome message after 5 minutes (300s)
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
        # Silently ignore – the user blocked the bot
        context.application.logger.info("Message blocked – user has blocked the bot.")
    else:
        logging.exception("Unhandled exception", exc_info=context.error)

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



# ── UPDATED update_reminder_list  ───────────────────────────────
async def update_reminder_list(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """
    Builds/edits the single “📋 Upcoming Reminders” message and now keeps
    its message‑id in SQLite so we can edit the same message after a
    restart instead of creating a duplicate.
    """
    tz = get_chat_tz(chat_id)
    user_reminders = reminders.get(chat_id, {})
    user_notes     = fetch_notes(chat_id)

    # ── compose text ──────────────────────────────────────────────────
    if not user_reminders and not user_notes:
        text = "📋 <b>No upcoming reminders.</b>"
    else:
        lines = ["📋 <b>Upcoming Reminders:</b>"]
        for msg, (ts, _) in sorted(user_reminders.items(), key=lambda x: x[1][0]):
            tstr = datetime.fromtimestamp(ts, tz=tz).strftime("%d %b %H:%M")
            lines.append(f"• <b>{msg}</b> at <i>{tstr}</i>")
        for _, note_txt in user_notes:
            lines.append(f"• <b>{note_txt}</b>")
        text = "\n".join(lines)

    keyboard = get_removal_keyboard(chat_id)

    # ── decide whether to edit or send ────────────────────────────────
    mid = reminder_list_message_ids.get(chat_id)
    if mid is None:                      # not in RAM → try DB
        mid = db_get_list_msg_id(chat_id)
        if mid:
            reminder_list_message_ids[chat_id] = mid

    try:
        if mid:                          # try to edit existing list
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=mid,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:                            # create a brand‑new list
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            reminder_list_message_ids[chat_id] = msg.message_id
            db_set_list_msg_id(chat_id, msg.message_id)
    except Exception as e:
        # the stored message no longer exists → forget it and try once more
        if "message to edit not found" in str(e).lower():
            reminder_list_message_ids.pop(chat_id, None)
            db_delete_list_msg_id(chat_id)
            await update_reminder_list(context, chat_id)   # one retry








async def send_scheduled_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message: str,
    delay_seconds: int,
    *,
    store_in_db: bool = True
):
    fire_at = int(datetime.now(timezone.utc).timestamp()           # changed
                  + delay_seconds)

    if store_in_db:
        db_add_reminder(chat_id, message, fire_at)

    # async job ----------------------------------------------------------
    async def task_body():
        await asyncio.sleep(delay_seconds)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Complete", callback_data=f"complete|{message}"),
            InlineKeyboardButton("🔁 Snooze 5m", callback_data=f"snooze|{message}|300"),
        ]])

        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Reminder: {message}",
            reply_markup=keyboard,
        )

        # on delivery: replace stored task with message‑id,
        #               remove row from DB
        reminders[chat_id][message] = (fire_at, sent.message_id)
        db_delete_reminder(chat_id, message)
        await update_reminder_list(context, chat_id)

    # register and keep handle in RAM -----------------------------------
    task = asyncio.create_task(task_body())
    reminders.setdefault(chat_id, {})[message] = (fire_at, task)
    await update_reminder_list(context, chat_id)

    
    

async def snooze_reminder_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, message, snooze_sec = query.data.split("|")
    snooze_seconds = int(snooze_sec)
    chat_id = query.message.chat_id

    # remove current message + DB row
    try:
        await context.bot.delete_message(chat_id=chat_id,
                                         message_id=query.message.message_id)
    except Exception:
        pass
    db_delete_reminder(chat_id, message)

    # schedule fresh
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
            _, handle = reminders[chat_id][msg_to_remove]
            if isinstance(handle, asyncio.Task):
                handle.cancel()
            del reminders[chat_id][msg_to_remove]
        db_delete_reminder(chat_id, msg_to_remove)
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

    # relative words ───────────────────────────────────────────────
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

    # full “22 June 19:30 …” ────────────────────────────────────────
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
                pass  # try next fmt

    # partial “22 June 8 walk” (hour only) ─────────────────────────
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

    # plain “14:00 …” ──────────────────────────────────────────────
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
    text = update.message.text.lower()

    # helper : auto‑delete
    async def delete_later(mid, delay=5):
        await asyncio.sleep(delay)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    asyncio.create_task(delete_later(msg_id))

    # ---- delete all ----------------------------------------------------
    if text in {"delete all", "del all"}:
        for _, handle in reminders.get(chat_id, {}).values():
            if isinstance(handle, asyncio.Task):
                handle.cancel()
        reminders[chat_id] = {}
        db_delete_all_reminders(chat_id)          # ← NEW
        await update_reminder_list(context, chat_id)
        m = await context.bot.send_message(chat_id, "🗑️ All reminders deleted.")
        asyncio.create_task(delete_later(m.message_id))
        return

    # ---- delete single -------------------------------------------------
    if text.startswith(("delete ", "del ")):
        target = text.split(maxsplit=1)[1]
        if target in reminders.get(chat_id, {}):
            _, handle = reminders[chat_id][target]
            if isinstance(handle, asyncio.Task):
                handle.cancel()
            del reminders[chat_id][target]
            db_delete_reminder(chat_id, target)   # ← NEW
            await update_reminder_list(context, chat_id)
            m = await context.bot.send_message(chat_id, f"✅ Reminder “{target}” deleted.")
        else:
            m = await context.bot.send_message(chat_id, f"⚠️ No reminder named “{target}”.")
        asyncio.create_task(delete_later(m.message_id))
        return

    # ---- current time --------------------------------------------------
    if "time" in text:
        now = datetime.now(tz)
        m = await context.bot.send_message(chat_id, f"Current time: {now:%H:%M:%S}")
        asyncio.create_task(delete_later(m.message_id))
        return

    # ---- natural‑language scheduling -----------------------------------
    delay_dt, msg_dt = parse_datetime_message(text, tz)
    if delay_dt == -1:
        m = await context.bot.send_message(chat_id, "⏰ This time has already passed.")
        asyncio.create_task(delete_later(m.message_id))
        return
    if delay_dt is not None:
        m = await context.bot.send_message(chat_id, "⏳ Reminder scheduled!")
        asyncio.create_task(delete_later(m.message_id))
        asyncio.create_task(
            send_scheduled_message(context, chat_id, msg_dt, int(delay_dt))
        )
        return

    # ---- “10m feed cat” etc. ------------------------------------------
    delay_s, msg_rel = parse_time_prefix(text)
    if delay_s:
        mins, secs = divmod(delay_s, 60)
        delay_str = f"{mins} minutes" + (f" {secs} seconds" if secs else "")
        m = await context.bot.send_message(chat_id, f"⏳ Reminder set in {delay_str}!")
        asyncio.create_task(delete_later(m.message_id))
        asyncio.create_task(
            send_scheduled_message(context, chat_id, msg_rel, delay_s)
        )
        return

    # ---- notes mode fallback or unknown -------------------------------
    if notes_enabled(chat_id):
        await send_note(context, chat_id, update.message.text)
    else:
        m = await context.bot.send_message(chat_id, "I didn’t understand that. Try again.")
        asyncio.create_task(delete_later(m.message_id))

async def restore_tasks_on_startup(app: Application):
    """
    1.  Load saved list‑message IDs so we can edit (not duplicate) them.
    2.  Recreate asyncio tasks for every future reminder row.
    3.  Refresh the list once per chat.
    """
    ctx = ContextTypes.DEFAULT_TYPE(application=app)

    # ① list‑message IDs ------------------------------------------------
    for chat_id, mid in DB.execute("SELECT chat_id, list_msg_id FROM reminder_meta"):
        reminder_list_message_ids[chat_id] = mid

    # ② schedule pending reminders -------------------------------------
    chats_touched: set[int] = set()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    for chat_id, text, fire_at in db_fetch_future():
        delay = fire_at - now_ts
        if delay <= 0:
            continue
        await send_scheduled_message(ctx, chat_id, text, delay, store_in_db=False)
        chats_touched.add(chat_id)

    # ③ make sure each chat has an up‑to‑date list ----------------------
    for chat_id in chats_touched:
        await update_reminder_list(ctx, chat_id)


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



app = (
    Application.builder()
    .token(TOKEN)
    .post_init(restore_tasks_on_startup)   # ← attach here
    .build()
)

app.add_handler(CommandHandler("start", start_command))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("timezone", timezone_command))
app.add_handler(CommandHandler("dtz", detect_timezone_command))
app.add_handler(CommandHandler("notes", notes_toggle_command))

app.add_handler(CallbackQueryHandler(complete_note_handler, pattern=r"^complete_note\|"))
app.add_handler(CallbackQueryHandler(help_button_handler, pattern=r"^(collapse_help|uncollapse_help|delete_help)$"))
app.add_handler(CallbackQueryHandler(complete_reminder_handler, pattern=r"^complete\|"))
app.add_handler(CallbackQueryHandler(handle_removal_button, pattern=r"^(start_removal|remove_reminder\|.*|confirm_delete\|.*|cancel_confirm|cancel_removal)$"))
app.add_handler(CallbackQueryHandler(snooze_reminder_handler, pattern=r"^snooze\|"))
app.add_handler(CallbackQueryHandler(delete_own_message_handler, pattern=r"^delmsg$"))
app.add_handler(MessageHandler(filters.Regex(r'^❌\s*Cancel$'), detect_cancel_handler))


app.add_handler(MessageHandler(filters.LOCATION, location_handler))
app.add_error_handler(error_handler)
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.COMMAND, unknown_command_handler))


print("Bot is running...")
app.run_polling()