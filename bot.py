import os, re, hmac, hashlib, time, uuid, json, logging, asyncio, html as htmllib
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode, quote

import aiohttp, asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton as Btn
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
E = os.environ.get
D = lambda x: Decimal(str(x))
q2 = lambda x: D(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
esc = lambda t: htmllib.escape(str(t))

# ================= CONFIG (সব Render Environment Variable থেকে) =================
BOT_TOKEN = E("BOT_TOKEN")
DATABASE_URL = (E("DATABASE_URL") or "").split("?")[0]
API_BASE = E("SHOP_API_BASE", "https://safwantigershopbot-production.up.railway.app/api").rstrip("/")
API_KEY = E("SHOP_KEY")
ADMINS = [int(x) for x in re.split(r"[,\s]+", E("ADMIN_IDS", "")) if x.strip().isdigit()]
ADMIN_USERNAME = (E("ADMIN_USERNAME") or "").lstrip("@")
PRICING_ENV = E("PRICING", "a2.5")
BINANCE_KEY, BINANCE_SECRET, BINANCE_PAY_ID = E("BINANCE_API_KEY"), E("BINANCE_API_SECRET"), E("BINANCE_PAY_ID")
BYBIT_KEY, BYBIT_SECRET, BYBIT_UID = E("BYBIT_API_KEY"), E("BYBIT_API_SECRET"), E("BYBIT_UID")
BKASH_NUMBER = E("BKASH_NUMBER")
WHATSAPP_LINK = E("WHATSAPP_LINK")  # যেমন https://wa.me/8801XXXXXXXXX
BDT_RATE = D(E("BDT_RATE", "125"))  # ১ USDT = কত টাকা
MIN_DEPOSIT = D(E("MIN_DEPOSIT", "1"))

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
adm = Router()
adm.message.filter(F.from_user.id.in_(ADMINS))
adm.callback_query.filter(F.from_user.id.in_(ADMINS))
pool: asyncpg.Pool = None


class Dep(StatesGroup):
    txid = State()


class Qty(StatesGroup):
    n = State()


class Bk(StatesGroup):
    info = State()


class Adm(StatesGroup):
    price = State()
    rule = State()
    note = State()


# ================= PRICING =================
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
    return {"a": f"a{v} → দামের সাথে +{v} USDT", "p": f"p{v} → দামের সাথে +{v}%", "m": f"m{v} → দাম × {v}"}[k]


ENV_RULE = parse_rule(PRICING_ENV) or ("a", D("2.5"))
ST = {"rule": ENV_RULE}  # বর্তমান গ্লোবাল রুল
OVR = {}  # product_id(str) -> manual price
NOTES = {}  # product_id(str) -> অ্যাডমিনের কাস্টম নোট


def contact_btns(ctx=""):
    """Telegram + WhatsApp কন্টাক্ট বাটন (লিংক Render Env থেকে)"""
    row = []
    if ADMIN_USERNAME:
        u = f"https://t.me/{ADMIN_USERNAME}" + (f"?text={quote(ctx)}" if ctx else "")
        row.append(Btn(text="📩 Telegram Admin", url=u))
    if WHATSAPP_LINK:
        u = WHATSAPP_LINK
        if ctx and "wa.me" in u:
            u += ("&" if "?" in u else "?") + "text=" + quote(ctx)
        row.append(Btn(text="💬 WhatsApp", url=u))
    return row


def add_contact(b, ctx=""):
    r = contact_btns(ctx)
    if r:
        b.row(*r)


def note_for(p):
    """নোটের অগ্রাধিকার: ১) অ্যাডমিনের নোট ২) সাফওয়ানের warranty+description ৩) নেই"""
    n = NOTES.get(str(p["id"]))
    if n:
        return n, "admin"
    parts = []
    w, d = clean(p["warranty"], 350), clean(p["desc"], 600)
    if w:
        parts.append("🛡 " + w)
    if d:
        parts.append(d)
    return ("\n\n".join(parts), "safwan") if parts else ("", "none")


def sell_price(p):
    o = OVR.get(str(p["id"]))
    return o if o is not None else apply_rule(p["price"], ST["rule"])


# ================= DB =================
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, ssl="require", statement_cache_size=0, min_size=1, max_size=5)
    await pool.execute("""
    CREATE TABLE IF NOT EXISTS users(id BIGINT PRIMARY KEY, balance NUMERIC(18,2) NOT NULL DEFAULT 0,
      username TEXT, name TEXT, created TIMESTAMPTZ DEFAULT now());
    CREATE TABLE IF NOT EXISTS deposits(id SERIAL PRIMARY KEY, method TEXT, txid TEXT, user_id BIGINT,
      amount NUMERIC(18,2), created TIMESTAMPTZ DEFAULT now(), UNIQUE(method, txid));
    CREATE TABLE IF NOT EXISTS orders(id SERIAL PRIMARY KEY, user_id BIGINT, pid TEXT, product TEXT, qty INT,
      cost NUMERIC(18,2), base_cost NUMERIC(18,2), request_id TEXT, status TEXT, delivery TEXT,
      created TIMESTAMPTZ DEFAULT now());
    CREATE TABLE IF NOT EXISTS overrides(pid TEXT PRIMARY KEY, price NUMERIC(18,2));
    CREATE TABLE IF NOT EXISTS notes(pid TEXT PRIMARY KEY, note TEXT);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS bkash(id SERIAL PRIMARY KEY, user_id BIGINT, trx TEXT UNIQUE,
      amount_bdt NUMERIC(18,2), status TEXT DEFAULT 'pending', created TIMESTAMPTZ DEFAULT now());
    """)
    for r in await pool.fetch("SELECT pid, price FROM overrides"):
        OVR[r["pid"]] = r["price"]
    for r in await pool.fetch("SELECT pid, note FROM notes"):
        NOTES[r["pid"]] = r["note"]
    v = await pool.fetchval("SELECT value FROM settings WHERE key='pricing'")
    if v and parse_rule(v):
        ST["rule"] = parse_rule(v)


