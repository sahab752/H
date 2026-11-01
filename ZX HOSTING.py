"""Premium Hosting Bot (fixed version).

Chalane ke steps:
    1. pip install "python-telegram-bot>=20"
    2. Neeche "APNI DETAILS DAALO" wale box mein token, username, admin ID paste karo.
    3. python premium_hosting_bot.py        <- bas yehi ek command, saare features chalu

Features: upload (.py/.js/.zip) -> owner approval -> ▶️ Run / ⏹ Stop / 📜 Logs (real runner),
plans + UPI payments, force-join, broadcast, admin panel, support tickets, auto-restart on reboot.
(Node.js scripts ke liye server pe `node` install hona chahiye.)
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from functools import partial
from copy import deepcopy
from html import escape
from pathlib import Path
from typing import Any, Awaitable, Callable

try:  # optional: load .env if python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

if sys.version_info < (3, 9):
    raise SystemExit("❌ Python 3.9 ya usse naya version chahiye. Panel/server mein Python version badlo.")


def _ensure_deps() -> None:
    """python-telegram-bot install nahi hai (ya purana hai) to apne aap install kar do."""
    try:
        import telegram  # noqa: F401
        from telegram.ext import Application  # noqa: F401
        return
    except ImportError:
        pass
    print("📦 python-telegram-bot install ho raha hai, thoda wait karo...", flush=True)
    base = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "python-telegram-bot>=21"]
    for extra in ([], ["--user"], ["--break-system-packages"]):
        if subprocess.call(base + extra) == 0:
            importlib.invalidate_caches()
            return
    raise SystemExit("❌ python-telegram-bot install nahi ho paya.\n"
                     "Manually chalao:  pip install \"python-telegram-bot>=21\"")


_ensure_deps()

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ChatMemberStatus
from telegram.error import Conflict, InvalidToken, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs URLs that contain the token
log = logging.getLogger("hostbot")

# ----------------------------------------------------------------------------
# Config (token is NEVER hard-coded; set it as an environment variable)
# ----------------------------------------------------------------------------

# =============================================================================
#  ✏️  SIRF YAHAN APNI DETAILS DAALO  (quotes " " ke andar)
# =============================================================================
BOT_TOKEN_HERE = "8588564180:AAHnPKQe6bfFaRImQGKNbMh8--P1z8Bw3d8"   # @BotFather se mila token, jaise 123456789:ABCdef...
BOT_USERNAME_HERE = "@ZX_SHOP_BUY_BOT"          # bot ka username (@ ke saath ya bina, dono chalega)
OWNER_IDS_HERE = "8669105804"                  # aapki numeric Telegram ID. Kai owner ho to comma se: "111,222"
# =============================================================================

# (Optional) agar server pe environment variable set hai to wo use hoga, warna upar wali values.
BOT_TOKEN = (os.getenv("8588564180:AAHnPKQe6bfFaRImQGKNbMh8--P1z8Bw3d8") or BOT_TOKEN_HERE).strip()
OWNER_IDS = {x.strip() for x in (os.getenv("8669105804") or OWNER_IDS_HERE).split(",")
             if x.strip().isdigit()}
BOT_USERNAME = (os.getenv("@ZX_SHOP_BUY_BOT") or BOT_USERNAME_HERE).strip().lstrip("@")
BOT_VERSION = "v4.0 (run-any-file + autofix + lifetime)"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = DATA_DIR / "db.json"
RUN_DIR = BASE_DIR / "running"          # har script ka apna folder
LOG_DIR = DATA_DIR / "logs"
RUNTIME_PATH = DATA_DIR / "runtime.json"
DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

# Runner settings (optional, environment variables se badal sakte ho)
RUNNER_ENABLED = os.getenv("RUNNER_ENABLED", "1") != "0"
MAX_RUNNING_TOTAL = int(os.getenv("MAX_RUNNING_TOTAL", "20"))   # server pe ek saath max scripts
MAX_MEMORY_MB = int(os.getenv("MAX_MEMORY_MB", "2048"))          # python script ki memory limit
AUTO_INSTALL = os.getenv("AUTO_INSTALL", "1") != "0"            # requirements.txt / package.json auto install
STARTUP_WAIT = float(os.getenv("STARTUP_WAIT", "6"))             # itne second tak crash check
ZIP_MAX_FILES = 2000
ZIP_MAX_BYTES = 200 * 1024 * 1024

DEFAULT_DB: dict[str, Any] = {
    "users": {}, "files": {}, "plans": {}, "payments": {}, "channels": [],
    "admins": [], "bots": {}, "sessions": {},
    "settings": {"bot_on": True, "upi_on": False, "upi_id": "", "free_host": 1, "lifetime_limit": 10},
}

# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
_db_lock = threading.Lock()


def save_db(db: dict) -> None:
    with _db_lock:
        temp = DB_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, DB_PATH)
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass


def load_db() -> dict:
    if not DB_PATH.exists():
        db = deepcopy(DEFAULT_DB)
        save_db(db)
        return db
    try:
        data = json.loads(DB_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("db root is not an object")
    except (OSError, ValueError) as exc:
        # Keep a copy of the broken file so data is never silently destroyed.
        backup = DB_PATH.with_name(f"db.corrupt-{int(time.time())}.json")
        try:
            shutil.copy2(DB_PATH, backup)
        except OSError:
            pass
        log.error("Database unreadable (%r). Backup saved to %s", exc, backup)
        return deepcopy(DEFAULT_DB)
    for key, value in DEFAULT_DB.items():
        if key not in data or not isinstance(data[key], type(value)):
            data[key] = deepcopy(value)
    for key, value in DEFAULT_DB["settings"].items():
        data["settings"].setdefault(key, value)
    return data


def ensure_user(db: dict, user) -> bool:
    """Create/update the user record. Returns True when the db changed."""
    uid = str(user.id)
    changed = False
    rec = db["users"].get(uid)
    if rec is None:
        rec = {"id": uid, "username": "", "first_name": user.first_name or "",
               "blocked": False, "created_at": int(time.time())}
        db["users"][uid] = rec
        changed = True
    username = user.username or ""
    if rec.get("username") != username:
        rec["username"] = username
        changed = True
    return changed


def set_session(db: dict, uid: int | str, step: str, **extra: Any) -> None:
    db["sessions"][str(uid)] = {"step": step, **extra}
    save_db(db)


def clear_session(db: dict, uid: int | str) -> None:
    if db["sessions"].pop(str(uid), None) is not None:
        save_db(db)


# ----------------------------------------------------------------------------
# Permissions / business helpers
# ----------------------------------------------------------------------------
def is_owner(uid: int | str) -> bool:
    return str(uid) in OWNER_IDS


def is_admin(uid: int | str, db: dict) -> bool:
    return is_owner(uid) or str(uid) in {str(x) for x in db.get("admins", [])}


def admin_ids(db: dict) -> set[int]:
    ids = OWNER_IDS | {str(a) for a in db.get("admins", [])}
    return {int(x) for x in ids if str(x).isdigit()}


def count_files(db: dict, uid: int | str) -> int:
    # Rejected / deleted files must not use up the hosting quota.
    return sum(1 for f in db["files"].values()
               if str(f.get("user_id")) == str(uid)
               and f.get("status") not in ("deleted", "rejected"))


def active_plan(db: dict, uid: int | str) -> dict | None:
    now = int(time.time())
    choices = [p for p in db["payments"].values()
               if str(p.get("user_id")) == str(uid) and p.get("status") == "approved"
               and int(p.get("expires_at", 0)) > now]
    return max(choices, key=lambda p: int(p.get("expires_at", 0)), default=None)


def allowance(db: dict, uid: int | str) -> int:
    now = int(time.time())
    limits = [int(p.get("host_limit", 1)) for p in db["payments"].values()
              if str(p.get("user_id")) == str(uid) and p.get("status") == "approved"
              and int(p.get("expires_at", 0)) > now]
    return max(limits) if limits else int(db["settings"].get("free_host", 1))


def fmt_date(ts: int) -> str:
    return time.strftime("%d %b %Y", time.gmtime(int(ts)))


async def send_to_admins(context: ContextTypes.DEFAULT_TYPE, db: dict,
                         send: Callable[[int], Awaitable[Any]]) -> None:
    for aid in admin_ids(db):
        try:
            await send(aid)
        except TelegramError as exc:
            log.warning("Could not notify admin %s: %s", aid, exc)


# ----------------------------------------------------------------------------
# Keyboards
# ----------------------------------------------------------------------------
USER_ROWS = [
    ["🟢 📤 Upload File", "🔵 📂 Check File"],
    ["💎 Buy Plan", "🧩 Manual Install"],
    ["🤖 All Bot", "🎫 Support Ticket"],
    ["👤 My Profile"],
]
OWNER_ROWS = [
    ["🟢 📥 Upload File", "🔵 🟢 Running Bot"],
    ["👥 All User", "🚫 Block User"],
    ["🔓 Unban User", "🆓 Set Free Host"],
    ["➕ Make Admin", "➖ Remove Admin"],
    ["📁 Pending File", "💳 Pending Payment"],
    ["📢 Broadcast", "🧾 Plan Set"],
    ["🗑 Manage Plans", "🏦 UPI Set"],
    ["⚙️ Manage UPI", "🔌 Bot Setting"],
    ["🗃 All Bot Files", "➕ Bot Add"],
    ["📋 Manage Bots", "📢 Force Join Add"],
    ["📋 Force Join List", "⬅️ User Menu"],
]


def _kb(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(b) for b in r] for r in rows],
                               resize_keyboard=True)


def user_menu(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = USER_ROWS + ([["👑 Owner Panel"]] if admin else [])
    return _kb(rows)


def owner_menu() -> ReplyKeyboardMarkup:
    return _kb(OWNER_ROWS)


def menu_for(uid: int | str, db: dict) -> ReplyKeyboardMarkup:
    return user_menu(is_admin(uid, db))


def inline(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def norm(text: str) -> str:
    """'🟢 📤 Upload File' -> 'upload file' (so old/new button labels both work)."""
    return re.sub(r"^[^A-Za-z0-9]+", "", text).strip().lower()


# ----------------------------------------------------------------------------
# Access gate (blocked / maintenance / force-join)
# ----------------------------------------------------------------------------
async def joined_all(uid: int, context: ContextTypes.DEFAULT_TYPE, db: dict) -> bool:
    for ch in db.get("channels", []):
        chat_id = ch.get("chat_id")
        if not chat_id:
            continue
        try:
            member = await context.bot.get_chat_member(chat_id=chat_id, user_id=uid)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return False
        except TelegramError as exc:
            log.warning("Force-join check failed for %s (is the bot admin there?): %s",
                        chat_id, exc)
            return False
    return True


async def ask_join(chat_id: int, context: ContextTypes.DEFAULT_TYPE, db: dict) -> None:
    rows = [[InlineKeyboardButton("📢 " + ch.get("title", "Join Channel"), url=ch["url"])]
            for ch in db.get("channels", []) if ch.get("url")]
    rows.append([InlineKeyboardButton("✅ Joined / Verify", callback_data="verify_join")])
    await context.bot.send_message(
        chat_id, "⚠️ <b>Join Required!</b>\n\nPlease join every required channel, then tap Verify.",
        parse_mode="HTML", reply_markup=inline(rows))


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE, db: dict,
               check_join: bool = True) -> bool:
    """True when the user may use the bot. Sends the explanation otherwise."""
    user, chat = update.effective_user, update.effective_chat
    if not user or not chat:
        return False
    uid = str(user.id)
    if is_admin(uid, db):
        return True
    if db["users"].get(uid, {}).get("blocked"):
        await context.bot.send_message(chat.id, "🚫 Your account is blocked.")
        return False
    if not db["settings"].get("bot_on", True):
        await context.bot.send_message(chat.id, "🔧 Bot is temporarily unavailable. Please try later.")
        return False
    if check_join and db.get("channels") and not await joined_all(user.id, context, db):
        await ask_join(chat.id, context, db)
        return False
    return True


# ----------------------------------------------------------------------------
# Small shared UI helpers
# ----------------------------------------------------------------------------
async def edit_msg(q, text: str) -> None:
    """Edit a callback message (works for text, photo and document messages)."""
    try:
        if q.message.text is not None:
            await q.edit_message_text(text, parse_mode="HTML")
        else:
            await q.edit_message_caption(caption=text, parse_mode="HTML")
    except TelegramError as exc:
        log.debug("edit_msg failed: %s", exc)


async def send_home(update: Update, context: ContextTypes.DEFAULT_TYPE, db: dict | None = None) -> None:
    db = db or load_db()
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    uid = str(user.id)
    username = f"@{user.username}" if user.username else "(no username)"
    plan = active_plan(db, uid)
    status = ("♾️ Lifetime Premium" if is_lifetime(plan) else "💎 Premium User") if plan else "🆓 Free User"
    text = (
        f"〽️ <b>Welcome, {escape(user.first_name or 'User')}!</b>\n\n"
        f"🆔 Your User ID: <code>{uid}</code>\n"
        f"👤 Username: {escape(username)}\n"
        f"🔰 Your Status: {status}\n"
        f"📁 Files Uploaded: {count_files(db, uid)}/{allowance(db, uid)}\n\n"
        "🤖 Host Python (<code>.py</code>) or JS (<code>.js</code>) scripts.\n"
        f"Upload single scripts or <code>.zip</code> archives (maximum {MAX_UPLOAD_MB} MB).\n"
        "📦 Manual module installation available.\n\n👇 Use buttons below."
    )
    await msg.reply_text(text, parse_mode="HTML", reply_markup=user_menu(is_admin(uid, db)))


async def show_plans(chat_id: int, context: ContextTypes.DEFAULT_TYPE, db: dict) -> None:
    rows = [[InlineKeyboardButton(f"{p.get('name', 'Plan')} • ₹{int(p.get('price', 0))}" + (" • ♾️ Lifetime" if int(p.get('days', 1)) <= 0 else ""),
                                  callback_data=f"plan:{pid}")]
            for pid, p in db.get("plans", {}).items() if p.get("active", True)]
    if not rows:
        await context.bot.send_message(chat_id, "No plans are available right now. Please contact support.")
        return
    rows.append([InlineKeyboardButton("⬅️ Home", callback_data="home")])
    await context.bot.send_message(chat_id, "💎 <b>Choose a hosting plan</b>",
                                   parse_mode="HTML", reply_markup=inline(rows))


async def send_user_list(chat_id: int, context: ContextTypes.DEFAULT_TYPE, db: dict) -> None:
    rows = []
    for target, u in list(db["users"].items())[:80]:
        label = f"@{u.get('username') or 'no_username'} • {target}"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"userdetail:{target}")])
    if not rows:
        await context.bot.send_message(chat_id, "No users yet.")
    else:
        await context.bot.send_message(
            chat_id, f"👥 Select a user ({len(db['users'])} total, showing first {len(rows)}):",
            reply_markup=inline(rows))


async def send_file_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int, f: dict) -> None:
    caption = (f"📁 <b>Pending file</b>\nUser: <code>{f.get('user_id')}</code>\n"
               f"File: {escape(f.get('name', 'file'))}\nFile ID: <code>{f.get('id')}</code>")
    markup = inline([[InlineKeyboardButton("✅ Accept", callback_data=f"approve_file:{f['id']}"),
                      InlineKeyboardButton("❌ Reject", callback_data=f"reject_file:{f['id']}")]])
    try:
        if f.get("file_id"):
            await context.bot.send_document(chat_id, f["file_id"], caption=caption,
                                            parse_mode="HTML", reply_markup=markup)
            return
    except TelegramError:
        pass
    await context.bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=markup)


async def send_payment_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int, p: dict) -> None:
    caption = (f"💳 <b>Pending payment</b>\nUser: <code>{p.get('user_id')}</code>\n"
               f"Plan: {escape(p.get('plan_name', 'Plan'))}\nAmount: ₹{p.get('amount')}\n"
               f"UTR: <code>{escape(p.get('transaction_id', ''))}</code>")
    markup = inline([[InlineKeyboardButton("✅ Accept", callback_data=f"approve_pay:{p['id']}"),
                      InlineKeyboardButton("❌ Reject", callback_data=f"reject_pay:{p['id']}")]])
    fid = p.get("screenshot_file_id")
    try:
        if fid and p.get("screenshot_kind") == "document":
            await context.bot.send_document(chat_id, fid, caption=caption,
                                            parse_mode="HTML", reply_markup=markup)
            return
        if fid:
            await context.bot.send_photo(chat_id, fid, caption=caption,
                                         parse_mode="HTML", reply_markup=markup)
            return
    except TelegramError:
        pass
    await context.bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=markup)


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.effective_chat:
        return
    db = load_db()
    if ensure_user(db, user):
        save_db(db)
    if not await gate(update, context, db):
        return
    await send_home(update, context, db)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    db = load_db()
    clear_session(db, user.id)
    await msg.reply_text("✅ Cancelled.", reply_markup=menu_for(user.id, db))



# ----------------------------------------------------------------------------
# ♾️ LIFETIME HOSTING  (commands: /lifetime /removelifetime /lifetimelist
#                        /setlifetimelimit /planinfo /myplan /lifetimehelp)
# ----------------------------------------------------------------------------
LIFETIME_EXPIRES = 4102444800            # 1 Jan 2100 (UTC) = practically "kabhi expire nahi"
LIFETIME_MAX_LIMIT = 1000


def is_lifetime(p: dict | None) -> bool:
    return bool(p and (p.get("lifetime") or int(p.get("expires_at", 0)) >= LIFETIME_EXPIRES))


def days_label(days: Any) -> str:
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 0
    return "Lifetime ♾️" if d <= 0 else f"{d} days"


def lifetime_default_limit(db: dict) -> int:
    try:
        return max(1, int(db["settings"].get("lifetime_limit", 10)))
    except (TypeError, ValueError):
        return 10


def lifetime_records(db: dict, uid: int | str) -> list[dict]:
    return [p for p in db["payments"].values()
            if str(p.get("user_id")) == str(uid) and p.get("status") == "approved" and is_lifetime(p)]


def resolve_target(db: dict, arg: str) -> str | None:
    """Numeric ID ya @username -> user id (str). Na mile to None."""
    arg = arg.strip()
    if arg.isdigit():
        return arg
    name = arg.lstrip("@").lower()
    if not name:
        return None
    for tid, u in db["users"].items():
        if (u.get("username") or "").lower() == name:
            return tid
    return None


def plan_summary(db: dict, uid: int | str) -> str:
    plan = active_plan(db, uid)
    if plan and is_lifetime(plan):
        status, validity = "♾️ Lifetime Premium", "Kabhi expire nahi hoga"
    elif plan:
        left = max(0, (int(plan["expires_at"]) - int(time.time())) // 86400)
        status, validity = "💎 Premium", f"{fmt_date(plan['expires_at'])} (UTC) • {left} din baaki"
    else:
        status, validity = "🆓 Free", "—"
    name = escape(plan.get("plan_name", "Plan")) if plan else "Free"
    return (f"🔰 Status: <b>{status}</b>\n📦 Plan: {name}\n⏳ Validity: {validity}\n"
            f"📁 Files: {count_files(db, uid)}/{allowance(db, uid)}")


async def cmd_lifetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/lifetime <user_id|@username> [host_limit]  -> user ko lifetime hosting do (owner only)."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    if not is_owner(user.id):
        await msg.reply_text("⛔ Ye command sirf owner ke liye hai.")
        return
    db = load_db()
    args = context.args or []
    if not args:
        await msg.reply_text("Usage:\n/lifetime <user_id ya @username> [host_limit]\n\n"
                             "Example:\n/lifetime 123456789\n/lifetime 123456789 5\n/lifetime @username 20")
        return
    target = resolve_target(db, args[0])
    if not target:
        await msg.reply_text("❌ User nahi mila. Numeric ID do, ya user pehle bot ko /start kare.")
        return
    limit = lifetime_default_limit(db)
    if len(args) > 1:
        if not args[1].isdigit() or not 1 <= int(args[1]) <= LIFETIME_MAX_LIMIT:
            await msg.reply_text(f"❌ host_limit 1 se {LIFETIME_MAX_LIMIT} ke beech ka number hona chahiye.")
            return
        limit = int(args[1])
    now = int(time.time())
    existing = lifetime_records(db, target)
    if existing:
        existing[0]["host_limit"] = limit
        existing[0]["updated_at"] = now
        done = "🔄 Lifetime already tha — host limit update ho gayi."
    else:
        pid = "LT" + uuid.uuid4().hex[:8]
        db["payments"][pid] = {
            "id": pid, "user_id": target, "plan_id": "lifetime", "plan_name": "Lifetime Hosting",
            "amount": 0, "host_limit": limit, "days": 0, "status": "approved", "lifetime": True,
            "granted_by": str(user.id), "created_at": now, "expires_at": LIFETIME_EXPIRES,
            "transaction_id": "", "screenshot_file_id": "",
        }
        db["users"].setdefault(target, {"id": target, "username": "", "first_name": "",
                                        "blocked": False, "created_at": now})
        done = "✅ Lifetime hosting activate ho gayi."
    save_db(db)
    await msg.reply_text(f"{done}\n\n👤 User: <code>{target}</code>\n📁 Host limit: {limit}\n♾️ Validity: Lifetime",
                         parse_mode="HTML")
    try:
        await context.bot.send_message(
            int(target),
            f"🎉 <b>Lifetime Hosting activated!</b>\n\n♾️ Aapka plan kabhi expire nahi hoga.\n"
            f"📁 Hosting limit: <b>{limit}</b> files/bots\n\nAb 📤 Upload File se apni bot files host karo.",
            parse_mode="HTML")
    except (TelegramError, ValueError):
        await msg.reply_text("ℹ️ User ko message nahi ja paya (shayad usne bot start/ block kiya hai). Plan phir bhi active hai.")


async def cmd_removelifetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/removelifetime <user_id|@username> -> lifetime hosting hata do (owner only)."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    if not is_owner(user.id):
        await msg.reply_text("⛔ Ye command sirf owner ke liye hai.")
        return
    db = load_db()
    args = context.args or []
    if not args:
        await msg.reply_text("Usage:\n/removelifetime <user_id ya @username>")
        return
    target = resolve_target(db, args[0])
    recs = lifetime_records(db, target) if target else []
    if not recs:
        await msg.reply_text("❌ Is user ke paas koi active lifetime plan nahi hai.")
        return
    now = int(time.time())
    for r in recs:
        r["status"] = "revoked"
        r["revoked_at"] = now
        r["revoked_by"] = str(user.id)
    save_db(db)
    await msg.reply_text(f"✅ Lifetime hosting hata di gayi.\n\n👤 User: <code>{target}</code>\n"
                         "ℹ️ Uski uploaded files delete nahi hui; limit ab free/baaki plan ke hisaab se hogi.",
                         parse_mode="HTML")
    try:
        await context.bot.send_message(int(target), "⚠️ Aapka Lifetime Hosting plan owner ne hata diya hai. "
                                                    "Details ke liye support se contact karo.")
    except (TelegramError, ValueError):
        pass


async def cmd_lifetimelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/lifetimelist -> saare lifetime users ki list (admin/owner)."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    db = load_db()
    if not is_admin(user.id, db):
        await msg.reply_text("⛔ Ye command sirf admin/owner ke liye hai.")
        return
    recs = sorted((p for p in db["payments"].values() if p.get("status") == "approved" and is_lifetime(p)),
                  key=lambda p: int(p.get("created_at", 0)))
    if not recs:
        await msg.reply_text("Abhi koi lifetime user nahi hai.")
        return
    lines = [f"♾️ <b>Lifetime users ({len(recs)})</b>\n"]
    for i, p in enumerate(recs, 1):
        tid = str(p.get("user_id"))
        uname = db["users"].get(tid, {}).get("username") or "no_username"
        lines.append(f"{i}. <code>{tid}</code> • @{escape(uname)} • limit {p.get('host_limit')} • "
                     f"files {count_files(db, tid)} • {fmt_date(p.get('created_at', 0))}")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3500:
            await msg.reply_text(chunk, parse_mode="HTML")
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await msg.reply_text(chunk, parse_mode="HTML")


async def cmd_setlifetimelimit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setlifetimelimit <number> -> naye lifetime users ka default host limit (owner only)."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    if not is_owner(user.id):
        await msg.reply_text("⛔ Ye command sirf owner ke liye hai.")
        return
    db = load_db()
    args = context.args or []
    if not args or not args[0].isdigit() or not 1 <= int(args[0]) <= LIFETIME_MAX_LIMIT:
        await msg.reply_text(f"Usage:\n/setlifetimelimit <1-{LIFETIME_MAX_LIMIT}>\n\n"
                             f"Abhi default limit: {lifetime_default_limit(db)}")
        return
    db["settings"]["lifetime_limit"] = int(args[0])
    save_db(db)
    await msg.reply_text(f"✅ Lifetime default host limit ab {int(args[0])} hai.\n"
                         "(Ye sirf aage ke /lifetime grants par lagega; purane users ka limit same rahega.)")


async def cmd_planinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/planinfo <user_id|@username> -> kisi bhi user ka plan status (admin/owner)."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    db = load_db()
    if not is_admin(user.id, db):
        await msg.reply_text("⛔ Ye command sirf admin/owner ke liye hai.")
        return
    args = context.args or []
    target = resolve_target(db, args[0]) if args else None
    if not target:
        await msg.reply_text("Usage:\n/planinfo <user_id ya @username>")
        return
    u = db["users"].get(target, {})
    running = sum(1 for fid, f in db["files"].items()
                  if str(f.get("user_id")) == target and is_running(fid))
    await msg.reply_text(
        f"👤 <b>User plan info</b>\nID: <code>{target}</code>\n"
        f"Username: @{escape(u.get('username') or 'none')}\n\n{plan_summary(db, target)}\n"
        f"🟢 Running scripts: {running}", parse_mode="HTML")


async def cmd_myplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/myplan -> user apna plan dekhe."""
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    db = load_db()
    if ensure_user(db, user):
        save_db(db)
    if not await gate(update, context, db):
        return
    await msg.reply_text(f"📋 <b>Aapka plan</b>\n\n{plan_summary(db, user.id)}", parse_mode="HTML")


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/version -> server par abhi kaunsi bot file chal rahi hai (admin/owner)."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    db = load_db()
    if not is_admin(user.id, db):
        return
    await msg.reply_text(
        f"🤖 <b>Premium Hosting Bot {escape(BOT_VERSION)}</b>\n"
        f"📂 File: <code>{escape(str(Path(__file__).resolve()))}</code>\n"
        f"🛠 Auto-fix: ON\n♾️ Lifetime: ON\n🐍 Python {sys.version.split()[0]}\n\n"
        "Agar yahan ye message nahi aaya, to server par purani bot file chal rahi hai.",
        parse_mode="HTML")


