import os
import time
import threading
import requests
import pandas as pd
from flask import Flask

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
CHECK_INTERVAL_SECONDS = 300
TRADE_QUANTITY = 0.001
SYMBOL = "BTCUSDT"

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/bitcoin/ohlc"

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Telegram send error:", e)

def get_klines():
    params = {"vs_currency": "usd", "days": "1"}
    resp = requests.get(COINGECKO_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close"])
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

def try_place_order(side):
    try:
        from binance.client import Client
        client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=True)
        client.create_order(symbol=SYMBOL, side=side, type="MARKET", quantity=TRADE_QUANTITY)
        return True, None
    except Exception as e:
        return False, str(e)

def check_market():
    global position_open
    df = get_klines()
    rsi_series = calculate_rsi(df)
    latest_rsi = rsi_series.iloc[-1]
    latest_price = df["close"].iloc[-1]
    print(f"Price: {latest_price} | RSI: {latest_rsi:.2f}")

    if latest_rsi < RSI_OVERSOLD and not position_open:
        ok, err = try_place_order("BUY")
        position_open = True
        if ok:
            send_telegram(f"🟢 BUY সিগন্যাল ও অর্ডার সফল\nPrice: {latest_price}\nRSI: {latest_rsi:.2f}")
        else:
            send_telegram(f"🟢 BUY সিগন্যাল (শুধু সিগন্যাল, অর্ডার ব্যর্থ)\nPrice: {latest_price}\nRSI: {latest_rsi:.2f}")

    elif latest_rsi > RSI_OVERBOUGHT and position_open:
        ok, err = try_place_order("SELL")
        position_open = False
        if ok:
            send_telegram(f"🔴 SELL সিগন্যাল ও অর্ডার সফল\nPrice: {latest_price}\nRSI: {latest_rsi:.2f}")
        else:
            send_telegram(f"🔴 SELL সিগন্যাল (শুধু সিগন্যাল, অর্ডার ব্যর্থ)\nPrice: {latest_price}\nRSI: {latest_rsi:.2f}")

def bot_loop():
    send_telegram("🤖 বট চালু হয়েছে! RSI স্ট্র্যাটেজি মনিটর করছি (CoinGecko ডেটা)...")
    while True:
        try:
            check_market()
        except Exception as e:
            print("Error:", e)
            send_telegram(f"⚠️ এরর হয়েছে: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    run_web()