async def ensure_user(u):
    await pool.execute(
        "INSERT INTO users(id,username,name) VALUES($1,$2,$3) ON CONFLICT(id) DO UPDATE SET username=$2, name=$3",
        u.id, u.username, u.full_name)


async def balance(uid):
    await pool.execute("INSERT INTO users(id) VALUES($1) ON CONFLICT DO NOTHING", uid)
    return await pool.fetchval("SELECT balance FROM users WHERE id=$1", uid)


async def deduct(uid, amt):
    return await pool.fetchval("UPDATE users SET balance=balance-$2 WHERE id=$1 AND balance>=$2 RETURNING balance", uid, amt)


async def credit(uid, amt):
    await balance(uid)
    return await pool.fetchval("UPDATE users SET balance=balance+$2 WHERE id=$1 RETURNING balance", uid, amt)


async def credit_deposit(method, txid, uid, amt):
    """একই txid দুইবার ব্যবহার করা যাবে না"""
    await balance(uid)
    async with pool.acquire() as c:
        async with c.transaction():
            ins = await c.fetchval("INSERT INTO deposits(method,txid,user_id,amount) VALUES($1,$2,$3,$4) "
                                   "ON CONFLICT DO NOTHING RETURNING id", method, txid, uid, amt)
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


# ================= Shop API =================
def H():
    return {"Authorization": f"Bearer {API_KEY}"}


_cache = {"t": 0, "d": []}
TAG = re.compile(r"<[^>]+>")
CE = re.compile(r"\{\{ce:\d+\|(.*?)\}\}")


def clean(t, n=500):
    if not t:
        return ""
    t = CE.sub(r"\1", t)
    t = TAG.sub("", t)
    t = htmllib.unescape(t).replace("*", "").strip()
    return t[:n] + ("…" if len(t) > n else "")


async def get_products(force=False):
    if not force and time.time() - _cache["t"] < 15:
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
        unlimited = bool(p.get("unlimited_stock"))
        stock = None if unlimited else int(p.get("stock") or 0)
        ok = bool(p.get("available", True)) and (unlimited or stock > 0)
        out.append({"id": pid, "name": p.get("name") or f"Product {pid}", "price": D(price), "stock": stock,
                    "ok": ok, "warranty": p.get("warranty"), "desc": p.get("description")})
    out.sort(key=lambda x: x["name"].lower())
    _cache.update(t=time.time(), d=out)
    return out


async def find(pid, force=False):
    for p in await get_products(force):
        if str(p["id"]) == str(pid):
            return p


async def api_order(pid, qty, rid):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{API_BASE}/order", headers=H(),
                          json={"product_id": pid, "quantity": qty, "request_id": rid},
                          timeout=aiohttp.ClientTimeout(total=60)) as r:
            try:
                data = await r.json()
            except Exception:
                data = {"error": await r.text()}
            return r.status, data


# ================= Auto verify =================
async def verify_binance(txid):
    now = int(time.time() * 1000)
    params = {"startTimestamp": now - 86400000, "endTimestamp": now, "limit": 100, "timestamp": now, "recvWindow": 20000}
    qs = urlencode(params)
    sig = hmac.new(BINANCE_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://api.binance.com/sapi/v1/pay/transactions?{qs}&signature={sig}",
                         headers={"X-MBX-APIKEY": BINANCE_KEY}) as r:
            data = await r.json()
    logging.info("binance resp: %s", str(data)[:300])
    for t in data.get("data") or []:
        ids = {str(t.get("orderId", "")), str(t.get("transactionId", ""))}
        if txid in ids and t.get("currency") == "USDT" and D(t.get("amount", 0)) > 0:
            return q2(t["amount"])
    return None


async def verify_bybit(txid):
    now = int(time.time() * 1000)
    qs = urlencode({"coin": "USDT", "startTime": now - 7 * 86400000 + 60000, "endTime": now, "limit": 50})
    ts, rw = str(now), "20000"
    sig = hmac.new(BYBIT_SECRET.encode(), (ts + BYBIT_KEY + rw + qs).encode(), hashlib.sha256).hexdigest()
    hdr = {"X-BAPI-API-KEY": BYBIT_KEY, "X-BAPI-TIMESTAMP": ts, "X-BAPI-SIGN": sig, "X-BAPI-RECV-WINDOW": rw}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://api.bybit.com/v5/asset/deposit/query-internal-record?{qs}", headers=hdr) as r:
            data = await r.json()
    logging.info("bybit resp: %s", str(data)[:300])
    for t in (data.get("result") or {}).get("rows") or []:
        if str(t.get("id")) == txid and t.get("coin") == "USDT" and str(t.get("status")) == "2" \
                and int(t.get("type", 1)) == 1:
            return q2(t["amount"])
    return None


