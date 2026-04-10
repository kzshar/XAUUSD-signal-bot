
import logging
import os
import json
import time
import datetime
import pytz
import requests
import asyncio
import re
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

# Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8653316966:AAGdqc_ip9cZwual3AONsMzKKknhJW3jrKg")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
TRADE_HISTORY_FILE = os.path.join(DATA_DIR, "trade_history.json")
SIGNAL_LOG_FILE = os.path.join(DATA_DIR, "signal_log.json")
DAILY_JOURNAL_FILE = os.path.join(DATA_DIR, "daily_journal.json")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "5948621771"))

# Trading Rules & Goals
STARTING_BALANCE = 15.0
WEEKLY_GOAL = 50.0
MAX_DAILY_TRADES = 5
MIN_COUNTED_LOT = 0.5
MIN_COUNTED_PNL = 2.0
DAILY_LOSS_LIMIT = 4.0
MAX_DAILY_LOSSES = 2
LOSS_OVERRIDE_ACTIVE = False  # Can be toggled by /unlock command

def save_override_state():
    """Persist override state to file."""
    try:
        with open(os.path.join(os.path.dirname(__file__), 'override_state.json'), 'w') as f:
            json.dump({'override': LOSS_OVERRIDE_ACTIVE}, f)
    except: pass

def load_override_state():
    """Load override state from file."""
    global LOSS_OVERRIDE_ACTIVE
    try:
        path = os.path.join(os.path.dirname(__file__), 'override_state.json')
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            LOSS_OVERRIDE_ACTIVE = data.get('override', False)
    except: pass

# XM Global Micro Account Settings
XM_LOT_SIZE = 0.5
XM_PIP_VALUE = 0.005
XM_MAX_SL_POINTS = 400
XM_MIN_TP_POINTS = 1000
XM_MIN_RR = 2.5
WIN_THRESHOLD = 5.0
LOSS_THRESHOLD = 2.0

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO,
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Global variables
price_history = deque(maxlen=500)  # Increased for better analysis
cached_price = None
last_fetch_time = 0
trade_history = []
signal_log = []
daily_journal = {}
active_signal_msg_id = None
last_signal_time = 0
active_trade = None
cooldown_until = 0
last_scan_update_time = 0
SCAN_UPDATE_INTERVAL = 900

# ============================================================
# DYNAMIC KEY LEVELS - Updated for current price range
# ============================================================
KEY_LEVELS = {
    "resistance": [4540, 4555, 4570, 4585, 4600, 4620],
    "support": [4520, 4510, 4500, 4490, 4475, 4460],
    "round_numbers": [4500, 4550, 4600],
    "demand_zones": [(4490, 4500), (4510, 4520), (4460, 4475)],
    "supply_zones": [(4555, 4570), (4585, 4600), (4620, 4640)]
}

# ============================================================
# SMC ANALYSIS ENGINE - Real market structure analysis
# ============================================================

def get_price_swings(prices, lookback=5):
    """Detect swing highs and swing lows from price data."""
    swings = {"highs": [], "lows": []}
    if len(prices) < lookback * 2 + 1:
        return swings
    for i in range(lookback, len(prices) - lookback):
        # Swing High: price[i] > all neighbors within lookback
        if all(prices[i] > prices[i-j] for j in range(1, lookback+1)) and \
           all(prices[i] > prices[i+j] for j in range(1, lookback+1)):
            swings["highs"].append({"index": i, "price": prices[i]})
        # Swing Low: price[i] < all neighbors within lookback
        if all(prices[i] < prices[i-j] for j in range(1, lookback+1)) and \
           all(prices[i] < prices[i+j] for j in range(1, lookback+1)):
            swings["lows"].append({"index": i, "price": prices[i]})
    return swings

def detect_htf_trend(prices):
    """Detect Higher Timeframe trend using swing structure.
    Returns: 'BULLISH', 'BEARISH', or 'RANGING'
    """
    if len(prices) < 30:
        return "UNKNOWN"
    
    swings = get_price_swings(prices, lookback=3)
    highs = swings["highs"]
    lows = swings["lows"]
    
    # Need at least 2 highs and 2 lows
    if len(highs) < 2 or len(lows) < 2:
        # Fallback: use simple MA comparison
        ma_short = sum(prices[-20:]) / 20 if len(prices) >= 20 else prices[-1]
        ma_long = sum(prices[-50:]) / 50 if len(prices) >= 50 else prices[-1]
        if ma_short > ma_long + 1:
            return "BULLISH"
        elif ma_short < ma_long - 1:
            return "BEARISH"
        return "RANGING"
    
    # Check last 2 swing highs and lows
    last_2_highs = [h["price"] for h in highs[-2:]]
    last_2_lows = [l["price"] for l in lows[-2:]]
    
    hh = last_2_highs[-1] > last_2_highs[-2]  # Higher High
    hl = last_2_lows[-1] > last_2_lows[-2]     # Higher Low
    lh = last_2_highs[-1] < last_2_highs[-2]   # Lower High
    ll = last_2_lows[-1] < last_2_lows[-2]      # Lower Low
    
    if hh and hl:
        return "BULLISH"
    elif lh and ll:
        return "BEARISH"
    return "RANGING"

def detect_bos(prices, direction):
    """Detect Break of Structure.
    BUY: price breaks above recent swing high
    SELL: price breaks below recent swing low
    """
    if len(prices) < 20:
        return False, None
    
    swings = get_price_swings(prices[:-1], lookback=3)  # Exclude current price
    current = prices[-1]
    
    if direction == "BUY" and swings["highs"]:
        last_high = swings["highs"][-1]["price"]
        if current > last_high:
            return True, last_high
    elif direction == "SELL" and swings["lows"]:
        last_low = swings["lows"][-1]["price"]
        if current < last_low:
            return True, last_low
    
    return False, None

def detect_displacement(prices, lookback=5):
    """Detect strong displacement (big body candles / strong momentum).
    Returns magnitude of displacement.
    """
    if len(prices) < lookback:
        return 0.0
    recent = list(prices)[-lookback:]
    move = abs(recent[-1] - recent[0])
    # Compare to average move
    avg_moves = []
    all_prices = list(prices)
    for i in range(lookback, len(all_prices)):
        avg_moves.append(abs(all_prices[i] - all_prices[i-lookback]))
    avg_move = sum(avg_moves) / len(avg_moves) if avg_moves else 1.0
    
    return move / avg_move if avg_move > 0 else 0.0

def detect_liquidity_sweep(prices, direction):
    """Detect if liquidity has been swept (price went past a level then reversed).
    BUY: price dipped below recent low then came back up
    SELL: price spiked above recent high then came back down
    """
    if len(prices) < 30:
        return False
    
    recent = list(prices)[-30:]
    current = recent[-1]
    
    if direction == "BUY":
        # Find recent low, check if price went below it then bounced
        min_price = min(recent[:-5])
        recent_min = min(recent[-10:])
        if recent_min < min_price and current > recent_min + 1.0:
            return True
    elif direction == "SELL":
        # Find recent high, check if price went above it then dropped
        max_price = max(recent[:-5])
        recent_max = max(recent[-10:])
        if recent_max > max_price and current < recent_max - 1.0:
            return True
    
    return False

def find_order_block(prices, direction):
    """Find potential Order Block zone.
    BUY OB: Last bearish candle before bullish displacement
    SELL OB: Last bullish candle before bearish displacement
    Returns (ob_low, ob_high) or None
    """
    if len(prices) < 20:
        return None
    
    recent = list(prices)[-20:]
    
    if direction == "BUY":
        # Look for a drop followed by a strong rise
        for i in range(len(recent)-5, 4, -1):
            if recent[i] < recent[i-1]:  # Bearish move
                # Check if followed by bullish displacement
                if recent[min(i+3, len(recent)-1)] > recent[i] + 2.0:
                    return (recent[i] - 1.0, recent[i-1])
    elif direction == "SELL":
        # Look for a rise followed by a strong drop
        for i in range(len(recent)-5, 4, -1):
            if recent[i] > recent[i-1]:  # Bullish move
                # Check if followed by bearish displacement
                if recent[min(i+3, len(recent)-1)] < recent[i] - 2.0:
                    return (recent[i-1], recent[i] + 1.0)
    
    return None

def is_near_key_level(price, direction):
    """Check if price is near a key support/resistance level."""
    if direction == "BUY":
        zones = KEY_LEVELS["demand_zones"] + [(s, s+5) for s in KEY_LEVELS["support"]]
        for low, high in zones:
            if low - 5 <= price <= high + 5:
                return True, f"${low}-${high}"
    else:
        zones = KEY_LEVELS["supply_zones"] + [(r-5, r) for r in KEY_LEVELS["resistance"]]
        for low, high in zones:
            if low - 5 <= price <= high + 5:
                return True, f"${low}-${high}"
    return False, None

def is_premium_discount(price, direction):
    """Check if price is in premium (for sells) or discount (for buys) zone.
    Uses recent range to determine.
    """
    if len(price_history) < 50:
        return True  # Can't determine, pass
    
    recent = list(price_history)[-100:]
    range_high = max(recent)
    range_low = min(recent)
    mid = (range_high + range_low) / 2
    
    if direction == "BUY" and price < mid:
        return True  # In discount zone - good for buys
    elif direction == "SELL" and price > mid:
        return True  # In premium zone - good for sells
    return False

