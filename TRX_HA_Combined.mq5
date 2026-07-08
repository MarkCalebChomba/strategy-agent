//+------------------------------------------------------------------+
//|                                          TRX_HA_Combined.mq5     |
//|         TRXUSDT 1m Heikin-Ashi Momentum (lb=1,2,3) Combined      |
//|         Next-bar execution, hard SL, 0.25% fixed risk             |
//+------------------------------------------------------------------+
#property copyright "Strategy Bot"
#property version   "3.00"
#property description "TRXUSDT 1m HA Momentum combined (lb=1,2,3)"
#property description "Next-bar entry & exit, ATR-based SL, 0.25% risk"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\AccountInfo.mqh>

//+------------------------------------------------------------------+
//| Input parameters                                                  |
//+------------------------------------------------------------------+
input group "=== Risk Management ==="
input double   InpRiskPct       = 0.25;        // Risk % per trade (% of balance)
input double   InpMaxAggRiskPct = 10.0;        // Max aggregate risk %
input double   InpStartBal      = 10000.0;     // Starting balance for risk calc
input int      InpMagic         = 20260708;    // EA magic number

input group "=== Strategy ==="
input int      InpLB1           = 1;           // Lookback 1 (0=disable)
input int      InpLB2           = 2;           // Lookback 2 (0=disable)
input int      InpLB3           = 3;           // Lookback 3 (0=disable)
input int      InpATRPeriod     = 14;          // ATR period
input double   InpATRMult       = 2.0;         // ATR multiplier for SL
input double   InpMinStopPct    = 0.5;         // Min stop % of price

input group "=== Symbol ==="
input string   InpSymbol        = "TRXUSDT";   // Symbol to trade
input ENUM_TIMEFRAMES InpTF     = PERIOD_M1;   // Timeframe

input group "=== News Filter ==="
input int      InpNewsMinutes   = 5;           // Minutes before/after news to skip

//+------------------------------------------------------------------+
//| Structures                                                        |
//+------------------------------------------------------------------+
struct SLookback {
   int      lb;
   bool     enabled;
   ulong    magic;             // Magic = InpMagic + lb
   string   comment;           // Position comment
   bool     pending_entry;     // Buy signal queued for next bar
   bool     pending_exit;      // Sell signal queued for next bar
   ulong    ticket;            // Current position ticket (0 = none)
   double   entry_price;       // Position entry price
   double   stop_price;        // Position stop-loss level
   double   risk_amount;       // Fixed $ risk per trade
   double   pos_volume;        // Lot volume
};

//+------------------------------------------------------------------+
//| Global variables                                                  |
//+------------------------------------------------------------------+
CTrade         Trade;
CPositionInfo  PositionInfo;
CAccountInfo   AccountInfo;

SLookback      g_lb[3];         // 0=lb1, 1=lb2, 2=lb3
datetime       g_last_bar;      // Last processed bar time
double         g_ha_open[];     // Heikin-Ashi open
double         g_ha_close[];    // Heikin-Ashi close
double         g_ha_high[];     // Heikin-Ashi high
double         g_ha_low[];      // Heikin-Ashi low
int            g_ha_size;       // Size of HA arrays
bool           g_initialized;   // HA arrays initialized
datetime       g_news_events[][2]; // News event time windows (start, end)
int            g_news_count;
bool           g_news_loaded;
datetime       g_bar_time;      // Current bar time for position tracking

