"""Altcoin Radar v2: volume, breakout, momentum, optional Binance futures OI."""
import json, os, sys, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

SPOT="https://data-api.binance.vision"
FUTURES="https://fapi.binance.com"
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT=os.getenv("TELEGRAM_CHAT_ID","")
TEST=os.getenv("TEST_MODE","false").lower()=="true"
STATE=Path("state.json")
MAX_PAIRS=65
MAX_ALERTS=3
MIN_VOLUME=5_000_000
MIN_SCORE=72
COOLDOWN=6*3600

def get(url,params=None):
    if params: url+="?"+urllib.parse.urlencode(params)
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"AltcoinRadar/2"}),timeout=16) as r:
                return json.load(r)
        except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,ValueError):
            if i==2: raise
            time.sleep(i+1)

def telegram(text):
    if not TOKEN or not CHAT: raise RuntimeError("Telegram secrets are missing")
    payload=urllib.parse.urlencode({"chat_id":CHAT,"text":text,"disable_web_page_preview":"true"}).encode()
    req=urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data=payload)
    try:
        with urllib.request.urlopen(req,timeout=20) as r:
            result=json.load(r)
        if not result.get("ok"): raise RuntimeError("Telegram send failed")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram returned HTTP {e.code}") from None

def state_read():
    try:
        obj=json.loads(STATE.read_text())
        return obj if isinstance(obj,dict) else {}
    except (OSError,ValueError): return {}

def rsi(prices):
    delta=[prices[i]-prices[i-1] for i in range(-14,0)]
    gains=sum(max(v,0) for v in delta)/14
    losses=sum(max(-v,0) for v in delta)/14
    return 100 if losses==0 and gains>0 else (50 if losses==0 else 100-100/(1+gains/losses))

def universe():
    exchange=get(SPOT+"/api/v3/exchangeInfo")
    allowed={x["symbol"] for x in exchange["symbols"] if x.get("status")=="TRADING"
             and x.get("quoteAsset")=="USDT" and x.get("isSpotTradingAllowed",True)
             and x.get("baseAsset") not in {"BTC","ETH","USDT","USDC","FDUSD","TUSD"}
             and not x.get("baseAsset","").endswith(("UP","DOWN","BULL","BEAR"))}
    rows=[]
    for x in get(SPOT+"/api/v3/ticker/24hr"):
        try:
            v=float(x["quoteVolume"])
            change=float(x["priceChangePercent"])
            if x["symbol"] in allowed and v>=MIN_VOLUME and -8<=change<=12:
                rows.append((x["symbol"],v))
        except (KeyError,ValueError,TypeError): pass
    return sorted(rows,key=lambda a:a[1],reverse=True)[:MAX_PAIRS]

def analyse(symbol,volume24):
    data=get(SPOT+"/api/v3/klines",{"symbol":symbol,"interval":"15m","limit":90})
    now_ms=int(time.time()*1000)
    rows=[x for x in data if int(x[6])<now_ms-1500]
    if len(rows)<60: return None
    close=[float(x[4]) for x in rows]
    vol=[float(x[7]) for x in rows]
    price=close[-1]
    change_1h=100*(price/close[-5]-1)
    change_4h=100*(price/close[-17]-1)
    avg=sum(vol[-21:-1])/20
    rvol=vol[-1]/avg if avg else 0
    strength=rsi(close)
    breakout=price>max(float(x[2]) for x in rows[-21:-1])
    if not (rvol>=2.0 and 0.5<=change_1h<=4.0 and -2<=change_4h<=9 and 48<=strength<=70):
        return None
    # Separate categories: fresh volume, breakout, trend continuation.
    if breakout: category="BREAKOUT"
    elif rvol>=3 and change_1h<=2.5: category="EARLY_VOLUME"
    else: category="MOMENTUM"
    score=0
    score+=min(35,20+7.5*(rvol-2))
    score+=20 if 1<=change_1h<=3 else 12
    score+=15 if 52<=strength<=65 else 7
    score+=20 if breakout else 0
    score+=10 if volume24>=20_000_000 else 5
    return dict(symbol=symbol,price=price,volume24=volume24,rvol=rvol,rsi=strength,
                change_1h=change_1h,change_4h=change_4h,breakout=breakout,
                category=category,base_score=round(score),candle_end=int(rows[-1][6]))

