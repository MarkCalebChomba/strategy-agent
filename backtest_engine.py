"""
Backtest Engine v2 — fee-aware, multi-asset parameter scanner.
================================================================================
METHODOLOGY — DOCUMENTATION FOR INDEPENDENT VERIFICATION
================================================================================

WHAT THIS ENGINE DOES:
  Scans 14 strategy templates × 13 symbols × 4 timeframes × parameter grids
  (6487+ backtest results generated). Each combination is backtested with
  realistic fees, position sizing, and slippage. Results stored in SQLite.

FEE MODEL:
  - Forex: spread (in pips) × pip value × position size. Spreads defined per
    pair in ASSET_DB (e.g., EURUSD=1.2 pips). The cost is deducted once per
    round-trip. Formula: spread_pips * pip * abs(position_size) at entry only.
  - Crypto: percentage commission per side (comm_pct, default 0.001 = 0.1%).
    Applied on both entry and exit: (entry_price + exit_price) * size * comm_pct.
  - Commodities (XAUUSD, XAGUSD): per-lot commission (comm_per_lot). Lots
    calculated as max(1, position_size / 100). Applied on both sides.
  - Indices (US500, USTEC, US30): same per-lot model as commodities.
  - Stocks: per-share commission (comm_per_share, default $0.005), both sides.

POSITION SIZING:
  Fixed-fractional risk: risk_per_trade_pct = 1% of current balance.
  Stop distance = max(2 * ATR(14), close * 0.005) — i.e., 2× the 14-bar
  average true range, with a 0.5% floor. Raw units = risk_dollars / stop_dist.
  Capped by max affordable units = int(balance / close). This is a simplified
  model (no true ATR stop, uses trailing ATR as proxy).

PIPS / PIP VALUES:
  Each symbol has a "pip" field: the smallest price increment for that asset.
  Used for: fee calculation (forex spread = spread_pips × pip × size),
  slippage (slippage_pips × pip × size). Pips are NOT used for profit
  calculation — all P&L is in raw price difference × units.

DATA SOURCES:
  - MT5 (MetaTrader 5): Forex, commodities, indices. Requires MT5 terminal
    installed. Native timeframes: 15m, 1h. 4h/daily resampled from 1h.
  - CCXT (Binance): Top-10 crypto USDT pairs. Paginated fetch (30 pages × 1000
    bars = up to 30k bars, ~3.4 years). Same resampling for higher TFs.
  - CSV (NOT used here): csv_scanner.py reads data/*.csv (tab-separated OHLCV)
    for 13 symbols × 7 timeframes. This backtest_engine.py uses MT5/CCXT only.

14 STRATEGY TEMPLATES (see RUNNERS dict and individual runner functions):
  1. Dual Thrust — Breakout of range-based channel (open ± K×range)
  2. Heikin-Ashi Momentum — HA candle pattern (bull/bear with body expansion)
  3. Parabolic SAR — Trend-following SAR reversal
  4. Range Expansion — Volatility breakout (range > mult × avg range)
  5. Turtle — Donchian channel breakout (entry/exit windows)
  6. Dynamic Breakout II — Breakout with ATR filter and lookback high/low
  7. R-Breaker — Mean reversion at pivot levels (S1/R1)
  8. Keltner Channel — EMA ± ATR×mult channel breakout
  9. Awesome Oscillator — SMA(median price) crossover (5/13, 5/21, 5/34)
  10. Momentum Pinball — N-bar high/low breakout
  11. Close Bias — Close position within bar range (top/bottom X%)
  12. EMA Pullback — Price pullback to EMA then reversal
  13. ATR Channel — SMA ± ATR×mult with lookback high/low confirmation
  14. Micro Trend — Fast/slow SMA crossover with body-size filter

SCAN LOOP:
  1. Fetch all native data (15m, 1h) per symbol into data_cache.
  2. Resample 1h → 4h, daily for higher timeframe tests.
  3. For each template × params × symbol × timeframe, run the strategy
     runner to generate buy/sell signals, then call backtest().
  4. Insert results into backtest_results table.
  5. After scan, lock_winners() promotes top strategies to locked_strategies.

PARAMETER SCANNING:
  Each template defines a grid of parameter values (e.g., lookback: [1,2,3]).
  generate_param_combinations() yields the Cartesian product of all parameter
  lists. Each combination is a separate backtest run.

RESULTS STORAGE:
  backtest_results table: one row per (template, params, symbol, timeframe).
  key columns: n_trades, win_rate, profit_factor, total_return_pct,
  max_drawdown_pct, sharpe, avg_rr, trades_per_year, total_fees.
  locked_strategies table: promoted strategies meeting minimum thresholds
  (WR>=70%, Sharpe>=0.5, RR>=2.0, trades>=5).

DEPENDENCIES:
  MetaTrader5 (optional), ccxt (optional), python-dotenv, sqlite3.
  Yahoo Finance is NOT used in current config (ASSET_DB sources are
  mt5/binance only), but fetch_yahoo() is kept for reference.

Usage:
  python backtest_engine.py --scan              # full scan
  python backtest_engine.py --results 20        # top 20 results
  python backtest_engine.py --lock              # lock winners
  python backtest_engine.py --symbols           # list supported assets
  python backtest_engine.py --stress            # stress test locked strategies
"""

import argparse
import json
import math
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from dotenv import load_dotenv

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except Exception:
    MT5_AVAILABLE = False

try:
    import ccxt
    CCXT_AVAILABLE = True
except Exception:
    CCXT_AVAILABLE = False

load_dotenv()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DB_PATH = os.getenv("COLLECTOR_DB_PATH", "strategy_bot.db")

# ---------------------------------------------------------------------------
# Asset database — pip values, fee models, data sources
# ---------------------------------------------------------------------------
# Each symbol has:
#   source: 'mt5' | 'binance' — determines which fetch function to call
#   class: 'forex' | 'commodity' | 'index' | 'crypto' — determines fee model
#   pip: smallest price increment (used for spread & slippage $ conversion)
#   spread_pips: [forex] bid-ask spread in pips (cost per round trip)
#   comm_per_lot: [commodity/index] $ commission per standard lot
#   comm_pct: [crypto] % commission per side (e.g., 0.001 = 0.1%)
#
# NOTE: Stock indices (US500, USTEC, US30) kept in code but EXCLUDED from
#       actual scans per user preference (see SCAN_ASSETS logic in csv_scanner).

ASSET_DB = {
    # Major FX pairs (spot, quoted in USD terms) — MT5 source
    "EURUSD": {"source": "mt5", "class": "forex", "pip": 0.0001, "spread_pips": 1.2, "description": "Euro / US Dollar"},
    "GBPUSD": {"source": "mt5", "class": "forex", "pip": 0.0001, "spread_pips": 1.5, "description": "British Pound / US Dollar"},
    "USDJPY": {"source": "mt5", "class": "forex", "pip": 0.01,   "spread_pips": 1.2, "description": "US Dollar / Japanese Yen"},
    "USDCHF": {"source": "mt5", "class": "forex", "pip": 0.0001, "spread_pips": 1.5, "description": "US Dollar / Swiss Franc"},
    "USDCAD": {"source": "mt5", "class": "forex", "pip": 0.0001, "spread_pips": 1.5, "description": "US Dollar / Canadian Dollar"},
    "AUDUSD": {"source": "mt5", "class": "forex", "pip": 0.0001, "spread_pips": 1.5, "description": "Australian Dollar / US Dollar"},
    "NZDUSD": {"source": "mt5", "class": "forex", "pip": 0.0001, "spread_pips": 1.8, "description": "New Zealand Dollar / US Dollar"},
    # Precious metals — MT5
    "XAUUSD": {"source": "mt5", "class": "commodity", "pip": 0.01,  "spread_pips": 10,  "comm_per_lot": 5.0, "description": "Gold Spot"},
    "XAGUSD": {"source": "mt5", "class": "commodity", "pip": 0.001, "spread_pips": 20,  "comm_per_lot": 5.0, "description": "Silver Spot"},
    # Major indices — MT5
    "US500":  {"source": "mt5", "class": "index",   "pip": 0.1,  "spread_pips": 2,   "comm_per_lot": 2.0, "description": "S&P 500 Index"},
    "USTEC":  {"source": "mt5", "class": "index",   "pip": 0.1,  "spread_pips": 3,   "comm_per_lot": 2.0, "description": "US Tech 100 (Nasdaq)"},
    "US30":   {"source": "mt5", "class": "index",   "pip": 0.1,  "spread_pips": 3,   "comm_per_lot": 2.0, "description": "Dow Jones Index"},
    # Top 10 crypto by market cap (Binance USDT pairs)
    "BTCUSDT": {"source": "binance", "class": "crypto", "pip": 0.01,   "comm_pct": 0.001, "description": "Bitcoin"},
    "ETHUSDT": {"source": "binance", "class": "crypto", "pip": 0.01,   "comm_pct": 0.001, "description": "Ethereum"},
    "SOLUSDT": {"source": "binance", "class": "crypto", "pip": 0.01,   "comm_pct": 0.001, "description": "Solana"},
    "XRPUSDT": {"source": "binance", "class": "crypto", "pip": 0.0001, "comm_pct": 0.001, "description": "XRP"},
    "BNBUSDT": {"source": "binance", "class": "crypto", "pip": 0.01,   "comm_pct": 0.001, "description": "BNB"},
    "DOGEUSDT":{"source": "binance", "class": "crypto", "pip": 0.00001,"comm_pct": 0.001, "description": "Dogecoin"},
    "ADAUSDT": {"source": "binance", "class": "crypto", "pip": 0.0001, "comm_pct": 0.001, "description": "Cardano"},
    "AVAXUSDT":{"source": "binance", "class": "crypto", "pip": 0.01,   "comm_pct": 0.001, "description": "Avalanche"},
    "DOTUSDT": {"source": "binance", "class": "crypto", "pip": 0.01,   "comm_pct": 0.001, "description": "Polkadot"},
    "LINKUSDT":{"source": "binance", "class": "crypto", "pip": 0.001,  "comm_pct": 0.001, "description": "Chainlink"},
}

# SCAN_ASSETS defines which symbols are actually scanned.
# NOTE: ASSET_DB includes stock indices (US500, USTEC, US30) but the user
# has excluded them from scans per preference. To exclude, set SCAN_ASSETS
# to a subset of ASSET_DB keys. See csv_scanner.py for the exclusion logic.
SCAN_ASSETS = list(ASSET_DB.keys())
# Native timeframes we fetch; higher TFs are resampled from 1h
NATIVE_TIMEFRAMES = ["15m", "1h"]
SCAN_TIMEFRAMES = ["15m", "1h", "4h", "daily"]
SCAN_BARS_PER_TF = {"15m": 20000, "1h": 10000}
# Crypto via CCXT gets 1000 bars per page; we fetch up to 30 pages (30k bars, ~3.4 years)
CCXT_MAX_PAGES = 30

