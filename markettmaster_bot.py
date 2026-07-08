import os
import sqlite3
import logging
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

import yfinance as yf
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# --- CONFIG ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "bot_data.db"
IST = ZoneInfo("Asia/Kolkata")

# How far price must fall from the previous close to trigger a drop alert
DROP_ALERT_PERCENT = 5.0
# How close price must get to the 52-week high/low to trigger a proximity alert
YEAR_EXTREME_BAND = 0.02  # 2%

# How often the alert-checking job runs while the market is open (seconds)
ALERT_CHECK_INTERVAL = 900  # 15 minutes

# When the daily digest is sent (IST). Market closes 15:30, so 15:35 gives
# yfinance a few minutes to reflect the final print.
DIGEST_HOUR, DIGEST_MINUTE = 15, 35

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("market_master")


def fix_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not symbol.endswith(".NS") and "-" not in symbol:
        return f"{symbol}.NS"
    return symbol


def is_market_open(now: datetime | None = None) -> bool:
    """NSE regular session: 9:15-15:30 IST, Monday-Friday. Doesn't account
    for exchange holidays, since those aren't available without a paid feed."""
    now = now or datetime.now(IST)
    if now.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


# --- DATABASE SETUP ---
def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute('''CREATE TABLE IF NOT EXISTS watchlist (
        user_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        target_price REAL,
        stop_loss REAL,
        last_price REAL,
        drop_alert_date TEXT,
        high_alert_date TEXT,
        low_alert_date TEXT,
        UNIQUE(user_id, symbol)
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS portfolio (
        user_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        quantity REAL NOT NULL,
        avg_buy_price REAL NOT NULL,
        UNIQUE(user_id, symbol)
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        user_id INTEGER PRIMARY KEY,
        digest_enabled INTEGER DEFAULT 0
    )''')

    # Lightweight migration path: if an older bot_data.db (pre this version)
    # is reused, add any columns it's missing rather than crashing.
    for stmt in [
        "ALTER TABLE watchlist ADD COLUMN stop_loss REAL",
        "ALTER TABLE watchlist ADD COLUMN drop_alert_date TEXT",
        "ALTER TABLE watchlist ADD COLUMN high_alert_date TEXT",
        "ALTER TABLE watchlist ADD COLUMN low_alert_date TEXT",
    ]:
        try:
            cur.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.commit()
    conn.close()


