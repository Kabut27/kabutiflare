"""
KabutiFlare — Telegram Bot + Mini App
Manage Cloudflare DNS, SSL, Workers, Page Rules & Email Routing from Telegram.
"""

import os
import json
import logging
import socket
import requests
import asyncio
import io
from datetime import datetime
from functools import wraps
try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    WebAppInfo, BotCommand, MenuButtonWebApp
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler,
    filters
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CONFIG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
CF_API = "https://api.cloudflare.com/client/v4"
ADMIN_ID = 474008580
ALLOWED_USERS = {474008580, 5069084099}
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
GITHUB_REPO = "https://raw.githubusercontent.com/Kabut27/kabutiflare/main"

# Deploy access: only users in this set can use /deploy
# Admin can manage this list via /grantdeploy and /revokedeploy
DEPLOY_ALLOWED_USERS: set = {474008580}  # admin always has access

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is required. Set via environment variable or .env file.")

# Brand divider used across messages for a consistent, polished look
DIV = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"

def webapp_btn(text, zid=None):
    """Return WebApp button only if WEBAPP_URL is set, else None."""
    if not WEBAPP_URL:
        return None
    url = f"{WEBAPP_URL}?zone={zid}" if zid else WEBAPP_URL
    return InlineKeyboardButton(text, web_app=WebAppInfo(url=url))

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FORCE IPv4 GLOBALLY (fixes Cloudflare token/IPv6 edge cases)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_orig_getaddrinfo = socket.getaddrinfo

def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

socket.getaddrinfo = _ipv4_getaddrinfo

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CONVERSATION STATES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONNECT_METHOD, CONNECT_EMAIL, CONNECT_KEY = 0, 1, 2
ADD_TYPE, ADD_NAME, ADD_CONTENT, ADD_PROXY = 10, 11, 12, 13
EDIT_VALUE = 50
BROADCAST_MSG = 60
PR_URL, PR_ACTION = 70, 71
WK_CODE = 80
# Deploy flow uses plain text replies for every step (1/2 instead of
# inline buttons) so the conversation can never lose a step to a stray
# callback handler — this is the #1 cause of "deploy skips a step" bugs.
DEP_HOST, DEP_PORT, DEP_USER, DEP_AUTH, DEP_PASS, DEP_KEY, DEP_BOTTOKEN, DEP_WEBAPP, DEP_CONFIRM = 90, 91, 92, 93, 94, 95, 96, 97, 98

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  USER DATABASE (persistent JSON)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def load_db():
    try:
        with open(DB_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"users": {}, "cf_logins": {}}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def track_user(uid, name="", username=""):
    db = load_db()
    uid_str = str(uid)
    if uid_str not in db["users"]:
        db["users"][uid_str] = {
            "name": name, "username": username,
            "first_seen": datetime.now().isoformat(),
            "last_seen": datetime.now().isoformat()
        }
    else:
        db["users"][uid_str]["last_seen"] = datetime.now().isoformat()
        if name: db["users"][uid_str]["name"] = name
        if username: db["users"][uid_str]["username"] = username
    save_db(db)

def track_cf_login(uid, name=""):
    db = load_db()
    uid_str = str(uid)
    db["cf_logins"][uid_str] = {
        "name": name,
        "last_login": datetime.now().isoformat()
    }
    save_db(db)

def is_admin(uid):
    return uid == ADMIN_ID

def has_deploy_access(uid):
    """Returns True if the user is allowed to use /deploy."""
    return uid in DEPLOY_ALLOWED_USERS

def grant_deploy(uid):
    DEPLOY_ALLOWED_USERS.add(uid)

def revoke_deploy(uid):
    DEPLOY_ALLOWED_USERS.discard(uid)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  IN-MEMORY SESSIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
sessions = {}
def get_s(uid): return sessions.get(uid, {})
def set_s(uid, data):
    if uid not in sessions: sessions[uid] = {}
    sessions[uid].update(data)
def del_s(uid): sessions.pop(uid, None)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLOUDFLARE API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def cf_h(uid):
    s = get_s(uid)
    if s.get("auth") == "token":
        return {"Authorization": f"Bearer {s['key']}", "Content-Type": "application/json"}
    return {"X-Auth-Email": s.get("email", ""), "X-Auth-Key": s.get("key", ""), "Content-Type": "application/json"}

def get_account_id(uid=None, zone=None):
    if zone and zone.get("account", {}).get("id"):
        return zone["account"]["id"]
    return ""

def cf_get(uid, path, params=None):
    r = requests.get(f"{CF_API}{path}", headers=cf_h(uid), params=params, timeout=15)
    d = r.json()
    if not d.get("success"):
        raise Exception(", ".join(e.get("message", "") for e in d.get("errors", [])) or "Unknown error")
    return d

def cf_post(uid, path, body):
    r = requests.post(f"{CF_API}{path}", headers=cf_h(uid), json=body, timeout=15)
    d = r.json()
    if not d.get("success"):
        raise Exception(", ".join(e.get("message", "") for e in d.get("errors", [])) or "Unknown error")
    return d

def cf_put(uid, path, body):
    r = requests.put(f"{CF_API}{path}", headers=cf_h(uid), json=body, timeout=15)
    d = r.json()
    if not d.get("success"):
        raise Exception(", ".join(e.get("message", "") for e in d.get("errors", [])) or "Unknown error")
    return d

def cf_del(uid, path):
    r = requests.delete(f"{CF_API}{path}", headers=cf_h(uid), timeout=15)
    d = r.json()
    if not d.get("success"):
        raise Exception(", ".join(e.get("message", "") for e in d.get("errors", [])) or "Unknown error")
    return d

def cf_patch(uid, path, body):
    r = requests.patch(f"{CF_API}{path}", headers=cf_h(uid), json=body, timeout=15)
    return r.json()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AUTH GUARD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def need_auth(func):
    @wraps(func)
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid not in ALLOWED_USERS:
            msg = update.message or update.callback_query.message
            await msg.reply_text("⛔ <b>Access denied.</b>\n\nYou're not authorized to use this bot.", parse_mode="HTML")
            return
        if not get_s(uid).get("key"):
            msg = update.message or update.callback_query.message
            await msg.reply_text(
                "🔒 <b>Not connected yet</b>\n\nConnect your Cloudflare account first to continue.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔐 Connect to Cloudflare", callback_data="do_connect")
                ]])
            )
            return
        return await func(update, ctx)
    return w

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /start
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("⛔ <b>Access denied.</b>", parse_mode="HTML")
        return
    name = update.effective_user.first_name or "there"
    username = update.effective_user.username or ""
    track_user(uid, name, username)
    logged = bool(get_s(uid).get("key"))

    text = (
        f"╭─❀ <b>KABUTIFLARE</b> ❀─╮\n"
        f"<i>Cloudflare control, right in Telegram</i>\n"
        f"{DIV}\n\n"
        f"👋 Welcome back, <b>{name}</b>!\n\n"
    )

    if logged:
        text += "🟢 <b>Status:</b> Connected to Cloudflare\n\nChoose an action below:"
        btns = [
            [InlineKeyboardButton("🌐 My Domains", callback_data="do_domains"),
             InlineKeyboardButton("📊 Analytics", callback_data="do_analytics")],
        ]
        if WEBAPP_URL:
            btns.append([webapp_btn("🖥 Open Dashboard")])
        btns.append([
            InlineKeyboardButton("📖 Help", callback_data="do_help"),
            InlineKeyboardButton("🔌 Disconnect", callback_data="do_disconnect"),
        ])
        if has_deploy_access(uid):
            btns.append([InlineKeyboardButton("🚀 Deploy to Server", callback_data="do_deploy")])
    else:
        text += "🔴 <b>Status:</b> Not connected\n\nConnect your Cloudflare account to get started:"
        btns = [
            [InlineKeyboardButton("🔐 Connect to Cloudflare", callback_data="do_connect")],
            [InlineKeyboardButton("📖 Help", callback_data="do_help")],
        ]
        if has_deploy_access(uid):
            btns.append([InlineKeyboardButton("🚀 Deploy to Server", callback_data="do_deploy")])

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /help
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def send_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    if update.callback_query:
        await update.callback_query.answer()

    text = (
        "╭─❀ <b>HELP & GUIDE</b> ❀─╮\n"
        f"{DIV}\n\n"
        "🔹 <b>Commands</b>\n"
        "/connect — Connect to Cloudflare\n"
        "/domains — List your domains\n"
        "/dns — Manage DNS records\n"
        "/analytics — View traffic analytics\n"
        "/disconnect — Disconnect account\n"
        "/deploy — Install bot on a server\n\n"
        "🔹 <b>Features</b>\n"
        "📋 DNS — View · Add · Edit · Delete records\n"
        "🔒 SSL/TLS — Switch encryption modes\n"
        "📛 Nameservers — View NS records\n"
        "🌐 Zone Settings — HTTPS, TLS 1.3, HTTP/3, Brotli…\n"
        "🔀 Page Rules — Create redirects & rules\n"
        "👷 Workers — Upload, edit & manage scripts\n"
        "📧 Email Routing — Manage forwarding rules\n"
        "📊 Analytics — Traffic, cache, threats (24h)\n"
        "🖥 Dashboard — Full Mini App experience\n\n"
        "🔹 <b>Getting an API Token</b>\n"
        "1️⃣ dash.cloudflare.com → My Profile → API Tokens\n"
        "2️⃣ Create Custom Token\n"
        "3️⃣ Grant these permissions:\n"
        "   <code>Zone · DNS · Edit</code>\n"
        "   <code>Zone · Zone Settings · Edit</code>\n"
        "   <code>Zone · SSL and Certificates · Edit</code>\n"
        "   <code>Zone · Page Rules · Edit</code>\n"
        "   <code>Zone · Workers Routes · Edit</code>\n"
        "   <code>Zone · Email Routing Rules · Edit</code>\n"
        "   <code>Account · Workers Scripts · Edit</code>\n"
        "4️⃣ Zone Resources → All zones\n"
        "5️⃣ Copy your token and send it here\n"
    )
    btns = [[InlineKeyboardButton("🔐 Connect Now", callback_data="do_connect")]]
    if update.callback_query:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    else:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /connect
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def connect_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
    else:
        msg = update.message

    btns = [
        [InlineKeyboardButton("🛡 API Token  (Recommended)", callback_data="m_token")],
        [InlineKeyboardButton("🔑 Global API Key + Email", callback_data="m_apikey")],
        [InlineKeyboardButton("❌ Cancel", callback_data="m_cancel")],
    ]
    text = (
        "╭─❀ <b>CONNECT TO CLOUDFLARE</b> ❀─╮\n"
        f"{DIV}\n\n"
        "Choose how you'd like to authenticate:"
    )

    if update.callback_query:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        except Exception:
            await msg.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    else:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    return CONNECT_METHOD

