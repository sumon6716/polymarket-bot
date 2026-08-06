import requests
import time
import os
from datetime import datetime

API_URL = "https://gamma-api.polymarket.com/markets"

REFRESH_SECONDS = 15
MOMENTUM_THRESHOLD = 1.0
MARKET_LIMIT = 20

previous_prices = {}


def get_live_markets(limit=MARKET_LIMIT, active=True, closed=False):
    params = {
        "limit": limit,
        "active": str(active).lower(),
        "closed": str(closed).lower(),
    }
    try:
        response = requests.get(API_URL, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"এরর হয়েছে ডেটা আনার সময়: {e}")
        return []


def get_market_price(market):
    try:
        prices = market.get("outcomePrices")
        if prices:
            if isinstance(prices, str):
                import json
                prices = json.loads(prices)
            return float(prices[0])
    except (ValueError, TypeError, IndexError, KeyError):
        pass
    return None


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def check_momentum(markets):
    alerts = []

    for market in markets:
        market_id = market.get("id")
        question = market.get("question", "শিরোনাম নেই")
        current_price = get_market_price(market)

        if market_id is None or current_price is None:
            continue

        previous_price = previous_prices.get(market_id)

        if previous_price is not None:
            change = current_price - previous_price
            percent_change = (change / previous_price) * 100 if previous_price != 0 else 0

            if abs(percent_change) >= MOMENTUM_THRESHOLD:
                direction = "উপরে" if change > 0 else "নিচে"
                alerts.append({
                    "question": question,
                    "previous_price": previous_price,
                    "current_price": current_price,
                    "percent_change": percent_change,
                    "direction": direction,
                })

        previous_prices[market_id] = current_price

    return alerts


def print_alerts(alerts):
    if not alerts:
        print("এই মুহূর্তে উল্লেখযোগ্য দামের পরিবর্তন (মোমেন্টাম) পাওয়া যায়নি।")
        return

    alerts.sort(key=lambda a: abs(a["percent_change"]), reverse=True)

    print(f"{len(alerts)}টি মোমেন্টাম এলার্ট পাওয়া গেছে:\n")
    for i, alert in enumerate(alerts, start=1):
        print(f"{i}. প্রশ্ন: {alert['question']}")
        print(f"   {alert['direction']} পরিবর্তন: {alert['percent_change']:+.2f}%")
        print(f"   আগের দাম: {alert['previous_price']:.3f}  ->  বর্তমান দাম: {alert['current_price']:.3f}")
        print("-" * 60)


def run_momentum_monitor(limit=MARKET_LIMIT, refresh_seconds=REFRESH_SECONDS,
                          threshold=MOMENTUM_THRESHOLD):
    print("মোমেন্টাম মনিটরিং শুরু হচ্ছে...")
    print(f"থ্রেশহোল্ড: {threshold}% | রিফ্রেশ: প্রতি {refresh_seconds} সেকেন্ড")
    print("(প্রথম রাউন্ডে কোনো তুলনা হবে না, শুধু বেসলাইন দাম রেকর্ড হবে)")
    time.sleep(2)

    round_number = 0

    try:
        while True:
            round_number += 1
            clear_screen()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            print("=" * 60)
            print(f"Polymarket মোমেন্টাম মনিটর - রাউন্ড #{round_number}")
            print(f"সময়: {now}")
            print(f"থ্রেশহোল্ড: {threshold}%  |  রিফ্রেশ: {refresh_seconds}s")
            print("=" * 60 + "\n")

            markets = get_live_markets(limit=limit)

            if not markets:
                print("কোনো মার্কেট ডেটা পাওয়া যায়নি। পরের রাউন্ডে আবার চেষ্টা হবে।")
            else:
                alerts = check_momentum(markets)
                print_alerts(alerts)

            print(f"\nপরবর্তী চেক {refresh_seconds} সেকেন্ড পর...")
            time.sleep(refresh_seconds)

    except KeyboardInterrupt:
        print("\n\nমনিটরিং বন্ধ করা হয়েছে। ধন্যবাদ!")


if __name__ == "__main__":
    run_momentum_monitor()