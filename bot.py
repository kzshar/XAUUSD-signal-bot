#!/usr/bin/env python3
"""
PawOo Gold Signal Bot v4.1
==========================
XAU/USD Scalping Signal Bot - EMA + SMC/ICT Hybrid Strategy
Clean rebuild - Production ready for Railway.app

Strategy: EMA(9/21) crossover + SMC/ICT confirmation + RSI filter
Data: Yahoo Finance GC=F candles + gold-api.com spot price
Broker: XM Micro (GOLDm#) 0.5 lot
"""

import asyncio
import datetime
import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

import pytz
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8653316966:AAGdqc_ip9cZwual3AONsMzKKknhJW3jrKg")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "5948621771"))
DUBAI_TZ = pytz.timezone("Asia/Dubai")
VERSION = "4.1"

# Trading Parameters
LOT_SIZE = 0.5
PIP_VALUE = 0.005  # XM Micro 1 point = $0.005
SL_DOLLARS = 4.0
TP1_DOLLARS = 10.0
TP2_DOLLARS = 15.0
MAX_DAILY_TRADES = 5
DAILY_LOSS_LIMIT = -10.0
COOLDOWN_SECONDS = 600  # 10 minutes between signals
SCAN_INTERVAL = 30

# Strategy Parameters
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 50   # H1 trend EMA
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
MIN_CHECKLIST_SCORE = 5  # Minimum 5/9 for signal (was 6, now easier)

# File paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_HISTORY_FILE = os.path.join(BASE_DIR, "trade_history.json")
SIGNAL_LOG_FILE = os.path.join(BASE_DIR, "signal_log.json")
BOT_STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("PawOoBot")

# ═══════════════════════════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════════════════════════
candles_m5: List[Dict] = []
candles_h1: List[Dict] = []
cached_price: Optional[float] = None
last_price_fetch: float = 0
last_candle_fetch: float = 0
active_trade: Optional[Dict] = None
active_signal_msg_id: Optional[int] = None
trade_history: List[Dict] = []
signal_log: List[Dict] = []
cooldown_until: float = 0
signal_count_today: int = 0
last_reset_date: str = ""
last_ema_cross: Optional[str] = None  # Track last EMA cross direction

# ═══════════════════════════════════════════════════════════════
# TIME & MARKET FUNCTIONS
# ═══════════════════════════════════════════════════════════════
def now_dubai() -> datetime.datetime:
    return datetime.datetime.now(DUBAI_TZ)


def is_market_open() -> bool:
    """XM GOLDm# market hours (Dubai time).
    Daily maintenance: 00:50 - 03:10
    Weekend: Saturday 00:50 - Sunday 23:30
    """
    n = now_dubai()
    wd, h, m = n.weekday(), n.hour, n.minute
    if wd == 5:  # Saturday
        return h == 0 and m < 50
    if wd == 6:  # Sunday
        return h >= 23 and m >= 30
    if h == 0 and m >= 50:
        return False
    if h in (1, 2):
        return False
    if h == 3 and m < 10:
        return False
    return True


def get_session() -> str:
    n = now_dubai()
    h, m = n.hour, n.minute
    t = h * 60 + m
    if t >= 990 or t < 50:
        return "New York"
    if 660 <= t < 990:
        return "London"
    if 990 <= t < 1200:
        return "London/NY"
    return "Asian"


def is_active_session() -> bool:
    return get_session() in ("London", "New York", "London/NY")


# ═══════════════════════════════════════════════════════════════
# DATA FETCHING
# ═══════════════════════════════════════════════════════════════
def fetch_spot_price() -> Optional[float]:
    global cached_price, last_price_fetch
    now = time.time()
    if now - last_price_fetch < 20 and cached_price:
        return cached_price
    try:
        r = requests.get("https://api.gold-api.com/price/XAU", timeout=8)
        if r.status_code == 200:
            price = float(r.json()["price"])
            cached_price = price
            last_price_fetch = now
            return price
    except Exception as e:
        logger.warning(f"gold-api failed: {e}")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d",
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            price = float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
            cached_price = price
            last_price_fetch = now
            return price
    except Exception as e:
        logger.warning(f"Yahoo fallback failed: {e}")
    return cached_price


