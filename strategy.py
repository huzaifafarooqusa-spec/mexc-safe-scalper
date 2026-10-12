"""Strategy core shared by the live scanner (scalper.py) and the backtester (backtest.py).

Everything here is pure Python and side-effect free so that live trading and
backtests make decisions with exactly the same code.

Kline format (MEXC /api/v3/klines):
    [openTime, open, high, low, close, volume, closeTime, quoteVolume]
"""
import math
from datetime import datetime, timezone

KLINE_MS = 5 * 60 * 1000

# Parameters chosen from the walk-forward backtest (see README / backtest.py).
DEFAULT_PARAMS = {
    # Entry
    "bb_period": 20,
    "bb_std": 2.0,
    "rsi_period": 14,
    "rsi_entry": 32.0,          # enter when RSI <= this
    "trend_ema": 200,           # only buy dips while close > EMA(n); 0 disables
    "vol_period": 20,
    "vol_spike_max": 4.0,       # skip capitulation/news candles (volume > n x average); 0 disables
    # Exit
    "tp_pct": 0.015,
    "sl_pct": 0.02,
    "max_hold_candles": 144,    # 12h on 5m candles; 0 disables time exit
    # Costs (MEXC spot: verify current fees for your account tier)
    "taker_fee": 0.0005,
    "maker_fee": 0.0,
    "slippage": 0.0005,
    # Portfolio / risk
    "slot_size": 200.0,
    "max_slots": 3,
    "cooldown_candles": 24,     # 2h lockout on a symbol after a stop loss
    "daily_loss_limit_pct": 0.03,  # stop new entries after -3% realized in a UTC day
}


# --- Indicators (full series, so live and backtest agree) ---------------------

