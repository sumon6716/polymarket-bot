import requests
import time
import json
import os
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ================== CONFIG ==================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

POLYMARKET_API_URL = "https://gamma-api.polymarket.com/markets"
DEFAULT_CHECK_INTERVAL_SECONDS = 30
DEFAULT_THRESHOLD = 1.0
MIN_THRESHOLD = 0.1
MAX_THRESHOLD = 50.0
MARKET_LIMIT = 30
ALERT_COOLDOWN_SECONDS = 600
SUBSCRIBERS_FILE = "subscribers.json"
PORT = int(os.environ.get("PORT", 10000))

CATEGORIES = ["Politics", "Crypto", "Sports", "Pop Culture"]
CATEGORY_KEYWORDS = {
    "Politics": ["election", "president", "senate", "congress", "governor", "vote", "poll"],
    "Crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "token"],
    "Sports": ["nfl", "nba", "mlb", "nhl", "soccer", "football", "match", "game", "win the"],
    "Pop Culture": ["oscar", "grammy", "movie", "album", "celebrity", "award"],
}

previous_prices = {}
last_alert_time = {}

def load_env():
    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

load_env()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Polymarket Telegram bot is running.")

    def log_message(self, format, *args):
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    server.serve_forever()


def default_user_settings():
    return {
        "threshold": DEFAULT_THRESHOLD,
        "categories": list(CATEGORIES),
        "watchlist": [],
        "premium": False,
    }


def load_subscribers():
    if not os.path.exists(SUBSCRIBERS_FILE):
        return {}
    try:
        with open(SUBSCRIBERS_FILE, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

    if isinstance(data, list):
        return {str(cid): default_user_settings() for cid in data}
    return data


def save_subscribers(subscribers):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(subscribers, f, indent=2)


def telegram_request(method, payload=None, params=None, retries=3):
    url = f"{TELEGRAM_API}/{method}"
    for attempt in range(retries):
        try:
            if payload is not None:
                response = requests.post(url, json=payload, timeout=15)
            else:
                response = requests.get(url, params=params, timeout=20)

            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 3)
                print(f"Rate limited by Telegram. Waiting {retry_after}s...")
                time.sleep(retry_after)
                continue

            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            wait = 2 ** attempt
            print(f"Telegram API error on {method} (attempt {attempt + 1}/{retries}): {e}. Retrying in {wait}s...")
            time.sleep(wait)
    return None


def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram_request("sendMessage", payload=payload)


