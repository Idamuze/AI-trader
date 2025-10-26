//+------------------------------------------------------------------+
//|                                      AI_Screenshot_Trading_EA.mq5|
//|                                                       Version 2.0|
//|                                          Multi-Timeframe Analysis|
//+------------------------------------------------------------------+
#property copyright "Copyright 2025, Your Company"
#property link      "https://www.yoursite.com"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>

// Input parameters
input group "=== General parameters ==="
input string   ServerURL = "http://127.0.0.1:5001";  // Python server URL
input int      AnalysisIntervalMinutes = 30;                         // Analysis interval in minutes
input int      BarsToShow = 200;                                     // Number of bars to show in screenshot
input int      ScreenshotWidth = 1920;                               // Screenshot width
input int      ScreenshotHeight = 1080;                              // Screenshot height
input bool     EnableTrading = false;                                // Enable automatic trading
input bool     EnableAlerts = false;                                 // Enable alerts
input double   RiskPercent = 1.0;                                    // Risk percent per trade
input int      MagicNumber = 12345;                                  // Magic number
input bool     SaveScreenshots = false;                              // Save screenshots locally
input bool     ShowIndicatorsOnChart = true;                         // Show indicators on chart

input group "=== News Filter Settings ==="
input bool     EnableNewsFilter = true;                              // Enable news filter
input int      NewsAvoidHoursBefore = 2;                             // Hours to avoid before high impact news
input int      NewsAvoidHoursAfter = 1;                              // Hours to avoid after high impact news
input ENUM_CALENDAR_EVENT_IMPORTANCE MinNewsImportance = CALENDAR_IMPORTANCE_HIGH; // Minimum importance to avoid
input bool     FilterOnlyRelevantCurrencies = true;                  // Only filter news for chart currency
input bool     ShowNewsStatus = true;                                // Show news status on chart
input int      NewsLookaheadDays = 7;                                // Days to look ahead for news

input group "=== Image zoom settings ==="
input bool     EnforceMinimumScale = true;                           // Enforce minimum zoom level
input int      MinimumChartScale = 3;                                // Minimum chart scale (0-5, higher = more zoomed in)
input bool     PrioritizeReadability = true;                         // Prioritize readability over exact bar count
input int      MaxZoomOutAttempts = 5;                               // Maximum zoom out attempts before giving up
input bool     UseTimeframeAwareZoom = false;                        // Use different zoom settings per timeframe
input string   TimeframeZoomSettings = "M1:3,M5:2,M15:1,H1:1,H4:0,D1:0"; // Format: TF:MinScale,TF:MinScale

input group "=== Trading time settings ==="
input bool     UseTimeRestriction = true;                            // Enable time restriction for screenshots
input int      StartHour = 10;                                       // Start hour (0-23)
input int      StartMinute = 5;                                      // Start minute (0-59)
input int      EndHour = 20;                                         // End hour (0-23)
input int      EndMinute = 0;                                        // End minute (0-59)

// Global variables
CTrade trade;

datetime lastAnalysisTime = 0;
string lastDecision = "";
string lastReasoning = "";
double lastEntry = 0;
double lastSL = 0;
double lastTP = 0;
long chartID;

// News filter variables
datetime nextHighImpactNews = 0;
string nextNewsDescription = "";
string nextNewsCurrency = "";
datetime newsAvoidStart = 0;
datetime newsAvoidEnd = 0;
bool newsUpdateInProgress = false;

// Multi-timeframe indicator handles
// H4 indicators (for trend context)
int h4_ema200Handle;
int h4_ema50Handle;
int h4_atrHandle;
int h4_rsiHandle;

// H1 indicators (for market structure)
int h1_ema50Handle;
int h1_ema20Handle;
int h1_atrHandle;
int h1_rsiHandle;

// M15 indicators (for entry timing)
int m15_ema20Handle;
int m15_rsiHandle;
int m15_atrHandle;
int m15_adxHandle;

string screenshot_paths[3];  // Store paths for H4, H1, M15
ENUM_TIMEFRAMES analysis_timeframes[] = {PERIOD_H4, PERIOD_H1, PERIOD_M15};
string timeframe_names[] = {"H4", "H1", "M15"};

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   chartID = ChartID();
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(10);
   trade.SetTypeFilling(ORDER_FILLING_FOK);
   
   // Test notification setup
   Print("Testing MT5 notification setup...");
   string testMsg = "AI Trading EA v2.0 started on " + Symbol();
   if(SendNotification(testMsg))
   {
      Print("✅ MT5 notifications are working properly");
   }
   else
   {
      Print("❌ MT5 notifications not configured!");
      Print("To enable MT5 push notifications:");
      Print("1. Open MT5 on your phone");
      Print("2. Go to Settings → Messages");
      Print("3. Get your MetaQuotes ID");
      Print("4. On desktop MT5: Tools → Options → Notifications");
      Print("5. Enable notifications and enter your MetaQuotes ID");
      Print("6. Test using the 'Test' button");
   }
   
   // Initialize multi-timeframe indicators
   string symbol = Symbol();

   // H4 indicators (trend context) - SIMPLIFIED
   h4_ema200Handle = iMA(symbol, PERIOD_H4, 200, 0, MODE_EMA, PRICE_CLOSE);
   h4_ema50Handle = iMA(symbol, PERIOD_H4, 50, 0, MODE_EMA, PRICE_CLOSE);
   h4_atrHandle = iATR(symbol, PERIOD_H4, 14);
   h4_rsiHandle = iRSI(symbol, PERIOD_H4, 14, PRICE_CLOSE);

   // H1 indicators (market structure) - SIMPLIFIED
   h1_ema50Handle = iMA(symbol, PERIOD_H1, 50, 0, MODE_EMA, PRICE_CLOSE);
   h1_ema20Handle = iMA(symbol, PERIOD_H1, 20, 0, MODE_EMA, PRICE_CLOSE);
   h1_atrHandle = iATR(symbol, PERIOD_H1, 14);
   h1_rsiHandle = iRSI(symbol, PERIOD_H1, 14, PRICE_CLOSE);

   // M15 indicators (entry timing) - SIMPLIFIED
   m15_ema20Handle = iMA(symbol, PERIOD_M15, 20, 0, MODE_EMA, PRICE_CLOSE);
   m15_rsiHandle = iRSI(symbol, PERIOD_M15, 14, PRICE_CLOSE);
   m15_atrHandle = iATR(symbol, PERIOD_M15, 14);
   m15_adxHandle = iADX(symbol, PERIOD_M15, 14);

   // Verify all handles are valid
   if(h4_ema200Handle == INVALID_HANDLE || h4_ema50Handle == INVALID_HANDLE ||
      h4_atrHandle == INVALID_HANDLE || h4_rsiHandle == INVALID_HANDLE ||
      h1_ema50Handle == INVALID_HANDLE || h1_ema20Handle == INVALID_HANDLE ||
      h1_atrHandle == INVALID_HANDLE || h1_rsiHandle == INVALID_HANDLE ||
      m15_ema20Handle == INVALID_HANDLE || m15_rsiHandle == INVALID_HANDLE ||
      m15_atrHandle == INVALID_HANDLE || m15_adxHandle == INVALID_HANDLE)
   {
      Print("Failed to create simplified multi-timeframe indicators");
      return(INIT_FAILED);
   }

   Print("Simplified multi-timeframe indicators initialized successfully:");
   Print("  H4: EMA200, EMA50, ATR, RSI (4 indicators)");
   Print("  H1: EMA50, EMA20, ATR, RSI (4 indicators)");
   Print("  M15: EMA20, RSI, ATR, ADX (4 indicators)");
   
   // Set up chart for screenshots
   SetupChartForScreenshots();
   
   // Create labels for display
   CreateLabels();
   
   // Initialize news filter
   if(EnableNewsFilter)
   {
      Print("News filter enabled - checking calendar...");
      UpdateNewsInformation();
   }
   
   EventSetTimer(60); // Check every minute
   
   Print("AI Screenshot Trading EA v2.0 with News Filter initialized successfully");
   
   // Display settings
   if(UseTimeRestriction){
      Print("Screenshot time restriction enabled: ", 
            IntegerToString(StartHour, 2, '0'), ":", IntegerToString(StartMinute, 2, '0'),
            " to ", 
            IntegerToString(EndHour, 2, '0'), ":", IntegerToString(EndMinute, 2, '0'));
   }
   else{
      Print("Screenshot time restriction disabled - 24/7 operation");
   }
   
   if(EnableNewsFilter)
   {
      Print("News filter enabled: Avoiding ", NewsAvoidHoursBefore, "h before and ", 
            NewsAvoidHoursAfter, "h after ", EnumToString(MinNewsImportance), " importance news");
   }
   
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   
      // Release multi-timeframe indicators
      // H4 indicators
      IndicatorRelease(h4_ema200Handle);
      IndicatorRelease(h4_ema50Handle);
      IndicatorRelease(h4_atrHandle);
      IndicatorRelease(h4_rsiHandle);

      // H1 indicators
      IndicatorRelease(h1_ema50Handle);
      IndicatorRelease(h1_ema20Handle);
      IndicatorRelease(h1_atrHandle);
      IndicatorRelease(h1_rsiHandle);

      // M15 indicators
      IndicatorRelease(m15_ema20Handle);
      IndicatorRelease(m15_rsiHandle);
      IndicatorRelease(m15_atrHandle);
      IndicatorRelease(m15_adxHandle);
   
   // Clean up
   ObjectsDeleteAll(0, "AI_");
}

