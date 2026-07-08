"""
Test TRXUSDT 1m HA (lb=1,2,3) on each 20k-bar section independently.
Shows return, DD, trades per section to check robustness.

METHODOLOGY
===========
- Reads TRXUSDT1_dedup.csv from data/ directory
- Splits the full dataset into non-overlapping sections of 10,000 bars each
- Each section is backtested independently with a fresh $10,000 starting balance
- Three HA Momentum variants (lookback=1, 2, 3) run simultaneously on each section
- Results show per-section metrics and a section-level summary

KEY ASSUMPTIONS
===============
- Sections are independent: no carryover of positions or capital between sections
- Each section resets to $10,000 starting balance
- Minimum 1,000 bars required per section (otherwise skipped)
- Risk-based position sizing: 0.25% of starting capital per trade ($25)
- Maximum aggregate risk: 10% of starting capital ($1,000) across all open positions
- Position value = risk_amount / stop_distance, where stop_distance = max(2*ATR, close*0.5%)
- ATR computed over 14 bars using high-low range (simplified, not true ATR)

IMPORTANT NOTES FOR VERIFICATION
=================================
- The "20% in 2 weeks" metric uses 10 calendar days as the threshold
- Trade frequency depends on HA signal generation; multiple signals can fire per bar
- ATR is computed intra-bar without proper Wilder smoothing (simple average of ranges)
- Slippage, commission, and spread are NOT modeled (zero-cost assumption)
- The dedup CSV has been pre-processed to remove duplicate bars
"""

import os, sys
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import RUNNERS

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
# Section size: 10,000 bars per independent backtest section
SECTION_SIZE = 10000
# Starting capital for each section ($10,000)
START_BAL = 10000.0
# Fixed risk per trade: 0.25% of starting capital ($25)
RISK_PCT = 0.0025
# Maximum aggregate risk across all open positions: 10% of starting capital ($1,000)
MAX_AGG_RISK = 0.10

def parse_csv(filepath):
    """
    Parse a tab-separated CSV file with columns: datetime, open, high, low, close, volume.
    Returns (data, dts) where data is list of OHLC dicts and dts is list of datetimes.
    """
    data, dts = [], []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            parts = line.split("\t")
            if len(parts) < 6: continue
            try:
                dt = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
                data.append({"open": float(parts[1]), "high": float(parts[2]), "low": float(parts[3]), "close": float(parts[4])})
                dts.append(dt)
            except (ValueError, IndexError): continue
    return data, dts

def split_sections(data, dts, size=SECTION_SIZE):
    """
    Split data into non-overlapping sections of `size` bars.
    Only keeps sections with at least 1,000 bars.
    Returns list of (section_data, section_dts, start_index) tuples.
    """
    sections = []
    for start in range(0, len(data), size):
        end = min(start + size, len(data))
        if end - start >= 1000:
            sections.append((data[start:end], dts[start:end], start))
    return sections

