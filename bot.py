
import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
import pytz
import numpy as np
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Configuration --- #
BOT_TOKEN = os.environ.get('BOT_TOKEN', '8785447693:AAHieuYnespi21eYPIxQn-rEPQ0D6qCZIJ0')
ADMIN_CHAT_ID = int(os.environ.get('ADMIN_CHAT_ID', '5948621771'))
DATA_DIR = os.environ.get('DATA_DIR', './data')

TRADE_JOURNAL_PATH = os.path.join(DATA_DIR, 'trade_journal.json')
LEARNING_STATE_PATH = os.path.join(DATA_DIR, 'learning_state.json')
WEEKLY_REPORTS_DIR = os.path.join(DATA_DIR, 'weekly_reports')

# Trading Parameters
SL_AMOUNT = 5.0  # $5
TP_AMOUNT = 8.0  # $8
MAX_TRADES_PER_DAY = 5
COOLDOWN_MINUTES = 10
TRADE_TIMEOUT_HOURS = 4 # 48 M5 candles

# Timezones
DUBAI_TZ = pytz.timezone('Asia/Dubai')
UTC_TZ = pytz.utc

# Market Hours (Dubai Time)
MARKET_OPEN_MONDAY = DUBAI_TZ.localize(datetime(2000, 1, 1, 2, 5, 0))
MARKET_CLOSE_SATURDAY = DUBAI_TZ.localize(datetime(2000, 1, 1, 0, 55, 0))
DAILY_BREAK_START = DUBAI_TZ.localize(datetime(2000, 1, 1, 0, 55, 0))
DAILY_BREAK_END = DUBAI_TZ.localize(datetime(2000, 1, 1, 2, 5, 0))

# Sessions (Dubai Time)
SESSION_ASIAN_START = DUBAI_TZ.localize(datetime(2000, 1, 1, 2, 5, 0))
SESSION_ASIAN_END = DUBAI_TZ.localize(datetime(2000, 1, 1, 10, 0, 0))
SESSION_LONDON_START = DUBAI_TZ.localize(datetime(2000, 1, 1, 11, 0, 0))
SESSION_LONDON_END = DUBAI_TZ.localize(datetime(2000, 1, 1, 16, 0, 0))
SESSION_NY_START = DUBAI_TZ.localize(datetime(2000, 1, 1, 16, 0, 0))
SESSION_NY_END = DUBAI_TZ.localize(datetime(2000, 1, 1, 0, 55, 0)) # Next day for calculation

# Confidence Scoring Initial Values
INITIAL_CONFIDENCE = 60
CONFIDENCE_ADJUST_WIN = 3
CONFIDENCE_ADJUST_LOSS = -5
MIN_CONFIDENCE_THRESHOLD = 40
MAX_CONFIDENCE_THRESHOLD = 80

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Data Storage & Management --- #
def load_json(filepath, default_data):
    if not os.path.exists(filepath):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w') as f:
            json.dump(default_data, f, indent=4)
        return default_data
    with open(filepath, 'r') as f:
        return json.load(f)

def save_json(filepath, data):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=4)

