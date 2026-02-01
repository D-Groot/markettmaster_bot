
# Market Master v4.0 – Telegram Stock Market Bot

Market Master v4.0 is a Telegram bot built to help Indian investors track stock prices, manage a personal watchlist, and receive automated alerts for important market movements.  
The bot focuses on simplicity, reliability, and beginner-friendly usage.

Telegram Bot: https://t.me/markettmaster_bot

---

## Features

### Live Stock Prices
Fetch real-time prices for Indian stocks listed on NSE using Yahoo Finance data.

Example:
/price TCS.NS

---

### Personal Watchlist
Save stocks permanently and track them anytime.

Commands:
/add RELIANCE.NS
/watchlist
/remove RELIANCE.NS


Watchlists are stored using a local SQLite database.

---

### Automated Price Alerts

The bot runs a background task every hour and checks all user watchlists.

#### Target Price Alert
Set a price at which you want to be notified.

Example:
/alert TCS.NS 4200


Once triggered, the alert is automatically reset to prevent repeated notifications.

#### 5% Price Drop Alert
If a stock drops by 5% or more compared to its last recorded price, the bot sends an alert automatically.  
This helps identify potential buying opportunities.

---

### Top 10 Nifty Stocks Snapshot
View the latest price and daily percentage change for major Nifty bluechip stocks.

Command:

Includes stocks like:
- RELIANCE
- TCS
- HDFCBANK
- ICICIBANK
- INFY
- SBIN
- ITC
- TITAN

---

### Beginner-Friendly Guide
Helps users understand correct stock symbols.

Command:
/guide

Example:
Indian stocks require the .NS suffix
TATASTEEL → TATASTEEL.NS


---

## Available Commands

| Command | Description |
|------|------------|
| /start | Welcome message and help |
| /price SYMBOL | Get live stock price |
| /add SYMBOL | Add stock to watchlist |
| /watchlist | View saved stocks |
| /remove SYMBOL | Remove stock from watchlist |
| /alert SYMBOL PRICE | Set a target price alert |
| /top | View top 10 Nifty stocks |
| /guide | Stock symbol help |

---

## How Alerts Work

1. The bot checks all watchlists every hour
2. Fetches latest prices using Yahoo Finance
3. Compares prices with:
   - User-defined target price
   - Previously stored price for drop detection
4. Sends alerts when conditions are met
5. Updates stored prices for the next cycle

This approach avoids spam and ensures meaningful notifications.

---

## Database Structure

SQLite database: `bot_data.db`

Table: `watchlist`

| Column | Description |
|------|------------|
| user_id | Telegram user ID |
| symbol | Stock symbol (e.g., TCS.NS) |
| target_price | User-defined alert price |
| last_price | Last checked price |

---

## Tech Stack

- Python
- python-telegram-bot
- yfinance (Yahoo Finance)
- SQLite
- Async background jobs

---

## Disclaimer

This bot is for educational and informational purposes only.  
It does not provide financial advice. Always do your own research before investing.

---

## Status

Market Master v4.0 is actively running with:
- Hourly price checks
- Automated alerts
- Persistent watchlists
- Live market data

---

Built for Telegram with ❤️
Designed for simplicity and real-world usefulness.

