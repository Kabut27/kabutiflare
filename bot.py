"""
KabutiFlare v2 — Telegram Bot + Distributed HA Auto-Failover
Manage Cloudflare DNS, SSL, Workers, Page Rules, Email Routing.
Admin: user tracking, broadcast, and 24/7 multi-node failover (admin only).
"""

import os
import re
import json
import math
import time
import signal
import secrets
import logging
import socket
import ipaddress
import asyncio
import threading
from html import escape
from datetime import datetime, timezone, timedelta
from functools import wraps

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo,
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault, MenuButtonWebApp,
)
from telegram.error import Conflict, TelegramError, BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, TypeHandler, filters,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CONFIG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _env(name, default=""):
    return os.getenv(name, default).strip()


def _env_int(name, default, minimum=None):
    try:
        v = int(_env(name, str(default)))
    except ValueError:
        v = default
    return max(minimum, v) if minimum is not None else v


def _env_bool(name, default=False):
    return _env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = _env("DATA_DIR") or os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "users.json")

BOT_TOKEN = _env("BOT_TOKEN")
WEBAPP_URL = _env("WEBAPP_URL")
CF_API = "https://api.cloudflare.com/client/v4"
CF_GRAPHQL = f"{CF_API}/graphql"

_admin_raw = _env("ADMIN_ID")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is required. Set it in .env")
if not _admin_raw.lstrip("-").isdigit():
    raise ValueError("ADMIN_ID is required (your numeric Telegram ID). Set it in .env")
ADMIN_ID = int(_admin_raw)

PRIVATE_MODE = _env_bool("PRIVATE_MODE", False)
ALLOWED_USERS = {int(x) for x in _env("ALLOWED_USERS").split(",") if x.strip().isdigit()}
ALLOWED_USERS.add(ADMIN_ID)

# ── Admin-only Auto-Failover & HA leader election ──
CF_FO_API_TOKEN = _env("CF_API_TOKEN")
CF_FO_ZONE_ID = _env("CF_ZONE_ID")
CONFIG_RECORD_NAME = _env("CONFIG_RECORD_NAME").rstrip(".")
TG_LEADER_RECORD_NAME = f"tgleader.{CONFIG_RECORD_NAME}" if CONFIG_RECORD_NAME else ""
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID") or str(ADMIN_ID)

FO_INIT_PRIORITY = _env("NODE_PRIORITY")
FO_INIT_RECORDS = _env("TARGET_RECORDS")

CHECK_METHOD = _env("CHECK_METHOD", "tcp").lower()
CHECK_PORT = _env_int("CHECK_PORT", 443)
CHECK_SCHEME = _env("CHECK_SCHEME", "https").lower()
HEALTH_PATH = _env("HEALTH_PATH", "/") or "/"
CHECK_TIMEOUT = _env_int("CHECK_TIMEOUT", 5, 1)
CHECK_INTERVAL = _env_int("CHECK_INTERVAL", 30, 10)
DNS_TTL = _env_int("DNS_TTL", 60)
DEFAULT_PROXIED = _env_bool("DEFAULT_PROXIED", False)

CF_TG_HEARTBEAT_INTERVAL = _env_int("CF_TG_HEARTBEAT_INTERVAL", 30, 10)
CF_TG_LEADER_TTL = _env_int("CF_TG_LEADER_TTL", 90, 30)

FO_ENABLED = bool(CF_FO_API_TOKEN and CF_FO_ZONE_ID and CONFIG_RECORD_NAME)

DNS_PER_PAGE = 10
ZONES_PER_PAGE = 8
USERS_PER_PAGE = 8

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("kabutiflare")


def _resolve_node_id() -> str:
    env_id = _env("NODE_ID")
    if env_id:
        return env_id
    id_file = os.path.join(DATA_DIR, "node-id")
    try:
        if os.path.isfile(id_file):
            with open(id_file, "r", encoding="utf-8") as f:
                val = f.read().strip()
            if val:
                return val
    except Exception:
        pass
    host = re.sub(r"[^a-zA-Z0-9_-]", "", socket.gethostname().split(".")[0]) or "node"
    nid = f"{host}-{secrets.token_hex(4)}"
    try:
        with open(id_file, "w", encoding="utf-8") as f:
            f.write(nid + "\n")
    except Exception:
        pass
    return nid


NODE_ID = _resolve_node_id()

# Force IPv4 (fixes Cloudflare/Telegram IPv6 edge cases). Disable with FORCE_IPV4=false on IPv6-only VPS.
if _env_bool("FORCE_IPV4", True):
    _orig_getaddrinfo = socket.getaddrinfo

    def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_getaddrinfo

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  UI KIT  (one consistent look for the whole bot)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DIV = "━━━━━━━━━━━━━━━━━━"


def h(val) -> str:
    """Escape dynamic text for Telegram HTML."""
    return escape(str(val)) if val is not None else ""


def hdr(icon, title, sub="") -> str:
    out = f"{icon}  <b>{h(title)}</b>\n"
    if sub:
        out += f"<i>{h(sub)}</i>\n"
    return out + f"{DIV}\n\n"


def loading(text) -> str:
    return f"⏳ <i>{h(text)}</i>"


def bar(pct, n=10) -> str:
    pct = max(0, min(100, pct))
    f = round(pct / 100 * n)
    return "▰" * f + "▱" * (n - f)


def btn(text, cb):
    return InlineKeyboardButton(text, callback_data=cb)


def back_row(cb, label="Back"):
    return [btn(f"‹ {label}", cb)]


def webapp_btn(text, zid=None):
    if not WEBAPP_URL:
        return None
    url = f"{WEBAPP_URL}?zone={zid}" if zid else WEBAPP_URL
    return InlineKeyboardButton(text, web_app=WebAppInfo(url=url))


def kb(rows):
    if not rows:
        return None
    clean = [[b for b in r if b] for r in rows]
    clean = [r for r in clean if r]
    return InlineKeyboardMarkup(clean) if clean else None


def err_text(e, title="Something went wrong") -> str:
    return f"❌ <b>{h(title)}</b>\n<code>{h(e)}</code>"


async def edit(msg, text, rows=None):
    try:
        return await msg.edit_text(
            text, parse_mode="HTML", reply_markup=kb(rows), disable_web_page_preview=True
        )
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return msg
        raise


async def say(msg, text, rows=None):
    return await msg.reply_text(
        text, parse_mode="HTML", reply_markup=kb(rows), disable_web_page_preview=True
    )


def msg_of(update):
    return update.callback_query.message if update.callback_query else update.message


async def render(update, text, rows=None):
    """Edit in place for button taps, reply for commands."""
    if update.callback_query:
        return await edit(update.callback_query.message, text, rows)
    return await say(update.message, text, rows)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s):
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fmt_dt(iso) -> str:
    dt = parse_iso(iso)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"


def ago(iso) -> str:
    dt = parse_iso(iso)
    if not dt:
        return "—"
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STATES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONNECT_METHOD, CONNECT_EMAIL, CONNECT_KEY = 0, 1, 2
ADD_TYPE, ADD_NAME, ADD_CONTENT, ADD_PROXY = 10, 11, 12, 13
EDIT_VALUE = 50
BROADCAST_MSG, BROADCAST_CONFIRM = 60, 61
EM_FROM, EM_TO = 200, 201
FO_ADD_IP, FO_REM_IP, FO_SET_PRIO, FO_ADD_REC, FO_REM_REC = 300, 301, 302, 303, 304

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  USER DATABASE (in-memory + atomic writes)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_db_lock = threading.RLock()
_db = None


def db() -> dict:
    global _db
    if _db is None:
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        d.setdefault("users", {})
        d.setdefault("cf_logins", {})
        d.setdefault("banned_users", [])
        _db = d
    return _db


def save_db():
    with _db_lock:
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, DB_FILE)


def track_user(uid, name="", username="") -> bool:
    """Returns True if this is a brand-new user."""
    d = db()
    k = str(uid)
    with _db_lock:
        u = d["users"].get(k)
        if u is None:
            now = now_iso()
            d["users"][k] = {"name": name, "username": username, "first_seen": now, "last_seen": now}
            save_db()
            return True
        changed = False
        if name and u.get("name") != name:
            u["name"] = name
            changed = True
        if u.get("username", "") != username:
            u["username"] = username
            changed = True
        last = parse_iso(u.get("last_seen", ""))
        if not last or (datetime.now(timezone.utc) - last).total_seconds() > 300:
            u["last_seen"] = now_iso()
            changed = True
        if changed:
            save_db()
        return False


def track_cf_login(uid, name=""):
    with _db_lock:
        db()["cf_logins"][str(uid)] = {"name": name, "last_login": now_iso()}
        save_db()


def is_admin(uid) -> bool:
    return uid == ADMIN_ID


def is_banned(uid) -> bool:
    return uid != ADMIN_ID and uid in db()["banned_users"]


def ban_user(uid):
    if uid == ADMIN_ID:
        return
    with _db_lock:
        if uid not in db()["banned_users"]:
            db()["banned_users"].append(uid)
            save_db()
    del_s(uid)


def unban_user(uid):
    with _db_lock:
        db()["banned_users"] = [u for u in db()["banned_users"] if u != uid]
        save_db()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  IN-MEMORY SESSIONS (Cloudflare credentials never touch disk)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
sessions = {}


def get_s(uid):
    return sessions.get(uid, {})


def set_s(uid, data):
    sessions.setdefault(uid, {}).update(data)


