"""
Live bot: yfinance + Binance CCXT data → FX Pesa MT5 execution.
Runs all A-Exceeding strategies. Next-bar execution, hard SL, 0.25% risk.
MT5 terminal must be open and logged into FX Pesa.
"""
import os, sys, time, json, logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_engine import RUNNERS

log = logging.getLogger("live_bot")

# ---------------------------------------------------------------------------
# Data: Yahoo Finance (forex/metals)
# ---------------------------------------------------------------------------
import yfinance as yf
import pandas as pd

class YFData:
    """Fetch OHLC from Yahoo Finance."""
    TICKERS = {
        "EURUSD": "EURUSD=X",
        "XAUUSD": "GC=F",
        "XAGUSD": "SI=F",
    }
    TIMEFRAMES = {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h",
        "8h": "8h", "12h": "12h", "1d": "1d",
    }

    def fetch(self, symbol, timeframe, limit=200):
        yf_ticker = self.TICKERS.get(symbol)
        if not yf_ticker:
            return None
        yf_tf = self.TIMEFRAMES.get(timeframe, "1h")
        try:
            df = yf.download(yf_ticker, period="1mo", interval=yf_tf, progress=False)
        except Exception as e:
            log.warning("yfinance %s: %s", yf_ticker, e)
            return None
        if df is None or len(df) == 0:
            log.warning("yfinance %s: no data returned", yf_ticker)
            return None
        # Handle MultiIndex columns from yfinance
        if isinstance(df.columns, pd.MultiIndex):
            data = []
            for idx, row in df.iterrows():
                data.append({
                    "open": float(row[("Open", yf_ticker)]),
                    "high": float(row[("High", yf_ticker)]),
                    "low": float(row[("Low", yf_ticker)]),
                    "close": float(row[("Close", yf_ticker)]),
                    "volume": int(row[("Volume", yf_ticker)]) if ("Volume", yf_ticker) in row else 0,
                    "time": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
                })
        else:
            data = []
            for idx, row in df.iterrows():
                data.append({
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]) if "Volume" in row else 0,
                    "time": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
                })
        return data[-limit:]

# ---------------------------------------------------------------------------
# Data: Binance CCXT (crypto)
# ---------------------------------------------------------------------------
class BinanceData:
    TIMEFRAMES = {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h",
        "8h": "8h", "12h": "12h", "1d": "1d",
    }

    def __init__(self):
        import ccxt
        self.ex = ccxt.binance({"enableRateLimit": True})

    def fetch(self, symbol, timeframe, limit=200):
        tf = self.TIMEFRAMES.get(timeframe, "1m")
        pair = symbol.replace("USDT", "/USDT")
        try:
            raw = self.ex.fetch_ohlcv(pair, tf, limit=limit)
        except Exception:
            return None
        return [{"open": r[1], "high": r[2], "low": r[3], "close": r[4],
                 "volume": r[5], "time": str(r[0])} for r in raw]