async def cmd_lifetimehelp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/lifetimehelp -> admin ke liye lifetime commands ki list."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    db = load_db()
    if not is_admin(user.id, db):
        await msg.reply_text("⛔ Ye command sirf admin/owner ke liye hai.")
        return
    await msg.reply_text(
        "♾️ <b>Lifetime Hosting Commands</b>\n\n"
        "👑 <b>Owner only</b>\n"
        "/lifetime &lt;id|@user&gt; [limit] — lifetime do / limit badlo\n"
        "/removelifetime &lt;id|@user&gt; — lifetime hatao\n"
        "/setlifetimelimit &lt;n&gt; — naye grants ka default limit\n\n"
        "🛡 <b>Admin + Owner</b>\n"
        "/lifetimelist — saare lifetime users\n"
        "/planinfo &lt;id|@user&gt; — kisi ka plan status\n\n"
        "👤 <b>Sab users</b>\n"
        "/myplan — apna plan dekho\n\n"
        "💡 Paid lifetime plan: 🧾 Plan Set mein duration <b>0</b> daalo.",
        parse_mode="HTML")


# ----------------------------------------------------------------------------
# Script runner (▶️ Run / ⏹ Stop / 📜 Logs)
# ----------------------------------------------------------------------------
class RunnerError(Exception):
    pass