def fetch_candles() -> bool:
    global candles_m5, candles_h1, last_candle_fetch
    now = time.time()
    if now - last_candle_fetch < 60:
        return True
    headers = {"User-Agent": "Mozilla/5.0"}
    success = False

    # M5 candles (5d)
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=5m&range=5d",
            headers=headers, timeout=10
        )
        if r.status_code == 200:
            data = r.json()["chart"]["result"][0]
            ts = data["timestamp"]
            q = data["indicators"]["quote"][0]
            new = []
            for i in range(len(ts)):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if any(v is None for v in (o, h, l, c)):
                    continue
                if o == h == l == c:
                    continue
                new.append({"time": ts[i], "open": float(o), "high": float(h),
                            "low": float(l), "close": float(c)})
            if new:
                candles_m5 = new
                success = True
                logger.info(f"M5: {len(candles_m5)} candles")
    except Exception as e:
        logger.error(f"M5 fetch error: {e}")

    # H1 candles (1mo)
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1h&range=1mo",
            headers=headers, timeout=10
        )
        if r.status_code == 200:
            data = r.json()["chart"]["result"][0]
            ts = data["timestamp"]
            q = data["indicators"]["quote"][0]
            new = []
            for i in range(len(ts)):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if any(v is None for v in (o, h, l, c)):
                    continue
                if o == h == l == c:
                    continue
                new.append({"time": ts[i], "open": float(o), "high": float(h),
                            "low": float(l), "close": float(c)})
            if new:
                candles_h1 = new
                logger.info(f"H1: {len(candles_h1)} candles")
    except Exception as e:
        logger.error(f"H1 fetch error: {e}")

    if success:
        last_candle_fetch = now
    return success


# ═══════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ═══════════════════════════════════════════════════════════════
def calculate_ema(candles: List[Dict], period: int) -> List[float]:
    """Calculate EMA for given period. Returns list of EMA values."""
    if len(candles) < period:
        return []
    closes = [c["close"] for c in candles]
    multiplier = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]  # First EMA = SMA
    for i in range(period, len(closes)):
        ema.append(closes[i] * multiplier + ema[-1] * (1 - multiplier))
    return ema