# ================= Customer UI =================
def main_menu(uid):
    b = InlineKeyboardBuilder()
    b.button(text="🛒 Shop", callback_data="shop:0")
    b.button(text="💰 Wallet", callback_data="wallet")
    b.button(text="➕ Deposit", callback_data="deposit")
    b.button(text="📜 My Orders", callback_data="myorders")
    if uid in ADMINS:
        b.button(text="🛠 Admin", callback_data="adm")
    b.adjust(1, 2, 1, 1)
    add_contact(b)
    return b.as_markup()


@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    bal = await balance(m.from_user.id)
    await m.answer(f"👋 স্বাগতম!\n💰 ব্যালেন্স: {bal} USDT", reply_markup=main_menu(m.from_user.id))


@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("বাতিল করা হয়েছে।", reply_markup=main_menu(m.from_user.id))


@dp.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await balance(c.from_user.id)
    await c.message.edit_text(f"🏠 মেনু\n💰 ব্যালেন্স: {bal} USDT", reply_markup=main_menu(c.from_user.id))


@dp.callback_query(F.data == "wallet")
async def wallet(c: CallbackQuery):
    bal = await balance(c.from_user.id)
    b = InlineKeyboardBuilder()
    b.button(text="➕ Deposit", callback_data="deposit")
    b.button(text="⬅️ Back", callback_data="home")
    b.adjust(1)
    await c.message.edit_text(f"💰 আপনার ব্যালেন্স: {bal} USDT", reply_markup=b.as_markup())


@dp.callback_query(F.data == "myorders")
async def myorders(c: CallbackQuery):
    rows = await pool.fetch("SELECT id,product,qty,cost,created FROM orders WHERE user_id=$1 AND status='done' "
                            "ORDER BY id DESC LIMIT 8", c.from_user.id)
    b = InlineKeyboardBuilder()
    for r in rows:
        b.row(Btn(text=f"{r['product'][:30]} ×{r['qty']} — {r['cost']}$", callback_data=f"o:{r['id']}"))
    b.row(Btn(text="⬅️ Back", callback_data="home"))
    await c.message.edit_text("📜 আপনার শেষ অর্ডারগুলো:" if rows else "এখনো কোনো অর্ডার নেই।", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("o:"))
async def order_view(c: CallbackQuery):
    r = await pool.fetchrow("SELECT product,qty,cost,delivery,created FROM orders WHERE id=$1 AND user_id=$2",
                            int(c.data[2:]), c.from_user.id)
    if not r:
        return await c.answer("পাওয়া যায়নি", show_alert=True)
    b = InlineKeyboardBuilder(); b.button(text="⬅️ Back", callback_data="myorders")
    await c.message.edit_text(f"📦 {esc(r['product'])} ×{r['qty']}\n💵 {r['cost']} USDT\n\n"
                              f"<code>{esc((r['delivery'] or '')[:3500])}</code>", parse_mode="HTML", reply_markup=b.as_markup())


# ---- Shop ----
PAGE = 8


@dp.callback_query(F.data.startswith("shop:"))
async def shop(c: CallbackQuery):
    page = int(c.data.split(":")[1])
    ps = [p for p in await get_products() if p["ok"]]
    if not ps:
        return await c.answer("এখন কোনো প্রোডাক্ট নেই / লোড হয়নি", show_alert=True)
    b = InlineKeyboardBuilder()
    for p in ps[page * PAGE:(page + 1) * PAGE]:
        stk = "∞" if p["stock"] is None else p["stock"]
        b.row(Btn(text=f"{p['name'][:32]} — {sell_price(p)}$ [{stk}]", callback_data=f"p:{p['id']}"))
    nav = []
    if page > 0:
        nav.append(Btn(text="◀️", callback_data=f"shop:{page-1}"))
    if (page + 1) * PAGE < len(ps):
        nav.append(Btn(text="▶️", callback_data=f"shop:{page+1}"))
    if nav:
        b.row(*nav)
    b.row(Btn(text="⬅️ Back", callback_data="home"))
    await c.message.edit_text(f"🛒 প্রোডাক্ট লিস্ট (পেজ {page+1}/{(len(ps)-1)//PAGE+1})\n[ ] ভেতরে = স্টক", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("p:"))
async def product(c: CallbackQuery):
    p = await find(c.data[2:], force=True)
    if not p or not p["ok"]:
        return await c.answer("❌ স্টক শেষ বা প্রোডাক্ট নেই", show_alert=True)
    b = InlineKeyboardBuilder()
    for n in (1, 2, 5, 10):
        if p["stock"] is None or n <= p["stock"]:
            b.button(text=f"{n}x", callback_data=f"c:{p['id']}:{n}")
    b.adjust(4)
    b.row(Btn(text="✏️ অন্য সংখ্যা", callback_data=f"cq:{p['id']}"))
    add_contact(b, f"প্রোডাক্ট নিয়ে প্রশ্ন: {p['name']}")
    b.row(Btn(text="⬅️ Back", callback_data="shop:0"))
    st = "Unlimited" if p["stock"] is None else p["stock"]
    note, _ = note_for(p)
    txt = (f"📦 <b>{esc(p['name'])}</b>\n💵 দাম: <b>{sell_price(p)} USDT</b>\n📊 স্টক: {st} টি\n\n"
           f"📝 <b>কেনার আগে নোট পড়ুন:</b>\n{esc(note) if note else 'কোনো বিশেষ নোট নেই।'}\n\nকতগুলো কিনবেন?")
    await c.message.edit_text(txt, parse_mode="HTML", reply_markup=b.as_markup())


