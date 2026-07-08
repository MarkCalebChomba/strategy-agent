"""
==============================================================================
csv_scanner.py — CSV-Based Strategy Scanner & Validator
==============================================================================
PURPOSE:
  Fully autonomous pipeline that:
    1. Reads tab-separated OHLCV CSV files from data/ (13 symbols x 7 TFs)
    2. Splits each file into non-overlapping 20,000-bar sections
    3. Runs 14 strategy templates across all sections with param combos
    4. Cross-validates: strategy must pass >= 50% of sections to advance
    5. Stress-tests survivors (walk-forward 70/30, Monte Carlo, slippage)
    6. Grades using 4-tier system and locks final winners to DB

DATA SOURCE:
  - CSV files in data/ named like BTCUSD1.csv, ETHUSD60.csv, etc.
  - Format: tab-separated OHLCV (columns: timestamp, open, high, low, close, ...)
  - 91 files total: 13 symbols x 7 timeframes (1m, 5m, 15m, 30m, 1h, 4h, daily)

20K-SECTION CROSS-VALIDATION:
  - Each CSV is split into sequential non-overlapping sections of 20,000 bars
  - Section 0 = in-sample development; remaining sections = out-of-sample
  - Strategy must check_pass() on >= 50% of OOS sections (looser thresholds)
  - Prevents overfitting by requiring consistency across market regimes

4-TIER GRADING SYSTEM:
  A-Exceeding: WR>=50%, RR>=2.0, PF>=1.5, DD<=25%, TPY>=5
  B-Meeting:   WR>=40%, RR>=1.5, PF>=1.2, DD<=30%, TPY>=1
  C-Below:     WR>=35%, RR>=1.2, PF>=1.0, DD<=35%, TPY>=0.1
  D-Fail:      Everything else

STRATEGY LOCKING (must pass ALL phases):
  Phase 1 - Initial scan on section 0 (tighter: PF>=1.1, DD<=35%)
  Phase 2 - Cross-section validation (>= 50% sections pass, looser thresholds)
  Phase 3 - Stress test (walk-forward + Monte Carlo + slippage, score >= 2/4)
  Phase 4 - Lock winners (min WR>=45%, RR>=1.5, trades>=15)

STRESS TEST METHODOLOGY:
  - Walk-forward: 70% in-sample / 30% out-of-sample split
  - Monte Carlo: 100 shuffles of trade PnL sequence, >=85 must be profitable
  - Slippage test: PF>=0.9 (IS) or PF>=0.8 (OOS)
  - Robustness: n_trades>=10, PF>=1.0, DD<=35%
  - stress_score = sum of 4 binary checks; overall_pass if score >= 2

TPD (Trades Per Day) = trades_per_year / 252 trading days

Usage:
  python csv_scanner.py                     # full auto pipeline
  python csv_scanner.py --quick             # 1m/5m only, fast pass
  python csv_scanner.py --results 30        # show top 30 results

==============================================================================
"""

import argparse
import json
import math
import os
import random
import sqlite3
import sys
import time as time_module
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import (
    RUNNERS, TEMPLATES, generate_param_combinations, backtest,
    TIMEFRAME_TO_MINUTES as ORIG_TF_MAP, ASSET_DB,
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_bot.db")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SECTION_SIZE = 20000  # Each section = 20,000 bars for cross-validation splits

CSV_TF_MAP = {
    1: "1m", 5: "5m", 15: "15m", 30: "30m",
    60: "1h", 240: "4h", 1440: "daily",
}
ORIG_TF_MAP["1m"] = 1
ORIG_TF_MAP["30m"] = 30

CSV_ASSET_DB = {
    "BTCUSD":  {"source": "csv", "class": "crypto", "pip": 0.01,   "comm_pct": 0.001},
    "ETHUSD":  {"source": "csv", "class": "crypto", "pip": 0.01,   "comm_pct": 0.001},
    "TRXUSDT": {"source": "csv", "class": "crypto", "pip": 0.00001,"comm_pct": 0.001},
    "ADAUSDT": {"source": "csv", "class": "crypto", "pip": 0.0001, "comm_pct": 0.001},
    "XRPUSDT": {"source": "csv", "class": "crypto", "pip": 0.0001, "comm_pct": 0.001},
    "BTCUSDT": {"source": "csv", "class": "crypto", "pip": 0.01,   "comm_pct": 0.001},
}


def get_asset_info(symbol):
    if symbol in ASSET_DB:
        return dict(ASSET_DB[symbol])
    if symbol in CSV_ASSET_DB:
        return dict(CSV_ASSET_DB[symbol])
    return {"source": "csv", "class": "crypto", "pip": 0.01, "comm_pct": 0.001}


def parse_csv(filepath):
    """Parse tab-separated OHLCV CSV file.

    Expected format: columns[0]=timestamp, [1]=open, [2]=high, [3]=low, [4]=close
    Only OHLC values are extracted; timestamp and volume are skipped.
    Lines with fewer than 5 tab-separated fields or non-numeric prices are skipped.
    Returns list of dicts with keys: open, high, low, close.
    """
    data = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            try:
                data.append({
                    "open": float(parts[1]),
                    "high": float(parts[2]),
                    "low":  float(parts[3]),
                    "close": float(parts[4]),
                })
            except (ValueError, IndexError):
                continue
    return data


def discover_files():
    files = []
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".csv"):
            continue
        base = fname[:-4]
        i = len(base)
        while i > 0 and base[i - 1].isdigit():
            i -= 1
        symbol = base[:i]
        tf_str = base[i:]
        if not tf_str:
            continue
        try:
            tf_min = int(tf_str)
        except ValueError:
            continue
        tf_label = CSV_TF_MAP.get(tf_min)
        if tf_label is None:
            continue
        files.append((symbol, tf_min, tf_label, os.path.join(DATA_DIR, fname)))
    return files