def calculate_rsi(candles: List[Dict], period: int = 14) -> Optional[float]:
    """RSI using Wilder's smoothed method (matches MT5/TradingView)."""
    if len(candles) < period + 1:
        return None
    closes = [c["close"] for c in candles]
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[:period]]
    losses = [-d if d < 0 else 0 for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period, len(deltas)):
        d = deltas[i]
        avg_gain = (avg_gain * (period - 1) + (d if d > 0 else 0)) / period
        avg_loss = (avg_loss * (period - 1) + (-d if d < 0 else 0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def get_ema_signal(candles: List[Dict]) -> Tuple[Optional[str], float, float]:
    """Check EMA(9/21) crossover signal.
    
    Returns: (direction or None, ema_fast, ema_slow)
    - BUY: EMA9 crosses above EMA21 (golden cross)
    - SELL: EMA9 crosses below EMA21 (death cross)
    """
    if len(candles) < EMA_SLOW + 2:
        return None, 0, 0
    
    ema_fast = calculate_ema(candles, EMA_FAST)
    ema_slow = calculate_ema(candles, EMA_SLOW)
    
    if len(ema_fast) < 2 or len(ema_slow) < 2:
        return None, 0, 0
    
    # Align lengths (EMA_SLOW starts later)
    offset = EMA_SLOW - EMA_FAST
    if offset > 0 and len(ema_fast) > offset:
        ema_fast = ema_fast[offset:]
    
    if len(ema_fast) < 2 or len(ema_slow) < 2:
        return None, 0, 0
    
    curr_fast = ema_fast[-1]
    curr_slow = ema_slow[-1]
    prev_fast = ema_fast[-2]
    prev_slow = ema_slow[-2]
    
    direction = None
    # Golden cross: fast crosses above slow
    if prev_fast <= prev_slow and curr_fast > curr_slow:
        direction = "BUY"
    # Death cross: fast crosses below slow
    elif prev_fast >= prev_slow and curr_fast < curr_slow:
        direction = "SELL"
    # Already crossed - check if still valid (within last 3 candles)
    elif curr_fast > curr_slow:
        # Check if cross happened recently (within last 5 candles)
        for i in range(max(0, len(ema_fast)-5), len(ema_fast)-1):
            j = i
            if j < len(ema_slow) and j > 0:
                if ema_fast[j-1] <= ema_slow[j-1] and ema_fast[j] > ema_slow[j]:
                    direction = "BUY"
                    break
    elif curr_fast < curr_slow:
        for i in range(max(0, len(ema_fast)-5), len(ema_fast)-1):
            j = i
            if j < len(ema_slow) and j > 0:
                if ema_fast[j-1] >= ema_slow[j-1] and ema_fast[j] < ema_slow[j]:
                    direction = "SELL"
                    break
    
    return direction, round(curr_fast, 2), round(curr_slow, 2)


def get_ema_trend_bias(candles: List[Dict]) -> str:
    """Get trend bias from EMA positions.
    
    - Price above both EMAs + EMA9 > EMA21 = BULLISH
    - Price below both EMAs + EMA9 < EMA21 = BEARISH
    - Mixed = RANGING
    """
    if len(candles) < EMA_SLOW + 1:
        return "UNKNOWN"
    
    ema_fast = calculate_ema(candles, EMA_FAST)
    ema_slow = calculate_ema(candles, EMA_SLOW)
    
    if not ema_fast or not ema_slow:
        return "UNKNOWN"
    
    price = candles[-1]["close"]
    ef = ema_fast[-1]
    es = ema_slow[-1]
    
    if price > ef > es:
        return "BULLISH"
    elif price < ef < es:
        return "BEARISH"
    return "RANGING"


def detect_htf_trend(candles: List[Dict]) -> str:
    """H1 trend using EMA50 + swing structure."""
    if len(candles) < EMA_TREND + 1:
        return "UNKNOWN"
    
    ema50 = calculate_ema(candles, EMA_TREND)
    if not ema50:
        return "UNKNOWN"
    
    price = candles[-1]["close"]
    ema_val = ema50[-1]
    
    # Also check recent swing structure
    recent = candles[-20:]
    swing_highs = []
    swing_lows = []
    for i in range(2, len(recent) - 2):
        if recent[i]["high"] > recent[i-1]["high"] and recent[i]["high"] > recent[i+1]["high"]:
            swing_highs.append(recent[i]["high"])
        if recent[i]["low"] < recent[i-1]["low"] and recent[i]["low"] < recent[i+1]["low"]:
            swing_lows.append(recent[i]["low"])
    
    above_ema = price > ema_val
    
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1] > swing_highs[-2]
        hl = swing_lows[-1] > swing_lows[-2]
        ll = swing_lows[-1] < swing_lows[-2]
        lh = swing_highs[-1] < swing_highs[-2]
        
        if above_ema and (hh or hl):
            return "BULLISH"
        elif not above_ema and (ll or lh):
            return "BEARISH"
    
    if above_ema:
        return "BULLISH"
    else:
        return "BEARISH"


# ═══════════════════════════════════════════════════════════════
# SMC/ICT ANALYSIS
# ═══════════════════════════════════════════════════════════════
def detect_bos(candles: List[Dict], direction: str) -> Tuple[bool, Optional[float]]:
    """Break of Structure."""
    if len(candles) < 20:
        return False, None
    recent = candles[-20:]
    current_close = recent[-1]["close"]
    swing_highs, swing_lows = [], []
    for i in range(2, len(recent) - 2):
        if recent[i]["high"] > recent[i-1]["high"] and recent[i]["high"] > recent[i+1]["high"]:
            swing_highs.append(recent[i]["high"])
        if recent[i]["low"] < recent[i-1]["low"] and recent[i]["low"] < recent[i+1]["low"]:
            swing_lows.append(recent[i]["low"])
    if direction == "BUY" and swing_highs:
        if current_close > swing_highs[-1]:
            return True, swing_highs[-1]
    elif direction == "SELL" and swing_lows:
        if current_close < swing_lows[-1]:
            return True, swing_lows[-1]
    return False, None


def detect_order_block(candles: List[Dict], direction: str) -> Tuple[bool, Optional[float], Optional[float]]:
    """Order Block detection."""
    if len(candles) < 15:
        return False, None, None
    recent = candles[-15:]
    price = recent[-1]["close"]
    for i in range(len(recent) - 3, 1, -1):
        c = recent[i]
        body = abs(c["close"] - c["open"])
        if direction == "BUY" and c["close"] < c["open"]:
            next_move = recent[i+1]["close"] - recent[i+1]["open"]
            if next_move > body * 1.2:
                ob_low, ob_high = c["low"], c["open"]
                if ob_low <= price <= ob_high * 1.003:
                    return True, ob_low, ob_high
        elif direction == "SELL" and c["close"] > c["open"]:
            next_move = recent[i+1]["open"] - recent[i+1]["close"]
            if next_move > body * 1.2:
                ob_low, ob_high = c["close"], c["high"]
                if ob_low * 0.997 <= price <= ob_high:
                    return True, ob_low, ob_high
    return False, None, None


def detect_fvg(candles: List[Dict], direction: str) -> bool:
    """Fair Value Gap."""
    if len(candles) < 10:
        return False
    recent = candles[-10:]
    for i in range(1, len(recent) - 1):
        if direction == "BUY":
            gap = recent[i+1]["low"] - recent[i-1]["high"]
            if gap > 0.3:
                return True
        else:
            gap = recent[i-1]["low"] - recent[i+1]["high"]
            if gap > 0.3:
                return True
    return False


def detect_liquidity_sweep(candles: List[Dict], direction: str) -> bool:
    """Liquidity sweep / stop hunt."""
    if len(candles) < 15:
        return False
    recent = candles[-15:]
    last = recent[-1]
    if direction == "BUY":
        lows = sorted([c["low"] for c in recent[:-1]])[:3]
        if lows and last["low"] <= min(lows) and last["close"] > min(lows):
            return True
    else:
        highs = sorted([c["high"] for c in recent[:-1]], reverse=True)[:3]
        if highs and last["high"] >= max(highs) and last["close"] < max(highs):
            return True
    return False


def detect_displacement(candles: List[Dict], direction: str) -> Tuple[bool, float]:
    """Displacement / momentum candle."""
    if len(candles) < 10:
        return False, 0
    recent = candles[-10:]
    bodies = [abs(c["close"] - c["open"]) for c in recent[:-1]]
    avg_body = sum(bodies) / len(bodies) if bodies else 1
    last = recent[-1]
    last_body = abs(last["close"] - last["open"])
    if avg_body == 0:
        return False, 0
    mult = last_body / avg_body
    if direction == "BUY" and last["close"] > last["open"] and mult >= 1.5:
        return True, round(mult, 1)
    elif direction == "SELL" and last["close"] < last["open"] and mult >= 1.5:
        return True, round(mult, 1)
    return False, round(mult, 1)


def is_premium_discount(candles: List[Dict], price: float, direction: str) -> bool:
    """Premium/Discount zone check."""
    if len(candles) < 50:
        return False
    recent = candles[-50:]
    highest = max(c["high"] for c in recent)
    lowest = min(c["low"] for c in recent)
    if highest == lowest:
        return False
    mid = (highest + lowest) / 2
    return (price < mid) if direction == "BUY" else (price > mid)


def is_near_key_level(candles: List[Dict], price: float) -> Tuple[bool, Optional[str]]:
    """Key support/resistance level check."""
    if len(candles) < 50:
        return False, None
    recent = candles[-50:]
    levels = []
    for i in range(2, len(recent) - 2):
        if recent[i]["high"] > recent[i-1]["high"] and recent[i]["high"] > recent[i+1]["high"]:
            levels.append(recent[i]["high"])
        if recent[i]["low"] < recent[i-1]["low"] and recent[i]["low"] < recent[i+1]["low"]:
            levels.append(recent[i]["low"])
    for level in levels:
        if abs(price - level) < 4.0:
            return True, f"${level:,.2f}"
    return False, None


# ═══════════════════════════════════════════════════════════════
# SIGNAL CHECKLIST
# ═══════════════════════════════════════════════════════════════
def run_checklist(price: float, direction: str) -> Tuple[int, List[Dict]]:
    """Run full checklist (9 items). Returns (score, items)."""
    checks = []
    score = 0

    # 1. HTF Trend (H1)
    htf = detect_htf_trend(candles_h1)
    ok = (direction == "BUY" and htf == "BULLISH") or (direction == "SELL" and htf == "BEARISH")
    checks.append({"name": "HTF Trend", "pass": ok,
                    "detail": f"{htf} - {'aligned' if ok else 'NOT aligned'}"})
    if ok: score += 1

    # 2. EMA Trend Bias (M5)
    bias = get_ema_trend_bias(candles_m5)
    ok = (direction == "BUY" and bias == "BULLISH") or (direction == "SELL" and bias == "BEARISH")
    checks.append({"name": "EMA Trend", "pass": ok,
                    "detail": f"{bias} - {'aligned' if ok else 'NOT aligned'}"})
    if ok: score += 1

    # 3. EMA Crossover
    ema_dir, ef, es = get_ema_signal(candles_m5)
    ok = ema_dir == direction
    checks.append({"name": "EMA Cross", "pass": ok,
                    "detail": f"EMA9={ef} / EMA21={es} {'✓ ' + direction if ok else 'No cross'}"})
    if ok: score += 1

    # 4. BOS
    bos_ok, bos_lv = detect_bos(candles_m5, direction)
    checks.append({"name": "BOS", "pass": bos_ok,
                    "detail": f"Confirmed at ${bos_lv:,.2f}" if bos_ok else "Not confirmed"})
    if bos_ok: score += 1

    # 5. Order Block
    ob_ok, ob_lo, ob_hi = detect_order_block(candles_m5, direction)
    checks.append({"name": "Order Block", "pass": ob_ok,
                    "detail": f"${ob_lo:,.2f}-${ob_hi:,.2f}" if ob_ok else "Not in OB"})
    if ob_ok: score += 1

    # 6. Liquidity Sweep
    liq = detect_liquidity_sweep(candles_m5, direction)
    checks.append({"name": "Liq Sweep", "pass": liq,
                    "detail": "Detected" if liq else "Not detected"})
    if liq: score += 1

    # 7. Displacement
    disp_ok, disp_m = detect_displacement(candles_m5, direction)
    checks.append({"name": "Displacement", "pass": disp_ok,
                    "detail": f"Strong ({disp_m}x)" if disp_ok else f"Weak ({disp_m}x)"})
    if disp_ok: score += 1

    # 8. RSI filter (not extreme = good for entry direction)
    rsi = calculate_rsi(candles_m5, RSI_PERIOD)
    rsi_ok = False
    if rsi is not None:
        if direction == "BUY" and rsi < 55:  # Not overbought
            rsi_ok = True
        elif direction == "SELL" and rsi > 45:  # Not oversold
            rsi_ok = True
    rsi_str = f"{rsi}" if rsi else "N/A"
    checks.append({"name": "RSI", "pass": rsi_ok,
                    "detail": f"{rsi_str} - {'OK' if rsi_ok else 'Against direction'}"})
    if rsi_ok: score += 1

    # 9. R:R ratio
    rr = TP1_DOLLARS / SL_DOLLARS
    ok = rr >= 2.0
    checks.append({"name": "R:R", "pass": ok,
                    "detail": f"1:{rr:.1f} (SL ${SL_DOLLARS} / TP ${TP1_DOLLARS})"})
    if ok: score += 1

    return score, checks


# ═══════════════════════════════════════════════════════════════
# TRADE MANAGEMENT
# ═══════════════════════════════════════════════════════════════
def load_data():
    global trade_history, signal_log, active_trade, signal_count_today, last_reset_date
    for path, target, default in [
        (TRADE_HISTORY_FILE, "trade_history", []),
        (SIGNAL_LOG_FILE, "signal_log", []),
    ]:
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    globals()[target] = json.load(f)
        except Exception as e:
            logger.error(f"Load {path}: {e}")
            globals()[target] = default
    try:
        if os.path.exists(BOT_STATE_FILE):
            with open(BOT_STATE_FILE, "r") as f:
                st = json.load(f)
                active_trade = st.get("active_trade")
                signal_count_today = st.get("signal_count_today", 0)
                last_reset_date = st.get("last_reset_date", "")
    except Exception as e:
        logger.error(f"Load state: {e}")


def save_data():
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=2, default=str)
        with open(SIGNAL_LOG_FILE, "w") as f:
            json.dump(signal_log, f, indent=2, default=str)
        with open(BOT_STATE_FILE, "w") as f:
            json.dump({"active_trade": active_trade,
                        "signal_count_today": signal_count_today,
                        "last_reset_date": last_reset_date}, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Save error: {e}")


def reset_daily_counters():
    global signal_count_today, last_reset_date
    today = now_dubai().date().isoformat()
    if today != last_reset_date:
        signal_count_today = 0
        last_reset_date = today
        save_data()


def get_daily_pnl() -> float:
    today = now_dubai().date().isoformat()
    return sum(t.get("pnl", 0) for t in trade_history if t.get("date") == today)


# ═══════════════════════════════════════════════════════════════
# SIGNAL GENERATION
# ═══════════════════════════════════════════════════════════════
async def generate_signal(context: ContextTypes.DEFAULT_TYPE, price: float):
    """Generate signal based on EMA crossover + SMC/ICT confirmation."""
    global cooldown_until, signal_count_today, active_signal_msg_id, last_ema_cross

    # Guards
    if not is_market_open():
        return
    if active_trade or active_signal_msg_id:
        return
    if time.time() < cooldown_until:
        return
    if signal_count_today >= MAX_DAILY_TRADES:
        return
    if get_daily_pnl() <= DAILY_LOSS_LIMIT:
        return
    if len(candles_m5) < 50 or len(candles_h1) < 20:
        return

    # Step 1: Check EMA crossover for direction
    ema_dir, ema_fast, ema_slow = get_ema_signal(candles_m5)
    
    if ema_dir is None:
        # No recent EMA cross - also check trend bias as alternative
        bias = get_ema_trend_bias(candles_m5)
        if bias == "BULLISH":
            ema_dir = "BUY"
        elif bias == "BEARISH":
            ema_dir = "SELL"
        else:
            return  # No clear direction

    # Avoid duplicate signals for same cross
    if ema_dir == last_ema_cross:
        return
    
    # Step 2: Check HTF trend alignment
    htf = detect_htf_trend(candles_h1)
    if ema_dir == "BUY" and htf == "BEARISH":
        return
    if ema_dir == "SELL" and htf == "BULLISH":
        return

    # Step 3: RSI filter - don't buy overbought, don't sell oversold
    rsi = calculate_rsi(candles_m5, RSI_PERIOD)
    if rsi is not None:
        if ema_dir == "BUY" and rsi > RSI_OVERBOUGHT:
            return
        if ema_dir == "SELL" and rsi < RSI_OVERSOLD:
            return

    # Step 4: Run full checklist
    score, checks = run_checklist(price, ema_dir)
    if score < MIN_CHECKLIST_SCORE:
        return

    # Signal confirmed!
    last_ema_cross = ema_dir
    direction = ema_dir

    if direction == "BUY":
        sl = price - SL_DOLLARS
        tp1 = price + TP1_DOLLARS
        tp2 = price + TP2_DOLLARS
    else:
        sl = price + SL_DOLLARS
        tp1 = price - TP1_DOLLARS
        tp2 = price - TP2_DOLLARS

    session = get_session()
    rsi_str = f"{rsi}" if rsi else "N/A"

    checklist_text = ""
    for item in checks:
        e = "✅" if item["pass"] else "❌"
        checklist_text += f"\n{e} {item['name']}: {item['detail']}"

    msg = f"""🔔 NEW TRADE SIGNAL!
━━━━━━━━━━━━━━━━━
📊 {direction} @ ${price:,.2f}
🔴 SL: ${sl:,.2f} (${SL_DOLLARS} = -${SL_DOLLARS * LOT_SIZE * 100 * PIP_VALUE:.2f})
🎯 TP1: ${tp1:,.2f} (${TP1_DOLLARS} = +${TP1_DOLLARS * LOT_SIZE * 100 * PIP_VALUE:.2f})
🎯 TP2: ${tp2:,.2f}
🏷 Lot: {LOT_SIZE} | R:R 1:{TP1_DOLLARS/SL_DOLLARS:.1f} | RSI: {rsi_str}
📈 EMA9: {ema_fast} | EMA21: {ema_slow}

📋 Checklist: {score}/9{checklist_text}

🏛 XM Micro | {LOT_SIZE} lot
🕐 Session: {session}
📡 {len(candles_m5)} M5 + {len(candles_h1)} H1 candles
━━━━━━━━━━━━━━━━━"""

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ ဝင်မယ်", callback_data=f"enter_{direction}_{price}_{sl}_{tp1}_{tp2}"),
        InlineKeyboardButton("❌ Skip", callback_data="skip"),
        InlineKeyboardButton("⏰ Wait", callback_data="wait_signal")
    ]])

    sent = await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg, reply_markup=keyboard)
    active_signal_msg_id = sent.message_id

    signal_log.append({
        "time": now_dubai().isoformat(), "direction": direction, "price": price,
        "sl": sl, "tp1": tp1, "rsi": rsi, "score": score, "session": session,
        "ema_fast": ema_fast, "ema_slow": ema_slow
    })
    signal_count_today += 1
    cooldown_until = time.time() + COOLDOWN_SECONDS
    save_data()
    logger.info(f"Signal: {direction} @ ${price:,.2f} | EMA {ema_fast}/{ema_slow} | RSI {rsi} | Score {score}/9")


