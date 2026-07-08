# Verification Guide

This document tells an independent verifier (human or AI) how to check every claim made in this project.

## 1. Data Integrity

### 1.1 CSV File Structure
- 91 files in `data/` named `{SYMBOL}{TF}.csv` (e.g., `TRXUSDT1.csv` = TRXUSDT 1-minute)
- TF encoding: 1=1m, 5=5m, 15=15m, 30=30m, 60=1h, 240=4h, 1440=daily
- Format: tab-separated, no header: `date\ttime\topen\thigh\tlow\tclose\tvolume`
- **Verify**: Count files, check format, spot-check timestamps

### 1.2 Deduplication
- `TRXUSDT1.csv` had 200,000 rows but only 97,370 unique timestamps
- Remaining 102,630 rows were exact OHLC duplicates (identical values)
- 97,370 unique bars span 2026-05-01 to 2026-07-08 (67.6 days)
- **Verify**: `python -c "from datetime import datetime; d={}; [d.update({datetime.strptime(p[0],'%Y-%m-%d %H:%M'):1}) for p in [l.split('\t') for l in open('data/TRXUSDT1.csv')] if len(p)>=6]; print(f'{len(d)} unique, {200000-len(d)} duplicates')"`

### 1.3 Sorting
- Original CSV had 0.68% backwards timestamps (1,368 negative gaps)
- After dedup + sort: 100% of gaps are exactly 60 seconds
- **Verify**: `python -c "from datetime import datetime; d={}; [d.update({datetime.strptime(p[0],'%Y-%m-%d %H:%M'):p}) for p in [l.split('\t') for l in open('data/TRXUSDT1.csv')] if len(p)>=6]; sorted_dts=sorted(d.keys()); gaps={}; [gaps.update({(sorted_dts[i]-sorted_dts[i-1]).total_seconds():1}) for i in range(1,len(sorted_dts))]; print(gaps)"` — should show only `{60.0: 97369}`

## 2. Backtest Engine

### 2.1 Position Sizing
- Risk per trade: 0.25% of starting balance ($25 on $10k)
- Max aggregate risk: 10% ($1,000)
- Position value = risk_dollars / stop_pct where stop = max(2×ATR, close×0.005)
- **Verify**: Check `test_2week.py` line ~79-86 for the position sizing formula

### 2.2 Capital Tracking
- Equity = starting_balance + closed_PnL + unrealized_PnL
- Unrealized PnL = sum of position_value × (current_close / entry_price - 1)
- No cash deduction for open positions; only risk margin is tracked
- **Verify**: `test_2week.py` line ~88-90

### 2.3 Combined Strategy (lb=1,2,3)
- Three Heikin-Ashi Momentum strategies run simultaneously on the same symbol
- Each with different lookback (1, 2, 3 periods)
- Each can have at most 1 open position at a time
- Total positions ≤ 3 (one per lookback)
- **Verify**: `test_sections.py` line ~46-83 for the combined logic

## 3. Section Validation

### 3.1 Methodology
- 97,370 unique bars divided into 10 sections of ~10,000 bars each
- Each section tested independently (no look-ahead)
- Strategy starts fresh on each section (no carry-over)
- **Verify**: `test_sections.py` line ~33-39

### 3.2 Results to Reproduce
Run `python test_sections.py` and verify:

| Section | Expected Return | Expected DD | Expected TPD | 20% target |
|---------|----------------|-------------|-------------|------------|
| 1 | +18.0% | 6.2% | 14.50 | N/A |
| 5 | +58.8% | 9.3% | 23.83 | 1d |
| 7 | +73.0% | 12.0% | 31.67 | 3d |
| 8 | +156.0% | 6.2% | 80.17 | 0d |
| 10 | +24.1% | 1.9% | 16.20 | 4d |

All 10 sections: positive, 8/10 hit 20% within 10 days.

## 4. Equity Curve

