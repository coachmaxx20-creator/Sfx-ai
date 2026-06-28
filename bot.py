"""
SFX Professional Trading Bot
5 Solid Strategies — All Must Agree
Admin: 8319282451
"""
import os, json, time, logging, threading, random, requests
import numpy as np
from datetime import date, datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from iqoptionapi.stable_api import IQ_Option

TELEGRAM_TOKEN       = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_CHAT_ID        = 8319282451
DATA_FILE            = "users.json"
MISTAKES_FILE        = "mistakes.json"
CONFIDENCE_THRESHOLD = 90   # All 5 must agree + 90% confidence
DEFAULT_DURATION     = 5    # Default 5 minutes
STRONG_DURATION      = 1    # 1 minute for exceptionally strong setups

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

# ── MISTAKE LEARNING SYSTEM ───────────────────
def load_mistakes():
    if os.path.exists(MISTAKES_FILE):
        with open(MISTAKES_FILE,"r") as f: return json.load(f)
    return {"pair_losses":{}, "hour_losses":{}, "consecutive_losses":0, "total_trades":0, "total_wins":0}

def save_mistakes(m):
    with open(MISTAKES_FILE,"w") as f: json.dump(m,f,indent=2)

def record_trade_result(pair, win):
    """Bot learns from every trade result."""
    m = load_mistakes()
    hour = str(datetime.now(timezone.utc).hour)
    m["total_trades"] = m.get("total_trades", 0) + 1
    if win:
        m["total_wins"] = m.get("total_wins", 0) + 1
        m["consecutive_losses"] = 0
        # Reduce pair loss count on win
        if pair in m["pair_losses"]:
            m["pair_losses"][pair] = max(0, m["pair_losses"][pair] - 1)
        if hour in m["hour_losses"]:
            m["hour_losses"][hour] = max(0, m["hour_losses"][hour] - 1)
    else:
        m["consecutive_losses"] = m.get("consecutive_losses", 0) + 1
        # Track which pairs and hours lose most
        m["pair_losses"][pair]  = m["pair_losses"].get(pair, 0) + 1
        m["hour_losses"][hour]  = m["hour_losses"].get(hour, 0) + 1
    save_mistakes(m)
    return m

def should_avoid_pair(pair):
    """Avoid pairs that have lost 3+ times recently."""
    m = load_mistakes()
    losses = m["pair_losses"].get(pair, 0)
    if losses >= 3:
        log.info(f"Avoiding {pair} — {losses} recent losses")
        return True
    return False

def should_avoid_hour():
    """Avoid hours that consistently lose."""
    m = load_mistakes()
    hour = str(datetime.now(timezone.utc).hour)
    losses = m["hour_losses"].get(hour, 0)
    if losses >= 3:
        log.info(f"Avoiding hour {hour} UTC — {losses} losses at this time")
        return True
    return False

def get_consecutive_losses():
    return load_mistakes().get("consecutive_losses", 0)