//+------------------------------------------------------------------+
//| Update news information from calendar                            |
//+------------------------------------------------------------------+

void UpdateNewsInformation()
{
   if(newsUpdateInProgress) return;
   newsUpdateInProgress = true;
   
   datetime timeFrom = TimeCurrent();
   datetime timeTo = timeFrom + NewsLookaheadDays * 24 * 3600; // Look ahead N days
   
   // Get chart symbol currencies
   string baseCurrency = StringSubstr(Symbol(), 0, 3);
   string quoteCurrency = StringSubstr(Symbol(), 3, 3);
   
   MqlCalendarValue values[];
   
   // Get all calendar values in the time range
   int count = CalendarValueHistory(values, timeFrom, timeTo);
   
   if(count <= 0)
   {
      //Print("No calendar events found or calendar not available");
      newsUpdateInProgress = false;
      return;
   }
   
   //Print("Found ", count, " calendar events, filtering for high impact...");
   
   // Reset next news info
   nextHighImpactNews = 0;
   nextNewsDescription = "";
   nextNewsCurrency = "";
   
   for(int i = 0; i < count; i++)
   {
      MqlCalendarEvent event;
      MqlCalendarCountry country;
      
      if(CalendarEventById(values[i].event_id, event) && 
         CalendarCountryById(event.country_id, country))
      {
         // Check importance level
         if(event.importance < MinNewsImportance)
            continue;
            
         // Check if this is relevant currency if filtering is enabled
         if(FilterOnlyRelevantCurrencies)
         {
            if(country.currency != baseCurrency && country.currency != quoteCurrency)
               continue;
         }
         
         // Check if this is the next upcoming high impact news
         if(values[i].time > TimeCurrent() && 
            (nextHighImpactNews == 0 || values[i].time < nextHighImpactNews))
         {
            nextHighImpactNews = values[i].time;
            nextNewsDescription = event.name;
            nextNewsCurrency = country.currency;
            
            // Calculate avoidance period
            newsAvoidStart = nextHighImpactNews - NewsAvoidHoursBefore * 3600;
            newsAvoidEnd = nextHighImpactNews + NewsAvoidHoursAfter * 3600;
            
            /*Print("Next high impact news: ", TimeToString(nextHighImpactNews), 
                  " - ", nextNewsCurrency, " - ", nextNewsDescription);
            Print("Avoidance period: ", TimeToString(newsAvoidStart), " to ", TimeToString(newsAvoidEnd));*/
         }
      }
   }
   
   /*if(nextHighImpactNews == 0)
   {
      Print("No upcoming high impact news found in next ", NewsLookaheadDays, " days");
   }*/
   
   newsUpdateInProgress = false;
}

//+------------------------------------------------------------------+
//| Check if currently in news avoidance period                      |
//| Returns: true if should avoid trading due to news                |
//+------------------------------------------------------------------+
bool IsNewsAvoidancePeriod()
{
   if(!EnableNewsFilter) return false;
   
   datetime currentTime = TimeCurrent();
   
   // Update news info every hour or if we don't have next news info
   static datetime lastNewsUpdate = 0;
   if(currentTime - lastNewsUpdate > 3600 || nextHighImpactNews == 0)
   {
      UpdateNewsInformation();
      lastNewsUpdate = currentTime;
   }
   
   // Check if we're in avoidance period
   if(nextHighImpactNews > 0 && currentTime >= newsAvoidStart && currentTime <= newsAvoidEnd)
   {
      return true;
   }
   
   return false;
}

//+------------------------------------------------------------------+
//| Get formatted news status string for display                     |
//| Returns: Human-readable news status                              |
//+------------------------------------------------------------------+
string GetNewsStatusString()
{
   if(!EnableNewsFilter) return "News Filter: Disabled";
   
   datetime currentTime = TimeCurrent();
   
   if(nextHighImpactNews == 0)
   {
      return "News: No high impact events";
   }
   
   if(IsNewsAvoidancePeriod())
   {
      int hoursToEnd = (int)((newsAvoidEnd - currentTime) / 3600);
      int minutesToEnd = (int)(((newsAvoidEnd - currentTime) % 3600) / 60);
      return StringFormat("News: AVOIDING - %s %s (%dh%dm left)", 
                         nextNewsCurrency, nextNewsDescription, hoursToEnd, minutesToEnd);
   }
   
   int hoursToNews = (int)((nextHighImpactNews - currentTime) / 3600);
   int minutesToNews = (int)(((nextHighImpactNews - currentTime) % 3600) / 60);
   
   if(hoursToNews < NewsAvoidHoursBefore)
   {
      return StringFormat("News: Approaching - %s %s in %dh%dm", 
                         nextNewsCurrency, nextNewsDescription, hoursToNews, minutesToNews);
   }
   
   return StringFormat("News: Next %s %s in %dh%dm", 
                      nextNewsCurrency, nextNewsDescription, hoursToNews, minutesToNews);
}

