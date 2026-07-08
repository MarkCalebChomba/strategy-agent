"""
Combined Portfolio — runs TRX combined HA + all A-tier + top B-tier.
Shows total expected TPD, return, DD across multiple allocation schemes.

METHODOLOGY
===========
1. TRX COMBINED HA BACKTEST
   - Runs Heikin-Ashi Momentum (lb=1, 2, 3) simultaneously on TRXUSDT1_dedup.csv
   - Same backtest engine as test_sections.py and equity_curve.py
   - Risk-based sizing: $25/trade (0.25%), $1,000 max aggregate (10%)
   - Equity curve calculated as starting_bal + closed_PnL + unrealized_PnL
   - Simplified Sharpe: WR/100 * RR - 0.5 (approximation, not annualized)

2. DB STRATEGIES LOADING
   - Reads locked_strategies from strategy_bot.db
   - Grades each strategy using the 4-tier system (A-Exceeding / B-Meeting / C-Below / D-Fail)
   - Filters to A-Exceeding and B-Meeting tiers
   - Excludes individual TRXUSDT 1m HA strategies (they are subsumed by the combined run)
   - Deduplicates by (template, symbol, timeframe) — keeps first occurrence

3. PORTFOLIO COMBINATION
   - Weighted average of return, trades-per-year, trades-per-day
   - Combined drawdown: assumes correlation rho=0.2 between all strategy pairs
     var_portfolio = sum(w_i^2 * dd_i^2) + 2 * sum_{i<j}(w_i * w_j * dd_i * dd_j * rho)
     combined_dd = sqrt(var_portfolio)
   - Weighted average of simplified Sharpe ratios

4. ALLOCATION SCHEMES
   - TRX 80% + others equal 20%: largest allocation to TRX combined
   - TRX 50% + others equal 50%: balanced
   - TRX 30% + others equal 70%: diversified away from TRX
   - Equal weight all: each strategy gets 1/N

KEY ASSUMPTIONS
===============
- Correlation assumption: rho=0.2 between all strategy pairs (moderate positive correlation)
  This is a simplified assumption. Real pairwise correlations vary significantly.
  Lower rho reduces combined DD; higher rho increases combined DD.
  rho=0.2 is conservative (slightly positive but not strongly correlated).
- TRX combined HA is tested on ~67 days of dedup CSV data (limited sample)
- DB strategies are tested on historical data of varying length
- Simplified Sharpe = WR/100 * RR - 0.5 (NOT annualized, NOT risk-free rate adjusted)
- No rebalancing costs, slippage, or capacity constraints modeled
- Strategies are assumed to maintain their historical performance forward
- Individual TRX HA (lb=2, lb=3) are removed since lb=1,2,3 combined already includes them
- Deduplication by (template, symbol, timeframe) may discard valid param variants

IMPORTANT NOTES FOR VERIFICATION
=================================
- The TRX combined backtest uses a risk-based model, not position size-based
- Return and DD are percentage-based, scaled by allocation weight
- Combined DD formula assumes normal distributions (variance-covariance approach)
- TPD can exceed 3.0 if TRX combined is weighted heavily (its individual TPD is ~2.18)
- The equal-weight scenario computes each_w = 1.0 / len(all_strats) for all strategies including TRX
- In contrast, combined_metrics() assigns trx_weight to TRX and divides remainder among others
- Volume-weighted allocation is NOT explicitly modeled (only in AGENTS.md findings)
"""

import math, sqlite3, os, sys
from datetime import datetime
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import RUNNERS

DB_PATH = "strategy_bot.db"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TRADING_DAYS = 252
START_BAL = 10000.0
RISK_PCT = 0.0025
MAX_AGG_RISK = 0.10
FEE_PCT = 0.001
STOP_SLIPPAGE = 0.001