# --- MARKET DATA HELPER ---
def fetch_stock_data(symbol: str) -> dict | None:
    """Returns {price, previous_close, year_high, year_low} or None if the
    symbol is invalid / data can't be fetched. Each field is independently
    guarded since yfinance's fast_info doesn't always populate every key."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = info["last_price"]
        if price is None:
            return None
    except Exception as e:
        logger.warning(f"Could not fetch price for {symbol}: {e}")
        return None

    def safe_get(key):
        try:
            val = info[key]
            return val if val else None
        except Exception:
            return None

    return {
        "price": price,
        "previous_close": safe_get("previous_close"),
        "year_high": safe_get("year_high"),
        "year_low": safe_get("year_low"),
    }


def parse_float_arg(raw: str) -> float | None:
    try:
        val = float(raw)
        return val if val > 0 else None
    except ValueError:
        return None


# --- TOP STOCKS COMMAND ---
async def top_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wait_msg = await update.message.reply_text("Fetching performance of top 10 Nifty stocks...")
    nifty_top_10 = [
        "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
        "SBIN.NS", "BHARTIARTL.NS", "LTIM.NS", "ITC.NS", "TITAN.NS"
    ]

    lines = ["--- TOP 10 NIFTY STOCKS ---\n"]
    for symbol in nifty_top_10:
        data = fetch_stock_data(symbol)
        if not data or not data["previous_close"]:
            lines.append(f"{symbol}: Data unavailable")
            continue
        change = ((data["price"] - data["previous_close"]) / data["previous_close"]) * 100
        lines.append(f"{symbol}: Rs. {data['price']:.2f} ({change:+.2f}%)")

    await wait_msg.edit_text("\n".join(lines))


# --- BACKGROUND JOB: CHECK WATCHLIST ALERTS ---
async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    if not is_market_open():
        return  # only poll during NSE market hours to save API calls & avoid stale-data false alarms

    today_str = date.today().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''SELECT user_id, symbol, target_price, stop_loss, last_price,
                          drop_alert_date, high_alert_date, low_alert_date
                   FROM watchlist''')
    rows = cur.fetchall()

    for (user_id, symbol, target, stop_loss, last_price,
         drop_alert_date, high_alert_date, low_alert_date) in rows:

        data = fetch_stock_data(symbol)
        if not data:
            continue
        price = data["price"]

        try:
            if target and price >= target:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎯 TARGET HIT: {symbol} reached Rs. {price:.2f} (Target: Rs. {target:.2f})"
                )
                cur.execute("UPDATE watchlist SET target_price = NULL WHERE user_id=? AND symbol=?",
                            (user_id, symbol))

            if stop_loss and price <= stop_loss:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🛑 STOP-LOSS HIT: {symbol} fell to Rs. {price:.2f} (Stop: Rs. {stop_loss:.2f})"
                )
                cur.execute("UPDATE watchlist SET stop_loss = NULL WHERE user_id=? AND symbol=?",
                            (user_id, symbol))

            if data["previous_close"] and drop_alert_date != today_str:
                drop_pct = ((data["previous_close"] - price) / data["previous_close"]) * 100
                if drop_pct >= DROP_ALERT_PERCENT:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(f"📉 PRICE DROP: {symbol} is down {drop_pct:.2f}% today\n"
                              f"Prev close: Rs. {data['previous_close']:.2f} -> Now: Rs. {price:.2f}")
                    )
                    cur.execute("UPDATE watchlist SET drop_alert_date=? WHERE user_id=? AND symbol=?",
                                (today_str, user_id, symbol))

            if data["year_high"] and high_alert_date != today_str:
                if price >= data["year_high"] * (1 - YEAR_EXTREME_BAND):
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(f"🚀 NEAR 52-WEEK HIGH: {symbol} at Rs. {price:.2f} "
                              f"(52wk high: Rs. {data['year_high']:.2f})")
                    )
                    cur.execute("UPDATE watchlist SET high_alert_date=? WHERE user_id=? AND symbol=?",
                                (today_str, user_id, symbol))

            if data["year_low"] and low_alert_date != today_str:
                if price <= data["year_low"] * (1 + YEAR_EXTREME_BAND):
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(f"⚠️ NEAR 52-WEEK LOW: {symbol} at Rs. {price:.2f} "
                              f"(52wk low: Rs. {data['year_low']:.2f})")
                    )
                    cur.execute("UPDATE watchlist SET low_alert_date=? WHERE user_id=? AND symbol=?",
                                (today_str, user_id, symbol))

            cur.execute("UPDATE watchlist SET last_price=? WHERE user_id=? AND symbol=?",
                        (price, user_id, symbol))
        except Exception as e:
            logger.error(f"Error processing alert for {symbol}/{user_id}: {e}")
            continue

    conn.commit()
    conn.close()


# --- BACKGROUND JOB: DAILY DIGEST ---
async def daily_digest(context: ContextTypes.DEFAULT_TYPE):
    if date.today().weekday() >= 5:
        return  # skip weekends

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM settings WHERE digest_enabled=1")
    user_ids = [row[0] for row in cur.fetchall()]

    for user_id in user_ids:
        lines = ["--- 📊 DAILY DIGEST ---"]

        cur.execute("SELECT symbol FROM watchlist WHERE user_id=?", (user_id,))
        watch_symbols = [r[0] for r in cur.fetchall()]
        if watch_symbols:
            lines.append("\nWatchlist:")
            for symbol in watch_symbols:
                data = fetch_stock_data(symbol)
                if not data or not data["previous_close"]:
                    lines.append(f"  {symbol}: data unavailable")
                    continue
                change = ((data["price"] - data["previous_close"]) / data["previous_close"]) * 100
                lines.append(f"  {symbol}: Rs. {data['price']:.2f} ({change:+.2f}%)")

        cur.execute("SELECT symbol, quantity, avg_buy_price FROM portfolio WHERE user_id=?", (user_id,))
        holdings = cur.fetchall()
        if holdings:
            lines.append("\nPortfolio:")
            total_pnl = 0.0
            for symbol, qty, avg_price in holdings:
                data = fetch_stock_data(symbol)
                if not data:
                    lines.append(f"  {symbol}: data unavailable")
                    continue
                pnl = (data["price"] - avg_price) * qty
                total_pnl += pnl
                pct = ((data["price"] - avg_price) / avg_price) * 100
                lines.append(f"  {symbol}: {pnl:+.2f} Rs. ({pct:+.2f}%)")
            lines.append(f"\nTotal P&L: Rs. {total_pnl:+.2f}")

        if len(lines) == 1:
            continue  # nothing to report, skip empty digest

        try:
            await context.bot.send_message(chat_id=user_id, text="\n".join(lines))
        except Exception as e:
            logger.error(f"Failed to send digest to {user_id}: {e}")

    conn.close()


# --- COMMAND HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    welcome_text = (
        f"Namaste {user_name}. Welcome to Market Master v5.0.\n\n"
        "Prices & Watchlist:\n"
        "/price [symbol] - Live rate\n"
        "/add [symbol] - Add to watchlist\n"
        "/watchlist - View your saved stocks\n"
        "/remove [symbol] - Remove from watchlist\n"
        "/top - Top 10 Nifty stocks\n\n"
        "Alerts:\n"
        "/alert [symbol] [price] - Set upside target\n"
        "/stoploss [symbol] [price] - Set downside stop-loss\n\n"
        "Portfolio:\n"
        "/buy [symbol] [qty] [price] - Log a purchase\n"
        "/sell [symbol] [qty] - Reduce/close a holding\n"
        "/portfolio - View live P&L\n\n"
        "Daily Digest:\n"
        "/digest_on - Get a summary after market close\n"
        "/digest_off - Turn off the digest\n\n"
        "/guide - Help with symbols"
    )
    await update.message.reply_text(welcome_text)


async def guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guide_text = (
        "--- BEGINNER GUIDE ---\n"
        "This bot tracks NSE-listed stocks. The '.NS' suffix is added automatically.\n"
        "Example: /price TATASTEEL  ->  looks up TATASTEEL.NS\n\n"
        "Alerts only fire during NSE market hours (9:15am-3:30pm IST, Mon-Fri).\n"
        "Commands like /price and /portfolio work anytime."
    )
    await update.message.reply_text(guide_text)


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price [symbol]\nExample: /price TCS")
        return
    symbol = fix_symbol(context.args[0])
    data = fetch_stock_data(symbol)
    if not data:
        await update.message.reply_text(f"Couldn't find data for {symbol}. Check the symbol and try again.")
        return
    text = f"{symbol}: Rs. {data['price']:.2f}"
    if data["previous_close"]:
        change = ((data["price"] - data["previous_close"]) / data["previous_close"]) * 100
        text += f" ({change:+.2f}%)"
    await update.message.reply_text(text)


async def add_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add [symbol]")
        return
    symbol = fix_symbol(context.args[0])

    if not fetch_stock_data(symbol):
        await update.message.reply_text(f"Couldn't find data for {symbol}. Check the symbol and try again.")
        return

    user_id = update.effective_user.id
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO watchlist (user_id, symbol) VALUES (?, ?)", (user_id, symbol))
    added = cur.rowcount > 0
    conn.commit()
    conn.close()

    if added:
        await update.message.reply_text(f"Added {symbol} to your watchlist.")
    else:
        await update.message.reply_text(f"{symbol} is already on your watchlist.")


async def view_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT symbol, target_price, stop_loss FROM watchlist WHERE user_id=?", (user_id,))
    stocks = cur.fetchall()
    conn.close()

    if not stocks:
        await update.message.reply_text("Your watchlist is empty. Add one with /add [symbol].")
        return

    wait_msg = await update.message.reply_text("Fetching your stocks...")
    lines = ["--- YOUR WATCHLIST ---"]
    for symbol, target, stop_loss in stocks:
        data = fetch_stock_data(symbol)
        if not data:
            lines.append(f"{symbol}: data unavailable")
            continue
        line = f"{symbol}: Rs. {data['price']:.2f}"
        if data["previous_close"]:
            change = ((data["price"] - data["previous_close"]) / data["previous_close"]) * 100
            line += f" ({change:+.2f}%)"
        extras = []
        if target:
            extras.append(f"target Rs.{target:.2f}")
        if stop_loss:
            extras.append(f"stop Rs.{stop_loss:.2f}")
        if extras:
            line += f" [{', '.join(extras)}]"
        lines.append(line)

    await wait_msg.edit_text("\n".join(lines))


async def remove_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove [symbol]")
        return
    symbol = fix_symbol(context.args[0])
    user_id = update.effective_user.id
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM watchlist WHERE user_id=? AND symbol=?", (user_id, symbol))
    removed = cur.rowcount > 0
    conn.commit()
    conn.close()
    if removed:
        await update.message.reply_text(f"Removed {symbol} from watchlist.")
    else:
        await update.message.reply_text(f"{symbol} wasn't on your watchlist.")


async def set_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /alert [symbol] [price]\nExample: /alert TCS 4200")
        return
    symbol = fix_symbol(context.args[0])
    target = parse_float_arg(context.args[1])
    if target is None:
        await update.message.reply_text("Price must be a positive number, e.g. /alert TCS 4200")
        return
    if not fetch_stock_data(symbol):
        await update.message.reply_text(f"Couldn't find data for {symbol}. Check the symbol and try again.")
        return

    user_id = update.effective_user.id
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''INSERT INTO watchlist (user_id, symbol, target_price) VALUES (?, ?, ?)
                   ON CONFLICT(user_id, symbol) DO UPDATE SET target_price=excluded.target_price''',
                (user_id, symbol, target))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Upside alert set for {symbol} at Rs. {target:.2f}.")


async def set_stoploss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /stoploss [symbol] [price]\nExample: /stoploss TCS 3900")
        return
    symbol = fix_symbol(context.args[0])
    stop = parse_float_arg(context.args[1])
    if stop is None:
        await update.message.reply_text("Price must be a positive number, e.g. /stoploss TCS 3900")
        return
    if not fetch_stock_data(symbol):
        await update.message.reply_text(f"Couldn't find data for {symbol}. Check the symbol and try again.")
        return

    user_id = update.effective_user.id
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''INSERT INTO watchlist (user_id, symbol, stop_loss) VALUES (?, ?, ?)
                   ON CONFLICT(user_id, symbol) DO UPDATE SET stop_loss=excluded.stop_loss''',
                (user_id, symbol, stop))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Stop-loss set for {symbol} at Rs. {stop:.2f}.")


async def buy_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("Usage: /buy [symbol] [qty] [price]\nExample: /buy TCS 10 4150")
        return
    symbol = fix_symbol(context.args[0])
    qty = parse_float_arg(context.args[1])
    price = parse_float_arg(context.args[2])
    if qty is None or price is None:
        await update.message.reply_text("Quantity and price must be positive numbers.")
        return
    if not fetch_stock_data(symbol):
        await update.message.reply_text(f"Couldn't find data for {symbol}. Check the symbol and try again.")
        return

    user_id = update.effective_user.id
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT quantity, avg_buy_price FROM portfolio WHERE user_id=? AND symbol=?", (user_id, symbol))
    row = cur.fetchone()
    if row:
        old_qty, old_avg = row
        new_qty = old_qty + qty
        new_avg = ((old_qty * old_avg) + (qty * price)) / new_qty
        cur.execute("UPDATE portfolio SET quantity=?, avg_buy_price=? WHERE user_id=? AND symbol=?",
                    (new_qty, new_avg, user_id, symbol))
    else:
        cur.execute("INSERT INTO portfolio (user_id, symbol, quantity, avg_buy_price) VALUES (?, ?, ?, ?)",
                    (user_id, symbol, qty, price))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Logged purchase: {qty} of {symbol} @ Rs. {price:.2f}.")


async def sell_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /sell [symbol] [qty]\nExample: /sell TCS 5")
        return
    symbol = fix_symbol(context.args[0])
    qty = parse_float_arg(context.args[1])
    if qty is None:
        await update.message.reply_text("Quantity must be a positive number.")
        return

    user_id = update.effective_user.id
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT quantity FROM portfolio WHERE user_id=? AND symbol=?", (user_id, symbol))
    row = cur.fetchone()
    if not row:
        await update.message.reply_text(f"You don't hold any {symbol}.")
        conn.close()
        return
    held_qty = row[0]
    if qty > held_qty:
        await update.message.reply_text(f"You only hold {held_qty} of {symbol}.")
        conn.close()
        return

    remaining = held_qty - qty
    if remaining <= 0:
        cur.execute("DELETE FROM portfolio WHERE user_id=? AND symbol=?", (user_id, symbol))
    else:
        cur.execute("UPDATE portfolio SET quantity=? WHERE user_id=? AND symbol=?",
                    (remaining, user_id, symbol))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Sold {qty} of {symbol}. Remaining: {max(remaining, 0)}.")


async def view_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT symbol, quantity, avg_buy_price FROM portfolio WHERE user_id=?", (user_id,))
    holdings = cur.fetchall()
    conn.close()

    if not holdings:
        await update.message.reply_text("You have no holdings logged. Add one with /buy [symbol] [qty] [price].")
        return

    wait_msg = await update.message.reply_text("Calculating your portfolio...")
    lines = ["--- YOUR PORTFOLIO ---"]
    total_invested = 0.0
    total_current = 0.0
    for symbol, qty, avg_price in holdings:
        data = fetch_stock_data(symbol)
        invested = qty * avg_price
        total_invested += invested
        if not data:
            lines.append(f"{symbol}: qty {qty}, avg Rs.{avg_price:.2f} - live data unavailable")
            total_current += invested
            continue
        current_val = qty * data["price"]
        total_current += current_val
        pnl = current_val - invested
        pct = (pnl / invested) * 100 if invested else 0
        lines.append(
            f"{symbol}: qty {qty} | avg Rs.{avg_price:.2f} -> Rs.{data['price']:.2f} | "
            f"P&L Rs.{pnl:+.2f} ({pct:+.2f}%)"
        )

    total_pnl = total_current - total_invested
    total_pct = (total_pnl / total_invested) * 100 if total_invested else 0
    lines.append(f"\nTotal invested: Rs. {total_invested:.2f}")
    lines.append(f"Total value: Rs. {total_current:.2f}")
    lines.append(f"Total P&L: Rs. {total_pnl:+.2f} ({total_pct:+.2f}%)")

    await wait_msg.edit_text("\n".join(lines))


async def digest_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''INSERT INTO settings (user_id, digest_enabled) VALUES (?, 1)
                   ON CONFLICT(user_id) DO UPDATE SET digest_enabled=1''', (user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"Daily digest enabled. You'll get a summary around {DIGEST_HOUR}:{DIGEST_MINUTE:02d} IST on trading days."
    )


async def digest_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''INSERT INTO settings (user_id, digest_enabled) VALUES (?, 0)
                   ON CONFLICT(user_id) DO UPDATE SET digest_enabled=0''', (user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("Daily digest disabled.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled exception: {context.error}", exc_info=context.error)


if __name__ == '__main__':
    if not TOKEN:
        print("Error: No BOT_TOKEN found in environment variables. Set it in a .env file or your host's config.")
    else:
        init_db()
        app = ApplicationBuilder().token(TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("guide", guide))
        app.add_handler(CommandHandler("price", get_price))
        app.add_handler(CommandHandler("add", add_watchlist))
        app.add_handler(CommandHandler("watchlist", view_watchlist))
        app.add_handler(CommandHandler("remove", remove_watchlist))
        app.add_handler(CommandHandler("top", top_stocks))
        app.add_handler(CommandHandler("alert", set_alert))
        app.add_handler(CommandHandler("stoploss", set_stoploss))
        app.add_handler(CommandHandler("buy", buy_stock))
        app.add_handler(CommandHandler("sell", sell_stock))
        app.add_handler(CommandHandler("portfolio", view_portfolio))
        app.add_handler(CommandHandler("digest_on", digest_on))
        app.add_handler(CommandHandler("digest_off", digest_off))
        app.add_error_handler(error_handler)

        job_queue = app.job_queue
        job_queue.run_repeating(check_alerts, interval=ALERT_CHECK_INTERVAL, first=10)
        job_queue.run_daily(
            daily_digest,
            time=dtime(hour=DIGEST_HOUR, minute=DIGEST_MINUTE, tzinfo=IST),
        )

        print("Market Master v5.0 (Production) is active...", flush=True)
        app.run_polling()
