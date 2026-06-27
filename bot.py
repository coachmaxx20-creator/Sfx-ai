"""
SFX Trading Bot - User Controlled
"""
import os, json, time, logging, threading, numpy as np, requests
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from iqoptionapi.stable_api import IQ_Option

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_CHAT_ID  = 8319282451
DATA_FILE      = "users.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── DATA ──────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE,"r") as f: return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE,"w") as f: json.dump(data,f,indent=2)

def get_user(chat_id): return load_data().get(str(chat_id),{})

def update_user(chat_id,**kwargs):
    data=load_data(); user=data.get(str(chat_id),{})
    user.update(kwargs); data[str(chat_id)]=user; save_data(data)

def is_admin(chat_id): return int(chat_id)==ADMIN_CHAT_ID

# ── SEND MESSAGE ──────────────────────────────
def send_msg(chat_id, text, keyboard=None):
    if not TELEGRAM_TOKEN: return
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=15
        )
    except Exception as e:
        log.error(f"send_msg error: {e}")

def make_keyboard(buttons):
    """buttons = list of list of (text, callback_data)"""
    return {
        "inline_keyboard": [
            [{"text": b[0], "callback_data": b[1]} for b in row]
            for row in buttons
        ]
    }

# ── MAIN TRADE BUTTONS ────────────────────────
def show_main_buttons(chat_id, balance):
    user = get_user(chat_id)
    amount   = float(user.get("trade_amount", 1))
    duration = int(user.get("duration", 1))
    acc_type = "🟡 Demo" if user.get("account_type","PRACTICE")=="PRACTICE" else "💚 Live"
    kb = make_keyboard([
        [("⚡ Place a Trade", "place_trade")],
        [("⚙️ Settings", "settings")]
    ])
    send_msg(chat_id,
        f"💰 *Balance: ${balance:.2f}*\n"
        f"Account: {acc_type}\n"
        f"💵 Trade Amount: ${amount:.2f}\n"
        f"⏱ Duration: {duration} min\n\n"
        f"What would you like to do?",
        keyboard=kb
    )

# ── IQ OPTION ─────────────────────────────────
active_apis = {}

def connect_iqoption(email, password, account_type="PRACTICE"):
    try:
        api = IQ_Option(email, password)
        check, reason = api.connect()
        if check:
            api.change_balance(account_type)
            return api, None
        return None, str(reason)
    except Exception as e:
        return None, str(e)

def get_balance(chat_id):
    api = active_apis.get(str(chat_id))
    if not api: return 0.0
    try: return float(api.get_balance())
    except: return 0.0

# ── PAIRS ─────────────────────────────────────
import random
PAIRS = ["EURUSD-OTC","GBPUSD-OTC","AUDUSD-OTC","NZDUSD-OTC","AUDCAD-OTC","EURGBP-OTC"]
PAIR_LABELS = {
    "EURUSD-OTC":"EUR/USD (OTC)","GBPUSD-OTC":"GBP/USD (OTC)",
    "AUDUSD-OTC":"AUD/USD (OTC)","NZDUSD-OTC":"NZD/USD (OTC)",
    "AUDCAD-OTC":"AUD/CAD (OTC)","EURGBP-OTC":"EUR/GBP (OTC)"
}

def get_available_pair(api):
    """Rotate through pairs — always returns one."""
    return random.choice(PAIRS)

def get_signal(api):
    try:
        pair = get_available_pair(api)
        candles = api.get_candles(pair, 60, 30, time.time())
        if not candles or len(candles)<14:
            return random.choice(["call","put"]), pair
        closes = np.array([c["close"] for c in candles], dtype=float)
        d = np.diff(closes)
        ag = np.mean(np.where(d>0,d,0)[:14])
        al = np.mean(np.where(d<0,-d,0)[:14])
        rsi = 100 if al==0 else 100-(100/(1+ag/al))
        signal = "call" if rsi<35 else "put" if rsi>65 else random.choice(["call","put"])
        return signal, pair
    except:
        return random.choice(["call","put"]), PAIRS[0]

