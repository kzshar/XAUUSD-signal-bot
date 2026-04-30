#!/usr/bin/env python3
"""
PawOo Gold Signal Bot v4.0
==========================
XAU/USD Scalping Signal Bot with RSI + SMC Hybrid Strategy
Clean rebuild - Production ready for Railway.app

Strategy: RSI(14) M5 + SMC confirmation + H1 trend alignment
Data: Yahoo Finance GC=F candles + gold-api.com spot price
Broker: XM Micro (GOLDm#) 0.5 lot
"""

import asyncio
import datetime
import json
import logging
import os
import time
from collections import deque
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
VERSION = "4.0"

# Trading Parameters
LOT_SIZE = 0.5
PIP_VALUE = 0.005  # XM Micro 1 point = $0.005
SL_DOLLARS = 4.0   # $4 move for SL
TP1_DOLLARS = 10.0  # $10 move for TP1
TP2_DOLLARS = 15.0  # $15 move for TP2
MAX_DAILY_TRADES = 5
DAILY_LOSS_LIMIT = -10.0  # Stop trading if daily loss exceeds this
COOLDOWN_SECONDS = 900  # 15 minutes between signals
SCAN_INTERVAL = 30  # Scan every 30 seconds

# Strategy Parameters
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
MIN_CHECKLIST_SCORE = 6  # Minimum 6/9 for signal
RSI_PERIOD = 14

# File paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_HISTORY_FILE = os.path.join(BASE_DIR, "trade_history.json")
SIGNAL_LOG_FILE = os.path.join(BASE_DIR, "signal_log.json")
DAILY_JOURNAL_FILE = os.path.join(BASE_DIR, "daily_journal.json")
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

# ═══════════════════════════════════════════════════════════════
# TIME & MARKET FUNCTIONS
# ═══════════════════════════════════════════════════════════════
def now_dubai() -> datetime.datetime:
    """Get current time in Dubai timezone."""
    return datetime.datetime.now(DUBAI_TZ)


def is_market_open() -> bool:
    """Check if XM GOLDm# market is open.
    
    Schedule (Dubai time):
    - Daily maintenance: 00:50 - 03:10 (Mon-Sat morning)
    - Weekend: Saturday 00:50 - Sunday 23:30
    """
    n = now_dubai()
    wd, h, m = n.weekday(), n.hour, n.minute
    
    # Weekend close
    if wd == 5:  # Saturday
        if h == 0 and m < 50:
            return True  # Friday session ending
        return False
    if wd == 6:  # Sunday
        return h >= 23 and m >= 30
    
    # Daily maintenance break: 00:50 - 03:10
    if h == 0 and m >= 50:
        return False
    if h in (1, 2):
        return False
    if h == 3 and m < 10:
        return False
    
    return True


def get_session() -> str:
    """Get current trading session name.
    
    London: 11:00 - 20:00 Dubai
    New York: 16:30 - 01:00 Dubai (next day)
    Asian: 03:10 - 11:00 Dubai
    """
    n = now_dubai()
    h, m = n.hour, n.minute
    t = h * 60 + m  # minutes since midnight
    
    # New York: 16:30 - 01:00 (next day)
    if t >= 990 or t < 50:  # 16:30=990min, 00:50=50min
        return "New York"
    # London: 11:00 - 20:00
    if 660 <= t < 1200:  # 11:00=660, 20:00=1200
        return "London"
    # London+NY overlap: 16:30 - 20:00
    if 990 <= t < 1200:
        return "London/NY"
    return "Asian"


def is_active_session() -> bool:
    """Only trade during London and New York sessions."""
    session = get_session()
    return session in ("London", "New York", "London/NY")


# ═══════════════════════════════════════════════════════════════
# DATA FETCHING
# ═══════════════════════════════════════════════════════════════
def fetch_spot_price() -> Optional[float]:
    """Fetch XAU/USD spot price from gold-api.com."""
    global cached_price, last_price_fetch
    
    now = time.time()
    if now - last_price_fetch < 20 and cached_price:
        return cached_price
    
    try:
        r = requests.get("https://api.gold-api.com/price/XAU", timeout=8)
        if r.status_code == 200:
            data = r.json()
            price = float(data["price"])
            cached_price = price
            last_price_fetch = now
            return price
    except Exception as e:
        logger.warning(f"gold-api.com failed: {e}")
    
    # Fallback: Yahoo Finance GC=F
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d",
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            price = float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
            cached_price = price
            last_price_fetch = now
            return price
    except Exception as e:
        logger.warning(f"Yahoo price fallback failed: {e}")
    
    return cached_price


