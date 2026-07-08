"""
Pipeline orchestrator: runs the full strategy discovery pipeline.

Collects (Reddit + RSS) → Extracts (AI) → Backtests → Locks winners.

Usage:
    python orchestrator.py              # run one full cycle
    python orchestrator.py --watch      # keep running on a schedule
    python orchestrator.py --status     # show pipeline status
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

DB_PATH = os.getenv("COLLECTOR_DB_PATH", "strategy_bot.db")

# Locking thresholds - strategies above these get "locked"
LOCK_MIN_TRADES = 5
LOCK_MIN_WIN_RATE = 50.0
LOCK_MIN_SHARPE = 0.1
LOCK_MIN_PROFIT_FACTOR = 1.0


def run_step(name: str, command: list) -> bool:
    print(f"\n{'='*60}")
    print(f"  [{name}]")
    print(f"{'='*60}")
    result = subprocess.run(command, capture_output=False)
    return result.returncode == 0


def lock_top_strategies(conn: sqlite3.Connection):
    """Lock strategies that meet quality thresholds."""
    rows = conn.execute("""
        SELECT b.strategy_id, b.template, b.symbol, b.timeframe,
               b.win_rate, b.sharpe, b.total_return_pct, b.max_drawdown_pct,
               b.n_trades, b.profit_factor, s.strategy_name
        FROM backtest_results b
        JOIN strategies s ON b.strategy_id = s.id
        WHERE b.strategy_id NOT IN (SELECT strategy_id FROM locked_strategies)
          AND b.n_trades >= ?
          AND b.win_rate >= ?
          AND b.sharpe >= ?
          AND b.profit_factor >= ?
        ORDER BY b.sharpe DESC
    """, (LOCK_MIN_TRADES, LOCK_MIN_WIN_RATE, LOCK_MIN_SHARPE, LOCK_MIN_PROFIT_FACTOR)).fetchall()

    if not rows:
        print("  No strategies meet locking thresholds.")
        return

    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        sid, template, symbol, timeframe, wr, sharpe, ret, dd, trades, pf, name = r
        conn.execute("""
            INSERT OR IGNORE INTO locked_strategies
                (strategy_id, best_template, symbol, timeframe,
                 win_rate, sharpe, total_return_pct, max_drawdown_pct,
                 locked_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (sid, template, symbol, timeframe, wr, sharpe, ret, dd, now,
              f"Locked on {now}. WR={wr}% Sharpe={sharpe} PF={pf}"))
        print(f"  LOCKED [{sid}] {name} - WR={wr}% Sharpe={sharpe} Return={ret}%")

    conn.commit()


def show_status(conn: sqlite3.Connection):
    print(f"\n{'='*60}")
    print("  PIPELINE STATUS")
    print(f"{'='*60}")

    counts = {}
    for table, label in [
        ("raw_items", "Raw items total"),
        ("strategies", "Strategies extracted"),
        ("backtest_results", "Backtested"),
        ("locked_strategies", "Locked (performing)"),
    ]:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        counts[label] = row[0]

    for label, count in [
        ("Raw posts collected", counts["Raw items total"]),
        (" - AI extracted as strategies", counts["Strategies extracted"]),
        (" - Backtested", counts["Backtested"]),
        (" - Locked (passing thresholds)", counts["Locked (performing)"]),
    ]:
        print(f"  {label:40s} {count:>5}")

    # Show status breakdown of raw_items
    statuses = conn.execute("SELECT status, COUNT(*) FROM raw_items GROUP BY status ORDER BY status").fetchall()
    print(f"\n  Queue status:")
    for status, count in statuses:
        print(f"    {status:20s} {count}")

    # Show locked strategies
    locked = conn.execute("""
        SELECT s.strategy_name, l.win_rate, l.sharpe, l.total_return_pct, l.locked_at
        FROM locked_strategies l
        JOIN strategies s ON l.strategy_id = s.id
        ORDER BY l.sharpe DESC
    """).fetchall()
    if locked:
        print(f"\n  Locked strategies:")
        for r in locked:
            print(f"    {r[0][:40]:40s} WR={r[1]:5.1f}% Sharpe={r[2]:.2f} Ret={r[3]:+.1f}% Locked: {r[4][:10]}")


def main():
    parser = argparse.ArgumentParser(description="Strategy pipeline orchestrator")
    parser.add_argument("--watch", action="store_true", help="Run continuously")
    parser.add_argument("--status", action="store_true", help="Show pipeline status")
    parser.add_argument("--interval", type=int, default=3600, help="Poll interval in seconds (default: 1h)")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)

    if args.status:
        show_status(conn)
        conn.close()
        return

    print("STRATEGY PIPELINE ORCHESTRATOR")
    print(f"{'='*60}")

    if args.watch:
        print(f"Watch mode: running every {args.interval}s")

    def run_cycle():
        run_step("BLOG COLLECTOR", [sys.executable, "blog_collector.py"])
        run_step("REDDIT COLLECTOR", [sys.executable, "reddit_collector.py"])
        run_step("AI EXTRACTION", [sys.executable, "extract_strategies.py"])
        run_step("BACKTEST ENGINE", [sys.executable, "backtest_engine.py"])

        print(f"\n{'='*60}")
        print("  LOCKING STRATEGIES")
        print(f"{'='*60}")
        lock_top_strategies(conn)
        show_status(conn)

    run_cycle()

    if args.watch:
        print(f"\nNext run in {args.interval}s...")
        try:
            while True:
                time.sleep(args.interval)
                run_cycle()
        except KeyboardInterrupt:
            print("\nStopped.")

    conn.close()


if __name__ == "__main__":
    main()
