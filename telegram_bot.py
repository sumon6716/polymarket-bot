import requests
import time
import json
import os
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ================== সেটিংস ==================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

POLYMARKET_API_URL = "https://gamma-api.polymarket.com/markets"
CHECK_INTERVAL_SECONDS = 30
MOMENTUM_THRESHOLD = 1.0
MARKET_LIMIT = 20
SUBSCRIBERS_FILE = "subscribers.json"
PORT = int(os.environ.get("PORT", 10000))

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
previous_prices = {}


# ================== KEEP-ALIVE ওয়েব সার্ভার ==================
# Render-এর ফ্রি প্ল্যানে সার্ভিস সচল রাখতে একটা ছোট ওয়েব সার্ভার লাগে,
# যাতে UptimeRobot এটাকে "পিং" করে জাগিয়ে রাখতে পারে।
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Polymarket Telegram bot is running.")

    def log_message(self, format, *args):
        pass  # সার্ভার লগ নিরব রাখা হলো


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    server.serve_forever()


# ================== সাবস্ক্রাইবার ম্যানেজমেন্ট ==================
def load_subscribers():
    if os.path.exists(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE, "r") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, IOError):
            return set()
    return set()


def save_subscribers(subscribers):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(list(subscribers), f)


# ================== টেলিগ্রাম হেল্পার ==================
def send_message(chat_id, text):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except requests.exceptions.RequestException as e:
        print(f"মেসেজ পাঠাতে এরর (chat_id={chat_id}): {e}")


def broadcast_message(subscribers, text):
    for chat_id in subscribers:
        send_message(chat_id, text)


def get_updates(offset=None):
    url = f"{TELEGRAM_API}/getUpdates"
    params = {"timeout": 10, "offset": offset}
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        return response.json().get("result", [])
    except requests.exceptions.RequestException as e:
        print(f"getUpdates এরর: {e}")
        return []


def handle_commands(subscribers, last_update_id):
    updates = get_updates(offset=last_update_id + 1 if last_update_id else None)

    for update in updates:
        last_update_id = update["update_id"]
        message = update.get("message")
        if not message:
            continue

        chat_id = message["chat"]["id"]
        text = message.get("text", "")

        if text == "/start":
            if chat_id not in subscribers:
                subscribers.add(chat_id)
                save_subscribers(subscribers)
            send_message(
                chat_id,
                "স্বাগতম! আপনি এখন Polymarket মোমেন্টাম এলার্ট পাবেন।\n\n"
                f"যখনই কোনো মার্কেটের দাম {MOMENTUM_THRESHOLD}% বা তার বেশি বদলাবে, "
                "আপনাকে জানানো হবে।\n\n"
                "বন্ধ করতে /stop লিখুন।\n\n"
                "⚠️ এটি আর্থিক পরামর্শ নয় — শুধু দামের পরিবর্তন জানায়।",
            )
        elif text == "/stop":
            if chat_id in subscribers:
                subscribers.discard(chat_id)
                save_subscribers(subscribers)
            send_message(chat_id, "এলার্ট বন্ধ করা হয়েছে। আবার চালু করতে /start লিখুন।")

    return last_update_id


# ================== মার্কেট ডেটা ==================
def get_live_markets(limit=MARKET_LIMIT):
    params = {"limit": limit, "active": "true", "closed": "false"}
    try:
        response = requests.get(POLYMARKET_API_URL, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Polymarket ডেটা আনতে এরর: {e}")
        return []


def get_market_price(market):
    try:
        prices = market.get("outcomePrices")
        if prices:
            if isinstance(prices, str):
                prices = json.loads(prices)
            return float(prices[0])
    except (ValueError, TypeError, IndexError, KeyError):
        pass
    return None


def check_momentum(markets):
    alerts = []
    for market in markets:
        market_id = market.get("id")
        question = market.get("question", "শিরোনাম নেই")
        current_price = get_market_price(market)

        if market_id is None or current_price is None:
            continue

        previous_price = previous_prices.get(market_id)

        if previous_price is not None and previous_price != 0:
            percent_change = ((current_price - previous_price) / previous_price) * 100
            if abs(percent_change) >= MOMENTUM_THRESHOLD:
                direction = "উপরে" if percent_change > 0 else "নিচে"
                alerts.append({
                    "question": question,
                    "previous_price": previous_price,
                    "current_price": current_price,
                    "percent_change": percent_change,
                    "direction": direction,
                })

        previous_prices[market_id] = current_price

    return alerts


def format_alert(alert):
    return (
        f"<b>মোমেন্টাম এলার্ট</b>\n\n"
        f"প্রশ্ন: {alert['question']}\n"
        f"পরিবর্তন: {alert['direction']} {alert['percent_change']:+.2f}%\n"
        f"আগের দাম: {alert['previous_price']:.3f}\n"
        f"বর্তমান দাম: {alert['current_price']:.3f}"
    )


# ================== মূল লুপ ==================
def run_bot():
    if not TELEGRAM_BOT_TOKEN:
        print("এরর: TELEGRAM_BOT_TOKEN পরিবেশ ভেরিয়েবলে পাওয়া যায়নি।")
        return

    subscribers = load_subscribers()
    last_update_id = None
    last_check_time = 0

    print("টেলিগ্রাম বট চালু হয়েছে...")
    print(f"বর্তমান সাবস্ক্রাইবার সংখ্যা: {len(subscribers)}")

    while True:
        last_update_id = handle_commands(subscribers, last_update_id)

        now = time.time()
        if now - last_check_time >= CHECK_INTERVAL_SECONDS:
            last_check_time = now
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{timestamp}] মার্কেট চেক করা হচ্ছে... (সাবস্ক্রাইবার: {len(subscribers)})")

            markets = get_live_markets()
            if markets:
                alerts = check_momentum(markets)
                for alert in alerts:
                    print(f"এলার্ট: {alert['question']} ({alert['percent_change']:+.2f}%)")
                    if subscribers:
                        broadcast_message(subscribers, format_alert(alert))

        time.sleep(2)


if __name__ == "__main__":
    # ব্যাকগ্রাউন্ডে ছোট ওয়েব সার্ভার চালু (Render-কে সচল রাখার জন্য)
    threading.Thread(target=start_health_server, daemon=True).start()
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\nবট বন্ধ করা হয়েছে। ধন্যবাদ!")