### 4.1 Verify
Run `python equity_curve.py` and check:
- Full return: +495.3%
- Max DD: 6.2%
- 20% target achieved in 7 days
- Every week positive (11/11 weeks)
- Max drawdown periods < 6.2% and recover within hours

### 4.2 Equity Calculation
```
For each bar i:
  equity[i] = START_BAL + sum(closed_trade_PnLs) + sum(unrealized_PnLs)
  where unrealized_PnL[position] = pos_value × (close[i] / entry_price - 1)
```

## 5. Database Queries

### 5.1 All Locked Strategies
```sql
SELECT ls.id, ls.template, ls.symbol, ls.timeframe,
       ls.win_rate, ls.avg_rr, ls.profit_factor,
       ls.max_drawdown_pct, ls.trades_per_year
FROM locked_strategies ls
ORDER BY ls.trades_per_year DESC;
```

### 5.2 Grade Distribution
```sql
SELECT
  CASE
    WHEN win_rate >= 50 AND avg_rr >= 2.0 AND profit_factor >= 1.5
         AND max_drawdown_pct <= 25 AND trades_per_year >= 5 THEN 'A-Exceeding'
    WHEN win_rate >= 40 AND avg_rr >= 1.5 AND profit_factor >= 1.2
         AND max_drawdown_pct <= 30 AND trades_per_year >= 1 THEN 'B-Meeting'
    WHEN win_rate >= 35 AND avg_rr >= 1.2 AND profit_factor >= 1.0
         AND max_drawdown_pct <= 35 AND trades_per_year >= 0.1 THEN 'C-Below'
    ELSE 'D-Fail'
  END as grade,
  COUNT(*) as count
FROM locked_strategies
GROUP BY grade
ORDER BY grade;
```
Expected: 11 A, 139 B, 12 C, 1 D.

## 6. Portfolio Combination

### 6.1 Methodology
- TRX combined HA is backtested on dedup CSV directly
- Other strategies use DB metrics (return%, DD, TPY)
- Combined DD uses variance-covariance formula with ρ=0.2:
  ```
  σ²_portfolio = Σ w_i²·σ_i² + 2·Σ Σ w_i·w_j·σ_i·σ_j·ρ
  ```
- **Verify**: `combined_portfolio.py` lines ~190-210

### 6.2 Results
Run `python combined_portfolio.py` and verify:
- TRX 80% + others: +424.6%, 5.7% DD, 19.89 TPD
- Equal weight all 60: +147.9%, 6.6% DD, 0.45 TPD

### 6.3 Correlation Assumption
ρ=0.2 is assumed between all strategy pairs. This is conservative (strategies trade different symbols at different timeframes, so actual correlation is likely lower, meaning real DD would be lower than estimated).

## 7. Known Limitations

### 7.1 Data Period
Only 67 days of TRXUSDT data (2026-05-01 to 2026-07-08). Longer historical data would improve validation confidence.

### 7.2 Crypto Bull Market
TRX was essentially flat (+0.64%) over the period, but sections 4-7 were bearish (-3% to -6% price moves). Strategy profited in all conditions.

### 7.3 lookback=1 Not in DB
The lb=1 variant failed csv_scanner Phase 1 (PF<1.1 in first 20k section) but performs best in practice (16.6 TPD). Only found through direct testing.

### 7.4 CSV Data Source Only
Backtest engine supports MT5 and CCXT, but the 14 strategy templates were tested only on CSV data. MT5/CCXT results from `backtest_results` table exist but were not the primary focus.

## 8. To Fully Reproduce

```bash
# 1. Clean run of TRX combined HA
python test_sections.py

# 2. Equity curve
python equity_curve.py

# 3. Combined portfolio
python combined_portfolio.py

# 4. Database queries (see section 5)
python -c "
import sqlite3
conn = sqlite3.connect('strategy_bot.db')
cur = conn.cursor()
# Grade distribution
cur.execute('SELECT ...')  # use query from 5.2
for r in cur.fetchall(): print(r)
conn.close()
"
```