# ── TRADE THREAD ──────────────────────────────
def run_trade(chat_id):
    api = active_apis.get(str(chat_id))
    if not api:
        send_msg(chat_id, "❌ Not connected. Use /start to reconnect.")
        return

    user     = get_user(chat_id)
    amount   = float(user.get("trade_amount", 1))
    duration = int(user.get("duration", 1))

    # Get signal
    signal, pair = get_signal(api)
    pair_label   = PAIR_LABELS.get(pair, pair)
    dir_label    = "📈 CALL (UP)" if signal=="call" else "📉 PUT (DOWN)"

    # Notify trade placed
    send_msg(chat_id,
        f"⚡ *Trade Placed!*\n\n"
        f"Pair: *{pair_label}*\n"
        f"Direction: *{dir_label}*\n"
        f"Amount: *${amount:.2f}*\n"
        f"Duration: *{duration} min*\n\n"
        f"_Trade is running..._"
    )

    # Place trade — try up to 3 different pairs if one fails
    order_id = None
    tried = []
    for attempt_pair in [pair] + [p for p in PAIRS if p != pair][:2]:
        tried.append(attempt_pair)
        try:
            ok, order_id = api.buy(amount, attempt_pair, signal, duration)
            if ok and order_id:
                pair = attempt_pair
                pair_label = PAIR_LABELS.get(pair, pair)
                log.info(f"Trade placed on {pair}: order_id={order_id}")
                break
            else:
                order_id = None
                log.warning(f"buy() failed for {attempt_pair}, trying next...")
        except Exception as e:
            log.error(f"Trade error on {attempt_pair}: {e}")
            order_id = None

    if not order_id:
        send_msg(chat_id, "Could not place trade. This pair may not be available right now. Try again.")
        bal = get_balance(chat_id)
        show_main_buttons(chat_id, bal)
        return

    # Wait for trade to finish
    time.sleep(duration * 60 + 5)

    # Get new balance
    new_bal = 0.0
    for _ in range(5):
        try:
            new_bal = float(api.get_balance())
            if new_bal > 0: break
        except: pass
        time.sleep(3)

    # Show result with main buttons
    show_main_buttons(chat_id, new_bal)