def del_s(uid):
    sessions.pop(uid, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLOUDFLARE API (per-user sessions)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def cf_h(uid):
    s = get_s(uid)
    if s.get("auth") == "token":
        return {"Authorization": f"Bearer {s['key']}", "Content-Type": "application/json"}
    return {"X-Auth-Email": s.get("email", ""), "X-Auth-Key": s.get("key", ""),
            "Content-Type": "application/json"}


def _cf_errors(d, status=None) -> str:
    msg = ", ".join(e.get("message", "") for e in (d.get("errors") or []))
    return msg or (f"HTTP {status}" if status else "Unknown error")


def cf_call(uid, method, path, body=None, params=None, strict=True):
    r = requests.request(method, f"{CF_API}{path}", headers=cf_h(uid), json=body,
                         params=params, timeout=15)
    try:
        d = r.json()
    except ValueError:
        raise Exception(f"HTTP {r.status_code} (invalid response)")
    if strict and not d.get("success"):
        raise Exception(_cf_errors(d, r.status_code))
    return d


async def acf_get(uid, path, params=None):
    return await asyncio.to_thread(cf_call, uid, "GET", path, None, params)


async def acf_post(uid, path, body):
    return await asyncio.to_thread(cf_call, uid, "POST", path, body)


async def acf_put(uid, path, body):
    return await asyncio.to_thread(cf_call, uid, "PUT", path, body)


async def acf_del(uid, path):
    return await asyncio.to_thread(cf_call, uid, "DELETE", path)


async def acf_patch(uid, path, body):
    return await asyncio.to_thread(cf_call, uid, "PATCH", path, body, None, False)


async def acf_all(uid, path, per_page=50, extra=None):
    out, page = [], 1
    while True:
        p = {"per_page": per_page, "page": page}
        if extra:
            p.update(extra)
        d = await acf_get(uid, path, p)
        out.extend(d.get("result", []))
        if page >= (d.get("result_info") or {}).get("total_pages", 1):
            break
        page += 1
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FAILOVER ENGINE & DISTRIBUTED LEADER ELECTION (admin only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DOMAIN_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

fo_runtime = {
    "is_leader": False,
    "leader_id": "",
    "last_active_ip": "",
    "last_check_ts": 0,
    "health_map": {},
    "all_down": False,
}


def is_valid_ip(ip: str) -> bool:
    try:
        ipaddress.IPv4Address(ip.strip())
        return True
    except Exception:
        return False


def is_valid_domain(domain: str) -> bool:
    d = domain.strip().lower().rstrip(".")
    return len(d) <= 253 and bool(DOMAIN_RE.match(d))


def fo_cf_api(method: str, endpoint: str, body=None, params=None):
    """Cloudflare call using the admin's .env token & zone, with 1 retry."""
    url = f"{CF_API}/zones/{CF_FO_ZONE_ID}{endpoint}"
    headers = {"Authorization": f"Bearer {CF_FO_API_TOKEN}", "Content-Type": "application/json"}
    last_err = None
    for attempt in range(2):
        try:
            r = requests.request(method, url, headers=headers, json=body, params=params, timeout=12)
            d = r.json()
            if d.get("success"):
                return d
            last_err = _cf_errors(d, r.status_code)
        except Exception as e:
            last_err = str(e)
        if attempt == 0:
            time.sleep(1)
    raise Exception(last_err or "Cloudflare API error")


async def afo_cf_api(method, endpoint, body=None, params=None):
    return await asyncio.to_thread(fo_cf_api, method, endpoint, body, params)


def fo_update_txt_record(rec_name: str, content: str) -> bool:
    resp = fo_cf_api("GET", "/dns_records", params={"type": "TXT", "name": rec_name})
    existing = resp.get("result", [])
    payload = {"type": "TXT", "name": rec_name, "content": content, "ttl": 60}
    if existing:
        fo_cf_api("PUT", f"/dns_records/{existing[0]['id']}", body=payload)
        for extra in existing[1:]:
            try:
                fo_cf_api("DELETE", f"/dns_records/{extra['id']}")
            except Exception:
                pass
    else:
        fo_cf_api("POST", "/dns_records", body=payload)
    return True


def fo_get_shared_config() -> tuple[list[str], list[str]]:
    """Read PRIORITY & RECORDS from the shared Cloudflare TXT record."""
    resp = fo_cf_api("GET", "/dns_records", params={"type": "TXT", "name": CONFIG_RECORD_NAME})
    existing = resp.get("result", [])
    txt_val = (existing[0].get("content", "") if existing else "").strip().strip('"')

    if not txt_val:
        init_ips = [x.strip() for x in FO_INIT_PRIORITY.split(",") if is_valid_ip(x.strip())]
        init_recs = [x.strip().lower().rstrip(".") for x in FO_INIT_RECORDS.split(",")
                     if is_valid_domain(x.strip())]
        if init_ips or init_recs:
            try:
                fo_save_shared_config(init_ips, init_recs)
            except Exception as e:
                logger.warning(f"Failed seeding initial shared config TXT: {e}")
        return init_ips, init_recs

    ips, recs = [], []
    for part in txt_val.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().upper()
        if k in ("PRIORITY", "NODE_PRIORITY"):
            ips = [x.strip() for x in v.split(",") if x.strip()]
        elif k in ("RECORDS", "TARGET_RECORDS"):
            recs = [x.strip().lower().rstrip(".") for x in v.split(",") if x.strip()]
    return ips, recs


def fo_save_shared_config(ips: list[str], recs: list[str]) -> bool:
    return fo_update_txt_record(CONFIG_RECORD_NAME, f"PRIORITY={','.join(ips)};RECORDS={','.join(recs)}")


def fo_claim_leadership() -> bool:
    try:
        fo_update_txt_record(TG_LEADER_RECORD_NAME, f"{NODE_ID}:{int(time.time())}")
        fo_runtime["leader_id"] = NODE_ID
        return True
    except Exception as e:
        logger.warning(f"claim_leadership failed: {e}")
        return False


def fo_release_leadership():
    if not FO_ENABLED or not TG_LEADER_RECORD_NAME:
        return
    try:
        fo_update_txt_record(TG_LEADER_RECORD_NAME, f"{NODE_ID}:0")
        logger.info(f"Released leadership ({NODE_ID}).")
    except Exception as e:
        logger.warning(f"release_leadership failed: {e}")


def fo_read_leader_txt() -> tuple[str, int]:
    resp = fo_cf_api("GET", "/dns_records", params={"type": "TXT", "name": TG_LEADER_RECORD_NAME})
    existing = resp.get("result", [])
    txt_val = (existing[0].get("content", "") if existing else "").strip().strip('"')
    if ":" in txt_val:
        lid, lts = txt_val.rsplit(":", 1)
        return lid.strip(), int(lts) if lts.isdigit() else 0
    return "", 0


async def fo_try_become_leader() -> bool:
    """Leader election through a Cloudflare TXT lease, with a 2s anti-race verify."""
    try:
        leader_id, leader_ts = await asyncio.to_thread(fo_read_leader_txt)
    except Exception as e:
        logger.warning(f"Leader check failed: {e}")
        return False

    age = int(time.time()) - leader_ts
    if leader_id == NODE_ID and age < CF_TG_LEADER_TTL:
        fo_runtime["leader_id"] = NODE_ID
        return True
    if leader_id and age < CF_TG_LEADER_TTL:
        fo_runtime["leader_id"] = leader_id
        return False

    if not await asyncio.to_thread(fo_claim_leadership):
        return False
    await asyncio.sleep(2)
    try:
        verify_id, _ = await asyncio.to_thread(fo_read_leader_txt)
        fo_runtime["leader_id"] = verify_id
        return verify_id == NODE_ID
    except Exception:
        return False


async def fo_heartbeat() -> str:
    """Renew the lease. Returns 'ok', 'lost' (someone else owns it) or 'error'."""
    try:
        lid, ts = await asyncio.to_thread(fo_read_leader_txt)
    except Exception:
        return "error"
    if lid and lid != NODE_ID and (time.time() - ts) < CF_TG_LEADER_TTL:
        fo_runtime["leader_id"] = lid
        return "lost"
    return "ok" if await asyncio.to_thread(fo_claim_leadership) else "error"


def fo_send_alert_sync(text: str):
    if not BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Alert send failed: {e}")


async def fo_send_alert(text: str):
    await asyncio.to_thread(fo_send_alert_sync, text)


async def fo_check_node_health(ip: str) -> bool:
    if CHECK_METHOD in ("http", "https"):
        url = f"{CHECK_SCHEME}://{ip}:{CHECK_PORT}{HEALTH_PATH}"

        def _probe():
            try:
                r = requests.get(url, timeout=CHECK_TIMEOUT, verify=False, allow_redirects=False)
                return 200 <= r.status_code < 400
            except Exception:
                return False

        return await asyncio.to_thread(_probe)
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, CHECK_PORT), timeout=CHECK_TIMEOUT)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def fo_check_all_nodes_parallel(ips: list[str]) -> dict[str, str]:
    if not ips:
        return {}
    results = await asyncio.gather(*(fo_check_node_health(ip) for ip in ips))
    return {ip: ("UP" if ok else "DOWN") for ip, ok in zip(ips, results)}


def fo_ensure_domain_points_to(domain: str, target_ip: str) -> int:
    """0 = already correct, 2 = updated/created, 1 = failed. Keeps the proxied flag."""
    try:
        resp = fo_cf_api("GET", "/dns_records", params={"type": "A", "name": domain})
        existing = resp.get("result", [])
        rec_id = existing[0]["id"] if existing else ""
        cur_ip = existing[0].get("content", "") if existing else ""
        cur_proxied = existing[0].get("proxied", DEFAULT_PROXIED) if existing else DEFAULT_PROXIED

        if rec_id and cur_ip == target_ip:
            return 0
        payload = {"type": "A", "name": domain, "content": target_ip, "ttl": DNS_TTL,
                   "proxied": bool(cur_proxied)}
        if rec_id:
            fo_cf_api("PUT", f"/dns_records/{rec_id}", body=payload)
        else:
            fo_cf_api("POST", "/dns_records", body=payload)
        logger.info(f"DNS UPDATED: {domain} -> {target_ip} (proxied={cur_proxied})")
        return 2
    except Exception as e:
        logger.error(f"Failed updating DNS {domain} -> {target_ip}: {e}")
        return 1


async def fo_run_one_cycle(notify: bool = True) -> dict:
    """One failover cycle: probe all nodes, confirm DOWNs, point domains to best UP node."""
    ips, domains = await asyncio.to_thread(fo_get_shared_config)
    if not ips or not domains:
        return {"skipped": True, "reason": "Orodha ya nodes au domains ni tupu."}

    health = await fo_check_all_nodes_parallel(ips)

    # Anti-flapping: re-probe every DOWN node once after 2s before trusting it
    downs = [ip for ip in ips if health.get(ip) == "DOWN"]
    if downs:
        await asyncio.sleep(2)
        health.update(await fo_check_all_nodes_parallel(downs))

    prev = fo_runtime.get("health_map", {})
    transitions = [(ip, health[ip]) for ip in ips if ip in prev and prev[ip] != health[ip]]
    fo_runtime["health_map"] = health
    fo_runtime["last_check_ts"] = int(time.time())

    best_ip = next((ip for ip in ips if health.get(ip) == "UP"), "")

    if not best_ip:
        logger.warning("ALL FAILOVER NODES ARE DOWN!")
        if notify and not fo_runtime["all_down"]:
            await fo_send_alert(
                f"🚨 <b>TAHADHARI KUU</b>\n<i>Node: {h(NODE_ID)}</i>\n{DIV}\n\n"
                "Nodes <b>ZOTE</b> kwenye orodha ya Priority hazijibu (OFFLINE).\n"
                "DNS haijabadilishwa."
            )
        fo_runtime["all_down"] = True
        return {"ok": False, "all_down": True, "health_map": health}
    fo_runtime["all_down"] = False

    changed, failed = [], []
    for dom in domains:
        rc = await asyncio.to_thread(fo_ensure_domain_points_to, dom, best_ip)
        if rc == 2:
            changed.append(dom)
        elif rc == 1:
            failed.append(dom)

    if notify and (changed or failed or transitions):
        lines = [f"⚡ <b>FAILOVER</b>\n<i>Node: {h(NODE_ID)}</i>\n{DIV}\n"]
        for ip, st in transitions:
            lines.append(f"{'🟢 Imerudi' if st == 'UP' else '🔴 Imezimika'}: <code>{h(ip)}</code>")
        if changed:
            lines.append(f"\n🎯 <b>IP Mpya:</b> <code>{h(best_ip)}</code>")
            lines.append(f"🔄 <b>IP ya Awali:</b> <code>{h(fo_runtime.get('last_active_ip') or 'Hakuna')}</code>")
            lines.append(f"🌐 <b>Domains:</b> <code>{h(', '.join(changed))}</code>")
        if failed:
            lines.append(f"\n⚠️ Imeshindwa kusasisha: <code>{h(', '.join(failed))}</code>")
        await fo_send_alert("\n".join(lines))

    fo_runtime["last_active_ip"] = best_ip
    return {"ok": True, "best_ip": best_ip, "changed": changed, "failed": failed, "health_map": health}


async def failover_monitor_loop(shutdown_event: asyncio.Event):
    """Runs on every node but ONLY the elected leader probes and writes DNS."""
    logger.info(f"⚡ Failover monitor ready on {NODE_ID} "
                f"({CHECK_METHOD.upper()}:{CHECK_PORT}, every {CHECK_INTERVAL}s, leader-only)")
    await asyncio.sleep(5)
    while not shutdown_event.is_set():
        if fo_runtime.get("is_leader"):
            try:
                await fo_run_one_cycle(notify=True)
            except Exception as e:
                logger.error(f"Failover cycle error: {e}")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=CHECK_INTERVAL)
        except asyncio.TimeoutError:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GATE (ban / private mode + user tracking) & DECORATORS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _is_blocked(uid: int) -> bool:
    return is_banned(uid) or (PRIVATE_MODE and uid not in ALLOWED_USERS)


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if _is_blocked(user.id):
        if update.callback_query:
            await update.callback_query.answer("⛔ Access denied.", show_alert=True)
        elif update.message:
            await update.message.reply_text("⛔ <b>Access denied.</b>", parse_mode="HTML")
        raise ApplicationHandlerStop
    try:
        is_new = track_user(user.id, user.first_name or "", user.username or "")
    except Exception as e:
        logger.error(f"track_user error: {e}")
        return
    if is_new and not is_admin(user.id):
        uname = f" @{h(user.username)}" if user.username else ""
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"🆕 <b>New user</b>\n{DIV}\n\n👤 <b>{h(user.first_name or 'User')}</b>{uname}\n"
                f"🆔 <code>{user.id}</code>",
                parse_mode="HTML",
                reply_markup=kb([[btn("👤 View user", f"ud_{user.id}")]]),
            )
        except Exception:
            pass