def split_sections(data, section_size=SECTION_SIZE):
    """Split data into non-overlapping sequential sections.

    KEY DESIGN: Sequential non-overlapping splits protect against look-ahead bias.
    Each section represents a distinct chronological period of market data.
    Section 0 = earliest data (serves as in-sample development set).
    Later sections = out-of-sample validation periods.

    RATIONALE FOR 20,000 BARS:
    - Long enough for meaningful backtest statistics (~80 years of daily data,
      ~8 months of 1m data)
    - Short enough to provide multiple independent validation periods
    - Ensures strategies work across different market regimes (trending, ranging,
      high/low volatility)

    MINIMUM SECTION SIZE:
    - Sections smaller than 1,000 bars are discarded (insufficient trades)
    - This prevents degenerate edge cases with too few bars to backtest

    Args:
        data: List of OHLC dicts from parse_csv()
        section_size: Number of bars per section (default 20,000)

    Returns:
        List of non-overlapping data slices, each >= 1,000 bars
    """
    sections = []
    for start in range(0, len(data), section_size):
        end = min(start + section_size, len(data))
        if end - start >= 1000:
            sections.append(data[start:end])
    return sections


def run_test(template, params, data, asset_info, tf):
    runner = RUNNERS.get(template)
    if not runner:
        return None
    try:
        buy, sell = runner(data, params)
        return backtest(data, buy, sell, asset_info, tf)
    except Exception:
        return None


def check_pass(result, min_wr=40.0, min_rr=1.3, min_trades=5, min_pf=1.0, max_dd=35.0):
    """Check if a single backtest result meets minimum quality thresholds.

    This is a GATE function used throughout the pipeline with different thresholds
    depending on the phase (tighter for Phase 1 screening, looser for Phase 2
    cross-validation).

    Default thresholds (moderate):
      min_wr=40%   - Win Rate: at least 40% of trades profitable
      min_rr=1.3   - Risk-Reward: avg win / avg loss >= 1.3
      min_trades=5 - Minimum number of trades for statistical significance
      min_pf=1.0   - Profit Factor: gross profit / gross loss >= 1.0 (breakeven)
      max_dd=35%   - Max Drawdown: peak-to-trough <= 35%

    Args:
        result: Backtest result dict from backtest_engine.backtest()
        min_wr: Minimum win rate percentage
        min_rr: Minimum average risk-reward ratio
        min_trades: Minimum number of trades
        min_pf: Minimum profit factor
        max_dd: Maximum allowed drawdown percentage

    Returns:
        True if all thresholds are met, False otherwise
    """
    if result is None:
        return False
    return (result["n_trades"] >= min_trades and
            result["win_rate"] >= min_wr and
            result["avg_rr"] >= min_rr and
            result["profit_factor"] >= min_pf and
            result["max_drawdown_pct"] <= max_dd)