# ═══════════════════════════════════════════════════════════════
# CALLBACKS
# ═══════════════════════════════════════════════════════════════
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_trade, active_signal_msg_id
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("enter_"):
        parts = data.split("_")
        direction, entry_price = parts[1], float(parts[2])
        sl, tp1, tp2 = float(parts[3]), float(parts[4]), float(parts[5])
        active_trade = {
            "direction": direction, "entry": entry_price, "sl": sl,
            "tp1": tp1, "tp2": tp2, "time": now_dubai().isoformat(),
            "date": now_dubai().date().isoformat(), "session": get_session()
        }
        active_signal_msg_id = None
        save_data()
        await query.edit_message_text(
            query.message.text + f"\n\n✅ ENTERED! {direction} @ ${entry_price:,.2f}\n⏳ Monitoring..."
        )
    elif data == "skip":
        active_signal_msg_id = None
        await query.edit_message_text(query.message.text + "\n\n❌ Signal Skipped")
    elif data == "wait_signal":
        await query.edit_message_text(query.message.text + "\n\n⏰ Waiting 5 min...")
        asyncio.create_task(expire_signal(context, query.message.message_id))


async def expire_signal(context, msg_id):
    global active_signal_msg_id
    await asyncio.sleep(300)
    if active_signal_msg_id == msg_id:
        active_signal_msg_id = None
        try:
            await context.bot.edit_message_text(
                chat_id=ADMIN_CHAT_ID, message_id=msg_id,
                text="⏰ Signal expired (5 min timeout)"
            )
        except:
            pass


