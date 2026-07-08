//+------------------------------------------------------------------+
//|                                              StrategyBot.mq5     |
//|                        Multi-strategy EA for MT5                 |
//+------------------------------------------------------------------+
#property copyright "Strategy Bot"
#property version   "1.00"
#property description "Implements top strategies from CSV scan"
#property description "1 - Heikin-Ashi Momentum (TRXUSDT 1m #1)"
#property description "2 - Awesome Oscillator (XAGUSD 4h)"
#property description "3 - Keltner Channel (XAUUSD 1h/XAGUSD 4h)"
#property description "4 - ATR Channel (EURUSD 1h)"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\AccountInfo.mqh>
#include <Trade\SymbolInfo.mqh>
#include <Indicators\Indicators.mqh>

input group "=== Strategy Selection ==="
input int      InpStrategy      = 1;           // 1=Heikin-Ashi, 2=Awesome Osc, 3=Keltner, 4=ATR Channel

input group "=== Heikin-Ashi Momentum ==="
input int      InpHA_Map        = 23;          // Load saved (0=none, 23=TRXUSDT1m)
input bool     InpHA_Reverse    = false;       // Reverse signals (hedge mode)
input int      InpHA_Lookback   = 2;           // Consecutive bull/bear bars (1-5)

input group "=== Keltner Channel ==="
input int      InpKC_EMA        = 20;          // EMA period
input int      InpKC_ATR        = 14;          // ATR period
input double   InpKC_Mult       = 2.5;         // ATR multiplier

input group "=== ATR Channel ==="
input int      InpATR_Period    = 10;          // Channel period
input double   InpATR_Mult      = 3.0;         // ATR multiplier
input int      InpATR_Lookback  = 10;          // New high/low lookback

input group "=== Risk Management ==="
input double   InpRiskPct       = 1.0;         // Risk % per trade
input double   InpStopLossATR   = 2.0;         // Stop loss in ATR
input double   InpTakeProfitRR  = 2.0;         // Take profit R:R ratio
input bool     InpUseTrailing   = false;       // Use trailing stop
input double   InpTrailActivate = 1.0;         // Trail activates at N*ATR profit
input double   InpTrailStep     = 0.5;         // Trail step in ATR

input group "=== Filter ==="
input int      InpMinSpread     = 0;           // Min spread in points (0=any)
input int      InpMaxSpread     = 100;         // Max spread in points
input ENUM_TIMEFRAMES InpTF     = PERIOD_CURRENT; // Chart TF override

CTrade         m_trade;
CPositionInfo  m_position;
CAccountInfo   m_account;
CSymbolInfo    m_symbol;

double m_atr_handle;
double m_ema_handle;
double m_fast_sma_handle;
double m_slow_sma_handle;
int    m_prev_bars;

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   m_symbol.Name(_Symbol);
   m_symbol.Refresh();
   
   if(InpMaxSpread > 0 && m_symbol.Spread() > InpMaxSpread)
   {
      Print("Spread too high: ", m_symbol.Spread());
      return INIT_FAILED;
   }
   
   m_trade.SetExpertMagicNumber(12345);
   m_prev_bars = 0;
   
   Print("StrategyBot initialized: ", EnumToString((ENUM_TIMEFRAMES)InpTF == PERIOD_CURRENT ? Period() : InpTF));
   Print("Strategy: ", InpStrategy, " Risk: ", InpRiskPct, "%");
   
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Comment("");
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
   ENUM_TIMEFRAMES tf = (InpTF == PERIOD_CURRENT) ? Period() : InpTF;
   int bars = Bars(_Symbol, tf);
   if(bars < 50) return;
   if(bars == m_prev_bars) return;
   m_prev_bars = bars;
   
   switch(InpStrategy)
   {
      case 1: RunHeikinAshi(tf); break;
      case 2: RunAwesomeOscillator(tf); break;
      case 3: RunKeltner(tf); break;
      case 4: RunATRChannel(tf); break;
   }
}

