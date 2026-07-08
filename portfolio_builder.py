"""
Portfolio Builder — combine strategies into multi-asset portfolio.
Uses Monte Carlo simulation to estimate combined metrics.

Usage:
  python portfolio_builder.py                        # default: all A-tier
  python portfolio_builder.py --tier A               # A-tier only
  python portfolio_builder.py --tier A,B             # A + B tier
  python portfolio_builder.py --equal                # equal weight (skip optimization)
  python portfolio_builder.py --detailed             # show per-strategy stats
"""

import argparse
import math
import random
import sqlite3
import sys
from collections import defaultdict

DB_PATH = "strategy_bot.db"

random.seed(42)

TIER_THRESHOLDS = {
    "A-Exceeding": {"min_wr": 50, "min_rr": 2.0, "min_pf": 1.5, "max_dd": 25, "min_tpy": 5},
    "B-Meeting":   {"min_wr": 40, "min_rr": 1.5, "min_pf": 1.2, "max_dd": 30, "min_tpy": 1},
    "C-Below":     {"min_wr": 35, "min_rr": 1.2, "min_pf": 1.0, "max_dd": 35, "min_tpy": 0.1},
}

TRADING_DAYS_PER_YEAR = 252


def grade(result):
    wr = result["win_rate"]
    rr = result["avg_rr"]
    pf = result.get("profit_factor", result.get("pf", 0))
    dd = result["max_drawdown_pct"]
    tpy = result["trades_per_year"]
    for tier_name, thresh in TIER_THRESHOLDS.items():
        if (wr >= thresh["min_wr"] and rr >= thresh["min_rr"] and
                pf >= thresh["min_pf"] and dd <= thresh["max_dd"] and
                tpy >= thresh["min_tpy"]):
            return tier_name
    return "D-Fail"


def load_strategies(db_path, tiers=None):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    if tiers:
        all_strategies = []
        for tier in tiers.split(","):
            tier = tier.strip()
            if tier == "A":
                tier = "A-Exceeding"
            elif tier == "B":
                tier = "B-Meeting"
            cur.execute("""
                SELECT id, template, symbol, timeframe, win_rate, avg_rr,
                       total_return_pct, max_drawdown_pct, trades_per_year,
                       profit_factor, calmar, sharpe
                FROM locked_strategies
            """)
            for row in cur.fetchall():
                result = {
                    "id": row[0], "template": row[1], "symbol": row[2],
                    "timeframe": row[3], "win_rate": row[4], "avg_rr": row[5],
                    "total_return_pct": row[6], "max_drawdown_pct": row[7],
                    "trades_per_year": row[8], "profit_factor": row[9],
                    "calmar": row[10], "sharpe": row[11],
                }
                if grade(result) == tier:
                    all_strategies.append(result)
    else:
        cur.execute("""
            SELECT id, template, symbol, timeframe, win_rate, avg_rr,
                   total_return_pct, max_drawdown_pct, trades_per_year,
                   profit_factor, calmar, sharpe
            FROM locked_strategies
        """)
        all_strategies = [
            {
                "id": r[0], "template": r[1], "symbol": r[2],
                "timeframe": r[3], "win_rate": r[4], "avg_rr": r[5],
                "total_return_pct": r[6], "max_drawdown_pct": r[7],
                "trades_per_year": r[8], "profit_factor": r[9],
                "calmar": r[10], "sharpe": r[11],
            }
            for r in cur.fetchall()
        ]

    conn.close()
    return all_strategies


RISK_PER_TRADE = 0.01  # 1% risk per trade (matches backtest_engine.py)