class BotState:
    def __init__(self):
        self.trade_journal = load_json(TRADE_JOURNAL_PATH, [])
        self.learning_state = load_json(LEARNING_STATE_PATH, self._get_initial_learning_state())
        self.active_trades = {}
        self.last_signal_time = None
        self.today_trades_count = 0
        self.today_wins = 0
        self._reset_daily_stats()

    def _get_initial_learning_state(self):
        return {
            'confidence_scores': {
                'EMA_aligned': INITIAL_CONFIDENCE,
                'price_near_EMA21': INITIAL_CONFIDENCE,
                'candle_pattern': INITIAL_CONFIDENCE,
                'RSI_neutral': INITIAL_CONFIDENCE,
                'momentum': INITIAL_CONFIDENCE,
            },
            'current_confidence_threshold': 55, # Initial threshold (must be <= INITIAL_CONFIDENCE)
            'adaptive_threshold_settings': {
                'base_threshold': 55,
                'tight_filter_active': False,
                'extra_filter_active': False,
            },
            'session_performance': {
                'Asian': {'trades': 0, 'wins': 0, 'confidence_modifier': 0},
                'London': {'trades': 0, 'wins': 0, 'confidence_modifier': 0},
                'NY': {'trades': 0, 'wins': 0, 'confidence_modifier': 0},
            },
            'last_weekly_review': datetime.now(DUBAI_TZ).isoformat()
        }

    def _reset_daily_stats(self):
        now = datetime.now(DUBAI_TZ)
        if not hasattr(self, '_last_daily_reset') or self._last_daily_reset.date() != now.date():
            self.today_trades_count = 0
            self.today_wins = 0
            self._last_daily_reset = now

    def record_trade(self, trade_entry):
        self.trade_journal.append(trade_entry)
        save_json(TRADE_JOURNAL_PATH, self.trade_journal)
        self.today_trades_count += 1
        if trade_entry['result'] == 'WIN':
            self.today_wins += 1
        self.update_learning_state(trade_entry)

    def update_learning_state(self, trade_entry):
        # Update confidence scores
        for condition, present in trade_entry['entry_conditions_met'].items():
            if present and condition in self.learning_state['confidence_scores']:
                if trade_entry['result'] == 'WIN':
                    self.learning_state['confidence_scores'][condition] += CONFIDENCE_ADJUST_WIN
                elif trade_entry['result'] == 'LOSS':
                    self.learning_state['confidence_scores'][condition] += CONFIDENCE_ADJUST_LOSS
                # Ensure scores stay within a reasonable range (e.g., 0-100)
                self.learning_state['confidence_scores'][condition] = max(0, min(100, self.learning_state['confidence_scores'][condition]))

        # Update session performance
        session = trade_entry['session']
        if session in self.learning_state['session_performance']:
            self.learning_state['session_performance'][session]['trades'] += 1
            if trade_entry['result'] == 'WIN':
                self.learning_state['session_performance'][session]['wins'] += 1

        # Adjust adaptive thresholds
        self._adjust_adaptive_thresholds()

        save_json(LEARNING_STATE_PATH, self.learning_state)

    def _adjust_adaptive_thresholds(self):
        recent_trades = self.trade_journal[-20:]
        last_10_trades = recent_trades[-10:]
        last_20_trades = recent_trades

        wr_10 = (sum(1 for t in last_10_trades if t['result'] == 'WIN') / len(last_10_trades)) * 100 if last_10_trades else 0
        wr_20 = (sum(1 for t in last_20_trades if t['result'] == 'WIN') / len(last_20_trades)) * 100 if last_20_trades else 0

        current_threshold = self.learning_state['adaptive_threshold_settings']['base_threshold']
        tight_filter_active = self.learning_state['adaptive_threshold_settings']['tight_filter_active']
        extra_filter_active = self.learning_state['adaptive_threshold_settings']['extra_filter_active']

        # Tighten entry if WR < 40% (last 10 trades)
        if wr_10 < 40 and not tight_filter_active:
            current_threshold += 10 # Make it harder to trigger
            self.learning_state['adaptive_threshold_settings']['tight_filter_active'] = True
            logger.info("Adaptive system: Tightening entry due to 10-trade WR < 40%.")
        elif wr_10 >= 55 and tight_filter_active:
            current_threshold -= 5 # Relax slightly
            self.learning_state['adaptive_threshold_settings']['tight_filter_active'] = False
            logger.info("Adaptive system: Relaxing entry due to 10-trade WR > 55%.")

        # Pause trading if WR < 35% (last 20 trades)
        if wr_20 < 35 and not extra_filter_active:
            self.learning_state['adaptive_threshold_settings']['extra_filter_active'] = True
            self.learning_state['current_confidence_threshold'] = 1000 # Effectively pause trading
            logger.warning("Adaptive system: Pausing trading for 1 hour due to 20-trade WR < 35%.")
            asyncio.create_task(self._resume_trading_after_pause())
        elif wr_20 >= 35 and extra_filter_active:
            self.learning_state['adaptive_threshold_settings']['extra_filter_active'] = False
            current_threshold = self.learning_state['adaptive_threshold_settings']['base_threshold'] # Reset to base
            logger.info("Adaptive system: Resuming trading as 20-trade WR improved.")

        self.learning_state['current_confidence_threshold'] = max(MIN_CONFIDENCE_THRESHOLD, min(MAX_CONFIDENCE_THRESHOLD, current_threshold))

    async def _resume_trading_after_pause(self):
        await asyncio.sleep(3600) # Wait for 1 hour
        self.learning_state['adaptive_threshold_settings']['extra_filter_active'] = False
        self.learning_state['current_confidence_threshold'] = self.learning_state['adaptive_threshold_settings']['base_threshold']
        save_json(LEARNING_STATE_PATH, self.learning_state)
        logger.info("Adaptive system: Trading resumed after 1-hour pause.")
        await application.bot.send_message(chat_id=ADMIN_CHAT_ID, text="Trading resumed after 1-hour pause due to low 20-trade WR. Settings reset to base.")

    def get_session_confidence_modifier(self, session_name):
        session_data = self.learning_state['session_performance'].get(session_name)
        if not session_data or session_data['trades'] < 5: # Need enough data
            return 0
        wr = (session_data['wins'] / session_data['trades']) * 100
        if wr > 60: return 5
        if wr < 40: return -5
        return 0

    def weekly_self_review(self):
        now = datetime.now(DUBAI_TZ)
        last_review_str = self.learning_state['last_weekly_review']
        last_review = datetime.fromisoformat(last_review_str).astimezone(DUBAI_TZ)

        if now.weekday() == 6 and (now - last_review).days >= 6: # Sunday and at least a week passed
            logger.info("Performing weekly self-review...")
            report_filename = os.path.join(WEEKLY_REPORTS_DIR, f"weekly_report_{now.strftime('%Y%m%d')}.json")
            weekly_trades = [t for t in self.trade_journal if datetime.fromisoformat(t['timestamp']).astimezone(DUBAI_TZ) > last_review]

            # Calculate per-session stats
            session_stats = {'Asian': {'trades': 0, 'wins': 0, 'wr': 0}, 'London': {'trades': 0, 'wins': 0, 'wr': 0}, 'NY': {'trades': 0, 'wins': 0, 'wr': 0}}
            for trade in weekly_trades:
                session = trade['session']
                if session in session_stats:
                    session_stats[session]['trades'] += 1
                    if trade['result'] == 'WIN':
                        session_stats[session]['wins'] += 1
            for session, data in session_stats.items():
                if data['trades'] > 0:
                    session_stats[session]['wr'] = (data['wins'] / data['trades']) * 100

            # Identify worst-performing conditions (simple approach: lowest average confidence for losses)
            condition_performance = {cond: {'wins': 0, 'losses': 0} for cond in self.learning_state['confidence_scores']}
            for trade in weekly_trades:
                for cond, present in trade['entry_conditions_met'].items():
                    if present and cond in condition_performance:
                        if trade['result'] == 'WIN':
                            condition_performance[cond]['wins'] += 1
                        elif trade['result'] == 'LOSS':
                            condition_performance[cond]['losses'] += 1
            
            worst_conditions = sorted(condition_performance.items(), key=lambda item: item[1]['losses'] - item[1]['wins'], reverse=True)

            # Auto-adjust parameters (example: reset base threshold if overall WR is good)
            overall_wr = (sum(1 for t in weekly_trades if t['result'] == 'WIN') / len(weekly_trades)) * 100 if weekly_trades else 0
            if overall_wr > 50:
                self.learning_state['adaptive_threshold_settings']['base_threshold'] = 60 # Reset to default good
            else:
                self.learning_state['adaptive_threshold_settings']['base_threshold'] = 70 # Tighten for next week

            report_data = {
                'review_date': now.isoformat(),
                'period_start': last_review_str,
                'period_end': now.isoformat(),
                'total_trades': len(weekly_trades),
                'overall_win_rate': overall_wr,
                'session_stats': session_stats,
                'condition_performance': condition_performance,
                'worst_performing_conditions': [wc[0] for wc in worst_conditions[:3]],
                'new_base_threshold': self.learning_state['adaptive_threshold_settings']['base_threshold']
            }
            save_json(report_filename, report_data)
            self.learning_state['last_weekly_review'] = now.isoformat()
            save_json(LEARNING_STATE_PATH, self.learning_state)

            report_text = f"Weekly Self-Review Report ({last_review.strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')})\n\n"
            report_text += f"Overall Win Rate: {overall_wr:.2f}% ({len(weekly_trades)} trades)\n\n"
            report_text += "Session Performance:\n"
            for session, data in session_stats.items():
                report_text += f"  {session}: {data['trades']} trades, {data['wins']} wins, {data['wr']:.2f}% WR\n"
            report_text += "\nTop 3 Worst Performing Conditions (based on win/loss difference):\n"
            for wc in worst_conditions[:3]:
                report_text += f"  - {wc[0]} (Wins: {wc[1]['wins']}, Losses: {wc[1]['losses']})\n"
            report_text += f"\nNew Base Confidence Threshold for next week: {self.learning_state['adaptive_threshold_settings']['base_threshold']}%\n"

            asyncio.create_task(application.bot.send_message(chat_id=ADMIN_CHAT_ID, text=report_text))
            logger.info("Weekly self-review completed and report sent.")

