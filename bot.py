import os
import re
import hmac
import hashlib
import time
import uuid
import json
import logging
import asyncio
import html as htmllib
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode, quote, urlparse

import aiohttp
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
    MenuButtonCommands,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
E = os.environ.get
D = lambda x: Decimal(str(x))
q2 = lambda x: D(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
esc = lambda t: htmllib.escape(str(t))

# ================= CONFIG (All from Render / Hosting Environment Variables) =================
BOT_TOKEN = (E("BOT_TOKEN") or "").strip()
DATABASE_URL = (E("DATABASE_URL") or "").strip().strip("\"'").split("?")[0]
# Alternative separate DB variables (safe when password has special characters)
DB_HOST = (E("DB_HOST") or "").strip()
DB_PORT = int(E("DB_PORT", "5432"))
DB_USER = (E("DB_USER") or "").strip()
DB_PASSWORD = E("DB_PASSWORD") or ""
DB_NAME = (E("DB_NAME") or "postgres").strip()

API_BASE = E("SHOP_API_BASE", "https://safwantigershopbot-production.up.railway.app/api").rstrip("/")
API_KEY = (E("SHOP_KEY") or "").strip()
SHOP_NAME = (E("SHOP_NAME") or "Premium Digital Shop").strip()

ADMINS = [int(x) for x in re.split(r"[,\s]+", E("ADMIN_IDS", "")) if x.strip().isdigit()]
ADMIN_USERNAME = (E("ADMIN_USERNAME") or "").strip().lstrip("@")
WHATSAPP_LINK = (E("WHATSAPP_LINK") or "").strip()  # e.g. https://wa.me/8801XXXXXXXXX
CONTACT_NUMBER = (E("CONTACT_NUMBER") or E("PHONE_NUMBER") or "").strip()  # e.g. +8801610359086

PRICING_ENV = E("PRICING", "a2.5")
BINANCE_KEY = (E("BINANCE_API_KEY") or "").strip()
BINANCE_SECRET = (E("BINANCE_API_SECRET") or "").strip()
BINANCE_PAY_ID = (E("BINANCE_PAY_ID") or "").strip()

BYBIT_KEY = (E("BYBIT_API_KEY") or "").strip()
BYBIT_SECRET = (E("BYBIT_API_SECRET") or "").strip()
BYBIT_UID = (E("BYBIT_UID") or "").strip()

BKASH_NUMBER = (E("BKASH_NUMBER") or "").strip()
NAGAD_NUMBER = (E("NAGAD_NUMBER") or "").strip()
BDT_RATE = D(E("BDT_RATE", "125"))  # 1 USDT = how many BDT
MIN_DEPOSIT = D(E("MIN_DEPOSIT", "1"))

# Optional JSON map of category_id or keyword -> Telegram custom_emoji_id (for Premium bot custom icons)
CUSTOM_EMOJI_JSON = (E("CUSTOM_EMOJI_MAP") or "").strip()
try:
    CUSTOM_EMOJI_MAP = json.loads(CUSTOM_EMOJI_JSON) if CUSTOM_EMOJI_JSON else {}
except Exception:
    CUSTOM_EMOJI_MAP = {}

bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
adm = Router()
adm.message.filter(F.from_user.id.in_(ADMINS))
adm.callback_query.filter(F.from_user.id.in_(ADMINS))
pool: asyncpg.Pool = None


# ================= FSM STATES =================
class Dep(StatesGroup):
    txid = State()


class Qty(StatesGroup):
    n = State()


class Bk(StatesGroup):
    info = State()


class SearchSt(StatesGroup):
    q = State()


class Adm(StatesGroup):
    price = State()
    rule = State()
    note = State()
    contact = State()


# ================= BUTTON BUILDER WITH TELEGRAM API 9.4+ STYLE & ICON SUPPORT =================
def Btn(text: str, style: str = None, icon_custom_emoji_id: str = None, **kwargs) -> InlineKeyboardButton:
    """
    Creates an InlineKeyboardButton supporting Telegram Bot API 9.4+ `style`
    ('success' = Green, 'primary' = Blue, 'danger' = Red) and `icon_custom_emoji_id`.
    Works across both aiogram 3.13.x (via extra fields) and aiogram 3.24+.
    """
    payload = {"text": text, **kwargs}
    if style in ("success", "primary", "danger"):
        payload["style"] = style
    if icon_custom_emoji_id:
        payload["icon_custom_emoji_id"] = str(icon_custom_emoji_id)
    try:
        return InlineKeyboardButton(**payload)
    except TypeError:
        b = InlineKeyboardButton(text=text, **kwargs)
        if style in ("success", "primary", "danger"):
            object.__setattr__(b, "style", style)
        if icon_custom_emoji_id:
            object.__setattr__(b, "icon_custom_emoji_id", str(icon_custom_emoji_id))
        return b


def KBtn(text: str, style: str = None, icon_custom_emoji_id: str = None) -> KeyboardButton:
    payload = {"text": text}
    if style in ("success", "primary", "danger"):
        payload["style"] = style
    if icon_custom_emoji_id:
        payload["icon_custom_emoji_id"] = str(icon_custom_emoji_id)
    try:
        return KeyboardButton(**payload)
    except TypeError:
        b = KeyboardButton(text=text)
        return b


def strip_button_extras(markup, strip_style=False, strip_emoji=True):
    """Fallback helper if Telegram rejects custom emoji IDs (e.g. non-Premium bot owner)."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    new_rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            d = b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else dict(b)
            if strip_emoji:
                d.pop("icon_custom_emoji_id", None)
            if strip_style:
                d.pop("style", None)
            new_row.append(InlineKeyboardButton(**d))
        new_rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=new_rows)


async def safe_edit(c_or_msg, text: str, reply_markup=None, parse_mode="HTML", **kwargs):
    """Edits a message safely with automatic fallback if custom emoji or message-not-modified occurs."""
    target = c_or_msg.message if isinstance(c_or_msg, CallbackQuery) else c_or_msg
    try:
        return await target.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    except Exception as e:
        err = str(e)
        if "message is not modified" in err.lower():
            if isinstance(c_or_msg, CallbackQuery):
                try:
                    await c_or_msg.answer("🔄 Refreshed!")
                except Exception:
                    pass
            return
        if "CUSTOM_EMOJI" in err.upper() or "PREMIUM" in err.upper():
            clean_kb = strip_button_extras(reply_markup, strip_style=False, strip_emoji=True)
            try:
                return await target.edit_text(text, reply_markup=clean_kb, parse_mode=parse_mode, **kwargs)
            except Exception:
                pass
        if "STYLE" in err.upper() or "BUTTON" in err.upper():
            clean_kb = strip_button_extras(reply_markup, strip_style=True, strip_emoji=True)
            try:
                return await target.edit_text(text, reply_markup=clean_kb, parse_mode=parse_mode, **kwargs)
            except Exception:
                pass
        try:
            return await target.answer(text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
        except Exception:
            clean_kb = strip_button_extras(reply_markup, strip_style=True, strip_emoji=True)
            return await target.answer(text, reply_markup=clean_kb, parse_mode=parse_mode, **kwargs)


async def safe_answer(m: Message, text: str, reply_markup=None, parse_mode="HTML", **kwargs):
    try:
        return await m.answer(text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    except Exception as e:
        err = str(e)
        if "CUSTOM_EMOJI" in err.upper() or "PREMIUM" in err.upper():
            clean_kb = strip_button_extras(reply_markup, strip_style=False, strip_emoji=True)
            return await m.answer(text, reply_markup=clean_kb, parse_mode=parse_mode, **kwargs)
        clean_kb = strip_button_extras(reply_markup, strip_style=True, strip_emoji=True)
        return await m.answer(text, reply_markup=clean_kb, parse_mode=parse_mode, **kwargs)


# ================= ORGANIZED CATEGORY FOLDERS & BRAND ICONS =================
# Ordered to match the 3-column grid layout from Screenshot 1
CATEGORIES = [
    ("chatgpt", "ChatGPT", "🤖", ["chatgpt", "openai", "gpt-4", "gpt4", "gpt plus", "gpt pro", "gpt team", "o1", "o3"]),
    ("gemini", "Gemini", "✨", ["gemini", "google ai", "google one", "notebooklm", "google pro"]),
    ("grok", "Grok", "✖️", ["grok", "xai", "supergrok"]),
    ("codex", "Codex API", "🟢", ["codex"]),
    ("claude", "Claude", "✴️", ["claude", "anthropic"]),
    ("kimi", "Kimi", "🖤", ["kimi", "moonshot"]),
    ("video_ai", "Video AI", "▶️", ["heygen", "runway", "sora", "kling", "pika", "luma", "synthesia", "invideo", "veo", "hailuo", "video ai", "fliki"]),
    ("elevenlabs", "ElevenLabs", "⏸", ["elevenlabs", "eleven labs", "murf", "voice ai", "speechify"]),
    ("ai_tools", "AI Tools", "🧠", ["replit", "perplexity", "midjourney", "lovable", "bolt", "v0", "gamma", "quillbot", "grammarly", "blackbox", "poe", "copilot", "notion", "jasper", "writesonic", "turnitin", "suno", "udio"]),
    ("qwen", "Qwen API", "🟣", ["qwen", "tongyi"]),
    ("deepseek", "DeepSeek", "🐳", ["deepseek"]),
    ("cursor", "Cursor API", "⬛", ["cursor", "windsurf", "codeium", "tabnine"]),
    ("adobe", "Adobe", "🅰️", ["adobe", "photoshop", "premiere", "illustrator", "lightroom", "acrobat", "creative cloud", "firefly"]),
    ("autodesk", "Autodesk", "📐", ["autodesk", "autocad", "maya", "revit", "3ds max", "fusion 360"]),
    ("design", "Design", "✂️", ["capcut", "canva", "figma", "envato", "freepik", "picsart", "filmora", "davinci", "shutterstock", "placeit", "vecteezy"]),
    ("office", "Office", "🪟", ["office", "microsoft", "windows", "zoom", "workspace", "icloud", "dropbox", "onedrive", "m365"]),
    ("education", "Education", "🦉", ["duolingo", "coursera", "udemy", "skillshare", "chegg", "scribd", "blinkist", "leetcode", "datacamp", "brilliant"]),
    ("streaming", "Streaming", "🎬", ["netflix", "youtube", "prime video", "amazon prime", "disney", "hbo", "max", "hulu", "crunchyroll", "iptv", "hoichoi", "chorki", "sonyliv", "zee5", "apple tv", "peacock", "paramount"]),
    ("music", "Music", "🎵", ["spotify", "apple music", "tidal", "deezer", "soundcloud", "yt music", "amazon music"]),
    ("vpn", "VPN", "🛡", ["vpn", "nord", "expressvpn", "surfshark", "cyberghost", "proton", "ipvanish", "pia", "windscribe", "hma", "mullvad", "vypr", "adguard vpn"]),
    ("security", "Security", "🔒", ["kaspersky", "bitdefender", "norton", "mcafee", "avast", "malwarebytes", "1password", "lastpass", "dashlane", "antivirus"]),
    ("games", "Games", "🎮", ["xbox", "playstation", "psn", "steam", "valorant", "pubg", "free fire", "genshin", "roblox", "minecraft", "ea play", "ubisoft", "game pass", "nintendo", "riot"]),
    ("social", "Social", "💼", ["linkedin", "telegram", "twitter", "x premium", "tinder", "bumble", "snapchat", "facebook", "instagram", "tiktok", "meta"]),
    ("discord", "Discord", "👾", ["discord", "nitro", "server boost"]),
    ("other", "Others", "📦", []),
]

CAT_MAP = {cid: (label, icon, kws) for cid, label, icon, kws in CATEGORIES}

# Product-level specific icon overrides so individual products inside folders show exact brand icons
PRODUCT_BRAND_ICONS = [
    ("chatgpt", "🤖"),
    ("openai", "🤖"),
    ("codex", "🟢"),
    ("gemini", "✨"),
    ("google one", "1️⃣"),
    ("youtube", "▶️"),
    ("grok", "✖️"),
    ("claude", "✴️"),
    ("kimi", "🖤"),
    ("capcut", "✂️"),
    ("canva", "🎨"),
    ("figma", "🎨"),
    ("replit", "⚡"),
    ("zoom", "📹"),
    ("heygen", "💎"),
    ("elevenlabs", "⏸"),
    ("deepseek", "🐳"),
    ("cursor", "⬛"),
    ("qwen", "🟣"),
    ("adobe", "🅰️"),
    ("autodesk", "📐"),
    ("duolingo", "🦉"),
    ("netflix", "🎬"),
    ("prime", "🎬"),
    ("disney", "🎬"),
    ("crunchyroll", "🎬"),
    ("spotify", "🎵"),
    ("apple music", "🎵"),
    ("vpn", "🛡"),
    ("nord", "🛡"),
    ("expressvpn", "🛡"),
    ("surfshark", "🛡"),
    ("discord", "👾"),
    ("nitro", "👾"),
    ("linkedin", "💼"),
    ("telegram", "✈️"),
    ("office", "🪟"),
    ("microsoft", "🪟"),
    ("windows", "🪟"),
    ("perplexity", "🧭"),
    ("midjourney", "⛵"),
    ("grammarly", "🟢"),
    ("quillbot", "🪶"),
]


# ================= RUNTIME STATE & PRICING =================
RULE_RE = re.compile(r"^\s*([apm])\s*(\d+(?:\.\d+)?)\s*$", re.I)


def parse_rule(s):
    m = RULE_RE.match(s or "")
    return (m.group(1).lower(), D(m.group(2))) if m else None


def apply_rule(base, rule):
    k, v = rule
    r = base + v if k == "a" else base * (1 + v / 100) if k == "p" else base * v
    return max(q2(r), Decimal("0.01"))


def rule_text(rule):
    k, v = rule
    return {
        "a": f"a{v} → Base Price + {v} USDT",
        "p": f"p{v} → Base Price + {v}%",
        "m": f"m{v} → Base Price × {v}",
    }[k]


ENV_RULE = parse_rule(PRICING_ENV) or ("a", D("2.5"))
ST = {
    "rule": ENV_RULE,
    "contact_number": CONTACT_NUMBER,
    "whatsapp_link": WHATSAPP_LINK,
    "show_empty_folders": False,
    "pin_all_favs_in_folders": True,
}
OVR = {}       # product_id(str) -> manual price (Decimal)
NOTES = {}     # product_id(str) -> custom admin note (str)
FAVS = []      # Ordered list of up to 3 favourite product_ids (list[str])
CAT_OVR = {}   # product_id(str) -> manual category_id (str)


def detect_category(p: dict) -> str:
    pid = str(p["id"])
    if pid in CAT_OVR and CAT_OVR[pid] in CAT_MAP:
        return CAT_OVR[pid]
    text = f"{p.get('name', '')} {p.get('raw_cat', '')}".lower()
    for cid, label, icon, kws in CATEGORIES:
        if cid == "other":
            continue
        for kw in kws:
            if kw in text:
                return cid
    return "other"


def detect_product_icon(p: dict) -> tuple[str, str | None]:
    """Returns (unicode_icon, custom_emoji_id)."""
    name_low = p.get("name", "").lower()
    ce_id = p.get("ce_id")
    for kw, em in PRODUCT_BRAND_ICONS:
        if kw in name_low:
            if not ce_id and kw in CUSTOM_EMOJI_MAP:
                ce_id = str(CUSTOM_EMOJI_MAP[kw])
            return em, ce_id
    cid = detect_category(p)
    _, cat_icon, _ = CAT_MAP.get(cid, ("Others", "📦", []))
    if not ce_id and cid in CUSTOM_EMOJI_MAP:
        ce_id = str(CUSTOM_EMOJI_MAP[cid])
    return cat_icon, ce_id


# ================= CONTACT & SUPPORT HELPERS =================
def get_whatsapp_url(ctx=""):
    w = (ST.get("whatsapp_link") or WHATSAPP_LINK or "").strip()
    num = (ST.get("contact_number") or CONTACT_NUMBER or "").strip()
    if not w and num:
        digits = re.sub(r"\D+", "", num)
        if digits.startswith("01") and len(digits) == 11:
            digits = "88" + digits
        if digits:
            w = f"https://wa.me/{digits}"
    if w and ctx and ("wa.me" in w or "whatsapp.com" in w):
        w += ("&" if "?" in w else "?") + "text=" + quote(ctx)
    return w


def contact_btns(ctx=""):
    """Returns Telegram Admin, WhatsApp, and Contact Phone Number buttons."""
    row = []
    if ADMIN_USERNAME:
        u = f"https://t.me/{ADMIN_USERNAME}" + (f"?text={quote(ctx)}" if ctx else "")
        row.append(Btn(text="📩 Telegram Admin", url=u, style="primary"))
    w_url = get_whatsapp_url(ctx)
    if w_url:
        row.append(Btn(text="💬 WhatsApp", url=w_url, style="success"))
    num = (ST.get("contact_number") or CONTACT_NUMBER or "").strip()
    if num:
        row.append(Btn(text=f"📞 {num}", callback_data="contact:phone", style="primary"))
    return row


def add_contact(b: InlineKeyboardBuilder, ctx=""):
    r = contact_btns(ctx)
    if r:
        # Put Telegram + WhatsApp on 1st row, Phone number on 2nd row if 3 buttons
        if len(r) <= 2:
            b.row(*r)
        else:
            b.row(r[0], r[1])
            b.row(r[2])


def note_for(p):
    """Priority: 1) Admin Custom Note 2) Upstream Warranty + Description 3) None"""
    n = NOTES.get(str(p["id"]))
    if n:
        return n, "admin"
    parts = []
    w, d = clean(p.get("warranty"), 350), clean(p.get("desc"), 600)
    if w:
        parts.append("🛡 Warranty: " + w)
    if d:
        parts.append(d)
    return ("\n\n".join(parts), "safwan") if parts else ("", "none")


def sell_price(p):
    o = OVR.get(str(p["id"]))
    return o if o is not None else apply_rule(p["price"], ST["rule"])


def format_product_btn_text(p: dict, max_name_len: int = 34) -> str:
    """Formats button label matching Screenshot 3:
    `<Icon> <Product Name> - <Price>USDT (Stock: <N>)`
    """
    icon, _ = detect_product_icon(p)
    name = p["name"]
    if len(name) > max_name_len:
        name = name[: max_name_len - 1].rstrip() + "…"
    price_str = f"{sell_price(p):f}".rstrip("0").rstrip(".") if "." in f"{sell_price(p):f}" else f"{sell_price(p)}"
    stk_str = "∞" if p["stock"] is None else (str(p["stock"]) if p["ok"] else "0")
    return f"{icon} {name} - {price_str}USDT (Stock: {stk_str})"


def make_product_btn(p: dict, is_fav: bool = False, origin: str = "all:0") -> InlineKeyboardButton:
    """
    Creates the styled InlineKeyboardButton for a product:
    - Out of stock (`not p['ok']` or `stock == 0`) -> RED (`style='danger'`)
    - Favourite (`is_fav=True`) -> GREEN (`style='success'`)
    - Normal in-stock -> BLUE (`style='primary'`)
    """
    _, ce_id = detect_product_icon(p)
    if not p["ok"] or (p["stock"] is not None and p["stock"] <= 0):
        style = "danger"
    elif is_fav:
        style = "success"
    else:
        style = "primary"
    label = format_product_btn_text(p)
    return Btn(
        text=label,
        callback_data=f"p:{p['id']}:{origin}",
        style=style,
        icon_custom_emoji_id=ce_id,
    )


# ================= DATABASE =================
async def init_db():
    global pool, FAVS
    opts = dict(ssl="require", statement_cache_size=0, min_size=1, max_size=5)
    if DB_HOST:
        kw = dict(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, database=DB_NAME)
        logging.info("DB connect: user=%s host=%s port=%s", kw["user"], kw["host"], kw["port"])
        pool = await asyncpg.create_pool(**kw, **opts)
    else:
        logging.info("DB connect via DATABASE_URL: host=%s", urlparse(DATABASE_URL).hostname)
        pool = await asyncpg.create_pool(DATABASE_URL, **opts)
    await pool.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id BIGINT PRIMARY KEY,
        balance NUMERIC(18,2) NOT NULL DEFAULT 0,
        username TEXT,
        name TEXT,
        created TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS deposits(
        id SERIAL PRIMARY KEY,
        method TEXT,
        txid TEXT,
        user_id BIGINT,
        amount NUMERIC(18,2),
        created TIMESTAMPTZ DEFAULT now(),
        UNIQUE(method, txid)
    );
    CREATE TABLE IF NOT EXISTS orders(
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        pid TEXT,
        product TEXT,
        qty INT,
        cost NUMERIC(18,2),
        base_cost NUMERIC(18,2),
        request_id TEXT,
        status TEXT,
        delivery TEXT,
        created TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS overrides(pid TEXT PRIMARY KEY, price NUMERIC(18,2));
    CREATE TABLE IF NOT EXISTS notes(pid TEXT PRIMARY KEY, note TEXT);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS bkash(
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        trx TEXT UNIQUE,
        amount_bdt NUMERIC(18,2),
        status TEXT DEFAULT 'pending',
        created TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS favorites(
        pid TEXT PRIMARY KEY,
        rank INT NOT NULL DEFAULT 1,
        created TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS product_categories(
        pid TEXT PRIMARY KEY,
        cat_id TEXT NOT NULL
    );
    """)
    for t in ("users", "deposits", "orders", "overrides", "settings", "bkash", "notes", "favorites", "product_categories"):
        try:
            await pool.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        except Exception as e:
            logging.warning("RLS %s: %s", t, e)

    OVR.clear()
    for r in await pool.fetch("SELECT pid, price FROM overrides"):
        OVR[str(r["pid"])] = r["price"]

    NOTES.clear()
    for r in await pool.fetch("SELECT pid, note FROM notes"):
        NOTES[str(r["pid"])] = r["note"]

    FAVS.clear()
    for r in await pool.fetch("SELECT pid FROM favorites ORDER BY rank ASC, created ASC LIMIT 3"):
        FAVS.append(str(r["pid"]))

    CAT_OVR.clear()
    for r in await pool.fetch("SELECT pid, cat_id FROM product_categories"):
        CAT_OVR[str(r["pid"])] = str(r["cat_id"])

    rows = await pool.fetch("SELECT key, value FROM settings")
    s_map = {r["key"]: r["value"] for r in rows}
    if s_map.get("pricing") and parse_rule(s_map["pricing"]):
        ST["rule"] = parse_rule(s_map["pricing"])
    if "contact_number" in s_map:
        ST["contact_number"] = s_map["contact_number"]
    if "whatsapp_link" in s_map:
        ST["whatsapp_link"] = s_map["whatsapp_link"]
    if "show_empty_folders" in s_map:
        ST["show_empty_folders"] = s_map["show_empty_folders"] == "1"
    if "pin_all_favs_in_folders" in s_map:
        ST["pin_all_favs_in_folders"] = s_map["pin_all_favs_in_folders"] == "1"