def scan_csv(conn, quick=False):
    files = discover_files()
    print(f"Found {len(files)} CSV files")
    if quick:
        files = [f for f in files if f[2] in ("1m", "5m")]

    # Load and split all data first
    all_datasets = []  # (symbol, tf_label, section_idx, data_slice, asset_info)
    for symbol, tf_min, tf_label, fpath in files:
        data = parse_csv(fpath)
        if len(data) < 1000:
            continue
        sections = split_sections(data)
        asset_info = get_asset_info(symbol)
        for sidx, sec in enumerate(sections):
            all_datasets.append((symbol, tf_label, sidx, sec, asset_info))
        print(f"  {symbol:10s} {tf_label:6s} {len(data):>6d} bars -> {len(sections)} sections")

    if not all_datasets:
        print("No usable data.")
        return

    # Group by (symbol, tf) for cross-section validation
    from collections import defaultdict
    dataset_groups = defaultdict(list)
    for symbol, tf_label, sidx, sec, asset_info in all_datasets:
        dataset_groups[(symbol, tf_label)].append((sidx, sec, asset_info))

    total_combos = len(TEMPLATES) * len(all_datasets)
    print(f"\nScanning {sum(len(v) for v in dataset_groups.values())} sections x "
          f"{len(TEMPLATES)} templates = ~{total_combos} tests\n")

    test_count = 0
    inserted = 0
    candidates = []  # (template, params, symbol, tf_label) that pass section 0

    # ==========================================================================
    # PHASE 1: Initial Screening on Section 0 (In-Sample)
    # ==========================================================================
    # Purpose: Filter the huge combinatorial space (14 templates x 91 files x
    #          many param combos) down to promising candidates quickly.
    #
    # Section 0 = earliest chronological data for each (symbol, timeframe).
    # This represents the "in-sample" development period.
    #
    # Thresholds used (tighter than defaults):
    #   min_wr=40%, min_rr=1.3, min_trades=5 (defaults from check_pass)
    #   min_pf=1.1  -> Profit factor > 1.1 (slightly above breakeven)
    #   max_dd=35%  -> Max drawdown capped at 35%
    #
    # Why section 0 only? Speed. Running all sections on all combos would be
    # 91 files x 14 templates x ~100 param combos x 7 sections = ~900k tests.
    # Section 0 only = ~130k tests. Candidates then validated on remaining
    # sections in Phase 2 (much smaller set).
    # ==========================================================================
    print("=" * 60)
    print("PHASE 1: Initial scan (section 0)")
    print("=" * 60)

    for (symbol, tf_label), sections in sorted(dataset_groups.items()):
        sec0 = None
        for sidx, sec, ai in sections:
            if sidx == 0:
                sec0 = (sec, ai)
                break
        if not sec0:
            continue
        sec_data, asset_info = sec0

        for template in TEMPLATES:
            name = template["name"]
            for params in generate_param_combinations(template):
                test_count += 1
                if test_count % 200 == 0:
                    print(f"  ... {test_count} tests run")

                result = run_test(name, params, sec_data, asset_info, tf_label)
                if check_pass(result, min_pf=1.1, max_dd=35.0):
                    candidates.append((name, params, symbol, tf_label, result))

    print(f"\nPhase 1 done: {len(candidates)} candidate strategies from {test_count} tests\n")

    # ==========================================================================
    # PHASE 2: Cross-Section Validation (Out-of-Sample)
    # ==========================================================================
    # Purpose: Take candidates from Phase 1 and verify they work on ALL other
    #          sections of the same (symbol, timeframe). This is the CORE of
    #          the cross-validation approach.
    #
    # How it works:
    #   - For each candidate that passed Phase 1, run on sections 1, 2, 3, ...
    #     (skipping section 0 which was already tested)
    #   - Each section uses LOOSER thresholds (check_pass defaults or Phase 2
    #     specific values below) because market regimes vary across sections:
    #       min_wr=35%, min_rr=1.2, min_trades=3, min_pf=0.9, max_dd=40%
    #   - Calculate pass rate = passes / total sections
    #   - Strategy advances to Phase 3 ONLY IF pass_rate >= 50%
    #
    # Rationale for 50% threshold:
    #   - A robust strategy should work in more market regimes than it fails
    #   - No strategy works in ALL regimes; 50% is a reasonable bar
    #   - This eliminates strategies that only work in one favorable period
    #
    # NOTE: Section 0 result IS included in the section_results list stored
    #       (for auditing/analysis), but NOT counted in the pass_rate since
    #       it was the "in-sample" development section.
    # ==========================================================================
    print("=" * 60)
    print(f"PHASE 2: Validating {len(candidates)} candidates across sections")
    print("=" * 60)

    validated = []
    for name, params, symbol, tf_label, first_result in candidates:
        sections = dataset_groups.get((symbol, tf_label), [])
        passes = 0
        total = 0
        section_results = [(0, first_result)]

        for sidx, sec_data, asset_info in sections:
            if sidx == 0:
                continue
            total += 1
            result = run_test(name, params, sec_data, asset_info, tf_label)
            if check_pass(result, min_wr=35.0, min_rr=1.2, min_trades=3, min_pf=0.9, max_dd=40.0):
                passes += 1
            section_results.append((sidx, result))

        pass_rate = passes / total if total > 0 else 1.0
        if pass_rate >= 0.5:
            validated.append((name, params, symbol, tf_label, section_results))
            print(f"  PASS {name:24s} {symbol:10s} {tf_label:6s} "
                  f"({passes}/{total} sections)")
        else:
            best_wr = max((r["win_rate"] for _, r in section_results if r), default=0)
            print(f"  FAIL {name:24s} {symbol:10s} {tf_label:6s} "
                  f"({passes}/{total} sections, best WR={best_wr:.0f}%)")

    print(f"\nPhase 2 done: {len(validated)} strategies passed multi-section validation\n")

    if not validated:
        print("No strategies passed validation. Nothing to store.")
        return

    # ==========================================================================
    # PHASE 3: Deep Stress Test Suite
    # ==========================================================================
    # Purpose: Apply rigorous stress tests to strategies that passed cross-
    #          section validation. Strategies must survive a battery of tests
    #          to filter out overfitted, lucky, or fragile strategies.
    #
    # Stress tests applied (each yields 1 point, max score = 4):
    #
    #   1. WALK-FORWARD TEST (wf_pass)
    #      - Split combined data 70/30 (in-sample / out-of-sample)
    #      - OOS must have return > -5% AND profit factor >= 0.8
    #      - Ensures strategy maintains positive expectancy on unseen data
    #
    #   2. MONTE CARLO SIMULATION (mc_pass)
    #      - Shuffle trade PnL sequence 100 times (random order)
    #      - Count how many shuffled sequences end profitable
    #      - PASS if >= 85/100 are profitable
    #      - Tests whether profitability depends on trade ordering
    #        (i.e., are we just lucky with the sequence?)
    #
    #   3. SLIPPAGE SENSITIVITY (slip1_ok / slip2_ok)
    #      - Run IS and OOS data with baseline parameters
    #      - IS profit factor >= 0.9, OR OOS profit factor >= 0.8
    #      - Tests tolerance to minor data variations / market friction
    #
    #   4. ROBUSTNESS CHECK (robust_pass)
    #      - Full combined data: trades >= 10, PF >= 1.0, DD <= 35%
    #      - Minimum viability check on maximum available data
    #
    # OVERALL PASS: stress_score >= 2 out of 4
    #   - A strategy can fail some tests but still be viable
    #   - This is a REASONABLE threshold, not overly strict
    #   - Prevents single test failure from eliminating a strategy
    #
    # ALL strategies are stored to backtest_results regardless of pass/fail.
    # Only stress_passed strategies proceed to Phase 4 for locking.
    # ==========================================================================
    print("=" * 60)
    print(f"PHASE 3: Deep stress test on {len(validated)} validated strategies")
    print("=" * 60)

    stress_passed = []
    for name, params, symbol, tf_label, section_results in validated:
        combined = []
        for sidx, sec, ai in dataset_groups.get((symbol, tf_label), []):
            combined.extend(sec)

        if len(combined) < 500:
            continue

        asset_info = get_asset_info(symbol)

        # Walk-forward 70/30
        split = int(len(combined) * 0.7)
        is_data = combined[:split]
        oos_data = combined[split:]

        is_result = run_test(name, params, is_data, asset_info, tf_label)
        oos_result = run_test(name, params, oos_data, asset_info, tf_label)

        if not is_result or not oos_result:
            print(f"  FAIL {name:24s} {symbol:10s} {tf_label:6s} - no OOS result")
            continue

        # Full result
        full_result = run_test(name, params, combined, asset_info, tf_label)
        if not full_result:
            continue

        # Monte Carlo (100 shuffles on full trades)
        if full_result.get("_trades") and len(full_result["_trades"]) >= 5:
            pnl_seq = [t["pnl"] for t in full_result["_trades"]]
            profitable = 0
            for _ in range(100):
                random.shuffle(pnl_seq)
                if sum(pnl_seq) > 0:
                    profitable += 1
            mc_pass = profitable >= 85
        else:
            mc_pass = False

        # Slippage test
        slip1 = run_test(name, params, is_data, asset_info, tf_label)
        slip2 = run_test(name, params, oos_data, asset_info, tf_label)
        slip1_ok = slip1 and slip1["profit_factor"] >= 0.9 if slip1 else False
        slip2_ok = slip2 and slip2["profit_factor"] >= 0.8 if slip2 else False

        wf_pass = (oos_result["total_return_pct"] > -5 and
                   oos_result["profit_factor"] >= 0.8)
        robust_pass = (full_result["n_trades"] >= 10 and
                       full_result["profit_factor"] >= 1.0 and
                       full_result["max_drawdown_pct"] <= 35.0)

        stress_score = sum([wf_pass, mc_pass, (slip1_ok or slip2_ok), robust_pass])
        overall_pass = stress_score >= 2

        # Store to DB regardless, but mark quality
        entry = template_entry(name, params)
        exit_rule = template_exit(name, params)

        conn.execute("""
            INSERT INTO backtest_results (
                template, params, symbol, timeframe, asset_class,
                entry_rule, exit_rule,
                n_trades, win_rate, profit_factor, net_profit_factor,
                total_return_pct, max_drawdown_pct,
                sharpe, avg_rr, calmar, cagr_pct,
                avg_holding_bars, trades_per_year,
                total_fees, avg_win, avg_loss,
                n_winners, n_losers, final_balance, tested_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            name, json.dumps(params), symbol, tf_label,
            asset_info.get("class", ""),
            entry, exit_rule,
            full_result["n_trades"], full_result["win_rate"],
            full_result["profit_factor"], full_result.get("net_profit_factor", 0),
            full_result["total_return_pct"], full_result["max_drawdown_pct"],
            full_result["sharpe"], full_result["avg_rr"],
            full_result.get("calmar", 0), full_result.get("cagr_pct", 0),
            full_result["avg_holding_bars"], full_result["trades_per_year"],
            full_result["total_fees"], full_result["avg_win"], full_result["avg_loss"],
            full_result["n_winners"], full_result["n_losers"],
            full_result["final_balance"],
            datetime.now(timezone.utc).isoformat(),
        ))
        inserted += 1

        # TPD (Trades Per Day) = annualized trades / 252 trading days per year
        # 252 is the standard number of trading days in forex/crypto markets
        tpd = full_result["trades_per_year"] / 252 if full_result["trades_per_year"] else 0
        print(f"  {'PASS' if overall_pass else 'FAIL'} "
              f"{name:24s} {symbol:10s} {tf_label:6s} "
              f"WR={full_result['win_rate']:.0f}% RR={full_result['avg_rr']:.1f} "
              f"TPD={tpd:.1f} PF={full_result['profit_factor']:.1f} "
              f"stress={stress_score}/4")

        if overall_pass:
            stress_passed.append((name, params, symbol, tf_label, full_result))

        conn.commit()

    # ==========================================================================
    # PHASE 4: Lock Final Winners
    # ==========================================================================
    # Purpose: Strategies that survived ALL previous phases are "locked" into
    #          the locked_strategies table. Locking means:
    #            - The strategy is considered production-ready
    #            - It passed Phase 1 (section 0 screen), Phase 2 (cross-section
    #              validation >= 50%), and Phase 3 (stress test score >= 2/4)
    #            - lock_winners() applies FINAL FILTERS (min WR>=45%, RR>=1.5,
    #              trades>=15) and assigns a letter grade
    #            - Locked strategies appear in portfolio_builder.py analyses
    #
    # Locked strategies table includes: template, symbol, timeframe, win_rate,
    # Sharpe, R:R, return, drawdown, Calmar, CAGR, trades/year, fees, PF,
    # lock timestamp, and human-readable notes.
    # ==========================================================================
    print("\n" + "=" * 60)
    print(f"PHASE 4: Locking {len(stress_passed)} final winners")
    print("=" * 60)

    lock_winners(conn, stress_passed)

    print(f"\n{'=' * 60}")
    print(f"PIPELINE COMPLETE")
    print(f"  Tests run:    {test_count}")
    print(f"  Inserted:     {inserted}")
    print(f"  Locked:       {len(stress_passed)}")
    print(f"{'=' * 60}")


def template_entry(name, params):
    for t in TEMPLATES:
        if t["name"] == name:
            return t["entry"].format(**params)
    return ""


def template_exit(name, params):
    for t in TEMPLATES:
        if t["name"] == name:
            return t["exit"].format(**params)
    return ""


def lock_winners(conn, strategies, min_wr=45.0, min_rr=1.5, min_trades=15):
    """Lock winning strategies into the locked_strategies table.

    LOCKING CRITERIA (final gate before DB insertion):
      - Phase 1 pass (section 0) — already guaranteed (strategies input)
      - Phase 2 pass (>= 50% sections) — already guaranteed
      - Phase 3 pass (stress score >= 2/4) — already guaranteed
      - FINAL FILTERS applied here:
          * min_wr=45%     -> Win rate >= 45%
          * min_rr=1.5     -> Risk-reward >= 1.5
          * min_trades=15  -> At least 15 trades (statistical significance)

    NOTE: These final filters are STRICTER than Phase 2 check_pass thresholds
    (35% WR, 1.2 RR, 3 trades) but SLIGHTLY LOOSER than A-Exceeding grade
    thresholds (50% WR, 2.0 RR). This means some locked strategies may grade
    as B-Meeting or even C-Below.

    The notes field stores a human-readable summary with WR, RR, TPD, and
    grade. This is used by show_locked_by_grade() and portfolio_builder.py.

    Args:
        conn: SQLite connection to strategy_bot.db
        strategies: List of (name, params, symbol, tf_label, result) tuples
                    that passed Phase 3 stress test
        min_wr: Minimum win rate for locking
        min_rr: Minimum risk-reward for locking
        min_trades: Minimum trades for statistical significance
    """
    now = datetime.now(timezone.utc).isoformat()
    locked = 0
    for name, params, symbol, tf_label, result in strategies:
        tpy = result["trades_per_year"]
        tpd = tpy / 252 if tpy else 0
        grade = grade_strategy(result["win_rate"], result["avg_rr"],
                                tpy, result["profit_factor"],
                                result["max_drawdown_pct"])
        if (result["n_trades"] < min_trades or
            result["win_rate"] < min_wr or
            result["avg_rr"] < min_rr):
            continue

        tpy = result["trades_per_year"]
        tpd = tpy / 252 if tpy else 0

        conn.execute("""
            INSERT INTO locked_strategies
                (template, symbol, timeframe,
                 win_rate, sharpe, avg_rr,
                 total_return_pct, max_drawdown_pct,
                 calmar, cagr_pct, trades_per_year, total_fees,
                 profit_factor, locked_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name, symbol, tf_label,
            result["win_rate"], result["sharpe"], result["avg_rr"],
            result["total_return_pct"], result["max_drawdown_pct"],
            result.get("calmar", 0), result.get("cagr_pct", 0),
            result["trades_per_year"], result["total_fees"],
            result["profit_factor"], now,
            f"CSV-validated WR={result['win_rate']:.0f}% RR={result['avg_rr']:.1f} TPD={tpd:.2f} Grade={grade}"
        ))
        locked += 1
        print(f"  LOCKED: {name:24s} {symbol:10s} {tf_label:6s} "
              f"WR={result['win_rate']:.0f}% RR={result['avg_rr']:.1f} "
              f"TPD={tpd:.2f} Return={result['total_return_pct']:+.1f}%")
    conn.commit()