def futures_oi(symbol):
    """Best-effort: historical 1h OI on Binance USDT-M futures; None if unavailable."""
    try:
        rows=get(FUTURES+"/futures/data/openInterestHist",
                 {"symbol":symbol,"period":"1h","limit":5})
        if not isinstance(rows,list) or len(rows)<5: return None
        rows=sorted(rows,key=lambda x:int(x["timestamp"]))
        # Compare same metric across four 1h intervals.
        first=float(rows[-5]["sumOpenInterestValue"])
        last=float(rows[-1]["sumOpenInterestValue"])
        return 100*(last/first-1) if first>0 else None
    except (ValueError,TypeError,KeyError,urllib.error.HTTPError,urllib.error.URLError,TimeoutError):
        return None

def alert(x):
    oi="Unavailable (spot-only assessment)" if x["oi"] is None else f'{x["oi"]:+.2f}% (4h)'
    return (f'🔎 ALTCOIN RADAR V2\n\n'
            f'Pair: {x["symbol"]}\nType: {x["category"]}\n'
            f'Score: {x["score"]}/100 (rule-based, not probability)\n'
            f'Price: {x["price"]:.8g}\n'
            f'1h: {x["change_1h"]:+.2f}% | 4h: {x["change_4h"]:+.2f}%\n'
            f'15m relative volume: {x["rvol"]:.2f}x\n'
            f'15m RSI: {x["rsi"]:.1f}\n'
            f'20-candle breakout: {"YES" if x["breakout"] else "NO"}\n'
            f'Futures OI value change: {oi}\n'
            f'24h spot quote volume: ${x["volume24"]:,.0f}\n'
            f'Closed candle: {time.strftime("%Y-%m-%d %H:%M UTC",time.gmtime(x["candle_end"]/1000))}\n'
            f'https://www.binance.com/en/trade/{x["symbol"][:-4]}_USDT?type=spot')

def main():
    if TEST:
        telegram("✅ Altcoin Radar V2: Telegram test succeeded.")
        print("Telegram test sent")
        return
    state=state_read()
    candidates=universe()
    print(f"Scanning {len(candidates)} pairs")
    pre=[]
    for sym,v in candidates:
        try:
            item=analyse(sym,v)
            if item: pre.append(item)
        except Exception as exc:
            print(f"Skipped {sym}: {type(exc).__name__}")
        time.sleep(.07)
    # OI calls only for candidates that pass spot filters, to reduce requests.
    for item in pre:
        item["oi"]=futures_oi(item["symbol"])
        # OI is an additional confirmation, not a mandatory condition.
        # Positive OI adds points; negative OI does not imply a short.
        oi=item["oi"]
        item["score"]=min(100,item["base_score"]+(10 if oi is not None and oi>=3 else 5 if oi is not None and oi>=1 else 0))
        time.sleep(.1)
    qualified=sorted((x for x in pre if x["score"]>=MIN_SCORE),
                     key=lambda x:(x["score"],x["rvol"]),reverse=True)
    now=int(time.time())
    sent=0
    for x in qualified:
        if sent>=MAX_ALERTS: break
        prior=state.get(x["symbol"],{})
        if now-int(prior.get("sent_at",0))<COOLDOWN: continue
        telegram(alert(x))
        state[x["symbol"]]={"sent_at":now,"candle_end":x["candle_end"]}
        STATE.write_text(json.dumps(state,indent=2)+"\n")
        print(f'Sent {x["symbol"]} score={x["score"]}')
        sent+=1
    state={k:v for k,v in state.items() if isinstance(v,dict) and now-int(v.get("sent_at",0))<7*86400}
    STATE.write_text(json.dumps(state,indent=2)+"\n")
    print(f"Spot passed={len(pre)}; qualified={len(qualified)}; sent={sent}")

if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(f"Radar failed: {type(exc).__name__}: {str(exc)[:150]}",file=sys.stderr)
        sys.exit(1)