def fetch_candles() -> bool:
    """Fetch M5 and H1 candles from Yahoo Finance GC=F.
    
    Returns True if successful.
    """
    global candles_m5, candles_h1, last_candle_fetch
    
    now = time.time()
    if now - last_candle_fetch < 60:  # Don't fetch more than once per minute
        return True
    
    headers = {"User-Agent": "Mozilla/5.0"}
    success = False
    
    # Fetch M5 candles (5d range)
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=5m&range=5d",
            headers=headers, timeout=10
        )
        if r.status_code == 200:
            data = r.json()["chart"]["result"][0]
            timestamps = data["timestamp"]
            quotes = data["indicators"]["quote"][0]
            
            new_candles = []
            for i in range(len(timestamps)):
                o = quotes["open"][i]
                h = quotes["high"][i]
                l = quotes["low"][i]
                c = quotes["close"][i]
                
                # Skip None values
                if any(v is None for v in (o, h, l, c)):
                    continue
                # Filter settlement candles (O=H=L=C)
                if o == h == l == c:
                    continue
                
                new_candles.append({
                    "time": timestamps[i],
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "volume": quotes["volume"][i] or 0
                })
            
            if new_candles:
                candles_m5 = new_candles
                success = True
                logger.info(f"Fetched {len(candles_m5)} M5 candles")
    except Exception as e:
        logger.error(f"M5 candle fetch error: {e}")
    
    # Fetch H1 candles (1mo range)
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1h&range=1mo",
            headers=headers, timeout=10
        )
        if r.status_code == 200:
            data = r.json()["chart"]["result"][0]
            timestamps = data["timestamp"]
            quotes = data["indicators"]["quote"][0]
            
            new_candles = []
            for i in range(len(timestamps)):
                o = quotes["open"][i]
                h = quotes["high"][i]
                l = quotes["low"][i]
                c = quotes["close"][i]
                
                if any(v is None for v in (o, h, l, c)):
                    continue
                if o == h == l == c:
                    continue
                
                new_candles.append({
                    "time": timestamps[i],
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "volume": quotes["volume"][i] or 0
                })
            
            if new_candles:
                candles_h1 = new_candles
                logger.info(f"Fetched {len(candles_h1)} H1 candles")
    except Exception as e:
        logger.error(f"H1 candle fetch error: {e}")
    
    if success:
        last_candle_fetch = now
    return success


# ═══════════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS
# ═══════════════════════════════════════════════════════════════
def calculate_rsi(candles: List[Dict], period: int = 14) -> Optional[float]:
    """Calculate RSI using Wilder's smoothed method (matches MT5/TradingView)."""
    if len(candles) < period + 1:
        return None
    
    closes = [c["close"] for c in candles]
    
    # Calculate price changes
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    
    # First average: simple average of first 'period' changes
    gains = [d if d > 0 else 0 for d in deltas[:period]]
    losses = [-d if d < 0 else 0 for d in deltas[:period]]
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    # Wilder's smoothing for remaining values
    for i in range(period, len(deltas)):
        d = deltas[i]
        gain = d if d > 0 else 0
        loss = -d if d < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 1)


def detect_htf_trend(candles: List[Dict]) -> str:
    """Detect H1 trend using swing structure.
    
    Returns: 'BULLISH', 'BEARISH', or 'RANGING'
    """
    if len(candles) < 20:
        return "UNKNOWN"
    
    recent = candles[-20:]
    
    # Find swing highs and lows
    swing_highs = []
    swing_lows = []
    
    for i in range(2, len(recent) - 2):
        if recent[i]["high"] > recent[i-1]["high"] and recent[i]["high"] > recent[i+1]["high"]:
            if recent[i]["high"] > recent[i-2]["high"] and recent[i]["high"] > recent[i+2]["high"]:
                swing_highs.append(recent[i]["high"])
        if recent[i]["low"] < recent[i-1]["low"] and recent[i]["low"] < recent[i+1]["low"]:
            if recent[i]["low"] < recent[i-2]["low"] and recent[i]["low"] < recent[i+2]["low"]:
                swing_lows.append(recent[i]["low"])
    
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        # Fallback: compare first half vs second half
        mid = len(recent) // 2
        first_avg = sum(c["close"] for c in recent[:mid]) / mid
        second_avg = sum(c["close"] for c in recent[mid:]) / (len(recent) - mid)
        if second_avg > first_avg * 1.001:
            return "BULLISH"
        elif second_avg < first_avg * 0.999:
            return "BEARISH"
        return "RANGING"
    
    # Higher highs + higher lows = bullish
    hh = swing_highs[-1] > swing_highs[-2]
    hl = swing_lows[-1] > swing_lows[-2]
    ll = swing_lows[-1] < swing_lows[-2]
    lh = swing_highs[-1] < swing_highs[-2]
    
    if hh and hl:
        return "BULLISH"
    elif ll and lh:
        return "BEARISH"
    return "RANGING"


def detect_bos(candles: List[Dict], direction: str) -> Tuple[bool, Optional[float]]:
    """Detect Break of Structure on M5.
    
    BUY: price breaks above recent swing high
    SELL: price breaks below recent swing low
    """
    if len(candles) < 20:
        return False, None
    
    recent = candles[-20:]
    current_close = recent[-1]["close"]
    
    # Find recent swing points (look back 10-18 candles, not the last 2)
    swing_highs = []
    swing_lows = []
    
    for i in range(2, len(recent) - 2):
        if recent[i]["high"] > recent[i-1]["high"] and recent[i]["high"] > recent[i+1]["high"]:
            swing_highs.append(recent[i]["high"])
        if recent[i]["low"] < recent[i-1]["low"] and recent[i]["low"] < recent[i+1]["low"]:
            swing_lows.append(recent[i]["low"])
    
    if direction == "BUY" and swing_highs:
        last_sh = swing_highs[-1]
        if current_close > last_sh:
            return True, last_sh
    elif direction == "SELL" and swing_lows:
        last_sl = swing_lows[-1]
        if current_close < last_sl:
            return True, last_sl
    
    return False, None