async def connect_method(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "m_cancel":
        await q.message.edit_text("❌ Cancelled.")
        return ConversationHandler.END
    if q.data == "m_token":
        ctx.user_data["auth"] = "token"
        await q.message.edit_text(
            "🛡 <b>API Token</b>\n\nPaste your API Token below:\n\n<i>⚠️ Your message will be deleted immediately for security.</i>",
            parse_mode="HTML")
        return CONNECT_KEY
    if q.data == "m_apikey":
        ctx.user_data["auth"] = "apikey"
        await q.message.edit_text(
            "🔑 <b>Global API Key</b>\n\nFirst, send your Cloudflare <b>email address</b>:", parse_mode="HTML")
        return CONNECT_EMAIL
    return CONNECT_METHOD

async def connect_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["email"] = update.message.text.strip()
    await update.message.reply_text(
        f"📧 Email: <code>{ctx.user_data['email']}</code>\n\n"
        "Now send your <b>Global API Key</b>:\n\n<i>⚠️ Your message will be deleted immediately for security.</i>",
        parse_mode="HTML")
    return CONNECT_KEY

async def connect_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    key = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    method = ctx.user_data.get("auth", "token")
    email = ctx.user_data.get("email", "")
    set_s(uid, {"key": key, "auth": method, "email": email})

    wait = await update.effective_chat.send_message("⏳ Verifying your credentials...")
    try:
        if method == "token":
            cf_get(uid, "/user/tokens/verify")
        else:
            cf_get(uid, "/zones", {"per_page": 1})

        track_cf_login(uid, update.effective_user.first_name or "")
        _btns = [[InlineKeyboardButton("🌐 View My Domains", callback_data="do_domains")]]
        if WEBAPP_URL:
            _btns.append([webapp_btn("📊 Open Dashboard")])
        await wait.edit_text(
            "✅ <b>Connected successfully!</b>\n\nYour Cloudflare account is now linked.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(_btns))
    except Exception as e:
        del_s(uid)
        await wait.edit_text(
            f"❌ <b>Connection failed</b>\n\n<code>{e}</code>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Try Again", callback_data="do_connect")]
            ]))
    return ConversationHandler.END

async def connect_cancel_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /domains
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@need_auth
async def cmd_domains(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    uid = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()

    wait = await msg.reply_text("⏳ Fetching your domains...")
    try:
        zones = []
        page = 1
        while True:
            d = cf_get(uid, "/zones", {"per_page": 50, "page": page})
            zones.extend(d["result"])
            if page >= d.get("result_info", {}).get("total_pages", 1):
                break
            page += 1

        set_s(uid, {"zones": zones})
        if not zones:
            await wait.edit_text("📭 No domains found in this Cloudflare account.")
            return

        ico = {"active": "🟢", "pending": "🟡", "moved": "🔴", "deactivated": "🔴"}
        text = f"╭─❀ <b>YOUR DOMAINS</b> ❀─╮\n{DIV}\n\n🌐 <b>{len(zones)}</b> domain(s) found\n"
        btns = []
        for z in zones:
            i = ico.get(z["status"], "⚪")
            plan = z.get("plan", {}).get("name", "Free")
            btns.append([InlineKeyboardButton(f"{i} {z['name']}  •  {plan}", callback_data=f"zone_{z['id']}")])
        _last = [InlineKeyboardButton("🔄 Refresh", callback_data="do_domains")]
        if WEBAPP_URL:
            _last.append(webapp_btn("📊 Dashboard"))
        btns.append(_last)
        await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ZONE OVERVIEW
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def zone_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("zone_", "")
    s = get_s(uid)
    zone = next((z for z in s.get("zones", []) if z["id"] == zid), None)
    if not zone:
        await q.message.edit_text("❌ Domain not found. Please refresh.")
        return
    set_s(uid, {"cur_zone": zone})
    st = {"active": "🟢 Active", "pending": "🟡 Pending", "moved": "🔴 Moved"}
    ns = "\n".join(f"   <code>{n}</code>" for n in zone.get("name_servers", []))
    plan_name = zone.get('plan', {}).get('name', 'Free')
    text = (
        f"╭─❀ <b>{zone['name']}</b> ❀─╮\n{DIV}\n\n"
        f"📍 Status: {st.get(zone['status'], zone['status'])}\n"
        f"💳 Plan: <b>{plan_name}</b>\n\n"
        f"📛 <b>Nameservers:</b>\n{ns}\n"
    )
    btns = [
        [InlineKeyboardButton("📋 DNS Records", callback_data=f"dns_{zid}"),
         InlineKeyboardButton("➕ Add Record", callback_data=f"add_{zid}")],
        [InlineKeyboardButton("🔒 SSL/TLS", callback_data=f"ssl_{zid}"),
         InlineKeyboardButton("📛 Nameservers", callback_data=f"ns_{zid}")],
        [InlineKeyboardButton("🌐 Zone Settings", callback_data=f"zs_{zid}"),
         InlineKeyboardButton("🔀 Page Rules", callback_data=f"pr_{zid}")],
        [InlineKeyboardButton("👷 Workers", callback_data=f"wk_{zid}"),
         InlineKeyboardButton("📧 Email Routing", callback_data=f"em_{zid}")],
        [InlineKeyboardButton("📊 Analytics", callback_data=f"analytics_{zid}")],
    ]
    if WEBAPP_URL:
        btns.append([webapp_btn("🖥 Open in Dashboard", zid)])
    btns.append([InlineKeyboardButton("🔙 All Domains", callback_data="do_domains")])
    await q.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DNS LIST
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TYPE_ICON = {"A": "🔵", "AAAA": "🟣", "CNAME": "🟢", "MX": "🟡", "TXT": "🩷", "NS": "🔷", "SRV": "🟠", "CAA": "🟤"}

async def dns_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("dns_", "")
    s = get_s(uid)
    zone = s.get("cur_zone", {})

    wait = await q.message.edit_text("⏳ Fetching DNS records...")
    try:
        recs = []
        page = 1
        while True:
            d = cf_get(uid, f"/zones/{zid}/dns_records", {"per_page": 100, "page": page})
            recs.extend(d["result"])
            if page >= d.get("result_info", {}).get("total_pages", 1):
                break
            page += 1
        set_s(uid, {"dns": recs})

        if not recs:
            await wait.edit_text(
                f"📭 No DNS records for <b>{zone.get('name','')}</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Record", callback_data=f"add_{zid}")],
                    [InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")],
                ])
            )
            return

        counts = {}
        for r in recs:
            counts[r["type"]] = counts.get(r["type"], 0) + 1

        text = f"╭─❀ <b>DNS — {zone.get('name','')}</b> ❀─╮\n{DIV}\n\n📋 <b>{len(recs)}</b> record(s)\n\n"
        fbtns = []
        row = []
        for t, c in sorted(counts.items()):
            icon = TYPE_ICON.get(t, "⚪")
            text += f"{icon} <b>{t}</b> — {c}\n"
            row.append(InlineKeyboardButton(f"{icon} {t} ({c})", callback_data=f"ft_{zid}_{t}"))
            if len(row) >= 2:
                fbtns.append(row); row = []
        if row: fbtns.append(row)

        fbtns.append([InlineKeyboardButton("➕ Add Record", callback_data=f"add_{zid}")])
        fbtns.append([InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")])
        await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(fbtns))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DNS FILTER (by type)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def dns_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    parts = q.data.split("_"); zid, rtype = parts[1], parts[2]
    s = get_s(uid); zone = s.get("cur_zone", {})
    recs = [r for r in s.get("dns", []) if r["type"] == rtype]
    icon = TYPE_ICON.get(rtype, "⚪")
    text = f"{icon} <b>{rtype} Records — {zone.get('name','')}</b>\n{len(recs)} record(s)\n\n"
    btns = []
    for r in recs[:20]:
        nm = r["name"].replace(f".{zone.get('name','')}", "") or "@"
        ct = r["content"][:25] + ("…" if len(r["content"]) > 25 else "")
        px = "☁️" if r.get("proxied") else "🔘"
        text += f"<code>{nm}</code> → <code>{ct}</code> {px}\n"
        btns.append([InlineKeyboardButton(f"✏️ {nm} → {ct}", callback_data=f"ed_{r['id']}")])
    btns.append([InlineKeyboardButton("🔙 All Records", callback_data=f"dns_{zid}"), InlineKeyboardButton("➕ Add", callback_data=f"add_{zid}")])
    await q.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ADD DNS RECORD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    zid = q.data.replace("add_", ""); ctx.user_data["add_zid"] = zid
    types = ["A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "CAA"]
    btns = []; row = []
    for t in types:
        row.append(InlineKeyboardButton(t, callback_data=f"at_{t}"))
        if len(row) >= 4: btns.append(row); row = []
    if row: btns.append(row)
    btns.append([InlineKeyboardButton("❌ Cancel", callback_data=f"zone_{zid}")])
    await q.message.edit_text("➕ <b>New DNS Record</b>\n\nSelect a record type:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    return ADD_TYPE

async def add_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["add_type"] = q.data.replace("at_", "")
    t = ctx.user_data["add_type"]
    ph = {"A": "@ or www", "AAAA": "@ or www", "CNAME": "www", "MX": "@", "TXT": "@ or _dmarc", "NS": "sub", "SRV": "_sip._tcp", "CAA": "@"}
    await q.message.edit_text(f"➕ <b>{t} Record</b>\n\nSend the <b>name</b>:\n<i>{ph.get(t,'')} — root = @</i>", parse_mode="HTML")
    return ADD_NAME

async def add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["add_name"] = update.message.text.strip()
    t = ctx.user_data["add_type"]
    hints = {"A": "<code>1.2.3.4</code>", "AAAA": "<code>2001:db8::1</code>", "CNAME": "<code>example.com</code>", "MX": "<code>mail.example.com</code>", "TXT": "<code>v=spf1 ...</code>", "NS": "<code>ns1.example.com</code>", "SRV": "<code>priority weight port target</code>", "CAA": '<code>0 issue "letsencrypt.org"</code>'}
    await update.message.reply_text(f"✅ Name: <code>{ctx.user_data['add_name']}</code>\n\n<b>Value:</b>\n{hints.get(t,'')}", parse_mode="HTML")
    return ADD_CONTENT

async def add_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["add_content"] = update.message.text.strip()
    t = ctx.user_data["add_type"]
    if t in ("A", "AAAA", "CNAME"):
        await update.message.reply_text(f"✅ Value: <code>{ctx.user_data['add_content']}</code>\n\nProxy status:", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("☁️ Proxied", callback_data="px_on"), InlineKeyboardButton("🔘 DNS Only", callback_data="px_off")]]))
        return ADD_PROXY
    else:
        ctx.user_data["add_proxy"] = False
        return await do_add_submit(update, ctx)