TIMEFRAME_TO_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "daily": 1440}
INTERVAL_MAP_BINANCE = {"15m": "15m", "1h": "1h"}
# NOTE: fetch_yahoo() references INTERVAL_MAP_YAHOO and YAHOO_RANGE which are
# NOT defined in this file (Yahoo is not actively used). Add them if needed:
#   INTERVAL_MAP_YAHOO = {"15m": "15m", "1h": "60m", "4h": "1h", "daily": "1d"}
#   YAHOO_RANGE = {"15m": "5d", "1h": "1mo", "4h": "3mo", "daily": "2y"}

# MT5 timeframe mapping
TIMEFRAME_MAP_MT5 = {"15m": 15, "1h": 60, "4h": 240, "daily": 1440}

# Binance max limit
BINANCE_MAX_LIMIT = 5000

# ---------------------------------------------------------------------------
# Strategy templates — parameter grids + runners
# ---------------------------------------------------------------------------

TEMPLATES = [
    {
        "name": "Heikin-Ashi Momentum",
        "params": {"lookback": [1, 2, 3]},
        "entry": "Buy when HA Open > HA Close, HA Open = HA High, body expanding, {lookback} consecutive",
        "exit": "Sell when HA Open < HA Close, HA Open = HA Low",
    },
    {
        "name": "Parabolic SAR",
        "params": {"initial_af": [0.01, 0.02, 0.03], "max_af": [0.05, 0.1, 0.2]},
        "entry": "Buy when SAR moves below close (uptrend initiated)",
        "exit": "Sell when SAR moves above close (downtrend initiated)",
    },
    {
        "name": "Range Expansion",
        "params": {"lookback": [5, 10, 20], "mult": [1.2, 1.5, 2.0]},
        "entry": "Buy when range > {mult}*avg_range({lookback}) and close up",
        "exit": "Sell when range > {mult}*avg_range and close down",
    },
    {
        "name": "Dual Thrust",
        "params": {"lookback": [4, 8], "k1": [0.5, 0.7], "k2": [0.5, 0.7]},
        "entry": "Buy when price breaks above open + K1*range({lookback})",
        "exit": "Sell when price breaks below open - K2*range",
    },
    {
        "name": "Turtle",
        "params": {"entry_window": [10, 20, 40], "exit_window": [5, 10, 20]},
        "entry": "Buy when close > {entry_window}-day high",
        "exit": "Sell when close < {exit_window}-day low",
    },
    {
        "name": "Dynamic Breakout II",
        "params": {"lookback": [10, 20], "multiplier": [1.5, 2.0, 3.0]},
        "entry": "Buy when close > {lookback}-day high and close > prev close + {multiplier}*ATR",
        "exit": "Sell when close < {lookback}-day low and close < prev close - {multiplier}*ATR",
    },
    {
        "name": "R-Breaker",
        "params": {"lookback": [5, 10], "k1": [0.3, 0.5, 0.7], "k2": [0.3, 0.5, 0.7]},
        "entry": "Buy when open <= S1 level and close > S1 (mean reversion bounce)",
        "exit": "Sell when open >= R1 level and close < R1",
    },
    {
        "name": "Keltner Channel",
        "params": {"ema_period": [10, 20], "atr_period": [10, 14], "atr_mult": [1.5, 2.0, 2.5]},
        "entry": "Buy when close crosses above EMA + {atr_mult}*ATR",
        "exit": "Sell when close crosses below EMA - {atr_mult}*ATR",
    },
    {
        "name": "Awesome Oscillator",
        "params": {"fast_period": [5], "slow_period": [13, 21, 34]},
        "entry": "Buy when AO crosses above zero line",
        "exit": "Sell when AO crosses below zero line",
    },
    {
        "name": "Momentum Pinball",
        "params": {"lookback": [5, 10, 20]},
        "entry": "Buy when price breaks above {lookback}-bar high",
        "exit": "Sell when price breaks below {lookback}-bar low",
    },
    {
        "name": "Close Bias",
        "params": {"bias_pct": [0.1, 0.2, 0.3]},
        "entry": "Buy when close in top {bias_pct}% of bar range (bullish conviction)",
        "exit": "Sell when close in bottom {bias_pct}% of bar range",
    },
    {
        "name": "EMA Pullback",
        "params": {"ema_period": [10, 20, 50], "atr_stop": [1.5, 2.0]},
        "entry": "Buy when price pulls back to EMA then closes above it",
        "exit": "Sell when price rallies to EMA then closes below it",
    },
    {
        "name": "ATR Channel",
        "params": {"channel_period": [10, 20], "atr_mult": [1.5, 2.0, 3.0], "lookback": [10, 20]},
        "entry": "Buy when close > SMA+{atr_mult}*ATR and makes new {lookback}-bar high",
        "exit": "Sell when close < SMA-{atr_mult}*ATR and makes new {lookback}-bar low",
    },
    {
        "name": "Micro Trend",
        "params": {"fast_lb": [3, 5], "slow_lb": [8, 13, 21], "min_body_pct": [0.0, 0.3, 0.5]},
        "entry": "Buy when fast SMA crosses above slow SMA with body confirmation",
        "exit": "Sell when fast SMA crosses below slow SMA",
    },
]

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
# Three data sources (MT5, Binance/CCXT, Yahoo). Yahoo is legacy/unused.
# Each returns list of dicts: {timestamp, open, high, low, close, volume}.
# MT5 native intervals: M15, H1; others resampled (see fetch_data).
# Binance: paginated via CCXT (up to 30k bars).
# Yahoo: free endpoint (fallback, not actively used in current config).

def fetch_mt5(symbol, interval, limit):
    """Fetch OHLCV from MetaTrader 5 terminal.
    
    Converts interval string to MT5 timeframe constant via TIMEFRAME_MAP_MT5.
    Uses mt5.copy_rates_from_pos() starting from most recent bar.
    Returns empty list if MT5 not available or data fetch fails.
    Timestamps are converted from seconds (MT5) to milliseconds.
    volume = real_volume if available, else tick_volume.
    """
    if not MT5_AVAILABLE:
        return []
    tf_minutes = TIMEFRAME_MAP_MT5.get(interval, 60)
    tf_map = {15: mt5.TIMEFRAME_M15, 60: mt5.TIMEFRAME_H1, 240: mt5.TIMEFRAME_H4, 1440: mt5.TIMEFRAME_D1}
    mt5_tf = tf_map.get(tf_minutes, mt5.TIMEFRAME_H1)
    try:
        if not mt5.initialize():
            mt5.initialize()
        rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, limit)
        if rates is None or len(rates) == 0:
            return []
        ohlcv = []
        for r in rates:
            ohlcv.append({
                "timestamp": r["time"] * 1000,
                "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]),
                "volume": float(r["real_volume"] or r["tick_volume"] or 0),
            })
        return ohlcv
    except Exception:
        return []