def detect_order_block(candles: List[Dict], direction: str) -> Tuple[bool, Optional[float], Optional[float]]:
    """Detect nearest Order Block.
    
    BUY OB: Last bearish candle before a bullish impulse move
    SELL OB: Last bullish candle before a bearish impulse move
    """
    if len(candles) < 15:
        return False, None, None
    
    recent = candles[-15:]
    current_price = recent[-1]["close"]
    
    for i in range(len(recent) - 3, 1, -1):
        candle = recent[i]
        body = abs(candle["close"] - candle["open"])
        
        if direction == "BUY":
            # Bearish candle followed by strong bullish move
            if candle["close"] < candle["open"]:  # Bearish
                next_move = recent[i+1]["close"] - recent[i+1]["open"]
                if next_move > body * 1.5:  # Strong bullish follow
                    ob_low = candle["low"]
                    ob_high = candle["open"]
                    # Price should be near OB
                    if ob_low <= current_price <= ob_high * 1.002:
                        return True, ob_low, ob_high
        else:  # SELL
            # Bullish candle followed by strong bearish move
            if candle["close"] > candle["open"]:  # Bullish
                next_move = recent[i+1]["open"] - recent[i+1]["close"]
                if next_move > body * 1.5:  # Strong bearish follow
                    ob_low = candle["close"]
                    ob_high = candle["high"]
                    if ob_low * 0.998 <= current_price <= ob_high:
                        return True, ob_low, ob_high
    
    return False, None, None


def detect_fvg(candles: List[Dict], direction: str) -> bool:
    """Detect Fair Value Gap (imbalance).
    
    BUY FVG: Gap between candle[i-1] high and candle[i+1] low
    SELL FVG: Gap between candle[i-1] low and candle[i+1] high
    """
    if len(candles) < 10:
        return False
    
    recent = candles[-10:]
    
    for i in range(1, len(recent) - 1):
        if direction == "BUY":
            gap = recent[i+1]["low"] - recent[i-1]["high"]
            if gap > 0.5:  # Significant gap
                return True
        else:
            gap = recent[i-1]["low"] - recent[i+1]["high"]
            if gap > 0.5:
                return True
    
    return False


def detect_liquidity_sweep(candles: List[Dict], direction: str) -> bool:
    """Detect liquidity sweep (stop hunt).
    
    BUY: Price wicks below recent lows then closes above
    SELL: Price wicks above recent highs then closes below
    """
    if len(candles) < 15:
        return False
    
    recent = candles[-15:]
    last = recent[-1]
    
    # Find recent swing levels
    if direction == "BUY":
        recent_lows = sorted([c["low"] for c in recent[:-1]])[:3]
        if recent_lows:
            lowest = min(recent_lows)
            # Wick below low but close above
            if last["low"] <= lowest and last["close"] > lowest:
                return True
    else:
        recent_highs = sorted([c["high"] for c in recent[:-1]], reverse=True)[:3]
        if recent_highs:
            highest = max(recent_highs)
            if last["high"] >= highest and last["close"] < highest:
                return True
    
    return False


def detect_displacement(candles: List[Dict], direction: str) -> Tuple[bool, float]:
    """Detect displacement (strong momentum candle).
    
    Returns (detected, strength_multiplier)
    """
    if len(candles) < 10:
        return False, 0
    
    recent = candles[-10:]
    
    # Average body size
    bodies = [abs(c["close"] - c["open"]) for c in recent[:-1]]
    avg_body = sum(bodies) / len(bodies) if bodies else 1
    
    last = recent[-1]
    last_body = abs(last["close"] - last["open"])
    
    if avg_body == 0:
        return False, 0
    
    multiplier = last_body / avg_body
    
    if direction == "BUY" and last["close"] > last["open"] and multiplier >= 1.5:
        return True, round(multiplier, 1)
    elif direction == "SELL" and last["close"] < last["open"] and multiplier >= 1.5:
        return True, round(multiplier, 1)
    
    return False, round(multiplier, 1)


def is_premium_discount(candles: List[Dict], price: float, direction: str) -> bool:
    """Check if price is in correct zone.
    
    BUY: Price should be in discount zone (below 50% of range)
    SELL: Price should be in premium zone (above 50% of range)
    """
    if len(candles) < 50:
        return False
    
    recent = candles[-50:]
    highest = max(c["high"] for c in recent)
    lowest = min(c["low"] for c in recent)
    
    if highest == lowest:
        return False
    
    midpoint = (highest + lowest) / 2
    
    if direction == "BUY":
        return price < midpoint  # Discount zone
    else:
        return price > midpoint  # Premium zone


