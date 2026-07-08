# Strategy Bot — Autonomous Trading Strategy Discovery & Validation

Finds, backtests, and validates trading strategies across crypto (CCXT Binance), forex/metals (MT5), and CSV data. Pipeline collects strategy ideas → backtests 14 templates across 13 symbols × 7 timeframes → validates via 20k-section cross-validation → locks winners → builds combined portfolio.

## Key Result

**TRXUSDT 1m Heikin-Ashi Momentum combined (lb=1,2,3) achieves 20% return in 7 days** with 0.25% fixed risk per trade, 10% max aggregate risk:
- 78.4% win rate, 2.75 reward:risk, 10.01 profit factor
- 6.2% max drawdown over 67 days
- 24.85 trades/day from 3 concurrent lookback variants
- Validated across all 10 independent 10k-bar sections (10/10 profitable, 8/10 hit 20% within 2 weeks)

## Files

| File | Purpose |
|------|---------|
| `backtest_engine.py` | Core engine: 14 strategy templates, fee models, position sizing, data fetching (MT5/CCXT/CSV) |
| `csv_scanner.py` | CSV pipeline: reads 91 tab-separated files, splits into 20k sections, cross-validates, 4-tier grading |
| `test_sections.py` | Section validation: runs TRX HA (lb=1,2,3) on each 10k-bar section independently |
| `equity_curve.py` | Full equity curve + weekly breakdown for all 10 sections |
| `combined_portfolio.py` | Combines TRX HA + all A/B-tier strategies into multi-asset portfolio |
| `portfolio_builder.py` | Monte Carlo portfolio simulator with 4 allocation schemes |
| `find_2week.py` | Exploration: tests all strategies for 20% return in 2 weeks |
| `test_2week.py` | Clean backtest: combined HA (lb=1,2,3) with correct capital accounting |
| `StrategyBot.mq5` | MT5 EA with 4 strategy templates |
| `orchestrator.py` | Pipeline orchestrator: collect → extract → backtest → lock |

## Data

- 91 CSV files in `data/` (13 symbols × 7 timeframes: 1m, 5m, 15m, 30m, 1h, 4h, daily)
- Tab-separated format: `date\time\topen\thigh\tlow\tclose\tvolume`
- TRXUSDT1.csv: 200k rows → deduplicated to 97,370 unique 1m bars (2026-05-01 to 2026-07-08)
- Covers: BTC, ETH, XRP, SOL, TRX, ADA, EURUSD, GBPUSD, USDJPY, USDCAD, XAUUSD, XAGUSD, (plus stock indices excluded per user preference)

## Database (`strategy_bot.db`)

- `backtest_results`: 6,487 results across 13 symbols × 7 TFs × 14 templates × params
- `locked_strategies`: 163 strategies (11 A-Exceeding, 139 B-Meeting, 12 C-Below, 1 D-Fail)
- See VERIFICATION.md for full schema and query examples

## Setup

```bash
pip install -r requirements.txt
# Configure .env with API keys (see .env.example)
python csv_scanner.py             # full scan
python test_sections.py           # section validation
python equity_curve.py            # equity over time
python combined_portfolio.py      # portfolio view
```

## Grade Thresholds

| Tier | WR | RR | PF | DD | TPY |
|------|-----|-----|-----|-----|-----|
| A-Exceeding | ≥50% | ≥2.0 | ≥1.5 | ≤25% | ≥5 |
| B-Meeting | ≥40% | ≥1.5 | ≥1.2 | ≤30% | ≥1 |
| C-Below | ≥35% | ≥1.2 | ≥1.0 | ≤35% | ≥0.1 |
| D-Fail | Everything else |

## Methodology

See `VERIFICATION.md` for detailed step-by-step verification guide.

## Portfolio Result (TRX 80% + 59 A/B-tier strategies)

| Scheme | Return | DD (ρ=0.2) | TPD |
|--------|--------|------------|-----|
| TRX HA alone | +495.3% | 6.2% | 24.85 |
| TRX 80% + others | +424.6% | 5.7% | 19.89 |
| Equal weight all 60 | +147.9% | 6.6% | 0.45 |