//+------------------------------------------------------------------+
//| Expert initialization function                                    |
//+------------------------------------------------------------------+
int OnInit() {
   // Validate inputs
   if (InpRiskPct <= 0 || InpRiskPct > 5) {
      Print("Invalid risk %: must be 0-5");
      return INIT_PARAMETERS_INCORRECT;
   }
   if (InpStartBal <= 0) {
      Print("Invalid starting balance");
      return INIT_PARAMETERS_INCORRECT;
   }

   // Setup trade object
   Trade.SetExpertMagicNumber(InpMagic);
   Trade.SetDeviationInPoints(10);

   // Initialize lookbacks
   int lbs[] = {InpLB1, InpLB2, InpLB3};
   for (int i = 0; i < 3; i++) {
      g_lb[i].lb = lbs[i];
      g_lb[i].enabled = (lbs[i] >= 1 && lbs[i] <= 5);
      g_lb[i].magic = InpMagic + lbs[i];
      g_lb[i].comment = "HA_LB" + IntegerToString(lbs[i]);
      g_lb[i].pending_entry = false;
      g_lb[i].pending_exit = false;
      g_lb[i].ticket = 0;
      g_lb[i].risk_amount = InpStartBal * InpRiskPct / 100.0;
      if (g_lb[i].enabled)
         Print("Lookback ", lbs[i], " enabled, risk $", g_lb[i].risk_amount);
   }

   g_last_bar = 0;
   g_initialized = false;
   g_ha_size = 0;
   g_news_loaded = false;
   g_news_count = 0;
   g_bar_time = 0;

   // Subscribe to symbol
   if (!SymbolSelect(InpSymbol, true))
      Print("Warning: could not select ", InpSymbol);

   Print("EA initialized. Starting balance: $", InpStartBal,
         ", risk: ", InpRiskPct, "% ($", InpStartBal * InpRiskPct / 100.0, "/trade)");
   Print("Symbol: ", InpSymbol, " TF: ", EnumToString(InpTF));

   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason) {
   Comment("");
}

//+------------------------------------------------------------------+
//| Heikin-Ashi calculation                                           |
//+------------------------------------------------------------------+
void CalculateHA(int bars_needed) {
   double open[], high[], low[], close[];
   ArraySetAsSeries(open, true);
   ArraySetAsSeries(high, true);
   ArraySetAsSeries(low, true);
   ArraySetAsSeries(close, true);

   // Copy from bar 1 (completed bar) — bar 0 is still forming
   int total = CopyOpen(InpSymbol, InpTF, 1, bars_needed + 5, open);
   CopyHigh(InpSymbol, InpTF, 1, bars_needed + 5, high);
   CopyLow(InpSymbol, InpTF, 1, bars_needed + 5, low);
   CopyClose(InpSymbol, InpTF, 1, bars_needed + 5, close);

   if (total < bars_needed + 2) return;

   ArrayResize(g_ha_open, total);
   ArrayResize(g_ha_close, total);
   ArrayResize(g_ha_high, total);
   ArrayResize(g_ha_low, total);
   g_ha_size = total;

   // First bar
   g_ha_close[0] = (open[0] + high[0] + low[0] + close[0]) / 4.0;
   g_ha_open[0] = open[0];
   g_ha_high[0] = MathMax(high[0], MathMax(g_ha_open[0], g_ha_close[0]));
   g_ha_low[0] = MathMin(low[0], MathMin(g_ha_open[0], g_ha_close[0]));

   // Subsequent bars
   for (int i = 1; i < total; i++) {
      g_ha_close[i] = (open[i] + high[i] + low[i] + close[i]) / 4.0;
      g_ha_open[i] = (g_ha_open[i-1] + g_ha_close[i-1]) / 2.0;
      g_ha_high[i] = MathMax(high[i], MathMax(g_ha_open[i], g_ha_close[i]));
      g_ha_low[i] = MathMin(low[i], MathMin(g_ha_open[i], g_ha_close[i]));
   }

   g_initialized = true;
}

//+------------------------------------------------------------------+
//| ATR calculation (simple avg of high-low ranges)                   |
//+------------------------------------------------------------------+
double CalcATR(int period) {
   double high[], low[];
   ArraySetAsSeries(high, true);
   ArraySetAsSeries(low, true);

   int h = CopyHigh(InpSymbol, InpTF, 1, period, high);
   int l = CopyLow(InpSymbol, InpTF, 1, period, low);
   if (h < period || l < period) return 0.0;

   double sum = 0.0;
   for (int i = 0; i < period; i++)
      sum += (high[i] - low[i]);
   return sum / period;
}