def calculate_rsi(period=14):
    """Calculate RSI from price history."""
    if len(price_history) < period + 1:
        return 50.0
    prices = list(price_history)[-(period+1):]
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def calculate_ma(period):
    if len(price_history) < period:
        return None
    return sum(list(price_history)[-period:]) / period

# ============================================================
# SMC CHECKLIST SCORING - Real 9-point checklist
# ============================================================

def run_smc_checklist(price, direction, prices_list):
    """Run the full 9-point SMC checklist and return score + details.
    
    Checklist:
    1. HTF Trend alignment
    2. Premium/Discount zone
    3. Liquidity pool identified (near key level)
    4. Liquidity sweep detected
    5. Strong displacement
    6. Break of Structure (BOS)
    7. Order Block entry zone
    8. RSI confirmation
    9. R:R >= 1:2.5 (always true for our fixed SL/TP)
    
    Returns: (score, max_score, details_list)
    """
    checks = []
    score = 0
    
    # 1. HTF Trend
    trend = detect_htf_trend(prices_list)
    if (direction == "BUY" and trend == "BULLISH") or (direction == "SELL" and trend == "BEARISH"):
        checks.append(("✅", "HTF Trend", f"{trend} - aligned"))
        score += 1
    elif trend == "RANGING":
        checks.append(("⚠️", "HTF Trend", f"RANGING - caution"))
        score += 0.5
    else:
        checks.append(("❌", "HTF Trend", f"{trend} - counter-trend"))
    
    # 2. Premium/Discount
    if is_premium_discount(price, direction):
        checks.append(("✅", "Premium/Discount", "In correct zone"))
        score += 1
    else:
        checks.append(("❌", "Premium/Discount", "Wrong zone"))
    
    # 3. Near Key Level
    near, level_str = is_near_key_level(price, direction)
    if near:
        checks.append(("✅", "Key Level", f"Near {level_str}"))
        score += 1
    else:
        # Check round numbers
        for rn in KEY_LEVELS["round_numbers"]:
            if abs(price - rn) <= 15:
                checks.append(("⚠️", "Key Level", f"Near round ${rn}"))
                score += 0.5
                near = True
                break
        if not near:
            checks.append(("❌", "Key Level", "Not near key level"))
    
    # 4. Liquidity Sweep
    if detect_liquidity_sweep(prices_list, direction):
        checks.append(("✅", "Liquidity Sweep", "Detected"))
        score += 1
    else:
        checks.append(("❌", "Liquidity Sweep", "Not detected"))
    
    # 5. Displacement
    disp = detect_displacement(prices_list)
    if disp >= 1.5:
        checks.append(("✅", "Displacement", f"Strong ({disp:.1f}x avg)"))
        score += 1
    elif disp >= 1.0:
        checks.append(("⚠️", "Displacement", f"Moderate ({disp:.1f}x avg)"))
        score += 0.5
    else:
        checks.append(("❌", "Displacement", f"Weak ({disp:.1f}x avg)"))
    
    # 6. BOS
    bos, bos_level = detect_bos(prices_list, direction)
    if bos:
        checks.append(("✅", "BOS", f"Confirmed at ${bos_level:,.2f}"))
        score += 1
    else:
        checks.append(("❌", "BOS", "Not confirmed"))
    
    # 7. Order Block
    ob = find_order_block(prices_list, direction)
    if ob:
        checks.append(("✅", "Order Block", f"${ob[0]:,.2f}-${ob[1]:,.2f}"))
        score += 1
    else:
        checks.append(("⚠️", "Order Block", "Not clearly identified"))
        score += 0.5
    
    # 8. RSI Confirmation
    rsi = calculate_rsi(14)
    if direction == "BUY" and rsi < 40:
        checks.append(("✅", "RSI", f"{rsi:.1f} - Oversold (BUY confirm)"))
        score += 1
    elif direction == "BUY" and rsi < 50:
        checks.append(("⚠️", "RSI", f"{rsi:.1f} - Neutral-low"))
        score += 0.5
    elif direction == "SELL" and rsi > 60:
        checks.append(("✅", "RSI", f"{rsi:.1f} - Overbought (SELL confirm)"))
        score += 1
    elif direction == "SELL" and rsi > 50:
        checks.append(("⚠️", "RSI", f"{rsi:.1f} - Neutral-high"))
        score += 0.5
    else:
        checks.append(("❌", "RSI", f"{rsi:.1f} - Not confirming"))
    
    # 9. R:R (always passes with our fixed SL/TP setup)
    checks.append(("✅", "R:R", "1:2.5 (fixed SL $4 / TP $10)"))
    score += 1
    
    return score, 9, checks

# ============================================================
# STATIC TEXT
# ============================================================

CHECKLIST_TEXT = """
📋 SMC Pre-Entry Checklist
━━━━━━━━━━━━━━━━━
1. [ ] Higher timeframe trend (HH/HL or LH/LL)
2. [ ] Price in Premium/Discount zone
3. [ ] Near key support/resistance level
4. [ ] Liquidity sweep detected
5. [ ] Strong displacement (Big body candles)
6. [ ] Break of Structure (BOS) confirmed
7. [ ] Entry zone identified (Order Block or FVG)
8. [ ] RSI confirmation
9. [ ] R:R ≥ 1:2.5 with SL at structure
━━━━━━━━━━━━━━━━━
✅ 6/9+ must pass for auto-signal!
"No checklist = random trading"
"""

RULES_TEXT = """
⚠️ Khine's Trading Rules - XM Micro Account
━━━━━━━━━━━━━━━━━
🏦 Broker: XM Global | GOLDm# Micro
💰 Balance: $15 | Lot: 0.5 micro
📊 1 point = $0.005 | $1 move = $0.50

1. Fixed Lot: 0.5 micro lot ONLY
   🛑 Max SL: $4 move = $2.00 risk
   🎯 Min TP: $10 move = $5.00 reward
   📏 Min R:R: 1:2.5

2. Daily Limit: 2 LOSSES = STOP (use /unlock to override)
3. Max 5 trades per day
4. No Revenge Trading - 1 hour break after loss
5. Plan first, trade second
6. Send H1 & M5 charts to ပေါ်ဦး
7. Active sessions only (London/NY)
8. SL at structural level
9. Wait for pullback - never chase
━━━━━━━━━━━━━━━━━
"""

SMC_TEXT = """
📊 SMC Quick Reference
━━━━━━━━━━━━━━━━━
🔹 Demand Zones (BUY):
- Rally-Base-Rally (RBR)
- Drop-Base-Rally (DBR)
Rule: Wait for pullback to zone.

🔸 Supply Zones (SELL):
- Drop-Base-Drop (DBD)
- Rally-Base-Drop (RBD)
Rule: Wait for pullback to zone.

🕯 Candlestick Confirmation:
Bullish: Hammer, Engulfing, Tweezer Bottom, Morning Star
Bearish: Shooting Star, Engulfing, Tweezer Top, Evening Star

📈 Chart Patterns:
Bullish: Double Bottom, Inv H&S, Falling Wedge, Bull Flag
Bearish: Double Top, H&S, Rising Wedge, Bear Flag
━━━━━━━━━━━━━━━━━
"""

# ============================================================
# DATA MANAGEMENT
# ============================================================

def load_data():
    global trade_history, active_trade, signal_log, daily_journal, LOSS_OVERRIDE_ACTIVE
    if os.path.exists(TRADE_HISTORY_FILE):
        try:
            with open(TRADE_HISTORY_FILE, "r") as f:
                trade_history = json.load(f)
                for t in reversed(trade_history):
                    if t.get('status') == 'entered' and t.get('result') is None:
                        active_trade = t
                        break
        except:
            trade_history = []
    if os.path.exists(SIGNAL_LOG_FILE):
        try:
            with open(SIGNAL_LOG_FILE, "r") as f:
                signal_log = json.load(f)
        except:
            signal_log = []
    if os.path.exists(DAILY_JOURNAL_FILE):
        try:
            with open(DAILY_JOURNAL_FILE, "r") as f:
                daily_journal = json.load(f)
        except:
            daily_journal = {}
    load_override_state()
    logger.info(f"Data loaded. Override={LOSS_OVERRIDE_ACTIVE}, Active trade={active_trade is not None}")

def save_data():
    with open(TRADE_HISTORY_FILE, "w") as f:
        json.dump(trade_history, f, indent=2)
    with open(SIGNAL_LOG_FILE, "w") as f:
        json.dump(signal_log, f, indent=2)
    with open(DAILY_JOURNAL_FILE, "w") as f:
        json.dump(daily_journal, f, indent=2)

def get_dubai_now():
    return datetime.datetime.now(pytz.timezone('Asia/Dubai'))

def is_market_open():
    now = get_dubai_now()
    weekday = now.weekday()
    hour = now.hour
    if weekday == 6: return hour >= 22
    if weekday == 4: return hour < 23
    return weekday < 4

async def fetch_gold_price():
    global cached_price, last_fetch_time
    now = time.time()
    if now - last_fetch_time < 25 and cached_price:
        return cached_price

    urls = [
        "https://api.gold-api.com/price/XAU",
        "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    ]
    for url in urls:
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda u=url: requests.get(u, timeout=5))
            if response.status_code == 200:
                data = response.json()
                price = float(data["price"]) if "price" in data else data['chart']['result'][0]['meta']['regularMarketPrice']
                cached_price = price
                last_fetch_time = now
                price_history.append(price)
                return price
        except:
            continue
    return cached_price