//+------------------------------------------------------------------+
//| Heikin-Ashi Momentum                                             |
//+------------------------------------------------------------------+
void RunHeikinAshi(ENUM_TIMEFRAMES tf)
{
   int n = 100;
   double ha_open[], ha_close[];
   ArraySetAsSeries(ha_open, true);
   ArraySetAsSeries(ha_close, true);
   ArrayResize(ha_open, n);
   ArrayResize(ha_close, n);
   
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   if(CopyRates(_Symbol, tf, 0, n, rates) < n) return;
   
   // Calculate Heikin-Ashi
   ha_close[0] = (rates[0].open + rates[0].high + rates[0].low + rates[0].close) / 4;
   ha_open[0] = rates[0].open;
   
   for(int i = 1; i < n; i++)
   {
      ha_close[i] = (rates[i].open + rates[i].high + rates[i].low + rates[i].close) / 4;
      ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2;
   }
   
   int lb = InpHA_Lookback;
   if(lb < 1) lb = 1;
   if(lb > 5) lb = 5;
   
   int i = 2;
   if(i >= n) return;
   
   // Check entry: consecutive bull/bear bars
   bool bull_streak = true;
   bool bear_streak = true;
   double body = MathAbs(ha_open[i] - ha_close[i]);
   double prev_body = MathAbs(ha_open[i-1] - ha_close[i-1]);
   bool body_expanding = body > prev_body && prev_body > 0;
   
   for(int j = i - lb + 1; j <= i; j++)
   {
      if(j < 0) continue;
      if(ha_open[j] <= ha_close[j]) bull_streak = false;
      if(ha_open[j] >= ha_close[j]) bear_streak = false;
   }
   
   bool bull_entry = (ha_open[i] > ha_close[i] && ha_open[i] >= rates[i].high - (rates[i].high - rates[i].low)*0.1
                      && body_expanding && bull_streak);
   bool bear_entry = (ha_open[i] < ha_close[i] && ha_open[i] <= rates[i].low + (rates[i].high - rates[i].low)*0.1
                      && body_expanding && bear_streak);
   
   // Check exit signal
   int j = 1;
   bool bull_exit = (ha_open[j] < ha_close[j] && ha_open[j] <= rates[j].low + (rates[j].high - rates[j].low)*0.1);
   bool bear_exit = (ha_open[j] > ha_close[j] && ha_open[j] >= rates[j].high - (rates[j].high - rates[j].low)*0.1);
   
   // Manage existing positions
   if(PositionSelect(_Symbol))
   {
      if((m_position.PositionType() == POSITION_TYPE_BUY && (bear_entry || bull_exit)) ||
         (m_position.PositionType() == POSITION_TYPE_SELL && (bull_entry || bear_exit)))
      {
         ClosePosition();
      }
      return;
   }
   
   // New entry
   if(bull_entry)
      EnterTrade(ORDER_TYPE_BUY);
   else if(bear_entry)
      EnterTrade(ORDER_TYPE_SELL);
}

//+------------------------------------------------------------------+
//| Awesome Oscillator                                                |
//+------------------------------------------------------------------+
void RunAwesomeOscillator(ENUM_TIMEFRAMES tf)
{
   int fast = 5, slow = 34;
   int n = slow + 10;
   
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   if(CopyRates(_Symbol, tf, 0, n, rates) < n) return;
   
   double median[];
   ArrayResize(median, n);
   for(int i = 0; i < n; i++)
      median[i] = (rates[i].high + rates[i].low) / 2;
   
   double fast_ma[], slow_ma[];
   ArraySetAsSeries(fast_ma, true);
   ArraySetAsSeries(slow_ma, true);
   
   SimpleMA(n-1, fast, median, fast_ma);
   SimpleMA(n-1, slow, median, slow_ma);
   
   if(ArraySize(fast_ma) < 3 || ArraySize(slow_ma) < 3) return;
   
   double ao_curr = fast_ma[0] - slow_ma[0];
   double ao_prev = fast_ma[1] - slow_ma[1];
   
   bool buy = ao_prev <= 0 && ao_curr > 0;
   bool sell = ao_prev >= 0 && ao_curr < 0;
   
   ManageAndEnter(buy, sell, tf);
}