# Global bot state instance
bot_state = BotState()

# --- Technical Analysis & Price Data --- #
# Cache for candle data to avoid excessive API calls
_candles_m5 = []
_candles_h1 = []
_last_candle_fetch = 0
_cached_price = None

async def get_yahoo_finance_data(symbol='GC=F', interval='5m', range_param='5d'):
    """Fetch real candle data from Yahoo Finance GC=F (Gold Futures)."""
    global _candles_m5, _candles_h1, _last_candle_fetch
    import time as _time
    
    now = _time.time()
    # Cache for 60 seconds to avoid rate limiting
    if now - _last_candle_fetch < 60 and ((interval == '5m' and _candles_m5) or (interval in ('60m', '1h') and _candles_h1)):
        if interval == '5m':
            return _candles_m5
        else:
            return _candles_h1
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    # Determine URL parameters
    if interval == '5m':
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=5d"
    elif interval in ('60m', '1h'):
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1h&range=1mo"
    else:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={range_param}"
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()["chart"]["result"][0]
            ts = data["timestamp"]
            q = data["indicators"]["quote"][0]
            candles = []
            for i in range(len(ts)):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if any(v is None for v in (o, h, l, c)):
                    continue
                if o == h == l == c:
                    continue
                candles.append({
                    'timestamp': ts[i],
                    'open': float(o),
                    'high': float(h),
                    'low': float(l),
                    'close': float(c),
                    'volume': int(q.get('volume', [0]*len(ts))[i] or 0)
                })
            
            if candles:
                if interval == '5m':
                    _candles_m5 = candles
                    logger.info(f"M5: {len(_candles_m5)} candles fetched")
                else:
                    _candles_h1 = candles
                    logger.info(f"H1: {len(_candles_h1)} candles fetched")
                _last_candle_fetch = now
                return candles
            else:
                logger.warning(f"No valid candles parsed for {interval}")
        else:
            logger.error(f"Yahoo Finance returned status {r.status_code} for {interval}")
    except Exception as e:
        logger.error(f"Error fetching {interval} data: {e}")
    
    # Return cached data if fetch fails
    if interval == '5m':
        return _candles_m5
    return _candles_h1

