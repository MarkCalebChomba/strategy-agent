"""
Equity curve for ALL 10 sections of TRXUSDT 1m HA combined (lb=1,2,3).
Shows each section's performance side by side.

METHODOLOGY
===========
- Reads TRXUSDT1_dedup.csv from data/
- Splits into 10 non-overlapping sections of 10,000 bars each
- Each section is backtested independently with HA Momentum (lb=1, 2, 3) running simultaneously
- Per-section detail includes: trade log, equity curve chart, weekly breakdown, streaks
- Combined run on all bars at the end for overall metrics
- Summary table compares all 10 sections side-by-side

EQUITY CALCULATION
==================
- equity[i] = START_BAL + sum(closed_trade_PnLs up to bar i) + sum(unrealized_PnLs at bar i)
- START_BAL = $10,000 appended at index 0 before first bar
- equity_curve has length n+1 (initial balance + one entry per bar)
- Closed PnL: sum of all completed trade profits/losses
- Unrealized PnL: open positions marked to current close price = pos_val * (close/entry_price - 1)

DRAWDOWN MEASUREMENT
====================
- Peak-to-trough method: max_dd = max((peak - current) / peak * 100) over the entire curve
- Peak resets only when equity reaches a new all-time high
- Drawdown is tracked as a percentage of the current peak
- Worst drawdown periods are identified and sorted by depth (top 5 shown if > 2%)

KEY ASSUMPTIONS
===============
- Zero slippage, commission, spread (idealized execution)
- Risk-based sizing: $25 fixed risk per trade, $1,000 max aggregate
- Stop distance = max(2*ATR_14, close*0.5%), converted to fraction of price
- ATR computed as simple average of high-low ranges over 14 bars
- Sections are independent (no carryover)
- All signals execute at the close price of the signal bar

WEEKLY BREAKDOWN
================
- Groups trades by ISO week number (e.g., "2026-W27")
- Shows total PnL and return percentage per week
- Useful for identifying time-dependent performance patterns

TEXT-BASED EQUITY CHART
=======================
- Vertically: equity value levels (min to max, split into 10 rows)
- Horizontally: time steps (sampled to ~55 characters width)
- Filled block character (0x2588) indicates equity in that value range at that time
- Allows visual inspection of equity growth and drawdown periods without a GUI
"""

import os, sys
from datetime import datetime, timedelta
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import RUNNERS

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
START_BAL = 10000.0
RISK_PCT = 0.0025
MAX_AGG_RISK = 0.10
SECTION_SIZE = 10000

def parse_csv(filepath):
    data, dts = [], []
    with open(filepath) as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) < 6: continue
            dt = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
            data.append({"open": float(parts[1]), "high": float(parts[2]), "low": float(parts[3]), "close": float(parts[4])})
            dts.append(dt)
    return data, dts

def run_section(data, dts):
    """
    Run HA Momentum (lb=1,2,3) on a single section.

    Returns (equity_curve, trades_log, dts).
    trades_log contains dicts with keys: bar, time, pnl, lb (which lookback variant).

    EQUITY CURVE: list of length n+1 (index 0 = START_BAL, index i = equity after bar i-1)
    equity[i] = START_BAL + closed_PnL_up_to_bar_i + unrealized_PnL_at_bar_i
    """
    n = len(data)
    risk_pt = START_BAL * RISK_PCT
    max_risk = START_BAL * MAX_AGG_RISK

    strategies = []
    for lb in [1, 2, 3]:
        buy, sell = RUNNERS["Heikin-Ashi Momentum"](data, {"lookback": lb})
        strategies.append({"lb": lb, "buy": buy, "sell": sell})

    positions, trades_log = [], []
    total_risk = 0.0
    equity_curve = [START_BAL]

    for i in range(n):
        close = data[i]["close"]
        remaining = []
        for p in positions:
            si = p["strat_idx"]
            if i < len(strategies[si]["sell"]) and strategies[si]["sell"][i]:
                # Closed PnL = position_value * percentage_move
                pnl = p["pos_val"] * (close - p["entry_price"]) / p["entry_price"] if p["entry_price"] > 0 else 0
                trades_log.append({"bar": i, "time": dts[i], "pnl": pnl, "lb": p["lb"]})
                total_risk -= risk_pt
            else:
                remaining.append(p)
        positions = remaining

        for si, s in enumerate(strategies):
            if i < len(s["buy"]) and s["buy"][i]:
                if any(p["strat_idx"] == si for p in positions): continue
                if total_risk + risk_pt > max_risk: continue
                atr = sum(data[max(0,i-14):i][j]["high"] - data[max(0,i-14):i][j]["low"] for j in range(min(14,i))) / max(1, min(14,i))
                sd = max(2*atr, close*0.005)
                sp = sd/close if close>0 else 0.02
                pv = risk_pt / sp
                total_risk += risk_pt
                positions.append({"strat_idx": si, "lb": s["lb"], "entry_price": close, "pos_val": pv})

        # Equity: closed trades sum + unrealized mark-to-market of open positions
        closed_total = sum(t["pnl"] for t in trades_log)
        unrealized = sum(p["pos_val"]*(close-p["entry_price"])/p["entry_price"] for p in positions if p["entry_price"] > 0)
        equity_curve.append(START_BAL + closed_total + unrealized)

    return equity_curve, trades_log, dts

