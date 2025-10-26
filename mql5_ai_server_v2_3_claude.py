#!/usr/bin/env python3
"""
AI Trading Server - Screenshot Analysis with Claude Vision + Signal Tracking + Breakeven Management
Receives chart screenshots and tracks signal performance with breakeven stop-loss adjustments
VERSION 2.3 - TRIGGER SYSTEM FOR CONDITIONAL SETUPS
Changes in v2.3:
- Added trigger system for WAIT decisions with next_trigger field
- Trigger database with automatic expiry and superseding
- Background watcher thread for trigger evaluation
- Trigger telemetry endpoints (/triggers_summary, /triggers_pending)
- Re-analysis when triggers fire
- Enhanced prompt to generate actionable triggers
"""

import json
import logging
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone
import traceback
import base64
import os
from werkzeug.utils import secure_filename
import re
import sqlite3
from threading import Thread
import time
import sys
import io
from pathlib import Path
import anthropic
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Fix console encoding for Windows (add right after imports)
if sys.platform == "win32":
    # Set console to UTF-8
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# === CONFIGURATION (Loaded from .env file) ===
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
CLAUDE_MODEL = os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-5-20250929')
ANTHROPIC_API_URL = os.getenv('ANTHROPIC_API_URL', 'https://api.anthropic.com/v1/messages')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# MT5 configuration
MT5_TERMINAL_ID = os.getenv('MT5_TERMINAL_ID', 'D0E8209F77C8CF37AD8BF550E51FF075')
MT5_FILES_PATH = Path.home() / "AppData/Roaming/MetaQuotes/Terminal" / MT5_TERMINAL_ID / "MQL5/Files"

# Server configuration
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'screenshots')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
MAX_IMAGE_SIZE = int(os.getenv('MAX_IMAGE_SIZE_MB', '20')) * 1024 * 1024
DATABASE_FILE = os.getenv('DATABASE_FILE', 'signal_tracking.db')
PRICE_UPDATE_INTERVAL = int(os.getenv('PRICE_UPDATE_INTERVAL', '60'))

# Signal blocking configuration
ENABLE_SIGNAL_BLOCKING = os.getenv('ENABLE_SIGNAL_BLOCKING', 'True').lower() == 'true'

# Validate required environment variables
if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY not found in environment variables. Please check your .env file.")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not found in environment variables. Please check your .env file.")
if not TELEGRAM_CHAT_ID:
    raise ValueError("TELEGRAM_CHAT_ID not found in environment variables. Please check your .env file.")

# Initialize Anthropic client
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Initialize Flask app
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_IMAGE_SIZE

# Create upload folder if it doesn't exist
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# Configure logging with UTF-8 encoding
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('screenshot_trading_server.log', encoding='utf-8'),  # Added encoding
        logging.StreamHandler(sys.stdout)  # Use the UTF-8 stdout
    ]
)
logger = logging.getLogger(__name__)

# Token usage tracking
token_usage = {
    'total_requests': 0,
    'total_prompt_tokens': 0,
    'total_completion_tokens': 0,
    'total_tokens': 0,
    'session_start': datetime.now(),
    'daily_usage': {},
    'last_request_cost': 0,
    'last_request_tokens': 0,
    'cache_creation_tokens': 0,
    'cache_read_tokens': 0,
    'total_cache_savings': 0
}

# Track statistics across analyses (v2.2 addition)
ANALYSIS_STATS = {
    'total': 0,
    'decisions': {'BUY': 0, 'SELL': 0, 'WAIT': 0},
    'confidence': {'High': 0, 'Medium': 0, 'Low': 0},
    'timeframe_conflicts': 0,
    'rr_failures': 0
}

def update_stats(ai_response):
    """Update statistics based on AI response"""
    ANALYSIS_STATS['total'] += 1

    decision = ai_response.get('decision', 'WAIT')
    ANALYSIS_STATS['decisions'][decision] = ANALYSIS_STATS['decisions'].get(decision, 0) + 1

    confidence = ai_response.get('confidence', 'Medium')
    ANALYSIS_STATS['confidence'][confidence] = ANALYSIS_STATS['confidence'].get(confidence, 0) + 1

    # Check for timeframe conflict mentions
    reasoning = ai_response.get('reasoning', '').lower()
    if 'conflict' in reasoning or 'disagree' in reasoning:
        ANALYSIS_STATS['timeframe_conflicts'] += 1

def print_stats():
    """Print current statistics"""
    total = ANALYSIS_STATS['total']
    if total == 0:
        return

    logger.info("\n" + "="*80)
    logger.info("📊 SESSION STATISTICS")
    logger.info("="*80)
    logger.info(f"Total Analyses: {total}")
    logger.info(f"\nDecisions:")
    for decision, count in ANALYSIS_STATS['decisions'].items():
        pct = (count / total) * 100
        logger.info(f"   {decision}: {count} ({pct:.1f}%)")

    logger.info(f"\nConfidence Levels:")
    for conf, count in ANALYSIS_STATS['confidence'].items():
        pct = (count / total) * 100
        logger.info(f"   {conf}: {count} ({pct:.1f}%)")

    conflicts = ANALYSIS_STATS['timeframe_conflicts']
    if conflicts > 0:
        pct = (conflicts / total) * 100
        logger.info(f"\nTimeframe Conflicts Mentioned: {conflicts} ({pct:.1f}%)")
    logger.info("="*80 + "\n")

# ====== TRIGGERS DATABASE SETUP (V2.3) ======

def init_triggers_db():
    """Initialize triggers database"""
    conn = sqlite3.connect('triggers.db')
    c = conn.cursor()

    # Main triggers table
    c.execute('''
        CREATE TABLE IF NOT EXISTS triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            trigger_json TEXT NOT NULL,
            context_json TEXT,
            playbook TEXT,
            setup_type TEXT,
            expiry_ts TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'PENDING',
            consumed_at TIMESTAMP,
            result TEXT,
            fire_reason TEXT,

            CHECK(status IN ('PENDING', 'CONSUMED', 'EXPIRED', 'SUPERSEDED', 'CLEARED'))
        )
    ''')

    # Index for faster queries
    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_status_symbol
        ON triggers(status, symbol)
    ''')

    # Statistics table
    c.execute('''
        CREATE TABLE IF NOT EXISTS trigger_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL,
            created INTEGER DEFAULT 0,
            fired INTEGER DEFAULT 0,
            expired INTEGER DEFAULT 0,
            converted INTEGER DEFAULT 0,
            UNIQUE(date)
        )
    ''')

    conn.commit()
    conn.close()

    logger.info("✅ Triggers database initialized")


def update_trigger_stats(event_type):
    """Update daily trigger statistics"""
    try:
        conn = sqlite3.connect('triggers.db')
        c = conn.cursor()

        today = datetime.now().date().isoformat()

        # Insert or update
        c.execute('''
            INSERT INTO trigger_stats (date, created, fired, expired, converted)
            VALUES (?, 0, 0, 0, 0)
            ON CONFLICT(date) DO NOTHING
        ''', (today,))

        # Increment the specific counter
        field_map = {
            'created': 'created',
            'fired': 'fired',
            'expired': 'expired',
            'converted': 'converted'
        }

        field = field_map.get(event_type)
        if field:
            c.execute(f'''
                UPDATE trigger_stats
                SET {field} = {field} + 1
                WHERE date = ?
            ''', (today,))

        conn.commit()
        conn.close()

    except Exception as e:
        logger.error(f"⚠️ Stats update error: {e}")


def save_trigger(symbol, analysis, enhanced_context):
    """
    Save trigger when WAIT decision with next_trigger
    - Supersedes any existing pending trigger for this symbol
    - Sets expiry timestamp
    - Validates required fields
    """
    try:
        # Only save if WAIT
        if analysis.get('decision') != 'WAIT':
            return False

        # Validate next_trigger
        next_trigger = analysis.get('next_trigger')
        if not next_trigger or next_trigger.get('type') == 'none':
            logger.info(f"⏸️ WAIT but no actionable trigger for {symbol}")
            return False

        # Validate required fields
        required = ['type', 'timeframe', 'level', 'direction']
        for field in required:
            if field not in next_trigger:
                logger.warning(f"⚠️ Trigger missing: {field}")
                return False

        conn = sqlite3.connect('triggers.db')
        c = conn.cursor()

        # SUPERSEDE any existing pending triggers for this symbol
        c.execute('''
            UPDATE triggers
            SET status='SUPERSEDED', consumed_at=?
            WHERE symbol=? AND status='PENDING'
        ''', (datetime.now().isoformat(), symbol))

        superseded = c.rowcount
        if superseded > 0:
            logger.info(f"🔄 Superseded {superseded} old trigger(s) for {symbol}")

        # Calculate expiry
        expiry_bars = next_trigger.get('expiry_bars', 8)
        timeframe = next_trigger.get('timeframe', 'M15')
        tf_minutes = {'M15': 15, 'M30': 30, 'H1': 60, 'H4': 240}.get(timeframe, 15)
        expiry_ts = datetime.now() + timedelta(minutes=tf_minutes * expiry_bars)

        # Store H4 context
        h4_context = {
            'trend': analysis.get('h4_analysis', {}).get('trend'),
            'trade_bias': analysis.get('h4_analysis', {}).get('trade_bias'),
            'key_levels': analysis.get('h4_analysis', {}).get('key_levels', [])
        }

        # Insert new trigger
        c.execute('''
            INSERT INTO triggers
            (symbol, trigger_json, context_json, playbook, setup_type, expiry_ts, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            symbol,
            json.dumps(next_trigger),
            json.dumps(h4_context),
            analysis.get('playbook', 'unknown'),
            next_trigger.get('type'),
            expiry_ts.isoformat(),
            'PENDING'
        ))

        conn.commit()
        trigger_id = c.lastrowid
        conn.close()

        update_trigger_stats('created')

        logger.info(f"✅ Trigger #{trigger_id} saved for {symbol}")
        logger.info(f"   Type: {next_trigger['type']}")
        logger.info(f"   Level: {next_trigger['level']}")
        logger.info(f"   Expires: {expiry_ts.strftime('%H:%M')}")

        return True

    except Exception as e:
        logger.error(f"❌ Error saving trigger: {e}")
        return False


def clear_pending_triggers(symbol, reason="Main analysis override"):
    """
    Clear ALL pending triggers for a symbol (regardless of direction)

    Args:
        symbol: Trading symbol
        reason: Why triggers are being cleared

    Returns:
        Number of triggers cleared
    """
    try:
        conn = sqlite3.connect('triggers.db')
        c = conn.cursor()

        # Clear ALL pending triggers for this symbol
        c.execute('''
            UPDATE triggers
            SET status='CLEARED', consumed_at=?, fire_reason=?
            WHERE symbol=? AND status='PENDING'
        ''', (datetime.now().isoformat(), reason, symbol))

        cleared_count = c.rowcount
        conn.commit()
        conn.close()

        if cleared_count > 0:
            logger.info(f"🧹 Cleared {cleared_count} pending trigger(s) for {symbol}: {reason}")

        return cleared_count

    except Exception as e:
        logger.error(f"❌ Error clearing triggers: {e}")
        return 0

# ====== SIGNAL TRACKING DATABASE SETUP WITH BREAKEVEN FEATURES ======

def init_database():
    """Initialize SQLite database for signal tracking with breakeven features"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    # Create signals table with breakeven and hypothetical tracking columns
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            decision TEXT NOT NULL,
            confidence TEXT NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            risk_reward TEXT,
            reasoning TEXT,
            market_structure TEXT,
            invalidation_criteria TEXT,
            status TEXT DEFAULT 'ACTIVE',
            result TEXT DEFAULT NULL,
            exit_price REAL DEFAULT NULL,
            exit_timestamp TEXT DEFAULT NULL,
            pnl_pips REAL DEFAULT NULL,
            duration_minutes INTEGER DEFAULT NULL,
            screenshot_path TEXT,
            notes TEXT DEFAULT NULL,
            -- Breakeven tracking columns
            original_stop_loss REAL,
            current_stop_loss REAL,
            breakeven_triggered INTEGER DEFAULT 0,
            breakeven_timestamp TEXT DEFAULT NULL,
            stop_modifications TEXT DEFAULT NULL,
            -- Hypothetical tracking columns
            hypothetical_exit_price REAL DEFAULT NULL,
            hypothetical_result TEXT DEFAULT NULL,
            hypothetical_pnl_pips REAL DEFAULT NULL,
            breakeven_impact TEXT DEFAULT NULL
        )
    ''')
    
    # Create price_updates table for tracking price movements
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS price_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            timestamp TEXT NOT NULL
        )
    ''')
    
    # Create performance_summary table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS performance_summary (
            date TEXT PRIMARY KEY,
            total_signals INTEGER DEFAULT 0,
            buy_signals INTEGER DEFAULT 0,
            sell_signals INTEGER DEFAULT 0,
            wait_signals INTEGER DEFAULT 0,
            winners INTEGER DEFAULT 0,
            losers INTEGER DEFAULT 0,
            breakeven INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0,
            avg_winner_pips REAL DEFAULT 0,
            avg_loser_pips REAL DEFAULT 0,
            total_pips REAL DEFAULT 0,
            risk_reward_achieved REAL DEFAULT 0
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("✅ Signal tracking database initialized with breakeven features")

def migrate_existing_signals():
    """Migrate existing signals to support breakeven tracking"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Add new columns if they don't exist (for upgrading from v1.7)
        columns_to_add = [
            ('original_stop_loss', 'REAL'),
            ('current_stop_loss', 'REAL'),
            ('breakeven_triggered', 'INTEGER DEFAULT 0'),
            ('breakeven_timestamp', 'TEXT DEFAULT NULL'),
            ('stop_modifications', 'TEXT DEFAULT NULL'),
            ('hypothetical_exit_price', 'REAL DEFAULT NULL'),
            ('hypothetical_result', 'TEXT DEFAULT NULL'),
            ('hypothetical_pnl_pips', 'REAL DEFAULT NULL'),
            ('breakeven_impact', 'TEXT DEFAULT NULL')
        ]
        
        for column_name, column_type in columns_to_add:
            try:
                cursor.execute(f'ALTER TABLE signals ADD COLUMN {column_name} {column_type}')
                logger.info(f"Added column {column_name} to signals table")
            except sqlite3.OperationalError:
                # Column already exists
                pass
        
        # Update existing signals to populate new columns
        cursor.execute('''
            UPDATE signals 
            SET original_stop_loss = stop_loss,
                current_stop_loss = stop_loss
            WHERE original_stop_loss IS NULL
        ''')
        
        conn.commit()
        conn.close()
        logger.info("Existing signals migrated for breakeven tracking")
        
    except Exception as e:
        logger.error(f"Migration error: {str(e)}")

def get_pip_multiplier(symbol):
    """
    Get pip multiplier for a given symbol

    Args:
        symbol: Trading symbol (e.g., XAUUSD, EURUSD, USDJPY)

    Returns:
        int: Pip multiplier (10 for gold, 100 for JPY, 10000 for standard forex)
    """
    symbol = str(symbol).upper()

    if any(gold_term in symbol for gold_term in ['XAU', 'GOLD', 'GC']):
        # Gold: 1 pip = 0.1 (e.g., 1950.0 to 1951.0 = 10 pips)
        return 10
    elif any(jpy_pair in symbol for jpy_pair in ['JPY', 'USDJPY', 'EURJPY', 'GBPJPY', 'AUDJPY']):
        # JPY pairs: 1 pip = 0.01 (e.g., 148.00 to 149.00 = 100 pips)
        return 100
    else:
        # Standard forex pairs: 1 pip = 0.0001 (e.g., 1.1000 to 1.1001 = 1 pip)
        return 10000