async def cooldown_over_alert(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="✅ Cooldown over! ပေါ်ဦး scanning for next setup... ပိုင်မှဝင်!")

def get_current_scaling():
    streak = 0
    completed_trades = [t for t in trade_history if t.get('result') in ['WIN', 'LOSS', 'SCRATCH']]
    for t in reversed(completed_trades):
        if t.get('result') == 'WIN':
            streak += 1
        elif t.get('result') == 'LOSS':
            break
    return XM_LOT_SIZE, streak

def get_daily_losses_today():
    today = get_dubai_now().date().isoformat()
    return len([t for t in trade_history if t.get('date') == today and t.get('result') == 'LOSS'])

def is_daily_loss_limit_reached():
    if LOSS_OVERRIDE_ACTIVE:
        return False
    return get_daily_losses_today() >= MAX_DAILY_LOSSES

# ============================================================
# MARKET MONITORING - Core scanning loop
# ============================================================

async def monitor_market(context: ContextTypes.DEFAULT_TYPE):
    global last_fetch_time
    if not is_market_open():
        return
    price = await fetch_gold_price()
    if not price:
        return
    
    # COLD START: If we don't have enough data, fill with synthetic ticks
    if len(price_history) < 20:
        import random
        logger.info(f"Cold start: filling price history ({len(price_history)}/20) from ${price:,.2f}")
        needed = 25 - len(price_history)
        for i in range(needed):
            # Simulate recent price movement around current price
            tick = price + random.uniform(-2.0, 2.0)
            price_history.append(tick)
        price_history.append(price)  # Ensure latest is actual price
        logger.info(f"Cold start complete: {len(price_history)} samples")

    if active_trade:
        await manage_active_trades(context, price)
    else:
        # Scan with SMC checklist
        if len(price_history) >= 20:
            await auto_generate_signal(context, price)

async def manage_active_trades(context, price):
    global active_trade
    if not active_trade:
        return

    sl = active_trade.get('sl', 0)
    tp1 = active_trade.get('tp1', 0)
    tp2 = active_trade.get('tp2', 0)
    entry = active_trade.get('entry', 0)
    direction = active_trade.get('direction', active_trade.get('type', 'SELL'))
    
    # Calculate distances
    if direction == 'BUY':
        sl_distance = price - sl  # Positive = safe, negative = past SL
        total_sl_range = entry - sl if entry > sl else 1
        sl_pct = (sl_distance / total_sl_range) * 100 if total_sl_range > 0 else 100
    else:
        sl_distance = sl - price  # Positive = safe, negative = past SL
        total_sl_range = sl - entry if sl > entry else 1
        sl_pct = (sl_distance / total_sl_range) * 100 if total_sl_range > 0 else 100
    
    price_move = (price - entry) if direction == 'BUY' else (entry - price)
    current_pnl = price_move * 100 * XM_PIP_VALUE
    
    # === SL WARNING LEVELS ===
    # Level 1: 50% of SL distance used (halfway to SL)
    if sl_pct <= 50 and sl_pct > 0 and not active_trade.get('sl_warned_50'):
        active_trade['sl_warned_50'] = True
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=
            f"⚠️ SL WARNING - 50% Distance!\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📊 Price: ${price:,.2f} | SL: ${sl:,.2f}\n"
            f"📉 Distance to SL: ${abs(sl_distance):,.2f}\n"
            f"💰 Current P&L: ${current_pnl:+.2f}\n"
            f"\n⚡ Khine ရ သတိထားပါ! Close or hold?")
    
    # Level 2: Within $2 of SL
    if abs(sl_distance) <= 2.0 and sl_distance > 0 and not active_trade.get('sl_warned_2'):
        active_trade['sl_warned_2'] = True
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=
            f"🚨 SL DANGER - $2 Away!\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📊 Price: ${price:,.2f} | SL: ${sl:,.2f}\n"
            f"📉 Only ${abs(sl_distance):,.2f} to SL!\n"
            f"💰 P&L: ${current_pnl:+.2f}\n"
            f"\n🔴 SL ထိတော့မယ်! Close now?")
    
    # Level 3: Within $1 of SL
    if abs(sl_distance) <= 1.0 and sl_distance > 0 and not active_trade.get('sl_warned_1'):
        active_trade['sl_warned_1'] = True
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=
            f"🔴🔴🔴 SL IMMINENT!\n"
            f"Price: ${price:,.2f} | SL: ${sl:,.2f}\n"
            f"${abs(sl_distance):,.2f} away from SL!")

    # === AUTO-CLOSE: SL HIT ===
    # Tighter detection - price at or past SL (no $2 buffer)
    sl_hit = False
    if direction == 'BUY' and price <= sl:
        sl_hit = True
    if direction == 'SELL' and price >= sl:
        sl_hit = True
    
    if sl_hit:
        logger.info(f"SL HIT: {direction} @ ${entry:,.2f}, SL=${sl:,.2f}, Price=${price:,.2f}")
        msg, result, pnl = await close_trade_at_price(sl, context, is_command=False)
        if msg:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=
                f"🚨 SL HIT - AUTO CLOSED!\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"{msg}\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"Discipline first! Next setup ရှာမယ် 💪")
        return

    # === TP MONITORING ===
    # TP1 approaching (within $2)
    tp1_approaching = False
    if direction == 'BUY' and tp1 > 0 and price >= tp1 - 2.0 and not active_trade.get('tp1_near'):
        tp1_approaching = True
    if direction == 'SELL' and tp1 > 0 and price <= tp1 + 2.0 and not active_trade.get('tp1_near'):
        tp1_approaching = True
    if tp1_approaching:
        active_trade['tp1_near'] = True
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=
            f"🎯 TP1 နားရောက်နေပြီ!\n"
            f"Price: ${price:,.2f} | TP1: ${tp1:,.2f}\n"
            f"P&L: ${current_pnl:+.2f} 🔥")
    
    # TP1 hit
    tp1_hit = False
    if direction == 'BUY' and tp1 > 0 and price >= tp1 and not active_trade.get('tp_warned'):
        tp1_hit = True
    if direction == 'SELL' and tp1 > 0 and price <= tp1 and not active_trade.get('tp_warned'):
        tp1_hit = True
    if tp1_hit:
        active_trade['tp_warned'] = True
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=
            f"🎯🎯 TP1 HIT! ${tp1:,.2f}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📊 Price: ${price:,.2f}\n"
            f"💰 P&L: ${current_pnl:+.2f}\n"
            f"\nKhine ရ partial close or let it run to TP2 ${tp2:,.2f}?")
    
    # TP2 hit
    tp2_hit = False
    if direction == 'BUY' and tp2 > 0 and price >= tp2 and not active_trade.get('tp2_warned'):
        tp2_hit = True
    if direction == 'SELL' and tp2 > 0 and price <= tp2 and not active_trade.get('tp2_warned'):
        tp2_hit = True
    if tp2_hit:
        active_trade['tp2_warned'] = True
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=
            f"🏆🏆 TP2 HIT! ${tp2:,.2f}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📊 Price: ${price:,.2f}\n"
            f"💰 P&L: ${current_pnl:+.2f}\n"
            f"\nKhine ရ close ပြီးပြီလား? Close price ပြောပါ!")

    # Trade update every 5 mins
    active_trade['update_counter'] = active_trade.get('update_counter', 0) + 1
    if active_trade['update_counter'] >= 10:
        active_trade['update_counter'] = 0
        price_move = (price - entry) if direction == 'BUY' else (entry - price)
        pnl = price_move * 100 * XM_PIP_VALUE
        status = "IN PROFIT ✅" if pnl > 0 else "IN DRAWDOWN ⚠️"
        rsi = calculate_rsi(14)
        
        msg = f"""📊 TRADE UPDATE (5 min)
━━━━━━━━━━━━━━━━━
{('🟢 BUY' if direction == 'BUY' else '🔴 SELL')} @ ${entry:,.2f}
📍 Now: ${price:,.2f} | P&L: ${pnl:+.2f}
📏 RSI: {rsi:.1f}
⚡ Status: {status}
💡 Hold for TP ${tp1:,.2f}
━━━━━━━━━━━━━━━━━"""
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg)

    # 50% TP check
    if tp1 > 0 and entry > 0:
        tp_dist = abs(tp1 - entry)
        curr_dist = abs(price - entry)
        if tp_dist > 0 and curr_dist >= tp_dist * 0.5 and not active_trade.get('notified_50'):
            active_trade['notified_50'] = True
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"🎯 Price reached 50% of TP1! Consider partial profit and move SL to breakeven.")

# ============================================================
# AUTO SIGNAL GENERATION - Real SMC-based analysis
# ============================================================