# 4-Tier grading thresholds used for filtering DB strategies
TIER_THRESHOLDS = {
    "A-Exceeding": {"min_wr": 50, "min_rr": 2.0, "min_pf": 1.5, "max_dd": 25, "min_tpy": 5},
    "B-Meeting":   {"min_wr": 40, "min_rr": 1.5, "min_pf": 1.2, "max_dd": 30, "min_tpy": 1},
}

def grade(wr, rr, pf, dd, tpy):
    """
    Assign a tier grade based on strategy metrics.
    Checks A first, then B. If neither match, returns D-Fail.
    Note: C-Below tier exists but is not currently used for filtering in this script.
    """
    for tier, t in TIER_THRESHOLDS.items():
        if wr >= t["min_wr"] and rr >= t["min_rr"] and pf >= t["min_pf"] and dd <= t["max_dd"] and tpy >= t["min_tpy"]:
            return tier
    return "D-Fail"

def run_trx_combined():
    """
    Run TRXUSDT 1m HA combined (lb=1,2,3) on dedup CSV.
    V2: Stop-loss enforcement, 0.1% fees, next-bar execution.
    """
    data, dts = [], []
    with open(os.path.join(DATA_DIR, "TRXUSDT1_dedup.csv")) as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) < 6: continue
            dt = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
            data.append({"open": float(parts[1]), "high": float(parts[2]), "low": float(parts[3]), "close": float(parts[4])})
            dts.append(dt)

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
    pending = []

    for i in range(n):
        bar = data[i]
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]

        # Execute pending entries at this bar's OPEN
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
                trades_log.append(net_pnl)
                total_risk -= risk_pt
            else:
                remaining.append(p)
        positions = remaining

        # Signal-based exits
        remaining = []
        for p in positions:
            si = p["strat_idx"]
            if i < len(strategies[si]["sell"]) and strategies[si]["sell"][i]:
                gross_pnl = p["pos_val"] * (c - p["entry_price"]) / p["entry_price"]
                exit_fee = p["pos_val"] * (c / p["entry_price"]) * FEE_PCT
                net_pnl = gross_pnl - p["entry_fee"] - exit_fee
                trades_log.append(net_pnl)
                total_risk -= risk_pt
            else:
                remaining.append(p)
        positions = remaining

        # Queue entries for next bar
        for si, s in enumerate(strategies):
            if i < len(s["buy"]) and s["buy"][i]:
                if not any(pe["strat_idx"] == si for pe in pending):
                    pending.append({"strat_idx": si, "lb": s["lb"]})

        # Equity
        closed_total = sum(trades_log)
        unrealized = sum(p["pos_val"] * (c - p["entry_price"]) / p["entry_price"] for p in positions if p["entry_price"] > 0)
        equity_curve.append(START_BAL + closed_total + unrealized)

    winners = [t for t in trades_log if t > 0]
    losers = [t for t in trades_log if t <= 0]
    wr = len(winners)/len(trades_log)*100 if trades_log else 0
    total_ret = (equity_curve[-1] - START_BAL)/START_BAL*100

    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak: peak = v
        dd = (peak - v)/peak*100
        if dd > max_dd: max_dd = dd

    total_days = (dts[-1] - dts[0]).days
    tpy = len(trades_log) / total_days * 365 if total_days > 0 else 0
    tpd = len(trades_log) / max(total_days, 1)

    avg_win = sum(winners)/len(winners) if winners else 0
    avg_loss = abs(sum(losers)/len(losers)) if losers else 1
    rr = avg_win/avg_loss if avg_loss > 0 else 0

    gross_win = sum(winners) if winners else 0
    gross_loss = abs(sum(losers)) if losers else 1
    pf = gross_win/gross_loss if gross_loss > 0 else 0

    return {
        "name": "TRXUSDT 1m HA Combined (lb=1,2,3)",
        "symbol": "TRXUSDT", "timeframe": "1m",
        "win_rate": round(wr, 1), "avg_rr": round(rr, 2),
        "total_return_pct": round(total_ret, 1),
        "max_drawdown_pct": round(max_dd, 1),
        "trades_per_year": round(tpy, 0),
        "profit_factor": round(pf, 2),
        "sharpe": round(wr/100*rr - 0.5, 3),
        "tpd": round(tpd, 2),
    }