def compute_stats(equity_curve, trades_log, dts):
    """
    Compute summary statistics from equity curve and trade log.

    Metrics calculated:
    - Win rate (WR%): percentage of trades with positive PnL
    - Risk/reward ratio (RR): avg_win / abs(avg_loss)
    - Profit factor (PF): gross_profit / abs(gross_loss)
    - Total return: percentage change from START_BAL to final equity
    - Max drawdown: peak-to-trough percentage decline
    - Trades per day (TPD): total_trades / total_calendar_days
    - Days to 20% target: first time equity >= START_BAL * 1.20
    """
    winners = [t for t in trades_log if t["pnl"] > 0]
    losers = [t for t in trades_log if t["pnl"] <= 0]
    total = len(trades_log)
    wr = len(winners)/total*100 if total else 0
    total_pnl = sum(t["pnl"] for t in trades_log)
    total_ret = (equity_curve[-1]/START_BAL - 1)*100
    avg_win = sum(t["pnl"] for t in winners)/len(winners) if winners else 1
    avg_loss = abs(sum(t["pnl"] for t in losers)/len(losers)) if losers else 1
    rr = avg_win/avg_loss if avg_loss else 0
    gross_win = sum(t["pnl"] for t in winners) if winners else 0
    gross_loss = abs(sum(t["pnl"] for t in losers)) if losers else 1
    pf = gross_win/gross_loss if gross_loss else 0
    total_days = (dts[-1]-dts[0]).days or 1
    tpd = total/max(total_days, 1)

    # Drawdown: peak-to-trough as percentage of current peak
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak: peak = v
        dd = (peak-v)/peak*100
        if dd > max_dd: max_dd = dd

    # First time equity reaches 20% profit (START_BAL * 1.20)
    target = START_BAL * 1.20
    days_to_20 = None
    for i, v in enumerate(equity_curve):
        if v >= target and i > 0:
            days_to_20 = (dts[min(i-1, len(dts)-1)] - dts[0]).days
            break

    return {
        "trades": total, "wr": wr, "ret": total_ret, "dd": max_dd,
        "rr": rr, "pf": pf, "tpd": tpd, "to20": days_to_20,
        "total_pnl": total_pnl, "winners": len(winners), "losers": len(losers),
    }