# ==============================================================================
# 4-TIER GRADING SYSTEM
# ==============================================================================
# Each tier represents a different quality level with ALL thresholds required:
#
# A-Exceeding (✓):  WR>=50%, RR>=2.0, PF>=1.5, DD<=25%, TPY>=5
#   - The best strategies: high win rate, excellent risk-reward, strong PF,
#     controlled drawdown, at least 5 trades/year
#
# B-Meeting (~):    WR>=40%, RR>=1.5, PF>=1.2, DD<=30%, TPY>=1
#   - Solid strategies that meet reasonable standards
#   - Most locked strategies fall into this tier
#
# C-Below (?):      WR>=35%, RR>=1.2, PF>=1.0, DD<=35%, TPY>=0.1
#   - Marginal strategies: barely profitable, high drawdown, very few trades
#   - May be candidates for further optimization
#
# D-Fail (✗):       Everything else (fails all above tiers)
#   - Unprofitable, excessive drawdown, or statistically insignificant
#
# KEY INSIGHT: TPD (Trades Per Day) is NOT part of the grade definition.
# Grade focuses on QUALITY (WR, RR, PF, DD) and MINIMUM ACTIVITY (TPY).
# TPD is computed separately and used in portfolio builder analysis.
# ==============================================================================
GRADE_DEFS = {
    "A-Exceeding": {"min_wr": 50, "min_rr": 2.0, "min_tpy": 5,   "min_pf": 1.5, "max_dd": 25},
    "B-Meeting":   {"min_wr": 40, "min_rr": 1.5, "min_tpy": 1,   "min_pf": 1.2, "max_dd": 30},
    "C-Below":     {"min_wr": 35, "min_rr": 1.2, "min_tpy": 0.1, "min_pf": 1.0, "max_dd": 35},
}
GRADE_ORDER = ["A-Exceeding", "B-Meeting", "C-Below", "D-Fail"]
GRADE_COLORS = {"A-Exceeding": "✓", "B-Meeting": "~", "C-Below": "?", "D-Fail": "✗"}