async def save_favs():
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("DELETE FROM favorites")
            for idx, pid in enumerate(FAVS[:3], start=1):
                await c.execute("INSERT INTO favorites(pid, rank) VALUES($1, $2)", str(pid), idx)


async def ensure_user(u):
    await pool.execute(
        "INSERT INTO users(id,username,name) VALUES($1,$2,$3) ON CONFLICT(id) DO UPDATE SET username=$2, name=$3",
        u.id, u.username, u.full_name,
    )


async def balance(uid):
    await pool.execute("INSERT INTO users(id) VALUES($1) ON CONFLICT DO NOTHING", uid)
    return await pool.fetchval("SELECT balance FROM users WHERE id=$1", uid)


async def deduct(uid, amt):
    return await pool.fetchval(
        "UPDATE users SET balance=balance-$2 WHERE id=$1 AND balance>=$2 RETURNING balance", uid, amt
    )


async def credit(uid, amt):
    await balance(uid)
    return await pool.fetchval("UPDATE users SET balance=balance+$2 WHERE id=$1 RETURNING balance", uid, amt)


async def credit_deposit(method, txid, uid, amt):
    """Prevents double-spending the same transaction ID."""
    await balance(uid)
    async with pool.acquire() as c:
        async with c.transaction():
            ins = await c.fetchval(
                "INSERT INTO deposits(method,txid,user_id,amount) VALUES($1,$2,$3,$4) "
                "ON CONFLICT DO NOTHING RETURNING id",
                method, txid, uid, amt,
            )
            if not ins:
                return False
            await c.execute("UPDATE users SET balance=balance+$2 WHERE id=$1", uid, amt)
            return True


async def notify(text, **kw):
    for a in ADMINS:
        try:
            await bot.send_message(a, text, **kw)
        except Exception as e:
            logging.warning("notify %s failed: %s", a, e)


# ================= SHOP API =================
def H():
    return {"Authorization": f"Bearer {API_KEY}"}


_cache = {"t": 0, "d": []}
TAG = re.compile(r"<[^>]+>")
CE = re.compile(r"\{\{ce:(\d+)\|(.*?)\}\}")


def extract_ce_and_clean(t, n=500) -> tuple[str, str | None]:
    if not t:
        return "", None
    s = str(t)
    m = CE.search(s)
    ce_id = m.group(1) if m else None
    s = CE.sub(r"\2", s)
    s = TAG.sub("", s)
    s = htmllib.unescape(s).replace("*", "").strip()
    return (s[:n] + ("…" if len(s) > n else "")), ce_id


def clean(t, n=500):
    txt, _ = extract_ce_and_clean(t, n)
    return txt


async def get_products(force=False):
    if not force and time.time() - _cache["t"] < 15 and _cache["d"]:
        return _cache["d"]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{API_BASE}/products", headers=H(), timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = await r.json()
    except Exception as e:
        logging.warning("products fetch failed: %s", e)
        return _cache["d"]
    if isinstance(data, dict):
        data = data.get("products") or data.get("data") or data.get("items") or []
    out = []
    for p in data:
        pid, price = p.get("id", p.get("product_id")), p.get("price")
        if pid is None or price is None:
            continue
        raw_name = p.get("name") or f"Product {pid}"
        clean_name, ce_id = extract_ce_and_clean(raw_name, 120)
        if not ce_id:
            ce_id = p.get("icon_custom_emoji_id") or p.get("custom_emoji_id")
        unlimited = bool(p.get("unlimited_stock"))
        stock = None if unlimited else int(p.get("stock") or 0)
        ok = bool(p.get("available", True)) and (unlimited or (stock is not None and stock > 0))
        out.append({
            "id": str(pid),
            "raw_id": pid,
            "name": clean_name or f"Product {pid}",
            "price": D(price),
            "stock": stock,
            "ok": ok,
            "warranty": p.get("warranty"),
            "desc": p.get("description"),
            "raw_cat": p.get("category") or p.get("group") or "",
            "ce_id": str(ce_id) if ce_id else None,
        })
    out.sort(key=lambda x: (not x["ok"], x["name"].lower()))
    _cache.update(t=time.time(), d=out)
    return out