def need_auth(func):
    @wraps(func)
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not get_s(user.id).get("key"):
            if update.callback_query:
                await update.callback_query.answer()
            await say(
                msg_of(update),
                hdr("🔒", "Not connected", "Link Cloudflare first") +
                "Connect your Cloudflare account to continue.",
                [[btn("🔐 Connect Cloudflare", "do_connect")]],
            )
            return ConversationHandler.END
        return await func(update, ctx)
    return w


def admin_only(func):
    @wraps(func)
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_admin(user.id):
            if update.callback_query:
                await update.callback_query.answer("⛔ Admin only.", show_alert=True)
            elif update.message:
                await update.message.reply_text("⛔ <b>Admin only.</b>", parse_mode="HTML")
            return ConversationHandler.END
        return await func(update, ctx)
    return w


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /start  &  /help
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    user = update.effective_user
    uid = user.id
    logged = bool(get_s(uid).get("key"))

    text = hdr("☁️", "KabutiFlare", "Cloudflare control center, inside Telegram")
    text += f"Hey <b>{h(user.first_name or 'there')}</b> 👋\n\n"
    if logged:
        text += "🟢 <b>Connected</b> to Cloudflare\n<i>Pick something to manage:</i>"
        rows = [[btn("🌐 Domains", "do_domains"), btn("📊 Analytics", "do_analytics")],
                [webapp_btn("🖥 Open Dashboard")]]
    else:
        text += "⚪ <b>Not connected</b>\n<i>Link your Cloudflare account to get started.</i>"
        rows = [[btn("🔐 Connect Cloudflare", "do_connect")]]
    if is_admin(uid):
        rows.append([btn("👑 Admin Panel", "adm_menu")])
    last = [btn("📖 Help", "do_help")]
    if logged:
        last.append(btn("🔌 Disconnect", "do_disconnect"))
    rows.append(last)
    await render(update, text, rows)


async def cancel_and_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)
    return ConversationHandler.END


async def cmd_cancel_idle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Nothing to cancel. Use /start to open the menu.")


async def send_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    text = hdr("📖", "Help & Guide")
    text += (
        "<b>Commands</b>\n"
        "/start · /connect · /domains · /analytics · /disconnect\n\n"
        "<b>What you can do</b>\n"
        "📋 DNS — view, add, edit, delete\n"
        "🔒 SSL/TLS — switch encryption mode\n"
        "🌐 Zone settings — HTTPS, TLS, HTTP/3, Brotli…\n"
        "🔀 Page rules · 👷 Workers · 📧 Email routing\n"
        "📊 Analytics — traffic, cache, threats (24h)\n\n"
        "<b>API token permissions</b>\n"
        "<code>Zone · DNS · Edit</code>\n"
        "<code>Zone · Zone Settings · Edit</code>\n"
        "<code>Zone · SSL and Certificates · Edit</code>\n"
        "<code>Zone · Page Rules · Edit</code>\n"
        "<code>Zone · Workers Routes · Edit</code>\n"
        "<code>Zone · Email Routing Rules · Edit</code>\n"
        "<code>Zone · Analytics · Read</code>\n"
        "Zone Resources → <b>All zones</b>\n"
    )
    if is_admin(update.effective_user.id):
        text += (
            f"\n👑 <b>Admin</b>\n"
            "/admin · /users · /broadcast · /ban · /unban · /banlist\n"
            "/failover · /status · /listips · /listrecords · /cleanup\n"
            "/addip &lt;ip&gt; [pos] · /removeip &lt;ip&gt;\n"
            "/setpriority &lt;ip1,ip2&gt;\n"
            "/addrecord &lt;dom&gt; · /removerecord &lt;dom&gt;\n"
        )
    await render(update, text, [[btn("🔐 Connect", "do_connect"), btn("🏠 Home", "do_start")]])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /connect
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def connect_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    text = hdr("🔐", "Connect Cloudflare", "Choose how to authenticate") + \
        "An <b>API Token</b> is safer: you control exactly what it can do."
    rows = [[btn("🛡 API Token  ·  Recommended", "m_token")],
            [btn("🔑 Global API Key + Email", "m_apikey")],
            [btn("✖ Cancel", "m_cancel")]]
    try:
        await render(update, text, rows)
    except Exception:
        await say(msg_of(update), text, rows)
    return CONNECT_METHOD