# ═══════════════════════════════════════════════════════════════
# TRADE MONITORING
# ═══════════════════════════════════════════════════════════════
async def monitor_active_trade(context: ContextTypes.DEFAULT_TYPE, price: float):
    global active_trade
    if not active_trade:
        return
    d = active_trade["direction"]
    entry, sl, tp1 = active_trade["entry"], active_trade["sl"], active_trade["tp1"]
    hit = None
    if d == "BUY":
        if price <= sl: hit = "SL"
        elif price >= tp1: hit = "TP1"
    else:
        if price >= sl: hit = "SL"
        elif price <= tp1: hit = "TP1"
    if hit:
        move = (price - entry) if d == "BUY" else (entry - price)
        pnl = round(move * LOT_SIZE * 100 * PIP_VALUE, 2)
        result = "WIN" if pnl > 0 else "LOSS"
        trade_history.append({
            **active_trade, "exit_price": price, "exit_time": now_dubai().isoformat(),
            "result": result, "pnl": pnl, "hit": hit
        })
        active_trade = None
        save_data()
        emoji = "🟢" if result == "WIN" else "🔴"
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID,
            text=f"""{emoji} TRADE CLOSED - {result}!
━━━━━━━━━━━━━━━━━
📊 {d} @ ${entry:,.2f} → ${price:,.2f} ({hit})
💰 PnL: ${pnl:+.2f}
━━━━━━━━━━━━━━━━━""")