PROCS: dict[str, asyncio.subprocess.Process] = {}
STOPPING: set[str] = set()
BG_TASKS: set[asyncio.Task] = set()
PY_ENTRIES = ["main.py", "bot.py", "app.py", "run.py", "start.py", "index.py"]
JS_ENTRIES = ["index.js", "main.js", "bot.js", "app.js", "server.js", "start.js"]


def clean_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:120] or "upload.bin"


def is_running(fid: str) -> bool:
    p = PROCS.get(fid)
    return bool(p and p.returncode is None)


def _autostart_load() -> set[str]:
    try:
        return set(json.loads(RUNTIME_PATH.read_text(encoding="utf-8")).get("autostart", []))
    except (OSError, ValueError, AttributeError):
        return set()


def _autostart_save(items: set[str]) -> None:
    try:
        RUNTIME_PATH.write_text(json.dumps({"autostart": sorted(items)}), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save runtime state: %s", exc)


def _autostart_change(fid: str, add: bool) -> None:
    items = _autostart_load()
    (items.add if add else items.discard)(fid)
    _autostart_save(items)


def safe_extract_zip(zip_path: Path, dest: Path) -> None:
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise RunnerError("This .zip file is corrupted.") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > ZIP_MAX_FILES:
            raise RunnerError(f"Zip has too many files (max {ZIP_MAX_FILES}).")
        if sum(i.file_size for i in infos) > ZIP_MAX_BYTES:
            raise RunnerError("Zip is too large after extraction.")
        base = dest.resolve()
        for info in infos:
            target = (dest / info.filename).resolve()
            if target != base and base not in target.parents:
                raise RunnerError("Zip contains an unsafe path.")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise RunnerError("Zip contains a symbolic link, which is not allowed.")
        zf.extractall(dest)


TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
SKIP_DIRS = {".deps", "node_modules", "__pycache__", ".git", "venv", ".venv", "env", "__MACOSX", "site-packages"}
PY_BOT_HINTS = ("telebot", "TeleBot(", "telegram.ext", "Application.builder", "aiogram", "pyrogram",
                "telethon", "infinity_polling", "run_polling", ".polling(", "Updater(")
JS_BOT_HINTS = ("node-telegram-bot-api", "telegraf", "TelegramBot", "grammy", "discord.js", "client.login", "listen(")


LANG_EXT = {".py": "python", ".js": "node", ".mjs": "node", ".cjs": "node",
            ".sh": "bash", ".php": "php", ".rb": "ruby", ".pl": "perl", ".go": "go"}
ALLOWED_EXTS = tuple(LANG_EXT) + (".zip",)
OTHER_ENTRIES = {
    "bash": ["start.sh", "run.sh", "main.sh", "bot.sh"],
    "php": ["index.php", "main.php", "bot.php", "start.php"],
    "ruby": ["main.rb", "bot.rb", "app.rb"],
    "perl": ["main.pl", "bot.pl"],
    "go": ["main.go"],
}
KIND_BIN = {"node": "node", "bash": "bash", "php": "php", "ruby": "ruby", "perl": "perl", "go": "go"}
INSTALL_HINT = {"node": "pkg install nodejs", "bash": "pkg install bash", "php": "pkg install php",
                "ruby": "pkg install ruby", "perl": "pkg install perl", "go": "pkg install golang"}


def _candidates(root: Path, ext: str, max_depth: int = 3) -> list[Path]:
    found: list[Path] = []
    base = len(root.parts)
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        if len(Path(dp).parts) - base >= max_depth:
            dns[:] = []
        found.extend(Path(dp) / fn for fn in fns if fn.lower().endswith(ext))
    return found


def _score_source(path: Path, hints: tuple) -> int:
    try:
        txt = path.read_text(encoding="utf-8", errors="ignore")[:300000]
    except OSError:
        return 0
    score = 0
    if TOKEN_RE.search(txt):
        score += 10                      # file ke andar bot token mila
    if any(h in txt for h in hints):
        score += 5                       # bot library use ho rahi hai
    if "__main__" in txt or "polling" in txt:
        score += 2
    if path.name.lower().startswith(("setup", "config", "utils", "helper", "settings", "__init__", "test")):
        score -= 4
    return score + min(len(txt) // 3000, 3)


def find_entry(root: Path) -> tuple[Path, str]:
    """Kisi bhi naam / bhasha ki bot file dhoondho (Main.py, MAIN.PY, random.py, .js, .sh, .php ...)."""
    names = {p.name.lower(): p for p in root.iterdir() if p.is_file()}
    for n in PY_ENTRIES:
        if n in names:
            return names[n], "python"
    for n in JS_ENTRIES:
        if n in names:
            return names[n], "node"
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            main = json.loads(pkg.read_text(encoding="utf-8")).get("main")
            if main and (root / main).is_file():
                return root / main, "node"
        except (OSError, ValueError):
            pass
    for kind, entries in OTHER_ENTRIES.items():
        for n in entries:
            if n in names:
                return names[n], kind
    pys = sorted(p for p in names.values() if p.suffix.lower() == ".py")
    if len(pys) == 1:
        return pys[0], "python"
    jss = sorted(p for p in names.values() if p.suffix.lower() in (".js", ".mjs", ".cjs"))
    if len(jss) == 1:
        return jss[0], "node"
    # Koi bhi naam ho: sabse zyada "bot jaisi" file (token / telebot / polling wali) chuno
    py_all = _candidates(root, ".py")
    if py_all:
        best = max(py_all, key=lambda p: (_score_source(p, PY_BOT_HINTS), -len(p.relative_to(root).parts)))
        return best, "python"
    js_all = [c for e in (".js", ".mjs", ".cjs") for c in _candidates(root, e)]
    if js_all:
        best = max(js_all, key=lambda p: (_score_source(p, JS_BOT_HINTS), -len(p.relative_to(root).parts)))
        return best, "node"
    for kind, exts in (("bash", (".sh",)), ("php", (".php",)), ("ruby", (".rb",)),
                       ("perl", (".pl",)), ("go", (".go",))):
        found = [c for e in exts for c in _candidates(root, e)]
        if found:
            return min(found, key=lambda p: (len(p.relative_to(root).parts), -p.stat().st_size)), kind
    raise RunnerError("Is upload mein koi chalne wali file nahi mili (.py .js .sh .php .rb .pl .go).")


def build_args(kind: str, rel: str) -> tuple[list[str], str]:
    """(command list, error message). Error khali ho to command sahi hai."""
    if kind == "python":
        return [sys.executable, "-u", rel], ""
    binary = shutil.which(KIND_BIN[kind]) or (shutil.which("sh") if kind == "bash" else None)
    if not binary:
        return [], (f"❌ '{KIND_BIN[kind]}' is server par install nahi hai, isliye ye {kind} file nahi chal sakti.\n"
                    f"Install karo (Termux: {INSTALL_HINT[kind]}  |  Linux: apt install {KIND_BIN[kind]}).")
    if kind == "go":
        return [binary, "run", rel], ""
    return [binary, rel], ""


# Import ka naam aur pip package ka naam alag hota hai - yahan map hai
PIP_NAMES = {
    "telebot": "pyTelegramBotAPI", "telegram": "python-telegram-bot", "cv2": "opencv-python",
    "PIL": "Pillow", "bs4": "beautifulsoup4", "yaml": "PyYAML", "dotenv": "python-dotenv",
    "sklearn": "scikit-learn", "Crypto": "pycryptodome", "dateutil": "python-dateutil",
    "telethon": "Telethon", "pyrogram": "Pyrogram", "flask_cors": "flask-cors", "jwt": "PyJWT",
    "serial": "pyserial", "git": "GitPython", "magic": "python-magic", "docx": "python-docx",
    "fitz": "PyMuPDF", "Image": "Pillow", "tgcrypto": "TgCrypto", "OpenSSL": "pyOpenSSL",
    "discord": "discord.py", "googleapiclient": "google-api-python-client", "psycopg2": "psycopg2-binary",
}
_PY_MISSING_RE = re.compile(r"No module named ['\"]([A-Za-z0-9_.\-]+)['\"]")
_JS_MISSING_RE = re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]")
MAX_AUTOFIX = 8   # ek start mein max itne missing modules khud install honge


def missing_module(kind: str, text: str) -> str | None:
    if kind not in ("python", "node"):
        return None
    rx = _PY_MISSING_RE if kind == "python" else _JS_MISSING_RE
    found = rx.findall(text)
    if not found:
        return None
    name = found[-1]
    if kind == "node":
        if name.startswith((".", "/")):
            return None
        return "/".join(name.split("/")[:2]) if name.startswith("@") else name.split("/")[0]
    return name.split(".")[0]


def prepare_workdir(f: dict) -> tuple[Path, Path, str]:
    """Copy / extract the stored upload into its own run folder (blocking; run in a thread)."""
    src = Path(f.get("path", ""))
    if not src.is_file():
        raise RunnerError("Stored file is missing on the server.")
    base = RUN_DIR / f["id"]
    ready = base / ".ready"
    if not ready.exists():
        shutil.rmtree(base, ignore_errors=True)
        root = base / "app"
        root.mkdir(parents=True, mode=0o700)
        if f["name"].lower().endswith(".zip"):
            safe_extract_zip(src, root)
            items = [x for x in root.iterdir() if x.name != "__MACOSX"]
            if len(items) == 1 and items[0].is_dir():
                root = items[0]
        else:
            shutil.copy2(src, root / clean_name(f["name"]))
        ready.write_text(str(root), encoding="utf-8")
    root = Path(ready.read_text(encoding="utf-8"))
    entry, kind = find_entry(root)
    return root, entry, kind


def _limits(kind: str) -> None:
    try:
        import resource
        if kind == "python":                       # node (V8) breaks with an address-space limit
            mem = MAX_MEMORY_MB * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        fsize = 200 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
    except Exception:  # noqa: BLE001  (e.g. Windows)
        pass


async def _run_quiet(args: list[str], cwd: Path, timeout: int, env: dict) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, "Installation timed out."
    return proc.returncode or 0, out.decode("utf-8", "replace")[-600:]


async def install_deps(root: Path, kind: str, env: dict) -> list[str]:
    """requirements.txt / package.json install. Kabhi exception nahi deta; warnings ki list lautata hai."""
    warns: list[str] = []
    try:
        if kind == "python" and (root / "requirements.txt").is_file() and not (root / ".deps").exists():
            base = [sys.executable, "-m", "pip", "install", "--target", str(root / ".deps"),
                    "--no-input", "--disable-pip-version-check", "-q"]
            code, _ = await _run_quiet(base + ["-r", "requirements.txt"], root, 300, env)
            if code != 0:      # bulk fail: ek-ek package try karo, jo fail ho use chhod do
                try:
                    lines = (root / "requirements.txt").read_text(encoding="utf-8", errors="ignore").splitlines()
                except OSError:
                    lines = []
                pkgs = [ln.split("#")[0].strip() for ln in lines]
                pkgs = [x for x in pkgs if x and not x.startswith(("-", "git+", "http"))][:40]
                failed = []
                for pkg in pkgs:
                    c, _ = await _run_quiet(base + [pkg], root, 180, env)
                    if c != 0:
                        bare = re.split(r"[<>=!~\[; ]", pkg, 1)[0]      # version pin hata ke dobara
                        c2 = (await _run_quiet(base + [bare], root, 180, env))[0] if bare and bare != pkg else 1
                        if c2 != 0:
                            failed.append(pkg)
                if failed:
                    warns.append("Ye packages install nahi ho paye: " + ", ".join(failed[:8]))
            (root / ".deps").mkdir(exist_ok=True)
        if kind == "node" and (root / "package.json").is_file() and not (root / "node_modules").exists():
            npm = shutil.which("npm")
            if not npm:
                warns.append("npm server par install nahi hai (package.json ke modules install nahi hue).")
            else:
                code, out = await _run_quiet([npm, "install", "--omit=dev", "--no-audit", "--no-fund"], root, 300, env)
                if code != 0:
                    warns.append("npm install fail hua: " + out[-200:])
            (root / "node_modules").mkdir(exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("install_deps error: %r", exc)
        warns.append(f"dependency install error: {exc}")
    return warns


def crash_hint(text: str) -> str:
    low = text.lower()
    if "conflict" in low and "getupdates" in low:
        return "💡 Is bot token se bot kahin aur bhi chal raha hai. Baaki jagah band karke ek hi jagah chalao."
    if "invalidtoken" in low or "unauthorized" in low or "token is invalid" in low:
        return "💡 Script ke andar ka bot token galat ya revoke ho chuka hai. @BotFather se sahi token daalo."
    if "eoferror" in low:
        return "💡 Script input() maang rahi hai. Hosting par keyboard input nahi hota — token/ID code ke andar likho."
    return ""


def tail_log(fid: str, limit: int = 1800) -> str:
    path = LOG_DIR / f"{fid}.log"
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - limit))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