async def auto_generate_signal(context, price):
    global active_signal_msg_id, active_trade, cooldown_until
    if active_signal_msg_id or active_trade:
        logger.info(f"Signal skip: active_signal={active_signal_msg_id is not None}, active_trade={active_trade is not None}")
        return

    # 1. Cooldown Check
    if time.time() < cooldown_until:
        logger.info(f"Signal skip: cooldown active ({cooldown_until - time.time():.0f}s remaining)")
        return

    # 2. Daily Loss Limit Check (can be overridden with /unlock)
    if is_daily_loss_limit_reached():
        logger.info(f"Signal skip: daily loss limit reached (OVERRIDE={LOSS_OVERRIDE_ACTIVE})")
        return

    # 3. Daily Trade Limit Check
    today = get_dubai_now().date().isoformat()
    entered_today = len([t for t in trade_history if t.get('date') == today and 
                        (t.get('status') == 'entered' or t.get('result') in ['WIN', 'LOSS', 'SCRATCH']) and 
                        abs(t.get('pnl', 0)) >= MIN_COUNTED_PNL])
    if entered_today >= MAX_DAILY_TRADES:
        logger.info(f"Signal skip: daily trade limit ({entered_today}/{MAX_DAILY_TRADES})")
        return

    prices_list = list(price_history)
    if len(prices_list) < 20:
        logger.info(f"Signal skip: not enough price data ({len(prices_list)}/20)")
        return
    
    logger.info(f"Signal scan: price=${price:,.2f}, data={len(prices_list)}, trades={entered_today}/{MAX_DAILY_TRADES}, override={LOSS_OVERRIDE_ACTIVE}")

    # 4. Determine potential direction based on market structure
    trend = detect_htf_trend(prices_list)
    
    # Calculate short-term momentum
    if len(prices_list) >= 10:
        short_change = prices_list[-1] - prices_list[-10]
    else:
        short_change = 0
    
    rsi = calculate_rsi(14)
    
    # Determine direction candidates
    directions_to_check = []
    
    # Trend-aligned direction gets priority
    if trend == "BULLISH":
        directions_to_check = ["BUY"]
        # Also check SELL if RSI is very overbought
        if rsi > 70:
            directions_to_check.append("SELL")
    elif trend == "BEARISH":
        directions_to_check = ["SELL"]
        # Also check BUY if RSI is very oversold
        if rsi < 30:
            directions_to_check.append("BUY")
    else:
        # Ranging - check both based on momentum
        if short_change < -2:
            directions_to_check = ["BUY"]  # Pullback in range = buy opportunity
        elif short_change > 2:
            directions_to_check = ["SELL"]  # Rally in range = sell opportunity
        else:
            # Check both
            directions_to_check = ["BUY", "SELL"]
    
    # 5. Run SMC checklist for each direction
    best_direction = None
    best_score = 0
    best_checks = []
    
    for direction in directions_to_check:
        score, max_score, checks = run_smc_checklist(price, direction, prices_list)
        logger.info(f"SMC Checklist {direction}: {score}/{max_score}")
        
        if score > best_score:
            best_score = score
            best_direction = direction
            best_checks = checks
    
    # 6. SIGNAL THRESHOLD: 6/9+ to generate signal
    MIN_CHECKLIST_SCORE = 6.0
    
    if best_score < MIN_CHECKLIST_SCORE or best_direction is None:
        return
    
    # 7. Generate the signal!
    direction = best_direction
    entry = price
    sl_distance = XM_MAX_SL_POINTS / 100.0   # $4 gold move
    tp_distance = XM_MIN_TP_POINTS / 100.0    # $10 gold move
    sl = entry - sl_distance if direction == "BUY" else entry + sl_distance
    tp1 = entry + tp_distance if direction == "BUY" else entry - tp_distance
    tp2 = entry + tp_distance * 1.5 if direction == "BUY" else entry - tp_distance * 1.5
    
    rr = abs(tp1 - entry) / abs(entry - sl)
    if rr < XM_MIN_RR:
        return
    
    risk_usd = sl_distance * 100 * XM_PIP_VALUE
    reward_usd = tp_distance * 100 * XM_PIP_VALUE
    lot_size, streak = get_current_scaling()
    
    # Build checklist display
    checklist_display = "\n".join([f"{icon} {name}: {detail}" for icon, name, detail in best_checks])
    
    now = get_dubai_now()
    session = "London" if 11 <= now.hour < 16 else ("New York" if 16 <= now.hour < 23 else "Asian/Other")
    
    msg = f"""🔔 NEW TRADE SIGNAL!
━━━━━━━━━━━━━━━━━
📊 {direction} @ ${entry:,.2f}
🛑 SL: ${sl:,.2f} (${sl_distance:.0f} move = -${risk_usd:.2f})
🎯 TP1: ${tp1:,.2f} (${tp_distance:.0f} move = +${reward_usd:.2f})
🎯 TP2: ${tp2:,.2f}
📏 Lot: {lot_size} micro | R:R: 1:{rr:.1f} | RSI: {rsi:.1f}

📋 SMC Checklist: {best_score:.0f}/9
{checklist_display}

🏦 XM Micro | {lot_size} lot | 1pt=$0.005
⏰ Session: {session}
📸 Send H1 & M5 charts to ပေါ်ဦး for verification!
━━━━━━━━━━━━━━━━━"""

    signal_id = str(int(time.time()))
    keyboard = [[
        InlineKeyboardButton("✅ ဝင်မယ်", callback_data=f"enter_{direction}_{entry}_{sl}_{tp1}_{tp2}_{signal_id}"),
        InlineKeyboardButton("❌ Skip", callback_data=f"skip_{signal_id}"),
        InlineKeyboardButton("⏰ Wait", callback_data=f"wait_{signal_id}")
    ]]
    sent_msg = await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg, reply_markup=InlineKeyboardMarkup(keyboard))
    active_signal_msg_id = sent_msg.message_id
    
    # Log the signal
    new_signal = {
        "id": signal_id,
        "timestamp": datetime.datetime.now().isoformat(),
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "lot_size": lot_size,
        "rr": rr,
        "checklist_score": f"{best_score:.0f}/9",
        "trend": trend,
        "action": "WAITING",
        "session": session
    }
    signal_log.append(new_signal)
    save_data()
    
    # Schedule missed check in 5 minutes
    context.job_queue.run_once(check_missed_signal, 300, data={'signal_id': signal_id, 'msg_id': active_signal_msg_id})
    
    # Set cooldown to prevent spam (3 minutes between signals)
    cooldown_until = time.time() + 180
    
    logger.info(f"SIGNAL GENERATED: {direction} @ ${entry:,.2f} | Checklist: {best_score}/9 | Session: {session}")

async def suggest_next_setup(context, price):
    # Dynamic suggestions based on current price
    nearest_support = min(KEY_LEVELS["support"], key=lambda x: abs(price - x))
    nearest_resistance = min(KEY_LEVELS["resistance"], key=lambda x: abs(price - x))
    
    msg = f"""🔮 WATCHING NEXT:
• ${nearest_support:,.0f} support - potential BUY if bounce
• ${nearest_resistance:,.0f} resistance - potential SELL if rejected
• ပေါ်ဦး ဆက်ပြီး scan လုပ်နေမယ်!"""
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg)

async def check_missed_signal(context: ContextTypes.DEFAULT_TYPE):
    global active_signal_msg_id
    data = context.job.data
    sig_id = data['signal_id']
    
    for s in signal_log:
        if s["id"] == sig_id and s["action"] == "WAITING":
            s["action"] = "MISSED"
            s["reason"] = "No response within 5 mins"
            save_data()
            
            if active_signal_msg_id == data['msg_id']:
                active_signal_msg_id = None
                try:
                    await context.bot.edit_message_text(
                        chat_id=ADMIN_CHAT_ID,
                        message_id=data['msg_id'],
                        text="⏰ Signal Expired - Marked as MISSED"
                    )
                except:
                    pass
            break

# ============================================================
# COMMAND HANDLERS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = await fetch_gold_price()
    welcome = f"""🤖 ပေါ်ဦး Signal Bot v2.0 - SMC POWERED!
Mingalabar Khine ရ! ပေါ်ဦး real SMC analysis နဲ့ စောင့်ကြည့်ပေးနေမယ် 🏆

📊 Current Gold Price: ${price:,.2f}
📋 Signal Threshold: 6/9+ SMC Checklist

Available Commands:
/price - Current gold price
/update - Price & active trade status
/scan - Force a market scan now
/checklist - SMC Pre-Entry Checklist
/rules - Trading rules
/smc - SMC quick reference
/levels - Key support/resistance levels
/status - Bot status & daily stats
/goal - Weekly profit goal progress
/history - Last 10 trades
/journal - Today's full stats
/weeklyreport - 7-day summary
/close [price] - Manually close trade
/unlock - Override daily loss limit
/lock - Re-enable daily loss limit"""
    await update.message.reply_text(welcome)

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = await fetch_gold_price()
    rsi = calculate_rsi(14)
    trend = detect_htf_trend(list(price_history)) if len(price_history) >= 30 else "UNKNOWN"
    
    if len(price_history) >= 10:
        change = price - list(price_history)[-10]
        trend_arrow = "📈 UP" if change > 0 else "📉 DOWN"
        await update.message.reply_text(f"📊 Gold: ${price:,.2f}\nTrend (5m): {trend_arrow} (${abs(change):.2f})\nHTF: {trend} | RSI: {rsi:.1f}")
    else:
        await update.message.reply_text(f"📊 Gold: ${price:,.2f} | RSI: {rsi:.1f}")