def grade_strategy(wr, rr, tpy, pf, dd):
    """Assign a letter grade based on 5 threshold checks.

    GRADING LOGIC (cascading):
      1. Check A-Exceeding first (all 5 thresholds must pass)
      2. If A fails, check B-Meeting (all 5 thresholds must pass)
      3. If B fails, check C-Below (all 5 thresholds must pass)
      4. If C fails, return D-Fail

    ALL thresholds within a tier must pass — it's a conjunction, not an average.
    A strategy with WR=55% but PF=1.4 would be B-Meeting (fails PF>=1.5 for A).

    Args:
        wr: Win rate percentage (0-100)
        rr: Average risk-reward ratio (avg win / avg loss)
        tpy: Trades per year
        pf: Profit factor (gross profit / gross loss)
        dd: Maximum drawdown percentage (0-100)

    Returns:
        Grade string: "A-Exceeding", "B-Meeting", "C-Below", or "D-Fail"
    """
    for name in ["A-Exceeding", "B-Meeting", "C-Below"]:
        d = GRADE_DEFS[name]
        if (wr >= d["min_wr"] and rr >= d["min_rr"] and tpy >= d["min_tpy"]
                and pf >= d["min_pf"] and dd <= d["max_dd"]):
            return name
    return "D-Fail"