async def connect_method(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "m_cancel":
        await edit(q.message, "✖ Cancelled.", [[btn("🏠 Home", "do_start")]])
        return ConversationHandler.END
    if q.data == "m_token":
        ctx.user_data["auth"] = "token"
        await edit(q.message, hdr("🛡", "API Token") + "Paste your token below.\n\n"
                   "<i>🧹 Your message is deleted right away for security.</i>")
        return CONNECT_KEY
    if q.data == "m_apikey":
        ctx.user_data["auth"] = "apikey"
        await edit(q.message, hdr("🔑", "Global API Key") +
                   "First, send your Cloudflare <b>email address</b>:")
        return CONNECT_EMAIL
    return CONNECT_METHOD


async def connect_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["email"] = update.message.text.strip()
    await say(update.message,
              f"📧 <code>{h(ctx.user_data['email'])}</code>\n\nNow send your <b>Global API Key</b>:\n\n"
              "<i>🧹 Your message is deleted right away for security.</i>")
    return CONNECT_KEY


async def connect_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    key = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    method = ctx.user_data.get("auth", "token")
    set_s(uid, {"key": key, "auth": method, "email": ctx.user_data.get("email", "")})
    wait = await update.effective_chat.send_message(loading("Verifying your credentials…"),
                                                    parse_mode="HTML")
    try:
        if method == "token":
            await acf_get(uid, "/user/tokens/verify")
        else:
            await acf_get(uid, "/zones", {"per_page": 1})
        try:
            track_cf_login(uid, update.effective_user.first_name or "")
        except Exception as e:
            logger.error(f"track_cf_login error: {e}")
        await edit(wait, hdr("✅", "Connected!") + "Your Cloudflare account is now linked.",
                   [[btn("🌐 View my domains", "do_domains")], [webapp_btn("📊 Open Dashboard")]])
    except Exception as e:
        del_s(uid)
        await edit(wait, err_text(e, "Connection failed"), [[btn("🔄 Try again", "do_connect")]])
    return ConversationHandler.END


async def connect_cancel_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✖ Cancelled.")
    return ConversationHandler.END


async def cmd_disconnect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    del_s(update.effective_user.id)
    await render(update, hdr("🔌", "Disconnected") + "Your Cloudflare session was cleared.",
                 [[btn("🔐 Connect again", "do_connect")]])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DOMAINS & ZONE OVERVIEW
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STATUS_DOT = {"active": "🟢", "pending": "🟡", "moved": "🔴", "deactivated": "🔴"}


@need_auth
async def cmd_domains(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    page = 0
    cached = None
    if q:
        await q.answer()
        if q.data.startswith("dp_"):
            page = int(q.data[3:])
            cached = get_s(uid).get("zones")

    m = await render(update, loading("Fetching your domains…"))
    try:
        zones = cached if cached is not None else await acf_all(uid, "/zones")
        set_s(uid, {"zones": zones})
        if not zones:
            await edit(m, hdr("📭", "No domains") + "This Cloudflare account has no domains yet.",
                       [[btn("🏠 Home", "do_start")]])
            return

        pages = max(1, math.ceil(len(zones) / ZONES_PER_PAGE))
        page = max(0, min(page, pages - 1))
        chunk = zones[page * ZONES_PER_PAGE:(page + 1) * ZONES_PER_PAGE]
        text = hdr("🌐", "Your Domains", f"{len(zones)} total" + (f" · page {page + 1}/{pages}" if pages > 1 else ""))
        text += "<i>Tap a domain to manage it.</i>"
        rows = []
        for z in chunk:
            plan = z.get("plan", {}).get("name", "Free")
            rows.append([btn(f"{STATUS_DOT.get(z['status'], '⚪')} {z['name']}  ·  {plan}", f"zone_{z['id']}")])
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(btn("‹ Prev", f"dp_{page - 1}"))
            nav.append(btn(f"{page + 1}/{pages}", "noop"))
            if page < pages - 1:
                nav.append(btn("Next ›", f"dp_{page + 1}"))
            rows.append(nav)
        rows.append([btn("🔄 Refresh", "do_domains"), btn("🏠 Home", "do_start")])
        await edit(m, text, rows)
    except Exception as e:
        await edit(m, err_text(e), [[btn("🏠 Home", "do_start")]])


@need_auth
async def zone_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("zone_", "")
    zone = next((z for z in get_s(uid).get("zones", []) if z["id"] == zid), None)
    if not zone:
        try:
            zone = (await acf_get(uid, f"/zones/{zid}"))["result"]
        except Exception:
            await edit(q.message, "❌ Domain not found. Please refresh.", [back_row("do_domains", "Domains")])
            return
    set_s(uid, {"cur_zone": zone})
    ns = "\n".join(f"  • <code>{h(n)}</code>" for n in zone.get("name_servers", []))
    text = (
        hdr("🌐", zone["name"]) +
        f"{STATUS_DOT.get(zone['status'], '⚪')} <b>{h(zone['status'].title())}</b>   ·   "
        f"💳 <b>{h(zone.get('plan', {}).get('name', 'Free'))}</b>\n\n"
        f"📛 <b>Nameservers</b>\n{ns or '  —'}"
    )
    rows = [
        [btn("📋 DNS Records", f"dns_{zid}"), btn("➕ Add Record", f"add_{zid}")],
        [btn("🔒 SSL/TLS", f"ssl_{zid}"), btn("📛 Nameservers", f"ns_{zid}")],
        [btn("⚙️ Settings", f"zs_{zid}"), btn("🔀 Page Rules", f"pr_{zid}")],
        [btn("👷 Workers", f"wk_{zid}"), btn("📧 Email", f"em_{zid}")],
        [btn("📊 Analytics", f"analytics_{zid}")],
        [webapp_btn("🖥 Open in Dashboard", zid)],
        back_row("do_domains", "All domains"),
    ]
    await edit(q.message, text, rows)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DNS  (list · filter · add · edit · proxy · delete)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TYPE_ICON = {"A": "🔵", "AAAA": "🟣", "CNAME": "🟢", "MX": "🟡",
             "TXT": "🩷", "NS": "🔷", "SRV": "🟠", "CAA": "🟤"}


def _short(s, n=25):
    return s[:n] + ("…" if len(s) > n else "")


@need_auth
async def dns_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("dns_", "")
    zone = get_s(uid).get("cur_zone", {})
    m = await edit(q.message, loading("Fetching DNS records…"))
    try:
        recs = await acf_all(uid, f"/zones/{zid}/dns_records", per_page=100)
        set_s(uid, {"dns": recs})
        if not recs:
            await edit(m, hdr("📭", "No DNS records", zone.get("name", "")),
                       [[btn("➕ Add record", f"add_{zid}")], back_row(f"zone_{zid}")])
            return
        counts = {}
        for r in recs:
            counts[r["type"]] = counts.get(r["type"], 0) + 1
        text = hdr("📋", "DNS Records", f"{zone.get('name', '')} · {len(recs)} records")
        rows, row = [], []
        for t, c in sorted(counts.items()):
            icon = TYPE_ICON.get(t, "⚪")
            text += f"{icon} <b>{h(t)}</b>  {c}\n"
            row.append(btn(f"{icon} {t} ({c})", f"ft_{zid}_{t}_0"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([btn("➕ Add record", f"add_{zid}")])
        rows.append(back_row(f"zone_{zid}"))
        await edit(m, text, rows)
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"zone_{zid}")])


@need_auth
async def dns_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    parts = q.data.split("_")
    zid, rtype = parts[1], parts[2]
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    s = get_s(uid)
    zone = s.get("cur_zone", {})
    zone_name = zone.get("name", "")
    recs = [r for r in s.get("dns", []) if r["type"] == rtype]

    total = len(recs)
    pages = max(1, math.ceil(total / DNS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = recs[page * DNS_PER_PAGE:(page + 1) * DNS_PER_PAGE]

    icon = TYPE_ICON.get(rtype, "⚪")
    text = hdr(icon, f"{rtype} Records", f"{zone_name} · {total} record(s)" +
               (f" · page {page + 1}/{pages}" if pages > 1 else ""))
    rows = []
    for r in chunk:
        nm = r["name"]
        if zone_name and nm == zone_name:
            nm = "@"
        elif zone_name and nm.endswith(f".{zone_name}"):
            nm = nm[: -(len(zone_name) + 1)]
        ct = _short(r.get("content", ""))
        px = "☁️" if r.get("proxied") else "🔘"
        text += f"<code>{h(nm)}</code> → <code>{h(ct)}</code> {px}\n"
        rows.append([btn(f"✏️ {nm} → {ct}", f"ed_{r['id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("‹ Prev", f"ft_{zid}_{rtype}_{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn("Next ›", f"ft_{zid}_{rtype}_{page + 1}"))
        rows.append(nav)
    rows.append([btn("‹ All records", f"dns_{zid}"), btn("➕ Add", f"add_{zid}")])
    await edit(q.message, text, rows)


def build_dns_body(t, name, content, proxied):
    """Build a Cloudflare DNS payload. Raises ValueError on bad input."""
    body = {"type": t, "name": name, "ttl": 1}
    content = content.strip()
    if t == "A":
        if not is_valid_ip(content):
            raise ValueError("Invalid IPv4 address")
        body["content"] = content
    elif t == "AAAA":
        try:
            ipaddress.IPv6Address(content)
        except Exception:
            raise ValueError("Invalid IPv6 address")
        body["content"] = content
    elif t == "MX":
        p = content.split()
        if len(p) == 2 and p[0].isdigit():
            body["priority"], body["content"] = int(p[0]), p[1]
        else:
            body["priority"], body["content"] = 10, content
    elif t == "SRV":
        p = content.split()
        if len(p) != 4 or not all(x.isdigit() for x in p[:3]):
            raise ValueError("Format: priority weight port target")
        body["data"] = {"priority": int(p[0]), "weight": int(p[1]), "port": int(p[2]), "target": p[3]}
    elif t == "CAA":
        m = re.match(r'^(\d+)\s+(issue|issuewild|iodef)\s+"?([^"]+)"?$', content)
        if not m:
            raise ValueError('Format: 0 issue "letsencrypt.org"')
        body["data"] = {"flags": int(m.group(1)), "tag": m.group(2), "value": m.group(3)}
    else:
        body["content"] = content
    if t in ("A", "AAAA", "CNAME"):
        body["proxied"] = bool(proxied)
    return body


@need_auth
async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    zid = q.data.replace("add_", "")
    ctx.user_data["add_zid"] = zid
    types = ["A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "CAA"]
    rows = [[btn(f"{TYPE_ICON.get(t, '⚪')} {t}", f"at_{t}") for t in types[i:i + 4]] for i in range(0, 8, 4)]
    rows.append([btn("✖ Cancel", f"zone_{zid}")])
    await edit(q.message, hdr("➕", "New DNS Record", "Step 1 · choose a type"), rows)
    return ADD_TYPE


async def add_cancel_to_zone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await zone_selected(update, ctx)
    return ConversationHandler.END


async def add_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    t = q.data.replace("at_", "")
    ctx.user_data["add_type"] = t
    ph = {"A": "@ or www", "AAAA": "@ or www", "CNAME": "www", "MX": "@", "TXT": "@ or _dmarc",
          "NS": "sub", "SRV": "_sip._tcp", "CAA": "@"}
    await edit(q.message, hdr("➕", f"{t} Record", "Step 2 · name") +
               f"Send the <b>name</b>:\n<code>{h(ph.get(t, ''))}</code>   <i>(root = @)</i>\n\n"
               "<i>/cancel to abort</i>")
    return ADD_NAME


async def add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["add_name"] = update.message.text.strip()
    hints = {
        "A": "<code>1.2.3.4</code>", "AAAA": "<code>2001:db8::1</code>",
        "CNAME": "<code>target.example.com</code>",
        "MX": "<code>mail.example.com</code> or <code>10 mail.example.com</code>",
        "TXT": "<code>v=spf1 include:_spf.google.com ~all</code>", "NS": "<code>ns1.example.com</code>",
        "SRV": "<code>priority weight port target</code>", "CAA": '<code>0 issue "letsencrypt.org"</code>',
    }
    t = ctx.user_data["add_type"]
    await say(update.message, hdr("➕", f"{t} Record", "Step 3 · value") +
              f"Name: <code>{h(ctx.user_data['add_name'])}</code>\n\nSend the <b>value</b>:\n{hints.get(t, '')}")
    return ADD_CONTENT


async def add_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    content = update.message.text.strip()
    t = ctx.user_data["add_type"]
    try:
        build_dns_body(t, "x", content, False)
    except ValueError as e:
        await say(update.message, f"⚠️ {h(e)}\nTry again or /cancel.")
        return ADD_CONTENT
    ctx.user_data["add_content"] = content
    if t in ("A", "AAAA", "CNAME"):
        await say(update.message, hdr("➕", f"{t} Record", "Step 4 · proxy") +
                  f"Value: <code>{h(content)}</code>\n\nProxy through Cloudflare?",
                  [[btn("☁️ Proxied", "px_on"), btn("🔘 DNS only", "px_off")]])
        return ADD_PROXY
    ctx.user_data["add_proxy"] = False
    return await do_add_submit(update, ctx)


async def add_proxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["add_proxy"] = q.data == "px_on"
    return await do_add_submit(update, ctx)


async def do_add_submit(update, ctx):
    q = update.callback_query
    msg = q.message if q else update.message
    uid = q.from_user.id if q else update.effective_user.id
    zid = ctx.user_data["add_zid"]
    t = ctx.user_data["add_type"]
    name = ctx.user_data["add_name"].strip().lower()
    zone_name = get_s(uid).get("cur_zone", {}).get("name", "")
    if name in ("@", zone_name):
        name = zone_name
    elif not name.endswith(f".{zone_name}"):
        name = f"{name}.{zone_name}"

    wait = await say(msg, loading("Creating record…"))
    try:
        body = build_dns_body(t, name, ctx.user_data["add_content"], ctx.user_data.get("add_proxy", False))
        await acf_post(uid, f"/zones/{zid}/dns_records", body)
        px = "  ☁️ Proxied" if body.get("proxied") else ("  🔘 DNS only" if "proxied" in body else "")
        await edit(wait, hdr("✅", "Record created") +
                   f"<code>{h(t)}</code> · <code>{h(name)}</code>\n→ <code>{h(ctx.user_data['add_content'])}</code>{px}",
                   [[btn("📋 View records", f"dns_{zid}"), btn("➕ Add another", f"add_{zid}")]])
    except Exception as e:
        await edit(wait, err_text(e), [back_row(f"zone_{zid}")])
    return ConversationHandler.END


@need_auth
async def edit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    rid = q.data.replace("ed_", "")
    s = get_s(uid)
    rec = next((r for r in s.get("dns", []) if r["id"] == rid), None)
    zid = s.get("cur_zone", {}).get("id", "")
    if not rec:
        await edit(q.message, "❌ Record not found. Go back and refresh.", [back_row(f"dns_{zid}")])
        return
    px = "☁️ Proxied" if rec.get("proxied") else "🔘 DNS only"
    ttl = "Auto" if rec.get("ttl") == 1 else f"{rec.get('ttl')}s"
    text = (hdr("✏️", "Edit Record") +
            f"<code>{h(rec['type'])}</code> · <code>{h(rec['name'])}</code>\n"
            f"→ <code>{h(rec.get('content', ''))}</code>\nTTL {ttl}" +
            (f"  ·  {px}" if rec["type"] in ("A", "AAAA", "CNAME") else ""))
    rows = []
    if rec["type"] not in ("SRV", "CAA"):
        rows.append([btn("📝 Change value", f"ec_{rid}")])
    if rec["type"] in ("A", "AAAA", "CNAME"):
        rows.append([btn(f"☁️ {'Disable' if rec.get('proxied') else 'Enable'} proxy", f"ep_{rid}")])
    rows.append([btn("🗑 Delete", f"dl_{rid}")])
    rows.append(back_row(f"dns_{zid}"))
    await edit(q.message, text, rows)


@need_auth
async def toggle_proxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    rid = q.data.replace("ep_", "")
    s = get_s(uid)
    rec = next((r for r in s.get("dns", []) if r["id"] == rid), None)
    zid = s.get("cur_zone", {}).get("id", "")
    if not rec:
        return
    new_px = not rec.get("proxied", False)
    m = await edit(q.message, loading("Updating…"))
    try:
        await acf_put(uid, f"/zones/{zid}/dns_records/{rid}", {
            "type": rec["type"], "name": rec["name"], "content": rec["content"],
            "ttl": rec.get("ttl", 1), "proxied": new_px})
        rec["proxied"] = new_px
        await edit(m, hdr("✅", "Proxy " + ("enabled ☁️" if new_px else "disabled 🔘")) +
                   f"<code>{h(rec['name'])}</code>", [[btn("📋 Records", f"dns_{zid}")]])
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"dns_{zid}")])


@need_auth
async def edit_content_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    rid = q.data.replace("ec_", "")
    rec = next((r for r in get_s(q.from_user.id).get("dns", []) if r["id"] == rid), None)
    if not rec:
        return ConversationHandler.END
    ctx.user_data["edit_rec"] = rec
    ctx.user_data["edit_rid"] = rid
    await edit(q.message, hdr("📝", "Change value") +
               f"Current:\n<code>{h(rec.get('content', ''))}</code>\n\nSend the new value (or /cancel):")
    return EDIT_VALUE


async def edit_content_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    val = update.message.text.strip()
    rec = ctx.user_data.get("edit_rec")
    rid = ctx.user_data.get("edit_rid")
    zid = get_s(uid).get("cur_zone", {}).get("id", "")
    if not rec:
        await update.message.reply_text("❌ Record not found.")
        return ConversationHandler.END
    try:
        body = build_dns_body(rec["type"], rec["name"], val, rec.get("proxied", False))
        body["ttl"] = rec.get("ttl", 1)
        if rec["type"] == "MX" and len(val.split()) != 2:
            body["priority"] = rec.get("priority", 10)
    except ValueError as e:
        await say(update.message, f"⚠️ {h(e)}\nTry again or /cancel.")
        return EDIT_VALUE
    wait = await say(update.message, loading("Updating…"))
    try:
        await acf_put(uid, f"/zones/{zid}/dns_records/{rid}", body)
        await edit(wait, hdr("✅", "Updated") +
                   f"<code>{h(rec['name'])}</code>\n→ <code>{h(val)}</code>",
                   [[btn("📋 Records", f"dns_{zid}")]])
    except Exception as e:
        await edit(wait, err_text(e), [back_row(f"dns_{zid}")])
    return ConversationHandler.END


@need_auth
async def delete_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    rid = q.data.replace("dl_", "")
    s = get_s(q.from_user.id)
    rec = next((r for r in s.get("dns", []) if r["id"] == rid), None)
    zid = s.get("cur_zone", {}).get("id", "")
    if not rec:
        return
    await edit(q.message, hdr("⚠️", "Delete record?") +
               f"<code>{h(rec['type'])}</code> · <code>{h(rec['name'])}</code>\n"
               f"→ <code>{h(rec.get('content', ''))}</code>\n\n<b>This cannot be undone.</b>",
               [[btn("🗑 Yes, delete", f"dx_{rid}"), btn("✖ Cancel", f"dns_{zid}")]])


@need_auth
async def delete_execute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    rid = q.data.replace("dx_", "")
    zid = get_s(uid).get("cur_zone", {}).get("id", "")
    try:
        await acf_del(uid, f"/zones/{zid}/dns_records/{rid}")
        await edit(q.message, hdr("✅", "Record deleted"), [[btn("📋 Records", f"dns_{zid}")]])
    except Exception as e:
        await edit(q.message, err_text(e), [back_row(f"dns_{zid}")])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SSL/TLS & NAMESERVERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SSL_MODES = {"off": "🔴 Off", "flexible": "🟡 Flexible", "full": "🟢 Full", "strict": "🔵 Full (Strict)"}


async def _render_ssl(msg, zone_name, zid, current, note=""):
    text = hdr("🔒", "SSL/TLS", zone_name) + f"Current mode: <b>{SSL_MODES.get(current, h(current))}</b>\n"
    if note:
        text += f"\n{note}\n"
    text += "\n<i>Select a mode:</i>"
    rows = [[btn(f"✅ {label}", "noop") if m == current else btn(label, f"sslset_{zid}_{m}")]
            for m, label in SSL_MODES.items()]
    rows.append(back_row(f"zone_{zid}"))
    await edit(msg, text, rows)


@need_auth
async def ssl_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("ssl_", "")
    zone = get_s(uid).get("cur_zone", {})
    m = await edit(q.message, loading("Fetching SSL settings…"))
    try:
        r = await acf_get(uid, f"/zones/{zid}/settings/ssl")
        await _render_ssl(m, zone.get("name", ""), zid, r["result"]["value"])
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"zone_{zid}")])


@need_auth
async def ssl_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid, mode = q.data.replace("sslset_", "").split("_", 1)
    zone = get_s(uid).get("cur_zone", {})
    m = await edit(q.message, loading("Changing SSL mode…"))
    try:
        r = await acf_patch(uid, f"/zones/{zid}/settings/ssl", {"value": mode})
        if not r.get("success"):
            raise Exception(_cf_errors(r))
        await _render_ssl(m, zone.get("name", ""), zid, mode, f"✅ Changed to <b>{h(SSL_MODES.get(mode, mode))}</b>")
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"ssl_{zid}")])


