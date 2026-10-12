import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy as st  # noqa: E402

P = dict(st.DEFAULT_PARAMS)
T0 = 1_700_000_000_000 - (1_700_000_000_000 % st.KLINE_MS)


def candle(i, o, h, l, c, vol=1000.0):
    open_t = T0 + i * st.KLINE_MS
    return [open_t, str(o), str(h), str(l), str(c), "1", open_t + st.KLINE_MS - 1, str(vol)]


class Indicators(unittest.TestCase):
    def test_rsi_extremes(self):
        up = [float(i) for i in range(1, 40)]
        self.assertEqual(st.rsi_series(up, 14)[-1], 100.0)
        down = list(reversed(up))
        self.assertAlmostEqual(st.rsi_series(down, 14)[-1], 0.0)

    def test_rsi_matches_wilder_reference(self):
        # Classic Wilder example (New Concepts in Technical Trading Systems): first RSI ~70.53
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
                  45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
        rsi = st.rsi_series(closes, 14)
        self.assertIsNone(rsi[13])
        self.assertAlmostEqual(rsi[14], 70.46, delta=0.2)
        self.assertAlmostEqual(rsi[19], 57.9, delta=1.0)

    def test_bollinger_flat_series(self):
        upper, mid, lower = st.bollinger_series([10.0] * 25, 20, 2.0)
        self.assertEqual((upper[-1], mid[-1], lower[-1]), (10.0, 10.0, 10.0))
        self.assertIsNone(lower[18])

    def test_ema_constant(self):
        self.assertAlmostEqual(st.ema_series([5.0] * 30, 10)[-1], 5.0)


class Candles(unittest.TestCase):
    def test_closed_candles_drops_forming(self):
        ks = [candle(0, 1, 1, 1, 1), candle(1, 1, 1, 1, 1)]
        now = ks[1][0] + 1000  # inside the second candle
        self.assertEqual(st.closed_candles(ks, now), ks[:1])


class Exits(unittest.TestCase):
    def setUp(self):
        self.trade = {"entry_price": 100.0, "target_price": 101.5, "stop_loss_price": 98.0, "entry_ts": T0}

    def test_stop_first_when_both_touched(self):
        self.assertEqual(st.exit_check(self.trade, candle(1, 100, 102, 97, 100), P), ("SL", 98.0))

    def test_gap_through_stop_fills_at_open(self):
        self.assertEqual(st.exit_check(self.trade, candle(1, 96, 97, 95, 96), P), ("SL", 96.0))

    def test_take_profit(self):
        self.assertEqual(st.exit_check(self.trade, candle(1, 100, 101.6, 99.5, 101), P), ("TP", 101.5))

    def test_time_exit(self):
        p = {**P, "max_hold_candles": 3}
        self.assertIsNone(st.exit_check(self.trade, candle(2, 100, 100.5, 99.5, 100), p))
        self.assertEqual(st.exit_check(self.trade, candle(3, 100, 100.5, 99.5, 100.2), p), ("TIME", 100.2))


class Portfolio(unittest.TestCase):
    def test_round_trip_pnl_includes_fees_and_slippage(self):
        p = {**P, "taker_fee": 0.001, "maker_fee": 0.0, "slippage": 0.0, "slot_size": 100.0}
        s = st.new_state(p)
        t = st.open_trade(s, "X", 10.0, T0, p)
        self.assertAlmostEqual(t["amount"], 99.9 / 10.0)
        rec = st.close_trade(s, "X", "TP", t["target_price"], T0 + st.KLINE_MS, p)
        expected = 9.99 * 10.0 * (1 + p["tp_pct"]) - 100.0
        self.assertAlmostEqual(rec["pnl_usdt"], round(expected, 4))
        self.assertAlmostEqual(s["virtual_balance"], 1000.0 + expected)
        self.assertEqual(s["stats"]["wins"], 1)

    def test_cooldown_after_stop(self):
        s = st.new_state(P)
        st.open_trade(s, "X", 10.0, T0, P)
        st.close_trade(s, "X", "SL", 9.0, T0, P)
        self.assertEqual(st.can_enter(s, "X", T0 + st.KLINE_MS, P), (False, "cooldown"))
        later = T0 + (P["cooldown_candles"] + 1) * st.KLINE_MS
        st.roll_day(s, later)
        self.assertTrue(st.can_enter(s, "X", later, P)[0])

    def test_daily_loss_limit(self):
        p = {**P, "cooldown_candles": 0, "daily_loss_limit_pct": 0.01}
        s = st.new_state(p)
        st.roll_day(s, T0)
        st.open_trade(s, "X", 10.0, T0, p)
        st.close_trade(s, "X", "SL", 9.0, T0, p)  # ~ -20 USDT on a 1000 account
        self.assertEqual(st.can_enter(s, "Y", T0, p), (False, "daily loss limit"))

    def test_slot_limit(self):
        p = {**P, "max_slots": 1}
        s = st.new_state(p)
        st.open_trade(s, "X", 10.0, T0, p)
        self.assertEqual(st.can_enter(s, "Y", T0, p), (False, "no free slot"))

    def test_migrate_legacy_state(self):
        legacy = {
            "virtual_balance": 800.0,
            "open_trades": {"X": {"symbol": "X", "entry_price": 1.0, "amount": 200.0, "invested_usdt": 200.0,
                                  "target_price": 1.006, "stop_loss_price": 0.975,
                                  "entry_time": "2026-10-12 14:18:46 UTC"}},
            "closed_trades": [],
            "stats": {"total_trades": 1, "wins": 0, "losses": 1, "total_profit_usdt": -5.0},
        }
        s = st.migrate_state(legacy, P)
        self.assertIn("cooldowns", s)
        self.assertEqual(s["stats"]["fees_usdt"], 0.0)
        self.assertEqual(s["open_trades"]["X"]["entry_ts"], 1791814726000)


class Signal(unittest.TestCase):
    def ind(self, close=99.0, lower=100.0, rsi=25.0, ema=95.0, vol=1.0, vol_avg=1.0):
        return {"close": [close], "lower_bb": [lower], "rsi": [rsi], "ema": [ema], "vol": [vol], "vol_avg": [vol_avg]}

    def test_dip_in_uptrend_enters(self):
        self.assertEqual(st.entry_signal(self.ind(), 0, P), (True, "ok"))

    def test_filters(self):
        cases = {
            "above lower band": self.ind(close=101.0),
            "rsi not oversold": self.ind(rsi=40.0),
            "below trend ema": self.ind(ema=99.5),
            "volume spike": self.ind(vol=10.0),
            "warmup": self.ind(rsi=None),
        }
        for reason, ind in cases.items():
            self.assertEqual(st.entry_signal(ind, 0, P), (False, reason))

    def test_filters_can_be_disabled(self):
        p = {**P, "trend_ema": 0, "vol_spike_max": 0}
        self.assertTrue(st.entry_signal(self.ind(ema=None, vol=10.0), 0, p)[0])

    def test_compute_indicators_shapes(self):
        ks = [candle(i, 100 + i % 3, 101 + i % 3, 99 + i % 3, 100 + i % 3) for i in range(250)]
        ind = st.compute_indicators(ks, P)
        self.assertEqual({len(v) for v in ind.values()}, {250})
        self.assertIsNone(ind["ema"][198])
        self.assertIsNotNone(ind["ema"][199])
        self.assertIsNone(ind["vol_avg"][19])
        self.assertEqual(ind["vol_avg"][20], 1000.0)


if __name__ == "__main__":
    unittest.main()
