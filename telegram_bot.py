import os
import time
import requests
import pandas as pd
from binance.client import Client

# --- Environment variables (Render-এ সেট করা আছে) ---
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- সেটিংস ---
SYMBOL = "BTCUSDT"
INTERVAL = Client.KLINE_INTERVAL_15MINUTE
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
CHECK_INTERVAL_SECONDS = 300  # প্রতি ৫ মিনিটে চেক করবে
TRADE_QUANTITY = 0.001  # BTC পরিমাণ (টেস্টনেট, আসল টাকা না)

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
client.API_URL = 'https://testnet.binance.vision/api'

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Telegram send error:", e)

def get_klines():
    klines = client.get_klines(symbol=SYMBOL, interval=INTERVAL, limit=100)
    df = pd.DataFrame(klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "num_trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    df["close"] = df["close"].astype(float)
    return df

def calculate_rsi(df, period=RSI_PERIOD):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

position_open = False

def check_market():
    global position_open
    df = get_klines()
    rsi_series = calculate_rsi(df)
    latest_rsi = rsi_series.iloc[-1]
    latest_price = df["close"].iloc[-1]
    print(f"Price: {latest_price} | RSI: {latest_rsi:.2f}")

    if latest_rsi < RSI_OVERSOLD and not position_open:
        try:
            client.create_order(symbol=SYMBOL, side="BUY", type="MARKET", quantity=TRADE_QUANTITY)
            position_open = True
            send_telegram(f"🟢 BUY সিগন্যাল\nSymbol: {SYMBOL}\nPrice: {latest_price}\nRSI: {latest_rsi:.2f}")
        except Exception as e:
            send_telegram(f"⚠️ BUY অর্ডার ব্যর্থ: {e}")

    elif latest_rsi > RSI_OVERBOUGHT and position_open:
        try:
            client.create_order(symbol=SYMBOL, side="SELL", type="MARKET", quantity=TRADE_QUANTITY)
            position_open = False
            send_telegram(f"🔴 SELL সিগন্যাল\nSymbol: {SYMBOL}\nPrice: {latest_price}\nRSI: {latest_rsi:.2f}")
        except Exception as e:
            send_telegram(f"⚠️ SELL অর্ডার ব্যর্থ: {e}")

if __name__ == "__main__":
    send_telegram("🤖 বট চালু হয়েছে! RSI স্ট্র্যাটেজি মনিটর করছি...")
    while True:
        try:
            check_market()
        except Exception as e:
            print("Error:", e)
            send_telegram(f"⚠️ এরর হয়েছে: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)
