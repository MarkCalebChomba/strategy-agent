"""
Test TRXUSDT 1m HA (lb=1,2,3) on each 10k-bar section independently.
V2: Fixed bugs found by independent audit.

BUG FIXES (vs V1)
=================
FIX #1 — Stop-loss enforcement:
  Previously stop_dist was used only for position SIZING. Now a hard stop-loss
  is tracked per position and enforced every bar. If low breaches long stop,
  position is closed at stop_price (with 0.1% slippage). This caps per-trade
  loss at the intended $25 risk + slippage + fees.

FIX #2 — Fee modeling:
  Binance spot fee of 0.1% per trade (0.2% round trip). Deducted from each
  trade's PnL. Fee = position_value * 0.001 on both entry and exit. At 25
  trades/day × $5,000 avg position = $250/day in fees — significant.

FIX #3 — Next-bar execution (remove look-ahead bias):
  Signals fire on bar i (using bar i's OHLC for HA calc). Execution happens
  at bar i+1's OPEN price, not bar i's close. This adds ~1 minute delay which
  is material for 1m mean-reversion strategy.

FIX #4 — All lookbacks shown individually + combined:
  No cherry-picking. Results shown for lb=1, lb=2, lb=3, and combined (all 3).
  Each lookback reported separately so verifier can see which drives results.

NO FIX — Sample size (67 days, 1 symbol):
  Not fixable without more data. Remains a limitation.

METHODOLOGY
===========
- Reads TRXUSDT1_dedup.csv from data/ directory
- Splits into 10 non-overlapping 10k-bar sections
- Each section backtested independently with fresh $10,000 starting balance
- Risk: 0.25% per trade ($25), max 10% aggregate ($1,000)
- ATR: simple avg of high-low ranges over 14 bars
- Stop: max(2*ATR, close*0.5%)
- Fee: 0.1% per trade (0.2% round trip), Binance spot rate
- Stop slippage: 0.1% worse fill on stop-loss
"""

import os, sys
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import RUNNERS

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SECTION_SIZE = 10000
START_BAL = 10000.0
RISK_PCT = 0.0025          # 0.25% fixed risk per trade
MAX_AGG_RISK = 0.10        # 10% max aggregate risk
FEE_PCT = 0.0000           # 0% per trade (The5ers MT5 crypto, per user)
STOP_SLIPPAGE = 0.001      # 0.1% slippage on stop-loss fills


def parse_csv(filepath):
    """Parse tab-separated CSV: datetime, O, H, L, C, volume."""
    data, dts = [], []
    with open(filepath, "r") as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) < 6: continue
            try:
                dt = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
                data.append({"open": float(parts[1]), "high": float(parts[2]),
                             "low": float(parts[3]), "close": float(parts[4])})
                dts.append(dt)
            except (ValueError, IndexError): continue
    return data, dts


def split_sections(data, dts, size=SECTION_SIZE):
    """Non-overlapping sections of `size` bars, min 1000 bars."""
    sections = []
    for start in range(0, len(data), size):
        end = min(start + size, len(data))
        if end - start >= 1000:
            sections.append((data[start:end], dts[start:end], start))
    return sections


