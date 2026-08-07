# Polymarket Bot

Polymarket-এর Gamma API থেকে লাইভ মার্কেট ডেটা নিয়ে আসে এবং দামের দ্রুত পরিবর্তন (মোমেন্টাম) ট্র্যাক করে টার্মিনালে এলার্ট দেখায়।

## এটি কী করে

- প্রতি নির্দিষ্ট সময় পর পর (ডিফল্ট ১৫ সেকেন্ড) Polymarket-এর সক্রিয় মার্কেট থেকে দাম নিয়ে আসে
- আগের রাউন্ডের দামের সাথে তুলনা করে
- দাম নির্দিষ্ট শতাংশের (ডিফল্ট ১%) বেশি বদলালে টার্মিনালে এলার্ট দেখায়

## প্রয়োজনীয়তা

- Python 3.x
- requests লাইব্রেরি

## ইনস্টল করুন

pip install requests

## চালানোর নিয়ম

python bot.py

বন্ধ করতে Ctrl+C চাপুন।

## সেটিংস বদলানো

bot.py ফাইলের উপরের দিকে এই ভ্যারিয়েবলগুলো বদলাতে পারবেন:

- REFRESH_SECONDS: কত সেকেন্ড পর পর দাম চেক হবে (ডিফল্ট 15)
- MOMENTUM_THRESHOLD: কত % পরিবর্তনে এলার্ট আসবে (ডিফল্ট 1.0)
- MARKET_LIMIT: একবারে কতগুলো মার্কেট মনিটর করা হবে (ডিফল্ট 20)

## গুরুত্বপূর্ণ সতর্কতা



# Polymarket Bot (English Version)

This open-source Python bot connects to the Polymarket Live Feed API to monitor active prediction markets and track rapid price changes (momentum) directly in your terminal.

## What It Does

- Fetches live data from active Polymarket events at regular intervals (Default: 15 seconds).
- Compares the latest price updates with the previous round's data.
- Triggers a terminal alert if the price changes beyond a specific percentage (Default: 1.0%).

## Requirements

- Python 3.x
- requests library

## Installation

pip install requests
## How to Run

python bot.py
*Press Ctrl+C to stop the bot.*

## Configuration Settings

You can easily modify these global variables at the top of the bot.py file:

- REFRESH_SECONDS: Time interval between price checks in seconds (Default: 15).
- MOMENTUM_THRESHOLD: Minimum percentage change required to trigger an alert (Default: 1.0).
- MARKET_LIMIT: Maximum number of markets to monitor simultaneously (Default: 20).

## Disclaimer

This tool is strictly for informational and educational purposes to track price movement. It does not constitute financial advice, market predictions, or any guarantee of profits. Trading on Polymarket involves significant risk; trade at your own discretion.রিবর্তন শনাক্ত করে — এটি কোনো আর্থিক পরামর্শ, ভবিষ্যদ্বাণী বা গ্যারান্টিড লাভের নিশ্চয়তা দেয় না। Polymarket-এ ট্রেড করা ঝুঁকিপূর্ণ; নিজ দায়িত্বে সিদ্ধান্ত নিন।