@need_auth
async def ns_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("ns_", "")
    m = await edit(q.message, loading("Fetching nameservers…"))
    try:
        z = (await acf_get(uid, f"/zones/{zid}"))["result"]
        fmt = lambda lst: "\n".join(f"  • <code>{h(n)}</code>" for n in lst) or "  —"
        await edit(m, hdr("📛", "Nameservers", z.get("name", "")) +
                   f"☁️ <b>Cloudflare</b>\n{fmt(z.get('name_servers', []))}\n\n"
                   f"🏷 <b>Original (registrar)</b>\n{fmt(z.get('original_name_servers', []))}\n\n"
                   f"Status: <b>{h(z.get('status', '—'))}</b>", [back_row(f"zone_{zid}")])
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"zone_{zid}")])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ZONE SETTINGS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ZONE_TOGGLES = [
    ("always_use_https", "🔗 Always HTTPS"),
    ("automatic_https_rewrites", "🔄 Auto HTTPS Rewrites"),
    ("tls_1_3", "🔐 TLS 1.3"),
    ("http3", "🌐 HTTP/3"),
    ("0rtt", "⚡ 0-RTT"),
    ("brotli", "🗜 Brotli"),
    ("early_hints", "💡 Early Hints"),
    ("websockets", "🔌 WebSockets"),
    ("opportunistic_encryption", "🔒 Opp. Encryption"),
]
TLS_VERSIONS = ["1.0", "1.1", "1.2", "1.3"]


async def _set_zone_setting(uid, zid, key, value):
    r = await acf_patch(uid, f"/zones/{zid}/settings/{key}", {"value": value})
    if not r.get("success"):
        raise Exception(_cf_errors(r))


async def _render_zone_settings(msg, zone_name, zid, results, note=""):
    text = hdr("⚙️", "Zone Settings", zone_name)
    if note:
        text += f"{note}\n\n"
    min_tls = results.get("min_tls_version", "—")
    rows = [[btn(f"🔐 Min TLS · {min_tls}", f"tls_{zid}")]]
    for idx, (key, label) in enumerate(ZONE_TOGGLES):
        val = results.get(key, "—")
        icon = "🟢" if val == "on" else ("⚫" if val == "off" else "⚪")
        text += f"{icon} {h(label)}  <b>{h(val)}</b>\n"
        rows.append([btn(f"{icon} {label}", f"zst_{zid}_{idx}")])
    text += f"\n🔐 Min TLS  <b>{h(min_tls)}</b>\n<i>Tap a setting to toggle it.</i>"
    rows.append(back_row(f"zone_{zid}"))
    await edit(msg, text, rows)


@need_auth
async def zone_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    d = q.data
    s = get_s(uid)
    zone = s.get("cur_zone", {})
    zname = zone.get("name", "")
    cache = s.get("zs_results", {})

    if d.startswith("tlsset_"):
        zid, ver = d[len("tlsset_"):].rsplit("_", 1)
        m = await edit(q.message, loading(f"Setting Min TLS to {ver}…"))
        try:
            await _set_zone_setting(uid, zid, "min_tls_version", ver)
            cache["min_tls_version"] = ver
            set_s(uid, {"zs_results": cache})
            await _render_zone_settings(m, zname, zid, cache, f"✅ Min TLS → <b>{h(ver)}</b>")
        except Exception as e:
            await edit(m, err_text(e), [back_row(f"zs_{zid}")])
        return

    if d.startswith("tls_"):
        zid = d[len("tls_"):]
        cur = cache.get("min_tls_version", "1.2")
        rows = [[btn(f"✅ TLS {v}", "noop") if v == cur else btn(f"TLS {v}", f"tlsset_{zid}_{v}")]
                for v in TLS_VERSIONS]
        rows.append(back_row(f"zs_{zid}", "Settings"))
        await edit(q.message, hdr("🔐", "Min TLS Version", zname) +
                   f"Current: <b>TLS {h(cur)}</b>\n\nChoose the minimum version (<b>1.2</b> recommended):", rows)
        return

    if d.startswith("zst_"):
        zid, idx = d[len("zst_"):].rsplit("_", 1)
        key, label = ZONE_TOGGLES[int(idx)]
        new_val = "off" if cache.get(key, "off") == "on" else "on"
        m = await edit(q.message, loading(f"Toggling {label}…"))
        try:
            await _set_zone_setting(uid, zid, key, new_val)
            cache[key] = new_val
            set_s(uid, {"zs_results": cache})
            await _render_zone_settings(m, zname, zid, cache, f"✅ {h(label)} → <b>{new_val}</b>")
        except Exception as e:
            await edit(m, err_text(e, label), [back_row(f"zs_{zid}")])
        return

    zid = d.replace("zs_", "")
    m = await edit(q.message, loading("Fetching zone settings…"))
    try:
        r = await acf_get(uid, f"/zones/{zid}/settings")
        results = {item["id"]: item.get("value", "—") for item in r.get("result", [])}
        set_s(uid, {"zs_results": results})
        await _render_zone_settings(m, zname, zid, results)
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"zone_{zid}")])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PAGE RULES & WORKERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@need_auth
async def page_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("pr_", "")
    zone = get_s(uid).get("cur_zone", {})
    m = await edit(q.message, loading("Fetching page rules…"))
    try:
        rules = (await acf_get(uid, f"/zones/{zid}/pagerules", {"status": "active", "per_page": 50}))["result"]
        set_s(uid, {"pagerules": rules})
        if not rules:
            await edit(m, hdr("📭", "No page rules", zone.get("name", "")),
                       [[btn("➕ Add rule", f"pradd_{zid}")], back_row(f"zone_{zid}")])
            return
        text = hdr("🔀", "Page Rules", f"{zone.get('name', '')} · {len(rules)} rule(s)")
        rows = []
        for rule in rules:
            url = rule.get("targets", [{}])[0].get("constraint", {}).get("value", "?")
            icon = "🟢" if rule.get("status") == "active" else "⏸"
            text += f"{icon} <code>{h(url[:40])}</code>\n"
            rows.append([btn(f"{icon} {url[:35]}", f"pred_{rule['id']}")])
        rows.append([btn("➕ Add rule", f"pradd_{zid}")])
        rows.append(back_row(f"zone_{zid}"))
        await edit(m, text, rows)
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"zone_{zid}")])


@need_auth
async def page_rule_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    zid = q.data.replace("pradd_", "")
    zone_name = get_s(q.from_user.id).get("cur_zone", {}).get("name", "")
    await edit(q.message, hdr("➕", "Add Page Rule") +
               "⚠️ Cloudflare has deprecated the Page Rules API.\n\nCreate new rules in the dashboard:\n"
               f"<a href='https://dash.cloudflare.com/?to=/:account/{h(zone_name)}/rules'>Open Rules for {h(zone_name)}</a>",
               [back_row(f"pr_{zid}")])


@need_auth
async def page_rule_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    rid = q.data.replace("pred_", "")
    s = get_s(uid)
    zid = s.get("cur_zone", {}).get("id", "")
    rule = next((r for r in s.get("pagerules", []) if r["id"] == rid), None)
    if not rule:
        await edit(q.message, "❌ Rule not found. Go back and refresh.", [back_row(f"pr_{zid}")])
        return
    url = rule.get("targets", [{}])[0].get("constraint", {}).get("value", "?")
    acts = ""
    for a in rule.get("actions", []):
        aval = a.get("value", "")
        if isinstance(aval, dict):
            aval = ", ".join(f"{k}={v}" for k, v in aval.items())
        acts += f"  • {h(a.get('id', '?').replace('_', ' ').title())}: <code>{h(aval)}</code>\n"
    await edit(q.message, hdr("🔀", "Page Rule") + f"🔗 <code>{h(url)}</code>\n"
               f"Status: {'🟢 Active' if rule.get('status') == 'active' else '⏸ Disabled'}\n\n"
               f"<b>Actions</b>\n{acts or '  —'}",
               [[btn("🗑 Delete rule", f"prdel_{rid}")], back_row(f"pr_{zid}")])


@need_auth
async def page_rule_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    rid = q.data.replace("prdel_", "")
    zid = get_s(uid).get("cur_zone", {}).get("id", "")
    m = await edit(q.message, loading("Deleting page rule…"))
    try:
        await acf_del(uid, f"/zones/{zid}/pagerules/{rid}")
        await edit(m, hdr("✅", "Page rule deleted"), [back_row(f"pr_{zid}")])
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"pr_{zid}")])


@need_auth
async def workers_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("wk_", "")
    zone = get_s(uid).get("cur_zone", {})
    m = await edit(q.message, loading("Fetching workers…"))
    try:
        routes = (await acf_get(uid, f"/zones/{zid}/workers/routes"))["result"]
        if not routes:
            await edit(m, hdr("📭", "No worker routes", zone.get("name", "")), [back_row(f"zone_{zid}")])
            return
        text = hdr("👷", "Workers", f"{zone.get('name', '')} · {len(routes)} route(s)")
        rows = []
        for route in routes[:15]:
            pattern = route.get("pattern", "?")
            text += f"<code>{h(pattern[:35])}</code> → <b>{h(route.get('script', '—'))}</b>\n"
            rows.append([btn(f"🔧 {pattern[:30]}", f"wkr_{route['id']}")])
        rows.append(back_row(f"zone_{zid}"))
        await edit(m, text, rows)
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"zone_{zid}")])