//+------------------------------------------------------------------+
//| Setup main chart for screenshots                                 |
//| - Configures chart appearance (colors, grid, etc.)               |
//| - Sets zoom level and bar visibility                             |
//| - Applies indicators if ShowIndicatorsOnChart is enabled         |
//+------------------------------------------------------------------+
void SetupChartForScreenshots()
{
   // Set chart properties for clean screenshots
   ChartSetInteger(chartID, CHART_SHOW_GRID, true);
   ChartSetInteger(chartID, CHART_SHOW_PERIOD_SEP, true);
   ChartSetInteger(chartID, CHART_MODE, CHART_CANDLES);
   ChartSetInteger(chartID, CHART_AUTOSCROLL, true);   // Enable autoscroll
   ChartSetInteger(chartID, CHART_SHIFT, false);       // No right margin
   
   // Colors for better visibility
   ChartSetInteger(chartID, CHART_COLOR_BACKGROUND, clrWhite);
   ChartSetInteger(chartID, CHART_COLOR_FOREGROUND, clrBlack);
   ChartSetInteger(chartID, CHART_COLOR_CHART_UP, clrGreen);
   ChartSetInteger(chartID, CHART_COLOR_CHART_DOWN, clrRed);
   ChartSetInteger(chartID, CHART_COLOR_CANDLE_BULL, clrGreen);
   ChartSetInteger(chartID, CHART_COLOR_CANDLE_BEAR, clrRed);
   
   // Set initial scale based on settings
   if(EnforceMinimumScale)
   {
      int minScale = GetTimeframeMinimumScale();
      long currentScale = ChartGetInteger(chartID, CHART_SCALE);
      
      if(currentScale < minScale)
      {
         Print("Setting initial scale to minimum: ", minScale);
         ChartSetInteger(chartID, CHART_SCALE, minScale);
      }
   }
   
   ChartRedraw(chartID);
}

//+------------------------------------------------------------------+
//| Get minimum scale for current timeframe                          |
//+------------------------------------------------------------------+
int GetTimeframeMinimumScale()
{
   if(!UseTimeframeAwareZoom)
      return MinimumChartScale;
   
   string currentTF = EnumToString(Period());
   string settings = TimeframeZoomSettings;
   
   // Parse timeframe-specific settings
   string pairs[];
   int pairCount = StringSplit(settings, ',', pairs);
   
   for(int i = 0; i < pairCount; i++)
   {
      string tfScale[];
      if(StringSplit(pairs[i], ':', tfScale) == 2)
      {
         if(tfScale[0] == currentTF)
         {
            return (int)StringToInteger(tfScale[1]);
         }
      }
   }
   
   // Default fallback
   return MinimumChartScale;
}

//+------------------------------------------------------------------+
//| Enhanced Position chart for screenshot with zoom control         |
//+------------------------------------------------------------------+
void PositionChartForScreenshot()
{
   Print("=== POSITIONING CHART FOR SCREENSHOT ===");
   
   // Set up chart to show latest bars
   ChartSetInteger(chartID, CHART_SHIFT, false);
   ChartSetInteger(chartID, CHART_AUTOSCROLL, true);
   ChartNavigate(chartID, CHART_END, 0);
   
   // Get current state
   long currentScale = ChartGetInteger(chartID, CHART_SCALE);
   long visibleBars = ChartGetInteger(chartID, CHART_VISIBLE_BARS);
   int targetBars = BarsToShow;
   
   // Get minimum scale for current timeframe
   int minScale = GetTimeframeMinimumScale();
   
   Print("Initial state - Scale: ", currentScale, ", Visible bars: ", visibleBars, ", Target: ", targetBars);
   Print("Minimum allowed scale: ", minScale, " (", UseTimeframeAwareZoom ? "timeframe-aware" : "global", ")");
   
   // If we're already below minimum scale, zoom in first
   if(currentScale < minScale)
   {
      Print("Current scale below minimum, zooming in to scale ", minScale);
      ChartSetInteger(chartID, CHART_SCALE, minScale);
      ChartRedraw(chartID);
      Sleep(100);
      
      currentScale = ChartGetInteger(chartID, CHART_SCALE);
      visibleBars = ChartGetInteger(chartID, CHART_VISIBLE_BARS);
      Print("After zoom in - Scale: ", currentScale, ", Visible bars: ", visibleBars);
   }
   
   // Try to reach target bars while respecting minimum scale
   int attempts = 0;
   bool targetReached = false;
   
   while(visibleBars < targetBars && currentScale > minScale && attempts < MaxZoomOutAttempts)
   {
      long newScale = currentScale - 1;
      
      if(newScale < minScale)
      {
         Print("Cannot zoom out further - would violate minimum scale (", minScale, ")");
         break;
      }
      
      ChartSetInteger(chartID, CHART_SCALE, newScale);
      ChartRedraw(chartID);
      Sleep(100);
      
      long newVisibleBars = ChartGetInteger(chartID, CHART_VISIBLE_BARS);
      attempts++;
      
      Print("Zoom attempt ", attempts, " - Scale: ", newScale, " → ", newVisibleBars, " bars");
      
      if(newVisibleBars >= targetBars)
      {
         targetReached = true;
         Print("✅ Target reached! Showing ", newVisibleBars, " bars at scale ", newScale);
         break;
      }
      
      currentScale = newScale;
      visibleBars = newVisibleBars;
   }
   
   // If prioritizing readability and target not reached
   if(!targetReached && PrioritizeReadability)
   {
      // Adjust target to what's achievable at minimum scale
      int achievableBars = (int)ChartGetInteger(chartID, CHART_VISIBLE_BARS);
      
      Print("🔍 Prioritizing readability - showing ", achievableBars, " bars instead of ", targetBars);
      Print("Candles will be larger and more readable for AI analysis");
      
      // Update the effective target for this analysis
      targetBars = achievableBars;
   }
   
   // Final positioning and validation
   ChartNavigate(chartID, CHART_END, 0);  // Ensure we're at the latest bars
   ChartRedraw(chartID);
   Sleep(200);
   
   // Final state report
   long finalScale = ChartGetInteger(chartID, CHART_SCALE);
   long finalBars = ChartGetInteger(chartID, CHART_VISIBLE_BARS);
   
   Print("=== FINAL CHART STATE ===");
   Print("Scale: ", finalScale, " (min allowed: ", minScale, ")");
   Print("Visible bars: ", finalBars, " (target was: ", BarsToShow, ")");
   Print("Chart positioned for optimal AI readability");
   
   // Validation warnings
   if(finalScale == minScale && finalBars < BarsToShow * 0.8)
   {
      Print("⚠️ WARNING: Showing significantly fewer bars than requested due to zoom limits");
      Print("Consider: Reducing BarsToShow or lowering MinimumChartScale for this timeframe");
   }
   
   if(!EnforceMinimumScale && finalScale < 1)
   {
      Print("⚠️ WARNING: Chart is heavily zoomed out - candles may be too small for AI analysis");
      Print("Consider: Enabling EnforceMinimumScale or reducing BarsToShow");
   }
   
   Print("========================");
}

