"""Backtest the scalper on historical MEXC 5m candles using the live strategy code.

    python backtest.py                 # run current DEFAULT_PARAMS on the last 90 days
    python backtest.py --days 120      # longer history
    python backtest.py --grid          # walk-forward parameter search (train 2/3, test 1/3)

Candles are cached in .cache/ so repeated runs don't hit the API.
"""
import argparse
import itertools
import json
import os
import sys
import time
import urllib.request
from multiprocessing import Pool

import strategy as st
from scalper import COINS, MEXC_BASE_URL, HEADERS, ssl_ctx

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
PAGE = 500  # MEXC max candles per request


def fetch_history(symbol, days, end_ms=None):
    os.makedirs(CACHE_DIR, exist_ok=True)
    end_ms = end_ms or (int(time.time() * 1000) // st.KLINE_MS) * st.KLINE_MS
    start_ms = end_ms - days * 86400 * 1000
    path = os.path.join(CACHE_DIR, f"{symbol}_{days}d_{end_ms // 3600000}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)

    candles, cursor = [], start_ms
    while cursor < end_ms:
        page_end = min(cursor + PAGE * st.KLINE_MS, end_ms)
        url = (f"{MEXC_BASE_URL}/klines?symbol={symbol}&interval=5m"
               f"&startTime={cursor}&endTime={page_end}&limit={PAGE}")
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20, context=ssl_ctx) as resp:
            batch = json.loads(resp.read().decode("utf-8"))
        candles.extend(batch)
        cursor = page_end
        time.sleep(0.1)

    # De-duplicate and keep only fully closed candles
    seen, clean = set(), []
    for k in sorted(candles, key=lambda k: k[0]):
        if k[0] not in seen and int(k[6]) < end_ms:
            seen.add(k[0])
            clean.append(k)
    with open(path, "w") as f:
        json.dump(clean, f)
    return clean


def simulate(data, p, start_ts=None, end_ts=None, indicators=None):
    """Portfolio simulation that mirrors scalper.process_trades step by step."""
    indicators = indicators or {s: st.compute_indicators(c, p) for s, c in data.items()}
    index = {s: {int(k[0]): i for i, k in enumerate(c)} for s, c in data.items()}
    timeline = sorted({int(k[0]) for c in data.values() for k in c})
    if start_ts:
        timeline = [t for t in timeline if t >= start_ts]
    if end_ts:
        timeline = [t for t in timeline if t < end_ts]

    state = st.new_state(p)
    all_trades = []  # live state caps closed_trades at 200; keep everything here
    peak = equity = state["virtual_balance"]
    max_dd = 0.0
    for t in timeline:
        st.roll_day(state, t)
        for sym, trade in list(state["open_trades"].items()):
            i = index[sym].get(t)
            if i is None or t < trade["entry_ts"]:
                continue
            hit = st.exit_check(trade, data[sym][i], p)
            if hit:
                all_trades.append(st.close_trade(state, sym, hit[0], hit[1], int(data[sym][i][6]), p))

        for sym in COINS:
            i = index.get(sym, {}).get(t)
            if i is None:
                continue
            ok, _ = st.entry_signal(indicators[sym], i, p)
            if ok and st.can_enter(state, sym, t, p)[0]:
                st.open_trade(state, sym, indicators[sym]["close"][i], int(data[sym][i][6]), p,
                              indicators[sym]["rsi"][i])

        # Mark-to-market equity for drawdown
        equity = state["virtual_balance"]
        for sym, trade in state["open_trades"].items():
            i = index[sym].get(t)
            px = indicators[sym]["close"][i] if i is not None else trade["entry_price"]
            equity += trade["amount"] * px
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)

    return report(state, all_trades, equity, max_dd, timeline)


