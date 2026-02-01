import sqlite3
import logging
import yfinance as yf
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# First, we setup logging so we can see if our bot has any errors in the background
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

# --- DATABASE SETUP ---
# I am using SQLite to save user data so it is not deleted when I restart the bot
def init_db():
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    # This table saves: User ID, Stock Symbol, Target Price, and Last Price for drop alerts
    cursor.execute('''CREATE TABLE IF NOT EXISTS watchlist 
                      (user_id INTEGER, symbol TEXT, target_price REAL, last_price REAL)''')
    conn.commit()
    conn.close()

# Run the database function when the script starts
init_db()

# --- TOP STOCKS COMMAND ---
# This command shows the performance of the 10 biggest companies in India (Nifty 10)
async def top_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wait_msg = await update.message.reply_text("Fetching performance of top 10 Nifty stocks...")
    nifty_top_10 = [
        "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", 
        "SBIN.NS", "BHARTIARTL.NS", "LTIM.NS", "ITC.NS", "TITAN.NS"
    ]
    
    summary = "--- TOP 10 NIFTY STOCKS ---\n\n"
    for symbol in nifty_top_10:
        try:
            # We get 2 days of data to calculate the percentage change from yesterday
            ticker = yf.Ticker(symbol)
            data = ticker.history(period="2d")
            if len(data) < 2: continue
            
            price = data['Close'].iloc[-1]
            prev_close = data['Close'].iloc[-2]
            change = ((price - prev_close) / prev_close) * 100
            
            summary += f"{symbol}: Rs. {price:.2f} ({change:+.2f}%)\n"
        except:
            summary += f"{symbol}: Data unavailable\n"
            
    await wait_msg.edit_text(summary)

# --- BACKGROUND JOB: CHECK ALERTS ---
# This is the "Smart" part. It runs every hour automatically.
async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    # Get all stocks that people have added to their list
    cursor.execute("SELECT user_id, symbol, target_price, last_price FROM watchlist")
    rows = cursor.fetchall()
    
    for user_id, symbol, target, last_price in rows:
        try:
            data = yf.Ticker(symbol).history(period="1d")
            current_price = data['Close'].iloc[-1]

            # Logic 1: Check if the stock reached the target price set by the user
            if target and current_price >= target:
                await context.bot.send_message(
                    chat_id=user_id, 
                    text=f"TARGET HIT: {symbol} has reached Rs. {current_price:.2f} (Target: {target})"
                )
                # After hit, remove the target so we don't spam the user
                cursor.execute("UPDATE watchlist SET target_price = NULL WHERE user_id = ? AND symbol = ?", (user_id, symbol))

            # Logic 2: Check if the stock dropped by 5% or more
            if last_price:
                drop_percent = ((last_price - current_price) / last_price) * 100
                if drop_percent >= 5.0:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"PRICE DROP ALERT: {symbol} fell by {drop_percent:.2f}%\n"
                             f"Previous: Rs. {last_price:.2f} -> Now: Rs. {current_price:.2f}"
                    )
            
            # We always update the last_price so the next check is accurate
            cursor.execute("UPDATE watchlist SET last_price = ? WHERE user_id = ? AND symbol = ?", (current_price, user_id, symbol))
            
        except: continue
    
    conn.commit()
    conn.close()

# --- COMMAND HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    welcome_text = (
        f"Namaste {user_name}. Welcome to Market Master v4.0.\n\n"
        "Available Commands:\n"
        "1. /price [symbol] - Live rates\n"
        "2. /add [symbol] - Add to watchlist\n"
        "3. /watchlist - View your saved stocks\n"
        "4. /top - View top 10 Nifty stocks\n"
        "5. /remove [symbol] - Remove from watchlist\n"
        "6. /guide - Help with symbols"
    )
    await update.message.reply_text(welcome_text)

# Users can set a target price using /alert TCS.NS 4000
async def set_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /alert [symbol] [price]\nExample: /alert TCS.NS 4200")
        return
    
    symbol, target = context.args[0].upper(), float(context.args[1])
    user_id = update.effective_user.id
    
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    # Update target price in database
    cursor.execute("UPDATE watchlist SET target_price = ? WHERE user_id = ? AND symbol = ?", (target, user_id, symbol))
    if cursor.rowcount == 0:
        cursor.execute("INSERT INTO watchlist (user_id, symbol, target_price) VALUES (?, ?, ?)", (user_id, symbol, target))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Alert set for {symbol} at Rs. {target}.")

async def guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guide_text = (
        "--- BEGINNER GUIDE ---\n"
        "For Indian stocks, add '.NS'.\n"
        "Example: /price TATASTEEL.NS\n"
        "Example: /add RELIANCE.NS"
    )
    await update.message.reply_text(guide_text)

# Gets live price using yfinance library
async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide a symbol. Example: /price TCS.NS")
        return
    symbol = context.args[0].upper()
    try:
        data = yf.Ticker(symbol).history(period="5d")
        if data.empty:
            await update.message.reply_text(f"No data found for {symbol}.")
            return
        current_price = data['Close'].iloc[-1]
        await update.message.reply_text(f"Price of {symbol}: Rs. {current_price:.2f}")
    except:
        await update.message.reply_text("Error fetching data.")

# Saves a stock to the database watchlist
async def add_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add [symbol]")
        return
    symbol = context.args[0].upper()
    user_id = update