# ═══════════════════════════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════════════════════════
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"""🤖 PawOo Gold Signal Bot v{VERSION}
━━━━━━━━━━━━━━━━━
📊 Strategy: EMA(9/21) + SMC/ICT Hybrid
📈 Pair: XAU/USD (Gold)
🏛 Broker: XM Micro (GOLDm#)

Commands:
/scan - Force scan
/price - Price & indicators
/status - Performance
/close [price] - Close trade

EMA crossover + SMC confirmation
Min score: {MIN_CHECKLIST_SCORE}/9 for signal
━━━━━━━━━━━━━━━━━""")


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = fetch_spot_price()
    fetch_candles()
    rsi = calculate_rsi(candles_m5, RSI_PERIOD)
    htf = detect_htf_trend(candles_h1)
    bias = get_ema_trend_bias(candles_m5)
    _, ef, es = get_ema_signal(candles_m5)
    session = get_session()
    market = "🟢 OPEN" if is_market_open() else "🔴 CLOSED"
    await update.message.reply_text(f"""📊 Gold Price & Analysis
━━━━━━━━━━━━━━━━━
💰 Price: ${price:,.2f if price else 0}
📉 RSI(14): {rsi or 'N/A'}
📈 EMA9: {ef} | EMA21: {es}
📈 M5 Bias: {bias} | H1 Trend: {htf}
🕐 {session} | {market}
📡 {len(candles_m5)} M5 + {len(candles_h1)} H1
━━━━━━━━━━━━━━━━━""")


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = fetch_spot_price()
    fetch_candles()
    if not price:
        await update.message.reply_text("❌ Cannot fetch price.")
        return

    rsi = calculate_rsi(candles_m5, RSI_PERIOD)
    htf = detect_htf_trend(candles_h1)
    ema_dir, ef, es = get_ema_signal(candles_m5)
    bias = get_ema_trend_bias(candles_m5)

    buy_score, buy_checks = run_checklist(price, "BUY")
    sell_score, sell_checks = run_checklist(price, "SELL")
    best_dir = "BUY" if buy_score >= sell_score else "SELL"
    best_score = max(buy_score, sell_score)
    best_checks = buy_checks if buy_score >= sell_score else sell_checks

    checklist_text = ""
    for item in best_checks:
        e = "✅" if item["pass"] else "❌"
        checklist_text += f"\n{e} {item['name']}: {item['detail']}"

    status = "✅ Signal ready!" if best_score >= MIN_CHECKLIST_SCORE else f"⏳ Need {MIN_CHECKLIST_SCORE - best_score} more"
    ema_cross_str = f"→ {ema_dir}" if ema_dir else "No cross"

    msg = f"""🔍 Scan Result
━━━━━━━━━━━━━━━━━
💰 ${price:,.2f} | RSI: {rsi} | H1: {htf}
📈 EMA9: {ef} | EMA21: {es} | {ema_cross_str}
📈 M5 Bias: {bias}

📋 Best: {best_dir} ({best_score}/9) {status}
{checklist_text}

📡 {len(candles_m5)} M5 + {len(candles_h1)} H1
━━━━━━━━━━━━━━━━━"""
    await update.message.reply_text(msg)

    if best_score >= MIN_CHECKLIST_SCORE and is_market_open():
        if not active_trade and not active_signal_msg_id:
            if time.time() >= cooldown_until and signal_count_today < MAX_DAILY_TRADES:
                await generate_signal(context, price)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = [t for t in trade_history if t.get("result") in ("WIN", "LOSS")]
    wins = len([t for t in total if t["result"] == "WIN"])
    wr = (wins / len(total) * 100) if total else 0
    total_pnl = sum(t.get("pnl", 0) for t in trade_history)
    today_pnl = get_daily_pnl()
    market = "🟢 OPEN" if is_market_open() else "🔴 CLOSED"
    session = get_session()
    trade_str = ""
    if active_trade:
        p = fetch_spot_price()
        if p:
            d = active_trade["direction"]
            e = active_trade["entry"]
            mv = (p - e) if d == "BUY" else (e - p)
            pnl = mv * LOT_SIZE * 100 * PIP_VALUE
            trade_str = f"\n\n📊 Active: {d} @ ${e:,.2f}\n💰 PnL: ${pnl:+.2f}"
    await update.message.reply_text(f"""📊 PawOo v{VERSION} Status
━━━━━━━━━━━━━━━━━
{market} | {session}
📈 Signals: {signal_count_today}/{MAX_DAILY_TRADES} | Today: ${today_pnl:+.2f}
🎯 Win Rate: {wr:.0f}% ({wins}/{len(total)})
💰 Total PnL: ${total_pnl:+.2f}
📝 Signals: {len(signal_log)}{trade_str}
━━━━━━━━━━━━━━━━━""")


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_trade
    if not active_trade:
        await update.message.reply_text("❌ No active trade.")
        return
    parts = update.message.text.strip().split()
    try:
        cp = float(parts[1].replace("$", "").replace(",", "")) if len(parts) > 1 else fetch_spot_price()
    except:
        cp = fetch_spot_price()
    if not cp:
        await update.message.reply_text("❌ Cannot get price. /close 4550.00")
        return
    d, e = active_trade["direction"], active_trade["entry"]
    mv = (cp - e) if d == "BUY" else (e - cp)
    pnl = round(mv * LOT_SIZE * 100 * PIP_VALUE, 2)
    result = "WIN" if pnl > 0 else "LOSS"
    trade_history.append({**active_trade, "exit_price": cp, "exit_time": now_dubai().isoformat(),
                          "result": result, "pnl": pnl, "hit": "Manual"})
    active_trade = None
    save_data()
    emoji = "🟢" if result == "WIN" else "🔴"
    await update.message.reply_text(f"""{emoji} Closed - {result}
━━━━━━━━━━━━━━━━━
📊 {d} @ ${e:,.2f} → ${cp:,.2f}
💰 PnL: ${pnl:+.2f}
━━━━━━━━━━━━━━━━━""")


