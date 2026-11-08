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
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
    BotCommandScopeAllPrivateChats,
    MenuButtonCommands,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
E = os.environ.get
D = lambda x: Decimal(str(x))
q2 = lambda x: D(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
esc = lambda t: htmllib.escape(str(t))

# Environment configuration
BOT_TOKEN = (E("BOT_TOKEN") or "").strip()
ADMIN_BOT_TOKEN = (E("ADMIN_BOT_TOKEN") or "").strip()
LOG_BOT_TOKEN = (E("LOG_BOT_TOKEN") or "").strip()

DATABASE_URL = (E("DATABASE_URL") or "").strip().strip("\"'").split("?")[0]
DB_HOST = (E("DB_HOST") or "").strip()
DB_PORT = int(E("DB_PORT", "5432"))
DB_USER = (E("DB_USER") or "").strip()
DB_PASSWORD = E("DB_PASSWORD") or ""
DB_NAME = (E("DB_NAME") or "postgres").strip()

API_BASE = E("SHOP_API_BASE", "https://safwantigershopbot-production.up.railway.app/api").rstrip("/")
API_KEY = (E("SHOP_KEY") or "").strip()
SHOP_NAME = (E("SHOP_NAME") or "SubStore").strip()

ADMINS = [int(x) for x in re.findall(r"\d+", E("ADMIN_IDS", ""))]
ADMIN_USERNAME = (E("ADMIN_USERNAME") or "").strip().lstrip("@")
WHATSAPP_LINK = (E("WHATSAPP_LINK") or "").strip()
CONTACT_NUMBER = (E("CONTACT_NUMBER") or E("PHONE_NUMBER") or "").strip()

PRICING_ENV = E("PRICING", "a2.5")
BINANCE_KEY = (E("BINANCE_API_KEY") or "").strip()
BINANCE_SECRET = (E("BINANCE_API_SECRET") or "").strip()
BINANCE_PAY_ID = (E("BINANCE_PAY_ID") or "").strip()

BYBIT_KEY = (E("BYBIT_API_KEY") or "").strip()
BYBIT_SECRET = (E("BYBIT_API_SECRET") or "").strip()
BYBIT_UID = (E("BYBIT_UID") or "").strip()

BKASH_NUMBER = (E("BKASH_NUMBER") or "").strip()
BDT_RATE = D(E("BDT_RATE", "125"))
MIN_DEPOSIT = D(E("MIN_DEPOSIT", "1"))

CUSTOM_EMOJI_JSON = (E("CUSTOM_EMOJI_MAP") or "").strip()
try:
    CUSTOM_EMOJI_MAP = json.loads(CUSTOM_EMOJI_JSON) if CUSTOM_EMOJI_JSON else {}
except Exception:
    CUSTOM_EMOJI_MAP = {}

# Bot instances
bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
admin_bot = Bot(ADMIN_BOT_TOKEN) if ADMIN_BOT_TOKEN else bot
log_bot = Bot(LOG_BOT_TOKEN) if LOG_BOT_TOKEN else admin_bot

dp = Dispatcher()
adm_dp = Dispatcher()
log_dp = Dispatcher()

adm = Router()


@adm.message.outer_middleware()
async def adm_msg_auth(handler, event: Message, data: dict):
    if not event.from_user or event.from_user.id not in ADMINS:
        uid = event.from_user.id if event.from_user else "Unknown"
        return await event.answer(f"⛔ Access denied. Your ID (<code>{uid}</code>) is not in ADMIN_IDS.", parse_mode="HTML")
    return await handler(event, data)


@adm.callback_query.outer_middleware()
async def adm_cb_auth(handler, event: CallbackQuery, data: dict):
    if not event.from_user or event.from_user.id not in ADMINS:
        return await event.answer("⛔ Access denied.", show_alert=True)
    return await handler(event, data)

pool: asyncpg.Pool = None
USER_LOCKS: dict[int, asyncio.Lock] = {}


def get_lock(uid: int) -> asyncio.Lock:
    if uid not in USER_LOCKS:
        USER_LOCKS[uid] = asyncio.Lock()
    return USER_LOCKS[uid]


# Conversation states
class Dep(StatesGroup):
    txid = State()
    bk_amt = State()
    bk_trx = State()


class DirectPay(StatesGroup):
    txid = State()


class Qty(StatesGroup):
    n = State()


class SearchSt(StatesGroup):
    q = State()


class Adm(StatesGroup):
    price = State()
    rule = State()
    note = State()
    contact = State()
    bal_uid = State()
    bal_amt = State()
    man_amt = State()
    up_uid = State()
    up_rule = State()


# Button builders and safe message helpers
def Btn(text: str, style: str = None, icon_custom_emoji_id: str = None, **kwargs) -> InlineKeyboardButton:
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


def KBtn(text: str, style: str = None) -> KeyboardButton:
    payload = {"text": text}
    if style in ("success", "primary", "danger"):
        payload["style"] = style
    try:
        return KeyboardButton(**payload)
    except TypeError:
        return KeyboardButton(text=text)


def strip_button_extras(markup, strip_style=False, strip_emoji=True):
    if isinstance(markup, ReplyKeyboardMarkup):
        new_kb = []
        for row in markup.keyboard:
            new_kb.append([KeyboardButton(text=b.text) for b in row])
        return ReplyKeyboardMarkup(keyboard=new_kb, resize_keyboard=True, is_persistent=True)
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


async def safe_bot_send(target_bot: Bot, chat_id: int | str, text: str, reply_markup=None, parse_mode="HTML", **kwargs):
    if not target_bot:
        return None
    try:
        return await target_bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    except Exception as e:
        err = str(e).upper()
        if "CUSTOM_EMOJI" in err or "PREMIUM" in err:
            clean_kb = strip_button_extras(reply_markup, strip_style=False, strip_emoji=True)
            try:
                return await target_bot.send_message(chat_id, text, reply_markup=clean_kb, parse_mode=parse_mode, **kwargs)
            except Exception:
                pass
        if "STYLE" in err or "BUTTON" in err:
            clean_kb = strip_button_extras(reply_markup, strip_style=True, strip_emoji=True)
            try:
                return await target_bot.send_message(chat_id, text, reply_markup=clean_kb, parse_mode=parse_mode, **kwargs)
            except Exception:
                pass
        logging.warning("send_message failed to %s: %s", chat_id, e)
        return None


async def safe_edit(c_or_msg, text: str, reply_markup=None, parse_mode="HTML", **kwargs):
    target = c_or_msg.message if isinstance(c_or_msg, CallbackQuery) else c_or_msg
    try:
        return await target.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    except Exception as e:
        err = str(e)
        if "message is not modified" in err.lower():
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
        err = str(e).upper()
        if "CUSTOM_EMOJI" in err or "PREMIUM" in err:
            clean_kb = strip_button_extras(reply_markup, strip_style=False, strip_emoji=True)
            return await m.answer(text, reply_markup=clean_kb, parse_mode=parse_mode, **kwargs)
        clean_kb = strip_button_extras(reply_markup, strip_style=True, strip_emoji=True)
        return await m.answer(text, reply_markup=clean_kb, parse_mode=parse_mode, **kwargs)


# Product categories and brand icons
CATEGORIES = [
    ("chatgpt", "ChatGPT", "🤖", ["chatgpt", "openai", "gpt-4", "gpt4", "gpt plus", "gpt pro", "gpt team", "gpt business"]),
    ("gemini", "Gemini", "✨", ["gemini", "google ai", "google one", "notebooklm", "google pro"]),
    ("grok", "Grok", "✖️", ["grok", "xai", "supergrok"]),
    ("codex", "Codex API", "🟢", ["codex"]),
    ("claude", "Claude", "✴️", ["claude", "anthropic"]),
    ("kimi", "Kimi", "🖤", ["kimi", "moonshot"]),
    ("video_ai", "Video AI", "▶️", ["heygen", "runway", "sora", "kling", "pika", "luma", "synthesia", "invideo", "veo", "hailuo", "video ai", "fliki"]),
    ("elevenlabs", "ElevenLabs", "⏸", ["elevenlabs", "eleven labs", "murf", "voice ai", "speechify"]),
    ("ai_tools", "AI Tools", "🧠", ["replit", "perplexity", "midjourney", "lovable", "bolt", "v0", "gamma", "quillbot", "grammarly", "blackbox", "poe", "copilot", "notion", "jasper", "writesonic", "turnitin", "suno", "udio", "muse ai"]),
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

# Pricing and runtime state
RULE_RE = re.compile(r"^\s*([apm])\s*([+-]?\d+(?:\.\d+)?)\s*$", re.I)


def parse_rule(s):
    m = RULE_RE.match(s or "")
    if not m:
        return None
    k, v = m.group(1).lower(), D(m.group(2))
    if k == "m" and v <= 0:
        return None
    return (k, v)


def apply_rule(base, rule):
    k, v = rule
    r = base + v if k == "a" else base * (1 + v / 100) if k == "p" else base * v
    return max(q2(r), Decimal("0.01"))


def rule_text(rule):
    k, v = rule
    sign = "+" if v >= 0 else ""
    return {
        "a": f"a{v} (Price {sign}{v} USDT)",
        "p": f"p{v} (Price {sign}{v}%)",
        "m": f"m{v} (Price × {v})",
    }[k]


def user_rule_change_info(rule) -> tuple[str, str]:
    """Returns (direction, formatted_amount) where direction is 'discount', 'increase', or 'neutral'."""
    k, v = rule
    if k == "a":
        if v < 0:
            return "discount", f"{abs(v)} USDT"
        if v > 0:
            return "increase", f"{v} USDT"
        return "neutral", "0 USDT"
    if k == "p":
        if v < 0:
            return "discount", f"{abs(v)}%"
        if v > 0:
            return "increase", f"{v}%"
        return "neutral", "0%"
    if k == "m":
        if v < 1:
            pct = q2((Decimal("1") - v) * 100)
            return "discount", f"{pct}% (×{v})"
        if v > 1:
            pct = q2((v - Decimal("1")) * 100)
            return "increase", f"{pct}% (×{v})"
        return "neutral", "×1"
    return "neutral", ""


ENV_RULE = parse_rule(PRICING_ENV) or ("a", D("2.5"))
ST = {
    "rule": ENV_RULE,
    "contact_number": CONTACT_NUMBER,
    "whatsapp_link": WHATSAPP_LINK,
    "show_empty_folders": False,
}
OVR = {}
NOTES = {}
FAVS = []
CAT_OVR = {}
USER_RULES: dict[int, tuple[str, Decimal]] = {}


def detect_category(p: dict) -> str:
    pid = str(p["id"])
    if pid in CAT_OVR and CAT_OVR[pid] in CAT_MAP:
        return CAT_OVR[pid]
    name_low = p.get("name", "").lower()
    best_cid = None
    best_pos = 10**9
    for cid, _, _, kws in CATEGORIES:
        if cid == "other":
            continue
        for kw in kws:
            idx = name_low.find(kw)
            if idx != -1 and idx < best_pos:
                best_pos = idx
                best_cid = cid
    if best_cid:
        return best_cid
    raw_cat = p.get("raw_cat", "").lower()
    if raw_cat:
        for cid, _, _, kws in CATEGORIES:
            if cid != "other" and any(kw in raw_cat for kw in kws):
                return cid
    return "other"


def detect_product_icon(p: dict) -> tuple[str, str | None]:
    name_low = p.get("name", "").lower()
    ce_id = p.get("ce_id")
    best_icon = None
    best_kw = None
    best_pos = 10**9
    for kw, em in PRODUCT_BRAND_ICONS:
        idx = name_low.find(kw)
        if idx != -1 and idx < best_pos:
            best_pos = idx
            best_icon = em
            best_kw = kw
    if best_icon:
        if not ce_id and best_kw in CUSTOM_EMOJI_MAP:
            ce_id = str(CUSTOM_EMOJI_MAP[best_kw])
        return best_icon, ce_id
    cid = detect_category(p)
    _, cat_icon, _ = CAT_MAP.get(cid, ("Others", "📦", []))
    if not ce_id and cid in CUSTOM_EMOJI_MAP:
        ce_id = str(CUSTOM_EMOJI_MAP[cid])
    return cat_icon, ce_id


# Contact helpers (only shown in /support and failed manual verification window)
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


def add_contact_rows(b: InlineKeyboardBuilder, ctx=""):
    r = contact_btns(ctx)
    if r:
        if len(r) <= 2:
            b.row(*r)
        else:
            b.row(r[0], r[1])
            b.row(r[2])


def note_for(p):
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


def sell_price(p, uid: int = None):
    o = OVR.get(str(p["id"]))
    std_price = o if o is not None else apply_rule(p["price"], ST["rule"])
    if uid is not None and int(uid) in USER_RULES:
        return apply_rule(std_price, USER_RULES[int(uid)])
    return std_price


def format_product_btn_text(p: dict, max_name_len: int = 34, uid: int = None) -> str:
    icon, _ = detect_product_icon(p)
    name = p["name"]
    if len(name) > max_name_len:
        name = name[: max_name_len - 1].rstrip() + "…"
    sp = sell_price(p, uid)
    price_str = f"{sp:f}".rstrip("0").rstrip(".") if "." in f"{sp:f}" else f"{sp}"
    stk_str = "∞" if p["stock"] is None else (str(p["stock"]) if p["ok"] else "0")
    return f"{icon} {name} - {price_str}USDT (Stock: {stk_str})"


def make_product_btn(p: dict, is_fav: bool = False, origin: str = "all-0", uid: int = None) -> InlineKeyboardButton:
    _, ce_id = detect_product_icon(p)
    if not p["ok"] or (p["stock"] is not None and p["stock"] <= 0):
        style = "danger"
    elif is_fav:
        style = "success"
    else:
        style = "primary"
    return Btn(
        text=format_product_btn_text(p, uid=uid),
        callback_data=f"p:{p['id']}:1:{origin}",
        style=style,
        icon_custom_emoji_id=ce_id,
    )


# Database initialization and helpers
async def init_db():
    global pool, FAVS
    opts = dict(ssl="require", statement_cache_size=0, min_size=1, max_size=5)
    if DB_HOST:
        kw = dict(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, database=DB_NAME)
        pool = await asyncpg.create_pool(**kw, **opts)
    else:
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
        order_code TEXT,
        user_id BIGINT,
        pid TEXT,
        product TEXT,
        qty INT,
        cost NUMERIC(18,2),
        base_cost NUMERIC(18,2),
        request_id TEXT,
        pay_method TEXT DEFAULT 'wallet',
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
        usdt_amount NUMERIC(18,2),
        amount_bdt NUMERIC(18,2),
        status TEXT DEFAULT 'pending',
        created TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS manual_verifications(
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        username TEXT,
        full_name TEXT,
        kind TEXT NOT NULL,
        method TEXT NOT NULL,
        txid TEXT NOT NULL,
        pid TEXT,
        qty INT DEFAULT 1,
        expected_usdt NUMERIC(18,2) DEFAULT 0,
        attempts INT DEFAULT 0,
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
    CREATE TABLE IF NOT EXISTS user_pricing(
        user_id BIGINT PRIMARY KEY,
        rule TEXT NOT NULL,
        updated TIMESTAMPTZ DEFAULT now()
    );
    """)

    await pool.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_code TEXT;")
    await pool.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS pay_method TEXT DEFAULT 'wallet';")
    await pool.execute("ALTER TABLE bkash ADD COLUMN IF NOT EXISTS usdt_amount NUMERIC(18,2);")

    for t in (
        "users", "deposits", "orders", "overrides", "settings",
        "bkash", "notes", "favorites", "product_categories", "manual_verifications", "user_pricing"
    ):
        try:
            await pool.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        except Exception:
            pass

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

    USER_RULES.clear()
    for r in await pool.fetch("SELECT user_id, rule FROM user_pricing"):
        pr = parse_rule(r["rule"])
        if pr:
            USER_RULES[int(r["user_id"])] = pr

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


async def bd_now() -> str:
    return await pool.fetchval("SELECT to_char(now() AT TIME ZONE 'Asia/Dhaka', 'YYYY-MM-DD HH12:MI:SS AM')")


async def notify_admin(text: str, reply_markup=None):
    for a in ADMINS:
        await safe_bot_send(admin_bot, a, text, reply_markup=reply_markup)


async def send_log(text: str):
    """Sends all activity logs directly to ADMIN_IDS via the dedicated Log Bot."""
    for a in ADMINS:
        await safe_bot_send(log_bot, a, text)


async def save_favs():
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("DELETE FROM favorites")
            for idx, pid in enumerate(FAVS[:3], start=1):
                await c.execute("INSERT INTO favorites(pid, rank) VALUES($1, $2)", str(pid), idx)


async def ensure_user(u):
    is_new = await pool.fetchval(
        "INSERT INTO users(id,username,name) VALUES($1,$2,$3) ON CONFLICT(id) DO NOTHING RETURNING id",
        u.id, u.username, u.full_name,
    )
    if is_new:
        t_str = await bd_now()
        await send_log(
            f"👤 <b>NEW USER REGISTERED</b>\n\n"
            f"🏷 <b>Name:</b> {esc(u.full_name)}\n"
            f"🔗 <b>Username:</b> @{esc(u.username or 'N/A')}\n"
            f"🆔 <b>User ID:</b> <code>{u.id}</code>\n"
            f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
        )
    else:
        await pool.execute("UPDATE users SET username=$2, name=$3 WHERE id=$1", u.id, u.username, u.full_name)


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


async def is_tx_used(method: str, txid: str) -> bool:
    row = await pool.fetchval("SELECT id FROM deposits WHERE method=$1 AND txid=$2", method, txid)
    return bool(row)


async def mark_tx_used(method: str, txid: str, uid: int, amt: Decimal) -> bool:
    async with pool.acquire() as c:
        async with c.transaction():
            ins = await c.fetchval(
                "INSERT INTO deposits(method,txid,user_id,amount) VALUES($1,$2,$3,$4) "
                "ON CONFLICT DO NOTHING RETURNING id",
                method, txid, uid, amt,
            )
            return bool(ins)


async def credit_deposit(method, txid, uid, amt):
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


# Upstream Shop API
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
    if not force and time.time() - _cache["t"] < 30 and _cache["d"]:
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
    send_pid = int(pid) if str(pid).isdigit() else pid
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{API_BASE}/order",
                headers=H(),
                json={"product_id": send_pid, "quantity": qty, "request_id": rid},
                timeout=aiohttp.ClientTimeout(total=55),
            ) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = {"error": await r.text()}
                if r.status == 200 and isinstance(data, dict) and not data.get("error") and data.get("ok", data.get("success", True)) is not False:
                    return "ok", data
                if r.status == 200 and isinstance(data, list):
                    return "ok", {"items": data}
                if r.status in (502, 503, 504):
                    return "timeout", data
                return "failed", data
    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        logging.warning("api_order timeout/network error: %s", e)
        return "timeout", {"error": str(e)}
    except Exception as e:
        logging.exception(e)
        return "timeout", {"error": str(e)}


# Exchange API verification (Binance & Bybit)
async def verify_binance(txid: str):
    if not BINANCE_KEY or not BINANCE_SECRET:
        return None
    txid_clean = txid.strip()
    async with aiohttp.ClientSession() as s:
        try:
            now = int(time.time() * 1000)
            params = {"limit": 100, "timestamp": now, "recvWindow": 20000}
            qs = urlencode(params)
            sig = hmac.new(BINANCE_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
            async with s.get(
                f"https://api.binance.com/sapi/v1/pay/transactions?{qs}&signature={sig}",
                headers={"X-MBX-APIKEY": BINANCE_KEY},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json()
            for t in data.get("data") or []:
                ids = {
                    str(t.get("orderId", "")).strip(),
                    str(t.get("transactionId", "")).strip(),
                    str(t.get("prepayId", "")).strip(),
                }
                amt = D(t.get("amount", 0))
                if txid_clean in ids and str(t.get("currency", "")).upper() == "USDT" and amt > 0:
                    return q2(amt)
        except Exception as e:
            logging.warning("binance pay check error: %s", e)

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
            if isinstance(dep_data, list):
                for d_row in dep_data:
                    d_ids = {str(d_row.get("txId", "")).strip(), str(d_row.get("id", "")).strip()}
                    if txid_clean in d_ids and str(d_row.get("coin", "")).upper() == "USDT" and int(d_row.get("status", 0)) == 1:
                        return q2(d_row["amount"])
        except Exception as e:
            logging.warning("binance onchain check error: %s", e)
    return None


async def verify_bybit(txid: str):
    if not BYBIT_KEY or not BYBIT_SECRET:
        return None
    txid_clean = txid.strip()
    now = int(time.time() * 1000)
    rw = "20000"

    async with aiohttp.ClientSession() as s:
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


async def run_two_stage_verification(msg: Message, method: str, txid: str):
    bars_30 = [
        ("▰▱▱▱▱▱", "15%", "25s"),
        ("▰▰▱▱▱▱", "35%", "20s"),
        ("▰▰▰▱▱▱", "50%", "15s"),
        ("▰▰▰▰▱▱", "70%", "10s"),
        ("▰▰▰▰▰▱", "85%", "5s"),
        ("▰▰▰▰▰▰", "100%", "0s"),
    ]
    for bar, pct, rem in bars_30:
        await safe_edit(
            msg,
            f"🛰 <b>Stage 1/2: Synchronizing with {method.capitalize()} Ledger</b>\n\n"
            f"<code>{bar} {pct}</code>  •  ⏳ <i>{rem} remaining</i>\n"
            f"🧾 <b>TxID:</b> <code>{esc(txid)}</code>",
        )
        await asyncio.sleep(5)

    try:
        amt = await (verify_binance(txid) if method == "binance" else verify_bybit(txid))
    except Exception as e:
        logging.warning("verification api call failed: %s", e)
        amt = None

    bars_10 = [
        ("▰▰▰▱▱▱", "50%", "10s"),
        ("▰▰▰▰▰▰", "100%", "5s"),
    ]
    for bar, pct, rem in bars_10:
        await safe_edit(
            msg,
            f"🔐 <b>Stage 2/2: Verifying Transaction Signature</b>\n\n"
            f"<code>{bar} {pct}</code>  •  ⏳ <i>Confirming receipt...</i>\n"
            f"🧾 <b>TxID:</b> <code>{esc(txid)}</code>",
        )
        await asyncio.sleep(5)

    return amt


# Order fulfillment helper
async def execute_product_order(uid: int, u_name: str, u_username: str, p: dict, qty: int, total: Decimal, pay_method: str, target_msg=None):
    base = p["price"] * qty
    rid = f"u{uid}-{uuid.uuid4().hex[:10]}"
    order_code = f"ORD-{int(time.time()) % 1000000:06d}-{uuid.uuid4().hex[:4].upper()}"
    t_str = await bd_now()

    outcome, data = await api_order(p["id"], qty, rid)
    if not isinstance(data, dict):
        data = {"items": data}

    ins = (
        "INSERT INTO orders(order_code,user_id,pid,product,qty,cost,base_cost,request_id,pay_method,status,delivery) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id"
    )

    if outcome == "timeout":
        oid = await pool.fetchval(ins, order_code, uid, str(p["id"]), p["name"], qty, total, base, rid, pay_method, "pending", str(data)[:500])
        ab = InlineKeyboardBuilder()
        ab.row(
            Btn(text="✅ Mark Delivered", callback_data=f"adm:ord:ok:{oid}", style="success"),
            Btn(text="💸 Refund User", callback_data=f"adm:ord:ref:{oid}", style="danger"),
        )
        await notify_admin(
            f"⚠️ <b>Order Timeout — Held as Pending (#{oid})</b>\n"
            f"🆔 Order ID: <code>{order_code}</code>\n"
            f"👤 User: {esc(u_name)} (@{esc(u_username or 'N/A')}) <code>{uid}</code>\n"
            f"📦 Product: <b>{esc(p['name'])} ×{qty}</b>\n"
            f"💵 Paid: <b>{total} USDT</b> ({pay_method.upper()})\n\n"
            f"<i>Upstream API timed out. Check if the order succeeded upstream before refunding.</i>",
            reply_markup=ab.as_markup(),
        )
        await send_log(
            f"⚠️ <b>ORDER PENDING (API TIMEOUT)</b>\n\n"
            f"🆔 <b>Order ID:</b> <code>{order_code}</code> (#{oid})\n"
            f"👤 <b>User:</b> {esc(u_name)} (@{esc(u_username or 'N/A')}) <code>{uid}</code>\n"
            f"📦 <b>Item:</b> {esc(p['name'])} ×{qty}\n"
            f"💵 <b>Paid:</b> <code>{total} USDT</code> ({pay_method.upper()})\n"
            f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
        )
        txt = (
            f"⏳ <b>Order Processing (#{order_code})</b>\n\n"
            f"Your payment of <code>{total} USDT</code> was received, but the delivery server took too long to respond.\n"
            f"Our admin has been notified to complete or refund your order shortly."
        )
        b = InlineKeyboardBuilder()
        b.row(Btn(text="🛍 Back to Shop", callback_data="shop:folders", style="primary"))
        if target_msg:
            await safe_edit(target_msg, txt, reply_markup=b.as_markup())
        else:
            await safe_bot_send(bot, uid, txt, reply_markup=b.as_markup())
        return False

    if outcome == "failed":
        await credit(uid, total)
        oid = await pool.fetchval(ins, order_code, uid, str(p["id"]), p["name"], qty, total, base, rid, pay_method, "failed", str(data)[:500])
        await notify_admin(
            f"❌ <b>Order Failed & Refunded (#{oid} / {order_code})</b>\n"
            f"👤 {esc(u_name)} (<code>{uid}</code>)\n"
            f"📦 {esc(p['name'])} ×{qty}\n"
            f"Reason: <code>{esc(str(data)[:300])}</code>"
        )
        await send_log(
            f"❌ <b>ORDER FAILED (AUTO-REFUNDED)</b>\n\n"
            f"🆔 <b>Order ID:</b> <code>{order_code}</code> (#{oid})\n"
            f"👤 <b>User:</b> {esc(u_name)} (@{esc(u_username or 'N/A')}) <code>{uid}</code>\n"
            f"📦 <b>Item:</b> {esc(p['name'])} ×{qty}\n"
            f"💵 <b>Refunded:</b> <code>{total} USDT</code>\n"
            f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
        )
        txt = (
            f"❌ <b>Order Failed</b>\n"
            f"<code>{total} USDT</code> has been credited to your wallet balance immediately."
        )
        b = InlineKeyboardBuilder()
        b.row(Btn(text="🛍 Back to Shop", callback_data="shop:folders", style="primary"))
        if target_msg:
            await safe_edit(target_msg, txt, reply_markup=b.as_markup())
        else:
            await safe_bot_send(bot, uid, txt, reply_markup=b.as_markup())
        return False

    items = data.get("items") or data.get("delivered") or data.get("data") or data
    if isinstance(items, list):
        items = "\n".join(i if isinstance(i, str) else json.dumps(i, ensure_ascii=False) for i in items)
    elif not isinstance(items, str):
        items = json.dumps(items, ensure_ascii=False, indent=1)

    oid = await pool.fetchval(ins, order_code, uid, str(p["id"]), p["name"], qty, total, base, rid, pay_method, "done", items)
    _cache["t"] = 0
    bal = await balance(uid)

    b = InlineKeyboardBuilder()
    b.row(
        Btn(text="🛍 Shop", callback_data="shop:folders", style="success"),
        Btn(text="📜 My Orders", callback_data="myorders", style="primary"),
    )
    txt = (
        f"✅ <b>Order Completed!</b>\n\n"
        f"🆔 <b>Order ID:</b> <code>{order_code}</code>\n"
        f"📦 <b>Product:</b> {esc(p['name'])} × {qty}\n"
        f"💵 <b>Paid:</b> <code>{total} USDT</code> ({pay_method.upper()})\n\n"
        f"🔑 <b>Delivered Credentials:</b>\n"
        f"<code>{esc(items[:3500])}</code>\n\n"
        f"💰 <b>Wallet Balance:</b> <code>{bal} USDT</code>"
    )
    if target_msg:
        await safe_edit(target_msg, txt, reply_markup=b.as_markup())
    else:
        await safe_bot_send(bot, uid, txt, reply_markup=b.as_markup())

    await send_log(
        f"🛍 <b>PURCHASE SUCCESSFUL</b>\n\n"
        f"🆔 <b>Order ID:</b> <code>{order_code}</code> (#{oid})\n"
        f"👤 <b>Customer:</b> {esc(u_name)} (@{esc(u_username or 'N/A')})\n"
        f"🔢 <b>User ID:</b> <code>{uid}</code>\n"
        f"📦 <b>Item:</b> {esc(p['name'])}\n"
        f"🔢 <b>Quantity:</b> <code>{qty}</code>\n"
        f"💳 <b>Method:</b> <code>{pay_method.upper()}</code>\n"
        f"💵 <b>Total Paid:</b> <code>{total} USDT</code>\n"
        f"📈 <b>Net Profit:</b> <code>{q2(total - base)} USDT</code>\n"
        f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
    )
    return True


# Customer UI menus
def bottom_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KBtn(text="🛍 Shop", style="success"), KBtn(text="💳 Top Up", style="success")],
            [KBtn(text="👤 Profile", style="primary"), KBtn(text="💬 Support", style="primary")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(Btn(text="🛍 Browse Products", callback_data="shop:folders", style="success"))
    b.row(
        Btn(text="💳 Deposit", callback_data="deposit", style="primary"),
        Btn(text="💰 Wallet", callback_data="wallet", style="primary"),
    )
    b.row(
        Btn(text="📜 My Orders", callback_data="myorders", style="primary"),
        Btn(text="👤 Profile", callback_data="profile", style="primary"),
    )
    b.row(Btn(text="🔍 Search", callback_data="search", style="primary"))
    return b.as_markup()


async def welcome_text(user) -> str:
    bal = await balance(user.id)
    orders_cnt = await pool.fetchval("SELECT count(*) FROM orders WHERE user_id=$1 AND status='done'", user.id) or 0
    return (
        f"✨ <b>Welcome to {esc(SHOP_NAME)}</b>\n\n"
        f"👤 <b>User:</b> {esc(user.full_name)} (<code>{user.id}</code>)\n"
        f"💰 <b>Balance:</b> <code>{bal} USDT</code>\n"
        f"📦 <b>Orders:</b> <code>{orders_cnt}</code>"
    )


# Customer Bot commands (StateFilter("*") ensures commands work from any state)
@dp.message(CommandStart(), StateFilter("*"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt = await welcome_text(m.from_user)
    await safe_answer(m, "⚡", reply_markup=bottom_reply_kb())
    await safe_answer(m, txt, reply_markup=main_menu())


@dp.message(Command("products"), StateFilter("*"))
async def cmd_products(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt, kb = await build_folders_view(uid=m.from_user.id)
    await safe_answer(m, txt, reply_markup=kb)


@dp.message(F.text.in_({"🛍 Shop", "🛍️ Shop", "Shop"}), StateFilter("*"))
async def btn_products(m: Message, state: FSMContext):
    await cmd_products(m, state)


@dp.message(Command("deposit"), StateFilter("*"))
async def cmd_deposit(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt, kb = deposit_menu_view()
    await safe_answer(m, txt, reply_markup=kb)


@dp.message(F.text.in_({"💳 Top Up", "Top Up"}), StateFilter("*"))
async def btn_deposit(m: Message, state: FSMContext):
    await cmd_deposit(m, state)


@dp.message(Command("orders"), StateFilter("*"))
async def cmd_orders(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt, kb = await myorders_view(m.from_user.id)
    await safe_answer(m, txt, reply_markup=kb)


@dp.message(Command("settings", "profile"), StateFilter("*"))
async def cmd_profile(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt, kb = await profile_view(m.from_user)
    await safe_answer(m, txt, reply_markup=kb)


@dp.message(F.text.in_({"👤 Profile", "Profile"}), StateFilter("*"))
async def btn_profile(m: Message, state: FSMContext):
    await cmd_profile(m, state)


@dp.message(Command("support"), StateFilter("*"))
async def cmd_support(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt, kb = support_view()
    await safe_answer(m, txt, reply_markup=kb)


@dp.message(F.text.in_({"💬 Support", "Support"}), StateFilter("*"))
async def btn_support(m: Message, state: FSMContext):
    await cmd_support(m, state)


@dp.message(Command("search"), StateFilter("*"))
async def cmd_search(m: Message, state: FSMContext):
    await ensure_user(m.from_user)
    await state.set_state(SearchSt.q)
    b = InlineKeyboardBuilder()
    b.row(Btn(text="❌ Cancel", callback_data="shop:folders", style="danger"))
    await safe_answer(m, "🔍 <b>Search Products</b>\nEnter a product or brand keyword:", reply_markup=b.as_markup())


@dp.message(Command("api"), StateFilter("*"))
async def cmd_api_info(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt, kb = support_view(api_inquiry=True)
    await safe_answer(m, txt, reply_markup=kb)


@dp.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    txt = await welcome_text(m.from_user)
    await safe_answer(m, txt, reply_markup=main_menu())


@dp.callback_query(F.data == "home", StateFilter("*"))
async def cb_home(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    txt = await welcome_text(c.from_user)
    await safe_edit(c, txt, reply_markup=main_menu())


# Profile, Wallet, Support, and Order History
async def profile_view(user):
    bal = await balance(user.id)
    stats = await pool.fetchrow(
        """SELECT
            (SELECT count(*) FROM orders WHERE user_id=$1 AND status='done') AS orders_ok,
            (SELECT coalesce(sum(cost),0) FROM orders WHERE user_id=$1 AND status='done') AS total_spent,
            (SELECT coalesce(sum(amount),0) FROM deposits WHERE user_id=$1) AS total_dep
        """,
        user.id,
    )
    txt = (
        f"👤 <b>Profile & Settings</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"🏷 <b>Name:</b> {esc(user.full_name)}\n"
        f"🔗 <b>Username:</b> @{esc(user.username) if user.username else 'Not set'}\n\n"
        f"💰 <b>Balance:</b> <code>{bal} USDT</code>\n"
        f"📥 <b>Total Deposited:</b> <code>{q2(stats['total_dep'])} USDT</code>\n"
        f"🛍 <b>Total Orders:</b> <code>{stats['orders_ok']}</code>\n"
        f"💵 <b>Total Spent:</b> <code>{q2(stats['total_spent'])} USDT</code>"
    )
    b = InlineKeyboardBuilder()
    b.row(
        Btn(text="💳 Deposit", callback_data="deposit", style="success"),
        Btn(text="📜 My Orders", callback_data="myorders", style="primary"),
    )
    b.row(Btn(text="◀️ Back", callback_data="home", style="danger"))
    return txt, b.as_markup()


@dp.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    await c.answer()
    txt, kb = await profile_view(c.from_user)
    await safe_edit(c, txt, reply_markup=kb)


@dp.callback_query(F.data == "wallet")
async def cb_wallet(c: CallbackQuery):
    await c.answer()
    bal = await balance(c.from_user.id)
    b = InlineKeyboardBuilder()
    b.row(Btn(text="➕ Deposit Funds", callback_data="deposit", style="success"))
    b.row(Btn(text="📜 My Orders", callback_data="myorders", style="primary"))
    b.row(Btn(text="◀️ Back", callback_data="home", style="danger"))
    await safe_edit(c, f"💰 <b>Wallet Balance:</b> <code>{bal} USDT</code>", reply_markup=b.as_markup())


def support_view(api_inquiry: bool = False):
    num = (ST.get("contact_number") or CONTACT_NUMBER or "").strip()
    w_url = get_whatsapp_url("Hello! I need support.")
    title = "🔌 <b>Reseller API Access</b>\n\nContact admin for API access:" if api_inquiry else "💬 <b>Support & Contact</b>\n\nReach out to admin directly:"
    lines = [title, ""]
    if ADMIN_USERNAME:
        lines.append(f"📩 <b>Telegram:</b> @{esc(ADMIN_USERNAME)}")
    if w_url:
        lines.append(f"💬 <b>WhatsApp:</b> <a href=\"{esc(w_url)}\">Open Chat</a>")
    if num:
        lines.append(f"📞 <b>Phone:</b> <code>{esc(num)}</code>")

    b = InlineKeyboardBuilder()
    add_contact_rows(b, "Hello! I need support.")
    b.row(Btn(text="◀️ Back", callback_data="home", style="danger"))
    return "\n".join(lines), b.as_markup()


@dp.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery):
    await c.answer()
    txt, kb = support_view()
    await safe_edit(c, txt, reply_markup=kb)


@dp.callback_query(F.data == "contact:phone")
async def cb_contact_phone(c: CallbackQuery):
    num = (ST.get("contact_number") or CONTACT_NUMBER or "").strip()
    if not num:
        return await c.answer("Contact number not set.", show_alert=True)
    await c.answer(f"Phone: {num}", show_alert=True)
    try:
        await bot.send_contact(chat_id=c.from_user.id, phone_number=num, first_name=SHOP_NAME[:30], last_name="Support")
    except Exception:
        pass
    await c.message.answer(f"📞 <b>Contact Number:</b> <code>{esc(num)}</code>", parse_mode="HTML")


async def myorders_view(uid: int):
    rows = await pool.fetch(
        "SELECT id,order_code,product,qty,cost,created FROM orders WHERE user_id=$1 AND status='done' "
        "ORDER BY id DESC LIMIT 10",
        uid,
    )
    b = InlineKeyboardBuilder()
    for r in rows:
        code = r["order_code"] or f"#{r['id']}"
        b.row(Btn(text=f"📦 {code} — {r['product'][:22]} ×{r['qty']} ({r['cost']}$)", callback_data=f"o:{r['id']}", style="primary"))
    b.row(Btn(text="🛍 Shop", callback_data="shop:folders", style="success"))
    b.row(Btn(text="◀️ Back", callback_data="home", style="danger"))
    txt = "📜 <b>Your Recent Orders:</b>" if rows else "📜 <b>No orders found yet.</b>"
    return txt, b.as_markup()


@dp.callback_query(F.data == "myorders")
async def cb_myorders(c: CallbackQuery):
    await c.answer()
    txt, kb = await myorders_view(c.from_user.id)
    await safe_edit(c, txt, reply_markup=kb)


@dp.callback_query(F.data.startswith("o:"))
async def cb_order_view(c: CallbackQuery):
    await c.answer()
    r = await pool.fetchrow(
        "SELECT id,order_code,product,qty,cost,delivery,created AT TIME ZONE 'Asia/Dhaka' AS t "
        "FROM orders WHERE id=$1 AND user_id=$2",
        int(c.data[2:]),
        c.from_user.id,
    )
    if not r:
        return await c.answer("Order not found.", show_alert=True)
    code = r["order_code"] or f"#{r['id']}"
    b = InlineKeyboardBuilder()
    b.row(Btn(text="◀️ Back", callback_data="myorders", style="danger"))
    await safe_edit(
        c,
        f"📦 <b>Order {esc(code)}</b>\n\n"
        f"🏷 <b>Product:</b> {esc(r['product'])}\n"
        f"🔢 <b>Quantity:</b> <code>{r['qty']}</code>\n"
        f"💵 <b>Paid:</b> <code>{r['cost']} USDT</code>\n"
        f"🕒 <b>Date:</b> <code>{r['t']:%Y-%m-%d %H:%M}</code>\n\n"
        f"🔑 <b>Delivery:</b>\n<code>{esc((r['delivery'] or '')[:3500])}</code>",
        reply_markup=b.as_markup(),
    )


# Organized folders and clean product listings
PAGE = 8


async def build_folders_view(force: bool = False, uid: int = None):
    ps = await get_products(force=force)
    p_by_id = {str(p["id"]): p for p in ps}
    fav_products = [p_by_id[pid] for pid in FAVS if pid in p_by_id][:3]
    fav_cats = {detect_category(p) for p in fav_products}

    cat_counts = {cid: 0 for cid, _, _, _ in CATEGORIES}
    for p in ps:
        cid = detect_category(p)
        cat_counts[cid] = cat_counts.get(cid, 0) + 1

    b = InlineKeyboardBuilder()

    for fp in fav_products:
        _, ce_id = detect_product_icon(fp)
        btn_style = "success" if fp["ok"] else "danger"
        b.row(
            Btn(
                text=format_product_btn_text(fp, uid=uid),
                callback_data=f"p:{fp['id']}:1:folders",
                style=btn_style,
                icon_custom_emoji_id=ce_id,
            )
        )

    folder_btns = []
    show_all = ST.get("show_empty_folders", False) or len(ps) == 0
    for cid, label, icon, _ in CATEGORIES:
        cnt = cat_counts.get(cid, 0)
        if not show_all and cnt == 0 and cid not in fav_cats:
            continue
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

    b.row(
        Btn(text="🔍 Search", callback_data="search", style="primary"),
        Btn(text="🛍 All items", callback_data="all:0", style="primary"),
    )
    b.row(
        Btn(text="🔄 Refresh", callback_data="shop:folders:ref", style="success"),
        Btn(text="◀️ Back", callback_data="home", style="danger"),
    )

    return "🛍 <b>Select a Category or Product:</b>", b.as_markup()


async def build_product_list_view(mode: str, page: int, force: bool = False, uid: int = None):
    ps = await get_products(force=force)
    p_by_id = {str(p["id"]): p for p in ps}
    fav_set = set(FAVS[:3])

    if mode == "all":
        cat_items = list(ps)
        pinned_favs = [p_by_id[pid] for pid in FAVS[:3] if pid in p_by_id]
        origin_prefix = f"all-{page}"
        page_cb_prefix = "all"
    else:
        cid = mode.split(":", 1)[1]
        cat_items = [p for p in ps if detect_category(p) == cid]
        pinned_favs = [p_by_id[pid] for pid in FAVS[:3] if pid in p_by_id and detect_category(p_by_id[pid]) == cid]
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
        is_fav = str(p["id"]) in fav_set
        b.row(make_product_btn(p, is_fav=is_fav, origin=origin_prefix, uid=uid))

    nav = [Btn(text="🔄 Refresh", callback_data=f"{page_cb_prefix}:{page}:ref", style="success")]
    if page > 0:
        nav.append(Btn(text="◀️ Prev", callback_data=f"{page_cb_prefix}:{page-1}", style="primary"))
    nav.append(Btn(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if (page + 1) < total_pages:
        nav.append(Btn(text="▶️ Next", callback_data=f"{page_cb_prefix}:{page+1}", style="primary"))
    b.row(*nav)
    b.row(Btn(text="◀️ Back", callback_data="shop:folders", style="danger"))

    txt = f"Available Products (Page {page+1}/{total_pages}):" if ordered_products else "No products available in this folder."
    return txt, b.as_markup()


@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data.in_({"shop:0", "shop:folders", "shop:folders:ref"}), StateFilter("*"))
async def cb_shop_folders(c: CallbackQuery, state: FSMContext):
    force = c.data.endswith(":ref")
    await c.answer("Refreshed!" if force else None)
    await state.clear()
    txt, kb = await build_folders_view(force=force, uid=c.from_user.id)
    await safe_edit(c, txt, reply_markup=kb)


@dp.callback_query(F.data.startswith("all:"))
async def cb_all_items(c: CallbackQuery):
    parts = c.data.split(":")
    page = int(parts[1])
    force = len(parts) > 2 and parts[2] == "ref"
    await c.answer("Refreshed!" if force else None)
    txt, kb = await build_product_list_view("all", page, force=force, uid=c.from_user.id)
    await safe_edit(c, txt, reply_markup=kb)


@dp.callback_query(F.data.startswith("cat:"))
async def cb_cat_items(c: CallbackQuery):
    parts = c.data.split(":")
    cid = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0
    force = len(parts) > 3 and parts[3] == "ref"
    await c.answer("Refreshed!" if force else None)
    txt, kb = await build_product_list_view(f"cat:{cid}", page, force=force, uid=c.from_user.id)
    await safe_edit(c, txt, reply_markup=kb)


# Search
@dp.callback_query(F.data == "search")
async def cb_search(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(SearchSt.q)
    b = InlineKeyboardBuilder()
    b.row(Btn(text="◀️ Back", callback_data="shop:folders", style="danger"))
    await safe_edit(c, "🔍 <b>Enter keyword to search products:</b>", reply_markup=b.as_markup())


@dp.message(SearchSt.q, F.text)
async def search_input(m: Message, state: FSMContext):
    q = m.text.strip().lower()
    await state.clear()
    ps = await get_products()
    fav_set = set(FAVS[:3])
    matches = [p for p in ps if q in p["name"].lower() or q in detect_category(p)]
    b = InlineKeyboardBuilder()
    for p in matches[:20]:
        b.row(make_product_btn(p, is_fav=(str(p["id"]) in fav_set), origin="folders", uid=m.from_user.id))
    b.row(
        Btn(text="🔍 Search Again", callback_data="search", style="primary"),
        Btn(text="◀️ Categories", callback_data="shop:folders", style="danger"),
    )
    txt = f"🔍 <b>Search Results ({len(matches)}):</b>" if matches else f"🔍 No products found for <code>{esc(m.text.strip())}</code>."
    await safe_answer(m, txt, reply_markup=b.as_markup())


# Product view with [-] [qty] [+] stepper, Custom Quantity, and Purchase Now
def resolve_back_cb(origin: str) -> str:
    if not origin or origin == "folders":
        return "shop:folders"
    if origin.startswith("all-"):
        return f"all:{origin.split('-')[1]}"
    if origin.startswith("cat-"):
        parts = origin.split("-")
        return f"cat:{parts[1]}:{parts[2]}"
    return "shop:folders"


def render_product_detail(p: dict, qty: int, origin: str, uid: int = None):
    icon, _ = detect_product_icon(p)
    max_stk = p["stock"] if p["stock"] is not None else 100
    qty = max(1, min(qty, max(1, max_stk)))
    unit_p = sell_price(p, uid=uid)
    total_p = q2(unit_p * qty)
    st_text = "Unlimited" if p["stock"] is None else (str(p["stock"]) if p["ok"] else "0 (Out of Stock)")
    note, _ = note_for(p)
    back_cb = resolve_back_cb(origin)

    if qty > 1:
        price_block = (
            f"💵 <b>Base Price:</b> <code>{unit_p} USDT</code>\n"
            f"💰 <b>Total Price ({qty}×):</b> <b>{total_p} USDT</b>"
        )
    else:
        price_block = f"💵 <b>Price:</b> <b>{unit_p} USDT</b>"

    txt = (
        f"{icon} <b>{esc(p['name'])}</b>\n\n"
        f"{price_block}\n"
        f"📊 <b>Stock:</b> <code>{st_text}</code>\n"
    )
    if note:
        txt += f"\n📝 <b>Product Info:</b>\n<blockquote>{esc(note)}</blockquote>\n"

    b = InlineKeyboardBuilder()
    if p["ok"]:
        minus_q = max(1, qty - 1)
        plus_q = min(max_stk, qty + 1)
        b.row(
            Btn(text="➖", callback_data=f"p:{p['id']}:{minus_q}:{origin}", style="primary"),
            Btn(text=f"{qty}", callback_data="noop"),
            Btn(text="➕", callback_data=f"p:{p['id']}:{plus_q}:{origin}", style="primary"),
        )
        b.row(Btn(text="✏️ Custom Quantity", callback_data=f"cq:{p['id']}:{origin}", style="primary"))
        b.row(Btn(text=f"🛒 Purchase Now ({total_p} USDT)", callback_data=f"paym:{p['id']}:{qty}:{origin}", style="success"))
    else:
        b.row(Btn(text="🔴 Out of Stock", callback_data="noop", style="danger"))

    b.row(Btn(text="◀️ Back", callback_data=back_cb, style="danger"))
    return txt, b.as_markup()


@dp.callback_query(F.data.startswith("p:"))
async def cb_product(c: CallbackQuery):
    parts = c.data.split(":", 3)
    pid = parts[1]
    qty = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    origin = parts[3] if len(parts) > 3 else (parts[2] if len(parts) == 3 and not parts[2].isdigit() else "folders")

    p = await find(pid, force=False)
    if not p:
        return await c.answer("Product not found.", show_alert=True)
    if p["stock"] is not None and p["ok"] and qty > p["stock"]:
        await c.answer(f"Maximum available stock is {p['stock']}", show_alert=True)
        qty = p["stock"]
    else:
        await c.answer()

    txt, kb = render_product_detail(p, qty, origin, uid=c.from_user.id)
    await safe_edit(c, txt, reply_markup=kb)


@dp.callback_query(F.data.startswith("cq:"))
async def cb_custom_qty(c: CallbackQuery, state: FSMContext):
    parts = c.data.split(":", 2)
    pid = parts[1]
    origin = parts[2] if len(parts) > 2 else "folders"
    p = await find(pid, force=False)
    if not p or not p["ok"]:
        return await c.answer("Out of stock.", show_alert=True)
    mx = p["stock"] if p["stock"] is not None else 100
    await state.set_state(Qty.n)
    await state.update_data(pid=str(p["id"]), origin=origin)
    await c.message.answer(f"🔢 Enter quantity (1 to {mx}):", parse_mode="HTML")
    await c.answer()


@dp.message(Qty.n, F.text)
async def qty_in(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("Please enter a valid number.")
    n = int(m.text.strip())
    st_data = await state.get_data()
    p = await find(st_data["pid"], force=False)
    if not p or not p["ok"]:
        await state.clear()
        return await m.answer("❌ Product is out of stock.")
    mx = p["stock"] if p["stock"] is not None else 100
    if not 1 <= n <= mx:
        return await m.answer(f"Please enter a number between 1 and {mx}.")
    await state.clear()
    txt, kb = render_product_detail(p, n, st_data.get("origin", "folders"), uid=m.from_user.id)
    await safe_answer(m, txt, reply_markup=kb)


# Purchase Now -> Payment Method Selection
@dp.callback_query(F.data.startswith("paym:"))
async def cb_pay_methods(c: CallbackQuery):
    parts = c.data.split(":", 3)
    pid, qty = parts[1], int(parts[2])
    origin = parts[3] if len(parts) > 3 else "folders"
    p = await find(pid, force=True)
    if not p or not p["ok"] or (p["stock"] is not None and p["stock"] < qty):
        return await c.answer("❌ Insufficient stock.", show_alert=True)
    await c.answer()

    unit_p = sell_price(p, uid=c.from_user.id)
    total = q2(unit_p * qty)
    cents = int(total * 100)
    bal = await balance(c.from_user.id)

    b = InlineKeyboardBuilder()
    b.row(Btn(text=f"💰 Wallet (Balance: {bal} USDT)", callback_data=f"buy:{pid}:{qty}:{cents}", style="success"))
    b.row(Btn(text=f"🟡 Direct Pay — Binance ({total} USDT)", callback_data=f"dpay:binance:{pid}:{qty}:{cents}", style="primary"))
    b.row(Btn(text=f"⚫ Direct Pay — Bybit ({total} USDT)", callback_data=f"dpay:bybit:{pid}:{qty}:{cents}", style="primary"))
    b.row(Btn(text="◀️ Back", callback_data=f"p:{pid}:{qty}:{origin}", style="danger"))

    price_info = (
        f"💵 <b>Base Price:</b> <code>{unit_p} USDT</code>\n💰 <b>Total Payable:</b> <b>{total} USDT</b>"
        if qty > 1
        else f"💰 <b>Total Payable:</b> <b>{total} USDT</b>"
    )
    txt = (
        f"💳 <b>Select Payment Method</b>\n\n"
        f"📦 <b>Item:</b> {esc(p['name'])}\n"
        f"🔢 <b>Quantity:</b> <code>{qty}</code>\n"
        f"{price_info}\n\n"
        f"Choose <b>Wallet</b> or <b>Direct Pay</b> below:"
    )
    await safe_edit(c, txt, reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("buy:"))
async def cb_wallet_buy(c: CallbackQuery):
    _, pid, qty_s, cents_s = c.data.split(":")
    qty, uid = int(qty_s), c.from_user.id

    lock = get_lock(uid)
    if lock.locked():
        return await c.answer("⏳ Please wait, your previous request is processing.", show_alert=True)

    async with lock:
        p = await find(pid, force=True)
        if not p or not p["ok"] or (p["stock"] is not None and p["stock"] < qty):
            return await c.answer("❌ Out of stock.", show_alert=True)
        total = q2(sell_price(p, uid=uid) * qty)
        if int(total * 100) != int(cents_s):
            b = InlineKeyboardBuilder()
            b.row(Btn(text="🔄 View Updated Price", callback_data=f"p:{pid}:{qty}:folders", style="primary"))
            return await safe_edit(c, f"⚠️ Price changed to <code>{sell_price(p, uid=uid)} USDT</code>.", reply_markup=b.as_markup())
        if await deduct(uid, total) is None:
            return await c.answer("❌ Insufficient wallet balance! Please deposit or use Direct Pay.", show_alert=True)
        await c.answer()
        await safe_edit(c, "⏳ <b>Processing your order...</b>")
        u = c.from_user
        await execute_product_order(uid, u.full_name, u.username, p, qty, total, "wallet", target_msg=c)


# Direct Pay (Binance & Bybit)
@dp.callback_query(F.data.startswith("dpay:"))
async def cb_direct_pay_start(c: CallbackQuery, state: FSMContext):
    _, method, pid, qty_s, cents_s = c.data.split(":")
    qty = int(qty_s)
    p = await find(pid, force=False)
    if not p or not p["ok"] or (p["stock"] is not None and p["stock"] < qty):
        return await c.answer("❌ Out of stock.", show_alert=True)
    await c.answer()

    total = q2(sell_price(p, uid=c.from_user.id) * qty)
    await state.set_state(DirectPay.txid)
    await state.update_data(method=method, pid=str(pid), qty=qty, total=str(total))

    b = InlineKeyboardBuilder()
    b.row(Btn(text="◀️ Back", callback_data=f"paym:{pid}:{qty}:folders", style="danger"))

    if method == "binance":
        pay_target = f"🆔 <b>Binance Pay ID:</b> <code>{esc(BINANCE_PAY_ID or 'Not set')}</code>"
    else:
        pay_target = f"🆔 <b>Bybit UID:</b> <code>{esc(BYBIT_UID or 'Not set')}</code>"

    txt = (
        f"💳 <b>Direct Pay via {method.capitalize()}</b>\n\n"
        f"📦 <b>Product:</b> {esc(p['name'])} × {qty}\n"
        f"💵 <b>Exact Amount to Send:</b> <code>{total} USDT</code>\n"
        f"{pay_target}\n\n"
        f"1️⃣ Send <b>exactly {total} USDT</b> to the ID above.\n"
        f"2️⃣ Paste your <b>Transaction ID / Order ID</b> below:"
    )
    await safe_edit(c, txt, reply_markup=b.as_markup())


@dp.message(DirectPay.txid, F.text)
async def direct_pay_tx_in(m: Message, state: FSMContext):
    txid = m.text.strip()
    if len(txid) < 5 or len(txid) > 100:
        return await m.answer("⚠️ Please enter a valid Transaction / Order ID.")
    st_data = await state.get_data()
    method = st_data["method"]
    pid = st_data["pid"]
    qty = int(st_data["qty"])
    expected_total = D(st_data["total"])
    await state.clear()

    if await is_tx_used(method, txid):
        return await m.answer("⚠️ This Transaction ID has already been used.")

    req_id = await pool.fetchval(
        "INSERT INTO manual_verifications(user_id,username,full_name,kind,method,txid,pid,qty,expected_usdt,attempts,status) "
        "VALUES($1,$2,$3,'direct',$4,$5,$6,$7,$8,1,'checking') RETURNING id",
        m.from_user.id, m.from_user.username, m.from_user.full_name, method, txid, pid, qty, expected_total,
    )
    wait = await m.answer("🛰 <b>Initializing verification...</b>", parse_mode="HTML")
    await process_crypto_verification(wait, m.from_user, req_id)


# Deposit Flow
def deposit_menu_view():
    b = InlineKeyboardBuilder()
    b.row(Btn(text="🟡 Binance", callback_data="dep:binance", style="success"))
    b.row(Btn(text="⚫ Bybit", callback_data="dep:bybit", style="success"))
    b.row(Btn(text="🟣 bKash", callback_data="dep:bkash", style="primary"))
    b.row(Btn(text="◀️ Back", callback_data="home", style="danger"))
    txt = "💳 <b>Deposit Funds</b>\n\nSelect a payment method below:"
    return txt, b.as_markup()


@dp.callback_query(F.data == "deposit")
async def cb_deposit(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    txt, kb = deposit_menu_view()
    await safe_edit(c, txt, reply_markup=kb)


@dp.callback_query(F.data.startswith("dep:"))
async def cb_dep_method(c: CallbackQuery, state: FSMContext):
    await c.answer()
    m = c.data[4:]
    b = InlineKeyboardBuilder()
    b.row(Btn(text="◀️ Back", callback_data="deposit", style="danger"))

    if m == "binance":
        await state.set_state(Dep.txid)
        await state.update_data(method="binance")
        txt = (
            f"🟡 <b>Binance Deposit</b>\n\n"
            f"🆔 <b>Binance Pay ID:</b> <code>{esc(BINANCE_PAY_ID or 'Not set')}</code>\n\n"
            f"1️⃣ Send any amount of <b>USDT</b> via Binance Pay.\n"
            f"2️⃣ Paste your <b>Order ID / Transaction ID</b> here in chat:"
        )
    elif m == "bybit":
        await state.set_state(Dep.txid)
        await state.update_data(method="bybit")
        txt = (
            f"⚫ <b>Bybit Deposit</b>\n\n"
            f"🆔 <b>Bybit UID:</b> <code>{esc(BYBIT_UID or 'Not set')}</code>\n\n"
            f"1️⃣ Send any amount of <b>USDT</b> to the UID above via Internal Transfer.\n"
            f"2️⃣ Paste your <b>Transaction ID</b> here in chat:"
        )
    else:
        await state.set_state(Dep.bk_amt)
        txt = (
            f"🟣 <b>bKash Deposit</b>\n\n"
            f"💱 <b>Rate:</b> <code>1 USDT = {BDT_RATE} BDT</code>\n"
            f"📉 <b>Minimum Deposit:</b> <code>{MIN_DEPOSIT} USDT</code>\n\n"
            f"✏️ <b>Enter the amount in USDT you want to deposit (e.g. 10):</b>"
        )
    await safe_edit(c, txt, reply_markup=b.as_markup())


# bKash Step 1 & Step 2
@dp.message(Dep.bk_amt, F.text)
async def bk_amt_in(m: Message, state: FSMContext):
    try:
        usdt_amt = q2(D(m.text.strip()))
        assert usdt_amt >= MIN_DEPOSIT
    except Exception:
        return await m.answer(f"⚠️ Please enter a valid amount (Minimum: <b>{MIN_DEPOSIT} USDT</b>).", parse_mode="HTML")

    bdt_amt = (usdt_amt * BDT_RATE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    await state.set_state(Dep.bk_trx)
    await state.update_data(usdt_amt=str(usdt_amt), bdt_amt=str(bdt_amt))

    b = InlineKeyboardBuilder()
    b.row(Btn(text="◀️ Back", callback_data="deposit", style="danger"))
    await safe_answer(
        m,
        f"🟣 <b>bKash Send Money Instructions</b>\n\n"
        f"💵 <b>Deposit Amount:</b> <code>{usdt_amt} USDT</code>\n"
        f"🇧🇩 <b>Payable Amount:</b> <b>{bdt_amt} BDT</b> <i>(Rate: {BDT_RATE} BDT)</i>\n"
        f"📱 <b>bKash Number (Send Money):</b> <code>{esc(BKASH_NUMBER or 'Not set')}</code>\n\n"
        f"1️⃣ Send <b>{bdt_amt} BDT</b> to the bKash number above.\n"
        f"2️⃣ Enter your <b>Transaction ID (TrxID)</b> right here:",
        reply_markup=b.as_markup(),
    )


@dp.message(Dep.bk_trx, F.text)
async def bk_trx_in(m: Message, state: FSMContext):
    trx = m.text.strip().upper()
    if len(trx) < 4 or len(trx) > 40 or " " in trx:
        return await m.answer("⚠️ Please enter a valid bKash Transaction ID (TrxID).")

    st_data = await state.get_data()
    usdt_amt = D(st_data["usdt_amt"])
    bdt_amt = D(st_data["bdt_amt"])

    rid = await pool.fetchval(
        "INSERT INTO bkash(user_id,trx,usdt_amount,amount_bdt,status) VALUES($1,$2,$3,$4,'pending') "
        "ON CONFLICT DO NOTHING RETURNING id",
        m.from_user.id, trx, usdt_amt, bdt_amt,
    )
    if not rid:
        return await m.answer("⚠️ This Transaction ID has already been submitted.")

    await state.clear()
    await safe_answer(m, "⏳ <b>Your deposit is being verified by admin.</b>")

    u = m.from_user
    t_str = await bd_now()
    ab = InlineKeyboardBuilder()
    ab.row(
        Btn(text="✅ Approve", callback_data=f"adm:bk:ok:{rid}", style="success"),
        Btn(text="❌ Reject", callback_data=f"adm:bk:no:{rid}", style="danger"),
    )
    await notify_admin(
        f"🟣 <b>bKash Deposit Verification (#{rid})</b>\n\n"
        f"👤 <b>User:</b> {esc(u.full_name)} (@{esc(u.username or 'N/A')})\n"
        f"🆔 <b>User ID:</b> <code>{u.id}</code>\n"
        f"💵 <b>Deposit:</b> <b>{usdt_amt} USDT</b> (<code>{bdt_amt} BDT</code>)\n"
        f"🧾 <b>TrxID:</b> <code>{esc(trx)}</code>",
        reply_markup=ab.as_markup(),
    )
    await send_log(
        f"⏳ <b>BKASH VERIFICATION SUBMITTED</b>\n\n"
        f"👤 <b>User:</b> {esc(u.full_name)} (@{esc(u.username or 'N/A')})\n"
        f"🆔 <b>User ID:</b> <code>{u.id}</code>\n"
        f"💵 <b>Requested:</b> <code>{usdt_amt} USDT</code> ({bdt_amt} BDT)\n"
        f"🧾 <b>TrxID:</b> <code>{esc(trx)}</code>\n"
        f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
    )


# Binance & Bybit Wallet Deposit
@dp.message(Dep.txid, F.text)
async def dep_tx_in(m: Message, state: FSMContext):
    txid = m.text.strip()
    if len(txid) < 5 or len(txid) > 100:
        return await m.answer("⚠️ Please send a valid Transaction / Order ID.")
    method = (await state.get_data())["method"]
    await state.clear()

    if await is_tx_used(method, txid):
        return await m.answer("⚠️ This Transaction ID has already been used.")

    req_id = await pool.fetchval(
        "INSERT INTO manual_verifications(user_id,username,full_name,kind,method,txid,attempts,status) "
        "VALUES($1,$2,$3,'deposit',$4,$5,1,'checking') RETURNING id",
        m.from_user.id, m.from_user.username, m.from_user.full_name, method, txid,
    )
    wait = await m.answer("🛰 <b>Initializing verification...</b>", parse_mode="HTML")
    await process_crypto_verification(wait, m.from_user, req_id)


async def process_crypto_verification(wait_msg: Message, user, req_id: int):
    req = await pool.fetchrow("SELECT * FROM manual_verifications WHERE id=$1", req_id)
    if not req or req["status"] in ("completed", "approved", "rejected"):
        return

    method = req["method"]
    txid = req["txid"]
    kind = req["kind"]
    attempts = req["attempts"]

    amt = await run_two_stage_verification(wait_msg, method, txid)
    t_str = await bd_now()

    if amt and amt > 0:
        if kind == "direct":
            expected = req["expected_usdt"]
            if amt >= expected:
                if await mark_tx_used(method, txid, user.id, amt):
                    await pool.execute("UPDATE manual_verifications SET status='completed' WHERE id=$1", req_id)
                    if amt > expected:
                        await credit(user.id, q2(amt - expected))
                    await send_log(
                        f"✅ <b>VERIFICATION SUCCESSFUL (DIRECT PAY)</b>\n\n"
                        f"👤 <b>User:</b> {esc(user.full_name)} (@{esc(user.username or 'N/A')})\n"
                        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
                        f"💳 <b>Method:</b> <code>{method.upper()}</code>\n"
                        f"💵 <b>Verified Amount:</b> <code>{amt} USDT</code>\n"
                        f"🧾 <b>TxID:</b> <code>{esc(txid)}</code>\n"
                        f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
                    )
                    p = await find(req["pid"], force=True)
                    if not p or not p["ok"] or (p["stock"] is not None and p["stock"] < req["qty"]):
                        await credit(user.id, expected)
                        b = InlineKeyboardBuilder()
                        b.row(Btn(text="🛍 Shop", callback_data="shop:folders", style="success"))
                        await safe_edit(
                            wait_msg,
                            f"⚠️ Payment of <code>{amt} USDT</code> verified, but item just went out of stock. Full amount added to your wallet!",
                            reply_markup=b.as_markup(),
                        )
                        return
                    await execute_product_order(
                        user.id, req["full_name"] or user.full_name, req["username"] or user.username,
                        p, req["qty"], expected, f"direct_{method}", target_msg=wait_msg,
                    )
                    return
        else:
            if await credit_deposit(method, txid, user.id, amt):
                await pool.execute("UPDATE manual_verifications SET status='completed' WHERE id=$1", req_id)
                bal = await balance(user.id)
                b = InlineKeyboardBuilder()
                b.row(Btn(text="🛍 Shop", callback_data="shop:folders", style="success"))
                await safe_edit(
                    wait_msg,
                    f"✅ <b>Deposit Successful!</b>\n\n"
                    f"➕ <b>Added:</b> <code>{amt} USDT</code>\n"
                    f"💰 <b>Total Balance:</b> <code>{bal} USDT</code>",
                    reply_markup=b.as_markup(),
                )
                await send_log(
                    f"✅ <b>VERIFICATION SUCCESSFUL (DEPOSIT)</b>\n\n"
                    f"👤 <b>User:</b> {esc(user.full_name)} (@{esc(user.username or 'N/A')})\n"
                    f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
                    f"💳 <b>Method:</b> <code>{method.upper()} (Auto)</code>\n"
                    f"➕ <b>Amount:</b> <code>{amt} USDT</code>\n"
                    f"🧾 <b>TxID:</b> <code>{esc(txid)}</code>\n"
                    f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
                )
                return

    # Log failed automatic verification attempt
    await send_log(
        f"❌ <b>VERIFICATION FAILED (ATTEMPT {attempts}/2)</b>\n\n"
        f"👤 <b>User:</b> {esc(user.full_name)} (@{esc(user.username or 'N/A')})\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"📌 <b>Type:</b> <code>{kind.upper()}</code>\n"
        f"💳 <b>Method:</b> <code>{method.upper()}</code>\n"
        f"🧾 <b>TxID:</b> <code>{esc(txid)}</code>\n"
        f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
    )

    b = InlineKeyboardBuilder()
    if attempts < 2:
        b.row(Btn(text="🔄 Try Again", callback_data=f"vretry:{req_id}", style="primary"))
    b.row(Btn(text="👨‍💼 Verify Manually by Admin", callback_data=f"vman:{req_id}", style="success"))
    add_contact_rows(b, f"Help with {method.upper()} TxID: {txid}")

    await safe_edit(
        wait_msg,
        f"❌ <b>Verification Failed</b>\n\n"
        f"🧾 <b>TxID:</b> <code>{esc(txid)}</code>\n"
        f"{'You can try automatic verification one more time or request manual verification by admin:' if attempts < 2 else 'Automatic verification limit reached. Please click below to verify manually by admin:'}",
        reply_markup=b.as_markup(),
    )


@dp.callback_query(F.data.startswith("vretry:"))
async def cb_verify_retry(c: CallbackQuery):
    req_id = int(c.data.split(":")[1])
    req = await pool.fetchrow("SELECT * FROM manual_verifications WHERE id=$1 AND user_id=$2", req_id, c.from_user.id)
    if not req:
        return await c.answer("Request not found.", show_alert=True)
    if req["attempts"] >= 2:
        return await c.answer("Retry limit reached. Please use Verify Manually by Admin.", show_alert=True)
    await c.answer()
    await pool.execute("UPDATE manual_verifications SET attempts=attempts+1 WHERE id=$1", req_id)
    await process_crypto_verification(c.message, c.from_user, req_id)


@dp.callback_query(F.data.startswith("vman:"))
async def cb_verify_manual_escalate(c: CallbackQuery):
    req_id = int(c.data.split(":")[1])
    req = await pool.fetchrow(
        "UPDATE manual_verifications SET status='manual_pending' WHERE id=$1 AND user_id=$2 AND status!='manual_pending' RETURNING *",
        req_id, c.from_user.id,
    )
    if not req:
        return await c.answer("Already sent to admin.", show_alert=True)
    await c.answer()

    b = InlineKeyboardBuilder()
    add_contact_rows(b, f"Manual verification for TxID: {req['txid']}")
    b.row(Btn(text="◀️ Main Menu", callback_data="home", style="danger"))
    await safe_edit(
        c,
        "⏳ <b>Your payment info has been sent to admin, wait for him to verify.</b>",
        reply_markup=b.as_markup(),
    )

    ab = InlineKeyboardBuilder()
    ab.row(
        Btn(text="✅ Approve", callback_data=f"adm:mv:ok:{req_id}", style="success"),
        Btn(text="❌ Reject", callback_data=f"adm:mv:no:{req_id}", style="danger"),
    )
    kind_label = f"Direct Purchase ({req['expected_usdt']} USDT)" if req["kind"] == "direct" else "Wallet Deposit"
    t_str = await bd_now()
    await notify_admin(
        f"🧾 <b>Manual Verification Request (#{req_id})</b>\n\n"
        f"👤 <b>User:</b> {esc(req['full_name'])} (@{esc(req['username'] or 'N/A')})\n"
        f"🆔 <b>User ID:</b> <code>{req['user_id']}</code>\n"
        f"📌 <b>Type:</b> {kind_label}\n"
        f"💳 <b>Method:</b> <code>{req['method'].upper()}</code>\n"
        f"🧾 <b>TxID:</b> <code>{esc(req['txid'])}</code>",
        reply_markup=ab.as_markup(),
    )
    await send_log(
        f"👨‍💼 <b>MANUAL VERIFICATION REQUESTED</b>\n\n"
        f"👤 <b>User:</b> {esc(req['full_name'])} (@{esc(req['username'] or 'N/A')})\n"
        f"🆔 <b>User ID:</b> <code>{req['user_id']}</code>\n"
        f"📌 <b>Type:</b> {kind_label}\n"
        f"💳 <b>Method:</b> <code>{req['method'].upper()}</code>\n"
        f"🧾 <b>TxID:</b> <code>{esc(req['txid'])}</code>\n"
        f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
    )


# Admin Bot Panel & Handlers (StateFilter("*") on top-level commands so /admin always opens)
def adm_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KBtn(text="🛠 Admin Panel", style="success"), KBtn(text="⏳ Pending", style="primary")],
            [KBtn(text="⭐ Top 3 Favourites", style="primary"), KBtn(text="📊 Stats", style="primary")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def adm_menu():
    b = InlineKeyboardBuilder()
    b.row(
        Btn(text="⭐ Top 3 Favourites", callback_data="adm:favs", style="success"),
        Btn(text="💲 Product Manager", callback_data="adm:pl:0", style="primary"),
    )
    b.row(
        Btn(text="⏳ Pending Verifications", callback_data="adm:pending", style="success"),
        Btn(text="💳 Add / Deduct Balance", callback_data="adm:bal", style="primary"),
    )
    b.row(
        Btn(text="📊 Sales & Stats", callback_data="adm:stats", style="primary"),
        Btn(text="🧾 Recent Orders", callback_data="adm:orders", style="primary"),
    )
    b.row(
        Btn(text="⚙️ Pricing Rule", callback_data="adm:rule", style="primary"),
        Btn(text="👥 User Pricing (/pricingm)", callback_data="adm:uprice", style="success"),
    )
    b.row(
        Btn(text="📞 Contact Settings", callback_data="adm:contact", style="primary"),
    )
    b.row(
        Btn(text="📁 Folder Options", callback_data="adm:fopt", style="primary"),
        Btn(text="🏦 Shop API Balance", callback_data="adm:api", style="primary"),
    )
    return b.as_markup()


def back_adm():
    b = InlineKeyboardBuilder()
    b.row(Btn(text="◀️ Admin Panel", callback_data="adm", style="danger"))
    return b.as_markup()


@adm.message(CommandStart(), StateFilter("*"))
async def adm_start_cmd(m: Message, state: FSMContext):
    await state.clear()
    await safe_answer(
        m,
        "🛠 <b>Admin Control Panel</b>\nManage products, Top 3 Favourites, payments, balances, and store settings:",
        reply_markup=adm_menu(),
    )


@adm.message(Command("admin", "cancel", "menu"), StateFilter("*"))
async def adm_panel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await safe_answer(
        m,
        "🛠 <b>Admin Control Panel</b>\nManage products, Top 3 Favourites, payments, balances, and store settings:",
        reply_markup=adm_menu(),
    )


@adm.message(F.text == "⏳ Pending", StateFilter("*"))
async def adm_quick_pending(m: Message, state: FSMContext):
    await state.clear()
    txt, kb = await build_adm_pending_view()
    await safe_answer(m, txt, reply_markup=kb)


@adm.message(F.text == "⭐ Top 3 Favourites", StateFilter("*"))
async def adm_quick_favs(m: Message, state: FSMContext):
    await state.clear()
    txt, kb = await build_adm_favs_view()
    await safe_answer(m, txt, reply_markup=kb)


@adm.message(F.text == "📊 Stats", StateFilter("*"))
async def adm_quick_stats(m: Message, state: FSMContext):
    await state.clear()
    txt = await build_adm_stats_text()
    await safe_answer(m, txt, reply_markup=back_adm())


@adm.callback_query(F.data == "adm", StateFilter("*"))
async def adm_home(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    await safe_edit(
        c,
        "🛠 <b>Admin Control Panel</b>\nManage products, Top 3 Favourites, payments, balances, and store settings:",
        reply_markup=adm_menu(),
    )


# Admin: Pending Verifications
async def build_adm_pending_view():
    bk_rows = await pool.fetch("SELECT id,user_id,trx,usdt_amount,amount_bdt FROM bkash WHERE status='pending' ORDER BY id ASC LIMIT 8")
    mv_rows = await pool.fetch("SELECT id,user_id,kind,method,txid,expected_usdt FROM manual_verifications WHERE status='manual_pending' ORDER BY id ASC LIMIT 8")
    ord_rows = await pool.fetch("SELECT id,order_code,user_id,product,qty,cost FROM orders WHERE status='pending' ORDER BY id ASC LIMIT 5")

    b = InlineKeyboardBuilder()
    lines = ["⏳ <b>Pending Verifications & Orders</b>\n"]

    if not bk_rows and not mv_rows and not ord_rows:
        lines.append("✅ <i>No pending items right now.</i>")
    if bk_rows:
        lines.append("\n🟣 <b>bKash Deposits:</b>")
        for r in bk_rows:
            usdt = r["usdt_amount"] or q2(r["amount_bdt"] / BDT_RATE)
            lines.append(f"• #{r['id']} User <code>{r['user_id']}</code> — {usdt} USDT ({r['amount_bdt']} BDT) | <code>{esc(r['trx'])}</code>")
            b.row(
                Btn(text=f"✅ bKash #{r['id']} ({usdt}$)", callback_data=f"adm:bk:ok:{r['id']}", style="success"),
                Btn(text=f"❌ Reject #{r['id']}", callback_data=f"adm:bk:no:{r['id']}", style="danger"),
            )
    if mv_rows:
        lines.append("\n🟡 <b>Manual Crypto Verifications:</b>")
        for r in mv_rows:
            lines.append(f"• #{r['id']} User <code>{r['user_id']}</code> ({r['method'].upper()} {r['kind']}) | <code>{esc(r['txid'])}</code>")
            b.row(
                Btn(text=f"✅ Crypto #{r['id']}", callback_data=f"adm:mv:ok:{r['id']}", style="success"),
                Btn(text=f"❌ Reject #{r['id']}", callback_data=f"adm:mv:no:{r['id']}", style="danger"),
            )
    if ord_rows:
        lines.append("\n⚠️ <b>Pending Timeout Orders:</b>")
        for r in ord_rows:
            lines.append(f"• #{r['id']} ({r['order_code']}) User <code>{r['user_id']}</code> — {esc(r['product'][:20])} ×{r['qty']} ({r['cost']}$)")
            b.row(
                Btn(text=f"✅ Delivered #{r['id']}", callback_data=f"adm:ord:ok:{r['id']}", style="success"),
                Btn(text=f"💸 Refund #{r['id']}", callback_data=f"adm:ord:ref:{r['id']}", style="danger"),
            )

    b.row(Btn(text="◀️ Admin Panel", callback_data="adm", style="primary"))
    return "\n".join(lines), b.as_markup()


@adm.callback_query(F.data == "adm:pending", StateFilter("*"))
async def adm_pending(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    txt, kb = await build_adm_pending_view()
    await safe_edit(c, txt, reply_markup=kb)


# Admin: bKash Approve / Reject
@adm.callback_query(F.data.startswith("adm:bk:ok:") | F.data.startswith("adm:bk:no:"), StateFilter("*"))
async def cb_adm_bk_decide(c: CallbackQuery):
    _, _, act, rid_s = c.data.split(":")
    rid = int(rid_s)
    new_st = "approved" if act == "ok" else "rejected"
    r = await pool.fetchrow(
        "UPDATE bkash SET status=$2 WHERE id=$1 AND status='pending' RETURNING user_id,trx,usdt_amount,amount_bdt",
        rid, new_st,
    )
    if not r:
        return await c.answer("Already processed.", show_alert=True)
    await c.answer("Processed!")

    u_row = await pool.fetchrow("SELECT username, name FROM users WHERE id=$1", r["user_id"])
    u_name = u_row["name"] if u_row else f"User {r['user_id']}"
    u_uname = u_row["username"] if u_row else None
    t_str = await bd_now()
    usdt = r["usdt_amount"] or q2(r["amount_bdt"] / BDT_RATE)

    if act == "ok":
        await credit_deposit("bkash", r["trx"], r["user_id"], usdt)
        new_bal = await balance(r["user_id"])
        kb = InlineKeyboardBuilder()
        kb.row(Btn(text="🛍 Shop", callback_data="shop:folders", style="success"))
        await safe_bot_send(
            bot,
            r["user_id"],
            f"✅ <b>{usdt} USDT has been added to your balance!</b>\n"
            f"💰 <b>Total Balance:</b> <code>{new_bal} USDT</code>",
            reply_markup=kb.as_markup(),
        )
        await send_log(
            f"✅ <b>BKASH VERIFICATION APPROVED</b>\n\n"
            f"👤 <b>User:</b> {esc(u_name)} (@{esc(u_uname or 'N/A')})\n"
            f"🆔 <b>User ID:</b> <code>{r['user_id']}</code>\n"
            f"➕ <b>Added:</b> <code>{usdt} USDT</code> ({r['amount_bdt']} BDT)\n"
            f"🧾 <b>TrxID:</b> <code>{esc(r['trx'])}</code>\n"
            f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
        )
    else:
        await safe_bot_send(bot, r["user_id"], "❌ <b>Your bKash deposit request was rejected by admin.</b>")
        await send_log(
            f"❌ <b>BKASH VERIFICATION REJECTED</b>\n\n"
            f"👤 <b>User:</b> {esc(u_name)} (@{esc(u_uname or 'N/A')})\n"
            f"🆔 <b>User ID:</b> <code>{r['user_id']}</code>\n"
            f"💵 <b>Amount:</b> <code>{usdt} USDT</code> ({r['amount_bdt']} BDT)\n"
            f"🧾 <b>TrxID:</b> <code>{esc(r['trx'])}</code>\n"
            f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
        )

    await safe_edit(c, (c.message.html_text or c.message.text) + f"\n\n➡️ <b>{new_st.upper()}</b>")


# Admin: Manual Crypto Verification Approve / Reject
@adm.callback_query(F.data.startswith("adm:mv:ok:") | F.data.startswith("adm:mv:no:"), StateFilter("*"))
async def cb_adm_mv_decide(c: CallbackQuery, state: FSMContext):
    _, _, act, req_id_s = c.data.split(":")
    req_id = int(req_id_s)
    req = await pool.fetchrow("SELECT * FROM manual_verifications WHERE id=$1", req_id)
    if not req or req["status"] in ("completed", "approved", "rejected"):
        return await c.answer("Already processed.", show_alert=True)

    t_str = await bd_now()
    if act == "no":
        await pool.execute("UPDATE manual_verifications SET status='rejected' WHERE id=$1", req_id)
        await c.answer("Rejected.")
        await safe_bot_send(bot, req["user_id"], f"❌ <b>Manual verification for TxID <code>{esc(req['txid'])}</code> was rejected.</b>")
        await send_log(
            f"❌ <b>MANUAL VERIFICATION REJECTED</b>\n\n"
            f"👤 <b>User:</b> {esc(req['full_name'])} (@{esc(req['username'] or 'N/A')})\n"
            f"🆔 <b>User ID:</b> <code>{req['user_id']}</code>\n"
            f"💳 <b>Method:</b> <code>{req['method'].upper()}</code>\n"
            f"🧾 <b>TxID:</b> <code>{esc(req['txid'])}</code>\n"
            f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
        )
        return await safe_edit(c, (c.message.html_text or c.message.text) + "\n\n➡️ <b>REJECTED</b>")

    if req["kind"] == "direct":
        await pool.execute("UPDATE manual_verifications SET status='approved' WHERE id=$1", req_id)
        await mark_tx_used(req["method"], req["txid"], req["user_id"], req["expected_usdt"])
        await c.answer("Approved & delivering product!")
        await safe_edit(c, (c.message.html_text or c.message.text) + "\n\n➡️ <b>APPROVED (Delivering Product)</b>")
        await send_log(
            f"✅ <b>MANUAL VERIFICATION APPROVED (DIRECT PAY)</b>\n\n"
            f"👤 <b>User:</b> {esc(req['full_name'])} (@{esc(req['username'] or 'N/A')})\n"
            f"🆔 <b>User ID:</b> <code>{req['user_id']}</code>\n"
            f"💳 <b>Method:</b> <code>{req['method'].upper()}</code>\n"
            f"💵 <b>Amount:</b> <code>{req['expected_usdt']} USDT</code>\n"
            f"🧾 <b>TxID:</b> <code>{esc(req['txid'])}</code>\n"
            f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
        )
        p = await find(req["pid"], force=True)
        if p and p["ok"]:
            await execute_product_order(
                req["user_id"], req["full_name"] or "", req["username"] or "",
                p, req["qty"], req["expected_usdt"], f"direct_{req['method']}",
            )
        else:
            await credit(req["user_id"], req["expected_usdt"])
            kb = InlineKeyboardBuilder()
            kb.row(Btn(text="🛍 Shop", callback_data="shop:folders", style="success"))
            await safe_bot_send(
                bot, req["user_id"],
                f"✅ Payment approved! Since the item went out of stock, <code>{req['expected_usdt']} USDT</code> was added to your wallet.",
                reply_markup=kb.as_markup(),
            )
    else:
        await state.set_state(Adm.man_amt)
        await state.update_data(req_id=req_id)
        await c.answer()
        await c.message.answer(
            f"💵 Enter the <b>USDT amount</b> to credit for User <code>{req['user_id']}</code> (TxID: <code>{esc(req['txid'])}</code>):",
            parse_mode="HTML",
        )


@adm.message(Adm.man_amt, F.text)
async def adm_man_amt_in(m: Message, state: FSMContext):
    try:
        amt = q2(D(m.text.strip()))
        assert amt > 0
    except Exception:
        return await m.answer("⚠️ Enter a valid positive USDT amount (e.g. 10).")

    req_id = (await state.get_data())["req_id"]
    await state.clear()
    req = await pool.fetchrow(
        "UPDATE manual_verifications SET status='approved' WHERE id=$1 AND status!='approved' RETURNING *",
        req_id,
    )
    if not req:
        return await m.answer("Already processed.")

    await credit_deposit(req["method"], req["txid"], req["user_id"], amt)
    new_bal = await balance(req["user_id"])
    kb = InlineKeyboardBuilder()
    kb.row(Btn(text="🛍 Shop", callback_data="shop:folders", style="success"))
    await safe_bot_send(
        bot,
        req["user_id"],
        f"✅ <b>{amt} USDT has been added to your balance!</b>\n"
        f"💰 <b>Total Balance:</b> <code>{new_bal} USDT</code>",
        reply_markup=kb.as_markup(),
    )
    t_str = await bd_now()
    await send_log(
        f"✅ <b>MANUAL DEPOSIT APPROVED</b>\n\n"
        f"👤 <b>User:</b> {esc(req['full_name'])} (@{esc(req['username'] or 'N/A')})\n"
        f"🆔 <b>User ID:</b> <code>{req['user_id']}</code>\n"
        f"💳 <b>Method:</b> <code>{req['method'].upper()} (Admin Verified)</code>\n"
        f"➕ <b>Amount:</b> <code>{amt} USDT</code>\n"
        f"🧾 <b>TxID:</b> <code>{esc(req['txid'])}</code>\n"
        f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
    )
    await safe_answer(m, f"✅ Credited <b>{amt} USDT</b> to User <code>{req['user_id']}</code>!", reply_markup=adm_menu())


# Admin: Pending Timeout Order Resolution
@adm.callback_query(F.data.startswith("adm:ord:ok:") | F.data.startswith("adm:ord:ref:"), StateFilter("*"))
async def cb_adm_ord_resolve(c: CallbackQuery):
    _, _, act, oid_s = c.data.split(":")
    oid = int(oid_s)
    r = await pool.fetchrow("SELECT * FROM orders WHERE id=$1 AND status='pending'", oid)
    if not r:
        return await c.answer("Order already resolved.", show_alert=True)
    await c.answer("Resolved!")
    t_str = await bd_now()
    if act == "ok":
        await pool.execute("UPDATE orders SET status='done' WHERE id=$1", oid)
        await safe_bot_send(bot, r["user_id"], f"✅ Your order <code>{r['order_code']}</code> ({esc(r['product'])}) has been confirmed!")
        await send_log(
            f"✅ <b>PENDING ORDER CONFIRMED</b>\n\n"
            f"🆔 <b>Order ID:</b> <code>{r['order_code']}</code> (#{oid})\n"
            f"🆔 <b>User ID:</b> <code>{r['user_id']}</code>\n"
            f"📦 <b>Item:</b> {esc(r['product'])} ×{r['qty']}\n"
            f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
        )
        await safe_edit(c, (c.message.html_text or c.message.text) + "\n\n➡️ <b>MARKED DELIVERED</b>")
    else:
        await pool.execute("UPDATE orders SET status='failed' WHERE id=$1", oid)
        await credit(r["user_id"], r["cost"])
        new_bal = await balance(r["user_id"])
        kb = InlineKeyboardBuilder()
        kb.row(Btn(text="🛍 Shop", callback_data="shop:folders", style="success"))
        await safe_bot_send(
            bot, r["user_id"],
            f"💸 Order <code>{r['order_code']}</code> was refunded: <b>+{r['cost']} USDT</b>\n💰 Total Balance: <code>{new_bal} USDT</code>",
            reply_markup=kb.as_markup(),
        )
        await send_log(
            f"💸 <b>PENDING ORDER REFUNDED</b>\n\n"
            f"🆔 <b>Order ID:</b> <code>{r['order_code']}</code> (#{oid})\n"
            f"🆔 <b>User ID:</b> <code>{r['user_id']}</code>\n"
            f"💵 <b>Refunded:</b> <code>{r['cost']} USDT</code>\n"
            f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
        )
        await safe_edit(c, (c.message.html_text or c.message.text) + "\n\n➡️ <b>REFUNDED TO USER</b>")


# Admin: Interactive Balance Manager
@adm.callback_query(F.data == "adm:bal", StateFilter("*"))
async def cb_adm_bal(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Adm.bal_uid)
    await safe_edit(c, "💳 <b>Add / Deduct User Balance</b>\n\nSend the customer's <b>Telegram User ID</b>:", reply_markup=back_adm())


@adm.message(Adm.bal_uid, F.text)
async def adm_bal_uid_in(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("⚠️ Please enter a numeric Telegram User ID.")
    uid = int(m.text.strip())
    cur = await balance(uid)
    await state.set_state(Adm.bal_amt)
    await state.update_data(target_uid=uid)
    await m.answer(
        f"👤 User <code>{uid}</code> current balance: <b>{cur} USDT</b>\n\n"
        f"Enter amount to add (e.g. <code>5</code>) or deduct (e.g. <code>-2.5</code>):",
        parse_mode="HTML",
    )


@adm.message(Adm.bal_amt, F.text)
async def adm_bal_amt_in(m: Message, state: FSMContext):
    try:
        amt = q2(D(m.text.strip()))
        assert amt != 0
    except Exception:
        return await m.answer("⚠️ Enter a valid non-zero number (e.g. 5 or -2).")
    uid = (await state.get_data())["target_uid"]
    await state.clear()
    new_bal = await credit(uid, amt)
    t_str = await bd_now()
    kb = InlineKeyboardBuilder()
    kb.row(Btn(text="🛍 Shop", callback_data="shop:folders", style="success"))
    if amt > 0:
        await safe_bot_send(
            bot, uid,
            f"✅ <b>{amt} USDT has been added to your balance!</b>\n💰 <b>Total Balance:</b> <code>{new_bal} USDT</code>",
            reply_markup=kb.as_markup(),
        )
    else:
        await safe_bot_send(
            bot, uid,
            f"⚠️ <b>{abs(amt)} USDT has been deducted from your balance by admin.</b>\n💰 <b>Total Balance:</b> <code>{new_bal} USDT</code>",
            reply_markup=kb.as_markup(),
        )
    await send_log(
        f"💳 <b>ADMIN BALANCE ADJUSTMENT</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{uid}</code>\n"
        f"➕ <b>Change:</b> <code>{amt:+} USDT</code>\n"
        f"💰 <b>New Balance:</b> <code>{new_bal} USDT</code>\n"
        f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
    )
    await safe_answer(m, f"✅ Updated! User <code>{uid}</code> new balance: <b>{new_bal} USDT</b>", reply_markup=adm_menu())


# Admin: Top 3 Favourites Manager
async def build_adm_favs_view():
    ps = await get_products(True)
    p_by_id = {str(p["id"]): p for p in ps}
    b = InlineKeyboardBuilder()
    lines = ["⭐ <b>Top 3 Favourite Products</b>\n"]
    if not FAVS:
        lines.append("<i>No favourite products selected yet.</i>")
    else:
        for idx, pid in enumerate(FAVS[:3], start=1):
            p = p_by_id.get(str(pid))
            if p:
                cid = detect_category(p)
                cat_label, cat_icon, _ = CAT_MAP.get(cid, ("Others", "📦", []))
                lines.append(f"{idx}. <b>{esc(p['name'])}</b> — <code>{sell_price(p)} USDT</code> ({cat_icon} {cat_label})")
                b.row(Btn(text=f"❌ Remove #{idx}: {p['name'][:24]}", callback_data=f"adm:fav:rm:{pid}", style="danger"))
            else:
                lines.append(f"{idx}. ID <code>{esc(pid)}</code>")
                b.row(Btn(text=f"❌ Remove ID {pid}", callback_data=f"adm:fav:rm:{pid}", style="danger"))

    b.row(Btn(text="➕ Select Favourites from Product List", callback_data="adm:pl:0", style="success"))
    if FAVS:
        b.row(Btn(text="🗑 Clear All Favourites", callback_data="adm:fav:clear", style="danger"))
    b.row(Btn(text="◀️ Admin Panel", callback_data="adm", style="primary"))
    return "\n".join(lines), b.as_markup()


@adm.callback_query(F.data == "adm:favs", StateFilter("*"))
async def adm_favs_view(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    txt, kb = await build_adm_favs_view()
    await safe_edit(c, txt, reply_markup=kb)


@adm.callback_query(F.data.startswith("adm:fav:rm:"), StateFilter("*"))
async def adm_fav_rm(c: CallbackQuery, state: FSMContext):
    pid = c.data.split(":", 3)[3]
    if pid in FAVS:
        FAVS.remove(pid)
        await save_favs()
    await c.answer("Removed!")
    txt, kb = await build_adm_favs_view()
    await safe_edit(c, txt, reply_markup=kb)


@adm.callback_query(F.data == "adm:fav:clear", StateFilter("*"))
async def adm_fav_clear(c: CallbackQuery, state: FSMContext):
    FAVS.clear()
    await save_favs()
    await c.answer("Cleared!")
    txt, kb = await build_adm_favs_view()
    await safe_edit(c, txt, reply_markup=kb)


@adm.callback_query(F.data.startswith("adm:fav:tg:"), StateFilter("*"))
async def adm_fav_toggle(c: CallbackQuery):
    pid = c.data.split(":", 3)[3]
    if pid in FAVS:
        FAVS.remove(pid)
        await save_favs()
        await c.answer("Removed from Favourites!")
    else:
        if len(FAVS) >= 3:
            return await c.answer("3 favourites already set! Use Replace buttons below.", show_alert=True)
        FAVS.append(pid)
        await save_favs()
        await c.answer("Added to Top 3 Favourites!")
    await render_adm_product(c, pid)


@adm.callback_query(F.data.startswith("adm:fav:rep:"), StateFilter("*"))
async def adm_fav_replace(c: CallbackQuery):
    _, _, _, slot_s, pid = c.data.split(":", 4)
    slot = int(slot_s)
    if pid in FAVS:
        FAVS.remove(pid)
    while len(FAVS) < 3:
        FAVS.append(pid)
    FAVS[slot] = pid
    dedup = []
    for x in FAVS:
        if x not in dedup:
            dedup.append(x)
    FAVS[:] = dedup[:3]
    await save_favs()
    await c.answer(f"Set as Favourite #{slot+1}!")
    await render_adm_product(c, pid)


# Admin: Folder Options & Contact Settings
@adm.callback_query(F.data == "adm:fopt", StateFilter("*"))
async def adm_fopt(c: CallbackQuery):
    await c.answer()
    empty_st = "ON (Show all 24 folders)" if ST.get("show_empty_folders") else "OFF (Only active folders)"
    b = InlineKeyboardBuilder()
    b.row(Btn(text=f"📁 Show Empty Folders: {empty_st}", callback_data="adm:fopt:empty", style="primary"))
    b.row(Btn(text="◀️ Admin Panel", callback_data="adm", style="danger"))
    await safe_edit(c, f"📁 <b>Folder Display Options</b>\n\n<b>Show Empty Folders:</b> <code>{empty_st}</code>", reply_markup=b.as_markup())


@adm.callback_query(F.data == "adm:fopt:empty", StateFilter("*"))
async def adm_fopt_empty(c: CallbackQuery):
    ST["show_empty_folders"] = not ST.get("show_empty_folders", False)
    val = "1" if ST["show_empty_folders"] else "0"
    await pool.execute("INSERT INTO settings(key,value) VALUES('show_empty_folders',$1) ON CONFLICT(key) DO UPDATE SET value=$1", val)
    await adm_fopt(c)


@adm.callback_query(F.data == "adm:contact", StateFilter("*"))
async def adm_contact(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    num = ST.get("contact_number") or CONTACT_NUMBER or "Not set"
    w = get_whatsapp_url() or "Not set"
    b = InlineKeyboardBuilder()
    b.row(Btn(text="📞 Change Contact Number", callback_data="adm:contact:setnum", style="success"))
    b.row(Btn(text="💬 Change WhatsApp Link", callback_data="adm:contact:setwa", style="primary"))
    b.row(Btn(text="◀️ Admin Panel", callback_data="adm", style="danger"))
    txt = (
        f"📞 <b>Support Contact Settings</b>\n\n"
        f"📩 <b>Telegram:</b> @{esc(ADMIN_USERNAME or 'Not set')}\n"
        f"📞 <b>Phone:</b> <code>{esc(num)}</code>\n"
        f"💬 <b>WhatsApp:</b> <code>{esc(w)}</code>"
    )
    await safe_edit(c, txt, reply_markup=b.as_markup())


@adm.callback_query(F.data.in_({"adm:contact:setnum", "adm:contact:setwa"}), StateFilter("*"))
async def adm_contact_prompt(c: CallbackQuery, state: FSMContext):
    field = "contact_number" if c.data.endswith("setnum") else "whatsapp_link"
    await state.set_state(Adm.contact)
    await state.update_data(field=field)
    await c.message.answer(f"Send new value for <b>{field}</b>:", parse_mode="HTML")
    await c.answer()


@adm.message(Adm.contact, F.text)
async def adm_contact_save(m: Message, state: FSMContext):
    field = (await state.get_data())["field"]
    val = m.text.strip()
    ST[field] = val
    await pool.execute("INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2", field, val)
    await state.clear()
    await safe_answer(m, f"✅ Updated <b>{field}</b> to <code>{esc(val)}</code>", reply_markup=adm_menu())


# Admin: Stats, Orders, API Balance, Pricing Rule, Product Manager
TODAY = "(created AT TIME ZONE 'Asia/Dhaka')::date = (now() AT TIME ZONE 'Asia/Dhaka')::date"


async def build_adm_stats_text() -> str:
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
    top = await pool.fetch("SELECT product, sum(qty) q FROM orders WHERE status='done' GROUP BY product ORDER BY q DESC LIMIT 5")
    t = (
        f"📊 <b>Store Statistics</b>\n\n"
        f"👥 Users: <code>{r['users']}</code> | 💼 Total Balances: <code>{q2(r['bal'])} USDT</code>\n\n"
        f"<b>Today (BD):</b>\n"
        f"🛍 Orders: <code>{r['o_td']}</code> | 💵 Rev: <code>{q2(r['rev_td'])}$</code> | 📈 Profit: <code>{q2(r['pro_td'])}$</code>\n"
        f"➕ Deposits: <code>{q2(r['dep_td'])} USDT</code>\n\n"
        f"<b>All-Time:</b>\n"
        f"🛍 Orders: <code>{r['o_all']}</code> | 💵 Rev: <code>{q2(r['rev_all'])}$</code> | 📈 Profit: <code>{q2(r['pro_all'])}$</code>\n"
        f"➕ Deposits: <code>{q2(r['dep_all'])} USDT</code> | ❌ Failed: <code>{r['failed']}</code>\n\n"
        f"<b>Top Products:</b>\n"
    )
    t += "\n".join(f"• {esc(x['product'][:35])} — {x['q']}" for x in top) or "—"
    return t


@adm.callback_query(F.data == "adm:stats", StateFilter("*"))
async def adm_stats(c: CallbackQuery):
    await c.answer()
    t = await build_adm_stats_text()
    await safe_edit(c, t, reply_markup=back_adm())


@adm.callback_query(F.data == "adm:orders", StateFilter("*"))
async def adm_orders(c: CallbackQuery):
    await c.answer()
    rows = await pool.fetch(
        "SELECT id,order_code,user_id,product,qty,cost,status,created AT TIME ZONE 'Asia/Dhaka' AS t "
        "FROM orders ORDER BY id DESC LIMIT 12"
    )
    t = "🧾 <b>Recent Orders</b>\n\n" + "\n".join(
        f"{r['order_code'] or ('#' + str(r['id']))} {'✅' if r['status']=='done' else ('⏳' if r['status']=='pending' else '❌')} "
        f"{r['t']:%d/%m %H:%M} <code>{r['user_id']}</code> {esc(r['product'][:20])} ×{r['qty']} ({r['cost']}$)"
        for r in rows
    )
    await safe_edit(c, t if rows else "No orders yet.", reply_markup=back_adm())


@adm.callback_query(F.data == "adm:api", StateFilter("*"))
async def adm_api(c: CallbackQuery):
    await c.answer()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{API_BASE}/balance", headers=H(), timeout=aiohttp.ClientTimeout(total=15)) as r:
                txt = await r.text()
    except Exception as e:
        txt = str(e)
    await safe_edit(c, f"🏦 <b>Shop API Balance:</b>\n<code>{esc(txt[:500])}</code>", reply_markup=back_adm())


@adm.callback_query(F.data == "adm:rule", StateFilter("*"))
async def adm_rule(c: CallbackQuery):
    await c.answer()
    b = InlineKeyboardBuilder()
    b.row(Btn(text="✏️ Change Pricing Rule", callback_data="adm:rule:set", style="success"))
    b.row(Btn(text="♻️ Reset to Default", callback_data="adm:rule:reset", style="primary"))
    b.row(Btn(text="◀️ Admin Panel", callback_data="adm", style="danger"))
    await safe_edit(
        c,
        f"⚙️ <b>Pricing Rule</b>\n\n"
        f"Active: <code>{esc(rule_text(ST['rule']))}</code>\n"
        f"Default: <code>{esc(rule_text(ENV_RULE))}</code>\n\n"
        f"Formats: <code>a2.5</code> (+2.5 USDT), <code>p10</code> (+10%), <code>m2</code> (×2)",
        reply_markup=b.as_markup(),
    )


@adm.callback_query(F.data == "adm:rule:set", StateFilter("*"))
async def adm_rule_set(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.rule)
    await c.message.answer("Enter new pricing rule (<code>a2.5</code> / <code>p10</code> / <code>m2</code>):", parse_mode="HTML")
    await c.answer()


@adm.message(Adm.rule, F.text)
async def adm_rule_in(m: Message, state: FSMContext):
    r = parse_rule(m.text)
    if not r:
        return await m.answer("Invalid format. Use a2.5, p10, or m2.")
    await pool.execute("INSERT INTO settings(key,value) VALUES('pricing',$1) ON CONFLICT(key) DO UPDATE SET value=$1", m.text.strip())
    ST["rule"] = r
    await state.clear()
    await safe_answer(m, f"✅ Rule set to: <code>{esc(rule_text(r))}</code>", reply_markup=adm_menu())


@adm.callback_query(F.data == "adm:rule:reset", StateFilter("*"))
async def adm_rule_reset(c: CallbackQuery):
    await pool.execute("DELETE FROM settings WHERE key='pricing'")
    ST["rule"] = ENV_RULE
    await c.answer("Reset!")
    await adm_rule(c)


@adm.callback_query(F.data.startswith("adm:pl:"), StateFilter("*"))
async def adm_pl(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    await render_pl(c, int(c.data.split(":")[2]))


async def render_pl(c: CallbackQuery, page: int):
    ps = await get_products(True)
    b = InlineKeyboardBuilder()
    for p in ps[page * 10 : (page + 1) * 10]:
        pid = str(p["id"])
        ns = note_for(p)[1]
        is_fav = pid in FAVS[:3]
        mk = ("⭐" if is_fav else "") + ("⛔" if not p["ok"] else "") + ("✏️" if pid in OVR else "") + ("📝" if ns == "admin" else "")
        btn_style = "success" if is_fav else ("danger" if not p["ok"] else "primary")
        b.row(Btn(text=f"{mk} {p['name'][:25]} | {p['price']}→{sell_price(p)}$", callback_data=f"adm:p:{pid}", style=btn_style))
    nav = []
    if page > 0:
        nav.append(Btn(text="◀️ Prev", callback_data=f"adm:pl:{page-1}", style="primary"))
    if (page + 1) * 10 < len(ps):
        nav.append(Btn(text="▶️ Next", callback_data=f"adm:pl:{page+1}", style="primary"))
    if nav:
        b.row(*nav)
    b.row(Btn(text="⭐ Top 3 Favourites", callback_data="adm:favs", style="success"))
    b.row(Btn(text="♻️ Reset All Manual Prices", callback_data="adm:prall", style="danger"))
    b.row(Btn(text="◀️ Admin Panel", callback_data="adm", style="primary"))
    await safe_edit(c, f"💲 <b>Product Manager (Page {page+1}/{max(1, (len(ps)-1)//10+1)})</b>", reply_markup=b.as_markup())


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
        b.row(Btn(text=f"🌟 Remove Favourite (#{fav_rank})", callback_data=f"adm:fav:tg:{pid}", style="danger"))
    else:
        if len(FAVS) < 3:
            b.row(Btn(text=f"⭐ Add to Top 3 Favourites ({len(FAVS)}/3)", callback_data=f"adm:fav:tg:{pid}", style="success"))
        else:
            b.row(
                Btn(text="⭐ Replace #1", callback_data=f"adm:fav:rep:0:{pid}", style="success"),
                Btn(text="⭐ Replace #2", callback_data=f"adm:fav:rep:1:{pid}", style="success"),
                Btn(text="⭐ Replace #3", callback_data=f"adm:fav:rep:2:{pid}", style="success"),
            )

    b.row(
        Btn(text="✏️ Set Price", callback_data=f"adm:ps:{pid}", style="primary"),
        Btn(text="♻️ Reset Price", callback_data=f"adm:pr:{pid}", style="primary"),
    )
    b.row(
        Btn(text="📝 Set Note", callback_data=f"adm:ns:{pid}", style="primary"),
        Btn(text="🗑 Reset Note", callback_data=f"adm:nr:{pid}", style="primary"),
    )
    b.row(Btn(text=f"📁 Folder: {cat_icon} {cat_label}", callback_data=f"adm:cat:{pid}", style="primary"))
    b.row(Btn(text="◀️ Product List", callback_data="adm:pl:0", style="danger"))

    st = "Unlimited" if p["stock"] is None else p["stock"]
    note, src = note_for(p)
    await safe_edit(
        c,
        f"📦 <b>{esc(p['name'])}</b>\n"
        f"ID: <code>{pid}</code> | Stock: <code>{st}</code>\n"
        f"Folder: <b>{cat_icon} {esc(cat_label)}</b>\n"
        f"Base: <code>{p['price']}$</code> | Selling: <b>{sell_price(p)}$</b> {'(Manual)' if man is not None else '(Auto)'}\n\n"
        f"Note ({src}):\n<blockquote>{esc(note[:500]) if note else '—'}</blockquote>",
        reply_markup=b.as_markup(),
    )


@adm.callback_query(F.data.startswith("adm:p:"), StateFilter("*"))
async def adm_p(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    await render_adm_product(c, c.data.split(":")[2])


@adm.callback_query(F.data.startswith("adm:cat:"), StateFilter("*"))
async def adm_cat_select(c: CallbackQuery):
    await c.answer()
    pid = c.data.split(":")[2]
    b = InlineKeyboardBuilder()
    btns = [Btn(text=f"{icon} {label}", callback_data=f"adm:setcat:{pid}:{cid}", style="primary") for cid, label, icon, _ in CATEGORIES]
    for i in range(0, len(btns), 3):
        b.row(*btns[i : i + 3])
    b.row(Btn(text="♻️ Auto-Detect", callback_data=f"adm:setcat:{pid}:auto", style="success"))
    b.row(Btn(text="◀️ Back", callback_data=f"adm:p:{pid}", style="danger"))
    await safe_edit(c, f"📁 <b>Select Folder for Product <code>{esc(pid)}</code>:</b>", reply_markup=b.as_markup())


@adm.callback_query(F.data.startswith("adm:setcat:"), StateFilter("*"))
async def adm_cat_save(c: CallbackQuery):
    _, _, pid, cid = c.data.split(":", 3)
    if cid == "auto":
        await pool.execute("DELETE FROM product_categories WHERE pid=$1", pid)
        CAT_OVR.pop(pid, None)
        await c.answer("Reset to Auto-Detect!")
    else:
        await pool.execute("INSERT INTO product_categories(pid,cat_id) VALUES($1,$2) ON CONFLICT(pid) DO UPDATE SET cat_id=$2", pid, cid)
        CAT_OVR[pid] = cid
        await c.answer("Folder updated!")
    await render_adm_product(c, pid)


@adm.callback_query(F.data.startswith("adm:ps:"), StateFilter("*"))
async def adm_ps(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.price)
    await state.update_data(pid=c.data.split(":")[2])
    await c.message.answer("Enter new selling price in USDT (e.g. 3.5):")
    await c.answer()


@adm.message(Adm.price, F.text)
async def adm_price_in(m: Message, state: FSMContext):
    try:
        v = q2(D(m.text.strip()))
        assert v > 0
    except Exception:
        return await m.answer("Enter a valid positive price.")
    pid = (await state.get_data())["pid"]
    await pool.execute("INSERT INTO overrides(pid,price) VALUES($1,$2) ON CONFLICT(pid) DO UPDATE SET price=$2", pid, v)
    OVR[pid] = v
    await state.clear()
    await safe_answer(m, f"✅ Price set to <code>{v} USDT</code>", reply_markup=adm_menu())


@adm.callback_query(F.data.startswith("adm:pr:"), StateFilter("*"))
async def adm_pr(c: CallbackQuery):
    pid = c.data.split(":")[2]
    await pool.execute("DELETE FROM overrides WHERE pid=$1", pid)
    OVR.pop(pid, None)
    await c.answer("Price reset!")
    await render_adm_product(c, pid)


@adm.callback_query(F.data == "adm:prall", StateFilter("*"))
async def adm_prall(c: CallbackQuery):
    await pool.execute("DELETE FROM overrides")
    OVR.clear()
    await c.answer("All manual prices reset!")
    await render_pl(c, 0)


@adm.callback_query(F.data.startswith("adm:ns:"), StateFilter("*"))
async def adm_ns(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.note)
    await state.update_data(pid=c.data.split(":")[2])
    await c.message.answer("Send custom note for this product (max 1500 chars):")
    await c.answer()


@adm.message(Adm.note, F.text)
async def adm_note_in(m: Message, state: FSMContext):
    txt = m.text.strip()
    if not txt or len(txt) > 1500:
        return await m.answer("Note must be 1–1500 characters.")
    pid = (await state.get_data())["pid"]
    await pool.execute("INSERT INTO notes(pid,note) VALUES($1,$2) ON CONFLICT(pid) DO UPDATE SET note=$2", pid, txt)
    NOTES[pid] = txt
    await state.clear()
    await safe_answer(m, "✅ Note saved!", reply_markup=adm_menu())


@adm.callback_query(F.data.startswith("adm:nr:"), StateFilter("*"))
async def adm_nr(c: CallbackQuery):
    pid = c.data.split(":")[2]
    await pool.execute("DELETE FROM notes WHERE pid=$1", pid)
    NOTES.pop(pid, None)
    await c.answer("Note reset!")
    await render_adm_product(c, pid)


async def apply_and_notify_user_pricing(uid: int, raw_rule: str) -> tuple[bool, str]:
    """Saves or resets custom pricing rule for a specific user and sends instant notification."""
    raw = raw_rule.strip().lower()
    t_str = await bd_now()
    kb = InlineKeyboardBuilder()
    kb.row(Btn(text="🛍 Browse Products", callback_data="shop:folders", style="success"))

    if raw in ("reset", "off", "none", "default", "0", "a0", "p0", "m1"):
        await pool.execute("DELETE FROM user_pricing WHERE user_id=$1", uid)
        USER_RULES.pop(uid, None)
        await safe_bot_send(
            bot,
            uid,
            "ℹ️ <b>Pricing Update Notice</b>\n\nYour account pricing has been reset to standard store rates.",
            reply_markup=kb.as_markup(),
        )
        await send_log(
            f"♻️ <b>USER PRICING RESET</b>\n\n"
            f"🆔 <b>User ID:</b> <code>{uid}</code>\n"
            f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
        )
        return True, f"♻️ Custom pricing rule reset for User <code>{uid}</code>."

    r = parse_rule(raw)
    if not r:
        return False, "⚠️ Invalid rule format! Examples:\n• <code>a-1</code> (-1 USDT discount)\n• <code>a1.5</code> (+1.5 USDT increase)\n• <code>p-10</code> (-10% discount)\n• <code>m0.9</code> (×0.9 multiplier)\n• <code>reset</code> (remove custom rule)"

    direction, amt_text = user_rule_change_info(r)
    if direction == "neutral":
        return await apply_and_notify_user_pricing(uid, "reset")

    await pool.execute(
        "INSERT INTO user_pricing(user_id,rule,updated) VALUES($1,$2,now()) "
        "ON CONFLICT(user_id) DO UPDATE SET rule=$2, updated=now()",
        uid, raw,
    )
    USER_RULES[uid] = r

    if direction == "discount":
        await safe_bot_send(
            bot,
            uid,
            f"🎉 <b>Congratulations! Exclusive Discount Activated!</b>\n\n"
            f"💎 Admin has reduced your price by <b>{amt_text}</b> across all products in our store!\n"
            f"✨ Your special VIP pricing is now live on every item.",
            reply_markup=kb.as_markup(),
        )
    else:
        await safe_bot_send(
            bot,
            uid,
            f"⚠️ <b>Sorry! Pricing Update Notice</b>\n\n"
            f"📈 Your account pricing has been increased by <b>{amt_text}</b> across all products.",
            reply_markup=kb.as_markup(),
        )

    await send_log(
        f"🏷 <b>CUSTOM USER PRICING SET</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{uid}</code>\n"
        f"⚙️ <b>Rule:</b> <code>{esc(rule_text(r))}</code> ({direction.upper()}: {amt_text})\n"
        f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
    )
    return True, f"✅ Custom pricing for User <code>{uid}</code> set to <b>{esc(rule_text(r))}</b> ({direction}: {amt_text}). Customer has been notified!"


@adm.message(Command("pricingm"), StateFilter("*"))
async def cmd_pricingm(m: Message, state: FSMContext):
    await state.clear()
    parts = m.text.strip().split()
    if len(parts) != 3 or not parts[1].isdigit():
        return await m.answer(
            "⚙️ <b>Set Specific Customer Pricing</b>\n\n"
            "<b>Usage:</b> <code>/pricingm USER_ID RULE</code>\n"
            "• Discount 1 USDT: <code>/pricingm 6927361223 a-1</code>\n"
            "• Discount 10%: <code>/pricingm 6927361223 p-10</code>\n"
            "• Multiply ×0.85: <code>/pricingm 6927361223 m0.85</code>\n"
            "• Increase 1 USDT: <code>/pricingm 6927361223 a1</code>\n"
            "• Reset user: <code>/pricingm 6927361223 reset</code>",
            parse_mode="HTML",
            reply_markup=adm_menu(),
        )
    uid = int(parts[1])
    ok, msg = await apply_and_notify_user_pricing(uid, parts[2])
    await safe_answer(m, msg, reply_markup=adm_menu())


async def build_adm_uprice_view():
    rows = await pool.fetch(
        "SELECT up.user_id, up.rule, u.name, u.username "
        "FROM user_pricing up LEFT JOIN users u ON u.id = up.user_id "
        "ORDER BY up.updated DESC LIMIT 20"
    )
    b = InlineKeyboardBuilder()
    lines = [
        "👥 <b>Custom Customer Pricing Rules</b>\n",
        "Command shortcut: <code>/pricingm USER_ID a-1</code>\n",
    ]
    if not rows:
        lines.append("<i>No custom customer pricing rules set right now.</i>")
    else:
        for r in rows:
            uid = r["user_id"]
            pr = parse_rule(r["rule"])
            r_txt = rule_text(pr) if pr else r["rule"]
            u_label = f"{r['name'] or 'User'} (@{r['username']})" if r["username"] else (r["name"] or f"User {uid}")
            lines.append(f"• <code>{uid}</code> {esc(u_label[:22])} → <b>{esc(r_txt)}</b>")
            b.row(Btn(text=f"❌ Reset {uid} ({r['rule']})", callback_data=f"adm:up:rm:{uid}", style="danger"))

    b.row(Btn(text="➕ Add / Update Customer Rule", callback_data="adm:up:add", style="success"))
    if rows:
        b.row(Btn(text="♻️ Reset All Custom User Rules", callback_data="adm:up:clear", style="danger"))
    b.row(Btn(text="◀️ Admin Panel", callback_data="adm", style="primary"))
    return "\n".join(lines), b.as_markup()


@adm.callback_query(F.data == "adm:uprice", StateFilter("*"))
async def cb_adm_uprice(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    txt, kb = await build_adm_uprice_view()
    await safe_edit(c, txt, reply_markup=kb)


@adm.callback_query(F.data == "adm:up:add", StateFilter("*"))
async def cb_adm_up_add(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Adm.up_uid)
    await safe_edit(
        c,
        "👥 <b>Set Customer Custom Pricing</b>\n\nSend the customer's numeric <b>Telegram User ID</b>:",
        reply_markup=back_adm(),
    )


@adm.message(Adm.up_uid, F.text)
async def adm_up_uid_in(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("⚠️ Please enter a valid numeric Telegram User ID.")
    uid = int(m.text.strip())
    cur = USER_RULES.get(uid)
    cur_txt = rule_text(cur) if cur else "Standard Store Pricing"
    await state.set_state(Adm.up_rule)
    await state.update_data(target_uid=uid)
    await m.answer(
        f"👤 User <code>{uid}</code> current rule: <b>{esc(cur_txt)}</b>\n\n"
        f"Send the rule to apply on top of store prices:\n"
        f"• <code>a-1</code> (Reduce price by 1 USDT)\n"
        f"• <code>p-10</code> (Reduce price by 10%)\n"
        f"• <code>m0.9</code> (Multiply price by 0.9)\n"
        f"• <code>a1</code> (Increase price by 1 USDT)\n"
        f"• <code>reset</code> (Reset to standard pricing)",
        parse_mode="HTML",
    )


@adm.message(Adm.up_rule, F.text)
async def adm_up_rule_in(m: Message, state: FSMContext):
    uid = (await state.get_data())["target_uid"]
    ok, msg = await apply_and_notify_user_pricing(uid, m.text.strip())
    if not ok:
        return await m.answer(msg, parse_mode="HTML")
    await state.clear()
    txt, kb = await build_adm_uprice_view()
    await safe_answer(m, msg + "\n\n" + txt, reply_markup=kb)


@adm.callback_query(F.data.startswith("adm:up:rm:"), StateFilter("*"))
async def cb_adm_up_rm(c: CallbackQuery):
    uid = int(c.data.split(":")[3])
    await c.answer("Reset!")
    await apply_and_notify_user_pricing(uid, "reset")
    txt, kb = await build_adm_uprice_view()
    await safe_edit(c, txt, reply_markup=kb)


@adm.callback_query(F.data == "adm:up:clear", StateFilter("*"))
async def cb_adm_up_clear(c: CallbackQuery):
    await pool.execute("DELETE FROM user_pricing")
    USER_RULES.clear()
    await c.answer("All custom user pricing rules reset!")
    txt, kb = await build_adm_uprice_view()
    await safe_edit(c, txt, reply_markup=kb)


@adm.message(Command("addbal"), StateFilter("*"))
async def addbal(m: Message, state: FSMContext):
    await state.clear()
    try:
        _, uid_s, amt_s = m.text.split()
        uid, amt = int(uid_s), q2(D(amt_s))
        nb = await credit(uid, amt)
        t_str = await bd_now()
        await m.answer(f"✅ Updated! User <code>{uid}</code> balance: <b>{nb} USDT</b>", parse_mode="HTML")
        kb = InlineKeyboardBuilder()
        kb.row(Btn(text="🛍 Shop", callback_data="shop:folders", style="success"))
        if amt > 0:
            await safe_bot_send(
                bot, uid,
                f"✅ <b>{amt} USDT has been added to your balance!</b>\n💰 <b>Total Balance:</b> <code>{nb} USDT</code>",
                reply_markup=kb.as_markup(),
            )
        else:
            await safe_bot_send(
                bot, uid,
                f"⚠️ <b>{abs(amt)} USDT has been deducted from your balance by admin.</b>\n💰 <b>Total Balance:</b> <code>{nb} USDT</code>",
                reply_markup=kb.as_markup(),
            )
        await send_log(
            f"💳 <b>ADMIN BALANCE ADJUSTMENT</b>\n\n"
            f"🆔 <b>User ID:</b> <code>{uid}</code>\n"
            f"➕ <b>Change:</b> <code>{amt:+} USDT</code>\n"
            f"💰 <b>New Balance:</b> <code>{nb} USDT</code>\n"
            f"🕒 <b>Time (BD):</b> <code>{t_str}</code>"
        )
    except Exception:
        await m.answer("Usage: <code>/addbal USER_ID AMOUNT</code>", parse_mode="HTML")


@adm.message()
async def adm_fallback_msg(m: Message, state: FSMContext):
    await state.clear()
    await safe_answer(
        m,
        "🛠 <b>Admin Control Panel</b>\nManage products, Top 3 Favourites, payments, balances, and store settings:",
        reply_markup=adm_menu(),
    )


adm_dp.include_router(adm)


# Log Bot start handler so admins can initialize chat with the Log Bot
@log_dp.message(CommandStart())
async def log_bot_start(m: Message):
    if m.from_user and m.from_user.id in ADMINS:
        await m.answer("📋 <b>Log Bot Active!</b>\nAll store events (new users, verifications, deposits, and purchases) will be sent here automatically.", parse_mode="HTML")
    else:
        await m.answer("⛔ Access restricted.")


# Global error handlers
@dp.error()
async def customer_error_handler(event: ErrorEvent):
    logging.error("Customer bot error: %s", event.exception, exc_info=True)
    return True


@adm_dp.error()
async def admin_error_handler(event: ErrorEvent):
    logging.error("Admin bot error: %s", event.exception, exc_info=True)
    return True


@log_dp.error()
async def log_error_handler(event: ErrorEvent):
    logging.error("Log bot error: %s", event.exception, exc_info=True)
    return True


# Slash commands setup (clears any previously cached /admin command from Customer Bot)
async def setup_bot_commands():
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
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
        await bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
        await bot.set_my_commands(user_cmds, scope=BotCommandScopeAllPrivateChats())
        for aid in ADMINS:
            try:
                await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=aid))
                await bot.set_my_commands(user_cmds, scope=BotCommandScopeChat(chat_id=aid))
            except Exception:
                pass
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        logging.warning("Customer bot commands setup warning: %s", e)

    if ADMIN_BOT_TOKEN and admin_bot and ADMIN_BOT_TOKEN != BOT_TOKEN:
        admin_cmds = [
            BotCommand(command="admin", description="Open Admin Control Panel"),
            BotCommand(command="pricingm", description="Set user pricing (/pricingm ID a-1)"),
            BotCommand(command="addbal", description="Add or deduct user balance"),
            BotCommand(command="start", description="Open Admin Control Panel"),
        ]
        try:
            await admin_bot.set_my_commands(admin_cmds, scope=BotCommandScopeDefault())
            for aid in ADMINS:
                try:
                    await admin_bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=aid))
                except Exception:
                    pass
            await admin_bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception as e:
            logging.warning("Admin bot commands setup warning: %s", e)


# Health check server and multi-bot polling runner
async def health(_):
    return web.Response(text="ok")


async def main():
    await init_db()
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(E("PORT", "10000"))).start()

    await setup_bot_commands()
    await bot.delete_webhook(drop_pending_updates=True)

    upd_types = ["message", "callback_query"]
    tasks = [dp.start_polling(bot, allowed_updates=upd_types, handle_signals=False)]
    if ADMIN_BOT_TOKEN and ADMIN_BOT_TOKEN != BOT_TOKEN:
        await admin_bot.delete_webhook(drop_pending_updates=True)
        logging.info("Starting Admin Bot polling for ADMINS=%s", ADMINS)
        tasks.append(adm_dp.start_polling(admin_bot, allowed_updates=upd_types, handle_signals=False))
    else:
        logging.warning("ADMIN_BOT_TOKEN not set or same as BOT_TOKEN; attaching admin router to main bot")
        dp.include_router(adm)

    if LOG_BOT_TOKEN and LOG_BOT_TOKEN not in (BOT_TOKEN, ADMIN_BOT_TOKEN):
        await log_bot.delete_webhook(drop_pending_updates=True)
        logging.info("Starting Log Bot polling")
        tasks.append(log_dp.start_polling(log_bot, allowed_updates=upd_types, handle_signals=False))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
