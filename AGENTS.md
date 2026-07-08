# Quick reference for Strategy Bot

## Files in project
- `reddit_collector.py` - Reddit keyword search via RapidAPI (50 req/month budget)
- `blog_collector.py` - RSS blog fetcher (10 feeds, unlimited free)
- `extract_strategies.py` - AI extraction via OpenRouter (liquid 1.2B model)
- `backtest_engine.py` - 14 strategy templates, MT5 (FX/metals) + CCXT Binance (crypto) data, fee-aware, with stress test suite
- `csv_scanner.py` - CSV-based scanner: reads data/*.csv, splits into 20k sections, cross-validates, 4-tier grading system
- `orchestrator.py` - Pipeline tie-together with locking
- `clean_database.py` - Remove junk posts
- `test_sections.py` - Section validation (V2 with all bug fixes: stop-loss, fees, next-bar execution)
- `equity_curve.py` - Full equity curve (V2 with all bug fixes)
- `data/` - 91 CSV files (13 symbols x 7 timeframes), tab-separated OHLCV
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
- `python test_sections.py` - section validation (V2)
- `python equity_curve.py` - equity curve (V2)

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

## Top A-Exceeding Locked Bots (WR>=50%, RR>=2.0, PF>=1.5, DD<=25%)
| # | Bot | Trades/yr | WR | RR | Return | DD | PF | TPD |
|---|-----|-----------|----|----|--------|----|----|-----|
| 1 | XAGUSD 4h Awesome Osc 5/21 | 40 | 50.0% | 2.4 | +36% | 7.7% | 2.9 | 0.16 |
| 2 | XAUUSD 4h Turtle 20/5 | 29 | 50.0% | 2.2 | +18% | 5.5% | 2.8 | 0.12 |
| 3 | XAUUSD 1h Keltner 20/14/2.5 | 24 | 57.5% | 2.3 | +50% | 12.8% | 3.4 | 0.10 |
| 4 | EURUSD 1h ATR Channel 10/3.0/20 | 13 | 50.0% | 2.9 | +11% | 3.9% | 3.0 | 0.05 |
| 5 | XAGUSD 4h Keltner 20/14/1.5 | 13 | 57.1% | 4.4 | +57% | 11.9% | 5.9 | 0.05 |

TRXUSDT Heikin-Ashi Momentum entries removed from A-tier after V2 audit (WR dropped below 50% threshold, now B-Meeting). See Critical Context below.

## 4-Tier Grading System
- **A-Exceeding**: WR>=50%, RR>=2.0, PF>=1.5, DD<=25%, TPY>=5
- **B-Meeting**: WR>=40%, RR>=1.5, PF>=1.2, DD<=30%, TPY>=1
- **C-Below**: WR>=35%, RR>=1.2, PF>=1.0, DD<=35%, TPY>=0.1
- **D-Fail**: Everything else

Use `--grade` to see breakdown, `--grade-locked A-Exceeding` to filter by tier.

## Critical Context (V2 Corrected)

**TRXUSDT 1m Heikin-Ashi Momentum Combined (lb=1,2,3) - V2**
```
LB=1 only:    1409 trades, 48.4% WR, +174.5% ret, 1.5% DD, 2.24 RR, 2.10 PF, 21.03 TPD, 19d to 20%
LB=2 only:     426 trades, 44.8% WR, +50.0% ret, 3.2% DD, 2.15 RR, 1.75 PF, 6.36 TPD, 30d to 20%
LB=3 only:     349 trades, 43.3% WR, +28.6% ret, 6.5% DD, 1.95 RR, 1.48 PF, 5.21 TPD, 46d to 20%
Combined:     2184 trades, 46.9% WR, +253.1% ret, 4.1% DD, 2.14 RR, 1.89 PF, 32.60 TPD, 10d to 20%
```

All graded **B-Meeting** (WR<50%). V2 fixes applied: hard stop-loss enforcement, Binance 0.1% fee per trade, next-bar open execution, all lookbacks shown individually. Still profitable, hits 20% in 10 days (combined), but WR dropped from 78.4% to 46.9% and PF from 10.01 to 1.89 versus V1.

## Progress
**Done (V2 Audit Fixes Applied):**
- Hard stop-loss enforcement (was only used for sizing, now tracked and executed)
- Binance 0.1% fee per trade (0.2% round trip) deducted from each trade's PnL
- Next-bar open execution (signal on bar i, enter at bar i+1 open)
- All lookbacks shown individually + combined (no lb=1 cherry-picking)
- test_sections.py and equity_curve.py updated and re-run

**In Progress:**
- Re-grading locked strategies database with V2 corrections
- Re-running portfolio builder with corrected TRX metrics

## Key decisions & findings
- Trade-off is fundamental: no strategy achieves WR>=50%, RR>=2.0, AND >=3 TPD simultaneously across 14 templates x 13 assets x 7 TFs
- V2 audit corrected TRX HA combined from 78.4% WR/10.01 PF/24.85 TPD to 46.9% WR/1.89 PF/32.60 TPD (WR dropped below 50%, now B-Meeting)
- Combined TRX HA still achieves 20% in 10 days with 32.60 TPD - strongest TPD of any bot by far
- Individual lookbacks: lb=1 strongest (21.03 TPD), lb=2 (6.36 TPD), lb=3 weakest (5.21 TPD)
- 14 strategy templates from je-suis-tm/quant-trading implemented in backtest_engine.py
- CSV data (13 symbols x 7 TFs, tab-separated) downloaded from user's PC is now the primary data source
- Stock indices (US30, SP500, NAS100, GER40, UK100, US500, USTEC) excluded

## Portfolio Builder (`portfolio_builder.py`)
- Combines multiple strategies into multi-asset portfolio with configurable allocation
- Shows 4 allocation schemes: equal weight, risk parity (1/DD), Sharpe-weighted, volume-weighted
- Uses correlated DD estimation (rho=0.2 assumed between strategies)
- Usage: `python portfolio_builder.py --detailed` (A-tier, all schemes)
  - `--tier B` for B-tier, `--tier A,B` for both
  - `--equal` for single equal-weight run only
  - `--years N` simulation length, `--sims N` Monte Carlo iterations

## Portfolio Findings (5 A-tier strategies; V2 TRX re-run pending)
| Allocation | Return | DD (rho=0.2) | Sharpe | TPY | TPD |
|-----------|--------|-------------|--------|-----|-----|
| Equal weight | 78.5% | 5.0% | 0.47 | 96 | 0.38 |
| Risk parity (1/DD) | 57.4% | 4.2% | 0.45 | 69 | 0.27 |
| Sharpe-weighted | 50.5% | 5.2% | 0.56 | 41 | 0.16 |
| Volume-weighted (TRX 81%) | 272.2% | 9.7% | 0.19 | 453 | **1.80** |

Portfolio numbers above use V1 TRX metrics. V2 re-run pending - volume-weighted allocation will shift as TRX WR dropped from 78.4% to 46.9%.

## Current status (as of July 8, 2026)
- 6487 backtest results in DB across 13 symbols x 7 timeframes x 14 templates
- 163 locked strategies (11 A-Exceeding, 139 B-Meeting, 12 C-Below, 1 D-Fail) - TRX HA entries pending re-grade from A to B tier
- 91 CSV files in data/ (13 symbols x 7 TFs), tab-separated OHLCV from user's download folder
- No stock indices per user preference

## Next Steps
- Review V2 corrected results (WR drop from 78.4% to 46.9% is significant but system remains profitable)
- Re-run portfolio builder with V2 TRX combined metrics
- Re-grade and re-lock all strategies with V2 backtest engine
- Investigate if other symbols/timeframes have similar V1-to-V2 drops