# ═══════════════════════════════════════════════════════════════
# MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    text = update.message.text.lower().strip()
    if any(w in text for w in ["hi", "hello", "yo", "bot", "status", "update"]):
        price = fetch_spot_price()
        fetch_candles()
        rsi = calculate_rsi(candles_m5, RSI_PERIOD)
        htf = detect_htf_trend(candles_h1)
        bias = get_ema_trend_bias(candles_m5)
        _, ef, es = get_ema_signal(candles_m5)
        session = get_session()
        market = "OPEN" if is_market_open() else "CLOSED"
        await update.message.reply_text(f"""🔍 PawOo v{VERSION} ACTIVE!
━━━━━━━━━━━━━━━━━
💰 ${price:,.2f if price else 0} | RSI: {rsi or 'N/A'}
📈 EMA9: {ef} | EMA21: {es} | Bias: {bias}
📈 H1: {htf} | {session} | {market}
📊 {signal_count_today}/{MAX_DAILY_TRADES} trades | ${get_daily_pnl():+.2f}
📡 {len(candles_m5)} M5 + {len(candles_h1)} H1
━━━━━━━━━━━━━━━━━""")
        if price and is_market_open():
            if not active_trade and not active_signal_msg_id:
                await generate_signal(context, price)


# ═══════════════════════════════════════════════════════════════
# AUTO-SCANNER
# ═══════════════════════════════════════════════════════════════
async def auto_scanner(context: ContextTypes.DEFAULT_TYPE):
    reset_daily_counters()
    if not is_market_open():
        return
    price = fetch_spot_price()
    if not price:
        return
    if time.time() - last_candle_fetch >= 120:
        fetch_candles()
    if active_trade:
        await monitor_active_trade(context, price)
        return
    if not is_active_session():
        return
    await generate_signal(context, price)


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    logger.info(f"═══ PawOo Gold Signal Bot v{VERSION} Starting ═══")
    logger.info(f"Strategy: EMA({EMA_FAST}/{EMA_SLOW}) + SMC/ICT | Min score: {MIN_CHECKLIST_SCORE}/9")
    load_data()
    fetch_spot_price()
    fetch_candles()
    logger.info(f"Data: {len(candles_m5)} M5 + {len(candles_h1)} H1")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("close", close_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.job_queue.run_repeating(auto_scanner, interval=SCAN_INTERVAL, first=5)

    logger.info("Bot started! EMA+SMC scanning every 30s.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