//+------------------------------------------------------------------+
//| Keltner Channel                                                   |
//+------------------------------------------------------------------+
void RunKeltner(ENUM_TIMEFRAMES tf)
{
   int n = MathMax(InpKC_EMA, InpKC_ATR) + 50;
   
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   if(CopyRates(_Symbol, tf, 0, n, rates) < n) return;
   
   double close[];
   ArraySetAsSeries(close, true);
   ArrayResize(close, n);
   for(int i = 0; i < n; i++) close[i] = rates[i].close;
   
   double ema[], atr_vals[];
   ArraySetAsSeries(ema, true);
   ArraySetAsSeries(atr_vals, true);
   
   EMA(n-1, InpKC_EMA, close, ema);
   ATR(rates, InpKC_ATR, n, atr_vals);
   
   if(ArraySize(ema) < 2 || ArraySize(atr_vals) < 2) return;
   if(ema[0] <= 0 || atr_vals[0] <= 0) return;
   
   double upper = ema[0] + InpKC_Mult * atr_vals[0];
   double lower = ema[0] - InpKC_Mult * atr_vals[0];
   double prev_close = close[1];
   double curr_close = close[0];
   
   bool buy = prev_close <= upper && curr_close > upper;
   bool sell = prev_close >= lower && curr_close < lower;
   
   ManageAndEnter(buy, sell, tf);
}

//+------------------------------------------------------------------+
//| ATR Channel                                                       |
//+------------------------------------------------------------------+
void RunATRChannel(ENUM_TIMEFRAMES tf)
{
   int n = MathMax(InpATR_Period, InpATR_Lookback) + 50;
   
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   if(CopyRates(_Symbol, tf, 0, n, rates) < n) return;
   
   double close[];
   ArraySetAsSeries(close, true);
   ArrayResize(close, n);
   double atr_vals[];
   ArraySetAsSeries(atr_vals, true);
   
   for(int i = 0; i < n; i++) close[i] = rates[i].close;
   ATR(rates, 14, n, atr_vals);
   
   if(ArraySize(atr_vals) < InpATR_Period + 2) return;
   
   double sma_val = 0;
   for(int i = 0; i < InpATR_Period; i++)
      sma_val += close[i];
   sma_val /= InpATR_Period;
   
   double upper = sma_val + InpATR_Mult * atr_vals[InpATR_Period-1];
   double lower = sma_val - InpATR_Mult * atr_vals[InpATR_Period-1];
   
   double high_n = rates[0].high;
   double low_n = rates[0].low;
   for(int i = 1; i < InpATR_Lookback; i++)
   {
      if(rates[i].high > high_n) high_n = rates[i].high;
      if(rates[i].low < low_n) low_n = rates[i].low;
   }
   
   bool buy = close[0] > upper && close[1] <= upper && close[0] > high_n;
   bool sell = close[0] < lower && close[1] >= lower && close[0] < low_n;
   
   ManageAndEnter(buy, sell, tf);
}

//+------------------------------------------------------------------+
//| Helpers                                                           |
//+------------------------------------------------------------------+
void ManageAndEnter(bool buy, bool sell, ENUM_TIMEFRAMES tf)
{
   if(PositionSelect(_Symbol))
   {
      if((m_position.PositionType() == POSITION_TYPE_BUY && sell) ||
         (m_position.PositionType() == POSITION_TYPE_SELL && buy))
         ClosePosition();
      return;
   }
   
   if(buy) EnterTrade(ORDER_TYPE_BUY);
   else if(sell) EnterTrade(ORDER_TYPE_SELL);
}