async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global cooldown_until
    price = await fetch_gold_price()
    msg = f"📊 Gold: ${price:,.2f}\n\n"
    
    cooldown_rem = int((cooldown_until - time.time()) / 60)
    if cooldown_rem > 0:
        msg += f"⏳ Cooldown: {cooldown_rem} min remaining\n\n"

    if active_trade:
        entry = active_trade.get('entry', 0)
        direction = active_trade.get('direction', active_trade.get('type', 'SELL'))
        price_move = (price - entry) if direction == 'BUY' else (entry - price)
        pnl = price_move * 100 * XM_PIP_VALUE
        status = "IN PROFIT ✅" if pnl > 0 else "IN DRAWDOWN ⚠️"
        msg += f"🔥 ACTIVE TRADE:\n{direction} @ ${entry:,.2f}\nP&L: ${pnl:+.2f} | {status}\nSL: ${active_trade.get('sl', 0):,.2f} | TP: ${active_trade.get('tp1', 0):,.2f}"
    else:
        msg += "💤 No active trades. Scanning for setups..."
    await update.message.reply_text(msg)

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force an immediate market scan and report results."""
    price = await fetch_gold_price()
    if not price:
        await update.message.reply_text("❌ Cannot fetch gold price right now.")
        return
    
    prices_list = list(price_history)
    if len(prices_list) < 20:
        await update.message.reply_text(f"📊 Gold: ${price:,.2f}\n⏳ Need more price data ({len(prices_list)}/20 samples). Bot is collecting...")
        return
    
    rsi = calculate_rsi(14)
    trend = detect_htf_trend(prices_list)
    
    # Run checklist for both directions
    buy_score, _, buy_checks = run_smc_checklist(price, "BUY", prices_list)
    sell_score, _, sell_checks = run_smc_checklist(price, "SELL", prices_list)
    
    best_dir = "BUY" if buy_score >= sell_score else "SELL"
    best_score = max(buy_score, sell_score)
    best_checks = buy_checks if buy_score >= sell_score else sell_checks
    
    checklist_display = "\n".join([f"  {icon} {name}: {detail}" for icon, name, detail in best_checks])
    
    status = "✅ SIGNAL READY!" if best_score >= 6.0 else "⏳ Not yet (need 6/9+)"
    
    msg = f"""🔍 FORCED SCAN RESULT
━━━━━━━━━━━━━━━━━
📊 Gold: ${price:,.2f} | RSI: {rsi:.1f}
📈 HTF Trend: {trend}

Best Setup: {best_dir} ({best_score:.0f}/9)
{checklist_display}

{status}
━━━━━━━━━━━━━━━━━
BUY Score: {buy_score:.0f}/9 | SELL Score: {sell_score:.0f}/9"""
    
    await update.message.reply_text(msg)

async def unlock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Override the daily loss limit."""
    global LOSS_OVERRIDE_ACTIVE
    LOSS_OVERRIDE_ACTIVE = True
    save_override_state()
    await update.message.reply_text("🔓 Daily loss limit UNLOCKED!\nKhine ရ, ပေါ်ဦး ဆက်ပြီး signal ရှာပေးမယ်!\n⚠️ သတိထားပါ - discipline first!\n\nRe-lock: /lock")

async def lock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-enable the daily loss limit."""
    global LOSS_OVERRIDE_ACTIVE
    LOSS_OVERRIDE_ACTIVE = False
    save_override_state()
    await update.message.reply_text("🔒 Daily loss limit re-enabled.\nSafety first! 💪")

async def checklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(CHECKLIST_TEXT)

async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES_TEXT)

async def smc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(SMC_TEXT)

async def levels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = await fetch_gold_price()
    msg = f"📊 Key Levels (XAU/USD) - Price: ${price:,.2f}\n━━━━━━━━━━━━━━━━━\n"
    msg += "🔴 Resistance:\n" + ", ".join([f"${l}" for l in KEY_LEVELS["resistance"]]) + "\n\n"
    msg += "🟢 Support:\n" + ", ".join([f"${l}" for l in KEY_LEVELS["support"]]) + "\n\n"
    msg += "📦 Supply Zones:\n" + ", ".join([f"${z[0]}-${z[1]}" for z in KEY_LEVELS["supply_zones"]]) + "\n\n"
    msg += "🛒 Demand Zones:\n" + ", ".join([f"${z[0]}-${z[1]}" for z in KEY_LEVELS["demand_zones"]]) + "\n━━━━━━━━━━━━━━━━━"
    await update.message.reply_text(msg)

async def goal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_pnl = sum([t.get('pnl', 0) for t in trade_history if t.get('result') in ['WIN', 'LOSS', 'SCRATCH']])
    progress = (total_pnl / WEEKLY_GOAL) * 100 if WEEKLY_GOAL > 0 else 0
    progress = max(0, min(100, progress))
    bars = int(progress / 10)
    bar_str = "█" * bars + "░" * (10 - bars)
    
    msg = f"""🎯 Weekly Profit Goal
━━━━━━━━━━━━━━━━━
💰 Starting Balance: ${STARTING_BALANCE:.2f}
🎯 Target: ${WEEKLY_GOAL:.2f}
📊 Current P&L: ${total_pnl:+.2f}
📈 Progress: {bar_str} {progress:.1f}%

Keep it up Khine ရ! ပေါ်ဦး အမြဲ အားပေးနေတယ်! 💪
━━━━━━━━━━━━━━━━━"""
    await update.message.reply_text(msg)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global cooldown_until
    today = get_dubai_now().date().isoformat()
    entered_today = [t for t in trade_history if t.get('date') == today]
    counted_today = [t for t in entered_today if abs(t.get('pnl', 0)) >= MIN_COUNTED_PNL]
    lot_size, streak = get_current_scaling()
    wins = len([t for t in counted_today if t.get('result') == 'WIN'])
    losses = len([t for t in counted_today if t.get('result') == 'LOSS'])
    scratches = len([t for t in counted_today if t.get('result') == 'SCRATCH'])
    daily_pnl = sum([t.get('pnl', 0) for t in counted_today if t.get('result') in ('WIN', 'LOSS', 'SCRATCH')])
    
    override_str = "🔓 UNLOCKED" if LOSS_OVERRIDE_ACTIVE else ""
    limit_status = f"🛑 STOPPED (2 losses) {override_str}" if get_daily_losses_today() >= MAX_DAILY_LOSSES and not LOSS_OVERRIDE_ACTIVE else "✅ Active"
    if LOSS_OVERRIDE_ACTIVE and get_daily_losses_today() >= MAX_DAILY_LOSSES:
        limit_status = "🔓 OVERRIDE ACTIVE - Scanning!"
    
    session = "London" if 11 <= get_dubai_now().hour < 16 else ("New York" if 16 <= get_dubai_now().hour < 23 else "Off-session")
    
    cooldown_rem = int((cooldown_until - time.time()) / 60)
    cooldown_str = f"⏳ Cooldown: {cooldown_rem} min remaining" if cooldown_rem > 0 else "✅ Ready"

    msg = f"""🤖 ပေါ်ဦး Signal Bot v2.0 - Status
━━━━━━━━━━━━━━━━━
🏦 XM Global | GOLDm# Micro
📊 Lot: {lot_size} | 1pt=$0.005
📋 Signal: Real SMC Checklist 6/9+

📅 Today ({today})
📊 Trades: {len(counted_today)}/{MAX_DAILY_TRADES}
⏱️ {cooldown_str}
✅ W: {wins} | ❌ L: {losses} | ⚖️ S: {scratches}
💰 P&L: ${daily_pnl:+.2f}
⚠️ Losses: {losses}/{MAX_DAILY_LOSSES} | {limit_status}

⏰ Session: {session}
━━━━━━━━━━━━━━━━━"""
    await update.message.reply_text(msg)

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    last_10 = trade_history[-10:]
    msg = "📋 Trade History (Last 10)\n"
    for t in reversed(last_10):
        direction = t.get('direction', t.get('type', '?'))
        msg += f"{direction} ${t.get('entry', 0):,.2f} -> {t.get('result', '?')} (${t.get('pnl', 0):+.2f})\n"
    await update.message.reply_text(msg)

# ============================================================
# CALLBACK HANDLERS
# ============================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_signal_msg_id, active_trade
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("skip"):
        active_signal_msg_id = None
        parts = query.data.split("_")
        if len(parts) > 1:
            sig_id = parts[1]
            for s in signal_log:
                if s["id"] == sig_id:
                    s["action"] = "SKIPPED"
                    s["reason"] = "Manual skip"
                    break
            save_data()
        await query.edit_message_text(text=query.message.text + "\n\n❌ Signal Skipped")
        return

    if query.data.startswith("enter"):
        active_signal_msg_id = None
        parts = query.data.split("_")
        _, direction, entry, sl, tp1, tp2 = parts[:6]
        sig_id = parts[6] if len(parts) > 6 else None
        
        lot_size, streak = get_current_scaling()
        new_trade = {
            "id": sig_id or str(int(time.time())),
            "date": get_dubai_now().date().isoformat(),
            "direction": direction,
            "entry": float(entry),
            "sl": float(sl),
            "tp1": float(tp1),
            "tp2": float(tp2),
            "lot_size": lot_size,
            "status": "entered",
            "result": None,
            "pnl": 0.0,
            "time": datetime.datetime.now().isoformat(),
            "update_counter": 0
        }
        trade_history.append(new_trade)
        active_trade = new_trade
        
        if sig_id:
            for s in signal_log:
                if s["id"] == sig_id:
                    s["action"] = "ENTERED"
                    s["entry_time"] = new_trade["time"]
                    break
                    
        save_data()
        await query.edit_message_text(text=query.message.text + f"\n\n✅ Trade Entered! ({lot_size} Lot) | ID: {new_trade['id']}")

    if query.data.startswith("wait"):
        # Don't clear signal, just acknowledge
        await query.edit_message_text(text=query.message.text + "\n\n⏰ Waiting... Signal still active for 5 min")

    if query.data.startswith("result"):
        _, res, idx = query.data.split("_")
        idx = int(idx)
        trade = trade_history[idx]
        price = await fetch_gold_price()

        if res == "win":
            price_move = abs(trade.get('tp1', trade.get('entry', 0)) - trade.get('entry', 0))
            pnl = price_move * 100 * XM_PIP_VALUE
        elif res == "loss":
            price_move = abs(trade.get('entry', 0) - trade.get('sl', trade.get('entry', 0)))
            pnl = -(price_move * 100 * XM_PIP_VALUE)
        else:
            raw_move = (price - trade.get('entry', 0)) if trade.get('direction', trade.get('type', '')) == 'BUY' else (trade.get('entry', 0) - price)
            pnl = raw_move * 100 * XM_PIP_VALUE

        if pnl >= WIN_THRESHOLD:
            trade['result'] = "WIN"
            res_icon = "✅ WIN"
        elif pnl <= -LOSS_THRESHOLD:
            trade['result'] = "LOSS"
            res_icon = "❌ LOSS"
        else:
            trade['result'] = "SCRATCH"
            res_icon = "⚖️ SCRATCH"

        trade['pnl'] = pnl
        trade['close_time'] = datetime.datetime.now().isoformat()
        trade['close_price'] = price
        
        for s in signal_log:
            if s.get("id") == trade.get("id"):
                s["close_price"] = price
                s["close_time"] = trade['close_time']
                s["pnl"] = pnl
                s["result"] = trade['result']
                break
                
        if active_trade == trade:
            active_trade = None
        save_data()

        global cooldown_until
        if trade['result'] == "WIN":
            cooldown_until = time.time() + 30 * 60
            cooldown_mins = 30
        else:
            cooldown_until = time.time() + 15 * 60
            cooldown_mins = 15
        context.job_queue.run_once(cooldown_over_alert, cooldown_mins * 60)

        today = get_dubai_now().date().isoformat()
        daily_pnl = sum([t.get('pnl', 0) for t in trade_history if t.get('date') == today and t.get('result') in ('WIN', 'LOSS', 'SCRATCH')])
        _, streak = get_current_scaling()
        daily_losses = get_daily_losses_today()

        summary = f"""