def get_recent_rates(symbol, timeframe='M15', bars=10):
    """
    SIMPLIFIED VERSION: Uses current price only (no historical bars)

    Returns simulated "bar" using current price for trigger evaluation
    This allows triggers to work with just price_feed.json current prices

    Returns list with single "bar" dict: {open, high, low, close, time}
    """
    try:
        # Get current price from existing function
        current_price = get_current_price(symbol)

        if current_price is None:
            logger.warning(f"⚠️ Cannot get current price for {symbol}")
            return None

        # Create a simulated "bar" using current price
        # For trigger evaluation, we treat current price as both open/close/high/low
        current_time = datetime.now().isoformat()

        simulated_bar = {
            'open': current_price,
            'high': current_price,  # Simplified: assume current is high/low
            'low': current_price,
            'close': current_price,
            'time': current_time
        }

        # Return a list with just this single bar
        # Triggers will check if current price meets condition
        return [simulated_bar]

    except Exception as e:
        logger.error(f"❌ Error getting rates: {e}")
        return None


def eval_trigger(trigger, symbol):
    """
    SIMPLIFIED: Evaluate if trigger condition is met using current price only

    Returns: (met: bool, reason: str)
    """
    try:
        timeframe = trigger.get('timeframe', 'M15')
        rates = get_recent_rates(symbol, timeframe, bars=1)  # Only need current

        if not rates or len(rates) < 1:
            return False, "No price data available"

        level = float(trigger['level'])
        trigger_type = trigger['type']
        direction = trigger['direction']

        current_bar = rates[-1]
        current_price = current_bar['close']

        # Slop for price touching levels (0.5 pips tolerance - slightly more lenient for current price check)
        pip_mult = get_pip_multiplier(symbol)
        slop = 0.5 / pip_mult  # 0.5 pip tolerance

        # SIMPLIFIED EVALUATION (without bar confirmation)
        # Level break - check if price has crossed the level
        if trigger_type == 'level_break':
            if direction == 'above' and current_price > level:
                return True, f"Price at {current_price:.5f} is above {level}"

            elif direction == 'below' and current_price < level:
                return True, f"Price at {current_price:.5f} is below {level}"

        # Retest and hold - check if price is near level (within slop)
        elif trigger_type == 'retest_hold':
            at_level = abs(current_price - level) <= slop

            if direction == 'bullish' and at_level and current_price >= level:
                return True, f"Price at {current_price:.5f} retesting {level} (bullish)"

            elif direction == 'bearish' and at_level and current_price <= level:
                return True, f"Price at {current_price:.5f} retesting {level} (bearish)"

        # Range edge reject - check if price is at boundary
        elif trigger_type == 'range_edge_reject':
            at_boundary = abs(current_price - level) <= slop

            if direction == 'bullish' and at_boundary:
                return True, f"Price at {current_price:.5f} near support {level}"

            elif direction == 'bearish' and at_boundary:
                return True, f"Price at {current_price:.5f} near resistance {level}"

        # EMA retouch - check if price is touching EMA level
        elif trigger_type == 'ema_retouch':
            touching_ema = abs(current_price - level) <= slop

            if touching_ema:
                return True, f"Price at {current_price:.5f} touching EMA {level}"

        return False, f"Condition not met (price: {current_price:.5f}, level: {level})"

    except Exception as e:
        logger.error(f"❌ Trigger evaluation error: {e}")
        return False, f"Error: {str(e)}"

def calculate_pips(entry_price, exit_price, symbol, decision):
    """
    Calculate pips with correct multiplier based on symbol

    Args:
        entry_price: Entry price level
        exit_price: Exit price level
        symbol: Trading symbol (e.g., XAUUSD, EURUSD)
        decision: BUY or SELL

    Returns:
        float: P&L in pips (positive = profit, negative = loss)
    """
    try:
        # Get symbol-specific pip multiplier
        pip_multiplier = get_pip_multiplier(symbol)
        
        # Calculate price difference based on trade direction
        if decision == 'BUY':
            price_diff = exit_price - entry_price
        elif decision == 'SELL':
            price_diff = entry_price - exit_price
        else:
            return 0.0
        
        # Convert to pips
        pips = price_diff * pip_multiplier
        
        return round(pips, 1)
        
    except (ValueError, TypeError) as e:
        logger.error(f"Pips calculation error: {str(e)}")
        return 0.0

def save_signal_to_db(signal_data, screenshot_path):
    """Save new signal to database with breakeven initialization"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO signals (
                timestamp, symbol, timeframe, decision, confidence, entry_price,
                stop_loss, take_profit, risk_reward, reasoning, market_structure,
                invalidation_criteria, screenshot_path, original_stop_loss, current_stop_loss
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().isoformat(),
            signal_data.get('symbol'),
            signal_data.get('timeframe'),
            signal_data.get('decision'),
            signal_data.get('confidence'),
            signal_data.get('entry'),
            signal_data.get('sl'),
            signal_data.get('tp'),
            signal_data.get('risk_reward'),
            signal_data.get('reasoning'),
            signal_data.get('market_structure'),
            signal_data.get('trade_invalidation'),
            screenshot_path,
            signal_data.get('sl'),  # original_stop_loss
            signal_data.get('sl')   # current_stop_loss
        ))
        
        signal_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        logger.info(f"📊 Signal saved to database with ID: {signal_id}")
        return signal_id
        
    except Exception as e:
        logger.error(f"Database save error: {str(e)}")
        return None

def update_signal_result(signal_id, result, exit_price, pnl_pips, notes=None):
    """Update signal with result"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Calculate duration
        cursor.execute('SELECT timestamp FROM signals WHERE id = ?', (signal_id,))
        start_time = datetime.fromisoformat(cursor.fetchone()[0])
        duration = int((datetime.now() - start_time).total_seconds() / 60)
        
        cursor.execute('''
            UPDATE signals SET 
                status = 'CLOSED',
                result = ?,
                exit_price = ?,
                exit_timestamp = ?,
                pnl_pips = ?,
                duration_minutes = ?,
                notes = ?
            WHERE id = ?
        ''', (result, exit_price, datetime.now().isoformat(), pnl_pips, duration, notes, signal_id))
        
        conn.commit()
        conn.close()
        
        logger.info(f"📈 Signal {signal_id} updated: {result} ({pnl_pips:+.1f} pips)")
        return True
        
    except Exception as e:
        logger.error(f"Database update error: {str(e)}")
        return False

def update_signal_with_hypothetical(signal_id, actual_result, actual_exit, actual_pnl,
                                   hyp_result, hyp_exit, hyp_pnl, breakeven_impact):
    """Update signal with both actual and hypothetical outcomes"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Calculate duration
        cursor.execute('SELECT timestamp FROM signals WHERE id = ?', (signal_id,))
        start_time = datetime.fromisoformat(cursor.fetchone()[0])
        duration = int((datetime.now() - start_time).total_seconds() / 60)
        
        cursor.execute('''
            UPDATE signals SET 
                status = 'CLOSED',
                result = ?,
                exit_price = ?,
                exit_timestamp = ?,
                pnl_pips = ?,
                duration_minutes = ?,
                hypothetical_exit_price = ?,
                hypothetical_result = ?,
                hypothetical_pnl_pips = ?,
                breakeven_impact = ?
            WHERE id = ?
        ''', (actual_result, actual_exit, datetime.now().isoformat(), actual_pnl, duration,
              hyp_exit, hyp_result, hyp_pnl, breakeven_impact, signal_id))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Signal {signal_id} updated: {actual_result} ({actual_pnl:+.1f} pips) | Hypothetical: {hyp_result} ({hyp_pnl:+.1f} pips)")
        return True
        
    except Exception as e:
        logger.error(f"Enhanced signal update error: {str(e)}")
        return False

def has_active_signal(symbol):
    """Check if there's already an active signal for this symbol"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, decision, entry_price, COALESCE(current_stop_loss, stop_loss) as effective_sl, 
                   take_profit, timestamp
            FROM signals 
            WHERE symbol = ? AND status = 'ACTIVE' AND decision IN ('BUY', 'SELL')
            ORDER BY timestamp DESC
            LIMIT 1
        ''', (symbol,))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            signal_id, decision, entry, sl, tp, timestamp = result
            return {
                'exists': True,
                'signal_id': signal_id,
                'decision': decision,
                'entry': entry,
                'sl': sl,
                'tp': tp,
                'timestamp': timestamp
            }
        
        return {'exists': False}
        
    except Exception as e:
        logger.error(f"Error checking active signals: {str(e)}")
        return {'exists': False}

def check_breakeven_conditions():
    """Check active signals for breakeven stop-loss adjustment"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Get active signals that haven't been moved to breakeven yet
        cursor.execute('''
            SELECT id, symbol, decision, entry_price, stop_loss, take_profit, 
                   current_stop_loss, breakeven_triggered
            FROM signals 
            WHERE status = 'ACTIVE' 
            AND decision IN ('BUY', 'SELL') 
            AND breakeven_triggered = 0
        ''')
        
        signals = cursor.fetchall()
        conn.close()
        
        for signal in signals:
            signal_id, symbol, decision, entry, original_sl, tp, current_sl, breakeven_triggered = signal
            
            # Get current price
            current_price = get_current_price(symbol)
            if not current_price:
                continue
            
            # Check if we've reached 1:1 risk/reward (breakeven point)
            entry_to_tp_distance = abs(tp - entry)
            breakeven_price = None
            
            if decision == 'BUY':
                # For BUY: breakeven when price reaches entry + (entry - original_sl)
                risk_distance = entry - original_sl
                breakeven_price = entry + risk_distance
                
                if current_price >= breakeven_price:
                    new_stop_loss = entry  # Move SL to entry (breakeven)
                    update_stop_loss_to_breakeven(signal_id, new_stop_loss, current_price)
                    
            elif decision == 'SELL':
                # For SELL: breakeven when price reaches entry - (original_sl - entry)
                risk_distance = original_sl - entry
                breakeven_price = entry - risk_distance
                
                if current_price <= breakeven_price:
                    new_stop_loss = entry  # Move SL to entry (breakeven)
                    update_stop_loss_to_breakeven(signal_id, new_stop_loss, current_price)
        
    except Exception as e:
        logger.error(f"Breakeven check error: {str(e)}")

def update_stop_loss_to_breakeven(signal_id, new_stop_loss, trigger_price):
    """Update signal with new breakeven stop loss"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Get current signal data
        cursor.execute('SELECT symbol, decision, stop_modifications FROM signals WHERE id = ?', (signal_id,))
        signal_data = cursor.fetchone()
        
        if not signal_data:
            return False
            
        symbol, decision, existing_modifications = signal_data
        
        # Parse existing modifications or create new array
        try:
            modifications = json.loads(existing_modifications) if existing_modifications else []
        except:
            modifications = []
        
        # Add new modification record
        modification = {
            'timestamp': datetime.now().isoformat(),
            'type': 'BREAKEVEN',
            'trigger_price': trigger_price,
            'new_stop_loss': new_stop_loss,
            'reason': 'Moved to breakeven at 1:1 R/R'
        }
        modifications.append(modification)
        
        # Update database
        cursor.execute('''
            UPDATE signals SET 
                current_stop_loss = ?,
                breakeven_triggered = 1,
                breakeven_timestamp = ?,
                stop_modifications = ?
            WHERE id = ?
        ''', (new_stop_loss, datetime.now().isoformat(), json.dumps(modifications), signal_id))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Signal {signal_id} moved to breakeven: SL updated to {new_stop_loss}")
        
        # Send Telegram notification
        telegram_message = f"""
🔒 <b>Stop Loss Moved to Breakeven</b>
<b>Signal ID:</b> {signal_id}
<b>Symbol:</b> {symbol}
<b>Decision:</b> {decision}
<b>New Stop Loss:</b> {new_stop_loss}
<b>Trigger Price:</b> {trigger_price}
<b>Status:</b> Risk eliminated - now trading with house money!
"""
        send_telegram_message(telegram_message)
        
        return True
        
    except Exception as e:
        logger.error(f"Breakeven update error: {str(e)}")
        return False

def calculate_breakeven_impact(actual_result, hypothetical_result, actual_pnl, hypothetical_pnl, breakeven_used):
    """Determine the impact of using breakeven strategy"""
    if not breakeven_used:
        return 'NO_BREAKEVEN_USED'
    
    if not hypothetical_result:
        return 'NO_IMPACT'  # Trade still running in hypothetical scenario
    
    if actual_result == 'BREAKEVEN' and hypothetical_result == 'LOSS':
        return 'SAVED_LOSS'  # Breakeven saved us from a loss
    elif actual_result == 'BREAKEVEN' and hypothetical_result == 'WIN':
        return 'MISSED_PROFIT'  # Breakeven caused us to miss profit
    elif actual_result == 'WIN' and hypothetical_result == 'WIN':
        if actual_pnl < hypothetical_pnl:
            return 'REDUCED_PROFIT'  # Won both but made less with breakeven
        else:
            return 'NO_IMPACT'
    else:
        return 'NO_IMPACT'

def check_active_signals():
    """Check all active signals for TP/SL hits, breakeven conditions, and hypothetical tracking"""
    try:
        # First check for breakeven conditions
        check_breakeven_conditions()
        
        # Then check for TP/SL hits using current_stop_loss and track hypothetical outcomes
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, symbol, decision, entry_price, stop_loss, 
                   COALESCE(current_stop_loss, stop_loss) as effective_sl,
                   take_profit, timestamp, breakeven_triggered
            FROM signals 
            WHERE status = 'ACTIVE' AND decision IN ('BUY', 'SELL')
        ''')
        
        active_signals = cursor.fetchall()
        conn.close()
        
        for signal in active_signals:
            signal_id, symbol, decision, entry, original_sl, effective_sl, tp, timestamp, breakeven_triggered = signal
            
            # Get current price
            current_price = get_current_price(symbol)
            if not current_price:
                continue
            
            # Check actual exit conditions (with current stop loss)
            actual_result = None
            actual_exit_price = None
            
            # Check hypothetical exit conditions (with original stop loss)
            hypothetical_result = None
            hypothetical_exit_price = None
            
            if decision == 'BUY':
                # Actual outcome (using current effective stop loss)
                if current_price >= tp:
                    actual_result = 'WIN'
                    actual_exit_price = tp
                elif current_price <= effective_sl:
                    actual_result = 'BREAKEVEN' if effective_sl == entry else 'LOSS'
                    actual_exit_price = effective_sl
                
                # Hypothetical outcome (what would happen with original SL - no breakeven)
                if current_price >= tp:
                    hypothetical_result = 'WIN'
                    hypothetical_exit_price = tp
                elif current_price <= original_sl:
                    hypothetical_result = 'LOSS'
                    hypothetical_exit_price = original_sl
                    
            elif decision == 'SELL':
                # Actual outcome (using current effective stop loss)
                if current_price <= tp:
                    actual_result = 'WIN'
                    actual_exit_price = tp
                elif current_price >= effective_sl:
                    actual_result = 'BREAKEVEN' if effective_sl == entry else 'LOSS'
                    actual_exit_price = effective_sl
                
                # Hypothetical outcome (what would happen with original SL - no breakeven)
                if current_price <= tp:
                    hypothetical_result = 'WIN'
                    hypothetical_exit_price = tp
                elif current_price >= original_sl:
                    hypothetical_result = 'LOSS'
                    hypothetical_exit_price = original_sl
            
            # Close signal if actual result exists
            if actual_result:
                actual_pnl = calculate_pips(entry, actual_exit_price, symbol, decision)
                hypothetical_pnl = calculate_pips(entry, hypothetical_exit_price, symbol, decision) if hypothetical_result and hypothetical_exit_price else 0
                
                # Log what-if analysis results
                logger.info(f"🔍 What-if analysis for {symbol} (ID: {signal_id}):")
                logger.info(f"   Actual: {actual_result} at {actual_exit_price} = {actual_pnl:+.1f} pips")
                if hypothetical_result:
                    logger.info(f"   Hypothetical: {hypothetical_result} at {hypothetical_exit_price} = {hypothetical_pnl:+.1f} pips")
                    logger.info(f"   Difference: {actual_pnl - hypothetical_pnl:+.1f} pips due to breakeven management")
                else:
                    logger.info(f"   Hypothetical: Trade would still be running (no exit triggered)")
                
                # Determine breakeven impact
                breakeven_impact = calculate_breakeven_impact(
                    actual_result, hypothetical_result, 
                    actual_pnl, hypothetical_pnl, 
                    breakeven_triggered
                )
                
                # Update signal with both actual and hypothetical data
                update_signal_with_hypothetical(
                    signal_id, actual_result, actual_exit_price, actual_pnl,
                    hypothetical_result, hypothetical_exit_price, hypothetical_pnl,
                    breakeven_impact
                )
                
                # Send enhanced notification
                send_enhanced_signal_notification(
                    signal_id, symbol, decision, actual_result, actual_pnl,
                    hypothetical_result, hypothetical_pnl, breakeven_impact, timestamp
                )
        
    except Exception as e:
        logger.error(f"Enhanced signal checking error: {str(e)}")