# ---------------------------------------------------------------------------
# Execution: MetaTrader5 → FX Pesa
# ---------------------------------------------------------------------------
class MT5Exec:
    def __init__(self):
        self.ready = False

    def connect(self):
        try:
            import MetaTrader5 as mt5
            self.mt5 = mt5
        except ImportError:
            log.error("MetaTrader5 not installed: pip install MetaTrader5")
            return False
        if not mt5.initialize():
            log.error("MT5 init: %s", mt5.last_error())
            return False
        self.ready = True
        a = mt5.account_info()
        if a:
            log.info("MT5 account: %d %s bal=%.2f eq=%.2f",
                     a.login, a.server, a.balance, a.equity)
        else:
            log.warning("MT5 connected but no account info")
        return True

    def balance(self):
        if not self.ready: return 10000
        a = self.mt5.account_info()
        return a.balance if a else 10000

    def price(self, symbol):
        t = self.mt5.symbol_info_tick(symbol)
        return (t.ask, t.bid) if t else (None, None)

    def position_ticket(self, symbol, magic):
        for p in self.mt5.positions_get(symbol=symbol) or []:
            if p.magic == magic:
                return p.ticket
        return 0

    def open_buy(self, symbol, lots, sl_price, magic, comment="HA"):
        ask, _ = self.price(symbol)
        if not ask: return 0
        req = {
            "action": self.mt5.TRADE_ACTION_DEAL, "symbol": symbol,
            "volume": lots, "type": self.mt5.ORDER_TYPE_BUY,
            "price": ask, "sl": sl_price, "tp": 0,
            "deviation": 10, "magic": magic,
            "comment": comment, "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
        }
        r = self.mt5.order_send(req)
        if r and r.retcode == self.mt5.TRADE_RETCODE_DONE:
            log.info("OPEN %s #%d @%.5f SL=%.5f lot=%.2f", symbol, r.order, ask, sl_price, lots)
            return r.order
        log.warning("OPEN FAIL %s: retcode=%d", symbol, r.retcode if r else -1)
        return 0

    def close(self, ticket):
        p = self.mt5.positions_get(ticket=ticket)
        if not p or len(p) == 0: return True
        p = p[0]
        _, bid = self.price(p.symbol)
        price = bid if p.type == 0 else self.mt5.symbol_info_tick(p.symbol).ask
        if not price: return False
        req = {
            "action": self.mt5.TRADE_ACTION_DEAL, "symbol": p.symbol,
            "volume": p.volume,
            "type": self.mt5.ORDER_TYPE_SELL if p.type == 0 else self.mt5.ORDER_TYPE_BUY,
            "position": p.ticket, "price": price,
            "deviation": 10, "magic": p.magic,
            "comment": "Close", "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
        }
        r = self.mt5.order_send(req)
        if r and r.retcode == self.mt5.TRADE_RETCODE_DONE:
            log.info("CLOSE #%d %s", p.ticket, p.symbol)
            return True
        log.warning("CLOSE FAIL #%d: retcode=%d", p.ticket, r.retcode if r else -1)
        return False

    def calc_lots(self, symbol, risk_amount, stop_dist):
        info = self.mt5.symbol_info(symbol)
        if not info: return 0
        sl_ticks = stop_dist / info.trade_tick_size
        if sl_ticks < 1: return 0
        loss_per_lot = sl_ticks * info.trade_tick_value
        if loss_per_lot <= 0: return 0
        lots = risk_amount / loss_per_lot
        step = info.volume_step
        if step > 0: lots = (lots // step) * step
        return max(info.volume_min, min(info.volume_max, lots))

    def shutdown(self):
        if self.ready:
            self.mt5.shutdown()

# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
class Strategy:
    def __init__(self, symbol, exchange, mt5_symbol, timeframe, template, params,
                 data_source, risk_pct=0.0025, atr_mult=2.0, min_stop_pct=0.005):
        self.symbol = symbol
        self.exchange = exchange  # "binance" or "yfinance"
        self.mt5_symbol = mt5_symbol
        self.timeframe = timeframe
        self.template = template
        self.params = params if isinstance(params, dict) else json.loads(params)
        self.data_source = data_source
        self.risk_pct = risk_pct
        self.atr_mult = atr_mult
        self.min_stop_pct = min_stop_pct
        self.magic = 20260708 + hash(f"{symbol}_{template}_{json.dumps(params, sort_keys=True)}") % 10000
        self.runner = RUNNERS[template]
        self.pending_entry = False
        self.pending_exit = False
        self.last_bar_key = None
        self.last_entry_time = 0  # for cooldown

    def __repr__(self):
        return f"{self.symbol} {self.timeframe} {self.template} {self.params}"

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class LiveBot:
    def __init__(self, config_path="config.json"):
        with open(config_path) as f:
            cfg = json.load(f)
        self.cfg = cfg
        self.risk_pct = cfg.get("risk_pct", 0.0025)
        self.poll_seconds = cfg.get("poll_seconds", 30)

        # Data sources
        self.yf = YFData()
        self.binance = BinanceData()

        # Execution
        self.mt5 = MT5Exec()

        # Strategies
        self.strategies = []
        for s in cfg.get("strategies", []):
            ds = self.yf if s["data_source"] == "yfinance" else self.binance
            strat = Strategy(
                symbol=s["symbol"],
                exchange=s["data_source"],
                mt5_symbol=s.get("mt5_symbol", s["symbol"]),
                timeframe=s["timeframe"],
                template=s["template"],
                params=s["params"],
                data_source=ds,
                risk_pct=s.get("risk_pct", self.risk_pct),
            )
            self.strategies.append(strat)

        log.info("Loaded %d strategies", len(self.strategies))

    def calc_stop(self, data, price):
        n = min(14, len(data))
        if n < 3: return price * self.cfg.get("min_stop_pct", 0.005)
        atr = sum(data[-i]["high"] - data[-i]["low"] for i in range(1, n+1)) / n
        return max(self.cfg.get("atr_mult", 2.0) * atr, price * self.cfg.get("min_stop_pct", 0.005))

    def is_new_bar(self, strat, data):
        if not data: return False
        key = data[-1].get("time", "")
        if key == strat.last_bar_key: return False
        strat.last_bar_key = key
        return True

    def process(self, strat):
        now = time.time()

        # 1. Fetch data
        data = strat.data_source.fetch(strat.symbol, strat.timeframe, limit=200)
        if not data or len(data) < 10: return

        # 2. New bar?
        if not self.is_new_bar(strat, data): return

        # 3. Execute pending exit
        if strat.pending_exit:
            ticket = self.mt5.position_ticket(strat.mt5_symbol, strat.magic)
            if ticket:
                self.mt5.close(ticket)
            strat.pending_exit = False

        # 4. Execute pending entry (with cooldown)
        cooldown = self.cfg.get("cooldown_seconds", 0)
        if strat.pending_entry and (cooldown == 0 or now - strat.last_entry_time >= cooldown):
            if not self.mt5.position_ticket(strat.mt5_symbol, strat.magic):
                ask, _ = self.mt5.price(strat.mt5_symbol)
                if ask:
                    sd = self.calc_stop(data, ask)
                    sl = ask - sd
                    lots = self.mt5.calc_lots(strat.mt5_symbol,
                                              self.mt5.balance() * self.risk_pct, sd)
                    if lots > 0:
                        comment = f"{strat.template[:8]}_{list(strat.params.values())[0]}"
                        self.mt5.open_buy(strat.mt5_symbol, lots, sl, strat.magic, comment)
                        strat.last_entry_time = now
            strat.pending_entry = False
        elif strat.pending_entry and cooldown > 0 and now - strat.last_entry_time < cooldown:
            log.info("Cooldown %s: %.0fs remaining", strat, cooldown - (now - strat.last_entry_time))

        # 5. Generate signals on just-completed bar (data[-2])
        if len(data) < 3: return
        idx = len(data) - 2
        try:
            buy, sell = strat.runner(data, strat.params)
        except Exception as e:
            log.warning("Signal %s: %s", strat, e)
            return

        if idx >= len(buy) or idx >= len(sell): return
        has_pos = self.mt5.position_ticket(strat.mt5_symbol, strat.magic) != 0

        if has_pos and sell[idx]:
            strat.pending_exit = True
            log.info("SELL %s -> exit queued", strat)
        if not has_pos and buy[idx]:
            strat.pending_entry = True
            log.info("BUY %s -> entry queued", strat)

    def run(self):
        if not self.mt5.connect():
            log.error("MT5 connection failed. Start MT5 and log into FX Pesa.")
            return

        log.info("Bot running. %d strategies. Risk=%.2f%%/trade", len(self.strategies), self.risk_pct*100)
        try:
            while True:
                for strat in self.strategies:
                    try:
                        self.process(strat)
                    except Exception as e:
                        log.error("%s: %s", strat, e)
                time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            log.info("Shutdown")
        finally:
            self.mt5.shutdown()

# ---------------------------------------------------------------------------
# Config template
# ---------------------------------------------------------------------------
CONFIG_TEMPLATE = {
    "_instructions": "Fill in MT5 symbol mappings. Keep data_source as 'yfinance' or 'binance'.",
    "risk_pct": 0.0025,
    "max_agg_risk": 0.10,
    "atr_mult": 2.0,
    "min_stop_pct": 0.005,
    "poll_seconds": 30,
    "cooldown_seconds": 300,  # min seconds between entries (0 = no limit)
    "strategies": [
        {"symbol": "XAGUSD", "data_source": "yfinance", "mt5_symbol": "XAGUSD",
         "timeframe": "4h", "template": "Keltner Channel",
         "params": {"ema_period": 20, "atr_period": 14, "atr_mult": 1.5}},
        {"symbol": "XAUUSD", "data_source": "yfinance", "mt5_symbol": "XAUUSD",
         "timeframe": "1h", "template": "Keltner Channel",
         "params": {"ema_period": 20, "atr_period": 14, "atr_mult": 2.5}},
        {"symbol": "EURUSD", "data_source": "yfinance", "mt5_symbol": "EURUSD",
         "timeframe": "1h", "template": "ATR Channel",
         "params": {"channel_period": 10, "atr_mult": 3.0, "lookback": 20}},
        {"symbol": "XAGUSD", "data_source": "yfinance", "mt5_symbol": "XAGUSD",
         "timeframe": "4h", "template": "Awesome Oscillator",
         "params": {"fast_period": 5, "slow_period": 21}},
        {"symbol": "XAUUSD", "data_source": "yfinance", "mt5_symbol": "XAUUSD",
         "timeframe": "4h", "template": "Turtle",
         "params": {"entry_window": 20, "exit_window": 5}},
        {"symbol": "TRXUSDT", "data_source": "binance", "mt5_symbol": "TRXUSD.lv",
         "timeframe": "1m", "template": "Heikin-Ashi Momentum",
         "params": {"lookback": 1}},
        {"symbol": "TRXUSDT", "data_source": "binance", "mt5_symbol": "TRXUSD.lv",
         "timeframe": "1m", "template": "Heikin-Ashi Momentum",
         "params": {"lookback": 2}},
        {"symbol": "TRXUSDT", "data_source": "binance", "mt5_symbol": "TRXUSD.lv",
         "timeframe": "1m", "template": "Heikin-Ashi Momentum",
         "params": {"lookback": 3}},
    ]
}

def generate_config():
    p = "config.json"
    if os.path.exists(p):
        log.warning("%s exists, not overwriting", p)
        return
    with open(p, "w") as f:
        json.dump(CONFIG_TEMPLATE, f, indent=2)
    log.info("Created %s — edit mt5_symbol if FX Pesa uses different names", p)

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler()])

    if len(sys.argv) > 1 and sys.argv[1] == "--setup":
        generate_config()
        sys.exit(0)

    if not os.path.exists("config.json"):
        log.info("Run: python live_bot.py --setup")
        sys.exit(1)

    LiveBot().run()