async def _watch(bot, fid: str, proc: asyncio.subprocess.Process, logf, name: str, owner: str) -> None:
    started = time.monotonic()
    code = await proc.wait()
    try:
        logf.close()
    except OSError:
        pass
    if PROCS.get(fid) is proc:
        PROCS.pop(fid, None)
    if fid in STOPPING:
        STOPPING.discard(fid)
        return
    _autostart_change(fid, False)
    if time.monotonic() - started < STARTUP_WAIT + 1:
        return  # startup crash: start_script already reports it to the user
    tail = tail_log(fid, 700).strip()
    if code == 0:
        text = f"✅ Your script <b>{escape(name)}</b> finished (exit code 0)."
    else:
        text = f"⚠️ Your script <b>{escape(name)}</b> stopped (exit code {code})."
    if tail:
        text += f"\n\n<pre>{escape(tail)}</pre>"
    hint = crash_hint(tail)
    if hint:
        text += "\n\n" + escape(hint)
    try:
        await bot.send_message(int(owner), text, parse_mode="HTML")
    except (TelegramError, ValueError):
        pass


# ---------------------------------------------------------------------------
# 🛠 AUTO-FIX: script ki aam galtiyan bot khud theek karke dobara chalata hai
#   • placeholder + value:   ADMIN_ID = YOUR_ADMIN_ID 8669105804   ->  ADMIN_ID = 8669105804
#   • copy-paste ke curly quotes “ ” ‘ ’, non-breaking space, zero-width chars, – — dash
#   • TabError (tabs/spaces mix)        • file encoding (cp1252/latin-1 -> utf-8)
# Sirf run-folder ki copy badalti hai; original upload safe rehta hai (.orig backup bhi banta hai).
# ---------------------------------------------------------------------------
_PH = r"[A-Za-z0-9_]*(?:YOUR|PASTE|APNA|APNI|PUT|ENTER|INSERT|ADD)_[A-Za-z0-9_]+"
_RE_PH_FIRST = re.compile(rf"^(\s*[A-Za-z_][\w.]*\s*(?::[^=]+)?=\s*)({_PH})[ \t]+(\S.*)$")
_RE_PH_LAST = re.compile(rf"^(\s*[A-Za-z_][\w.]*\s*=\s*)(-?\d[\d_.]*|\"[^\"]*\"|'[^']*')[ \t]+({_PH})[ \t]*(#.*)?$")
_CHAR_FIXES = {"\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2018": "'", "\u2019": "'",
               "\u00a0": " ", "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
               "\u2013": "-", "\u2014": "-"}
_PY_SYNTAX_RE = re.compile(r"(SyntaxError|IndentationError|TabError)")


def _error_target(run_log: str, root: Path, entry: Path) -> tuple[Path, int]:
    base = root.resolve()
    for path, line in reversed(re.findall(r'File "([^"]+)", line (\d+)', run_log)):
        try:
            rp = Path(path).resolve()
        except OSError:
            continue
        if rp.suffix == ".py" and rp.is_file() and base in rp.parents and ".deps" not in rp.parts:
            return rp, int(line)
    return entry, 0


def autofix_python(run_log: str, root: Path, entry: Path) -> list[str]:
    """Blocking (thread mein chalao). Return: kya-kya theek kiya uski list (khali = kuch nahi hua)."""
    try:
        return _autofix_python(run_log, root, entry)
    except Exception as exc:  # noqa: BLE001
        log.warning("autofix_python error: %r", exc)
        return []