def print_equity_chart(equity_curve, dts, label, height=10, width=50):
    """
    Print a text-based equity curve chart.

    Renders equity values vertically (height rows from min to max equity)
    and horizontally (width columns sampling the equity curve at regular intervals).
    Uses Unicode full block character (0x2588) to mark which value range
    the equity falls in at each sampled time step.

    NOTES
    =====
    - height=10 rows divide the equity range into 10 bands
    - width=50 columns sample the curve at equally spaced indices
    - If equity range is very small, uses START_BAL*0.01 as fallback range
    """
    min_eq = min(equity_curve)
    max_eq = max(equity_curve)
    rng = max_eq - min_eq if max_eq > min_eq else START_BAL * 0.01
    start_date = dts[0].date()
    end_date = dts[-1].date()

    print(f"  {label}")
    print(f"  {start_date} to {end_date}")
    for row in range(height, 0, -1):
        val = min_eq + (rng * row / height)
        val2 = min_eq + (rng * (row-1) / height)
        line = f"  {val:>7.0f} |"
        step = max(1, len(equity_curve)//width)
        for i in range(0, len(equity_curve), step):
            eq = equity_curve[i]
            if val2 <= eq <= val:
                line += chr(0x2588)
            elif eq < val2:
                pass
        print(line)
    print(f"  {'':>8}{chr(0x2500)*width}")
    print(f"  {'':>8} {start_date} {'':>{width-22}} {end_date}")
    print()

def print_section_detail(equity_curve, trades_log, dts, stats, sec_num):
    """
    Print detailed per-section report including:
    - Summary metrics (trades, WR, RR, PF, return, DD, TPD, 20% target)
    - Text equity curve chart
    - Weekly PnL breakdown
    - Max win/loss streaks
    """
    print(f"  {'='*65}")
    print(f"  SECTION {sec_num} — {dts[0].date()} to {dts[-1].date()}")
    print(f"  {'='*65}")
    print(f"  Trades: {stats['trades']:>5d}  Winners: {stats['winners']:>4d}  "
          f"Losers: {stats['losers']:>4d}")
    print(f"  WR: {stats['wr']:>5.1f}%  RR: {stats['rr']:>5.2f}  PF: {stats['pf']:>5.2f}")
    print(f"  Return: {stats['ret']:>+8.1f}%  DD: {stats['dd']:>5.1f}%  "
          f"TPD: {stats['tpd']:>5.2f}")
    to20 = f"{stats['to20']}d" if stats['to20'] is not None else "N/A"
    print(f"  20% target: {to20:>4}")
    print()

    # Compact equity
    print_equity_chart(equity_curve, dts, f"  Equity Curve:", height=8, width=55)

    # Weekly breakdown: group trades by ISO week, compute total PnL and return
    weekly = defaultdict(list)
    for t in trades_log:
        w = t["time"].isocalendar()[1]
        y = t["time"].year
        wk = f"{y}-W{w:02d}"
        weekly[wk].append(t["pnl"])

    print(f"  Weekly:")
    print(f"  {'Week':>8} {'Trades':>7} {'PnL':>10} {'Ret%':>7}")
    weeks_sorted = sorted(weekly.keys())
    for wk in weeks_sorted:
        pnls = weekly[wk]
        wk_pnl = sum(pnls)
        wk_total = len(pnls)
        wk_ret = wk_pnl/START_BAL*100
        print(f"  {wk:>8} {wk_total:>7d} {wk_pnl:>+10.2f} {wk_ret:>+6.2f}%")

    # Consecutive win/loss streaks
    max_ws = max_ls = 0
    cur_w = cur_l = 0
    for t in trades_log:
        if t["pnl"] > 0:
            cur_w += 1; cur_l = 0
            if cur_w > max_ws: max_ws = cur_w
        else:
            cur_l += 1; cur_w = 0
            if cur_l > max_ls: max_ls = cur_l
    print(f"  Max win streak: {max_ws}  Max loss streak: {max_ls}")
    print()

def print_summary_table(all_sections):
    """
    Print a comparison table of all sections side by side.
    Shows: period, trades, win rate, return, drawdown, RR, PF, TPD, days to 20%.
    Includes average row at bottom.
    """
    print(f"\n  {'='*85}")
    print(f"  SECTION COMPARISON")
    print(f"  {'='*85}")
    print(f"  {'#':3s} {'Period':24s} {'Trades':>7s} {'WR%':>5s} {'Ret%':>8s} "
          f"{'DD%':>6s} {'RR':>5s} {'PF':>5s} {'TPD':>6s} {'To20%':>6s}")
    print(f"  {'-'*85}")
    for i, (eq, tlog, dts, stats) in enumerate(all_sections):
        to20 = f"{stats['to20']}d" if stats['to20'] is not None else "N/A"
        label = f"{dts[0].strftime('%b %d')}-{dts[-1].strftime('%b %d')}"
        print(f"  {i+1:3d} {label:24s} {stats['trades']:>7d} {stats['wr']:>5.1f} "
              f"{stats['ret']:>+8.1f} {stats['dd']:>6.1f} {stats['rr']:>5.2f} "
              f"{stats['pf']:>5.2f} {stats['tpd']:>6.2f} {to20:>6s}")
    print(f"  {'-'*85}")
    avg_ret = sum(s[3]["ret"] for s in all_sections)/len(all_sections)
    avg_dd = sum(s[3]["dd"] for s in all_sections)/len(all_sections)
    avg_wr = sum(s[3]["wr"] for s in all_sections)/len(all_sections)
    avg_tpd = sum(s[3]["tpd"] for s in all_sections)/len(all_sections)
    total_trades = sum(s[3]["trades"] for s in all_sections)
    print(f"  {'AVERAGE':30s} {total_trades:>7d} {avg_wr:>5.1f} {avg_ret:>+8.1f} "
          f"{avg_dd:>6.1f} {'':>5s} {'':>5s} {avg_tpd:>6.2f}")
    print()

def main():
    data, dts = parse_csv(os.path.join(DATA_DIR, "TRXUSDT1_dedup.csv"))
    print(f"Total data: {len(data)} bars, {dts[0].date()} to {dts[-1].date()}")
    print()

    all_sections = []
    for sec_num in range(10):
        start = sec_num * SECTION_SIZE
        end = min(start + SECTION_SIZE, len(data))
        if end - start < 1000: continue
        sec_data = data[start:end]
        sec_dts = dts[start:end]
        eq, tlog, sdts = run_section(sec_data, sec_dts)
        stats = compute_stats(eq, tlog, sdts)
        all_sections.append((eq, tlog, sdts, stats))
        print_section_detail(eq, tlog, sdts, stats, sec_num + 1)

    print_summary_table(all_sections)

    # Combined: run on ALL data
    print(f"  {'='*85}")
    print(f"  COMBINED — ALL BARS (full dataset)")
    print(f"  {'='*85}")
    eq, tlog, sdts = run_section(data, dts)
    stats = compute_stats(eq, tlog, sdts)
    print(f"  Trades: {stats['trades']}  WR: {stats['wr']:.1f}%  "
          f"Ret: {stats['ret']:+.1f}%  DD: {stats['dd']:.1f}%  "
          f"RR: {stats['rr']:.2f}  PF: {stats['pf']:.2f}  TPD: {stats['tpd']:.2f}")
    to20 = f"{stats['to20']}d" if stats['to20'] is not None else "N/A"
    print(f"  20% target: {to20}")
    print()

    print_equity_chart(eq, sdts, "  Full Equity Curve:", height=12, width=65)

    # Worst drawdowns for combined
    # Iterate through equity curve identifying drawdown periods
    # A drawdown period starts when equity drops below peak and ends when it recovers to new high
    peak = eq[0]
    in_dd = False
    dd_start = 0
    dd_list = []
    max_dd_val = 0
    for i, v in enumerate(eq):
        if v > peak:
            if in_dd:
                dd_list.append((dd_start, i-1, max_dd_val))
                in_dd = False
                max_dd_val = 0
            peak = v
        else:
            dd = (peak - v)/peak*100
            if not in_dd:
                in_dd = True
                dd_start = i-1
            if dd > max_dd_val:
                max_dd_val = dd
    if in_dd:
        dd_list.append((dd_start, len(eq)-1, max_dd_val))

    # Sort drawdowns by depth descending and show top 5 that exceed 2%
    dd_list.sort(key=lambda x: x[2], reverse=True)
    print(f"  Worst drawdowns:")
    print(f"  {'Start':14s} {'End':14s} {'Depth':>6s} {'Duration':>9s}")
    for ds, de, dval in dd_list[:5]:
        if dval > 2.0:
            dur = de - ds
            start_d = dts[min(ds, len(dts)-1)]
            end_d = dts[min(de, len(dts)-1)]
            print(f"  {start_d.strftime('%b %d %H:%M'):14s} {end_d.strftime('%b %d %H:%M'):14s} {dval:>5.1f}% {dur:>8d} bars")

    # Weekly for combined
    print(f"\n  Weekly (combined):")
    weekly = defaultdict(list)
    for t in tlog:
        w = t["time"].isocalendar()[1]
        y = t["time"].year
        wk = f"{y}-W{w:02d}"
        weekly[wk].append(t["pnl"])
    print(f"  {'Week':>8} {'Trades':>7} {'PnL':>10} {'Ret%':>7}")
    for wk in sorted(weekly.keys()):
        pnls = weekly[wk]
        wk_pnl = sum(pnls)
        wk_ret = wk_pnl/START_BAL*100
        print(f"  {wk:>8} {len(pnls):>7d} {wk_pnl:>+10.2f} {wk_ret:>+6.2f}%")

    print(f"\n  {'='*85}")
    print(f"  SUMMARY")
    print(f"  {'='*85}")
    print(f"  Sections positive:  {sum(1 for s in all_sections if s[3]['ret'] > 0)}/10")
    print(f"  Sections hit 20%:   {sum(1 for s in all_sections if s[3]['to20'] is not None and s[3]['to20'] <= 10)}/10")
    print(f"  Avg return:         {sum(s[3]['ret'] for s in all_sections)/len(all_sections):+.1f}%")
    print(f"  Avg DD:             {sum(s[3]['dd'] for s in all_sections)/len(all_sections):.1f}%")
    print(f"  Avg WR:             {sum(s[3]['wr'] for s in all_sections)/len(all_sections):.1f}%")
    print(f"  Avg TPD:            {sum(s[3]['tpd'] for s in all_sections)/len(all_sections):.2f}")
    print(f"  Full return:        {stats['ret']:+.1f}%")
    print(f"  Full DD:            {stats['dd']:.1f}%")
    print(f"  Days to 20% target: {to20}")

    # Best/worst sections
    best = max(all_sections, key=lambda s: s[3]["ret"])
    worst = min(all_sections, key=lambda s: s[3]["ret"])
    print(f"\n  Best section:  Section {all_sections.index(best)+1} ({best[2][0].date()} to {best[2][-1].date()}) "
          f"{best[3]['ret']:+.1f}%")
    print(f"  Worst section: Section {all_sections.index(worst)+1} ({worst[2][0].date()} to {worst[2][-1].date()}) "
          f"{worst[3]['ret']:+.1f}%")

if __name__ == "__main__":
    main()