def load_db_strategies(tiers="A,B"):
    """
    Load A-tier and B-tier strategies from locked_strategies table.

    GRADING FILTER
    ==============
    Each locked strategy is graded using the 4-tier system.
    Only A-Exceeding and B-Meeting are returned.
    Trades per day (TPD) is derived from trades_per_year / 252.

    DATABASE QUERY
    ==============
    Reads all columns from locked_strategies:
    id, template, symbol, timeframe, win_rate, avg_rr,
    total_return_pct, max_drawdown_pct, trades_per_year, profit_factor
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, template, symbol, timeframe, win_rate, avg_rr,
               total_return_pct, max_drawdown_pct, trades_per_year,
               profit_factor
        FROM locked_strategies
    """)
    all_strategies = []
    for r in cur.fetchall():
        result = {
            "id": r[0], "template": r[1], "symbol": r[2],
            "timeframe": r[3], "win_rate": r[4] or 0, "avg_rr": r[5] or 0,
            "total_return_pct": r[6] or 0, "max_drawdown_pct": r[7] or 0,
            "trades_per_year": r[8] or 0, "profit_factor": r[9] or 0,
        }
        g = grade(result["win_rate"], result["avg_rr"], result["profit_factor"],
                  abs(result["max_drawdown_pct"]), result["trades_per_year"])
        if g in ["A-Exceeding", "B-Meeting"]:
            result["grade"] = g
            result["tpd"] = result["trades_per_year"] / TRADING_DAYS
            all_strategies.append(result)
    conn.close()
    return all_strategies