def _autofix_python(run_log: str, root: Path, entry: Path) -> list[str]:
    if not _PY_SYNTAX_RE.search(run_log):
        return []
    target, lineno = _error_target(run_log, root, entry)
    try:
        raw = target.read_bytes()
    except OSError:
        return []
    notes: list[str] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
        notes.append("file encoding utf-8 mein convert ki")
    original = text

    if "invalid character" in run_log or "non-printable" in run_log:
        fixed = text
        for bad, good in _CHAR_FIXES.items():
            fixed = fixed.replace(bad, good)
        if fixed != text:
            text = fixed
            notes.append("curly quotes / hidden characters sahi kiye")

    if "TabError" in run_log or "inconsistent use of tabs" in run_log:
        if "\t" in text:
            text = text.expandtabs(4)
            notes.append("tabs ko spaces mein badla")

    if lineno:
        lines = text.split("\n")
        if 1 <= lineno <= len(lines):
            old_line = lines[lineno - 1]
            new_line = old_line
            m = _RE_PH_FIRST.match(old_line)
            if m:
                new_line = m.group(1) + m.group(3)
            else:
                m = _RE_PH_LAST.match(old_line)
                if m:
                    new_line = m.group(1) + m.group(2) + ((" " + m.group(4)) if m.group(4) else "")
            if new_line == old_line and "Missing parentheses" in run_log:      # Python 2 print "x"
                m = re.match(r"^(\s*)print\s+(.*\S)\s*$", old_line)
                if m:
                    new_line = f"{m.group(1)}print({m.group(2)})"
            if new_line == old_line:                                           # except X, e:
                m = re.match(r"^(\s*except\s+[\w.]+)\s*,\s*(\w+)\s*:(.*)$", old_line)
                if m:
                    new_line = f"{m.group(1)} as {m.group(2)}:{m.group(3)}"
            if new_line != old_line:
                lines[lineno - 1] = new_line
                text = "\n".join(lines)
                notes.append(f"line {lineno} theek ki  →  {new_line.strip()[:60]}")

    if text == original and not notes:
        return []
    try:
        backup = target.with_name(target.name + ".orig")
        if not backup.exists():
            backup.write_bytes(raw)
        target.write_bytes(text.encode("utf-8"))      # (write_text(newline=) sirf Python 3.10+ me chalta hai)
    except OSError:
        return []
    return notes


async def _start_script_impl(bot, db: dict, fid: str) -> tuple[bool, str]:
    if not RUNNER_ENABLED:
        return False, "The script runner is disabled by the owner."
    f = db["files"].get(fid)
    if not f or f.get("status") != "approved":
        return False, "Only approved files can run."
    if is_running(fid):
        return True, "Already running."
    live = [k for k in PROCS if is_running(k)]
    if len(live) >= MAX_RUNNING_TOTAL:
        return False, "Server is full right now. Please try again later."
    owner = str(f.get("user_id"))
    if not is_admin(owner, db):
        mine = sum(1 for k in live if str(db["files"].get(k, {}).get("user_id")) == owner)
        if mine >= max(1, allowance(db, owner)):
            return False, "You reached your running-scripts limit. Stop another script first."
    try:
        root, entry, kind = await asyncio.to_thread(prepare_workdir, f)
    except RunnerError as exc:
        return False, str(exc)
    except OSError as exc:
        return False, f"Could not prepare the script: {exc}"

    # Scripts never get the bot token or any other server secret.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root), "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(root / ".deps"), "NODE_PATH": str(root / "node_modules"),
    }
    pre_warn: list[str] = []
    if AUTO_INSTALL:
        pre_warn = await install_deps(root, kind, env)
    rel = str(entry.relative_to(root))
    args, err = build_args(kind, rel)
    if err:
        return False, err
    tried: set[str] = set()
    fixed_notes: list[str] = []
    for attempt in range(MAX_AUTOFIX + 1):
        logf = (LOG_DIR / f"{fid}.log").open("ab")
        logf.write(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
        logf.flush()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(root), env=env, stdin=asyncio.subprocess.DEVNULL, stdout=logf, stderr=logf,
                start_new_session=True,
                preexec_fn=partial(_limits, kind) if os.name == "posix" else None)
        except OSError as exc:
            logf.close()
            return False, f"Could not start the script: {exc}"
        PROCS[fid] = proc
        _autostart_change(fid, True)
        task = asyncio.create_task(_watch(bot, fid, proc, logf, f.get("name", "script"), owner))
        BG_TASKS.add(task)
        task.add_done_callback(BG_TASKS.discard)
        waited = 0.0
        while waited < STARTUP_WAIT and proc.returncode is None:   # startup crash pakadne ke liye wait
            await asyncio.sleep(0.5)
            waited += 0.5
        if proc.returncode is None:
            _autostart_change(fid, True)
            extra = ("\n\n🛠 Auto-fix applied:\n• " + "\n• ".join(fixed_notes)) if fixed_notes else ""
            return True, "Script is running." + extra
        await asyncio.sleep(0.3)     # _watch ko apna kaam khatam karne do
        # Sirf is run ka log (pichle run ka nahi)
        run_log = tail_log(fid, 4000).rsplit("--- started", 1)[-1].split("\n", 1)[-1]
        fixes = ("\n\n🛠 Auto-fix applied:\n• " + "\n• ".join(fixed_notes)) if fixed_notes else ""
        if proc.returncode == 0:     # script ne apna kaam poora karke normal exit kiya = error nahi
            _autostart_change(fid, False)
            return True, "✅ Script finished successfully (exit code 0).\n\n" + (run_log.strip()[-700:] or "(no output)") + fixes
        mod = missing_module(kind, run_log) if AUTO_INSTALL else None
        if mod and mod not in tried and attempt < MAX_AUTOFIX:
            tried.add(mod)
            local = (root / mod).exists() or (root / f"{mod}.py").exists() or (root / f"{mod}.js").exists()
            if not local:
                if kind == "python":
                    pkg = PIP_NAMES.get(mod, mod)
                    cmd = [sys.executable, "-m", "pip", "install", pkg, "--target", str(root / ".deps"),
                           "--no-input", "--disable-pip-version-check", "-q"]
                else:
                    npm = shutil.which("npm")
                    if not npm:
                        return False, "Missing module '%s' but npm is not installed on the server." % mod
                    pkg = mod
                    cmd = [npm, "install", pkg, "--no-audit", "--no-fund"]
                code, out = await _run_quiet(cmd, root, 300, env)
                if code == 0:
                    fixed_notes.append(f"missing module '{pkg}' install kiya")
                    log.info("Auto-installed %s for %s", pkg, fid)
                    continue
                return False, f"Module '{mod}' install nahi ho paya:\n{out}"
        if kind == "python" and attempt < MAX_AUTOFIX:
            notes = await asyncio.to_thread(autofix_python, run_log, root, entry)
            if notes:
                fixed_notes.extend(notes)
                log.info("Auto-fix for %s: %s", fid, "; ".join(notes))
                continue
        hint = ""
        if _PY_SYNTAX_RE.search(run_log):
            hint = "\n\n💡 Ye galti script ke code ke andar hai. Bot ise khud theek nahi kar paya — error wali line sahi karke file dobara upload karo."
        elif re.search(r"NameError: name '[A-Z0-9_]*(YOUR|PASTE|APNA|APNI)", run_log):
            hint = "\n\n💡 Script mein YOUR_/APNA_ jaisi placeholder value hai — use apni asli value (token/ID) se badlo."
        else:
            h = crash_hint(run_log)
            if h:
                hint = "\n\n" + h
        warn = ("\n\n⚠️ " + " | ".join(pre_warn)) if pre_warn else ""
        return False, "Script exited immediately.\n\n" + (run_log.strip()[-700:] or "(no output)") + fixes + warn + hint
    return False, "Script exited immediately."


async def start_script(bot, db: dict, fid: str) -> tuple[bool, str]:
    """Safe wrapper: koi bhi andar ki galti 'Something went wrong' nahi banegi, saaf message milega."""
    try:
        return await _start_script_impl(bot, db, fid)
    except Exception as exc:  # noqa: BLE001
        log.exception("start_script crashed for %s", fid)
        return False, f"Internal error while starting: {type(exc).__name__}: {str(exc)[:300]}"


async def _terminate(fid: str, proc: asyncio.subprocess.Process) -> None:
    STOPPING.add(fid)
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), 8)
    except asyncio.TimeoutError:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def stop_script(fid: str, remember: bool = False) -> bool:
    """Stop a running script. remember=True keeps it in the auto-restart list (used on bot shutdown)."""
    if not remember:
        _autostart_change(fid, False)
    proc = PROCS.get(fid)
    if proc and proc.returncode is None:
        await _terminate(fid, proc)
        return True
    return False


async def post_init(app: Application) -> None:
    """Bot connect hone par confirmation + pehle chalte hue scripts dobara chalu karo."""
    try:
        me = await app.bot.get_me()
        log.info("✅ Bot connected: @%s — ab Telegram mein /start bhejo.", me.username)
    except TelegramError as exc:
        log.warning("get_me failed: %s", exc)
    if not RUNNER_ENABLED:
        return
    db = load_db()
    for fid in sorted(_autostart_load()):
        f = db["files"].get(fid)
        if not f or f.get("status") != "approved":
            _autostart_change(fid, False)
            continue
        ok, info = await start_script(app.bot, db, fid)
        log.info("Auto-restart %s (%s): %s", fid, f.get("name"), info.splitlines()[0])


async def post_shutdown(app: Application) -> None:
    for fid in list(PROCS):
        await stop_script(fid, remember=True)

# ----------------------------------------------------------------------------
# Menu actions  (each: async fn(update, context, db, uid))
# ----------------------------------------------------------------------------
MenuFn = Callable[[Update, ContextTypes.DEFAULT_TYPE, dict, str], Awaitable[None]]


def ask(step: str, prompt: str) -> MenuFn:
    async def handler(update, context, db, uid):
        set_session(db, uid, step)
        await update.effective_message.reply_text(prompt)
    return handler


async def m_owner_panel(update, context, db, uid):
    await update.effective_message.reply_text("👑 <b>Owner Panel</b>", parse_mode="HTML",
                                              reply_markup=owner_menu())


async def m_user_menu(update, context, db, uid):
    await update.effective_message.reply_text("User menu:", reply_markup=menu_for(uid, db))


async def m_upload(update, context, db, uid):
    await update.effective_message.reply_text(
        f"Send a bot file: .py .js .sh .php .rb .pl .go or a .zip (maximum {MAX_UPLOAD_MB} MB). It will be reviewed by the owner.")


async def m_check_file(update, context, db, uid):
    msg = update.effective_message
    files = {fid: f for fid, f in db["files"].items()
             if str(f.get("user_id")) == uid and f.get("status") != "deleted"}
    if not files:
        await msg.reply_text("You have no uploaded files yet.")
        return
    rows = [[InlineKeyboardButton(f"{'🟢 ' if is_running(fid) else ''}{f['name']} • {f['status']}"[:60],
                                  callback_data=f"fileinfo:{fid}")]
            for fid, f in files.items()]
    await msg.reply_text("📂 Your hosted files:", reply_markup=inline(rows))


async def m_buy_plan(update, context, db, uid):
    await show_plans(update.effective_message.chat_id, context, db)


async def m_all_bot(update, context, db, uid):
    msg = update.effective_message
    bots = db.get("bots", {})
    if not bots:
        await msg.reply_text("No bots have been added yet.")
        return
    rows = [[InlineKeyboardButton(f"🤖 {b.get('name', 'Bot')}"[:60], callback_data=f"botopen:{bid}")]
            for bid, b in bots.items()]
    await msg.reply_text("🤖 <b>All Bot</b>\nChoose a bot to open:", parse_mode="HTML",
                         reply_markup=inline(rows))