async def add_proxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["add_proxy"] = q.data == "px_on"
    return await do_add_submit(update, ctx)

async def do_add_submit(update, ctx):
    if update.callback_query: msg = update.callback_query.message; uid = update.callback_query.from_user.id
    else: msg = update.message; uid = update.effective_user.id
    zid = ctx.user_data["add_zid"]; t = ctx.user_data["add_type"]; name = ctx.user_data["add_name"]
    content = ctx.user_data["add_content"]; proxied = ctx.user_data.get("add_proxy", False)
    s = get_s(uid); zone = s.get("cur_zone", {})
    if name == "@": name = zone.get("name", name)
    elif not name.endswith(zone.get("name", "")): name = f"{name}.{zone.get('name', '')}"
    body = {"type": t, "name": name, "content": content, "ttl": 1}
    if t in ("A", "AAAA", "CNAME"): body["proxied"] = proxied
    wait = await msg.reply_text("⏳ Creating record...")
    try:
        cf_post(uid, f"/zones/{zid}/dns_records", body)
        px_txt = "☁️ Proxied" if proxied else "🔘 DNS Only"
        await wait.edit_text(f"✅ <b>Record created!</b>\n\n<code>{t}</code> | <code>{name}</code>\n→ <code>{content}</code> {px_txt}", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 View Records", callback_data=f"dns_{zid}"), InlineKeyboardButton("➕ Add Another", callback_data=f"add_{zid}")]]))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")
    return ConversationHandler.END

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EDIT / DELETE DNS RECORD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def edit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; rid = q.data.replace("ed_", "")
    s = get_s(uid); rec = next((r for r in s.get("dns", []) if r["id"] == rid), None)
    zid = s.get("cur_zone", {}).get("id", "")
    if not rec: await q.message.edit_text("❌ Not found."); return
    ctx.user_data["edit_rec"] = rec
    px_txt = "☁️ Proxied" if rec.get("proxied") else "🔘 DNS Only"
    ttl = "Auto" if rec.get("ttl") == 1 else f"{rec['ttl']}s"
    text = f"✏️ <b>Edit Record</b>\n\n<code>{rec['type']}</code> | <code>{rec['name']}</code>\n→ <code>{rec['content']}</code>\nTTL: {ttl} | {px_txt}"
    btns = [[InlineKeyboardButton("📝 Change Value", callback_data=f"ec_{rid}")]]
    if rec["type"] in ("A", "AAAA", "CNAME"):
        toggle = "Disable" if rec.get("proxied") else "Enable"
        btns.append([InlineKeyboardButton(f"☁️ {toggle} Proxy", callback_data=f"ep_{rid}")])
    btns.append([InlineKeyboardButton("🗑 Delete Record", callback_data=f"dl_{rid}")])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data=f"dns_{zid}")])
    await q.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

async def toggle_proxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; rid = q.data.replace("ep_", "")
    s = get_s(uid); rec = next((r for r in s.get("dns", []) if r["id"] == rid), None)
    zid = s.get("cur_zone", {}).get("id", "")
    if not rec: return
    new_px = not rec.get("proxied", False)
    wait = await q.message.edit_text("⏳ Updating...")
    try:
        cf_put(uid, f"/zones/{zid}/dns_records/{rid}", {"type": rec["type"], "name": rec["name"], "content": rec["content"], "ttl": rec.get("ttl", 1), "proxied": new_px})
        st = "Enabled ☁️" if new_px else "Disabled 🔘"
        await wait.edit_text(f"✅ Proxy {st}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Records", callback_data=f"dns_{zid}")]]))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

async def edit_content_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rid = q.data.replace("ec_", "")
    s = get_s(q.from_user.id); rec = next((r for r in s.get("dns", []) if r["id"] == rid), None)
    if not rec: return ConversationHandler.END
    ctx.user_data["edit_rec"] = rec; ctx.user_data["edit_rid"] = rid
    await q.message.edit_text(f"📝 Current value:\n<code>{rec['content']}</code>\n\nSend the new value:", parse_mode="HTML")
    return EDIT_VALUE

async def edit_content_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; val = update.message.text.strip()
    rec = ctx.user_data.get("edit_rec"); rid = ctx.user_data.get("edit_rid")
    s = get_s(uid); zid = s.get("cur_zone", {}).get("id", "")
    if not rec: await update.message.reply_text("❌ Not found."); return ConversationHandler.END
    wait = await update.message.reply_text("⏳ Updating...")
    try:
        body = {"type": rec["type"], "name": rec["name"], "content": val, "ttl": rec.get("ttl", 1)}
        if rec["type"] in ("A", "AAAA", "CNAME"): body["proxied"] = rec.get("proxied", False)
        cf_put(uid, f"/zones/{zid}/dns_records/{rid}", body)
        await wait.edit_text(f"✅ <b>Updated!</b>\n<code>{rec['name']}</code> → <code>{val}</code>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Records", callback_data=f"dns_{zid}")]]))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")
    return ConversationHandler.END