@need_auth
async def worker_route_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    rid = q.data.replace("wkr_", "")
    zid = get_s(uid).get("cur_zone", {}).get("id", "")
    m = await edit(q.message, loading("Fetching route…"))
    try:
        route = (await acf_get(uid, f"/zones/{zid}/workers/routes/{rid}"))["result"]
        await edit(m, hdr("🔧", "Worker Route") + f"🌐 Pattern: <code>{h(route.get('pattern', '?'))}</code>\n"
                   f"👷 Script: <b>{h(route.get('script') or '— (none)')}</b>",
                   [[btn("🗑 Delete route", f"wkrdel_{rid}")], back_row(f"wk_{zid}")])
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"wk_{zid}")])


@need_auth
async def worker_route_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    rid = q.data.replace("wkrdel_", "")
    zid = get_s(uid).get("cur_zone", {}).get("id", "")
    m = await edit(q.message, loading("Deleting route…"))
    try:
        await acf_del(uid, f"/zones/{zid}/workers/routes/{rid}")
        await edit(m, hdr("✅", "Route deleted"), [back_row(f"wk_{zid}")])
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"wk_{zid}")])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EMAIL ROUTING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _rule_parts(rule):
    matchers = rule.get("matchers") or [{}]
    actions = rule.get("actions") or [{}]
    m_type, m_val = matchers[0].get("type", ""), matchers[0].get("value", "")
    a_type = actions[0].get("type", "?")
    a_val = (actions[0].get("value") or [""])[0]
    return m_type, m_val, a_type, a_val


@need_auth
async def email_routing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("em_", "")
    zone = get_s(uid).get("cur_zone", {})
    m = await edit(q.message, loading("Fetching email routing…"))
    try:
        enabled = (await acf_get(uid, f"/zones/{zid}/email/routing"))["result"].get("enabled", False)
        rules = []
        try:
            rules = (await acf_get(uid, f"/zones/{zid}/email/routing/rules"))["result"]
        except Exception:
            pass
        set_s(uid, {"em_rules": rules})

        text = hdr("📧", "Email Routing", zone.get("name", "")) + \
            f"Status: {'🟢 <b>Enabled</b>' if enabled else '🔴 <b>Disabled</b>'}\n📋 <b>{len(rules)}</b> rule(s)\n"
        rows = []
        catch_lines, normal_lines = "", ""
        for i, rule in enumerate(rules):
            m_type, m_val, a_type, a_val = _rule_parts(rule)
            on = "🟢" if rule.get("enabled") else "⏸"
            tog = "⏸ Disable" if rule.get("enabled") else "▶️ Enable"
            if m_type == "all" or not m_val:
                label = f"Forward → <code>{h(a_val)}</code>" if a_type == "forward" else "Drop (reject)"
                catch_lines += f"{on} 🌐 {label}\n"
                rows.append([btn(f"🌐 {tog} catch-all", f"emtogr_{zid}_{i}"), btn("🗑", f"emdel_{zid}_{i}")])
            else:
                normal_lines += f"{on} <code>{h(m_val)}</code>\n     ↪ <code>{h(a_val)}</code>\n"
                rows.append([btn(f"{tog} {m_val}"[:40], f"emtogr_{zid}_{i}"), btn("🗑", f"emdel_{zid}_{i}")])
        if normal_lines:
            text += f"\n<b>Forwarding</b>\n{normal_lines}"
        if catch_lines:
            text += f"\n<b>Catch-all</b>\n{catch_lines}"
        if not rules:
            text += "\n<i>No rules yet.</i>"
        rows.append([btn("➕ Add rule", f"emadd_{zid}")])
        rows.append([btn("🔴 Disable routing" if enabled else "🟢 Enable routing",
                         f"emtog_{zid}_{'disable' if enabled else 'enable'}")])
        rows.append(back_row(f"zone_{zid}"))
        await edit(m, text, rows)
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"zone_{zid}")])


@need_auth
async def email_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    zid = q.data.replace("emadd_", "")
    ctx.user_data["em_zid"] = zid
    zone_name = get_s(q.from_user.id).get("cur_zone", {}).get("name", "")
    await edit(q.message, hdr("➕", "Add Email Rule", "Step 1 of 2 · source") +
               f"Send the <b>source address</b> on your domain:\n<code>hello@{h(zone_name)}</code>\n\n"
               "<i>/cancel to abort</i>")
    return EM_FROM


async def email_add_from(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from_addr = update.message.text.strip().lower()
    if "@" not in from_addr:
        await say(update.message, "⚠️ Send a valid email address:")
        return EM_FROM
    ctx.user_data["em_from"] = from_addr
    await say(update.message, hdr("➕", "Add Email Rule", "Step 2 of 2 · destination") +
              f"From: <code>{h(from_addr)}</code>\n\nSend the <b>destination address</b>:\n<code>you@gmail.com</code>")
    return EM_TO


async def email_add_to(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    to_addr = update.message.text.strip().lower()
    if "@" not in to_addr:
        await say(update.message, "⚠️ Send a valid email address:")
        return EM_TO
    uid = update.effective_user.id
    zid = ctx.user_data.get("em_zid", "")
    from_addr = ctx.user_data.get("em_from", "")
    wait = await say(update.message, loading("Creating rule…"))
    try:
        await acf_post(uid, f"/zones/{zid}/email/routing/rules", {
            "enabled": True, "name": f"Forward {from_addr} → {to_addr}",
            "matchers": [{"field": "to", "type": "literal", "value": from_addr}],
            "actions": [{"type": "forward", "value": [to_addr]}]})
        await edit(wait, hdr("✅", "Rule created") + f"📨 <code>{h(from_addr)}</code>\n     ↪ <code>{h(to_addr)}</code>",
                   [[btn("📧 View rules", f"em_{zid}"), btn("➕ Add another", f"emadd_{zid}")]])
    except Exception as e:
        await edit(wait, err_text(e, "Failed to create rule"), [back_row(f"em_{zid}")])
    return ConversationHandler.END


@need_auth
async def email_toggle_rule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid, idx = q.data.replace("emtogr_", "").rsplit("_", 1)
    rules = get_s(uid).get("em_rules", [])
    if int(idx) >= len(rules):
        await edit(q.message, "❌ Rule not found.", [back_row(f"em_{zid}")])
        return
    rule = rules[int(idx)]
    tag = rule.get("tag", rule.get("id", ""))
    m = await edit(q.message, loading("Updating rule…"))
    try:
        new_enabled = not rule.get("enabled", True)
        await acf_put(uid, f"/zones/{zid}/email/routing/rules/{tag}", {
            "enabled": new_enabled, "name": rule.get("name", ""),
            "matchers": rule.get("matchers", []), "actions": rule.get("actions", [])})
        await edit(m, hdr("✅", "Rule " + ("enabled" if new_enabled else "disabled")), [back_row(f"em_{zid}", "Email")])
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"em_{zid}")])


@need_auth
async def email_delete_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    zid, idx = q.data.replace("emdel_", "").rsplit("_", 1)
    rules = get_s(q.from_user.id).get("em_rules", [])
    detail = f"rule #{idx}"
    if int(idx) < len(rules):
        _, m_val, _, a_val = _rule_parts(rules[int(idx)])
        detail = f"<code>{h(m_val or 'catch-all')}</code>\n     ↪ <code>{h(a_val)}</code>"
    await edit(q.message, hdr("⚠️", "Delete rule?") + f"{detail}\n\n<b>This cannot be undone.</b>",
               [[btn("🗑 Yes, delete", f"emdelx_{zid}_{idx}"), btn("✖ Cancel", f"em_{zid}")]])


@need_auth
async def email_delete_execute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid, idx = q.data.replace("emdelx_", "").rsplit("_", 1)
    rules = get_s(uid).get("em_rules", [])
    if int(idx) >= len(rules):
        await edit(q.message, "❌ Rule not found.", [back_row(f"em_{zid}")])
        return
    tag = rules[int(idx)].get("tag", rules[int(idx)].get("id", ""))
    m = await edit(q.message, loading("Deleting rule…"))
    try:
        await acf_del(uid, f"/zones/{zid}/email/routing/rules/{tag}")
        await edit(m, hdr("✅", "Rule deleted"), [[btn("📧 View rules", f"em_{zid}")]])
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"em_{zid}")])


@need_auth
async def email_toggle_routing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid, action = q.data.replace("emtog_", "").rsplit("_", 1)
    enable = action == "enable"
    m = await edit(q.message, loading("Updating email routing…"))
    try:
        await acf_post(uid, f"/zones/{zid}/email/routing/{'enable' if enable else 'disable'}", {})
        await edit(m, hdr("✅", "Email Routing " + ("enabled" if enable else "disabled")),
                   [[btn("📧 View routing", f"em_{zid}")]])
    except Exception as e:
        await edit(m, err_text(e), [back_row(f"em_{zid}")])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ANALYTICS (GraphQL, last 24h)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _fmt_bytes(b):
    for unit, size in (("GB", 1_073_741_824), ("MB", 1_048_576), ("KB", 1024)):
        if b >= size:
            return f"{b / size:.2f} {unit}" if unit != "KB" else f"{b / size:.1f} KB"
    return f"{b} B"