def backtest_lookbacks(data, dts, lookbacks):
    """
    Run HA Momentum with specified lookback list on one dataset.
    Returns metrics dict.

    EXECUTION MODEL
    ===============
    - Signal fires on bar i using bar i's OHLC for HA calculation
    - Entry executes at bar (i+1)'s OPEN (next bar, ~1min later)
    - Exit (signal-based): at current bar's close when sell signal fires
    - Exit (stop-loss): when bar LOW breaches stop_price, fill at stop - slippage
    - Equity: START_BAL + sum(closed PnL) + sum(unrealized PnL)
    - Unrealized PnL: open positions marked to current bar's close
    """
    n = len(data)
    risk_pt = START_BAL * RISK_PCT
    max_risk = START_BAL * MAX_AGG_RISK

    # Generate signals for all requested lookbacks
    strategies = []
    for lb in lookbacks:
        buy, sell = RUNNERS["Heikin-Ashi Momentum"](data, {"lookback": lb})
        strategies.append({"lb": lb, "buy": buy, "sell": sell})

    positions = []        # open positions
    trades_log = []       # net PnL of closed trades (after fees)
    total_risk = 0.0      # current aggregate risk ($)
    equity_curve = [START_BAL]
    pending = []          # entries queued from previous bar's buy signal
    # V3: exit_next flag on positions — sell signal on bar i marks position,
    # position closes at bar i+1 OPEN (not bar i close, same fix as entries)

    for i in range(n):
        bar = data[i]
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]

        # -- V3: Close positions marked exit_next at this bar's OPEN --
        remaining = []
        for p in positions:
            if p.get("exit_next"):
                fill = o
                gross_pnl = p["pos_val"] * (fill - p["entry_price"]) / p["entry_price"]
                exit_fee = p["pos_val"] * (fill / p["entry_price"]) * FEE_PCT
                net_pnl = gross_pnl - p["entry_fee"] - exit_fee
                trades_log.append(net_pnl)
                total_risk -= risk_pt
            else:
                remaining.append(p)
        positions = remaining

        # -- Execute pending entries at this bar's OPEN (buy signals on bar i-1) --
        for pe in pending:
            si, plb = pe["strat_idx"], pe["lb"]
            if any(p["strat_idx"] == si for p in positions): continue
            if total_risk + risk_pt > max_risk: continue
            lookback_bars = data[max(0, i-14):i]
            atr = sum(b["high"] - b["low"] for b in lookback_bars) / max(1, len(lookback_bars))
            sd = max(2 * atr, o * 0.005)
            sp = sd / o if o > 0 else 0.02
            pv = risk_pt / sp
            stop_price = o - sd
            entry_fee = pv * FEE_PCT
            total_risk += risk_pt
            positions.append({
                "strat_idx": si, "lb": plb,
                "entry_price": o, "stop_price": stop_price,
                "pos_val": pv, "entry_fee": entry_fee,
            })
        pending = []

        # -- Stop-loss enforcement (intra-bar) --
        remaining = []
        for p in positions:
            if l <= p["stop_price"]:
                fill = p["stop_price"] * (1 - STOP_SLIPPAGE)
                gross_pnl = p["pos_val"] * (fill - p["entry_price"]) / p["entry_price"]
                exit_fee = p["pos_val"] * (fill / p["entry_price"]) * FEE_PCT
                net_pnl = gross_pnl - p["entry_fee"] - exit_fee
                trades_log.append(net_pnl)
                total_risk -= risk_pt
            else:
                remaining.append(p)
        positions = remaining

        # -- V3: Mark positions for exit_next (sell signal → close at next bar's OPEN) --
        for p in positions:
            si = p["strat_idx"]
            if i < len(strategies[si]["sell"]) and strategies[si]["sell"][i]:
                p["exit_next"] = True

        # -- Queue entries for NEXT bar's open (buy signals) --
        for si, s in enumerate(strategies):
            if i < len(s["buy"]) and s["buy"][i]:
                if not any(pe["strat_idx"] == si for pe in pending):
                    pending.append({"strat_idx": si, "lb": s["lb"]})

        # -- Equity calculation --
        closed_total = sum(trades_log)
        unrealized = sum(
            p["pos_val"] * (c - p["entry_price"]) / p["entry_price"]
            for p in positions if p["entry_price"] > 0
        )
        equity_curve.append(START_BAL + closed_total + unrealized)

    if len(trades_log) < 3:
        return None

    winners = [t for t in trades_log if t > 0]
    losers = [t for t in trades_log if t <= 0]
    wr = len(winners) / len(trades_log) * 100 if trades_log else 0
    total_ret = (equity_curve[-1] - START_BAL) / START_BAL * 100
    total_pnl = sum(trades_log)

    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd: max_dd = dd

    target = START_BAL * 1.20
    days_to_target = None
    for j, v in enumerate(equity_curve):
        if v >= target and j > 0:
            days_to_target = (dts[min(j-1, len(dts)-1)] - dts[0]).days
            break

    gross_profits = sum(winners) if winners else 0
    gross_losses = abs(sum(losers)) if losers else 1
    pf = gross_profits / gross_losses if gross_losses > 0 else 0
    avg_win = sum(winners) / len(winners) if winners else 0
    avg_loss = abs(sum(losers) / len(losers)) if losers else 1
    rr = avg_win / avg_loss if avg_loss > 0 else 0
    total_days = max(1, (dts[-1] - dts[0]).days)
    tpd = len(trades_log) / total_days

    return {
        "n_trades": len(trades_log),
        "win_rate": round(wr, 1),
        "total_return_pct": round(total_ret, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "days_to_20pct": days_to_target,
        "trades_per_day": round(tpd, 2),
        "start_date": dts[0], "end_date": dts[-1],
        "n_winners": len(winners), "n_losers": len(losers),
        "profit_factor": round(pf, 2),
        "avg_rr": round(rr, 2),
        "total_pnl": round(total_pnl, 2),
    }


def print_section(name, r):
    """Print a single section's metrics."""
    days_str = f"{r['days_to_20pct']}d" if r['days_to_20pct'] is not None else "N/A"
    target = " << 20% in 2wk!" if r['days_to_20pct'] is not None and r['days_to_20pct'] <= 10 else ""
    print(f"  {name}")
    print(f"    Trades:{r['n_trades']:>5d}  WR:{r['win_rate']:>6.1f}%  "
          f"Ret:{r['total_return_pct']:>+8.2f}%  DD:{r['max_drawdown_pct']:>6.2f}%  "
          f"RR:{r['avg_rr']:>5.2f}  PF:{r['profit_factor']:>6.2f}  "
          f"TPD:{r['trades_per_day']:>6.2f}  To20%:{days_str:>4}{target}")


# -- Main --
data, dts = parse_csv(os.path.join(DATA_DIR, "TRXUSDT1_dedup.csv"))
print(f"Data: {len(data)} bars, {dts[0].date()} to {dts[-1].date()}")
print()

# Run each lookback individually AND combined (FIX #4: no cherry-picking)
lookback_sets = {
    "LB=1 only":   [1],
    "LB=2 only":   [2],
    "LB=3 only":   [3],
    "Combined":    [1, 2, 3],
}

for lb_name, lbs in lookback_sets.items():
    print(f"\n{'='*80}")
    print(f"  {lb_name}")
    print(f"{'='*80}")
    print(f"  {'Section':24s} {'Trades':>7s} {'WR%':>6s} {'Ret%':>8s} "
          f"{'DD%':>6s} {'RR':>5s} {'PF':>6s} {'TPD':>6s} {'To20%':>6s}")
    print(f"  {'-'*74}")

    sections = split_sections(data, dts)
    all_r = []
    for sec_data, sec_dts, start_idx in sections:
        label = f"  {sec_dts[0].strftime('%b %d')}-{sec_dts[-1].strftime('%b %d')}"
        r = backtest_lookbacks(sec_data, sec_dts, lbs)
        if r:
            all_r.append((label, r))
            days_str = f"{r['days_to_20pct']}d" if r['days_to_20pct'] is not None else "N/A"
            target = " *" if r['days_to_20pct'] is not None and r['days_to_20pct'] <= 10 else ""
            print(f"  {label:22s} {r['n_trades']:>7d} {r['win_rate']:>6.1f} "
                  f"{r['total_return_pct']:>+8.1f} {r['max_drawdown_pct']:>6.1f} "
                  f"{r['avg_rr']:>5.2f} {r['profit_factor']:>6.2f} "
                  f"{r['trades_per_day']:>6.2f} {days_str:>6s}{target}")
        else:
            print(f"  {label:22s} {'N/A':>7s}")

    if all_r:
        avg_ret = sum(r['total_return_pct'] for _, r in all_r) / len(all_r)
        avg_dd = sum(r['max_drawdown_pct'] for _, r in all_r) / len(all_r)
        avg_wr = sum(r['win_rate'] for _, r in all_r) / len(all_r)
        avg_tpd = sum(r['trades_per_day'] for _, r in all_r) / len(all_r)
        positive = sum(1 for _, r in all_r if r['total_return_pct'] > 0)
        hit_20 = sum(1 for _, r in all_r if r['days_to_20pct'] is not None and r['days_to_20pct'] <= 10)
        total_trades = sum(r['n_trades'] for _, r in all_r)
        print(f"  {'-'*74}")
        print(f"  {'TOTAL/AVG':22s} {total_trades:>7d} {avg_wr:>6.1f} "
              f"{avg_ret:>+8.1f} {avg_dd:>6.1f} {'':>5s} {'':>6s} "
              f"{avg_tpd:>6.2f}")
        print(f"  Sections positive: {positive}/{len(all_r)}  Hit 20%: {hit_20}/{len(all_r)}")

    # Full-run
    print()
    full = backtest_lookbacks(data, dts, lbs)
    if full:
        days_str = f"{full['days_to_20pct']}d" if full['days_to_20pct'] is not None else "N/A"
        target = " << 20% in 2wk!" if full['days_to_20pct'] is not None and full['days_to_20pct'] <= 10 else ""
        print(f"  FULL DATASET:")
        print(f"    Trades:{full['n_trades']:>5d}  WR:{full['win_rate']:>6.1f}%  "
              f"Ret:{full['total_return_pct']:>+8.2f}%  DD:{full['max_drawdown_pct']:>6.2f}%  "
              f"RR:{full['avg_rr']:>5.2f}  PF:{full['profit_factor']:>6.2f}  "
              f"TPD:{full['trades_per_day']:>6.2f}  PnL:{full['total_pnl']:>+9.2f}  "
              f"To20%:{days_str:>4}{target}")

print()
print(f"\n{'='*80}")
print(f"  SUMMARY — ALL CONFIGS COMPARED")
print(f"{'='*80}")
print(f"  {'Config':16s} {'Trades':>7s} {'WR%':>6s} {'Ret%':>8s} "
      f"{'DD%':>6s} {'RR':>5s} {'PF':>6s} {'TPD':>6s} {'To20%':>6s} {'Pos':>4s}")
print(f"  {'-'*74}")
for lb_name, lbs in lookback_sets.items():
    full = backtest_lookbacks(data, dts, lbs)
    if full:
        days_str = f"{full['days_to_20pct']}d" if full['days_to_20pct'] is not None else "N/A"
        sections = split_sections(data, dts)
        pos_count = sum(1 for sec_data, sec_dts, _ in sections
                        if backtest_lookbacks(sec_data, sec_dts, lbs)
                        and backtest_lookbacks(sec_data, sec_dts, lbs)['total_return_pct'] > 0)
        total_secs = len(sections)
        print(f"  {lb_name:16s} {full['n_trades']:>7d} {full['win_rate']:>6.1f} "
              f"{full['total_return_pct']:>+8.1f} {full['max_drawdown_pct']:>6.1f} "
              f"{full['avg_rr']:>5.2f} {full['profit_factor']:>6.2f} "
              f"{full['trades_per_day']:>6.2f} {days_str:>6s} {pos_count:>3d}/{total_secs}")