def is_near_key_level(candles: List[Dict], price: float) -> Tuple[bool, Optional[str]]:
    """Check if price is near a key support/resistance level."""
    if len(candles) < 50:
        return False, None
    
    recent = candles[-50:]
    
    # Find key levels from swing points
    levels = []
    for i in range(2, len(recent) - 2):
        if recent[i]["high"] > recent[i-1]["high"] and recent[i]["high"] > recent[i+1]["high"]:
            levels.append(recent[i]["high"])
        if recent[i]["low"] < recent[i-1]["low"] and recent[i]["low"] < recent[i+1]["low"]:
            levels.append(recent[i]["low"])
    
    # Check proximity (within $3)
    for level in levels:
        if abs(price - level) < 3.0:
            return True, f"${level:,.2f}"
    
    return False, None


# ═══════════════════════════════════════════════════════════════
# SMC CHECKLIST
# ═══════════════════════════════════════════════════════════════
def run_smc_checklist(price: float, direction: str) -> Tuple[float, List[Dict]]:
    """Run full SMC checklist for signal quality.
    
    Returns (score out of 9, checklist items)
    """
    checks = []
    score = 0
    
    # 1. HTF Trend alignment
    htf_trend = detect_htf_trend(candles_h1)
    trend_aligned = (direction == "BUY" and htf_trend == "BULLISH") or \
                    (direction == "SELL" and htf_trend == "BEARISH")
    checks.append({
        "name": "HTF Trend",
        "pass": trend_aligned,
        "detail": f"{htf_trend} - {'aligned' if trend_aligned else 'NOT aligned'}"
    })
    if trend_aligned:
        score += 1
    
    # 2. Premium/Discount zone
    pd_ok = is_premium_discount(candles_m5, price, direction)
    checks.append({
        "name": "Premium/Discount",
        "pass": pd_ok,
        "detail": "In correct zone" if pd_ok else "Wrong zone"
    })
    if pd_ok:
        score += 1
    
    # 3. Key Level
    near_key, level_str = is_near_key_level(candles_m5, price)
    checks.append({
        "name": "Key Level",
        "pass": near_key,
        "detail": f"Near {level_str}" if near_key else "Not near key level"
    })
    if near_key:
        score += 1
    
    # 4. Liquidity Sweep
    liq_sweep = detect_liquidity_sweep(candles_m5, direction)
    checks.append({
        "name": "Liquidity Sweep",
        "pass": liq_sweep,
        "detail": "Detected" if liq_sweep else "Not detected"
    })
    if liq_sweep:
        score += 1
    
    # 5. Displacement
    disp_ok, disp_mult = detect_displacement(candles_m5, direction)
    checks.append({
        "name": "Displacement",
        "pass": disp_ok,
        "detail": f"Strong ({disp_mult}x avg)" if disp_ok else f"Weak ({disp_mult}x avg)"
    })
    if disp_ok:
        score += 1
    
    # 6. BOS
    bos_ok, bos_level = detect_bos(candles_m5, direction)
    checks.append({
        "name": "BOS",
        "pass": bos_ok,
        "detail": f"Confirmed at ${bos_level:,.2f}" if bos_ok else "Not confirmed"
    })
    if bos_ok:
        score += 1
    
    # 7. Order Block
    ob_ok, ob_low, ob_high = detect_order_block(candles_m5, direction)
    checks.append({
        "name": "Order Block",
        "pass": ob_ok,
        "detail": f"${ob_low:,.2f}-${ob_high:,.2f}" if ob_ok else "Not in OB"
    })
    if ob_ok:
        score += 1
    
    # 8. RSI confirmation
    rsi = calculate_rsi(candles_m5, RSI_PERIOD)
    rsi_ok = False
    rsi_detail = "N/A"
    if rsi is not None:
        if direction == "BUY" and rsi < RSI_OVERSOLD:
            rsi_ok = True
            rsi_detail = f"{rsi} - Oversold (BUY confirm)"
        elif direction == "SELL" and rsi > RSI_OVERBOUGHT:
            rsi_ok = True
            rsi_detail = f"{rsi} - Overbought (SELL confirm)"
        elif 40 <= rsi <= 60:
            rsi_detail = f"{rsi} - Neutral"
        else:
            rsi_detail = f"{rsi} - {'High' if rsi > 50 else 'Low'}"
    checks.append({
        "name": "RSI",
        "pass": rsi_ok,
        "detail": rsi_detail
    })
    if rsi_ok:
        score += 1
    
    # 9. R:R ratio (always pass with fixed SL/TP)
    rr = TP1_DOLLARS / SL_DOLLARS
    rr_ok = rr >= 2.5
    checks.append({
        "name": "R:R",
        "pass": rr_ok,
        "detail": f"1:{rr:.1f} (fixed SL ${SL_DOLLARS} / TP ${TP1_DOLLARS})"
    })
    if rr_ok:
        score += 1
    
    return score, checks


