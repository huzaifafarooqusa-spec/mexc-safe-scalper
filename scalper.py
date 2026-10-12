import os
import sys
import time
import json
import math
import ssl
import html
import urllib.request
import urllib.error
from datetime import datetime, timezone

KLINE_MS = 5 * 60 * 1000

# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
if not BOT_TOKEN or not CHAT_ID:
    sys.exit("BOT_TOKEN and CHAT_ID environment variables are required")

MEXC_BASE_URL = "https://api.mexc.com/api/v3"
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# Selected high-liquidity coins from portfolio
COINS = [
    "SEIUSDT",
    "FETUSDT",
    "RENDERUSDT",
    "DOGEUSDT",
    "TIAUSDT",
    "DOTUSDT",
    "WLDUSDT",
    "SOLUSDT"
]

# Scalping Parameters
TAKE_PROFIT_PCT = 0.006     # +0.60% Net Scalp
STOP_LOSS_PCT = 0.025       # -2.50% Safety Stop Loss
MAX_CONCURRENT_TRADES = 3   # Max 3 coins at a time
DEFAULT_SLOT_SIZE = 200.0   # $200 per trade slot

# SSL Context for requests
ssl_ctx = ssl.create_default_context()
HEADERS = {"User-Agent": "Mozilla/5.0 MEXCScalperBot/1.0"}

def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=12, context=ssl_ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))

