"""
KabutiFlare — Telegram Bot + Mini App
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

# ━━━━━━━━━━ CONFIG ━━━━━━━━━━
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
CF_API = "https://api.cloudflare.com/client/v4"
ADMIN_ID = 474008580
ALLOWED_USERS = {474008580, 5069084099}
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is required. Set via environment variable or .env file.")

def webapp_btn(text, zid=None):
    """Return WebApp button only if WEBAPP_URL is set, else None."""
    if not WEBAPP_URL:
        return None
    url = f"{WEBAPP_URL}?zone={zid}" if zid else WEBAPP_URL
    return InlineKeyboardButton(text, web_app=WebAppInfo(url=url))

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ━━━━━━━━━━ FORCE IPv4 GLOBALLY ━━━━━━━━━━
_orig_getaddrinfo = socket.getaddrinfo

def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

socket.getaddrinfo = _ipv4_getaddrinfo

# ━━━━━━━━━━ STATES ━━━━━━━━━━
CONNECT_METHOD, CONNECT_EMAIL, CONNECT_KEY = 0, 1, 2
ADD_TYPE, ADD_NAME, ADD_CONTENT, ADD_PROXY = 10, 11, 12, 13
EDIT_VALUE = 50
BROADCAST_MSG = 60
PR_URL, PR_ACTION = 70, 71
WK_CODE = 80
DEP_HOST, DEP_PORT, DEP_USER, DEP_AUTH, DEP_PASS, DEP_KEY, DEP_BOTTOKEN, DEP_WEBAPP = 90, 91, 92, 93, 94, 95, 96, 97

# ━━━━━━━━━━ USER DATABASE (persistent JSON) ━━━━━━━━━━
def load_db():
    try:
        with open(DB_FILE, "r") as f:
            return json.load(f)
    except:
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

# ━━━━━━━━━━ SESSIONS ━━━━━━━━━━
sessions = {}
def get_s(uid): return sessions.get(uid, {})
def set_s(uid, data):
    if uid not in sessions: sessions[uid] = {}
    sessions[uid].update(data)
def del_s(uid): sessions.pop(uid, None)

# ━━━━━━━━━━ CLOUDFLARE API ━━━━━━━━━━
def cf_h(uid):
    s = get_s(uid)
    if s.get("auth") == "token":
        return {"Authorization": f"Bearer {s['key']}", "Content-Type": "application/json"}
    return {"X-Auth-Email": s.get("email",""), "X-Auth-Key": s.get("key",""), "Content-Type": "application/json"}

def cf_get(uid, path, params=None):
    r = requests.get(f"{CF_API}{path}", headers=cf_h(uid), params=params, timeout=15)
    d = r.json()
    if not d.get("success"):
        raise Exception(", ".join(e.get("message","") for e in d.get("errors",[])) or "Unknown error")
    return d

def cf_post(uid, path, body):
    r = requests.post(f"{CF_API}{path}", headers=cf_h(uid), json=body, timeout=15)
    d = r.json()
    if not d.get("success"):
        raise Exception(", ".join(e.get("message","") for e in d.get("errors",[])) or "Unknown error")
    return d

def cf_put(uid, path, body):
    r = requests.put(f"{CF_API}{path}", headers=cf_h(uid), json=body, timeout=15)
    d = r.json()
    if not d.get("success"):
        raise Exception(", ".join(e.get("message","") for e in d.get("errors",[])) or "Unknown error")
    return d

def cf_del(uid, path):
    r = requests.delete(f"{CF_API}{path}", headers=cf_h(uid), timeout=15)
    d = r.json()
    if not d.get("success"):
        raise Exception(", ".join(e.get("message","") for e in d.get("errors",[])) or "Unknown error")
    return d

def cf_patch(uid, path, body):
    r = requests.patch(f"{CF_API}{path}", headers=cf_h(uid), json=body, timeout=15)
    return r.json()

# ━━━━━━━━━━ AUTH CHECK ━━━━━━━━━━
def need_auth(func):
    @wraps(func)
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid not in ALLOWED_USERS:
            msg = update.message or update.callback_query.message
            await msg.reply_text("⛔ Access denied.")
            return
        if not get_s(uid).get("key"):
            msg = update.message or update.callback_query.message
            await msg.reply_text(
                "⚠️ Please connect your Cloudflare account first.\n\n/connect",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔐 Connect", callback_data="do_connect")
                ]])
            )
            return
        return await func(update, ctx)
    return w

# ━━━━━━━━━━ /start ━━━━━━━━━━
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Access denied.")
        return
    name = update.effective_user.first_name or "User"
    username = update.effective_user.username or ""
    track_user(uid, name, username)
    logged = bool(get_s(uid).get("key"))

    text = f"👋 Hello <b>{name}</b>!\n\n🔸 <b>KabutiFlare</b> — Manage Cloudflare DNS from Telegram\n\n"

    if logged:
        text += "✅ Your account is connected.\n"
        btns = [
            [InlineKeyboardButton("🌐 My Domains", callback_data="do_domains")],
        ]
        if WEBAPP_URL:
            btns.append([webapp_btn("📊 Dashboard")])
        btns.append([InlineKeyboardButton("📖 Help", callback_data="do_help"),
                     InlineKeyboardButton("🔌 Disconnect", callback_data="do_disconnect")])
        btns.append([InlineKeyboardButton("🚀 Deploy to Server", callback_data="do_deploy")])
    else:
        text += "To get started, connect your Cloudflare account:\n"
        btns = [
            [InlineKeyboardButton("🔐 Connect to Cloudflare", callback_data="do_connect")],
            [InlineKeyboardButton("📖 Help", callback_data="do_help"),
             InlineKeyboardButton("🚀 Deploy to Server", callback_data="do_deploy")],
        ]

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━ /help ━━━━━━━━━━
async def send_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    if update.callback_query: await update.callback_query.answer()

    text = (
        "📖 <b>KabutiFlare Help</b>\n\n"
        "🔹 <b>Commands:</b>\n"
        "/connect — Connect to Cloudflare\n"
        "/domains — List your domains\n"
        "/dns — Manage DNS records\n"
        "/disconnect — Disconnect account\n\n"
        "🔹 <b>Features:</b>\n"
        "📋 DNS — View/Add/Edit/Delete records\n"
        "🔒 SSL/TLS — Change encryption mode\n"
        "📛 NS — View nameservers\n"
        "🌐 Settings — Always HTTPS, TLS 1.3, HTTP/3, Brotli, Minify, etc.\n"
        "🔀 Page Rules — Create/Edit/Delete redirects and rules\n"
        "👷 Workers — View/Edit/Upload/Delete scripts\n"
        "📧 Email — Manage email routing\n"
        "📊 Dashboard — Mini App with all features above\n\n"
        "🔹 <b>Getting an API Token:</b>\n"
        "1. dash.cloudflare.com → My Profile → API Tokens\n"
        "2. Create Custom Token\n"
        "3. Required permissions:\n"
        "   <code>Zone — DNS → Edit</code>\n"
        "   <code>Zone — Zone Settings → Edit</code>\n"
        "   <code>Zone — SSL and Certificates → Edit</code>\n"
        "   <code>Zone — Page Rules → Edit</code>\n"
        "   <code>Zone — Workers Routes → Edit</code>\n"
        "   <code>Zone — Email Routing Rules → Edit</code>\n"
        "   <code>Account — Workers Scripts → Edit</code>\n"
        "4. Zone Resources: All zones\n"
        "5. Copy your token\n"
    )
    btns = [[InlineKeyboardButton("🔐 Connect", callback_data="do_connect")]]
    if update.callback_query:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    else:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━ /connect ━━━━━━━━━━
async def connect_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
    else:
        msg = update.message

    btns = [
        [InlineKeyboardButton("🛡 API Token (Recommended)", callback_data="m_token")],
        [InlineKeyboardButton("🔑 Global API Key + Email", callback_data="m_apikey")],
        [InlineKeyboardButton("❌ Cancel", callback_data="m_cancel")],
    ]
    text = "🔐 <b>Connect to Cloudflare</b>\n\nSelect your authentication method:"

    if update.callback_query:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        except:
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
            "🛡 <b>API Token</b>\n\nPlease send your API Token:\n\n<i>⚠️ Message will be deleted immediately.</i>",
            parse_mode="HTML")
        return CONNECT_KEY
    if q.data == "m_apikey":
        ctx.user_data["auth"] = "apikey"
        await q.message.edit_text(
            "🔑 <b>Global API Key</b>\n\nPlease send your Cloudflare <b>Email</b>:", parse_mode="HTML")
        return CONNECT_EMAIL
    return CONNECT_METHOD

async def connect_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["email"] = update.message.text.strip()
    await update.message.reply_text(
        f"📧 Email: <code>{ctx.user_data['email']}</code>\n\n"
        "Now send your <b>Global API Key</b>:\n\n<i>⚠️ Message will be deleted immediately.</i>",
        parse_mode="HTML")
    return CONNECT_KEY

async def connect_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    key = update.message.text.strip()
    try: await update.message.delete()
    except: pass

    method = ctx.user_data.get("auth", "token")
    email = ctx.user_data.get("email", "")
    set_s(uid, {"key": key, "auth": method, "email": email})

    wait = await update.effective_chat.send_message("⏳ Verifying...")
    try:
        if method == "token":
            cf_get(uid, "/user/tokens/verify")
        else:
            cf_get(uid, "/zones", {"per_page": 1})

        track_cf_login(uid, update.effective_user.first_name or "")
        _btns = [[InlineKeyboardButton("🌐 Domains", callback_data="do_domains")]]
        if WEBAPP_URL:
            _btns.append([webapp_btn("📊 Dashboard")])
        await wait.edit_text(
            "✅ <b>Connected successfully!</b>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(_btns))
    except Exception as e:
        del_s(uid)
        await wait.edit_text(
            f"❌ <b>Error</b>\n\n<code>{e}</code>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Try Again", callback_data="do_connect")]
            ]))
    return ConversationHandler.END

async def connect_cancel_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ━━━━━━━━━━ /domains ━━━━━━━━━━
@need_auth
async def cmd_domains(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    uid = update.effective_user.id
    if update.callback_query: await update.callback_query.answer()

    wait = await msg.reply_text("⏳ Fetching domains...")
    try:
        zones = []
        page = 1
        while True:
            d = cf_get(uid, "/zones", {"per_page": 50, "page": page})
            zones.extend(d["result"])
            if page >= d.get("result_info", {}).get("total_pages", 1): break
            page += 1

        set_s(uid, {"zones": zones})
        if not zones:
            await wait.edit_text("📭 No domains found.")
            return

        ico = {"active": "🟢", "pending": "🟡", "moved": "🔴", "deactivated": "🔴"}
        text = f"🌐 <b>Domains</b> — {len(zones)} total\n"
        btns = []
        for z in zones:
            i = ico.get(z["status"], "⚪")
            plan = z.get("plan", {}).get("name", "Free")
            btns.append([InlineKeyboardButton(f"{i} {z['name']} • {plan}", callback_data=f"zone_{z['id']}")])
        _last = [InlineKeyboardButton("🔄 Refresh", callback_data="do_domains")]
        if WEBAPP_URL:
            _last.append(webapp_btn("📊 Dashboard"))
        btns.append(_last)
        await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

# ━━━━━━━━━━ ZONE SELECTED ━━━━━━━━━━
async def zone_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("zone_", "")
    s = get_s(uid)
    zone = next((z for z in s.get("zones", []) if z["id"] == zid), None)
    if not zone:
        await q.message.edit_text("❌ Domain not found.")
        return
    set_s(uid, {"cur_zone": zone})
    st = {"active": "🟢 Active", "pending": "🟡 Pending", "moved": "🔴 Moved"}
    ns = "\n".join(f"  <code>{n}</code>" for n in zone.get("name_servers", []))
    text = (f"📋 <b>{zone['name']}</b>\n\nStatus: {st.get(zone['status'], zone['status'])}\n"
            f"Plan: {zone.get('plan', {}).get('name', 'Free')}\nNameservers:\n{ns}\n")
    btns = [
        [InlineKeyboardButton("📋 DNS", callback_data=f"dns_{zid}"),
         InlineKeyboardButton("➕ Add Record", callback_data=f"add_{zid}")],
        [InlineKeyboardButton("🔒 SSL/TLS", callback_data=f"ssl_{zid}"),
         InlineKeyboardButton("📛 NS", callback_data=f"ns_{zid}")],
        [InlineKeyboardButton("🌐 Settings", callback_data=f"zs_{zid}"),
         InlineKeyboardButton("🔀 Page Rules", callback_data=f"pr_{zid}")],
        [InlineKeyboardButton("👷 Workers", callback_data=f"wk_{zid}"),
         InlineKeyboardButton("📧 Email", callback_data=f"em_{zid}")],
    ]
    if WEBAPP_URL:
        btns.append([webapp_btn("📊 Dashboard", zid)])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="do_domains")])
    await q.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━ DNS LIST ━━━━━━━━━━
async def dns_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("dns_", "")
    s = get_s(uid)
    zone = s.get("cur_zone", {})
    try:
        recs = []
        page = 1
        while True:
            d = cf_get(uid, f"/zones/{zid}/dns_records", {"per_page": 100, "page": page})
            recs.extend(d["result"])
            if page >= d.get("result_info", {}).get("total_pages", 1): break
            page += 1
        set_s(uid, {"dns": recs})
        if not recs:
            await q.message.edit_text(f"📭 No records found for <b>{zone.get('name','')}</b>.", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Record", callback_data=f"add_{zid}")],
                    [InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")],
                ]))
            return
        tc = {}
        for r in recs: tc[r["type"]] = tc.get(r["type"], 0) + 1
        ti = {"A":"🔵","AAAA":"🟣","CNAME":"🟢","MX":"🟡","TXT":"🩷","NS":"🔷","SRV":"🟠","CAA":"🟤"}
        text = f"📋 <b>DNS — {zone.get('name','')}</b>\n{len(recs)} records\n\n"
        for t, c in sorted(tc.items()): text += f"{ti.get(t,'⚪')} <b>{t}</b>: {c}\n"
        text += "\n"
        for r in recs[:15]:
            px = "☁️" if r.get("proxied") else "🔘"
            nm = r["name"].replace(f".{zone.get('name','')}", "") or "@"
            ct = r["content"][:30] + ("…" if len(r["content"]) > 30 else "")
            text += f"<code>{r['type']:5}</code> {nm} → <code>{ct}</code> {px}\n"
        if len(recs) > 15: text += f"\n<i>… and {len(recs)-15} more records</i>\n"
        fbtns = []
        row = []
        for t in sorted(tc.keys()):
            row.append(InlineKeyboardButton(f"{ti.get(t,'')}{t}({tc[t]})", callback_data=f"ft_{zid}_{t}"))
            if len(row) >= 4: fbtns.append(row); row = []
        if row: fbtns.append(row)
        fbtns.append([InlineKeyboardButton("➕ Add", callback_data=f"add_{zid}"), InlineKeyboardButton("🔄", callback_data=f"dns_{zid}")])
        if WEBAPP_URL:
            fbtns.append([webapp_btn("📊 Dashboard", zid)])
        fbtns.append([InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")])
        await q.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(fbtns))
    except Exception as e:
        await q.message.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

# ━━━━━━━━━━ DNS FILTER ━━━━━━━━━━
async def dns_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    parts = q.data.split("_"); zid, rtype = parts[1], parts[2]
    s = get_s(uid); zone = s.get("cur_zone", {})
    recs = [r for r in s.get("dns", []) if r["type"] == rtype]
    ti = {"A":"🔵","AAAA":"🟣","CNAME":"🟢","MX":"🟡","TXT":"🩷","NS":"🔷","SRV":"🟠","CAA":"🟤"}
    text = f"{ti.get(rtype,'⚪')} <b>{rtype} — {zone.get('name','')}</b>\n{len(recs)} records\n\n"
    btns = []
    for r in recs[:20]:
        nm = r["name"].replace(f".{zone.get('name','')}", "") or "@"
        ct = r["content"][:25] + ("…" if len(r["content"]) > 25 else "")
        px = "☁️" if r.get("proxied") else "🔘"
        text += f"<code>{nm}</code> → <code>{ct}</code> {px}\n"
        btns.append([InlineKeyboardButton(f"✏️ {nm} → {ct}", callback_data=f"ed_{r['id']}")])
    btns.append([InlineKeyboardButton("🔙 All", callback_data=f"dns_{zid}"), InlineKeyboardButton("➕", callback_data=f"add_{zid}")])
    await q.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━ ADD DNS ━━━━━━━━━━
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
    await q.message.edit_text("➕ <b>Record Type:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    return ADD_TYPE

async def add_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["add_type"] = q.data.replace("at_", "")
    t = ctx.user_data["add_type"]
    ph = {"A":"@ or www","AAAA":"@ or www","CNAME":"www","MX":"@","TXT":"@ or _dmarc","NS":"sub","SRV":"_sip._tcp","CAA":"@"}
    await q.message.edit_text(f"➕ <b>{t}</b>\n\nSend the <b>Name</b>:\n<i>{ph.get(t,'')} — root = @</i>", parse_mode="HTML")
    return ADD_NAME

async def add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["add_name"] = update.message.text.strip()
    t = ctx.user_data["add_type"]
    hints = {"A":"<code>1.2.3.4</code>","AAAA":"<code>2001:db8::1</code>","CNAME":"<code>example.com</code>","MX":"<code>mail.example.com</code>","TXT":"<code>v=spf1 ...</code>","NS":"<code>ns1.example.com</code>","SRV":"<code>priority weight port target</code>","CAA":'<code>0 issue "letsencrypt.org"</code>'}
    await update.message.reply_text(f"✅ Name: <code>{ctx.user_data['add_name']}</code>\n\n<b>Value:</b>\n{hints.get(t,'')}", parse_mode="HTML")
    return ADD_CONTENT

async def add_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["add_content"] = update.message.text.strip()
    t = ctx.user_data["add_type"]
    if t in ("A", "AAAA", "CNAME"):
        await update.message.reply_text(f"✅ Value: <code>{ctx.user_data['add_content']}</code>\n\nProxy:", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("☁️ Enabled", callback_data="px_on"), InlineKeyboardButton("🔘 Disabled", callback_data="px_off")]]))
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
        px_txt = "☁️" if proxied else "🔘"
        await wait.edit_text(f"✅ <b>Created!</b>\n\n<code>{t}</code> | <code>{name}</code>\n→ <code>{content}</code> {px_txt}", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Records", callback_data=f"dns_{zid}"), InlineKeyboardButton("➕ Add Another", callback_data=f"add_{zid}")]]))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")
    return ConversationHandler.END

# ━━━━━━━━━━ EDIT DNS ━━━━━━━━━━
async def edit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; rid = q.data.replace("ed_", "")
    s = get_s(uid); rec = next((r for r in s.get("dns", []) if r["id"] == rid), None)
    zid = s.get("cur_zone", {}).get("id", "")
    if not rec: await q.message.edit_text("❌ Not found."); return
    ctx.user_data["edit_rec"] = rec
    px_txt = "☁️" if rec.get("proxied") else "🔘"
    ttl = "Auto" if rec.get("ttl") == 1 else f"{rec['ttl']}s"
    text = f"✏️ <b>Edit Record</b>\n\n<code>{rec['type']}</code> | <code>{rec['name']}</code>\n→ <code>{rec['content']}</code>\nTTL: {ttl} | Proxy: {px_txt}"
    btns = [[InlineKeyboardButton("📝 Change Value", callback_data=f"ec_{rid}")]]
    if rec["type"] in ("A", "AAAA", "CNAME"):
        toggle = "Disable" if rec.get("proxied") else "Enable"
        btns.append([InlineKeyboardButton(f"☁️ {toggle} Proxy", callback_data=f"ep_{rid}")])
    btns.append([InlineKeyboardButton("🗑 Delete", callback_data=f"dl_{rid}")])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data=f"dns_{zid}")])
    await q.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

async def toggle_proxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; rid = q.data.replace("ep_", "")
    s = get_s(uid); rec = next((r for r in s.get("dns", []) if r["id"] == rid), None)
    zid = s.get("cur_zone", {}).get("id", "")
    if not rec: return
    new_px = not rec.get("proxied", False)
    wait = await q.message.edit_text("⏳...")
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
    await q.message.edit_text(f"📝 Current:\n<code>{rec['content']}</code>\n\nNew value:", parse_mode="HTML")
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
        await wait.edit_text(f"✅ Updated!\n<code>{rec['name']}</code> → <code>{val}</code>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Records", callback_data=f"dns_{zid}")]]))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")
    return ConversationHandler.END

# ━━━━━━━━━━ DELETE ━━━━━━━━━━
async def delete_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rid = q.data.replace("dl_", ""); s = get_s(q.from_user.id)
    rec = next((r for r in s.get("dns", []) if r["id"] == rid), None)
    zid = s.get("cur_zone", {}).get("id", "")
    if not rec: return
    await q.message.edit_text(
        f"⚠️ <b>Delete?</b>\n\n<code>{rec['type']}</code> | <code>{rec['name']}</code>\n→ <code>{rec['content']}</code>\n\n<b>This cannot be undone!</b>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Delete", callback_data=f"dx_{rid}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"dns_{zid}")]]))

async def delete_execute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id; rid = q.data.replace("dx_", "")
    zid = get_s(uid).get("cur_zone", {}).get("id", "")
    try:
        cf_del(uid, f"/zones/{zid}/dns_records/{rid}")
        await q.message.edit_text("✅ Deleted.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Records", callback_data=f"dns_{zid}")]]))
    except Exception as e:
        await q.message.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

# ━━━━━━━━━━ /disconnect ━━━━━━━━━━
async def cmd_disconnect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    uid = update.effective_user.id
    if update.callback_query: await update.callback_query.answer()
    del_s(uid)
    await msg.reply_text("🔌 Disconnected.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Connect", callback_data="do_connect")]]))

# ━━━━━━━━━━ /stats (ADMIN ONLY) ━━━━━━━━━━
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
        f"📊 <b>KabutiFlare Stats</b>\n\n"
        f"👥 Total users: <b>{total_users}</b>\n"
        f"☁️ CF logins: <b>{total_cf}</b>\n"
        f"🟢 Active sessions: <b>{active_sessions}</b>\n\n"
        f"📋 <b>Recent users:</b>\n{recent or '  No users yet.'}\n"
        f"<i>✅ = connected to CF | ❌ = not connected</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ━━━━━━━━━━ /broadcast (ADMIN ONLY) ━━━━━━━━━━
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

# ━━━━━━━━━━ SSL/TLS ━━━━━━━━━━
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
        wait = await q.message.edit_text("⏳ Changing SSL...")
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

    wait = await q.message.edit_text("⏳ Fetching SSL...")
    r = cf_get(uid, f"/zones/{zid}/settings/ssl")
    if not r.get("success"):
        await wait.edit_text("❌ Error fetching SSL", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")]]))
        return

    current = r["result"]["value"]
    modes = {"off": "🔴 Off", "flexible": "🟡 Flexible", "full": "🟢 Full", "strict": "🔵 Full (Strict)"}
    text = f"🔒 <b>SSL/TLS — {zone.get('name', '')}</b>\n\nCurrent: <b>{modes.get(current, current)}</b>\n\nSelect a mode:"

    btns = []
    for m, label in modes.items():
        if m == current:
            btns.append([InlineKeyboardButton(f"✅ {label}", callback_data="noop")])
        else:
            btns.append([InlineKeyboardButton(label, callback_data=f"sslset_{zid}_{m}")])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")])

    await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━ NS INFO ━━━━━━━━━━
async def ns_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("ns_", "")
    s = get_s(uid)
    zone = s.get("cur_zone", {})

    wait = await q.message.edit_text("⏳ Fetching NS info...")
    r = cf_get(uid, f"/zones/{zid}")
    if not r.get("success"):
        await wait.edit_text("❌ Error", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")]]))
        return

    z = r["result"]
    ns_list = z.get("name_servers", [])
    orig_ns = z.get("original_name_servers", [])

    ns_txt = "\n".join(f"  <code>{n}</code>" for n in ns_list) or "—"
    orig_txt = "\n".join(f"  <code>{n}</code>" for n in orig_ns) or "—"

    text = (f"📛 <b>Nameservers — {z.get('name', '')}</b>\n\n"
            f"<b>Cloudflare Nameservers:</b>\n{ns_txt}\n\n"
            f"<b>Original Nameservers (Registrar):</b>\n{orig_txt}\n\n"
            f"Status: <b>{z.get('status', '—')}</b>")

    btns = [[InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")]]
    await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

# ━━━━━━━━━━ ZONE SETTINGS ━━━━━━━━━━
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
            except:
                body = {"value": val}
        wait = await q.message.edit_text("⏳ Applying...")
        r = cf_patch(uid, f"/zones/{zid}/settings/{setting}", body)
        if r.get("success"):
            await wait.edit_text("✅ Setting changed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"zs_{zid}")]]))
        else:
            err = r.get("errors", [{}])[0].get("message", "Error")
            await wait.edit_text(f"❌ {err}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"zs_{zid}")]]))
        return

    zid = d.replace("zs_", "")
    wait = await q.message.edit_text("⏳ Fetching settings...")
    try:
        results = {}
        for key, _ in ZONE_TOGGLES:
            try:
                r = cf_get(uid, f"/zones/{zid}/settings/{key}")
                results[key] = r["result"]["value"]
            except:
                results[key] = "—"

        text = f"🌐 <b>Zone Settings — {zone.get('name', '')}</b>\n\n"
        btns = []
        for key, label in ZONE_TOGGLES:
            val = results.get(key, "—")
            if val == "on":
                icon = "✅"
                next_val = "off"
            elif val == "off":
                icon = "❌"
                next_val = "on"
            else:
                icon = "🔘"
                next_val = "on"
            text += f"{icon} {label}: <b>{val}</b>\n"
            btns.append([InlineKeyboardButton(f"{icon} {label} → Toggle", callback_data=f"zst_{zid}_{key}_{next_val}")])

        btns.append([InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")])
        await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

# ━━━━━━━━━━ PAGE RULES ━━━━━━━━━━
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

        text = f"🔀 <b>Page Rules — {zone.get('name','')}</b>\n{len(rules)} rules\n\n"
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

# ━━━━━━━━━━ WORKERS ━━━━━━━━━━
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

        text = f"👷 <b>Workers — {zone.get('name','')}</b>\n{len(routes)} routes\n\n"
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

# ━━━━━━━━━━ EMAIL ROUTING ━━━━━━━━━━
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
        except:
            pass

        toggle_cb = f"emtog_{zid}_{'disable' if enabled else 'enable'}"
        btns = [
            [InlineKeyboardButton(f"{'❌ Disable' if enabled else '✅ Enable'} Routing", callback_data=toggle_cb)],
            [InlineKeyboardButton("🔙 Back", callback_data=f"zone_{zid}")],
        ]
        await wait.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        await wait.edit_text(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

# ━━━━━━━━━━ DEPLOY ━━━━━━━━━━
async def deploy_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
    else:
        msg = update.message

    if not HAS_PARAMIKO:
        await msg.reply_text(
            "⚠️ <b>Deploy feature unavailable</b>\n\nThe <code>paramiko</code> library is not installed.\n\nInstall it with:\n<code>pip install paramiko</code>",
            parse_mode="HTML"
        )
        return ConversationHandler.END

    await msg.reply_text(
        "🚀 <b>Deploy KabutiFlare to Server</b>\n\n"
        "This will install the bot on a remote server via SSH.\n\n"
        "Send the server <b>hostname or IP</b>:\n\n"
        "/cancel to abort.",
        parse_mode="HTML"
    )
    return DEP_HOST

async def deploy_host(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["dep_host"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Host: <code>{ctx.user_data['dep_host']}</code>\n\n"
        "Send the <b>SSH port</b> (default: 22):",
        parse_mode="HTML"
    )
    return DEP_PORT

async def deploy_port(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    port_text = update.message.text.strip()
    try:
        ctx.user_data["dep_port"] = int(port_text)
    except ValueError:
        ctx.user_data["dep_port"] = 22
    await update.message.reply_text(
        f"✅ Port: <code>{ctx.user_data['dep_port']}</code>\n\n"
        "Send the <b>SSH username</b> (e.g. root):",
        parse_mode="HTML"
    )
    return DEP_USER

async def deploy_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["dep_user"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ User: <code>{ctx.user_data['dep_user']}</code>\n\n"
        "Select authentication method:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Password", callback_data="dep_auth_pass")],
            [InlineKeyboardButton("🗝 SSH Key", callback_data="dep_auth_key")],
        ])
    )
    return DEP_AUTH

async def deploy_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "dep_auth_pass":
        ctx.user_data["dep_auth"] = "password"
        await q.message.edit_text(
            "🔑 Send your <b>SSH password</b>:\n\n<i>⚠️ Message will be deleted immediately.</i>",
            parse_mode="HTML"
        )
        return DEP_PASS
    else:
        ctx.user_data["dep_auth"] = "key"
        await q.message.edit_text(
            "🗝 Send your <b>SSH private key</b> content (PEM format):\n\n<i>⚠️ Message will be deleted immediately.</i>",
            parse_mode="HTML"
        )
        return DEP_KEY

async def deploy_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["dep_pass"] = update.message.text.strip()
    try:
        await update.message.delete()
    except:
        pass
    await update.effective_chat.send_message(
        "✅ Password received.\n\nSend the <b>Bot Token</b> for the deployed bot:",
        parse_mode="HTML"
    )
    return DEP_BOTTOKEN

async def deploy_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["dep_key"] = update.message.text.strip()
    try:
        await update.message.delete()
    except:
        pass
    await update.effective_chat.send_message(
        "✅ SSH key received.\n\nSend the <b>Bot Token</b> for the deployed bot:",
        parse_mode="HTML"
    )
    return DEP_BOTTOKEN

async def deploy_bottoken(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["dep_bottoken"] = update.message.text.strip()
    try:
        await update.message.delete()
    except:
        pass
    await update.effective_chat.send_message(
        "✅ Bot token received.\n\n"
        "Send the <b>WEBAPP_URL</b> (optional, press /skip to leave empty):",
        parse_mode="HTML"
    )
    return DEP_WEBAPP

async def deploy_webapp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ctx.user_data["dep_webapp"] = "" if text == "/skip" else text
    return await do_deploy(update, ctx)

def _run_deploy_ssh(host, port, user, auth, password, key_text, bot_token, webapp_url):
    """Blocking SSH deploy — runs in a thread via run_in_executor."""
    import time

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    if auth == "key":
        key_file = io.StringIO(key_text)
        try:
            pkey = paramiko.RSAKey.from_private_key(key_file)
        except paramiko.SSHException:
            key_file.seek(0)
            pkey = paramiko.Ed25519Key.from_private_key(key_file)
        ssh.connect(host, port=port, username=user, pkey=pkey, timeout=30)
    else:
        ssh.connect(host, port=port, username=user, password=password, timeout=30)

    webapp_answer = webapp_url if webapp_url else ""
    install_cmd = (
        f"printf '1\n{bot_token}\n{webapp_answer}\n' | "
        f"bash <(curl -fsSL https://raw.githubusercontent.com/Kabut27/kabutiflare/main/install.sh)"
    )

    stdin, stdout, stderr = ssh.exec_command(install_cmd, get_pty=True)

    output_lines = []
    for line in iter(stdout.readline, ""):
        line = line.strip()
        if line:
            output_lines.append(line)

    exit_status = stdout.channel.recv_exit_status()
    err_output = stderr.read().decode().strip()
    ssh.close()

    return exit_status, output_lines, err_output


async def do_deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.user_data

    host = d.get("dep_host", "")
    port = d.get("dep_port", 22)
    user = d.get("dep_user", "root")
    auth = d.get("dep_auth", "password")
    password = d.get("dep_pass", "")
    key_text = d.get("dep_key", "")
    bot_token = d.get("dep_bottoken", "")
    webapp_url = d.get("dep_webapp", "")

    wait = await update.message.reply_text(
        f"⏳ <b>Connecting to {host}:{port}...</b>\n\n"
        f"<i>This may take 3-5 minutes while Docker installs...</i>",
        parse_mode="HTML"
    )

    loop = asyncio.get_event_loop()

    # Keep sending "still working" updates every 20s so Telegram doesn't think bot died
    import time
    start_time = time.time()
    done = asyncio.Event()

    async def heartbeat():
        dots = 1
        while not done.is_set():
            await asyncio.sleep(20)
            if done.is_set():
                break
            elapsed = int(time.time() - start_time)
            try:
                await wait.edit_text(
                    f"⏳ <b>Installing on {host}...</b>\n\n"
                    f"<i>Running install.sh{'.' * dots} ({elapsed}s elapsed)</i>\n"
                    f"<i>Please wait, Docker setup takes a few minutes.</i>",
                    parse_mode="HTML"
                )
            except:
                pass
            dots = (dots % 3) + 1

    heartbeat_task = asyncio.create_task(heartbeat())

    try:
        exit_status, output_lines, err_output = await loop.run_in_executor(
            None,
            _run_deploy_ssh,
            host, port, user, auth, password, key_text, bot_token, webapp_url
        )
        done.set()
        heartbeat_task.cancel()

        if exit_status == 0:
            last_lines = "\n".join(output_lines[-5:]) if output_lines else ""
            await wait.edit_text(
                f"✅ <b>Deploy Successful!</b>\n\n"
                f"🖥 Server: <code>{host}:{port}</code>\n"
                f"👤 User: <code>{user}</code>\n"
                f"🐳 Bot is now running via Docker\n\n"
                f"<b>Useful commands on server:</b>\n"
                f"<code>docker compose -f /opt/kabutiflare/docker-compose.yml logs -f</code>\n"
                f"<code>docker compose -f /opt/kabutiflare/docker-compose.yml restart</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Home", callback_data="do_domains")
                ]])
            )
        else:
            err_preview = err_output[-400:] if err_output else "\n".join(output_lines[-5:]) or "Unknown error"
            await wait.edit_text(
                f"❌ <b>Install script failed (exit {exit_status})</b>\n\n"
                f"<code>{err_preview}</code>\n\n"
                f"Run manually on server:\n"
                f"<code>printf '1\nYOUR_TOKEN\n\n' | bash &lt;(curl -fsSL https://raw.githubusercontent.com/Kabut27/kabutiflare/main/install.sh)</code>",
                parse_mode="HTML"
            )

    except paramiko.AuthenticationException:
        done.set()
        heartbeat_task.cancel()
        await wait.edit_text(
            "❌ <b>Authentication failed!</b>\n\nCheck your username/password or SSH key.",
            parse_mode="HTML"
        )
    except Exception as e:
        done.set()
        heartbeat_task.cancel()
        await wait.edit_text(
            f"❌ <b>Deploy failed!</b>\n\n<code>{e}</code>",
            parse_mode="HTML"
        )

    return ConversationHandler.END

async def deploy_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Deploy cancelled.")
    return ConversationHandler.END

# ━━━━━━━━━━ MAIN ━━━━━━━━━━
async def noop_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()

async def post_init(app):
    commands = [
        BotCommand("start", "Main menu"),
        BotCommand("connect", "Connect Cloudflare account"),
        BotCommand("domains", "List your domains"),
        BotCommand("dns", "Manage DNS records"),
        BotCommand("disconnect", "Disconnect account"),
        BotCommand("help", "Help & guide"),
        BotCommand("deploy", "Deploy bot to a server"),
        BotCommand("stats", "Bot stats (admin only)"),
        BotCommand("broadcast", "Broadcast message (admin only)"),
    ]
    await app.bot.set_my_commands(commands)
    if WEBAPP_URL:
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="📊 Dashboard", web_app=WebAppInfo(url=WEBAPP_URL))
            )
        except:
            pass

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

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
        fallbacks=[
            CommandHandler("cancel", broadcast_cancel),
        ],
        allow_reentry=True,
    )

    deploy_conv = ConversationHandler(
        entry_points=[
            CommandHandler("deploy", deploy_start),
            CallbackQueryHandler(deploy_start, pattern="^do_deploy$"),
        ],
        states={
            DEP_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_host)],
            DEP_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_port)],
            DEP_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_user)],
            DEP_AUTH: [CallbackQueryHandler(deploy_auth, pattern="^dep_auth_")],
            DEP_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_pass)],
            DEP_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_key)],
            DEP_BOTTOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_bottoken)],
            DEP_WEBAPP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_webapp),
                CommandHandler("skip", deploy_webapp),
            ],
        },
        fallbacks=[CommandHandler("cancel", deploy_cancel)],
        allow_reentry=True,
    )

    app.add_handler(connect_conv)
    app.add_handler(add_conv)
    app.add_handler(edit_conv)
    app.add_handler(broadcast_conv)
    app.add_handler(deploy_conv)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", send_help))
    app.add_handler(CommandHandler("domains", cmd_domains))
    app.add_handler(CommandHandler("disconnect", cmd_disconnect))
    app.add_handler(CommandHandler("stats", cmd_stats))

    app.add_handler(CallbackQueryHandler(cmd_domains, pattern="^do_domains$"))
    app.add_handler(CallbackQueryHandler(cmd_disconnect, pattern="^do_disconnect$"))
    app.add_handler(CallbackQueryHandler(send_help, pattern="^do_help$"))
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

    logger.info("KabutiFlare bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