def send_enhanced_signal_notification(signal_id, symbol, decision, actual_result, actual_pnl,
                                   hyp_result, hyp_pnl, breakeven_impact, timestamp):
    """Send notification with breakeven impact analysis"""
    signal_age = datetime.now() - datetime.fromisoformat(timestamp)
    
    # Base message
    status_emoji = "🎯" if actual_result == 'WIN' else "🔒" if actual_result == 'BREAKEVEN' else "❌"
    
    message = f"""
{status_emoji} <b>Signal Closed - ID: {signal_id}</b>
<b>Symbol:</b> {symbol} | <b>Decision:</b> {decision}
<b>Result:</b> {actual_result} ({actual_pnl:+.1f} pips)
<b>Duration:</b> {signal_age}
"""
    
    # Add breakeven impact analysis
    if breakeven_impact == 'SAVED_LOSS':
        message += f"""
💰 <b>Breakeven Impact: SAVED LOSS</b>
Without breakeven: {hyp_result} ({hyp_pnl:+.1f} pips)
Breakeven saved: {abs(hyp_pnl):.1f} pips
"""
    elif breakeven_impact == 'MISSED_PROFIT':
        message += f"""
📉 <b>Breakeven Impact: MISSED PROFIT</b>
Without breakeven: {hyp_result} ({hyp_pnl:+.1f} pips)
Profit missed: {hyp_pnl:.1f} pips
"""
    elif breakeven_impact == 'REDUCED_PROFIT':
        message += f"""
📊 <b>Breakeven Impact: REDUCED PROFIT</b>
Without breakeven: {hyp_pnl:+.1f} pips
Reduction: {hyp_pnl - actual_pnl:.1f} pips
"""
    elif breakeven_impact == 'NO_BREAKEVEN_USED':
        message += "\n💡 <b>Breakeven Impact:</b> Not used"
    
    if actual_result == 'BREAKEVEN':
        message += "\n💡 <b>Risk eliminated by breakeven stop!</b>"
    
    send_telegram_message(message)