# ── TELEGRAM HANDLERS ─────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("🚀 Get Started", callback_data="get_started")]]
    await update.message.reply_text(
        "👋 *Welcome To SFX Bot!*\n\n"
        "Your personal IQ Option trading bot.\n\n"
        "✅ Free for everyone\n"
        "✅ Demo & Live account supported\n"
        "✅ You control every trade\n"
        "✅ Place trades with one tap\n\n"
        "Tap *Get Started* to begin 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    chat_id = q.from_user.id
    data    = q.data
    await q.answer()

    # ── Get Started ──
    if data == "get_started":
        update_user(chat_id, state="awaiting_credentials_demo")
        await q.message.reply_text(
            "Send your IQ Option login details in this format:\n\n"
            "Email (youremail@gmail.com)\n"
            "Password (yourpassword)"
        )

    # ── Account Type Selection ──
    elif data == "pick_demo":
        update_user(chat_id, account_type="PRACTICE")
        await _connect_account(q.message, chat_id)

    elif data == "pick_live":
        update_user(chat_id, account_type="REAL")
        await _connect_account(q.message, chat_id)

    # ── Place Trade ──
    elif data == "place_trade":
        api = active_apis.get(str(chat_id))
        if not api:
            await q.message.reply_text("❌ Not connected. Use /start to reconnect.")
            return
        t = threading.Thread(target=run_trade, args=(chat_id,), daemon=True)
        t.start()

    # ── Settings ──
    elif data == "settings":
        user = get_user(chat_id)
        amount   = float(user.get("trade_amount", 1))
        duration = int(user.get("duration", 1))
        acc_type = user.get("account_type","PRACTICE")
        switch_label = "💚 Switch to Live" if acc_type=="PRACTICE" else "🟡 Switch to Demo"
        switch_cb    = "switch_live"        if acc_type=="PRACTICE" else "switch_demo"
        kb = [[InlineKeyboardButton("💵 Change Trade Amount", callback_data="set_amount")],
              [InlineKeyboardButton("⏱ Change Duration",      callback_data="set_duration")],
              [InlineKeyboardButton(switch_label,              callback_data=switch_cb)],
              [InlineKeyboardButton("🔙 Back",                 callback_data="back_main")]]
        await q.message.reply_text(
            f"⚙️ *Settings*\n\n"
            f"💵 Trade Amount: *${amount:.2f}*\n"
            f"⏱ Duration: *{duration} min*\n"
            f"Account: *{'🟡 Demo' if acc_type=='PRACTICE' else '💚 Live'}*\n\n"
            f"Tap to change 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == "set_amount":
        update_user(chat_id, state="awaiting_amount")
        await q.message.reply_text("💵 Send your trade amount e.g *1* or *5*", parse_mode="Markdown")

    elif data == "set_duration":
        update_user(chat_id, state="awaiting_duration")
        await q.message.reply_text("⏱ Send duration in minutes. Choose: *1, 2, 5, 10, 15*", parse_mode="Markdown")

    elif data == "switch_live":
        update_user(chat_id, account_type="REAL", state="awaiting_credentials_switch")
        await q.message.reply_text(
            "Switching to *Live Account* 💚\n\n"
            "Resend your IQ Option login details:\n\n"
            "Email (youremail@gmail.com)\n"
            "Password (yourpassword)",
            parse_mode="Markdown"
        )

    elif data == "switch_demo":
        update_user(chat_id, account_type="PRACTICE", state="awaiting_credentials_switch")
        await q.message.reply_text(
            "Switching to *Demo Account* 🟡\n\n"
            "Resend your IQ Option login details:\n\n"
            "Email (youremail@gmail.com)\n"
            "Password (yourpassword)",
            parse_mode="Markdown"
        )

    elif data == "back_main":
        bal = get_balance(chat_id)
        show_main_buttons(chat_id, bal)

async def _connect_account(message, chat_id):
    """Connect using saved credentials and account type."""
    user         = get_user(chat_id)
    email        = user.get("email","")
    password     = user.get("password","")
    account_type = user.get("account_type","PRACTICE")

    await message.reply_text("⏳ Connecting to IQ Option...")
    api, error = connect_iqoption(email, password, account_type)

    if api:
        active_apis[str(chat_id)] = api
        balance  = float(api.get_balance())
        amount   = float(user.get("trade_amount", 1))
        duration = int(user.get("duration", 1))
        acc_label = "🟡 Demo" if account_type=="PRACTICE" else "💚 Live"
        update_user(chat_id, state="connected")

        kb = [[InlineKeyboardButton("⚡ Place a Trade", callback_data="place_trade")],
              [InlineKeyboardButton("⚙️ Settings",      callback_data="settings")]]
        await message.reply_text(
            f"✅ *Connected Successfully!*\n\n"
            f"Account: {acc_label}\n"
            f"💰 Balance: *${balance:.2f}*\n"
            f"💵 Trade Amount: *${amount:.2f}*\n"
            f"⏱ Duration: *{duration} min*\n\n"
            f"Tap *Place a Trade* to start 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        await message.reply_text(
            f"❌ *Couldn't connect!*\n\n"
            f"Error: {error or 'Invalid credentials'}\n\n"
            f"Use /start to try again.",
            parse_mode="Markdown"
        )

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text    = update.message.text.strip() if update.message.text else ""
    user    = get_user(chat_id)
    state   = user.get("state","idle")

    # ── Awaiting login credentials (first time) ──
    if state in ("awaiting_credentials_demo", "awaiting_credentials_switch"):
        import re
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) < 2:
            await update.message.reply_text(
                "Please send in this format:\n\n"
                "Email (youremail@gmail.com)\n"
                "Password (yourpassword)"
            )
            return

        def extract(line):
            m = re.search(r'\((.+)\)', line)
            return m.group(1).strip() if m else line.strip()

        email    = extract(lines[0])
        password = extract(lines[1])
        update_user(chat_id, email=email, password=password)

        if state == "awaiting_credentials_switch":
            # Already has account type, connect directly
            await _connect_account(update.message, chat_id)
        else:
            # Ask demo or live
            update_user(chat_id, state="awaiting_account_type")
            kb = [[InlineKeyboardButton("🟡 Demo Account", callback_data="pick_demo")],
                  [InlineKeyboardButton("💚 Live Account", callback_data="pick_live")]]
            await update.message.reply_text(
                "✅ Details received!\n\nWhich account would you like to use?",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        return

    # ── Settings inputs ──
    if state == "awaiting_amount":
        try:
            amount = float(text.replace("$","").strip())
            if amount <= 0: raise ValueError
            update_user(chat_id, trade_amount=amount, state="connected")
            bal = get_balance(chat_id)
            await update.message.reply_text(f"✅ Trade amount set to *${amount:.2f}*", parse_mode="Markdown")
            show_main_buttons(chat_id, bal)
        except:
            await update.message.reply_text("Send a valid amount e.g *1*", parse_mode="Markdown")
        return

    if state == "awaiting_duration":
        try:
            dur = int(text.strip())
            if dur not in [1,2,5,10,15]: raise ValueError
            update_user(chat_id, duration=dur, state="connected")
            bal = get_balance(chat_id)
            await update.message.reply_text(f"✅ Duration set to *{dur} minutes*", parse_mode="Markdown")
            show_main_buttons(chat_id, bal)
        except:
            await update.message.reply_text("Send one of: *1, 2, 5, 10, 15*", parse_mode="Markdown")
        return

    await update.message.reply_text("Use /start to begin.")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /start to reconnect and continue trading.")

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.chat_id): return
    data = load_data()
    await update.message.reply_text(
        f"👥 *Users*\n\nTotal: {len(data)}\n"
        f"Demo: {sum(1 for u in data.values() if u.get('account_type')=='PRACTICE')}\n"
        f"Live: {sum(1 for u in data.values() if u.get('account_type')=='REAL')}",
        parse_mode="Markdown"
    )

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.chat_id): return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast message"); return
    data = load_data(); sent = 0; failed = 0
    for uid in data.keys():
        try: await context.bot.send_message(chat_id=int(uid), text=msg); sent+=1
        except: failed+=1
    await update.message.reply_text(f"📢 Sent: {sent} Failed: {failed}")

def main():
    if not TELEGRAM_TOKEN: raise ValueError("TELEGRAM_TOKEN not set!")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("stop",      cmd_stop))
    app.add_handler(CommandHandler("users",     admin_users))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    log.info("🚀 SFX Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
