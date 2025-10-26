# AI Trading Server v2.3

Multi-timeframe forex trading analysis server with AI-powered signal generation and conditional trigger system.

## Overview

This Python Flask server analyzes forex charts across multiple timeframes (H4, H1, M15) to generate high-probability trading signals. It uses vision AI to analyze chart screenshots and technical indicators to make trading decisions.

## Key Features

### Version 2.3 - Trigger System
- **Conditional Setup Tracking**: Save triggers when setups aren't ready, automatically re-analyze when conditions are met
- **4 Trigger Types**: level_break, retest_hold, range_edge_reject, ema_retouch
- **Background Watcher**: Checks pending triggers every 2 minutes
- **Automatic Expiry**: Triggers expire after 8 bars (~2 hours for M15)
- **Smart Superseding**: New triggers automatically replace old ones for the same symbol

### Core Capabilities
- **Multi-timeframe Analysis**: H4 for trend context, H1 for structure, M15 for entry timing
- **Breakeven Management**: Automatically moves stop-loss to breakeven when trade is profitable
- **Signal Tracking**: SQLite database tracks all signals with performance metrics
- **Risk Management**: Minimum 1.5:1 risk-reward ratio, configurable RSI filters (75/25)
- **Telegram Notifications**: Real-time alerts with chart screenshots

## Performance Expectations

**Without Triggers (v2.2):**
- 40 analyses per day
- 5-10 direct trade signals

**With Triggers (v2.3):**
- 40 analyses per day
- 5-10 direct signals + 14-18 trigger conversions
- **Total: 19-28 trade signals per day**

## Architecture

### Databases
- `signal_tracking.db` - Trade signals and performance history
- `triggers.db` - Pending triggers and statistics

### Key Endpoints
- `POST /analyze_multi_timeframe` - Main analysis endpoint
- `GET /triggers_summary` - Trigger statistics (created, fired, expired, converted)
- `GET /triggers_pending` - List active pending triggers
- `GET /performance` - Trading performance metrics
- `GET /signals` - Signal history

## Requirements

- Python 3.8+
- Flask
- Anthropic API key
- MT5 with price feed JSON export
- Telegram bot (optional)

## Configuration

Set these environment variables in `.env`:

```
ANTHROPIC_API_KEY=your_key_here
CLAUDE_MODEL=claude-sonnet-4-5-20250929
MT5_TERMINAL_ID=your_terminal_id
TELEGRAM_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

## Technical Details

### Trigger Evaluation
- Uses current price only (no historical bars required)
- 0.5 pip tolerance for level matching
- Works with existing MT5 price_feed.json

### Validation Rules
1. Minimum RR: 1.5:1 (rejects lower)
2. RSI extremes: >75 overbought, <25 oversold
3. Stop loss: 10-100 pips (symbol-aware)
4. No duplicate signals per symbol
5. Trading hours: 06:00-20:00 UTC

### Prompt Engineering
- Timeframe hierarchy: H4 sets bias, H1 identifies zones, M15 confirms entry
- Generates actionable triggers for WAIT decisions
- Validates confluence factors and risk factors

## Version History

### v2.3 (Current)
- Added trigger system for conditional setups
- Simplified trigger evaluation for current-price-only
- Fixed critical slop calculation bug
- Added telemetry endpoints
- Background watcher thread

### v2.2
- Enhanced multi-timeframe consensus
- Improved JSON validation
- Added RR verification
- Session statistics tracking

### v2.1
- Enhanced prompt with clearer timeframe hierarchy
- Fixed RSI and RR validation contradictions

## License

Private - Not for redistribution

## Disclaimer

This software is for educational purposes only. Trading forex carries significant risk. Use at your own risk.
