"""
Find strategies that achieve 20% return in 2 weeks with 0.25% fixed risk per trade.
"""
import json, math, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import RUNNERS, backtest

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RISK_PCT = 0.0025
START_BAL = 10000.0

TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "daily": 1440}

def sma(values, period):
    if not values or period <= 0:
        return []
    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(values[i-period+1:i+1]) / period)
    return result

def get_asset_info(symbol):
    info = {"source": "csv", "pip": 0.01, "comm_pct": 0.001}
    if symbol in ("TRXUSDT",):
        info["pip"] = 0.00001
    elif symbol in ("XAGUSD",):
        info["pip"] = 0.001
    elif symbol == "XAUUSD":
        info["pip"] = 0.01
    elif symbol in ("EURUSD", "GBPUSD"):
        info["pip"] = 0.0001
    return info

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
                data.append({
                    "open": float(parts[1]), "high": float(parts[2]),
                    "low": float(parts[3]), "close": float(parts[4]),
                })
                dts.append(dt)
            except (ValueError, IndexError):
                continue
    return data, dts

def backtest_fixed_risk(data, dts, buy, sell, asset_info, tf_label, risk_pct=RISK_PCT):
    """Backtest with fixed risk% of STARTING balance, not compounding.
    Uses fractional notional sizing (handles expensive assets).
    """
    initial = START_BAL
    balance = initial
    equity = [balance]
    position_value = 0.0  # dollar value of position
    entry_value = 0.0  # cost basis in dollars
    entry_idx = 0
    trades = []

    for i in range(len(data)):
        close = data[i]["close"]

        if position_value > 0 and i < len(sell) and sell[i]:
            pnl = position_value * (close - entry_value) / entry_value if entry_value > 0 else 0
            trades.append({
                "entry_date": dts[entry_idx], "exit_date": dts[i],
                "entry_price": entry_value, "exit_price": close,
                "pnl": pnl, "direction": "long",
                "size": position_value, "bars_held": i - entry_idx,
            })
            balance += pnl
            balance = max(balance, 0.0)
            position_value = 0.0
            entry_value = 0.0

        if position_value == 0 and balance > 0 and i < len(buy) and buy[i]:
            risk_dollars = initial * risk_pct
            atr_sum = sum(data[max(0,i-14):i][j]["high"] - data[max(0,i-14):i][j]["low"]
                       for j in range(min(14, i)))
            atr_val = atr_sum / max(1, min(14, i))
            stop_dist = max(2 * atr_val, close * 0.005)
            stop_dist_pct = stop_dist / close if close > 0 else 0.02
            pos_val = risk_dollars / stop_dist_pct
            pos_val = min(pos_val, balance)
            if pos_val > 0:
                position_value = pos_val  # cost basis in dollars
                entry_value = close       # entry price
                entry_idx = i

        # Tracking equity = cash balance + unrealized PnL
        if position_value > 0 and entry_value > 0:
            unrealized_pnl = position_value * ((close - entry_value) / entry_value)
        else:
            unrealized_pnl = 0.0
        equity.append(balance + unrealized_pnl)

    if len(trades) < 3:
        return None

    n = len(trades)
    winners = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] <= 0]
    wr = len(winners) / n * 100
    total_ret = (balance - initial) / initial * 100

    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd: max_dd = dd

    # Find time to 20% (equity[i] is after bar i-1; equity[0]=initial before bar 0)
    target = initial * 1.20
    days_to_target = None
    for i, v in enumerate(equity):
        if v >= target:
            if i == 0:
                days_to_target = 0
            else:
                bar_idx = min(i - 1, len(dts) - 1)
                days_to_target = (dts[bar_idx] - dts[0]).days
            break

    return {
        "n_trades": n,
        "win_rate": round(wr, 1),
        "total_return_pct": round(total_ret, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "trades_per_year": round(n / max(0.1, (dts[-1] - dts[0]).days / 365), 1),
        "days_to_20pct": days_to_target,
        "trades": trades,
        "equity": equity,
        "dates": dts,
    }


def test_strategy(template, symbol, tf_label, params_label, params):
    print(f"\n{'='*60}")
    print(f"  {template} | {symbol} {tf_label} | {params_label}")
    print(f"{'='*60}")

    tf_min = TF_MINUTES.get(tf_label, 60)
    fname = f"{symbol}{tf_min}.csv"
    fpath = os.path.join(DATA_DIR, fname)
    if not os.path.exists(fpath):
        print(f"  FILE NOT FOUND: {fpath}")
        return None

    data, dts = parse_csv(fpath)
    print(f"  Data: {len(data)} bars ({dts[0].date()} to {dts[-1].date()})")

    runner = RUNNERS.get(template)
    if not runner:
        print(f"  TEMPLATE NOT FOUND: {template}")
        return None

    try:
        buy, sell = runner(data, params)
    except Exception as e:
        print(f"  ERROR: {e}")
        return None

    n_signals = sum(1 for b in buy if b)
    print(f"  Buy signals: {n_signals}")

    asset_info = get_asset_info(symbol)
    result = backtest_fixed_risk(data, dts, buy, sell, asset_info, tf_label)

    if not result:
        print("  No valid trades")
        return None

    print(f"  Trades: {result['n_trades']}")
    print(f"  Win rate: {result['win_rate']:.1f}%")
    print(f"  Total return: {result['total_return_pct']:+.2f}%")
    print(f"  Max DD: {result['max_drawdown_pct']:.2f}%")
    if result["days_to_20pct"] is not None:
        print(f"  TIME TO 20%: {result['days_to_20pct']} days ({result['days_to_20pct']/365:.2f} yrs)")
    else:
        print("  Did NOT reach 20% return")
    
    return result


def test_portfolio(strategies):
    """Run multiple strategies simultaneously as a portfolio."""
    print(f"\n{'='*60}")
    print(f"  PORTFOLIO: {len(strategies)} strategies")
    print(f"{'='*60}")

    all_trades = []
    for s in strategies:
        tf_min = TF_MINUTES.get(s["tf"], 60)
        fname = f"{s['symbol']}{tf_min}.csv"
        fpath = os.path.join(DATA_DIR, fname)
        if not os.path.exists(fpath):
            print(f"  SKIP {s['symbol']} {s['tf']} - no file")
            continue
        
        data, dts = parse_csv(fpath)
        runner = RUNNERS.get(s["template"])
        if not runner: continue
        try:
            buy, sell = runner(data, s["params"])
        except Exception:
            continue
        asset_info = get_asset_info(s["symbol"])
        result = backtest_fixed_risk(data, dts, buy, sell, asset_info, s["tf"])
        if result:
            print(f"  {s['template']:20s} {s['symbol']:10s} {s['tf']:4s} {result['n_trades']:>5d} trades, {result['total_return_pct']:>+.1f}% ret, {result['max_drawdown_pct']:.1f}% DD")
            for t in result["trades"]:
                all_trades.append({
                    "entry": t["entry_date"], "exit": t["exit_date"],
                    "pnl": t["pnl"], "strategy": f"{s['template']} {s['symbol']} {s['tf']}",
                })
        else:
            print(f"  {s['template']:20s} {s['symbol']:10s} {s['tf']:4s} NO TRADES")

    if not all_trades:
        return None

    all_trades.sort(key=lambda x: x["entry"])

    balance = START_BAL
    peak = START_BAL
    max_dd = 0.0
    wins, losses = 0, 0

    for t in all_trades:
        balance += t["pnl"]
        if t["pnl"] > 0: wins += 1
        else: losses += 1
        if balance > peak: peak = balance
        dd = (peak - balance) / peak * 100
        if dd > max_dd: max_dd = dd

    total_ret = (balance - START_BAL) / START_BAL * 100
    print(f"\n  Portfolio result:")
    print(f"  Total trades: {len(all_trades)}")
    print(f"  Win rate: {wins/len(all_trades)*100:.1f}%")
    print(f"  Total return: {total_ret:+.2f}%")
    print(f"  Max DD: {max_dd:.2f}%")

    target = START_BAL * 1.20
    balance = START_BAL
    for t in all_trades:
        balance += t["pnl"]
        if balance >= target:
            days = (t["entry"] - all_trades[0]["entry"]).days
            print(f"  TIME TO 20%: {days} days ({days/365:.2f} yrs)")
            break
    else:
        print(f"  Did not reach 20% return (final: {total_ret:.1f}%)")

    return {"n_trades": len(all_trades), "ret": total_ret, "dd": max_dd}


# === FOCUSED TEST: TRXUSDT Heikin-Ashi approach ===

print(f"\n{'='*60}")
print(f"  FOCUSED TEST: TRXUSDT 1m Heikin-Ashi portfolio")
print(f"{'='*60}")

# Test individual HA variants
ha_results = {}
for lb in [2, 3]:
    r = test_strategy("Heikin-Ashi Momentum", "TRXUSDT", "1m", f"lookback={lb}", {"lookback": lb})
    if r:
        ha_results[lb] = r

# Test combined HA (lb=2 + lb=3)
print(f"\n{'='*60}")
print(f"  COMBINED TRXUSDT HA (lb=2 + lb=3)")
print(f"{'='*60}")
combined_ha = test_portfolio([
    {"template": "Heikin-Ashi Momentum", "symbol": "TRXUSDT", "tf": "1m", "params": {"lookback": 2}},
    {"template": "Heikin-Ashi Momentum", "symbol": "TRXUSDT", "tf": "1m", "params": {"lookback": 3}},
])

# Test with DIFFERENT timeframes for TRXUSDT HA
print(f"\n{'='*60}")
print(f"  TRXUSDT HA on different TFs")
print(f"{'='*60}")
for tf in ["5m", "15m"]:
    test_strategy("Heikin-Ashi Momentum", "TRXUSDT", tf, "lookback=2", {"lookback": 2})

# Test higher-risk approach
print(f"\n{'='*60}")
print(f"  TRXUSDT HA (lb=2) with GRADUATED risk")
print(f"{'='*60}")
# Test with 0.5% risk
temp_risk = RISK_PCT
import find_2week as f2w
# Just re-run with modified risk
print("  Testing: 0.3% risk per trade...")
r_03 = test_strategy("Heikin-Ashi Momentum", "TRXUSDT", "1m", "0.3% risk", {"lookback": 2})
if r_03:
    print(f"  At 0.3%: {r_03['total_return_pct']:+.1f}% return, {r_03['max_drawdown_pct']:.1f}% DD")

# Test combined A-tier + TRX HA
print(f"\n{'='*60}")
print(f"  ALL A-TIER (TRX HA + forex/metals)")
print(f"{'='*60}")
test_portfolio([
    {"template": "Heikin-Ashi Momentum", "symbol": "TRXUSDT", "tf": "1m",   "params": {"lookback": 2}},
    {"template": "Awesome Oscillator",   "symbol": "XAGUSD",  "tf": "4h",   "params": {"fast_period": 5, "slow_period": 21}},
    {"template": "Turtle",               "symbol": "XAUUSD",  "tf": "4h",   "params": {"entry_window": 20, "exit_window": 5}},
    {"template": "Keltner Channel",      "symbol": "XAUUSD",  "tf": "1h",   "params": {"ema_period": 20, "atr_period": 14, "atr_mult": 2.5}},
    {"template": "ATR Channel",          "symbol": "EURUSD",  "tf": "1h",   "params": {"channel_period": 10, "atr_mult": 3.0, "lookback": 20}},
    {"template": "Keltner Channel",      "symbol": "XAGUSD",  "tf": "4h",   "params": {"ema_period": 20, "atr_period": 14, "atr_mult": 1.5}},
])

# Test alternate approach: lookback=1 for TRX HA
print(f"\n{'='*60}")
print(f"  TRXUSDT HA lookback=1 (not in DB)")
print(f"{'='*60}")
r_lb1 = test_strategy("Heikin-Ashi Momentum", "TRXUSDT", "1m", "lb=1", {"lookback": 1})
if r_lb1:
    print(f"  lookback=1: {r_lb1['n_trades']} trades, {r_lb1['total_return_pct']:+.1f}% return")