async def delete_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rid = q.data.replace("dl_", ""); s = get_s(q.from_user.id)
    rec = next((r for r in s.get("dns", []) if r["id"] == rid), None)
    zid = s.get("cur_zone", {}).get("id", "")
    if not rec: return
    await q.message.edit_text(
        f"⚠️ <b>Confirm Delete</b>\n\n<code>{rec['type']}</code> | <code>{rec['name']}</code>\n→ <code>{rec['content']}</code>\n\n<b>This action cannot be undone!</b>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Yes, Delete", callback_data=f"dx_{rid}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"dns_{zid}")]]))

async def delete_execute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; rid = q.data.replace("dx_", "")
    zid = get_s(uid).get("cur_zone", {}).get("id", "")
    try:
        cf_del(uid, f"/zones/{zid}/dns_records/{rid}")
        await q.message.edit_text("✅ Record deleted.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Records", callback_data=f"dns_{zid}")]]))
    except Exception as e:
        await q.message.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /disconnect
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def cmd_disconnect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    uid = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
    del_s(uid)
    await msg.reply_text("🔌 <b>Disconnected.</b>\n\nYour Cloudflare session has been cleared.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Connect Again", callback_data="do_connect")]]))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /stats (admin only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Access denied.")
        return

    db = load_db()
    total_users = len(db.get("users", {}))
    total_cf = len(db.get("cf_logins", {}))
    active_sessions = len([u for u in sessions.values() if u.get("key")])

    users = db.get("users", {})
    sorted_users = sorted(users.items(), key=lambda x: x[1].get("last_seen", ""), reverse=True)

    recent = ""
    for uid_str, info in sorted_users[:10]:
        name = info.get("name", "?")
        uname = f"@{info['username']}" if info.get("username") else ""
        seen = info.get("last_seen", "?")[:10]
        cf = "✅" if uid_str in db.get("cf_logins", {}) else "❌"
        recent += f"  {cf} <code>{uid_str}</code> {name} {uname} — {seen}\n"

    text = (
        f"╭─❀ <b>KABUTIFLARE STATS</b> ❀─╮\n{DIV}\n\n"
        f"👥 Total users: <b>{total_users}</b>\n"
        f"☁️ CF logins: <b>{total_cf}</b>\n"
        f"🟢 Active sessions: <b>{active_sessions}</b>\n\n"
        f"📋 <b>Recent users:</b>\n{recent or '  No users yet.'}\n"
        f"<i>✅ = connected to CF | ❌ = not connected</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /broadcast (admin only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def broadcast_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    db = load_db()
    total = len(db.get("users", {}))
    await update.message.reply_text(
        f"📢 <b>Broadcast</b>\n\n"
        f"👥 Will be sent to <b>{total}</b> users.\n\n"
        f"Send the message you want to broadcast.\n"
        f"Supports text, photo, video, document.\n\n"
        f"/cancel to abort.",
        parse_mode="HTML"
    )
    return BROADCAST_MSG

async def broadcast_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return ConversationHandler.END

    db = load_db()
    users = db.get("users", {})
    total = len(users)
    sent, failed, blocked = 0, 0, 0

    wait = await update.message.reply_text(f"📢 Sending to {total} users...")

    for uid_str in users:
        try:
            chat_id = int(uid_str)
            if update.message.text:
                await ctx.bot.send_message(chat_id, update.message.text, parse_mode="HTML")
            elif update.message.photo:
                await ctx.bot.send_photo(chat_id, update.message.photo[-1].file_id,
                    caption=update.message.caption or "", parse_mode="HTML")
            elif update.message.video:
                await ctx.bot.send_video(chat_id, update.message.video.file_id,
                    caption=update.message.caption or "", parse_mode="HTML")
            elif update.message.document:
                await ctx.bot.send_document(chat_id, update.message.document.file_id,
                    caption=update.message.caption or "", parse_mode="HTML")
            else:
                await ctx.bot.copy_message(chat_id, update.message.chat_id, update.message.message_id)
            sent += 1
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "deactivated" in err or "not found" in err:
                blocked += 1
            else:
                failed += 1
            logger.warning(f"Broadcast to {uid_str}: {e}")

        if (sent + failed + blocked) % 25 == 0:
            await asyncio.sleep(1)

    await wait.edit_text(
        f"📢 <b>Broadcast Complete</b>\n\n"
        f"✅ Sent: <b>{sent}</b>\n"
        f"🚫 Blocked: <b>{blocked}</b>\n"
        f"❌ Failed: <b>{failed}</b>\n"
        f"📊 Total: <b>{total}</b>",
        parse_mode="HTML"
    )
    return ConversationHandler.END

async def broadcast_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Broadcast cancelled.")
    return ConversationHandler.END

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /grantdeploy & /revokedeploy (admin only)
#  Usage: /grantdeploy 123456789
#         /revokedeploy 123456789
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def cmd_grantdeploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(
            "⛔ <b>Access denied.</b>\n\nOnly the bot admin can manage deploy permissions.",
            parse_mode="HTML"
        )
        return

    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "╭─❀ <b>GRANT DEPLOY ACCESS</b> ❀─╮\n"
            f"{DIV}\n\n"
            "⚠️ <b>Usage:</b> <code>/grantdeploy USER_ID</code>\n\n"
            "<i>Example: /grantdeploy 123456789</i>",
            parse_mode="HTML"
        )
        return

    target_uid = int(args[0])
    if target_uid not in ALLOWED_USERS:
        await update.message.reply_text(
            "╭─❀ <b>GRANT DEPLOY</b> ❀─╮\n"
            f"{DIV}\n\n"
            f"⚠️ User <code>{target_uid}</code> is not in ALLOWED_USERS.\n\n"
            "Only allowed users can receive deploy access.",
            parse_mode="HTML"
        )
        return

    grant_deploy(target_uid)
    db = load_db()
    user_info = db.get("users", {}).get(str(target_uid), {})
    name = user_info.get("name", "Unknown")
    username = f"@{user_info['username']}" if user_info.get("username") else ""

    await update.message.reply_text(
        "╭─❀ <b>DEPLOY ACCESS GRANTED</b> ❀─╮\n"
        f"{DIV}\n\n"
        f"✅ <b>Success!</b>\n\n"
        f"👤 User: <b>{name}</b> {username}\n"
        f"🆔 ID: <code>{target_uid}</code>\n\n"
        f"🚀 They can now use <b>/deploy</b> to deploy to servers.",
        parse_mode="HTML"
    )

async def cmd_revokedeploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(
            "⛔ <b>Access denied.</b>\n\nOnly the bot admin can manage deploy permissions.",
            parse_mode="HTML"
        )
        return

    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "╭─❀ <b>REVOKE DEPLOY ACCESS</b> ❀─╮\n"
            f"{DIV}\n\n"
            "⚠️ <b>Usage:</b> <code>/revokedeploy USER_ID</code>\n\n"
            "<i>Example: /revokedeploy 123456789</i>",
            parse_mode="HTML"
        )
        return

    target_uid = int(args[0])
    if target_uid == ADMIN_ID:
        await update.message.reply_text(
            "╭─❀ <b>REVOKE DEPLOY</b> ❀─╮\n"
            f"{DIV}\n\n"
            "❌ <b>Cannot revoke admin's own deploy access.</b>",
            parse_mode="HTML"
        )
        return

    if target_uid not in DEPLOY_ALLOWED_USERS:
        await update.message.reply_text(
            "╭─❀ <b>REVOKE DEPLOY</b> ❀─╮\n"
            f"{DIV}\n\n"
            f"⚠️ User <code>{target_uid}</code> doesn't currently have deploy access.",
            parse_mode="HTML"
        )
        return

    revoke_deploy(target_uid)
    db = load_db()
    user_info = db.get("users", {}).get(str(target_uid), {})
    name = user_info.get("name", "Unknown")
    username = f"@{user_info['username']}" if user_info.get("username") else ""

    await update.message.reply_text(
        "╭─❀ <b>DEPLOY ACCESS REVOKED</b> ❀─╮\n"
        f"{DIV}\n\n"
        f"🚫 <b>Access removed.</b>\n\n"
        f"👤 User: <b>{name}</b> {username}\n"
        f"🆔 ID: <code>{target_uid}</code>\n\n"
        f"They can no longer use <b>/deploy</b>.",
        parse_mode="HTML"
    )

