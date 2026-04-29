#!/usr/bin/env python3
"""
XAU/USD Signal Bot — Performance Monitor
=========================================
Runs alongside the signal bot as a separate systemd service.
Every 5 minutes it reads data files, computes metrics, generates
actionable suggestions, writes performance_report.json and
pawoo_message.json, and sends Telegram summaries on schedule.

Service : performance-monitor.service
Log     : /home/ubuntu/signal_bot/monitor.log
Timezone: Asia/Dubai
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
import datetime
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import pytz
import requests

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "8653316966:AAGdqc_ip9cZwual3AONsMzKKknhJW3jrKg")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "5948621771"))
DUBAI_TZ      = pytz.timezone("Asia/Dubai")

BASE_DIR            = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
TRADE_HISTORY_FILE  = os.path.join(BASE_DIR, "trade_history.json")
SIGNAL_LOG_FILE     = os.path.join(BASE_DIR, "signal_log.json")
DAILY_JOURNAL_FILE  = os.path.join(BASE_DIR, "daily_journal.json")
OVERRIDE_STATE_FILE = os.path.join(BASE_DIR, "override_state.json")
BOT_LOG_FILE        = os.path.join(BASE_DIR, "bot.log")
MONITOR_LOG_FILE    = os.path.join(BASE_DIR, "monitor.log")
PERFORMANCE_REPORT  = os.path.join(BASE_DIR, "performance_report.json")
PAWOO_MESSAGE_FILE  = os.path.join(BASE_DIR, "pawoo_message.json")
MONITOR_STATE_FILE  = os.path.join(BASE_DIR, "monitor_state.json")

# Analysis thresholds
MIN_CHECKLIST_SCORE   = 5.0
HIGH_SCORE_THRESHOLD  = 7.0
WIN_RATE_GOOD         = 0.60
WIN_RATE_POOR         = 0.40
PROFIT_FACTOR_GOOD    = 1.5
TIGHT_SL_THRESHOLD    = 2.0   # $2 move = "tight SL"
RESTART_THRESHOLD     = 3     # cold starts/day before alert
ERROR_RATE_THRESHOLD  = 0.10  # 10% error lines
SCAN_SIGNAL_RATIO_MIN = 0.02  # 2% of scans should yield signals

# Telegram schedule
SUMMARY_INTERVAL_SECS = 4 * 3600   # 4-hour summaries
EOD_HOUR_DUBAI        = 23
EOD_MINUTE_DUBAI      = 59

# SMC checklist item names (matches bot.py order)
CHECKLIST_ITEMS = [
    "HTF Trend", "BOS/CHoCH", "Order Block", "FVG",
    "Liquidity Sweep", "Displacement", "RSI",
    "Premium/Discount", "Candle Pattern",
]

# ─────────────────────────────────────────────────────────────
# LOGGING  (lazy-open so the file is created fresh each run)
# ─────────────────────────────────────────────────────────────
def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("performance_monitor")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [MONITOR] %(levelname)s %(message)s")
    # File handler — open in append mode
    try:
        fh = logging.FileHandler(MONITOR_LOG_FILE, mode="a", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger

log = _setup_logging()


# ─────────────────────────────────────────────────────────────
# TIMEZONE HELPERS
# ─────────────────────────────────────────────────────────────
def now_dubai() -> datetime.datetime:
    return datetime.datetime.now(DUBAI_TZ)

def today_str() -> str:
    return now_dubai().date().isoformat()

def is_market_open() -> bool:
    """Gold market hours (Dubai time) - matches bot.py logic.
    XM GOLDm# daily maintenance: 00:55 - 03:05 Dubai
    Weekend: Saturday 00:50 onwards until Sunday 23:30
    """
    n = now_dubai()
    wd, h, m = n.weekday(), n.hour, n.minute
    # Weekend
    if wd == 5:  # Saturday
        if h == 0 and m < 50:
            return True
        return False
    if wd == 6:  # Sunday
        return h >= 23 and m >= 30
    # Daily maintenance break: 00:50 - 03:10 Dubai
    if h == 0 and m >= 50:
        return False
    if h == 1 or h == 2:
        return False
    if h == 3 and m < 10:
        return False
    return True


# ─────────────────────────────────────────────────────────────
# SAFE JSON HELPERS
# ─────────────────────────────────────────────────────────────
def load_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        log.warning(f"Failed to load {path}: {exc}")
        return default

def save_json(path: str, data: Any) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str, ensure_ascii=False)
    except Exception as exc:
        log.error(f"Failed to save {path}: {exc}")

def load_monitor_state() -> Dict:
    default = {
        "last_telegram_summary": 0.0,
        "last_eod_date": "",
        "last_offline_alert": 0.0,
        "cold_starts_today": 0,
        "cold_starts_date": "",
        "last_known_log_size": 0,
    }
    saved = load_json(MONITOR_STATE_FILE, {})
    default.update(saved)
    return default

def save_monitor_state(state: Dict) -> None:
    save_json(MONITOR_STATE_FILE, state)


# ─────────────────────────────────────────────────────────────
# UTILITY: parse checklist score
# ─────────────────────────────────────────────────────────────
def parse_score(raw: Any) -> Optional[float]:
    """Convert '6/9', '7/9', 7, 7.0 → float, or None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if "/" in s:
        try:
            return float(s.split("/")[0])
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────
# ── MODULE 1: TRADE PERFORMANCE METRICS ──────────────────────
# ─────────────────────────────────────────────────────────────