async def find(pid, force=False):
    for p in await get_products(force):
        if str(p["id"]) == str(pid):
            return p
    return None


async def api_order(pid, qty, rid):
    # Preserve int product_id if numeric
    send_pid = int(pid) if str(pid).isdigit() else pid
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{API_BASE}/order",
            headers=H(),
            json={"product_id": send_pid, "quantity": qty, "request_id": rid},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as r:
            try:
                data = await r.json()
            except Exception:
                data = {"error": await r.text()}
            return r.status, data


# ================= AUTO VERIFY (BINANCE & BYBIT) =================
async def verify_binance(txid: str):
    """
    Verifies a USDT deposit via Binance API:
    1) Checks Binance Pay Trade History (`GET /sapi/v1/pay/transactions`) for internal Pay transfers
       matching `orderId`, `transactionId`, or `prepayId` (using official `startTime`/`endTime` params).
    2) Checks Binance On-Chain Capital Deposit History (`GET /sapi/v1/capital/deposit/hisrec`)
       in case the customer sent an on-chain deposit and provided the `txId`.
    """
    if not BINANCE_KEY or not BINANCE_SECRET:
        return None
    txid_clean = txid.strip()
    now = int(time.time() * 1000)
    # 1. Check Binance Pay History (last 7 days)
    params = {
        "startTime": now - 7 * 86400000,
        "endTime": now,
        "limit": 100,
        "timestamp": now,
        "recvWindow": 20000,
    }
    qs = urlencode(params)
    sig = hmac.new(BINANCE_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(
                f"https://api.binance.com/sapi/v1/pay/transactions?{qs}&signature={sig}",
                headers={"X-MBX-APIKEY": BINANCE_KEY},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json()
            logging.info("binance pay resp: %s", str(data)[:300])
            for t in data.get("data") or []:
                ids = {
                    str(t.get("orderId", "")).strip(),
                    str(t.get("transactionId", "")).strip(),
                    str(t.get("prepayId", "")).strip(),
                }
                amt = D(t.get("amount", 0))
                # Incoming payments have positive amount; also verify receiver if receiverInfo present
                if txid_clean in ids and str(t.get("currency", "")).upper() == "USDT" and amt > 0:
                    return q2(amt)
        except Exception as e:
            logging.warning("binance pay check error: %s", e)

        # 2. Check Binance On-Chain Deposit History (`GET /sapi/v1/capital/deposit/hisrec`)
        try:
            now2 = int(time.time() * 1000)
            dep_params = {
                "coin": "USDT",
                "startTime": now2 - 7 * 86400000,
                "endTime": now2,
                "limit": 100,
                "timestamp": now2,
                "recvWindow": 20000,
            }
            qs2 = urlencode(dep_params)
            sig2 = hmac.new(BINANCE_SECRET.encode(), qs2.encode(), hashlib.sha256).hexdigest()
            async with s.get(
                f"https://api.binance.com/sapi/v1/capital/deposit/hisrec?{qs2}&signature={sig2}",
                headers={"X-MBX-APIKEY": BINANCE_KEY},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r2:
                dep_data = await r2.json()
            logging.info("binance onchain resp: %s", str(dep_data)[:300])
            if isinstance(dep_data, list):
                for d_row in dep_data:
                    d_ids = {str(d_row.get("txId", "")).strip(), str(d_row.get("id", "")).strip()}
                    # status 1 = success
                    if txid_clean in d_ids and str(d_row.get("coin", "")).upper() == "USDT" and int(d_row.get("status", 0)) == 1:
                        return q2(d_row["amount"])
        except Exception as e:
            logging.warning("binance onchain check error: %s", e)
    return None


async def verify_bybit(txid: str):
    """
    Verifies a USDT deposit via Bybit V5 API:
    1) Checks Internal Deposit Records (`GET /v5/asset/deposit/query-internal-record`) matching
       either `txID` (the internal transfer ID shown to the user) OR `id` (row ID) with `status == 2` (Success).
    2) Checks On-Chain Deposit Records (`GET /v5/asset/deposit/query-record`) matching `txID` or `id`
       with `status == 3` (Success).
    """
    if not BYBIT_KEY or not BYBIT_SECRET:
        return None
    txid_clean = txid.strip()
    now = int(time.time() * 1000)
    rw = "20000"

    async with aiohttp.ClientSession() as s:
        # 1. Internal Off-Chain Transfers
        try:
            ts = str(int(time.time() * 1000))
            qs = urlencode({"coin": "USDT", "startTime": now - 7 * 86400000 + 60000, "endTime": now, "limit": 50})
            sig = hmac.new(BYBIT_SECRET.encode(), (ts + BYBIT_KEY + rw + qs).encode(), hashlib.sha256).hexdigest()
            hdr = {
                "X-BAPI-API-KEY": BYBIT_KEY,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-SIGN": sig,
                "X-BAPI-RECV-WINDOW": rw,
            }
            async with s.get(
                f"https://api.bybit.com/v5/asset/deposit/query-internal-record?{qs}",
                headers=hdr,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json()
            logging.info("bybit internal resp: %s", str(data)[:300])
            for t in (data.get("result") or {}).get("rows") or []:
                row_ids = {str(t.get("txID", "")).strip(), str(t.get("id", "")).strip()}
                if (
                    txid_clean in row_ids
                    and str(t.get("coin", "")).upper() == "USDT"
                    and str(t.get("status")) == "2"
                    and int(t.get("type", 1)) == 1
                ):
                    return q2(t["amount"])
        except Exception as e:
            logging.warning("bybit internal check error: %s", e)

        # 2. On-Chain Deposits
        try:
            ts2 = str(int(time.time() * 1000))
            qs2 = urlencode({"coin": "USDT", "startTime": now - 7 * 86400000 + 60000, "endTime": now, "limit": 50})
            sig2 = hmac.new(BYBIT_SECRET.encode(), (ts2 + BYBIT_KEY + rw + qs2).encode(), hashlib.sha256).hexdigest()
            hdr2 = {
                "X-BAPI-API-KEY": BYBIT_KEY,
                "X-BAPI-TIMESTAMP": ts2,
                "X-BAPI-SIGN": sig2,
                "X-BAPI-RECV-WINDOW": rw,
            }
            async with s.get(
                f"https://api.bybit.com/v5/asset/deposit/query-record?{qs2}",
                headers=hdr2,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r2:
                data2 = await r2.json()
            logging.info("bybit onchain resp: %s", str(data2)[:300])
            for t in (data2.get("result") or {}).get("rows") or []:
                row_ids = {str(t.get("txID", "")).strip(), str(t.get("id", "")).strip()}
                if (
                    txid_clean in row_ids
                    and str(t.get("coin", "")).upper() == "USDT"
                    and str(t.get("status")) == "3"
                ):
                    return q2(t["amount"])
        except Exception as e:
            logging.warning("bybit onchain check error: %s", e)
    return None


# ================= PERSISTENT REPLY KEYBOARD & MAIN MENU =================
def bottom_reply_kb() -> ReplyKeyboardMarkup:
    """Persistent bottom navigation keyboard matching Screenshot 1."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KBtn(text="🛍 Shop", style="success"), KBtn(text="💳 Top Up", style="success")],
            [KBtn(text="👤 Profile", style="primary"), KBtn(text="💬 Support", style="primary")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu(uid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(Btn(text="🛍 Browse Shop (Organized Folders)", callback_data="shop:folders", style="success"))
    b.row(
        Btn(text="💳 Top Up / Deposit", callback_data="deposit", style="primary"),
        Btn(text="💰 My Wallet", callback_data="wallet", style="primary"),
    )
    b.row(
        Btn(text="📜 My Orders", callback_data="myorders", style="primary"),
        Btn(text="👤 Profile & Settings", callback_data="profile", style="primary"),
    )
    b.row(
        Btn(text="🔍 Search Products", callback_data="search", style="primary"),
        Btn(text="💬 Help & Support", callback_data="support", style="primary"),
    )
    if uid in ADMINS:
        b.row(Btn(text="🛠 Admin Control Panel", callback_data="adm", style="danger"))
    add_contact(b)
    return b.as_markup()


async def welcome_text(user) -> str:
    bal = await balance(user.id)
    bdt_eq = q2(bal * BDT_RATE)
    orders_cnt = await pool.fetchval("SELECT count(*) FROM orders WHERE user_id=$1 AND status='done'", user.id) or 0
    return (
        f"✨ <b>Welcome to {esc(SHOP_NAME)}!</b>\n"
        f"<blockquote>Premium AI tools, digital subscriptions & instant automated delivery.</blockquote>\n\n"
        f"👤 <b>Account:</b> {esc(user.full_name)} (<code>{user.id}</code>)\n"
        f"💰 <b>Wallet Balance:</b> <code>{bal} USDT</code> <i>(≈ {bdt_eq} BDT)</i>\n"
        f"📦 <b>Completed Orders:</b> <code>{orders_cnt}</code>\n\n"
        f"👇 <b>Select an option below or use <code>/products</code> to browse:</b>"
    )


# ================= SLASH COMMANDS & BOTTOM MENU HANDLERS =================
@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt = await welcome_text(m.from_user)
    await safe_answer(m, "⚡ <b>Main Menu Initialized</b>", reply_markup=bottom_reply_kb())
    await safe_answer(m, txt, reply_markup=main_menu(m.from_user.id))


@dp.message(Command("products"))
@dp.message(F.text.in_({"🛍 Shop", "🛍️ Shop", "Shop"}))
async def cmd_products(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt, kb = await build_folders_view()
    await safe_answer(m, txt, reply_markup=kb)


@dp.message(Command("deposit"))
@dp.message(F.text.in_({"💳 Top Up", "Top Up"}))
async def cmd_deposit(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt, kb = deposit_menu_view()
    await safe_answer(m, txt, reply_markup=kb)


@dp.message(Command("orders"))
async def cmd_orders(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt, kb = await myorders_view(m.from_user.id)
    await safe_answer(m, txt, reply_markup=kb)


@dp.message(Command("settings"))
@dp.message(Command("profile"))
@dp.message(F.text.in_({"👤 Profile", "Profile"}))
async def cmd_profile(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt, kb = await profile_view(m.from_user)
    await safe_answer(m, txt, reply_markup=kb)


@dp.message(Command("support"))
@dp.message(F.text.in_({"💬 Support", "Support"}))
async def cmd_support(m: Message, state: FSMContext):
    await state.clear()
    txt, kb = support_view()
    await safe_answer(m, txt, reply_markup=kb)


@dp.message(Command("search"))
async def cmd_search(m: Message, state: FSMContext):
    await state.set_state(SearchSt.q)
    b = InlineKeyboardBuilder()
    b.row(Btn(text="❌ Cancel Search", callback_data="shop:folders", style="danger"))
    await safe_answer(
        m,
        "🔍 <b>Search Products</b>\n\nType the product or brand name you are looking for (e.g. <code>ChatGPT</code>, <code>Gemini</code>, <code>CapCut</code>, <code>Netflix</code>):",
        reply_markup=b.as_markup(),
    )


@dp.message(Command("api"))
async def cmd_api_info(m: Message, state: FSMContext):
    await state.clear()
    txt, kb = support_view(api_inquiry=True)
    await safe_answer(m, txt, reply_markup=kb)


@dp.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    txt = await welcome_text(m.from_user)
    await safe_answer(m, "✅ <b>Current action cancelled.</b>\n\n" + txt, reply_markup=main_menu(m.from_user.id))


@dp.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    txt = await welcome_text(c.from_user)
    await safe_edit(c, txt, reply_markup=main_menu(c.from_user.id))


# ================= PROFILE, WALLET, SUPPORT & CONTACT =================
async def profile_view(user):
    bal = await balance(user.id)
    bdt_eq = q2(bal * BDT_RATE)
    stats = await pool.fetchrow(
        """SELECT
            (SELECT count(*) FROM orders WHERE user_id=$1 AND status='done') AS orders_ok,
            (SELECT coalesce(sum(cost),0) FROM orders WHERE user_id=$1 AND status='done') AS total_spent,
            (SELECT coalesce(sum(amount),0) FROM deposits WHERE user_id=$1) AS total_dep
        """,
        user.id,
    )
    txt = (
        f"👤 <b>Your Profile & Settings</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"🏷 <b>Name:</b> {esc(user.full_name)}\n"
        f"🔗 <b>Username:</b> @{esc(user.username) if user.username else 'Not set'}\n\n"
        f"💳 <b>Wallet Balance:</b> <code>{bal} USDT</code> <i>(≈ {bdt_eq} BDT)</i>\n"
        f"📥 <b>Total Deposited:</b> <code>{q2(stats['total_dep'])} USDT</code>\n"
        f"🛍 <b>Completed Orders:</b> <code>{stats['orders_ok']}</code>\n"
        f"💵 <b>Total Spent:</b> <code>{q2(stats['total_spent'])} USDT</code>\n"
        f"🌐 <b>Language:</b> English (Default)"
    )
    b = InlineKeyboardBuilder()
    b.row(
        Btn(text="💳 Top Up Wallet", callback_data="deposit", style="success"),
        Btn(text="📜 Order History", callback_data="myorders", style="primary"),
    )
    b.row(Btn(text="🛍 Browse Products", callback_data="shop:folders", style="primary"))
    b.row(Btn(text="⬅️ Back to Main Menu", callback_data="home", style="danger"))
    return txt, b.as_markup()


@dp.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    txt, kb = await profile_view(c.from_user)
    await safe_edit(c, txt, reply_markup=kb)


@dp.callback_query(F.data == "wallet")
async def cb_wallet(c: CallbackQuery):
    bal = await balance(c.from_user.id)
    bdt_eq = q2(bal * BDT_RATE)
    b = InlineKeyboardBuilder()
    b.row(Btn(text="➕ Add Funds (Deposit)", callback_data="deposit", style="success"))
    b.row(Btn(text="📜 My Orders", callback_data="myorders", style="primary"))
    b.row(Btn(text="⬅️ Back to Main Menu", callback_data="home", style="danger"))
    txt = (
        f"💰 <b>My Wallet</b>\n\n"
        f"💵 <b>Available Balance:</b> <code>{bal} USDT</code>\n"
        f"🇧🇩 <b>BDT Equivalent:</b> <code>{bdt_eq} BDT</code> <i>(Rate: 1 USDT = {BDT_RATE} BDT)</i>\n\n"
        f"<i>Instant automated top-ups available via Binance Pay, Bybit UID, and bKash.</i>"
    )
    await safe_edit(c, txt, reply_markup=b.as_markup())


def support_view(api_inquiry: bool = False):
    num = (ST.get("contact_number") or CONTACT_NUMBER or "").strip()
    w_url = get_whatsapp_url("Hello! I need support regarding your shop.")
    header = "🔌 <b>Reseller & API Access</b>\n\nContact our admin team below to request API keys or bulk reseller pricing:\n\n" if api_inquiry else (
        "💬 <b>Customer Support & Contact Center</b>\n\n"
        "Need help with an order, deposit, or custom product request? Reach out to us directly via any option below:\n\n"
    )
    lines = [header]
    if ADMIN_USERNAME:
        lines.append(f"📩 <b>Telegram Admin:</b> @{esc(ADMIN_USERNAME)}")
    if w_url:
        lines.append(f"💬 <b>WhatsApp:</b> <a href=\"{esc(w_url)}\">Click to Chat on WhatsApp</a>")
    if num:
        lines.append(f"📞 <b>Contact Number (Call / WhatsApp):</b> <code>{esc(num)}</code> <i>(Tap to copy)</i>")
    if not (ADMIN_USERNAME or w_url or num):
        lines.append("ℹ️ <i>Contact details will appear here once configured by the Admin.</i>")
    lines.append("\n⏱ <b>Support Hours:</b> 24/7 Fast Response")

    b = InlineKeyboardBuilder()
    add_contact(b, "Hello! I need assistance with my account/order.")
    b.row(Btn(text="🛍 Back to Shop", callback_data="shop:folders", style="primary"))
    b.row(Btn(text="⬅️ Main Menu", callback_data="home", style="danger"))
    return "\n".join(lines), b.as_markup()


@dp.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery):
    txt, kb = support_view()
    await safe_edit(c, txt, reply_markup=kb)


@dp.callback_query(F.data == "contact:phone")
async def cb_contact_phone(c: CallbackQuery):
    num = (ST.get("contact_number") or CONTACT_NUMBER or "").strip()
    if not num:
        return await c.answer("Contact number is not set yet.", show_alert=True)
    await c.answer(f"📞 Contact Number: {num}", show_alert=True)
    try:
        await bot.send_contact(
            chat_id=c.from_user.id,
            phone_number=num,
            first_name=SHOP_NAME[:30],
            last_name="Support",
        )
    except Exception:
        pass
    await c.message.answer(
        f"📞 <b>Direct Contact Number:</b> <code>{esc(num)}</code>\n"
        f"<i>Tap the number above to copy it or call directly from the contact card.</i>",
        parse_mode="HTML",
    )


# ================= ORDERS HISTORY =================
async def myorders_view(uid: int):
    rows = await pool.fetch(
        "SELECT id,product,qty,cost,created FROM orders WHERE user_id=$1 AND status='done' "
        "ORDER BY id DESC LIMIT 10",
        uid,
    )
    b = InlineKeyboardBuilder()
    for r in rows:
        b.row(
            Btn(
                text=f"📦 #{r['id']} {r['product'][:26]} ×{r['qty']} — {r['cost']} USDT",
                callback_data=f"o:{r['id']}",
                style="primary",
            )
        )
    b.row(Btn(text="🛍 Browse Shop", callback_data="shop:folders", style="success"))
    b.row(Btn(text="⬅️ Back to Main Menu", callback_data="home", style="danger"))
    txt = (
        "📜 <b>Your Recent Completed Orders</b>\n<i>Tap any order below to view delivery credentials:</i>"
        if rows
        else "📜 <b>No Completed Orders Yet</b>\n\nYou haven't purchased any items yet. Tap <b>Browse Shop</b> below to explore our products!"
    )
    return txt, b.as_markup()


@dp.callback_query(F.data == "myorders")
async def cb_myorders(c: CallbackQuery):
    txt, kb = await myorders_view(c.from_user.id)
    await safe_edit(c, txt, reply_markup=kb)


@dp.callback_query(F.data.startswith("o:"))
async def cb_order_view(c: CallbackQuery):
    r = await pool.fetchrow(
        "SELECT id,product,qty,cost,delivery,created AT TIME ZONE 'Asia/Dhaka' AS t "
        "FROM orders WHERE id=$1 AND user_id=$2",
        int(c.data[2:]),
        c.from_user.id,
    )
    if not r:
        return await c.answer("Order not found.", show_alert=True)
    b = InlineKeyboardBuilder()
    b.row(Btn(text="⬅️ Back to My Orders", callback_data="myorders", style="danger"))
    await safe_edit(
        c,
        f"📦 <b>Order #{r['id']} Details</b>\n\n"
        f"🏷 <b>Product:</b> {esc(r['product'])}\n"
        f"🔢 <b>Quantity:</b> <code>{r['qty']}</code>\n"
        f"💵 <b>Total Paid:</b> <code>{r['cost']} USDT</code>\n"
        f"🕒 <b>Date:</b> <code>{r['t']:%Y-%m-%d %H:%M}</code>\n\n"
        f"🔑 <b>Delivered Credentials / License:</b>\n"
        f"<code>{esc((r['delivery'] or '')[:3500])}</code>",
        reply_markup=b.as_markup(),
    )


# ================= ORGANIZED SHOP: FOLDERS, FAVOURITES & PRODUCT LISTS =================
PAGE = 8


async def build_folders_view(force: bool = False):
    """
    Builds the Organized Category Folders screen (`/products` or `Shop`):
    1. Pinned at the very top: Top 3 Favourite products with GREEN background (`style='success'`).
    2. Organized Category Folders in a 3-column grid:
       - Any folder that contains at least 1 of the Top 3 Favourite products ALSO turns GREEN (`style='success'`).
       - All other folders are BLUE (`style='primary'`).
    3. Bottom utility buttons: `🔍 Search`, `🛍 All items`, `🔄 Refresh`, `◀️ Back`.
    """
    ps = await get_products(force=force)
    p_by_id = {str(p["id"]): p for p in ps}
    fav_products = [p_by_id[pid] for pid in FAVS if pid in p_by_id][:3]
    fav_cats = {detect_category(p) for p in fav_products}

    # Group products by category
    cat_counts = {cid: 0 for cid, _, _, _ in CATEGORIES}
    for p in ps:
        cid = detect_category(p)
        cat_counts[cid] = cat_counts.get(cid, 0) + 1

    b = InlineKeyboardBuilder()

    # 1. Top 3 Favourite Products (Full-width rows at the very top, GREEN background)
    for fp in fav_products:
        _, ce_id = detect_product_icon(fp)
        # Keep favourite products green on the top showcase (or red if out of stock)
        btn_style = "success" if fp["ok"] else "danger"
        b.row(
            Btn(
                text=format_product_btn_text(fp),
                callback_data=f"p:{fp['id']}:folders",
                style=btn_style,
                icon_custom_emoji_id=ce_id,
            )
        )

    # 2. Category Folders (3-column grid like Screenshot 1)
    folder_btns = []
    show_all = ST.get("show_empty_folders", False) or len(ps) == 0
    for cid, label, icon, _ in CATEGORIES:
        cnt = cat_counts.get(cid, 0)
        if not show_all and cnt == 0 and cid not in fav_cats:
            continue
        # Folder is GREEN if it contains any of the Top 3 Favourite products, otherwise BLUE
        f_style = "success" if cid in fav_cats else "primary"
        ce_id = str(CUSTOM_EMOJI_MAP[cid]) if cid in CUSTOM_EMOJI_MAP else None
        folder_btns.append(
            Btn(
                text=f"{icon} {label}",
                callback_data=f"cat:{cid}:0",
                style=f_style,
                icon_custom_emoji_id=ce_id,
            )
        )

    for i in range(0, len(folder_btns), 3):
        b.row(*folder_btns[i : i + 3])

    # 3. Search & All Items row (matching Screenshot 1)
    b.row(
        Btn(text="🔍 Search", callback_data="search", style="primary"),
        Btn(text="🛍 All items", callback_data="all:0", style="primary"),
    )
    b.row(
        Btn(text="🔄 Refresh", callback_data="shop:folders:ref", style="success"),
        Btn(text="◀️ Back", callback_data="home", style="danger"),
    )

    total_in_stock = sum(1 for p in ps if p["ok"])
    txt = (
        f"🛍 <b>{esc(SHOP_NAME)} — Product Categories</b>\n"
        f"<blockquote>📦 Total Products: <b>{len(ps)}</b>  •  ✅ In Stock: <b>{total_in_stock}</b>  •  ⭐ Featured: <b>{len(fav_products)}/3</b></blockquote>\n"
        f"🟢 <b>Green</b> = Featured / Favourite  |  🔵 <b>Blue</b> = Available  |  🔴 <b>Red</b> = Out of Stock\n\n"
        f"👇 <b>Tap a featured product at the top or choose a category folder:</b>"
    )
    return txt, b.as_markup()


async def build_product_list_view(mode: str, page: int, force: bool = False):
    """
    Builds the product list for either:
    - `mode == 'all'` (`🛍 All items`)
    - `mode == 'cat:<cid>'` (Specific category folder, e.g. `cat:gemini`)
    Sorting & Coloring Rules:
    1) Pinned at the very top: Top 3 Favourite products (belonging to this folder, or all 3 if `pin_all_favs_in_folders`
       is enabled or viewing a green folder / All items) with GREEN background (`style='success'`).
    2) In-stock products with BLUE background (`style='primary'`).
    3) Out-of-stock products with RED background (`style='danger'`).
    """
    ps = await get_products(force=force)
    p_by_id = {str(p["id"]): p for p in ps}
    fav_set = set(FAVS[:3])

    if mode == "all":
        title = "🛍 All Available Products"
        cat_items = list(ps)
        pinned_favs = [p_by_id[pid] for pid in FAVS[:3] if pid in p_by_id]
        origin_prefix = f"all-{page}"
        page_cb_prefix = "all"
    else:
        cid = mode.split(":", 1)[1]
        label, icon, _ = CAT_MAP.get(cid, ("Products", "📦", []))
        title = f"{icon} {label} — Products"
        cat_items = [p for p in ps if detect_category(p) == cid]
        # Favourites belonging to this folder first; if admin enabled pin_all_favs_in_folders and folder has favs, show all 3
        own_favs = [p_by_id[pid] for pid in FAVS[:3] if pid in p_by_id and detect_category(p_by_id[pid]) == cid]
        if ST.get("pin_all_favs_in_folders", True) and own_favs:
            other_favs = [p_by_id[pid] for pid in FAVS[:3] if pid in p_by_id and pid not in {x["id"] for x in own_favs}]
            pinned_favs = own_favs + other_favs
        else:
            pinned_favs = own_favs
        origin_prefix = f"cat-{cid}-{page}"
        page_cb_prefix = f"cat:{cid}"

    pinned_ids = {str(p["id"]) for p in pinned_favs}
    remaining = [p for p in cat_items if str(p["id"]) not in pinned_ids]
    in_stock = [p for p in remaining if p["ok"]]
    out_of_stock = [p for p in remaining if not p["ok"]]

    ordered_products = pinned_favs + in_stock + out_of_stock
    total_items = len(ordered_products)
    total_pages = max(1, (total_items - 1) // PAGE + 1)
    page = max(0, min(page, total_pages - 1))

    b = InlineKeyboardBuilder()
    slice_items = ordered_products[page * PAGE : (page + 1) * PAGE]

    for p in slice_items:
        is_fav = (str(p["id"]) in fav_set) or (str(p["id"]) in pinned_ids)
        b.row(make_product_btn(p, is_fav=is_fav, origin=origin_prefix))

    # Pagination & Refresh Row (matching Screenshot 3: Green Refresh, Grey 1/7, Blue Next, Red Back)
    nav = [Btn(text="🔄 Refresh", callback_data=f"{page_cb_prefix}:{page}:ref", style="success")]
    if page > 0:
        nav.append(Btn(text="◀️ Prev", callback_data=f"{page_cb_prefix}:{page-1}", style="primary"))
    nav.append(Btn(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if (page + 1) < total_pages:
        nav.append(Btn(text="▶️ Next", callback_data=f"{page_cb_prefix}:{page+1}", style="primary"))
    b.row(*nav)

    b.row(Btn(text="◀️ Back", callback_data="shop:folders", style="danger"))

    if not ordered_products:
        txt = f"<b>{esc(title)}</b>\n\nℹ️ No products are currently listed in this folder."
    else:
        txt = (
            f"<b>{esc(title)}</b>\n"
            f"<blockquote>🟢 <b>Top Favourite</b>  •  🔵 <b>In Stock</b>  •  🔴 <b>Out of Stock</b></blockquote>\n"
            f"Available Products (Page {page+1}/{total_pages}):"
        )
    return txt, b.as_markup()


@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data.in_({"shop:0", "shop:folders", "shop:folders:ref"}))
async def cb_shop_folders(c: CallbackQuery, state: FSMContext):
    await state.clear()
    force = c.data.endswith(":ref")
    txt, kb = await build_folders_view(force=force)
    await safe_edit(c, txt, reply_markup=kb)
    if force:
        try:
            await c.answer("✅ Stock & prices refreshed!")
        except Exception:
            pass


@dp.callback_query(F.data.startswith("all:"))
async def cb_all_items(c: CallbackQuery):
    parts = c.data.split(":")
    page = int(parts[1])
    force = len(parts) > 2 and parts[2] == "ref"
    txt, kb = await build_product_list_view("all", page, force=force)
    await safe_edit(c, txt, reply_markup=kb)
    if force:
        try:
            await c.answer("✅ Refreshed!")
        except Exception:
            pass


@dp.callback_query(F.data.startswith("cat:"))
async def cb_cat_items(c: CallbackQuery):
    parts = c.data.split(":")
    cid = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0
    force = len(parts) > 3 and parts[3] == "ref"
    txt, kb = await build_product_list_view(f"cat:{cid}", page, force=force)
    await safe_edit(c, txt, reply_markup=kb)
    if force:
        try:
            await c.answer("✅ Refreshed!")
        except Exception:
            pass


# ================= PRODUCT SEARCH =================
@dp.callback_query(F.data == "search")
async def cb_search(c: CallbackQuery, state: FSMContext):
    await state.set_state(SearchSt.q)
    b = InlineKeyboardBuilder()
    b.row(Btn(text="⬅️ Back to Categories", callback_data="shop:folders", style="danger"))
    await safe_edit(
        c,
        "🔍 <b>Search Products</b>\n\n"
        "Type any keyword to search across all categories (e.g. <code>ChatGPT</code>, <code>Gemini</code>, <code>CapCut</code>, <code>Plus</code>, <code>Pro</code>):\n"
        "<i>(Or send /cancel to abort)</i>",
        reply_markup=b.as_markup(),
    )


@dp.message(SearchSt.q, F.text)
async def search_input(m: Message, state: FSMContext):
    q = m.text.strip().lower()
    await state.clear()
    ps = await get_products()
    fav_set = set(FAVS[:3])
    matches = [p for p in ps if q in p["name"].lower() or q in detect_category(p)]
    b = InlineKeyboardBuilder()
    for p in matches[:20]:
        b.row(make_product_btn(p, is_fav=(str(p["id"]) in fav_set), origin="folders"))
    b.row(
        Btn(text="🔍 Search Again", callback_data="search", style="primary"),
        Btn(text="⬅️ Categories", callback_data="shop:folders", style="danger"),
    )
    if not matches:
        await safe_answer(
            m,
            f"🔍 No products matched <code>{esc(m.text.strip())}</code>.\nTry another keyword or browse our organized folders:",
            reply_markup=b.as_markup(),
        )
    else:
        await safe_answer(
            m,
            f"🔍 <b>Search Results for \"{esc(m.text.strip())}\"</b> ({len(matches)} found):",
            reply_markup=b.as_markup(),
        )


# ================= PRODUCT DETAILS & PURCHASE FLOW =================
def resolve_back_cb(origin: str) -> str:
    if not origin or origin == "folders":
        return "shop:folders"
    if origin.startswith("all-"):
        pg = origin.split("-")[1]
        return f"all:{pg}"
    if origin.startswith("cat-"):
        parts = origin.split("-")
        return f"cat:{parts[1]}:{parts[2]}"
    return "shop:folders"


@dp.callback_query(F.data.startswith("p:"))
async def cb_product(c: CallbackQuery):
    parts = c.data.split(":", 2)
    pid = parts[1]
    origin = parts[2] if len(parts) > 2 else "folders"
    back_cb = resolve_back_cb(origin)

    p = await find(pid, force=True)
    if not p:
        return await c.answer("❌ Product no longer exists.", show_alert=True)

    icon, _ = detect_product_icon(p)
    cid = detect_category(p)
    cat_label, cat_icon, _ = CAT_MAP.get(cid, ("Others", "📦", []))
    is_fav = str(p["id"]) in FAVS[:3]
    st = "Unlimited (∞)" if p["stock"] is None else (f"{p['stock']} available" if p["ok"] else "0 (Out of Stock)")
    note, _ = note_for(p)

    b = InlineKeyboardBuilder()
    if p["ok"]:
        qty_btns = []
        for n in (1, 2, 5, 10):
            if p["stock"] is None or n <= p["stock"]:
                qty_btns.append(Btn(text=f"🛒 Buy {n}×", callback_data=f"c:{p['id']}:{n}", style="success" if n == 1 else "primary"))
        if qty_btns:
            b.row(*qty_btns[:2])
            if len(qty_btns) > 2:
                b.row(*qty_btns[2:])
        b.row(Btn(text="✏️ Custom Quantity", callback_data=f"cq:{p['id']}", style="primary"))
    else:
        b.row(Btn(text="🔴 Out of Stock — Request Restock", callback_data="support", style="danger"))

    add_contact(b, f"Hello! I have a question about: {p['name']} (ID: {p['id']})")
    b.row(Btn(text="⬅️ Back to Products", callback_data=back_cb, style="danger"))

    fav_badge = "🌟 <b>TOP FEATURED PRODUCT</b>\n" if is_fav else ""
    status_badge = "🟢 In Stock" if p["ok"] else "🔴 Out of Stock"
    txt = (
        f"{fav_badge}"
        f"{icon} <b>{esc(p['name'])}</b>\n"
        f"📁 <b>Category:</b> {cat_icon} {esc(cat_label)}  •  <b>Status:</b> {status_badge}\n"
        f"💵 <b>Price:</b> <code>{sell_price(p)} USDT</code> <i>(≈ {q2(sell_price(p)*BDT_RATE)} BDT)</i>\n"
        f"📊 <b>Stock:</b> <code>{st}</code>\n\n"
        f"📝 <b>Product Details & Terms:</b>\n"
        f"<blockquote>{esc(note) if note else 'Instant automated delivery after confirmation.'}</blockquote>\n\n"
        f"{'👇 <b>Select quantity below to purchase:</b>' if p['ok'] else '⚠️ <i>This item is currently out of stock. Contact support to pre-order or request a restock!</i>'}"
    )
    await safe_edit(c, txt, reply_markup=b.as_markup())


def confirm_view(p, qty):
    total = sell_price(p) * qty
    bdt_total = q2(total * BDT_RATE)
    icon, _ = detect_product_icon(p)
    b = InlineKeyboardBuilder()
    b.row(
        Btn(text=f"✅ Confirm & Pay ({total} USDT)", callback_data=f"buy:{p['id']}:{qty}:{int(total*100)}", style="success"),
        Btn(text="❌ Cancel", callback_data=f"p:{p['id']}:folders", style="danger"),
    )
    add_contact(b, f"Order inquiry: {p['name']} ×{qty}")
    txt = (
        f"🧾 <b>Order Confirmation</b>\n\n"
        f"{icon} <b>Product:</b> {esc(p['name'])}\n"
        f"🔢 <b>Quantity:</b> <code>{qty}</code>\n"
        f"💵 <b>Total Cost:</b> <code>{total} USDT</code> <i>(≈ {bdt_total} BDT)</i>\n\n"
        f"⚠️ <i>Please make sure you have read the product notes carefully. Clicking <b>Confirm & Pay</b> will deduct {total} USDT from your wallet and deliver your order immediately.</i>"
    )
    return txt, b.as_markup()


@dp.callback_query(F.data.startswith("c:"))
async def cb_confirm(c: CallbackQuery):
    _, pid, qty = c.data.split(":")
    qty = int(qty)
    p = await find(pid, force=True)
    if not p or not p["ok"] or (p["stock"] is not None and p["stock"] < qty):
        return await c.answer("❌ Insufficient stock for this quantity.", show_alert=True)
    t, kb = confirm_view(p, qty)
    await safe_edit(c, t, reply_markup=kb)


@dp.callback_query(F.data.startswith("cq:"))
async def cb_custom_qty(c: CallbackQuery, state: FSMContext):
    p = await find(c.data[3:], force=True)
    if not p or not p["ok"]:
        return await c.answer("❌ Out of stock.", show_alert=True)
    mx = p["stock"] if p["stock"] is not None else 100
    await state.set_state(Qty.n)
    await state.update_data(pid=str(p["id"]))
    await c.message.answer(
        f"🔢 <b>Enter Custom Quantity</b>\nHow many units of <b>{esc(p['name'])}</b> would you like to buy? (<code>1</code> to <code>{mx}</code>)\n<i>(Send /cancel to abort)</i>",
        parse_mode="HTML",
    )
    await c.answer()


@dp.message(Qty.n, F.text)
async def qty_in(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("⚠️ Please enter a valid number.")
    n = int(m.text.strip())
    p = await find((await state.get_data())["pid"], force=True)
    if not p or not p["ok"]:
        await state.clear()
        return await m.answer("❌ Sorry, this product is now out of stock.")
    mx = p["stock"] if p["stock"] is not None else 100
    if not 1 <= n <= mx:
        return await m.answer(f"⚠️ Please enter a number between 1 and {mx}.")
    await state.clear()
    t, kb = confirm_view(p, n)
    await safe_answer(m, t, reply_markup=kb)


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery):
    _, pid, qty, cents = c.data.split(":")
    qty, uid = int(qty), c.from_user.id
    p = await find(pid, force=True)
    if not p or not p["ok"] or (p["stock"] is not None and p["stock"] < qty):
        return await c.answer("❌ Sorry, this product just ran out of stock.", show_alert=True)
    total, base = sell_price(p) * qty, p["price"] * qty
    if int(total * 100) != int(cents):
        b = InlineKeyboardBuilder()
        b.row(Btn(text="🔄 View Updated Price", callback_data=f"p:{pid}:folders", style="primary"))
        return await safe_edit(
            c,
            f"⚠️ <b>Price Updated</b>\nThe price for this product was updated to <code>{sell_price(p)} USDT</code> per unit.",
            reply_markup=b.as_markup(),
        )
    if await deduct(uid, total) is None:
        return await c.answer("❌ Insufficient wallet balance! Please deposit funds first.", show_alert=True)

    await safe_edit(c, "⏳ <b>Processing your order...</b>\n<i>Please wait while we fetch your credentials.</i>")
    rid = f"u{uid}-{uuid.uuid4().hex[:12]}"
    try:
        status, data = await api_order(pid, qty, rid)
        logging.info("ORDER RESP %s %s", status, str(data)[:500])
        ok = status == 200 and not data.get("error") and data.get("ok", data.get("success", True)) is not False
    except Exception as e:
        logging.exception(e)
        ok, data = False, {"error": "timeout"}

    if not isinstance(data, dict):
        data = {"items": data}
    ins = "INSERT INTO orders(user_id,pid,product,qty,cost,base_cost,request_id,status,delivery) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)"

    if not ok:
        await credit(uid, total)  # Automatic refund
        await pool.execute(ins, uid, str(pid), p["name"], qty, total, base, rid, "failed", str(data)[:500])
        await notify(f"⚠️ <b>Order Failed (Refunded)</b>\nUser: <code>{uid}</code>\n{esc(p['name'])} ×{qty}\n<code>{esc(str(data)[:300])}</code>", parse_mode="HTML")
        b = InlineKeyboardBuilder()
        b.row(Btn(text="⬅️ Back to Shop", callback_data="shop:folders", style="primary"))
        add_contact(b, f"Order failed for {p['name']} ×{qty}")
        return await safe_edit(
            c,
            "❌ <b>Order could not be completed automatically.</b>\n"
            f"Your <code>{total} USDT</code> has been refunded to your wallet immediately. Please try again shortly or contact support.",
            reply_markup=b.as_markup(),
        )

    items = data.get("items") or data.get("delivered") or data.get("data") or data
    if isinstance(items, list):
        items = "\n".join(i if isinstance(i, str) else json.dumps(i, ensure_ascii=False) for i in items)
    elif not isinstance(items, str):
        items = json.dumps(items, ensure_ascii=False, indent=1)

    await pool.execute(ins, uid, str(pid), p["name"], qty, total, base, rid, "done", items)
    bal = await balance(uid)
    b = InlineKeyboardBuilder()
    b.row(
        Btn(text="🛍 Continue Shopping", callback_data="shop:folders", style="success"),
        Btn(text="📜 My Orders", callback_data="myorders", style="primary"),
    )
    await safe_edit(
        c,
        f"✅ <b>Order Completed Successfully!</b>\n\n"
        f"📦 <b>Product:</b> {esc(p['name'])} × {qty}\n"
        f"💵 <b>Paid:</b> <code>{total} USDT</code>\n\n"
        f"🔑 <b>Your Delivery / Credentials:</b>\n"
        f"<code>{esc(items[:3500])}</code>\n\n"
        f"💰 <b>Remaining Wallet Balance:</b> <code>{bal} USDT</code>",
        reply_markup=b.as_markup(),
    )
    _cache["t"] = 0
    u = c.from_user
    await notify(
        f"🛍 <b>New Sale Completed!</b>\n"
        f"👤 {esc(u.full_name)} (@{esc(u.username or 'N/A')}) <code>{uid}</code>\n"
        f"📦 {esc(p['name'])} ×{qty}\n"
        f"💵 Paid: <b>{total} USDT</b> | 📈 Profit: <b>{q2(total - base)} USDT</b>",
        parse_mode="HTML",
    )


# ================= DEPOSIT & AUTO-VERIFICATION FLOW =================
def deposit_menu_view():
    b = InlineKeyboardBuilder()
    b.row(
        Btn(text="🟡 Binance Pay / USDT (Auto)", callback_data="dep:binance", style="success"),
        Btn(text="⚫ Bybit UID / USDT (Auto)", callback_data="dep:bybit", style="success"),
    )
    b.row(Btn(text="🟣 bKash Send Money (BDT)", callback_data="dep:bkash", style="primary"))
    add_contact(b, "Hello! I need help with depositing funds.")
    b.row(Btn(text="⬅️ Back to Main Menu", callback_data="home", style="danger"))
    txt = (
        f"💳 <b>Add Funds to Your Wallet</b>\n\n"
        f"⚡ <b>Binance & Bybit deposits are verified automatically within seconds!</b>\n"
        f"💱 <b>bKash Rate:</b> <code>1 USDT = {BDT_RATE} BDT</code>\n"
        f"📉 <b>Minimum Deposit:</b> <code>{MIN_DEPOSIT} USDT</code>\n\n"
        f"👇 <b>Choose your preferred payment method:</b>"
    )
    return txt, b.as_markup()


@dp.callback_query(F.data == "deposit")
async def cb_deposit(c: CallbackQuery, state: FSMContext):
    await state.clear()
    txt, kb = deposit_menu_view()
    await safe_edit(c, txt, reply_markup=kb)


@dp.callback_query(F.data.startswith("dep:"))
async def cb_dep_method(c: CallbackQuery, state: FSMContext):
    m = c.data[4:]
    b = InlineKeyboardBuilder()
    if m == "binance":
        await state.set_state(Dep.txid)
        await state.update_data(method="binance")
        txt = (
            f"🟡 <b>Binance Automated Deposit (USDT)</b>\n\n"
            f"🆔 <b>Binance Pay ID:</b> <code>{esc(BINANCE_PAY_ID or 'Not configured')}</code> <i>(Tap to copy)</i>\n\n"
            f"<b>How to Deposit:</b>\n"
            f"1️⃣ Open Binance App → <b>Binance Pay</b> → Send <b>USDT</b> to the Pay ID above.\n"
            f"2️⃣ After sending, copy the <b>Order ID</b> or <b>Transaction ID</b> from your receipt.\n"
            f"3️⃣ Paste and send the <b>Order ID / Transaction ID</b> right here in chat.\n\n"
            f"⚠️ <i>Minimum deposit: <b>{MIN_DEPOSIT} USDT</b>. Verified automatically in seconds!</i>"
        )
    elif m == "bybit":
        await state.set_state(Dep.txid)
        await state.update_data(method="bybit")
        txt = (
            f"⚫ <b>Bybit Automated Deposit (USDT)</b>\n\n"
            f"🆔 <b>Bybit UID:</b> <code>{esc(BYBIT_UID or 'Not configured')}</code> <i>(Tap to copy)</i>\n\n"
            f"<b>How to Deposit:</b>\n"
            f"1️⃣ Open Bybit App → Withdraw → <b>Internal Transfer (Free)</b> → Enter the UID above.\n"
            f"2️⃣ Send <b>USDT</b> and copy the <b>Transaction ID (txID)</b> from your transfer history.\n"
            f"3️⃣ Paste and send the <b>Transaction ID</b> right here in chat.\n\n"
            f"⚠️ <i>Minimum deposit: <b>{MIN_DEPOSIT} USDT</b>. Verified automatically in seconds!</i>"
        )
    else:
        b.row(Btn(text="✅ I Have Sent Payment (Submit TrxID)", callback_data="bk:paid", style="success"))
        txt = (
            f"🟣 <b>bKash Deposit (BDT → USDT)</b>\n\n"
            f"📱 <b>bKash Personal (Send Money):</b> <code>{esc(BKASH_NUMBER or 'Contact Admin')}</code> <i>(Tap to copy)</i>\n"
            f"💱 <b>Exchange Rate:</b> <code>1 USDT = {BDT_RATE} BDT</code>\n\n"
            f"<b>How to Deposit:</b>\n"
            f"1️⃣ Send BDT using <b>Send Money</b> to the bKash number above.\n"
            f"2️⃣ Copy your bKash <b>TrxID</b>.\n"
            f"3️⃣ Tap <b>✅ I Have Sent Payment</b> below to submit your TrxID and amount."
        )
    add_contact(b, f"Need help with {m.upper()} deposit")
    b.row(Btn(text="⬅️ Back to Payment Methods", callback_data="deposit", style="danger"))
    await safe_edit(c, txt, reply_markup=b.as_markup())


@dp.message(Dep.txid, F.text)
async def got_tx(m: Message, state: FSMContext):
    method = (await state.get_data())["method"]
    txid = m.text.strip()
    if len(txid) < 5 or len(txid) > 100:
        return await m.answer("⚠️ Please send a valid Order ID / Transaction ID (or send /cancel to abort).")
    wait = await m.answer("🔍 <b>Verifying your transaction with exchange API...</b>", parse_mode="HTML")
    try:
        amt = await (verify_binance(txid) if method == "binance" else verify_bybit(txid))
    except Exception as e:
        logging.exception(e)
        return await wait.edit_text("⚠️ Could not reach verification server right now. Please try again in 1 minute.")
    if not amt:
        return await wait.edit_text(
            "❌ <b>Payment Not Found Yet</b>\n\n"
            "Please double-check that:\n"
            "• You sent <b>USDT</b>\n"
            "• You copied the exact <b>Order ID / Transaction ID</b>\n"
            "• Or wait 30–60 seconds and send the ID again.",
            parse_mode="HTML",
        )
    if amt < MIN_DEPOSIT:
        return await wait.edit_text(f"❌ Minimum deposit amount is <b>{MIN_DEPOSIT} USDT</b>.", parse_mode="HTML")
    if not await credit_deposit(method, txid, m.from_user.id, amt):
        return await wait.edit_text("⚠️ <b>This Transaction ID has already been claimed!</b>", parse_mode="HTML")

    await state.clear()
    bal = await balance(m.from_user.id)
    await wait.edit_text(
        f"✅ <b>Deposit Verified!</b>\n\n"
        f"➕ <b>Credited:</b> <code>{amt} USDT</code> ({method.upper()})\n"
        f"💰 <b>New Wallet Balance:</b> <code>{bal} USDT</code>",
        parse_mode="HTML",
        reply_markup=main_menu(m.from_user.id),
    )
    u = m.from_user
    await notify(
        f"💵 <b>Auto-Verified Deposit: +{amt} USDT ({method.upper()})</b>\n"
        f"👤 {esc(u.full_name)} (@{esc(u.username or 'N/A')}) <code>{u.id}</code>\n"
        f"🧾 TxID: <code>{esc(txid)}</code>",
        parse_mode="HTML",
    )


# ---- bKash Deposit ----
@dp.callback_query(F.data == "bk:paid")
async def cb_bk_paid(c: CallbackQuery, state: FSMContext):
    await state.set_state(Bk.info)
    await c.message.answer(
        "🟣 <b>Submit bKash Payment Details</b>\n\n"
        "Please send your <b>TrxID</b> and <b>Amount (BDT)</b> separated by a space in a single message.\n"
        "📌 <b>Example:</b> <code>ABC123XYZ 500</code>\n"
        "<i>(Send /cancel to abort)</i>",
        parse_mode="HTML",
    )
    await c.answer()


@dp.message(Bk.info, F.text)
async def bk_info(m: Message, state: FSMContext):
    parts = m.text.split()
    try:
        amt = D(parts[-1])
        trx = parts[0].strip()
        assert len(parts) >= 2 and amt > 0
    except Exception:
        return await m.answer("⚠️ Invalid format! Please send like this:\n<code>ABC123XYZ 500</code>", parse_mode="HTML")
    rid = await pool.fetchval(
        "INSERT INTO bkash(user_id,trx,amount_bdt) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING id",
        m.from_user.id, trx, amt,
    )
    if not rid:
        return await m.answer("⚠️ This bKash TrxID has already been submitted.")
    await state.clear()
    usdt_est = q2(amt / BDT_RATE)
    await safe_answer(
        m,
        f"✅ <b>bKash Deposit Request Submitted!</b>\n\n"
        f"🧾 <b>TrxID:</b> <code>{esc(trx)}</code>\n"
        f"💵 <b>Amount:</b> <code>{amt} BDT</code> <i>(≈ {usdt_est} USDT)</i>\n\n"
        f"⏳ <i>Our admin is reviewing your payment. Your wallet will be credited as soon as it is approved!</i>",
        reply_markup=main_menu(m.from_user.id),
    )
    b = InlineKeyboardBuilder()
    b.row(
        Btn(text="✅ Approve", callback_data=f"bk:ok:{rid}", style="success"),
        Btn(text="❌ Reject", callback_data=f"bk:no:{rid}", style="danger"),
    )
    u = m.from_user
    await notify(
        f"🟣 <b>New bKash Deposit Request #{rid}</b>\n"
        f"👤 {esc(u.full_name)} (@{esc(u.username or 'N/A')}) <code>{u.id}</code>\n"
        f"🧾 TrxID: <code>{esc(trx)}</code>\n"
        f"💵 Amount: <b>{amt} BDT</b> ≈ <b>{usdt_est} USDT</b>\n"
        f"⚠️ <i>Verify in your bKash app before approving.</i>",
        parse_mode="HTML",
        reply_markup=b.as_markup(),
    )


@adm.callback_query(F.data.startswith("bk:ok:") | F.data.startswith("bk:no:"))
async def cb_bk_decide(c: CallbackQuery):
    _, act, rid = c.data.split(":")
    new = "approved" if act == "ok" else "rejected"
    r = await pool.fetchrow(
        "UPDATE bkash SET status=$2 WHERE id=$1 AND status='pending' RETURNING user_id,trx,amount_bdt",
        int(rid), new,
    )
    if not r:
        return await c.answer("Already processed.", show_alert=True)
    if act == "ok":
        usdt = q2(r["amount_bdt"] / BDT_RATE)
        await credit_deposit("bkash", r["trx"], r["user_id"], usdt)
        try:
            await bot.send_message(
                r["user_id"],
                f"✅ <b>bKash Deposit Approved!</b>\n➕ <code>{usdt} USDT</code> has been added to your wallet.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    else:
        try:
            await bot.send_message(
                r["user_id"],
                "❌ <b>Your bKash deposit request was rejected.</b>\nPlease contact Support if you believe this was a mistake.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    await c.message.edit_text(c.message.text + f"\n\n➡️ {new.upper()} by {c.from_user.full_name}")


# ================= ADMIN CONTROL PANEL =================
def adm_menu():
    b = InlineKeyboardBuilder()
    b.row(
        Btn(text="⭐ Top 3 Favourites", callback_data="adm:favs", style="success"),
        Btn(text="💲 Product Manager", callback_data="adm:pl:0", style="primary"),
    )
    b.row(
        Btn(text="📊 Sales & Stats", callback_data="adm:stats", style="primary"),
        Btn(text="🧾 Recent Orders", callback_data="adm:orders", style="primary"),
    )
    b.row(
        Btn(text="⚙️ Pricing Rule", callback_data="adm:rule", style="primary"),
        Btn(text="📞 Contact Settings", callback_data="adm:contact", style="primary"),
    )
    b.row(
        Btn(text="📁 Folder Options", callback_data="adm:fopt", style="primary"),
        Btn(text="🏦 Shop API Balance", callback_data="adm:api", style="primary"),
    )
    b.row(Btn(text="⬅️ Back to Main Menu", callback_data="home", style="danger"))
    return b.as_markup()


def back_adm():
    b = InlineKeyboardBuilder()
    b.row(Btn(text="⬅️ Back to Admin Panel", callback_data="adm", style="danger"))
    return b.as_markup()


@adm.message(Command("admin"))
async def adm_cmd(m: Message):
    await safe_answer(
        m,
        "🛠 <b>Admin Control Panel</b>\nManage products, Top 3 Favourites, folder settings, pricing rules, and stats:",
        reply_markup=adm_menu(),
    )


@adm.callback_query(F.data == "adm")
async def adm_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(
        c,
        "🛠 <b>Admin Control Panel</b>\nManage products, Top 3 Favourites, folder settings, pricing rules, and stats:",
        reply_markup=adm_menu(),
    )


# ---- Top 3 Favourites Manager ----
@adm.callback_query(F.data == "adm:favs")
async def adm_favs_view(c: CallbackQuery):
    ps = await get_products(True)
    p_by_id = {str(p["id"]): p for p in ps}
    b = InlineKeyboardBuilder()
    lines = [
        "⭐ <b>Top 3 Favourite Products Manager</b>\n",
        "<blockquote>• Up to <b>3 products</b> can be marked as Favourite.\n"
        "• They appear at the <b>very top of <code>/products</code></b> with a <b>GREEN background</b>.\n"
        "• Their category folder (e.g. <b>Gemini</b>) also turns <b>GREEN</b>.\n"
        "• Inside their folder, they stay pinned at the very top in <b>GREEN</b>!</blockquote>\n",
        "<b>Current Top 3 Favourites:</b>",
    ]
    if not FAVS:
        lines.append("<i>No favourite products selected yet.</i>")
    else:
        for idx, pid in enumerate(FAVS[:3], start=1):
            p = p_by_id.get(str(pid))
            if p:
                cid = detect_category(p)
                cat_label, cat_icon, _ = CAT_MAP.get(cid, ("Others", "📦", []))
                lines.append(f"{idx}️⃣ <b>{esc(p['name'])}</b> — <code>{sell_price(p)} USDT</code> ({cat_icon} {cat_label})")
                b.row(
                    Btn(text=f"❌ Remove #{idx}: {p['name'][:24]}", callback_data=f"adm:fav:rm:{pid}", style="danger")
                )
            else:
                lines.append(f"{idx}️⃣ Product ID <code>{esc(pid)}</code> (Not in current feed)")
                b.row(Btn(text=f"❌ Remove ID {pid}", callback_data=f"adm:fav:rm:{pid}", style="danger"))

    b.row(Btn(text="➕ Pick / Toggle Favourites from Product List", callback_data="adm:pl:0", style="success"))
    if FAVS:
        b.row(Btn(text="🗑 Clear All Favourites", callback_data="adm:fav:clear", style="danger"))
    b.row(Btn(text="⬅️ Back to Admin Panel", callback_data="adm", style="primary"))
    await safe_edit(c, "\n".join(lines), reply_markup=b.as_markup())


@adm.callback_query(F.data.startswith("adm:fav:rm:"))
async def adm_fav_rm(c: CallbackQuery):
    pid = c.data.split(":", 3)[3]
    if pid in FAVS:
        FAVS.remove(pid)
        await save_favs()
    await c.answer("Removed from Top 3 Favourites!")
    await adm_favs_view(c)


@adm.callback_query(F.data == "adm:fav:clear")
async def adm_fav_clear(c: CallbackQuery):
    FAVS.clear()
    await save_favs()
    await c.answer("Cleared all favourites!")
    await adm_favs_view(c)


@adm.callback_query(F.data.startswith("adm:fav:tg:"))
async def adm_fav_toggle(c: CallbackQuery):
    pid = c.data.split(":", 3)[3]
    if pid in FAVS:
        FAVS.remove(pid)
        await save_favs()
        await c.answer("Removed from Top 3 Favourites!")
    else:
        if len(FAVS) >= 3:
            # Replace oldest or show alert? Let's inform admin or replace slot 3 if they want, or ask to remove one first
            return await c.answer("⚠️ You already have 3 Favourite products! Remove one first or replace below.", show_alert=True)
        FAVS.append(pid)
        await save_favs()
        await c.answer("⭐ Added to Top 3 Favourites!")
    await render_adm_product(c, pid)


@adm.callback_query(F.data.startswith("adm:fav:rep:"))
async def adm_fav_replace(c: CallbackQuery):
    # adm:fav:rep:<slot_idx>:<pid>
    _, _, _, slot_s, pid = c.data.split(":", 4)
    slot = int(slot_s)
    if pid in FAVS:
        FAVS.remove(pid)
    while len(FAVS) < 3:
        FAVS.append(pid)
    FAVS[slot] = pid
    # Deduplicate preserving order
    dedup = []
    for x in FAVS:
        if x not in dedup:
            dedup.append(x)
    FAVS[:] = dedup[:3]
    await save_favs()
    await c.answer(f"⭐ Set as Favourite #{slot+1}!")
    await render_adm_product(c, pid)


# ---- Folder Options ----
@adm.callback_query(F.data == "adm:fopt")
async def adm_fopt(c: CallbackQuery):
    empty_st = "ON (Show all 24 folders)" if ST.get("show_empty_folders") else "OFF (Only folders with products)"
    pin_st = "All 3 Favourites in Green Folders" if ST.get("pin_all_favs_in_folders", True) else "Only Folder's Own Favourites"
    b = InlineKeyboardBuilder()
    b.row(Btn(text=f"📁 Show Empty Folders: {empty_st}", callback_data="adm:fopt:empty", style="primary"))
    b.row(Btn(text=f"📌 Folder Top Pin: {pin_st}", callback_data="adm:fopt:pin", style="success"))
    b.row(Btn(text="⬅️ Back to Admin Panel", callback_data="adm", style="danger"))
    txt = (
        f"📁 <b>Category Folder Display Settings</b>\n\n"
        f"1️⃣ <b>Show Empty Folders:</b> <code>{empty_st}</code>\n"
        f"2️⃣ <b>Pin Behaviour Inside Green Folders:</b> <code>{pin_st}</code>\n\n"
        f"<i>Tap a button below to toggle:</i>"
    )
    await safe_edit(c, txt, reply_markup=b.as_markup())


@adm.callback_query(F.data == "adm:fopt:empty")
async def adm_fopt_empty(c: CallbackQuery):
    ST["show_empty_folders"] = not ST.get("show_empty_folders", False)
    val = "1" if ST["show_empty_folders"] else "0"
    await pool.execute(
        "INSERT INTO settings(key,value) VALUES('show_empty_folders',$1) ON CONFLICT(key) DO UPDATE SET value=$1", val
    )
    await adm_fopt(c)


@adm.callback_query(F.data == "adm:fopt:pin")
async def adm_fopt_pin(c: CallbackQuery):
    ST["pin_all_favs_in_folders"] = not ST.get("pin_all_favs_in_folders", True)
    val = "1" if ST["pin_all_favs_in_folders"] else "0"
    await pool.execute(
        "INSERT INTO settings(key,value) VALUES('pin_all_favs_in_folders',$1) ON CONFLICT(key) DO UPDATE SET value=$1", val
    )
    await adm_fopt(c)


# ---- Contact Settings ----
@adm.callback_query(F.data == "adm:contact")
async def adm_contact(c: CallbackQuery, state: FSMContext):
    await state.clear()
    num = ST.get("contact_number") or CONTACT_NUMBER or "Not set"
    w = get_whatsapp_url() or "Not set"
    b = InlineKeyboardBuilder()
    b.row(Btn(text="📞 Set / Change Contact Number", callback_data="adm:contact:setnum", style="success"))
    b.row(Btn(text="💬 Set / Change WhatsApp Link", callback_data="adm:contact:setwa", style="primary"))
    b.row(Btn(text="⬅️ Back to Admin Panel", callback_data="adm", style="danger"))
    txt = (
        f"📞 <b>Contact & Support Settings</b>\n\n"
        f"📩 <b>Telegram Admin:</b> @{esc(ADMIN_USERNAME or 'Not set in ENV')}\n"
        f"📞 <b>Contact Number:</b> <code>{esc(num)}</code>\n"
        f"💬 <b>WhatsApp Link:</b> <code>{esc(w)}</code>\n\n"
        f"<i>You can set `CONTACT_NUMBER` and `WHATSAPP_LINK` in Render Environment Variables or update them live below:</i>"
    )
    await safe_edit(c, txt, reply_markup=b.as_markup())


@adm.callback_query(F.data.in_({"adm:contact:setnum", "adm:contact:setwa"}))
async def adm_contact_prompt(c: CallbackQuery, state: FSMContext):
    field = "contact_number" if c.data.endswith("setnum") else "whatsapp_link"
    await state.set_state(Adm.contact)
    await state.update_data(field=field)
    prompt = (
        "📞 Send your <b>Contact Phone Number</b> (e.g. <code>+8801610359086</code>):\n<i>(Send /cancel to abort)</i>"
        if field == "contact_number"
        else "💬 Send your <b>WhatsApp Link</b> (e.g. <code>https://wa.me/8801610359086</code>):\n<i>(Send /cancel to abort)</i>"
    )
    await c.message.answer(prompt, parse_mode="HTML")
    await c.answer()


@adm.message(Adm.contact, F.text)
async def adm_contact_save(m: Message, state: FSMContext):
    field = (await state.get_data())["field"]
    val = m.text.strip()
    ST[field] = val
    await pool.execute(
        "INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2", field, val
    )
    await state.clear()
    await safe_answer(m, f"✅ <b>{field}</b> updated to: <code>{esc(val)}</code>", reply_markup=adm_menu())


# ---- Stats & Orders ----
TODAY = "(created AT TIME ZONE 'Asia/Dhaka')::date = (now() AT TIME ZONE 'Asia/Dhaka')::date"


@adm.callback_query(F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    r = await pool.fetchrow(f"""SELECT
      (SELECT count(*) FROM users) users,
      (SELECT coalesce(sum(balance),0) FROM users) bal,
      (SELECT count(*) FROM orders WHERE status='done') o_all,
      (SELECT coalesce(sum(cost),0) FROM orders WHERE status='done') rev_all,
      (SELECT coalesce(sum(cost-base_cost),0) FROM orders WHERE status='done') pro_all,
      (SELECT count(*) FROM orders WHERE status='done' AND {TODAY}) o_td,
      (SELECT coalesce(sum(cost),0) FROM orders WHERE status='done' AND {TODAY}) rev_td,
      (SELECT coalesce(sum(cost-base_cost),0) FROM orders WHERE status='done' AND {TODAY}) pro_td,
      (SELECT coalesce(sum(amount),0) FROM deposits) dep_all,
      (SELECT coalesce(sum(amount),0) FROM deposits WHERE {TODAY}) dep_td,
      (SELECT count(*) FROM orders WHERE status='failed') failed""")
    top = await pool.fetch(
        "SELECT product, sum(qty) q FROM orders WHERE status='done' GROUP BY product ORDER BY q DESC LIMIT 5"
    )
    t = (
        f"📊 <b>Store Analytics & Statistics</b>\n\n"
        f"👥 <b>Total Users:</b> <code>{r['users']}</code>\n"
        f"💼 <b>Total User Wallet Balances:</b> <code>{q2(r['bal'])} USDT</code>\n\n"
        f"📅 <b>Today (Asia/Dhaka)</b>\n"
        f"🛍 Orders: <code>{r['o_td']}</code> | 💵 Revenue: <code>{q2(r['rev_td'])} USDT</code> | 📈 Profit: <code>{q2(r['pro_td'])} USDT</code>\n"
        f"➕ Deposits Today: <code>{q2(r['dep_td'])} USDT</code>\n\n"
        f"🌐 <b>All-Time Totals</b>\n"
        f"🛍 Orders: <code>{r['o_all']}</code> | 💵 Revenue: <code>{q2(r['rev_all'])} USDT</code> | 📈 Profit: <code>{q2(r['pro_all'])} USDT</code>\n"
        f"➕ Total Deposits: <code>{q2(r['dep_all'])} USDT</code> | ❌ Failed Orders: <code>{r['failed']}</code>\n\n"
        f"🔥 <b>Top Selling Products:</b>\n"
    )
    t += "\n".join(f"• {esc(x['product'][:35])} — <b>{x['q']} sold</b>" for x in top) or "—"
    await safe_edit(c, t, reply_markup=back_adm())


@adm.callback_query(F.data == "adm:orders")
async def adm_orders(c: CallbackQuery):
    rows = await pool.fetch(
        "SELECT id,user_id,product,qty,cost,status,created AT TIME ZONE 'Asia/Dhaka' AS t "
        "FROM orders ORDER BY id DESC LIMIT 12"
    )
    t = "🧾 <b>Last 12 Orders</b>\n\n" + "\n".join(
        f"#{r['id']} {'✅' if r['status']=='done' else '❌'} {r['t']:%d/%m %H:%M} <code>{r['user_id']}</code> "
        f"{esc(r['product'][:22])} ×{r['qty']} — <b>{r['cost']}$</b>"
        for r in rows
    )
    await safe_edit(c, t if rows else "No orders recorded yet.", reply_markup=back_adm())


@adm.callback_query(F.data == "adm:api")
async def adm_api(c: CallbackQuery):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{API_BASE}/balance", headers=H(), timeout=aiohttp.ClientTimeout(total=15)) as r:
                txt = await r.text()
    except Exception as e:
        txt = str(e)
    await safe_edit(c, f"🏦 <b>Upstream Shop API Balance:</b>\n<code>{esc(txt[:500])}</code>", reply_markup=back_adm())


# ---- Pricing Rule ----
@adm.callback_query(F.data == "adm:rule")
async def adm_rule(c: CallbackQuery):
    b = InlineKeyboardBuilder()
    b.row(Btn(text="✏️ Change Pricing Rule", callback_data="adm:rule:set", style="success"))
    b.row(Btn(text="♻️ Reset to Env Default", callback_data="adm:rule:reset", style="primary"))
    b.row(Btn(text="⬅️ Back to Admin Panel", callback_data="adm", style="danger"))
    await safe_edit(
        c,
        f"⚙️ <b>Global Pricing Markup Rule</b>\n\n"
        f"📌 <b>Active Rule:</b> <code>{esc(rule_text(ST['rule']))}</code>\n"
        f"🔧 <b>Env Default:</b> <code>{esc(rule_text(ENV_RULE))}</code>\n\n"
        f"<b>Supported Formats:</b>\n"
        f"• <code>a2.5</code> = Add +2.5 USDT to base price\n"
        f"• <code>p10</code> = Add +10% markup to base price\n"
        f"• <code>m2</code> = Multiply base price × 2\n\n"
        f"ℹ️ <i>Products with a manual price override are not affected by the global rule.</i>",
        reply_markup=b.as_markup(),
    )


@adm.callback_query(F.data == "adm:rule:set")
async def adm_rule_set(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.rule)
    await c.message.answer(
        "✏️ Enter new pricing rule (e.g. <code>a2.5</code>, <code>p10</code>, or <code>m2</code>):\n<i>(Send /cancel to abort)</i>",
        parse_mode="HTML",
    )
    await c.answer()


@adm.message(Adm.rule, F.text)
async def adm_rule_in(m: Message, state: FSMContext):
    r = parse_rule(m.text)
    if not r:
        return await m.answer("⚠️ Invalid format! Use <code>a2.5</code>, <code>p10</code>, or <code>m2</code>.", parse_mode="HTML")
    await pool.execute(
        "INSERT INTO settings(key,value) VALUES('pricing',$1) ON CONFLICT(key) DO UPDATE SET value=$1", m.text.strip()
    )
    ST["rule"] = r
    await state.clear()
    await safe_answer(m, f"✅ <b>Updated Pricing Rule:</b> <code>{esc(rule_text(r))}</code>", reply_markup=adm_menu())


@adm.callback_query(F.data == "adm:rule:reset")
async def adm_rule_reset(c: CallbackQuery):
    await pool.execute("DELETE FROM settings WHERE key='pricing'")
    ST["rule"] = ENV_RULE
    await c.answer("Reset to environment default!")
    await adm_rule(c)


# ---- Product Manager (Prices, Notes, Top 3 Favourites, Category Folder) ----
@adm.callback_query(F.data.startswith("adm:pl:"))
async def adm_pl(c: CallbackQuery):
    await render_pl(c, int(c.data.split(":")[2]))


async def render_pl(c: CallbackQuery, page: int):
    ps = await get_products(True)
    b = InlineKeyboardBuilder()
    for p in ps[page * 10 : (page + 1) * 10]:
        pid = str(p["id"])
        ns = note_for(p)[1]
        is_fav = pid in FAVS[:3]
        mk = (
            ("⭐" if is_fav else "")
            + ("⛔" if not p["ok"] else "")
            + ("✏️" if pid in OVR else "")
            + ("📝" if ns == "admin" else "")
        )
        btn_style = "success" if is_fav else ("danger" if not p["ok"] else "primary")
        b.row(
            Btn(
                text=f"{mk} {p['name'][:25]} | {p['price']}→{sell_price(p)}$",
                callback_data=f"adm:p:{pid}",
                style=btn_style,
            )
        )
    nav = []
    if page > 0:
        nav.append(Btn(text="◀️ Prev", callback_data=f"adm:pl:{page-1}", style="primary"))
    if (page + 1) * 10 < len(ps):
        nav.append(Btn(text="▶️ Next", callback_data=f"adm:pl:{page+1}", style="primary"))
    if nav:
        b.row(*nav)
    b.row(Btn(text="⭐ Manage Top 3 Favourites", callback_data="adm:favs", style="success"))
    b.row(Btn(text="♻️ Reset All Manual Prices", callback_data="adm:prall", style="danger"))
    b.row(Btn(text="⬅️ Back to Admin Panel", callback_data="adm", style="primary"))
    await safe_edit(
        c,
        f"💲 <b>Product Manager (Page {page+1}/{max(1, (len(ps)-1)//10+1)})</b>\n"
        f"⭐ Favourite (Green) | ✏️ Manual Price | 📝 Custom Note | ⛔ Out of Stock (Red)",
        reply_markup=b.as_markup(),
    )


async def render_adm_product(c: CallbackQuery, pid: str):
    p = await find(pid, True)
    if not p:
        return await c.answer("Product not found.", show_alert=True)
    pid = str(p["id"])
    man = OVR.get(pid)
    is_fav = pid in FAVS[:3]
    cid = detect_category(p)
    cat_label, cat_icon, _ = CAT_MAP.get(cid, ("Others", "📦", []))

    b = InlineKeyboardBuilder()
    if is_fav:
        fav_rank = FAVS.index(pid) + 1
        b.row(Btn(text=f"🌟 Remove from Top 3 Favourites (Slot #{fav_rank})", callback_data=f"adm:fav:tg:{pid}", style="danger"))
    else:
        if len(FAVS) < 3:
            b.row(Btn(text=f"⭐ Add to Top 3 Favourites ({len(FAVS)}/3 used)", callback_data=f"adm:fav:tg:{pid}", style="success"))
        else:
            b.row(
                Btn(text="⭐ Replace #1", callback_data=f"adm:fav:rep:0:{pid}", style="success"),
                Btn(text="⭐ Replace #2", callback_data=f"adm:fav:rep:1:{pid}", style="success"),
                Btn(text="⭐ Replace #3", callback_data=f"adm:fav:rep:2:{pid}", style="success"),
            )

    b.row(
        Btn(text="✏️ Set Manual Price", callback_data=f"adm:ps:{pid}", style="primary"),
        Btn(text="♻️ Reset Price (Auto)", callback_data=f"adm:pr:{pid}", style="primary"),
    )
    b.row(
        Btn(text="📝 Set/Edit Custom Note", callback_data=f"adm:ns:{pid}", style="primary"),
        Btn(text="🗑 Reset Note", callback_data=f"adm:nr:{pid}", style="primary"),
    )
    b.row(Btn(text=f"📁 Folder: {cat_icon} {cat_label} (Change)", callback_data=f"adm:cat:{pid}", style="primary"))
    b.row(Btn(text="⬅️ Back to Product List", callback_data="adm:pl:0", style="danger"))

    st = "Unlimited" if p["stock"] is None else p["stock"]
    note, src = note_for(p)
    src_t = {"admin": "Admin Custom Note", "safwan": "Upstream Default", "none": "None"}[src]
    await safe_edit(
        c,
        f"📦 <b>{esc(p['name'])}</b>\n"
        f"🆔 ID: <code>{pid}</code> | 📊 Stock: <code>{st}</code> | {'🟢 In Stock' if p['ok'] else '🔴 Out of Stock'}\n"
        f"📁 Folder: <b>{cat_icon} {esc(cat_label)}</b> {'(Manual)' if pid in CAT_OVR else '(Auto)'}\n"
        f"⭐ Favourite Status: <b>{'YES (Green Highlight)' if is_fav else 'No'}</b>\n\n"
        f"💵 <b>Upstream Base Price:</b> <code>{p['price']} USDT</code>\n"
        f"⚙️ <b>Rule Price:</b> <code>{apply_rule(p['price'], ST['rule'])} USDT</code>\n"
        f"🏷 <b>Active Selling Price:</b> <b>{sell_price(p)} USDT</b> {'(Manual ✏️)' if man is not None else '(Auto)'}\n\n"
        f"📝 <b>Note ({src_t}):</b>\n<blockquote>{esc(note[:500]) if note else '—'}</blockquote>",
        reply_markup=b.as_markup(),
    )


@adm.callback_query(F.data.startswith("adm:p:"))
async def adm_p(c: CallbackQuery):
    pid = c.data.split(":")[2]
    await render_adm_product(c, pid)


@adm.callback_query(F.data.startswith("adm:cat:"))
async def adm_cat_select(c: CallbackQuery):
    pid = c.data.split(":")[2]
    b = InlineKeyboardBuilder()
    btns = []
    for cid, label, icon, _ in CATEGORIES:
        btns.append(Btn(text=f"{icon} {label}", callback_data=f"adm:setcat:{pid}:{cid}", style="primary"))
    for i in range(0, len(btns), 3):
        b.row(*btns[i : i + 3])
    b.row(Btn(text="♻️ Auto-Detect Category", callback_data=f"adm:setcat:{pid}:auto", style="success"))
    b.row(Btn(text="⬅️ Back to Product", callback_data=f"adm:p:{pid}", style="danger"))
    await safe_edit(c, f"📁 <b>Select Category Folder for Product <code>{esc(pid)}</code>:</b>", reply_markup=b.as_markup())


@adm.callback_query(F.data.startswith("adm:setcat:"))
async def adm_cat_save(c: CallbackQuery):
    _, _, pid, cid = c.data.split(":", 3)
    if cid == "auto":
        await pool.execute("DELETE FROM product_categories WHERE pid=$1", pid)
        CAT_OVR.pop(pid, None)
        await c.answer("Reset to Auto-Detect folder!")
    else:
        await pool.execute(
            "INSERT INTO product_categories(pid,cat_id) VALUES($1,$2) ON CONFLICT(pid) DO UPDATE SET cat_id=$2",
            pid, cid,
        )
        CAT_OVR[pid] = cid
        await c.answer(f"Moved to {cid} folder!")
    await render_adm_product(c, pid)


@adm.callback_query(F.data.startswith("adm:ps:"))
async def adm_ps(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.price)
    await state.update_data(pid=c.data.split(":")[2])
    await c.message.answer(
        "✏️ Enter new selling price in <b>USDT</b> (e.g. <code>3.50</code>):\n<i>(Send /cancel to abort)</i>",
        parse_mode="HTML",
    )
    await c.answer()


@adm.message(Adm.price, F.text)
async def adm_price_in(m: Message, state: FSMContext):
    try:
        v = q2(D(m.text.strip()))
        assert v > 0
    except Exception:
        return await m.answer("⚠️ Please enter a valid positive number, e.g. <code>3.5</code>", parse_mode="HTML")
    pid = (await state.get_data())["pid"]
    await pool.execute(
        "INSERT INTO overrides(pid,price) VALUES($1,$2) ON CONFLICT(pid) DO UPDATE SET price=$2", pid, v
    )
    OVR[pid] = v
    await state.clear()
    p = await find(pid)
    warn = f"\n⚠️ <b>Warning:</b> Below upstream base cost ({p['price']} USDT)!" if p and v < p["price"] else ""
    await safe_answer(m, f"✅ <b>Selling price set to:</b> <code>{v} USDT</code>{warn}", reply_markup=adm_menu())


@adm.callback_query(F.data.startswith("adm:pr:"))
async def adm_pr(c: CallbackQuery):
    pid = c.data.split(":")[2]
    await pool.execute("DELETE FROM overrides WHERE pid=$1", pid)
    OVR.pop(pid, None)
    await c.answer("Reset to rule-based price!")
    await render_adm_product(c, pid)


@adm.callback_query(F.data == "adm:prall")
async def adm_prall(c: CallbackQuery):
    await pool.execute("DELETE FROM overrides")
    OVR.clear()
    await c.answer("All manual prices reset!")
    await render_pl(c, 0)


@adm.callback_query(F.data.startswith("adm:ns:"))
async def adm_ns(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.note)
    await state.update_data(pid=c.data.split(":")[2])
    await c.message.answer(
        "📝 Send the custom note for this product (customers will see this before purchasing, max 1500 chars):\n<i>(Send /cancel to abort)</i>",
        parse_mode="HTML",
    )
    await c.answer()


@adm.message(Adm.note, F.text)
async def adm_note_in(m: Message, state: FSMContext):
    txt = m.text.strip()
    if not txt or len(txt) > 1500:
        return await m.answer("⚠️ Note must be between 1 and 1500 characters.")
    pid = (await state.get_data())["pid"]
    await pool.execute(
        "INSERT INTO notes(pid,note) VALUES($1,$2) ON CONFLICT(pid) DO UPDATE SET note=$2", pid, txt
    )
    NOTES[pid] = txt
    await state.clear()
    await safe_answer(m, "✅ <b>Custom note saved!</b>", reply_markup=adm_menu())


@adm.callback_query(F.data.startswith("adm:nr:"))
async def adm_nr(c: CallbackQuery):
    pid = c.data.split(":")[2]
    await pool.execute("DELETE FROM notes WHERE pid=$1", pid)
    NOTES.pop(pid, None)
    await c.answer("Custom note removed!")
    await render_adm_product(c, pid)


@adm.message(Command("addbal"))
async def addbal(m: Message):
    try:
        _, uid, amt = m.text.split()
        nb = await credit(int(uid), D(amt))
        await m.answer(f"✅ Balance updated! New balance for <code>{uid}</code>: <b>{nb} USDT</b>", parse_mode="HTML")
        if D(amt) > 0:
            await bot.send_message(
                int(uid),
                f"✅ <b>Wallet Credited!</b>\n➕ <code>{amt} USDT</code> has been added to your wallet by Admin.",
                parse_mode="HTML",
            )
    except Exception:
        await m.answer("Usage: <code>/addbal USER_ID AMOUNT</code> (use negative amount to deduct)", parse_mode="HTML")


dp.include_router(adm)


# ================= SLASH COMMAND MENU REGISTRATION =================
async def setup_bot_commands():
    """Registers the `/` command menu popup matching Screenshot 2."""
    user_cmds = [
        BotCommand(command="start", description="Open the main menu"),
        BotCommand(command="products", description="Browse products"),
        BotCommand(command="deposit", description="Add funds to your wallet"),
        BotCommand(command="orders", description="View your order history"),
        BotCommand(command="settings", description="Your profile & settings"),
        BotCommand(command="support", description="Get help"),
        BotCommand(command="api", description="Reseller API access"),
    ]
    try:
        await bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        admin_cmds = user_cmds + [
            BotCommand(command="admin", description="Open Admin Control Panel"),
            BotCommand(command="addbal", description="Add/deduct user balance"),
        ]
        for aid in ADMINS:
            try:
                await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=aid))
            except Exception:
                pass
    except Exception as e:
        logging.warning("Could not set bot commands: %s", e)


# ================= WEB HEALTHCHECK (Render / UptimeRobot) =================
async def health(_):
    return web.Response(text="ok")


async def main():
    await init_db()
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(E("PORT", "10000"))).start()
    await bot.delete_webhook(drop_pending_updates=True)
    await setup_bot_commands()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