# ═══════════════════════════════════════════════════════════════
# TRADE MANAGEMENT
# ═══════════════════════════════════════════════════════════════
def load_data():
    """Load persistent data from JSON files."""
    global trade_history, signal_log, active_trade, signal_count_today, last_reset_date
    
    try:
        if os.path.exists(TRADE_HISTORY_FILE):
            with open(TRADE_HISTORY_FILE, "r") as f:
                trade_history = json.load(f)
    except Exception as e:
        logger.error(f"Error loading trade history: {e}")
        trade_history = []
    
    try:
        if os.path.exists(SIGNAL_LOG_FILE):
            with open(SIGNAL_LOG_FILE, "r") as f:
                signal_log = json.load(f)
    except Exception as e:
        logger.error(f"Error loading signal log: {e}")
        signal_log = []
    
    try:
        if os.path.exists(BOT_STATE_FILE):
            with open(BOT_STATE_FILE, "r") as f:
                state = json.load(f)
                active_trade = state.get("active_trade")
                signal_count_today = state.get("signal_count_today", 0)
                last_reset_date = state.get("last_reset_date", "")
    except Exception as e:
        logger.error(f"Error loading bot state: {e}")


def save_data():
    """Save persistent data to JSON files."""
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error saving trade history: {e}")
    
    try:
        with open(SIGNAL_LOG_FILE, "w") as f:
            json.dump(signal_log, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error saving signal log: {e}")
    
    try:
        state = {
            "active_trade": active_trade,
            "signal_count_today": signal_count_today,
            "last_reset_date": last_reset_date,
        }
        with open(BOT_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error saving bot state: {e}")


def reset_daily_counters():
    """Reset daily counters if new day."""
    global signal_count_today, last_reset_date
    
    today = now_dubai().date().isoformat()
    if today != last_reset_date:
        signal_count_today = 0
        last_reset_date = today
        save_data()
        logger.info(f"Daily counters reset for {today}")


def get_daily_pnl() -> float:
    """Calculate today's PnL."""
    today = now_dubai().date().isoformat()
    return sum(t.get("pnl", 0) for t in trade_history if t.get("date") == today)


def is_daily_loss_limit_reached() -> bool:
    """Check if daily loss limit is reached."""
    return get_daily_pnl() <= DAILY_LOSS_LIMIT


# ═══════════════════════════════════════════════════════════════
# SIGNAL GENERATION
# ═══════════════════════════════════════════════════════════════
async def generate_signal(context: ContextTypes.DEFAULT_TYPE, price: float):
    """Generate and send a trade signal if conditions are met."""
    global cooldown_until, signal_count_today, active_signal_msg_id
    
    # Guards
    if not is_market_open():
        return
    if not is_active_session():
        return
    if active_trade:
        return
    if active_signal_msg_id:
        return
    if time.time() < cooldown_until:
        return
    if signal_count_today >= MAX_DAILY_TRADES:
        return
    if is_daily_loss_limit_reached():
        return
    if len(candles_m5) < 50 or len(candles_h1) < 20:
        return
    
    # Calculate RSI
    rsi = calculate_rsi(candles_m5, RSI_PERIOD)
    if rsi is None:
        return
    
    # Determine direction based on RSI
    direction = None
    if rsi < RSI_OVERSOLD:
        direction = "BUY"
    elif rsi > RSI_OVERBOUGHT:
        direction = "SELL"
    else:
        return  # RSI not in extreme zone
    
    # Check HTF trend alignment
    htf_trend = detect_htf_trend(candles_h1)
    if direction == "BUY" and htf_trend == "BEARISH":
        return  # Don't buy against bearish trend
    if direction == "SELL" and htf_trend == "BULLISH":
        return  # Don't sell against bullish trend
    
    # Run SMC checklist
    score, checks = run_smc_checklist(price, direction)
    
    if score < MIN_CHECKLIST_SCORE:
        return  # Not enough confirmation
    
    # Calculate SL/TP
    if direction == "BUY":
        sl = price - SL_DOLLARS
        tp1 = price + TP1_DOLLARS
        tp2 = price + TP2_DOLLARS
    else:
        sl = price + SL_DOLLARS
        tp1 = price - TP1_DOLLARS
        tp2 = price - TP2_DOLLARS
    
    # Build signal message
    session = get_session()
    sl_pnl = SL_DOLLARS * LOT_SIZE * 100 * PIP_VALUE
    tp1_pnl = TP1_DOLLARS * LOT_SIZE * 100 * PIP_VALUE
    tp2_pnl = TP2_DOLLARS * LOT_SIZE * 100 * PIP_VALUE
    
    checklist_text = ""
    for item in checks:
        emoji = "✅" if item["pass"] else "❌"
        checklist_text += f"\n{emoji} {item['name']}: {item['detail']}"
    
    msg = f"""🔔 NEW TRADE SIGNAL!
━━━━━━━━━━━━━━━━━
📊 {direction} @ ${price:,.2f}
🔴 SL: ${sl:,.2f} (${SL_DOLLARS} move = -${sl_pnl:.2f})
🎯 TP1: ${tp1:,.2f} (${TP1_DOLLARS} move = +${tp1_pnl:.2f})
🎯 TP2: ${tp2:,.2f} (${TP2_DOLLARS} move = +${tp2_pnl:.2f})
🏷 Lot: {LOT_SIZE} micro | R:R: 1:{TP1_DOLLARS/SL_DOLLARS:.1f} | RSI: {rsi}

📋 SMC Checklist: {score}/9{checklist_text}

🏛 XM Micro | {LOT_SIZE} lot | 1pt=${PIP_VALUE}
🕐 Session: {session}
📡 Data: {len(candles_m5)} M5 + {len(candles_h1)} H1 candles
━━━━━━━━━━━━━━━━━"""
    
    # Send with buttons
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ ဝင်မယ်", callback_data=f"enter_{direction}_{price}_{sl}_{tp1}_{tp2}"),
            InlineKeyboardButton("❌ Skip", callback_data="skip"),
            InlineKeyboardButton("⏰ Wait", callback_data="wait_signal")
        ]
    ])
    
    sent = await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID, text=msg, reply_markup=keyboard
    )
    active_signal_msg_id = sent.message_id
    
    # Log signal
    signal_entry = {
        "id": f"s{len(signal_log)+1}",
        "time": now_dubai().isoformat(),
        "direction": direction,
        "price": price,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rsi": rsi,
        "score": score,
        "session": session,
        "checklist": checks,
    }
    signal_log.append(signal_entry)
    signal_count_today += 1
    cooldown_until = time.time() + COOLDOWN_SECONDS
    save_data()
    
    logger.info(f"Signal generated: {direction} @ ${price:,.2f} | RSI: {rsi} | Score: {score}/9")