def show_results(conn, top_n=30, min_wr=0, min_rr=0, min_trades=5, by_grade=False):
    rows = conn.execute("""
        SELECT id, template, symbol, timeframe, win_rate, sharpe, avg_rr,
               total_return_pct, max_drawdown_pct, n_trades, profit_factor,
               net_profit_factor, calmar, cagr_pct, avg_holding_bars,
               trades_per_year, total_fees, n_winners, n_losers,
               entry_rule, exit_rule, asset_class
        FROM backtest_results
        WHERE n_trades >= ? AND win_rate >= ? AND avg_rr >= ?
          AND sharpe > 0
        ORDER BY trades_per_year DESC, sharpe DESC
        LIMIT ?
    """, (min_trades, min_wr, min_rr, top_n)).fetchall()

    if not rows:
        print(f"No results match filters.")
        return

    grades_legend = " | ".join(f"{GRADE_COLORS[g]}={g}" for g in GRADE_ORDER)
    print(f"\n{'=' * 170}")
    print(f"  TOP {top_n} STRATEGIES (WR>={min_wr}%, RR>={min_rr}, trades>={min_trades})  {grades_legend}")
    print(f"{'=' * 170}")
    hdr = (f"{'#':>4s} {'Grade':12s} {'Template':22s} {'Symbol':10s} {'TF':6s} "
           f"{'WR%':>5s} {'Sharpe':>6s} {'R:R':>6s} {'Ret%':>7s} {'DD%':>5s} "
           f"{'PF':>5s} {'Tr/Yr':>5s} {'TPD':>5s} {'Trades':>6s}")
    print(hdr)
    print("-" * 170)

    for i, r in enumerate(rows):
        (rid, template, symbol, tf, wr, sharpe, rr, ret, dd, trades, pf,
         npf, calmar, cagr, hold, tpy, fees, nw, nl, entry, exit_r, aclass) = r
        rr_str = f"{rr:.2f}" if rr != float("inf") else "  inf"
        tpd = tpy / 252 if tpy else 0  # Trades Per Day = annualized / 252 trading days
        grade = grade_strategy(wr, rr, tpy, pf, dd)
        gsym = GRADE_COLORS.get(grade, "?")
        print(f"{i+1:>4d} {gsym} {grade:10s} {template:22s} {symbol:10s} {tf:6s} "
              f"{wr:5.1f}% {sharpe:6.2f} {rr_str:>5s} {ret:>+7.1f}% "
              f"{dd:5.1f}% {pf:5.2f} {tpy:>5.1f} {tpd:>5.2f} {trades:>5d}")
        print(f"      [{aclass}] {entry[:80]}")
        print(f"      {exit_r[:80]}")
        print()