def _enrich_trades_with_signals(trades: List[Dict], signals: List[Dict]) -> List[Dict]:
    """
    trade_history.json entries (t1-t8 style) lack session/checklist_score.
    Cross-reference with signal_log.json by matching on id.
    Returns enriched copy of trades list.
    """
    sig_map: Dict[str, Dict] = {str(s.get("id", "")): s for s in signals}
    enriched = []
    for t in trades:
        t2 = dict(t)
        tid = str(t2.get("id", ""))
        if tid in sig_map:
            sig = sig_map[tid]
            # Fill missing fields from signal
            if not t2.get("session"):
                t2["session"] = sig.get("session", "Unknown")
            if not t2.get("checklist_score"):
                t2["checklist_score"] = sig.get("checklist_score")
            if not t2.get("direction"):
                t2["direction"] = sig.get("direction") or t2.get("type", "UNKNOWN")
        # Normalise direction field
        if not t2.get("direction"):
            t2["direction"] = t2.get("type", "UNKNOWN")
        if not t2.get("session"):
            t2["session"] = "Unknown"
        enriched.append(t2)
    return enriched


def compute_trade_metrics(trades: List[Dict], signals: List[Dict]) -> Dict:
    """Comprehensive trade performance metrics."""
    if not trades:
        return {}

    trades = _enrich_trades_with_signals(trades, signals)

    counted  = [t for t in trades if t.get("result") in ("WIN", "LOSS")]
    wins     = [t for t in counted if t.get("result") == "WIN"]
    losses   = [t for t in counted if t.get("result") == "LOSS"]
    scratches= [t for t in trades  if t.get("result") == "SCRATCH"]

    total_counted = len(counted)
    win_rate = len(wins) / total_counted if total_counted else 0.0

    # PnL
    win_pnls   = [abs(t.get("pnl", 0)) for t in wins]
    loss_pnls  = [abs(t.get("pnl", 0)) for t in losses]
    avg_win    = sum(win_pnls)  / len(win_pnls)  if win_pnls  else 0.0
    avg_loss   = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    total_pnl  = sum(t.get("pnl", 0) for t in trades)
    gross_profit = sum(win_pnls)
    gross_loss   = sum(loss_pnls)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

    # Achieved R:R
    rr_list = []
    for t in counted:
        entry = t.get("entry", 0)
        sl    = t.get("sl", 0)
        tp1   = t.get("tp1", 0)
        if entry and sl and tp1 and abs(entry - sl) > 0:
            rr_list.append(abs(tp1 - entry) / abs(entry - sl))
    avg_rr = sum(rr_list) / len(rr_list) if rr_list else 0.0

    # Consecutive streaks
    max_consec_wins = max_consec_losses = cur_w = cur_l = 0
    for t in counted:
        if t.get("result") == "WIN":
            cur_w += 1; cur_l = 0
        else:
            cur_l += 1; cur_w = 0
        max_consec_wins   = max(max_consec_wins,   cur_w)
        max_consec_losses = max(max_consec_losses, cur_l)

    # Session breakdown
    sess_stats: Dict[str, Dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for t in counted:
        sess = t.get("session", "Unknown")
        if t.get("result") == "WIN":
            sess_stats[sess]["wins"] += 1
        else:
            sess_stats[sess]["losses"] += 1
        sess_stats[sess]["pnl"] += t.get("pnl", 0)

    session_win_rates: Dict[str, Dict] = {}
    for sess, s in sess_stats.items():
        tot = s["wins"] + s["losses"]
        session_win_rates[sess] = {
            "win_rate": round(s["wins"] / tot, 4) if tot else 0.0,
            "wins": s["wins"], "losses": s["losses"],
            "pnl": round(s["pnl"], 2), "total": tot,
        }

    best_session  = max(session_win_rates, key=lambda k: session_win_rates[k]["win_rate"], default=None)
    worst_session = min(session_win_rates, key=lambda k: session_win_rates[k]["win_rate"], default=None)

    # Daily / weekly PnL
    daily_pnl: Dict[str, float] = defaultdict(float)
    for t in trades:
        date = (t.get("date") or
                (t.get("time", "")[:10] if t.get("time") else None) or
                (t.get("timestamp", "")[:10] if t.get("timestamp") else None))
        if date:
            daily_pnl[date] += t.get("pnl", 0)

    weekly_pnl: Dict[str, float] = defaultdict(float)
    for ds, pnl in daily_pnl.items():
        try:
            d = datetime.date.fromisoformat(ds)
            wk = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
            weekly_pnl[wk] += pnl
        except ValueError:
            pass

    # Checklist score vs win rate
    score_buckets: Dict[str, Dict] = defaultdict(lambda: {"wins": 0, "losses": 0})
    for t in counted:
        sc = parse_score(t.get("checklist_score"))
        if sc is not None:
            key = f"{int(sc)}/9"
            if t.get("result") == "WIN":
                score_buckets[key]["wins"] += 1
            else:
                score_buckets[key]["losses"] += 1

    score_win_rates: Dict[str, Dict] = {}
    for key, b in score_buckets.items():
        tot = b["wins"] + b["losses"]
        score_win_rates[key] = {
            "win_rate": round(b["wins"] / tot, 4) if tot else 0.0,
            "wins": b["wins"], "losses": b["losses"], "total": tot,
        }

    # Tight SL analysis (losses that closed within $2 of entry)
    tight_sl_losses = sum(
        1 for t in losses
        if t.get("entry") and (t.get("close_price") or t.get("close"))
        and abs((t.get("close_price") or t.get("close", 0)) - t.get("entry", 0)) <= TIGHT_SL_THRESHOLD
    )
    tight_sl_pct = tight_sl_losses / len(losses) if losses else 0.0

    # Direction breakdown
    dir_stats: Dict[str, Dict] = defaultdict(lambda: {"wins": 0, "losses": 0})
    for t in counted:
        d = t.get("direction", "UNKNOWN")
        if t.get("result") == "WIN":
            dir_stats[d]["wins"] += 1
        else:
            dir_stats[d]["losses"] += 1

    return {
        "total_trades":       len(trades),
        "counted_trades":     total_counted,
        "wins":               len(wins),
        "losses":             len(losses),
        "scratches":          len(scratches),
        "win_rate":           round(win_rate, 4),
        "avg_win":            round(avg_win,  2),
        "avg_loss":           round(avg_loss, 2),
        "avg_rr":             round(avg_rr,   2),
        "profit_factor":      round(min(profit_factor, 999.0), 2),
        "total_pnl":          round(total_pnl, 2),
        "gross_profit":       round(gross_profit, 2),
        "gross_loss":         round(gross_loss,   2),
        "max_consec_wins":    max_consec_wins,
        "max_consec_losses":  max_consec_losses,
        "session_win_rates":  session_win_rates,
        "best_session":       best_session,
        "worst_session":      worst_session,
        "daily_pnl":          {k: round(v, 2) for k, v in daily_pnl.items()},
        "weekly_pnl":         {k: round(v, 2) for k, v in weekly_pnl.items()},
        "score_win_rates":    score_win_rates,
        "tight_sl_losses":    tight_sl_losses,
        "tight_sl_pct":       round(tight_sl_pct, 4),
        "direction_stats":    dict(dir_stats),
    }


# ─────────────────────────────────────────────────────────────
# ── MODULE 2: SIGNAL BOT HEALTH ──────────────────────────────
# ─────────────────────────────────────────────────────────────

def check_bot_process() -> Tuple[bool, str]:
    """Return (is_running, detail). Works in both systemd and container environments."""
    # Method 1: pgrep
    try:
        result = subprocess.run(["pgrep", "-f", "bot.py"],
                                capture_output=True, text=True)
        pids = [p for p in result.stdout.strip().split() if p]
        if pids:
            return True, f"Running (PID {', '.join(pids)})"
    except Exception:
        pass
    
    # Method 2: Check bot.log freshness (if log updated in last 5 min, bot is alive)
    try:
        if os.path.exists(BOT_LOG_FILE):
            mtime = os.path.getmtime(BOT_LOG_FILE)
            age_min = (time.time() - mtime) / 60.0
            if age_min < 5.0:
                return True, f"Running (log active {age_min:.1f}m ago)"
    except Exception:
        pass
    
    return False, "No bot.py process found"


def analyze_bot_log(state: Dict) -> Dict:
    """Parse bot.log for health indicators."""
    if not os.path.exists(BOT_LOG_FILE):
        return {"log_exists": False}

    log_size = os.path.getsize(BOT_LOG_FILE)

    try:
        with open(BOT_LOG_FILE, "r", errors="replace") as fh:
            lines = fh.readlines()
    except Exception as exc:
        return {"log_exists": True, "read_error": str(exc)}

    total_lines  = len(lines)
    recent_lines = lines[-5000:] if total_lines > 5000 else lines

    error_count  = 0
    scan_count   = 0
    signal_count = 0
    cold_starts  = 0
    api_failures = 0
    last_ts      = None

    re_ts       = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    re_error    = re.compile(r"\b(ERROR|CRITICAL|Exception|Traceback)\b", re.IGNORECASE)
    re_scan     = re.compile(r"Signal scan:", re.IGNORECASE)
    re_signal   = re.compile(r"(NEW TRADE SIGNAL|Signal generated|SMC Checklist.*[7-9]/9)", re.IGNORECASE)
    re_cold     = re.compile(r"Cold start", re.IGNORECASE)
    re_api_fail = re.compile(r"(fetch_gold_price.*fail|gold.*api.*error|price.*timeout|requests\.exceptions)", re.IGNORECASE)

    for line in recent_lines:
        m = re_ts.match(line)
        if m:
            last_ts = m.group(1)
        if re_error.search(line):
            error_count += 1
        if re_scan.search(line):
            scan_count += 1
        if re_signal.search(line):
            signal_count += 1
        if re_cold.search(line):
            cold_starts += 1
        if re_api_fail.search(line):
            api_failures += 1

    error_rate        = error_count / len(recent_lines) if recent_lines else 0.0
    scan_signal_ratio = signal_count / scan_count if scan_count > 0 else 0.0

    # Recency check
    bot_stale = True
    last_entry_age_mins = None
    if last_ts:
        try:
            last_dt = DUBAI_TZ.localize(
                datetime.datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
            )
            age = (now_dubai() - last_dt).total_seconds() / 60
            last_entry_age_mins = round(age, 1)
            bot_stale = age > 15
        except Exception:
            pass

    # Update cold-start counter in persistent state
    today = today_str()
    if state.get("cold_starts_date") != today:
        state["cold_starts_today"] = 0
        state["cold_starts_date"]  = today
    state["cold_starts_today"] += cold_starts
    state["last_known_log_size"] = log_size

    return {
        "log_exists":           True,
        "total_lines":          total_lines,
        "recent_lines_checked": len(recent_lines),
        "error_count":          error_count,
        "error_rate":           round(error_rate, 4),
        "scan_count":           scan_count,
        "signal_count":         signal_count,
        "scan_signal_ratio":    round(scan_signal_ratio, 4),
        "cold_starts_session":  cold_starts,
        "cold_starts_today":    state["cold_starts_today"],
        "api_failures":         api_failures,
        "last_log_entry":       last_ts,
        "last_entry_age_mins":  last_entry_age_mins,
        "bot_stale":            bot_stale,
        "log_size_bytes":       log_size,
    }


def check_override_state() -> Dict:
    data = load_json(OVERRIDE_STATE_FILE, {"override": False})
    return {"loss_limit_override": bool(data.get("override", False))}


# ─────────────────────────────────────────────────────────────
# ── MODULE 3: SMC ANALYSIS QUALITY ───────────────────────────
# ─────────────────────────────────────────────────────────────

def compute_smc_quality(signals: List[Dict], trades: List[Dict]) -> Dict:
    """Analyse signal_log.json for SMC quality metrics."""
    if not signals:
        return {}

    # Build trade result map by id
    trade_map: Dict[str, Dict] = {str(t.get("id", "")): t for t in trades}

    scores: List[float] = []
    score_results: Dict[str, List[str]] = defaultdict(list)
    false_signals   = 0
    missed_signals  = 0
    entered_signals = 0
    session_signals: Dict[str, int] = defaultdict(int)
    direction_signals: Dict[str, int] = defaultdict(int)

    # Signals per day (for today count)
    today_signals = 0
    today = today_str()

    for sig in signals:
        action    = sig.get("action", "")
        score_raw = sig.get("checklist_score")
        score     = parse_score(score_raw)
        session   = sig.get("session", "Unknown")
        direction = sig.get("direction", "Unknown")
        sig_date  = (sig.get("timestamp", "") or "")[:10]

        session_signals[session]     += 1
        direction_signals[direction] += 1
        if sig_date == today:
            today_signals += 1

        if action == "MISSED":
            missed_signals += 1
            continue

        if action == "ENTERED":
            entered_signals += 1

        if score is not None:
            scores.append(score)

        # Determine result: from signal itself, or from trade_map
        result = sig.get("result")
        if result is None:
            tid = str(sig.get("id", ""))
            if tid in trade_map:
                result = trade_map[tid].get("result")

        if result in ("WIN", "LOSS") and score is not None:
            key = f"{int(score)}/9"
            score_results[key].append(result)

        # False signal: LOSS where price barely moved from entry
        if result == "LOSS":
            entry = sig.get("entry", 0)
            close = sig.get("close_price", 0)
            if entry and close and abs(close - entry) <= TIGHT_SL_THRESHOLD:
                false_signals += 1

    avg_score = sum(scores) / len(scores) if scores else 0.0

    # Score accuracy table
    score_accuracy: Dict[str, Dict] = {}
    for key, results in score_results.items():
        wins_n = results.count("WIN")
        tot    = len(results)
        score_accuracy[key] = {
            "win_rate": round(wins_n / tot, 4) if tot else 0.0,
            "wins": wins_n, "total": tot,
        }

    # Score trend (last 20 signals)
    recent_scores = [parse_score(s.get("checklist_score")) for s in signals[-20:]]
    recent_scores = [s for s in recent_scores if s is not None]
    score_trend = "stable"
    if len(recent_scores) >= 6:
        half = len(recent_scores) // 2
        avg_first  = sum(recent_scores[:half]) / half
        avg_second = sum(recent_scores[half:]) / (len(recent_scores) - half)
        if avg_second > avg_first + 0.3:
            score_trend = "improving"
        elif avg_second < avg_first - 0.3:
            score_trend = "declining"

    # Checklist item pass/fail from bot.log
    item_stats = _parse_checklist_items_from_log()

    return {
        "total_signals":         len(signals),
        "entered_signals":       entered_signals,
        "missed_signals":        missed_signals,
        "false_signals":         false_signals,
        "signals_today":         today_signals,
        "avg_checklist_score":   round(avg_score, 2),
        "score_accuracy":        score_accuracy,
        "score_trend":           score_trend,
        "session_signals":       dict(session_signals),
        "direction_signals":     dict(direction_signals),
        "checklist_item_stats":  item_stats,
    }


def _parse_checklist_items_from_log() -> Dict[str, Dict]:
    """Count ✅/❌ per checklist item name from bot.log."""
    if not os.path.exists(BOT_LOG_FILE):
        return {}

    re_pass = re.compile(r"✅\s+([^:]+):")
    re_fail = re.compile(r"❌\s+([^:]+):")
    item_pass: Dict[str, int] = defaultdict(int)
    item_fail: Dict[str, int] = defaultdict(int)

    try:
        with open(BOT_LOG_FILE, "r", errors="replace") as fh:
            for line in fh:
                for m in re_pass.finditer(line):
                    item_pass[m.group(1).strip()] += 1
                for m in re_fail.finditer(line):
                    item_fail[m.group(1).strip()] += 1
    except Exception:
        pass

    stats: Dict[str, Dict] = {}
    for item in set(list(item_pass) + list(item_fail)):
        p = item_pass[item]; f = item_fail[item]; tot = p + f
        stats[item] = {
            "passes": p, "fails": f, "total": tot,
            "pass_rate": round(p / tot, 4) if tot else 0.0,
        }
    return stats


# ─────────────────────────────────────────────────────────────
# ── MODULE 4: SUGGESTIONS ENGINE ─────────────────────────────
# ─────────────────────────────────────────────────────────────

def generate_suggestions(
    trade_metrics:  Dict,
    bot_health:     Dict,
    smc_quality:    Dict,
    override_state: Dict,
) -> List[str]:
    """Generate actionable suggestions from all metrics."""
    suggestions: List[str] = []
    log_info = bot_health.get("log_analysis", {})

    # ── Bot health alerts ──────────────────────────────────────
    if not bot_health.get("bot_running", True):
        suggestions.append(
            "🚨 CRITICAL: Signal bot process is NOT running — restart immediately: "
            "`sudo systemctl restart signal-bot.service`"
        )

    if log_info.get("bot_stale") and is_market_open():
        age = log_info.get("last_entry_age_mins", "?")
        suggestions.append(
            f"⚠️ Bot log has not updated for {age} mins during market hours — "
            "bot may be frozen or stuck."
        )

    cold_starts = log_info.get("cold_starts_today", 0)
    if cold_starts >= RESTART_THRESHOLD:
        suggestions.append(
            f"🔄 Bot has cold-started {cold_starts}x today — check for memory errors "
            "or API timeouts in bot.log."
        )

    error_rate = log_info.get("error_rate", 0.0)
    if error_rate > ERROR_RATE_THRESHOLD:
        suggestions.append(
            f"🐛 Error rate in bot.log is {error_rate:.1%} — unusually high. "
            "Review recent ERROR lines for recurring exceptions."
        )

    api_failures = log_info.get("api_failures", 0)
    if api_failures >= 5:
        suggestions.append(
            f"📡 {api_failures} API failures in recent log — gold price feed is unstable. "
            "Consider adding a third price source fallback."
        )

    scan_ratio = log_info.get("scan_signal_ratio", 1.0)
    if scan_ratio < SCAN_SIGNAL_RATIO_MIN and log_info.get("scan_count", 0) > 20:
        suggestions.append(
            f"📉 Only {scan_ratio:.1%} of scans are generating signals — market may be "
            "ranging or checklist threshold is too strict."
        )

    if override_state.get("loss_limit_override"):
        suggestions.append(
            "⚠️ Loss-limit override is ACTIVE — daily loss protection is disabled. "
            "Re-lock with /lock after session."
        )

    # ── Trade performance ──────────────────────────────────────
    if not trade_metrics:
        if not suggestions:
            suggestions.append("✅ No trade data yet — waiting for first trades.")
        return suggestions

    win_rate      = trade_metrics.get("win_rate", 0.0)
    total_counted = trade_metrics.get("counted_trades", 0)
    profit_factor = trade_metrics.get("profit_factor", 0.0)
    avg_win       = trade_metrics.get("avg_win",  0.0)
    avg_loss      = trade_metrics.get("avg_loss", 0.0)
    max_cl        = trade_metrics.get("max_consec_losses", 0)
    tight_sl_pct  = trade_metrics.get("tight_sl_pct", 0.0)

    if total_counted >= 5:
        if win_rate < WIN_RATE_POOR:
            suggestions.append(
                f"📊 Win rate is only {win_rate:.0%} over {total_counted} trades — "
                "below acceptable threshold. Review entry criteria."
            )
        elif win_rate >= WIN_RATE_GOOD:
            suggestions.append(
                f"✅ Win rate is strong at {win_rate:.0%} over {total_counted} trades — "
                "strategy is performing well."
            )

        if profit_factor < 1.0:
            suggestions.append(
                f"💸 Profit factor is {profit_factor:.2f} — losing more than winning in dollar terms. "
                "Consider widening TP targets or tightening entries."
            )
        elif profit_factor >= PROFIT_FACTOR_GOOD:
            suggestions.append(
                f"💰 Profit factor is excellent at {profit_factor:.2f} — "
                "reward is well above risk on winning trades."
            )

    if avg_loss > 0 and avg_win < avg_loss * 0.8 and total_counted >= 3:
        suggestions.append(
            f"⚖️ Avg win (${avg_win:.2f}) < avg loss (${avg_loss:.2f}) — "
            "R:R is unfavourable. Move SL to breakeven earlier or target TP2."
        )

    if max_cl >= 3:
        suggestions.append(
            f"🔴 Max consecutive losses is {max_cl} — "
            "check if these share a pattern: same session, direction, or score."
        )

    if tight_sl_pct >= 0.5 and trade_metrics.get("losses", 0) >= 3:
        suggestions.append(
            f"📏 {tight_sl_pct:.0%} of losses closed within ${TIGHT_SL_THRESHOLD} of entry — "
            "SL may be too tight. Place SL at a stronger structural level."
        )

    # ── Session analysis ───────────────────────────────────────
    session_wr = {k: v for k, v in trade_metrics.get("session_win_rates", {}).items()
                  if v.get("total", 0) >= 3}
    if len(session_wr) >= 2:
        best  = max(session_wr, key=lambda k: session_wr[k]["win_rate"])
        worst = min(session_wr, key=lambda k: session_wr[k]["win_rate"])
        bwr   = session_wr[best]["win_rate"]
        wwr   = session_wr[worst]["win_rate"]
        if bwr - wwr >= 0.25:
            suggestions.append(
                f"🕐 {best} session win rate is {bwr:.0%} vs {wwr:.0%} in {worst} — "
                f"focus on {best} and reduce {worst} exposure."
            )

    # ── Checklist score analysis ───────────────────────────────
    score_wr = trade_metrics.get("score_win_rates", {})
    score_acc = smc_quality.get("score_accuracy", {})
    # Merge both sources
    merged_scores: Dict[str, Dict] = {}
    for key in set(list(score_wr) + list(score_acc)):
        a = score_wr.get(key, {})
        b = score_acc.get(key, {})
        tot = a.get("total", 0) + b.get("total", 0)
        if tot == 0:
            continue
        wins_n = a.get("wins", 0) + b.get("wins", 0)
        merged_scores[key] = {"win_rate": wins_n / tot, "total": tot}

    high_wr = low_wr = None
    for key, data in merged_scores.items():
        if data["total"] < 2:
            continue
        try:
            sv = int(key.split("/")[0])
        except (ValueError, IndexError):
            continue
        if sv >= int(HIGH_SCORE_THRESHOLD):
            high_wr = data["win_rate"]
        elif sv == int(MIN_CHECKLIST_SCORE):
            low_wr = data["win_rate"]

    if high_wr is not None and low_wr is not None and high_wr - low_wr >= 0.20:
        suggestions.append(
            f"🎯 Signals with {int(HIGH_SCORE_THRESHOLD)}/9+ score have "
            f"{high_wr:.0%} win rate vs {low_wr:.0%} for {int(MIN_CHECKLIST_SCORE)}/9 — "
            f"consider raising the minimum threshold to {int(HIGH_SCORE_THRESHOLD)}."
        )

    # ── Checklist item pass rates ──────────────────────────────
    item_stats = {k: v for k, v in smc_quality.get("checklist_item_stats", {}).items()
                  if v.get("total", 0) >= 5}
    if item_stats:
        worst_item = min(item_stats, key=lambda k: item_stats[k]["pass_rate"])
        best_item  = max(item_stats, key=lambda k: item_stats[k]["pass_rate"])
        if item_stats[worst_item]["pass_rate"] < 0.40:
            suggestions.append(
                f"🔍 '{worst_item}' is the least-passed checklist item "
                f"({item_stats[worst_item]['pass_rate']:.0%} pass rate) — "
                "review whether this condition is calibrated for current market."
            )
        if item_stats[best_item]["pass_rate"] > 0.85:
            suggestions.append(
                f"✅ '{best_item}' passes {item_stats[best_item]['pass_rate']:.0%} of the time — "
                "your most reliable SMC condition."
            )

    # ── SMC quality ────────────────────────────────────────────
    avg_score   = smc_quality.get("avg_checklist_score", 0.0)
    score_trend = smc_quality.get("score_trend", "stable")

    if avg_score > 0:
        if avg_score < MIN_CHECKLIST_SCORE + 0.3:
            suggestions.append(
                f"📋 Avg checklist score is {avg_score:.1f}/9 — barely above threshold. "
                "Bot is taking marginal setups. Consider waiting for 7/9+."
            )
        elif avg_score >= HIGH_SCORE_THRESHOLD:
            suggestions.append(
                f"📋 Avg checklist score is {avg_score:.1f}/9 — high quality setups."
            )

    if score_trend == "declining":
        suggestions.append(
            "📉 Checklist scores are trending downward — market structure is deteriorating. "
            "Reduce position size or pause trading."
        )
    elif score_trend == "improving":
        suggestions.append(
            "📈 Checklist scores are trending upward — market structure is improving. "
            "Good time to be active."
        )

    false_signals = smc_quality.get("false_signals", 0)
    entered       = max(smc_quality.get("entered_signals", 1), 1)
    if false_signals / entered >= 0.30:
        suggestions.append(
            f"⚡ {false_signals} false signals ({false_signals/entered:.0%} of entries) — "
            "price reversing immediately after entry. Review entry timing."
        )

    missed = smc_quality.get("missed_signals", 0)
    if missed >= 3:
        suggestions.append(
            f"⏰ {missed} signals missed (no response within 5 mins) — "
            "ensure Telegram notifications are working."
        )

    # ── Direction bias ─────────────────────────────────────────
    dir_stats = trade_metrics.get("direction_stats", {})
    buy_tot  = dir_stats.get("BUY",  {}).get("wins", 0) + dir_stats.get("BUY",  {}).get("losses", 0)
    sell_tot = dir_stats.get("SELL", {}).get("wins", 0) + dir_stats.get("SELL", {}).get("losses", 0)
    if buy_tot >= 3 and sell_tot >= 3:
        buy_wr  = dir_stats["BUY"]["wins"]  / buy_tot
        sell_wr = dir_stats["SELL"]["wins"] / sell_tot
        if abs(buy_wr - sell_wr) >= 0.25:
            better    = "BUY" if buy_wr > sell_wr else "SELL"
            better_wr = max(buy_wr, sell_wr)
            worse_wr  = min(buy_wr, sell_wr)
            suggestions.append(
                f"📊 {better} signals have {better_wr:.0%} win rate vs {worse_wr:.0%} — "
                f"current market favours {better} setups."
            )

    if not suggestions:
        suggestions.append(
            "✅ All systems nominal. No critical issues detected. "
            "Keep following the SMC checklist."
        )

    return suggestions


# ─────────────────────────────────────────────────────────────
# ── MODULE 5: REPORTING ──────────────────────────────────────
# ─────────────────────────────────────────────────────────────

def build_performance_report(
    trade_metrics:  Dict,
    bot_health:     Dict,
    smc_quality:    Dict,
    override_state: Dict,
    suggestions:    List[str],
) -> Dict:
    return {
        "generated_at":   now_dubai().isoformat(),
        "timezone":       "Asia/Dubai",
        "trade_metrics":  trade_metrics,
        "bot_health":     bot_health,
        "smc_quality":    smc_quality,
        "override_state": override_state,
        "suggestions":    suggestions,
    }


def build_pawoo_message(
    trade_metrics:  Dict,
    smc_quality:    Dict,
    bot_health:     Dict,
    suggestions:    List[str],
    report_type:    str = "periodic",
) -> Dict:
    """Build pawoo_message.json payload (read by Manus coordinator)."""
    is_running  = bot_health.get("bot_running", False)
    log_info    = bot_health.get("log_analysis", {})
    win_rate    = trade_metrics.get("win_rate", 0.0)
    total_pnl   = trade_metrics.get("total_pnl", 0.0)
    counted     = trade_metrics.get("counted_trades", 0)
    avg_score   = smc_quality.get("avg_checklist_score", 0.0)
    total_sigs  = smc_quality.get("total_signals", 0)
    today_pnl   = trade_metrics.get("daily_pnl", {}).get(today_str(), 0.0)

    status_icon = "🟢" if is_running else "🔴"
    pnl_icon    = "📈" if total_pnl >= 0 else "📉"

    message = (
        f"{status_icon} XAU/USD Bot Performance Report\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Win Rate: {win_rate:.0%} ({counted} counted trades)\n"
        f"{pnl_icon} Total PnL: ${total_pnl:+.2f} | Today: ${today_pnl:+.2f}\n"
        f"📋 Avg Checklist Score: {avg_score:.1f}/9\n"
        f"📡 Total Signals: {total_sigs}\n"
        f"🤖 Bot Status: {'Running' if is_running else 'OFFLINE'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Top Suggestion:\n{suggestions[0] if suggestions else 'All clear.'}"
    )

    return {
        "action":       "performance_report",
        "report_type":  report_type,
        "generated_at": now_dubai().isoformat(),
        "message":      message,
        "suggestions":  suggestions,
        "metrics": {
            "win_rate":            win_rate,
            "total_pnl":           total_pnl,
            "today_pnl":           today_pnl,
            "counted_trades":      counted,
            "signals_today":       smc_quality.get("signals_today", 0),
            "avg_checklist_score": avg_score,
            "profit_factor":       trade_metrics.get("profit_factor", 0.0),
            "bot_running":         is_running,
            "error_rate":          log_info.get("error_rate", 0.0),
            "cold_starts_today":   log_info.get("cold_starts_today", 0),
        },
    }


# ─────────────────────────────────────────────────────────────
# ── TELEGRAM ─────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("Telegram message sent.")
            return True
        log.warning(f"Telegram failed: {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as exc:
        log.error(f"Telegram exception: {exc}")
        return False


def format_telegram_summary(
    trade_metrics: Dict,
    bot_health:    Dict,
    smc_quality:   Dict,
    suggestions:   List[str],
    report_type:   str = "4h",
) -> str:
    is_running = bot_health.get("bot_running", False)
    log_info   = bot_health.get("log_analysis", {})

    win_rate      = trade_metrics.get("win_rate", 0.0)
    total_pnl     = trade_metrics.get("total_pnl", 0.0)
    wins          = trade_metrics.get("wins", 0)
    losses        = trade_metrics.get("losses", 0)
    scratches     = trade_metrics.get("scratches", 0)
    avg_win       = trade_metrics.get("avg_win", 0.0)
    avg_loss      = trade_metrics.get("avg_loss", 0.0)
    profit_factor = trade_metrics.get("profit_factor", 0.0)
    max_cl        = trade_metrics.get("max_consec_losses", 0)
    avg_score     = smc_quality.get("avg_checklist_score", 0.0)
    total_sigs    = smc_quality.get("total_signals", 0)
    missed_sigs   = smc_quality.get("missed_signals", 0)
    today_pnl     = trade_metrics.get("daily_pnl", {}).get(today_str(), 0.0)

    status_icon = "🟢" if is_running else "🔴"
    pnl_icon    = "📈" if total_pnl >= 0 else "📉"
    today_icon  = "📈" if today_pnl >= 0 else "📉"
    header      = "📊 4-HOUR PERFORMANCE REPORT" if report_type == "4h" else "🌙 END-OF-DAY REPORT"

    # Session table
    session_lines = []
    for sess, data in trade_metrics.get("session_win_rates", {}).items():
        if data.get("total", 0) > 0:
            wr   = data["win_rate"]
            icon = "✅" if wr >= WIN_RATE_GOOD else ("⚠️" if wr >= 0.40 else "❌")
            session_lines.append(
                f"  {icon} {sess}: {wr:.0%} "
                f"({data['wins']}W/{data['losses']}L, ${data['pnl']:+.2f})"
            )
    session_text = "\n".join(session_lines) if session_lines else "  No session data yet"

    # Score table
    score_lines = []
    for key in sorted(trade_metrics.get("score_win_rates", {}).keys()):
        data = trade_metrics["score_win_rates"][key]
        if data.get("total", 0) > 0:
            wr   = data["win_rate"]
            icon = "✅" if wr >= WIN_RATE_GOOD else ("⚠️" if wr >= 0.40 else "❌")
            score_lines.append(
                f"  {icon} Score {key}: {wr:.0%} ({data['wins']}W/{data['losses']}L)"
            )
    score_text = "\n".join(score_lines) if score_lines else "  Not enough data"

    top_suggestions = "\n".join(
        f"  {i+1}. {s}" for i, s in enumerate(suggestions[:3])
    )

    return (
        f"<b>{header}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>🤖 Bot Health</b>\n"
        f"  {status_icon} Process: {'Running ✅' if is_running else 'OFFLINE 🚨'}\n"
        f"  📝 Last log: {log_info.get('last_log_entry', 'N/A')} "
        f"({log_info.get('last_entry_age_mins', '?')} mins ago)\n"
        f"  🔄 Cold starts today: {log_info.get('cold_starts_today', 0)}\n"
        f"  🐛 Error rate: {log_info.get('error_rate', 0.0):.1%}\n"
        f"  📡 API failures: {log_info.get('api_failures', 0)}\n"
        f"  🔍 Scans: {log_info.get('scan_count', 0)} | "
        f"Signals: {log_info.get('signal_count', 0)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{pnl_icon} Trade Performance</b>\n"
        f"  📊 Win Rate: <b>{win_rate:.0%}</b> "
        f"({wins}W / {losses}L / {scratches}S)\n"
        f"  💰 Total PnL: <b>${total_pnl:+.2f}</b>\n"
        f"  {today_icon} Today PnL: <b>${today_pnl:+.2f}</b>\n"
        f"  ⚖️ Avg Win: ${avg_win:.2f} | Avg Loss: ${avg_loss:.2f}\n"
        f"  📐 Profit Factor: {profit_factor:.2f}\n"
        f"  🔴 Max Consec Losses: {max_cl}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>📋 SMC Quality</b>\n"
        f"  Avg Score: <b>{avg_score:.1f}/9</b> | "
        f"Signals: {total_sigs} | Missed: {missed_sigs}\n"
        f"  Score Win Rates:\n{score_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>🕐 Session Breakdown</b>\n"
        f"{session_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>💡 Top Suggestions</b>\n"
        f"{top_suggestions}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>ပေါ်ဦး — Performance Monitor | "
        f"{now_dubai().strftime('%Y-%m-%d %H:%M')} Dubai</i>"
    )


# ─────────────────────────────────────────────────────────────
# ── MAIN CYCLE ───────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────

def run_monitor_cycle(state: Dict) -> Dict:
    log.info("=== Monitor cycle start ===")

    # Load data
    trades   = load_json(TRADE_HISTORY_FILE, [])
    signals  = load_json(SIGNAL_LOG_FILE,    [])
    override = check_override_state()

    # Compute metrics
    trade_metrics = compute_trade_metrics(trades, signals)
    log_analysis  = analyze_bot_log(state)
    bot_running, proc_detail = check_bot_process()

    bot_health = {
        "bot_running":    bot_running,
        "process_detail": proc_detail,
        "log_analysis":   log_analysis,
    }

    smc_quality = compute_smc_quality(signals, trades)
    suggestions = generate_suggestions(trade_metrics, bot_health, smc_quality, override)

    # Write reports
    report = build_performance_report(
        trade_metrics, bot_health, smc_quality, override, suggestions
    )
    save_json(PERFORMANCE_REPORT, report)

    pawoo_msg = build_pawoo_message(
        trade_metrics, smc_quality, bot_health, suggestions
    )
    save_json(PAWOO_MESSAGE_FILE, pawoo_msg)

    log.info(
        f"Cycle complete — WinRate={trade_metrics.get('win_rate', 0):.0%} "
        f"PnL=${trade_metrics.get('total_pnl', 0):.2f} "
        f"Suggestions={len(suggestions)} "
        f"BotRunning={bot_running}"
    )

    # Telegram scheduling
    now_ts = time.time()
    now_dt = now_dubai()

    should_send_4h = (
        is_market_open() and
        (now_ts - state.get("last_telegram_summary", 0)) >= SUMMARY_INTERVAL_SECS
    )
    should_send_eod = (
        now_dt.hour == EOD_HOUR_DUBAI and
        now_dt.minute >= EOD_MINUTE_DUBAI and
        state.get("last_eod_date") != today_str()
    )

    if should_send_eod:
        log.info("Sending end-of-day Telegram summary.")
        msg = format_telegram_summary(
            trade_metrics, bot_health, smc_quality, suggestions, report_type="eod"
        )
        if send_telegram(msg):
            state["last_eod_date"]          = today_str()
            state["last_telegram_summary"]  = now_ts

    elif should_send_4h:
        log.info("Sending 4-hour Telegram summary.")
        msg = format_telegram_summary(
            trade_metrics, bot_health, smc_quality, suggestions, report_type="4h"
        )
        if send_telegram(msg):
            state["last_telegram_summary"] = now_ts

    # Offline alert (max once per 30 mins during market hours)
    if not bot_running and is_market_open():
        if now_ts - state.get("last_offline_alert", 0) > 1800:
            log.warning("Bot is offline during market hours — sending alert.")
            send_telegram(
                "🚨 <b>ALERT: Signal Bot is OFFLINE!</b>\n"
                f"No bot.py process at {now_dt.strftime('%H:%M')} Dubai.\n"
                "Restart: <code>sudo systemctl restart signal-bot.service</code>\n"
                "— Performance Monitor Bot"
            )
            state["last_offline_alert"] = now_ts

    return state


# ─────────────────────────────────────────────────────────────
# ── ENTRY POINT ──────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────

def main():
    log.info("Performance Monitor Bot starting up.")
    log.info(f"Data directory: {BASE_DIR}")
    log.info(f"Current Dubai time: {now_dubai().strftime('%Y-%m-%d %H:%M:%S')}")

    send_telegram(
        "🚀 <b>Performance Monitor Bot Started</b>\n"
        f"Monitoring XAU/USD Signal Bot\n"
        f"📁 Dir: <code>{BASE_DIR}</code>\n"
        f"⏱ Cycle: every 5 minutes\n"
        f"🕐 {now_dubai().strftime('%Y-%m-%d %H:%M')} Dubai\n"
        "— ပေါ်ဦး Performance Monitor"
    )

    state = load_monitor_state()

    while True:
        try:
            state = run_monitor_cycle(state)
            save_monitor_state(state)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as exc:
            log.error(f"Unhandled exception in cycle: {exc}", exc_info=True)

        log.info("Sleeping 5 minutes...")
        time.sleep(300)


if __name__ == "__main__":
    main()
