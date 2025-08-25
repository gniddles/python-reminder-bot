from datetime import datetime, timedelta, timezone
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
import telegram
from telegram.error import Forbidden
from telegram.ext import Application, MessageHandler, ContextTypes, filters, CommandHandler, CallbackQueryHandler
import asyncio
import re
import logging
import sqlite3
from timezonefinder import TimezoneFinder
from zoneinfo import ZoneInfo, available_timezones
import math

logging.basicConfig(level=logging.INFO)
TOKEN = "1014634066:AAGTFzlrmJQ7KSM4Bh98o2050IqiL508w5g"

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
daily_sent_today: set[tuple[int, int]] = set()
datetime.now(timezone.utc)
detect_prompt_ids = {}
reminders = {}
reminder_list_message_ids = {}
removal_state = {}
editing_state = {}

DEFAULT_TZ = ZoneInfo("Europe/Kyiv")

DB = sqlite3.connect("reminder_bot_copy.db")
# Patch to ensure 'created_at' column exists (compatible with SQLite)
def ensure_created_at_column():
    try:
        DB.execute("SELECT days FROM daily_reminders LIMIT 1")
    except sqlite3.OperationalError:
        DB.execute("ALTER TABLE daily_reminders ADD COLUMN days TEXT DEFAULT '0,1,2,3,4,5,6'")

        DB.execute("""
            CREATE TABLE daily_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                time TEXT NOT NULL,
                text TEXT NOT NULL,
                last_done_date TEXT DEFAULT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        DB.execute("""
            INSERT INTO daily_reminders (id, chat_id, time, text, last_done_date, created_at)
            SELECT id, chat_id, time, text, last_done_date, datetime('now') FROM daily_reminders_old
        """)
        DB.execute("DROP TABLE daily_reminders_old")
        DB.commit()

ensure_created_at_column()
DB.execute("""
CREATE TABLE IF NOT EXISTS reminders (
    chat_id   INTEGER NOT NULL,
    text      TEXT    NOT NULL,
    fire_at   INTEGER NOT NULL,
    PRIMARY KEY (chat_id, text)
)
""")
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
CREATE TABLE IF NOT EXISTS user_daily_display (
    chat_id INTEGER PRIMARY KEY,
    hide_inactive INTEGER NOT NULL DEFAULT 0
)
""")
DB.commit()
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
DB.execute("""
CREATE TABLE IF NOT EXISTS daily_reminder_messages (
    daily_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (daily_id, chat_id)
)
""")
try:
    DB.execute("SELECT days FROM daily_reminders LIMIT 1")
except sqlite3.OperationalError:
    DB.execute("ALTER TABLE daily_reminders ADD COLUMN days TEXT DEFAULT '0,1,2,3,4,5,6'")
    DB.commit()

tf = TimezoneFinder()

def get_daily_display_mode(chat_id: int) -> bool:
    row = DB.execute("SELECT hide_inactive FROM user_daily_display WHERE chat_id=?", (chat_id,)).fetchone()
    return bool(row[0]) if row else False

def set_daily_display_mode(chat_id: int, hide: bool):
    DB.execute("INSERT OR REPLACE INTO user_daily_display(chat_id, hide_inactive) VALUES (?,?)", (chat_id, int(hide)))
    DB.commit()

async def daily_display_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cmd_mid = update.message.message_id

    # delete user command after 1s
    async def delete_cmd():
        await asyncio.sleep(1)
        try:
            await context.bot.delete_message(chat_id, cmd_mid)
        except:
            pass
    asyncio.create_task(delete_cmd())

    if not context.args:
        reply = await update.message.reply_text("Usage: /daily show OR /daily hide")
        return

    subcmd = context.args[0].lower()
    if subcmd == "hide":
        set_daily_display_mode(chat_id, True)
        reply = await update.message.reply_text("🔒 Inactive daily reminders will be hidden.")
    elif subcmd == "show":
        set_daily_display_mode(chat_id, False)
        reply = await update.message.reply_text("📖 All daily reminders will be shown.")
    else:
        reply = await update.message.reply_text("Usage: /daily show OR /daily hide")

    # delete bot reply after 3s
    async def delete_reply():
        await asyncio.sleep(3)
        try:
            await context.bot.delete_message(chat_id, reply.message_id)
        except:
            pass
    asyncio.create_task(delete_reply())

    await update_reminder_list(context, chat_id)