//+------------------------------------------------------------------+
//| New function: Validate chart readability                         |
//+------------------------------------------------------------------+
bool ValidateChartReadability()
{
   long currentScale = ChartGetInteger(chartID, CHART_SCALE);
   long visibleBars = ChartGetInteger(chartID, CHART_VISIBLE_BARS);
   int minScale = GetTimeframeMinimumScale();
   
   // Check if scale is acceptable
   if(EnforceMinimumScale && currentScale < minScale)
   {
      Print("❌ Chart readability check failed: Scale too low (", currentScale, " < ", minScale, ")");
      return false;
   }
   
   // Check if we have reasonable number of bars
   if(visibleBars < 50)
   {
      Print("❌ Chart readability check failed: Too few visible bars (", visibleBars, ")");
      return false;
   }
   
   // Check if chart is too crowded
   if(visibleBars > 1000 && currentScale < 2)
   {
      Print("⚠️ Chart readability warning: Very crowded chart (", visibleBars, " bars at scale ", currentScale, ")");
      return true; // Warning but not failure
   }
   
   Print("✅ Chart readability check passed: Scale ", currentScale, ", ", visibleBars, " bars");
   return true;
}
   
//+------------------------------------------------------------------+
//| Timer function                                                   |
//+------------------------------------------------------------------+
void OnTimer()
{
   datetime currentTime = TimeCurrent();
   
   if(currentTime - lastAnalysisTime >= AnalysisIntervalMinutes * 60)
   {
      // Check all conditions for analysis
      bool withinTradingHours = IsWithinTradingHours();
      bool newsAvoidance = IsNewsAvoidancePeriod();
      
      if(withinTradingHours && !newsAvoidance)
      {
         PerformAnalysis();
         lastAnalysisTime = currentTime;
      }
      else
      {
         // Update the last analysis time even when skipped to maintain interval
         lastAnalysisTime = currentTime;
         
         // Log once when entering restricted period
         static bool wasAvailable = true;
         if(wasAvailable)
         {
            if(!withinTradingHours)
            {
               Print("Outside trading hours - screenshots paused until ", 
                     IntegerToString(StartHour, 2, '0'), ":", 
                     IntegerToString(StartMinute, 2, '0'));
            }
            if(newsAvoidance)
            {
               Print("News avoidance period - screenshots paused due to upcoming ", 
                     nextNewsCurrency, " news: ", nextNewsDescription);
               Print("Avoidance period ends at: ", TimeToString(newsAvoidEnd));
            }
            wasAvailable = false;
         }
      }
   }
}

//+------------------------------------------------------------------+
//| UTILITY FUNCTIONS                                                |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Check if current time is within allowed trading hours            |
//+------------------------------------------------------------------+
bool IsWithinTradingHours()
{
   // If time restriction is disabled, always return true
   if(!UseTimeRestriction)
      return true;
   
   MqlDateTime currentTime;
   TimeToStruct(TimeCurrent(), currentTime);
   
   // Convert current time to minutes since midnight
   int currentMinutes = currentTime.hour * 60 + currentTime.min;
   
   // Convert start and end times to minutes since midnight
   int startMinutes = StartHour * 60 + StartMinute;
   int endMinutes = EndHour * 60 + EndMinute;
   
   // Handle same day comparison
   if(startMinutes <= endMinutes)
   {
      return (currentMinutes >= startMinutes && currentMinutes < endMinutes);
   }
   // Handle overnight time range (e.g., 22:00 to 02:00)
   else
   {
      return (currentMinutes >= startMinutes || currentMinutes < endMinutes);
   }
}

//+------------------------------------------------------------------+
//| Configure H4 chart for screenshot                                |
//+------------------------------------------------------------------+
void ConfigureH4ChartForScreenshot(long chart_id)
{
    // Remove all indicators first
    int total = ChartIndicatorsTotal(chart_id, 0);  // Main window
    for(int i = total - 1; i >= 0; i--)
    {
        string indicator_name = ChartIndicatorName(chart_id, 0, i);
        ChartIndicatorDelete(chart_id, 0, indicator_name);
    }

    // Add ONLY EMA 200 for visual clarity
    ChartIndicatorAdd(chart_id, 0, h4_ema200Handle);

    // Make chart clean and readable
    ChartSetInteger(chart_id, CHART_SHOW_GRID, true);
    ChartSetInteger(chart_id, CHART_SHOW_VOLUMES, false);
    ChartSetInteger(chart_id, CHART_SHOW_PERIOD_SEP, true);
    ChartSetInteger(chart_id, CHART_MODE, CHART_CANDLES);

    Print("[H4 Visual] EMA200 added to chart (trend context)");
}

//+------------------------------------------------------------------+
//| Configure H1 chart for screenshot                                |
//+------------------------------------------------------------------+
void ConfigureH1ChartForScreenshot(long chart_id)
{
    // Remove all indicators first
    int total = ChartIndicatorsTotal(chart_id, 0);
    for(int i = total - 1; i >= 0; i--)
    {
        string indicator_name = ChartIndicatorName(chart_id, 0, i);
        ChartIndicatorDelete(chart_id, 0, indicator_name);
    }

    // Add ONLY EMA 50 for visual clarity
    ChartIndicatorAdd(chart_id, 0, h1_ema50Handle);

    ChartSetInteger(chart_id, CHART_SHOW_GRID, true);
    ChartSetInteger(chart_id, CHART_SHOW_VOLUMES, false);
    ChartSetInteger(chart_id, CHART_SHOW_PERIOD_SEP, true);
    ChartSetInteger(chart_id, CHART_MODE, CHART_CANDLES);

    Print("[H1 Visual] EMA50 added to chart (market structure)");
}