def main():
    # 1. Run TRX combined HA
    print("Running TRXUSDT 1m HA combined (lb=1,2,3)...")
    trx = run_trx_combined()
    print(f"  {trx['name']}: WR={trx['win_rate']}%  RR={trx['avg_rr']}  "
          f"Ret={trx['total_return_pct']:+.1f}%  DD={trx['max_drawdown_pct']}%  "
          f"TPY={trx['trades_per_year']:.0f}  TPD={trx['tpd']:.2f}\n")

    # 2. Load DB strategies
    db_strats = load_db_strategies("A,B")
    print(f"Loaded {len(db_strats)} A/B-tier strategies from DB")

    # 3. Remove individual TRX HA (lb=2, lb=3) since they're in the combined
    #    The combined HA runs all three lookbacks simultaneously, so standalone
    #    TRX HA strategies from DB would double-count the same trades.
    filtered = []
    for s in db_strats:
        key = (s["template"], s["symbol"], s["timeframe"])
        if key == ("Heikin-Ashi Momentum", "TRXUSDT", "1m"):
            continue
        filtered.append(s)
    removed = len(db_strats) - len(filtered)
    print(f"Removed {removed} individual TRX HA strategies (subsumed by combined)")
    print()

    # 4. Deduplicate remaining by (template, symbol, timeframe)
    #    Keeps first occurrence, which means the first param variant encountered
    seen = set()
    unique = []
    for s in filtered:
        key = (s["template"], s["symbol"], s["timeframe"])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    print(f"After dedup: {len(unique)} unique strategies")
    print()

    # 5. Compute combined metrics with TRX as the lead
    def combined_metrics(strategies, trx_weight):
        """
        Compute weighted portfolio metrics for a TRX + others allocation.

        PARAMETERS
        ==========
        strategies : list of strategy dicts (excludes TRX)
        trx_weight : float between 0-1, allocation to TRX combined HA

        RETURNS
        =======
        dict with tpy, tpd, return_pct, dd_pct, sharpe

        PORTFOLIO MATH
        ==============
        - Return, TPY, TPD: weighted averages (linear)
        - Combined DD: sqrt(w^T * Sigma * w) where Sigma_ii = dd_i^2, Sigma_ij = rho * dd_i * dd_j
          This assumes drawdown percentages behave like standard deviations, which is a simplification.
        - Sharpe: weighted average of individual simplified Sharpe ratios
        """
        n = len(strategies)
        per_strat = 1.0 - trx_weight
        each_w = per_strat / n if n > 0 else 0

        total_tpy = trx["trades_per_year"] * trx_weight
        total_ret = trx["total_return_pct"] * trx_weight
        total_tpd = trx["tpd"] * trx_weight

        for s in strategies:
            total_tpy += s["trades_per_year"] * each_w
            total_ret += s["total_return_pct"] * each_w
            total_tpd += s["tpd"] * each_w

        # Combined DD with correlation ρ=0.2
        # Var(P) = sum(w_i^2 * dd_i^2) + 2 * sum_{i<j}(w_i * w_j * dd_i * dd_j * rho)
        # Combined DD = sqrt(Var(P))
        rho = 0.2
        dd_items = [(trx_weight, trx["max_drawdown_pct"])]
        for s in strategies:
            dd_items.append((each_w, s["max_drawdown_pct"]))

        var_sum = 0.0
        for i, (w1, d1) in enumerate(dd_items):
            var_sum += (w1 * d1) ** 2
            for j, (w2, d2) in enumerate(dd_items):
                if i < j:
                    var_sum += 2 * w1 * w2 * d1 * d2 * rho
        combined_dd = math.sqrt(var_sum)

        # Average Sharpe (weighted)
        sharpes = [(trx_weight, trx["win_rate"]/100 * trx["avg_rr"] - 0.5)]
        for s in strategies:
            shp = s["win_rate"]/100 * s["avg_rr"] - 0.5
            sharpes.append((each_w, shp))
        avg_sharpe = sum(w * sh for w, sh in sharpes)

        return {
            "tpy": round(total_tpy, 0),
            "tpd": round(total_tpd, 3),
            "return_pct": round(total_ret, 1),
            "dd_pct": round(combined_dd, 1),
            "sharpe": round(avg_sharpe, 3),
        }

    print("=" * 85)
    print("  COMBINED PORTFOLIO - All Winning Strategies")
    print("=" * 85)

    # Show top 15 strategies by TPD
    all_strats = [trx] + unique
    all_strats.sort(key=lambda s: s["tpd"], reverse=True)
    print(f"\n  Top strategies by TPD:")
    print(f"  {'#':3s} {'Name':45s} {'WR%':5s} {'RR':5s} {'Ret%':7s} "
          f"{'DD%':5s} {'TPD':6s}")
    print(f"  {'-'*80}")
    for i, s in enumerate(all_strats[:20]):
        name = f"{s['symbol']} {s['timeframe']} {s.get('template','')[:25]}"
        print(f"  {i+1:3d} {name:45s} {s['win_rate']:>5.1f} {s['avg_rr']:>5.2f} "
              f"{s['total_return_pct']:>+7.1f} {s['max_drawdown_pct']:>5.1f} "
              f"{s['tpd']:>6.2f}")
    print()

    # Allocation scenarios
    print(f"\n{'='*70}")
    print(f"  PORTFOLIO SCENARIOS (TRX HA combined + {len(unique)} other strategies)")
    print(f"{'='*70}")

    scenarios = [
        ("TRX 80% + others equal 20%", 0.80),
        ("TRX 50% + others equal 50%", 0.50),
        ("TRX 30% + others equal 70%", 0.30),
        ("Equal weight all", 1.0 / (len(all_strats)) * len(all_strats)),
    ]

    # For equal weight, we need to compute differently
    def equal_weight_metrics(all_strats):
        """
        Compute portfolio metrics with equal weight across ALL strategies (including TRX).
        Each strategy gets weight = 1 / N.
        This differs from combined_metrics where TRX gets trx_weight and others split remainder.
        """
        n = len(all_strats)
        each_w = 1.0 / n
        total_tpy = sum(s["trades_per_year"] * each_w for s in all_strats)
        total_ret = sum(s["total_return_pct"] * each_w for s in all_strats)
        total_tpd = sum(s["tpd"] * each_w for s in all_strats)

        rho = 0.2
        var_sum = 0.0
        for i, s1 in enumerate(all_strats):
            var_sum += (each_w * s1["max_drawdown_pct"]) ** 2
            for j, s2 in enumerate(all_strats):
                if i < j:
                    var_sum += 2 * each_w * each_w * s1["max_drawdown_pct"] * s2["max_drawdown_pct"] * rho
        combined_dd = math.sqrt(var_sum)

        sharpe_vals = [(s["win_rate"]/100 * s["avg_rr"] - 0.5) * each_w for s in all_strats]
        avg_sharpe = sum(sharpe_vals)

        return {
            "tpy": round(total_tpy, 0), "tpd": round(total_tpd, 3),
            "return_pct": round(total_ret, 1), "dd_pct": round(combined_dd, 1),
            "sharpe": round(avg_sharpe, 3),
        }

    print(f"\n  {'Scenario':50s} {'Ret%':>7s} {'DD%':>6s} {'Sharpe':>7s} {'TPY':>6s} {'TPD':>7s}")
    print(f"  {'-'*85}")

    for label, tw in scenarios:
        if label.startswith("Equal"):
            m = equal_weight_metrics(all_strats)
        else:
            m = combined_metrics(unique, tw)
        print(f"  {label:50s} {m['return_pct']:>+7.1f} {m['dd_pct']:>6.1f} "
              f"{m['sharpe']:>7.3f} {m['tpy']:>6.0f} {m['tpd']:>7.3f}")

    print()
    print(f"  TRX alone:           {trx['total_return_pct']:>+7.1f} {trx['max_drawdown_pct']:>6.1f}        "
          f"{trx['trades_per_year']:>6.0f} {trx['tpd']:>7.3f}")

    print()
    print("  NOTE: TRX combined HA (lb=1,2,3) tested on 67 days of dedup CSV data")
    print("  Other strategies from DB (backtest engine results)")
    print(f"  Total unique strategies in portfolio: {len(all_strats)}")

    # Show per-strategy allocation for the recommended scenario
    print(f"\n{'='*85}")
    print(f"  DETAILED ALLOCATION - TRX 80% + others equal 20%")
    print(f"{'='*85}")

    rec_tw = 0.80
    others_w = 1.0 - rec_tw
    each_other = others_w / len(unique) if unique else 0

    # Sort by weight desc
    items = [("TRXUSDT 1m HA Combined (lb=1,2,3)", rec_tw, trx)]
    for s in sorted(unique, key=lambda x: x["tpd"], reverse=True):
        name = f"{s['symbol']} {s['timeframe']} {s.get('template','')[:30]}"
        items.append((name, each_other, s))

    print(f"\n  {'#':3s} {'Strategy':55s} {'Alloc':>7s} {'WR%':>6s} {'RR':>5s} "
          f"{'Ret%':>7s} {'DD%':>6s} {'TPD':>7s}")
    print(f"  {'-'*98}")
    for i, (name, w, s) in enumerate(items):
        print(f"  {i+1:3d} {name:55s} {w*100:>6.1f}% {s['win_rate']:>6.1f} "
              f"{s['avg_rr']:>5.2f} {s['total_return_pct']:>+7.1f} "
              f"{s['max_drawdown_pct']:>6.1f} {s['tpd']:>7.2f}")

    m = combined_metrics(unique, rec_tw)
    print(f"  {'-'*98}")
    print(f"  {'PORTFOLIO TOTAL':55s} {'100.0%':>7s} {'':>6s} {'':>5s} "
          f"{m['return_pct']:>+7.1f} {m['dd_pct']:>6.1f} {m['tpd']:>7.3f}")

if __name__ == "__main__":
    main()