def report(state, trades, equity, max_dd, timeline):
    s = state["stats"]
    gross_win = sum(t["pnl_usdt"] for t in trades if t["pnl_usdt"] > 0)
    gross_loss = -sum(t["pnl_usdt"] for t in trades if t["pnl_usdt"] <= 0)
    by_kind = {}
    for t in trades:
        by_kind[t["result"]] = by_kind.get(t["result"], 0) + 1
    days = (timeline[-1] - timeline[0]) / 86400000 if timeline else 0
    return {
        "days": round(days, 1),
        "trades": s["total_trades"],
        "win_rate": round(s["wins"] / s["total_trades"] * 100, 1) if s["total_trades"] else 0.0,
        "return_pct": round((equity / 1000.0 - 1) * 100, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else float("inf"),
        "max_dd_pct": round(max_dd * 100, 2),
        "fees_usdt": round(s["fees_usdt"], 2),
        "exits": by_kind,
    }


# --- Walk-forward grid -----------------------------------------------------------

GRID = {
    "tp_pct": [0.004, 0.006, 0.008, 0.01, 0.015],
    "sl_pct": [0.01, 0.015, 0.02, 0.025],
    "rsi_entry": [25.0, 30.0, 32.0],
    "trend_ema": [0, 200],
    "max_hold_candles": [0, 72, 144, 288],
    "vol_spike_max": [0.0, 4.0],
}

_DATA = _IND = _SPLIT = None


def _init(data, split):
    global _DATA, _IND, _SPLIT
    _DATA, _SPLIT = data, split
    _IND = {}
    for ema in GRID["trend_ema"]:
        p = {**st.DEFAULT_PARAMS, "trend_ema": ema}
        _IND[ema] = {s: st.compute_indicators(c, p) for s, c in data.items()}


def _eval(combo):
    p = {**st.DEFAULT_PARAMS, **combo}
    ind = _IND[p["trend_ema"]]
    train = simulate(_DATA, p, end_ts=_SPLIT, indicators=ind)
    test = simulate(_DATA, p, start_ts=_SPLIT, indicators=ind)
    return combo, train, test


def score(r):
    # Reward return, punish drawdown, ignore thin samples
    if r["trades"] < 30:
        return -1e9
    return r["return_pct"] / max(r["max_dd_pct"], 1.0)


def run_grid(data):
    timeline = sorted({int(k[0]) for c in data.values() for k in c})
    split = timeline[int(len(timeline) * 2 / 3)]
    combos = [dict(zip(GRID, v)) for v in itertools.product(*GRID.values())]
    print(f"Evaluating {len(combos)} parameter sets (train 2/3, test 1/3)...", file=sys.stderr)
    with Pool(initializer=_init, initargs=(data, split)) as pool:
        results = pool.map(_eval, combos, chunksize=8)
    results.sort(key=lambda r: score(r[1]), reverse=True)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json", help="write full results to this file")
    args = ap.parse_args()

    data = {}
    for sym in COINS:
        print(f"Loading {sym}...", file=sys.stderr)
        data[sym] = fetch_history(sym, args.days)

    if not args.grid:
        print(json.dumps(simulate(data, st.DEFAULT_PARAMS), indent=2))
        return

    results = run_grid(data)
    print(f"{'tp':>6} {'sl':>6} {'rsi':>4} {'ema':>4} {'hold':>5} {'vol':>4} | "
          f"{'TRAIN ret%':>10} {'dd%':>6} {'pf':>5} {'n':>4} | {'TEST ret%':>9} {'dd%':>6} {'pf':>5} {'n':>4}")
    for combo, tr, te in results[:args.top]:
        print(f"{combo['tp_pct']:>6} {combo['sl_pct']:>6} {combo['rsi_entry']:>4.0f} {combo['trend_ema']:>4} "
              f"{combo['max_hold_candles']:>5} {combo['vol_spike_max']:>4.0f} | "
              f"{tr['return_pct']:>10} {tr['max_dd_pct']:>6} {tr['profit_factor']:>5} {tr['trades']:>4} | "
              f"{te['return_pct']:>9} {te['max_dd_pct']:>6} {te['profit_factor']:>5} {te['trades']:>4}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f)


if __name__ == "__main__":
    main()