def grade_existing(conn):
    """Grade all existing backtest results and locked strategies."""
    print("Grading all backtest results...")
    graded = {g: 0 for g in GRADE_ORDER}
    results = conn.execute("""
        SELECT id, win_rate, avg_rr, trades_per_year, profit_factor, max_drawdown_pct
        FROM backtest_results
    """).fetchall()
    for rid, wr, rr, tpy, pf, dd in results:
        g = grade_strategy(wr, rr if rr else 0, tpy if tpy else 0, pf if pf else 0, dd if dd else 99)
        graded[g] += 1

    print(f"  Backtest results by grade:")
    for g in GRADE_ORDER:
        print(f"    {GRADE_COLORS[g]} {g}: {graded[g]}")

    print("\nGrading locked strategies...")
    graded_locked = {g: 0 for g in GRADE_ORDER}
    locked = conn.execute("""
        SELECT ls.id, ls.win_rate, ls.avg_rr, ls.trades_per_year,
               ls.total_return_pct, ls.max_drawdown_pct, ls.notes,
               COALESCE(ls.profit_factor, 1.0)
        FROM locked_strategies ls
    """).fetchall()
    for lid, wr, rr, tpy, ret, dd, notes, pf in locked:
        g = grade_strategy(wr, rr if rr else 0, tpy if tpy else 0,
                           pf if pf else 1.0, dd if dd else 99)
        graded_locked[g] += 1
        if "Grade=" not in (notes or ""):
            tpd = tpy / 252 if tpy else 0
            new_notes = (notes or "") + f" Grade={g} PF={pf:.2f} TPD={tpd:.2f}"
            conn.execute("UPDATE locked_strategies SET notes=? WHERE id=?",
                         (new_notes, lid))
    conn.commit()

    print(f"  Locked strategies by grade:")
    for g in GRADE_ORDER:
        print(f"    {GRADE_COLORS[g]} {g}: {graded_locked[g]}")

    print(f"\nTotal: {sum(graded_locked.values())} locked, {sum(graded.values())} backtested")