def run_on_data(data, dts, label):
    """
    Run all 3 HA variants on one dataset, return metrics.

    BACKTEST LOGIC
    ==============
    - Three Heikin-Ashi Momentum strategies (lb=1, 2, 3) are initialized simultaneously
    - Each bar: process exits first (sell signals), then entries (buy signals)
    - Exit: if sell signal fires, close position and record PnL
    - Entry: if buy signal fires and no existing position for that strategy, enter
    - Position sizing: risk-based. Stop distance = max(2*ATR, close*0.5%).
      Position value = risk_per_trade / stop_percentage_of_price
    - Equity = starting_balance + sum(closed_trade_PnLs) + sum(unrealized_PnLs)
    - Equity curve is tracked per bar (START_BAL appended at index 0, then n bar values)

    RISK MANAGEMENT
    ===============
    - Each trade risks exactly $25 (0.25% of $10,000)
    - Aggregate risk cap: $1,000 (10% of capital) across all concurrent positions
    - Prevents over-concentration when multiple strategies signal simultaneously
    """
    n = len(data)
    risk_pt = START_BAL * RISK_PCT       # $25 fixed risk per trade
    max_risk = START_BAL * MAX_AGG_RISK  # $1,000 max aggregate risk

    # Initialize three HA Momentum variants with lookbacks 1, 2, 3
    strategies = []
    for lb in [1, 2, 3]:
        buy, sell = RUNNERS["Heikin-Ashi Momentum"](data, {"lookback": lb})
        strategies.append({"lb": lb, "buy": buy, "sell": sell})

    positions = []
    trades_log = []
    total_risk = 0.0
    equity_curve = [START_BAL]

    for i in range(n):
        close = data[i]["close"]

        # --- EXITS ---
        # Process sell signals first. If any strategy's sell signal fires, close that position.
        pnl_closed = 0.0
        remaining = []
        for p in positions:
            si = p["strat_idx"]
            sell = strategies[si]["sell"]
            if i < len(sell) and sell[i]:
                # Closed PnL = position_value * (close / entry_price - 1)
                pnl = p["pos_val"] * (close - p["entry_price"]) / p["entry_price"] if p["entry_price"] > 0 else 0
                pnl_closed += pnl
                trades_log.append(pnl)
                total_risk -= risk_pt
            else:
                remaining.append(p)
        positions = remaining

        # --- ENTRIES ---
        # Process buy signals. Skip if strategy already has an open position or aggregate risk exceeded.
        for si, s in enumerate(strategies):
            if i < len(s["buy"]) and s["buy"][i]:
                if any(p["strat_idx"] == si for p in positions): continue
                if total_risk + risk_pt > max_risk: continue
                # Compute ATR as average high-low range over last 14 bars (simplified)
                atr = sum(data[max(0,i-14):i][j]["high"] - data[max(0,i-14):i][j]["low"] for j in range(min(14,i))) / max(1, min(14,i))
                # Stop distance: max of 2*ATR or 0.5% of close
                sd = max(2*atr, close*0.005)
                sp = sd/close if close>0 else 0.02          # stop as fraction of price
                pv = risk_pt / sp                            # position value = $25 / stop_fraction
                total_risk += risk_pt
                positions.append({"strat_idx": si, "lb": s["lb"], "entry_price": close, "pos_val": pv})

        # --- EQUITY ---
        # equity = starting_balance + closed_PnL + unrealized_PnL
        # Unrealized PnL marks open positions to current close price
        closed_total = sum(trades_log)
        unrealized = sum(p["pos_val"]*(close-p["entry_price"])/p["entry_price"] for p in positions if p["entry_price"] > 0)
        equity_curve.append(START_BAL + closed_total + unrealized)

    # Require minimum 3 trades for meaningful statistics
    if len(trades_log) < 3:
        return None

    # Win rate: percentage of trades with positive PnL
    winners = [t for t in trades_log if t > 0]
    losers = [t for t in trades_log if t <= 0]
    wr = len(winners)/len(trades_log)*100
    # Total return percentage relative to starting balance
    total_ret = (equity_curve[-1] - START_BAL)/START_BAL*100

    # Drawdown: peak-to-trough decline in equity curve
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak: peak = v
        dd = (peak - v)/peak*100
        if dd > max_dd: max_dd = dd

    # Days to reach 20% profit target (starting_bal * 1.20)
    target = START_BAL * 1.20
    days_to_target = None
    for i, v in enumerate(equity_curve):
        if v >= target and i > 0:
            bar = min(i-1, len(dts)-1)
            days_to_target = (dts[bar]-dts[0]).days
            break

    return {
        "n_trades": len(trades_log),
        "win_rate": round(wr, 1),
        "total_return_pct": round(total_ret, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "days_to_20pct": days_to_target,
        "trades_per_day": round(len(trades_log)/max(1,(dts[-1]-dts[0]).days), 2),
        "start_date": dts[0], "end_date": dts[-1],
        "n_winners": len(winners), "n_losers": len(losers),
    }


# Main
data, dts = parse_csv(os.path.join(DATA_DIR, "TRXUSDT1_dedup.csv"))
print(f"Total data: {len(data)} bars, {dts[0].date()} to {dts[-1].date()}")
print()

sections = split_sections(data, dts)
print(f"Sections ({SECTION_SIZE} bars each): {len(sections)}\n")

all_results = []
for sec_data, sec_dts, start_idx in sections:
    label = f"bars {start_idx}-{start_idx+len(sec_data)-1}  ({sec_dts[0].date()} to {sec_dts[-1].date()})"
    r = run_on_data(sec_data, sec_dts, label)
    if r:
        all_results.append((label, r))
        days_str = f"{r['days_to_20pct']}d" if r['days_to_20pct'] is not None else "N/A"
        target = " << 20% in 2wk!" if r['days_to_20pct'] is not None and r['days_to_20pct'] <= 10 else ""
        print(f"  {label}")
        print(f"    Trades: {r['n_trades']:>5d}  WR: {r['win_rate']:>5.1f}%  "
              f"Ret: {r['total_return_pct']:>+8.2f}%  DD: {r['max_drawdown_pct']:>5.2f}%  "
              f"To20%: {days_str:>4}{target}")
    else:
        print(f"  {label}  -> NO VALID TRADES (< 3)")

print()
if all_results:
    print("=" * 70)
    print("  SECTION SUMMARY")
    print("=" * 70)
    passes = sum(1 for _, r in all_results if r['days_to_20pct'] is not None and r['days_to_20pct'] <= 10)
    within_2wk = sum(1 for _, r in all_results if r['days_to_20pct'] is not None and r['days_to_20pct'] <= 10)
    positive = sum(1 for _, r in all_results if r['total_return_pct'] > 0)
    print(f"  Sections with 20% in <=10 days: {within_2wk}/{len(all_results)}")
    print(f"  Sections positive:               {positive}/{len(all_results)}")
    avg_ret = sum(r['total_return_pct'] for _, r in all_results) / len(all_results)
    avg_dd = sum(r['max_drawdown_pct'] for _, r in all_results) / len(all_results)
    avg_wr = sum(r['win_rate'] for _, r in all_results) / len(all_results)
    print(f"  Avg return:                      {avg_ret:+.2f}%")
    print(f"  Avg DD:                          {avg_dd:.2f}%")
    print(f"  Avg win rate:                    {avg_wr:.1f}%")
    print()

    # Also show full-data run for comparison
    print("  FULL DATA (all bars):")
    full = run_on_data(data, dts, "full")
    if full:
        days_str = f"{full['days_to_20pct']}d" if full['days_to_20pct'] is not None else "N/A"
        print(f"    Trades: {full['n_trades']:>5d}  WR: {full['win_rate']:>5.1f}%  "
              f"Ret: {full['total_return_pct']:>+8.2f}%  DD: {full['max_drawdown_pct']:>5.2f}%  "
              f"To20%: {days_str:>4}")