def confirm_view(p, qty):
    total = sell_price(p) * qty
    b = InlineKeyboardBuilder()
    b.button(text="✅ নিশ্চিত করুন", callback_data=f"buy:{p['id']}:{qty}:{int(total*100)}")
    b.button(text="❌ বাতিল", callback_data="shop:0")
    b.adjust(2)
    add_contact(b, f"অর্ডার নিয়ে প্রশ্ন: {p['name']} ×{qty}")
    return (f"{p['name']} × {qty}\nমোট: {total} USDT\n\n⚠️ নোট ভালোভাবে পড়েছেন তো? নিশ্চিত করলে ব্যালেন্স থেকে টাকা কাটা হবে।",
            b.as_markup())


@dp.callback_query(F.data.startswith("c:"))
async def confirm(c: CallbackQuery):
    _, pid, qty = c.data.split(":")
    qty = int(qty)
    p = await find(pid, force=True)
    if not p or not p["ok"] or (p["stock"] is not None and p["stock"] < qty):
        return await c.answer("❌ স্টক নেই", show_alert=True)
    t, kb = confirm_view(p, qty)
    await c.message.edit_text(t, reply_markup=kb)


@dp.callback_query(F.data.startswith("cq:"))
async def custom_qty(c: CallbackQuery, state: FSMContext):
    p = await find(c.data[3:], force=True)
    if not p or not p["ok"]:
        return await c.answer("❌ স্টক শেষ", show_alert=True)
    mx = p["stock"] if p["stock"] is not None else 100
    await state.set_state(Qty.n)
    await state.update_data(pid=str(p["id"]))
    await c.message.answer(f"কয়টা কিনবেন? সংখ্যা লিখুন (১ থেকে {mx})\n(বাতিল: /cancel)")
    await c.answer()