def send_telegram(message_html):
    # Use HTML formatting to avoid Markdown entity parse errors
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": message_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }).encode("utf-8")
    
    req = urllib.request.Request(
        api_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "TelegramBot/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"❌ Telegram Error: {e}")
        return False

# --- STATE MANAGEMENT ---
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
            
    return {
        "virtual_balance": 1000.0,
        "slot_size": DEFAULT_SLOT_SIZE,
        "max_slots": MAX_CONCURRENT_TRADES,
        "open_trades": {},
        "closed_trades": [],
        "stats": {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_profit_usdt": 0.0
        }
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# --- TECHNICAL ANALYSIS (PURE PYTHON) ---
def calculate_bollinger_bands(closes, period=20, std_dev_mult=2.0):
    if len(closes) < period:
        return None, None, None
    subset = closes[-period:]
    sma = sum(subset) / period
    variance = sum((x - sma) ** 2 for x in subset) / period
    std = math.sqrt(variance)
    upper = sma + (std_dev_mult * std)
    lower = sma - (std_dev_mult * std)
    return upper, sma, lower

def calculate_rsi(closes, period=14):
    """Wilder's RSI (smoothed), matches TradingView."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = sum(d for d in deltas[:period] if d > 0) / period
    avg_loss = sum(-d for d in deltas[:period] if d < 0) / period
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

# --- TRADING LOGIC ---
def get_klines_data(symbol, interval="5m", limit=35):
    try:
        url = f"{MEXC_BASE_URL}/klines?symbol={symbol}&interval={interval}&limit={limit}"
        data = fetch_json(url)
        # Kline format: [openTime, open, high, low, close, volume, ...]
        return data
    except Exception as e:
        print(f"Error fetching klines for {symbol}: {e}")
        return None

def process_trades(state):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    open_trades = state.get("open_trades", {})
    closed_this_round = []

    # 1. CHECK OPEN POSITIONS (TAKE PROFIT / STOP LOSS)
    for symbol, trade in list(open_trades.items()):
        klines = get_klines_data(symbol, interval="5m", limit=200)
        if not klines:
            continue

        entry_price = trade["entry_price"]
        target_price = trade["target_price"]
        stop_price = trade["stop_loss_price"]
        amount = trade["amount"]
        invested = trade["invested_usdt"]

        entry_ms = trade.get("entry_ts")
        if entry_ms is None:
            entry_ms = int(datetime.strptime(trade["entry_time"], "%Y-%m-%d %H:%M:%S UTC")
                           .replace(tzinfo=timezone.utc).timestamp() * 1000)

        # Walk every candle since entry (not just the latest) so no TP/SL touch is missed.
        # The entry candle is skipped: its high/low may predate the entry.
        # If one candle touches both levels, assume the stop hit first (conservative).
        outcome = None
        for k in klines:
            if int(k[0]) + KLINE_MS <= entry_ms or int(k[0]) <= entry_ms - (entry_ms % KLINE_MS):
                continue
            high_price, low_price = float(k[2]), float(k[3])
            if low_price <= stop_price:
                outcome = ("SL", stop_price)
                break
            if high_price >= target_price:
                outcome = ("TP", target_price)
                break

        if outcome:
            kind, exit_price = outcome
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
            pnl_usdt = (exit_price - entry_price) * amount

            state["virtual_balance"] += (invested + pnl_usdt)
            state["stats"]["total_trades"] += 1
            state["stats"]["total_profit_usdt"] += pnl_usdt
            state["stats"]["wins" if kind == "TP" else "losses"] += 1
            state.setdefault("closed_trades", []).append({
                **trade,
                "exit_price": exit_price,
                "exit_time": now_str,
                "result": kind,
                "pnl_pct": round(pnl_pct, 4),
                "pnl_usdt": round(pnl_usdt, 4),
            })
            state["closed_trades"] = state["closed_trades"][-200:]

            win_rate = (state["stats"]["wins"] / state["stats"]["total_trades"]) * 100
            stats_lines = (
                f"🏦 <b>New Virtual Balance:</b> <code>${state['virtual_balance']:.2f} USDT</code>\n"
                f"🏆 <b>Overall Win Rate:</b> <code>{win_rate:.1f}%</code> ({state['stats']['wins']}/{state['stats']['total_trades']})\n"
                f"⏱️ <i>{now_str}</i>"
            )
            if kind == "TP":
                msg = (
                    f"🎯 <b>PROFIT TAKEN! ({html.escape(symbol)})</b>\n\n"
                    f"🟢 <b>Entry Price:</b> <code>${entry_price:.5f}</code>\n"
                    f"🔴 <b>Exit Price:</b> <code>${exit_price:.5f}</code>\n"
                    f"💰 <b>Net Profit:</b> <code>+{pnl_pct:.2f}%</code> (+${pnl_usdt:.2f} USDT)\n"
                    + stats_lines
                )
                print(f"✅ TP HIT: {symbol} at {exit_price}")
            else:
                msg = (
                    f"🛑 <b>STOP LOSS EXECUTED ({html.escape(symbol)})</b>\n\n"
                    f"🟢 <b>Entry Price:</b> <code>${entry_price:.5f}</code>\n"
                    f"🔴 <b>Exit Price:</b> <code>${exit_price:.5f}</code>\n"
                    f"📉 <b>Loss:</b> <code>{pnl_pct:.2f}%</code> (-${abs(pnl_usdt):.2f} USDT)\n"
                    + stats_lines
                )
                print(f"⚠️ SL HIT: {symbol} at {exit_price}")
            send_telegram(msg)
            closed_this_round.append(symbol)

        time.sleep(0.15)  # Rate limit protection

    for s in closed_this_round:
        del open_trades[s]

    # 2. CHECK FOR NEW BUY SIGNALS IF SLOTS ARE AVAILABLE
    available_slots = state["max_slots"] - len(open_trades)
    slot_size = state["slot_size"]

    if available_slots > 0 and state["virtual_balance"] >= slot_size:
        for symbol in COINS:
            if symbol in open_trades:
                continue
            if available_slots <= 0:
                break

            klines = get_klines_data(symbol, interval="5m", limit=30)
            if not klines or len(klines) < 22:
                continue

            closes = [float(k[4]) for k in klines]
            current_price = closes[-1]
            upper_bb, sma, lower_bb = calculate_bollinger_bands(closes, period=20, std_dev_mult=2.0)
            rsi = calculate_rsi(closes, period=14)

            if not lower_bb or not rsi:
                continue

            # STRATEGY ENTRY CONDITION:
            # 1. Price is at or below the lower Bollinger Band (Dip)
            # 2. RSI is oversold (RSI <= 32)
            if current_price <= lower_bb and rsi <= 32:
                amount = slot_size / current_price
                target_price = current_price * (1.0 + TAKE_PROFIT_PCT)
                stop_loss_price = current_price * (1.0 - STOP_LOSS_PCT)

                open_trades[symbol] = {
                    "symbol": symbol,
                    "entry_price": current_price,
                    "amount": amount,
                    "invested_usdt": slot_size,
                    "target_price": target_price,
                    "stop_loss_price": stop_loss_price,
                    "entry_rsi": round(rsi, 2),
                    "entry_time": now_str,
                    "entry_ts": int(datetime.now(timezone.utc).timestamp() * 1000)
                }
                state["virtual_balance"] -= slot_size
                available_slots -= 1

                msg = (
                    f"🟢 <b>NEW SPOT SCALP ENTERED! ({html.escape(symbol)})</b>\n\n"
                    f"💵 <b>Entry Price:</b> <code>${current_price:.5f}</code>\n"
                    f"🎯 <b>Take Profit (+0.6%):</b> <code>${target_price:.5f}</code>\n"
                    f"🛑 <b>Stop Loss (-2.5%):</b> <code>${stop_loss_price:.5f}</code>\n"
                    f"📊 <b>Indicator:</b> <code>RSI = {rsi:.1f}</code> (Oversold Dip)\n"
                    f"💼 <b>Slot Size:</b> <code>${slot_size:.2f} USDT</code>\n"
                    f"🏦 <b>Available Cash:</b> <code>${state['virtual_balance']:.2f} USDT</code>\n"
                    f"⏱️ <i>{now_str}</i>"
                )
                print(f"🚀 ENTERED: {symbol} at {current_price}")
                send_telegram(msg)

            time.sleep(0.15)  # Rate limit protection

    save_state(state)
    print(f"ℹ️ Scan complete. Active open trades: {len(open_trades)}/{state['max_slots']}")

if __name__ == "__main__":
    state = load_state()
    if "--loop" in sys.argv:
        print("🚀 MEXC Safe Scalper running in continuous loop mode (every 60s)...")
        while True:
            process_trades(state)
            time.sleep(60)
    else:
        process_trades(state)