async def m_profile(update, context, db, uid):
    user = update.effective_user
    plan = active_plan(db, uid)
    expiry = ("\nPlan: Lifetime ♾️ (never expires)" if is_lifetime(plan)
              else f"\nPlan expires: {fmt_date(plan['expires_at'])} (UTC)") if plan else ""
    await update.effective_message.reply_text(
        f"👤 <b>My Profile</b>\n\nUsername: @{escape(user.username or 'none')}\n"
        f"User ID: <code>{uid}</code>\nHosted files: {count_files(db, uid)}\n"
        f"Hosting limit: {allowance(db, uid)}\nStatus: {('Lifetime Premium' if is_lifetime(plan) else 'Premium') if plan else 'Free'}{expiry}",
        parse_mode="HTML")


async def m_running(update, context, db, uid):
    msg = update.effective_message
    live = [fid for fid in list(PROCS) if is_running(fid)]
    if not live:
        await msg.reply_text("No scripts are running right now.")
        return
    for fid in live[:40]:
        f = db["files"].get(fid, {})
        await msg.reply_text(
            f"🟢 <b>{escape(f.get('name', 'file'))}</b>\nUser: <code>{f.get('user_id')}</code>",
            parse_mode="HTML",
            reply_markup=inline([[InlineKeyboardButton("⏹ Stop", callback_data=f"stopfile:{fid}"),
                                  InlineKeyboardButton("📜 Logs", callback_data=f"logfile:{fid}")]]))


async def m_all_users(update, context, db, uid):
    await send_user_list(update.effective_message.chat_id, context, db)


async def m_toggle_upi(update, context, db, uid):
    db["settings"]["upi_on"] = not db["settings"].get("upi_on", False)
    save_db(db)
    await update.effective_message.reply_text(
        f"UPI payments are now {'ON' if db['settings']['upi_on'] else 'OFF'}.", reply_markup=owner_menu())


async def m_toggle_bot(update, context, db, uid):
    db["settings"]["bot_on"] = not db["settings"].get("bot_on", True)
    save_db(db)
    await update.effective_message.reply_text(
        f"Bot is now {'ON' if db['settings']['bot_on'] else 'OFF'} for users.", reply_markup=owner_menu())


async def m_all_files(update, context, db, uid):
    msg = update.effective_message
    sent = 0
    for fid, f in list(db["files"].items())[-50:]:  # last 50 only, avoids flood limits
        path = Path(f.get("path", ""))
        if not path.is_file():
            continue
        try:
            with path.open("rb") as fh:
                await context.bot.send_document(
                    msg.chat_id, fh, filename=f["name"],
                    caption=f"ID {fid} • user {f['user_id']} • {f.get('status')}")
            sent += 1
            await asyncio.sleep(0.3)
        except TelegramError:
            pass
    await msg.reply_text(f"Sent {sent} stored file(s) (latest 50 max).")


async def m_manage_bots(update, context, db, uid):
    msg = update.effective_message
    bots = db.get("bots", {})
    if not bots:
        await msg.reply_text("No bot links saved yet.")
        return
    rows = [[InlineKeyboardButton(f"🗑 {b.get('name', 'Bot')}"[:60], callback_data=f"remove_bot:{bid}")]
            for bid, b in bots.items()]
    await msg.reply_text("Tap an entry to remove it:", reply_markup=inline(rows))


async def m_manage_plans(update, context, db, uid):
    msg = update.effective_message
    plans = db.get("plans", {})
    if not plans:
        await msg.reply_text("No plans created yet. Use 🧾 Plan Set.")
        return
    rows = [[InlineKeyboardButton(f"🗑 {p.get('name')} • ₹{p.get('price')} • {days_label(p.get('days', 0))} • {p.get('host_limit')} hosts"[:60],
                                  callback_data=f"plan_del:{pid}")] for pid, p in plans.items()]
    await msg.reply_text("Tap a plan to delete it (users who already bought it keep it):",
                         reply_markup=inline(rows))


async def m_force_list(update, context, db, uid):
    msg = update.effective_message
    chans = db.get("channels", [])
    if not chans:
        await msg.reply_text("No force-join channels configured.")
        return
    rows = [[InlineKeyboardButton(f"🗑 {c.get('title')}"[:60], callback_data=f"fj_del:{c.get('chat_id')}")]
            for c in chans]
    await msg.reply_text("Tap a channel to remove it from force-join:", reply_markup=inline(rows))


async def m_pending_files(update, context, db, uid):
    chat_id = update.effective_message.chat_id
    pending = [f for f in db["files"].values() if f.get("status") == "pending"]
    if not pending:
        await context.bot.send_message(chat_id, "No pending file approvals.")
    for f in pending[:30]:
        await send_file_card(context, chat_id, f)


async def m_pending_payments(update, context, db, uid):
    chat_id = update.effective_message.chat_id
    pending = [p for p in db["payments"].values() if p.get("status") == "pending_review"]
    if not pending:
        await context.bot.send_message(chat_id, "No pending payments.")
    for p in pending[:30]:
        await send_payment_card(context, chat_id, p)


MENU: dict[str, MenuFn] = {
    "upload file": m_upload,
    "check file": m_check_file,
    "buy plan": m_buy_plan,
    "manual install": ask("manual_install", "Send package/dependency name(s) and the file that needs them. The owner will review the request."),
    "all bot": m_all_bot,
    "support ticket": ask("support_ticket", "Write your support message. The owner will receive it."),
    "my profile": m_profile,
    # ---- owner only ----
    "owner panel": m_owner_panel,
    "user menu": m_user_menu,
    "running bot": m_running,
    "all user": m_all_users,
    "block user": ask("block_user", "Send numeric Telegram user ID to block."),
    "unban user": ask("unblock_user", "Send numeric Telegram user ID to unblock."),
    "set free host": ask("set_free_host", "Enter the free hosting file limit (number)."),
    "make admin": ask("make_admin", "Send numeric Telegram user ID to make admin."),
    "remove admin": ask("remove_admin", "Send numeric Telegram user ID to remove from admins."),
    "pending file": m_pending_files,
    "pending payment": m_pending_payments,
    "broadcast": ask("broadcast", "Send the message to broadcast to all users."),
    "plan set": ask("plan_name", "Enter plan name:"),
    "manage plans": m_manage_plans,
    "upi set": ask("set_upi", "Send the UPI ID to use for payments."),
    "manage upi": m_toggle_upi,
    "bot setting": m_toggle_bot,
    "all bot files": m_all_files,
    "bot add": ask("bot_add_name", "Enter a display name for the bot you want to add:"),
    "manage bots": m_manage_bots,
    "force join add": ask("forcejoin_add", "Send channel in this format:\n@channelusername | https://t.me/channelusername"),
    "force join list": m_force_list,
}
USER_KEYS = {"upload file", "check file", "buy plan", "manual install", "all bot",
             "support ticket", "my profile", "user menu"}


# ----------------------------------------------------------------------------
# Multi-step flows (sessions)
# ----------------------------------------------------------------------------
async def do_broadcast(context: ContextTypes.DEFAULT_TYPE, targets: list[str],
                       text: str, report_chat: int) -> None:
    sent = failed = 0
    for target in targets:
        for attempt in range(2):
            try:
                await context.bot.send_message(int(target), text)
                sent += 1
                break
            except RetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramError:
                failed += 1
                break
        await asyncio.sleep(0.05)  # stay under Telegram's ~30 msg/sec limit
    try:
        await context.bot.send_message(report_chat, f"📢 Broadcast complete. Sent: {sent}, failed: {failed}.",
                                       reply_markup=owner_menu())
    except TelegramError:
        pass


