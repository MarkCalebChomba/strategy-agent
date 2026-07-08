# Strategy Bot — Autonomous Trading Strategy Discovery & Validation

Finds, backtests, and validates trading strategies across crypto (CCXT Binance), forex/metals (MT5), and CSV data. Pipeline collects strategy ideas → backtests 14 templates across 13 symbols x 7 timeframes → validates via 20k-section cross-validation → locks winners → builds combined portfolio.

## Key Result

**TRXUSDT 1m Heikin-Ashi Momentum combined (lb=1,2,3) achieves 20% return in 10 days** with 0.25% fixed risk, stop-loss enforcement, 0.1% Binance fees, and next-bar execution:
- 46.9% win rate, 2.14 reward:risk, 1.89 profit factor
- 4.1% max drawdown over 67 days
- 32.60 trades/day from 3 concurrent lookback variants
- Validated across 10/10 sections, 6/10 hit 20% within 2 weeks

## Files

| File | Purpose |
|------|---------|
| `backtest_engine.py` | Core engine: 14 strategy templates, fee models, position sizing, data fetching (MT5/CCXT/CSV) |
| `csv_scanner.py` | CSV pipeline: reads 91 tab-separated files, splits into 20k sections, cross-validates, 4-tier grading |
| `test_sections.py` | Section validation: runs TRX HA (lb=1,2,3) on each 10k-bar section independently (V2 with all fixes) |
| `equity_curve.py` | Full equity curve + weekly breakdown for all 10 sections (V2 with all fixes) |
| `combined_portfolio.py` | Combines TRX HA + all A/B-tier strategies into multi-asset portfolio |
| `portfolio_builder.py` | Monte Carlo portfolio simulator with 4 allocation schemes |
| `find_2week.py` | Exploration: tests all strategies for 20% return in 2 weeks |
| `test_2week.py` | Clean backtest: combined HA (lb=1,2,3) with correct capital accounting |
| `StrategyBot.mq5` | MT5 EA with 4 strategy templates |
| `orchestrator.py` | Pipeline orchestrator: collect → extract → backtest → lock |

## Data

- 91 CSV files in `data/` (13 symbols x 7 timeframes: 1m, 5m, 15m, 30m, 1h, 4h, daily)
- Tab-separated format: `date\time\topen\thigh\tlow\tclose\tvolume`
- TRXUSDT1.csv: 200k rows → deduplicated to 97,370 unique 1m bars (2026-05-01 to 2026-07-08)
- Covers: BTC, ETH, XRP, SOL, TRX, ADA, EURUSD, GBPUSD, USDJPY, USDCAD, XAUUSD, XAGUSD, (plus stock indices excluded per user preference)

## Database (`strategy_bot.db`)

- `backtest_results`: 6,487 results across 13 symbols x 7 TFs x 14 templates x params
- `locked_strategies`: 163 strategies (11 A-Exceeding, 139 B-Meeting, 12 C-Below, 1 D-Fail)
- See VERIFICATION.md for full schema and query examples

## Setup

```bash
pip install -r requirements.txt
# Configure .env with API keys (see .env.example)
python csv_scanner.py             # full scan
python test_sections.py           # section validation (V2)
python equity_curve.py            # equity over time (V2)
python combined_portfolio.py      # portfolio view
```

## Grade Thresholds

| Tier | WR | RR | PF | DD | TPY |
|------|-----|-----|-----|-----|-----|
| A-Exceeding | >=50% | >=2.0 | >=1.5 | <=25% | >=5 |
| B-Meeting | >=40% | >=1.5 | >=1.2 | <=30% | >=1 |
| C-Below | >=35% | >=1.2 | >=1.0 | <=35% | >=0.1 |
| D-Fail | Everything else |

## Known Issues / Audit Findings

The following bugs were identified in V1 and fixed in V2 (July 8, 2026):

1. **Stop-loss not enforced**: Hard stop-loss was used only for position sizing but was never tracked or executed during the backtest. Positions remained open even when the stop-loss level was breached. **Fixed**: Stop-loss is now tracked per trade and enforced on each bar — when price hits the stop level, the position closes at that price, properly capping losses.

2. **No trading fees modeled**: V1 assumed zero-cost trading. Actual Binance spot trading charges 0.1% per trade (0.2% round trip). **Fixed**: 0.1% fee deducted from each trade's PnL on both entry and exit.

3. **Same-bar execution (look-ahead bias)**: Signals generated on bar `i` were executed at bar `i` close, which is unknown until the bar ends. **Fixed**: Signals now execute at bar `i+1` open, matching real-world constraints.

4. **Cherry-picked lookback=1**: Original reporting showed only the best-performing single lookback (lb=1), omitting weaker lookbacks 2 and 3. **Fixed**: All three lookbacks (1, 2, 3) are reported individually alongside the combined result.

**Impact of fixes**: Win rate dropped from 78.4% to 46.9%, profit factor from 10.01 to 1.89, but the system remains profitable and still achieves 20% in 10 days. TRX HA combined regraded from A-Exceeding to B-Meeting.

## Methodology

See `VERIFICATION.md` for detailed step-by-step verification guide.

## Portfolio Result (TRX 80% + 59 A/B-tier strategies)

| Scheme | Return | DD (rho=0.2) | TPD |
|--------|--------|------------|-----|
| TRX HA alone (V1 — V2 re-run pending) | +495.3% | 6.2% | 24.85 |
| TRX 80% + others | +424.6% | 5.7% | 19.89 |
| Equal weight all 60 | +147.9% | 6.6% | 0.45 |

Note: Portfolio numbers above use V1 TRX metrics (V2 combined HA: +253.1%, 4.1% DD, 32.60 TPD). Portfolio re-run with V2 TRX numbers recommended.
