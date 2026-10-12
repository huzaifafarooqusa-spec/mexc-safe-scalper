"""MEXC spot dip-scalper: paper trades a Bollinger/RSI mean-reversion strategy and
reports every entry, exit and a daily summary to Telegram.

    python scalper.py           # one scan (GitHub Actions / cron)
    python scalper.py --loop    # run forever, scanning right after each 5m candle closes

Environment:
    BOT_TOKEN, CHAT_ID   Telegram bot credentials (required)
    STATE_FILE           path to the state JSON (default: state.json next to this file)

Strategy parameters live in strategy.DEFAULT_PARAMS and can be overridden with a
config.json next to this file, e.g. {"tp_pct": 0.01, "max_slots": 2}.
"""
import html
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone

import strategy as st

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.getenv("STATE_FILE") or os.path.join(BASE_DIR, "state.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

MEXC_BASE_URL = "https://api.mexc.com/api/v3"
KLINE_LIMIT = 500  # MEXC max; enough warm-up for EMA200 and ~41h of exit history

# High-liquidity coins, checked in priority order
COINS = [
    "SEIUSDT",
    "FETUSDT",
    "RENDERUSDT",
    "DOGEUSDT",
    "TIAUSDT",
    "DOTUSDT",
    "WLDUSDT",
    "SOLUSDT",
]

ssl_ctx = ssl.create_default_context()
HEADERS = {"User-Agent": "Mozilla/5.0 MEXCScalperBot/2.0"}


def load_params():
    params = dict(st.DEFAULT_PARAMS)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            overrides = json.load(f)
        unknown = set(overrides) - set(params)
        if unknown:
            sys.exit(f"Unknown keys in config.json: {sorted(unknown)}")
        params.update(overrides)
    return params


def fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def get_klines(symbol):
    try:
        return fetch_json(f"{MEXC_BASE_URL}/klines?symbol={symbol}&interval=5m&limit={KLINE_LIMIT}")
    except Exception as e:
        print(f"❌ Error fetching klines for {symbol}: {e}")
        return None


def send_telegram(message_html):
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": message_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "TelegramBot/1.0"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
                return resp.status == 200
        except Exception as e:
            print(f"❌ Telegram error (attempt {attempt + 1}): {e}")
            time.sleep(2 ** attempt)
    return False


# --- State ---------------------------------------------------------------------

def load_state(p):
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return st.migrate_state(json.load(f), p)
        except Exception as e:
            print(f"⚠️ Could not read state ({e}); starting fresh")
    return st.new_state(p)


def save_state(state):
    os.makedirs(os.path.dirname(os.path.abspath(STATE_FILE)), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)  # atomic: never leave a half-written state file


# --- Messages ------------------------------------------------------------------

def fmt_price(x):
    return f"${x:.6g}"


def msg_entry(trade, state, p):
    s = html.escape(trade["symbol"])
    return (
        f"🟢 <b>NEW SPOT SCALP: {s}</b>\n\n"
        f"💵 <b>Entry:</b> <code>{fmt_price(trade['entry_price'])}</code>\n"
        f"🎯 <b>Target (+{p['tp_pct'] * 100:.2f}%):</b> <code>{fmt_price(trade['target_price'])}</code>\n"
        f"🛑 <b>Stop (-{p['sl_pct'] * 100:.2f}%):</b> <code>{fmt_price(trade['stop_loss_price'])}</code>\n"
        f"⌛ <b>Max hold:</b> <code>{p['max_hold_candles'] * 5 / 60:.0f}h</code>\n"
        f"📊 <b>RSI:</b> <code>{trade['entry_rsi']}</code>\n"
        f"💼 <b>Size:</b> <code>${trade['invested_usdt']:.2f}</code> · "
        f"<b>Cash left:</b> <code>${state['virtual_balance']:.2f}</code>\n"
        f"⏱️ <i>{trade['entry_time']}</i>"
    )


def msg_exit(rec, state):
    s = html.escape(rec["symbol"])
    head = {"TP": "🎯 <b>PROFIT TAKEN", "SL": "🛑 <b>STOP LOSS", "TIME": "⌛ <b>TIME EXIT"}[rec["result"]]
    stats = state["stats"]
    win_rate = stats["wins"] / stats["total_trades"] * 100 if stats["total_trades"] else 0.0
    sign = "+" if rec["pnl_usdt"] >= 0 else "-"
    return (
        f"{head}: {s}</b>\n\n"
        f"🟢 <b>Entry:</b> <code>{fmt_price(rec['entry_price'])}</code>\n"
        f"🔴 <b>Exit:</b> <code>{fmt_price(rec['exit_price'])}</code>\n"
        f"💰 <b>Net P&amp;L (after fees):</b> <code>{sign}{abs(rec['pnl_pct']):.2f}%</code> "
        f"({sign}${abs(rec['pnl_usdt']):.2f})\n"
        f"🏦 <b>Balance:</b> <code>${state['virtual_balance']:.2f}</code>\n"
        f"🏆 <b>Win rate:</b> <code>{win_rate:.1f}%</code> ({stats['wins']}/{stats['total_trades']})\n"
        f"⏱️ <i>{rec['exit_time']}</i>"
    )


def msg_daily(state, prices, date):
    stats = state["stats"]
    equity = state["virtual_balance"]
    lines = []
    for sym, t in state["open_trades"].items():
        px = prices.get(sym, t["entry_price"])
        equity += t["amount"] * px
        lines.append(f"• {html.escape(sym)}: <code>{(px / t['entry_price'] - 1) * 100:+.2f}%</code>")
    today = [t for t in state["closed_trades"] if t["exit_time"].startswith(date)]
    win_rate = stats["wins"] / stats["total_trades"] * 100 if stats["total_trades"] else 0.0
    return (
        f"📅 <b>Daily scalper report: {date}</b>\n\n"
        f"🏦 <b>Equity:</b> <code>${equity:.2f}</code> (cash ${state['virtual_balance']:.2f})\n"
        f"📈 <b>Closed yesterday:</b> <code>{len(today)}</code> trades, "
        f"<code>{sum(t['pnl_usdt'] for t in today):+.2f}</code> USDT\n"
        f"🏆 <b>All-time:</b> <code>{stats['total_trades']}</code> trades, "
        f"<code>{win_rate:.1f}%</code> wins, <code>{stats['total_profit_usdt']:+.2f}</code> USDT, "
        f"fees <code>{stats['fees_usdt']:.2f}</code>\n"
        f"📂 <b>Open:</b> {len(state['open_trades'])}\n" + ("\n".join(lines) + "\n" if lines else "") +
        "✅ <i>Bot is alive</i>"
    )


# --- Main scan -----------------------------------------------------------------

def process_trades(state, p):
    now_ms = int(time.time() * 1000)
    symbols = list(dict.fromkeys(list(state["open_trades"]) + COINS))
    candles = {}
    for sym in symbols:
        raw = get_klines(sym)
        if raw:
            candles[sym] = st.closed_candles(raw, now_ms)
        time.sleep(0.1)
    if not candles:
        raise RuntimeError("Could not fetch candles for any symbol")

    st.roll_day(state, now_ms)
    prices = {s: float(c[-1][4]) for s, c in candles.items() if c}

    # Daily summary for the previous day, once
    today = state["day"]["date"]
    if state.get("last_report_date") != today:
        yesterday = datetime.fromtimestamp(now_ms / 1000 - 86400, tz=timezone.utc).strftime("%Y-%m-%d")
        if state.get("last_report_date") is not None:
            send_telegram(msg_daily(state, prices, yesterday))
        state["last_report_date"] = today

    # 1. Exits: walk every closed candle since entry so no touch is missed between runs
    for sym, trade in list(state["open_trades"].items()):
        for k in candles.get(sym, []):
            if int(k[0]) < trade["entry_ts"]:
                continue
            hit = st.exit_check(trade, k, p)
            if hit:
                rec = st.close_trade(state, sym, hit[0], hit[1], int(k[6]), p)
                print(f"{'✅' if rec['pnl_usdt'] > 0 else '⚠️'} {hit[0]}: {sym} at {rec['exit_price']}")
                send_telegram(msg_exit(rec, state))
                break

    # 2. Entries on the last closed candle
    limit_hit_before = state.get("loss_limit_alerted") == today
    for sym in COINS:
        c = candles.get(sym)
        if not c:
            continue
        ind = st.compute_indicators(c, p)
        i = len(c) - 1
        ok, reason = st.entry_signal(ind, i, p)
        rsi, lower = ind["rsi"][i], ind["lower_bb"][i]
        if rsi is not None and lower is not None:
            print(f"  {sym:<11} close={ind['close'][i]:<10.6g} lowerBB={lower:<10.6g} rsi={rsi:5.1f} -> {reason}")
        if not ok:
            continue
        allowed, why = st.can_enter(state, sym, now_ms, p)
        if not allowed:
            print(f"  ↳ signal on {sym} skipped: {why}")
            if why == "daily loss limit" and not limit_hit_before:
                send_telegram(f"⛔ <b>Daily loss limit reached</b> ({p['daily_loss_limit_pct'] * 100:.1f}%). "
                              f"No new entries until 00:00 UTC.")
                state["loss_limit_alerted"] = today
                limit_hit_before = True
            continue
        trade = st.open_trade(state, sym, ind["close"][i], int(c[i][6]), p, rsi)
        print(f"🚀 ENTERED: {sym} at {trade['entry_price']}")
        send_telegram(msg_entry(trade, state, p))

    save_state(state)
    print(f"ℹ️ Scan complete. Open trades: {len(state['open_trades'])}/{p['max_slots']}, "
          f"balance ${state['virtual_balance']:.2f}")


def seconds_to_next_candle():
    return st.KLINE_MS / 1000 - (time.time() % (st.KLINE_MS / 1000)) + 5


def main():
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("BOT_TOKEN and CHAT_ID environment variables are required")
    p = load_params()
    state = load_state(p)
    if "--loop" not in sys.argv:
        process_trades(state, p)
        return
    print("🚀 MEXC scalper running in loop mode (scans after each 5m close)...")
    while True:
        try:
            process_trades(state, p)
        except Exception as e:
            print(f"❌ Scan failed: {e}")
        time.sleep(seconds_to_next_candle())


if __name__ == "__main__":
    main()
