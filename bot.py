"""
SFX Trading Bot - 5 Strategy Confluence System
Admin: 8319282451
"""
import os, json, time, logging, threading, random, requests
import numpy as np
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from iqoptionapi.stable_api import IQ_Option

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_CHAT_ID  = 8319282451
DATA_FILE      = "users.json"
CONFIDENCE_THRESHOLD = 85  # Minimum confidence to place trade

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
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
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
    return {
        "inline_keyboard": [
            [{"text": b[0], "callback_data": b[1]} for b in row]
            for row in buttons
        ]
    }

def show_main_buttons(chat_id, balance):
    user     = get_user(chat_id)
    amount   = float(user.get("trade_amount", 1))
    acc_type = "Demo" if user.get("account_type","PRACTICE")=="PRACTICE" else "Live"
    kb = make_keyboard([
        [("⚡ Place a Trade", "place_trade")],
        [("⚙️ Settings",      "settings")]
    ])
    send_msg(chat_id,
        f"💰 *Balance: ${balance:.2f}*\n"
        f"Account: {'🟡' if acc_type=='Demo' else '💚'} {acc_type}\n"
        f"💵 Trade Amount: ${amount:.2f}\n\n"
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
PAIRS = ["EURUSD-OTC","GBPUSD-OTC","AUDUSD-OTC","NZDUSD-OTC","AUDCAD-OTC","EURGBP-OTC"]
PAIR_LABELS = {
    "EURUSD-OTC":"EUR/USD (OTC)","GBPUSD-OTC":"GBP/USD (OTC)",
    "AUDUSD-OTC":"AUD/USD (OTC)","NZDUSD-OTC":"NZD/USD (OTC)",
    "AUDCAD-OTC":"AUD/CAD (OTC)","EURGBP-OTC":"EUR/GBP (OTC)"
}

# ── TECHNICAL INDICATORS ──────────────────────
def ema(closes, period):
    closes = np.array(closes, dtype=float)
    k = 2/(period+1)
    v = closes[0]
    for c in closes[1:]: v = c*k + v*(1-k)
    return v

def ema_series(closes, period):
    closes = np.array(closes, dtype=float)
    k = 2/(period+1)
    result = [closes[0]]
    for c in closes[1:]:
        result.append(c*k + result[-1]*(1-k))
    return np.array(result)

def rsi(closes, period=14):
    closes = np.array(closes, dtype=float)
    d = np.diff(closes)
    gains  = np.where(d>0, d, 0)
    losses = np.where(d<0, -d, 0)
    ag = np.mean(gains[:period])
    al = np.mean(losses[:period])
    if al == 0: return 100.0
    return 100 - (100/(1+ag/al))

def atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["max"]; l = candles[i]["min"]; pc = candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    if not trs: return 0
    return float(np.mean(trs[-period:]))

def adx(candles, period=14):
    """Simplified ADX calculation."""
    try:
        highs  = [c["max"]   for c in candles]
        lows   = [c["min"]   for c in candles]
        closes = [c["close"] for c in candles]
        plus_dm, minus_dm, true_range = [], [], []
        for i in range(1, len(candles)):
            up   = highs[i]  - highs[i-1]
            down = lows[i-1] - lows[i]
            plus_dm.append(up   if up > down and up > 0   else 0)
            minus_dm.append(down if down > up and down > 0 else 0)
            h=highs[i]; l=lows[i]; pc=closes[i-1]
            true_range.append(max(h-l, abs(h-pc), abs(l-pc)))
        if len(true_range) < period: return 20.0
        atr_v  = np.mean(true_range[-period:])
        if atr_v == 0: return 20.0
        pdi    = (np.mean(plus_dm[-period:])  / atr_v) * 100
        mdi    = (np.mean(minus_dm[-period:]) / atr_v) * 100
        dx     = abs(pdi - mdi) / (pdi + mdi + 1e-10) * 100
        return float(dx)
    except: return 20.0

def bollinger_bands(closes, period=20, std_dev=2):
    closes = np.array(closes, dtype=float)
    if len(closes) < period: return None, None, None
    sma    = np.mean(closes[-period:])
    std    = np.std(closes[-period:])
    return sma + std_dev*std, sma, sma - std_dev*std

def macd(closes):
    closes = np.array(closes, dtype=float)
    if len(closes) < 26: return 0, 0, 0
    e12 = ema_series(closes, 12)
    e26 = ema_series(closes, 26)
    macd_line   = e12 - e26
    signal_line = ema_series(macd_line, 9)
    histogram   = macd_line - signal_line
    return float(macd_line[-1]), float(signal_line[-1]), float(histogram[-1])

def support_resistance(closes, highs, lows, lookback=20):
    """Find recent support and resistance levels."""
    recent_highs = highs[-lookback:]
    recent_lows  = lows[-lookback:]
    resistance   = float(np.max(recent_highs))
    support      = float(np.min(recent_lows))
    return support, resistance

def is_bullish_candle(candle):
    return candle["close"] > candle["open"]

def is_bearish_candle(candle):
    return candle["close"] < candle["open"]

def is_engulfing(c1, c2, direction="bull"):
    if direction == "bull":
        return (c2["close"] > c2["open"] and
                c1["close"] < c1["open"] and
                c2["close"] > c1["open"] and
                c2["open"]  < c1["close"])
    else:
        return (c2["close"] < c2["open"] and
                c1["close"] > c1["open"] and
                c2["close"] < c1["open"] and
                c2["open"]  > c1["close"])

def is_pin_bar(candle, direction="bull"):
    body  = abs(candle["close"] - candle["open"])
    total = candle["max"] - candle["min"]
    if total == 0: return False
    if direction == "bull":
        lower_wick = min(candle["open"], candle["close"]) - candle["min"]
        return lower_wick > body * 2 and body / total < 0.35
    else:
        upper_wick = candle["max"] - max(candle["open"], candle["close"])
        return upper_wick > body * 2 and body / total < 0.35

# ── 5 STRATEGIES ──────────────────────────────

def strategy1_trend_pullback(candles_1m):
    """Strategy 1: Trend Pullback Continuation."""
    try:
        if len(candles_1m) < 55: return None, 0

        closes = [c["close"] for c in candles_1m]
        highs  = [c["max"]   for c in candles_1m]
        lows   = [c["min"]   for c in candles_1m]

        ema20  = ema(closes[-20:], 20)
        ema50  = ema(closes[-50:], 50)
        rsi_v  = rsi(closes[-20:])
        atr_v  = atr(candles_1m[-20:])
        adx_v  = adx(candles_1m[-20:])
        price  = closes[-1]
        prev_c = candles_1m[-2]
        last_c = candles_1m[-1]

        if adx_v < 20 or atr_v < 0.0001:
            return None, 0

        score = 0

        # BUY setup
        if ema20 > ema50:
            score += 20
            if adx_v > 25: score += 20
            if ema50 < price < ema20 * 1.005: score += 20
            if 40 <= rsi_v <= 55: score += 20
            if is_engulfing(prev_c, last_c, "bull") or is_pin_bar(last_c, "bull") or is_bullish_candle(last_c): score += 20
            if score >= 60:
                return "call", score

        # SELL setup
        if ema20 < ema50:
            score += 20
            if adx_v > 25: score += 20
            if ema50 * 0.995 < price < ema20: score += 20
            if 45 <= rsi_v <= 60: score += 20
            if is_engulfing(prev_c, last_c, "bear") or is_pin_bar(last_c, "sell") or is_bearish_candle(last_c): score += 20
            if score >= 60:
                return "put", score

        return None, 0
    except Exception as e:
        log.error(f"Strategy1 error: {e}")
        return None, 0

def strategy2_breakout(candles_1m):
    """Strategy 2: Support & Resistance Breakout."""
    try:
        if len(candles_1m) < 25: return None, 0

        closes  = [c["close"] for c in candles_1m]
        highs   = [c["max"]   for c in candles_1m]
        lows    = [c["min"]   for c in candles_1m]
        volumes = [c.get("volume", 1) for c in candles_1m]

        support, resistance = support_resistance(closes[:-3], highs[:-3], lows[:-3])
        atr_v     = atr(candles_1m[-15:])
        avg_body  = np.mean([abs(c["close"]-c["open"]) for c in candles_1m[-10:-1]])
        last_c    = candles_1m[-1]
        prev_c    = candles_1m[-2]
        last_body = abs(last_c["close"] - last_c["open"])
        avg_vol   = np.mean(volumes[-10:-1])
        last_vol  = volumes[-1]

        score = 0

        # BUY breakout
        if last_c["close"] > resistance and last_body > avg_body:
            score += 30
            if atr_v > np.mean([atr(candles_1m[i:i+14]) for i in range(5)]): score += 20
            if last_vol > avg_vol: score += 20
            if prev_c["close"] > resistance: score += 20  # next candle holds
            wick = last_c["max"] - max(last_c["open"], last_c["close"])
            if wick < last_body * 0.5: score += 10
            return "call", score

        # SELL breakout
        if last_c["close"] < support and last_body > avg_body:
            score += 30
            if atr_v > np.mean([atr(candles_1m[i:i+14]) for i in range(5)]): score += 20
            if last_vol > avg_vol: score += 20
            if prev_c["close"] < support: score += 20
            wick = min(last_c["open"], last_c["close"]) - last_c["min"]
            if wick < last_body * 0.5: score += 10
            return "put", score

        return None, 0
    except Exception as e:
        log.error(f"Strategy2 error: {e}")
        return None, 0

def strategy3_reversal(candles_1m):
    """Strategy 3: Support & Resistance Reversal."""
    try:
        if len(candles_1m) < 25: return None, 0

        closes = [c["close"] for c in candles_1m]
        highs  = [c["max"]   for c in candles_1m]
        lows   = [c["min"]   for c in candles_1m]

        support, resistance = support_resistance(closes[:-3], highs[:-3], lows[:-3])
        rsi_v       = rsi(closes[-15:])
        bb_upper, bb_mid, bb_lower = bollinger_bands(closes)
        last_c      = candles_1m[-1]
        prev_c      = candles_1m[-2]
        price       = closes[-1]

        if bb_lower is None: return None, 0

        score = 0

        # BUY reversal at support
        if price <= support * 1.002:
            score += 25
            if rsi_v < 35: score += 25
            if last_c["low"] <= bb_lower: score += 20
            if is_engulfing(prev_c, last_c, "bull") or is_pin_bar(last_c, "bull"): score += 20
            if is_bullish_candle(last_c): score += 10
            if score >= 50:
                return "call", score

        # SELL reversal at resistance
        if price >= resistance * 0.998:
            score += 25
            if rsi_v > 65: score += 25
            if last_c["high"] >= bb_upper: score += 20
            if is_engulfing(prev_c, last_c, "bear") or is_pin_bar(last_c, "sell"): score += 20
            if is_bearish_candle(last_c): score += 10
            if score >= 50:
                return "put", score

        return None, 0
    except Exception as e:
        log.error(f"Strategy3 error: {e}")
        return None, 0

def strategy4_multi_timeframe(candles_1m, candles_5m):
    """Strategy 4: Multi-Timeframe Trend Confirmation."""
    try:
        if len(candles_1m) < 20 or len(candles_5m) < 20: return None, 0

        closes_5m = [c["close"] for c in candles_5m]
        closes_1m = [c["close"] for c in candles_1m]

        ema20_5m  = ema(closes_5m[-20:], 20)
        ema50_5m  = ema(closes_5m[-20:], min(50, len(closes_5m)))
        price_5m  = closes_5m[-1]
        price_1m  = closes_1m[-1]
        ema20_1m  = ema(closes_1m[-20:], 20)
        last_c    = candles_1m[-1]
        prev_c    = candles_1m[-2]

        score = 0

        # 5m bullish trend → look for 1m pullback + BUY
        if ema20_5m > ema50_5m and price_5m > ema20_5m:
            score += 30
            # 1m pullback to ema20
            if closes_1m[-3] < ema20_1m and price_1m > ema20_1m:
                score += 30
            if is_bullish_candle(last_c) or is_engulfing(prev_c, last_c, "bull"):
                score += 40
            if score >= 60:
                return "call", score

        # 5m bearish trend → look for 1m pullback + SELL
        if ema20_5m < ema50_5m and price_5m < ema20_5m:
            score += 30
            if closes_1m[-3] > ema20_1m and price_1m < ema20_1m:
                score += 30
            if is_bearish_candle(last_c) or is_engulfing(prev_c, last_c, "bear"):
                score += 40
            if score >= 60:
                return "put", score

        return None, 0
    except Exception as e:
        log.error(f"Strategy4 error: {e}")
        return None, 0

def strategy5_momentum(candles_1m):
    """Strategy 5: Momentum Confluence."""
    try:
        if len(candles_1m) < 55: return None, 0

        closes  = [c["close"] for c in candles_1m]
        price   = closes[-1]
        ema50_v = ema(closes[-50:], 50)
        atr_v   = atr(candles_1m[-14:])
        adx_v   = adx(candles_1m[-20:])
        m_line, s_line, hist = macd(closes)

        if adx_v < 25 or atr_v < 0.0001:
            return None, 0

        score = 0
        prev_closes = closes[:-1]
        prev_m, prev_s, prev_hist = macd(prev_closes)

        # BUY: price above EMA50, MACD bullish crossover, histogram increasing
        if price > ema50_v:
            score += 20
            if m_line > s_line and prev_m <= prev_s: score += 30  # crossover
            elif m_line > s_line: score += 15
            if hist > prev_hist and hist > 0: score += 25
            if adx_v > 25: score += 15
            if atr_v > 0.0002: score += 10
            if score >= 60:
                return "call", score

        # SELL: price below EMA50, MACD bearish crossover, histogram decreasing
        if price < ema50_v:
            score += 20
            if m_line < s_line and prev_m >= prev_s: score += 30  # crossover
            elif m_line < s_line: score += 15
            if hist < prev_hist and hist < 0: score += 25
            if adx_v > 25: score += 15
            if atr_v > 0.0002: score += 10
            if score >= 60:
                return "put", score

        return None, 0
    except Exception as e:
        log.error(f"Strategy5 error: {e}")
        return None, 0

# ── CONFLUENCE ENGINE ─────────────────────────
def analyse_market(api, pair):
    """
    Run all 5 strategies. 
    Returns (signal, confidence, duration, details) or (None, 0, 0, details).
    """
    try:
        # Fetch candles
        candles_1m = api.get_candles(pair, 60,    60, time.time())
        candles_5m = api.get_candles(pair, 300,   30, time.time())
        time.sleep(1)

        if not candles_1m or len(candles_1m) < 20:
            return None, 0, 1, ["Not enough data"]

        # Run all 5 strategies
        s1_sig, s1_score = strategy1_trend_pullback(candles_1m)
        s2_sig, s2_score = strategy2_breakout(candles_1m)
        s3_sig, s3_score = strategy3_reversal(candles_1m)
        s4_sig, s4_score = strategy4_multi_timeframe(candles_1m, candles_5m or candles_1m)
        s5_sig, s5_score = strategy5_momentum(candles_1m)

        results = [
            ("Trend Pullback",    s1_sig, s1_score),
            ("SR Breakout",       s2_sig, s2_score),
            ("SR Reversal",       s3_sig, s3_score),
            ("Multi-Timeframe",   s4_sig, s4_score),
            ("Momentum",          s5_sig, s5_score),
        ]

        log.info(f"Strategy results for {pair}: {results}")

        # Count agreements
        calls = [(name, score) for name, sig, score in results if sig == "call"]
        puts  = [(name, score) for name, sig, score in results if sig == "put"]

        details = []
        for name, sig, score in results:
            if sig:
                details.append(f"✅ {name}: {sig.upper()} ({score}%)")
            else:
                details.append(f"❌ {name}: No signal")

        # Need at least 2 strategies agreeing
        if len(calls) >= 2:
            final_signal = "call"
            agreeing     = calls
        elif len(puts) >= 2:
            final_signal = "put"
            agreeing     = puts
        else:
            return None, 0, 1, details

        # Calculate confidence
        avg_score  = np.mean([s for _, s in agreeing])
        agreement_bonus = 10 * (len(agreeing) - 1)  # +10 per extra agreement
        confidence = min(int(avg_score + agreement_bonus), 99)

        if confidence < CONFIDENCE_THRESHOLD:
            return None, confidence, 1, details

        # Choose duration based on ATR volatility
        closes = [c["close"] for c in candles_1m]
        atr_v  = atr(candles_1m[-14:])
        adx_v  = adx(candles_1m[-20:])

        if atr_v > 0.0008 or adx_v > 35:
            duration = 1   # High volatility — short duration
        elif atr_v > 0.0004 or adx_v > 25:
            duration = 2   # Medium volatility
        else:
            duration = 5   # Low volatility — longer duration

        return final_signal, confidence, duration, details

    except Exception as e:
        log.error(f"Analyse error: {e}")
        return None, 0, 1, [f"Analysis error: {str(e)[:50]}"]

# ── TRADE THREAD ──────────────────────────────
def run_trade(chat_id):
    api = active_apis.get(str(chat_id))
    if not api:
        send_msg(chat_id, "Not connected. Use /start to reconnect.")
        return

    user   = get_user(chat_id)
    amount = float(user.get("trade_amount", 1))

    # Pick pair
    pair       = random.choice(PAIRS)
    pair_label = PAIR_LABELS.get(pair, pair)

    send_msg(chat_id, f"🔍 *Analysing {pair_label}...*\n\n_Running 5 strategies. Please wait..._")

    # Analyse market
    signal, confidence, duration, details = analyse_market(api, pair)

    strategy_summary = "\n".join(details)

    if not signal:
        if confidence > 0:
            send_msg(chat_id,
                f"📊 *Analysis Complete — NO TRADE*\n\n"
                f"Pair: {pair_label}\n"
                f"Confidence: {confidence}% (need {CONFIDENCE_THRESHOLD}%)\n\n"
                f"*Strategy Results:*\n{strategy_summary}\n\n"
                f"_Not enough confluence. Try again later._",
                keyboard=make_keyboard([
                    [("⚡ Try Again", "place_trade")],
                    [("⚙️ Settings",  "settings")]
                ])
            )
        else:
            send_msg(chat_id,
                f"📊 *Analysis Complete — NO TRADE*\n\n"
                f"Pair: {pair_label}\n\n"
                f"*Strategy Results:*\n{strategy_summary}\n\n"
                f"_Strategies disagree. No clear direction._",
                keyboard=make_keyboard([
                    [("⚡ Try Again", "place_trade")],
                    [("⚙️ Settings",  "settings")]
                ])
            )
        return

    dir_label = "📈 CALL (UP)" if signal=="call" else "📉 PUT (DOWN)"

    # Place trade
    order_id = None
    for attempt_pair in [pair] + [p for p in PAIRS if p != pair][:2]:
        try:
            ok, order_id = api.buy(amount, attempt_pair, signal, duration)
            if ok and order_id:
                pair       = attempt_pair
                pair_label = PAIR_LABELS.get(pair, pair)
                break
            order_id = None
        except Exception as e:
            log.error(f"Trade error on {attempt_pair}: {e}")
            order_id = None

    if not order_id:
        send_msg(chat_id, "Could not place trade. Please try again.")
        bal = get_balance(chat_id)
        show_main_buttons(chat_id, bal)
        return

    # Notify trade placed
    send_msg(chat_id,
        f"⚡ *Trade Placed!*\n\n"
        f"Pair: *{pair_label}*\n"
        f"Direction: *{dir_label}*\n"
        f"Amount: *${amount:.2f}*\n"
        f"Duration: *{duration} min*\n"
        f"Confidence: *{confidence}%*\n\n"
        f"*Strategies agreed:*\n{strategy_summary}\n\n"
        f"_Trade is running..._"
    )

    # Wait for trade to expire
    time.sleep(duration * 60 + 5)

    # Get new balance
    new_bal = 0.0
    for _ in range(5):
        try:
            new_bal = float(api.get_balance())
            if new_bal > 0: break
        except: pass
        time.sleep(3)

    show_main_buttons(chat_id, new_bal)

# ── TELEGRAM HANDLERS ─────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("🚀 Get Started", callback_data="get_started")]]
    await update.message.reply_text(
        "👋 *Welcome To SFX Bot!*\n\n"
        "Your smart IQ Option trading bot powered by *5 professional strategies*.\n\n"
        "🧠 Analyses market with 5 strategies\n"
        "✅ Only trades when 2+ strategies agree\n"
        "🎯 Minimum 85% confidence required\n"
        "⏱ Bot chooses best trade duration\n"
        "💵 You set the trade amount\n\n"
        "Tap *Get Started* to begin 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; chat_id=q.from_user.id; data=q.data
    await q.answer()

    if data=="get_started":
        update_user(chat_id, state="awaiting_credentials")
        await q.message.reply_text(
            "Send your IQ Option login details:\n\n"
            "Email (youremail@gmail.com)\n"
            "Password (yourpassword)"
        )

    elif data=="pick_demo":
        update_user(chat_id, account_type="PRACTICE")
        await _connect_account(q.message, chat_id)

    elif data=="pick_live":
        update_user(chat_id, account_type="REAL")
        await _connect_account(q.message, chat_id)

    elif data=="place_trade":
        api = active_apis.get(str(chat_id))
        if not api:
            await q.message.reply_text("Not connected. Use /start to reconnect.")
            return
        t = threading.Thread(target=run_trade, args=(chat_id,), daemon=True)
        t.start()

    elif data=="settings":
        user     = get_user(chat_id)
        amount   = float(user.get("trade_amount", 1))
        acc_type = user.get("account_type","PRACTICE")
        switch_label = "💚 Switch to Live" if acc_type=="PRACTICE" else "🟡 Switch to Demo"
        switch_cb    = "switch_live"        if acc_type=="PRACTICE" else "switch_demo"
        kb = [[InlineKeyboardButton("💵 Change Trade Amount", callback_data="set_amount")],
              [InlineKeyboardButton(switch_label,              callback_data=switch_cb)],
              [InlineKeyboardButton("🔙 Back",                 callback_data="back_main")]]
        await q.message.reply_text(
            f"⚙️ *Settings*\n\n"
            f"💵 Trade Amount: *${amount:.2f}*\n"
            f"Account: *{'🟡 Demo' if acc_type=='PRACTICE' else '💚 Live'}*\n"
            f"🎯 Confidence Threshold: *{CONFIDENCE_THRESHOLD}%*\n\n"
            f"Tap to change 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data=="set_amount":
        update_user(chat_id, state="awaiting_amount")
        await q.message.reply_text("💵 Send your trade amount e.g *1* or *5*", parse_mode="Markdown")

    elif data=="switch_live":
        update_user(chat_id, account_type="REAL", state="awaiting_credentials_switch")
        await q.message.reply_text(
            "Switching to *Live Account* 💚\n\nResend your IQ Option login:\n\n"
            "Email (youremail@gmail.com)\n"
            "Password (yourpassword)",
            parse_mode="Markdown"
        )

    elif data=="switch_demo":
        update_user(chat_id, account_type="PRACTICE", state="awaiting_credentials_switch")
        await q.message.reply_text(
            "Switching to *Demo Account* 🟡\n\nResend your IQ Option login:\n\n"
            "Email (youremail@gmail.com)\n"
            "Password (yourpassword)",
            parse_mode="Markdown"
        )

    elif data=="back_main":
        bal = get_balance(chat_id)
        show_main_buttons(chat_id, bal)

async def _connect_account(message, chat_id):
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
        acc_label = "🟡 Demo" if account_type=="PRACTICE" else "💚 Live"
        update_user(chat_id, state="connected")
        kb = [[InlineKeyboardButton("⚡ Place a Trade", callback_data="place_trade")],
              [InlineKeyboardButton("⚙️ Settings",      callback_data="settings")]]
        await message.reply_text(
            f"✅ *Connected Successfully!*\n\n"
            f"Account: {acc_label}\n"
            f"💰 Balance: *${balance:.2f}*\n"
            f"💵 Trade Amount: *${amount:.2f}*\n\n"
            f"🧠 Bot uses *5 strategies* to analyse\n"
            f"🎯 Trades only at *{CONFIDENCE_THRESHOLD}%+ confidence*\n\n"
            f"Tap *Place a Trade* to start 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        await message.reply_text(
            f"❌ *Couldn't connect!*\n\nError: {error or 'Invalid credentials'}\n\nUse /start to try again.",
            parse_mode="Markdown"
        )

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id=update.message.chat_id
    text=update.message.text.strip() if update.message.text else ""
    user=get_user(chat_id); state=user.get("state","idle")

    if state in ("awaiting_credentials","awaiting_credentials_switch"):
        import re
        lines=[l.strip() for l in text.split("\n") if l.strip()]
        if len(lines)<2:
            await update.message.reply_text("Please send:\n\nEmail (youremail@gmail.com)\nPassword (yourpassword)")
            return
        def extract(line):
            m=re.search(r'\((.+)\)',line)
            return m.group(1).strip() if m else line.strip()
        email=extract(lines[0]); password=extract(lines[1])
        update_user(chat_id, email=email, password=password)
        if state=="awaiting_credentials_switch":
            await _connect_account(update.message, chat_id)
        else:
            update_user(chat_id, state="awaiting_account_type")
            kb=[[InlineKeyboardButton("🟡 Demo Account",callback_data="pick_demo")],
                [InlineKeyboardButton("💚 Live Account",callback_data="pick_live")]]
            await update.message.reply_text("Details received!\n\nWhich account?", reply_markup=InlineKeyboardMarkup(kb))
        return

    if state=="awaiting_amount":
        try:
            amount=float(text.replace("$","").strip())
            if amount<=0: raise ValueError
            update_user(chat_id, trade_amount=amount, state="connected")
            bal=get_balance(chat_id)
            await update.message.reply_text(f"✅ Trade amount set to *${amount:.2f}*", parse_mode="Markdown")
            show_main_buttons(chat_id, bal)
        except:
            await update.message.reply_text("Send a valid amount e.g *1*", parse_mode="Markdown")
        return

    await update.message.reply_text("Use /start to begin.")

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.chat_id): return
    data=load_data()
    await update.message.reply_text(
        f"👥 *Users*\n\nTotal: {len(data)}\n"
        f"Demo: {sum(1 for u in data.values() if u.get('account_type')=='PRACTICE')}\n"
        f"Live: {sum(1 for u in data.values() if u.get('account_type')=='REAL')}",
        parse_mode="Markdown"
    )

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.chat_id): return
    msg=" ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast message"); return
    data=load_data(); sent=0; failed=0
    for uid in data.keys():
        try: await context.bot.send_message(chat_id=int(uid),text=msg); sent+=1
        except: failed+=1
    await update.message.reply_text(f"Sent: {sent} Failed: {failed}")

def main():
    if not TELEGRAM_TOKEN: raise ValueError("TELEGRAM_TOKEN not set!")
    app=Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("users",     admin_users))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    log.info("SFX 5-Strategy Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