async def send_days_keyboard(bot, chat_id, daily_id, selected_days):
    """Send a keyboard allowing the user to toggle days for a daily reminder."""
    buttons = []
    for i, name in enumerate(DAY_NAMES):
        symbol = "✅" if i in selected_days else "❌"
        buttons.append(InlineKeyboardButton(f"{symbol} {name}", callback_data=f"toggle_day|{daily_id}|{i}"))
    rows = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
    rows.append([
        InlineKeyboardButton("💾 Save", callback_data=f"save_days|{daily_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")
    ])
    await bot.send_message(chat_id, "Select days for this reminder:", reply_markup=InlineKeyboardMarkup(rows))

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

        # Remove stored reference from daily_reminder_messages
        DB.execute("""
            DELETE FROM daily_reminder_messages
            WHERE daily_id=? AND chat_id=?
        """, (daily_id, chat_id))
        DB.commit()

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

last_checked_date = None

async def daily_reminder_loop(app: Application):
    """
    Loop that wakes at minute boundaries and sends pending daily reminders.
    Respects the `days` column (CSV of 0..6 where 0 = Monday).
    """
    global last_checked_date, daily_sent_today
    logging.info("🕒 Daily reminder loop started")

    # Align to the next minute boundary
    now_utc = datetime.now(timezone.utc)
    next_minute = (now_utc.replace(second=0, microsecond=0) + timedelta(minutes=1))
    await asyncio.sleep((next_minute - now_utc).total_seconds())

    while True:
        now_utc = datetime.now(timezone.utc)
        next_minute = (now_utc.replace(second=0, microsecond=0) + timedelta(minutes=1))
        sleep_seconds = (next_minute - now_utc).total_seconds()

        try:
            for row in DB.execute("SELECT DISTINCT chat_id FROM daily_reminders"):
                chat_id = row[0]
                tz = get_chat_tz(chat_id)
                now_local = now_utc.astimezone(tz)
                now_hour = now_local.hour
                now_minute = now_local.minute
                today_str = now_local.strftime("%Y-%m-%d")

                # Daily housekeeping once per day
                if last_checked_date != today_str:
                    last_checked_date = today_str
                    daily_sent_today.clear()

                    for daily_id, _, _, last_done in fetch_daily_reminders(chat_id):
                        row_msg = DB.execute(
                            "SELECT message_id FROM daily_reminder_messages WHERE daily_id=? AND chat_id=?",
                            (daily_id, chat_id)
                        ).fetchone()

                        if row_msg:
                            msg_id = row_msg[0]
                            # delete yesterday's message if not marked done for today
                            if not last_done or last_done != today_str:
                                try:
                                    await app.bot.delete_message(chat_id, msg_id)
                                except Exception as e:
                                    logging.debug(f"Could not delete old daily reminder message {msg_id}: {e}")

                            DB.execute(
                                "DELETE FROM daily_reminder_messages WHERE daily_id=? AND chat_id=?",
                                (daily_id, chat_id)
                            )

                        # Clear last_done_date if it belonged to a prior day
                        if last_done and last_done != today_str:
                            DB.execute(
                                "UPDATE daily_reminders SET last_done_date=NULL WHERE id=? AND chat_id=?",
                                (daily_id, chat_id)
                            )
                    DB.commit()
                    # refresh list once per chat after housekeeping
                    await update_reminder_list(app, chat_id)

                # Now evaluate which daily reminders should be sent this minute
                for daily_id, time_str, text, last_done in fetch_daily_reminders(chat_id):
                    # Normalize last_done if needed
                    if last_done and last_done != today_str:
                        DB.execute("UPDATE daily_reminders SET last_done_date=NULL WHERE id=? AND chat_id=?",
                                   (daily_id, chat_id))
                        DB.commit()
                        last_done = None

                    try:
                        target_hour, target_minute = map(int, time_str.split(":"))
                    except Exception:
                        logging.warning(f"Malformed daily time for id={daily_id}: {time_str}")
                        continue

                    # Check allowed days (default to all weekdays if column missing/empty)
                    row_days = DB.execute("SELECT days FROM daily_reminders WHERE id=? AND chat_id=?", (daily_id, chat_id)).fetchone()
                    if row_days and row_days[0]:
                        try:
                            allowed_days = set(int(x) for x in row_days[0].split(",") if x.strip() != "")
                        except Exception:
                            allowed_days = set(range(7))
                    else:
                        allowed_days = set(range(7))

                    if now_local.weekday() not in allowed_days:
                        # today is not selected for this daily reminder
                        continue

                    already_sent = (chat_id, daily_id) in daily_sent_today
                    row_msg = DB.execute(
                        "SELECT message_id FROM daily_reminder_messages WHERE daily_id=? AND chat_id=?",
                        (daily_id, chat_id)
                    ).fetchone()
                    db_has_message = bool(row_msg)

                    if (now_hour == target_hour and now_minute == target_minute) and (last_done != today_str) and (not already_sent) and (not db_has_message):
                        try:
                            keyboard = InlineKeyboardMarkup([
                                [InlineKeyboardButton("✅ Done", callback_data=f"daily_done|{daily_id}")]
                            ])
                            sent = await app.bot.send_message(
                                chat_id=chat_id,
                                text=f"📅 Daily Reminder: {text}",
                                reply_markup=keyboard
                            )

                            DB.execute("""
                                INSERT OR REPLACE INTO daily_reminder_messages (daily_id, chat_id, message_id)
                                VALUES (?,?,?)
                            """, (daily_id, chat_id, sent.message_id))
                            DB.commit()

                            daily_sent_today.add((chat_id, daily_id))
                            logging.info(f"Sent daily reminder id={daily_id} to chat={chat_id} at {time_str}")

                        except Exception as e:
                            logging.warning(f"Failed to send daily reminder id={daily_id} to {chat_id}: {e}")

        except Exception as e:
            logging.exception("Error in daily_reminder_loop iteration", exc_info=e)

        # sleep until the next minute boundary
        await asyncio.sleep(sleep_seconds)


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
                icon = "🗓️"
                label = f"{icon} {msg}" if state["mode"] == "removal" else f"{icon} {msg}"
                cb = f"remove_daily|{daily_id}" if state["mode"] == "removal" else f"edit_daily|{daily_id}"
                buttons.append([InlineKeyboardButton(label, callback_data=cb)])

            for msg in user_reminders.keys():
                icon = "⏰"
                label = f"{icon} {msg}" if state["mode"] == "removal" else f"{icon} {msg}"
                cb = f"remove_reminder|{msg}" if state["mode"] == "removal" else f"edit_reminder|{msg}"
                buttons.append([InlineKeyboardButton(label, callback_data=cb)])

            for note_id, note in user_notes:
                icon = "📝"
                label = f"{icon} {note}" if state["mode"] == "removal" else f"{icon} {note}"
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
    """
    Render the single upcoming-reminders message. For timed reminders we now
    convert the stored UTC `fire_at` -> UTC-aware datetime -> user's tz to
    guarantee correct minute formatting.
    """
    tz = get_chat_tz(chat_id)
    user_reminders = reminders.get(chat_id, {})
    user_notes = fetch_notes(chat_id)
    daily_reminders = fetch_daily_reminders(chat_id)

    today_str = datetime.now(tz).strftime('%Y-%m-%d')

    lines = []
    hide_inactive = get_daily_display_mode(chat_id)
    weekday_today = datetime.now(tz).weekday()

    if daily_reminders:
        lines.append("🗓️ <b>Daily Reminders:</b>")
        for daily_id, time_str, msg, last_done in daily_reminders:
            row_days = DB.execute("SELECT days FROM daily_reminders WHERE id=? AND chat_id=?", (daily_id, chat_id)).fetchone()
            allowed_days = set(int(x) for x in row_days[0].split(",")) if row_days and row_days[0] else set(range(7))

            if hide_inactive and weekday_today not in allowed_days:
                continue  # skip showing this reminder
            try:
                h, m = map(int, time_str.split(":"))
                local_dt = datetime.now(tz).replace(hour=h, minute=m, second=0, microsecond=0)
                display_time = local_dt.strftime("%H:%M")
            except Exception:
                display_time = time_str
            status = "✅ Done" if last_done == today_str else ""
            lines.append(f"• <b>{msg}</b> at <i>{display_time}</i> {status}")

    if user_reminders:
        lines.append("\n⏰ <b>Timed Reminders:</b>")
        # sort by stored timestamp
        for msg, (ts, _) in sorted(user_reminders.items(), key=lambda x: x[1][0]):
            try:
                # Interpret stored ts as UTC, then convert to user's tz
                send_dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz)
                tstr = send_dt.strftime("%d %b %H:%M")
            except Exception:
                # fallback
                tstr = datetime.fromtimestamp(ts).strftime("%d %b %H:%M")
            lines.append(f"• <b>{msg}</b> at <i>{tstr}</i>")

    if user_notes:
        lines.append("\n📝 <b>Notes:</b>")
        for _, note_txt in user_notes:
            lines.append(f"• <b>{note_txt}</b>")

    if not lines:
        text = "<b>REMINDER BOT</B>\n" + "\n📋 <b>No upcoming reminders.</b>"
    else:
        text = "<b>REMINDER BOT</b>\n" + "\n".join(lines)

    # choose keyboard state
    if not (user_reminders or user_notes or daily_reminders):
        removal_state.pop(chat_id, None)
        keyboard = None
    else:
        keyboard = get_removal_keyboard(chat_id)

    mid = reminder_list_message_ids.get(chat_id)
    if mid is None:
        mid = db_get_list_msg_id(chat_id)
        if mid:
            reminder_list_message_ids[chat_id] = mid

    try:
        if mid:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=mid,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            except telegram.error.BadRequest as e:
                if "Message is not modified" in str(e):
                    # Ignore harmless "not modified" error
                    pass
                else:
                    raise
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
        else:
            logging.exception("Failed to update reminder list", exc_info=e)