def simulate_strategy_trades(strat, years=5):
    """Simulate individual trades for one strategy using random daily occurrence.
    Returns list of (day_index, daily_return_decimal) for each trade day.
    """
    tpy = strat["trades_per_year"]
    wr = strat["win_rate"] / 100.0
    rr = strat["avg_rr"]
    total_days = int(years * TRADING_DAYS_PER_YEAR)
    trades = []

    daily_prob = tpy / TRADING_DAYS_PER_YEAR

    # Convert to daily return (decimal): risk% * RR/(RR+1) for win, risk% * -1/(RR+1) for loss
    win_ret = RISK_PER_TRADE * rr / (rr + 1) if rr > 0 else RISK_PER_TRADE * 0.5
    loss_ret = -RISK_PER_TRADE * 1.0 / (rr + 1) if rr > 0 else -RISK_PER_TRADE * 0.5

    for day in range(total_days):
        if random.random() < daily_prob:
            is_win = random.random() < wr
            ret = win_ret if is_win else loss_ret

            return_vol = abs(ret) * 0.3  # add variance: 30% of trade size
            ret += random.gauss(0, return_vol)

            trades.append((day, ret))
    return trades


def run_simulation(strategies, weights, years=10, n_sims=1000):
    """Run Monte Carlo simulation with given weights.
    weights: dict mapping strategy id -> allocation weight (sums to 1)
    Each strategy simulates trades via random Bernoulli daily occurrence.
    Portfolio: daily_ret = sum(weight_i * strat_daily_ret_i), compounded daily.
    """
    n_strats = len(strategies)
    if not n_strats:
        return None

    all_sharpes = []
    all_returns = []
    all_dds = []
    all_tpds = []
    total_days = int(years * TRADING_DAYS_PER_YEAR)

    for sim in range(n_sims):
        # Pre-generate daily return series for each strategy
        strat_daily_rets = []
        for s_idx, strat in enumerate(strategies):
            weight = weights.get(strat["id"], 0)
            if weight <= 0:
                strat_daily_rets.append(None)
                continue
            trades = simulate_strategy_trades(strat, years)
            daily = [0.0] * total_days
            for day, ret in trades:
                if day < total_days:
                    daily[day] += ret * weight
            strat_daily_rets.append(daily)

        # Combine into portfolio
        portfolio_daily = [0.0] * total_days
        for d in range(total_days):
            for s_idx, daily in enumerate(strat_daily_rets):
                if daily is not None:
                    portfolio_daily[d] += daily[d]

        # Compound equity curve
        eq = 1.0
        cum_eq = [1.0]
        peak = 1.0
        max_dd = 0.0
        for d in range(total_days):
            eq *= (1.0 + portfolio_daily[d])
            cum_eq.append(eq)
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd

        total_ret = (eq - 1.0) * 100

        # Sharpe from daily returns
        daily_rets = [portfolio_daily[d] for d in range(total_days)]
        if daily_rets:
            avg_r = sum(daily_rets) / len(daily_rets)
            var = sum((r - avg_r) ** 2 for r in daily_rets) / len(daily_rets)
            std_r = math.sqrt(var) if var > 0 else 1e-10
            sharpe = (avg_r / std_r) * math.sqrt(252)
        else:
            sharpe = 0.0

        all_sharpes.append(sharpe)
        all_returns.append(total_ret)
        all_dds.append(max_dd)
        all_tpds.append(sum(s["trades_per_year"] * weights.get(s["id"], 0) / TRADING_DAYS_PER_YEAR for s in strategies))

    if not all_sharpes:
        return None

    all_sharpes.sort()
    all_returns.sort()
    all_dds.sort()

    n = len(all_sharpes)
    return {
        "sharpe": {
            "mean": sum(all_sharpes) / n,
            "median": all_sharpes[n // 2],
            "p10": all_sharpes[int(n * 0.1)],
            "p90": all_sharpes[int(n * 0.9)],
        },
        "return_pct": {
            "mean": sum(all_returns) / n,
            "median": all_returns[n // 2],
        },
        "max_dd_pct": {
            "mean": sum(all_dds) / n,
            "median": all_dds[n // 2],
            "p10": all_dds[int(n * 0.1)],
            "p90": all_dds[int(n * 0.9)],
        },
        "tpd": sum(s["trades_per_year"] * weights.get(s["id"], 0) / TRADING_DAYS_PER_YEAR for s in strategies),
    }






def find_optimal_allocation(strategies, years=10, n_sims=200):
    """Grid search for optimal weights using Sharpe ratio."""
    n = len(strategies)
    if n == 0:
        return None, None

    # For 1-3 strategies, do fine grid; for >3, do coarse grid
    best_sharpe = -999
    best_weights = None
    best_metrics = None

    def try_weights(w):
        nonlocal best_sharpe, best_weights, best_metrics
        norm = sum(w)
        if norm <= 0:
            return
        w = [x / norm for x in w]
        weight_dict = {strategies[i]["id"]: w[i] for i in range(n)}
        result = run_simulation(strategies, weight_dict, years, n_sims)
        if result and result["sharpe"]["mean"] > best_sharpe:
            best_sharpe = result["sharpe"]["mean"]
            best_weights = w
            best_metrics = result

    if n == 1:
        try_weights([1.0])
    elif n == 2:
        for a1 in range(0, 101, 5):
            try_weights([a1, 100 - a1])
    elif n == 3:
        for a1 in range(10, 101, 10):
            for a2 in range(0, 101 - a1, 10):
                try_weights([a1, a2, 100 - a1 - a2])
    else:
        # Get TPD proportion as starting point, then try variations
        total_tpy = sum(s["trades_per_year"] for s in strategies)
        if total_tpy > 0:
            base = [s["trades_per_year"] / total_tpy for s in strategies]
        else:
            base = [1.0 / n] * n
        try_weights(base)
        try_weights([1.0 / n] * n)
        # Try giving more weight to high Sharpe
        sharpes = [s.get("sharpe", 0) or 0 for s in strategies]
        total_sharpe = sum(abs(s) + 0.01 for s in sharpes)
        sharpe_w = [(abs(s) + 0.01) / total_sharpe for s in sharpes]
        try_weights(sharpe_w)
        # Try giving more weight to low DD
        dds = [s["max_drawdown_pct"] for s in strategies]
        inv_dd = [1.0 / max(d, 0.1) for d in dds]
        total_inv = sum(inv_dd)
        dd_w = [d / total_inv for d in inv_dd]
        try_weights(dd_w)

    return best_weights, best_metrics


def print_strategy_table(strategies):
    print(f"{'#':>3} {'Template':24s} {'Symbol':10s} {'TF':6s} "
          f"{'WR%':>5} {'RR':>5} {'Ret%':>7} {'DD%':>5} {'TPY':>6} {'PF':>5} {'Sharpe':>7}")
    print("-" * 85)
    for i, s in enumerate(strategies):
        print(f"{i+1:>3} {s['template']:24s} {s['symbol']:10s} {s['timeframe']:6s} "
              f"{s['win_rate']:>5.1f} {s['avg_rr']:>5.2f} {s['total_return_pct']:>7.1f} "
              f"{s['max_drawdown_pct']:>5.1f} {s['trades_per_year']:>6.1f} "
              f"{s.get('profit_factor', 0):>5.2f} {s.get('sharpe', 0):>7.3f}")


def main():
    parser = argparse.ArgumentParser(description="Portfolio Builder")
    parser.add_argument("--tier", default="A", help="Tier filter (A, B, A,B, etc)")
    parser.add_argument("--equal", action="store_true", help="Skip optimization, use equal weights")
    parser.add_argument("--detailed", action="store_true", help="Show per-strategy stats")
    parser.add_argument("--years", type=int, default=10, help="Simulation years")
    parser.add_argument("--sims", type=int, default=1000, help="Monte Carlo simulations")
    args = parser.parse_args()

    strategies = load_strategies(DB_PATH, args.tier)
    if not strategies:
        print(f"No strategies found for tier={args.tier}")
        return

    # Deduplicate by (template, symbol, timeframe) — keep first occurrence
    seen = set()
    unique = []
    for s in strategies:
        key = (s["template"], s["symbol"], s["timeframe"])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    strategies = unique

    print("=" * 85)
    print(f"  PORTFOLIO BUILDER - {len(strategies)} strategies (tier={args.tier})")
    print("=" * 85)

    if args.detailed:
        print_strategy_table(strategies)
        print()

    # Compute portfolio metrics for each allocation strategy
    def compute_portfolio_metrics(strategies, weights_label, weights):
        total_w = sum(weights.values())
        if total_w <= 0:
            return
        w = {k: v / total_w for k, v in weights.items()}

        w_return = sum(s["total_return_pct"] * w.get(s["id"], 0) for s in strategies)
        w_dd = sum(s["max_drawdown_pct"] * w.get(s["id"], 0) for s in strategies)
        w_sharpe = sum((s.get("sharpe", 0) or 0) * w.get(s["id"], 0) for s in strategies)
        w_tpy = sum(s["trades_per_year"] * w.get(s["id"], 0) for s in strategies)
        w_pf = sum(s.get("profit_factor", 0) * w.get(s["id"], 0) for s in strategies)

        # Estimate combined DD with assumed correlation ρ=0.2 between strategies
        rho = 0.2
        n = len(strategies)
        var_sum = 0.0
        for i, s1 in enumerate(strategies):
            wi = w.get(s1["id"], 0)
            var_sum += (wi * s1["max_drawdown_pct"]) ** 2
            for j, s2 in enumerate(strategies):
                if i < j:
                    wj = w.get(s2["id"], 0)
                    var_sum += 2 * wi * wj * s1["max_drawdown_pct"] * s2["max_drawdown_pct"] * rho
        combined_dd = math.sqrt(var_sum)

        # Combined return assuming independence (simple weighted)
        combined_ret = w_return

        print(f"\n  {weights_label}:")
        for s in strategies:
            print(f"    {s['template']:24s} {s['symbol']:10s} {s['timeframe']:6s}  {w.get(s['id'], 0)*100:>6.2f}%")
        print(f"  {'-' * 50}")
        print(f"  Combined Return:  {combined_ret:>8.1f}%")
        print(f"  Combined DD (rho={rho}): {combined_dd:>8.1f}%")
        print(f"  Sharpe (wtd avg): {w_sharpe:>8.3f}")
        print(f"  PF (wtd avg):     {w_pf:>8.2f}")
        print(f"  Trades/yr:        {w_tpy:>8.1f}")
        print(f"  Trades/day:       {w_tpy / TRADING_DAYS_PER_YEAR:>8.3f}")

    if args.equal:
        weights = {s["id"]: 1.0 / len(strategies) for s in strategies}
        compute_portfolio_metrics(strategies, "Equal weight", weights)
        return

    # Full analysis: show multiple allocation schemes
    print("\n" + "=" * 70)
    print("  PORTFOLIO ANALYSIS - Multiple allocation schemes")
    print("=" * 70)

    # 1. Equal weight
    eq_w = {s["id"]: 1.0 / len(strategies) for s in strategies}
    compute_portfolio_metrics(strategies, "EQUAL WEIGHT", eq_w)

    # 2. Risk parity (inverse DD)
    inv_dd_total = sum(1.0 / max(s["max_drawdown_pct"], 0.1) for s in strategies)
    rp_w = {s["id"]: (1.0 / max(s["max_drawdown_pct"], 0.1)) / inv_dd_total for s in strategies}
    compute_portfolio_metrics(strategies, "RISK PARITY (1/DD)", rp_w)

    # 3. Sharpe-weighted
    total_sharpe = sum(abs(s.get("sharpe", 0) or 0) + 0.01 for s in strategies)
    sw_w = {s["id"]: ((abs(s.get("sharpe", 0)) + 0.01) / total_sharpe) for s in strategies}
    compute_portfolio_metrics(strategies, "SHARPE WEIGHTED", sw_w)

    # 4. TPD-weighted (volume focus)
    total_tpy_all = sum(s["trades_per_year"] for s in strategies)
    if total_tpy_all > 0:
        tw_w = {s["id"]: s["trades_per_year"] / total_tpy_all for s in strategies}
        compute_portfolio_metrics(strategies, "VOLUME WEIGHTED (TPY)", tw_w)

    # 5. Optimized (Monte Carlo grid search)
    print("\nOptimizing allocation via Monte Carlo...")
    best_w, _ = find_optimal_allocation(strategies, args.years, 200)
    if best_w:
        mc_w = {strategies[i]["id"]: best_w[i] for i in range(len(strategies))}
        compute_portfolio_metrics(strategies, "MC-OPTIMIZED (max Sharpe)", mc_w)
    print()


if __name__ == "__main__":
    main()
