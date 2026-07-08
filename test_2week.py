"""
Clean test: TRXUSDT 1m Heikin-Ashi Momentum with 0.25% fixed risk.
V2: Stop-loss enforcement, 0.1% fees, next-bar execution, all lookbacks shown.
"""
import os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import RUNNERS

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
START_BAL = 10000.0
RISK_PCT = 0.0025
MAX_AGG_RISK = 0.10
FEE_PCT = 0.0001
STOP_SLIPPAGE = 0.001

def parse_csv(filepath):
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

def run_backtest(data, dts, lookbacks):
    n = len(data)
    risk_pt = START_BAL * RISK_PCT
    max_risk = START_BAL * MAX_AGG_RISK

    strategies = []
    for lb in lookbacks:
        buy, sell = RUNNERS["Heikin-Ashi Momentum"](data, {"lookback": lb})
        strategies.append({"lb": lb, "buy": buy, "sell": sell})

    positions = []
    trades_log = []
    total_risk = 0.0
    equity_curve = [START_BAL]
    pending = []

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
                trades_log.append({"pnl": net_pnl, "date": dts[i], "lb": p["lb"], "reason": "signal"})
                total_risk -= risk_pt
            else:
                remaining.append(p)
        positions = remaining

        # Execute pending entries at this bar's OPEN (signals from bar i-1)
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

        # Stop-loss enforcement
        remaining = []
        for p in positions:
            if l <= p["stop_price"]:
                fill = p["stop_price"] * (1 - STOP_SLIPPAGE)
                gross_pnl = p["pos_val"] * (fill - p["entry_price"]) / p["entry_price"]
                exit_fee = p["pos_val"] * (fill / p["entry_price"]) * FEE_PCT
                net_pnl = gross_pnl - p["entry_fee"] - exit_fee
                trades_log.append({"pnl": net_pnl, "date": dts[i], "lb": p["lb"], "reason": "stop"})
                total_risk -= risk_pt
            else:
                remaining.append(p)
        positions = remaining

        # -- V3: Mark positions for exit_next (sell signal → close at next bar's OPEN) --
        for p in positions:
            si = p["strat_idx"]
            if i < len(strategies[si]["sell"]) and strategies[si]["sell"][i]:
                p["exit_next"] = True

        # Queue entries for next bar
        for si, s in enumerate(strategies):
            if i < len(s["buy"]) and s["buy"][i]:
                if not any(pe["strat_idx"] == si for pe in pending):
                    pending.append({"strat_idx": si, "lb": s["lb"]})

        # Equity
        closed_total = sum(t["pnl"] for t in trades_log)
        unrealized = sum(p["pos_val"] * (c - p["entry_price"]) / p["entry_price"] for p in positions if p["entry_price"] > 0)
        equity_curve.append(START_BAL + closed_total + unrealized)

    if len(trades_log) < 3:
        return None

    winners = [t for t in trades_log if t["pnl"] > 0]
    losers = [t for t in trades_log if t["pnl"] <= 0]
    wr = len(winners) / len(trades_log) * 100
    total_ret = (equity_curve[-1] - START_BAL) / START_BAL * 100

    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd: max_dd = dd

    target = START_BAL * 1.20
    days_to_target = None
    reached_date = None
    for j, v in enumerate(equity_curve):
        if v >= target and j > 0:
            bar_idx = min(j - 1, len(dts) - 1)
            days_to_target = (dts[bar_idx] - dts[0]).days
            reached_date = dts[bar_idx]
            break

    total_days = max(1, (dts[-1] - dts[0]).days)
    avg_win = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = abs(sum(t["pnl"] for t in losers) / len(losers)) if losers else 1
    rr = avg_win / avg_loss if avg_loss > 0 else 0
    gross_win = sum(t["pnl"] for t in winners) if winners else 0
    gross_loss = abs(sum(t["pnl"] for t in losers)) if losers else 1
    pf = gross_win / gross_loss if gross_loss > 0 else 0
    tpd = len(trades_log) / total_days

    return {
        "n_trades": len(trades_log),
        "win_rate": round(wr, 1),
        "total_return_pct": round(total_ret, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "days_to_20pct": days_to_target,
        "reached_date": reached_date,
        "avg_rr": round(rr, 2),
        "profit_factor": round(pf, 2),
        "trades_per_day": round(tpd, 2),
        "trades_log": trades_log,
    }

data, dts = parse_csv(os.path.join(DATA_DIR, "TRXUSDT1_dedup.csv"))
print(f"Data: {len(data)} bars, {dts[0].date()} to {dts[-1].date()}")
print(f"Fixed risk: {RISK_PCT*100}% of starting balance (${START_BAL*RISK_PCT:.2f}/trade)")
print(f"Max aggregate risk: {MAX_AGG_RISK*100}% (${START_BAL*MAX_AGG_RISK:.2f})")
print(f"Fee: {FEE_PCT*100}% per trade (0.2% round trip)")
print(f"Stop slippage: {STOP_SLIPPAGE*100}%")
print()

# Individual lookbacks
print("=" * 80)
print("  INDIVIDUAL LOOKBACKS + COMBINED")
print("=" * 80)
lookback_configs = {"LB=1 only": [1], "LB=2 only": [2], "LB=3 only": [3], "Combined (all 3)": [1, 2, 3]}
results = {}
for name, lbs in lookback_configs.items():
    r = run_backtest(data, dts, lbs)
    results[name] = r
    if r:
        days = f"{r['days_to_20pct']}d" if r['days_to_20pct'] is not None else "N/A"
        hit = " *** 20% in 2wk!" if r['days_to_20pct'] is not None and r['days_to_20pct'] <= 10 else ""
        print(f"  {name:20s}: Trades={r['n_trades']:>5d}  WR={r['win_rate']:>6.1f}%  "
              f"Ret={r['total_return_pct']:>+8.2f}%  DD={r['max_drawdown_pct']:>6.2f}%  "
              f"RR={r['avg_rr']:>5.2f}  PF={r['profit_factor']:>6.2f}  "
              f"TPD={r['trades_per_day']:>6.2f}  To20%:{days}{hit}")
    else:
        print(f"  {name:20s}: No valid trades")

print()