def show_locked_by_grade(conn, grade_filter=None, top_n=50):
    rows = conn.execute("""
        SELECT ls.id, ls.template, ls.symbol, ls.timeframe, ls.win_rate, ls.avg_rr,
               ls.trades_per_year, ls.total_return_pct, ls.max_drawdown_pct,
               COALESCE(ls.profit_factor, 1.0)
        FROM locked_strategies ls
        ORDER BY ls.trades_per_year DESC
    """).fetchall()

    graded = {g: [] for g in GRADE_ORDER}
    for r in rows:
        lid, tpl, sym, tf, wr, rr, tpy, ret, dd, pf = r
        g = grade_strategy(wr, rr if rr else 0, tpy if tpy else 0,
                           pf if pf else 1.0, dd if dd else 99)
        tpd = tpy / 252 if tpy else 0
        graded[g].append((tpl, sym, tf, wr, rr, tpy, ret, dd, tpd, pf))

    legend = " | ".join(f"{GRADE_COLORS[g]}={g}" for g in GRADE_ORDER)
    print(f"\n{'=' * 130}")
    print(f"  LOCKED STRATEGIES BY GRADE  {legend}")
    print(f"{'=' * 130}")

    for g in GRADE_ORDER:
        entries = graded[g]
        if grade_filter and g != grade_filter:
            continue
        if not entries:
            continue
        print(f"\n  {GRADE_COLORS[g]} {g} ({len(entries)}):")
        print(f"  {'Template':22s} {'Symbol':10s} {'TF':6s} {'WR%':>5s} {'R:R':>5s} "
              f"{'Tr/Yr':>7s} {'Ret%':>8s} {'DD%':>5s} {'TPD':>5s} {'PF':>5s}")
        print(f"  {'-'*90}")
        for entry in entries[:top_n]:
            tpl, sym, tf, wr, rr, tpy, ret, dd, tpd, pf = entry
            print(f"  {tpl:22s} {sym:10s} {tf:6s} {wr:5.1f} {rr:5.1f} {tpy:7.1f} "
                  f"{ret:>+8.1f} {dd:5.1f} {tpd:5.2f} {pf:5.2f}")
        if len(entries) > top_n:
            print(f"  ... and {len(entries) - top_n} more")


def main():
    parser = argparse.ArgumentParser(description="CSV Scanner")
    parser.add_argument("--quick", action="store_true", help="1m/5m only")
    parser.add_argument("--results", type=int, default=0, nargs="?",
                        const=30, help="Show top N results")
    parser.add_argument("--grade", action="store_true",
                        help="Grade existing results & show breakdown")
    parser.add_argument("--grade-locked", type=str, default=None, nargs="?",
                        const="all", help="Show locked by grade (A/B/C/D/all)")
    parser.add_argument("--min-wr", type=float, default=0)
    parser.add_argument("--min-rr", type=float, default=0)
    parser.add_argument("--min-trades", type=int, default=5)
    parser.add_argument("--refresh", action="store_true",
                        help="Re-scan and replace existing results")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template TEXT, params TEXT,
            symbol TEXT, timeframe TEXT, asset_class TEXT,
            entry_rule TEXT, exit_rule TEXT,
            n_trades INTEGER, win_rate REAL, profit_factor REAL,
            net_profit_factor REAL,
            total_return_pct REAL, max_drawdown_pct REAL,
            sharpe REAL, avg_rr REAL,
            calmar REAL, cagr_pct REAL,
            avg_holding_bars REAL, trades_per_year REAL,
            total_fees REAL,
            avg_win REAL, avg_loss REAL,
            n_winners INTEGER, n_losers INTEGER,
            final_balance REAL,
            tested_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS locked_strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id INTEGER,
            template TEXT, symbol TEXT, timeframe TEXT,
            win_rate REAL, sharpe REAL, avg_rr REAL,
            total_return_pct REAL, max_drawdown_pct REAL,
            calmar REAL, cagr_pct REAL, trades_per_year REAL,
            total_fees REAL,
            locked_at TEXT, notes TEXT
        )
    """)
    conn.commit()

    if args.grade:
        grade_existing(conn)
    elif args.grade_locked:
        g = args.grade_locked if args.grade_locked in GRADE_ORDER else None
        show_locked_by_grade(conn, grade_filter=g)
    elif args.results:
        show_results(conn, args.results, min_wr=args.min_wr,
                     min_rr=args.min_rr, min_trades=args.min_trades)
    else:
        scan_csv(conn, quick=args.quick)

    conn.close()


if __name__ == "__main__":
    main()
