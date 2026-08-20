import requests
import time
import json
import os
import sqlite3
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ================== CONFIG ==================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

POLYMARKET_API_URL = "https://gamma-api.polymarket.com/markets"
TRADES_API_URL = "https://data-api.polymarket.com/trades"
DEFAULT_CHECK_INTERVAL_SECONDS = 30
WHALE_CHECK_INTERVAL_SECONDS = 60
WHALE_TRADE_THRESHOLD_USD = 50000
DEFAULT_THRESHOLD = 1.0
MIN_THRESHOLD = 0.1
MAX_THRESHOLD = 50.0
MARKET_LIMIT = 30
ALERT_COOLDOWN_SECONDS = 600  # don't re-alert the same market within this window
SUBSCRIBERS_FILE = "subscribers.json"
SUBSCRIBERS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subscribers.db")
PORT = int(os.environ.get("PORT", 10000))

ADMIN_CHAT_ID = "5504538753"  # only this chat_id can use /broadcast and /stats
DONATION_ADDRESS = "TGb88HkX5pFq9eGVojYj7qQRJe83eUH9pV"  # USDT (TRC20 / Tron network only)

# Twitter/X posting (all read from environment variables set in Render, never hardcoded)
TWITTER_API_KEY = os.environ.get("TWITTER_API_KEY")
TWITTER_API_SECRET = os.environ.get("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.environ.get("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = os.environ.get("TWITTER_ACCESS_TOKEN_SECRET")

CATEGORIES = ["Politics", "Crypto", "Sports", "Pop Culture"]
CATEGORY_KEYWORDS = {
    "Politics": ["election", "president", "senate", "congress", "governor", "vote", "poll"],
    "Crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "token"],
    "Sports": ["nfl", "nba", "mlb", "nhl", "soccer", "football", "match", "game", "win the"],
    "Pop Culture": ["oscar", "grammy", "movie", "album", "celebrity", "award"],
}

previous_prices = {}       # market_id -> last seen price
last_alert_time = {}       # (chat_id, market_id) -> unix timestamp, for cooldown
processed_trade_ids = set()  # trade ids already checked for whale alerts
alert_stats = {"date": None, "count": 0}                 # alerts sent today (for /stats)
daily_summary_state = {"date": None, "moves": []}         # top movers collected today

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


# ================== KEEP-ALIVE WEB SERVER (for Render free tier) ==================
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


# ================== SUBSCRIBER STORAGE (SQLite) ==================
def default_user_settings():
    return {
        "threshold": DEFAULT_THRESHOLD,
        "categories": list(CATEGORIES),   # all enabled by default
        "watchlist": [],                  # list of market questions (substrings) to always alert on
        "premium": False,
    }


def _get_db_connection():
    conn = sqlite3.connect(SUBSCRIBERS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id TEXT PRIMARY KEY,
            threshold REAL,
            categories TEXT,
            watchlist TEXT,
            premium INTEGER
        )
    """)
    return conn


def _migrate_json_to_db_if_needed():
    """One-time migration: if subscribers.json exists and the DB is empty, import it."""
    if not os.path.exists(SUBSCRIBERS_FILE):
        return
    conn = _get_db_connection()
    count = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
    if count > 0:
        conn.close()
        return
    try:
        with open(SUBSCRIBERS_FILE, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        conn.close()
        return

    if isinstance(data, list):
        # old format: just a list of chat ids -> migrate
        data = {str(cid): default_user_settings() for cid in data}

    for chat_id, settings in data.items():
        conn.execute(
            "INSERT OR REPLACE INTO subscribers (chat_id, threshold, categories, watchlist, premium) VALUES (?, ?, ?, ?, ?)",
            (
                str(chat_id),
                settings.get("threshold", DEFAULT_THRESHOLD),
                json.dumps(settings.get("categories", list(CATEGORIES))),
                json.dumps(settings.get("watchlist", [])),
                1 if settings.get("premium", False) else 0,
            ),
        )
    conn.commit()
    conn.close()

    try:
        os.rename(SUBSCRIBERS_FILE, SUBSCRIBERS_FILE + ".migrated_bak")
    except OSError:
        pass
    print(f"Migrated {len(data)} subscriber(s) from {SUBSCRIBERS_FILE} to SQLite.")


def load_subscribers():
    """Returns dict: {chat_id_str: settings_dict}, loaded from SQLite."""
    _migrate_json_to_db_if_needed()
    conn = _get_db_connection()
    rows = conn.execute("SELECT chat_id, threshold, categories, watchlist, premium FROM subscribers").fetchall()
    conn.close()

    subscribers = {}
    for chat_id, threshold, categories, watchlist, premium in rows:
        subscribers[chat_id] = {
            "threshold": threshold,
            "categories": json.loads(categories),
            "watchlist": json.loads(watchlist),
            "premium": bool(premium),
        }
    return subscribers


def save_subscribers(subscribers):
    """Persists the full subscribers dict to SQLite (mirrors old JSON-file behavior)."""
    conn = _get_db_connection()
    conn.execute("DELETE FROM subscribers")
    for chat_id, settings in subscribers.items():
        conn.execute(
            "INSERT INTO subscribers (chat_id, threshold, categories, watchlist, premium) VALUES (?, ?, ?, ?, ?)",
            (
                str(chat_id),
                settings.get("threshold", DEFAULT_THRESHOLD),
                json.dumps(settings.get("categories", list(CATEGORIES))),
                json.dumps(settings.get("watchlist", [])),
                1 if settings.get("premium", False) else 0,
            ),
        )
    conn.commit()
    conn.close()


# ================== TELEGRAM API HELPERS (with retry/backoff) ==================
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


# ================== STATS TRACKING ==================
def record_alert_sent(n=1):
    today = datetime.now().strftime("%Y-%m-%d")
    if alert_stats["date"] != today:
        alert_stats["date"] = today
        alert_stats["count"] = 0
    alert_stats["count"] += n


# ================== KEYBOARDS ==================
def category_keyboard(user_settings):
    rows = []
    for cat in CATEGORIES:
        enabled = cat in user_settings["categories"]
        label = f"✅ {cat}" if enabled else f"⬜ {cat}"
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


# ================== COMMAND / CALLBACK HANDLING ==================
def get_market_category(market):
    """Best-effort category guess (Gamma API doesn't reliably expose one)."""
    text = (market.get("question") or "").lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return cat
    return None  # uncategorized markets pass every filter


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
            "/threshold &lt;number&gt; — set your alert sensitivity (e.g. /threshold 2)\n"
            "/categories — choose which topics you want alerts for\n"
            "/watchlist &lt;text&gt; — always get alerts for markets matching this text\n"
            "/mywatchlist — view your watchlist\n"
            "/status — view your current settings\n"
            "/stop — unsubscribe\n\n"
            "You'll also get 🐋 whale trade alerts and a daily top-movers summary automatically.\n\n"
            "⚠️ This is not financial advice — it only reports price movement.\n\n"
            "If this bot is useful to you, /donate to support it 🙏",
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
            send_message(chat_id, "Usage: /threshold 2  (sets alert sensitivity to 2%)")
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
            send_message(chat_id, "Usage: /watchlist bitcoin 150k  (any market containing this text will always alert you)")
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
            items = "\n".join(f"• {w}" for w in watchlist)
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

    elif text == "/donate":
        send_message(
            chat_id,
            "<b>🙏 Support this bot</b>\n\n"
            "If you find these alerts useful, you can send USDT (TRC20 / Tron network only):\n\n"
            f"<code>{DONATION_ADDRESS}</code>\n\n"
            "⚠️ Only send USDT on the Tron (TRC20) network to this address — sending any other coin "
            "or network may result in permanent loss of funds.\n\n"
            "Thank you for your support!",
        )

    elif text == "/stats":
        if str(chat_id) != ADMIN_CHAT_ID:
            send_message(chat_id, "This command is for the bot admin only.")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        alerts_today = alert_stats["count"] if alert_stats["date"] == today else 0
        send_message(
            chat_id,
            f"<b>Bot stats</b>\n"
            f"Subscribers: {len(subscribers)}\n"
            f"Alerts sent today: {alerts_today}",
        )

    elif text.startswith("/broadcast"):
        if str(chat_id) != ADMIN_CHAT_ID:
            send_message(chat_id, "This command is for the bot admin only.")
            return
        message_text = text[len("/broadcast"):].strip()
        if not message_text:
            send_message(chat_id, "Usage: /broadcast Your message here")
            return
        broadcast_message(subscribers, f"📢 <b>Announcement</b>\n\n{message_text}")
        record_alert_sent(len(subscribers))
        send_message(chat_id, f"Broadcast sent to {len(subscribers)} subscriber(s).")


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
        # update the keyboard in place
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


# ================== MARKET DATA ==================
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
    """Returns a list of (market, percent_change) for markets whose price changed this cycle."""
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
    is_up = percent_change > 0
    direction_emoji = "🟢" if is_up else "🔴"
    direction_word = "UP" if is_up else "DOWN"
    arrow = "⬆️" if is_up else "⬇️"

    question = market.get("question", "Unknown market")
    volume = market.get("volume")
    try:
        volume_str = f"${float(volume):,.0f}" if volume else "N/A"
    except (ValueError, TypeError):
        volume_str = "N/A"

    return (
        f"{direction_emoji} <b>MOMENTUM ALERT</b> {direction_emoji}\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>{question}</b>\n\n"
        f"{arrow} <b>{direction_word} {percent_change:+.2f}%</b>\n\n"
        f"💰 Previous: <code>{previous_price:.3f}</code>\n"
        f"💵 Current:  <code>{current_price:.3f}</code>\n"
        f"📈 Volume: {volume_str}\n"
        f"\n⚠️ Not financial advice."
    )


def should_alert_user(user_settings, market, percent_change):
    category = get_market_category(market)
    question_lower = (market.get("question") or "").lower()
    on_watchlist = any(w.lower() in question_lower for w in user_settings["watchlist"])

    if on_watchlist:
        return True  # watchlist bypasses threshold/category filters

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
            record_alert_sent(1)


# ================== WHALE TRADE ALERTS ==================
def get_recent_trades(limit=100):
    params = {"limit": limit}
    try:
        response = requests.get(TRADES_API_URL, params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching trade data: {e}")
        return []


def format_whale_alert(question, outcome, side, usd_value, price):
    side_word = (side or "").upper() or "TRADE"
    outcome_text = f" — {outcome}" if outcome else ""
    return (
        f"🐋 <b>WHALE ALERT</b> 🐋\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>{question}</b>{outcome_text}\n\n"
        f"{side_word}\n"
        f"💵 Size: ${usd_value:,.0f}\n"
        f"💰 Price: {price:.3f}\n"
        f"\n⚠️ Not financial advice."
    )


def check_whale_trades(subscribers):
    if not subscribers:
        return
    trades = get_recent_trades()
    for trade in trades:
        trade_id = trade.get("transactionHash") or trade.get("id") or trade.get("hash")
        if not trade_id or trade_id in processed_trade_ids:
            continue
        processed_trade_ids.add(trade_id)

        try:
            size = float(trade.get("size", 0))
            price = float(trade.get("price", 0))
        except (TypeError, ValueError):
            continue

        usd_value = size * price
        if usd_value < WHALE_TRADE_THRESHOLD_USD:
            continue

        question = trade.get("title") or trade.get("question") or trade.get("market") or "Unknown market"
        outcome = trade.get("outcome", "")
        side = trade.get("side", "")

        text = format_whale_alert(question, outcome, side, usd_value, price)
        broadcast_message(subscribers, text)
        record_alert_sent(len(subscribers))

    # keep memory usage bounded over long uptimes
    if len(processed_trade_ids) > 2000:
        processed_trade_ids.clear()


# ================== TWITTER / X POSTING ==================
def _twitter_oauth1_header(method, url, api_key, api_secret, access_token, access_token_secret):
    """Builds an OAuth 1.0a Authorization header (HMAC-SHA1) without extra dependencies."""
    import hmac
    import hashlib
    import base64
    import uuid
    from urllib.parse import quote

    oauth_params = {
        "oauth_consumer_key": api_key,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }
    param_string = "&".join(
        f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in sorted(oauth_params.items())
    )
    base_string = "&".join([method.upper(), quote(url, safe=""), quote(param_string, safe="")])
    signing_key = f"{quote(api_secret, safe='')}&{quote(access_token_secret, safe='')}"
    signature = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    ).decode()
    oauth_params["oauth_signature"] = signature
    return "OAuth " + ", ".join(f'{quote(k, safe="")}="{quote(v, safe="")}"' for k, v in sorted(oauth_params.items()))


def post_tweet(text):
    """Posts a tweet if Twitter credentials are configured; silently skips otherwise."""
    if not (TWITTER_API_KEY and TWITTER_API_SECRET and TWITTER_ACCESS_TOKEN and TWITTER_ACCESS_TOKEN_SECRET):
        return
    url = "https://api.twitter.com/2/tweets"
    text = text[:280]
    try:
        headers = {
            "Authorization": _twitter_oauth1_header(
                "POST", url, TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET
            ),
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, json={"text": text}, timeout=15)
        if response.status_code >= 300:
            print(f"Twitter post failed ({response.status_code}): {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"Error posting to Twitter: {e}")


def format_daily_summary_tweet(top_moves):
    lines = ["📊 Polymarket Daily Top Movers"]
    for question, pct in top_moves[:3]:
        arrow = "🟢⬆️" if pct > 0 else "🔴⬇️"
        short_q = question if len(question) <= 60 else question[:57] + "..."
        lines.append(f"{arrow} {short_q} ({pct:+.1f}%)")
    lines.append("#Polymarket #PredictionMarkets")
    return "\n".join(lines)[:280]


# ================== DAILY SUMMARY ==================
def check_daily_summary(subscribers):
    today = datetime.now().strftime("%Y-%m-%d")

    if daily_summary_state["date"] is None:
        daily_summary_state["date"] = today
        return

    if today == daily_summary_state["date"]:
        return  # still the same day, nothing to send yet

    moves = daily_summary_state["moves"]
    if moves and subscribers:
        top = sorted(moves, key=lambda m: abs(m[1]), reverse=True)[:5]
        lines = []
        for question, pct in top:
            arrow = "🟢⬆️" if pct > 0 else "🔴⬇️"
            lines.append(f"{arrow} {question} ({pct:+.2f}%)")
        text = "<b>📅 Daily Summary — Top Movers</b>\n\n" + "\n".join(lines)
        broadcast_message(subscribers, text)
        record_alert_sent(len(subscribers))
        post_tweet(format_daily_summary_tweet(top))

    daily_summary_state["date"] = today
    daily_summary_state["moves"] = []


# ================== MAIN LOOP ==================
def run_bot():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not found in environment.")
        return

    subscribers = load_subscribers()
    last_update_id = None
    last_check_time = 0
    last_whale_check_time = 0
    daily_summary_state["date"] = datetime.now().strftime("%Y-%m-%d")  # don't fire a summary on first run

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
                    for market, percent_change, _prev, _curr in moves:
                        daily_summary_state["moves"].append((market.get("question", "Unknown market"), percent_change))

            if now - last_whale_check_time >= WHALE_CHECK_INTERVAL_SECONDS:
                last_whale_check_time = now
                check_whale_trades(subscribers)

            check_daily_summary(subscribers)

        except Exception as e:
            # top-level safety net so one bad cycle never kills the whole bot
            print(f"Unexpected error in main loop: {e}. Continuing...")

        time.sleep(2)


if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\nBot stopped. Goodbye!")