void EnterTrade(ENUM_ORDER_TYPE type)
{
   m_symbol.Refresh();
   double price = (type == ORDER_TYPE_BUY) ? m_symbol.Ask() : m_symbol.Bid();
   double atr = GetATR(14);
   if(atr <= 0) atr = price * 0.01;
   
   double sl = (type == ORDER_TYPE_BUY) ? price - InpStopLossATR * atr : price + InpStopLossATR * atr;
   double tp = (type == ORDER_TYPE_BUY) ? price + (price - sl) * InpTakeProfitRR : price - (sl - price) * InpTakeProfitRR;
   
   double risk = m_account.Balance() * InpRiskPct / 100.0;
   double stop_dist = MathAbs(price - sl);
   double lot_size = (stop_dist > 0) ? risk / (stop_dist * m_symbol.TradeContractSize()) : m_symbol.LotsMin();
   
   double lot_min = m_symbol.LotsMin();
   double lot_max = m_symbol.LotsMax();
   double lot_step = m_symbol.LotsStep();
   lot_size = MathMax(lot_min, MathMin(lot_max, MathRound(lot_size / lot_step) * lot_step));
   
   if(type == ORDER_TYPE_BUY)
      m_trade.Buy(lot_size, _Symbol, price, sl, tp);
   else
      m_trade.Sell(lot_size, _Symbol, price, sl, tp);
   
   int err = GetLastError();
   if(err == 0)
      Print("Opened ", EnumToString(type), " Lot=", lot_size, " SL=", sl, " TP=", tp);
   else
      Print("Order failed: ", err);
}

void ClosePosition()
{
   if(m_trade.PositionClose(_Symbol))
      Print("Closed position");
}

double GetATR(int period)
{
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   if(CopyRates(_Symbol, Period(), 0, period+5, rates) < period+2) return 0;
   
   double sum = 0;
   for(int i = 1; i <= period; i++)
   {
      double tr = rates[i].high - rates[i].low;
      double hc = MathAbs(rates[i].high - rates[i-1].close);
      double lc = MathAbs(rates[i].low - rates[i-1].close);
      sum += MathMax(tr, MathMax(hc, lc));
   }
   return sum / period;
}

//+------------------------------------------------------------------+
//| Moving Average helpers                                            |
//+------------------------------------------------------------------+
void SimpleMA(int pos, int period, double &data[], double &out[])
{
   ArrayResize(out, pos+1);
   for(int i = pos; i >= period-1; i--)
   {
      double sum = 0;
      for(int j = 0; j < period; j++)
         sum += data[i-j];
      out[i] = sum / period;
   }
   for(int i = period-2; i >= 0; i--)
      out[i] = 0;
}

void EMA(int pos, int period, double &data[], double &out[])
{
   ArrayResize(out, pos+1);
   if(pos < 0) return;
   out[pos] = data[pos];
   double k = 2.0 / (period + 1);
   for(int i = pos-1; i >= 0; i--)
      out[i] = data[i] * k + out[i+1] * (1 - k);
}

void ATR(MqlRates &rates[], int period, int n, double &out[])
{
   ArrayResize(out, n);
   for(int i = 0; i < n; i++) out[i] = 0;
   if(n < period + 2) return;
   
   double tr[];
   ArrayResize(tr, n);
   tr[0] = rates[0].high - rates[0].low;
   
   for(int i = 1; i < n; i++)
   {
      double hl = rates[i].high - rates[i].low;
      double hc = MathAbs(rates[i].high - rates[i-1].close);
      double lc = MathAbs(rates[i].low - rates[i-1].close);
      tr[i] = MathMax(hl, MathMax(hc, lc));
   }
   
   double sum = 0;
   for(int i = 1; i <= period; i++)
      sum += tr[i];
   out[period] = sum / period;
   
   for(int i = period+1; i < n; i++)
      out[i] = (out[i-1] * (period-1) + tr[i]) / period;
}
//+------------------------------------------------------------------+