//+------------------------------------------------------------------+
//| Generate HA Momentum signals for one lookback                     |
//+------------------------------------------------------------------+
bool IsBuySignal(int lb) {
   if (!g_initialized || g_ha_size < lb + 2) return false;
   // Entry: lb consecutive bear candles (ha_open > ha_close),
   //   ha_open == ha_high (no upper wick), body expanding
   int i = 0; // current bar (most recent)
   double body = MathAbs(g_ha_open[i] - g_ha_close[i]);
   double prev_body = (i+1 < g_ha_size) ? MathAbs(g_ha_open[i+1] - g_ha_close[i+1]) : body;

   // Check lb consecutive bear candles ending at current bar
   bool consecutive_bear = true;
   for (int j = 0; j < lb && i+j < g_ha_size; j++) {
      if (!(g_ha_open[i+j] > g_ha_close[i+j])) {
         consecutive_bear = false;
         break;
      }
   }
   if (!consecutive_bear) return false;

   // Conditions on current bar (exact match with Python: body > prev_body)
   bool bear = (g_ha_open[i] > g_ha_close[i]);
   bool no_upper_wick = (g_ha_open[i] >= g_ha_high[i] - 0.0000001);
   bool expanding = (body > prev_body);

   return (bear && no_upper_wick && expanding);
}

bool IsSellSignal(int lb) {
   if (!g_initialized || g_ha_size < lb + 2) return false;
   // Exit: lb consecutive bull candles (ha_open < ha_close),
   //   ha_open == ha_low (no lower wick), body expanding
   int i = 0;
   double body = MathAbs(g_ha_open[i] - g_ha_close[i]);
   double prev_body = (i+1 < g_ha_size) ? MathAbs(g_ha_open[i+1] - g_ha_close[i+1]) : body;

   bool consecutive_bull = true;
   for (int j = 0; j < lb && i+j < g_ha_size; j++) {
      if (!(g_ha_open[i+j] < g_ha_close[i+j])) {
         consecutive_bull = false;
         break;
      }
   }
   if (!consecutive_bull) return false;

   bool bull = (g_ha_open[i] < g_ha_close[i]);
   bool no_lower_wick = (g_ha_open[i] <= g_ha_low[i] + 0.0000001);
   bool expanding = (body > prev_body);

   return (bull && no_lower_wick && expanding);
}

//+------------------------------------------------------------------+
//| Check if current time is within a news window                     |
//+------------------------------------------------------------------+
bool IsNewsWindow(datetime current_time) {
   if (!g_news_loaded) return false;
   for (int i = 0; i < g_news_count; i++) {
      if (current_time >= g_news_events[i][0] && current_time <= g_news_events[i][1])
         return true;
   }
   return false;
}

