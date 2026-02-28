import sqlite3
import logging
import yfinance as yf
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# Setup Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    # Updated table to store target_price for alerts and last_price for drop tracking
    cursor.execute('''CREATE TABLE IF NOT EXISTS watchlist
                      (user_id INTEGER, symbol TEXT, target_price REAL, last_price REAL)''')
    conn.commit()
    conn.close()

init_db()

# --- NEW: TOP STOCKS COMMAND ---
async def top_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wait_msg = await update.message.reply_text("Fetching performance of top 10 Nifty stocks...")
    # List of major Nifty bluechip stocks
    nifty_top_10 = [
        "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
        "SBIN.NS", "BHARTIARTL.NS", "LTIM.NS", "ITC.NS", "TITAN.NS"
    ]

    summary = "--- TOP 10 NIFTY STOCKS ---\n\n"
    for symbol in nifty_top_10:
        try:
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
async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, symbol, target_price, last_price FROM watchlist")
    rows = cursor.fetchall()

    for user_id, symbol, target, last_price in rows:
        try:
            data = yf.Ticker(symbol).history(period="1d")
            current_price = data['Close'].iloc[-1]

            # 1. Check for specific Price Alert (User set target)
            if target and current_price >= target:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"TARGET HIT: {symbol} has reached Rs. {current_price:.2f} (Target: {target})"
                )
                # Reset target to avoid repeated alerts
                cursor.execute("UPDATE watchlist SET target_price = NULL WHERE user_id = ? AND symbol = ?", (user_id, symbol))

            # 2. Check for 5% Price Drop Alert
            if last_price:
                drop_percent = ((last_price - current_price) / last_price) * 100
                if drop_percent >= 5.0:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"PRICE DROP ALERT: {symbol} fell by {drop_percent:.2f}%\n"
                             f"Previous: Rs. {last_price:.2f} -> Now: Rs. {current_price:.2f}"
                    )

            # Always update last_price to the most recent price for future comparison
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

async def set_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /alert [symbol] [price]\nExample: /alert TCS.NS 4200")
        return

    symbol, target = context.args[0].upper(), float(context.args[1])
    user_id = update.effective_user.id

    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE watchlist SET target_price = ? WHERE user_id = ? AND symbol = ?", (target, user_id, symbol))
    # If not in watchlist, add it
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

async def add_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add [symbol]")
        return
    symbol = context.args[0].upper()
    user_id = update.effective_user.id
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO watchlist (user_id, symbol) VALUES (?, ?)", (user_id, symbol))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Added {symbol} to your permanent watchlist.")

async def view_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute("SELECT symbol FROM watchlist WHERE user_id = ?", (user_id,))
    stocks = cursor.fetchall()
    conn.close()
    if not stocks:
        await update.message.reply_text("Your watchlist is empty.")
        return
    await update.message.reply_text("Fetching your stocks...")
    summary = "--- YOUR WATCHLIST ---\n"
    for row in stocks:
        symbol = row[0]
        try:
            data = yf.Ticker(symbol).history(period="5d")
            price = data['Close'].iloc[-1]
            summary += f"{symbol}: Rs. {price:.2f}\n"
        except:
            summary += f"{symbol}: Error\n"
    await update.message.reply_text(summary)

async def remove_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove [symbol]")
        return
    symbol = context.args[0].upper()
    user_id = update.effective_user.id
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM watchlist WHERE user_id = ? AND symbol = ?", (user_id, symbol))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Removed {symbol} from watchlist.")

# --- MAIN RUNNER ---

if __name__ == '__main__':
    TOKEN = "8312601408:AAEINAT4fywsepdOY9cUXpT9RbRY9l2Bf7o"
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("guide", guide))
    app.add_handler(CommandHandler("price", get_price))
    app.add_handler(CommandHandler("add", add_watchlist))
    app.add_handler(CommandHandler("watchlist", view_watchlist))
    app.add_handler(CommandHandler("remove", remove_watchlist))
    app.add_handler(CommandHandler("top", top_stocks))
    app.add_handler(CommandHandler("alert", set_alert))

    # Run alert check every 1 hour
    job_queue = app.job_queue
    job_queue.run_repeating(check_alerts, interval=3600, first=10)

    print("Market Master v4.0 (Top Stocks & Alerts) is active...", flush=True)
    app.run_polling()
