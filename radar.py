"""Free spot-altcoin radar: Binance public market-data -> Telegram."""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://data-api.binance.vision"
STATE = Path("state.json")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"
MIN_24H_QUOTE_VOLUME = 5_000_000
MAX_CANDIDATES = 55
MAX_ALERTS_PER_RUN = 3
SCORE_THRESHOLD = 70
COOLDOWN_SECONDS = 6 * 3600
HEADERS = {"User-Agent": "AltcoinRadar/1.0"}


def fetch(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=18) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (418, 451):
                break
            time.sleep(attempt + 1)
    raise RuntimeError(f"Public API request failed: {type(last).__name__} {last}")


def telegram_send(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in GitHub Actions secrets")
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=18) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError("Telegram rejected the message")
    except urllib.error.HTTPError as exc:
        # Avoid printing URL/token in error logs
        raise RuntimeError(f"Telegram HTTP error {exc.code}; check chat ID and bot permissions") from None


def rsi14(closes):
    if len(closes) < 15:
        return None
    diffs = [closes[i] - closes[i - 1] for i in range(-14, 0)]
    gains = sum(max(x, 0) for x in diffs) / 14
    losses = sum(max(-x, 0) for x in diffs) / 14
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100 - 100 / (1 + gains / losses)


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data):
    STATE.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def select_symbols():
    exchange = fetch(BASE + "/api/v3/exchangeInfo")
    symbols = {
        item["symbol"] for item in exchange["symbols"]
        if item.get("status") == "TRADING"
        and item.get("quoteAsset") == "USDT"
        and item.get("isSpotTradingAllowed", True)
        and item.get("baseAsset") not in {"BTC", "ETH", "USDC", "FDUSD", "TUSD", "USDT"}
        and not item.get("baseAsset", "").endswith(("UP", "DOWN", "BULL", "BEAR"))
    }
    tickers = fetch(BASE + "/api/v3/ticker/24hr")
    ranked = []
    for item in tickers:
        try:
            symbol = item["symbol"]
            quote_vol = float(item["quoteVolume"])
            day_change = float(item["priceChangePercent"])
            if symbol in symbols and quote_vol >= MIN_24H_QUOTE_VOLUME and -8 <= day_change <= 18:
                ranked.append((symbol, quote_vol, day_change))
        except (KeyError, TypeError, ValueError):
            pass
    ranked.sort(key=lambda row: row[1], reverse=True)
    return ranked[:MAX_CANDIDATES]


def analyse(symbol, volume24):
    rows = fetch(BASE + "/api/v3/klines", {"symbol": symbol, "interval": "15m", "limit": 60})
    now_ms = int(time.time() * 1000)
    closed = [r for r in rows if int(r[6]) <= now_ms - 1500]
    if len(closed) < 40:
        return None
    closes = [float(r[4]) for r in closed]
    volumes = [float(r[7]) for r in closed]  # quote asset volume (USDT)
    last = closes[-1]
    ref = closes[-5]
    change_1h = 100 * (last / ref - 1) if ref > 0 else 0
    prev_avg = sum(volumes[-21:-1]) / 20
    rvol = volumes[-1] / prev_avg if prev_avg > 0 else 0
    rsi = rsi14(closes)
    if rsi is None or not (rvol >= 2.0 and 0.5 <= change_1h <= 7.0 and 48 <= rsi <= 74):
        return None
    prev_high = max(float(r[2]) for r in closed[-21:-1])
    breakout = last > prev_high
    score = 0
    score += min(35, 20 + 7.5 * (rvol - 2))
    score += 20 if 1 <= change_1h <= 4 else 12
    score += 15 if 52 <= rsi <= 66 else 7
    score += 20 if breakout else 0
    score += 10 if volume24 >= 20_000_000 else 5
    score = round(score)
    if score < SCORE_THRESHOLD:
        return None
    return {"symbol": symbol, "rvol": rvol, "change_1h": change_1h, "rsi": rsi,
            "breakout": breakout, "score": score, "price": last,
            "volume24": volume24, "candle_end": int(closed[-1][6])}


def format_alert(x):
    link = f"https://www.binance.com/en/trade/{x['symbol'][:-4]}_USDT?type=spot"
    return (f"🔎 ALTCOIN RADAR | Watchlist alert\n\n"
            f"Coin: {x['symbol']}\n"
            f"Score (heuristic): {x['score']}/100\n"
            f"1h price change: {x['change_1h']:+.2f}%\n"
            f"15m relative volume: {x['rvol']:.2f}x\n"
            f"RSI (15m): {x['rsi']:.1f}\n"
            f"Previous 20-candle high broken: {'YES' if x['breakout'] else 'NO'}\n"
            f"24h USDT volume: {x['volume24']:,.0f}\n"
            f"Last closed 15m candle: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(x['candle_end']/1000))}\n"
            f"Chart: {link}\n\n"
            f"⚠️ Market-activity alert, NOT a buy signal or calibrated probability of profit.")


def main():
    if TEST_MODE:
        telegram_send("✅ Altcoin Radar: Telegram connection works. This is a test, not a trade signal.")
        print("Telegram test delivered")
        return
    state = load_state()
    candidates = select_symbols()
    print(f"Scanning {len(candidates)} liquid USDT spot pairs")
    found = []
    for symbol, vol, _ in candidates:
        try:
            result = analyse(symbol, vol)
            if result:
                found.append(result)
        except Exception as exc:
            print(f"Skipping {symbol}: {type(exc).__name__}")
        time.sleep(0.08)
    found.sort(key=lambda x: (x["score"], x["rvol"]), reverse=True)
    now = int(time.time())
    sent = 0
    for result in found:
        symbol = result["symbol"]
        previous = state.get(symbol, {})
        if now - int(previous.get("sent_at", 0)) < COOLDOWN_SECONDS:
            continue
        if sent >= MAX_ALERTS_PER_RUN:
            break
        telegram_send(format_alert(result))
        state[symbol] = {"sent_at": now, "candle_end": result["candle_end"]}
        save_state(state)
        print(f"Sent: {symbol}, score={result['score']}")
        sent += 1
    # Keep only recent state; reduce repository file growth
    state = {k: v for k, v in state.items() if now - int(v.get("sent_at", 0)) < 7 * 86400}
    save_state(state)
    print(f"Total qualified={len(found)}; alerts sent={sent}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Don't log secret-bearing API URLs
        print(f"Radar failed: {type(exc).__name__}: {str(exc)[:180]}", file=sys.stderr)
        sys.exit(1)