# ═══════════════════════════════════════════════════════════════
# CALLBACK HANDLERS
# ═══════════════════════════════════════════════════════════════
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses."""
    global active_trade, active_signal_msg_id
    
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("enter_"):
        # Parse: enter_BUY_price_sl_tp1_tp2
        parts = data.split("_")
        direction = parts[1]
        entry_price = float(parts[2])
        sl = float(parts[3])
        tp1 = float(parts[4])
        tp2 = float(parts[5])
        
        active_trade = {
            "id": f"t{len(trade_history)+1}",
            "direction": direction,
            "entry": entry_price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "time": now_dubai().isoformat(),
            "date": now_dubai().date().isoformat(),
            "session": get_session(),
        }
        active_signal_msg_id = None
        save_data()
        
        await query.edit_message_text(
            query.message.text + f"\n\n✅ ENTERED! {direction} @ ${entry_price:,.2f}\n⏳ Monitoring trade..."
        )
        logger.info(f"Trade entered: {direction} @ ${entry_price:,.2f}")
    
    elif data == "skip":
        active_signal_msg_id = None
        await query.edit_message_text(query.message.text + "\n\n❌ Signal Skipped")
        logger.info("Signal skipped by user")
    
    elif data == "wait_signal":
        await query.edit_message_text(
            query.message.text + "\n\n⏰ Waiting... Signal will expire in 5 minutes."
        )
        # Auto-expire after 5 minutes
        asyncio.create_task(expire_signal(context, query.message.message_id))


async def expire_signal(context: ContextTypes.DEFAULT_TYPE, msg_id: int):
    """Auto-expire a signal after 5 minutes."""
    global active_signal_msg_id
    
    await asyncio.sleep(300)
    if active_signal_msg_id == msg_id:
        active_signal_msg_id = None
        try:
            await context.bot.edit_message_text(
                chat_id=ADMIN_CHAT_ID,
                message_id=msg_id,
                text="⏰ Signal expired (5 min timeout)"
            )
        except:
            pass


# ═══════════════════════════════════════════════════════════════
# TRADE MONITORING
# ═══════════════════════════════════════════════════════════════
async def monitor_active_trade(context: ContextTypes.DEFAULT_TYPE, price: float):
    """Monitor active trade for TP/SL hits."""
    global active_trade
    
    if not active_trade:
        return
    
    direction = active_trade["direction"]
    entry = active_trade["entry"]
    sl = active_trade["sl"]
    tp1 = active_trade["tp1"]
    tp2 = active_trade["tp2"]
    
    hit = None
    result = None
    
    if direction == "BUY":
        if price <= sl:
            hit = "SL"
            result = "LOSS"
        elif price >= tp1:
            hit = "TP1"
            result = "WIN"
    else:  # SELL
        if price >= sl:
            hit = "SL"
            result = "LOSS"
        elif price <= tp1:
            hit = "TP1"
            result = "WIN"
    
    if hit:
        # Calculate PnL
        if direction == "BUY":
            price_move = price - entry
        else:
            price_move = entry - price
        pnl = price_move * LOT_SIZE * 100 * PIP_VALUE
        
        # Record trade
        trade_record = {
            **active_trade,
            "exit_price": price,
            "exit_time": now_dubai().isoformat(),
            "result": result,
            "pnl": round(pnl, 2),
            "hit": hit,
        }
        trade_history.append(trade_record)
        active_trade = None
        save_data()
        
        # Send notification
        emoji = "🟢" if result == "WIN" else "🔴"
        msg = f"""{emoji} TRADE CLOSED - {result}!