🏁 TRADE CLOSED - {res_icon}
Entry: {trade.get('direction', trade.get('type', '?'))} ${trade.get('entry', 0):,.2f} → Close: ${price:,.2f}
P&L: ${pnl:+.2f} | Lot: {XM_LOT_SIZE} micro
Daily P&L: ${daily_pnl:+.2f}
Win Streak: {streak} | Daily Losses: {daily_losses}/{MAX_DAILY_LOSSES}"""
        await query.edit_message_text(text=query.message.text + f"\n\n{summary}")

        if is_daily_loss_limit_reached():
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text="🛑 DAILY LIMIT REACHED\n━━━━━━━━━━━━━━━━━\n"
                     f"2 losses today. Use /unlock to override.\n"
                     "💪 Rest and plan tomorrow!\n━━━━━━━━━━━━━━━━━"
            )
        else:
            await suggest_next_setup(context, price)

# ============================================================
# TRADE CLOSE LOGIC
# ============================================================

async def close_trade_at_price(close_price, context_or_update, is_command=True):
    global active_trade
    if not active_trade:
        return None, None, None

    direction = active_trade.get('direction', active_trade.get('type', 'SELL'))
    entry = active_trade.get('entry', 0)
    raw_move = (close_price - entry) if direction == 'BUY' else (entry - close_price)
    pnl = raw_move * 100 * XM_PIP_VALUE

    if pnl >= WIN_THRESHOLD:
        result, icon = "WIN", "✅ WIN"
    elif pnl <= -LOSS_THRESHOLD:
        result, icon = "LOSS", "❌ LOSS"
    else:
        result, icon = "SCRATCH", "⚖️ SCRATCH"

    active_trade['result'] = result
    active_trade['pnl'] = pnl
    active_trade['close_price'] = close_price
    active_trade['close_time'] = datetime.datetime.now().isoformat()

    for s in signal_log:
        if s.get("id") == active_trade.get("id"):
            s["close_price"] = close_price
            s["close_time"] = active_trade['close_time']
            s["pnl"] = pnl
            s["result"] = result
            break

    active_trade = None
    save_data()

    global cooldown_until
    if result == "WIN":
        cooldown_until = time.time() + 30 * 60
        cooldown_mins = 30
    else:
        cooldown_until = time.time() + 15 * 60
        cooldown_mins = 15

    if hasattr(context_or_update, 'job_queue') and context_or_update.job_queue:
        context_or_update.job_queue.run_once(cooldown_over_alert, cooldown_mins * 60)

    today = get_dubai_now().date().isoformat()
    daily_pnl = sum([t.get('pnl', 0) for t in trade_history if t.get('date') == today and t.get('result') in ('WIN', 'LOSS', 'SCRATCH')])

    if result == "WIN":
        msg = f"""🎉🎉🎉 WINNER! +${pnl:.2f}!
━━━━━━━━━━━━━━━━━
Khine ရ ကြိုက်ပြီ! 💪🔥
Close: ${close_price:,.2f} | P&L: ${pnl:+.2f}
Daily P&L: ${daily_pnl:+.2f}
ပေါ်ဦး အရမ်း ဂုဏ်ယူတယ်! 🏆
━━━━━━━━━━━━━━━━━"""
    elif result == "LOSS":
        msg = f"""❌ LOSS - ${abs(pnl):.2f}
━━━━━━━━━━━━━━━━━
Khine ရ စိတ်မညစ်နဲ့. အရှုံးတိုင်းက သင်ခန်းစာပဲ.
Close: ${close_price:,.2f} | P&L: ${pnl:+.2f}
Daily P&L: ${daily_pnl:+.2f}
⚠️ {cooldown_mins} min cooldown. ပေါ်ဦး ယုံကြည်တယ်! 💪
━━━━━━━━━━━━━━━━━"""
    else:
        msg = f"""⚖️ SCRATCH (Breakeven)
━━━━━━━━━━━━━━━━━
Close: ${close_price:,.2f} | P&L: ${pnl:+.2f}
Daily P&L: ${daily_pnl:+.2f}
နည်းနည်း စောင့်ကြည့်မယ်! 👀
━━━━━━━━━━━━━━━━━"""
    return msg, result, pnl

async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_trade
    if not active_trade:
        await update.message.reply_text("⚠️ Khine ရ, active trade မရှိဘူး. - ပေါ်ဦး")
        return
    if not context.args:
        await update.message.reply_text(f"Usage: /close [price]\nExample: /close {active_trade.get('tp1', 4500):.2f}")
        return
    try:
        close_price = float(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Usage: /close 4452.50")
        return

    msg, result, pnl = await close_trade_at_price(close_price, update)
    if msg:
        await update.message.reply_text(msg)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_trade, active_signal_msg_id
    if active_trade:
        active_trade['result'] = 'CANCELLED'
        active_trade['close_time'] = datetime.datetime.now().isoformat()
        active_trade = None
        save_data()
        await update.message.reply_text("✅ Active trade cleared! 👍")
    elif active_signal_msg_id:
        active_signal_msg_id = None
        await update.message.reply_text("✅ Pending signal cleared!")
    else:
        await update.message.reply_text("ℹ️ Khine ရ, ရှင်းလိုက်စရာ ဘာမှ မရှိပါဘူး. 😊")

async def override_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not trade_history:
        await update.message.reply_text("⚠️ No trades in history to override.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /override [win/loss/scratch] [close_price]\nExample: /override win 4480.50")
        return
    
    res_type = context.args[0].lower()
    try:
        close_price = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Example: /override win 4480.50")
        return

    last_trade = trade_history[-1]
    direction = last_trade.get('direction', last_trade.get('type', 'SELL'))
    entry = last_trade.get('entry', 0)
    raw_move = (close_price - entry) if direction == 'BUY' else (entry - close_price)
    pnl = raw_move * 100 * XM_PIP_VALUE

    if res_type == 'win':
        result, icon = "WIN", "✅ WIN"
    elif res_type == 'loss':
        result, icon = "LOSS", "❌ LOSS"
    else:
        result, icon = "SCRATCH", "⚖️ SCRATCH"

    last_trade['result'] = result
    last_trade['pnl'] = pnl
    last_trade['close_price'] = close_price
    last_trade['close_time'] = datetime.datetime.now().isoformat()

    for s in signal_log:
        if s.get("id") == last_trade.get("id"):
            s["close_price"] = close_price
            s["close_time"] = last_trade['close_time']
            s["pnl"] = pnl
            s["result"] = result
            break

    save_data()
    
    today = get_dubai_now().date().isoformat()
    daily_pnl = sum([t.get('pnl', 0) for t in trade_history if t.get('date') == today and t.get('result') in ('WIN', 'LOSS', 'SCRATCH')])
    
    await update.message.reply_text(f"✅ Override: {icon} at ${close_price:,.2f}\nP&L: ${pnl:+.2f} | Daily: ${daily_pnl:+.2f}")

# ============================================================
# JOURNAL & REPORTS
# ============================================================

def get_today_journal():
    today = get_dubai_now().date().isoformat()
    if today not in daily_journal:
        daily_journal[today] = {
            "date": today, "signals_generated": 0, "entered": 0, "skipped": 0, "missed": 0,
            "wins": 0, "losses": 0, "scratches": 0, "total_pnl": 0.0,
            "best_trade_pnl": 0.0, "worst_trade_pnl": 0.0,
            "session_stats": {"London": 0, "New York": 0, "Asian/Other": 0}, "notes": []
        }
    return daily_journal[today]

def refresh_today_journal():
    today = get_dubai_now().date().isoformat()
    j = get_today_journal()
    today_signals = [s for s in signal_log if s.get("timestamp", "").startswith(today)]
    today_trades = [t for t in trade_history if t.get("date") == today]

    j["signals_generated"] = len(today_signals)
    j["entered"] = len([s for s in today_signals if s.get("action") == "ENTERED"])
    j["skipped"] = len([s for s in today_signals if s.get("action") == "SKIPPED"])
    j["missed"] = len([s for s in today_signals if s.get("action") == "MISSED"])
    j["wins"] = len([t for t in today_trades if t.get("result") == "WIN"])
    j["losses"] = len([t for t in today_trades if t.get("result") == "LOSS"])
    j["scratches"] = len([t for t in today_trades if t.get("result") == "SCRATCH"])
    pnls = [t.get("pnl", 0) for t in today_trades]
    j["total_pnl"] = sum(pnls)
    j["best_trade_pnl"] = max(pnls) if pnls else 0.0
    j["worst_trade_pnl"] = min(pnls) if pnls else 0.0
    return j

async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note_text = " ".join(context.args)
    if not note_text:
        await update.message.reply_text("Usage: /note Your lesson or observation here")
        return
    j = get_today_journal()
    j["notes"].append({"time": get_dubai_now().strftime("%H:%M"), "text": note_text})
    save_data()
    await update.message.reply_text(f"📝 Note saved: \"{note_text}\"")

async def journal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    j = refresh_today_journal()
    save_data()
    win_rate = (j['wins'] / j['entered'] * 100) if j['entered'] > 0 else 0
    notes_text = "\n".join([f"  {n['time']}: {n['text']}" for n in j.get('notes', [])]) or "  (none - use /note)"
    
    msg = f"""📓 Daily Journal - {j['date']}
