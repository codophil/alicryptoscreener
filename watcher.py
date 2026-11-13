"""Altcoin Market Watcher V3 – free scheduled Binance USD-M Futures scanner.

Standard library only. Never trades or asks for exchange API credentials.
A whale "trade" here is an aggregated executed trade (aggTrades), NOT an order-book order.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://fapi.binance.com"
CONFIG = Path(__file__).resolve().parent / "config.json"
STATE = Path(__file__).resolve().parent / "state.json"
LOG = logging.getLogger("watcher")


def fetch(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urlencode(params)
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": "AltcoinWatcher/3.0"}), timeout=14) as response:
                result = json.load(response)
                if isinstance(result, dict) and "code" in result and int(result["code"]) < 0:
                    raise RuntimeError(f"Binance API error code={result['code']}")
                return result
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Binance endpoint {path}: {type(exc).__name__}: {str(exc)[:120]}") from exc
            time.sleep(1 + attempt)


def telegram_send(message, token, chat_id):
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret is missing")
    body = urlencode({"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}).encode()
    req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=20) as response:
            result = json.load(response)
            if not result.get("ok"):
                raise RuntimeError("Telegram rejected message")
    except HTTPError as exc:
        # Never print Telegram URL: it contains the private bot token.
        raise RuntimeError(f"Telegram HTTP error {exc.code}; check bot, chat id and permissions") from None
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Telegram connectivity error: {type(exc).__name__}") from None


def load_state():
    try:
        obj = json.loads(STATE.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (ValueError, OSError):
        return {}


def save_state(state):
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def valid_symbols(config):
    exchange = fetch("/fapi/v1/exchangeInfo")
    allow = {
        item["symbol"] for item in exchange["symbols"]
        if item.get("status") == "TRADING"
        and item.get("quoteAsset") == "USDT"
        and item.get("contractType") == "PERPETUAL"
        and item.get("baseAsset") not in {"BTC", "ETH", "USDT", "USDC"}
    }
    tickers = fetch("/fapi/v1/ticker/24hr")
    pairs = []
    for row in tickers:
        try:
            volume = float(row["quoteVolume"])
            if row["symbol"] in allow and volume >= config["min_24h_quote_volume_usdt"]:
                pairs.append((row["symbol"], volume))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(pairs, key=lambda item: item[1], reverse=True)[:config["max_symbols"]]


def candles_for(symbol, baseline):
    data = fetch("/fapi/v1/klines", {"symbol": symbol, "interval": "5m", "limit": baseline + 5})
    now_ms = int(time.time() * 1000)
    # Only completed five-minute candles: never treat a partial 5m candle as a full-volume candle.
    return [row for row in data if int(row[6]) < now_ms - 1000]


def calc_metrics(rows, baseline, symbol, vol24):
    if len(rows) < baseline + 2:
        return None
    latest = rows[-1]
    history = rows[-baseline-1:-1]
    quote_vol = float(latest[7])
    reference = sum(float(row[7]) for row in history) / baseline
    if reference <= 0 or quote_vol <= 0:
        return None
    open_price = float(latest[1])
    price = float(latest[4])
    move = (price / open_price - 1) * 100 if open_price > 0 else 0
    buy_quote = float(latest[10])
    buy_pct = max(0, min(100, buy_quote / quote_vol * 100))
    previous = float(rows[-2][4])
    return {
        "symbol": symbol,
        "volume24": vol24,
        "price": price,
        "move": move,
        "five_min_volume": quote_vol,
        "rvol": quote_vol / reference,
        "buy_pct": buy_pct,
        "sell_pct": 100 - buy_pct,
        "candle_end": int(latest[6]),
        "previous_close": previous,
        "oi_pct": None,
    }


def oi_change_5m(symbol, candle_end):
    """Use two successive OI samples that are no newer than the closed signal candle.

    Historical OI time is a snapshot; the latest may arrive late. If matching
    samples are not available, return None instead of a fabricated 0% change.
    """
    rows = fetch("/futures/data/openInterestHist", {"symbol": symbol, "period": "5m", "limit": 5})
    if not isinstance(rows, list):
        return None
    candidates = sorted((r for r in rows if int(r["timestamp"]) <= candle_end + 30000),
                        key=lambda r: int(r["timestamp"]))
    if len(candidates) < 2:
        return None
    before, after = candidates[-2:]
    if candle_end - int(after["timestamp"]) > 7 * 60 * 1000:
        return None
    if int(after["timestamp"]) - int(before["timestamp"]) > 7 * 60 * 1000:
        return None
    # Contract quantity, not the dollar value, avoids counting price appreciation as position growth.
    old = float(before["sumOpenInterest"])
    new = float(after["sumOpenInterest"])
    return (new / old - 1) * 100 if old > 0 else None


def score_for(kind, m):
    rv = m["rvol"]
    move = abs(m["move"])
    buy = m["buy_pct"]
    oi = m["oi_pct"]
    # 1–10 independent, interpretable rule-based activity scores.
    if kind == "AWAKENING":
        points = 4 + min(3, max(0, int(rv / 6)))
        points += 1 if move <= 1.0 else 0
        points += 2 if oi is not None and abs(oi) >= 2 else 1 if oi is not None and abs(oi) >= 1 else 0
    elif kind == "MOMENTUM":
        points = 3 + min(3, int(rv / 3)) + min(3, int(move / 2))
        points += 1 if buy >= 60 or buy <= 40 else 0
    else:  # EARLY BUY WATCH
        points = 4 + min(2, int(rv / 4)) + (2 if buy >= 65 else 1) + (2 if oi is not None and oi >= 3 else 1)
    return max(1, min(10, points))


def classify(m, conf):
    events = []
    rv, move, vol, buy, oi = m["rvol"], m["move"], m["five_min_volume"], m["buy_pct"], m["oi_pct"]
    aw = conf["awakening"]
    if (aw["enabled"] and rv >= aw["min_relative_volume"] and vol >= aw["min_5m_volume_usdt"]
            and abs(move) <= aw["max_abs_price_move_pct"]
            and oi is not None and abs(oi) >= aw["min_abs_oi_change_pct"]):
        score = score_for("AWAKENING", m)
        if score >= aw["min_score"]:
            events.append(("AWAKENING", score))
    mo = conf["momentum"]
    if (mo["enabled"] and rv >= mo["min_relative_volume"] and vol >= mo["min_5m_volume_usdt"]
            and abs(move) >= mo["min_abs_price_move_pct"]):
        score = score_for("MOMENTUM", m)
        if score >= mo["min_score"]:
            events.append(("MOMENTUM", score))
    early = conf["early_buy_watch"]
    if (early["enabled"] and rv >= early["min_relative_volume"] and vol >= early["min_5m_volume_usdt"]
            and buy >= early["min_buy_pressure_pct"] and oi is not None and oi >= early["min_oi_change_pct"]
            and early["min_price_move_pct"] <= move <= early["max_price_move_pct"]):
        score = score_for("EARLY_BUY_WATCH", m)
        if score >= early["min_score"]:
            events.append(("EARLY_BUY_WATCH", score))
    return events


def market_side(m):
    buy = m["buy_pct"]
    if buy >= 58:
        return "🟢 Taker BUY dominant"
    if buy <= 42:
        return "🔴 Taker SELL dominant"
    return "⚪ Mixed / balanced"


def money(n):
    return f"${n/1e6:.2f}M" if n >= 1e6 else f"${n/1e3:.1f}K"


def market_message(kind, score, m, baseline):
    labels = {"AWAKENING": "🦅 AWAKENING", "MOMENTUM": "⚡ MOMENTUM", "EARLY_BUY_WATCH": "🎯 EARLY BUY WATCH"}
    oi = f"{m['oi_pct']:+.2f}%" if m["oi_pct"] is not None else "N/A"
    bars = "🟩" * score + "⬜" * (10 - score)
    return (f"ALTCOIN MARKET WATCHER V3\n{labels[kind]} — {m['symbol']}\n"
            f"📍 {market_side(m)}\n\n"
            f"💰 Price: {m['price']:.9g}  (5m: {m['move']:+.2f}%)\n"
            f"📊 Vol 5m: {money(m['five_min_volume'])}  ({m['rvol']:.1f}x previous {baseline} closed 5m candles)\n"
            f"⚖️ Taker Buy/Sell: {m['buy_pct']:.0f}% / {m['sell_pct']:.0f}%\n"
            f"🧲 OI contracts (5m): {oi}\n"
            f"⭐ Activity score: {score}/10\n{bars}\n"
            f"🕒 Candle closed: {datetime.fromtimestamp(m['candle_end']/1000,timezone.utc):%Y-%m-%d %H:%M} UTC\n"
            f"📈 https://www.binance.com/en/futures/{m['symbol']}\n"
            f"ℹ️ Market activity alert, not an automatic buy/sell recommendation.")


def get_whale_events(symbol, config, now_ms):
    """Sample up to the latest N aggregate executed trades, not complete 5m trade history."""
    opts = config["whale_trade"]
    trades = fetch("/fapi/v1/aggTrades", {"symbol": symbol, "limit": opts["max_trades_per_symbol"]})
    earliest = now_ms - opts["lookback_minutes"] * 60_000
    out = []
    for trade in trades:
        ts = int(trade["T"])
        if ts < earliest or ts > now_ms + 1000:
            continue
        value = float(trade["p"]) * float(trade["q"])
        if value < opts["min_trade_usdt"]:
            continue
        # isBuyerMaker=true => aggressive seller; false => aggressive buyer.
        side = "SELL" if trade["m"] else "BUY"
        out.append({"id": int(trade["a"]), "symbol": symbol, "side": side,
                    "price": float(trade["p"]), "value": value, "time": ts})
    return out


def whale_message(event):
    icon = "🟢" if event["side"] == "BUY" else "🔴"
    return (f"ALTCOIN MARKET WATCHER V3\n🐋 LARGE AGGREGATED TRADE — {event['symbol']}\n\n"
            f"💰 Price: {event['price']:.9g}\n"
            f"{icon} Taker side: {event['side']}\n"
            f"💵 Executed aggregate value: {money(event['value'])}\n"
            f"🆔 Aggregate trade ID: {event['id']}\n"
            f"🕒 {datetime.fromtimestamp(event['time']/1000,timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n"
            f"📈 https://www.binance.com/en/futures/{event['symbol']}\n"
            f"ℹ️ Sample of recent matched trades, NOT a resting order, not a complete whale feed.")


def run():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if os.getenv("TEST_MODE", "false").lower() == "true":
        telegram_send("✅ Market Watcher V3: Telegram test successful. No market scan was performed.", token, chat)
        LOG.info("Telegram test sent")
        return
    if not token or not chat:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID repository secrets")
    conf = json.loads(CONFIG.read_text(encoding="utf-8"))
    state = load_state()
    now = int(time.time())
    now_ms = now * 1000
    pairs = valid_symbols(conf)
    if not pairs:
        raise RuntimeError("No futures pairs returned; Binance access or filters may be blocked")
    LOG.info("Scanning %s USD-M perpetual pairs", len(pairs))
    results = []
    errors = 0
    oi_unavailable = 0
    oi_queries = 0
    # Collect all spot-like futures volume signals first; OI only for volume-active symbols.
    for symbol, volume24 in pairs:
        try:
            rows = candles_for(symbol, conf["baseline_candles"])
            m = calc_metrics(rows, conf["baseline_candles"], symbol, volume24)
            if m and m["rvol"] >= 2.5 and m["five_min_volume"] >= 75_000:
                results.append(m)
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            LOG.warning("Klines %s: %s", symbol, str(exc)[:100])
            errors += 1
        time.sleep(0.04)
    if errors == len(pairs):
        raise RuntimeError("All futures candle requests failed; do not treat this as zero alerts")
    # High-activity symbols checked for OI first, capped to keep API use predictable.
    results.sort(key=lambda m: m["rvol"] * math.log1p(m["five_min_volume"]), reverse=True)
    for m in results[:40]:
        try:
            m["oi_pct"] = oi_change_5m(m["symbol"], m["candle_end"])
            oi_queries += 1
            if m["oi_pct"] is None:
                oi_unavailable += 1
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            oi_unavailable += 1
            LOG.warning("OI %s unavailable: %s", m["symbol"], str(exc)[:90])
        time.sleep(0.05)

    proposed = []
    for m in results:
        for kind, score in classify(m, conf):
            key = f"{kind}:{m['symbol']}:{'UP' if m['move'] >= 0 else 'DOWN'}"
            last = state.get(key, {})
            cooldown = conf[kind.lower()]["cooldown_minutes"] * 60
            if now - int(last.get("sent_at", 0)) < cooldown:
                continue
            if int(last.get("candle_end", 0)) == m["candle_end"]:
                continue
            proposed.append({"key": key, "message": market_message(kind, score, m, conf["baseline_candles"]),
                             "score": score, "candle_end": m["candle_end"], "kind": kind})

    if conf["whale_trade"]["enabled"]:
        opts = conf["whale_trade"]
        for symbol, _ in pairs[:opts["scan_top_symbols"]]:
            try:
                trades = get_whale_events(symbol, conf, now_ms)
                for event in trades:
                    key = f"WHALE:{symbol}:{event['id']}"
                    if key in state:
                        continue
                    # Distinct aggregate IDs are independent events; no per-symbol suppression.
                    proposed.append({"key": key, "message": whale_message(event), "score": 0,
                                     "candle_end": event["time"], "kind": "WHALE_TRADE",
                                     "whale_value": event["value"]})
            except (RuntimeError, ValueError, TypeError, KeyError) as exc:
                LOG.warning("AggTrades %s: %s", symbol, str(exc)[:100])
            time.sleep(0.05)

    # Put strong volume/early events first, then largest whale trades.
    proposed.sort(key=lambda x: (x["score"], x.get("whale_value", 0)), reverse=True)
    sent = 0
    counts = {}
    for event in proposed:
        if sent >= conf["max_alerts_per_run"]:
            break
        telegram_send(event["message"], token, chat)
        state[event["key"]] = {"sent_at": now, "candle_end": event["candle_end"]}
        sent += 1
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
        save_state(state)
        time.sleep(0.2)
    # prune keys after seven days; retain exactly-enough data for cooldown and whale de-dupe
    state = {key: v for key, v in state.items() if isinstance(v, dict) and now - int(v.get("sent_at", 0)) < 7 * 86400}
    save_state(state)
    LOG.info("Pairs=%s | active_volume=%s | OI_calls=%s | OI_missing=%s | candle_errors=%s | candidates=%s | sent=%s | types=%s",
             len(pairs), len(results), oi_queries, oi_unavailable, errors, len(proposed), sent, counts)
    if oi_queries and oi_unavailable == oi_queries:
        LOG.warning("All requested OI values unavailable; AWAKENING and EARLY BUY WATCH need OI and may be silent")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        # Never dump an exception URL with a Telegram token.
        print(f"FATAL: {type(exc).__name__}: {str(exc)[:180]}", file=sys.stderr)
        raise SystemExit(1)