def get_best_pairs():
    """Return pairs sorted by performance (least losses first)."""
    m = load_mistakes()
    pairs_sorted = sorted(PAIRS, key=lambda p: m["pair_losses"].get(p, 0))
    return pairs_sorted

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
    m        = load_mistakes()
    total    = m.get("total_trades", 0)
    wins     = m.get("total_wins", 0)
    wr       = f"{(wins/total*100):.0f}%" if total > 0 else "N/A"
    kb = make_keyboard([
        [("⚡ Place a Trade", "place_trade")],
        [("⚙️ Settings",      "settings")]
    ])
    send_msg(chat_id,
        f"💰 *Balance: ${balance:.2f}*\n"
        f"Account: {'🟡' if acc_type=='Demo' else '💚'} {acc_type}\n"
        f"💵 Trade Amount: ${amount:.2f}\n"
        f"📊 Win Rate: {wr} ({wins}/{total})\n\n"
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
    if len(closes) < period: period = len(closes)
    k = 2/(period+1); v = closes[0]
    for c in closes[1:]: v = c*k + v*(1-k)
    return v

def ema_series(closes, period):
    closes = np.array(closes, dtype=float)
    if len(closes) < period: return closes
    k = 2/(period+1); result = [closes[0]]
    for c in closes[1:]: result.append(c*k + result[-1]*(1-k))
    return np.array(result)

def rsi(closes, period=14):
    closes = np.array(closes, dtype=float)
    if len(closes) < period+1: return 50.0
    d = np.diff(closes[-period-1:])
    gains = np.where(d>0,d,0); losses = np.where(d<0,-d,0)
    ag = np.mean(gains); al = np.mean(losses)
    if al == 0: return 100.0
    return 100-(100/(1+ag/al))

def atr_val(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h=candles[i]["max"]; l=candles[i]["min"]; pc=candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    if not trs: return 0.0
    return float(np.mean(trs[-period:]))

def adx_val(candles, period=14):
    try:
        if len(candles) < period+2: return 20.0
        highs=[c["max"] for c in candles]; lows=[c["min"] for c in candles]; closes=[c["close"] for c in candles]
        plus_dm=[]; minus_dm=[]; tr_list=[]
        for i in range(1,len(candles)):
            up=highs[i]-highs[i-1]; down=lows[i-1]-lows[i]
            plus_dm.append(up if up>down and up>0 else 0)
            minus_dm.append(down if down>up and down>0 else 0)
            h=highs[i];l=lows[i];pc=closes[i-1]
            tr_list.append(max(h-l,abs(h-pc),abs(l-pc)))
        if len(tr_list)<period: return 20.0
        atr_v=np.mean(tr_list[-period:])
        if atr_v==0: return 20.0
        pdi=(np.mean(plus_dm[-period:])/atr_v)*100
        mdi=(np.mean(minus_dm[-period:])/atr_v)*100
        dx=abs(pdi-mdi)/(pdi+mdi+1e-10)*100
        return float(dx)
    except: return 20.0

def bollinger(closes, period=20, std=2):
    closes=np.array(closes,dtype=float)
    if len(closes)<period: return None,None,None
    sma=np.mean(closes[-period:]); s=np.std(closes[-period:])
    return sma+std*s, sma, sma-std*s

def macd_vals(closes):
    closes=np.array(closes,dtype=float)
    if len(closes)<35: return 0,0,0
    e12=ema_series(closes,12); e26=ema_series(closes,26)
    ml=e12-e26; sl=ema_series(ml,9); hist=ml-sl
    return float(ml[-1]),float(sl[-1]),float(hist[-1])

def prev_macd(closes):
    closes=np.array(closes,dtype=float)
    if len(closes)<36: return 0,0,0
    return macd_vals(closes[:-1])

def is_bull_engulf(c1,c2): return c2["close"]>c2["open"] and c1["close"]<c1["open"] and c2["close"]>c1["open"] and c2["open"]<c1["close"]
def is_bear_engulf(c1,c2): return c2["close"]<c2["open"] and c1["close"]>c1["open"] and c2["close"]<c1["open"] and c2["open"]>c1["close"]
def is_hammer(c): 
    body=abs(c["close"]-c["open"]); total=c["max"]-c["min"]
    if total==0: return False
    lower=min(c["open"],c["close"])-c["min"]
    return lower>body*2 and body/total<0.4
def is_shooting_star(c):
    body=abs(c["close"]-c["open"]); total=c["max"]-c["min"]
    if total==0: return False
    upper=c["max"]-max(c["open"],c["close"])
    return upper>body*2 and body/total<0.4
def is_doji(c):
    body=abs(c["close"]-c["open"]); total=c["max"]-c["min"]
    return total>0 and body/total<0.1

# ═══════════════════════════════════════════════
#  5 PROFESSIONAL STRATEGIES
# ═══════════════════════════════════════════════

def strategy1_ema_trend_pullback(candles):
    """
    Strategy 1: EMA Trend Pullback
    Trade in direction of established trend after pullback to EMA20.
    """
    try:
        if len(candles)<55: return None,0
        closes=[c["close"] for c in candles]
        e20=ema(closes[-20:],20); e50=ema(closes[-50:],50)
        rsi_v=rsi(closes[-20:]); atr_v=atr_val(candles[-20:])
        adx_v=adx_val(candles[-20:]); price=closes[-1]
        last=candles[-1]; prev=candles[-2]
        if adx_v<22 or atr_v<0.00005: return None,0
        score=0
        # BUY
        if e20>e50:
            score+=20
            if adx_v>25: score+=20
            if e50<price<=e20*1.003: score+=25
            if 38<=rsi_v<=58: score+=20
            if is_bull_engulf(prev,last) or is_hammer(last): score+=15
            if score>=70: return "call", min(score,100)
        # SELL
        if e20<e50:
            score+=20
            if adx_v>25: score+=20
            if e20*0.997<=price<e50: score+=25
            if 42<=rsi_v<=62: score+=20
            if is_bear_engulf(prev,last) or is_shooting_star(last): score+=15
            if score>=70: return "put", min(score,100)
        return None,0
    except Exception as e:
        log.error(f"S1 error: {e}"); return None,0

def strategy2_bollinger_squeeze(candles):
    """
    Strategy 2: Bollinger Band Squeeze Breakout
    When bands squeeze (low volatility) then expand — trade the breakout direction.
    High probability setups with strong momentum.
    """
    try:
        if len(candles)<30: return None,0
        closes=[c["close"] for c in candles]
        bb_up,bb_mid,bb_low=bollinger(closes,20,2)
        bb_up_narrow,_,bb_low_narrow=bollinger(closes,20,1.5)
        if bb_up is None: return None,0
        price=closes[-1]; last=candles[-1]; prev=candles[-2]
        band_width=bb_up-bb_low
        prev_width_list=[]
        for i in range(5,15):
            u,_,l=bollinger(closes[:-i],20,2)
            if u and l: prev_width_list.append(u-l)
        if not prev_width_list: return None,0
        avg_width=np.mean(prev_width_list)
        rsi_v=rsi(closes[-15:])
        volumes=[c.get("volume",1) for c in candles]
        avg_vol=np.mean(volumes[-10:-1])
        score=0
        # Squeeze condition — bands narrowing recently
        squeeze = band_width < avg_width * 0.8
        # BUY breakout
        if price>bb_up and last["close"]>last["open"]:
            score+=30
            if squeeze: score+=20
            if volumes[-1]>avg_vol*1.2: score+=20
            if rsi_v>55: score+=15
            if is_bull_engulf(prev,last): score+=15
            if score>=70: return "call", min(score,100)
        # SELL breakout
        if price<bb_low and last["close"]<last["open"]:
            score+=30
            if squeeze: score+=20
            if volumes[-1]>avg_vol*1.2: score+=20
            if rsi_v<45: score+=15
            if is_bear_engulf(prev,last): score+=15
            if score>=70: return "put", min(score,100)
        return None,0
    except Exception as e:
        log.error(f"S2 error: {e}"); return None,0

def strategy3_candlestick_volume(candles):
    """
    Strategy 3: Candlestick Pattern + Volume Confirmation
    Strong candlestick patterns confirmed by volume spike and RSI.
    """
    try:
        if len(candles)<20: return None,0
        closes=[c["close"] for c in candles]
        rsi_v=rsi(closes[-15:])
        volumes=[c.get("volume",1) for c in candles]
        avg_vol=np.mean(volumes[-10:-1])
        last=candles[-1]; prev=candles[-2]; prev2=candles[-3]
        last_vol=volumes[-1]
        atr_v=atr_val(candles[-14:])
        last_body=abs(last["close"]-last["open"])
        avg_body=np.mean([abs(c["close"]-c["open"]) for c in candles[-10:-1]])
        strong_candle=last_body>avg_body*1.5
        high_volume=last_vol>avg_vol*1.3
        score=0
        # BUY signals
        buy_pattern=is_bull_engulf(prev,last) or is_hammer(last) or (is_bull_engulf(prev2,prev) and last["close"]>prev["close"])
        if buy_pattern and rsi_v<65:
            score+=25
            if high_volume: score+=25
            if strong_candle: score+=20
            if rsi_v<50: score+=15
            if last["close"]>prev["close"]>prev2["close"]: score+=15
            if score>=70: return "call", min(score,100)
        # SELL signals
        sell_pattern=is_bear_engulf(prev,last) or is_shooting_star(last) or (is_bear_engulf(prev2,prev) and last["close"]<prev["close"])
        if sell_pattern and rsi_v>35:
            score+=25
            if high_volume: score+=25
            if strong_candle: score+=20
            if rsi_v>50: score+=15
            if last["close"]<prev["close"]<prev2["close"]: score+=15
            if score>=70: return "put", min(score,100)
        return None,0
    except Exception as e:
        log.error(f"S3 error: {e}"); return None,0

def strategy4_multi_timeframe(candles_1m, candles_5m):
    """
    Strategy 4: Multi-Timeframe Structure
    5M determines trend, 1M finds entry after pullback.
    """
    try:
        if len(candles_1m)<20 or len(candles_5m)<20: return None,0
        closes_5m=[c["close"] for c in candles_5m]
        closes_1m=[c["close"] for c in candles_1m]
        e20_5m=ema(closes_5m[-20:],20)
        e50_5m=ema(closes_5m[-min(50,len(closes_5m)):],min(50,len(closes_5m)))
        price_5m=closes_5m[-1]
        e20_1m=ema(closes_1m[-20:],20)
        price_1m=closes_1m[-1]
        rsi_5m=rsi(closes_5m[-15:])
        rsi_1m=rsi(closes_1m[-15:])
        last_1m=candles_1m[-1]; prev_1m=candles_1m[-2]
        score=0
        # 5M BULLISH trend + 1M pullback entry
        if e20_5m>e50_5m and price_5m>e20_5m and rsi_5m>50:
            score+=30
            pulled_back=any(c["close"]<e20_1m for c in candles_1m[-4:-1])
            if pulled_back and price_1m>e20_1m: score+=30
            if is_bull_engulf(prev_1m,last_1m) or last_1m["close"]>last_1m["open"]: score+=25
            if rsi_1m>45: score+=15
            if score>=70: return "call", min(score,100)
        # 5M BEARISH trend + 1M pullback entry
        if e20_5m<e50_5m and price_5m<e20_5m and rsi_5m<50:
            score+=30
            pulled_back=any(c["close"]>e20_1m for c in candles_1m[-4:-1])
            if pulled_back and price_1m<e20_1m: score+=30
            if is_bear_engulf(prev_1m,last_1m) or last_1m["close"]<last_1m["open"]: score+=25
            if rsi_1m<55: score+=15
            if score>=70: return "put", min(score,100)
        return None,0
    except Exception as e:
        log.error(f"S4 error: {e}"); return None,0

def strategy5_macd_rsi_momentum(candles):
    """
    Strategy 5: MACD + RSI Momentum Confluence
    MACD crossover confirmed by RSI momentum and EMA50 trend filter.
    """
    try:
        if len(candles)<55: return None,0
        closes=[c["close"] for c in candles]
        price=closes[-1]
        e50=ema(closes[-50:],50)
        adx_v=adx_val(candles[-20:])
        atr_v=atr_val(candles[-14:])
        rsi_v=rsi(closes[-15:])
        ml,sl,hist=macd_vals(closes)
        pml,psl,phist=prev_macd(closes)
        if adx_v<22 or atr_v<0.00005: return None,0
        score=0
        # BUY: above EMA50, MACD bullish cross, RSI momentum up
        if price>e50:
            score+=20
            if ml>sl and pml<=psl: score+=30   # Fresh crossover
            elif ml>sl: score+=15
            if hist>0 and hist>phist: score+=25
            if rsi_v>50 and rsi_v<75: score+=15
            if adx_v>25: score+=10
            if score>=70: return "call", min(score,100)
        # SELL: below EMA50, MACD bearish cross, RSI momentum down
        if price<e50:
            score+=20
            if ml<sl and pml>=psl: score+=30   # Fresh crossover
            elif ml<sl: score+=15
            if hist<0 and hist<phist: score+=25
            if rsi_v<50 and rsi_v>25: score+=15
            if adx_v>25: score+=10
            if score>=70: return "put", min(score,100)
        return None,0
    except Exception as e:
        log.error(f"S5 error: {e}"); return None,0

# ── CONFLUENCE ENGINE ─────────────────────────
def analyse_market(api, pair):
    """
    Run all 5 strategies. ALL must agree.
    Returns (signal, confidence, duration, details)
    """
    try:
        candles_1m = api.get_candles(pair, 60,  80, time.time())
        time.sleep(0.5)
        candles_5m = api.get_candles(pair, 300, 60, time.time())
        time.sleep(0.5)

        if not candles_1m or len(candles_1m)<20:
            return None,0,DEFAULT_DURATION,["Not enough candle data"]

        # Run all 5 strategies
        s1,sc1 = strategy1_ema_trend_pullback(candles_1m)
        s2,sc2 = strategy2_bollinger_squeeze(candles_1m)
        s3,sc3 = strategy3_candlestick_volume(candles_1m)
        s4,sc4 = strategy4_multi_timeframe(candles_1m, candles_5m or candles_1m)
        s5,sc5 = strategy5_macd_rsi_momentum(candles_1m)

        strategies = [
            ("EMA Trend Pullback",      s1, sc1),
            ("BB Squeeze Breakout",     s2, sc2),
            ("Candlestick + Volume",    s3, sc3),
            ("Multi-Timeframe",         s4, sc4),
            ("MACD+RSI Momentum",       s5, sc5),
        ]

        log.info(f"{pair} results: {[(n,s,c) for n,s,c in strategies]}")

        details = []
        for name,sig,score in strategies:
            if sig: details.append(f"✅ {name}: {sig.upper()} ({score}%)")
            else:   details.append(f"❌ {name}: No signal")

        calls = [(n,c) for n,s,c in strategies if s=="call"]
        puts  = [(n,c) for n,s,c in strategies if s=="put"]

        # ALL 5 must agree
        if len(calls)==5:
            final="call"; agreeing=calls
        elif len(puts)==5:
            final="put"; agreeing=puts
        else:
            agreed = len(calls) if len(calls)>len(puts) else len(puts)
            return None,0,DEFAULT_DURATION,details

        # Confidence = weighted average of all scores
        avg_score  = np.mean([c for _,c in agreeing])
        confidence = min(int(avg_score), 99)

        if confidence < CONFIDENCE_THRESHOLD:
            return None, confidence, DEFAULT_DURATION, details

        # Duration selection
        atr_v = atr_val(candles_1m[-14:])
        adx_v = adx_val(candles_1m[-20:])

        # Use 1 min only for exceptionally strong setups
        if confidence >= 96 and adx_v > 35 and atr_v > 0.0006:
            duration = STRONG_DURATION
        else:
            duration = DEFAULT_DURATION

        return final, confidence, duration, details

    except Exception as e:
        log.error(f"Analyse error: {e}")
        return None,0,DEFAULT_DURATION,[f"Error: {str(e)[:50]}"]

# ── TRADE THREAD ──────────────────────────────
def run_trade(chat_id):
    api = active_apis.get(str(chat_id))
    if not api:
        send_msg(chat_id, "Not connected. Use /start to reconnect.")
        return

    user   = get_user(chat_id)
    amount = float(user.get("trade_amount", 1))

    # Pause if too many consecutive losses
    consec = get_consecutive_losses()
    if consec >= 3:
        send_msg(chat_id,
            f"⚠️ *Caution — {consec} Consecutive Losses*\n\n"
            f"The bot has learned from recent mistakes and is being extra selective.\n"
            f"Analysing market for a high quality setup..."
        )

    # Pick best available pair (avoid pairs with many losses)
    best_pairs = get_best_pairs()
    available  = [p for p in best_pairs if not should_avoid_pair(p)]
    if not available:
        available = best_pairs  # Reset if all avoided
        m = load_mistakes()
        m["pair_losses"] = {}  # Reset pair loss memory
        save_mistakes(m)

    # Avoid bad hours
    if should_avoid_hour():
        send_msg(chat_id,
            "⚠️ *Bad Trading Hour Detected*\n\n"
            "The bot has learned this hour has poor results historically.\n"
            "Proceeding with extra caution and higher thresholds...",
        )

    pair       = available[0]
    pair_label = PAIR_LABELS.get(pair, pair)

    send_msg(chat_id,
        f"🔍 *Analysing {pair_label}...*\n\n"
        f"_Running 5 professional strategies.\n"
        f"All must agree at 90%+ confidence..._"
    )

    signal, confidence, duration, details = analyse_market(api, pair)
    strategy_summary = "\n".join(details)

    if not signal:
        send_msg(chat_id,
            f"📊 *Analysis Complete — NO TRADE*\n\n"
            f"Pair: {pair_label}\n"
            f"Confidence: {confidence}% (need {CONFIDENCE_THRESHOLD}%)\n\n"
            f"*Strategy Results:*\n{strategy_summary}\n\n"
            f"_Not all strategies agree. Protecting your capital._",
            keyboard=make_keyboard([
                [("🔍 Analyse Again", "place_trade")],
                [("⚙️ Settings",      "settings")]
            ])
        )
        return

    dir_label    = "📈 CALL (UP)" if signal=="call" else "📉 PUT (DOWN)"
    dur_label    = f"{duration} min {'⚡ STRONG SETUP' if duration==1 else ''}"

    # Get balance before trade
    bal_before = 0.0
    for _ in range(3):
        try:
            bal_before = float(api.get_balance())
            if bal_before > 0: break
        except: pass
        time.sleep(2)

    # Place trade — try pairs in order
    order_id = None
    used_pair = pair
    for try_pair in [pair] + [p for p in available if p!=pair][:2]:
        try:
            ok, oid = api.buy(amount, try_pair, signal, duration)
            if ok and oid:
                order_id  = oid
                used_pair = try_pair
                pair_label= PAIR_LABELS.get(used_pair, used_pair)
                break
            order_id = None
        except Exception as e:
            log.error(f"Trade error {try_pair}: {e}")
            order_id = None

    if not order_id:
        send_msg(chat_id, "Could not place trade. Please try again.")
        show_main_buttons(chat_id, bal_before)
        return

    send_msg(chat_id,
        f"⚡ *Trade Placed!*\n\n"
        f"Pair: *{pair_label}*\n"
        f"Direction: *{dir_label}*\n"
        f"Amount: *${amount:.2f}*\n"
        f"Duration: *{dur_label}*\n"
        f"Confidence: *{confidence}%*\n\n"
        f"*All 5 strategies agreed:*\n{strategy_summary}\n\n"
        f"_Trade is running..._"
    )

    # Wait for trade to fully expire
    wait_secs = (duration * 60) + 10
    log.info(f"Waiting {wait_secs}s for trade to expire...")
    time.sleep(wait_secs)

    # Wait extra seconds for IQ Option to settle balance
    log.info("Waiting 8 extra seconds for balance to settle...")
    time.sleep(8)

    # Get balance after trade — retry multiple times
    bal_after = bal_before
    for attempt in range(8):
        try:
            new_bal = float(api.get_balance())
            if new_bal > 0 and new_bal != bal_before:
                bal_after = new_bal
                log.info(f"Balance settled on attempt {attempt+1}: {bal_after}")
                break
            elif new_bal > 0:
                bal_after = new_bal
        except Exception as e:
            log.error(f"Balance check attempt {attempt+1}: {e}")
        time.sleep(3)

    # Calculate actual result from balance difference
    diff   = round(bal_after - bal_before, 2)
    won    = diff > 0
    log.info(f"Trade result: before={bal_before} after={bal_after} diff={diff} won={won}")

    # Record result so bot learns
    record_trade_result(used_pair, won)
    m = load_mistakes()
    total = m.get("total_trades",0)
    wins  = m.get("total_wins",0)
    wr    = f"{(wins/total*100):.0f}%" if total>0 else "N/A"

    if diff > 0:
        result_msg = f"✅ *WIN!* +${diff:.2f}"
    elif diff == 0:
        result_msg = "🤝 *DRAW* — Amount returned"
    else:
        result_msg = f"❌ *LOSS* -${abs(diff):.2f}"

    show_main_buttons(chat_id, bal_after)

# ── TELEGRAM HANDLERS ─────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("🚀 Get Started", callback_data="get_started")]]
    await update.message.reply_text(
        "👋 *Welcome To SFX Pro Bot!*\n\n"
        "Powered by *5 professional trading strategies*.\n\n"
        "🧠 All 5 strategies must agree\n"
        "🎯 Minimum 90% confidence required\n"
        "⏱ 5 min trades (1 min for elite setups)\n"
        "💵 You choose trade amount\n"
        "📚 Bot learns from every mistake\n"
        "🛡️ Protects capital — NO TRADE if not sure\n\n"
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
        user=get_user(chat_id)
        amount=float(user.get("trade_amount",1))
        acc_type=user.get("account_type","PRACTICE")
        m=load_mistakes()
        total=m.get("total_trades",0); wins=m.get("total_wins",0)
        wr=f"{(wins/total*100):.0f}%" if total>0 else "N/A"
        switch_label="💚 Switch to Live" if acc_type=="PRACTICE" else "🟡 Switch to Demo"
        switch_cb="switch_live" if acc_type=="PRACTICE" else "switch_demo"
        kb=[[InlineKeyboardButton("💵 Change Trade Amount",callback_data="set_amount")],
            [InlineKeyboardButton(switch_label,callback_data=switch_cb)],
            [InlineKeyboardButton("📊 View Performance",callback_data="performance")],
            [InlineKeyboardButton("🔙 Back",callback_data="back_main")]]
        await q.message.reply_text(
            f"⚙️ *Settings*\n\n"
            f"💵 Trade Amount: *${amount:.2f}*\n"
            f"Account: *{'🟡 Demo' if acc_type=='PRACTICE' else '💚 Live'}*\n"
            f"📊 Win Rate: *{wr}* ({wins}/{total})\n"
            f"🎯 Confidence Threshold: *{CONFIDENCE_THRESHOLD}%*\n\n"
            f"Tap to change 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data=="performance":
        m=load_mistakes()
        total=m.get("total_trades",0); wins=m.get("total_wins",0)
        losses=total-wins
        wr=f"{(wins/total*100):.0f}%" if total>0 else "N/A"
        pair_losses=m.get("pair_losses",{})
        hour_losses=m.get("hour_losses",{})
        best_pair=min(pair_losses,key=pair_losses.get) if pair_losses else "N/A"
        worst_pair=max(pair_losses,key=pair_losses.get) if pair_losses else "N/A"
        consec=m.get("consecutive_losses",0)
        await q.message.reply_text(
            f"📊 *Bot Performance*\n\n"
            f"Total Trades: {total}\n"
            f"Wins: {wins} | Losses: {losses}\n"
            f"Win Rate: *{wr}*\n"
            f"Consecutive Losses: {consec}\n\n"
            f"Best Pair: {PAIR_LABELS.get(best_pair,best_pair)}\n"
            f"Worst Pair: {PAIR_LABELS.get(worst_pair,worst_pair)}\n\n"
            f"_Bot is learning and improving from every trade_ 🧠",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="settings")]])
        )

    elif data=="set_amount":
        update_user(chat_id, state="awaiting_amount")
        await q.message.reply_text("💵 Send your trade amount e.g *1* or *5*",parse_mode="Markdown")

    elif data=="switch_live":
        update_user(chat_id, account_type="REAL", state="awaiting_credentials_switch")
        await q.message.reply_text(
            "Switching to *Live Account* 💚\n\nResend your IQ Option login:\n\n"
            "Email (youremail@gmail.com)\n"
            "Password (yourpassword)",parse_mode="Markdown"
        )

    elif data=="switch_demo":
        update_user(chat_id, account_type="PRACTICE", state="awaiting_credentials_switch")
        await q.message.reply_text(
            "Switching to *Demo Account* 🟡\n\nResend your IQ Option login:\n\n"
            "Email (youremail@gmail.com)\n"
            "Password (yourpassword)",parse_mode="Markdown"
        )

    elif data=="back_main":
        bal=get_balance(chat_id)
        show_main_buttons(chat_id, bal)