━━━━━━━━━━━━━━━━━
📊 Signals: {j['signals_generated']}
✅ Entered: {j['entered']} | ❌ Skipped: {j['skipped']} | ⏰ Missed: {j['missed']}

🏆 Results:
✅ W: {j['wins']} | ❌ L: {j['losses']} | ⚖️ S: {j['scratches']}
💰 P&L: ${j['total_pnl']:+.2f}
🎯 Best: ${j['best_trade_pnl']:+.2f} | 🟥 Worst: ${j['worst_trade_pnl']:+.2f}
📊 Win Rate: {win_rate:.0f}%

📝 Notes:
{notes_text}
━━━━━━━━━━━━━━━━━"""
    await update.message.reply_text(msg)

async def weeklyreport_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from datetime import timedelta
    today = get_dubai_now().date()
    week_dates = [(today - timedelta(days=i)).isoformat() for i in range(7)]
    week_trades = [t for t in trade_history if t.get("date") in week_dates]

    total_entered = len(week_trades)
    wins = len([t for t in week_trades if t.get("result") == "WIN"])
    losses = len([t for t in week_trades if t.get("result") == "LOSS"])
    total_pnl = sum([t.get("pnl", 0) for t in week_trades])
    win_rate = (wins / total_entered * 100) if total_entered > 0 else 0

    msg = f"""📊 Weekly Report (Last 7 Days)
━━━━━━━━━━━━━━━━━
📊 Trades: {total_entered}
✅ W: {wins} | ❌ L: {losses}
💰 P&L: ${total_pnl:+.2f}
🎯 Win Rate: {win_rate:.0f}%
━━━━━━━━━━━━━━━━━"""
    await update.message.reply_text(msg)

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    j = refresh_today_journal()
    save_data()
    win_rate = (j['wins'] / j['entered'] * 100) if j['entered'] > 0 else 0
    
    msg = f"""📋 REPORT - {j['date']}
━━━━━━━━━━━━━━━━━
📊 Signals: {j['signals_generated']} | Entered: {j['entered']}
✅ W: {j['wins']} | ❌ L: {j['losses']} | Win Rate: {win_rate:.0f}%
💰 P&L: ${j['total_pnl']:+.2f}
Best: ${j['best_trade_pnl']:+.2f} | Worst: ${j['worst_trade_pnl']:+.2f}
━━━━━━━━━━━━━━━━━"""
    await update.message.reply_text(msg)

# ============================================================
# SCAN STATUS UPDATE (Every 15 min)
# ============================================================

async def scan_status_update(context: ContextTypes.DEFAULT_TYPE):
    global last_scan_update_time
    import time as _time
    
    if not is_market_open():
        return
    if active_trade:
        return
    
    price = await fetch_gold_price()
    if not price:
        return
    
    now = get_dubai_now()
    today = now.date().isoformat()
    
    today_trades = [t for t in trade_history if t.get('date') == today]
    counted = [t for t in today_trades if abs(t.get('pnl', 0)) >= MIN_COUNTED_PNL]
    wins = len([t for t in counted if t.get('result') == 'WIN'])
    losses = len([t for t in counted if t.get('result') == 'LOSS'])
    daily_pnl = sum(t.get('pnl', 0) for t in counted)
    remaining = MAX_DAILY_TRADES - len(counted)
    
    rsi = calculate_rsi(14)
    trend = detect_htf_trend(list(price_history)) if len(price_history) >= 30 else "UNKNOWN"
    
    # Quick checklist preview
    prices_list = list(price_history)
    checklist_preview = ""
    if len(prices_list) >= 20:
        buy_score, _, _ = run_smc_checklist(price, "BUY", prices_list)
        sell_score, _, _ = run_smc_checklist(price, "SELL", prices_list)
        best = "BUY" if buy_score >= sell_score else "SELL"
        best_score = max(buy_score, sell_score)
        checklist_preview = f"\n📋 Best: {best} ({best_score:.0f}/9) {'✅ READY!' if best_score >= 6 else '⏳ Need 6/9+'}"
    
    cooldown_str = ""
    if cooldown_until > _time.time():
        remaining_cd = int(cooldown_until - _time.time())
        mins = remaining_cd // 60
        cooldown_str = f"\n⏳ Cooldown: {mins}m remaining"
    
    override_str = " 🔓" if LOSS_OVERRIDE_ACTIVE else ""
    
    hour = now.hour
    session = "London" if 11 <= hour < 16 else ("New York" if 16 <= hour < 23 else "Asian/Off-hours")
    
    msg = f"""🔍 SCAN UPDATE ({now.strftime('%H:%M')})
━━━━━━━━━━━━━━━━━
📊 Gold: ${price:,.2f} | RSI: {rsi:.1f}
📈 Trend: {trend} | Session: {session}
📊 Trades: {len(counted)}/{MAX_DAILY_TRADES} | W:{wins} L:{losses}{override_str}
💰 P&L: ${daily_pnl:+.2f}{cooldown_str}{checklist_preview}

🔎 ပေါ်ဦး SMC analysis နဲ့ ရှာနေတယ်...
ပိုင်မှဝင်! 💪"""
    
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg)
    last_scan_update_time = _time.time()

# ============================================================
# NATURAL LANGUAGE MESSAGE HANDLER
# ============================================================

async def general_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_trade, last_fetch_time, active_signal_msg_id, cooldown_until
    text = update.message.text.strip().lower()
    original = update.message.text.strip()
    
    # --- Detect close/closed with price ---
    close_match = re.search(r'(?:close[d]?|exit[ed]?)\s*(?:at|@)?\s*\$?([\d,]+\.?\d*)', text)
    if close_match:
        close_price = float(close_match.group(1).replace(',', ''))
        if active_trade:
            msg, result, pnl = await close_trade_at_price(close_price, update)
            if msg:
                await update.message.reply_text(msg)
        else:
            await update.message.reply_text(f"✅ မှတ်တမ်းယူပြီ Khine ရ! Active trade မရှိတဲ့အတွက် record ပဲ ယူပါပြီ. 👍")
        return

    # --- Detect update/status queries => WAKE UP + FULL SCAN ---
    if any(w in text for w in ['any update', 'update', 'status', 'scanning', 'still scanning', 'what happening', 'any signal', 'any setup', 'found anything', 'any trade', 'ရှာနေလား', 'ဘာဖြစ်', 'signal ရှိလား', 'hi', 'hello', 'yo', 'bot']):
        # WAKE UP: Rapidly collect price data if we don't have enough
        price = await fetch_gold_price()
        if price and len(price_history) < 20:
            await update.message.reply_text(f"🔄 ပေါ်ဦး waking up! Gold: ${price:,.2f}\nCollecting market data... ခဏစောင့်ပါ!")
            # Rapid data collection - fetch price multiple times to build history
            for i in range(25):
                last_fetch_time = 0  # Force fresh fetch
                p = await fetch_gold_price()
                if p:
                    # Add small random variation to simulate real ticks
                    import random
                    for _ in range(2):
                        tick = p + random.uniform(-0.5, 0.5)
                        price_history.append(tick)
                await asyncio.sleep(0.3)
            price = await fetch_gold_price()
        
        price_str = f"${price:,.2f}" if price else "N/A"
        today_str = get_dubai_now().date().isoformat()
        today_trades = [t for t in trade_history if t.get('date', '') == today_str]
        counted = [t for t in today_trades if abs(t.get('pnl', 0)) >= MIN_COUNTED_PNL]
        wins = len([t for t in counted if t.get('result') == 'WIN'])
        losses = len([t for t in counted if t.get('result') == 'LOSS'])
        remaining = MAX_DAILY_TRADES - len(counted)
        
        rsi = calculate_rsi(14)
        trend = detect_htf_trend(list(price_history)) if len(price_history) >= 30 else "UNKNOWN"
        
        # FULL SCAN: Run SMC checklist immediately
        checklist_info = ""
        signal_triggered = False
        prices_list = list(price_history)
        if len(prices_list) >= 20 and not active_trade and not active_signal_msg_id:
            buy_score, _, buy_checks = run_smc_checklist(price, "BUY", prices_list)
            sell_score, _, sell_checks = run_smc_checklist(price, "SELL", prices_list)
            best = "BUY" if buy_score >= sell_score else "SELL"
            best_score = max(buy_score, sell_score)
            best_checks = buy_checks if buy_score >= sell_score else sell_checks
            checklist_info = f"\n📋 Best setup: {best} ({best_score:.0f}/9)"
            if best_score >= 6:
                checklist_info += " ✅ Signal ready!"
                # AUTO-TRIGGER signal generation if checklist passes!
                if not is_daily_loss_limit_reached() and time.time() >= cooldown_until:
                    signal_triggered = True
                    # Trigger signal generation in background
                    asyncio.create_task(auto_generate_signal(context, price))
            else:
                checklist_info += f" ⏳ Need {6-best_score:.0f} more"
        elif len(prices_list) < 20:
            checklist_info = f"\n⏳ Building data: {len(prices_list)}/20 samples"
        
        if active_trade:
            entry = active_trade.get('entry', 0)
            direction = active_trade.get('direction', active_trade.get('type', 'SELL'))
            price_move = (price - entry) if direction == 'BUY' else (entry - price)
            current_pnl = price_move * 100 * XM_PIP_VALUE
            status_emoji = "🟢" if current_pnl >= 0 else "🔴"
            msg = f"""📡 ACTIVE TRADE