//+------------------------------------------------------------------+
//| Configure M15 chart for screenshot                               |
//+------------------------------------------------------------------+
void ConfigureM15ChartForScreenshot(long chart_id)
{
    // Remove all indicators first
    int total_main = ChartIndicatorsTotal(chart_id, 0);
    for(int i = total_main - 1; i >= 0; i--)
    {
        string indicator_name = ChartIndicatorName(chart_id, 0, i);
        ChartIndicatorDelete(chart_id, 0, indicator_name);
    }

    // Remove indicators from sub-windows
    int total_sub = ChartIndicatorsTotal(chart_id, 1);
    for(int i = total_sub - 1; i >= 0; i--)
    {
        string indicator_name = ChartIndicatorName(chart_id, 1, i);
        ChartIndicatorDelete(chart_id, 1, indicator_name);
    }

    // Add EMA 20 to main window
    ChartIndicatorAdd(chart_id, 0, m15_ema20Handle);

    // Add RSI to sub-window (optional - comment out if too cluttered)
    ChartIndicatorAdd(chart_id, 1, m15_rsiHandle);

    ChartSetInteger(chart_id, CHART_SHOW_GRID, true);
    ChartSetInteger(chart_id, CHART_SHOW_VOLUMES, false);
    ChartSetInteger(chart_id, CHART_SHOW_PERIOD_SEP, true);
    ChartSetInteger(chart_id, CHART_MODE, CHART_CANDLES);

    Print("[M15 Visual] EMA20 + RSI added to chart (entry timing)");
}



//+------------------------------------------------------------------+
//| SCREENSHOT CAPTURE FUNCTIONS                                     |
//+------------------------------------------------------------------+
bool CaptureMultiTimeframeScreenshots(string symbol)
{
    bool success = true;

    // ChartScreenShot saves to MQL5\Files\ directory automatically
    string screenshotFolder = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\";

    for(int i = 0; i < 3; i++)
    {
        ENUM_TIMEFRAMES tf = analysis_timeframes[i];
        string tf_name = timeframe_names[i];

        // Create filename with timeframe and timestamp
        string timestamp = TimeToString(TimeCurrent(), TIME_DATE|TIME_MINUTES);
        StringReplace(timestamp, ":", "-");
        StringReplace(timestamp, " ", "_");
        StringReplace(timestamp, ".", "-");

        string filename = StringFormat("%s_%s_%s.png", symbol, tf_name, timestamp);

        // Full filepath
        string filepath = screenshotFolder + filename;

        Print("Attempting to capture ", tf_name, " screenshot for ", symbol);

        // Switch to this timeframe
        long temp_chart = ChartOpen(symbol, tf);
        if(temp_chart == 0)
        {
            Print("ERROR: Failed to open ", tf_name, " chart for ", symbol);
            success = false;
            continue;
        }

        // Configure chart with timeframe-specific indicators
        if(tf == PERIOD_H4)
        {
            ConfigureH4ChartForScreenshot(temp_chart);
        }
        else if(tf == PERIOD_H1)
        {
            ConfigureH1ChartForScreenshot(temp_chart);
        }
        else if(tf == PERIOD_M15)
        {
            ConfigureM15ChartForScreenshot(temp_chart);
        }

        ChartSetInteger(temp_chart, CHART_COLOR_BACKGROUND, clrWhite);
        ChartSetInteger(temp_chart, CHART_COLOR_FOREGROUND, clrBlack);

        // Wait for chart and indicators to fully render
        Sleep(2000);
        ChartRedraw(temp_chart);
        Sleep(500);

        // Take screenshot
        if(!ChartScreenShot(temp_chart, filename, ScreenshotWidth, ScreenshotHeight, ALIGN_RIGHT))
        {
            Print("ERROR: Screenshot failed for ", tf_name);
            Print("Last error: ", GetLastError());
            success = false;
        }
        else
        {
            screenshot_paths[i] = filepath;
            Print("[OK] ", tf_name, " screenshot saved: ", filepath);
        }

        // Close temporary chart
        ChartClose(temp_chart);
        Sleep(500);
    }

    return success;
}

//+------------------------------------------------------------------+
//| Send multi-timeframe analysis request to Flask server            |
//+------------------------------------------------------------------+
// Helper function to escape backslashes for JSON
string EscapeJsonString(string str)
{
    string result = str;
    StringReplace(result, "\\", "\\\\");
    return result;
}

string SendMultiTimeframeAnalysisToServer(string h4_path, string h1_path, string m15_path, string indicator_data)
{
    string url = ServerURL + "/analyze_multi_timeframe";

    char post[], result[];
      string headers = "Content-Type: application/json\r\n";

    // Escape backslashes in file paths for JSON
    string h4_escaped = EscapeJsonString(h4_path);
    string h1_escaped = EscapeJsonString(h1_path);
    string m15_escaped = EscapeJsonString(m15_path);

    // Build JSON request with file paths
    string json_request = StringFormat(
        "{\"symbol\":\"%s\",\"h4_screenshot\":\"%s\",\"h1_screenshot\":\"%s\",\"m15_screenshot\":\"%s\",\"indicators\":%s}",
        Symbol(),
        h4_escaped,
        h1_escaped,
        m15_escaped,
        indicator_data
    );

    Print("Sending request to: ", url);
    Print("JSON payload length: ", StringLen(json_request));

    // Convert to char array WITHOUT null terminator (which causes JSON parse errors)
    StringToCharArray(json_request, post, 0, WHOLE_ARRAY);
    ArrayResize(post, ArraySize(post) - 1);  // Remove null terminator

      int timeout = 60000;  // 60 second timeout
    ResetLastError();

    int res = WebRequest(
        "POST",
        url,
        headers,
        timeout,
        post,
        result,
        headers
    );

    if(res == -1)
    {
        int error = GetLastError();
        Print("WebRequest Error: ", error);
        Print("Make sure ", url, " is added to allowed URLs in MT5");
        Print("Tools -> Options -> Expert Advisors -> Allow WebRequest for listed URL");
        return "";
    }

    string response = CharArrayToString(result);
    Print("Received response: ", StringLen(response), " characters");

    return response;
}

//+------------------------------------------------------------------+
//| ANALYSIS AND AI COMMUNICATION FUNCTIONS                          |
//+------------------------------------------------------------------+
void PerformAnalysis()
{
    if(UseTimeRestriction && !IsWithinTradingHours())
    {
        Print("Analysis skipped - outside trading hours");
        return;
    }

    if(EnableNewsFilter && IsNewsAvoidancePeriod())
    {
        Print("Analysis skipped - news avoidance period");
        return;
    }

    Print("=== STARTING MULTI-TIMEFRAME ANALYSIS ===");
    Print("Timeframes: H4, H1, M15");

    string symbol = Symbol();

    // Capture all 3 timeframes
    if(!CaptureMultiTimeframeScreenshots(symbol))
    {
        Print("ERROR: Failed to capture all screenshots");
        return;
    }

    Print("[OK] All 3 screenshots captured successfully");

    // Gather MULTI-TIMEFRAME indicator data
    string indicator_json = CollectMultiTimeframeIndicatorData();

    Print("Sending to Flask server with multi-timeframe analysis...");

    // Send to Flask server with multiple screenshots
    string response = SendMultiTimeframeAnalysisToServer(
        screenshot_paths[0],  // H4
        screenshot_paths[1],  // H1
        screenshot_paths[2],  // M15
        indicator_json
    );

    if(response == "")
    {
        Print("ERROR: No response from server");
        return;
    }

    // Process response
    ProcessAIResponse(response);

    // Delete screenshots after analysis if SaveScreenshots is false
    if(!SaveScreenshots)
    {
        string screenshotFolder = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\";

        for(int i = 0; i < 3; i++)
        {
            // Extract just the filename from full path
            string fullPath = screenshot_paths[i];
            string filename = "";

            // Find last backslash to get filename only
            int lastSlash = StringLen(fullPath) - 1;
            for(int j = StringLen(fullPath) - 1; j >= 0; j--)
            {
                if(StringGetCharacter(fullPath, j) == '\\')
                {
                    lastSlash = j;
                    break;
                }
            }

            // Extract filename
            if(lastSlash >= 0 && lastSlash < StringLen(fullPath) - 1)
            {
                filename = StringSubstr(fullPath, lastSlash + 1);
            }
            else
            {
                filename = fullPath; // Fallback if no path separators found
            }

            Print("Attempting to delete: ", filename);

            if(FileDelete(filename))
            {
                Print("✓ Deleted screenshot: ", filename);
            }
            else
            {
                int error = GetLastError();
                if(error == 5002)
                {
                    Print("Note: Screenshot may have been already deleted or never created: ", filename);
                }
                else
                {
                    Print("Failed to delete screenshot: ", filename, " Error: ", error);
                }
            }
        }
    }

    Print("=== MULTI-TIMEFRAME ANALYSIS COMPLETE ===");
}