━━━━━━━━━━━━━━━━━
📊 {direction} @ ${entry:,.2f}
🏁 Exit: ${price:,.2f} ({hit})
💰 PnL: ${pnl:+.2f}
━━━━━━━━━━━━━━━━━"""
        
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg)
        logger.info(f"Trade closed: {result} | PnL: ${pnl:+.2f}")


# ═══════════════════════════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════════════════════════
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    msg = f"""🤖 PawOo Gold Signal Bot v{VERSION}
━━━━━━━━━━━━━━━━━
📊 Strategy: RSI + SMC Hybrid Scalping
📈 Pair: XAU/USD (Gold)
🏛 Broker: XM Micro (GOLDm#)
🕐 Sessions: London & New York

Commands:
/start - Bot info
/scan - Force scan now
/price - Current price & RSI
/status - Bot status & performance
/close [price] - Close active trade

Bot scans every 30s during active sessions.
Signal requires RSI extreme + SMC 6/9 score.
━━━━━━━━━━━━━━━━━"""
    await update.message.reply_text(msg)


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /price command."""
    price = fetch_spot_price()
    fetch_candles()
    
    rsi = calculate_rsi(candles_m5, RSI_PERIOD)
    trend = detect_htf_trend(candles_h1)
    session = get_session()
    market = "🟢 OPEN" if is_market_open() else "🔴 CLOSED"
    
    rsi_str = f"{rsi}" if rsi else "N/A"
    price_str = f"${price:,.2f}" if price else "N/A"
    
    msg = f"""📊 Gold Price & Analysis
━━━━━━━━━━━━━━━━━
💰 Price: {price_str}
📉 RSI(14): {rsi_str}
📈 H1 Trend: {trend}
🕐 Session: {session}
{market} Market
📡 Data: {len(candles_m5)} M5 + {len(candles_h1)} H1 candles
━━━━━━━━━━━━━━━━━"""
    await update.message.reply_text(msg)


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scan command - force a scan."""
    price = fetch_spot_price()
    fetch_candles()
    
    if not price:
        await update.message.reply_text("❌ Cannot fetch price. Try again.")
        return
    
    rsi = calculate_rsi(candles_m5, RSI_PERIOD)
    trend = detect_htf_trend(candles_h1)
    session = get_session()
    
    # Run checklist for both directions
    buy_score, buy_checks = run_smc_checklist(price, "BUY")
    sell_score, sell_checks = run_smc_checklist(price, "SELL")
    
    best_dir = "BUY" if buy_score >= sell_score else "SELL"
    best_score = max(buy_score, sell_score)
    best_checks = buy_checks if buy_score >= sell_score else sell_checks
    
    checklist_text = ""
    for item in best_checks:
        emoji = "✅" if item["pass"] else "❌"
        checklist_text += f"\n{emoji} {item['name']}: {item['detail']}"
    
    status = "✅ Signal ready!" if best_score >= MIN_CHECKLIST_SCORE else f"⏳ Need {MIN_CHECKLIST_SCORE - best_score:.0f} more"
    
    msg = f"""🔍 Scan Result
━━━━━━━━━━━━━━━━━
💰 Gold: ${price:,.2f} | RSI: {rsi}
📈 Trend: {trend} | Session: {session}

📋 Best: {best_dir} ({best_score}/9) {status}
{checklist_text}

📡 Data: {len(candles_m5)} M5 + {len(candles_h1)} H1
━━━━━━━━━━━━━━━━━"""
    
    await update.message.reply_text(msg)
    
    # Auto-trigger signal if conditions met
    if best_score >= MIN_CHECKLIST_SCORE and is_market_open() and is_active_session():
        if not active_trade and not active_signal_msg_id:
            if time.time() >= cooldown_until and signal_count_today < MAX_DAILY_TRADES:
                await generate_signal(context, price)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    today = now_dubai().date().isoformat()
    today_trades = [t for t in trade_history if t.get("date") == today]
    total_trades = len([t for t in trade_history if t.get("result") in ("WIN", "LOSS")])
    wins = len([t for t in trade_history if t.get("result") == "WIN"])
    win_rate = (wins / total_trades * 100) if total_trades else 0
    total_pnl = sum(t.get("pnl", 0) for t in trade_history)
    today_pnl = get_daily_pnl()
    
    market = "🟢 OPEN" if is_market_open() else "🔴 CLOSED"
    session = get_session()
    active = "Active" if is_active_session() else "Inactive"
    
    trade_status = ""
    if active_trade:
        price = fetch_spot_price()
        if price:
            d = active_trade["direction"]
            entry = active_trade["entry"]
            move = (price - entry) if d == "BUY" else (entry - price)
            pnl = move * LOT_SIZE * 100 * PIP_VALUE
            trade_status = f"\n\n📊 Active: {d} @ ${entry:,.2f}\n💰 Current PnL: ${pnl:+.2f}"
    
    msg = f"""📊 Bot Status - PawOo v{VERSION}
━━━━━━━━━━━━━━━━━
{market} | {session} ({active})
📈 Signals today: {signal_count_today}/{MAX_DAILY_TRADES}
💰 Today PnL: ${today_pnl:+.2f}

📊 All-time Performance:
🎯 Win Rate: {win_rate:.0f}% ({wins}/{total_trades})
💰 Total PnL: ${total_pnl:+.2f}
📝 Total Signals: {len(signal_log)}{trade_status}
━━━━━━━━━━━━━━━━━"""
    
    await update.message.reply_text(msg)


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /close command - manually close active trade."""
    global active_trade
    
    if not active_trade:
        await update.message.reply_text("❌ No active trade to close.")
        return
    
    # Get close price
    text = update.message.text.strip()
    parts = text.split()
    
    if len(parts) > 1:
        try:
            close_price = float(parts[1].replace("$", "").replace(",", ""))
        except:
            close_price = fetch_spot_price()
    else:
        close_price = fetch_spot_price()
    
    if not close_price:
        await update.message.reply_text("❌ Cannot get price. Try: /close 4550.00")
        return
    
    direction = active_trade["direction"]
    entry = active_trade["entry"]
    
    if direction == "BUY":
        price_move = close_price - entry
    else:
        price_move = entry - close_price
    pnl = price_move * LOT_SIZE * 100 * PIP_VALUE
    
    result = "WIN" if pnl > 0 else "LOSS"
    
    trade_record = {
        **active_trade,
        "exit_price": close_price,
        "exit_time": now_dubai().isoformat(),
        "result": result,
        "pnl": round(pnl, 2),
        "hit": "Manual",
    }
    trade_history.append(trade_record)
    active_trade = None
    save_data()
    
    emoji = "🟢" if result == "WIN" else "🔴"
    msg = f"""{emoji} Trade Manually Closed - {result}
━━━━━━━━━━━━━━━━━
📊 {direction} @ ${entry:,.2f} → ${close_price:,.2f}
💰 PnL: ${pnl:+.2f}
━━━━━━━━━━━━━━━━━"""
    await update.message.reply_text(msg)


# ═══════════════════════════════════════════════════════════════
# MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle general text messages."""
    if not update.message or not update.message.text:
        return
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    
    text = update.message.text.lower().strip()
    
    # Quick status on greeting
    if any(w in text for w in ["hi", "hello", "yo", "bot", "status", "update"]):
        price = fetch_spot_price()
        fetch_candles()
        rsi = calculate_rsi(candles_m5, RSI_PERIOD)
        trend = detect_htf_trend(candles_h1)
        session = get_session()
        market = "OPEN" if is_market_open() else "CLOSED"
        active_str = "Active" if is_active_session() else "Inactive"
        
        price_str = f"${price:,.2f}" if price else "N/A"
        rsi_str = f"{rsi}" if rsi else "N/A"
        
        msg = f"""🔍 PawOo v{VERSION} - ACTIVE & SCANNING!
━━━━━━━━━━━━━━━━━
💰 Gold: {price_str} | RSI: {rsi_str}
📈 Trend: {trend}
🕐 {session} ({active_str}) | Market {market}
📊 Trades: {signal_count_today}/{MAX_DAILY_TRADES} | PnL: ${get_daily_pnl():+.2f}
📡 Data: {len(candles_m5)} M5 + {len(candles_h1)} H1
━━━━━━━━━━━━━━━━━"""
        await update.message.reply_text(msg)
        
        # Auto-scan on wake-up
        if price and is_market_open() and is_active_session():
            if not active_trade and not active_signal_msg_id:
                await generate_signal(context, price)


# ═══════════════════════════════════════════════════════════════
# AUTO-SCANNER (Background Task)
# ═══════════════════════════════════════════════════════════════
async def auto_scanner(context: ContextTypes.DEFAULT_TYPE):
    """Background task: scan market every 30 seconds."""
    reset_daily_counters()
    
    if not is_market_open():
        return
    
    # Fetch data
    price = fetch_spot_price()
    if not price:
        return
    
    # Fetch candles every 2 minutes
    if time.time() - last_candle_fetch >= 120:
        fetch_candles()
    
    # Monitor active trade
    if active_trade:
        await monitor_active_trade(context, price)
        return
    
    # Only generate signals during active sessions
    if not is_active_session():
        return
    
    # Try to generate signal
    await generate_signal(context, price)


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    """Start the bot."""
    logger.info(f"═══ PawOo Gold Signal Bot v{VERSION} Starting ═══")
    logger.info(f"Strategy: RSI({RSI_PERIOD}) + SMC | Sessions: London/NY")
    logger.info(f"Risk: {LOT_SIZE} lot | SL: ${SL_DOLLARS} | TP1: ${TP1_DOLLARS} | TP2: ${TP2_DOLLARS}")
    
    # Load saved data
    load_data()
    logger.info(f"Loaded {len(trade_history)} trades, {len(signal_log)} signals")
    
    # Initial data fetch
    fetch_spot_price()
    fetch_candles()
    logger.info(f"Initial data: {len(candles_m5)} M5 + {len(candles_h1)} H1 candles")
    
    # Build application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("close", close_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Add auto-scanner job (every 30 seconds)
    app.job_queue.run_repeating(auto_scanner, interval=SCAN_INTERVAL, first=5)
    
    logger.info("Bot started! Scanning every 30 seconds during London/NY sessions.")
    
    # Send startup message
    async def send_startup():
        try:
            bot = app.bot
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🟢 PawOo v{VERSION} Started!\n📊 RSI+SMC Hybrid | London/NY sessions\n📡 {len(candles_m5)} M5 + {len(candles_h1)} H1 candles loaded"
            )
        except:
            pass
    
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Run bot
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