async def send_scheduled_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message: str,
    delay_seconds: float,
    *,
    store_in_db: bool = True,
    fire_at: int | None = None
):
    """
    Schedule a reminder to be sent at a specific future UTC timestamp (`fire_at`)
    or after `delay_seconds` seconds. This function:
      - computes a precise UTC fire timestamp (ceiled to avoid showing an earlier minute),
      - stores it in DB only if store_in_db is True and fire_at was not provided,
      - schedules an asyncio.Task that sleeps until that exact UTC second and then sends.
    Accepts `fire_at` (int seconds since epoch) to support restoring from DB without
    recomputing the timestamp.
    """
    # compute (or normalize) the UTC fire timestamp
    now_utc = datetime.now(timezone.utc)
    if fire_at is None:
        fire_time = now_utc + timedelta(seconds=delay_seconds)
        fire_at_ts = int(math.ceil(fire_time.timestamp()))
    else:
        fire_at_ts = int(fire_at)

    # persist in DB only when asked and when we computed the timestamp here
    if store_in_db and fire_at is None:
        db_add_reminder(chat_id, message, fire_at_ts)

    async def task_body():
        try:
            # compute remaining time until the saved fire_at (float precision)
            remaining = fire_at_ts - datetime.now(timezone.utc).timestamp()
            if remaining > 0:
                await asyncio.sleep(remaining)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Complete", callback_data=f"complete|{message}"),
                 InlineKeyboardButton("🔁 Snooze 5m", callback_data=f"snooze|{message}|300")]
            ])

            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ Reminder: {message}",
                reply_markup=keyboard,
            )

            # replace task handle with actual sent message id
            reminders.setdefault(chat_id, {})[message] = (fire_at_ts, sent.message_id)

            # cleanup DB entry only if we wrote it earlier
            if store_in_db:
                db_delete_reminder(chat_id, message)

            await update_reminder_list(context, chat_id)

        except asyncio.CancelledError:
            # task was cancelled before sending — leave DB entry as-is
            return
        except Exception as e:
            logging.exception(f"❌ Failed to deliver scheduled reminder `{message}` to {chat_id}: {e}")

    # create the task and store it in-memory
    task = asyncio.create_task(task_body())
    reminders.setdefault(chat_id, {})[message] = (fire_at_ts, task)

    # refresh the list display
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
    """
    Handles inline callbacks for editing/removing reminders/notes/dailies.
    - Shows "Edit text | Edit days" under the main reminders message.
    - Day selector toggles days in-place.
    - After saving/cancelling, returns to the main reminders list (clears edit mode).
    """
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    def _get_list_msg_id():
        mid = reminder_list_message_ids.get(chat_id)
        if not mid:
            mid = db_get_list_msg_id(chat_id)
        return mid

    def _build_days_markup(daily_id: int, selected_days: set[int]) -> InlineKeyboardMarkup:
        buttons = []
        for i, name in enumerate(DAY_NAMES):
            symbol = "✅" if i in selected_days else "❌"
            buttons.append(InlineKeyboardButton(f"{symbol} {name}", callback_data=f"toggle_day|{daily_id}|{i}"))
        rows = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
        rows.append([
            InlineKeyboardButton("💾 Save", callback_data=f"save_days|{daily_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")
        ])
        return InlineKeyboardMarkup(rows)

    # ---------- start edit / start removal ----------
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

    # ---------- removal confirmations ----------
    if query.data.startswith("remove_reminder|"):
        _, msg_to_remove = query.data.split("|", 1)
        removal_state[chat_id] = {"mode": "confirm", "target": msg_to_remove, "type": "reminder"}
        await update_reminder_list(context, chat_id)
        return

    if query.data.startswith("remove_note|"):
        _, note_id = query.data.split("|", 1)
        removal_state[chat_id] = {"mode": "confirm", "target": int(note_id), "type": "note"}
        await update_reminder_list(context, chat_id)
        return

    if query.data.startswith("remove_daily|"):
        _, daily_id = query.data.split("|", 1)
        removal_state[chat_id] = {"mode": "confirm", "target": int(daily_id), "type": "daily"}
        await update_reminder_list(context, chat_id)
        return

    # ---------- show edit submenu under main message ----------
    if query.data.startswith("edit_daily|"):
        _, daily_id = query.data.split("|", 1)
        daily_id = int(daily_id)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✏️ Edit text", callback_data=f"edit_daily_text|{daily_id}"),
                InlineKeyboardButton("📅 Edit days", callback_data=f"edit_daily_days|{daily_id}")
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")]
        ])
        mid = _get_list_msg_id()
        if mid:
            try:
                await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=mid, reply_markup=keyboard)
            except Exception:
                await context.bot.send_message(chat_id, "What would you like to edit?", reply_markup=keyboard)
        else:
            await context.bot.send_message(chat_id, "What would you like to edit?", reply_markup=keyboard)
        return

    # ---------- edit text flow (prompt user) ----------
    if query.data.startswith("edit_daily_text|"):
        _, daily_id = query.data.split("|", 1)
        editing_state[chat_id] = {"type": "daily", "daily_id": int(daily_id)}
        msg = await context.bot.send_message(
            chat_id,
            "✏️ Send me the edited version of your daily reminder.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")]])
        )
        editing_state[chat_id]["prompt_msg_id"] = msg.message_id
        return

    # ---------- edit days flow ----------
    if query.data.startswith("edit_daily_days|"):
        _, daily_id = query.data.split("|", 1)
        daily_id = int(daily_id)
        row = DB.execute("SELECT days FROM daily_reminders WHERE id=? AND chat_id=?", (daily_id, chat_id)).fetchone()
        if row and row[0]:
            try:
                current_days = set(int(x) for x in row[0].split(",") if x.strip() != "")
            except Exception:
                current_days = set(range(7))
        else:
            current_days = set(range(7))

        editing_state[chat_id] = {"type": "daily_days", "daily_id": daily_id, "days": current_days}
        days_markup = _build_days_markup(daily_id, current_days)
        mid = _get_list_msg_id()
        if mid:
            try:
                await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=mid, reply_markup=days_markup)
            except Exception:
                await context.bot.send_message(chat_id, "Select days for this reminder:", reply_markup=days_markup)
        else:
            await context.bot.send_message(chat_id, "Select days for this reminder:", reply_markup=days_markup)
        return

    # ---------- toggle days ----------
    if query.data.startswith("toggle_day|"):
        _, daily_id_s, day_idx_s = query.data.split("|", 2)
        daily_id = int(daily_id_s); day_idx = int(day_idx_s)
        state = editing_state.get(chat_id)
        if state and state.get("type") == "daily_days" and state.get("daily_id") == daily_id:
            if day_idx in state["days"]:
                state["days"].remove(day_idx)
            else:
                state["days"].add(day_idx)
            days_markup = _build_days_markup(daily_id, state["days"])
            mid = _get_list_msg_id()
            if mid:
                try:
                    await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=mid, reply_markup=days_markup)
                except Exception:
                    pass
        return

    # ---------- save days ----------
    if query.data.startswith("save_days|"):
        _, daily_id_s = query.data.split("|", 1)
        daily_id = int(daily_id_s)
        state = editing_state.pop(chat_id, None)
        if state and state.get("type") == "daily_days" and state.get("daily_id") == daily_id:
            days_str = ",".join(map(str, sorted(state["days"])))
            DB.execute("UPDATE daily_reminders SET days=? WHERE id=? AND chat_id=?", (days_str, daily_id, chat_id))
            DB.commit()
            removal_state.pop(chat_id, None)
            await update_reminder_list(context, chat_id)
        else:
            removal_state.pop(chat_id, None)
            await update_reminder_list(context, chat_id)
        return


    # ---------- cancel edit ----------
    if query.data == "cancel_edit":
        # remove any editing state
        state = editing_state.pop(chat_id, None)
        if state:
            # if there was a prompt message, delete it too
            prompt_id = state.get("prompt_msg_id")
            if prompt_id:
                try:
                    await context.bot.delete_message(chat_id, prompt_id)
                except Exception:
                    pass

        removal_state.pop(chat_id, None)
        await update_reminder_list(context, chat_id)
        return


    # ---------- confirm delete ----------
    if query.data.startswith("confirm_delete|"):
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
            row = DB.execute("SELECT message_id FROM notes WHERE id=? AND chat_id=?", (note_id, chat_id)).fetchone()
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
        return

    # ---------- cancel confirm ----------
    if query.data == "cancel_confirm":
        if chat_id in removal_state and removal_state[chat_id]["mode"] == "confirm":
            removal_state[chat_id] = {"mode": "removal", "target": None}
            await update_reminder_list(context, chat_id)
        return

    # ---------- cancel removal ----------
    if query.data == "cancel_removal":
        removal_state.pop(chat_id, None)
        await update_reminder_list(context, chat_id)
        return

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

    # delete user message after 1 second
    async def delete_later(mid, delay=1):
        await asyncio.sleep(delay)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except:
            pass
    asyncio.create_task(delete_later(msg_id))

    # --- Handle editing state first ---
    state = editing_state.get(chat_id)
    if state:
        prompt_id = state.get("prompt_msg_id")
        if prompt_id:
            try:
                await context.bot.delete_message(chat_id, prompt_id)
            except:
                pass

        if state["type"] == "reminder":
            original = state["original"]
            if chat_id in reminders and original in reminders[chat_id]:
                fire_at, handle = reminders[chat_id][original]
                if isinstance(handle, asyncio.Task):
                    try:
                        handle.cancel()
                    except:
                        pass
                try:
                    await send_scheduled_message(
                        context,
                        chat_id,
                        text,
                        fire_at - int(datetime.now(timezone.utc).timestamp())
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

        elif state["type"] == "daily":
            daily_id = state["daily_id"]
            DB.execute("UPDATE daily_reminders SET text=? WHERE id=? AND chat_id=?", (text, daily_id, chat_id))
            DB.commit()
            try:
                row = DB.execute(
                    "SELECT message_id FROM daily_reminder_messages WHERE daily_id=? AND chat_id=?",
                    (daily_id, chat_id)
                ).fetchone()
                if row:
                    live_mid = row[0]
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Done", callback_data=f"daily_done|{daily_id}")]
                    ])
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=live_mid,
                            text=f"📅 Daily Reminder: {text}",
                            reply_markup=keyboard
                        )
                    except telegram.error.BadRequest as e:
                        if "message to edit not found" in str(e).lower():
                            DB.execute(
                                "DELETE FROM daily_reminder_messages WHERE daily_id=? AND chat_id=?",
                                (daily_id, chat_id)
                            )
                            DB.commit()
                        else:
                            logging.warning(f"Could not update live daily reminder message: {e}")
            except Exception as e:
                logging.debug(f"Skipping live daily message update: {e}")
            removal_state.pop(chat_id, None)
            await update_reminder_list(context, chat_id)
            try:
                m = await context.bot.send_message(chat_id, "✅ Text updated.")
                asyncio.create_task(delete_later(m.message_id, delay=3))
            except Exception:
                pass
            return

        elif state["type"] == "note":
            note_id = state["note_id"]
            DB.execute("UPDATE notes SET text=? WHERE id=? AND chat_id=?", (text, note_id, chat_id))
            DB.commit()
            removal_state.pop(chat_id, None)
            await update_reminder_list(context, chat_id)
            try:
                m = await context.bot.send_message(chat_id, "✅ Note updated.")
                asyncio.create_task(delete_later(m.message_id, delay=3))
            except Exception:
                pass
            return

    # --- Compact reminders like "10m coffee", "1h30m gym" ---
    seconds, message = parse_time_prefix(text)
    if seconds and message:
        await send_scheduled_message(context, chat_id, message, seconds)
        return

    # --- Create daily reminder: "daily 07:00 workout" ---
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

    # --- Full delete all ---
    if text.strip().lower() in ("del all", "delete all"):
        # --- Delete all note messages ---
        rows = DB.execute(
            "SELECT message_id FROM notes WHERE chat_id=?",
            (chat_id,)
        ).fetchall()
        for (mid,) in rows:
            if mid:
                try:
                    await context.bot.delete_message(chat_id, mid)
                except Exception:
                    pass

        # --- Clear DB ---
        DB.execute("DELETE FROM notes WHERE chat_id=?", (chat_id,))
        DB.execute("DELETE FROM daily_reminders WHERE chat_id=?", (chat_id,))
        DB.commit()
        reminders.pop(chat_id, None)

        # --- Refresh reminder list ---
        await update_reminder_list(context, chat_id)
        return

    
        # --- Delete by prefix: "del <text>" / "delete <text>" ---
    m = re.match(r'^(?:del|delete)\s+(.+)$', text.strip(), re.IGNORECASE)
    if m:
        query = m.group(1).strip().lower()
        deleted_label = None
        deleted_text = None

        # 1) Try timed reminders (in-memory + DB cleanup)
        user_rems = reminders.get(chat_id, {})
        for key in list(user_rems.keys()):
            if key.lower().startswith(query):
                fire_at, handle = user_rems[key]
                # cancel scheduled task or delete delivered message
                try:
                    if isinstance(handle, asyncio.Task):
                        try:
                            handle.cancel()
                        except Exception:
                            pass
                        try:
                            db_delete_reminder(chat_id, key)
                        except Exception:
                            pass
                    elif isinstance(handle, int):
                        try:
                            await context.bot.delete_message(chat_id, handle)
                        except Exception:
                            pass
                finally:
                    reminders[chat_id].pop(key, None)
                deleted_label, deleted_text = "reminder", key
                break

        # 2) Try notes
        if not deleted_label:
            rows = DB.execute(
                "SELECT id, note, message_id FROM notes WHERE chat_id=? ORDER BY id",
                (chat_id,)
            ).fetchall()
            for nid, note_txt, mid in rows:
                if note_txt.lower().startswith(query):
                    if mid:
                        try:
                            await context.bot.delete_message(chat_id, mid)
                        except Exception:
                            pass
                    delete_note(chat_id, nid)
                    deleted_label, deleted_text = "note", note_txt
                    break

        # 3) Try daily reminders
        if not deleted_label:
            rows = DB.execute(
                "SELECT id, text FROM daily_reminders WHERE chat_id=?",
                (chat_id,)
            ).fetchall()
            for did, txt in rows:
                if txt.lower().startswith(query):
                    delete_daily_reminder_by_id(chat_id, did)
                    deleted_label, deleted_text = "daily", txt
                    break

        if deleted_label:
            await update_reminder_list(context, chat_id)
            mresp = await context.bot.send_message(
                chat_id, f"🗑️ Deleted {deleted_label}: {deleted_text}"
            )
            asyncio.create_task(delete_later(mresp.message_id, delay=3))
        else:
            mresp = await context.bot.send_message(chat_id, "⚠️ Nothing matched to delete.")
            asyncio.create_task(delete_later(mresp.message_id, delay=3))
        return


    # --- Explicit timed reminders: "in 10m tea", "at 18:45 dinner", "tomorrow 09:00 call" ---
    m = re.match(r"^\s*(in\s+\d+\s*[smhd]|at\s+\d{1,2}:\d{2}|tomorrow\s+\d{1,2}:\d{2})\s+(.+)", text, re.IGNORECASE)
    if m:
        when, message = m.groups()
        fire_at_ts = parse_time_to_timestamp(when, tz) # type: ignore
        if fire_at_ts is None:
            m = await context.bot.send_message(chat_id, "⚠️ I couldn’t parse the time. Try: 10m task, in 10m task, at 18:45, tomorrow 09:00")
            asyncio.create_task(delete_later(m.message_id, delay=3))
            return
        task = asyncio.create_task(task_body(context, chat_id, message, fire_at_ts)) # type: ignore
        reminders.setdefault(chat_id, {})[message] = (fire_at_ts, task)
        db_store_reminder(chat_id, message, fire_at_ts) # type: ignore
        await update_reminder_list(context, chat_id)
        return

    # --- Notes mode ---
    if notes_enabled(chat_id):
        lines = [line.strip() for line in update.message.text.splitlines() if line.strip()]
        for line in lines:
            await send_note(context, chat_id, line)
    else:
        m = await context.bot.send_message(chat_id, "I didn’t understand that. Try again.")
        asyncio.create_task(delete_later(m.message_id, delay=3))