//+------------------------------------------------------------------+
//| Collect simplified multi-timeframe indicator data                |
//| H4 Indicators: EMA 200, EMA 50, ATR, RSI                         |
//| H1 Indicators: EMA 50, EMA 20, ATR, RSI                          |
//| M15 Indicators: EMA 20, RSI, ATR, ADX                            |
//| Returns: JSON string with all indicator values                   |
//+------------------------------------------------------------------+
string CollectMultiTimeframeIndicatorData()
{
    string symbol = Symbol();
    double current_price = SymbolInfoDouble(symbol, SYMBOL_BID);

    Print("=== COLLECTING SIMPLIFIED MULTI-TIMEFRAME INDICATOR DATA ===");

    // ==================== H4 INDICATORS ====================
    double h4_ema200[], h4_ema50[], h4_atr[], h4_rsi[];

    ArraySetAsSeries(h4_ema200, true);
    ArraySetAsSeries(h4_ema50, true);
    ArraySetAsSeries(h4_atr, true);
    ArraySetAsSeries(h4_rsi, true);

    CopyBuffer(h4_ema200Handle, 0, 0, 1, h4_ema200);
    CopyBuffer(h4_ema50Handle, 0, 0, 1, h4_ema50);
    CopyBuffer(h4_atrHandle, 0, 0, 1, h4_atr);
    CopyBuffer(h4_rsiHandle, 0, 0, 1, h4_rsi);

    // Get H4 recent highs/lows
    MqlRates h4_rates[];
    ArraySetAsSeries(h4_rates, true);
    CopyRates(symbol, PERIOD_H4, 0, 20, h4_rates);

    double h4_high_20 = h4_rates[0].high;
    double h4_low_20 = h4_rates[0].low;
    for(int i = 1; i < 20; i++)
    {
        if(h4_rates[i].high > h4_high_20) h4_high_20 = h4_rates[i].high;
        if(h4_rates[i].low < h4_low_20) h4_low_20 = h4_rates[i].low;
    }

    string h4_trend = (current_price > h4_ema200[0]) ? "UPTREND" : "DOWNTREND";
    string h4_price_vs_ema = (current_price > h4_ema200[0]) ? "ABOVE" : "BELOW";

    Print("[H4] EMA200: ", DoubleToString(h4_ema200[0], Digits()),
          " RSI: ", DoubleToString(h4_rsi[0], 2),
          " Trend: ", h4_trend);

    // ==================== H1 INDICATORS ====================
    double h1_ema50[], h1_ema20[], h1_atr[], h1_rsi[];

    ArraySetAsSeries(h1_ema50, true);
    ArraySetAsSeries(h1_ema20, true);
    ArraySetAsSeries(h1_atr, true);
    ArraySetAsSeries(h1_rsi, true);

    CopyBuffer(h1_ema50Handle, 0, 0, 1, h1_ema50);
    CopyBuffer(h1_ema20Handle, 0, 0, 1, h1_ema20);
    CopyBuffer(h1_atrHandle, 0, 0, 1, h1_atr);
    CopyBuffer(h1_rsiHandle, 0, 0, 1, h1_rsi);

    // Get H1 recent highs/lows
    MqlRates h1_rates[];
    ArraySetAsSeries(h1_rates, true);
    CopyRates(symbol, PERIOD_H1, 0, 20, h1_rates);

    double h1_high_20 = h1_rates[0].high;
    double h1_low_20 = h1_rates[0].low;
    for(int i = 1; i < 20; i++)
    {
        if(h1_rates[i].high > h1_high_20) h1_high_20 = h1_rates[i].high;
        if(h1_rates[i].low < h1_low_20) h1_low_20 = h1_rates[i].low;
    }

    string h1_price_vs_ema = (current_price > h1_ema50[0]) ? "ABOVE" : "BELOW";

    Print("[H1] EMA50: ", DoubleToString(h1_ema50[0], Digits()),
          " RSI: ", DoubleToString(h1_rsi[0], 2),
          " Price vs EMA50: ", h1_price_vs_ema);

    // ==================== M15 INDICATORS ====================
    double m15_ema20[], m15_rsi[], m15_atr[], m15_adx[];

    ArraySetAsSeries(m15_ema20, true);
    ArraySetAsSeries(m15_rsi, true);
    ArraySetAsSeries(m15_atr, true);
    ArraySetAsSeries(m15_adx, true);

    CopyBuffer(m15_ema20Handle, 0, 0, 1, m15_ema20);
    CopyBuffer(m15_rsiHandle, 0, 0, 1, m15_rsi);
    CopyBuffer(m15_atrHandle, 0, 0, 1, m15_atr);
    CopyBuffer(m15_adxHandle, 0, 0, 1, m15_adx);

    string m15_price_vs_ema = (current_price > m15_ema20[0]) ? "ABOVE" : "BELOW";

    // Calculate price changes for volatility context
    MqlRates m15_rates[];
    ArraySetAsSeries(m15_rates, true);
    CopyRates(symbol, PERIOD_M15, 0, 100, m15_rates);

    double price_change_20 = MathAbs(m15_rates[0].close - m15_rates[19].close) * 10000;

    double total_changes = 0;
    for(int i = 20; i < 100; i++)
    {
        total_changes += MathAbs(m15_rates[i].close - m15_rates[i-20].close) * 10000;
    }
    double avg_change = total_changes / 80;

    // Determine volatility state
    string volatility_state = "NORMAL";
    if(price_change_20 > avg_change * 1.5) volatility_state = "HIGH";
    else if(price_change_20 < avg_change * 0.5) volatility_state = "LOW";

    Print("[M15] EMA20: ", DoubleToString(m15_ema20[0], Digits()),
          " RSI: ", DoubleToString(m15_rsi[0], 2),
          " ADX: ", DoubleToString(m15_adx[0], 2));

    // ==================== BUILD JSON ====================
    string json = "{";
    json += "\"symbol\":\"" + symbol + "\",";
    json += "\"current_price\":" + DoubleToString(current_price, Digits()) + ",";
    json += "\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_MINUTES) + "\",";

    // H4 DATA
    json += "\"h4_indicators\":{";
    json += "\"ema_200\":" + DoubleToString(h4_ema200[0], Digits()) + ",";
    json += "\"ema_50\":" + DoubleToString(h4_ema50[0], Digits()) + ",";
    json += "\"atr_14\":" + DoubleToString(h4_atr[0], Digits()) + ",";
    json += "\"rsi_14\":" + DoubleToString(h4_rsi[0], 2) + ",";
    json += "\"price_vs_ema200\":\"" + h4_price_vs_ema + "\",";
    json += "\"trend_direction\":\"" + h4_trend + "\",";
    json += "\"h4_high_20\":" + DoubleToString(h4_high_20, Digits()) + ",";
    json += "\"h4_low_20\":" + DoubleToString(h4_low_20, Digits());
    json += "},";

    // H1 DATA
    json += "\"h1_indicators\":{";
    json += "\"ema_50\":" + DoubleToString(h1_ema50[0], Digits()) + ",";
    json += "\"ema_20\":" + DoubleToString(h1_ema20[0], Digits()) + ",";
    json += "\"atr_14\":" + DoubleToString(h1_atr[0], Digits()) + ",";
    json += "\"rsi_14\":" + DoubleToString(h1_rsi[0], 2) + ",";
    json += "\"price_vs_ema50\":\"" + h1_price_vs_ema + "\",";
    json += "\"h1_high_20\":" + DoubleToString(h1_high_20, Digits()) + ",";
    json += "\"h1_low_20\":" + DoubleToString(h1_low_20, Digits());
    json += "},";

    // M15 DATA
    json += "\"m15_indicators\":{";
    json += "\"ema_20\":" + DoubleToString(m15_ema20[0], Digits()) + ",";
    json += "\"rsi_14\":" + DoubleToString(m15_rsi[0], 2) + ",";
    json += "\"atr_14\":" + DoubleToString(m15_atr[0], Digits()) + ",";
    json += "\"adx\":" + DoubleToString(m15_adx[0], 2) + ",";
    json += "\"price_vs_ema20\":\"" + m15_price_vs_ema + "\"";
    json += "},";

    // CALCULATED CONTEXT
    json += "\"calculated_context\":{";
    json += "\"price_change_20_candles\":" + DoubleToString(price_change_20, 2) + ",";
    json += "\"avg_price_change_last_100\":" + DoubleToString(avg_change, 2) + ",";
    json += "\"volatility_state\":\"" + volatility_state + "\"";
    json += "}";

    json += "}";

    Print("=== SIMPLIFIED MULTI-TIMEFRAME DATA COLLECTION COMPLETE ===");
    Print("Total indicators: H4(4) + H1(4) + M15(4) = 12 indicators");

    return json;
}