async def cmd_deploylist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """List all users who currently have deploy access."""
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(
            "⛔ <b>Access denied.</b>", parse_mode="HTML"
        )
        return

    db = load_db()
    lines = []
    for dep_uid in sorted(DEPLOY_ALLOWED_USERS):
        user_info = db.get("users", {}).get(str(dep_uid), {})
        name = user_info.get("name", "Unknown")
        username = f"@{user_info['username']}" if user_info.get("username") else ""
        tag = " 👑 Admin" if dep_uid == ADMIN_ID else ""
        lines.append(f"  • <code>{dep_uid}</code> — <b>{name}</b> {username}{tag}")

    text = (
        "╭─❀ <b>DEPLOY ACCESS LIST</b> ❀─╮\n"
        f"{DIV}\n\n"
        f"🚀 <b>{len(DEPLOY_ALLOWED_USERS)}</b> user(s) with deploy access:\n\n"
        + ("\n".join(lines) or "  No users.") + "\n\n"
        "<i>Use /grantdeploy ID or /revokedeploy ID to manage.</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SSL/TLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def ssl_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("ssl_", "").replace("sslset_", "")
    s = get_s(uid)
    zone = s.get("cur_zone", {})

    if q.data.startswith("sslset_"):
        parts = zid.split("_", 1)
        zid, mode = parts[0], parts[1]
        wait = await q.message.edit_text("⏳ Changing SSL mode...")
        r = cf_patch(uid, f"/zones/{zid}/settings/ssl", {"value": mode})
        if r.get("success"):
            await wait.edit_text(
                f"✅ SSL changed to <b>{mode}</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"ssl_{zid}")]])
            )
        else:
            err = r.get("errors", [{}])[0].get("message", "Error")
            await wait.edit_text(f"❌ {err}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"ssl_{zid}")]]))
        return

    wait = await q.message.edit_text("⏳ Fetching SSL settings...")
    r = cf_get(uid, f"/zones/{zid}/settings/ssl")
    if not r.get("success"):
        await wait.edit_text("❌ Error fetching SSL", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")]]))
        return

    current = r["result"]["value"]
    modes = {"off": "🔴 Off", "flexible": "🟡 Flexible", "full": "🟢 Full", "strict": "🔵 Full (Strict)"}
    text = f"🔒 <b>SSL/TLS — {zone.get('name', '')}</b>\n\nCurrent mode: <b>{modes.get(current, current)}</b>\n\nSelect a mode:"

    btns = []
    for m, label in modes.items():
        if m == current:
            btns.append([InlineKeyboardButton(f"✅ {label}", callback_data="noop")])
        else:
            btns.append([InlineKeyboardButton(label, callback_data=f"sslset_{zid}_{m}")])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")])

    await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NAMESERVERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def ns_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("ns_", "")
    s = get_s(uid)
    zone = s.get("cur_zone", {})

    wait = await q.message.edit_text("⏳ Fetching nameservers...")
    r = cf_get(uid, f"/zones/{zid}")
    if not r.get("success"):
        await wait.edit_text("❌ Error", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")]]))
        return

    z = r["result"]
    ns_list = z.get("name_servers", [])
    orig_ns = z.get("original_name_servers", [])

    ns_txt = "\n".join(f"   <code>{n}</code>" for n in ns_list) or "—"
    orig_txt = "\n".join(f"   <code>{n}</code>" for n in orig_ns) or "—"

    text = (f"📛 <b>Nameservers — {z.get('name', '')}</b>\n\n"
            f"<b>☁️ Cloudflare Nameservers:</b>\n{ns_txt}\n\n"
            f"<b>🏷 Original (Registrar):</b>\n{orig_txt}\n\n"
            f"Status: <b>{z.get('status', '—')}</b>")

    btns = [[InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")]]
    await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ZONE SETTINGS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ZONE_TOGGLES = [
    ("always_use_https", "🔗 Always HTTPS"),
    ("automatic_https_rewrites", "🔄 Auto HTTPS Rewrites"),
    ("min_tls_version", "🔐 Min TLS Version"),
    ("tls_1_3", "🔐 TLS 1.3"),
    ("http3", "🌐 HTTP/3"),
    ("0rtt", "⚡ 0-RTT"),
    ("minify", "📦 Minify"),
    ("brotli", "🗜 Brotli"),
    ("early_hints", "💡 Early Hints"),
    ("websockets", "🔌 WebSockets"),
    ("opportunistic_encryption", "🔒 Opportunistic Encryption"),
    ("browser_cache_ttl", "🕐 Browser Cache TTL"),
]

async def zone_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    d = q.data
    s = get_s(uid)
    zone = s.get("cur_zone", {})

    if d.startswith("zst_"):
        parts = d.replace("zst_", "").split("_", 1)
        zid, setting_val = parts[0], parts[1]
        rest = setting_val
        li = rest.rfind("_")
        setting = rest[:li]
        val = rest[li+1:]
        if val in ("on", "off"):
            body = {"value": val}
        elif val.startswith("1."):
            body = {"value": val}
        else:
            try:
                body = {"value": int(val)}
            except Exception:
                body = {"value": val}
        wait = await q.message.edit_text("⏳ Applying setting...")
        r = cf_patch(uid, f"/zones/{zid}/settings/{setting}", body)
        if r.get("success"):
            await wait.edit_text("✅ Setting updated.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"zs_{zid}")]]))
        else:
            err = r.get("errors", [{}])[0].get("message", "Error")
            await wait.edit_text(f"❌ {err}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"zs_{zid}")]]))
        return

    zid = d.replace("zs_", "")
    wait = await q.message.edit_text("⏳ Fetching zone settings...")
    try:
        results = {}
        for key, _ in ZONE_TOGGLES:
            try:
                r = cf_get(uid, f"/zones/{zid}/settings/{key}")
                results[key] = r["result"]["value"]
            except Exception:
                results[key] = "—"

        text = f"🌐 <b>Zone Settings — {zone.get('name', '')}</b>\n\n"
        btns = []
        for key, label in ZONE_TOGGLES:
            val = results.get(key, "—")
            if val == "on":
                icon = "✅"; next_val = "off"
            elif val == "off":
                icon = "❌"; next_val = "on"
            else:
                icon = "🔘"; next_val = "on"
            text += f"{icon} {label}: <b>{val}</b>\n"
            btns.append([InlineKeyboardButton(f"{icon} {label} → Toggle", callback_data=f"zst_{zid}_{key}_{next_val}")])

        btns.append([InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")])
        await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PAGE RULES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def page_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("pr_", "")
    s = get_s(uid)
    zone = s.get("cur_zone", {})

    wait = await q.message.edit_text("⏳ Fetching page rules...")
    try:
        d = cf_get(uid, f"/zones/{zid}/pagerules", {"status": "active", "per_page": 50})
        rules = d["result"]
        set_s(uid, {"pagerules": rules})

        if not rules:
            await wait.edit_text(
                f"📭 No page rules for <b>{zone.get('name','')}</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Rule", callback_data=f"pradd_{zid}")],
                    [InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")],
                ])
            )
            return

        text = f"🔀 <b>Page Rules — {zone.get('name','')}</b>\n{len(rules)} rule(s)\n\n"
        btns = []
        for rule in rules:
            url = rule.get("targets", [{}])[0].get("constraint", {}).get("value", "?")
            status = "✅" if rule.get("status") == "active" else "⏸"
            text += f"{status} <code>{url[:40]}</code>\n"
            btns.append([InlineKeyboardButton(f"{status} {url[:35]}", callback_data=f"pred_{rule['id']}")])

        btns.append([InlineKeyboardButton("➕ Add Rule", callback_data=f"pradd_{zid}")])
        btns.append([InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")])
        await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WORKERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def workers_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("wk_", "")
    s = get_s(uid)
    zone = s.get("cur_zone", {})

    wait = await q.message.edit_text("⏳ Fetching workers...")
    try:
        d = cf_get(uid, f"/zones/{zid}/workers/routes")
        routes = d["result"]

        if not routes:
            await wait.edit_text(
                f"📭 No worker routes for <b>{zone.get('name','')}</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")]])
            )
            return

        text = f"👷 <b>Workers — {zone.get('name','')}</b>\n{len(routes)} route(s)\n\n"
        btns = []
        for route in routes[:10]:
            pattern = route.get("pattern", "?")
            script = route.get("script", "—")
            text += f"<code>{pattern[:35]}</code> → <b>{script}</b>\n"
            btns.append([InlineKeyboardButton(f"🔧 {pattern[:30]}", callback_data=f"wkr_{route['id']}")])

        btns.append([InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")])
        await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EMAIL ROUTING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def email_routing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("em_", "")
    s = get_s(uid)
    zone = s.get("cur_zone", {})

    wait = await q.message.edit_text("⏳ Fetching email routing...")
    try:
        d = cf_get(uid, f"/zones/{zid}/email/routing")
        result = d["result"]
        enabled = result.get("enabled", False)
        status_icon = "✅" if enabled else "❌"
        status_text = "Enabled" if enabled else "Disabled"

        text = (f"📧 <b>Email Routing — {zone.get('name','')}</b>\n\n"
                f"Status: {status_icon} <b>{status_text}</b>\n"
                f"Tag: <code>{result.get('tag', '—')}</code>\n")

        try:
            rules_d = cf_get(uid, f"/zones/{zid}/email/routing/rules")
            rules = rules_d["result"]
            if rules:
                text += f"\n<b>Rules ({len(rules)}):</b>\n"
                for rule in rules[:5]:
                    matchers = rule.get("matchers", [{}])
                    actions = rule.get("actions", [{}])
                    m_val = matchers[0].get("value", "?") if matchers else "?"
                    a_val = actions[0].get("value", ["?"])[0] if actions else "?"
                    r_enabled = "✅" if rule.get("enabled") else "❌"
                    text += f"{r_enabled} <code>{m_val}</code> → <code>{a_val}</code>\n"
        except Exception:
            pass

        toggle_cb = f"emtog_{zid}_{'disable' if enabled else 'enable'}"
        btns = [
            [InlineKeyboardButton(f"{'❌ Disable' if enabled else '✅ Enable'} Routing", callback_data=toggle_cb)],
            [InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")],
        ]
        await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ANALYTICS
#  Uses Cloudflare GraphQL Analytics API (v4 compatible).
#  Shows last 24h traffic: requests, bandwidth, threats, cached %.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _fmt_bytes(b):
    """Human-readable bytes."""
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.2f} GB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.2f} MB"
    if b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b} B"

def _fmt_num(n):
    """Human-readable large numbers."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)

def _cf_analytics_zone(uid, zid):
    """
    Fetch zone analytics via Cloudflare v4 REST endpoint (last 24 h).
    Returns dict with requests, bandwidth, threats, pageviews, cached info.
    """
    from datetime import timezone, timedelta
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Use the v4 analytics/dashboard endpoint
    try:
        d = cf_get(uid, f"/zones/{zid}/analytics/dashboard",
                   {"since": since, "until": until, "continuous": "false"})
        totals = d.get("result", {}).get("totals", {})
        requests = totals.get("requests", {})
        bandwidth = totals.get("bandwidth", {})
        threats = totals.get("threats", {})
        pageviews = totals.get("pageviews", {})
        uniq = totals.get("uniques", {})

        total_req = requests.get("all", 0)
        cached_req = requests.get("cached", 0)
        uncached_req = requests.get("uncached", 0)
        cache_pct = round((cached_req / total_req * 100) if total_req > 0 else 0, 1)

        total_bw = bandwidth.get("all", 0)
        cached_bw = bandwidth.get("cached", 0)
        bw_cache_pct = round((cached_bw / total_bw * 100) if total_bw > 0 else 0, 1)

        threat_total = threats.get("all", 0)
        pv_total = pageviews.get("all", 0)
        visitors = uniq.get("all", 0)

        # Top countries from timeseries if available
        by_country = {}
        for bucket in d.get("result", {}).get("timeseries", []):
            for c, v in bucket.get("requests", {}).get("country", {}).items():
                by_country[c] = by_country.get(c, 0) + v
        top_countries = sorted(by_country.items(), key=lambda x: x[1], reverse=True)[:5]

        # HTTP status breakdown
        status_map = requests.get("http_status", {})

        return {
            "ok": True,
            "total_req": total_req,
            "cached_req": cached_req,
            "uncached_req": uncached_req,
            "cache_pct": cache_pct,
            "total_bw": total_bw,
            "cached_bw": cached_bw,
            "bw_cache_pct": bw_cache_pct,
            "threats": threat_total,
            "pageviews": pv_total,
            "visitors": visitors,
            "top_countries": top_countries,
            "status_map": status_map,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def zone_analytics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show analytics for a specific zone (last 24 hours)."""
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("analytics_", "")
    s = get_s(uid)
    zone = s.get("cur_zone", {})
    zone_name = zone.get("name", zid)

    wait = await q.message.edit_text(
        f"⏳ Fetching analytics for <b>{zone_name}</b>…",
        parse_mode="HTML"
    )

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _cf_analytics_zone, uid, zid)

    if not data["ok"]:
        await wait.edit_text(
            f"╭─❀ <b>ANALYTICS</b> ❀─╮\n{DIV}\n\n"
            f"❌ <b>Could not fetch analytics.</b>\n\n"
            f"<code>{data['error']}</code>\n\n"
            "<i>Note: Analytics require a zone with at least some traffic, "
            "and your API token needs Zone · Analytics · Read permission.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")]
            ])
        )
        return

    # Build country list
    country_lines = ""
    for code, count in data["top_countries"]:
        country_lines += f"  🌍 <b>{code}</b> — {_fmt_num(count)} req\n"
    if not country_lines:
        country_lines = "  <i>No country data</i>\n"

    # HTTP status breakdown
    status_lines = ""
    for code, count in sorted(data["status_map"].items(), key=lambda x: -x[1])[:5]:
        icon = "✅" if str(code).startswith("2") else ("⚠️" if str(code).startswith("3") else "❌")
        status_lines += f"  {icon} <b>HTTP {code}</b> — {_fmt_num(count)}\n"
    if not status_lines:
        status_lines = "  <i>No status data</i>\n"

    # Threat level
    threat_icon = "🛡️" if data["threats"] == 0 else ("⚠️" if data["threats"] < 100 else "🚨")

    text = (
        f"╭─❀ <b>ANALYTICS</b> ❀─╮\n"
        f"<i>{zone_name} — Last 24 hours</i>\n"
        f"{DIV}\n\n"
        f"🌐 <b>Traffic Overview</b>\n"
        f"  📥 Requests: <b>{_fmt_num(data['total_req'])}</b>\n"
        f"  👁 Pageviews: <b>{_fmt_num(data['pageviews'])}</b>\n"
        f"  👤 Unique visitors: <b>{_fmt_num(data['visitors'])}</b>\n\n"
        f"⚡ <b>Cache Performance</b>\n"
        f"  ✅ Cached: <b>{_fmt_num(data['cached_req'])}</b> ({data['cache_pct']}%)\n"
        f"  🔄 Uncached: <b>{_fmt_num(data['uncached_req'])}</b>\n\n"
        f"📦 <b>Bandwidth</b>\n"
        f"  Total: <b>{_fmt_bytes(data['total_bw'])}</b>\n"
        f"  Cached: <b>{_fmt_bytes(data['cached_bw'])}</b> ({data['bw_cache_pct']}%)\n\n"
        f"{threat_icon} <b>Threats Blocked</b>\n"
        f"  <b>{_fmt_num(data['threats'])}</b> threats in 24h\n\n"
        f"🌍 <b>Top Countries</b>\n{country_lines}\n"
        f"📋 <b>HTTP Status Breakdown</b>\n{status_lines}"
    )

    btns = [
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"analytics_{zid}")],
        [InlineKeyboardButton("🔙 Back to Zone", callback_data=f"zone_{zid}")],
    ]
    await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