def answer_callback_query(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    telegram_request("answerCallbackQuery", payload=payload)


def broadcast_message(subscribers, text, reply_markup=None):
    for chat_id in subscribers:
        send_message(chat_id, text, reply_markup=reply_markup)


def get_updates(offset=None):
    params = {"timeout": 10, "offset": offset}
    result = telegram_request("getUpdates", params=params, retries=2)
    if result is None:
        return []
    return result.get("result", [])


def category_keyboard(user_settings):
    rows = []
    for cat in CATEGORIES:
        enabled = cat in user_settings["categories"]
        label = f"OK {cat}" if enabled else f"-- {cat}"
        rows.append([{"text": label, "callback_data": f"cat:{cat}"}])
    rows.append([{"text": "Done", "callback_data": "cat:done"}])
    return {"inline_keyboard": rows}


def market_link_keyboard(market):
    slug = market.get("slug")
    if slug:
        url = f"https://polymarket.com/event/{slug}"
    else:
        url = "https://polymarket.com"
    return {"inline_keyboard": [[{"text": "View Market on Polymarket", "url": url}]]}


def get_market_category(market):
    text = (market.get("question") or "").lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return cat
    return None


def handle_message(chat_id, text, subscribers):
    text = text.strip()

    if text == "/start":
        if str(chat_id) not in subscribers:
            subscribers[str(chat_id)] = default_user_settings()
            save_subscribers(subscribers)
        send_message(
            chat_id,
            "<b>Welcome to Polymarket Momentum Alerts</b>\n\n"
            f"You'll be notified when a market's price moves {default_user_settings()['threshold']}% or more.\n\n"
            "Commands:\n"
            "/threshold &lt;number&gt; - set your alert sensitivity (e.g. /threshold 2)\n"
            "/categories - choose which topics you want alerts for\n"
            "/watchlist &lt;text&gt; - always get alerts for markets matching this text\n"
            "/mywatchlist - view your watchlist\n"
            "/status - view your current settings\n"
            "/stop - unsubscribe\n\n"
            "This is not financial advice - it only reports price movement.",
        )

    elif text == "/stop":
        if str(chat_id) in subscribers:
            del subscribers[str(chat_id)]
            save_subscribers(subscribers)
        send_message(chat_id, "You've been unsubscribed. Send /start anytime to resume.")

    elif text.startswith("/threshold"):
        if str(chat_id) not in subscribers:
            send_message(chat_id, "Please send /start first.")
            return
        parts = text.split()
        if len(parts) != 2:
            send_message(chat_id, "Usage: /threshold 2 (sets alert sensitivity to 2%)")
            return
        try:
            value = float(parts[1])
        except ValueError:
            send_message(chat_id, "Please provide a number, e.g. /threshold 2")
            return
        if not (MIN_THRESHOLD <= value <= MAX_THRESHOLD):
            send_message(chat_id, f"Threshold must be between {MIN_THRESHOLD}% and {MAX_THRESHOLD}%.")
            return
        subscribers[str(chat_id)]["threshold"] = value
        save_subscribers(subscribers)
        send_message(chat_id, f"Threshold set to {value}%. You'll now be alerted on moves of {value}% or more.")

    elif text == "/categories":
        if str(chat_id) not in subscribers:
            send_message(chat_id, "Please send /start first.")
            return
        send_message(
            chat_id,
            "Tap to toggle which categories you want alerts for:",
            reply_markup=category_keyboard(subscribers[str(chat_id)]),
        )

    elif text.startswith("/watchlist"):
        if str(chat_id) not in subscribers:
            send_message(chat_id, "Please send /start first.")
            return
        query = text[len("/watchlist"):].strip()
        if not query:
            send_message(chat_id, "Usage: /watchlist bitcoin 150k (any market containing this text will always alert you)")
            return
        subscribers[str(chat_id)]["watchlist"].append(query)
        save_subscribers(subscribers)
        send_message(chat_id, f"Added to your watchlist: \"{query}\"")

    elif text == "/mywatchlist":
        if str(chat_id) not in subscribers:
            send_message(chat_id, "Please send /start first.")
            return
        watchlist = subscribers[str(chat_id)]["watchlist"]
        if not watchlist:
            send_message(chat_id, "Your watchlist is empty. Add items with /watchlist <text>")
        else:
            items = "\n".join(f"- {w}" for w in watchlist)
            send_message(chat_id, f"<b>Your watchlist:</b>\n{items}")

    elif text == "/status":
        if str(chat_id) not in subscribers:
            send_message(chat_id, "You're not subscribed. Send /start to begin.")
            return
        s = subscribers[str(chat_id)]
        cats = ", ".join(s["categories"]) if s["categories"] else "None"
        send_message(
            chat_id,
            f"<b>Your settings</b>\n"
            f"Threshold: {s['threshold']}%\n"
            f"Categories: {cats}\n"
            f"Watchlist items: {len(s['watchlist'])}\n"
            f"Tier: {'Premium' if s['premium'] else 'Free'}",
        )


def handle_callback_query(callback_query, subscribers):
    chat_id = callback_query["message"]["chat"]["id"]
    data = callback_query.get("data", "")
    callback_id = callback_query["id"]

    if str(chat_id) not in subscribers:
        answer_callback_query(callback_id, "Please send /start first.")
        return

    if data.startswith("cat:"):
        value = data[len("cat:"):]
        if value == "done":
            answer_callback_query(callback_id, "Saved.")
            return
        settings = subscribers[str(chat_id)]
        if value in settings["categories"]:
            settings["categories"].remove(value)
        else:
            settings["categories"].append(value)
        save_subscribers(subscribers)
        answer_callback_query(callback_id)
        edit_payload = {
            "chat_id": chat_id,
            "message_id": callback_query["message"]["message_id"],
            "reply_markup": category_keyboard(settings),
        }
        telegram_request("editMessageReplyMarkup", payload=edit_payload)


def process_updates(subscribers, last_update_id):
    updates = get_updates(offset=last_update_id + 1 if last_update_id else None)
    for update in updates:
        last_update_id = update["update_id"]
        if "message" in update and "text" in update["message"]:
            chat_id = update["message"]["chat"]["id"]
            handle_message(chat_id, update["message"]["text"], subscribers)
        elif "callback_query" in update:
            handle_callback_query(update["callback_query"], subscribers)
    return last_update_id


def get_live_markets(limit=MARKET_LIMIT):
    params = {"limit": limit, "active": "true", "closed": "false"}
    try:
        response = requests.get(POLYMARKET_API_URL, params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Polymarket data: {e}")
        return []


def get_market_price(market):
    try:
        prices = market.get("outcomePrices")
        if prices:
            if isinstance(prices, str):
                prices = json.loads(prices)
            return float(prices[0])
    except (ValueError, TypeError, IndexError, KeyError, json.JSONDecodeError):
        pass
    return None


def compute_moves(markets):
    moves = []
    for market in markets:
        market_id = market.get("id")
        current_price = get_market_price(market)
        if market_id is None or current_price is None:
            continue

        previous_price = previous_prices.get(market_id)
        if previous_price is not None and previous_price != 0:
            percent_change = ((current_price - previous_price) / previous_price) * 100
            moves.append((market, percent_change, previous_price, current_price))

        previous_prices[market_id] = current_price
    return moves


def format_alert(market, percent_change, previous_price, current_price):
    direction = "UP" if percent_change > 0 else "DOWN"
    return (
        f"<b>Momentum Alert</b>\n\n"
        f"Market: {market.get('question', 'Unknown')}\n"
        f"Change: {direction} {percent_change:+.2f}%\n"
        f"Previous price: {previous_price:.3f}\n"
        f"Current price: {current_price:.3f}"
    )


def should_alert_user(user_settings, market, percent_change):
    category = get_market_category(market)
    question_lower = (market.get("question") or "").lower()
    on_watchlist = any(w.lower() in question_lower for w in user_settings["watchlist"])

    if on_watchlist:
        return True

    if category is not None and category not in user_settings["categories"]:
        return False

    return abs(percent_change) >= user_settings["threshold"]


def dispatch_alerts(subscribers, moves):
    now = time.time()
    for market, percent_change, prev_price, curr_price in moves:
        market_id = market.get("id")
        text = format_alert(market, percent_change, prev_price, curr_price)
        keyboard = market_link_keyboard(market)

        for chat_id, settings in subscribers.items():
            if not should_alert_user(settings, market, percent_change):
                continue

            cooldown_key = f"{chat_id}:{market_id}"
            last_sent = last_alert_time.get(cooldown_key, 0)
            if now - last_sent < ALERT_COOLDOWN_SECONDS:
                continue

            send_message(chat_id, text, reply_markup=keyboard)
            last_alert_time[cooldown_key] = now


def run_bot():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not found in environment.")
        return

    subscribers = load_subscribers()
    last_update_id = None
    last_check_time = 0

    print("Telegram bot started.")
    print(f"Current subscriber count: {len(subscribers)}")

    while True:
        try:
            last_update_id = process_updates(subscribers, last_update_id)

            now = time.time()
            if now - last_check_time >= DEFAULT_CHECK_INTERVAL_SECONDS:
                last_check_time = now
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{timestamp}] Checking markets... (subscribers: {len(subscribers)})")

                markets = get_live_markets()
                if markets:
                    moves = compute_moves(markets)
                    dispatch_alerts(subscribers, moves)

        except Exception as e:
            print(f"Unexpected error in main loop: {e}. Continuing...")

        time.sleep(2)


if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\nBot stopped. Goodbye!")