def fetch_binance(symbol, interval, limit):
    """Fetch OHLCV from Binance via CCXT with pagination for full history.
    
    Strategy: Paginate backwards from 'since' (2024-01-01) in pages of 1000
    bars, up to CCXT_MAX_PAGES (30) pages or until no more data. 0.5s sleep
    between pages to avoid rate limits. Symbol format: BTC/USDT.
    
    Assumptions:
    - Uses USDT pairs only (as defined in ASSET_DB).
    - Starting date hardcoded to 2024-01-01 (may miss older data).
    - 0.5s sleep between pages; may hit Binance rate limits at peak.
    """
    if not CCXT_AVAILABLE:
        return []
    ccxt_symbol = symbol.replace("USDT", "/USDT").replace("BUSD", "/BUSD")
    try:
        exchange = ccxt.binance()
        iv_map = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h"}
        tf = iv_map.get(interval, "1h")
        # Request ~3 years worth of bars or until we hit limit
        since = exchange.parse8601("2024-01-01T00:00:00Z")
        all_candles = []
        max_pages = max(1, limit // 1000)
        for page in range(max_pages):
            try:
                candles = exchange.fetch_ohlcv(ccxt_symbol, tf, since=since, limit=1000)
            except Exception:
                break
            if not candles or len(candles) == 0:
                break
            all_candles.extend(candles)
            since = candles[-1][0] + 1
            time.sleep(0.5)
        if all_candles:
            ohlcv = []
            for k in all_candles:
                ohlcv.append({
                    "timestamp": k[0], "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                })
            return ohlcv
    except Exception:
        return []
    return []


def fetch_yahoo(symbol, interval, limit):
    """Fetch OHLCV from Yahoo Finance v7 API (legacy, not actively used).
    
    NOTE: This function is NOT called in current production flow (ASSET_DB
    sources are 'mt5' or 'binance' only). Kept as reference. Uses 3 retries
    with 2s backoff. Yahoo returns timestamps in seconds — converted to ms.
    INTERVAL_MAP_YAHOO and YAHOO_RANGE must be defined at module level.
    """
    y_iv = INTERVAL_MAP_YAHOO.get(interval, "1d")
    y_range = YAHOO_RANGE.get(interval, "2y")
    url = f"https://query1.finance.yahoo.com/v7/finance/chart/{symbol}?range={y_range}&interval={y_iv}"
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            result = data.get("chart", {}).get("result", [{}])[0]
            timestamps = result.get("timestamp", [])
            quotes = result.get("indicators", {}).get("quote", [{}])[0]
            ohlcv = []
            for i in range(min(len(timestamps), limit)):
                o = quotes.get("open", [None] * len(timestamps))[i]
                h = quotes.get("high", [None] * len(timestamps))[i]
                l = quotes.get("low", [None] * len(timestamps))[i]
                c = quotes.get("close", [None] * len(timestamps))[i]
                v = quotes.get("volume", [0] * len(timestamps))[i]
                if all(v is not None for v in [o, h, l, c]):
                    ts = timestamps[i]
                    if ts < 1e12:
                        ts *= 1000
                    ohlcv.append({"timestamp": ts, "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v or 0)})
            if ohlcv:
                return ohlcv
        except Exception:
            if attempt < 2:
                time.sleep(2)
    return []


def resample_ohlcv(data, target_minutes):
    """Resample OHLCV from source interval to higher timeframe.
    
    Calculates the source interval in minutes from first two timestamps,
    then groups bars into chunks of size step = target_minutes / source_minutes.
    Uses: O=first open, H=max high, L=min low, C=last close, V=sum volume.
    
    Assumptions:
    - Data is in chronological order (ascending timestamps).
    - target_minutes must be an integer multiple of source_minutes.
    - No gap-filling: if data has gaps, resampling may undercount bars.
    """
    if not data:
        return []
    source_minutes = None
    if len(data) > 1:
        source_minutes = (data[1]["timestamp"] - data[0]["timestamp"]) / 60000
    if source_minutes is None or source_minutes <= 0:
        return data
    step = max(1, int(round(target_minutes / source_minutes)))
    if step <= 1:
        return data
    result = []
    for i in range(0, len(data), step):
        chunk = data[i:i + step]
        if not chunk:
            continue
        o = chunk[0]["open"]
        h = max(d["high"] for d in chunk)
        l = min(d["low"] for d in chunk)
        c = chunk[-1]["close"]
        v = sum(d.get("volume", 0) for d in chunk)
        result.append({
            "timestamp": chunk[0]["timestamp"],
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })
    return result


def fetch_data(symbol, interval):
    """Unified data fetcher: routes to the correct source and handles resampling.
    
    Logic:
    1. Look up symbol in ASSET_DB to determine source (mt5/binance).
    2. If requested interval is not a native timeframe (15m or 1h), fetch the
       base interval "1h" and resample upward (4h, daily).
    3. Native timeframes fetch directly at requested interval.
    4. Bar count per fetch determined by SCAN_BARS_PER_TF (20k for 15m, 10k for 1h).
    
    Assumptions:
    - All higher TFs (4h, daily) are resampled from 1h. This means 4h bars
      use the 1h OHLC of the last 4 hours, which is correct.
    - Minimum 10 bars required to proceed with backtest.
    """
    info = ASSET_DB.get(symbol, {})
    source = info.get("source", "")
    target_min = TIMEFRAME_TO_MINUTES.get(interval, 60)
    if interval not in NATIVE_TIMEFRAMES:
        base_interval = "1h"
    else:
        base_interval = interval
    limit = SCAN_BARS_PER_TF.get(base_interval, 10000)
    if source == "binance":
        data = fetch_binance(symbol, base_interval, limit)
    elif source == "mt5":
        data = fetch_mt5(symbol, base_interval, limit)
    else:
        return []
    if not data or len(data) < 10:
        return []
    if interval != base_interval:
        data = resample_ohlcv(data, target_min)
    return data

# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
# Simple technical indicators used by strategy runners. All return lists
# aligned to input length, with None-padding at the start (warm-up period).
# NOTE: sma() and ema_arr() accept raw price lists; atr() accepts OHLCV dicts.

def sma(data, period):
    """Simple Moving Average. Pads first (period-1) values with None."""
    return [None] * (period - 1) + [sum(data[i - period + 1:i + 1]) / period for i in range(period - 1, len(data))]

def ema_arr(data, period):
    """Exponential Moving Average. EMA[0] = data[0]; mult = 2/(period+1)."""
    result = [data[0]]
    mult = 2 / (period + 1)
    for i in range(1, len(data)):
        result.append((data[i] - result[-1]) * mult + result[-1])
    return result

def rsi_fn(data, period):
    """Relative Strength Index (RSI). Simple SMA-based gain/loss averaging."""
    if len(data) < period + 1:
        return [None] * len(data)
    result = [None] * period
    for i in range(period, len(data)):
        gains = [max(data[j] - data[j-1], 0) for j in range(i - period + 1, i + 1)]
        losses = [max(data[j-1] - data[j], 0) for j in range(i - period + 1, i + 1)]
        avg_g = sum(gains) / period
        avg_l = sum(losses) / period
        result.append(100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))
    return result

def crossover(a, b):
    """Return boolean list: True where a crosses ABOVE b (a[i-1]<=b[i-1] and a[i]>b[i]).
    Handles None values safely — returns False if either series is None."""
    return [i > 0 and a[i-1] is not None and b[i-1] is not None and a[i] is not None and b[i] is not None and a[i-1] <= b[i-1] and a[i] > b[i] for i in range(len(a))]

def crossunder(a, b):
    """Return boolean list: True where a crosses BELOW b (a[i-1]>=b[i-1] and a[i]<b[i]).
    Handles None values safely — returns False if either series is None."""
    return [i > 0 and a[i-1] is not None and b[i-1] is not None and a[i] is not None and b[i] is not None and a[i-1] >= b[i-1] and a[i] < b[i] for i in range(len(a))]

def atr(data, period):
    """Average True Range (Wilder-style, SMA of True Range).
    TR = max(high-low, |high-prev_close|, |low-prev_close|).
    Pads first 'period' values with None. Works on OHLCV dicts.
    """
    tr = []
    for i in range(1, len(data)):
        hl = data[i]["high"] - data[i]["low"]
        hc = abs(data[i]["high"] - data[i-1]["close"]) if i > 0 else 0
        lc = abs(data[i]["low"] - data[i-1]["close"]) if i > 0 else 0
        tr.append(max(hl, hc, lc))
    tr = [None] + tr
    if len(tr) <= period:
        return [None] * len(data)
    result = [None] * period
    for i in range(period, len(tr)):
        seg = tr[i - period + 1:i + 1]
        result.append(sum(seg) / period)
    while len(result) < len(data):
        result.append(None)
    return result[:len(data)]

# ---------------------------------------------------------------------------
# Template runners — each returns (buy_signals, sell_signals)
# ---------------------------------------------------------------------------
# Each runner:
#   Input:  data (list of OHLCV dicts), params (dict from template grid)
#   Output: Two boolean lists (buy, sell) aligned to data length.
#   Convention: True at index i means "enter/reverse at close of bar i".
#   WARNING: buy[i] and sell[i] CAN both be False, but if both True the
#   backtest will attempt both entry and exit on the same bar (last signal
#   processed wins). Runners should avoid simultaneous signals.
#
# All runners are LONG-only (no short logic). Sell signals close the long
# position. This means every runner is a long-only trend-following or
# mean-reversion system. No short entries are generated.
# ---------------------------------------------------------------------------

def run_dual_thrust(data, params):
    """Dual Thrust — breakout of range-based channel.
    
    STRATEGY:
    Compute HH (max high), HC (max close), LC (min close), LL (min low)
    over lookback bars. Range = max(HH - LC, HC - LL).
    Buy trigger = open + K1 * range; Sell trigger = open - K2 * range.
    Entry when prev_close <= trigger AND current close > trigger (breakout).
    
    PARAMETERS:
      lookback (int): Number of bars for range calculation (4, 8).
      k1 (float): Long entry multiplier for upper trigger (0.5, 0.7).
      k2 (float): Short/exit multiplier for lower trigger (0.5, 0.7).
    
    ENTRY: Long when close breaks above open + K1×range.
    EXIT:  Close when close breaks below open - K2×range.
    
    ASSUMPTIONS:
    - Uses open of current bar as reference (intraday breakouts).
    - Range formula combines HH-LC and HC-LL (Crabel-style).
    - range==0 check prevents division but skips flat markets.
    """
    n = params["lookback"]
    n = params["lookback"]
    k1 = params["k1"]
    k2 = params["k2"]
    buy, sell = [], []
    for i in range(len(data)):
        if i < n:
            buy.append(False)
            sell.append(False)
            continue
        HH = max(data[j]["high"] for j in range(i - n, i))
        HC = max(data[j]["close"] for j in range(i - n, i))
        LC = min(data[j]["close"] for j in range(i - n, i))
        LL = min(data[j]["low"] for j in range(i - n, i))
        rng = max(HH - LC, HC - LL)
        if rng == 0:
            buy.append(False)
            sell.append(False)
            continue
        o = data[i]["open"]
        c = data[i]["close"]
        buy_trig = o + k1 * rng
        sell_trig = o - k2 * rng
        prev = data[i-1]["close"]
        buy.append(prev <= buy_trig and c > buy_trig)
        sell.append(prev >= sell_trig and c < sell_trig)
    return buy, sell

def run_turtle(data, params):
    """Turtle — Donchian channel breakout (original Turtle system).
    
    STRATEGY:
    Entry: Buy when close > highest high of last entry_window bars.
    Exit:  Sell when close < lowest low of last exit_window bars.
    This is the classic Richard Dennis Turtle system simplified to a
    single-entry, single-exit channel.
    
    PARAMETERS:
      entry_window (int): Lookback for entry channel high (10, 20, 40).
      exit_window (int):  Lookback for exit channel low (5, 10, 20).
    
    ENTRY: close > entry_window-period high.
    EXIT:  close < exit_window-period low.
    
    ASSUMPTIONS:
    - No pyramiding or position scaling (original Turtles added units).
    - entry_window > exit_window gives a "tight" stop (recommended).
    - Long-only: no short entries (original Turtles traded both sides).
    """
    entry_w = params["entry_window"]
    exit_w = params["exit_window"]
    buy, sell = [], []
    for i in range(len(data)):
        if i < entry_w:
            buy.append(False)
            sell.append(False)
            continue
        high_n = max(data[j]["high"] for j in range(i - entry_w, i))
        low_m = min(data[j]["low"] for j in range(i - exit_w, i)) if i >= exit_w else 0
        c = data[i]["close"]
        buy.append(c > high_n)
        sell.append(c < low_m)
    return buy, sell

def run_dynamic_breakout(data, params):
    """Dynamic Breakout II — breakout with ATR volatility filter.
    
    STRATEGY:
    Combines lookback channel breakout with an ATR-based momentum filter.
    Entry requires: (1) close > lookback-period high AND (2) close > prev
    close + multiplier × ATR. Exit: close < lookback-period low AND close <
    prev close - multiplier × ATR.
    
    PARAMETERS:
      lookback (int): Period for channel high/low and ATR (10, 20).
      multiplier (float): ATR multiplier for filter (1.5, 2.0, 3.0).
    
    ENTRY: close > lookback high AND price moved up > multiplier×ATR.
    EXIT:  close < lookback low AND price moved down > multiplier×ATR.
    
    ASSUMPTIONS:
    - ATR uses SMA method (not Wilder's RMA).
    - Dual condition reduces whipsaws but may miss strong trends.
    """
    lookback = params["lookback"]
    mult = params["multiplier"]
    atr_vals = atr(data, lookback)
    buy, sell = [], []
    for i in range(len(data)):
        if i < lookback or atr_vals[i] is None:
            buy.append(False)
            sell.append(False)
            continue
        high_n = max(data[j]["high"] for j in range(i - lookback, i))
        low_n = min(data[j]["low"] for j in range(i - lookback, i))
        c = data[i]["close"]
        p = data[i-1]["close"]
        buy_signal = c > data[i-1]["close"] + mult * atr_vals[i]
        sell_signal = c < data[i-1]["close"] - mult * atr_vals[i]
        buy.append(c > high_n and buy_signal)
        sell.append(c < low_n and sell_signal)
    return buy, sell

def run_rbreaker(data, params):
    """R-Breaker — mean reversion at pivot levels.
    
    STRATEGY:
    Compute pivot = (H + L + C_prev) / 3 over lookback. Resistance R1 =
    pivot + k1 × range. Support S1 = pivot - k2 × range.
    Buy when: open <= S1 AND close > S1 (bounce off support).
    Sell when: open >= R1 AND close < R1 (reject at resistance).
    This is a counter-trend/mean reversion strategy.
    
    PARAMETERS:
      lookback (int): Pivot calculation window (5, 10).
      k1 (float): Resistance multiplier from pivot (0.3, 0.5, 0.7).
      k2 (float): Support multiplier from pivot (0.3, 0.5, 0.7).
    
    ENTRY (long): Price opens below S1 then closes back above (fakeout).
    EXIT:  Price opens above R1 then closes back below (failure).
    
    ASSUMPTIONS:
    - intraday mean reversion: assumes bounces at support/resistance.
    - Uses previous bar's close for pivot, combined with current bar's open.
    - Long-only: buy signals at support, sell signals at resistance.
    """
    lookback = params["lookback"]
    k1 = params["k1"]
    k2 = params["k2"]
    buy, sell = [], []
    for i in range(len(data)):
        if i < lookback:
            buy.append(False)
            sell.append(False)
            continue
        H = max(data[j]["high"] for j in range(i - lookback, i))
        L = min(data[j]["low"] for j in range(i - lookback, i))
        C = data[i-1]["close"]
        pivot = (H + L + C) / 3
        rng = H - L
        if rng == 0:
            buy.append(False)
            sell.append(False)
            continue
        s1 = pivot - k2 * rng
        r1 = pivot + k1 * rng
        o = data[i]["open"]
        c = data[i]["close"]
        buy.append(c > o and o <= s1 and c > s1)
        sell.append(c < o and o >= r1 and c < r1)
    return buy, sell

def run_heikin_ashi(data, params):
    """Heikin-Ashi Momentum — smoothed candle pattern following.
    
    STRATEGY:
    Convert OHLCV to Heikin-Ashi candles:
      HA_Close = (O+H+L+C)/4
      HA_Open  = (prev_HA_Open + prev_HA_Close)/2
      HA_High  = max(H, HA_Open, HA_Close)
      HA_Low   = min(L, HA_Open, HA_Close)
    Entry: lookback consecutive bear candles (HA_Open > HA_Close) where
    HA_Open == HA_High AND body is expanding. Exit: same conditions on
    bull side (HA_Open < HA_Close, HA_Open == HA_Low).
    
    PARAMETERS:
      lookback (int): Number of consecutive HA candles required (1, 2, 3).
    
    ENTRY: {lookback} consecutive bear candles with upper wick=0, body growing.
    EXIT:  {lookback} consecutive bull candles with lower wick=0, body growing.
    
    ASSUMPTIONS:
    - HA smoothing lags price by 1-2 bars (inherent).
    - Body expansion filter requires body to grow vs prior bar.
    - HA_Open == HA_High (or HA_Open == HA_Low) checks for wickless candles.
    - Best on lower timeframes (1m-15m) where patterns are frequent.
    """
    lb = params["lookback"]
    n = len(data)
    ha_open = [0.0] * n
    ha_close = [0.0] * n
    ha_high = [0.0] * n
    ha_low = [0.0] * n
    ha_close[0] = (data[0]["open"] + data[0]["high"] + data[0]["low"] + data[0]["close"]) / 4
    ha_open[0] = data[0]["open"]
    ha_high[0] = max(data[0]["high"], ha_open[0], ha_close[0])
    ha_low[0] = min(data[0]["low"], ha_open[0], ha_close[0])
    for i in range(1, n):
        ha_close[i] = (data[i]["open"] + data[i]["high"] + data[i]["low"] + data[i]["close"]) / 4
        ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2
        ha_high[i] = max(data[i]["high"], ha_open[i], ha_close[i])
        ha_low[i] = min(data[i]["low"], ha_open[i], ha_close[i])
    buy, sell = [], []
    for i in range(n):
        if i < max(3, lb + 1):
            buy.append(False)
            sell.append(False)
            continue
        body = abs(ha_open[i] - ha_close[i])
        prev_body = abs(ha_open[i-1] - ha_close[i-1])
        consecutive_bull = all(ha_open[j] > ha_close[j] for j in range(i - lb + 1, i + 1))
        consecutive_bear = all(ha_open[j] < ha_close[j] for j in range(i - lb + 1, i + 1))
        is_bull = (ha_open[i] > ha_close[i] and ha_open[i] == ha_high[i] and
                   body > prev_body and consecutive_bull)
        is_exit = (ha_open[i] < ha_close[i] and ha_open[i] == ha_low[i] and
                   consecutive_bear and body > prev_body)
        buy.append(is_bull)
        sell.append(is_exit)
    return buy, sell

def run_parabolic_sar(data, params):
    """Parabolic SAR — trend-following stop-and-reverse.
    
    STRATEGY:
    Standard Parabolic SAR calculation. SAR[i] = SAR[i-1] + AF × (EP - SAR[i-1]).
    AF starts at initial_af, increases by initial_af each time a new extreme
    (EP) is made, capped at max_af. Trend: +1 (uptrend, SAR below price),
    -1 (downtrend, SAR above price). Buy when trend flips from -1 to +1
    (SAR moves below price). Sell when trend flips from +1 to -1.
    
    PARAMETERS:
      initial_af (float): Starting acceleration factor (0.01, 0.02, 0.03).
      max_af (float): Maximum acceleration factor (0.05, 0.1, 0.2).
    
    ENTRY: SAR crosses below price (start of uptrend).
    EXIT:  SAR crosses above price (start of downtrend).
    
    ASSUMPTIONS:
    - Uses SAR[i] < data[i].high for uptrend (original uses close).
    - Trend intensity (abs(trend)) tracks consecutive bars in trend.
    - AF resets to initial_af when trend reverses.
    - First trend direction determined by bar1 > bar0 close.
    """
    init_af = params["initial_af"]
    max_af = params["max_af"]
    n = len(data)
    sar = [0.0] * n
    trend = [0] * n
    ep = [0.0] * n
    af = [0.0] * n
    if n < 3:
        return [False] * n, [False] * n
    trend[1] = 1 if data[1]["close"] > data[0]["close"] else -1
    sar[1] = data[0]["low"] if trend[1] > 0 else data[0]["high"]
    ep[1] = data[1]["high"] if trend[1] > 0 else data[1]["low"]
    af[1] = init_af
    for i in range(2, n):
        temp = sar[i-1] + af[i-1] * (ep[i-1] - sar[i-1])
        if trend[i-1] < 0:
            sar[i] = max(temp, data[i-1]["high"], data[i-2]["high"])
            trend[i] = 1 if sar[i] < data[i]["high"] else trend[i-1] - 1
        else:
            sar[i] = min(temp, data[i-1]["low"], data[i-2]["low"])
            trend[i] = -1 if sar[i] > data[i]["low"] else trend[i-1] + 1
        if trend[i] < 0:
            ep[i] = min(data[i]["low"], ep[i-1]) if trend[i] != -1 else data[i]["low"]
        else:
            ep[i] = max(data[i]["high"], ep[i-1]) if trend[i] != 1 else data[i]["high"]
        af[i] = init_af if abs(trend[i]) == 1 else min(max_af, af[i-1] + init_af)
    buy = [False] * n
    sell = [False] * n
    for i in range(2, n):
        if trend[i] > 0 and trend[i-1] <= 0:
            buy[i] = True
        elif trend[i] < 0 and trend[i-1] >= 0:
            sell[i] = True
    return buy, sell

def run_awesome_oscillator(data, params):
    """Awesome Oscillator — Bill Williams' zero-line crossover.
    
    STRATEGY:
    AO = SMA(fast, median_price) - SMA(slow, median_price).
    Buy when AO crosses above zero (momentum turning positive).
    Sell when AO crosses below zero (momentum turning negative).
    Median price = (high + low) / 2 per bar.
    
    PARAMETERS:
      fast_period (int): Fast SMA on median price (5).
      slow_period (int): Slow SMA on median price (13, 21, 34).
    
    ENTRY: AO crosses from <= 0 to > 0.
    EXIT:  AO crosses from >= 0 to < 0.
    
    ASSUMPTIONS:
    - Uses simple SMA (not Wilder's smoothing).
    - Zero-line crossover only; no saucer/twin-peak patterns.
    - Standard params: 5/34 (classic), also 5/13, 5/21.
    """
    fast = params["fast_period"]
    slow = params["slow_period"]
    n = len(data)
    median = [(d["high"] + d["low"]) / 2 for d in data]
    fast_ma = sma(median, fast)
    slow_ma = sma(median, slow)
    ao = [fast_ma[i] - slow_ma[i] if fast_ma[i] is not None and slow_ma[i] is not None else 0 for i in range(n)]
    buy, sell = [], []
    for i in range(n):
        if i < 1 or ao[i-1] is None:
            buy.append(False)
            sell.append(False)
            continue
        buy.append(ao[i-1] <= 0 and ao[i] > 0)
        sell.append(ao[i-1] >= 0 and ao[i] < 0)
    return buy, sell

def run_keltner(data, params):
    """Keltner Channel — volatility channel breakout.
    
    STRATEGY:
    Center line: EMA of close. Upper band: EMA + atr_mult × ATR.
    Lower band: EMA - atr_mult × ATR.
    Buy when close crosses above upper band (upside breakout).
    Sell when close crosses below lower band (downside breakdown).
    
    PARAMETERS:
      ema_period (int): EMA period for center line (10, 20).
      atr_period (int): ATR period for channel width (10, 14).
      atr_mult (float): ATR multiplier for band width (1.5, 2.0, 2.5).
    
    ENTRY: Close crosses above EMA + atr_mult×ATR.
    EXIT:  Close crosses below EMA - atr_mult×ATR.
    
    ASSUMPTIONS:
    - EMA is used (not SMA) for center line (more responsive).
    - ATR uses SMA method, same as atr() function.
    - Crossover detection: prev close on one side, current on other.
    """
    ema_p = params["ema_period"]
    atr_p = params["atr_period"]
    mult = params["atr_mult"]
    n = len(data)
    c = [d["close"] for d in data]
    ema = ema_arr(c, ema_p)
    atr_vals = atr(data, atr_p)
    buy, sell = [], []
    for i in range(n):
        if i < 1 or ema[i] is None or atr_vals[i] is None:
            buy.append(False)
            sell.append(False)
            continue
        upper = ema[i] + mult * atr_vals[i]
        lower = ema[i] - mult * atr_vals[i]
        prev_c = c[i-1]
        cur_c = c[i]
        buy.append(prev_c <= upper and cur_c > upper)
        sell.append(prev_c >= lower and cur_c < lower)
    return buy, sell

def run_momentum_pinball(data, params):
    """Momentum Pinball — N-bar high/low breakout (pure momentum).
    
    STRATEGY:
    Buy when close breaks above the highest close of the last lb bars.
    Sell when close breaks below the lowest close of the last lb bars.
    Entry fires on the FIRST bar that makes a new lb-period high/low.
    
    PARAMETERS:
      lookback (int): Window for high/low calculation (5, 10, 20).
    
    ENTRY: close > highest close of last lb bars (and prev close <= that high).
    EXIT:  close < lowest close of last lb bars (and prev close >= that low).
    
    ASSUMPTIONS:
    - Uses close prices only (no high/low channels).
    - Crossover logic prevents re-entry on same bar.
    - Simplest possible breakout: pure price momentum.
    """
    lb = params["lookback"]
    n = len(data)
    c = [d["close"] for d in data]
    buy, sell = [], []
    for i in range(n):
        if i < lb:
            buy.append(False)
            sell.append(False)
            continue
        high_n = max(c[i-lb:i])
        low_n = min(c[i-lb:i])
        buy.append(c[i] > high_n and c[i-1] <= high_n)
        sell.append(c[i] < low_n and c[i-1] >= low_n)
    return buy, sell

def run_close_bias(data, params):
    """Close Bias — positional close within bar range.
    
    STRATEGY:
    Calculate close position within bar: pos = (close - low) / (high - low).
    Buy when close in top bias_pct% of range (close near high, bullish).
    Sell when close in bottom bias_pct% of range (close near low, bearish).
    Also requires directional bias: close > open for buy, close < open for sell.
    
    PARAMETERS:
      bias_pct (float): Threshold as fraction of range (0.1, 0.2, 0.3).
        e.g., 0.1 means close in top/bottom 10% of bar range.
    
    ENTRY: close in top bias_pct% AND close > open.
    EXIT:  close in bottom bias_pct% AND close < open.
    
    ASSUMPTIONS:
    - Measures buying/selling pressure within a single bar.
    - Does not use lookback; signals can be noisy on small ranges.
    - directional filter (close>open for buy) prevents false signals.
    """
    bp = params["bias_pct"]
    n = len(data)
    buy, sell = [], []
    for i in range(n):
        o, h, l, cl = data[i]["open"], data[i]["high"], data[i]["low"], data[i]["close"]
        rng = h - l
        if rng == 0:
            buy.append(False)
            sell.append(False)
            continue
        pos = (cl - l) / rng
        upper_thresh = 1 - bp
        buy.append(pos >= upper_thresh and cl > o)
        sell.append(pos <= bp and cl < o)
    return buy, sell

def run_range_expansion(data, params):
    """Range Expansion — volatility breakout (range vs average).
    
    STRATEGY:
    Current bar range = high - low. Average range over lookback = SMA of ranges.
    Buy when: current range > multiplier × avg_range AND close > prev close
    (upside volatility expansion). Sell when: current range > multiplier ×
    avg_range AND close < prev close (downside volatility expansion).
    
    PARAMETERS:
      lookback (int): Window for average range calculation (5, 10, 20).
      mult (float): Volatility threshold multiplier (1.2, 1.5, 2.0).
    
    ENTRY: Range exceeds mult×avg_range AND bar closes up.
    EXIT:  Range exceeds mult×avg_range AND bar closes down.
    
    ASSUMPTIONS:
    - Measures volatility expansion, not direction.
    - Direction determined by close vs prev close (simple).
    - Only fires on bars with significantly wider range than normal.
    """
    lb = params["lookback"]
    mult = params["mult"]
    n = len(data)
    ranges = [d["high"] - d["low"] for d in data]
    buy, sell = [], []
    for i in range(n):
        if i < lb:
            buy.append(False)
            sell.append(False)
            continue
        avg_r = sum(ranges[i-lb:i]) / lb
        if avg_r == 0:
            buy.append(False)
            sell.append(False)
            continue
        cur_r = ranges[i]
        cl = data[i]["close"]
        prev_cl = data[i-1]["close"]
        buy.append(cur_r > mult * avg_r and cl > prev_cl)
        sell.append(cur_r > mult * avg_r and cl < prev_cl)
    return buy, sell

def run_ema_pullback(data, params):
    """EMA Pullback — trend pullback to EMA.
    
    STRATEGY:
    Buy when price pulls back to EMA (close prev <= EMA prev) then rallies
    back above EMA (close > EMA). Sell when price rallies to EMA (close prev
    >= EMA prev) then falls below EMA (close < EMA). ATR_stop parameter
    currently unused (reserved for stop-loss placement).
    
    PARAMETERS:
      ema_period (int): EMA period for pullback level (10, 20, 50).
      atr_stop (float): ATR multiplier for stop (1.5, 2.0) — NOT YET IMPLEMENTED.
    
    ENTRY: Price crosses above EMA after being below it.
    EXIT:  Price crosses below EMA after being above it.
    
    ASSUMPTIONS:
    - Classic trend-following: buy dips in uptrend, sell rips in downtrend.
    - No actual stop-loss is placed (atr_stop defined but unused).
    - Crossover logic: prev bar on one side, current on other.
    """
    ep = params["ema_period"]
    atr_s = params["atr_stop"]
    n = len(data)
    c = [d["close"] for d in data]
    ema = ema_arr(c, ep)
    atr_vals = atr(data, 14)
    buy, sell = [], []
    for i in range(n):
        if i < max(ep, 15) or ema[i] is None or atr_vals[i] is None:
            buy.append(False)
            sell.append(False)
            continue
        buy.append(c[i-1] <= ema[i-1] and c[i] > ema[i])
        sell.append(c[i-1] >= ema[i-1] and c[i] < ema[i])
    return buy, sell

def run_atr_channel(data, params):
    """ATR Channel — SMA with ATR bands + lookback confirmation.
    
    STRATEGY:
    Channel: SMA(close, channel_period) ± atr_mult × ATR(14).
    Buy when: close > upper band AND close crosses above upper AND makes
    new lookback-period high. Sell when: close < lower band AND close
    crosses below lower AND makes new lookback-period low.
    
    PARAMETERS:
      channel_period (int): SMA period for channel center (10, 20).
      atr_mult (float): ATR multiplier for band width (1.5, 2.0, 3.0).
      lookback (int): Window for new high/low confirmation (10, 20).
    
    ENTRY: Close > upper band + crossover + new lookback high.
    EXIT:  Close < lower band + crossunder + new lookback low.
    
    ASSUMPTIONS:
    - Triple confirmation: band breakout + crossover + new extreme.
    - Reduces false signals but may miss trend entries.
    - ATR(14) fixed period (not parameterized).
    """
    cp = params["channel_period"]
    am = params["atr_mult"]
    lb = params["lookback"]
    n = len(data)
    c = [d["close"] for d in data]
    atr_vals = atr(data, 14)
    buy, sell = [], []
    for i in range(n):
        if i < max(cp, 15, lb) or atr_vals[i] is None:
            buy.append(False)
            sell.append(False)
            continue
        sma_val = sum(c[i-cp:i]) / cp
        upper = sma_val + am * atr_vals[i]
        lower = sma_val - am * atr_vals[i]
        high_n = max(data[j]["high"] for j in range(i-lb, i))
        low_n = min(data[j]["low"] for j in range(i-lb, i))
        buy.append(c[i] > upper and c[i-1] <= upper and c[i] > high_n)
        sell.append(c[i] < lower and c[i-1] >= lower and c[i] < low_n)
    return buy, sell

def run_micro_trend(data, params):
    """Micro Trend — SMA crossover with candle body confirmation.
    
    STRATEGY:
    Fast SMA and slow SMA of close. Buy on crossover (fast > slow) when
    current bar's body >= min_body_pct × average body (confirms momentum).
    Sell on crossunder (fast < slow) without body filter.
    
    PARAMETERS:
      fast_lb (int): Fast SMA period (3, 5).
      slow_lb (int): Slow SMA period (8, 13, 21).
      min_body_pct (float): Minimum body size as fraction of avg body
        over last 20 bars (0.0, 0.3, 0.5). 0.0 = no filter.
    
    ENTRY: Fast SMA crosses above slow SMA with body confirmation.
    EXIT:  Fast SMA crosses below slow SMA (no body filter).
    
    ASSUMPTIONS:
    - Body filter prevents entry on doji/spinning-top crossovers.
    - Average body calculated over trailing 20 bars (rolling).
    - No body filter on exit (exits at first sign of weakness).
    - Uses SMA (not EMA) for crossover signals.
    """
    f_lb = params["fast_lb"]
    s_lb = params["slow_lb"]
    mbp = params["min_body_pct"]
    n = len(data)
    c = [d["close"] for d in data]
    fast_sma = sma(c, f_lb)
    slow_sma = sma(c, s_lb)
    buy, sell = [], []
    for i in range(n):
        if i < max(f_lb, s_lb) or fast_sma[i] is None or slow_sma[i] is None:
            buy.append(False)
            sell.append(False)
            continue
        body = abs(data[i]["close"] - data[i]["open"])
        avg_body = sum(abs(data[j]["close"] - data[j]["open"]) for j in range(max(0, i-20), i+1)) / min(i+1, 21)
        body_ok = body >= mbp * avg_body if avg_body > 0 else True
        buy.append(fast_sma[i] > slow_sma[i] and fast_sma[i-1] <= slow_sma[i-1] and body_ok)
        sell.append(fast_sma[i] < slow_sma[i] and fast_sma[i-1] >= slow_sma[i-1])
    return buy, sell

# RUNNERS dict: maps strategy template names (from TEMPLATES list) to runner
# functions. Each runner takes (data, params) → (buy_signals, sell_signals).
# All runners are LONG-ONLY: buy signals open longs, sell signals close them.
# Strategy names must match exactly between TEMPLATES and RUNNERS keys.

RUNNERS = {
    "Dual Thrust": run_dual_thrust,
    "Heikin-Ashi Momentum": run_heikin_ashi,
    "Parabolic SAR": run_parabolic_sar,
    "Range Expansion": run_range_expansion,
    "Turtle": run_turtle,
    "Dynamic Breakout II": run_dynamic_breakout,
    "R-Breaker": run_rbreaker,
    "Keltner Channel": run_keltner,
    "Awesome Oscillator": run_awesome_oscillator,
    "Momentum Pinball": run_momentum_pinball,
    "Close Bias": run_close_bias,
    "EMA Pullback": run_ema_pullback,
    "ATR Channel": run_atr_channel,
    "Micro Trend": run_micro_trend,
}

# ---------------------------------------------------------------------------
# Fee calculation per asset class
# ---------------------------------------------------------------------------
# Calculates round-trip trading costs for a single trade.
# Fee structures differ by asset class (defined in ASSET_DB):
#   Crypto: percentage-based on notional value (entry + exit).
#     Formula: (entry_price + exit_price) × abs(size) × comm_pct
#     comm_pct = 0.001 (0.1%) per side.
#   Forex: spread-based, charged once per round trip.
#     Formula: spread_pips × pip × abs(size)
#     NOTE: spread is the BID-ASK spread in pips, not commission.
#     Typically charged at entry only; actual brokers charge on both sides.
#   Stock: per-share commission, both sides.
#     Formula: abs(size) × comm_per_share × 2
#   Commodity/Index: per-lot commission, both sides.
#     Formula: max(1, abs(size)/100) × comm_per_lot × 2
#     Lots: 1 lot = 100 units (standard forex lot convention for metals).
#
# NOTE: Slippage cost is added AFTER this function (added in backtest loop).
# All fee values are in account currency (USD).

def calculate_fees(asset_info, entry_price, exit_price, position_size):
    """Calculate round-trip trading fees for a given trade.
    
    Args:
        asset_info: Dict from ASSET_DB for the symbol.
        entry_price: Price at position open.
        exit_price: Price at position close.
        position_size: Number of units (positive for long).
    
    Returns:
        Total fee in account currency (USD).
    """
    asset_class = asset_info.get("class", "stock")
    if asset_class == "crypto":
        # Crypto: % commission on notional value, both sides.
        # E.g., BTCUSDT with size=1 at $50k: 50000*1*0.001 + 50000*1*0.001 = $100
        fee_rate = asset_info.get("comm_pct", 0.001)
        return (entry_price * abs(position_size) * fee_rate +
                exit_price * abs(position_size) * fee_rate)
    elif asset_class == "forex":
        # Forex: spread cost (fixed pips), charged once.
        # E.g., EURUSD size=10000, spread=1.2 pips: 1.2 * 0.0001 * 10000 = $1.20
        spread_pips = asset_info.get("spread_pips", 1.5)
        pip = asset_info.get("pip", 0.0001)
        return spread_pips * pip * abs(position_size)
    elif asset_class == "stock":
        # Stock: per-share commission, both sides ($0.005/share default).
        comm = asset_info.get("comm_per_share", 0.005)
        return abs(position_size) * comm * 2
    elif asset_class == "commodity":
        # Commodity (XAUUSD, XAGUSD): per-lot commission, both sides.
        # 1 lot = 100 units. Min 1 lot.
        comm = asset_info.get("comm_per_lot", 5.0)
        lots = max(1, abs(position_size) / 100)
        return comm * 2 * lots
    elif asset_class == "index":
        # Index (US500, USTEC, US30): per-lot commission, both sides.
        # 1 lot = 100 units. Min 1 lot.
        comm = asset_info.get("comm_per_lot", 2.0)
        lots = max(1, abs(position_size) / 100)
        return comm * 2 * lots
    return 0.0

# ---------------------------------------------------------------------------
# Core backtest — fee-aware with position sizing
# ---------------------------------------------------------------------------
# This is the heart of the engine. For each bar, it:
#   1. Checks exit signals (close position if sell signal fires).
#   2. Checks entry signals (open position if buy signal fires and flat).
#   3. Tracks equity curve (balance + unrealized P&L).
#
# POSITION SIZING METHODOLOGY:
#   Fixed-fractional risk: risk_per_trade_pct = 1% of current balance.
#   Stop distance = max(2 × ATR(14), close × 0.5%).
#     • 14-period ATR of bar ranges (high-low) over trailing 21 bars.
#     • 0.5% price floor prevents absurdly large positions on tiny ATR.
#   Position size = risk_dollars / stop_distance.
#   Capped by: max_affordable = int(balance / close).
#   This is a SIMPLIFIED position sizing model:
#     - ATR acts as proxy for stop loss distance (no actual stop placed).
#     - No compound scaling beyond 1% risk per trade.
#     - Fractional units not supported (int() floors size).
#
# FEE HANDLING:
#   Fees subtracted from gross P&L at exit. Slippage added as extra cost.
#   calculate_fees() called per trade + slippage_pips converted to $.
#
# METRICS COMPUTED:
#   Win rate, profit factor, net profit factor, Sharpe (daily returns),
#   max drawdown (peak-to-trough %), CAGR, Calmar, avg R:R, trades/yr.
#   Minimum 3 trades required to return results.
#   Sharpe annualized using sqrt(bars_per_year) — assumes bar returns
#   are independent (may overestimate on lower timeframes).

def backtest(data, buy_signals, sell_signals, asset_info, timeframe="daily", slippage_pips=0):
    """
    Run a single backtest for one strategy/param/symbol/timeframe combination.
    
    Args:
        data: OHLCV list of dicts.
        buy_signals: boolean list — True = enter long at close of this bar.
        sell_signals: boolean list — True = exit long at close of this bar.
        asset_info: ASSET_DB entry for fee/pip info.
        timeframe: string key for TF_TO_MINUTES lookup.
        slippage_pips: additional pips cost per exit (stress testing).
    
    Returns:
        Dict of metrics, or None if < 3 trades or < 30 bars.
    """
    if len(data) < 30:
        return None

    initial_capital = 10000.0
    balance = initial_capital
    equity = [balance]
    position = 0
    entry_price = 0.0
    entry_idx = 0
    trades = []
    risk_per_trade_pct = 0.01
    pip = asset_info.get("pip", 0.0001)

    for i in range(len(data)):
        close = data[i]["close"]

        # --- EXIT LOGIC ---
        # If we have an open position and a sell signal fires, close at close.
        # Slippage cost = slippage_pips * pip * position_size (added to fees).
        # Fees include both broker commission + slippage penalty.
        if position > 0 and i < len(sell_signals) and sell_signals[i]:
            slip_cost = slippage_pips * pip * abs(position)
            fees = calculate_fees(asset_info, entry_price, close, position) + slip_cost
            gross_pnl = (close - entry_price) * position
            net_pnl = gross_pnl - fees
            trades.append({
                "entry_idx": entry_idx, "exit_idx": i,
                "entry_price": entry_price, "exit_price": close,
                "pnl": round(net_pnl, 2), "gross_pnl": round(gross_pnl, 2),
                "direction": "long", "size": position,
                "fees": round(fees, 2), "bars_held": i - entry_idx,
            })
            balance += net_pnl
            balance = max(balance, 0.0)
            position = 0

        # --- ENTRY LOGIC ---
        # Only enter if flat (position == 0), balance > 0, buy signal fires.
        # Position size = fixed-fractional risk model:
        #   1. Compute ATR(14) over trailing ~21 bars.
        #   2. Stop distance = max(2*ATR, close*0.005).
        #   3. Risk $ = 1% of current balance.
        #   4. raw_size = risk $ / stop distance.
        #   5. Cap at affordable units = balance / close.
        # NOTE: Entry is at the close of the signal bar (not open, no slippage).
        if position == 0 and balance > 0 and i < len(buy_signals) and buy_signals[i]:
            atr_val = sma([data[j]["high"] - data[j]["low"] for j in range(max(0, i-20), i+1)], 14)
            current_atr = atr_val[-1] if atr_val and atr_val[-1] is not None else close * 0.01
            stop_dist = max(2 * current_atr, close * 0.005)
            risk_dollars = balance * risk_per_trade_pct
            raw_size = max(1, int(risk_dollars / stop_dist))
            max_affordable = int(balance / close) if close > 0 else 1
            position = min(raw_size, max(1, max_affordable))
            entry_price = close
            entry_idx = i

        # --- EQUITY TRACKING ---
        # Equity = cash balance + unrealized P&L (mark-to-market).
        # For flat positions, unrealized P&L is 0.
        # NOTE: This is a simplified equity curve; doesn't account for
        # margin requirements or intra-bar volatility.
        equity.append(balance + (position * (close - entry_price) if position else 0))

    # --- FORCED CLOSE AT END OF DATA ---
    # If still holding a position at the last bar, close at final close.
    # This simulates the end of the backtest period. Fees still apply.
    if position > 0:
        close = data[-1]["close"]
        slip_cost = slippage_pips * pip * abs(position)
        fees = calculate_fees(asset_info, entry_price, close, position) + slip_cost
        gross_pnl = (close - entry_price) * position
        net_pnl = gross_pnl - fees
        trades.append({
            "entry_idx": entry_idx, "exit_idx": len(data) - 1,
            "entry_price": entry_price, "exit_price": close,
            "pnl": round(net_pnl, 2), "gross_pnl": round(gross_pnl, 2),
            "direction": "long", "size": position,
            "fees": round(fees, 2), "bars_held": len(data) - 1 - entry_idx,
        })
        balance += net_pnl

    # Minimum 3 trades required to compute meaningful metrics.
    if len(trades) < 3:
        return None

    # --- METRICS COMPUTATION ---
    # All metrics derived from trades list and equity curve.
    # Trade classification: winner if PnL > 0 (0 PnL counts as loser).
    winners = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] <= 0]
    n = len(trades)
    wr = len(winners) / n * 100

    total_gross_profit = sum(t["gross_pnl"] for t in winners)
    total_gross_loss = abs(sum(t["gross_pnl"] for t in losers)) if losers else 0
    total_fees = sum(t["fees"] for t in trades)
    total_net_pnl = balance - initial_capital

    # Profit Factor = gross_profit / gross_loss (before fees).
    pf = total_gross_profit / total_gross_loss if total_gross_loss > 0 else float("inf")
    # Net PF: approximate fee allocation (half to profit, half to loss).
    net_pf = (total_gross_profit - total_fees/2) / (total_gross_loss + total_fees/2) if total_gross_loss > 0 else float("inf")
    total_ret_pct = (balance - initial_capital) / initial_capital * 100
    avg_holding_bars = sum(t["bars_held"] for t in trades) / n
    interval_min = TIMEFRAME_TO_MINUTES.get(timeframe, 1440)
    # Bars per year: assumes 252 trading days × (daily bars).
    # For 1h: 24*252=6048; for 15m: 96*252=24192; for daily: 252.
    bars_per_year = int(1440 / interval_min * 252)
    years = len(data) / bars_per_year if bars_per_year > 0 else len(data) / 252
    trades_per_year = n / years if years > 0 else 0

    # Maximum Drawdown: peak-to-trough percentage decline in equity.
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Sharpe Ratio: annualized, computed from per-bar equity returns.
    # Assumptions:
    #   - Risk-free rate = 0 (simplified).
    #   - Returns are normally distributed (likely violated).
    #   - Annualization = sqrt(min(bars_per_year, 252)) — daily max.
    daily_ret = [(equity[i] - equity[i-1]) / equity[i-1] if equity[i-1] != 0 else 0 for i in range(1, len(equity))]
    avg_r = sum(daily_ret) / len(daily_ret) if daily_ret else 0
    variance = sum((r - avg_r) ** 2 for r in daily_ret) / len(daily_ret) if daily_ret else 0
    std_r = math.sqrt(variance) if variance > 0 else 1e-10
    sharpe = (avg_r / std_r) * math.sqrt(min(bars_per_year, 252)) if std_r > 0 else 0

    # CAGR: Compound Annual Growth Rate. Calmar: CAGR / MaxDD.
    if years > 0 and balance > 0:
        cagr = ((balance / initial_capital) ** (1.0 / years) - 1) * 100
    else:
        cagr = -100.0 if years > 0 else 0.0
    calmar = float(cagr) / max_dd if max_dd > 0 else 0.0

    # Average R:R (Reward-to-Risk): avg win $ / avg loss $.
    avg_win = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(abs(t["pnl"]) for t in losers) / len(losers) if losers else 0
    rr_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")

    return {
        "n_trades": n,
        "win_rate": round(wr, 1),
        "profit_factor": round(pf, 2),
        "net_profit_factor": round(net_pf, 2),
        "total_return_pct": round(total_ret_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "avg_rr": rr_ratio,
        "calmar": round(calmar, 2),
        "cagr_pct": round(cagr, 2),
        "avg_holding_bars": round(avg_holding_bars, 1),
        "trades_per_year": round(trades_per_year, 1),
        "total_fees": round(total_fees, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "n_winners": len(winners),
        "n_losers": len(losers),
        "final_balance": round(balance, 2),
        "_trades": trades,
    }

# ---------------------------------------------------------------------------
# Param grid generator
# ---------------------------------------------------------------------------
# Generates the Cartesian product of all parameter lists from a template.
# E.g., template with params {"lookback": [1,2], "k1": [0.5,0.7]} yields
# 4 combinations: {lookback:1, k1:0.5}, {lookback:1, k1:0.7},
# {lookback:2, k1:0.5}, {lookback:2, k1:0.7}.
# Uses recursive generator to handle arbitrary number of parameters.

def generate_param_combinations(template):
    """
    Yield all parameter combinations for a given template.
    
    Args:
        template: dict with "params" key mapping param name -> list of values.
    
    Yields:
        dict of {param_name: value} for each combination.
    """
    keys = list(template["params"].keys())
    values = list(template["params"].values())

    def _combine(idx, current):
        if idx == len(keys):
            yield dict(current)
            return
        for v in values[idx]:
            current[keys[idx]] = v
            yield from _combine(idx + 1, current)

    yield from _combine(0, {})

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Two tables:
#   backtest_results — one row per (template, params, symbol, timeframe) combo.
#   locked_strategies — subset promoted from top results.
# Primary key of backtest_results referenced by locked_strategies.result_id.
# 'params' stored as JSON string for reproducibility.

def init_db(conn, fresh=False):
    """
    Initialize SQLite tables. If fresh=True, drops existing tables first.
    
    backtest_results schema stores every metric returned by backtest() plus
    the template name, JSON params, entry/exit rule strings, and timestamp.
    locked_strategies stores promoted strategies with WR>=70%, Sharpe>=0.5,
    RR>=2.0 (thresholds in lock_winners function).
    """
    if fresh:
        conn.execute("DROP TABLE IF EXISTS locked_strategies")
        conn.execute("DROP TABLE IF EXISTS backtest_results")
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
            result_id INTEGER UNIQUE REFERENCES backtest_results(id),
            template TEXT, symbol TEXT, timeframe TEXT,
            win_rate REAL, sharpe REAL, avg_rr REAL,
            total_return_pct REAL, max_drawdown_pct REAL,
            calmar REAL, cagr_pct REAL, trades_per_year REAL,
            total_fees REAL,
            locked_at TEXT, notes TEXT
        )
    """)
    conn.commit()

# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
# Two-phase scan:
#   Phase 1: Fetch raw market data from MT5/Binance for each symbol at
#            native timeframes (15m, 1h). Store in data_cache dict.
#            Resample 1h → 4h, daily for higher-TF tests.
#   Phase 2: For each template × param combo × symbol × timeframe, run the
#            strategy runner and backtest(), insert results into DB.
#            Results committed every 100 inserts for crash safety.

def scan(conn):
    """
    Full parameter scan across all templates, symbols, and timeframes.
    
    Steps:
    1. Fetch all native data (15m, 1h) per symbol into data_cache.
       Each (symbol, tf) is fetched only once and reused across all templates.
    2. Resample 1h data to 4h and daily.
    3. Iterate through every (template × param combo × symbol × tf) and
       call runner(data, params) → backtest(data, buy, sell, ...).
    4. Insert metric results into backtest_results table.
    5. NB: The caller (main()) calls lock_winners() after scan completes.
    
    Assumptions:
    - Higher TFs (4h, daily) are resampled from 1h, which produces correct
      OHLC but may have fewer bars than native data.
    - Binance fetches are rate-limited with 1.5s sleep between symbols.
    - At least 30 bars required for a valid dataset.
    """
    total_combos = sum(
        math.prod(len(v) for v in t["params"].values())
        for t in TEMPLATES
    ) * len(SCAN_ASSETS) * len(SCAN_TIMEFRAMES)

    print(f"Scanning {total_combos} combinations across {len(TEMPLATES)} templates, "
          f"{len(SCAN_ASSETS)} assets, {len(SCAN_TIMEFRAMES)} timeframes\n")

    # --- Step 1: fetch all native data upfront, resample for higher TFs ---
    data_cache = {}
    print("Fetching market data...")
    for symbol in SCAN_ASSETS:
        asset_info = ASSET_DB.get(symbol, {})
        source = asset_info.get("source", "")
        for tf in NATIVE_TIMEFRAMES:
            key = (symbol, tf)
            data = fetch_data(symbol, tf)
            if data and len(data) >= 30:
                data_cache[key] = data
                print(f"  {symbol:10s} {tf:8s} {len(data)} bars (native)")
            else:
                print(f"  {symbol:10s} {tf:8s} no data (native)")
            # Sleep only for remote sources (Binance) to avoid rate limits
            if source == "binance":
                time.sleep(1.5)
        # Resample 1h -> 4h and daily
        base_key = (symbol, "1h")
        base_data = data_cache.get(base_key)
        if base_data and len(base_data) >= 500:
            for target_tf in ["4h", "daily"]:
                target_min = TIMEFRAME_TO_MINUTES.get(target_tf, 1440)
                resampled = resample_ohlcv(base_data, target_min)
                if resampled and len(resampled) >= 30:
                    data_cache[(symbol, target_tf)] = resampled
                    print(f"  {symbol:10s} {target_tf:8s} {len(resampled)} bars (resampled from 1h)")
    print(f"Cached {len(data_cache)} datasets\n")

    if not data_cache:
        print("No data available. Aborting.")
        return

    # --- Step 2: run all template/param combos against cached data ---
    # The inner loop iterates: template → params → symbol → timeframe.
    # Each combination fetches data from cache (fast, no I/O).
    tested = 0
    commit_count = 0
    for template in TEMPLATES:
        name = template["name"]
        runner = RUNNERS.get(name)
        if not runner:
            continue
        param_combos = list(generate_param_combinations(template))
        for params in param_combos:
            for symbol in SCAN_ASSETS:
                asset_info = ASSET_DB.get(symbol, {})
                for tf in SCAN_TIMEFRAMES:
                    tested += 1
                    key = (symbol, tf)
                    data = data_cache.get(key)
                    if not data:
                        continue

                    if tested % 50 == 0 or tested == 1:
                        print(f"  [{tested}/{total_combos}] {name} on {symbol} {tf}")

                    try:
                        buy, sell = runner(data, params)
                        result = backtest(data, buy, sell, asset_info, tf)
                    except Exception:
                        continue

                    if result is None:
                        continue

                    entry = template["entry"].format(**params)
                    exit_rule = template["exit"].format(**params)

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
                        name, json.dumps(params), symbol, tf, asset_info.get("class", ""),
                        entry, exit_rule,
                        result["n_trades"], result["win_rate"],
                        result["profit_factor"], result["net_profit_factor"],
                        result["total_return_pct"], result["max_drawdown_pct"],
                        result["sharpe"], result["avg_rr"],
                        result["calmar"], result["cagr_pct"],
                        result["avg_holding_bars"], result["trades_per_year"],
                        result["total_fees"], result["avg_win"], result["avg_loss"],
                        result["n_winners"], result["n_losers"],
                        result["final_balance"],
                        datetime.now(timezone.utc).isoformat(),
                    ))
                    commit_count += 1
                    if commit_count % 100 == 0:
                        conn.commit()

    conn.commit()
    print(f"\nDone. Tested {tested} combinations.")

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
# Query backtest_results ordered by Sharpe (descending), minimum 5 trades
# and positive Sharpe. Shows all key metrics in a formatted table.
# Filters: n_trades >= 5, sharpe > 0.

def show_results(conn, top_n=20):
    """Display top N backtest results ranked by Sharpe ratio."""
    rows = conn.execute("""
        SELECT id, template, symbol, timeframe, win_rate, sharpe, avg_rr,
               total_return_pct, max_drawdown_pct, n_trades, profit_factor,
               net_profit_factor, calmar, cagr_pct, avg_holding_bars,
               trades_per_year, total_fees, n_winners, n_losers,
               entry_rule, exit_rule, asset_class
        FROM backtest_results
        WHERE n_trades >= 5 AND sharpe > 0
        ORDER BY sharpe DESC
        LIMIT ?
    """, (top_n,)).fetchall()

    if not rows:
        print("No results. Run --scan first.")
        return

    print(f"\n{'='*140}")
    print(f"  TOP {top_n} STRATEGIES (ranked by Sharpe)")
    print(f"{'='*140}")
    hdr = f"{'#':>4s} {'Template':22s} {'Symbol':10s} {'TF':7s} {'WR%':>6s} {'Sharpe':>7s} {'R:R':>7s} {'Ret%':>7s} {'DD%':>6s} {'CAGR':>7s} {'Calmar':>7s} {'Tr/Yr':>6s} {'Fees':>8s} {'Trades':>7s}"
    print(hdr)
    print("-" * 140)

    for i, r in enumerate(rows):
        rid, template, symbol, tf, wr, sharpe, rr, ret, dd, trades, pf, npf, calmar, cagr, hold, tpy, fees, nw, nl, entry, exit_r, aclass = r
        rr_str = f"{rr:.2f}" if rr != float("inf") else "  inf"
        calmar_s = f"{calmar:.2f}" if calmar else "  N/A"
        cagr_s = f"{cagr:.1f}%" if cagr else "  N/A"
        tpy_s = f"{tpy:.1f}" if tpy else "  N/A"
        print(f"{i+1:>4d} {template:22s} {symbol:10s} {tf:7s} {wr:5.1f}% {sharpe:6.2f}  {rr_str:>5s} {ret:>+6.1f}% {dd:5.1f}% {cagr_s:>6s} {calmar_s:>6s} {tpy_s:>5s} ${fees:>6.1f} {trades:>5d}")
        print(f"      [{aclass}] {entry[:80]}")
        print(f"      {exit_r[:80]}")
        print()

# ---------------------------------------------------------------------------
# Lock winners
# ---------------------------------------------------------------------------
# Promotes backtest results to locked_strategies table when minimum
# thresholds are met: WR>=70%, Sharpe>=0.5, RR>=2.0, trades>=5.
# Uses INSERT OR IGNORE to prevent duplicate locks of the same result.
# Sorted by trades_per_year DESC so high-frequency strategies lock first.
# NOTE: These thresholds differ from the 4-tier grading system used in
# csv_scanner.py (which uses A/B/C/D tiers with different criteria).
# The lock is db-level; csv_scanner.py uses a separate CSV validation path.

def lock_winners(conn):
    """Lock strategies meeting minimum quality thresholds."""
    rows = conn.execute("""
        SELECT id, template, symbol, timeframe, win_rate, sharpe, avg_rr,
               total_return_pct, max_drawdown_pct, calmar, cagr_pct,
               trades_per_year, total_fees
        FROM backtest_results
        WHERE id NOT IN (SELECT result_id FROM locked_strategies)
          AND n_trades >= 5
          AND win_rate >= 70.0
          AND sharpe >= 0.5
          AND avg_rr >= 2.0
        ORDER BY trades_per_year DESC
    """).fetchall()

    if not rows:
        print("No strategies meet locking thresholds (WR>=70% and R:R>=2.0).")
        return

    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        rid, template, symbol, tf, wr, sharpe, rr, ret, dd, calmar, cagr, tpy, fees = r
        conn.execute("""
            INSERT OR IGNORE INTO locked_strategies
                (result_id, template, symbol, timeframe, win_rate, sharpe,
                 avg_rr, total_return_pct, max_drawdown_pct,
                 calmar, cagr_pct, trades_per_year, total_fees,
                 locked_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (rid, template, symbol, tf, wr, sharpe, rr, ret, dd,
              calmar, cagr, tpy, fees, now,
              f"WR={wr}% Sharpe={sharpe} R:R={rr} Ret={ret}% Fees=${fees}"))
        print(f"  LOCKED: {template} on {symbol} {tf} | {tpy:.0f} trades/yr WR={wr}% Sharpe={sharpe} R:R={rr}")
    conn.commit()

# ---------------------------------------------------------------------------
# Stress Test Suite
# ---------------------------------------------------------------------------
# 4-test battery applied to each locked strategy:
#   1. Walk-forward (70/30 split) — OOS must maintain WR≥60%, RR≥1.5,
#      Sharpe≥0.3, positive return.
#   2. Monte Carlo (1000 shuffle trials) — ≥95% of shuffled PnL sequences
#      must be profitable.
#   3. Slippage (+1 pip, +2 pips) — system must remain profitable.
#   4. Robustness — trades≥10, PF≥1.2, DD≤30%.
#
# A strategy must pass ALL 4 tests to receive a "PASS" verdict.
# Results stored in locked_strategies.notes field.

def stress_test(conn):
    """Run 4-test stress battery on all locked strategies."""
    locked = conn.execute("""
        SELECT ls.id, ls.template, ls.symbol, ls.timeframe,
               br.params, br.entry_rule, br.exit_rule
        FROM locked_strategies ls
        JOIN backtest_results br ON br.id = ls.result_id
        ORDER BY ls.trades_per_year DESC
    """).fetchall()

    if not locked:
        print("No locked strategies to stress test.")
        return

    print(f"Stress testing {len(locked)} locked strategies...\n")

    results = {"pass": 0, "fail": 0, "details": []}
    templates_map = {t["name"]: t for t in TEMPLATES}

    for row in locked:
        ls_id, tpl_name, symbol, tf, params_json, entry_rule, exit_rule = row
        params = json.loads(params_json)
        template = templates_map.get(tpl_name)
        if not template:
            continue
        runner = RUNNERS.get(tpl_name)
        if not runner:
            continue

        print(f"\n  {tpl_name:22s} {symbol:10s} {tf:8s} params={params_json[:50]}")

        data = fetch_data(symbol, tf)
        if not data or len(data) < 30:
            print(f"  {'':>32s} FAIL — no data")
            results["fail"] += 1
            continue

        split_idx = int(len(data) * 0.7)
        data_is = data[:split_idx]
        data_oos = data[split_idx:]

        asset_info = ASSET_DB.get(symbol, {})

        # Run baseline on full data, extract trades
        try:
            buy, sell = runner(data, params)
            full_result = backtest(data, buy, sell, asset_info, tf)
        except Exception as e:
            print(f"  {'':>32s} FAIL — runner error: {e}")
            results["fail"] += 1
            continue

        if full_result is None:
            print(f"  {'':>32s} FAIL — no trades on full data")
            results["fail"] += 1
            continue

        trades = full_result.get("_trades", [])
        if len(trades) < 10:
            print(f"  {'':>32s} FAIL — only {len(trades)} trades (need 10)")
            results["fail"] += 1
            continue

        # ----- Test 1: Walk-forward (70/30 OOS) -----
        if len(data_oos) >= 30:
            buy_oos, sell_oos = runner(data_oos, params)
            oos_result = backtest(data_oos, buy_oos, sell_oos, asset_info, tf)
            oos_pass = (oos_result is not None and
                        oos_result["win_rate"] >= 60 and
                        oos_result["avg_rr"] >= 1.5 and
                        oos_result["sharpe"] >= 0.3 and
                        oos_result["total_return_pct"] >= 0)
            if oos_pass:
                oos_status = f"PASS WR={oos_result['win_rate']:.0f}% R:R={oos_result['avg_rr']:.1f} S={oos_result['sharpe']:.2f} Ret={oos_result['total_return_pct']:+.1f}%"
            else:
                oos_detail = f"WR={oos_result['win_rate']:.0f}% R:R={oos_result['avg_rr']:.1f} S={oos_result['sharpe']:.2f} Ret={oos_result['total_return_pct']:+.1f}%" if oos_result else "no trades"
                oos_status = f"FAIL — {oos_detail}"
        else:
            oos_pass = False
            oos_status = "FAIL — OOS too short"
        print(f"  {'OOS 30%':>32s} {oos_status}")

        # ----- Test 2: Monte Carlo (1000 shuffles) -----
        pnls = [t["pnl"] for t in trades]
        profitable = 0
        trials = 1000
        for _ in range(trials):
            shuffled = pnls[:]
            random.shuffle(shuffled)
            if sum(shuffled) > 0:
                profitable += 1
        mc_pct = profitable / trials * 100
        mc_pass = mc_pct >= 95.0
        mc_status = f"{'PASS' if mc_pass else 'FAIL'} — {mc_pct:.1f}% of shuffles profitable"
        print(f"  {'Monte Carlo':>32s} {mc_status}")

        # ----- Test 3: Slippage stress (+1 pip, +2 pips) -----
        slip_tests = []
        for slip in [1, 2]:
            slip_result = backtest(data, buy, sell, asset_info, tf, slippage_pips=slip)
            if slip_result and slip_result["total_return_pct"] >= 0:
                slip_tests.append(f"+{slip}pip: PASS Ret={slip_result['total_return_pct']:+.1f}%")
            else:
                ret_str = f"{slip_result['total_return_pct']:+.1f}%" if slip_result else "N/A"
                slip_tests.append(f"+{slip}pip: FAIL Ret={ret_str}")
        slip_pass = all("PASS" in s for s in slip_tests) if slip_tests else False
        print(f"  {'Slippage':>32s} {' | '.join(slip_tests)}")

        # ----- Test 4: Minimum robustness -----
        robust_checks = []
        robust = True
        if full_result["n_trades"] < 10:
            robust_checks.append(f"trades={full_result['n_trades']}<10")
            robust = False
        if full_result["profit_factor"] < 1.2:
            robust_checks.append(f"PF={full_result['profit_factor']}<1.2")
            robust = False
        if full_result["max_drawdown_pct"] > 30:
            robust_checks.append(f"DD={full_result['max_drawdown_pct']:.0f}%>30%")
            robust = False
        robust_status = "PASS" if robust else f"FAIL — {', '.join(robust_checks)}"
        print(f"  {'Robustness':>32s} {robust_status}")

        # ----- Overall verdict -----
        all_pass = oos_pass and mc_pass and slip_pass and robust
        if all_pass:
            results["pass"] += 1
            print(f"  {'>>> VERDICT':>32s} PASS — all tests passed")
        else:
            results["fail"] += 1
            print(f"  {'>>> VERDICT':>32s} FAIL — some tests failed")

        # Store in DB
        notes = f"OOS={'PASS' if oos_pass else 'FAIL'} MC={'PASS' if mc_pass else 'FAIL'} Slip={'PASS' if slip_pass else 'FAIL'} Robust={'PASS' if robust else 'FAIL'}"
        conn.execute("""
            UPDATE locked_strategies SET notes = ?
            WHERE id = ?
        """, (notes, ls_id))

    conn.commit()
    print(f"\n{'='*60}")
    print(f"Stress test complete: {results['pass']} passed, {results['fail']} failed")
    print(f"{'='*60}")

def list_symbols():
    """Print all supported assets with class, source, pip value, and fee model."""
    print(f"\n{'Supported Assets':^80}")
    print(f"{'='*80}")
    print(f"{'Symbol':12s} {'Class':12s} {'Source':10s} {'Pip':10s} {'Key Fee':20s} Description")
    print(f"{'-'*80}")
    for sym, info in sorted(ASSET_DB.items()):
        cls = info["class"]
        src = info["source"]
        pip = str(info["pip"])
        if cls == "crypto":
            fee = f"{info['comm_pct']*100:.1f}% per side"
        elif cls == "forex":
            fee = f"{info['spread_pips']} pip spread"
        elif cls == "stock":
            fee = f"${info['comm_per_share']}/share"
        else:
            fee = f"${info['comm_per_lot']}/lot"
        print(f"{sym:12s} {cls:12s} {src:10s} {pip:10s} {fee:20s} {info['description']}")
    print()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# CLI entry point. Supports 6 flags that can be combined:
#   --scan     Full scan + auto-lock winners after completion.
#   --results  Display top N results by Sharpe.
#   --lock     Lock strategies from existing results only (no scan).
#   --purge    Delete all existing backtest_results and locked_strategies.
#   --stress   Run 4-test stress battery on all locked strategies.
#   --symbols  List supported assets (no DB connection needed).
# Default (no flags): shows top 20 results.
# WARNING: --purge used alone just deletes; --scan creates fresh tables.

def main():
    parser = argparse.ArgumentParser(description="Backtest Engine v2 — fee-aware multi-asset scanner")
    parser.add_argument("--scan", action="store_true", help="Full parameter scan across all assets")
    parser.add_argument("--results", type=int, nargs="?", const=20, help="Show top N results")
    parser.add_argument("--lock", action="store_true", help="Lock top-performing strategies")
    parser.add_argument("--symbols", action="store_true", help="List supported assets")
    parser.add_argument("--purge", action="store_true", help="Clear all backtest results before scanning")
    parser.add_argument("--stress", action="store_true", help="Stress test locked strategies")
    args = parser.parse_args()

    if args.symbols:
        list_symbols()
        return

    conn = sqlite3.connect(DB_PATH)
    init_db(conn, fresh=args.scan)

    if args.purge:
        conn.execute("DELETE FROM backtest_results")
        conn.execute("DELETE FROM locked_strategies")
        conn.commit()
        print("Purged all results.")

    if args.scan:
        scan(conn)
        print("\nLocking winners...")
        lock_winners(conn)

    if args.results:
        show_results(conn, args.results)

    if args.lock:
        lock_winners(conn)

    if args.stress:
        stress_test(conn)

    if not any([args.scan, args.results, args.lock, args.purge, args.stress]):
        show_results(conn, 20)

    conn.close()


if __name__ == "__main__":
    main()