//+------------------------------------------------------------------+
//| Send notification with proper formatting and length check        |
//+------------------------------------------------------------------+
bool SendFormattedNotification(string decision, double entry, double sl, double tp, string reason)
{
   // MT5 notifications have a 255 character limit
   // Create multiple notification formats from detailed to simple
   
   string fullMsg = StringFormat("AI %s Signal\nSymbol: %s\nEntry: %s\nSL: %s\nTP: %s\nReason: %s",
                                decision, Symbol(), 
                                DoubleToString(entry, Digits()),
                                DoubleToString(sl, Digits()),
                                DoubleToString(tp, Digits()),
                                reason);
   
   string mediumMsg = StringFormat("AI %s: %s\nEntry: %s\nSL: %s\nTP: %s",
                                  decision, Symbol(),
                                  DoubleToString(entry, Digits()),
                                  DoubleToString(sl, Digits()),
                                  DoubleToString(tp, Digits()));
   
   string shortMsg = StringFormat("AI %s: %s @ %s",
                                 decision, Symbol(),
                                 DoubleToString(entry, Digits()));
   
   // Try sending in order of preference
   if(StringLen(fullMsg) <= 255 && SendNotification(fullMsg))
   {
      Print("Full notification sent");
      return true;
   }
   else if(StringLen(mediumMsg) <= 255 && SendNotification(mediumMsg))
   {
      Print("Medium notification sent");
      return true;
   }
   else if(SendNotification(shortMsg))
   {
      Print("Short notification sent");
      return true;
   }
   
   Print("Failed to send any notification format");
   return false;
}

//+------------------------------------------------------------------+
//| Process AI response                                              |
//+------------------------------------------------------------------+
void ProcessAIResponse(string response)
{
   Print("Processing AI response: ", response);
   
   // Parse JSON response
   lastDecision = GetJsonValue(response, "decision");
   lastReasoning = GetJsonValue(response, "reasoning");
   
   string entryStr = GetJsonValue(response, "entry");
   string slStr = GetJsonValue(response, "sl");
   string tpStr = GetJsonValue(response, "tp");
   
   lastEntry = StringToDouble(entryStr);
   lastSL = StringToDouble(slStr);
   lastTP = StringToDouble(tpStr);
   
   // Update display
   UpdateDisplay();
   
   // Send alerts
   if(EnableAlerts && lastDecision != "WAIT")
   {
      // Send MT5 Alert (popup window)
      string alertMsg = StringFormat("AI Signal: %s\nSymbol: %s\nEntry: %s\nSL: %s\nTP: %s\nReason: %s",
                                    lastDecision, Symbol(),
                                    DoubleToString(lastEntry, Digits()),
                                    DoubleToString(lastSL, Digits()),
                                    DoubleToString(lastTP, Digits()),
                                    lastReasoning);
      Alert(alertMsg);
      
      // Send Push Notification with proper formatting
      bool notificationSent = SendFormattedNotification(lastDecision, lastEntry, lastSL, lastTP, lastReasoning);
      
      if(!notificationSent)
      {
         int error = GetLastError();
         Print("❌ Notification error code: ", error);
         
         // Provide specific guidance based on error
         switch(error)
         {
            case 4250:
               Alert("Push notifications not configured!\nSetup required in Tools→Options→Notifications");
               break;
            case 4251:
               Print("Invalid notification message format");
               break;
            case 4252:
               Print("Notification rate limit reached (max 10 per minute)");
               break;
            default:
               Print("Unknown notification error: ", error);
         }
      }
   }
   
   // Execute trade if enabled
   if(EnableTrading && lastDecision != "WAIT")
   {
      ExecuteTrade();
   }
}