async def global_analytics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show a quick analytics summary across all zones."""
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
        uid = update.callback_query.from_user.id
    else:
        msg = update.message
        uid = update.effective_user.id

    if uid not in ALLOWED_USERS:
        await msg.reply_text("⛔ <b>Access denied.</b>", parse_mode="HTML")
        return

    s = get_s(uid)
    if not s.get("key"):
        await msg.reply_text(
            "🔒 Connect your Cloudflare account first.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Connect", callback_data="do_connect")
            ]])
        )
        return

    zones = s.get("zones", [])
    if not zones:
        # Try to fetch zones first
        wait = await msg.reply_text("⏳ Fetching zones…")
        try:
            all_zones = []
            page = 1
            while True:
                d = cf_get(uid, "/zones", {"per_page": 50, "page": page})
                all_zones.extend(d["result"])
                if page >= d.get("result_info", {}).get("total_pages", 1):
                    break
                page += 1
            set_s(uid, {"zones": all_zones})
            zones = all_zones
        except Exception as e:
            await wait.edit_text(f"❌ Error fetching zones: <code>{e}</code>", parse_mode="HTML")
            return
        await wait.delete()

    if not zones:
        await msg.reply_text("📭 No domains found.", parse_mode="HTML")
        return

    # Show zone picker for analytics
    text = (
        f"╭─❀ <b>ANALYTICS</b> ❀─╮\n"
        f"{DIV}\n\n"
        f"📊 Select a domain to view analytics:\n\n"
        f"<i>Shows traffic, cache, bandwidth & threats\nfor the last 24 hours.</i>"
    )
    ico = {"active": "🟢", "pending": "🟡", "moved": "🔴", "deactivated": "🔴"}
    btns = []
    for z in zones:
        i = ico.get(z["status"], "⚪")
        btns.append([InlineKeyboardButton(f"{i} {z['name']}", callback_data=f"analytics_{z['id']}")])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="do_domains")])

    if update.callback_query:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    else:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DEPLOY TO SERVER (via SSH)
#
#  Design note: every step in this conversation uses a plain TEXT
#  reply — never an inline button — except the very first entry
#  point. This guarantees the flow can never "skip" a step because
#  a callback got intercepted elsewhere; MessageHandlers inside an
#  active ConversationHandler state always take priority.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEPLOY_SCRIPT = '''#!/bin/bash
set -e
echo "STEP_DOCKER"
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sh >/dev/null 2>&1
fi
if ! docker compose version &>/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || true
fi
echo "STEP_DOWNLOAD"
mkdir -p /opt/kabutiflare && cd /opt/kabutiflare
REPO="{repo}"
curl -fsSL "$REPO/bot.py" -o bot.py
curl -fsSL "$REPO/Dockerfile" -o Dockerfile
curl -fsSL "$REPO/docker-compose.yml" -o docker-compose.yml
curl -fsSL "$REPO/requirements.txt" -o requirements.txt 2>/dev/null || cat > requirements.txt << 'REQEOF'
python-telegram-bot==21.5
requests>=2.31.0
paramiko>=3.0.0
REQEOF
echo "STEP_CONFIG"
cat > .env << ENVEOF
BOT_TOKEN={bot_token}
WEBAPP_URL={webapp_url}
ENVEOF
[ -f users.json ] || echo '{{"users":{{}},"cf_logins":{{}}}}' > users.json
echo "STEP_BUILD"
docker compose down 2>/dev/null || true
docker compose build --no-cache 2>&1 | tail -8
echo "STEP_START"
docker compose up -d
sleep 2
echo "STEP_VERIFY"
docker compose ps
echo "DEPLOY_OK"
'''

def _ssh_connect(host, port, user, auth_method, password, key_data):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_args = {
        "hostname": host,
        "port": port,
        "username": user,
        "timeout": 30,
        "banner_timeout": 30,
        "auth_timeout": 30,
    }
    if auth_method == "pass":
        connect_args["password"] = password
    else:
        key_file = io.StringIO(key_data)
        try:
            pkey = paramiko.RSAKey.from_private_key(key_file)
        except Exception:
            key_file.seek(0)
            try:
                pkey = paramiko.Ed25519Key.from_private_key(key_file)
            except Exception:
                key_file.seek(0)
                pkey = paramiko.ECDSAKey.from_private_key(key_file)
        connect_args["pkey"] = pkey
    client.connect(**connect_args)
    return client

def _ssh_deploy(host, port, user, auth_method, password, key_data, bot_token, webapp_url):
    """Runs entirely in a thread executor — safe blocking SSH call."""
    try:
        client = _ssh_connect(host, port, user, auth_method, password, key_data)
        script = DEPLOY_SCRIPT.format(
            repo=GITHUB_REPO,
            bot_token=bot_token,
            webapp_url=webapp_url or "",
        )
        stdin, stdout, stderr = client.exec_command("bash -s", timeout=300)
        stdin.write(script)
        stdin.channel.shutdown_write()
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        client.close()
        if exit_code == 0 and "DEPLOY_OK" in out:
            return {"success": True, "output": out}
        return {"success": False, "error": f"Exit code: {exit_code}\n{err}\n{out}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── Entry point ──────────────────────────────────────────────
async def deploy_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
    else:
        msg = update.message

    if not HAS_PARAMIKO:
        await msg.reply_text(
            "╭─❀ <b>DEPLOY UNAVAILABLE</b> ❀─╮\n"
            f"{DIV}\n\n"
            "⚠️ The <code>paramiko</code> library is missing.\n\n"
            "Install it with:\n<code>pip install paramiko</code>",
            parse_mode="HTML"
        )
        return ConversationHandler.END

    # ── Deploy access check ──────────────────────────────────
    uid = update.effective_user.id if update.effective_user else None
    if uid is None or not has_deploy_access(uid):
        await msg.reply_text(
            "╭─❀ <b>DEPLOY ACCESS DENIED</b> ❀─╮\n"
            f"{DIV}\n\n"
            "🚫 <b>You don't have permission to deploy.</b>\n\n"
            "Contact the bot admin to request deploy access.",
            parse_mode="HTML"
        )
        return ConversationHandler.END

    # Reset any stale deploy data from a previous attempt
    for k in list(ctx.user_data.keys()):
        if k.startswith("dep_"):
            ctx.user_data.pop(k, None)

    text = (
        "╭─❀ <b>DEPLOY TO SERVER</b> ❀─╮\n"
        f"{DIV}\n\n"
        "🚀 Installs KabutiFlare on your Linux server via SSH "
        "(Docker-based, fully automated).\n\n"
        "📋 <b>What you need:</b>\n"
        "  🖥  A Linux server (Ubuntu / Debian)\n"
        "  🔐  SSH access — IP + password or key\n"
        "  🤖  A Bot Token from @BotFather\n\n"
        f"{DIV}\n"
        "<b>Step 1 of 6 — Server Address</b>\n\n"
        "Send the server <b>IP address or hostname</b>:\n\n"
        "<i>Type /cancel at any time to abort.</i>"
    )
    if update.callback_query:
        await msg.edit_text(text, parse_mode="HTML")
    else:
        await msg.reply_text(text, parse_mode="HTML")
    return DEP_HOST

# ── Step 1: Host ─────────────────────────────────────────────
async def deploy_host(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    host = update.message.text.strip()
    if not host:
        await update.message.reply_text("⚠️ Please send a valid IP address or hostname.")
        return DEP_HOST
    ctx.user_data["dep_host"] = host
    await update.message.reply_text(
        f"✅ <b>Server:</b> <code>{host}</code>\n\n"
        f"{DIV}\n"
        "<b>Step 2 of 6 — SSH Port</b>\n\n"
        "Send the SSH port (default is <code>22</code>):",
        parse_mode="HTML"
    )
    return DEP_PORT

# ── Step 2: Port ─────────────────────────────────────────────
async def deploy_port(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        port = int(raw)
    except ValueError:
        port = 22
    ctx.user_data["dep_port"] = port
    await update.message.reply_text(
        f"✅ <b>Port:</b> <code>{port}</code>\n\n"
        f"{DIV}\n"
        "<b>Step 3 of 6 — SSH Username</b>\n\n"
        "Send the SSH username (usually <code>root</code>):",
        parse_mode="HTML"
    )
    return DEP_USER

# ── Step 3: Username ─────────────────────────────────────────
async def deploy_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.message.text.strip()
    if not user:
        await update.message.reply_text("⚠️ Please send a valid username.")
        return DEP_USER
    ctx.user_data["dep_user"] = user
    await update.message.reply_text(
        f"✅ <b>Username:</b> <code>{user}</code>\n\n"
        f"{DIV}\n"
        "<b>Step 4 of 6 — Authentication</b>\n\n"
        "How do you want to authenticate?\n\n"
        "  Reply <b>1</b> → Password\n"
        "  Reply <b>2</b> → SSH Key",
        parse_mode="HTML"
    )
    return DEP_AUTH

# ── Step 4: Auth method (text-based — never a button) ───────
async def deploy_auth_method(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == "2":
        ctx.user_data["dep_auth_method"] = "key"
        await update.message.reply_text(
            "🔐 <b>SSH Key</b>\n\nUpload your private key file, or paste its contents directly:",
            parse_mode="HTML"
        )
        return DEP_KEY
    elif choice == "1":
        ctx.user_data["dep_auth_method"] = "pass"
        await update.message.reply_text(
            "🔑 <b>Password</b>\n\nSend the SSH password:\n\n<i>⚠️ Your message will be deleted immediately.</i>",
            parse_mode="HTML"
        )
        return DEP_PASS
    else:
        await update.message.reply_text(
            "⚠️ Please reply with <b>1</b> (Password) or <b>2</b> (SSH Key):",
            parse_mode="HTML"
        )
        return DEP_AUTH

# ── Step 5a: Password ────────────────────────────────────────
async def deploy_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["dep_password"] = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.effective_chat.send_message(
        "✅ <b>Password saved</b> — message deleted for safety.\n\n"
        f"{DIV}\n"
        "<b>Step 5 of 6 — Bot Token</b>\n\n"
        "Send the <b>Bot Token</b> for the bot you're deploying\n"
        "<i>(get one from @BotFather)</i>:",
        parse_mode="HTML"
    )
    return DEP_BOTTOKEN

# ── Step 5b: SSH Key ─────────────────────────────────────────
async def deploy_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.document:
        f = await update.message.document.get_file()
        ba = await f.download_as_bytearray()
        ctx.user_data["dep_key_data"] = ba.decode("utf-8", errors="replace")
    else:
        ctx.user_data["dep_key_data"] = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.effective_chat.send_message(
        "✅ <b>SSH key saved</b> — message deleted for safety.\n\n"
        f"{DIV}\n"
        "<b>Step 5 of 6 — Bot Token</b>\n\n"
        "Send the <b>Bot Token</b> for the bot you're deploying\n"
        "<i>(get one from @BotFather)</i>:",
        parse_mode="HTML"
    )
    return DEP_BOTTOKEN

# ── Step 6: Bot Token ────────────────────────────────────────
async def deploy_bottoken(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    if ":" not in token:
        await update.message.reply_text(
            "⚠️ That doesn't look like a valid bot token. It should look like "
            "<code>123456789:ABCdefGhIJKlmNoPQRstuVwxyZ</code>.\n\nTry again:",
            parse_mode="HTML"
        )
        return DEP_BOTTOKEN
    ctx.user_data["dep_bottoken"] = token
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.effective_chat.send_message(
        "✅ <b>Bot token saved</b> — message deleted for safety.\n\n"
        f"{DIV}\n"
        "<b>Step 6 of 6 — Mini App URL</b>\n\n"
        "Send your WebApp / Mini App URL,\n"
        "or send <code>skip</code> if you don't need one:",
        parse_mode="HTML"
    )
    return DEP_WEBAPP

# ── Final step: confirm + run deploy ────────────────────────
async def deploy_webapp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    webapp = update.message.text.strip()
    if webapp.lower() == "skip":
        webapp = ""
    ctx.user_data["dep_webapp"] = webapp

    host = ctx.user_data["dep_host"]
    port = ctx.user_data["dep_port"]
    user = ctx.user_data["dep_user"]
    auth_method = ctx.user_data["dep_auth_method"]
    password = ctx.user_data.get("dep_password", "")
    key_data = ctx.user_data.get("dep_key_data", "")
    bot_token = ctx.user_data["dep_bottoken"]

    wait = await update.message.reply_text(
        f"╭─❀ <b>DEPLOYING</b> ❀─╮\n{DIV}\n\n"
        f"🖥 Server: <code>{user}@{host}:{port}</code>\n"
        f"🔑 Auth: {'Password' if auth_method == 'pass' else 'SSH Key'}\n\n"
        f"⏳ <b>Connecting via SSH...</b>\n"
        f"<i>This usually takes 2–5 minutes. Please wait.</i>",
        parse_mode="HTML"
    )

    result = await asyncio.get_event_loop().run_in_executor(
        None, _ssh_deploy, host, port, user, auth_method, password, key_data, bot_token, webapp
    )

    if result["success"]:
        await wait.edit_text(
            f"╭─❀ <b>DEPLOYMENT SUCCESSFUL</b> ❀─╮\n{DIV}\n\n"
            f"✅ KabutiFlare is now running on your server!\n\n"
            f"🖥 Server: <code>{host}</code>\n"
            f"📁 Path: <code>/opt/kabutiflare</code>\n"
            f"🐳 Status: Running (Docker)\n\n"
            f"🔧 <b>Useful commands:</b>\n"
            f"<code>docker compose -f /opt/kabutiflare/docker-compose.yml logs -f</code>\n"
            f"<code>docker compose -f /opt/kabutiflare/docker-compose.yml restart</code>",
            parse_mode="HTML"
        )
    else:
        err = result["error"][:1200]
        tip = ""
        low = err.lower()
        if "timed out" in low or "timeout" in low:
            tip = "\n\n💡 <b>Tip:</b> Check that the server IP/port is correct and reachable from the internet (firewall, security group, etc.)."
        elif "authentication" in low:
            tip = "\n\n💡 <b>Tip:</b> Wrong password or SSH key. Double check and try /deploy again."
        elif "connection refused" in low:
            tip = "\n\n💡 <b>Tip:</b> SSH port may be wrong, or SSH service isn't running on the server."
        elif "name or service not known" in low or "nodename" in low:
            tip = "\n\n💡 <b>Tip:</b> The hostname/IP couldn't be resolved. Check for typos."
        await wait.edit_text(
            f"╭─❀ <b>DEPLOYMENT FAILED</b> ❀─╮\n{DIV}\n\n❌ <pre>{err}</pre>{tip}",
            parse_mode="HTML"
        )

    for k in ["dep_password", "dep_key_data", "dep_bottoken"]:
        ctx.user_data.pop(k, None)
    return ConversationHandler.END

async def deploy_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    for k in list(ctx.user_data.keys()):
        if k.startswith("dep_"):
            ctx.user_data.pop(k, None)
    await update.message.reply_text("❌ Deploy cancelled.")
    return ConversationHandler.END

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MISC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def noop_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()

async def post_init(app):
    commands = [
        BotCommand("start", "🏠 Main menu"),
        BotCommand("connect", "🔐 Connect Cloudflare account"),
        BotCommand("domains", "🌐 List your domains"),
        BotCommand("dns", "📋 Manage DNS records"),
        BotCommand("analytics", "📊 View traffic analytics"),
        BotCommand("disconnect", "🔌 Disconnect account"),
        BotCommand("help", "📖 Help & guide"),
        BotCommand("deploy", "🚀 Deploy bot to a server"),
        BotCommand("stats", "📊 Bot stats (admin only)"),
        BotCommand("broadcast", "📢 Broadcast message (admin only)"),
        BotCommand("grantdeploy", "🔓 Grant deploy access (admin only)"),
        BotCommand("revokedeploy", "🔒 Revoke deploy access (admin only)"),
        BotCommand("deploylist", "📋 List deploy access (admin only)"),
    ]
    await app.bot.set_my_commands(commands)
    if WEBAPP_URL:
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="📊 Dashboard", web_app=WebAppInfo(url=WEBAPP_URL))
            )
        except Exception:
            pass

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # ── Deploy conversation (registered FIRST so its states always
    #    win priority over any standalone callback/message handler) ──
    deploy_conv = ConversationHandler(
        entry_points=[
            CommandHandler("deploy", deploy_start),
            CallbackQueryHandler(deploy_start, pattern="^do_deploy$"),
        ],
        states={
            DEP_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_host)],
            DEP_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_port)],
            DEP_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_user)],
            DEP_AUTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_auth_method)],
            DEP_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_password)],
            DEP_KEY: [
                MessageHandler(filters.Document.ALL, deploy_key),
                MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_key),
            ],
            DEP_BOTTOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_bottoken)],
            DEP_WEBAPP: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_webapp)],
        },
        fallbacks=[
            CommandHandler("cancel", deploy_cancel),
            CommandHandler("start", cmd_start),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
        allow_reentry=True,
        conversation_timeout=900,  # auto-expire after 15 min of inactivity
    )

    connect_conv = ConversationHandler(
        entry_points=[
            CommandHandler("connect", connect_entry),
            CallbackQueryHandler(connect_entry, pattern="^do_connect$"),
        ],
        states={
            CONNECT_METHOD: [CallbackQueryHandler(connect_method, pattern="^m_")],
            CONNECT_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_email)],
            CONNECT_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_key)],
        },
        fallbacks=[
            CommandHandler("cancel", connect_cancel_msg),
            MessageHandler(filters.COMMAND, connect_cancel_msg),
        ],
        allow_reentry=True,
    )

    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern="^add_")],
        states={
            ADD_TYPE: [CallbackQueryHandler(add_type, pattern="^at_")],
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_content)],
            ADD_PROXY: [CallbackQueryHandler(add_proxy, pattern="^px_")],
        },
        fallbacks=[CommandHandler("cancel", connect_cancel_msg)],
        allow_reentry=True,
    )

    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_content_entry, pattern="^ec_")],
        states={
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_content_value)],
        },
        fallbacks=[CommandHandler("cancel", connect_cancel_msg)],
        allow_reentry=True,
    )

    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            BROADCAST_MSG: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_send)],
        },
        fallbacks=[CommandHandler("cancel", broadcast_cancel)],
        allow_reentry=True,
    )

    # Order matters: deploy_conv first, then the rest
    app.add_handler(deploy_conv)
    app.add_handler(connect_conv)
    app.add_handler(add_conv)
    app.add_handler(edit_conv)
    app.add_handler(broadcast_conv)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", send_help))
    app.add_handler(CommandHandler("domains", cmd_domains))
    app.add_handler(CommandHandler("dns", cmd_domains))
    app.add_handler(CommandHandler("analytics", global_analytics))
    app.add_handler(CommandHandler("disconnect", cmd_disconnect))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("grantdeploy", cmd_grantdeploy))
    app.add_handler(CommandHandler("revokedeploy", cmd_revokedeploy))
    app.add_handler(CommandHandler("deploylist", cmd_deploylist))

    # Specific callback handlers (kept explicit — no catch-all router,
    # so nothing can ever accidentally intercept a conversation step)
    app.add_handler(CallbackQueryHandler(cmd_domains, pattern="^do_domains$"))
    app.add_handler(CallbackQueryHandler(cmd_disconnect, pattern="^do_disconnect$"))
    app.add_handler(CallbackQueryHandler(send_help, pattern="^do_help$"))
    app.add_handler(CallbackQueryHandler(global_analytics, pattern="^do_analytics$"))
    app.add_handler(CallbackQueryHandler(zone_analytics, pattern="^analytics_"))
    app.add_handler(CallbackQueryHandler(zone_selected, pattern="^zone_"))
    app.add_handler(CallbackQueryHandler(dns_list, pattern="^dns_"))
    app.add_handler(CallbackQueryHandler(dns_filter, pattern="^ft_"))
    app.add_handler(CallbackQueryHandler(edit_start, pattern="^ed_"))
    app.add_handler(CallbackQueryHandler(toggle_proxy, pattern="^ep_"))
    app.add_handler(CallbackQueryHandler(delete_confirm, pattern="^dl_"))
    app.add_handler(CallbackQueryHandler(delete_execute, pattern="^dx_"))
    app.add_handler(CallbackQueryHandler(ssl_settings, pattern="^ssl_"))
    app.add_handler(CallbackQueryHandler(ssl_settings, pattern="^sslset_"))
    app.add_handler(CallbackQueryHandler(ns_info, pattern="^ns_"))
    app.add_handler(CallbackQueryHandler(zone_settings, pattern="^zs_"))
    app.add_handler(CallbackQueryHandler(zone_settings, pattern="^zst_"))
    app.add_handler(CallbackQueryHandler(page_rules, pattern="^pr_"))
    app.add_handler(CallbackQueryHandler(workers_list, pattern="^wk_"))
    app.add_handler(CallbackQueryHandler(email_routing, pattern="^em_"))
    app.add_handler(CallbackQueryHandler(noop_handler, pattern="^noop$"))

    logger.info("🚀 KabutiFlare bot started.")
    app.run_polling(
        allowed_updates=["message", "callback_query", "inline_query", "chat_member", "my_chat_member"],
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