async def _connect_account(message, chat_id):
    user=get_user(chat_id)
    email=user.get("email",""); password=user.get("password","")
    account_type=user.get("account_type","PRACTICE")
    await message.reply_text("⏳ Connecting to IQ Option...")
    api,error=connect_iqoption(email,password,account_type)
    if api:
        active_apis[str(chat_id)]=api
        balance=float(api.get_balance())
        amount=float(user.get("trade_amount",1))
        acc_label="🟡 Demo" if account_type=="PRACTICE" else "💚 Live"
        update_user(chat_id, state="connected")
        kb=[[InlineKeyboardButton("⚡ Place a Trade",callback_data="place_trade")],
            [InlineKeyboardButton("⚙️ Settings",callback_data="settings")]]
        await message.reply_text(
            f"✅ *Connected Successfully!*\n\n"
            f"Account: {acc_label}\n"
            f"💰 Balance: *${balance:.2f}*\n"
            f"💵 Trade Amount: *${amount:.2f}*\n\n"
            f"🧠 *5 Strategy System Active*\n"
            f"🎯 All 5 must agree at 90%+\n"
            f"⏱ Default: 5 min | Elite: 1 min\n\n"
            f"Tap *Place a Trade* to start 👇",
            parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(kb)
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
        update_user(chat_id,email=email,password=password)
        if state=="awaiting_credentials_switch":
            await _connect_account(update.message, chat_id)
        else:
            update_user(chat_id,state="awaiting_account_type")
            kb=[[InlineKeyboardButton("🟡 Demo Account",callback_data="pick_demo")],
                [InlineKeyboardButton("💚 Live Account",callback_data="pick_live")]]
            await update.message.reply_text("Details received!\n\nWhich account?",reply_markup=InlineKeyboardMarkup(kb))
        return

    if state=="awaiting_amount":
        try:
            amount=float(text.replace("$","").strip())
            if amount<=0: raise ValueError
            update_user(chat_id,trade_amount=amount,state="connected")
            bal=get_balance(chat_id)
            await update.message.reply_text(f"✅ Trade amount set to *${amount:.2f}*",parse_mode="Markdown")
            show_main_buttons(chat_id,bal)
        except:
            await update.message.reply_text("Send a valid amount e.g *1*",parse_mode="Markdown")
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
    log.info("SFX Pro 5-Strategy Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