async def handle_session(update: Update, context: ContextTypes.DEFAULT_TYPE, db: dict,
                         uid: str, session: dict, text: str) -> bool:
    """Handle free-text input for the current step. True if the message was consumed."""
    msg = update.effective_message
    user = update.effective_user
    step = session.get("step")
    admin = is_admin(uid, db)

    # ---- user flows ----
    if step == "payment_screenshot":
        await msg.reply_text("Please send the payment screenshot as a photo. (Use /cancel to stop.)")
        return True

    if step == "payment_transaction":
        p = db["payments"].get(session.get("payment_id"))
        if not p:
            clear_session(db, uid)
            await msg.reply_text("Payment request not found. Start again from 💎 Buy Plan.")
            return True
        utr = text.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{8,30}", utr):
            await msg.reply_text("That does not look like a valid UTR / transaction ID. Send it again (letters/digits only).")
            return True
        duplicate = any(x.get("id") != p["id"] and x.get("status") in ("pending_review", "approved")
                        and str(x.get("transaction_id", "")).lower() == utr.lower()
                        for x in db["payments"].values())
        if duplicate:
            await msg.reply_text("❌ This transaction ID was already submitted. Contact support if this is a mistake.")
            return True
        p["transaction_id"] = utr
        p["status"] = "pending_review"
        save_db(db)
        clear_session(db, uid)
        await msg.reply_text("⏳ Payment submitted. Please wait for owner approval.", reply_markup=menu_for(uid, db))
        await send_to_admins(context, db, lambda aid: send_payment_card(context, aid, p))
        return True

    if step == "support_ticket":
        clear_session(db, uid)
        await msg.reply_text("🎫 Your support ticket was sent to the owner. Please wait for a reply.")
        body = (f"🎫 <b>Support ticket</b>\nUser: <code>{uid}</code>\n"
                f"Username: @{escape(user.username or 'none')}\n\n{escape(text[:3000])}")
        await send_to_admins(context, db, lambda aid: context.bot.send_message(
            aid, body, parse_mode="HTML",
            reply_markup=inline([[InlineKeyboardButton("↩️ Reply", callback_data=f"support_reply:{uid}")]])))
        return True

    if step == "manual_install":
        clear_session(db, uid)
        body = f"🧩 Manual install request from <code>{uid}</code>:\n{escape(text[:3000])}"
        await send_to_admins(context, db, lambda aid: context.bot.send_message(aid, body, parse_mode="HTML"))
        await msg.reply_text("📦 Install request sent to the owner. Please wait for review.")
        return True

    # ---- admin flows ----
    if not admin:
        return False

    if step == "support_reply":
        target = session.get("target_user")
        try:
            await context.bot.send_message(int(target), f"📩 <b>Support reply:</b>\n{escape(text)}", parse_mode="HTML")
            await msg.reply_text("Reply sent.")
        except (TelegramError, TypeError, ValueError):
            await msg.reply_text("Could not send reply to that user.")
        clear_session(db, uid)
        return True

    if step == "bot_add_name":
        set_session(db, uid, "bot_add_url", bot_name=text[:80])
        await msg.reply_text("Now send the bot link, for example https://t.me/ExampleBot, or @ExampleBot.\n"
                             "A numeric ID alone cannot open a Telegram bot; an @username/link is required.")
        return True

    if step == "bot_add_url":
        url = "https://t.me/" + text[1:] if text.startswith("@") else text
        if not url.startswith("https://t.me/"):
            await msg.reply_text("Please send a valid Telegram link such as https://t.me/ExampleBot or @ExampleBot.")
            return True
        bid = uuid.uuid4().hex[:8]
        db["bots"][bid] = {"id": bid, "name": session.get("bot_name", "Bot"), "url": url,
                           "added_by": uid, "created_at": int(time.time())}
        save_db(db)
        clear_session(db, uid)
        await msg.reply_text(f"✅ Bot added: {escape(db['bots'][bid]['name'])}\nUsers can now open it from 🤖 All Bot.",
                             parse_mode="HTML", reply_markup=owner_menu())
        return True

    if step == "forcejoin_add":
        parts = [p.strip() for p in text.split("|", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1].startswith("https://t.me/"):
            await msg.reply_text("Format:\n@channelusername | https://t.me/channelusername")
            return True
        try:
            chat = await context.bot.get_chat(parts[0])
            me = await context.bot.get_chat_member(chat.id, context.bot.id)
        except TelegramError:
            await msg.reply_text("❌ Bot cannot access that channel. Add the bot to the channel as admin and try again.")
            return True
        if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await msg.reply_text("❌ Make the bot an ADMIN in that channel first (needed for membership checks), then try again.")
            return True
        if any(str(c.get("chat_id")) == parts[0] for c in db["channels"]):
            await msg.reply_text("That channel is already in the list.")
            return True
        db["channels"].append({"chat_id": parts[0], "title": chat.title or parts[0], "url": parts[1]})
        save_db(db)
        clear_session(db, uid)
        await msg.reply_text("✅ Force-join channel added.", reply_markup=owner_menu())
        return True

    if step == "plan_name":
        set_session(db, uid, "plan_price", plan_name=text[:60])
        await msg.reply_text("Enter plan price in INR (number only):")
        return True

    if step in ("plan_price", "plan_days", "plan_limit", "set_free_host"):
        try:
            value = int(text)
        except ValueError:
            await msg.reply_text("Please enter a whole number.")
            return True
        minimum = 0 if step in ("plan_price", "plan_days", "set_free_host") else 1
        if value < minimum:
            await msg.reply_text(f"Value must be at least {minimum}.")
            return True
        if step == "plan_price":
            set_session(db, uid, "plan_days", plan_name=session["plan_name"], price=value)
            await msg.reply_text("Enter plan duration in days.\n♾️ Lifetime plan banana ho to  0  bhejo:")
        elif step == "plan_days":
            set_session(db, uid, "plan_limit", plan_name=session["plan_name"], price=session["price"], days=value)
            await msg.reply_text("Enter hosting limit for this plan:")
        elif step == "plan_limit":
            pid = uuid.uuid4().hex[:8]
            db["plans"][pid] = {"id": pid, "name": session["plan_name"], "price": session["price"],
                                "days": session["days"], "host_limit": value, "active": True}
            save_db(db)
            clear_session(db, uid)
            await msg.reply_text(
                f"✅ Plan created: {session['plan_name']} — ₹{session['price']}, {days_label(session['days'])}, {value} hosts.",
                reply_markup=owner_menu())
        else:
            db["settings"]["free_host"] = value
            save_db(db)
            clear_session(db, uid)
            await msg.reply_text(f"✅ Free hosting limit set to {value}.", reply_markup=owner_menu())
        return True

    if step == "set_upi":
        db["settings"]["upi_id"] = text[:100]
        db["settings"]["upi_on"] = True
        save_db(db)
        clear_session(db, uid)
        await msg.reply_text("✅ UPI ID saved and payments enabled.", reply_markup=owner_menu())
        return True

    if step in ("block_user", "unblock_user", "make_admin", "remove_admin"):
        if not text.isdigit():
            await msg.reply_text("Please send a numeric Telegram user ID.")
            return True
        target = text
        if step == "make_admin":
            if target not in {str(x) for x in db["admins"]} and target not in OWNER_IDS:
                db["admins"].append(target)
            result = f"✅ Admin added: {target}"
        elif step == "remove_admin":
            if is_owner(target):
                result = "⛔ The owner cannot be removed."
            else:
                db["admins"] = [x for x in db["admins"] if str(x) != target]
                result = f"✅ Admin removed (if they were an admin): {target}"
        else:
            if step == "block_user" and is_admin(target, db):
                result = "⛔ You cannot block an admin/owner."
            else:
                rec = db["users"].setdefault(target, {"id": target, "username": "", "first_name": "",
                                                      "blocked": False, "created_at": int(time.time())})
                rec["blocked"] = step == "block_user"
                result = f"✅ User {target} {'blocked' if step == 'block_user' else 'unblocked'}."
        save_db(db)
        clear_session(db, uid)
        await msg.reply_text(result, reply_markup=owner_menu())
        return True

    if step == "broadcast":
        clear_session(db, uid)
        targets = [t for t, info in db["users"].items() if not info.get("blocked")]
        await msg.reply_text(f"📢 Broadcasting to {len(targets)} users in the background…")
        context.application.create_task(do_broadcast(context, targets, text, msg.chat_id))
        return True

    return False


# ----------------------------------------------------------------------------
# Files & photos
# ----------------------------------------------------------------------------
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE, db: dict, uid: str) -> None:
    msg = update.effective_message
    doc = msg.document if msg else None
    if not doc:
        return
    session = db["sessions"].get(uid, {})
    step = session.get("step")
    name = doc.file_name or "upload.bin"

    if step == "payment_screenshot":
        is_img = (doc.mime_type or "").startswith("image/") or name.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        if not is_img:
            await msg.reply_text("Please send a payment screenshot as an image/photo.")
            return
        p = db["payments"].get(session.get("payment_id"))
        if not p:
            clear_session(db, uid)
            await msg.reply_text("Payment request not found. Start again from 💎 Buy Plan.")
            return
        p.update(screenshot_file_id=doc.file_id, screenshot_kind="document", status="awaiting_transaction")
        save_db(db)
        set_session(db, uid, "payment_transaction", payment_id=p["id"])
        await msg.reply_text("Now send the UPI transaction ID / UTR number.")
        return
    if step == "payment_transaction":
        await msg.reply_text("Please send the UPI transaction ID / UTR as text. (Use /cancel to stop.)")
        return

    if not name.lower().endswith(ALLOWED_EXTS):
        await msg.reply_text("❌ Allowed files: " + ", ".join(ALLOWED_EXTS))
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await msg.reply_text(f"❌ Maximum file size is {MAX_UPLOAD_MB} MB.")
        return
    if not is_admin(uid, db) and count_files(db, uid) >= allowance(db, uid):
        await msg.reply_text("Your hosting limit is full. Tap 💎 Buy Plan to upgrade.")
        return
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:120] or "upload.bin"
        local_path = UPLOAD_DIR / f"{uuid.uuid4().hex}_{safe_name}"
        await tg_file.download_to_drive(custom_path=local_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Download failed: %r", exc)
        await msg.reply_text("Could not download the file from Telegram. Please try again.")
        return
    fid = uuid.uuid4().hex[:10]
    db["files"][fid] = {
        "id": fid, "user_id": uid, "name": name, "path": str(local_path),
        "file_id": doc.file_id, "status": "approved" if is_admin(uid, db) else "pending",
        "size": doc.file_size or 0, "created_at": int(time.time()),
    }
    save_db(db)
    if is_admin(uid, db):
        ok, info = await start_script(context.bot, db, fid)
        await msg.reply_text("✅ File saved and auto-approved (admin).\n" + ("🟢 " if ok else "❌ ") + info)
        return
    await msg.reply_text("📥 File received. Please wait for owner approval.")
    await send_to_admins(context, db, lambda aid: send_file_card(context, aid, db["files"][fid]))


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, db: dict, uid: str) -> None:
    msg = update.effective_message
    session = db["sessions"].get(uid, {})
    if session.get("step") != "payment_screenshot" or not msg or not msg.photo:
        return
    p = db["payments"].get(session.get("payment_id"))
    if not p:
        await msg.reply_text("Payment request not found. Please start again from Buy Plan.")
        clear_session(db, uid)
        return
    p.update(screenshot_file_id=msg.photo[-1].file_id, screenshot_kind="photo", status="awaiting_transaction")
    save_db(db)
    set_session(db, uid, "payment_transaction", payment_id=p["id"])
    await msg.reply_text("Screenshot received. Now send the UPI transaction ID / UTR number.")


async def document_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return
    db = load_db()
    if ensure_user(db, update.effective_user):
        save_db(db)
    if not await gate(update, context, db):
        return
    await handle_document(update, context, db, str(update.effective_user.id))


async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return
    db = load_db()
    if ensure_user(db, update.effective_user):
        save_db(db)
    if not await gate(update, context, db):
        return
    await handle_photo(update, context, db, str(update.effective_user.id))


# ----------------------------------------------------------------------------
# Text router
# ----------------------------------------------------------------------------
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or not msg.text:
        return
    uid = str(user.id)
    text = msg.text.strip()
    db = load_db()
    if ensure_user(db, user):
        save_db(db)
    if not await gate(update, context, db):
        return

    key = norm(text)
    if key in MENU:
        # Pressing a menu button always cancels any half-finished step.
        if uid in db["sessions"]:
            clear_session(db, uid)
        if key not in USER_KEYS and not is_admin(uid, db):
            await msg.reply_text("⛔ Owner-only section.")
            return
        await MENU[key](update, context, db, uid)
        return

    session = db["sessions"].get(uid)
    if session and await handle_session(update, context, db, uid, session, text):
        return
    await msg.reply_text("Please use the menu buttons or /start.", reply_markup=menu_for(uid, db))