def _fmt_num(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _cf_analytics_zone(uid, zid):
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = """
    { viewer { zones(filter: { zoneTag: "%s" }) {
        httpRequests1hGroups(limit: 48, filter: { datetime_geq: "%s", datetime_leq: "%s" }) {
          sum { requests cachedRequests bytes cachedBytes threats pageViews
                countryMap { clientCountryName requests }
                responseStatusMap { edgeResponseStatus requests } }
          uniq { uniques }
        } } } }
    """ % (zid, since, until)
    try:
        r = requests.post(CF_GRAPHQL, headers=cf_h(uid), json={"query": query}, timeout=20)
        d = r.json()
        if d.get("errors"):
            return {"ok": False, "error": ", ".join(e.get("message", "GraphQL error") for e in d["errors"])}
        zones_data = (((d.get("data") or {}).get("viewer") or {}).get("zones")) or []
        if not zones_data:
            return {"ok": False, "error": "No analytics data returned for this zone."}
        groups = zones_data[0].get("httpRequests1hGroups", [])

        t = dict(req=0, cached=0, bw=0, cbw=0, threats=0, pv=0, visitors=0)
        by_country, status_map = {}, {}
        for g in groups:
            sm = g.get("sum", {})
            t["req"] += sm.get("requests", 0)
            t["cached"] += sm.get("cachedRequests", 0)
            t["bw"] += sm.get("bytes", 0)
            t["cbw"] += sm.get("cachedBytes", 0)
            t["threats"] += sm.get("threats", 0)
            t["pv"] += sm.get("pageViews", 0)
            t["visitors"] += (g.get("uniq") or {}).get("uniques", 0)
            for c in sm.get("countryMap", []):
                k = c.get("clientCountryName", "??")
                by_country[k] = by_country.get(k, 0) + c.get("requests", 0)
            for st in sm.get("responseStatusMap", []):
                k = str(st.get("edgeResponseStatus", "?"))
                status_map[k] = status_map.get(k, 0) + st.get("requests", 0)
        return {
            "ok": True, **t,
            "cache_pct": round(t["cached"] / t["req"] * 100, 1) if t["req"] else 0,
            "bw_pct": round(t["cbw"] / t["bw"] * 100, 1) if t["bw"] else 0,
            "countries": sorted(by_country.items(), key=lambda x: x[1], reverse=True)[:5],
            "status": sorted(status_map.items(), key=lambda x: -x[1])[:5],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@need_auth
async def zone_analytics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    zid = q.data.replace("analytics_", "")
    s = get_s(uid)
    zone = next((z for z in s.get("zones", []) if z["id"] == zid), s.get("cur_zone", {}))
    if zone:
        set_s(uid, {"cur_zone": zone})
    zname = zone.get("name", zid)

    m = await edit(q.message, loading(f"Fetching analytics for {zname}…"))
    data = await asyncio.to_thread(_cf_analytics_zone, uid, zid)
    if not data["ok"]:
        await edit(m, err_text(data["error"], "Could not fetch analytics") +
                   "\n\n<i>Your token needs <code>Zone · Analytics · Read</code>.</i>", [back_row(f"zone_{zid}")])
        return

    countries = "".join(f"  🌍 <b>{h(c)}</b>  {_fmt_num(n)}\n" for c, n in data["countries"]) or "  <i>No data</i>\n"
    statuses = ""
    for code, n in data["status"]:
        icon = "🟢" if code.startswith("2") else ("🟡" if code.startswith("3") else "🔴")
        statuses += f"  {icon} <b>{h(code)}</b>  {_fmt_num(n)}\n"
    statuses = statuses or "  <i>No data</i>\n"
    th = data["threats"]
    threat_icon = "🛡️" if th == 0 else ("⚠️" if th < 100 else "🚨")

    text = (
        hdr("📊", "Analytics", f"{zname} · last 24h") +
        f"<b>Traffic</b>\n  📥 Requests  <b>{_fmt_num(data['req'])}</b>\n"
        f"  👁 Pageviews  <b>{_fmt_num(data['pv'])}</b>\n"
        f"  👤 Visitors  <b>{_fmt_num(data['visitors'])}</b>  <i>(hourly sum)</i>\n\n"
        f"<b>Cache</b>  {data['cache_pct']}%\n  {bar(data['cache_pct'])}\n"
        f"  ✅ {_fmt_num(data['cached'])}  ·  🔄 {_fmt_num(max(0, data['req'] - data['cached']))}\n\n"
        f"<b>Bandwidth</b>  {_fmt_bytes(data['bw'])}\n  {bar(data['bw_pct'])}\n"
        f"  cached {_fmt_bytes(data['cbw'])} ({data['bw_pct']}%)\n\n"
        f"{threat_icon} <b>Threats</b>  {_fmt_num(th)}\n\n"
        f"<b>Top countries</b>\n{countries}\n<b>HTTP status</b>\n{statuses}"
    )
    await edit(m, text, [[btn("🔄 Refresh", f"analytics_{zid}")], back_row(f"zone_{zid}", "Zone")])


@need_auth
async def global_analytics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    uid = update.effective_user.id
    zones = get_s(uid).get("zones")
    m = None
    if not zones:
        m = await render(update, loading("Fetching zones…"))
        try:
            zones = await acf_all(uid, "/zones")
            set_s(uid, {"zones": zones})
        except Exception as e:
            await edit(m, err_text(e), [[btn("🏠 Home", "do_start")]])
            return
    if not zones:
        await (edit(m, hdr("📭", "No domains")) if m else render(update, hdr("📭", "No domains")))
        return
    text = hdr("📊", "Analytics", "Pick a domain · last 24 hours")
    rows = [[btn(f"{STATUS_DOT.get(z['status'], '⚪')} {z['name']}", f"analytics_{z['id']}")] for z in zones[:40]]
    rows.append([btn("🏠 Home", "do_start")])
    if m:
        await edit(m, text, rows)
    else:
        await render(update, text, rows)


async def noop_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ADMIN PANEL · USERS · BAN · BROADCAST
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _user_icon(uid_str, d):
    if int(uid_str) in d["banned_users"]:
        return "🚫"
    return "☁️" if uid_str in d["cf_logins"] else "👤"


def _user_title(info):
    name = info.get("name") or "User"
    return f"{name} @{info['username']}" if info.get("username") else name


@admin_only
async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    d = db()
    day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    new24 = sum(1 for u in d["users"].values() if (parse_iso(u.get("first_seen", "")) or day_ago) > day_ago)
    active24 = sum(1 for u in d["users"].values() if (parse_iso(u.get("last_seen", "")) or day_ago) > day_ago)
    live = len([u for u in sessions.values() if u.get("key")])
    text = (
        hdr("👑", "Admin Panel", f"Node {NODE_ID}") +
        f"👥 Users  <b>{len(d['users'])}</b>   ·   🆕 24h  <b>{new24}</b>\n"
        f"🟢 Active 24h  <b>{active24}</b>   ·   ☁️ Connected  <b>{len(d['cf_logins'])}</b>\n"
        f"🔗 Live sessions  <b>{live}</b>   ·   🚫 Banned  <b>{len(d['banned_users'])}</b>\n\n"
        f"⚡ Failover  <b>{'🟢 Active' if FO_ENABLED else '⚪ Not configured'}</b>\n"
        f"🔒 Private mode  <b>{'On' if PRIVATE_MODE else 'Off'}</b>"
    )
    await render(update, text, [
        [btn("👥 Users", "ul_0"), btn("🚫 Banned", "adm_banned")],
        [btn("📢 Broadcast", "adm_bc"), btn("⚡ Auto-Failover", "fo_menu")],
        [btn("🔄 Refresh", "adm_menu"), btn("🏠 Home", "do_start")],
    ])


@admin_only
async def users_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    page = 0
    if q:
        await q.answer()
        if q.data.startswith("ul_"):
            page = int(q.data[3:])
    d = db()
    items = sorted(d["users"].items(), key=lambda x: x[1].get("last_seen", ""), reverse=True)
    pages = max(1, math.ceil(len(items) / USERS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = items[page * USERS_PER_PAGE:(page + 1) * USERS_PER_PAGE]

    text = hdr("👥", "Users", f"{len(items)} total · page {page + 1}/{pages}")
    rows = []
    if not chunk:
        text += "<i>No users yet.</i>"
    for uid_str, info in chunk:
        text += (f"{_user_icon(uid_str, d)} <b>{h(info.get('name') or 'User')}</b>"
                 f"{' @' + h(info['username']) if info.get('username') else ''}\n"
                 f"     <code>{h(uid_str)}</code> · {ago(info.get('last_seen', ''))}\n")
        rows.append([btn(f"{_user_icon(uid_str, d)} {_user_title(info)}"[:60], f"ud_{uid_str}")])
    text += "\n<i>☁️ connected · 👤 not connected · 🚫 banned</i>"
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("‹ Prev", f"ul_{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn("Next ›", f"ul_{page + 1}"))
        rows.append(nav)
    rows.append(back_row("adm_menu", "Admin"))
    await render(update, text, rows)


@admin_only
async def banned_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    d = db()
    banned = sorted(d["banned_users"])
    text = hdr("🚫", "Banned Users", f"{len(banned)} user(s)")
    rows = []
    if not banned:
        text += "<i>Nobody is banned.</i>"
    for buid in banned[:40]:
        info = d["users"].get(str(buid), {})
        text += f"• <b>{h(info.get('name', 'Unknown'))}</b> <code>{buid}</code>\n"
        rows.append([btn(f"✅ Unban · {_user_title(info) if info else buid}"[:60], f"uu_{buid}")])
    rows.append(back_row("adm_menu", "Admin"))
    await render(update, text, rows)


async def _render_user_detail(msg, target: int):
    d = db()
    info = d["users"].get(str(target))
    if not info:
        await edit(msg, "❌ User not found.", [back_row("ul_0", "Users")])
        return
    banned = target in d["banned_users"]
    login = d["cf_logins"].get(str(target))
    live = bool(get_s(target).get("key"))
    uname = f"@{h(info['username'])}" if info.get("username") else "—"
    text = (
        hdr("👤", info.get("name") or "User") +
        f"🆔 <code>{target}</code>\n"
        f"🔗 {uname}  ·  <a href='tg://user?id={target}'>open profile</a>\n"
        f"📅 Joined  {fmt_dt(info.get('first_seen'))}\n"
        f"🕒 Last seen  <b>{ago(info.get('last_seen', ''))}</b>\n"
        f"☁️ Cloudflare  {('<b>connected</b> · login ' + ago(login.get('last_login', ''))) if login else 'never connected'}\n"
        f"🔗 Session  {'🟢 live' if live else '⚪ none'}\n"
        f"🛡 Status  {'🚫 <b>Banned</b>' if banned else '✅ <b>Allowed</b>'}"
    )
    rows = []
    if target != ADMIN_ID:
        rows.append([btn("✅ Unban user", f"uu_{target}") if banned else btn("🚫 Ban user", f"ub_{target}")])
    rows.append(back_row("ul_0", "Users"))
    await edit(msg, text, rows)


@admin_only
async def user_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _render_user_detail(q.message, int(q.data[3:]))


@admin_only
async def user_ban_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    target = int(q.data[3:])
    if q.data.startswith("ub_"):
        ban_user(target)
        await q.answer("🚫 Banned")
    else:
        unban_user(target)
        await q.answer("✅ Unbanned")
    await _render_user_detail(q.message, target)


@admin_only
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not ctx.args[0].isdigit():
        await say(update.message, "⚠️ Usage: <code>/ban USER_ID</code>")
        return
    target = int(ctx.args[0])
    if target == ADMIN_ID:
        await update.message.reply_text("❌ You can't ban yourself.")
        return
    ban_user(target)
    await say(update.message, f"🚫 User <code>{target}</code> banned.")


@admin_only
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not ctx.args[0].isdigit():
        await say(update.message, "⚠️ Usage: <code>/unban USER_ID</code>")
        return
    unban_user(int(ctx.args[0]))
    await say(update.message, f"✅ User <code>{int(ctx.args[0])}</code> unbanned.")


# ── Broadcast (with preview + confirmation) ──
_bg_tasks: set = set()


def _recipients() -> list[int]:
    d = db()
    return [int(u) for u in d["users"] if int(u) != ADMIN_ID and int(u) not in d["banned_users"]]


@admin_only
async def broadcast_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    await render(update, hdr("📢", "Broadcast", f"{len(_recipients())} recipients") +
                 "Send the message to broadcast.\nText, photo, video or document all work.",
                 [[btn("✖ Cancel", "bc_no")]])
    return BROADCAST_MSG


async def broadcast_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    ctx.user_data["bc"] = (update.message.chat_id, update.message.message_id)
    await say(update.message, hdr("👀", "Confirm broadcast") +
              f"Send the message above to <b>{len(_recipients())}</b> users?",
              [[btn("✅ Send now", "bc_go"), btn("✖ Cancel", "bc_no")]])
    return BROADCAST_CONFIRM


async def broadcast_cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await edit(q.message, "✖ Broadcast cancelled.", [back_row("adm_menu", "Admin")])
    return ConversationHandler.END


async def broadcast_cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✖ Broadcast cancelled.")
    return ConversationHandler.END


async def _do_broadcast(bot, status_msg, src_chat, src_msg, targets):
    sent = blocked = failed = 0
    for i, chat_id in enumerate(targets, 1):
        for attempt in range(2):
            try:
                await bot.copy_message(chat_id, src_chat, src_msg)
                sent += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Forbidden:
                blocked += 1
                break
            except TelegramError as e:
                if "deactivated" in str(e).lower() or "not found" in str(e).lower():
                    blocked += 1
                else:
                    failed += 1
                    logger.warning(f"Broadcast to {chat_id}: {e}")
                break
        await asyncio.sleep(0.05)
        if i % 25 == 0:
            await asyncio.sleep(1)
    try:
        await edit(status_msg, hdr("📢", "Broadcast complete") +
                   f"✅ Sent  <b>{sent}</b>\n🚫 Blocked  <b>{blocked}</b>\n❌ Failed  <b>{failed}</b>\n"
                   f"📊 Total  <b>{len(targets)}</b>", [back_row("adm_menu", "Admin")])
    except Exception:
        pass


async def broadcast_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    src = ctx.user_data.get("bc")
    if not src or not is_admin(q.from_user.id):
        return ConversationHandler.END
    targets = _recipients()
    status = await edit(q.message, loading(f"Sending to {len(targets)} users…"))
    task = asyncio.create_task(_do_broadcast(ctx.bot, status, src[0], src[1], targets))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return ConversationHandler.END


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ADMIN-ONLY AUTO-FAILOVER UI (Kiswahili)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _fo_not_configured_msg() -> str:
    return (
        hdr("⚡", "Auto-Failover") +
        "⚠️ <b>Haijasanidiwa bado.</b>\n\nOngeza kwenye <code>.env</code>:\n"
        "<code>CF_API_TOKEN=...</code>\n<code>CF_ZONE_ID=...</code>\n"
        "<code>CONFIG_RECORD_NAME=_failover.domain.com</code>"
    )


def _fo_rows():
    return [
        [btn("🔄 Refresh", "fo_refresh"), btn("⚡ Force Check", "fo_force")],
        [btn("📋 Nodes", "fo_ips"), btn("📄 Domains", "fo_recs")],
        [btn("➕ Node", "fo_addip"), btn("➖ Node", "fo_remip")],
        [btn("🎯 Priority", "fo_prio"), btn("🧹 Safisha", "fo_cleanup")],
        [btn("🌐 + Domain", "fo_addrec"), btn("🗑 − Domain", "fo_remrec")],
        [btn("👑 Admin", "adm_menu"), btn("🏠 Home", "do_start")],
    ]


async def _rec_info(rec):
    try:
        r = await afo_cf_api("GET", "/dns_records", params={"type": "A", "name": rec})
        res = r.get("result", [])
        return rec, (res[0]["content"] if res else "Haijawekwa"), ("☁️ Proxied" if res and res[0].get("proxied") else "🔘 DNS only")
    except Exception:
        return rec, "Error", "⚠️"


async def _build_fo_status_text() -> str:
    ips, recs = await asyncio.to_thread(fo_get_shared_config)
    health = await fo_check_all_nodes_parallel(ips) if ips else {}
    infos = await asyncio.gather(*(_rec_info(r) for r in recs)) if recs else []
    best = next((ip for ip in ips if health.get(ip) == "UP"), "")
    leader = fo_runtime.get("leader_id") or NODE_ID

    lines = [
        hdr("⚡", "Auto-Failover", "Live status").rstrip("\n"),
        "",
        f"🤖 Leader  <code>{h(leader)}</code>",
        f"🔍 Probe  <code>{h(CHECK_METHOD.upper())}:{CHECK_PORT}</code> · kila <b>{CHECK_INTERVAL}s</b>",
        "",
        "🖥️ <b>Nodes</b>  <i>(kwa priority)</i>",
    ]
    if not ips:
        lines.append("  <i>Hakuna node. Bonyeza ➕ Node.</i>")
    for i, ip in enumerate(ips, 1):
        st = "🟢 UP" if health.get(ip) == "UP" else "🔴 DOWN"
        mark = "  🎯" if ip == best else ""
        lines.append(f"  {i}. <code>{h(ip)}</code>  <b>{st}</b>{mark}")
    lines += ["", "🌐 <b>Domains</b>"]
    if not recs:
        lines.append("  <i>Hakuna domain. Bonyeza 🌐 + Domain.</i>")
    for rec, cur, px in infos:
        lines.append(f"  • <code>{h(rec)}</code>\n     ➡️ <code>{h(cur)}</code>  {px}")
    return "\n".join(lines)


@admin_only
async def fo_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not FO_ENABLED:
        await render(update, _fo_not_configured_msg(), [back_row("adm_menu", "Admin")])
        return ConversationHandler.END
    m = await render(update, loading("Inakagua nodes na DNS…"))
    try:
        await edit(m, await _build_fo_status_text(), _fo_rows())
    except Exception as e:
        await edit(m, err_text(e, "Hitilafu"), _fo_rows())
    return ConversationHandler.END


@admin_only
async def fo_listips(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not FO_ENABLED:
        await render(update, _fo_not_configured_msg())
        return
    m = await render(update, loading("Inakagua nodes…"))
    try:
        ips, _ = await asyncio.to_thread(fo_get_shared_config)
        if not ips:
            text = hdr("📋", "Nodes") + "<i>Hakuna IP iliyoongezwa bado.</i>"
        else:
            hm = await fo_check_all_nodes_parallel(ips)
            text = hdr("📋", "Nodes", "kwa mpangilio wa priority") + "\n".join(
                f"{i}. <code>{h(ip)}</code>  <b>{'🟢 ONLINE' if hm.get(ip) == 'UP' else '🔴 OFFLINE'}</b>"
                for i, ip in enumerate(ips, 1))
        await edit(m, text, _fo_rows())
    except Exception as e:
        await edit(m, err_text(e), _fo_rows())


@admin_only
async def fo_listrecords(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not FO_ENABLED:
        await render(update, _fo_not_configured_msg())
        return
    m = await render(update, loading("Inavuta domains…"))
    try:
        _, recs = await asyncio.to_thread(fo_get_shared_config)
        if not recs:
            text = hdr("📄", "Domains") + "<i>Hakuna domain iliyoongezwa bado.</i>"
        else:
            infos = await asyncio.gather(*(_rec_info(r) for r in recs))
            text = hdr("📄", "Domains Zinazolindwa") + "\n".join(
                f"{i}. <code>{h(r)}</code>\n     ➡️ <code>{h(c)}</code>  {px}" for i, (r, c, px) in enumerate(infos, 1))
        await edit(m, text, _fo_rows())
    except Exception as e:
        await edit(m, err_text(e), _fo_rows())


@admin_only
async def fo_force_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not FO_ENABLED:
        await edit(q.message, _fo_not_configured_msg())
        return
    m = await edit(q.message, loading("Ukaguzi wa failover unaendelea…"))
    try:
        res = await fo_run_one_cycle(notify=False)
        if res.get("skipped"):
            await edit(m, f"ℹ️ {h(res['reason'])}", _fo_rows())
        elif res.get("all_down"):
            await edit(m, hdr("🚨", "Nodes ZOTE OFFLINE") + "DNS haijabadilishwa.", _fo_rows())
        else:
            changed = res.get("changed", [])
            extra = (f"\n🌐 Zilizohamishwa: <code>{h(', '.join(changed))}</code>" if changed
                     else "\n✅ Domains zote tayari ziko kwenye IP sahihi.")
            await edit(m, hdr("⚡", "Ukaguzi Umekamilika", NODE_ID) +
                       f"🎯 Best IP  <code>{h(res['best_ip'])}</code>{extra}", _fo_rows())
    except Exception as e:
        await edit(m, err_text(e), _fo_rows())


@admin_only
async def fo_cleanup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not FO_ENABLED:
        await render(update, _fo_not_configured_msg())
        return
    m = await render(update, loading("Inasafisha marudio…"))
    try:
        _, recs = await asyncio.to_thread(fo_get_shared_config)
        removed = 0
        targets = [("A", r) for r in recs] + [("TXT", CONFIG_RECORD_NAME)]
        for rtype, name in targets:
            r = await afo_cf_api("GET", "/dns_records", params={"type": rtype, "name": name})
            for extra in r.get("result", [])[1:]:
                try:
                    await afo_cf_api("DELETE", f"/dns_records/{extra['id']}")
                    removed += 1
                except Exception:
                    pass
        await edit(m, hdr("🧹", "Usafishaji Umekamilika") + f"Rekodi za marudio zilizofutwa: <b>{removed}</b>", _fo_rows())
    except Exception as e:
        await edit(m, err_text(e), _fo_rows())


async def _do_fo_addip(msg, raw_text: str):
    parts = raw_text.strip().split()
    new_ip = parts[0] if parts else ""
    pos = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    if not is_valid_ip(new_ip):
        await say(msg, f"❌ <b>IP si sahihi:</b> <code>{h(new_ip)}</code>\nMfano: <code>1.2.3.4</code>")
        return False
    ips, recs = await asyncio.to_thread(fo_get_shared_config)
    if new_ip in ips:
        await say(msg, f"ℹ️ <code>{h(new_ip)}</code> tayari ipo kwenye orodha.", _fo_rows())
        return True
    if pos is not None and 1 <= pos <= len(ips) + 1:
        ips.insert(pos - 1, new_ip)
    else:
        ips.append(new_ip)
    await asyncio.to_thread(fo_save_shared_config, ips, recs)
    await say(msg, hdr("✅", "Node imeongezwa") + f"<code>{h(new_ip)}</code>\n📋 Priority: <code>{h(','.join(ips))}</code>", _fo_rows())
    return True


async def _do_fo_remip(msg, target_ip: str):
    target_ip = target_ip.strip()
    ips, recs = await asyncio.to_thread(fo_get_shared_config)
    if target_ip not in ips:
        await say(msg, f"❌ <code>{h(target_ip)}</code> haipo kwenye orodha.", _fo_rows())
        return
    ips = [ip for ip in ips if ip != target_ip]
    await asyncio.to_thread(fo_save_shared_config, ips, recs)
    await say(msg, hdr("🗑", "Node imeondolewa") + f"<code>{h(target_ip)}</code>\n📋 Zilizobaki: <code>{h(','.join(ips) or 'Tupu')}</code>", _fo_rows())


async def _do_fo_setprio(msg, raw_list: str):
    clean = []
    for ip in [x.strip() for x in raw_list.replace(" ", "").split(",") if x.strip()]:
        if not is_valid_ip(ip):
            await say(msg, f"❌ <code>{h(ip)}</code> si sahihi. Orodha haijabadilishwa.", _fo_rows())
            return
        if ip not in clean:
            clean.append(ip)
    if not clean:
        await say(msg, "❌ Orodha ya IP ni tupu.", _fo_rows())
        return
    _, recs = await asyncio.to_thread(fo_get_shared_config)
    await asyncio.to_thread(fo_save_shared_config, clean, recs)
    await say(msg, hdr("🎯", "Priority mpya imehifadhiwa") + f"<code>{h(','.join(clean))}</code>", _fo_rows())


async def _do_fo_addrec(msg, new_rec: str):
    new_rec = new_rec.strip().lower().rstrip(".")
    if not is_valid_domain(new_rec):
        await say(msg, f"❌ <b>Domain si sahihi:</b> <code>{h(new_rec)}</code>", _fo_rows())
        return
    ips, recs = await asyncio.to_thread(fo_get_shared_config)
    if new_rec in recs:
        await say(msg, f"ℹ️ <code>{h(new_rec)}</code> tayari ipo.", _fo_rows())
        return
    recs.append(new_rec)
    await asyncio.to_thread(fo_save_shared_config, ips, recs)
    await say(msg, hdr("✅", "Domain imeongezwa") + f"<code>{h(new_rec)}</code>\n📄 Domains: <code>{h(','.join(recs))}</code>", _fo_rows())


async def _do_fo_remrec(msg, target_rec: str):
    target_rec = target_rec.strip().lower().rstrip(".")
    ips, recs = await asyncio.to_thread(fo_get_shared_config)
    if target_rec not in recs:
        await say(msg, f"❌ <code>{h(target_rec)}</code> haipo kwenye orodha.", _fo_rows())
        return
    recs = [r for r in recs if r != target_rec]
    await asyncio.to_thread(fo_save_shared_config, ips, recs)
    await say(msg, hdr("🗑", "Domain imeondolewa") + f"<code>{h(target_rec)}</code>\n📄 Zilizobaki: <code>{h(','.join(recs) or 'Tupu')}</code>", _fo_rows())


FO_ACTIONS = {
    "/addip": "fo_addip", "/removeip": "fo_remip", "/setpriority": "fo_prio",
    "/addrecord": "fo_addrec", "/removerecord": "fo_remrec",
}
FO_PROMPTS = {
    "fo_addip": (FO_ADD_IP, "➕ Tuma IP ya node:\n<code>1.2.3.4</code> au <code>1.2.3.4 1</code> (nafasi ya 1)"),
    "fo_remip": (FO_REM_IP, "➖ Tuma IP ya node ya kuondoa:"),
    "fo_prio": (FO_SET_PRIO, "🎯 Tuma orodha mpya ya IP kwa mpangilio:\n<code>3.3.3.3,2.2.2.2,1.1.1.1</code>"),
    "fo_addrec": (FO_ADD_REC, "🌐 Tuma domain ya kuongeza:\n<code>cdn1.domain.com</code>"),
    "fo_remrec": (FO_REM_REC, "🗑 Tuma domain ya kuondoa:"),
}


@admin_only
async def fo_prompt_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not FO_ENABLED:
        await say(msg_of(update), _fo_not_configured_msg())
        return ConversationHandler.END

    if update.message and ctx.args:
        cmd = update.me