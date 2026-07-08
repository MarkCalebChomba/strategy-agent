# Quick reference for Strategy Bot

## Files in project
- `reddit_collector.py` - Reddit keyword search via RapidAPI (50 req/month budget)
- `blog_collector.py` - RSS blog fetcher (10 feeds, unlimited free)
- `extract_strategies.py` - AI extraction via OpenRouter (liquid 1.2B model)
- `backtest_engine.py` - 14 strategy templates, MT5 (FX/metals) + CCXT Binance (crypto) data, fee-aware, with stress test suite
- `csv_scanner.py` - CSV-based scanner: reads data/*.csv, splits into 20k sections, cross-validates, 4-tier grading system
- `orchestrator.py` - Pipeline tie-together with locking
- `clean_database.py` - Remove junk posts
- `data/` - 91 CSV files (13 symbols × 7 timeframes), tab-separated OHLCV
- `strategy_bot.db` - SQLite database (6487 backtest results, 163 locked strategies)
- `.env` - API keys and config

## Usage commands
- `python csv_scanner.py` - full autonomous CSV scan + stress test + lock
- `python csv_scanner.py --quick` - 1m/5m only scan
- `python csv_scanner.py --results 30` - show top 30 by TPD (with grades)
- `python csv_scanner.py --grade` - grade all existing results & locked
- `python csv_scanner.py --grade-locked A-Exceeding` - show A-tier locked
- `python backtest_engine.py --stress` - stress test locked strategies
- `python backtest_engine.py --results 10` - show top 10 by Sharpe
- `python orchestrator.py` - run one full cycle

## Database tables
- `raw_items` - collected posts (status: new/extracted/no_strategy/error)
- `strategies` - AI-extracted strategy specs
- `backtest_results` - 6487 backtest metrics per strategy/param combo
- `locked_strategies` - 163 strategies passing CSV validation (has profit_factor column)
- `extraction_log` - AI call audit trail

## Key .env values
```
RAPIDAPI_KEY=key
OPENROUTER_API_KEY=sk-or-v1-key
OPENROUTER_MODEL=liquid/lfm-2.5-1.2b-instruct:free
EXTRACT_MAX_PER_RUN=10
COLLECTOR_DB_PATH=strategy_bot.db
```

## Top A-Exceeding Locked Bots (WR≥50%, RR≥2.0, PF≥1.5, DD≤25%)
| # | Bot | Trades/yr | WR | RR | Return | DD | PF | TPD |
|---|-----|-----------|----|----|--------|----|----|-----|
| 1 | TRXUSDT 1m Heikin-Ashi Mom (lb=2) | 550 | 56.8% | 3.5 | +326% | 11.5% | 24.6 | **2.18** |
| 2 | TRXUSDT 1m Heikin-Ashi Mom (lb=3) | 448 | 54.3% | 3.1 | +164% | 13.6% | 14.0 | **1.78** |
| 3 | XAGUSD 4h Awesome Osc 5/21 | 40 | 50.0% | 2.4 | +36% | 7.7% | 2.9 | 0.16 |
| 4 | XAUUSD 4h Turtle 20/5 | 29 | 50.0% | 2.2 | +18% | 5.5% | 2.8 | 0.12 |
| 5 | XAUUSD 1h Keltner 20/14/2.5 | 24 | 57.5% | 2.3 | +50% | 12.8% | 3.4 | 0.10 |
| 6 | EURUSD 1h ATR Channel 10/3.0/20 | 13 | 50.0% | 2.9 | +11% | 3.9% | 3.0 | 0.05 |
| 7 | XAGUSD 4h Keltner 20/14/1.5 | 13 | 57.1% | 4.4 | +57% | 11.9% | 5.9 | 0.05 |

All validated with CSV data (20k-section cross-validation + stress tests). TRXUSDT 1m uses user-provided CSV data, not MT5.

## 4-Tier Grading System
- **A-Exceeding**: WR≥50%, RR≥2.0, PF≥1.5, DD≤25%, TPY≥5
- **B-Meeting**: WR≥40%, RR≥1.5, PF≥1.2, DD≤30%, TPY≥1
- **C-Below**: WR≥35%, RR≥1.2, PF≥1.0, DD≤35%, TPY≥0.1
- **D-Fail**: Everything else

Use `--grade` to see breakdown, `--grade-locked A-Exceeding` to filter by tier.

## Key decisions & findings
- Trade-off is fundamental: no strategy achieves WR≥50%, RR≥2.0, AND ≥3 TPD simultaneously across 14 templates × 13 assets × 7 TFs
- User chose "keep WR≥50%, lower TPD" → ~1-2 trades/month per bot is acceptable for high quality
- Heikin-Ashi Momentum on TRXUSDT 1m is the closest to 3+ TPD (2.18 TPD, WR=57%, RR=3.5)
- 14 strategy templates from je-suis-tm/quant-trading implemented in backtest_engine.py
- CSV data (13 symbols × 7 TFs, tab-separated) downloaded from user's PC is now the primary data source
- Stock indices (US30, SP500, NAS100, GER40, UK100, US500, USTEC) excluded

## Portfolio Builder (`portfolio_builder.py`)
- Combines multiple strategies into multi-asset portfolio with configurable allocation
- Shows 4 allocation schemes: equal weight, risk parity (1/DD), Sharpe-weighted, volume-weighted
- Uses correlated DD estimation (rho=0.2 assumed between strategies)
- Usage: `python portfolio_builder.py --detailed` (A-tier, all schemes)
  - `--tier B` for B-tier, `--tier A,B` for both
  - `--equal` for single equal-weight run only
  - `--years N` simulation length, `--sims N` Monte Carlo iterations

## Portfolio Findings (7 A-tier strategies)
| Allocation | Return | DD (rho=0.2) | Sharpe | TPY | TPD |
|-----------|--------|-------------|--------|-----|-----|
| Equal weight | 78.5% | 5.0% | 0.47 | 96 | 0.38 |
| Risk parity (1/DD) | 57.4% | 4.2% | 0.45 | 69 | 0.27 |
| Sharpe-weighted | 50.5% | 5.2% | 0.56 | 41 | 0.16 |
| Volume-weighted (TRX 81%) | 272.2% | 9.7% | 0.19 | 453 | **1.80** |

- Volume-weighted gets closest to 3 TPD target (1.80) by allocating 81% to TRXUSDT HA
- Trade-off: higher TPD = lower risk-adjusted metrics
- R-Breaker EURUSD 1m (B-tier, 109 TPY, 0.43 TPD, WR=53%, RR=1.71) is best candidate to add for higher TPD

## Current status (as of July 8, 2026)
- 6487 backtest results in DB across 13 symbols × 7 timeframes × 14 templates
- 163 locked strategies (11 A-Exceeding, 139 B-Meeting, 12 C-Below, 1 D-Fail)
- 91 CSV files in data/ (13 symbols × 7 TFs), tab-separated OHLCV from user's download folder
- No stock indices per user preference