# ----------------------------------------------------------------------------
# Callback router
# ----------------------------------------------------------------------------
async def file_action(q, context, db: dict, uid: str, action: str, fid: str) -> None:
    chat_id = q.message.chat_id
    send = context.bot.send_message
    f = db["files"].get(fid)
    if not f:
        await send(chat_id, "File not found.")
        return
    if str(f.get("user_id")) != uid and not is_admin(uid, db):
        await send(chat_id, "Not allowed.")
        return
    logs_btn = InlineKeyboardButton("📜 Logs", callback_data=f"logfile:{fid}")
    stop_btn = InlineKeyboardButton("⏹ Stop", callback_data=f"stopfile:{fid}")
    run_btn = InlineKeyboardButton("▶️ Run", callback_data=f"runfile:{fid}")
    restart_btn = InlineKeyboardButton("🔄 Restart", callback_data=f"restartfile:{fid}")

    if action == "fileinfo":
        running = is_running(fid)
        rows = []
        if f.get("status") == "approved":
            rows.append([stop_btn, restart_btn] if running else [run_btn])
            rows.append([logs_btn])
        if f.get("status") == "deleted":
            rows.append([InlineKeyboardButton("♻️ Restore", callback_data=f"restorefile:{fid}")])
        else:
            rows.append([InlineKeyboardButton("🗑 Delete", callback_data=f"deletefile:{fid}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
        await send(
            chat_id,
            f"📄 <b>{escape(f['name'])}</b>\nStatus: {escape(f.get('status', 'unknown'))}\n"
            f"Runtime: {'🟢 running' if running else '⚪ stopped'}",
            parse_mode="HTML", reply_markup=inline(rows))

    elif action == "runfile":
        if f.get("status") != "approved":
            await send(chat_id, "Only approved files can run.")
            return
        await send(chat_id, "⏳ Starting your script…")
        ok, info = await start_script(context.bot, db, fid)
        await send(chat_id, ("🟢 " if ok else "❌ ") + info,
                   reply_markup=inline([[stop_btn, logs_btn]]) if ok else None)

    elif action == "restartfile":
        if f.get("status") != "approved":
            await send(chat_id, "Only approved files can run.")
            return
        await send(chat_id, "🔄 Restarting…")
        await stop_script(fid)
        ok, info = await start_script(context.bot, db, fid)
        await send(chat_id, ("🟢 " if ok else "❌ ") + info,
                   reply_markup=inline([[stop_btn, logs_btn]]) if ok else None)

    elif action == "stopfile":
        stopped = await stop_script(fid)
        await send(chat_id, "⏹ Script stopped." if stopped else "This script is not running.")

    elif action == "logfile":
        text = tail_log(fid).strip() or "(no output yet)"
        await send(chat_id, f"📜 <b>Logs:</b> {escape(f['name'])}\n<pre>{escape(text)}</pre>", parse_mode="HTML")

    elif action == "restorefile":
        f["status"] = "approved"
        save_db(db)
        await send(chat_id, "♻️ File restored. Tap ▶️ Run in 📂 Check File to start it.")

    elif action == "deletefile":
        await stop_script(fid)
        f["status"] = "deleted"
        save_db(db)
        shutil.rmtree(RUN_DIR / fid, ignore_errors=True)
        await send(chat_id, "🗑 File removed from your list. The original upload stays on disk for owner review.")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.from_user or not q.message:
        return
    await q.answer()
    db = load_db()
    uid = str(q.from_user.id)
    chat_id = q.message.chat_id
    data = q.data or ""
    action, _, arg = data.partition(":")
    if ensure_user(db, q.from_user):
        save_db(db)
    if not await gate(update, context, db, check_join=(action != "verify_join")):
        return
    admin = is_admin(uid, db)
    send = context.bot.send_message

    if action == "verify_join":
        if await joined_all(q.from_user.id, context, db):
            await send_home(update, context, db)
        else:
            await ask_join(chat_id, context, db)

    elif action == "home":
        await send(chat_id, "Home menu:", reply_markup=menu_for(uid, db))

    elif action == "show_plans":
        await show_plans(chat_id, context, db)

    elif action == "plan":
        plan = db["plans"].get(arg)
        if not plan or not plan.get("active", True):
            await send(chat_id, "Plan not found.")
            return
        if not db["settings"].get("upi_on") or not db["settings"].get("upi_id"):
            await send(chat_id, "Payment is currently unavailable. Please contact support.")
            return
        # Drop this user's earlier unfinished payment attempts.
        for pid in [k for k, v in db["payments"].items() if str(v.get("user_id")) == uid
                    and v.get("status") in ("awaiting_screenshot", "awaiting_transaction")]:
            db["payments"].pop(pid, None)
        payid = uuid.uuid4().hex[:10]
        db["payments"][payid] = {
            "id": payid, "user_id": uid, "plan_id": arg, "plan_name": plan["name"],
            "amount": int(plan["price"]), "host_limit": int(plan["host_limit"]),
            "days": int(plan["days"]), "status": "awaiting_screenshot",
            "created_at": int(time.time()), "expires_at": 0,
            "transaction_id": "", "screenshot_file_id": "",
        }
        set_session(db, uid, "payment_screenshot", payment_id=payid)  # also saves db
        await send(
            chat_id,
            f"💎 <b>{escape(plan['name'])}</b>\n💰 Amount: ₹{int(plan['price'])}\n"
            f"🏦 UPI ID: <code>{escape(db['settings']['upi_id'])}</code>\n\n"
            "Pay using UPI, then send the payment screenshot here. Admin approval is required.\n"
            "(Use /cancel to stop.)", parse_mode="HTML")

    elif action in ("approve_file", "reject_file"):
        if not admin:
            await send(chat_id, "Not allowed.")
            return
        f = db["files"].get(arg)
        if not f or f.get("status") != "pending":
            await send(chat_id, "Request is no longer pending.")
            return
        approved = action == "approve_file"
        f["status"] = "approved" if approved else "rejected"
        f["reviewed_at"] = int(time.time())
        save_db(db)
        run_note = ""
        if approved:
            ok, info = await start_script(context.bot, db, arg)
            run_note = "\n🟢 Started automatically." if ok else f"\n⚠️ Could not start: {info.splitlines()[0]}"
            user_msg = ("✅ Your file was approved and is now running 🟢\nManage it from 📂 Check File."
                        if ok else f"✅ Your file was approved, but it could not start:\n{info}\n\n"
                                   "Fix it, or tap ▶️ Run in 📂 Check File to retry.")
        else:
            user_msg = "❌ Your file upload was rejected."
        try:
            await send(int(f["user_id"]), user_msg)
        except TelegramError:
            pass
        await edit_msg(q, ("✅ Approved" if approved else "❌ Rejected") + f"\nFile: {escape(f['name'])}" + run_note)

    elif action in ("approve_pay", "reject_pay"):
        if not admin:
            await send(chat_id, "Not allowed.")
            return
        p = db["payments"].get(arg)
        if not p or p.get("status") != "pending_review":
            await send(chat_id, "Payment is no longer pending.")
            return
        approved = action == "approve_pay"
        if approved:
            p["status"] = "approved"
            if int(p["days"]) <= 0:
                p["lifetime"] = True
                p["expires_at"] = LIFETIME_EXPIRES
                note = f"✅ Payment approved! ♾️ Lifetime plan {p['plan_name']} is now active. It never expires."
            else:
                p["expires_at"] = int(time.time()) + int(p["days"]) * 86400
                note = f"✅ Payment approved! Plan {p['plan_name']} is active for {p['days']} days."
        else:
            p["status"] = "rejected"
            note = "❌ Payment rejected. Please contact support if you think this is a mistake."
        save_db(db)
        try:
            await send(int(p["user_id"]), note)
        except TelegramError:
            pass
        await edit_msg(q, "✅ Payment approved" if approved else "❌ Payment rejected")

    elif action == "botopen":
        bot = db.get("bots", {}).get(arg)
        if bot and bot.get("url"):
            await send(chat_id, f"Opening <b>{escape(bot['name'])}</b>:\n{escape(bot['url'])}", parse_mode="HTML",
                       reply_markup=inline([[InlineKeyboardButton("🤖 Open Bot", url=bot["url"])]]))
        else:
            await send(chat_id, "This bot link is no longer available.")

    elif action == "remove_bot" and admin:
        bot = db.get("bots", {}).pop(arg, None)
        save_db(db)
        await send(chat_id, f"Removed: {bot.get('name')}" if bot else "Bot entry not found.")

    elif action == "plan_del" and admin:
        plan = db["plans"].pop(arg, None)
        save_db(db)
        await send(chat_id, f"🗑 Plan deleted: {plan.get('name')}" if plan else "Plan not found.")

    elif action == "fj_del" and admin:
        before = len(db["channels"])
        db["channels"] = [c for c in db["channels"] if str(c.get("chat_id")) != arg]
        save_db(db)
        await send(chat_id, "🗑 Channel removed." if len(db["channels"]) < before else "Channel not found.")

    elif action in ("block", "unblock") and admin:
        if arg in db["users"]:
            if action == "block" and is_admin(arg, db):
                await send(chat_id, "⛔ You cannot block an admin/owner.")
                return
            db["users"][arg]["blocked"] = action == "block"
            save_db(db)
            await send(chat_id, f"User {arg}: {'blocked' if action == 'block' else 'unblocked'}.")

    elif action == "support_reply" and admin:
        set_session(db, uid, "support_reply", target_user=arg)
        await send(chat_id, f"Type your reply for user {arg}:")

    elif action == "userdetail" and admin:
        u = db["users"].get(arg, {})
        rows = [[InlineKeyboardButton("🚫 Ban", callback_data=f"block:{arg}"),
                 InlineKeyboardButton("🔓 Unban", callback_data=f"unblock:{arg}")],
                [InlineKeyboardButton("⬅️ Back to users", callback_data="owner_users")]]
        await send(chat_id,
                   f"👤 <b>User details</b>\nID: <code>{escape(arg)}</code>\n"
                   f"Username: @{escape(u.get('username') or 'none')}\n"
                   f"Hosted files: {count_files(db, arg)}\n"
                   f"Status: {'BLOCKED' if u.get('blocked') else 'Active'}",
                   parse_mode="HTML", reply_markup=inline(rows))

    elif action == "owner_users" and admin:
        await send_user_list(chat_id, context, db)

    elif action == "owner_pending_files" and admin:
        await m_pending_files(update, context, db, uid)

    elif action == "owner_pending_payments" and admin:
        await m_pending_payments(update, context, db, uid)

    elif action in ("fileinfo", "runfile", "restartfile", "stopfile", "logfile", "restorefile", "deletefile"):
        await file_action(q, context, db, uid, action, arg)


# ----------------------------------------------------------------------------
# Errors / startup
# ----------------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        log.error("❌ Is token se bot kahin aur bhi chal raha hai (doosra terminal/server/panel). "
                  "Baaki sab band karo, sirf ek jagah chalao.")
        return
    log.error("Unhandled error while processing update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        text = "⚠️ Something went wrong. Please try again."
        try:
            if update.effective_user and is_admin(update.effective_user.id, load_db()):
                err = context.error
                text += f"\n\n🔧 Admin debug:\n{type(err).__name__}: {str(err)[:300]}"
        except Exception:  # noqa: BLE001
            pass
        try:
            await context.bot.send_message(update.effective_chat.id, text)
        except TelegramError:
            pass


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("❌ Bot token nahi daala. File ke upar BOT_TOKEN_HERE mein @BotFather wala token paste karo.")
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
        raise SystemExit("❌ Bot token ka format galat hai. Poora token copy karo (123456789:ABC...), bina space/quotes ke.")
    if not OWNER_IDS:
        raise SystemExit("❌ Admin ID nahi daali. File ke upar OWNER_IDS_HERE mein apni numeric Telegram ID daalo.")
    app = (Application.builder().token(BOT_TOKEN)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("lifetime", cmd_lifetime))
    app.add_handler(CommandHandler("removelifetime", cmd_removelifetime))
    app.add_handler(CommandHandler("lifetimelist", cmd_lifetimelist))
    app.add_handler(CommandHandler("setlifetimelimit", cmd_setlifetimelimit))
    app.add_handler(CommandHandler("planinfo", cmd_planinfo))
    app.add_handler(CommandHandler("myplan", cmd_myplan))
    app.add_handler(CommandHandler("lifetimehelp", cmd_lifetimehelp))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, document_router))
    app.add_handler(MessageHandler(filters.PHOTO, photo_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)
    log.info("Premium Hosting Bot %s (@%s) started from %s. Press Ctrl+C to stop.", BOT_VERSION, BOT_USERNAME, Path(__file__).resolve())
    try:                                  # naye Python (3.12+) par event loop pehle se nahi hota
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        raise SystemExit("❌ Telegram ne token reject kar diya. @BotFather mein token check karo (revoke kiya ho to naya daalo).")
    except NetworkError as exc:
        raise SystemExit(f"❌ Telegram se connect nahi ho paya (internet/server block?): {exc}")


if __name__ == "__main__":
    main()