//+------------------------------------------------------------------+
//| JSON value extractor                                      |
//+------------------------------------------------------------------+
string GetJsonValue(string json, string key)
{
   string searchKey = "\"" + key + "\":";
   int keyPos = StringFind(json, searchKey);
   
   if(keyPos == -1) return "";
   
   int valueStart = keyPos + StringLen(searchKey);
   
   // Skip whitespace
   while(valueStart < StringLen(json) && 
         (StringGetCharacter(json, valueStart) == ' ' || 
          StringGetCharacter(json, valueStart) == '\t'))
   {
      valueStart++;
   }
   
   // Check if value is string
   bool isString = (StringGetCharacter(json, valueStart) == '"');
   
   if(isString)
   {
      valueStart++; // Skip opening quote
      int valueEnd = StringFind(json, "\"", valueStart);
      if(valueEnd == -1) return "";
      return StringSubstr(json, valueStart, valueEnd - valueStart);
   }
   else
   {
      // Numeric value
      int commaPos = StringFind(json, ",", valueStart);
      int bracePos = StringFind(json, "}", valueStart);
      
      int valueEnd = -1;
      if(commaPos == -1) valueEnd = bracePos;
      else if(bracePos == -1) valueEnd = commaPos;
      else valueEnd = MathMin(commaPos, bracePos);
      
      if(valueEnd == -1) return "";
      return StringSubstr(json, valueStart, valueEnd - valueStart);
   }
}

//+------------------------------------------------------------------+
//| Execute trade                                                    |
//+------------------------------------------------------------------+
void ExecuteTrade()
{
   if(lastDecision == "WAIT") return;
   
   double volume = CalculateVolume();
   
   if(lastDecision == "BUY")
   {
      if(trade.Buy(volume, Symbol(), lastEntry, lastSL, lastTP, "AI Screenshot Trade"))
      {
         Print("Buy order executed successfully");
      }
      else
      {
         Print("Buy order failed. Error: ", trade.ResultRetcode());
      }
   }
   else if(lastDecision == "SELL")
   {
      if(trade.Sell(volume, Symbol(), lastEntry, lastSL, lastTP, "AI Screenshot Trade"))
      {
         Print("Sell order executed successfully");
      }
      else
      {
         Print("Sell order failed. Error: ", trade.ResultRetcode());
      }
   }
}

//+------------------------------------------------------------------+
//| Calculate position volume                                        |
//+------------------------------------------------------------------+
double CalculateVolume()
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskAmount = balance * RiskPercent / 100.0;
   
   double slPoints = MathAbs(lastEntry - lastSL) / Point();
   double tickValue = SymbolInfoDouble(Symbol(), SYMBOL_TRADE_TICK_VALUE);
   
   double volume = riskAmount / (slPoints * tickValue);
   
   // Normalize volume
   double minLot = SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(Symbol(), SYMBOL_VOLUME_STEP);
   
   volume = MathFloor(volume / lotStep) * lotStep;
   volume = MathMax(minLot, MathMin(maxLot, volume));
   
   return volume;
}

//+------------------------------------------------------------------+
//| Create display labels                                            |
//+------------------------------------------------------------------+
void CreateLabels()
{
   int y = 40;
   int yStep = 20;
   
   CreateLabel("AI_Title", 10, y, "AI Screenshot Analysis v2.0", "Consolas Bold", 10, clrBlack);
   y += yStep * 2;
   
   CreateLabel("AI_Decision", 10, y, "Decision: Waiting...", "Consolas", 8, clrBlack);
   y += yStep;
   
   CreateLabel("AI_Entry", 10, y, "Entry: -", "Consolas", 8, clrBlack);
   y += yStep;
   
   CreateLabel("AI_SL", 10, y, "Stop Loss: -", "Consolas", 8, clrBlack);
   y += yStep;
   
   CreateLabel("AI_TP", 10, y, "Take Profit: -", "Consolas", 8, clrBlack);
   y += yStep * 2;
   
   CreateLabel("AI_Reason", 10, y, "Reasoning: -", "Consolas", 8, clrBlack);
   y += yStep * 2;
   
   CreateLabel("AI_NewsStatus", 10, y, "News: Checking...", "Consolas", 8, clrBlue);
   y += yStep;
   
   CreateLabel("AI_LastUpdate", 10, y, "Last update: -", "Consolas", 8, clrBlack);
}

//+------------------------------------------------------------------+
//| Create single label                                              |
//+------------------------------------------------------------------+
void CreateLabel(string name, int x, int y, string text, string font, int size, color clr)
{
   ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetString(0, name, OBJPROP_FONT, font);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, size);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
}

//+------------------------------------------------------------------+
//| Update on-chart display with latest analysis                     |
//| Shows: Last decision, entry, SL, TP, reasoning, news status      |
//+------------------------------------------------------------------+
void UpdateDisplay()
{
   if(lastDecision != "")
   {
      // All colors are now black - no color coding for decisions
      ObjectSetString(0, "AI_Decision", OBJPROP_TEXT, "Decision: " + lastDecision);
      ObjectSetInteger(0, "AI_Decision", OBJPROP_COLOR, clrBlack);
      
      ObjectSetString(0, "AI_Entry", OBJPROP_TEXT, "Entry: " + DoubleToString(lastEntry, Digits()));
      ObjectSetString(0, "AI_SL", OBJPROP_TEXT, "Stop Loss: " + DoubleToString(lastSL, Digits()));
      ObjectSetString(0, "AI_TP", OBJPROP_TEXT, "Take Profit: " + DoubleToString(lastTP, Digits()));
      
      // Truncate reasoning
      string shortReason = lastReasoning;
      if(StringLen(shortReason) > 60)
         shortReason = StringSubstr(shortReason, 0, 57) + "...";
      
      ObjectSetString(0, "AI_Reason", OBJPROP_TEXT, "Reasoning: " + shortReason);
   }
   
   // Update news status
   if(ShowNewsStatus)
   {
      string newsStatus = GetNewsStatusString();
      ObjectSetString(0, "AI_NewsStatus", OBJPROP_TEXT, newsStatus);
      
      // Color code news status
      if(IsNewsAvoidancePeriod())
         ObjectSetInteger(0, "AI_NewsStatus", OBJPROP_COLOR, clrRed);
      else if(EnableNewsFilter && nextHighImpactNews > 0 && (nextHighImpactNews - TimeCurrent()) < NewsAvoidHoursBefore * 3600)
         ObjectSetInteger(0, "AI_NewsStatus", OBJPROP_COLOR, clrOrange);
      else
         ObjectSetInteger(0, "AI_NewsStatus", OBJPROP_COLOR, clrGreen);
   }
   
   // Add time restriction status to display
   string timeStatus = "";
   if(UseTimeRestriction)
   {
      timeStatus += IsWithinTradingHours() ? "Trading Hours" : "Outside Hours";
   }
   if(EnableNewsFilter)
   {
      if(timeStatus != "") timeStatus += " | ";
      timeStatus += IsNewsAvoidancePeriod() ? "News Avoid" : "News OK";
   }
   
   ObjectSetString(0, "AI_LastUpdate", OBJPROP_TEXT, 
                  "Last Update: " + TimeToString(TimeCurrent()) + 
                  (timeStatus != "" ? " (" + timeStatus + ")" : ""));
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
   UpdateDisplay();
}