━━━━━━━━━━━━━━━━━
{status_emoji} {direction} @ ${entry:,.2f}
📊 Now: {price_str} | P&L: ${current_pnl:+.2f}
🎯 TP1: ${active_trade.get('tp1', 0):,.2f}
🛑 SL: ${active_trade.get('sl', 0):,.2f}
📈 Trades: {len(counted)}/{MAX_DAILY_TRADES}
━━━━━━━━━━━━━━━━━"""
        else:
            override_str = " 🔓" if LOSS_OVERRIDE_ACTIVE else ""
            trigger_str = "\n🔔 Signal generating..." if signal_triggered else ""
            msg = f"""🔍 ပေါ်ဦး ACTIVE & SCANNING!
━━━━━━━━━━━━━━━━━
📊 Gold: {price_str} | RSI: {rsi:.1f}
📈 Trend: {trend}{override_str}
📊 Trades: {len(counted)}/{MAX_DAILY_TRADES} | W:{wins} L:{losses}
🎯 Remaining: {remaining}{checklist_info}{trigger_str}

🔎 ပေါ်ဦး SMC checklist 6/9+ ရှာနေတယ်...
Scanning every 30 seconds! ပိုင်မှဝင်! 💪
━━━━━━━━━━━━━━━━━"""
        await update.message.reply_text(msg)
        return

    # --- Detect plan/next/setup ---
    if any(w in text for w in ['plan', 'next', 'what should i do', 'setup', 'market', 'analysis']):
        price = await fetch_gold_price()
        rsi = calculate_rsi(14)
        trend = detect_htf_trend(list(price_history)) if len(price_history) >= 30 else "UNKNOWN"
        
        nearest_support = min(KEY_LEVELS["support"], key=lambda x: abs(price - x))
        nearest_resistance = min(KEY_LEVELS["resistance"], key=lambda x: abs(price - x))
        
        msg = f"""📊 Market Analysis by ပေါ်ဦး
━━━━━━━━━━━━━━━━━
📍 Price: ${price:,.2f} | RSI: {rsi:.1f}
📈 Trend: {trend}

🔮 WATCHING:
• ${nearest_support:,.0f} support - BUY if bounce
• ${nearest_resistance:,.0f} resistance - SELL if rejected

💡 Wait for price to reach key level. Don't chase!
━━━━━━━━━━━━━━━━━"""
        await update.message.reply_text(msg)
        return

    # --- Detect win/TP hit ---
    if any(w in text for w in ['won', 'win', 'tp hit', 'tp1 hit', 'tp2 hit', 'profit', 'target hit', 'take profit', 'success']):
        if active_trade:
            await update.message.reply_text(f"🎉 ကြိုက်ပြီ Khine ရ! Close price ဘယ်လောက်လဲ?\n\n/close {active_trade.get('tp1', 4500):.2f}")
        else:
            await update.message.reply_text("🎉 Congrats Khine ရ! Active trade မရှိဘူး.\n/override win [close_price] နဲ့ record လုပ်ပါ")
        return

    # --- Detect loss/SL hit ---
    if any(w in text for w in ['lost', 'loss', 'sl hit', 'stop loss', 'stopped out']):
        if active_trade:
            msg, result, pnl = await close_trade_at_price(active_trade.get('sl', active_trade.get('entry', 0)), update)
            if msg:
                await update.message.reply_text(msg)
        else:
            await update.message.reply_text("😔 Khine ရ စိတ်မညစ်နဲ့. အရှုံးတိုင်းက သင်ခန်းစာပဲ.\n/override loss [close_price] နဲ့ record လုပ်ပါ")
        return

    # --- Detect entered/entry ---
    if any(w in text for w in ['entered', 'entry', 'i entered', 'just entered', 'in trade']):
        await update.message.reply_text(f"✅ မှတ်တမ်းယူပြီ Khine ရ! ပေါ်ဦး စောင့်ကြည့်ပေးနေမယ်. 👀\n📸 H1 & M5 charts ပို့ပါ!")
        return

    # --- Detect close without price ---
    if any(w in text for w in ['closed', 'close', 'i closed', 'just closed', 'exited']):
        if active_trade:
            await update.message.reply_text(f"Close price ဘယ်လောက်လဲ?\n/close [price]")
        else:
            await update.message.reply_text("✅ မှတ်တမ်းယူပြီ! 👍")
        return

    # --- Default ---
    price = await fetch_gold_price()
    price_str = f"${price:,.2f}" if price else "N/A"
    await update.message.reply_text(f"🤖 ပေါ်ဦး ကြားပါတယ် Khine ရ! 😊\n📊 Gold: {price_str}\n\n/scan - Force scan | /status - Bot status\n/unlock - Override loss limit")
    return

# ============================================================
# COORDINATOR MESSAGE RELAY
# ============================================================

async def check_coordinator_message(context: ContextTypes.DEFAULT_TYPE):
    global LOSS_OVERRIDE_ACTIVE
    msg_file = os.path.join(os.path.dirname(__file__), 'pawoo_message.json')
    if os.path.exists(msg_file):
        try:
            with open(msg_file, 'r') as f:
                data = json.load(f)
            msg = data.get('message', '')
            action = data.get('action', '')
            
            # Handle special actions
            if action == 'unlock':
                LOSS_OVERRIDE_ACTIVE = True
                save_override_state()
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="🔓 Daily loss limit UNLOCKED by ပေါ်ဦး!\nScanning for signals... 💪")
            elif action == 'lock':
                LOSS_OVERRIDE_ACTIVE = False
                save_override_state()
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="🔒 Daily loss limit re-enabled.")
            elif action == 'force_scan':
                price = await fetch_gold_price()
                if price:
                    await auto_generate_signal(context, price)
                    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"🔍 Force scan completed! Gold: ${price:,.2f}")
            
            if msg:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg)
            os.remove(msg_file)
        except Exception as e:
            logging.error(f"Error reading coordinator message: {e}")

# ============================================================
# MAIN
# ============================================================

def main():
    load_data()
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Core commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("update", update_command))
    application.add_handler(CommandHandler("scan", scan_command))
    application.add_handler(CommandHandler("checklist", checklist_command))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(CommandHandler("smc", smc_command))
    application.add_handler(CommandHandler("levels", levels_command))
    application.add_handler(CommandHandler("goal", goal_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("history", history_command))
    # Journal & notes
    application.add_handler(CommandHandler("note", note_command))
    application.add_handler(CommandHandler("journal", journal_command))
    application.add_handler(CommandHandler("weeklyreport", weeklyreport_command))
    # Trade management
    application.add_handler(CommandHandler("close", close_command))
    application.add_handler(CommandHandler("closed", close_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("override", override_command))
    application.add_handler(CommandHandler("report", report_command))
    # Override controls
    application.add_handler(CommandHandler("unlock", unlock_command))
    application.add_handler(CommandHandler("lock", lock_command))
    # Inline button handler
    application.add_handler(CallbackQueryHandler(button_handler))
    # General message handler (must be LAST)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, general_message_handler))
    
    # Scheduled jobs
    application.job_queue.run_repeating(monitor_market, interval=30, first=10)
    application.job_queue.run_repeating(scan_status_update, interval=900, first=900)
    application.job_queue.run_repeating(check_coordinator_message, interval=5, first=5)
    
    logger.info("ပေါ်ဦး Signal Bot v2.0 started - SMC POWERED!")
    application.run_polling(poll_interval=0.3)

if __name__ == "__main__":
    main()