def get_current_price(symbol):
    """Get current price from MT5 price feed file with enhanced validation"""
    try:
        price_file = MT5_FILES_PATH / "price_feed.json"
        
        if not price_file.exists():
            logger.error(f"❌ Price feed file not found: {price_file}")
            logger.error(f"   Expected path: {MT5_FILES_PATH}")
            logger.error(f"   Make sure MT5 is running and price_feed.json is being created")
            return None
            
        # Check if file is recent (within last 5 minutes)
        file_age = datetime.now().timestamp() - price_file.stat().st_mtime
        if file_age > 300:  # 5 minutes
            logger.warning(f"⚠️ Price feed file is stale ({file_age/60:.1f} minutes old) for {symbol}")
            logger.warning(f"   Skipping price-dependent actions for safety")
            return None  # Don't use stale data for breakeven/close actions

        with open(price_file, 'r') as f:
            data = json.load(f)
        
        prices = data.get('prices', {})
        if symbol in prices:
            price_data = prices[symbol]
            bid_price = float(price_data['bid'])
            logger.debug(f"✅ Current price for {symbol}: {bid_price}")
            return bid_price
        else:
            available_symbols = list(prices.keys()) if prices else []
            logger.error(f"❌ Symbol {symbol} not found in price feed. Available: {available_symbols}")
            return None
        
    except json.JSONDecodeError as e:
        logger.error(f"❌ Invalid JSON in price feed file: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"❌ Price fetch error for {symbol}: {str(e)}")
        return None

def parse_ai_response(response_text):
    """Parse AI response and extract trading decision with improved validation"""
    try:
        # Remove markdown code blocks if present
        cleaned = response_text.strip()
        if '```json' in cleaned:
            cleaned = cleaned.split('```json')[1].split('```')[0]
        elif '```' in cleaned:
            cleaned = cleaned.split('```')[1].split('```')[0]

        # Remove any leading/trailing whitespace
        cleaned = cleaned.strip()

        # Parse JSON
        data = json.loads(cleaned)

        # Validate required fields
        required_fields = ['decision', 'reasoning', 'confidence']
        for field in required_fields:
            if field not in data:
                raise ValueError(f"Missing required field: {field}")

        # Validate decision value
        if data['decision'] not in ['BUY', 'SELL', 'WAIT']:
            raise ValueError(f"Invalid decision: {data['decision']}")

        # Validate confluence_factors and risk_factors are arrays (not objects)
        if 'confluence_factors' in data:
            if not isinstance(data['confluence_factors'], list):
                print(f"WARNING: confluence_factors is not an array: {type(data['confluence_factors'])}")
                # Try to convert if it's a dict
                if isinstance(data['confluence_factors'], dict):
                    data['confluence_factors'] = list(data['confluence_factors'].values())

        if 'risk_factors' in data:
            if not isinstance(data['risk_factors'], list):
                print(f"WARNING: risk_factors is not an array: {type(data['risk_factors'])}")
                # Try to convert if it's a dict
                if isinstance(data['risk_factors'], dict):
                    data['risk_factors'] = list(data['risk_factors'].values())

        # If decision is not WAIT, validate trade fields
        if data['decision'] in ['BUY', 'SELL']:
            trade_fields = ['entry', 'sl', 'tp', 'risk_reward']
            for field in trade_fields:
                if field not in data or data[field] is None:
                    print(f"WARNING: Trade signal missing field: {field}")
                    # Convert to WAIT if trade fields are missing
                    data['decision'] = 'WAIT'
                    data['reasoning'] += f" [Converted to WAIT: missing {field}]"
                    break

        # Validate RR format if present
        if 'risk_reward' in data and data['risk_reward'] is not None:
            rr = data['risk_reward']
            # Should be in format "X.X:1" or "X:1"
            if not isinstance(rr, str) or ':' not in rr:
                print(f"WARNING: Invalid RR format: {rr}")

        return data

    except json.JSONDecodeError as e:
        print(f"❌ JSON decode error: {e}")
        print(f"Response text (first 500 chars): {response_text[:500]}")
        return None
    except Exception as e:
        print(f"❌ Parse error: {e}")
        print(f"Response text (first 500 chars): {response_text[:500]}")
        return None

def verify_risk_reward(entry, sl, tp, min_rr=1.5):
    """
    Verify that risk-reward ratio meets minimum requirement

    Args:
        entry: Entry price
        sl: Stop loss price
        tp: Take profit price
        min_rr: Minimum required risk-reward ratio (default 1.5)

    Returns:
        tuple: (meets_requirement: bool, actual_rr: float, rr_string: str)
    """
    try:
        if entry is None or sl is None or tp is None:
            return False, 0.0, "0:0"

        # Convert to float if needed
        entry = float(entry)
        sl = float(sl)
        tp = float(tp)

        # Calculate risk and reward
        risk = abs(entry - sl)
        reward = abs(tp - entry)

        if risk == 0:
            return False, 0.0, "0:0"

        # Calculate ratio
        rr_ratio = reward / risk

        # Format as string
        rr_string = f"{rr_ratio:.1f}:1"

        # Check if meets minimum
        meets_min = rr_ratio >= min_rr

        return meets_min, rr_ratio, rr_string

    except Exception as e:
        print(f"⚠️ RR verification error: {e}")
        return False, 0.0, "0:0"

def calculate_performance_stats(days=30):
    """Calculate performance statistics including breakeven metrics"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Get closed signals from last N days
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        
        cursor.execute('''
            SELECT decision, result, pnl_pips, confidence, duration_minutes, breakeven_triggered
            FROM signals 
            WHERE status = 'CLOSED' AND timestamp > ?
        ''', (cutoff_date,))
        
        signals = cursor.fetchall()
        conn.close()
        
        if not signals:
            return None
            
        total_signals = len(signals)
        winners = len([s for s in signals if s[1] == 'WIN'])
        losers = len([s for s in signals if s[1] == 'LOSS'])
        breakeven = len([s for s in signals if s[1] == 'BREAKEVEN'])
        
        win_rate = (winners / total_signals) * 100 if total_signals > 0 else 0
        
        winner_pips = [s[2] for s in signals if s[1] == 'WIN']
        loser_pips = [s[2] for s in signals if s[1] == 'LOSS']

        avg_winner = sum(winner_pips) / len(winner_pips) if winner_pips else 0
        avg_loser = sum(loser_pips) / len(loser_pips) if loser_pips else 0
        total_pips = sum([s[2] for s in signals if s[2] is not None])  # Include zero-pip exits
        
        # Confidence breakdown
        high_conf = len([s for s in signals if s[3] == 'High'])
        med_conf = len([s for s in signals if s[3] == 'Medium'])
        low_conf = len([s for s in signals if s[3] == 'Low'])
        
        high_conf_winners = len([s for s in signals if s[3] == 'High' and s[1] == 'WIN'])
        high_conf_win_rate = (high_conf_winners / high_conf * 100) if high_conf > 0 else 0
        
        # Breakeven statistics
        breakeven_used = len([s for s in signals if s[5] == 1])  # breakeven_triggered = 1
        
        return {
            'period_days': days,
            'total_signals': total_signals,
            'winners': winners,
            'losers': losers,
            'breakeven': breakeven,
            'win_rate': round(win_rate, 2),
            'avg_winner_pips': round(avg_winner, 1),
            'avg_loser_pips': round(avg_loser, 1),
            'total_pips': round(total_pips, 1),
            'confidence_breakdown': {
                'high': high_conf,
                'medium': med_conf,
                'low': low_conf,
                'high_confidence_win_rate': round(high_conf_win_rate, 2)
            },
            'avg_duration_minutes': round(sum([s[4] for s in signals if s[4]]) / len([s for s in signals if s[4]]), 1) if signals else 0,
            'breakeven_stats': {
                'signals_with_breakeven': breakeven_used,
                'breakeven_usage_rate': round((breakeven_used / total_signals * 100) if total_signals > 0 else 0, 1)
            }
        }
        
    except Exception as e:
        logger.error(f"Performance calculation error: {str(e)}")
        return None

def signal_tracking_worker():
    """Background worker to check signals and update performance"""
    logger.info("🔄 Signal tracking worker started with breakeven management")

    # Track cleanup cycles (run cleanup every 360 iterations)
    cleanup_counter = 0

    while True:
        try:
            check_active_signals()

            # Run screenshot cleanup periodically (every 6 hours with 60s interval)
            cleanup_counter += 1
            if cleanup_counter >= 360:
                cleanup_old_screenshots()
                cleanup_counter = 0

            time.sleep(PRICE_UPDATE_INTERVAL)
        except Exception as e:
            logger.error(f"Signal tracking worker error: {str(e)}")
            time.sleep(PRICE_UPDATE_INTERVAL)

# Start background worker
def start_signal_tracking():
    worker_thread = Thread(target=signal_tracking_worker, daemon=True)
    worker_thread.start()

# ====== CLAUDE-SPECIFIC FUNCTIONS WITH SEPARATE PROMPTS ======

def update_token_usage(usage_data):
    """Update token usage statistics for Claude API with cache metrics"""
    global token_usage

    input_tokens = usage_data.get('input_tokens', 0)
    output_tokens = usage_data.get('output_tokens', 0)
    cache_creation_tokens = usage_data.get('cache_creation_input_tokens', 0)
    cache_read_tokens = usage_data.get('cache_read_input_tokens', 0)
    total = input_tokens + output_tokens

    token_usage['total_requests'] += 1
    token_usage['total_prompt_tokens'] += input_tokens
    token_usage['total_completion_tokens'] += output_tokens
    token_usage['total_tokens'] += total
    token_usage['last_request_tokens'] = total
    token_usage['cache_creation_tokens'] += cache_creation_tokens
    token_usage['cache_read_tokens'] += cache_read_tokens

    # Calculate cache savings (cache reads cost 90% less than regular tokens)
    if cache_read_tokens > 0:
        cache_savings = cache_read_tokens * 0.9  # 90% savings on cached tokens
        token_usage['total_cache_savings'] += cache_savings
        logger.info(f"Cache hit! Read {cache_read_tokens} tokens from cache (saved ~{cache_savings:.0f} token cost)")

    if cache_creation_tokens > 0:
        logger.info(f"Cache miss. Created cache with {cache_creation_tokens} tokens")

    # Track daily usage
    today = datetime.now().strftime('%Y-%m-%d')
    if today not in token_usage['daily_usage']:
        token_usage['daily_usage'][today] = {
            'requests': 0,
            'prompt_tokens': 0,
            'completion_tokens': 0,
            'total_tokens': 0,
            'cost': 0,
            'cache_creation_tokens': 0,
            'cache_read_tokens': 0,
            'cache_savings': 0
        }

    token_usage['daily_usage'][today]['requests'] += 1
    token_usage['daily_usage'][today]['prompt_tokens'] += input_tokens
    token_usage['daily_usage'][today]['completion_tokens'] += output_tokens
    token_usage['daily_usage'][today]['total_tokens'] += total
    token_usage['daily_usage'][today]['cache_creation_tokens'] += cache_creation_tokens
    token_usage['daily_usage'][today]['cache_read_tokens'] += cache_read_tokens
    if cache_read_tokens > 0:
        token_usage['daily_usage'][today]['cache_savings'] += cache_read_tokens * 0.9

    # Calculate estimated cost (Claude 3.5 Sonnet pricing)
    input_cost = (input_tokens / 1_000_000) * 3.00  # $3 per 1M input tokens
    output_cost = (output_tokens / 1_000_000) * 15.00  # $15 per 1M output tokens
    total_cost = input_cost + output_cost
    
    token_usage['last_request_cost'] = total_cost
    token_usage['daily_usage'][today]['cost'] += total_cost
    
    # Enhanced console output
    print("\n" + "="*60)
    print("🔢 CLAUDE TOKEN USAGE UPDATE")
    print("="*60)
    print(f"📥 Input Tokens: {input_tokens:,}")
    print(f"📤 Output Tokens: {output_tokens:,}")
    print(f"📊 Total Tokens: {total:,}")
    print(f"💰 This Request Cost: ${total_cost:.4f}")
    print(f"💸 Today's Total Cost: ${token_usage['daily_usage'][today]['cost']:.4f}")
    print(f"📈 Session Total: {token_usage['total_tokens']:,} tokens")
    print(f"💵 Session Cost: ${sum(day['cost'] for day in token_usage['daily_usage'].values()):.4f}")
    print("="*60 + "\n")
    
    logger.info(f"🔢 Token Usage - Input: {input_tokens}, Output: {output_tokens}, Total: {total}")
    logger.info(f"💰 Estimated Cost - Input: ${input_cost:.4f}, Output: ${output_cost:.4f}, Total: ${total_cost:.4f}")
    
    return total_cost

def get_token_usage_summary():
    """Get comprehensive token usage summary with cache metrics"""
    global token_usage

    session_duration = datetime.now() - token_usage['session_start']

    # Calculate total estimated cost
    total_input_cost = (token_usage['total_prompt_tokens'] / 1_000_000) * 3.00  # Claude pricing
    total_output_cost = (token_usage['total_completion_tokens'] / 1_000_000) * 15.00  # Claude pricing
    total_estimated_cost = total_input_cost + total_output_cost

    # Calculate cache cost savings (cache reads cost 10% of normal price)
    cache_savings_cost = (token_usage['total_cache_savings'] / 1_000_000) * 3.00 * 0.9  # 90% savings

    # Get today's stats
    today = datetime.now().strftime('%Y-%m-%d')
    today_stats = token_usage['daily_usage'].get(today, {
        'requests': 0,
        'total_tokens': 0,
        'cost': 0,
        'cache_creation_tokens': 0,
        'cache_read_tokens': 0,
        'cache_savings': 0
    })

    # Calculate cache hit rate
    total_cache_operations = token_usage['cache_creation_tokens'] + token_usage['cache_read_tokens']
    cache_hit_rate = (token_usage['cache_read_tokens'] / total_cache_operations * 100) if total_cache_operations > 0 else 0

    return {
        'session_duration': str(session_duration).split('.')[0],
        'total_requests': token_usage['total_requests'],
        'total_prompt_tokens': token_usage['total_prompt_tokens'],
        'total_completion_tokens': token_usage['total_completion_tokens'],
        'total_tokens': token_usage['total_tokens'],
        'last_request_tokens': token_usage['last_request_tokens'],
        'last_request_cost': round(token_usage['last_request_cost'], 4),
        'cache_metrics': {
            'cache_creation_tokens': token_usage['cache_creation_tokens'],
            'cache_read_tokens': token_usage['cache_read_tokens'],
            'total_cache_savings_tokens': round(token_usage['total_cache_savings'], 2),
            'cache_savings_cost': round(cache_savings_cost, 4),
            'cache_hit_rate': round(cache_hit_rate, 2)
        },
        'estimated_cost': {
            'input_cost': round(total_input_cost, 4),
            'output_cost': round(total_output_cost, 4),
            'total_cost': round(total_estimated_cost, 4),
            'cost_after_cache_savings': round(total_estimated_cost - cache_savings_cost, 4)
        },
        'today': {
            'requests': today_stats.get('requests', 0),
            'tokens': today_stats.get('total_tokens', 0),
            'cost': round(today_stats.get('cost', 0), 4),
            'cache_creation_tokens': today_stats.get('cache_creation_tokens', 0),
            'cache_read_tokens': today_stats.get('cache_read_tokens', 0),
            'cache_savings': round(today_stats.get('cache_savings', 0), 2)
        },
        'daily_usage': token_usage['daily_usage'],
        'average_tokens_per_request': round(token_usage['total_tokens'] / max(token_usage['total_requests'], 1), 2),
        'average_cost_per_request': round(total_estimated_cost / max(token_usage['total_requests'], 1), 4)
    }

def send_telegram_message(message, photo_path=None):
    """Send message to Telegram with optional photo"""
    try:
        if photo_path and os.path.exists(photo_path):
            # Send photo with caption
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            with open(photo_path, 'rb') as photo:
                files = {'photo': photo}
                data = {
                    'chat_id': TELEGRAM_CHAT_ID,
                    'caption': message,
                    'parse_mode': 'HTML'
                }
                response = requests.post(url, files=files, data=data, timeout=30)
        else:
            # Send text only
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = {
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML'
            }
            response = requests.post(url, data=data, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"Telegram send failed: {response.text}")
    except Exception as e:
        logger.error(f"Telegram error: {str(e)}")

def test_claude_connection():
    """Test Claude API connection"""
    try:
        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01"
        }
        test_data = {
            "model": CLAUDE_MODEL,
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "Hello"}]
        }
        test_response = requests.post(ANTHROPIC_API_URL, headers=headers, json=test_data, timeout=5)
        if test_response.status_code == 200:
            print("✅ Claude API connection successful")
            logger.info("✓ Claude API connection successful")
            return True
        else:
            print(f"⚠️ Claude API test returned: {test_response.status_code}")
            logger.warning(f"⚠ Claude API test returned: {test_response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Claude API test failed: {str(e)}")
        logger.error(f"✗ Claude API test failed: {str(e)}")
        return False

def cleanup_old_screenshots():
    """Keep only the last 10 screenshots to save disk space"""
    try:
        screenshots = []
        for filename in os.listdir(UPLOAD_FOLDER):
            if filename.endswith(('.png', '.jpg', '.jpeg')):
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                screenshots.append((filepath, os.path.getmtime(filepath)))
        
        # Sort by modification time
        screenshots.sort(key=lambda x: x[1], reverse=True)
        
        # Delete old files (keep last 10)
        for filepath, _ in screenshots[10:]:
            os.remove(filepath)
            logger.debug(f"Deleted old screenshot: {filepath}")
            
    except Exception as e:
        logger.error(f"Cleanup error: {str(e)}")

# ====== NEW BREAKEVEN AND HYPOTHETICAL TRACKING ENDPOINTS ======


# WEEK 1: Enhanced System Prompt with Multi-Timeframe Analysis
SYSTEM_PROMPT_MULTI_TIMEFRAME = """
You are an expert forex trader analyzing multi-timeframe data to identify high-probability setups with clearly defined risk and favorable reward.

<core_principles>
1. Minimum RR: 1.5:1 (reject lower)
2. Trend: Higher highs/lows (up) or lower highs/lows (down)
3. Range: Equal highs/lows → Look for reversal setups at boundaries
4. RSI extremes: >75 overbought, <25 oversold (context, not hard filter)
5. Don't chase over extended moves - wait for pullbacks or reversals
6. Seek confluence: structure + MAs + momentum + patterns
7. SL beyond invalidation, TP at next structural target or beyond
</core_principles>

<timeframe_hierarchy>
CRITICAL CONCEPT: Different timeframes showing different directions is NORMAL.

H4 = TRADE BIAS (trend direction)
  - Uptrend → Look for LONG entries only at lower timeframes
  - Downtrend → Look for SHORT entries only at lower timeframes
  - Ranging → Look for reversals/rejections at H4 support or resistance levels

H1 = ENTRY ZONE IDENTIFICATION (where to enter)

  If H4 trending, identify pullback completion zones for entry (pullback entry zones) using analysis from H1 chart or H1 indicator data:

  Example chart analysis:
  • Price retraced to support/resistance zone
  • Higher low forming in uptrend (or lower high in downtrend)
  • Consolidation after strong move (sideways candles)
  • Counter-trend candles getting smaller (momentum fading)

  Example indicator data analysis:
  • Price retrace from key moving averages (close ≈ ema_20 or ema_50 in context)
  • RSI reset to 40-60 range (check h1.rsi after prior extreme)
  • Bollinger middle band (bb_position 35-65% indicates neutral zone)

  Example: H4 uptrend + H1 bearish correction = LONG opportunity forming

  If H4 ranging and rejected at support or resistance levels, identify reversal opportunities using analysis from H1 chart or H1 indicator data:

  Example chart analysis:
  • Price at range boundary (support or resistance)
  • Rejection candle forming (pin bar, engulfing, long wick)
  • Multiple touches of same level (established boundary)
  • Price attempting to break but failing (false breakout)

  Example indicator data analysis:
  • RSI at extreme (>70 at resistance, <30 at support)
  • Bollinger extreme (bb_position >80% at top, <20% at bottom)
  • Price at range boundary level (check support/resistance in context)

  KEY: Trade the bounce off range boundaries
  Example: H4 ranging + H1 at resistance + rejection = SHORT opportunity
           H4 ranging + H1 at support + rejection = LONG opportunity

  Simple questions to answer:
  ✓ Where is the entry zone? (pullback zone or range boundary)
  ✓ Is price at that zone now? (check visually + context data)
  ✓ Is structure ready for entry? (momentum fading or rejecting)

M15 = ENTRY TIMING (trigger confirmation when H1 trading zone identified)

  For trend trades (Pullback Triggers):
  • Bullish candlestick pattern (engulfing, pin bar, inside bar break)
  • Break of M15 consolidation in trend direction
  • Momentum shift visible (trend-direction candles resuming)
  • Confirms: "Enter NOW - pullback complete, trend resuming"

  For reversal trades (Reversal Triggers):
  • Rejection candle at boundary (pin bar, engulfing)
  • Failed breakout (price tried to break, reversed back)
  • Momentum shift away from boundary
  • Confirms: "Enter NOW - boundary holding, price reversing"
</timeframe_hierarchy>

<analysis_steps>
1. H4: Determine market state
   - UPTREND → Look for LONG pullback setups
   - DOWNTREND → Look for SHORT pullback setups
   - RANGING → Look for reversal setups at boundaries

2. H1: Identify entry zone
   - IF TRENDING: Where is pullback completing? (S/R, MA, consolidation)
   - IF RANGING: Is price at range boundary? (support or resistance)

3. M15: Check for entry trigger
   - IF TRENDING: Bullish/bearish pattern in trend direction?
   - IF RANGING: Rejection pattern at boundary?

4. RR: Verify (TP - Entry) / (Entry - SL) ≥ 1.5
   - Use next structural level or beyond for TP
   - Use invalidation point for SL

5. Decide:
   - All conditions met → BUY/SELL
   - Missing trigger or zone → WAIT with specific next_trigger
</analysis_steps>

<context_warnings>
Respect but don't overweight market context warnings:

- LOW_LIQUIDITY (Asian session) → Prefer stronger setups, tighter SL
- PRICE_EXTENDED → Prefer pullbacks, avoid chasing
- EXPANDING volatility → Wider stops appropriate

Guidelines:
- Single warning → Trade if setup excellent
- 2+ warnings → Cap confidence at Medium, require better confluence
- Warnings provide context, not automatic rejection
</context_warnings>

<wait_with_triggers>
When decision is WAIT because of the absence of M15 trigger or when waiting to time trade entries on the M15 timeframe, specify WHAT to watch for using next_trigger.

Trigger types:
- level_break: Price breaks above/below a level with N bar confirmation
- retest_hold: Price retests a level and holds (pullback completion or boundary test)
- range_edge_reject: Price touches range boundary and rejects back inside
- ema_retouch: Price returns to touch a moving average level

Guidelines:
- Choose the most specific trigger type for what you're waiting for
- Set level to EXACT price to watch (not approximate)
- Set confirm_bars: 1 for immediate, 2 for stronger confirmation
- Set expiry_bars: 8 bars (~2 hours for M15) is standard, adjust if needed
- Always provide a trigger when WAITing unless fundamentally invalid
</wait_with_triggers>

<output_format>
MUST return ONLY valid JSON (no markdown, no explanations):

{
    "h4_analysis": {
        "trend": "UPTREND|DOWNTREND|RANGING",
        "trade_bias": "LONG_ONLY|SHORT_ONLY|REVERSALS",
        "key_levels": ["1.08500", "1.08200"],
        "range_boundaries": {"support": 1.08200, "resistance": 1.08500}
    },
    "h1_analysis": {
        "structure": "Brief H1 structure description",
        "entry_zone_present": true,
        "entry_zone_type": "pullback|reversal|none",
        "support": 1.08350,
        "resistance": 1.08650
    },
    "m15_entry_setup": {
        "trigger_present": true,
        "trigger_type": "pullback_resume|boundary_reject|none",
        "entry_quality": "EXCELLENT|GOOD|ACCEPTABLE|POOR"
    },
    "decision": "BUY|SELL|WAIT",
    "next_trigger": {
        "type": "level_break|retest_hold|range_edge_reject|ema_retouch|none",
        "timeframe": "M15|H1",
        "level": 1.08350,
        "direction": "above|below|bullish|bearish",
        "confirm_bars": 1,
        "expiry_bars": 8,
        "description": "Wait for price to retest 1.0835 support and hold with bullish rejection"
    },
    "confluence_factors": [
        "H4 uptrend confirmed with higher highs",
        "H1 pullback to support zone at 1.0835",
        "Price at EMA50 on H1 (context data)",
        "RSI reset to 48 from prior 72 (healthy)",
        "Waiting for M15 bullish trigger"
    ],
    "risk_factors": [
        "Asian session with lower liquidity",
        "Pullback not yet showing reversal pattern"
    ],
    "confidence": "High|Medium|Low",
    "reasoning": "H4 shows [trend/range]. H1 shows [entry zone]. M15 [trigger status]. [Decision explanation].",
    "entry": 1.08400,
    "sl": 1.08200,
    "tp": 1.08800,
    "risk_reward": "2.0:1"
}

CRITICAL:
- confluence_factors and risk_factors MUST be arrays []
- If decision is WAIT: set entry/sl/tp/risk_reward to null, PROVIDE next_trigger
- If decision is BUY/SELL: set next_trigger to null (or omit entirely)
- next_trigger.type = "none" only if setup fundamentally flawed (needs more time/data)
- level in next_trigger must be exact price, not range
- risk_reward format: "X.X:1" (e.g. "2.0:1", "1.5:1")
</output_format>

<examples>
EXAMPLE 1: TRENDING - Wait for pullback completion
H4: Uptrend | H1: Bearish pullback in progress to 1.0835 support | M15: No trigger yet
→ WAIT with trigger: 
{
  "h4_analysis": {"trend": "UPTREND", "trade_bias": "LONG_ONLY"},
  "h1_analysis": {"entry_zone_type": "pullback", "entry_zone_present": true},
  "m15_entry_setup": {"trigger_present": false, "trigger_type": "none"},
  "next_trigger": {"type":"retest_hold", "level":1.0835, "direction":"bullish", "confirm_bars":1}
}

EXAMPLE 2: TRENDING - Immediate entry (pullback complete)
H4: Uptrend | H1: Pullback complete at support | M15: Bullish engulfing confirmed
→ BUY:
{
  "h4_analysis": {"trend": "UPTREND", "trade_bias": "LONG_ONLY"},
  "h1_analysis": {"entry_zone_type": "pullback", "entry_zone_present": true},
  "m15_entry_setup": {"trigger_present": true, "trigger_type": "pullback_resume"},
  "decision": "BUY",
  "entry": 1.0838, "sl": 1.0820, "tp": 1.0870,
  "next_trigger": null
}

EXAMPLE 3: RANGING - Wait for boundary
H4: Ranging 1.0800-1.0850 | H1: Price at 1.0825 (mid-range) | M15: Consolidating
→ WAIT:
{
  "h4_analysis": {"trend": "RANGING", "trade_bias": "REVERSALS", 
                  "range_boundaries": {"support": 1.0800, "resistance": 1.0850}},
  "h1_analysis": {"entry_zone_type": "none", "entry_zone_present": false},
  "m15_entry_setup": {"trigger_present": false, "trigger_type": "none"},
  "decision": "WAIT",
  "reasoning": "H4 ranging, price at mid-range 1.0825. Wait for boundary approach.",
  "next_trigger": {"type":"range_edge_reject", "level":1.0850, "direction":"bearish", "confirm_bars":1}
}
</examples>
"""


# ===========================================================================
# WEEK 1 ADDITIONS: Enhanced Market Context and Validation
# ===========================================================================

def get_enhanced_context(symbol, indicator_data):
    """Get market context that Claude can't see from screenshot"""

    current_time = datetime.now(timezone.utc)
    hour = current_time.hour

    # Determine trading session
    if 0 <= hour < 7:
        session = "ASIAN"
        liquidity = "LOW"
    elif 7 <= hour < 13:
        session = "LONDON"
        liquidity = "HIGH"
    elif 13 <= hour < 21:
        session = "NY"
        liquidity = "HIGH"
    else:
        session = "LATE_NY"
        liquidity = "LOW"

    # Get ATR values from indicator data
    atr_h4 = indicator_data.get('h4_atr', indicator_data.get('atr', 0))
    atr_m15 = indicator_data.get('m15_atr', indicator_data.get('atr', 0))

    volatility_state = "EXPANDING" if atr_m15 > atr_h4 else "CONTRACTING"

    # Get price position data
    current_price = indicator_data.get('current_price', 0)
    h4_high_20 = indicator_data.get('h4_high_20', current_price)
    h4_low_20 = indicator_data.get('h4_low_20', current_price)

    h4_range = h4_high_20 - h4_low_20
    if h4_range > 0:
        price_position_pct = ((current_price - h4_low_20) / h4_range) * 100
    else:
        price_position_pct = 50

    # Determine price position interpretation
    if price_position_pct > 80:
        position_interpretation = "Near H4 highs - resistance likely"
    elif price_position_pct < 20:
        position_interpretation = "Near H4 lows - support likely"
    else:
        position_interpretation = "Mid-range - room to move either direction"

    # Check if price is extended
    recent_move_pips = abs(indicator_data.get('price_change_20_candles', 0))
    avg_move_pips = indicator_data.get('avg_price_change', 50)
    is_extended = recent_move_pips > (avg_move_pips * 1.5)

    return {
        "time_context": {
            "current_utc_hour": hour,
            "session": session,
            "liquidity": liquidity,
            "warning": "[WARN]️ LOW_LIQUIDITY - Avoid trading" if liquidity == "LOW" else "[OK] OK"
        },
        "volatility_context": {
            "state": volatility_state,
            "atr_h4": round(atr_h4, 5),
            "atr_m15": round(atr_m15, 5),
            "warning": "[WARN]️ HIGH_VOLATILITY - Widen stops" if volatility_state == "EXPANDING" else "[OK] OK"
        },
        "price_position": {
            "in_h4_range": f"{price_position_pct:.1f}%",
            "interpretation": position_interpretation
        },
        "momentum_warning": {
            "is_extended": is_extended,
            "recent_move_pips": round(recent_move_pips, 1),
            "avg_move_pips": round(avg_move_pips, 1),
            "warning": "[WARN]️ PRICE_EXTENDED - Wait for pullback" if is_extended else "[OK] OK"
        }
    }


def validate_signal_before_execution(signal, context, indicator_data):
    """
    Validate Claude's signal against hard rules
    Returns: (is_valid, rejection_reason)
    """
    # Rule 1: No chasing trend at extreme RSI on M15
    # Support both old (flat) and new (nested) indicator data structures
    if 'm15_indicators' in indicator_data:
        rsi = indicator_data['m15_indicators'].get('rsi_14', 50)
    else:
        rsi = indicator_data.get('m15_rsi', indicator_data.get('rsi', 50))

    if signal.get('decision') == 'BUY' and rsi > 75:
        return False, "RSI_OVERBOUGHT"
    if signal.get('decision') == 'SELL' and rsi < 25:
        return False, "RSI_OVERSOLD"

    # Rule 2: Minimum risk-reward (calculate from actual values)
    entry = signal.get('entry')
    sl = signal.get('sl')
    tp = signal.get('tp')

    if entry and sl and tp:
        try:
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            rr = reward / risk if risk > 0 else 0.0

            if rr < 1.5:
                return False, f"RISK_REWARD_TOO_LOW ({rr:.2f}:1)"
        except Exception as e:
            return False, f"INVALID_RISK_REWARD_CALCULATION: {e}"
    else:
        # Fallback to string parsing if values not available
        rr_str = signal.get('risk_reward', '0:1')
        try:
            rr_parts = rr_str.split(':')
            if len(rr_parts) >= 1:
                rr = float(rr_parts[0])
            else:
                rr = 0.0

            if rr < 1.5:
                return False, "RISK_REWARD_TOO_LOW"
        except:
            return False, "INVALID_RISK_REWARD"

    # Rule 5: Stop loss must be reasonable size (symbol-aware)
    entry = signal.get('entry')
    sl = signal.get('sl')
    symbol = signal.get('symbol', context.get('symbol', 'UNKNOWN'))

    if entry and sl:
        # Use symbol-specific pip multiplier
        pip_multiplier = get_pip_multiplier(symbol)
        stop_size_pips = abs(entry - sl) * pip_multiplier

        if stop_size_pips < 10:
            return False, f"STOP_LOSS_TOO_TIGHT ({stop_size_pips:.1f} pips)"
        if stop_size_pips > 100:
            return False, f"STOP_LOSS_TOO_WIDE ({stop_size_pips:.1f} pips)"

    # Rule 7: If Claude says WAIT, don't trade
    if signal.get('decision') == 'WAIT':
        return False, "CLAUDE_SAYS_WAIT"

    return True, "PASSED_ALL_FILTERS"


def send_multi_timeframe_notification(symbol, analysis, context, m15_screenshot_path=None):
    """Send enhanced Telegram notification with multi-timeframe analysis and M15 chart"""
    try:
        decision = analysis.get('decision', 'UNKNOWN')
        confidence = analysis.get('confidence', 'N/A')
        filter_override = analysis.get('filter_override', False)

        if filter_override:
            emoji = "🚫"
            title = "Signal REJECTED by Filter"
        elif decision == 'BUY':
            emoji = "📈"
            title = "BUY Signal"
        elif decision == 'SELL':
            emoji = "📉"
            title = "SELL Signal"
        else:
            emoji = "⏸️"
            title = "WAIT Signal"

        message = f"""
{emoji} <b>{title} - Multi-Timeframe Analysis v2.3</b>
<b>Symbol:</b> {symbol}
<b>Decision:</b> {decision}
<b>Confidence:</b> {confidence}

<b>📊 H4 Trend:</b> {analysis.get('h4_analysis', {}).get('trend', 'N/A')}
<b>📈 Entry Quality:</b> {analysis.get('m15_entry_setup', {}).get('entry_quality', 'N/A')}

<b>⏰ Session:</b> {context['time_context']['session']} ({context['time_context']['liquidity']} liquidity)
"""

        if filter_override:
            message += f"\n<b>❌ Rejection Reason:</b> {analysis.get('rejection_reason', 'Unknown')}"
        elif decision in ['BUY', 'SELL']:
            message += f"""
<b>Entry:</b> {analysis.get('entry', 'N/A')}
<b>SL:</b> {analysis.get('sl', 'N/A')}
<b>TP:</b> {analysis.get('tp', 'N/A')}
<b>R:R:</b> {analysis.get('risk_reward', 'N/A')}

<b>✅ Checklist:</b> {analysis.get('checklist_results', {}).get('checkboxes_passed', 0)}/8 passed
<b>🆔 Signal ID:</b> {analysis.get('signal_id', 'Pending save')}
"""

        # Add reasoning for all decisions
        reasoning = analysis.get('reasoning', '')
        if reasoning:
            # Truncate reasoning if too long (Telegram caption limit is 1024 chars)
            if len(reasoning) > 400:
                reasoning = reasoning[:400] + "..."
            message += f"\n<b>💡 Reasoning:</b>\n{reasoning}"

        # Send message with M15 chart if available
        send_telegram_message(message, photo_path=m15_screenshot_path)

    except Exception as e:
        logger.error(f"Error sending Telegram notification: {e}")


@app.route('/signal/<int:signal_id>/modifications', methods=['GET'])
def get_signal_modifications(signal_id):
    """Get stop loss modification history for a signal"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT stop_modifications, breakeven_triggered, breakeven_timestamp,
                   original_stop_loss, current_stop_loss
            FROM signals WHERE id = ?
        ''', (signal_id,))
        
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            return jsonify({"error": "Signal not found"}), 404
        
        modifications_json, breakeven_triggered, breakeven_timestamp, original_sl, current_sl = result
        
        try:
            modifications = json.loads(modifications_json) if modifications_json else []
        except:
            modifications = []
        
        return jsonify({
            'signal_id': signal_id,
            'original_stop_loss': original_sl,
            'current_stop_loss': current_sl,
            'breakeven_triggered': bool(breakeven_triggered),
            'breakeven_timestamp': breakeven_timestamp,
            'modifications': modifications
        })
        
    except Exception as e:
        logger.error(f"Modification history error: {str(e)}")
        return jsonify({"error": "Failed to retrieve modifications"}), 500

@app.route('/breakeven_stats', methods=['GET'])
def get_breakeven_stats():
    """Get statistics on breakeven performance"""
    try:
        days = request.args.get('days', 30, type=int)
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Get breakeven statistics
        cursor.execute('''
            SELECT 
                COUNT(*) as total_with_breakeven,
                SUM(CASE WHEN result = 'BREAKEVEN' THEN 1 ELSE 0 END) as breakeven_exits,
                SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins_after_breakeven,
                AVG(CASE WHEN result = 'WIN' THEN pnl_pips ELSE NULL END) as avg_win_pips
            FROM signals 
            WHERE breakeven_triggered = 1 
            AND status = 'CLOSED' 
            AND timestamp > ?
        ''', (cutoff_date,))
        
        breakeven_stats = cursor.fetchone()
        
        conn.close()
        
        return jsonify({
            'period_days': days,
            'signals_with_breakeven': breakeven_stats[0] or 0,
            'breakeven_exits': breakeven_stats[1] or 0,
            'wins_after_breakeven': breakeven_stats[2] or 0,
            'avg_win_pips_after_breakeven': round(breakeven_stats[3] or 0, 1),
            'breakeven_success_rate': round((breakeven_stats[2] / breakeven_stats[0] * 100) if breakeven_stats[0] > 0 else 0, 1)
        })
        
    except Exception as e:
        logger.error(f"Breakeven stats error: {str(e)}")
        return jsonify({"error": "Failed to retrieve breakeven statistics"}), 500

# ====== EXISTING ROUTES WITH BREAKEVEN SUPPORT ======


@app.route('/performance', methods=['GET'])
def get_performance():
    """Get performance statistics including breakeven metrics"""
    days = request.args.get('days', 30, type=int)
    stats = calculate_performance_stats(days)
    
    if stats:
        return jsonify(stats)
    else:
        return jsonify({
            "message": "No performance data available",
            "period_days": days
        })

@app.route('/signals', methods=['GET'])
def get_signals():
    """Get signal history with optional filtering"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Get query parameters
        limit = request.args.get('limit', 50, type=int)
        status = request.args.get('status')  # ACTIVE, CLOSED
        decision = request.args.get('decision')  # BUY, SELL, WAIT
        days = request.args.get('days', 7, type=int)
        
        # Build query with breakeven columns
        query = '''
            SELECT id, timestamp, symbol, timeframe, decision, confidence, 
                   entry_price, stop_loss, take_profit, risk_reward, reasoning,
                   market_structure, status, result, exit_price, pnl_pips,
                   duration_minutes, breakeven_triggered, breakeven_impact
            FROM signals 
            WHERE timestamp > ?
        '''
        params = [(datetime.now() - timedelta(days=days)).isoformat()]
        
        if status:
            query += ' AND status = ?'
            params.append(status)
        
        if decision:
            query += ' AND decision = ?'
            params.append(decision)
        
        query += ' ORDER BY timestamp DESC LIMIT ?'
        params.append(limit)
        
        cursor.execute(query, params)
        signals = cursor.fetchall()
        conn.close()
        
        # Format results
        signal_list = []
        for signal in signals:
            signal_dict = {
                'id': signal[0],
                'timestamp': signal[1],
                'symbol': signal[2],
                'timeframe': signal[3],
                'decision': signal[4],
                'confidence': signal[5],
                'entry_price': signal[6],
                'stop_loss': signal[7],
                'take_profit': signal[8],
                'risk_reward': signal[9],
                'reasoning': signal[10],
                'market_structure': signal[11],
                'status': signal[12],
                'result': signal[13],
                'exit_price': signal[14],
                'pnl_pips': signal[15],
                'duration_minutes': signal[16],
                'breakeven_triggered': bool(signal[17]) if signal[17] is not None else False,
                'breakeven_impact': signal[18]
            }
            signal_list.append(signal_dict)
        
        return jsonify({
            'signals': signal_list,
            'total': len(signal_list),
            'filters': {
                'days': days,
                'status': status,
                'decision': decision,
                'limit': limit
            }
        })
        
    except Exception as e:
        logger.error(f"Signal retrieval error: {str(e)}")
        return jsonify({"error": "Failed to retrieve signals"}), 500


# ====== TRIGGER TELEMETRY ENDPOINTS (V2.3) ======

@app.route('/triggers_summary', methods=['GET'])
def triggers_summary():
    """Get trigger statistics"""
    try:
        conn = sqlite3.connect('triggers.db')
        c = conn.cursor()

        # Get today's stats
        today = datetime.now().date().isoformat()
        c.execute('''
            SELECT created, fired, expired, converted
            FROM trigger_stats
            WHERE date = ?
        ''', (today,))

        row = c.fetchone()
        today_stats = {
            'created': row[0] if row else 0,
            'fired': row[1] if row else 0,
            'expired': row[2] if row else 0,
            'converted': row[3] if row else 0
        }

        # Get pending count
        c.execute('SELECT COUNT(*) FROM triggers WHERE status=?', ('PENDING',))
        pending_count = c.fetchone()[0]

        # Get status breakdown
        c.execute('''
            SELECT status, COUNT(*)
            FROM triggers
            GROUP BY status
        ''')
        status_counts = dict(c.fetchall())

        # Conversion rate
        c.execute('''
            SELECT
                COUNT(CASE WHEN result IN ('BUY', 'SELL') THEN 1 END) as converted,
                COUNT(*) as total
            FROM triggers
            WHERE status = 'CONSUMED'
        ''')

        row = c.fetchone()
        conversion_rate = (row[0] / row[1] * 100) if row[1] > 0 else 0

        conn.close()

        return jsonify({
            'today': today_stats,
            'pending': pending_count,
            'status_breakdown': status_counts,
            'conversion_rate': round(conversion_rate, 1)
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/triggers_pending', methods=['GET'])
def get_pending_triggers_api():
    """Get list of pending triggers"""
    try:
        triggers = get_pending_triggers()

        formatted = []
        for t in triggers:
            formatted.append({
                'id': t['id'],
                'symbol': t['symbol'],
                'type': t['trigger']['type'],
                'level': t['trigger']['level'],
                'direction': t['trigger']['direction'],
                'created_at': t['created_at'],
                'expiry_ts': t['expiry_ts']
            })

        return jsonify({'pending_triggers': formatted})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/signal/<int:signal_id>', methods=['GET'])
def get_signal_details(signal_id):
    """Get detailed information about a specific signal including breakeven data"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM signals WHERE id = ?
        ''', (signal_id,))
        
        signal = cursor.fetchone()
        conn.close()
        
        if not signal:
            return jsonify({"error": "Signal not found"}), 404
        
        # Column names for reference (updated with new columns)
        columns = ['id', 'timestamp', 'symbol', 'timeframe', 'decision', 'confidence',
                  'entry_price', 'stop_loss', 'take_profit', 'risk_reward', 'reasoning',
                  'market_structure', 'invalidation_criteria', 'status', 'result',
                  'exit_price', 'exit_timestamp', 'pnl_pips', 'duration_minutes',
                  'screenshot_path', 'notes', 'original_stop_loss', 'current_stop_loss',
                  'breakeven_triggered', 'breakeven_timestamp', 'stop_modifications',
                  'hypothetical_exit_price', 'hypothetical_result', 'hypothetical_pnl_pips',
                  'breakeven_impact']
        
        signal_dict = dict(zip(columns, signal))
        
        # Parse stop_modifications if it exists
        if signal_dict.get('stop_modifications'):
            try:
                signal_dict['stop_modifications'] = json.loads(signal_dict['stop_modifications'])
            except:
                signal_dict['stop_modifications'] = []
        
        return jsonify(signal_dict)
        
    except Exception as e:
        logger.error(f"Signal detail error: {str(e)}")
        return jsonify({"error": "Failed to retrieve signal details"}), 500

@app.route('/signal/<int:signal_id>/close', methods=['POST'])
def close_signal_manually(signal_id):
    """Manually close a signal with result"""
    try:
        data = request.get_json()
        result = data.get('result')  # WIN, LOSS, BREAKEVEN
        exit_price = data.get('exit_price')
        notes = data.get('notes', '')
        
        if not result or not exit_price:
            return jsonify({"error": "Result and exit_price required"}), 400
        
        # Calculate P&L
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        cursor.execute('SELECT decision, entry_price, symbol FROM signals WHERE id = ?', (signal_id,))
        signal_data = cursor.fetchone()
        conn.close()
        
        if not signal_data:
            return jsonify({"error": "Signal not found"}), 404
        
        decision, entry_price, symbol = signal_data

        # Use the calculate_pips function
        pnl_pips = calculate_pips(entry_price, exit_price, symbol, decision)
        
        # Update signal
        success = update_signal_result(signal_id, result, exit_price, pnl_pips, notes)
        
        if success:
            return jsonify({
                "message": "Signal closed successfully",
                "signal_id": signal_id,
                "result": result,
                "pnl_pips": round(pnl_pips, 1)
            })
        else:
            return jsonify({"error": "Failed to close signal"}), 500
            
    except Exception as e:
        logger.error(f"Manual signal close error: {str(e)}")
        return jsonify({"error": "Failed to close signal"}), 500

@app.route('/active_signals', methods=['GET'])
def get_active_signals():
    """Get all currently active signals with breakeven info"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, symbol, timeframe, decision, entry_price, 
                   COALESCE(current_stop_loss, stop_loss) as effective_sl, 
                   take_profit, timestamp, reasoning, confidence, 
                   breakeven_triggered, breakeven_timestamp
            FROM signals 
            WHERE status = 'ACTIVE' AND decision IN ('BUY', 'SELL')
            ORDER BY symbol, timestamp DESC
        ''')
        
        signals = cursor.fetchall()
        conn.close()
        
        active_list = []
        for signal in signals:
            signal_time = datetime.fromisoformat(signal[7])
            age = datetime.now() - signal_time
            
            active_list.append({
                'id': signal[0],
                'symbol': signal[1],
                'timeframe': signal[2],
                'decision': signal[3],
                'entry': signal[4],
                'sl': signal[5],  # This is the effective SL (current or original)
                'tp': signal[6],
                'timestamp': signal[7],
                'age_minutes': int(age.total_seconds() / 60),
                'reasoning': signal[8],
                'confidence': signal[9],
                'breakeven_triggered': bool(signal[10]) if signal[10] is not None else False,
                'breakeven_timestamp': signal[11]
            })
        
        return jsonify({
            'active_signals': active_list,
            'total_active': len(active_list),
            'blocking_enabled': ENABLE_SIGNAL_BLOCKING
        })
        
    except Exception as e:
        logger.error(f"Error getting active signals: {str(e)}")
        return jsonify({"error": "Failed to retrieve active signals"}), 500

@app.route('/token_usage', methods=['GET'])
def get_usage():
    """Get detailed token usage statistics"""
    usage_summary = get_token_usage_summary()
    return jsonify(usage_summary)

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint with token usage and signal tracking status"""
    usage_summary = get_token_usage_summary()
    
    # Get active signals count
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM signals WHERE status = 'ACTIVE'")
        active_signals = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM signals")
        total_signals = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM signals WHERE breakeven_triggered = 1")
        breakeven_signals = cursor.fetchone()[0]
        conn.close()
    except:
        active_signals = 0
        total_signals = 0
        breakeven_signals = 0
    
    return jsonify({
        "status": "running",
        "timestamp": datetime.now().isoformat(),
        "ai_model": CLAUDE_MODEL,
        "screenshot_folder": UPLOAD_FOLDER,
        "signal_tracking": {
            "active_signals": active_signals,
            "total_signals": total_signals,
            "breakeven_signals": breakeven_signals,
            "database_file": DATABASE_FILE,
            "signal_blocking_enabled": ENABLE_SIGNAL_BLOCKING,
            "breakeven_management_enabled": True
        },
        "token_usage": {
            "session_total": usage_summary['total_tokens'],
            "total_requests": usage_summary['total_requests'],
            "estimated_cost": usage_summary['estimated_cost']['total_cost'],
            "session_duration": usage_summary['session_duration'],
            "today_cost": usage_summary['today']['cost']
        }
    })

@app.route('/', methods=['GET'])
def index():
    """Root endpoint with info and performance summary"""
    usage_summary = get_token_usage_summary()
    performance = calculate_performance_stats(7)  # Last 7 days
    
    return jsonify({
        "service": "AI Trading Screenshot Analysis Server",
        "version": "2.3",
        "ai_model": CLAUDE_MODEL,
        "endpoints": {
            "/analyze_multi_timeframe": "POST - Multi-timeframe chart analysis (H4, H1, M15)",
            "/performance": "GET - Get performance statistics (?days=N)",
            "/signals": "GET - Get signal history (?limit=N&status=ACTIVE|CLOSED&decision=BUY|SELL|WAIT&days=N)",
            "/signal/<id>": "GET - Get signal details",
            "/signal/<id>/close": "POST - Manually close signal",
            "/signal/<id>/modifications": "GET - Get stop loss modification history",
            "/active_signals": "GET - Get all active signals",
            "/breakeven_stats": "GET - Get breakeven performance statistics",
            "/health": "GET - Health check with signal tracking status",
            "/token_usage": "GET - Detailed token usage statistics"
        },
        "current_session": {
            "requests": usage_summary['total_requests'],
            "tokens": usage_summary['total_tokens'],
            "cost": f"${usage_summary['estimated_cost']['total_cost']:.4f}",
            "today_cost": f"${usage_summary['today']['cost']:.4f}"
        },
        "performance_last_7_days": performance,
        "features": [
            "Claude 3.5 Sonnet visual chart pattern recognition",
            "Advanced market structure analysis with examples", 
            "Separate Gold (reversal) and Forex (trend/reversal) strategies",
            "Automated breakeven stop-loss management",
            "Hypothetical 'what-if' scenario tracking",
            "Breakeven impact analysis and notifications",
            "Real-time TP/SL monitoring with breakeven adjustments",
            "Performance analytics and statistics",
            "Signal history and filtering",
            "Manual signal management",
            "Telegram notifications with breakeven alerts",
            "Token usage tracking",
            "Signal blocking - one signal per symbol at a time"
        ]
    })

@app.route('/performance_report', methods=['GET'])
def get_performance_report():
    """Get detailed performance report with formatted statistics including breakeven metrics"""
    days = request.args.get('days', 30, type=int)
    
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Get closed signals from last N days
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        
        # Get overall statistics including breakeven data
        cursor.execute('''
            SELECT 
                COUNT(*) as total_signals,
                SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as losers,
                SUM(CASE WHEN result = 'BREAKEVEN' THEN 1 ELSE 0 END) as breakeven,
                SUM(pnl_pips) as total_pips,
                AVG(CASE WHEN result = 'WIN' THEN pnl_pips ELSE NULL END) as avg_winner,
                AVG(CASE WHEN result = 'LOSS' THEN pnl_pips ELSE NULL END) as avg_loser,
                AVG(duration_minutes) as avg_duration,
                SUM(CASE WHEN breakeven_triggered = 1 THEN 1 ELSE 0 END) as signals_with_breakeven
            FROM signals 
            WHERE status = 'CLOSED' AND timestamp > ?
        ''', (cutoff_date,))
        
        stats = cursor.fetchone()
        
        # Get breakeven impact analysis
        cursor.execute('''
            SELECT 
                breakeven_impact,
                COUNT(*) as count,
                AVG(pnl_pips) as avg_pips,
                AVG(hypothetical_pnl_pips) as avg_hypothetical_pips
            FROM signals 
            WHERE status = 'CLOSED' AND timestamp > ? AND breakeven_impact IS NOT NULL
            GROUP BY breakeven_impact
        ''', (cutoff_date,))
        
        breakeven_impact_stats = cursor.fetchall()
        
        # Get performance by symbol
        cursor.execute('''
            SELECT 
                symbol,
                COUNT(*) as trades,
                SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as losses,
                SUM(pnl_pips) as total_pips,
                AVG(pnl_pips) as avg_pips,
                SUM(CASE WHEN breakeven_triggered = 1 THEN 1 ELSE 0 END) as breakeven_used
            FROM signals 
            WHERE status = 'CLOSED' AND timestamp > ?
            GROUP BY symbol
            ORDER BY total_pips DESC
        ''', (cutoff_date,))
        
        symbol_performance = cursor.fetchall()
        
        # Get performance by confidence level
        cursor.execute('''
            SELECT 
                confidence,
                COUNT(*) as trades,
                SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
                SUM(pnl_pips) as total_pips,
                SUM(CASE WHEN breakeven_triggered = 1 THEN 1 ELSE 0 END) as breakeven_used
            FROM signals 
            WHERE status = 'CLOSED' AND timestamp > ?
            GROUP BY confidence
        ''', (cutoff_date,))
        
        confidence_performance = cursor.fetchall()
        
        # Get daily performance
        cursor.execute('''
            SELECT 
                DATE(timestamp) as trading_date,
                COUNT(*) as trades,
                SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
                SUM(pnl_pips) as daily_pips,
                SUM(CASE WHEN breakeven_triggered = 1 THEN 1 ELSE 0 END) as breakeven_used
            FROM signals 
            WHERE status = 'CLOSED' AND timestamp > ?
            GROUP BY DATE(timestamp)
            ORDER BY trading_date DESC
            LIMIT 10
        ''', (cutoff_date,))
        
        daily_performance = cursor.fetchall()
        
        # Get best and worst trades
        cursor.execute('''
            SELECT 
                id, symbol, decision, entry_price, exit_price, pnl_pips, 
                result, timestamp, confidence, breakeven_triggered, breakeven_impact
            FROM signals 
            WHERE status = 'CLOSED' AND timestamp > ?
            ORDER BY pnl_pips DESC
            LIMIT 5
        ''', (cutoff_date,))
        
        best_trades = cursor.fetchall()
        
        cursor.execute('''
            SELECT 
                id, symbol, decision, entry_price, exit_price, pnl_pips, 
                result, timestamp, confidence, breakeven_triggered, breakeven_impact
            FROM signals 
            WHERE status = 'CLOSED' AND timestamp > ?
            ORDER BY pnl_pips ASC
            LIMIT 5
        ''', (cutoff_date,))
        
        worst_trades = cursor.fetchall()

        # Get all signals with result and pnl_pips for profit factor calculation
        cursor.execute('''
            SELECT id, result, pnl_pips
            FROM signals
            WHERE status = 'CLOSED' AND timestamp > ?
        ''', (cutoff_date,))

        signals = cursor.fetchall()

        conn.close()

        # Calculate derived statistics
        total_signals = stats[0] or 0
        winners = stats[1] or 0
        losers = stats[2] or 0
        breakeven = stats[3] or 0
        total_pips = stats[4] or 0
        avg_winner = stats[5] or 0
        avg_loser = stats[6] or 0
        avg_duration = stats[7] or 0
        signals_with_breakeven = stats[8] or 0

        win_rate = (winners / total_signals * 100) if total_signals > 0 else 0

        # Calculate profit factor properly: sum(wins) / abs(sum(losses))
        sum_wins = sum([s[2] for s in signals if s[1] == 'WIN' and s[2] is not None])
        sum_losses = sum([s[2] for s in signals if s[1] == 'LOSS' and s[2] is not None])
        profit_factor = sum_wins / abs(sum_losses) if sum_losses != 0 else 0
        
        # Format breakeven impact data
        impact_data = []
        for impact in breakeven_impact_stats:
            impact_data.append({
                'impact_type': impact[0],
                'count': impact[1],
                'avg_actual_pips': round(impact[2], 1),
                'avg_hypothetical_pips': round(impact[3], 1) if impact[3] else 0
            })
        
        # Format symbol performance
        symbols_data = []
        for symbol in symbol_performance:
            symbol_win_rate = (symbol[2] / symbol[1] * 100) if symbol[1] > 0 else 0
            symbols_data.append({
                'symbol': symbol[0],
                'trades': symbol[1],
                'wins': symbol[2],
                'losses': symbol[3],
                'win_rate': round(symbol_win_rate, 1),
                'total_pips': round(symbol[4], 1),
                'avg_pips': round(symbol[5], 1),
                'breakeven_used': symbol[6]
            })
        
        # Format confidence performance
        confidence_data = []
        for conf in confidence_performance:
            conf_win_rate = (conf[2] / conf[1] * 100) if conf[1] > 0 else 0
            confidence_data.append({
                'level': conf[0],
                'trades': conf[1],
                'wins': conf[2],
                'win_rate': round(conf_win_rate, 1),
                'total_pips': round(conf[3], 1),
                'breakeven_used': conf[4]
            })
        
        # Format daily performance
        daily_data = []
        for day in daily_performance:
            day_win_rate = (day[2] / day[1] * 100) if day[1] > 0 else 0
            daily_data.append({
                'date': day[0],
                'trades': day[1],
                'wins': day[2],
                'win_rate': round(day_win_rate, 1),
                'pips': round(day[3], 1),
                'breakeven_used': day[4]
            })
        
        # Format best/worst trades
        best_trades_data = []
        for trade in best_trades:
            best_trades_data.append({
                'id': trade[0],
                'symbol': trade[1],
                'decision': trade[2],
                'entry': trade[3],
                'exit': trade[4],
                'pips': round(trade[5], 1),
                'result': trade[6],
                'date': trade[7],
                'confidence': trade[8],
                'breakeven_used': bool(trade[9]) if trade[9] is not None else False,
                'breakeven_impact': trade[10]
            })
        
        worst_trades_data = []
        for trade in worst_trades:
            worst_trades_data.append({
                'id': trade[0],
                'symbol': trade[1],
                'decision': trade[2],
                'entry': trade[3],
                'exit': trade[4],
                'pips': round(trade[5], 1),
                'result': trade[6],
                'date': trade[7],
                'confidence': trade[8],
                'breakeven_used': bool(trade[9]) if trade[9] is not None else False,
                'breakeven_impact': trade[10]
            })
        
        # Build comprehensive report
        report = {
            'period_days': days,
            'report_generated': datetime.now().isoformat(),
            'summary': {
                'total_signals': total_signals,
                'winners': winners,
                'losers': losers,
                'breakeven': breakeven,
                'win_rate': round(win_rate, 1),
                'total_pips': round(total_pips, 1),
                'avg_winner_pips': round(avg_winner, 1),
                'avg_loser_pips': round(avg_loser, 1),
                'profit_factor': round(profit_factor, 2),
                'avg_duration_minutes': round(avg_duration, 1) if avg_duration else 0,
                'signals_with_breakeven': signals_with_breakeven,
                'breakeven_usage_rate': round((signals_with_breakeven / total_signals * 100) if total_signals > 0 else 0, 1)
            },
            'breakeven_impact_analysis': impact_data,
            'by_symbol': symbols_data,
            'by_confidence': confidence_data,
            'recent_daily': daily_data,
            'best_trades': best_trades_data,
            'worst_trades': worst_trades_data,
            'performance_grade': get_performance_grade(win_rate, profit_factor, total_pips)
        }
        
        return jsonify(report)
        
    except Exception as e:
        logger.error(f"Performance report error: {str(e)}")
        return jsonify({"error": "Failed to generate performance report"}), 500

def get_performance_grade(win_rate, profit_factor, total_pips):
    """Calculate a performance grade based on metrics"""
    if win_rate >= 60 and profit_factor >= 2 and total_pips > 0:
        return "A+ - Excellent"
    elif win_rate >= 55 and profit_factor >= 1.5 and total_pips > 0:
        return "A - Very Good"
    elif win_rate >= 50 and profit_factor >= 1.2 and total_pips > 0:
        return "B - Good"
    elif win_rate >= 45 and total_pips >= 0:
        return "C - Average"
    elif total_pips >= 0:
        return "D - Below Average"
    else:
        return "F - Poor"

@app.route('/performance_telegram', methods=['POST'])
def send_performance_telegram():
    """Send performance report to Telegram including breakeven stats"""
    days = request.args.get('days', 7, type=int)
    
    try:
        # Get performance stats
        stats = calculate_performance_stats(days)
        
        if not stats:
            return jsonify({"error": "No performance data available"}), 404
        
        # Format telegram message
        telegram_message = f"""
📊 <b>PERFORMANCE REPORT v2.3 - Last {days} Days</b>
═════════════════════════════════════

📈 <b>Overall Statistics:</b>
• Total Signals: {stats['total_signals']}
• Winners: {stats['winners']} | Losers: {stats['losers']} | BE: {stats['breakeven']}
• Win Rate: {stats['win_rate']}%
• Total P&L: {stats['total_pips']:+.1f} pips

💰 <b>Trade Quality:</b>
• Avg Winner: {stats['avg_winner_pips']:+.1f} pips
• Avg Loser: {stats['avg_loser_pips']:+.1f} pips
• Profit Factor: {stats['profit_factor']:.2f}

🔒 <b>Breakeven Management:</b>
• Signals with Breakeven: {stats['breakeven_stats']['signals_with_breakeven']}
• Breakeven Usage Rate: {stats['breakeven_stats']['breakeven_usage_rate']}%

🎯 <b>Confidence Analysis:</b>
• High Confidence Trades: {stats['confidence_breakdown']['high']}
• High Conf Win Rate: {stats['confidence_breakdown']['high_confidence_win_rate']}%
• Medium Confidence: {stats['confidence_breakdown']['medium']}
• Low Confidence: {stats['confidence_breakdown']['low']}

⏱ <b>Avg Trade Duration:</b> {stats['avg_duration_minutes']:.0f} minutes

📅 <b>Report Date:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        
        send_telegram_message(telegram_message)
        
        return jsonify({
            "message": "Performance report sent to Telegram",
            "period_days": days
        })
        
    except Exception as e:
        logger.error(f"Telegram performance report error: {str(e)}")
        return jsonify({"error": "Failed to send performance report"}), 500

@app.route('/weekly_summary', methods=['GET'])
def get_weekly_summary():
    """Get a weekly performance summary with trend analysis including breakeven data"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        weekly_data = []
        
        # Get last 4 weeks of data
        for week in range(4):
            week_start = datetime.now() - timedelta(days=(week+1)*7)
            week_end = datetime.now() - timedelta(days=week*7)
            
            cursor.execute('''
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(pnl_pips) as pips,
                    SUM(CASE WHEN breakeven_triggered = 1 THEN 1 ELSE 0 END) as breakeven_used
                FROM signals 
                WHERE status = 'CLOSED' 
                AND timestamp BETWEEN ? AND ?
            ''', (week_start.isoformat(), week_end.isoformat()))
            
            week_stats = cursor.fetchone()
            
            if week_stats[0] > 0:
                weekly_data.append({
                    'week': f"Week {week+1}",
                    'period': f"{week_start.strftime('%Y-%m-%d')} to {week_end.strftime('%Y-%m-%d')}",
                    'trades': week_stats[0],
                    'wins': week_stats[1],
                    'win_rate': round((week_stats[1] / week_stats[0] * 100), 1),
                    'total_pips': round(week_stats[2] or 0, 1),
                    'breakeven_used': week_stats[3]
                })
        
        conn.close()
        
        # Calculate trend
        if len(weekly_data) >= 2:
            current_week_pips = weekly_data[0]['total_pips']
            previous_week_pips = weekly_data[1]['total_pips']
            trend = "📈 Improving" if current_week_pips > previous_week_pips else "📉 Declining"
        else:
            trend = "📊 Insufficient Data"
        
        return jsonify({
            'weekly_performance': weekly_data,
            'trend': trend,
            'generated_at': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Weekly summary error: {str(e)}")
        return jsonify({"error": "Failed to generate weekly summary"}), 500

# ====== TRADING HOURS & COOLDOWN FUNCTIONS ======

def is_symbol_analysis_allowed(symbol):
    """
    Prevent any analysis for a symbol at least 1 hour after the close of the last trade
    Returns True if analysis is allowed, False if still in cooldown period
    """
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Get the most recent closed signal for this symbol
        cursor.execute('''
            SELECT exit_timestamp
            FROM signals 
            WHERE symbol = ? 
            AND status = 'CLOSED' 
            AND exit_timestamp IS NOT NULL
            ORDER BY exit_timestamp DESC 
            LIMIT 1
        ''', (symbol,))
        
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            # No previous closed trades for this symbol, analysis is allowed
            return True
            
        last_exit_time_str = result[0]
        last_exit_time = datetime.fromisoformat(last_exit_time_str)
        current_time = datetime.now()
        
        # Calculate time difference
        time_diff = current_time - last_exit_time
        cooldown_hours = 1  # 1 hour cooldown
        
        if time_diff.total_seconds() < (cooldown_hours * 3600):
            # Still in cooldown period
            remaining_time = (cooldown_hours * 3600) - time_diff.total_seconds()
            remaining_minutes = int(remaining_time / 60)
            logger.info(f"Symbol {symbol} still in cooldown. {remaining_minutes} minutes remaining.")
            return False
        else:
            # Cooldown period has passed, analysis is allowed
            logger.info(f"Symbol {symbol} cooldown period expired. Analysis allowed.")
            return True
            
    except Exception as e:
        logger.error(f"Error checking symbol analysis cooldown for {symbol}: {str(e)}")
        # In case of error, allow analysis (fail-safe)
        return True

def get_daily_net_wins():
    """
    Calculate net wins for today (wins - losses)
    Returns the net win count for the current day
    """
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Get today's date range
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
        
        # Count wins and losses for today
        cursor.execute('''
            SELECT 
                SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as losses,
                COUNT(*) as total_trades
            FROM signals 
            WHERE status = 'CLOSED' 
            AND timestamp BETWEEN ? AND ?
        ''', (today_start.isoformat(), today_end.isoformat()))
        
        result = cursor.fetchone()
        conn.close()
        
        wins = result[0] or 0
        losses = result[1] or 0
        total_trades = result[2] or 0
        net_wins = wins - losses
        
        logger.info(f"Daily stats - Wins: {wins}, Losses: {losses}, Net: {net_wins}, Total: {total_trades}")
        
        return {
            'net_wins': net_wins,
            'wins': wins,
            'losses': losses,
            'total_trades': total_trades
        }
        
    except Exception as e:
        logger.error(f"Error calculating daily net wins: {str(e)}")
        # In case of error, return 0 net wins (fail-safe)
        return {
            'net_wins': 0,
            'wins': 0,
            'losses': 0,
            'total_trades': 0
        }

def get_risky_active_trades():
    """
    Get count of active trades that are NOT at breakeven (still at risk)
    Returns count of risky trades and list of their details
    """
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        
        # Get active trades that are NOT at breakeven
        cursor.execute('''
            SELECT id, symbol, decision, entry_price, stop_loss, breakeven_triggered
            FROM signals 
            WHERE status = 'ACTIVE' 
            AND (breakeven_triggered = 0 OR breakeven_triggered IS NULL)
        ''', ())
        
        risky_trades = cursor.fetchall()
        conn.close()
        
        risky_count = len(risky_trades)
        risky_details = []
        
        for trade in risky_trades:
            risky_details.append({
                'signal_id': trade[0],
                'symbol': trade[1],
                'decision': trade[2],
                'entry_price': trade[3],
                'stop_loss': trade[4],
                'breakeven_triggered': trade[5]
            })
        
        logger.info(f"Risky active trades count: {risky_count}")
        
        return {
            'risky_count': risky_count,
            'risky_trades': risky_details
        }
        
    except Exception as e:
        logger.error(f"Error checking risky active trades: {str(e)}")
        # In case of error, assume there are risky trades (fail-safe)
        return {
            'risky_count': 999,  # High number to prevent stopping analysis
            'risky_trades': []
        }

def is_daily_analysis_allowed():
    """
    Check if analysis is allowed based on daily net wins limit and active trade status
    Stop analysis when:
    1. Net wins >= 3 AND
    2. No risky active trades (either no active trades OR all active trades are at breakeven)
    """
    daily_stats = get_daily_net_wins()
    risky_trades = get_risky_active_trades()
    
    net_wins = daily_stats['net_wins']
    risky_count = risky_trades['risky_count']
    daily_limit = 3  # Stop analysis when net wins reach 3
    
    if net_wins >= daily_limit and risky_count == 0:
        logger.info(f"Daily analysis STOPPED - Net wins: {net_wins}/{daily_limit}, Risky active trades: {risky_count}")
        return False
    elif net_wins >= daily_limit and risky_count > 0:
        logger.info(f"Daily limit reached but waiting for {risky_count} risky trades to reach breakeven - Net wins: {net_wins}/{daily_limit}")
        return True
    else:
        logger.info(f"Daily analysis allowed - Net wins: {net_wins}/{daily_limit}, Risky trades: {risky_count}")
        return True


# ====== TRIGGER PROCESSING FUNCTIONS (V2.3) ======

def is_valid_trading_time():
    """Check if within trading hours"""
    current_hour = datetime.now().hour
    # Customize: Allow 06:00-20:00 UTC
    return 6 <= current_hour < 20


def is_news_window(symbol, minutes_before=30):
    """
    Check if within X minutes of high-impact news
    TODO: Implement actual news calendar check
    """
    # Placeholder - implement your news calendar logic
    return False


def get_pending_triggers():
    """Get all pending triggers from database"""
    try:
        conn = sqlite3.connect('triggers.db')
        c = conn.cursor()

        c.execute('''
            SELECT id, symbol, trigger_json, context_json, expiry_ts, created_at
            FROM triggers
            WHERE status = 'PENDING'
            ORDER BY created_at ASC
        ''')

        results = []
        for row in c.fetchall():
            results.append({
                'id': row[0],
                'symbol': row[1],
                'trigger': json.loads(row[2]),
                'context': json.loads(row[3]) if row[3] else {},
                'expiry_ts': row[4],
                'created_at': row[5]
            })

        conn.close()
        return results

    except Exception as e:
        logger.error(f"❌ Error fetching triggers: {e}")
        return []


def mark_trigger_status(trigger_id, status, result=None, fire_reason=None):
    """Update trigger status in database"""
    try:
        conn = sqlite3.connect('triggers.db')
        c = conn.cursor()

        c.execute('''
            UPDATE triggers
            SET status=?, consumed_at=?, result=?, fire_reason=?
            WHERE id=?
        ''', (status, datetime.now().isoformat(), result, fire_reason, trigger_id))

        conn.commit()
        conn.close()

        # Update stats
        if status == 'EXPIRED':
            update_trigger_stats('expired')
        elif status == 'CONSUMED':
            update_trigger_stats('fired')
            if result in ['BUY', 'SELL']:
                update_trigger_stats('converted')

    except Exception as e:
        logger.error(f"❌ Error updating trigger: {e}")


def log_trade_signal(symbol, analysis, from_trigger=False):
    """Log trade signal"""
    source = "TRIGGER" if from_trigger else "DIRECT"
    logger.info(f"\n{'='*80}")
    logger.info(f"📊 TRADE SIGNAL ({source})")
    logger.info(f"{'='*80}")
    logger.info(f"Symbol: {symbol}")
    logger.info(f"Decision: {analysis['decision']}")
    logger.info(f"Entry: {analysis.get('entry')}")
    logger.info(f"SL: {analysis.get('sl')}")
    logger.info(f"TP: {analysis.get('tp')}")
    logger.info(f"RR: {analysis.get('risk_reward')}")
    logger.info(f"Confidence: {analysis.get('confidence')}")
    logger.info(f"{'='*80}\n")

    # TODO: Add your trade execution logic here


def get_current_market_context(symbol):
    """Get current market context"""
    now = datetime.now()
    hour = now.hour

    # Determine session
    if 0 <= hour < 7:
        session = "ASIAN"
        liquidity = "LOW"
    elif 7 <= hour < 13:
        session = "LONDON"
        liquidity = "HIGH"
    elif 13 <= hour < 17:
        session = "OVERLAP"
        liquidity = "VERY_HIGH"
    elif 17 <= hour < 21:
        session = "NEW_YORK"
        liquidity = "HIGH"
    else:
        session = "CLOSING"
        liquidity = "MEDIUM"

    return {
        "session": session,
        "liquidity": liquidity,
        "timestamp": now.isoformat()
    }


def re_analyze_with_trigger(symbol, trigger, context):
    """
    Re-analyze with trigger hit (lightweight version)

    Args:
        symbol: Trading symbol
        trigger: Trigger that fired
        context: Cached H4 context

    Returns:
        Parsed AI response or None
    """
    try:
        logger.info(f"🔄 Re-analyzing {symbol} with trigger hit...")

        # Get latest M15 data only (save tokens)
        m15_data = get_recent_rates(symbol, 'M15', bars=20)

        if not m15_data:
            logger.error(f"❌ No M15 data for {symbol}")
            return None

        # Build trigger context message
        trigger_context = f"""
TRIGGER EVENT OCCURRED:
The condition you specified has now happened:
- Type: {trigger['type']}
- Level: {trigger['level']}
- Direction: {trigger['direction']}
- Timeframe: {trigger['timeframe']}

Re-evaluate: Is this now a valid entry setup, or should we continue to WAIT?

H4 Context (from original analysis):
- Trend: {context.get('trend', 'UNKNOWN')}
- Bias: {context.get('trade_bias', 'NONE')}
"""

        # Get current market context
        current_context = get_current_market_context(symbol)

        # Build prompt (use existing prompt + trigger context)
        system_prompt = SYSTEM_PROMPT_MULTI_TIMEFRAME + "\n\n" + trigger_context

        # Build lightweight user message
        user_message = f"""
Symbol: {symbol}

H4 Analysis (cached):
- Trend: {context.get('trend')}
- Trade Bias: {context.get('trade_bias')}
- Key Levels: {context.get('key_levels', [])}

M15 Latest Bars (last 5):
{json.dumps(m15_data[-5:], indent=2)}

Current Market Context:
{json.dumps(current_context, indent=2)}

Analyze and provide decision.
"""

        # Call Claude API
        response = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}]
        )

        if not response:
            return None

        # Parse response
        response_text = response.content[0].text
        parsed = parse_ai_response(response_text)

        return parsed

    except Exception as e:
        logger.error(f"❌ Re-analysis error: {e}")
        return None


def process_pending_triggers():
    """
    Main trigger processing loop
    Call every 1-2 minutes
    """
    try:
        pending = get_pending_triggers()

        if not pending:
            return

        logger.info(f"\n{'='*80}")
        logger.info(f"🔍 Processing {len(pending)} pending trigger(s)")
        logger.info(f"{'='*80}")

        for t in pending:
            trigger_id = t['id']
            symbol = t['symbol']
            trigger = t['trigger']
            context = t['context']
            expiry_ts = t['expiry_ts']

            # Check if expired
            if datetime.now() > datetime.fromisoformat(expiry_ts):
                mark_trigger_status(trigger_id, 'EXPIRED')
                logger.info(f"⏰ Trigger #{trigger_id} expired for {symbol}")
                continue

            # Check session restrictions
            if not is_valid_trading_time():
                continue

            # Check news window
            if is_news_window(symbol):
                continue

            # Evaluate trigger condition
            met, reason = eval_trigger(trigger, symbol)

            if not met:
                continue

            logger.info(f"\n✅ TRIGGER FIRED!")
            logger.info(f"   ID: #{trigger_id}")
            logger.info(f"   Symbol: {symbol}")
            logger.info(f"   Reason: {reason}")

            # Re-analyze with trigger hit flag
            result = re_analyze_with_trigger(
                symbol=symbol,
                trigger=trigger,
                context=context
            )

            if result:
                decision = result.get('decision', 'UNKNOWN')
                logger.info(f"   Re-analysis: {decision}")

                mark_trigger_status(trigger_id, 'CONSUMED', decision, reason)

                if decision in ['BUY', 'SELL']:
                    logger.info(f"🎯 Trigger converted to {decision} signal!")
                    log_trade_signal(symbol, result, from_trigger=True)
                else:
                    logger.info(f"⏸️ Re-analysis still returned WAIT")
            else:
                logger.error(f"❌ Re-analysis failed")
                mark_trigger_status(trigger_id, 'CONSUMED', 'ERROR', reason)

        logger.info(f"{'='*80}\n")

    except Exception as e:
        logger.error(f"❌ Error in trigger processing: {e}")


def start_trigger_watcher(interval_seconds=120):
    """
    Start background thread to process triggers

    Args:
        interval_seconds: Check interval (default 120 = 2 minutes)
    """
    def watcher_loop():
        logger.info("🚀 Trigger watcher started")

        while True:
            try:
                process_pending_triggers()
            except Exception as e:
                logger.error(f"❌ Watcher error: {e}")

            time.sleep(interval_seconds)

    # Start daemon thread
    watcher_thread = Thread(target=watcher_loop, daemon=True)
    watcher_thread.start()

    logger.info(f"✅ Trigger watcher thread started (every {interval_seconds}s)")


# WEEK 1: Multi-Timeframe Analysis Endpoint
@app.route('/analyze_multi_timeframe', methods=['POST'])
def analyze_multi_timeframe():
    """
    New endpoint for multi-timeframe analysis
    Receives paths to 3 screenshots + indicator data
    """
    try:
        data = request.get_json()

        symbol = data.get('symbol', 'UNKNOWN')
        h4_screenshot = data.get('h4_screenshot')
        h1_screenshot = data.get('h1_screenshot')
        m15_screenshot = data.get('m15_screenshot')
        indicator_data = data.get('indicators', {})

        logger.info(f"=== MULTI-TIMEFRAME ANALYSIS REQUEST: {symbol} ===")

        # Check if signal blocking is enabled and symbol already has active signal
        if ENABLE_SIGNAL_BLOCKING:
            active_check = has_active_signal(symbol)
            if active_check['exists']:
                logger.warning(f"Analysis blocked - Symbol {symbol} already has active {active_check['decision']} signal (ID: {active_check['signal_id']})")
                return jsonify({
                    "error": "Symbol already has active signal",
                    "decision": "WAIT",
                    "active_signal": {
                        "id": active_check['signal_id'],  # Fixed: was 'id', should be 'signal_id'
                        "decision": active_check['decision'],
                        "entry": active_check['entry'],
                        "stop_loss": active_check['sl'],  # Fixed: was 'stop_loss', should be 'sl'
                        "take_profit": active_check['tp']  # Fixed: was 'take_profit', should be 'tp'
                    }
                }), 409  # 409 Conflict

        # Check cooldown period for this symbol
        if not is_symbol_analysis_allowed(symbol):
            logger.warning(f"Analysis blocked - Symbol {symbol} in cooldown period (1 hour after last trade closed)")
            return jsonify({"error": "Symbol in cooldown period", "decision": "WAIT"}), 429

        # Check daily analysis limits
        if not is_daily_analysis_allowed():
            logger.warning(f"Analysis blocked - Daily net wins limit reached")
            return jsonify({"error": "Daily analysis limit reached", "decision": "WAIT"}), 429

        # Check for too many risky active trades
        risky_trades = get_risky_active_trades()
        if risky_trades['risky_count'] > 3:  # Max 3 risky trades at a time
            logger.warning(f"Analysis blocked - Too many risky active trades ({risky_trades['risky_count']})")
            return jsonify({"error": "Too many risky active trades", "decision": "WAIT"}), 429

        # Validate screenshot files exist
        if not all([h4_screenshot, h1_screenshot, m15_screenshot]):
            return jsonify({"error": "Missing screenshot paths"}), 400

        # Get enhanced market context
        context = get_enhanced_context(symbol, indicator_data)
        logger.info(f"Market context: {context['time_context']['session']} session, {context['time_context']['liquidity']} liquidity")

        # Build user prompt with context warnings
        user_prompt = f"""<chart_analysis_request>
<market_context>
Symbol: {symbol}
Current Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

⏰ TIME CONTEXT:
- Session: {context['time_context']['session']}
- Liquidity: {context['time_context']['liquidity']}
- {context['time_context']['warning']}

📊 VOLATILITY:
- State: {context['volatility_context']['state']}
- H4 ATR: {context['volatility_context']['atr_h4']}
- M15 ATR: {context['volatility_context']['atr_m15']}
- {context['volatility_context']['warning']}

📍 PRICE POSITION:
- In H4 Range: {context['price_position']['in_h4_range']}
- {context['price_position']['interpretation']}

⚡ MOMENTUM:
- Recent Move: {context['momentum_warning']['recent_move_pips']} pips
- Average Move: {context['momentum_warning']['avg_move_pips']} pips
- {context['momentum_warning']['warning']}
</market_context>

[WARN]️ CRITICAL: Review ALL warnings above. If any warning contains "[WARN]️", be EXTRA cautious.

<indicator_data>
{json.dumps(indicator_data, indent=2)}
</indicator_data>

<task>
You are provided with THREE chart screenshots for {symbol}:
1. First image: H4 timeframe (trend context)
2. Second image: H1 timeframe (market structure)
3. Third image: M15 timeframe (entry timing)

Perform top-down analysis:
1. Analyze H4 for overall trend direction and key levels
2. Analyze H1 for market structure and confluence
3. Analyze M15 for precise entry opportunity
4. Complete the entry checklist
5. Make decision: BUY / SELL / WAIT

Remember: If ANY mandatory rule is violated or checklist item fails, return WAIT.
</task>
</chart_analysis_request>"""

        # Encode all 3 images
        images_content = []
        for screenshot_path, tf_name in [(h4_screenshot, 'H4'),
                                          (h1_screenshot, 'H1'),
                                          (m15_screenshot, 'M15')]:
            try:
                # Detect media type from file extension
                file_ext = screenshot_path.lower().split('.')[-1]
                if file_ext == 'jpg' or file_ext == 'jpeg':
                    media_type = "image/jpeg"
                elif file_ext == 'png':
                    media_type = "image/png"
                else:
                    media_type = "image/png"  # Default fallback

                with open(screenshot_path, 'rb') as f:
                    image_data_bytes = base64.b64encode(f.read()).decode('utf-8')
                    images_content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_data_bytes
                        }
                    })
                logger.info(f"[OK] Loaded {tf_name} screenshot ({media_type})")
            except Exception as e:
                logger.error(f"Error loading {tf_name} screenshot: {e}")
                return jsonify({"error": f"Failed to load {tf_name} screenshot"}), 500

        # Build message with all 3 images
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                *images_content  # All 3 images
            ]
        }]

        # Call Claude API with prompt caching
        logger.info("Sending request to Claude API with 3 images...")
        response = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            temperature=0.3,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT_MULTI_TIMEFRAME,
                    "cache_control": {"type": "ephemeral"}
                }
            ],
            messages=messages
        )

        # Update token usage
        update_token_usage(response.usage.__dict__)

        # Extract response
        response_text = response.content[0].text
        logger.info(f"📥 Claude response received ({len(response_text)} chars)")

        # Parse JSON response using improved parser
        analysis = parse_ai_response(response_text)

        if not analysis:
            logger.error(f"❌ Failed to parse Claude response")
            return jsonify({"error": "Invalid JSON response from Claude"}), 500

        # Log multi-timeframe analysis
        logger.info("="*80)
        if 'h4_analysis' in analysis:
            h4 = analysis['h4_analysis']
            logger.info(f"📊 H4 Analysis: Trend={h4.get('trend', 'N/A')}, Bias={h4.get('trade_bias', 'N/A')}")

        if 'h1_analysis' in analysis:
            h1 = analysis['h1_analysis']
            logger.info(f"📊 H1 Analysis: Entry Zone Present={h1.get('entry_zone_present', 'N/A')}")

        if 'm15_entry_setup' in analysis:
            m15 = analysis['m15_entry_setup']
            logger.info(f"📊 M15 Analysis: Trigger={m15.get('trigger_present', 'N/A')}, Quality={m15.get('entry_quality', 'N/A')}")

        # Log confluence factors
        if 'confluence_factors' in analysis and isinstance(analysis['confluence_factors'], list):
            logger.info(f"✅ Confluence Factors ({len(analysis['confluence_factors'])}):")
            for factor in analysis['confluence_factors'][:5]:  # Show first 5
                logger.info(f"   • {factor}")

        # Log risk factors
        if 'risk_factors' in analysis and isinstance(analysis['risk_factors'], list) and len(analysis['risk_factors']) > 0:
            logger.info(f"⚠️  Risk Factors ({len(analysis['risk_factors'])}):")
            for factor in analysis['risk_factors'][:3]:  # Show first 3
                logger.info(f"   • {factor}")
        logger.info("="*80)

        # Add symbol to analysis for validation (needed for pip calculations)
        analysis['symbol'] = symbol

        # Log decision
        decision = analysis.get('decision', 'WAIT')
        if decision in ['BUY', 'SELL']:
            logger.info(f"🎯 {decision} signal generated")
            logger.info(f"   Entry: {analysis.get('entry', 'N/A')}")
            logger.info(f"   SL: {analysis.get('sl', 'N/A')}")
            logger.info(f"   TP: {analysis.get('tp', 'N/A')}")
            logger.info(f"   RR: {analysis.get('risk_reward', 'N/A')}")

            # Verify RR
            entry = analysis.get('entry')
            sl = analysis.get('sl')
            tp = analysis.get('tp')

            meets_rr, actual_rr, rr_string = verify_risk_reward(entry, sl, tp, min_rr=1.5)

            if not meets_rr:
                logger.warning(f"⚠️ WARNING: RR {rr_string} does not meet minimum 1.5:1")
                logger.warning(f"   Reported RR: {analysis.get('risk_reward', 'N/A')}")
                logger.warning(f"   Calculated RR: {rr_string}")
            else:
                logger.info(f"✅ RR verified: {rr_string} (meets 1.5:1 minimum)")
        else:
            logger.info(f"⏸️  WAIT decision")
            # Extract reason from reasoning if available
            reasoning = analysis.get('reasoning', '')
            if reasoning:
                # Show first 200 chars of reasoning
                logger.info(f"   Reason: {reasoning[:200]}...")

        # Update statistics (v2.2 addition)
        update_stats(analysis)

        # VALIDATE SIGNAL with hard-coded filters (only for BUY/SELL, skip WAIT)

        if decision in ['BUY', 'SELL']:
            # Only validate actual trading signals
            is_valid, rejection_reason = validate_signal_before_execution(
                analysis, context, indicator_data
            )

            if not is_valid:
                logger.warning(f"⛔ Signal REJECTED by filter: {rejection_reason}")
                analysis['original_decision'] = decision
                analysis['decision'] = 'WAIT'
                analysis['rejection_reason'] = rejection_reason
                analysis['filter_override'] = True
                analysis['note'] = f"Claude suggested {analysis['original_decision']}, but overridden: {rejection_reason}"
            else:
                logger.info(f"✅ Signal PASSED all filters")
                analysis['filter_override'] = False
        else:
            # WAIT signal - no validation needed
            logger.info(f"📊 WAIT signal from Claude - no validation required")
            analysis['filter_override'] = False

        # Add context to response
        analysis['market_context'] = context

        logger.info(f"Final Decision: {analysis['decision']} (Confidence: {analysis.get('confidence', 'N/A')})")

        # V2.3: Handle triggers based on decision
        final_decision = analysis.get('decision')

        if final_decision in ['BUY', 'SELL'] and not analysis.get('filter_override', False):
            # Clear ALL pending triggers for this symbol
            cleared = clear_pending_triggers(
                symbol,
                reason=f"Trade signal: {final_decision}"
            )

            # Log trade signal
            log_trade_signal(symbol, analysis, from_trigger=False)

            if cleared > 0:
                logger.info(f"   (Cleared {cleared} pending trigger(s))")

        elif final_decision == 'WAIT':
            # Save new trigger (supersedes any existing)
            saved = save_trigger(symbol, analysis, context)
            if saved:
                logger.info(f"💾 Trigger saved for {symbol}")

        # Save valid BUY/SELL signals to database
        if not analysis.get('filter_override', False) and analysis.get('decision') in ['BUY', 'SELL']:
            signal_data = {
                'symbol': symbol,
                'timeframe': 'Multi-TF (H4+H1+M15)',
                'decision': analysis.get('decision'),
                'confidence': analysis.get('confidence'),
                'entry': analysis.get('entry'),
                'sl': analysis.get('sl'),
                'tp': analysis.get('tp'),
                'risk_reward': analysis.get('risk_reward'),
                'reasoning': analysis.get('reasoning', ''),
                'market_structure': str(analysis.get('h1_analysis', {})),
                'trade_invalidation': analysis.get('trade_invalidation', '')
            }
            screenshot_path = m15_screenshot  # Use M15 screenshot as primary reference
            signal_id = save_signal_to_db(signal_data, screenshot_path)
            if signal_id:
                analysis['signal_id'] = signal_id
                logger.info(f"✅ Signal saved to database with ID: {signal_id}")
            else:
                logger.error("❌ Failed to save signal to database")

        # Send Telegram notification with M15 chart
        send_multi_timeframe_notification(symbol, analysis, context, m15_screenshot)

        return jsonify(analysis)

    except Exception as e:
        logger.error(f"Error in multi-timeframe analysis: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    print("="*60)
    print("🚀 AI TRADING SCREENSHOT ANALYSIS SERVER v2.3")
    print("="*60)
    print(f"🤖 Using AI model: {CLAUDE_MODEL}")
    print(f"💰 Claude Pricing: $3.00/1M input, $15.00/1M output tokens")
    print("🆕 Multi-timeframe analysis (H4 + H1 + M15)")
    print("📊 Simplified indicators (12 essential indicators)")
    print("🔍 Enhanced with breakeven stop-loss management")
    print("🚫 Signal blocking enabled - one signal per symbol")
    print("✅ 7 hard-coded validation filters")
    print("💾 Prompt caching enabled (25-35% cost reduction)")
    print("🎯 V2.3: Trigger system for conditional setups")
    print("="*60)

    # Initialize databases
    init_database()
    init_triggers_db()

    # Migrate existing signals for breakeven support
    migrate_existing_signals()

    # Start signal tracking worker
    start_signal_tracking()

    # Start trigger watcher (2 min intervals)
    start_trigger_watcher(interval_seconds=120)
    
    # Test Claude connection
    if test_claude_connection():
        print("✅ Claude API connection successful")
    else:
        print("❌ Claude API connection failed - check your API key")
    
    # Send startup notification
    send_telegram_message("🚀 AI Trading Screenshot Analysis Server v2.3 started - Multi-Timeframe Analysis Ready")
    
    print("="*60)
    print("\n⏳ Waiting for analysis requests and tracking signals with breakeven management...\n")
    
    # Run the server on port 5001 (v1.7 uses port 5000)
    app.run(host='0.0.0.0', port=5001, debug=True)