//+------------------------------------------------------------------+
//| Sync position tracking from MT5 positions list                    |
//+------------------------------------------------------------------+
void SyncPositions() {
   // Reset all tracking
   for (int i = 0; i < 3; i++) {
      g_lb[i].ticket = 0;
   }

   // Scan open positions matching our magic numbers
   for (int i = PositionsTotal() - 1; i >= 0; i--) {
      if (!PositionInfo.SelectByIndex(i)) continue;
      if (PositionInfo.Symbol() != InpSymbol) continue;

      ulong magic = PositionInfo.Magic();
      for (int j = 0; j < 3; j++) {
         if (g_lb[j].enabled && magic == g_lb[j].magic) {
            g_lb[j].ticket = PositionInfo.Ticket();
            g_lb[j].entry_price = PositionInfo.PriceOpen();
            g_lb[j].pos_volume = PositionInfo.Volume();
            // Read stop price from position SL
            g_lb[j].stop_price = PositionInfo.StopLoss();
            break;
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Close a position by ticket                                        |
//+------------------------------------------------------------------+
bool ClosePosition(ulong ticket) {
   if (ticket == 0) return false;
   if (!PositionInfo.SelectByTicket(ticket)) return false;
   if (Trade.PositionClose(ticket)) {
      Print("Closed position #", ticket);
      return true;
   }
   Print("Failed to close position #", ticket, ", error: ", GetLastError());
   return false;
}

//+------------------------------------------------------------------+
//| Calculate lot size for a given risk amount                        |
//+------------------------------------------------------------------+
double CalcLotSize(double risk_amount, double stop_dist_price) {
   if (stop_dist_price <= 0) return 0;

   double tick_value = SymbolInfoDouble(InpSymbol, SYMBOL_TRADE_TICK_VALUE);
   double tick_size = SymbolInfoDouble(InpSymbol, SYMBOL_TRADE_TICK_SIZE);
   double point = SymbolInfoDouble(InpSymbol, SYMBOL_POINT);

   if (tick_value <= 0 || tick_size <= 0) {
      Print("Warning: tick_value=", tick_value, " tick_size=", tick_size);
      return 0;
   }

   // SL distance in ticks
   double sl_ticks = stop_dist_price / tick_size;
   if (sl_ticks < 1) {
      Print("Warning: SL too small: ", stop_dist_price, " < tick_size ", tick_size);
      return 0;
   }

   // Loss per 1 lot if SL hit = sl_ticks * tick_value
   double loss_per_lot = sl_ticks * tick_value;
   if (loss_per_lot <= 0) return 0;

   double lots = risk_amount / loss_per_lot;

   // Round to lot step
   double lot_step = SymbolInfoDouble(InpSymbol, SYMBOL_VOLUME_STEP);
   double min_lot = SymbolInfoDouble(InpSymbol, SYMBOL_VOLUME_MIN);
   double max_lot = SymbolInfoDouble(InpSymbol, SYMBOL_VOLUME_MAX);

   if (lot_step > 0)
      lots = MathFloor(lots / lot_step) * lot_step;
   lots = MathMax(min_lot, MathMin(max_lot, lots));

   return lots;
}

//+------------------------------------------------------------------+
//| Open a position for a given lookback                              |
//+------------------------------------------------------------------+
bool OpenPosition(int lb_index) {
   if (lb_index < 0 || lb_index >= 3) return false;
   if (!g_lb[lb_index].enabled) return false;
   if (g_lb[lb_index].ticket != 0) return false;

   double ask = SymbolInfoDouble(InpSymbol, SYMBOL_ASK);
   double price = ask;

   double atr = CalcATR(InpATRPeriod);
   if (atr <= 0) {
      Print("ATR = 0, skipping entry for LB", g_lb[lb_index].lb);
      return false;
   }
   double stop_dist = MathMax(InpATRMult * atr, price * InpMinStopPct / 100.0);
   double stop_price = price - stop_dist;

   double lots = CalcLotSize(g_lb[lb_index].risk_amount, stop_dist);
   if (lots <= 0) {
      Print("Lot size = 0 for LB", g_lb[lb_index].lb, ", cannot open");
      return false;
   }

   double margin_req = 0;
   if (!OrderCalcMargin(ORDER_TYPE_BUY, InpSymbol, lots, price, margin_req)) {
      Print("Margin calc failed for LB", g_lb[lb_index].lb, ", error: ", GetLastError());
      return false;
   }
   double free_margin = AccountInfo.FreeMargin();
   if (margin_req > free_margin) {
      Print("Insufficient margin for LB", g_lb[lb_index].lb,
            ": need $", margin_req, ", have $", free_margin);
      return false;
   }

   Trade.SetExpertMagicNumber(g_lb[lb_index].magic);
   if (Trade.Buy(lots, InpSymbol, price, stop_price, 0, g_lb[lb_index].comment)) {
      ulong ticket = Trade.ResultOrder();
      if (ticket > 0) {
         g_lb[lb_index].ticket = ticket;
         g_lb[lb_index].entry_price = price;
         g_lb[lb_index].stop_price = stop_price;
         g_lb[lb_index].pos_volume = lots;
         Print("Opened LB", g_lb[lb_index].lb, " #", ticket, " at ", price,
               " SL ", stop_price, " lot ", lots,
               " risk $", g_lb[lb_index].risk_amount, " margin $", margin_req);
         return true;
      }
   }
   Print("Failed to open LB", g_lb[lb_index].lb, ", error: ", GetLastError());
   return false;
}

//+------------------------------------------------------------------+
//| Close a position for a given lookback                             |
//+------------------------------------------------------------------+
bool ClosePositionForLB(int lb_index) {
   if (lb_index < 0 || lb_index >= 3) return false;
   if (!g_lb[lb_index].enabled || g_lb[lb_index].ticket == 0) return false;

   if (ClosePosition(g_lb[lb_index].ticket)) {
      g_lb[lb_index].ticket = 0;
      g_lb[lb_index].entry_price = 0;
      g_lb[lb_index].stop_price = 0;
      g_lb[lb_index].pos_volume = 0;
      return true;
   }
   return false;
}

//+------------------------------------------------------------------+
//| Process bar: generate signals, execute pending, set pending       |
//+------------------------------------------------------------------+
void ProcessNewBar(datetime bar_time) {
   g_bar_time = bar_time;

   // Calculate HA
   int max_lb = 0;
   for (int i = 0; i < 3; i++)
      if (g_lb[i].enabled && g_lb[i].lb > max_lb)
         max_lb = g_lb[i].lb;
   CalculateHA(max_lb + 5);

   if (!g_initialized) {
      Print("HA calculation failed, skipping bar");
      return;
   }

   bool in_news_window = IsNewsWindow(bar_time);

   // --- Step 1: Execute pending exits (sell signals from previous bar) ---
   for (int i = 0; i < 3; i++) {
      if (g_lb[i].enabled && g_lb[i].pending_exit && g_lb[i].ticket != 0) {
         Print("LB", g_lb[i].lb, ": executing pending exit at bar open");
         ClosePositionForLB(i);
         g_lb[i].pending_exit = false;
      }
   }

   // --- Step 2: Execute pending entries (buy signals from previous bar) ---
   for (int i = 0; i < 3; i++) {
      if (g_lb[i].enabled && g_lb[i].pending_entry) {
         if (!in_news_window) {
            Print("LB", g_lb[i].lb, ": executing pending entry at bar open");
            OpenPosition(i);
         } else {
            Print("LB", g_lb[i].lb, ": skipping pending entry (news window)");
         }
         g_lb[i].pending_entry = false;
      }
   }

   // --- Step 3: Sync positions (in case of manual close or SL hit) ---
   SyncPositions();

   // --- Step 4: Check sell signals → queue exits for next bar ---
   for (int i = 0; i < 3; i++) {
      if (!g_lb[i].enabled) continue;
      if (g_lb[i].ticket == 0) continue; // Must have open position to exit
      if (IsSellSignal(g_lb[i].lb)) {
         Print("LB", g_lb[i].lb, ": sell signal, queueing exit for next bar");
         g_lb[i].pending_exit = true;
      }
   }

   // --- Step 5: Check buy signals → queue entries for next bar ---
   for (int i = 0; i < 3; i++) {
      if (!g_lb[i].enabled) continue;
      if (g_lb[i].ticket != 0) continue; // Already in position
      if (IsBuySignal(g_lb[i].lb)) {
         Print("LB", g_lb[i].lb, ": buy signal, queueing entry for next bar");
         g_lb[i].pending_entry = true;
      }
   }
}

//+------------------------------------------------------------------+
//| Expert tick function                                              |
//+------------------------------------------------------------------+
void OnTick() {
   // Check for new bar
   datetime current_bar = iTime(InpSymbol, InpTF, 0);
   if (current_bar == 0) return;

   if (current_bar != g_last_bar) {
      // New bar detected
      if (g_last_bar != 0) {
         // Sync positions first (in case MT5 closed positions via SL)
         SyncPositions();
         // Process the previous bar's data (now complete)
         ProcessNewBar(current_bar);
      }
      g_last_bar = current_bar;

      // Display status
      string status = "";
      for (int i = 0; i < 3; i++) {
         if (!g_lb[i].enabled) continue;
         string pe = g_lb[i].pending_entry ? " PE" : "";
         string px = g_lb[i].pending_exit ? " PX" : "";
         string pos = (g_lb[i].ticket != 0) ? (" POS#" + IntegerToString(g_lb[i].ticket)) : "";
         status += "LB" + IntegerToString(g_lb[i].lb) + ":" + pos + pe + px + "  ";
      }
      Comment("HA Combined | ", InpSymbol, " ", EnumToString(InpTF), "\n",
              "Bar: ", current_bar, "\n",
              "News window: ", IsNewsWindow(current_bar) ? "YES" : "no", "\n",
              status);
   }
}

//+------------------------------------------------------------------+
//| Trade function — capture SL hits for logging                      |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result) {
   if (trans.type == TRADE_TRANSACTION_DEAL_ADD) {
      // A trade was executed — could be SL hit or manual close
      // Sync our position tracking
      SyncPositions();
   }
}
//+------------------------------------------------------------------+
