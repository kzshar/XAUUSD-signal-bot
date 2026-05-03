#!/usr/bin/env python3
"""
PawOo Performance Monitor v4.0
===============================
Runs alongside signal bot. Sends periodic performance reports.
"""

import json
import logging
import os
import time
import datetime
import sys

import pytz
import requests

# Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8653316966:AAGdqc_ip9cZwual3AONsMzKKknhJW3jrKg")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "5948621771"))
DUBAI_TZ = pytz.timezone("Asia/Dubai")
BASE_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

TRADE_HISTORY_FILE = os.path.join(BASE_DIR, "trade_history.json")
SIGNAL_LOG_FILE = os.path.join(BASE_DIR, "signal_log.json")
BOT_LOG_FILE = os.path.join(BASE_DIR, "bot.log")
MONITOR_STATE_FILE = os.path.join(BASE_DIR, "monitor_state.json")

REPORT_INTERVAL = 4 * 3600  # Every 4 hours
CHECK_INTERVAL = 300  # Check every 5 minutes

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MONITOR] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "monitor.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("Monitor")


def now_dubai():
    return datetime.datetime.now(DUBAI_TZ)


def is_market_open() -> bool:
    """Match bot.py market hours logic.
    XM GOLDm#: Mon-Fri 02:05-00:55 Dubai time.
    Weekend: Saturday 00:55 - Monday 02:05 CLOSED.
    Daily break: 00:55-02:05 Dubai.
    """
    n = now_dubai()
    wd, h, m = n.weekday(), n.hour, n.minute
    t = h * 60 + m
    if wd == 5:  # Saturday: only 00:00-00:55
        return t < 55
    if wd == 6:  # Sunday: CLOSED
        return False
    if wd == 0 and t < 125:  # Monday: closed until 02:05
        return False
    if t >= 55 and t < 125:  # Daily break 00:55-02:05
        return False
    return True


def load_json(path, default=None):
    if not os.path.exists(path):
        return default or []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return default or []


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_state():
    default = {"last_report": 0, "last_eod": ""}
    saved = load_json(MONITOR_STATE_FILE, {})
    if isinstance(saved, dict):
        default.update(saved)
    return default


def save_state(state):
    save_json(MONITOR_STATE_FILE, state)


def send_telegram(text):
    """Send message via Telegram."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": ADMIN_CHAT_ID,
            "text": text,
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def check_bot_health():
    """Check if signal bot is still running and responsive."""
    if not os.path.exists(BOT_LOG_FILE):
        return "unknown", 0
    
    try:
        mtime = os.path.getmtime(BOT_LOG_FILE)
        age_mins = (time.time() - mtime) / 60
        
        if age_mins < 5:
            return "healthy", age_mins
        elif age_mins < 30:
            return "slow", age_mins
        else:
            return "stale", age_mins
    except:
        return "unknown", 0


def generate_report():
    """Generate performance report."""
    trades = load_json(TRADE_HISTORY_FILE, [])
    signals = load_json(SIGNAL_LOG_FILE, [])
    
    today = now_dubai().date().isoformat()
    today_trades = [t for t in trades if t.get("date") == today]
    
    total_counted = [t for t in trades if t.get("result") in ("WIN", "LOSS")]
    wins = len([t for t in total_counted if t.get("result") == "WIN"])
    losses = len([t for t in total_counted if t.get("result") == "LOSS"])
    total = len(total_counted)
    win_rate = (wins / total * 100) if total else 0
    
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    today_pnl = sum(t.get("pnl", 0) for t in today_trades)
    
    # Bot health
    health, age = check_bot_health()
    health_emoji = "🟢" if health == "healthy" else "🟡" if health == "slow" else "🔴"
    
    msg = f"""📊 XAU/USD Bot Performance Report
━━━━━━━━━━━━━━━━━
📈 Win Rate: {win_rate:.0f}% ({wins}W/{losses}L)
💰 Total PnL: ${total_pnl:+.2f}
📅 Today: ${today_pnl:+.2f}
📝 Total Signals: {len(signals)}
{health_emoji} Bot: {health} (log {age:.0f}min ago)
━━━━━━━━━━━━━━━━━"""
    
    # Add suggestion
    suggestion = ""
    if health == "stale" and is_market_open():
        suggestion = "\n⚠️ Bot log stale during market hours — may be frozen!"
    elif win_rate < 40 and total >= 5:
        suggestion = "\n⚠️ Win rate below 40% — review strategy!"
    elif today_pnl <= -10:
        suggestion = "\n🛑 Daily loss limit reached — bot should stop trading!"
    
    if suggestion:
        msg += f"\n⚡ Suggestion:{suggestion}"
    
    return msg


def main():
    """Main monitor loop."""
    log.info("Performance Monitor v4.0 started")
    state = load_state()
    
    while True:
        try:
            now = time.time()
            
            # Send report every 4 hours during market hours
            if is_market_open() and (now - state["last_report"]) >= REPORT_INTERVAL:
                report = generate_report()
                if send_telegram(report):
                    state["last_report"] = now
                    save_state(state)
                    log.info("Report sent")
            
            # Check bot health every 5 min during market hours
            if is_market_open():
                health, age = check_bot_health()
                if health == "stale" and age > 60:
                    # Only alert once per hour
                    if (now - state.get("last_stale_alert", 0)) > 3600:
                        send_telegram(f"⚠️ Signal bot may be frozen! Log not updated for {age:.0f} minutes.")
                        state["last_stale_alert"] = now
                        save_state(state)
            
            # End of day report
            n = now_dubai()
            if n.hour == 23 and n.minute >= 55:
                today = n.date().isoformat()
                if state.get("last_eod") != today:
                    report = generate_report()
                    send_telegram(f"🌙 End of Day Report\n{report}")
                    state["last_eod"] = today
                    save_state(state)
            
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            log.error(f"Monitor error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