def sma_series(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def bollinger_series(closes, period=20, mult=2.0):
    """Return (upper, middle, lower) lists; None until enough data."""
    mid = sma_series(closes, period)
    upper = [None] * len(closes)
    lower = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        m = mid[i]
        var = sum((x - m) ** 2 for x in closes[i - period + 1:i + 1]) / period
        sd = math.sqrt(var)
        upper[i] = m + mult * sd
        lower[i] = m - mult * sd
    return upper, mid, lower


def rsi_series(closes, period=14):
    """Wilder's smoothed RSI (matches TradingView)."""
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def _rsi(avg_gain, avg_loss):
    if avg_loss == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def ema_series(values, period):
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    out[period - 1] = ema
    for i in range(period, len(values)):
        ema = values[i] * k + ema * (1 - k)
        out[i] = ema
    return out


def compute_indicators(candles, p):
    """Indicator series for a list of closed candles."""
    closes = [float(k[4]) for k in candles]
    volumes = [float(k[7]) if len(k) > 7 else float(k[5]) * float(k[4]) for k in candles]
    _, _, lower = bollinger_series(closes, p["bb_period"], p["bb_std"])
    return {
        "close": closes,
        "lower_bb": lower,
        "rsi": rsi_series(closes, p["rsi_period"]),
        "ema": ema_series(closes, p["trend_ema"]) if p["trend_ema"] else [None] * len(closes),
        "vol": volumes,
        # Average of the *previous* n candles, so the signal candle is compared to its history
        "vol_avg": [None] + sma_series(volumes, p["vol_period"])[:-1],
    }


# --- Decisions -----------------------------------------------------------------

def closed_candles(klines, now_ms):
    """Drop the still-forming candle so signals never repaint."""
    return [k for k in klines if int(k[6]) < now_ms]


def entry_signal(ind, i, p):
    """Return (ok, reason) for a long entry at the close of candle i."""
    close, lower, rsi = ind["close"][i], ind["lower_bb"][i], ind["rsi"][i]
    if lower is None or rsi is None:
        return False, "warmup"
    if close > lower:
        return False, "above lower band"
    if rsi > p["rsi_entry"]:
        return False, "rsi not oversold"
    if p["trend_ema"]:
        ema = ind["ema"][i]
        if ema is None:
            return False, "warmup"
        if close < ema:
            return False, "below trend ema"
    if p["vol_spike_max"]:
        avg = ind["vol_avg"][i]
        if avg and ind["vol"][i] > p["vol_spike_max"] * avg:
            return False, "volume spike"
    return True, "ok"


def exit_check(trade, candle, p):
    """Check one closed candle after entry. Return (kind, price) or None.

    Stop is checked before target: if a candle touches both we assume the worse
    outcome. A gap through the stop fills at the open.
    """
    open_, high, low, close = (float(x) for x in candle[1:5])
    if low <= trade["stop_loss_price"]:
        return "SL", min(open_, trade["stop_loss_price"])
    if high >= trade["target_price"]:
        return "TP", trade["target_price"]
    max_hold = p["max_hold_candles"]
    if max_hold and int(candle[0]) - trade["entry_ts"] >= max_hold * KLINE_MS:
        return "TIME", close
    return None


# --- Portfolio (state is a plain JSON-able dict) -------------------------------

def new_state(p, balance=1000.0):
    return {
        "virtual_balance": balance,
        "open_trades": {},
        "closed_trades": [],
        "cooldowns": {},
        "day": {"date": None, "start_equity": balance, "pnl": 0.0},
        "stats": {"total_trades": 0, "wins": 0, "losses": 0, "total_profit_usdt": 0.0, "fees_usdt": 0.0},
    }


def migrate_state(state, p):
    """Fill in keys missing from older state files."""
    fresh = new_state(p, state.get("virtual_balance", 1000.0))
    for key, val in fresh.items():
        state.setdefault(key, val)
    for key, val in fresh["stats"].items():
        state["stats"].setdefault(key, val)
    for t in state["open_trades"].values():
        if "entry_ts" not in t:
            t["entry_ts"] = int(datetime.strptime(t["entry_time"], "%Y-%m-%d %H:%M:%S UTC")
                                .replace(tzinfo=timezone.utc).timestamp() * 1000)
    return state


def equity_at_cost(state):
    return state["virtual_balance"] + sum(t["invested_usdt"] for t in state["open_trades"].values())


def roll_day(state, ts_ms):
    date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    if state["day"]["date"] != date:
        state["day"] = {"date": date, "start_equity": equity_at_cost(state), "pnl": 0.0}


def can_enter(state, symbol, ts_ms, p):
    if symbol in state["open_trades"]:
        return False, "already open"
    if len(state["open_trades"]) >= p["max_slots"]:
        return False, "no free slot"
    if state["virtual_balance"] < p["slot_size"]:
        return False, "insufficient balance"
    if state["cooldowns"].get(symbol, 0) > ts_ms:
        return False, "cooldown"
    day = state["day"]
    if p["daily_loss_limit_pct"] and day["pnl"] <= -p["daily_loss_limit_pct"] * day["start_equity"]:
        return False, "daily loss limit"
    return True, "ok"


def open_trade(state, symbol, signal_close, ts_ms, p, rsi=None):
    """Simulate a market buy at the signal candle's close (plus slippage and taker fee)."""
    entry = signal_close * (1 + p["slippage"])
    invested = p["slot_size"]
    fee = invested * p["taker_fee"]
    trade = {
        "symbol": symbol,
        "entry_price": entry,
        "amount": (invested - fee) / entry,
        "invested_usdt": invested,
        "entry_fee_usdt": fee,
        "target_price": entry * (1 + p["tp_pct"]),
        "stop_loss_price": entry * (1 - p["sl_pct"]),
        "entry_rsi": round(rsi, 2) if rsi is not None else None,
        "entry_ts": ts_ms,
        "entry_time": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    state["open_trades"][symbol] = trade
    state["virtual_balance"] -= invested
    return trade


def close_trade(state, symbol, kind, price, ts_ms, p):
    """Close at `price`. TP is a resting limit (maker); SL/TIME are market (taker + slippage)."""
    trade = state["open_trades"].pop(symbol)
    if kind == "TP":
        fill, fee_rate = price, p["maker_fee"]
    else:
        fill, fee_rate = price * (1 - p["slippage"]), p["taker_fee"]
    gross = trade["amount"] * fill
    exit_fee = gross * fee_rate
    proceeds = gross - exit_fee
    pnl = proceeds - trade["invested_usdt"]

    state["virtual_balance"] += proceeds
    s = state["stats"]
    s["total_trades"] += 1
    s["wins" if pnl > 0 else "losses"] += 1
    s["total_profit_usdt"] += pnl
    s["fees_usdt"] += trade.get("entry_fee_usdt", 0.0) + exit_fee
    roll_day(state, ts_ms)
    state["day"]["pnl"] += pnl
    if kind == "SL" and p["cooldown_candles"]:
        state["cooldowns"][symbol] = ts_ms + p["cooldown_candles"] * KLINE_MS

    record = {
        **trade,
        "exit_price": fill,
        "exit_ts": ts_ms,
        "exit_time": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "result": kind,
        "pnl_usdt": round(pnl, 4),
        "pnl_pct": round(pnl / trade["invested_usdt"] * 100, 4),
    }
    state["closed_trades"].append(record)
    state["closed_trades"] = state["closed_trades"][-200:]
    return record