@dp.message(Qty.n, F.text)
async def qty_in(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("শুধু সংখ্যা লিখুন।")
    n = int(m.text.strip())
    p = await find((await state.get_data())["pid"], force=True)
    if not p or not p["ok"]:
        await state.clear()
        return await m.answer("❌ স্টক শেষ হয়ে গেছে।")
    mx = p["stock"] if p["stock"] is not None else 100
    if not 1 <= n <= mx:
        return await m.answer(f"১ থেকে {mx} এর মধ্যে সংখ্যা দিন।")
    await state.clear()
    t, kb = confirm_view(p, n)
    await m.answer(t, reply_markup=kb)


@dp.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    _, pid, qty, cents = c.data.split(":")
    qty, uid = int(qty), c.from_user.id
    p = await find(pid, force=True)
    if not p or not p["ok"] or (p["stock"] is not None and p["stock"] < qty):
        return await c.answer("❌ স্টক শেষ হয়ে গেছে", show_alert=True)
    total, base = sell_price(p) * qty, p["price"] * qty
    if int(total * 100) != int(cents):
        b = InlineKeyboardBuilder(); b.button(text="🔄 নতুন দাম দেখুন", callback_data=f"p:{pid}")
        return await c.message.edit_text(f"⚠️ দাম বদলে গেছে। নতুন দাম: {sell_price(p)} USDT/টি", reply_markup=b.as_markup())
    if await deduct(uid, total) is None:
        return await c.answer("❌ ব্যালেন্স কম। আগে Deposit করুন।", show_alert=True)
    await c.message.edit_text("⏳ অর্ডার প্রসেস হচ্ছে...")
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
        await credit(uid, total)  # রিফান্ড
        await pool.execute(ins, uid, str(pid), p["name"], qty, total, base, rid, "failed", str(data)[:500])
        await notify(f"⚠️ অর্ডার ফেল\nUser: {uid}\n{p['name']} ×{qty}\n{str(data)[:300]}")
        b = InlineKeyboardBuilder(); b.button(text="⬅️ Back", callback_data="shop:0")
        return await c.message.edit_text("❌ অর্ডার ফেল। টাকা ফেরত দেওয়া হয়েছে। একটু পরে চেষ্টা করুন।", reply_markup=b.as_markup())
    items = data.get("items") or data.get("delivered") or data.get("data") or data
    if isinstance(items, list):
        items = "\n".join(i if isinstance(i, str) else json.dumps(i, ensure_ascii=False) for i in items)
    elif not isinstance(items, str):
        items = json.dumps(items, ensure_ascii=False, indent=1)
    await pool.execute(ins, uid, str(pid), p["name"], qty, total, base, rid, "done", items)
    bal = await balance(uid)
    await c.message.edit_text(f"✅ সফল!\n{esc(p['name'])} × {qty}\n\n<code>{esc(items[:3500])}</code>\n\n💰 বাকি: {bal} USDT",
                              parse_mode="HTML")
    _cache["t"] = 0
    u = c.from_user
    await notify(f"🛍 নতুন অর্ডার\n👤 {u.full_name} (@{u.username}) <code>{uid}</code>\n"
                 f"📦 {esc(p['name'])} ×{qty}\n💵 {total} USDT | লাভ: {q2(total-base)}", parse_mode="HTML")


# ---- Deposit ----
@dp.callback_query(F.data == "deposit")
async def deposit(c: CallbackQuery, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(text="🟡 Binance", callback_data="dep:binance")
    b.button(text="⚫ Bybit", callback_data="dep:bybit")
    b.button(text="🟣 bKash", callback_data="dep:bkash")
    b.adjust(2, 1)
    add_contact(b, "Deposit নিয়ে সাহায্য লাগবে")
    b.row(Btn(text="⬅️ Back", callback_data="home"))
    await c.message.edit_text("কোন মাধ্যমে Deposit করবেন?", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("dep:"))
async def dep_method(c: CallbackQuery, state: FSMContext):
    m = c.data[4:]
    b = InlineKeyboardBuilder()
    if m == "binance":
        await state.set_state(Dep.txid); await state.update_data(method="binance")
        txt = (f"🟡 <b>Binance Pay</b>\nPay ID: <code>{esc(BINANCE_PAY_ID)}</code>\n\n"
               f"১) USDT পাঠান (Binance Pay / Internal Transfer)\n"
               f"২) পেমেন্ট শেষে <b>Order ID / Transaction ID</b> কপি করে এখানে পাঠান।\n"
               f"মিনিমাম {MIN_DEPOSIT} USDT। (২৪ ঘণ্টার মধ্যের পেমেন্ট verify হবে)")
    elif m == "bybit":
        await state.set_state(Dep.txid); await state.update_data(method="bybit")
        txt = (f"⚫ <b>Bybit</b>\nUID: <code>{esc(BYBIT_UID)}</code>\n\n"
               f"১) UID-তে Internal Transfer দিয়ে USDT পাঠান\n"
               f"২) ট্রান্সফার শেষে <b>Transaction ID</b> কপি করে এখানে পাঠান।\n"
               f"মিনিমাম {MIN_DEPOSIT} USDT। (৭ দিনের মধ্যের পেমেন্ট verify হবে)")
    else:
        b.button(text="✅ পেমেন্ট করেছি", callback_data="bk:paid")
        txt = (f"🟣 <b>bKash</b>\nনম্বর: <code>{esc(BKASH_NUMBER)}</code> (Send Money)\n"
               f"রেট: 1 USDT = {BDT_RATE}৳\n\nটাকা পাঠিয়ে নিচের <b>পেমেন্ট করেছি</b> চাপুন।")
    for x in contact_btns(f"{m.upper()} Deposit নিয়ে সাহায্য লাগবে"):
        b.add(x)
    b.button(text="⬅️ Back", callback_data="deposit")
    b.adjust(1)
    await c.message.edit_text(txt, parse_mode="HTML", reply_markup=b.as_markup())


@dp.message(Dep.txid, F.text)
async def got_tx(m: Message, state: FSMContext):
    method = (await state.get_data())["method"]
    txid = m.text.strip()
    if len(txid) < 6 or len(txid) > 80:
        return await m.answer("সঠিক Transaction/Order ID পাঠান। (বাতিল: /cancel)")
    wait = await m.answer("🔍 যাচাই করা হচ্ছে...")
    try:
        amt = await (verify_binance(txid) if method == "binance" else verify_bybit(txid))
    except Exception as e:
        logging.exception(e)
        return await wait.edit_text("⚠️ যাচাই করতে সমস্যা হয়েছে, একটু পরে আবার পাঠান।")
    if not amt:
        return await wait.edit_text("❌ পেমেন্ট পাওয়া যায়নি। ID ঠিক আছে কিনা দেখুন, বা কয়েক মিনিট পরে আবার পাঠান।")
    if amt < MIN_DEPOSIT:
        return await wait.edit_text(f"❌ মিনিমাম {MIN_DEPOSIT} USDT।")
    if not await credit_deposit(method, txid, m.from_user.id, amt):
        return await wait.edit_text("⚠️ এই Transaction ID আগেই ব্যবহার হয়েছে।")
    await state.clear()
    bal = await balance(m.from_user.id)
    await wait.edit_text(f"✅ {amt} USDT যোগ হয়েছে!\n💰 নতুন ব্যালেন্স: {bal} USDT", reply_markup=main_menu(m.from_user.id))
    u = m.from_user
    await notify(f"💵 Deposit {amt} USDT ({method})\n👤 {u.full_name} <code>{u.id}</code>\nTx: <code>{esc(txid)}</code>",
                 parse_mode="HTML")


# ---- bKash (ইউজার TrxID দেয়, অ্যাডমিন Approve করে) ----
@dp.callback_query(F.data == "bk:paid")
async def bk_paid(c: CallbackQuery, state: FSMContext):
    await state.set_state(Bk.info)
    await c.message.answer("TrxID আর টাকার পরিমাণ এক মেসেজে পাঠান।\nউদাহরণ: <code>ABC123XYZ 500</code>\n(বাতিল: /cancel)",
                           parse_mode="HTML")
    await c.answer()


@dp.message(Bk.info, F.text)
async def bk_info(m: Message, state: FSMContext):
    parts = m.text.split()
    try:
        amt = D(parts[-1]); trx = parts[0]
        assert len(parts) >= 2 and amt > 0
    except Exception:
        return await m.answer("ফরম্যাট ভুল। উদাহরণ: ABC123XYZ 500")
    rid = await pool.fetchval("INSERT INTO bkash(user_id,trx,amount_bdt) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING id",
                              m.from_user.id, trx, amt)
    if not rid:
        return await m.answer("⚠️ এই TrxID আগেই জমা দেওয়া হয়েছে।")
    await state.clear()
    await m.answer("✅ জমা হয়েছে। Admin যাচাই করে ব্যালেন্স যোগ করবে।", reply_markup=main_menu(m.from_user.id))
    b = InlineKeyboardBuilder()
    b.button(text="✅ Approve", callback_data=f"bk:ok:{rid}")
    b.button(text="❌ Reject", callback_data=f"bk:no:{rid}")
    u = m.from_user
    await notify(f"🟣 bKash রিকোয়েস্ট #{rid}\n👤 {esc(u.full_name)} (@{u.username}) <code>{u.id}</code>\n"
                 f"TrxID: <code>{esc(trx)}</code>\nপরিমাণ: {amt}৳ ≈ {q2(amt / BDT_RATE)} USDT\n"
                 f"⚠️ bKash অ্যাপে মিলিয়ে তারপর Approve করুন।", parse_mode="HTML", reply_markup=b.as_markup())


@adm.callback_query(F.data.startswith("bk:ok:") | F.data.startswith("bk:no:"))
async def bk_decide(c: CallbackQuery):
    _, act, rid = c.data.split(":")
    new = "approved" if act == "ok" else "rejected"
    r = await pool.fetchrow("UPDATE bkash SET status=$2 WHERE id=$1 AND status='pending' RETURNING user_id,trx,amount_bdt",
                            int(rid), new)
    if not r:
        return await c.answer("আগেই প্রসেস হয়েছে", show_alert=True)
    if act == "ok":
        usdt = q2(r["amount_bdt"] / BDT_RATE)
        await credit_deposit("bkash", r["trx"], r["user_id"], usdt)
        try:
            await bot.send_message(r["user_id"], f"✅ bKash Deposit সফল: {usdt} USDT যোগ হয়েছে।")
        except Exception:
            pass
    else:
        try:
            await bot.send_message(r["user_id"], "❌ আপনার bKash রিকোয়েস্ট বাতিল হয়েছে। সমস্যা হলে Admin-কে জানান।")
        except Exception:
            pass
    await c.message.edit_text(c.message.text + f"\n\n➡️ {new.upper()} by {c.from_user.full_name}")


# ================= ADMIN =================
def adm_menu():
    b = InlineKeyboardBuilder()
    b.button(text="📊 Stats", callback_data="adm:stats")
    b.button(text="🧾 Recent Orders", callback_data="adm:orders")
    b.button(text="💲 Product Prices", callback_data="adm:pl:0")
    b.button(text="⚙️ Pricing Rule", callback_data="adm:rule")
    b.button(text="🏦 Shop API Balance", callback_data="adm:api")
    b.button(text="⬅️ Back", callback_data="home")
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def back_adm():
    b = InlineKeyboardBuilder(); b.button(text="⬅️ Admin", callback_data="adm")
    return b.as_markup()


@adm.message(Command("admin"))
async def adm_cmd(m: Message):
    await m.answer("🛠 Admin Panel", reply_markup=adm_menu())


@adm.callback_query(F.data == "adm")
async def adm_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("🛠 Admin Panel", reply_markup=adm_menu())


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
    top = await pool.fetch("SELECT product, sum(qty) q FROM orders WHERE status='done' GROUP BY product ORDER BY q DESC LIMIT 5")
    t = (f"📊 <b>Stats</b>\n👥 ইউজার: {r['users']}\n💼 সব ইউজারের ব্যালেন্স: {q2(r['bal'])} USDT\n\n"
         f"<b>আজ (BD)</b>\n🛍 অর্ডার: {r['o_td']} | 💵 {q2(r['rev_td'])} USDT | 📈 লাভ {q2(r['pro_td'])}\n"
         f"➕ Deposit: {q2(r['dep_td'])} USDT\n\n"
         f"<b>মোট</b>\n🛍 অর্ডার: {r['o_all']} | 💵 {q2(r['rev_all'])} USDT | 📈 লাভ {q2(r['pro_all'])}\n"
         f"➕ Deposit: {q2(r['dep_all'])} USDT | ❌ ফেল: {r['failed']}\n\n<b>🔥 Top</b>\n")
    t += "\n".join(f"• {esc(x['product'][:35])} — {x['q']}" for x in top) or "—"
    await c.message.edit_text(t, parse_mode="HTML", reply_markup=back_adm())


@adm.callback_query(F.data == "adm:orders")
async def adm_orders(c: CallbackQuery):
    rows = await pool.fetch("SELECT id,user_id,product,qty,cost,status,created AT TIME ZONE 'Asia/Dhaka' AS t "
                            "FROM orders ORDER BY id DESC LIMIT 12")
    t = "🧾 <b>শেষ অর্ডার</b>\n" + "\n".join(
        f"#{r['id']} {'✅' if r['status']=='done' else '❌'} {r['t']:%d/%m %H:%M} <code>{r['user_id']}</code> "
        f"{esc(r['product'][:22])} ×{r['qty']} {r['cost']}$" for r in rows)
    await c.message.edit_text(t if rows else "কোনো অর্ডার নেই", parse_mode="HTML", reply_markup=back_adm())


@adm.callback_query(F.data == "adm:api")
async def adm_api(c: CallbackQuery):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{API_BASE}/balance", headers=H(), timeout=aiohttp.ClientTimeout(total=15)) as r:
                txt = await r.text()
    except Exception as e:
        txt = str(e)
    await c.message.edit_text(f"🏦 Safwan Shop API:\n<code>{esc(txt[:500])}</code>", parse_mode="HTML", reply_markup=back_adm())


# ---- Pricing rule ----
@adm.callback_query(F.data == "adm:rule")
async def adm_rule(c: CallbackQuery):
    b = InlineKeyboardBuilder()
    b.button(text="✏️ রুল পরিবর্তন", callback_data="adm:rule:set")
    b.button(text="♻️ Env রুলে রিসেট", callback_data="adm:rule:reset")
    b.button(text="⬅️ Admin", callback_data="adm")
    b.adjust(1)
    await c.message.edit_text(
        f"⚙️ <b>বর্তমান রুল</b>\n{esc(rule_text(ST['rule']))}\n\nEnv (ডিফল্ট): {esc(rule_text(ENV_RULE))}\n\n"
        f"ফরম্যাট:\n<code>a2.5</code> = +2.5 USDT\n<code>p10</code> = +10%\n<code>m2</code> = ×2\n\n"
        f"ℹ️ যে প্রোডাক্টের দাম ম্যানুয়ালি সেট করা, সেগুলো এই রুলে বদলায় না।", parse_mode="HTML", reply_markup=b.as_markup())


@adm.callback_query(F.data == "adm:rule:set")
async def adm_rule_set(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.rule)
    await c.message.answer("নতুন রুল লিখুন: a2.5 / p10 / m2\n(বাতিল: /cancel)")
    await c.answer()


@adm.message(Adm.rule, F.text)
async def adm_rule_in(m: Message, state: FSMContext):
    r = parse_rule(m.text)
    if not r:
        return await m.answer("ভুল ফরম্যাট। a2.5 / p10 / m2 দিন।")
    await pool.execute("INSERT INTO settings(key,value) VALUES('pricing',$1) ON CONFLICT(key) DO UPDATE SET value=$1", m.text.strip())
    ST["rule"] = r
    await state.clear()
    await m.answer(f"✅ নতুন রুল: {rule_text(r)}", reply_markup=adm_menu())


@adm.callback_query(F.data == "adm:rule:reset")
async def adm_rule_reset(c: CallbackQuery):
    await pool.execute("DELETE FROM settings WHERE key='pricing'")
    ST["rule"] = ENV_RULE
    await c.answer("রিসেট হয়েছে")
    await adm_rule(c)


# ---- Per-product price ----
@adm.callback_query(F.data.startswith("adm:pl:"))
async def adm_pl(c: CallbackQuery):
    await render_pl(c, int(c.data.split(":")[2]))


async def render_pl(c: CallbackQuery, page: int):
    ps = await get_products(True)
    b = InlineKeyboardBuilder()
    for p in ps[page * 10:(page + 1) * 10]:
        ns = note_for(p)[1]
        mk = ("⛔" if not p["ok"] else "") + ("✏️" if str(p["id"]) in OVR else "") + \
             ("📝" if ns == "admin" else "❓" if ns == "none" else "")
        b.row(Btn(text=f"{mk}{p['name'][:26]} | {p['price']}→{sell_price(p)}", callback_data=f"adm:p:{p['id']}"))
    nav = []
    if page > 0:
        nav.append(Btn(text="◀️", callback_data=f"adm:pl:{page-1}"))
    if (page + 1) * 10 < len(ps):
        nav.append(Btn(text="▶️", callback_data=f"adm:pl:{page+1}"))
    if nav:
        b.row(*nav)
    b.row(Btn(text="♻️ সব ম্যানুয়াল দাম রিসেট", callback_data="adm:prall"))
    b.row(Btn(text="⬅️ Admin", callback_data="adm"))
    await c.message.edit_text("💲 প্রোডাক্ট (আসল → আপনার দাম)\n✏️ ম্যানুয়াল দাম | 📝 আপনার নোট | ❓ কোনো নোট নেই | ⛔ স্টক নেই", reply_markup=b.as_markup())


@adm.callback_query(F.data.startswith("adm:p:"))
async def adm_p(c: CallbackQuery):
    pid = c.data.split(":")[2]
    p = await find(pid, True)
    if not p:
        return await c.answer("প্রোডাক্ট পাওয়া যায়নি", show_alert=True)
    man = OVR.get(pid)
    b = InlineKeyboardBuilder()
    b.button(text="✏️ দাম সেট", callback_data=f"adm:ps:{pid}")
    b.button(text="♻️ রিসেট (রুল অনুযায়ী)", callback_data=f"adm:pr:{pid}")
    b.button(text="📝 নোট সেট/এডিট", callback_data=f"adm:ns:{pid}")
    b.button(text="🗑 নোট রিসেট (সাফওয়ানেরটা)", callback_data=f"adm:nr:{pid}")
    b.button(text="⬅️ লিস্ট", callback_data="adm:pl:0")
    b.adjust(1)
    st = "Unlimited" if p["stock"] is None else p["stock"]
    note, src = note_for(p)
    src_t = {"admin": "আপনার কাস্টম নোট", "safwan": "সাফওয়ানের দেওয়া", "none": "কোনো নোট নেই ❓"}[src]
    await c.message.edit_text(
        f"📦 <b>{esc(p['name'])}</b>\nID: {pid} | স্টক: {st}\n\nসাফওয়ান দাম: {p['price']}\n"
        f"রুল অনুযায়ী: {apply_rule(p['price'], ST['rule'])}\nবর্তমান বিক্রয় দাম: <b>{sell_price(p)}</b> "
        f"{'(ম্যানুয়াল ✏️)' if man is not None else '(অটো)'}\n\n📝 নোট ({src_t}):\n{esc(note[:500]) if note else '—'}",
        parse_mode="HTML", reply_markup=b.as_markup())


@adm.callback_query(F.data.startswith("adm:ps:"))
async def adm_ps(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.price)
    await state.update_data(pid=c.data.split(":")[2])
    await c.message.answer("নতুন বিক্রয় দাম (USDT) লিখুন, যেমন: 3.5\n(বাতিল: /cancel)")
    await c.answer()


@adm.message(Adm.price, F.text)
async def adm_price_in(m: Message, state: FSMContext):
    try:
        v = q2(D(m.text.strip()))
        assert v > 0
    except Exception:
        return await m.answer("সঠিক সংখ্যা দিন, যেমন 3.5")
    pid = (await state.get_data())["pid"]
    await pool.execute("INSERT INTO overrides(pid,price) VALUES($1,$2) ON CONFLICT(pid) DO UPDATE SET price=$2", pid, v)
    OVR[pid] = v
    await state.clear()
    p = await find(pid)
    warn = f"\n⚠️ সাফওয়ান দামের ({p['price']}) নিচে!" if p and v < p["price"] else ""
    await m.answer(f"✅ দাম সেট: {v} USDT{warn}", reply_markup=adm_menu())


@adm.callback_query(F.data.startswith("adm:pr:"))
async def adm_pr(c: CallbackQuery):
    pid = c.data.split(":")[2]
    await pool.execute("DELETE FROM overrides WHERE pid=$1", pid)
    OVR.pop(pid, None)
    await c.answer("রিসেট হয়েছে")
    await adm_p(c)


@adm.callback_query(F.data == "adm:prall")
async def adm_prall(c: CallbackQuery):
    await pool.execute("DELETE FROM overrides")
    OVR.clear()
    await c.answer("সব ম্যানুয়াল দাম রিসেট হয়েছে")
    await render_pl(c, 0)


@adm.callback_query(F.data.startswith("adm:ns:"))
async def adm_ns(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.note)
    await state.update_data(pid=c.data.split(":")[2])
    await c.message.answer("এই প্রোডাক্টের নোট লিখে পাঠান (কাস্টমার কেনার আগে দেখবে, সর্বোচ্চ ১৫০০ অক্ষর)।\n(বাতিল: /cancel)")
    await c.answer()


@adm.message(Adm.note, F.text)
async def adm_note_in(m: Message, state: FSMContext):
    txt = m.text.strip()
    if not txt or len(txt) > 1500:
        return await m.answer("নোট ১–১৫০০ অক্ষরের মধ্যে দিন।")
    pid = (await state.get_data())["pid"]
    await pool.execute("INSERT INTO notes(pid,note) VALUES($1,$2) ON CONFLICT(pid) DO UPDATE SET note=$2", pid, txt)
    NOTES[pid] = txt
    await state.clear()
    await m.answer("✅ নোট সেভ হয়েছে।", reply_markup=adm_menu())


@adm.callback_query(F.data.startswith("adm:nr:"))
async def adm_nr(c: CallbackQuery):
    pid = c.data.split(":")[2]
    await pool.execute("DELETE FROM notes WHERE pid=$1", pid)
    NOTES.pop(pid, None)
    await c.answer("নোট রিসেট হয়েছে")
    await adm_p(c)


@adm.message(Command("addbal"))
async def addbal(m: Message):
    try:
        _, uid, amt = m.text.split()
        nb = await credit(int(uid), D(amt))
        await m.answer(f"✅ Done. নতুন ব্যালেন্স: {nb}")
        if D(amt) > 0:
            await bot.send_message(int(uid), f"✅ আপনার wallet-এ {amt} USDT যোগ হয়েছে।")
    except Exception:
        await m.answer("ব্যবহার: /addbal USER_ID AMOUNT (মাইনাস দিয়ে কাটাও যায়)")


dp.include_router(adm)


# ================= Web (UptimeRobot) =================
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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
