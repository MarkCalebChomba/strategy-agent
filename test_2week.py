"""
Clean test: TRXUSDT 1m Heikin-Ashi Momentum with 0.25% fixed risk.
All lookback variants share ONE account. Correct capital accounting.
"""
import os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import RUNNERS

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
START_BAL = 10000.0
RISK_PCT = 0.0025
MAX_AGG_RISK = 0.10

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

def run_portfolio(data, dts, strategies):
    """Run all strategies on ONE account.
    Each trade risks RISK_PCT of START_BAL. Risk = committed margin.
    Max aggregate risk across all positions = MAX_AGG_RISK of START_BAL.
    Equity = starting_cash + closed_PnL + unrealized_PnL.
    """
    n = len(data)
    base_cash = START_BAL
    risk_per_trade = START_BAL * RISK_PCT
    max_risk = START_BAL * MAX_AGG_RISK
    positions = []
    equity_curve = [base_cash]
    trades_log = []
    total_risk = 0.0

    for i in range(n):
        close = data[i]["close"]

        # Exits
        pnl_closed = 0.0
        remaining = []
        for p in positions:
            si = p["strat_idx"]
            sell = strategies[si]["sell"]
            if i < len(sell) and sell[i]:
                pnl = p["pos_val"] * (close - p["entry_price"]) / p["entry_price"] if p["entry_price"] > 0 else 0
                pnl_closed += pnl
                trades_log.append({"pnl": pnl, "date": dts[i], "lb": p["lb"]})
                total_risk -= risk_per_trade
            else:
                remaining.append(p)
        positions = remaining

        # Entries
        for si, s in enumerate(strategies):
            if i < len(s["buy"]) and s["buy"][i]:
                if any(p["strat_idx"] == si for p in positions):
                    continue
                if total_risk + risk_per_trade > max_risk:
                    continue

                atr = sum(data[max(0,i-14):i][j]["high"] - data[max(0,i-14):i][j]["low"] for j in range(min(14, i))) / max(1, min(14, i))
                stop_dist = max(2 * atr, close * 0.005)
                stop_pct = stop_dist / close if close > 0 else 0.02
                pos_val = risk_per_trade / stop_pct  # notional value at risk

                total_risk += risk_per_trade
                positions.append({
                    "strat_idx": si, "lb": s["lb"],
                    "entry_price": close, "pos_val": pos_val, "entry_bar": i,
                })

        # Equity = cash (stable) + closed PnL + unrealized PnL
        closed_pnl_total = sum(t["pnl"] for t in trades_log)
        unrealized = sum(p["pos_val"] * (close - p["entry_price"]) / p["entry_price"] for p in positions if p["entry_price"] > 0)
        equity_curve.append(base_cash + closed_pnl_total + unrealized)

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
    for i, v in enumerate(equity_curve):
        if v >= target:
            if i > 0:
                bar = min(i - 1, len(dts) - 1)
                days_to_target = (dts[bar] - dts[0]).days
                reached_date = dts[bar]
            else:
                days_to_target = 0
            break

    return {
        "n_trades": len(trades_log),
        "win_rate": round(wr, 1),
        "total_return_pct": round(total_ret, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "days_to_20pct": days_to_target,
        "reached_date": reached_date,
        "trades_log": trades_log,
    }


data, dts = parse_csv(os.path.join(DATA_DIR, "TRXUSDT1.csv"))
print(f"Data: {len(data)} bars, {dts[0].date()} to {dts[-1].date()}")
print(f"Fixed risk: {RISK_PCT*100}% of starting balance (${START_BAL*RISK_PCT:.2f}/trade)")
print(f"Max aggregate risk: {MAX_AGG_RISK*100}% (${START_BAL*MAX_AGG_RISK:.2f})")
print()

# Generate signals for each lookback
strategies = []
for lb in [1, 2, 3]:
    buy, sell = RUNNERS["Heikin-Ashi Momentum"](data, {"lookback": lb})
    strategies.append({"lb": lb, "buy": buy, "sell": sell, "signals": sum(1 for b in buy if b)})
    print(f"  lb={lb}: {strategies[-1]['signals']} buy signals")

print(f"\n{'='*60}")
print(f"  RUNNING ALL 3 ON ONE ACCOUNT")
print(f"{'='*60}")

result = run_portfolio(data, dts, strategies)
if result:
    print(f"  Trades:      {result['n_trades']}")
    print(f"  Win rate:    {result['win_rate']:.1f}%")
    print(f"  Return:      {result['total_return_pct']:+.2f}%")
    print(f"  Max DD:      {result['max_drawdown_pct']:.2f}%")
    if result['days_to_20pct'] is not None:
        print(f"  To 20%:      {result['days_to_20pct']} days ({result['days_to_20pct']/5:.1f} trading weeks)")
        print(f"  Reached:     {result['reached_date'].date()}")
        if result['days_to_20pct'] <= 10:
            print(f"  *** WITHIN 2-WEEK TARGET ***")
        else:
            print(f"  OVER 2-week target by {result['days_to_20pct']-10} days")
    else:
        print(f"  Did NOT reach 20%")
else:
    print(f"  No valid trades")

print(f"\n{'='*60}")
print(f"  INDIVIDUAL (corrected)")  
print(f"{'='*60}")
for lb in [1, 2, 3]:
    single_strat = [{"lb": lb, "buy": strategies[lb-1]["buy"], "sell": strategies[lb-1]["sell"], "signals": strategies[lb-1]["signals"]}]
    r = run_portfolio(data, dts, single_strat)
    if r:
        days = r['days_to_20pct'] if r['days_to_20pct'] else 'N/A'
        print(f"  lb={lb}: {r['n_trades']:>5d} trades, {r['total_return_pct']:>+8.2f}% ret, {r['max_drawdown_pct']:>5.2f}% DD, {days} days to 20%")
