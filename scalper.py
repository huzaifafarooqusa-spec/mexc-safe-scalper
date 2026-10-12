import os
import sys
import time
import json
import math
import ssl
import urllib.request
import urllib.error
from datetime import datetime, timezone

# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "8995329698:AAH8t_4777maZKsC50TTaHIdEFimh5V8cmM")
CHAT_ID = os.getenv("CHAT_ID", "714192775")

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
ssl_ctx = ssl._create_unverified_context()
HEADERS = {"User-Agent": "Mozilla/5.0 MEXCScalperBot/1.0"}

def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=12, context=ssl_ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))

def send_telegram(message):
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
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
            
    # Default initial state
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
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent_deltas = deltas[-period:]
    gains = [d for d in recent_deltas if d > 0]
    losses = [abs(d) for d in recent_deltas if d < 0]

    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

# --- TRADING LOGIC ---
def get_current_price(symbol):
    try:
        data = fetch_json(f"{MEXC_BASE_URL}/ticker/price?symbol={symbol}")
        return float(data["price"])
    except Exception as e:
        print(f"Error fetching price for {symbol}: {e}")
        return None

def get_klines_data(symbol, interval="5m", limit=35):
    try:
        url = f"{MEXC_BASE_URL}/klines?symbol={symbol}&interval={interval}&limit={limit}"
        data = fetch_json(url)
        # Kline format: [openTime, open, high, low, close, volume, ...]
        closes = [float(k[4]) for k in data]
        return closes
    except Exception as e:
        print(f"Error fetching klines for {symbol}: {e}")
        return None

def process_trades(state):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    open_trades = state.get("open_trades", {})
    closed_this_round = []

    # 1. CHECK OPEN POSITIONS (TAKE PROFIT / STOP LOSS)
    for symbol, trade in list(open_trades.items()):
        current_price = get_current_price(symbol)
        if not current_price:
            continue

        entry_price = trade["entry_price"]
        target_price = trade["target_price"]
        stop_price = trade["stop_loss_price"]
        amount = trade["amount"]
        invested = trade["invested_usdt"]

        # Calculate current PnL
        pnl_pct = ((current_price - entry_price) / entry_price) * 100
        pnl_usdt = (current_price - entry_price) * amount

        # Condition A: Take Profit Hit (+0.60% or higher)
        if current_price >= target_price:
            state["virtual_balance"] += (invested + pnl_usdt)
            state["stats"]["total_trades"] += 1
            state["stats"]["wins"] += 1
            state["stats"]["total_profit_usdt"] += pnl_usdt

            win_rate = (state["stats"]["wins"] / state["stats"]["total_trades"]) * 100

            msg = (
                f"🎯 *PROFIT TAKEN! ({symbol})*\n\n"
                f"🟢 *Entry Price:* `${entry_price:.5f}`\n"
                f"🔴 *Exit Price:* `${current_price:.5f}`\n"
                f"💰 *Net Profit:* `+{pnl_pct:.2f}%` (+${pnl_usdt:.2f} USDT)\n"
                f"🏦 *New Virtual Balance:* `${state['virtual_balance']:.2f} USDT`\n"
                f"🏆 *Overall Win Rate:* `{win_rate:.1f}%` ({state['stats']['wins']}/{state['stats']['total_trades']})\n"
                f"⏱️ _{now_str}_"
            )
            print(f"✅ TP HIT: {symbol} at {current_price}")
            send_telegram(msg)
            closed_this_round.append(symbol)

        # Condition B: Stop Loss Hit (-2.50%)
        elif current_price <= stop_price:
            state["virtual_balance"] += (invested + pnl_usdt)
            state["stats"]["total_trades"] += 1
            state["stats"]["losses"] += 1
            state["stats"]["total_profit_usdt"] += pnl_usdt

            win_rate = (state["stats"]["wins"] / state["stats"]["total_trades"]) * 100

            msg = (
                f"🛑 *STOP LOSS EXECUTED ({symbol})*\n\n"
                f"🟢 *Entry Price:* `${entry_price:.5f}`\n"
                f"🔴 *Exit Price:* `${current_price:.5f}`\n"
                f"📉 *Loss:* `{pnl_pct:.2f}%` (-${abs(pnl_usdt):.2f} USDT)\n"
                f"🏦 *New Virtual Balance:* `${state['virtual_balance']:.2f} USDT`\n"
                f"🏆 *Overall Win Rate:* `{win_rate:.1f}%` ({state['stats']['wins']}/{state['stats']['total_trades']})\n"
                f"⏱️ _{now_str}_"
            )
            print(f"⚠️ SL HIT: {symbol} at {current_price}")
            send_telegram(msg)
            closed_this_round.append(symbol)

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

            closes = get_klines_data(symbol, interval="5m", limit=30)
            if not closes or len(closes) < 22:
                continue

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
                    "entry_time": now_str
                }
                state["virtual_balance"] -= slot_size
                available_slots -= 1

                msg = (
                    f"🟢 *NEW SPOT SCALP ENTERED! ({symbol})*\n\n"
                    f"💵 *Entry Price:* `${current_price:.5f}`\n"
                    f"🎯 *Take Profit (+0.6%):* `${target_price:.5f}`\n"
                    f"🛑 *Stop Loss (-2.5%):* `${stop_loss_price:.5f}`\n"
                    f"📊 *Indicator:* `RSI = {rsi:.1f}` (Oversold Dip)\n"
                    f"💼 *Slot Size:* `${slot_size:.2f} USDT`\n"
                    f"🏦 *Available Cash:* `${state['virtual_balance']:.2f} USDT`\n"
                    f"⏱️ _{now_str}_"
                )
                print(f"🚀 ENTERED: {symbol} at {current_price}")
                send_telegram(msg)

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
        # Single execution mode for GitHub Actions
        process_trades(state)
