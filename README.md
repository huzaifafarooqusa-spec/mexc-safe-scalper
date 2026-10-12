# MEXC Safe Spot Scalper

Paper-trades a mean-reversion dip-buying strategy on MEXC spot (5m candles) and reports every
entry, exit and a daily summary to Telegram. **No real orders are placed.**

## Strategy

Buy a dip only inside an uptrend, at the close of a finished candle:

| Rule | Default |
|---|---|
| Close at or below the lower Bollinger Band (20, 2σ) | ✓ |
| Wilder RSI(14) ≤ 32 | ✓ |
| Close above EMA(200), so no buying into downtrends | ✓ |
| Skip candles with volume > 4× the 20-candle average (news / capitulation) | ✓ |
| Take profit (resting limit) | +1.5% |
| Stop loss | −2.0% (stop is checked first if one candle touches both) |
| Time exit | 12 h |
| Slots | 3 × $200 |
| Cooldown after a stop on the same coin | 2 h |
| Daily loss limit (no new entries) | −3% of equity |
| Costs | 0.05% taker, 0% maker, 0.05% slippage on market fills |

Every parameter lives in `strategy.DEFAULT_PARAMS`. Override any of them with a `config.json`
next to `scalper.py`, e.g. `{"max_slots": 2, "slot_size": 100}`.

## Backtest (90 days to 2026-10-12, 8 coins, fees and slippage included)

| | Trades | Win rate | Return | Profit factor | Max drawdown |
|---|---|---|---|---|---|
| Original strategy (+0.6% / −2.5%, no filters) | 830 | 80.6% | **−11.7%** | 0.86 | 14.8% |
| Original strategy with zero fees (unrealistic) | 877 | 82.2% | +8.5% | 1.11 | 5.8% |
| **Current defaults** | 126 | 67.5% | **+10.4%** | 1.88 | 2.3% |
| Current defaults, out-of-sample last 30 days | 48 | 72.9% | +6.0% | 2.46 | 2.0% |

The parameters were chosen by a walk-forward grid search over 960 combinations (train on the first
60 days, test on the last 30). The trend filter is the main thing that matters: **without it 0.2%
of combinations were profitable in both periods; with it, 87%.** The defaults sit in the middle
of the robust region rather than on the single best cell.

Caveats: 90 days is one market regime and ~126 trades is a small sample. The backtest assumes
every 5m candle is scanned; GitHub's scheduler skips runs, so live results will differ. Verify
your actual MEXC fee tier.

```bash
python backtest.py              # current defaults
python backtest.py --days 120   # longer history
python backtest.py --grid       # re-run the walk-forward search (~10 min)
```

## Running

**GitHub Actions** (default): `.github/workflows/scalper.yml` runs every 5 minutes, and
`BOT_TOKEN`/`CHAT_ID` come from the repo secrets. State is committed to the `state` branch so
`main` history stays clean. A failed run sends a Telegram alert.

**Always-on (recommended for 5m scalping):** GitHub cron is best-effort and often runs 10–30
minutes late. Run the loop on any small host instead:

```bash
docker build -t mexc-scalper .
docker run -d --restart=always -e BOT_TOKEN=... -e CHAT_ID=... -v scalper-data:/data mexc-scalper
```

Loop mode scans 5 seconds after each 5m candle closes. Disable the workflow schedule if you
do this, so the two don't trade the same paper account.

## Tests

```bash
python -m unittest discover -s tests -v
```
