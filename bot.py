"""
SFX Auto Trading Bot
=====================
Telegram bot with real IQ Option connection.
Features:
- Demo & Live account support
- Daily profit goal system
- Auto stops when goal is reached
- Resets every new day
- User sets trade amount anytime
- Trades multiple currency pairs
- Admin: 8319282451 (free forever)
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timezone, date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from iqoptionapi.stable_api import IQ_Option

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_CHAT_ID  = 8319282451
DATA_FILE      = "users.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

telegram_app = None

# ─────────────────────────────────────────────
#  DATA STORE
# ─────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_user(chat_id):
    return load_data().get(str(chat_id), {})

def update_user(chat_id, **kwargs):
    data = load_data()
    user = data.get(str(chat_id), {})
    user.update(kwargs)
    data[str(chat_id)] = user
    save_data(data)

def is_admin(chat_id):
    return int(chat_id) == ADMIN_CHAT_ID

def is_subscribed(chat_id):
    if is_admin(chat_id):
        return True
    return get_user(chat_id).get("subscribed", False)

# ─────────────────────────────────────────────
#  IQ OPTION CONNECTION
# ─────────────────────────────────────────────
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

# ─────────────────────────────────────────────
#  TRADING ENGINE
# ─────────────────────────────────────────────
PAIRS = [
    "EURUSD-OTC", "GBPUSD-OTC", "AUDUSD-OTC",
    "NZDUSD-OTC", "AUDCAD-OTC", "EURGBP-OTC"
]
PAIR_LABELS = {
    "EURUSD-OTC": "EUR/USD (OTC)",
    "GBPUSD-OTC": "GBP/USD (OTC)",
    "AUDUSD-OTC": "AUD/USD (OTC)",
    "NZDUSD-OTC": "NZD/USD (OTC)",
    "AUDCAD-OTC": "AUD/CAD (OTC)",
    "EURGBP-OTC": "EUR/GBP (OTC)"
}

import numpy as np

class TradingEngine:
    def __init__(self, api, account_type="PRACTICE"):
        self.api          = api
        self.account_type = account_type
        self.today        = str(date.today())
        self.today_pnl    = 0.0
        self.today_wins   = 0
        self.today_losses = 0
        self.start_balance= 0.0

    def reset_day(self):
        self.today        = str(date.today())
        self.today_pnl    = 0.0
        self.today_wins   = 0
        self.today_losses = 0
        try:
            self.start_balance = self.api.get_balance()
        except:
            pass

    def check_day_reset(self):
        if str(date.today()) != self.today:
            return True
        return False

    def get_balance(self):
        try:
            return self.api.get_balance()
        except:
            return 0.0

    def get_signal(self):
        """Simple signal based on RSI from candles."""
        import random
        try:
            pair = random.choice(PAIRS)
            candles = self.api.get_candles(pair, 60, 30, time.time())
            if not candles or len(candles) < 14:
                return random.choice(["call", "put"]), pair

            closes = [c["close"] for c in candles]
            closes = np.array(closes, dtype=float)
            d = np.diff(closes)
            gains  = np.where(d > 0, d, 0)
            losses = np.where(d < 0, -d, 0)
            ag = np.mean(gains[:14])
            al = np.mean(losses[:14])
            rsi = 100 if al == 0 else 100 - (100 / (1 + ag / al))

            if rsi < 35:
                signal = "call"
            elif rsi > 65:
                signal = "put"
            else:
                signal = random.choice(["call", "put"])

            return signal, pair
        except Exception as e:
            log.error(f"Signal error: {e}")
            import random
            return random.choice(["call", "put"]), random.choice(PAIRS)

    def place_trade(self, signal, pair, amount, duration):
        try:
            ok, order_id = self.api.buy(amount, pair, signal, duration)
            if ok:
                return order_id
            return None
        except Exception as e:
            log.error(f"Trade error: {e}")
            return None

    def wait_and_get_result(self, amount, duration):
        """Get result by comparing balance before and after trade."""
        try:
            bal_before = self.api.get_balance()
            time.sleep(duration * 60 + 5)
            # Retry balance check up to 5 times
            for _ in range(5):
                try:
                    bal_after = self.api.get_balance()
                    diff = round(bal_after - bal_before, 2)
                    log.info(f"Balance before: {bal_before} after: {bal_after} diff: {diff}")
                    return diff
                except:
                    time.sleep(2)
            return 0
        except Exception as e:
            log.error(f"Balance check error: {e}")
            return 0

    def check_result(self, order_id, duration):
        """Wait for trade to expire then check result with multiple methods."""
        try:
            # Wait full duration + buffer
            time.sleep(duration * 60 + 5)

            # Method 1: check_win_v3 with retries
            for attempt in range(5):
                try:
                    result = self.api.check_win_v3(order_id)
                    if result is not None:
                        log.info(f"Result via check_win_v3: {result}")
                        return result
                except Exception as e:
                    log.error(f"check_win_v3 attempt {attempt+1}: {e}")
                time.sleep(2)

            # Method 2: check via get_async_order
            for attempt in range(5):
                try:
                    orders = self.api.get_optioninfo_v2(10)
                    if orders and "options" in orders:
                        for opt in orders["options"]:
                            if opt.get("id") == order_id:
                                profit = opt.get("profit", 0)
                                win    = opt.get("win", "")
                                if win == "win":
                                    return float(profit)
                                elif win == "loose":
                                    return -float(opt.get("amount", 0))
                                elif win == "equal":
                                    return 0
                except Exception as e:
                    log.error(f"optioninfo attempt {attempt+1}: {e}")
                time.sleep(2)

            log.warning(f"Could not get result for order {order_id} — treating as draw")
            return 0
        except Exception as e:
            log.error(f"check_result fatal error: {e}")
            return 0

# ─────────────────────────────────────────────
#  BOT THREADS
# ─────────────────────────────────────────────
bot_threads  = {}
user_engines = {}

def send_msg(chat_id, text):
    import asyncio
    if telegram_app:
        try:
            asyncio.run(telegram_app.bot.send_message(
                chat_id=chat_id, text=text, parse_mode="Markdown"
            ))
        except Exception as e:
            log.error(f"Send error: {e}")

def run_trading(chat_id):
    engine = user_engines.get(str(chat_id))
    if not engine:
        return

    # Set start balance for the day
    engine.start_balance = engine.get_balance()
    log.info(f"Bot started for {chat_id} | Balance: ${engine.start_balance:.2f}")

    while bot_threads.get(str(chat_id), {}).get("running", False):
        try:
            user = get_user(chat_id)

            # Check day reset
            if engine.check_day_reset():
                old_date  = engine.today
                end_bal   = engine.get_balance()
                added     = engine.today_pnl

                send_msg(chat_id,
                    f"🌅 *New Day — Stats Reset!*\n\n"
                    f"📅 Yesterday ({old_date}):\n"
                    f"• Wins: {engine.today_wins}\n"
                    f"• Losses: {engine.today_losses}\n"
                    f"• Added to account: ${added:+.2f}\n"
                    f"• End Balance: ${end_bal:.2f}\n\n"
                    f"🔄 Starting fresh today. Bot will chase your ${user.get('daily_goal', 30)} goal again!"
                )
                engine.reset_day()
                continue

            # Get settings
            daily_goal  = float(user.get("daily_goal",  30))
            trade_amount= float(user.get("trade_amount", 1))
            stop_loss   = float(user.get("stop_loss",   10))
            duration    = int(user.get("duration",       5))

            # Check daily goal reached
            if engine.today_pnl >= daily_goal:
                end_bal = engine.get_balance()
                send_msg(chat_id,
                    f"🎯 *Daily Goal Reached!*\n\n"
                    f"✅ Added ${engine.today_pnl:.2f} to your account today!\n"
                    f"🏦 Balance: ${end_bal:.2f}\n\n"
                    f"🛑 Bot stopped for today.\n"
                    f"⏰ Will reset automatically tomorrow and chase your ${daily_goal} goal again!"
                )
                bot_threads[str(chat_id)]["running"] = False

                # Save to history
                history = user.get("history", [])
                history.insert(0, {
                    "date":      str(date.today()),
                    "wins":      engine.today_wins,
                    "losses":    engine.today_losses,
                    "pnl":       round(engine.today_pnl, 2),
                    "start_bal": round(engine.start_balance, 2),
                    "end_bal":   round(end_bal, 2),
                    "goal":      daily_goal,
                    "goal_hit":  True
                })
                update_user(chat_id, history=history[:60])
                break

            # Check stop loss
            if engine.today_pnl <= -stop_loss:
                send_msg(chat_id,
                    f"🛑 *Daily Stop Loss Hit!*\n\n"
                    f"Lost ${abs(engine.today_pnl):.2f} today (limit: ${stop_loss}).\n"
                    f"Bot stopped to protect your account.\n\n"
                    f"⏰ Will reset tomorrow and try again."
                )
                bot_threads[str(chat_id)]["running"] = False

                end_bal = engine.get_balance()
                history = user.get("history", [])
                history.insert(0, {
                    "date":      str(date.today()),
                    "wins":      engine.today_wins,
                    "losses":    engine.today_losses,
                    "pnl":       round(engine.today_pnl, 2),
                    "start_bal": round(engine.start_balance, 2),
                    "end_bal":   round(end_bal, 2),
                    "goal":      daily_goal,
                    "goal_hit":  False
                })
                update_user(chat_id, history=history[:60])
                break

            # Get signal
            signal, pair = engine.get_signal()
            pair_label   = PAIR_LABELS.get(pair, pair)
            dir_label    = "📈 CALL (UP)" if signal == "call" else "📉 PUT (DOWN)"
            remaining    = daily_goal - engine.today_pnl

            send_msg(chat_id,
                f"⚡ *Trade Placed!*\n\n"
                f"Pair: {pair_label}\n"
                f"Direction: {dir_label}\n"
                f"Amount: ${trade_amount:.2f}\n"
                f"Duration: {duration} min\n"
                f"Today's P&L: ${engine.today_pnl:+.2f}\n"
                f"Goal remaining: ${remaining:.2f}\n"
                f"_Waiting for result..._"
            )

            # Place trade
            order_id = engine.place_trade(signal, pair, trade_amount, duration)

            if order_id:
                result = engine.check_result(order_id, duration)

                engine.today_pnl = round(engine.today_pnl + result, 2)
                if result > 0:
                    engine.today_wins += 1
                    outcome = f"✅ *WIN!* +${result:.2f}"
                elif result == 0:
                    outcome = "🤝 *DRAW* — Amount returned"
                else:
                    engine.today_losses += 1
                    outcome = f"❌ *LOSS* -${abs(result):.2f}"

                balance = engine.get_balance()
                send_msg(chat_id,
                    f"🏁 *Trade Result*\n\n"
                    f"{outcome}\n\n"
                    f"📊 Today's Stats:\n"
                    f"• Wins: {engine.today_wins} | Losses: {engine.today_losses}\n"
                    f"• Today's P&L: ${engine.today_pnl:+.2f}\n"
                    f"• Goal: ${engine.today_pnl:.2f} / ${daily_goal:.2f}\n"
                    f"• Balance: ${balance:.2f}"
                )
            else:
                send_msg(chat_id, "⚠️ Trade could not be placed. Retrying in 30 seconds...")
                time.sleep(30)

        except Exception as e:
            log.error(f"Trading error for {chat_id}: {e}")
            time.sleep(30)

    # Update stop state in Telegram
    try:
        import asyncio
        if telegram_app:
            asyncio.run(telegram_app.bot.send_message(
                chat_id=chat_id,
                text="🤖 Bot is now idle. Use /start to see options.",
                parse_mode="Markdown"
            ))
    except:
        pass

    log.info(f"Bot stopped for {chat_id}")

def start_thread(chat_id):
    stop_thread(chat_id)
    bot_threads[str(chat_id)] = {"running": True}
    t = threading.Thread(target=run_trading, args=(chat_id,), daemon=True)
    t.start()

def stop_thread(chat_id):
    if str(chat_id) in bot_threads:
        bot_threads[str(chat_id)]["running"] = False

# ─────────────────────────────────────────────
#  TELEGRAM HANDLERS
# ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user    = get_user(chat_id)

    kb = [[InlineKeyboardButton("🚀 Get Started", callback_data="get_started")]]
    await update.message.reply_text(
        "👋 *Welcome To SFX Auto Bot*\n\n"
        "Your fully automated IQ Option trading bot.\n\n"
        "🎯 You set a daily profit goal\n"
        "🤖 The bot trades automatically until it hits that goal\n"
        "🔄 Resets every new day and starts again\n"
        "📊 You can change trade amount anytime\n\n"
        "📌 *Demo account* — FREE (unlimited)\n"
        "📌 *Live account* — ₦5,000/month\n\n"
        "Tap *Get Started* to begin 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    chat_id = q.from_user.id
    data    = q.data
    await q.answer()

    if data == "get_started":
        kb = [
            [InlineKeyboardButton("🟡 Demo Account", callback_data="account_demo")],
            [InlineKeyboardButton("💚 Live Account", callback_data="account_live")]
        ]
        await q.message.reply_text(
            "Which account would you like to trade on?",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == "account_demo":
        update_user(chat_id, state="awaiting_credentials", account_type="PRACTICE")
        await q.message.reply_text(
            "Send your IQ Option login details in this format:\n\n"
            "Email (youremail@gmail.com)\n"
            "Password (yourpassword)"
        )

    elif data == "account_live":
        if is_admin(chat_id) or is_subscribed(chat_id):
            update_user(chat_id, state="awaiting_credentials", account_type="REAL")
            await q.message.reply_text(
                "Send your IQ Option login details in this format:\n\n"
                "Email (youremail@gmail.com)\n"
                "Password (yourpassword)"
            )
        else:
            kb = [[InlineKeyboardButton("🏦 Show Bank Details", callback_data="show_bank")]]
            await q.message.reply_text(
                "Thanks for choosing the live account! 🙏\n\n"
                "Live account access costs *₦5,000/month*.\n\n"
                "Tap *Show Bank Details* to make payment 👇",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )

    elif data == "show_bank":
        update_user(chat_id, state="awaiting_payment_receipt")
        await q.message.reply_text(
            "🏦 *Payment Details*\n\n"
            "Account Number: *2083703665*\n"
            "Bank Name: *Kuda*\n"
            "Account Name: *Arogundade, Olatunde Sameed*\n\n"
            "Send your payment receipt after paying ✅",
            parse_mode="Markdown"
        )

    elif data.startswith("approve_"):
        if is_admin(chat_id):
            tid = int(data.split("_")[1])
            update_user(tid, subscribed=True, state="awaiting_credentials", account_type="REAL")
            await q.message.reply_text(f"✅ User {tid} approved!")
            await context.bot.send_message(
                chat_id=tid,
                text=(
                    "✅ *Payment Approved! Welcome to SFX Live!*\n\n"
                    "Send your IQ Option login details in this format:\n\n"
                    "Email (youremail@gmail.com)\n"
                    "Password (yourpassword)"
                ),
                parse_mode="Markdown"
            )

    elif data.startswith("disapprove_"):
        if is_admin(chat_id):
            tid = int(data.split("_")[1])
            update_user(tid, state="idle")
            await q.message.reply_text(f"❌ User {tid} disapproved.")
            kb = [[InlineKeyboardButton("🏦 Show Bank Details", callback_data="show_bank")]]
            await context.bot.send_message(
                chat_id=tid,
                text=(
                    "❌ *Payment Not Confirmed*\n\n"
                    "Your payment could not be verified. "
                    "Please try again with the correct receipt.\n\n"
                    "Tap below to see bank details 👇"
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )

    elif data == "stop_bot":
        stop_thread(chat_id)
        update_user(chat_id, bot_running=False)
        await q.message.reply_text(
            "🛑 *Bot Stopped*\n\n"
            "Use /start to start again anytime.",
            parse_mode="Markdown"
        )

    elif data == "switch_demo":
        stop_thread(chat_id)
        update_user(chat_id, account_type="PRACTICE", state="awaiting_credentials")
        await q.message.reply_text(
            "Switching to Demo account.\n\n"
            "Send your IQ Option login details:\n\n"
            "Email (youremail@gmail.com)\n"
            "Password (yourpassword)"
        )

    elif data == "switch_live":
        if is_admin(chat_id) or is_subscribed(chat_id):
            stop_thread(chat_id)
            update_user(chat_id, account_type="REAL", state="awaiting_credentials")
            await q.message.reply_text(
                "Switching to Live account.\n\n"
                "Send your IQ Option login details:\n\n"
                "Email (youremail@gmail.com)\n"
                "Password (yourpassword)"
            )
        else:
            kb = [[InlineKeyboardButton("🏦 Show Bank Details", callback_data="show_bank")]]
            await q.message.reply_text(
                "Live account costs *₦5,000/month*.\n\n"
                "Tap below to pay 👇",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text    = update.message.text.strip() if update.message.text else ""
    user    = get_user(chat_id)
    state   = user.get("state", "idle")

    # ── Commands typed as text ──
    if text.lower() == "switch to live account":
        if is_admin(chat_id) or is_subscribed(chat_id):
            stop_thread(chat_id)
            update_user(chat_id, account_type="REAL", state="awaiting_credentials")
            await update.message.reply_text(
                "Switching to Live.\n\nSend your IQ Option login:\n\n"
                "Email (youremail@gmail.com)\n"
                "Password (yourpassword)"
            )
        else:
            kb = [[InlineKeyboardButton("🏦 Show Bank Details", callback_data="show_bank")]]
            await update.message.reply_text(
                "Live account costs *₦5,000/month* 👇",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        return

    if text.lower() == "switch to demo account":
        stop_thread(chat_id)
        update_user(chat_id, account_type="PRACTICE", state="awaiting_credentials")
        await update.message.reply_text(
            "Switching to Demo.\n\nSend your IQ Option login:\n\n"
            "Email (youremail@gmail.com)\n"
            "Password (yourpassword)"
        )
        return

    # ── Awaiting credentials ──
    if state == "awaiting_credentials":
        import re
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) < 2:
            await update.message.reply_text(
                "Please send in the correct format:\n\n"
                "Email (youremail@gmail.com)\n"
                "Password (yourpassword)"
            )
            return

        def extract(line):
            m = re.search(r'\((.+)\)', line)
            return m.group(1).strip() if m else line.strip()

        email        = extract(lines[0])
        password     = extract(lines[1])
        account_type = user.get("account_type", "PRACTICE")

        await update.message.reply_text("⏳ Connecting to IQ Option...")

        api, error = connect_iqoption(email, password, account_type)

        if api:
            active_apis[str(chat_id)] = api
            balance = api.get_balance()
            acc_label = "🟡 Demo" if account_type == "PRACTICE" else "💚 Live"

            engine = TradingEngine(api, account_type)
            engine.start_balance = balance
            user_engines[str(chat_id)] = engine

            update_user(chat_id,
                email=email, password=password,
                account_type=account_type, state="connected",
                daily_goal=user.get("daily_goal", 30),
                trade_amount=user.get("trade_amount", 1),
                stop_loss=user.get("stop_loss", 10),
                duration=user.get("duration", 5)
            )

            kb = [
                [InlineKeyboardButton("▶ Start Bot", callback_data="start_bot")],
                [InlineKeyboardButton("⚙️ Settings", callback_data="settings")]
            ]
            await update.message.reply_text(
                f"✅ *Connected Successfully!*\n\n"
                f"Account: {acc_label}\n"
                f"Balance: *${balance:.2f}*\n"
                f"Email: {email}\n\n"
                f"Daily Goal: *${user.get('daily_goal', 30):.2f}*\n"
                f"Trade Amount: *${user.get('trade_amount', 1):.2f}*\n"
                f"Duration: *{user.get('duration', 5)} min*\n\n"
                f"Tap *Start Bot* to begin trading 👇\n\n"
                f"_Commands:_\n"
                f"/settings — Change goal, amount, duration\n"
                f"/status — Check bot status\n"
                f"/history — View daily history\n"
                f"/stop — Stop the bot",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        else:
            await update.message.reply_text(
                f"❌ *Couldn't connect!*\n\n"
                f"Error: {error or 'Invalid credentials'}\n\n"
                f"Please check your email and password and try again:\n\n"
                f"Email (youremail@gmail.com)\n"
                f"Password (yourpassword)",
                parse_mode="Markdown"
            )
        return

    # ── Settings input ──
    if state == "awaiting_goal":
        try:
            goal = float(text.replace("$","").strip())
            if goal <= 0: raise ValueError
            update_user(chat_id, daily_goal=goal, state="connected")
            await update.message.reply_text(
                f"✅ Daily goal set to *${goal:.2f}*\n\n"
                f"The bot will stop automatically when your account grows by ${goal:.2f} in a day.",
                parse_mode="Markdown"
            )
        except:
            await update.message.reply_text("Please send a valid amount e.g. *30* or *50*", parse_mode="Markdown")
        return

    if state == "awaiting_amount":
        try:
            amount = float(text.replace("$","").strip())
            if amount <= 0: raise ValueError
            update_user(chat_id, trade_amount=amount, state="connected")
            await update.message.reply_text(
                f"✅ Trade amount set to *${amount:.2f}* per trade.",
                parse_mode="Markdown"
            )
        except:
            await update.message.reply_text("Please send a valid amount e.g. *1* or *5*", parse_mode="Markdown")
        return

    if state == "awaiting_stoploss":
        try:
            sl = float(text.replace("$","").strip())
            if sl <= 0: raise ValueError
            update_user(chat_id, stop_loss=sl, state="connected")
            await update.message.reply_text(
                f"✅ Daily stop loss set to *${sl:.2f}*\n\n"
                f"Bot will stop if you lose more than ${sl:.2f} in a day.",
                parse_mode="Markdown"
            )
        except:
            await update.message.reply_text("Please send a valid amount e.g. *10*", parse_mode="Markdown")
        return

    if state == "awaiting_duration":
        try:
            dur = int(text.strip())
            if dur not in [1,2,5,10,15]: raise ValueError
            update_user(chat_id, duration=dur, state="connected")
            await update.message.reply_text(
                f"✅ Trade duration set to *{dur} minutes*.",
                parse_mode="Markdown"
            )
        except:
            await update.message.reply_text(
                "Please send one of these values:\n*1, 2, 5, 10, 15*",
                parse_mode="Markdown"
            )
        return

    # ── Payment receipt ──
    if state == "awaiting_payment_receipt":
        username = update.message.from_user.username or update.message.from_user.first_name
        caption  = (
            f"💳 *Payment Receipt*\n\n"
            f"From: @{username}\n"
            f"Chat ID: `{chat_id}`\n\n"
            f"Approve or disapprove:"
        )
        kb = [[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{chat_id}"),
            InlineKeyboardButton("❌ Disapprove", callback_data=f"disapprove_{chat_id}")
        ]]
        if update.message.photo:
            await context.bot.send_photo(
                chat_id=ADMIN_CHAT_ID, photo=update.message.photo[-1].file_id,
                caption=caption, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        else:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=caption + f"\n\nMessage: {text}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        await update.message.reply_text(
            "✅ Receipt sent! Please wait while we verify your payment. "
            "You'll be notified once approved. 🙏"
        )
        return

    await update.message.reply_text(
        "Use /start to begin or /settings to configure the bot.",
        parse_mode="Markdown"
    )

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await msg_handler(update, context)

# ── Start bot callback ──
async def start_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    chat_id = q.from_user.id
    await q.answer()

    user   = get_user(chat_id)
    engine = user_engines.get(str(chat_id))

    if not engine:
        await q.message.reply_text("Please connect your IQ Option account first. Use /start")
        return

    if bot_threads.get(str(chat_id), {}).get("running", False):
        await q.message.reply_text("🤖 Bot is already running! Use /stop to stop it.")
        return

    goal   = float(user.get("daily_goal",   30))
    amount = float(user.get("trade_amount",  1))
    stop_l = float(user.get("stop_loss",    10))
    dur    = int(user.get("duration",        5))
    bal    = engine.get_balance()

    await q.message.reply_text(
        f"🚀 *Bot Started!*\n\n"
        f"💰 Balance: ${bal:.2f}\n"
        f"🎯 Daily Goal: +${goal:.2f}\n"
        f"💵 Per Trade: ${amount:.2f}\n"
        f"🛑 Stop Loss: ${stop_l:.2f}\n"
        f"⏱ Duration: {dur} min\n\n"
        f"The bot will trade automatically and stop once your account grows by ${goal:.2f} today.\n\n"
        f"Use /stop to stop anytime.",
        parse_mode="Markdown"
    )

    start_thread(chat_id)

# ── Commands ──
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user    = get_user(chat_id)
    engine  = user_engines.get(str(chat_id))
    running = bot_threads.get(str(chat_id), {}).get("running", False)

    if not engine:
        await update.message.reply_text("Not connected. Use /start to connect.")
        return

    balance  = engine.get_balance()
    goal     = float(user.get("daily_goal", 30))
    pct      = min((engine.today_pnl / goal * 100), 100) if goal > 0 else 0
    bar_len  = int(pct / 10)
    bar      = "█" * bar_len + "░" * (10 - bar_len)

    await update.message.reply_text(
        f"📊 *Bot Status*\n\n"
        f"Status: {'🟢 Running' if running else '🔴 Stopped'}\n"
        f"Account: {'🟡 Demo' if user.get('account_type')=='PRACTICE' else '💚 Live'}\n"
        f"Balance: *${balance:.2f}*\n\n"
        f"📅 *Today's Progress*\n"
        f"Goal: ${engine.today_pnl:.2f} / ${goal:.2f}\n"
        f"[{bar}] {pct:.0f}%\n"
        f"Wins: {engine.today_wins} | Losses: {engine.today_losses}\n"
        f"P&L: ${engine.today_pnl:+.2f}\n\n"
        f"⚙️ Settings:\n"
        f"• Trade amount: ${user.get('trade_amount', 1):.2f}\n"
        f"• Duration: {user.get('duration', 5)} min\n"
        f"• Stop loss: ${user.get('stop_loss', 10):.2f}",
        parse_mode="Markdown"
    )

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    stop_thread(chat_id)
    await update.message.reply_text(
        "🛑 *Bot Stopped*\n\nUse /start to start again.",
        parse_mode="Markdown"
    )

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user    = get_user(chat_id)
    kb = [
        [InlineKeyboardButton("🎯 Change Daily Goal",    callback_data="set_goal")],
        [InlineKeyboardButton("💵 Change Trade Amount",  callback_data="set_amount")],
        [InlineKeyboardButton("🛑 Change Stop Loss",     callback_data="set_stoploss")],
        [InlineKeyboardButton("⏱ Change Duration",      callback_data="set_duration")],
        [InlineKeyboardButton("🔄 Switch to Demo",       callback_data="switch_demo")],
        [InlineKeyboardButton("💚 Switch to Live",       callback_data="switch_live")],
    ]
    await update.message.reply_text(
        f"⚙️ *Settings*\n\n"
        f"🎯 Daily goal: ${user.get('daily_goal', 30):.2f}\n"
        f"💵 Trade amount: ${user.get('trade_amount', 1):.2f}\n"
        f"🛑 Stop loss: ${user.get('stop_loss', 10):.2f}\n"
        f"⏱ Duration: {user.get('duration', 5)} min\n"
        f"Account: {'🟡 Demo' if user.get('account_type')=='PRACTICE' else '💚 Live'}\n\n"
        f"Tap what you want to change 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user    = get_user(chat_id)
    history = user.get("history", [])

    if not history:
        await update.message.reply_text("No history yet. Start trading to see daily results!")
        return

    msg = "📅 *Daily Trading History*\n\n"
    for d in history[:7]:
        diff     = d["end_bal"] - d["start_bal"]
        goal_hit = "🎯" if d.get("goal_hit") else "❌"
        msg += (
            f"{goal_hit} *{d['date']}*\n"
            f"Start: ${d['start_bal']:.2f} → End: ${d['end_bal']:.2f}\n"
            f"Added: ${diff:+.2f} | Goal: ${d['goal']:.2f}\n"
            f"W: {d['wins']} L: {d['losses']} P&L: ${d['pnl']:+.2f}\n\n"
        )

    await update.message.reply_text(msg, parse_mode="Markdown")

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.chat_id): return
    data  = load_data()
    total = len(data)
    subs  = sum(1 for u in data.values() if u.get("subscribed"))
    demo  = sum(1 for u in data.values() if u.get("account_type") == "PRACTICE")
    live  = sum(1 for u in data.values() if u.get("account_type") == "REAL")
    await update.message.reply_text(
        f"👥 *SFX Bot Users*\n\n"
        f"Total: {total}\nSubscribed (live): {subs}\nDemo: {demo}\nLive: {live}",
        parse_mode="Markdown"
    )

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.chat_id): return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast Your message here")
        return
    data, sent, failed = load_data(), 0, 0
    for uid in data.keys():
        try:
            await context.bot.send_message(chat_id=int(uid), text=msg)
            sent += 1
        except:
            failed += 1
    await update.message.reply_text(f"📢 Sent to {sent} users. Failed: {failed}")

# Settings callbacks
async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    chat_id = q.from_user.id
    data    = q.data
    await q.answer()

    if data == "start_bot":
        await start_bot_callback(update, context)

    elif data == "settings":
        await cmd_settings_from_callback(update, context)

    elif data == "set_goal":
        update_user(chat_id, state="awaiting_goal")
        await q.message.reply_text(
            "🎯 *Set Daily Profit Goal*\n\n"
            "How much profit do you want the bot to add to your account each day?\n\n"
            "Send the amount e.g: *30* for $30",
            parse_mode="Markdown"
        )

    elif data == "set_amount":
        update_user(chat_id, state="awaiting_amount")
        await q.message.reply_text(
            "💵 *Set Trade Amount*\n\n"
            "How much should the bot trade per trade?\n\n"
            "Send the amount e.g: *1* for $1",
            parse_mode="Markdown"
        )

    elif data == "set_stoploss":
        update_user(chat_id, state="awaiting_stoploss")
        await q.message.reply_text(
            "🛑 *Set Daily Stop Loss*\n\n"
            "The bot will stop if losses exceed this amount in a day.\n\n"
            "Send the amount e.g: *10* for $10",
            parse_mode="Markdown"
        )

    elif data == "set_duration":
        update_user(chat_id, state="awaiting_duration")
        await q.message.reply_text(
            "⏱ *Set Trade Duration*\n\n"
            "How long should each trade last?\n\n"
            "Send one of: *1, 2, 5, 10, 15* (minutes)",
            parse_mode="Markdown"
        )

    elif data in ("switch_demo", "switch_live", "stop_bot", "approve_", "disapprove_"):
        await button_handler(update, context)

    else:
        await button_handler(update, context)

async def cmd_settings_from_callback(update, context):
    q       = update.callback_query
    chat_id = q.from_user.id
    user    = get_user(chat_id)
    kb = [
        [InlineKeyboardButton("🎯 Change Daily Goal",   callback_data="set_goal")],
        [InlineKeyboardButton("💵 Change Trade Amount", callback_data="set_amount")],
        [InlineKeyboardButton("🛑 Change Stop Loss",    callback_data="set_stoploss")],
        [InlineKeyboardButton("⏱ Change Duration",     callback_data="set_duration")],
        [InlineKeyboardButton("🔄 Switch to Demo",      callback_data="switch_demo")],
        [InlineKeyboardButton("💚 Switch to Live",      callback_data="switch_live")],
    ]
    await q.message.reply_text(
        f"⚙️ *Settings*\n\n"
        f"🎯 Daily goal: ${user.get('daily_goal', 30):.2f}\n"
        f"💵 Trade amount: ${user.get('trade_amount', 1):.2f}\n"
        f"🛑 Stop loss: ${user.get('stop_loss', 10):.2f}\n"
        f"⏱ Duration: {user.get('duration', 5)} min\n\n"
        f"Tap what you want to change 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    global telegram_app
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    telegram_app = app

    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("stop",      cmd_stop))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("settings",  cmd_settings))
    app.add_handler(CommandHandler("history",   cmd_history))
    app.add_handler(CommandHandler("users",     admin_users))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
    app.add_handler(CallbackQueryHandler(settings_callback))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))

    log.info("🚀 SFX Auto Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