async def on_startup(app: Application):
    await restore_tasks_on_startup(app)
    asyncio.create_task(daily_reminder_loop(app))  # ← Add this line


from types import SimpleNamespace

async def restore_tasks_on_startup(app: Application):
    """
    Re-create asyncio tasks for future reminders from DB (without changing DB).
    This preserves the original DB-stored `fire_at` and passes it into
    send_scheduled_message so we don't recompute different timestamps.
    """
    # Load list message IDs into memory
    for chat_id, mid in DB.execute("SELECT chat_id, list_msg_id FROM reminder_meta"):
        reminder_list_message_ids[chat_id] = mid

    now_ts = int(datetime.now(timezone.utc).timestamp())
    chats_touched: set[int] = set()

    # Fetch future reminders from DB (fire_at stored as epoch seconds)
    for chat_id, text, fire_at in db_fetch_future():
        delay = fire_at - now_ts
        if delay <= 0:
            # skip already-due (or overdue) reminders
            continue

        # Build a minimal context with a bot reference for send_scheduled_message
        context = SimpleNamespace()
        context.bot = app.bot
        context.application = app

        # Pass the original fire_at so send_scheduled_message will schedule
        # precisely for that timestamp (and won't overwrite DB).
        await send_scheduled_message(context, chat_id, text, delay, store_in_db=False, fire_at=fire_at)
        chats_touched.add(chat_id)

    # Update reminder list for each chat that had reminders restored
    for chat_id in chats_touched:
        # use the same simple context object
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
app.add_handler(CommandHandler("daily", daily_display_command))
app.add_handler(
    CommandHandler(["reminders", "list", "upcoming"], reminders_command)
)

app.add_handler(CallbackQueryHandler(handle_removal_button, pattern=r"^edit_daily\|"))
app.add_handler(CallbackQueryHandler(handle_removal_button, pattern=r"^edit_daily_text\|"))
app.add_handler(CallbackQueryHandler(handle_removal_button, pattern=r"^edit_daily_days\|"))
app.add_handler(CallbackQueryHandler(handle_removal_button, pattern=r"^toggle_day\|"))
app.add_handler(CallbackQueryHandler(handle_removal_button, pattern=r"^save_days\|"))
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