async def get_current_price():
    """Get current spot price from Yahoo Finance."""
    global _cached_price
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d",
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            price = float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
            _cached_price = price
            return price
    except Exception as e:
        logger.warning(f"Price fetch failed: {e}")
    return _cached_price

def calculate_ema(prices, period):
    if len(prices) < period:
        return [np.nan] * len(prices)
    ema = [np.nan] * len(prices)
    sma = np.mean(prices[:period])
    ema[period - 1] = sma
    multiplier = 2 / (period + 1)
    for i in range(period, len(prices)):
        ema[i] = (prices[i] - ema[i-1]) * multiplier + ema[i-1]
    return ema

def calculate_rsi(prices, period=14):
    """Wilder's smoothed RSI - matches MT5/TradingView."""
    if len(prices) < period + 1:
        return [np.nan] * len(prices)

    rsi_values = [np.nan] * len(prices)
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Initial SMA for first RSI value
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        rsi_values[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi_values[period] = 100.0 - (100.0 / (1.0 + rs))

    # Wilder's smoothing for subsequent values
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_values[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_values[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    return rsi_values

def is_bullish_candle(open_price, close_price, high_price, low_price):
    return close_price > open_price and (close_price - open_price) > (high_price - low_price) * 0.3 # Body is at least 30% of range

def is_bearish_candle(open_price, close_price, high_price, low_price):
    return close_price < open_price and (open_price - close_price) > (high_price - low_price) * 0.3 # Body is at least 30% of range

def get_current_session(dt_dubai):
    time_only = dt_dubai.replace(year=2000, month=1, day=1)
    if SESSION_ASIAN_START <= time_only <= SESSION_ASIAN_END:
        return 'Asian'
    elif SESSION_LONDON_START <= time_only <= SESSION_LONDON_END:
        return 'London'
    elif SESSION_NY_START <= time_only or time_only <= SESSION_NY_END: # NY session can cross midnight
        return 'NY'
    return 'Unknown'

def is_market_open(dt_dubai):
    # Check if it's Sunday
    if dt_dubai.weekday() == 6: # Sunday
        return False

    # Check daily break
    time_only = dt_dubai.replace(year=2000, month=1, day=1)
    if DAILY_BREAK_START <= time_only < DAILY_BREAK_END:
        return False

    # Check Saturday close
    if dt_dubai.weekday() == 5 and time_only >= MARKET_CLOSE_SATURDAY.replace(year=2000, month=1, day=1):
        return False

    # Check Monday open
    if dt_dubai.weekday() == 0 and time_only < MARKET_OPEN_MONDAY.replace(year=2000, month=1, day=1):
        return False

    return True

# --- ALPHA Strategy Logic --- #
async def check_alpha_strategy(current_candle, m5_candles, h1_candles):
    if len(m5_candles) < 50 or len(h1_candles) < 50: # Need enough data for EMAs
        return None, None, 0, None, None, None  # No signal

    closes_m5 = np.array([c['close'] for c in m5_candles])
    opens_m5 = np.array([c['open'] for c in m5_candles])
    highs_m5 = np.array([c['high'] for c in m5_candles])
    lows_m5 = np.array([c['low'] for c in m5_candles])

    ema9_m5 = calculate_ema(closes_m5, 9)
    ema21_m5 = calculate_ema(closes_m5, 21)
    ema50_m5 = calculate_ema(closes_m5, 50)
    rsi_m5 = calculate_rsi(closes_m5, 14)

    # Get latest values
    latest_close = current_candle['close']
    latest_open = current_candle['open']
    latest_high = current_candle['high']
    latest_low = current_candle['low']
    latest_ema9 = ema9_m5[-1]
    latest_ema21 = ema21_m5[-1]
    latest_ema50 = ema50_m5[-1]
    latest_rsi = rsi_m5[-1]

    # Check for NaN values in indicators
    if any(np.isnan([latest_ema9, latest_ema21, latest_ema50, latest_rsi])):
        return None, None, 0, None, None, None  # NaN indicators

    entry_conditions_met = {
        'EMA_aligned': False,
        'price_near_EMA21': False,
        'candle_pattern': False,
        'RSI_neutral': False,
        'momentum': False,
    }
    reasons = []
    signal_type = None

    # --- BUY Conditions ---
    if (latest_ema9 > latest_ema21 > latest_ema50): # Aligned uptrend
        entry_conditions_met['EMA_aligned'] = True
        reasons.append('EMA9 > EMA21 > EMA50 ✅')

        # Price near EMA21: candle low touches/near EMA21 zone AND close above EMA21
        if latest_low <= latest_ema21 + 4.0 and latest_close > latest_ema21:
            entry_conditions_met['price_near_EMA21'] = True
            reasons.append(f'Price bounced near EMA21 (low ${latest_low:.2f} near EMA21 ${latest_ema21:.2f}) ✅')

            if is_bullish_candle(latest_open, latest_close, latest_high, latest_low): # Bullish candle
                entry_conditions_met['candle_pattern'] = True
                reasons.append('Bullish candle ✅')

                if 35 <= latest_rsi <= 65: # RSI neutral zone
                    entry_conditions_met['RSI_neutral'] = True
                    reasons.append(f'RSI neutral zone ({latest_rsi:.1f}) ✅')

                    if latest_close > closes_m5[-4]: # Momentum (close > close[3])
                        entry_conditions_met['momentum'] = True
                        reasons.append('Momentum positive (close > close[3]) ✅')

                        # All BUY conditions met
                        signal_type = 'BUY'

    # --- SELL Conditions ---
    elif (latest_ema9 < latest_ema21 < latest_ema50): # Aligned downtrend
        entry_conditions_met['EMA_aligned'] = True
        reasons.append('EMA9 < EMA21 < EMA50 ✅')

        # Price near EMA21: candle high touches/near EMA21 zone AND close below EMA21
        if latest_high >= latest_ema21 - 4.0 and latest_close < latest_ema21:
            entry_conditions_met['price_near_EMA21'] = True
            reasons.append(f'Price rejected near EMA21 (high ${latest_high:.2f} near EMA21 ${latest_ema21:.2f}) ✅')

            if is_bearish_candle(latest_open, latest_close, latest_high, latest_low): # Bearish candle
                entry_conditions_met['candle_pattern'] = True
                reasons.append('Bearish candle ✅')

                if 35 <= latest_rsi <= 65: # RSI neutral zone
                    entry_conditions_met['RSI_neutral'] = True
                    reasons.append(f'RSI neutral zone ({latest_rsi:.1f}) ✅')

                    if latest_close < closes_m5[-4]: # Momentum (close < close[3])
                        entry_conditions_met['momentum'] = True
                        reasons.append('Momentum negative (close < close[3]) ✅')

                        # All SELL conditions met
                        signal_type = 'SELL'

    # Calculate total confidence score for the signal
    total_confidence = 0
    present_conditions_count = 0
    for condition, met in entry_conditions_met.items():
        if met:
            total_confidence += bot_state.learning_state['confidence_scores'].get(condition, INITIAL_CONFIDENCE)
            present_conditions_count += 1
    
    if present_conditions_count > 0:
        total_confidence = total_confidence / present_conditions_count
    else:
        total_confidence = 0

    # Apply session confidence modifier
    current_dubai_time = datetime.now(DUBAI_TZ)
    session_name = get_current_session(current_dubai_time)
    session_modifier = bot_state.get_session_confidence_modifier(session_name)
    total_confidence += session_modifier

    return signal_type, reasons, total_confidence, latest_rsi, session_name, entry_conditions_met

# --- Telegram Bot Functions --- #
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_html(
        f"Hi {user.mention_html()}! I am the PawOo Gold Signal Bot (v5.0). "
        "I send XAU/USD BUY/SELL signals based on the ALPHA strategy with a self-learning adaptive system. "
        "Use /help to see available commands."
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Scanning for signals... Please wait.")
    await auto_scan_for_signals(context, manual_scan=True, chat_id=update.effective_chat.id)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_state._reset_daily_stats()
    today_wr = (bot_state.today_wins / bot_state.today_trades_count) * 100 if bot_state.today_trades_count > 0 else 0
    last_10_trades = bot_state.trade_journal[-10:]
    wr_10 = (sum(1 for t in last_10_trades if t['result'] == 'WIN') / len(last_10_trades)) * 100 if last_10_trades else 0

    status_text = f"📊 Bot Status 📊\n\n"
    status_text += f"Today's Trades: {bot_state.today_wins}/{bot_state.today_trades_count} ({today_wr:.2f}% WR)\n"
    status_text += f"Last 10 Trades WR: {wr_10:.2f}%\n"
    status_text += f"Current Confidence Threshold: {bot_state.learning_state['current_confidence_threshold']:.2f}%\n"
    status_text += f"Trading Paused: {'Yes' if bot_state.learning_state['adaptive_threshold_settings']['extra_filter_active'] else 'No'}\n"
    status_text += f"Last Signal Time: {bot_state.last_signal_time.strftime('%Y-%m-%d %H:%M:%S %Z') if bot_state.last_signal_time else 'N/A'}\n"
    
    current_dubai_time = datetime.now(DUBAI_TZ)
    status_text += f"Market Open: {is_market_open(current_dubai_time)}\n"
    status_text += f"Current Session: {get_current_session(current_dubai_time)}\n"

    await update.message.reply_text(status_text)

async def journal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    journal_entries = bot_state.trade_journal[-10:] # Last 10 trades
    if not journal_entries:
        await update.message.reply_text("Trade journal is empty.")
        return

    journal_text = "📜 Last 10 Trade Journal Entries 📜\n\n"
    for entry in journal_entries:
        journal_text += f"Time: {entry['timestamp']}\n"
        journal_text += f"Type: {entry['signal_type']} @ {entry['entry_price']:.2f}\n"
        journal_text += f"Result: {entry['result']} (PnL: {entry['pnl']:.2f}, Bars Held: {entry['bars_held']})\n"
        journal_text += f"Confidence: {entry['signal_confidence']:.2f}%\n"
        journal_text += f"Session: {entry['session']}\n"
        journal_text += f"---\n"
    await update.message.reply_text(journal_text)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    total_trades = len(bot_state.trade_journal)
    total_wins = sum(1 for t in bot_state.trade_journal if t['result'] == 'WIN')
    overall_wr = (total_wins / total_trades) * 100 if total_trades > 0 else 0

    stats_text = f"📈 Overall Statistics 📈\n\n"
    stats_text += f"Total Trades: {total_trades}\n"
    stats_text += f"Overall Win Rate: {overall_wr:.2f}%\n\n"

    stats_text += "Session Breakdown:\n"
    for session, data in bot_state.learning_state['session_performance'].items():
        session_wr = (data['wins'] / data['trades']) * 100 if data['trades'] > 0 else 0
        stats_text += f"  {session}: {data['trades']} trades, {data['wins']} wins, {session_wr:.2f}% WR (Confidence Modifier: {bot_state.get_session_confidence_modifier(session)}%)\n"
    
    await update.message.reply_text(stats_text)

async def learn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    learn_text = f"🧠 Current Learning State 🧠\n\n"
    learn_text += f"Confidence Scores (Higher = More Important):\n"
    for condition, score in bot_state.learning_state['confidence_scores'].items():
        learn_text += f"  - {condition}: {score:.2f}%\n"
    learn_text += f"\nCurrent Signal Confidence Threshold: {bot_state.learning_state['current_confidence_threshold']:.2f}%\n"
    learn_text += f"Base Adaptive Threshold: {bot_state.learning_state['adaptive_threshold_settings']['base_threshold']:.2f}%\n"
    learn_text += f"Tight Filter Active: {bot_state.learning_state['adaptive_threshold_settings']['tight_filter_active']}\n"
    learn_text += f"Extra Filter Active (Trading Paused): {bot_state.learning_state['adaptive_threshold_settings']['extra_filter_active']}\n"
    learn_text += f"Last Weekly Review: {datetime.fromisoformat(bot_state.learning_state['last_weekly_review']).astimezone(DUBAI_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}\n"

    await update.message.reply_text(learn_text)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("You are not authorized to use this command.")
        return
    
    bot_state.trade_journal = []
    bot_state.learning_state = bot_state._get_initial_learning_state()
    bot_state.active_trades = {}
    bot_state.last_signal_time = None
    bot_state._reset_daily_stats()
    save_json(TRADE_JOURNAL_PATH, bot_state.trade_journal)
    save_json(LEARNING_STATE_PATH, bot_state.learning_state)
    await update.message.reply_text("Learning data has been reset.")
    logger.info("Learning data reset by admin.")

async def send_signal_message(chat_id, signal_type, entry_price, tp, sl, confidence, threshold, trend, rsi_val, session, reasons, learning_stats):
    emoji = "🟢" if signal_type == 'BUY' else "🔴"
    message_text = f"{emoji} {signal_type} SIGNAL - XAU/USD\n\n"
    message_text += f"📊 Entry: ${entry_price:.2f}\n"
    message_text += f"🎯 TP: ${tp:.2f} (+${TP_AMOUNT})\n"
    message_text += f"🛑 SL: ${sl:.2f} (-${SL_AMOUNT})\n"
    message_text += f"📐 R:R = 1:1.6\n\n"
    message_text += f"📋 Strategy: EMA21 Bounce (ALPHA)\n"
    message_text += f"💪 Confidence: {confidence:.2f}% (threshold: {threshold:.2f}%)\n"
    message_text += f"📈 Trend: {trend}\n"
    message_text += f"📊 RSI(14): {rsi_val:.1f}\n"
    message_text += f"⏰ Session: {session}\n\n"
    message_text += f"📝 Entry Reasons:\n"
    for reason in reasons:
        message_text += f"• {reason}\n"
    message_text += f"\n📊 Learning Stats:\n"
    message_text += f"• Today: {learning_stats['today_wins']}/{learning_stats['today_trades']} ({learning_stats['today_wr']:.2f}% WR)\n"
    message_text += f"• Last 10: {learning_stats['last_10_wr']:.2f}% WR\n"
    message_text += f"• This session avg: {learning_stats['session_wr']:.2f}% WR\n\n"
    message_text += f"⚠️ Risk: 0.5 lot | Max loss ${SL_AMOUNT}"

    await application.bot.send_message(chat_id=chat_id, text=message_text)

async def send_result_notification(chat_id, trade_entry):
    emoji = "✅" if trade_entry['result'] == 'WIN' else "❌" if trade_entry['result'] == 'LOSS' else "⚠️"
    message_text = f"{emoji} TRADE RESULT - XAU/USD {trade_entry['signal_type']}\n\n"
    message_text += f"Entry: ${trade_entry['entry_price']:.2f}\n"
    message_text += f"Exit: ${trade_entry['exit_price']:.2f}\n"
    message_text += f"Result: {trade_entry['result']}\n"
    message_text += f"PnL: ${trade_entry['pnl']:.2f}\n"
    message_text += f"Bars Held: {trade_entry['bars_held']}\n"
    message_text += f"Timestamp: {trade_entry['timestamp']}\n"
    await application.bot.send_message(chat_id=chat_id, text=message_text)

# --- Auto-Scanning and Trade Monitoring --- #
async def auto_scan_for_signals(context: ContextTypes.DEFAULT_TYPE, manual_scan=False, chat_id=None) -> None:
    job = context.job
    if not manual_scan:
        chat_id = ADMIN_CHAT_ID # Default for auto-scan

    current_dubai_time = datetime.now(DUBAI_TZ)
    if not is_market_open(current_dubai_time):
        logger.info("Market is closed or on daily break. Skipping scan.")
        if manual_scan:
            await application.bot.send_message(chat_id=chat_id, text="Market is currently closed or on daily break. Cannot scan for signals.")
        return

    bot_state._reset_daily_stats()

    # Cooldown check
    if bot_state.last_signal_time and (current_dubai_time - bot_state.last_signal_time).total_seconds() < COOLDOWN_MINUTES * 60:
        logger.info(f"Cooldown active. Next signal possible in {int(COOLDOWN_MINUTES - (current_dubai_time - bot_state.last_signal_time).total_seconds() / 60)} minutes.")
        if manual_scan:
            await application.bot.send_message(chat_id=chat_id, text=f"Cooldown active. Please wait {int(COOLDOWN_MINUTES - (current_dubai_time - bot_state.last_signal_time).total_seconds() / 60)} minutes before scanning again.")
        return

    # Max trades per day check
    if bot_state.today_trades_count >= MAX_TRADES_PER_DAY:
        logger.info(f"Max trades ({MAX_TRADES_PER_DAY}) reached for today. Skipping scan.")
        if manual_scan:
            await application.bot.send_message(chat_id=chat_id, text=f"Max trades ({MAX_TRADES_PER_DAY}) reached for today. No more signals will be sent.")
        return

    # Check if trading is paused by adaptive system
    if bot_state.learning_state['adaptive_threshold_settings']['extra_filter_active']:
        logger.warning("Trading is paused by adaptive system due to low 20-trade WR. Skipping scan.")
        if manual_scan:
            await application.bot.send_message(chat_id=chat_id, text="Trading is currently paused by the adaptive system due to low 20-trade win rate. Please check /learn for details.")
        return

    # Fetch price data
    m5_candles = await get_yahoo_finance_data(interval='5m', range_param='5d')  # Need enough for 50 EMA
    h1_candles = await get_yahoo_finance_data(interval='60m', range_param='1mo')  # For HTF trend

    if not m5_candles or not h1_candles:
        logger.error("Could not fetch sufficient price data.")
        if manual_scan:
            await application.bot.send_message(chat_id=chat_id, text="Error: Could not fetch price data. Please try again later.")
        return

    current_candle = m5_candles[-1]
    signal_type, reasons, total_confidence, rsi_val, session_name, entry_conditions_met = await check_alpha_strategy(current_candle, m5_candles, h1_candles)

    if signal_type and total_confidence >= bot_state.learning_state['current_confidence_threshold']:
        entry_price = current_candle['close']
        if signal_type == 'BUY':
            tp = entry_price + TP_AMOUNT
            sl = entry_price - SL_AMOUNT
            trend = "Strong Bullish (EMA aligned)"
        else: # SELL
            tp = entry_price - TP_AMOUNT
            sl = entry_price + SL_AMOUNT
            trend = "Strong Bearish (EMA aligned)"

        # Prepare learning stats for signal message
        today_wr = (bot_state.today_wins / bot_state.today_trades_count) * 100 if bot_state.today_trades_count > 0 else 0
        last_10_trades = bot_state.trade_journal[-10:]
        wr_10 = (sum(1 for t in last_10_trades if t['result'] == 'WIN') / len(last_10_trades)) * 100 if last_10_trades else 0
        session_data = bot_state.learning_state['session_performance'].get(session_name, {'trades': 0, 'wins': 0})
        session_wr = (session_data['wins'] / session_data['trades']) * 100 if session_data['trades'] > 0 else 0
        learning_stats = {
            'today_wins': bot_state.today_wins,
            'today_trades': bot_state.today_trades_count,
            'today_wr': today_wr,
            'last_10_wr': wr_10,
            'session_wr': session_wr
        }

        await send_signal_message(chat_id, signal_type, entry_price, tp, sl, total_confidence, bot_state.learning_state['current_confidence_threshold'], trend, rsi_val, session_name, reasons, learning_stats)
        bot_state.last_signal_time = current_dubai_time

        trade_id = f"{signal_type}_{current_dubai_time.isoformat()}"
        bot_state.active_trades[trade_id] = {
            'signal_type': signal_type,
            'entry_price': entry_price,
            'tp': tp,
            'sl': sl,
            'entry_time': current_dubai_time,
            'signal_confidence': total_confidence,
            'entry_conditions_met': entry_conditions_met,
            'session': session_name,
            'bars_held': 0
        }
        logger.info(f"Signal sent: {signal_type} at {entry_price:.2f} with confidence {total_confidence:.2f}%")
        asyncio.create_task(monitor_trade_outcome(context, trade_id, chat_id))
    else:
        logger.info(f"No signal or confidence too low ({total_confidence:.2f}% < {bot_state.learning_state['current_confidence_threshold']:.2f}%).")
        if manual_scan:
            await application.bot.send_message(chat_id=chat_id, text=f"No signal found or confidence too low ({total_confidence:.2f}% < {bot_state.learning_state['current_confidence_threshold']:.2f}%).")

async def monitor_trade_outcome(context: ContextTypes.DEFAULT_TYPE, trade_id: str, chat_id: int) -> None:
    trade = bot_state.active_trades.get(trade_id)
    if not trade:
        return

    logger.info(f"Monitoring trade {trade_id}...")
    entry_time = trade['entry_time']

    while True:
        await asyncio.sleep(30) # Monitor every 30 seconds
        
        current_dubai_time = datetime.now(DUBAI_TZ)
        if not is_market_open(current_dubai_time):
            logger.info(f"Market closed during monitoring of trade {trade_id}. Closing as timeout.")
            trade_entry = {
                'timestamp': current_dubai_time.isoformat(),
                'signal_type': trade['signal_type'],
                'entry_price': trade['entry_price'],
                'exit_price': trade['entry_price'], # Exit at entry price for timeout
                'result': 'TIMEOUT',
                'pnl': 0.0,
                'bars_held': trade['bars_held'],
                'signal_confidence': trade['signal_confidence'],
                'entry_conditions_met': trade['entry_conditions_met'],
                'session': trade['session'],
                'market_conditions': 'N/A' # Placeholder
            }
            bot_state.record_trade(trade_entry)
            await send_result_notification(chat_id, trade_entry)
            del bot_state.active_trades[trade_id]
            return

        # Check for trade timeout
        if (current_dubai_time - entry_time).total_seconds() > TRADE_TIMEOUT_HOURS * 3600:
            logger.info(f"Trade {trade_id} timed out.")
            trade_entry = {
                'timestamp': current_dubai_time.isoformat(),
                'signal_type': trade['signal_type'],
                'entry_price': trade['entry_price'],
                'exit_price': trade['entry_price'], # Exit at entry price for timeout
                'result': 'TIMEOUT',
                'pnl': 0.0,
                'bars_held': trade['bars_held'],
                'signal_confidence': trade['signal_confidence'],
                'entry_conditions_met': trade['entry_conditions_met'],
                'session': trade['session'],
                'market_conditions': 'N/A' # Placeholder
            }
            bot_state.record_trade(trade_entry)
            await send_result_notification(chat_id, trade_entry)
            del bot_state.active_trades[trade_id]
            return

        # Fetch latest price to check TP/SL
        current_price = await get_current_price()
        if current_price is None:
            logger.error("Could not fetch price data for trade monitoring.")
            continue
        trade['bars_held'] += 1 # Increment bars held

        result = None
        pnl = 0.0
        exit_price = current_price

        if trade['signal_type'] == 'BUY':
            if current_price >= trade['tp']:
                result = 'WIN'
                pnl = TP_AMOUNT
                exit_price = trade['tp']
            elif current_price <= trade['sl']:
                result = 'LOSS'
                pnl = -SL_AMOUNT
                exit_price = trade['sl']
        else: # SELL
            if current_price <= trade['tp']:
                result = 'WIN'
                pnl = TP_AMOUNT
                exit_price = trade['tp']
            elif current_price >= trade['sl']:
                result = 'LOSS'
                pnl = -SL_AMOUNT
                exit_price = trade['sl']

        if result:
            logger.info(f"Trade {trade_id} resulted in {result}.")
            trade_entry = {
                'timestamp': current_dubai_time.isoformat(),
                'signal_type': trade['signal_type'],
                'entry_price': trade['entry_price'],
                'exit_price': exit_price,
                'result': result,
                'pnl': pnl,
                'bars_held': trade['bars_held'],
                'signal_confidence': trade['signal_confidence'],
                'entry_conditions_met': trade['entry_conditions_met'],
                'session': trade['session'],
                'market_conditions': 'N/A' # Placeholder
            }
            bot_state.record_trade(trade_entry)
            await send_result_notification(chat_id, trade_entry)
            del bot_state.active_trades[trade_id]
            return

async def weekly_review_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_state.weekly_self_review()

# --- Main Bot Setup --- #
def main() -> None:
    global application
    # Create data directory if it doesn't exist
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(WEEKLY_REPORTS_DIR, exist_ok=True)

    application = Application.builder().token(BOT_TOKEN).build()

    # Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("scan", scan_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("journal", journal_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("learn", learn_command))
    application.add_handler(CommandHandler("reset", reset_command))

    # Job Queue for auto-scanning and weekly review
    job_queue = application.job_queue
    # Scan every 30 seconds
    job_queue.run_repeating(auto_scan_for_signals, interval=30, first=10, data={'chat_id': ADMIN_CHAT_ID})
    # Weekly review every Sunday at a specific time (e.g., 01:00 Dubai time)
    job_queue.run_daily(weekly_review_job, time=MARKET_OPEN_MONDAY.replace(hour=1, minute=0, second=0).timetz(), days=(6,), tzinfo=DUBAI_TZ)

    logger.info("Bot started polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

# Global application instance
application = None

if __name__ == '__main__':
    main()
