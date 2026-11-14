"""Public market alert scanner. No exchange credentials or trading operations."""
import concurrent.futures as cf
import datetime as dt
import json
import math
import os
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = {'OKX':'https://www.okx.com','GATE':'https://api.gateio.ws/api/v4',
       'KUCOIN':'https://api-futures.kucoin.com','BINANCE_SPOT':'https://data-api.binance.vision',
       'DEX':'https://api.dexscreener.com','GECKO':'https://api.geckoterminal.com/api/v2'}

class APIError(Exception): pass

def num(x, default=None):
    try:
        v=float(x)
        return v if math.isfinite(v) else default
    except (TypeError,ValueError,OverflowError): return default

def seq(x):
    if not isinstance(x,list): raise APIError('expected list')
    return x

class Client:
    def __init__(self, source, rate, budget):
        self.source=source; self.rate=rate; self.deadline=time.monotonic()+budget
        self.lock=threading.Lock(); self.next=0.; self.blocked=False
    def get(self,path,**params):
        for attempt in range(2):
            with self.lock:
                if self.blocked or time.monotonic()>=self.deadline: raise APIError('source unavailable or time budget reached')
                delay=max(0,self.next-time.monotonic()); self.next=max(self.next,time.monotonic())+1/self.rate
            if delay: time.sleep(delay)
            if time.monotonic()>=self.deadline: raise APIError('time budget reached')
            url=URL[self.source]+path+('?' + urllib.parse.urlencode(params) if params else '')
            try:
                with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'MarketWatcher/5.0','Accept':'application/json'}),timeout=9) as r:
                    out=json.load(r)
                if self.source in ('OKX','KUCOIN'):
                    if str(out.get('code')) != ('0' if self.source=='OKX' else '200000'):
                        raise APIError('API code '+str(out.get('code')))
                    return out['data']
                return out
            except urllib.error.HTTPError as e:
                if e.code in (401,403,451):
                    self.blocked=True; raise APIError(f'HTTP {e.code}: source access denied') from None
                if e.code==429:
                    with self.lock: self.next=max(self.next,time.monotonic()+5)
                if attempt or e.code not in (429,500,502,503,504): raise APIError(f'HTTP {e.code}') from None
            except (OSError,ValueError) as e:
                if attempt: raise APIError(type(e).__name__) from None
            time.sleep(1)
        raise APIError('request failed')

@dataclass
class Market:
    source:str; symbol:str; asset:str; daily:float|None; multiplier:float=1.; oi:float|None=None; oi_ts:float|None=None
@dataclass
class Candle:
    ts:int; o:float; c:float; volume:float; buy:float|None=None
@dataclass
class Signal:
    source:str; symbol:str; asset:str; ts:int; price:float; move:float; volume:float; rvol:float; baseline:str
    pressure:float|None=None; pressure_label:str='N/A'; oi:float|None=None; oi_minutes:float|None=None
    liquidity:float|None=None; link:str=''; kind:str=''; direction:str=''; score:int=0; tier:str='NORMAL'; confirms:tuple=(); persistent:int=0

def asset_name(x): return 'BTC' if x=='XBT' else x

def markets(client, now):
    src=client.source; out=[]
    if src=='OKX':
        rows=seq(client.get('/api/v5/market/tickers',instType='SWAP'))
        meta={x['instId']:x for x in seq(client.get('/api/v5/public/instruments',instType='SWAP')) if x.get('state')=='live' and x.get('settleCcy')=='USDT' and x.get('ctType')=='linear' and x.get('ctValCcy')==x['instId'].split('-')[0] and (x.get('instFamily') or x['instId'].removesuffix('-SWAP')).endswith('-USDT')}
        try: oi={x['instId']:x for x in seq(client.get('/api/v5/public/open-interest',instType='SWAP'))}
        except APIError: oi={}
        for x in rows:
            s=x['instId']; m=meta.get(s); p=num(x.get('last')); v=num(x.get('volCcy24h')); o=oi.get(s,{})
            if m and s.endswith('-USDT-SWAP') and p and v is not None:
                mult=num(m.get('ctVal')); ts=num(o.get('ts'))
                out.append(Market(src,s,s.split('-')[0],p*v,mult or 0,num(o.get('oi')),ts/1000 if ts else None))
    elif src=='GATE':
        rows=seq(client.get('/futures/usdt/tickers'))
        for x in rows:
            s=x['contract']; v=num(x.get('volume_24h_quote'))
            if s.endswith('_USDT') and v is not None: out.append(Market(src,s,s[:-5],v,0))
    elif src=='KUCOIN':
        for x in seq(client.get('/api/v1/contracts/active')):
            s=x['symbol']; a=x.get('baseCurrency',''); mult=num(x.get('multiplier'))
            if x.get('quoteCurrency')=='USDT' and x.get('settleCurrency')=='USDT' and not x.get('isInverse') and x.get('expireDate') is None and x.get('status','Open')=='Open' and x.get('marketType','CRYPTO')=='CRYPTO' and mult and mult>0:
                out.append(Market(src,s,asset_name(a),num(x.get('turnoverOf24h')),mult,num(x.get('openInterest')),time.time()))
    else:
        allowed={x['symbol']:x['baseAsset'] for x in client.get('/api/v3/exchangeInfo')['symbols'] if x.get('status')=='TRADING' and x.get('quoteAsset')=='USDT' and x.get('isSpotTradingAllowed',True)}
        for x in seq(client.get('/api/v3/ticker/24hr')):
            s=x['symbol']; v=num(x.get('quoteVolume'))
            if s in allowed and v is not None: out.append(Market(src,s,allowed[s],v))
    return out

def candles(client,m,now,n):
    src=m.source; result=[]; end=int(now//300)*300
    if src=='OKX':
        rows=client.get('/api/v5/market/candles',instId=m.symbol,bar='5m',limit=max(n+3,12))
        result=[Candle(int(x[0])//1000,float(x[1]),float(x[4]),float(x[7])) for x in rows if str(x[8])=='1']
    elif src=='GATE':
        rows=client.get('/futures/usdt/candlesticks',contract=m.symbol,interval='5m',limit=max(n+3,12))
        result=[Candle(int(x['t']),float(x['o']),float(x['c']),float(x['sum'])) for x in rows if x.get('sum') is not None]
    elif src=='KUCOIN':
        count=289 if m.daily is None else max(n+3,12)
        rows=client.get('/api/v1/kline/query',symbol=m.symbol,granularity=300,**{'from':(end-count*300)*1000,'to':end*1000})
        result=[Candle(int(x[0])//1000,float(x[1]),float(x[4]),float(x[6])) for x in rows if len(x)>=7]
    else:
        rows=client.get('/api/v3/klines',symbol=m.symbol,interval='5m',limit=max(n+3,12))
        result=[Candle(int(x[0])//1000,float(x[1]),float(x[4]),float(x[7]),float(x[10])) for x in rows]
    return sorted({x.ts:x for x in result if x.ts+300<=end and x.o>0 and x.c>0 and x.volume>=0 and all(math.isfinite(y) for y in (x.o,x.c,x.volume))}.values(),key=lambda x:x.ts)

def candle_signal(m,rows,now,cfg):
    n=cfg['baseline_candles']; end=int(now//300)*300
    if m.daily is None:
        # Missing daily turnover: sum available candles from prior 24h, a conservative lower bound.
        if sum(x.volume for x in rows if x.ts>=end-86400)<cfg['min_daily_usd']: return None
    if len(rows)<n+1: return None
    window=rows[-n-1:]; last=window[-1]
    if last.ts!=end-300 or any(b.ts-a.ts!=300 for a,b in zip(window,window[1:])): return None
    avg=statistics.mean(x.volume for x in window[:-1]); move=100*(last.c/last.o-1)
    if avg<=0 or last.volume<cfg['min_5m_usd']: return None
    rvol=last.volume/avg
    if rvol<min(cfg['awakening_rvol'],cfg['momentum_rvol'],cfg['early_buy_rvol']): return None
    # Count recent closed candles above twice the same prior baseline (diagnostic only).
    persistent=sum(x.volume>=2*avg for x in window[-3:])
    p=100*last.buy/last.volume if last.buy is not None and 0<=last.buy<=last.volume and last.volume else None
    return Signal(m.source,m.symbol,m.asset,last.ts,last.c,move,last.volume,rvol,f'{n*5}m previous',p,'taker notional / entire candle' if p is not None else 'N/A',persistent=persistent)

def oi_change(old,new,stamp,now):
    if not old or new is None or new<=0 or stamp is None or not 0<=now-stamp<=180: return None,None
    try: t,v=old; elapsed=(stamp-t)/60
    except (TypeError,ValueError): return None,None
    if not 3<=elapsed<=20 or not v>0: return None,None
    return 100*(new/v-1),elapsed

def sample_pressure(trades,start,volume):
    # Use only trades in the exact closed candle. Require sample notional coverage >=90%.
    valid=[(side,v) for ts,side,v in trades if start<=ts<start+300 and side in ('buy','sell') and v>0]
    total=sum(v for _,v in valid)
    if volume<=0 or not .90<=total/volume<=1.10: return None
    return 100*sum(v for side,v in valid if side=='buy')/total

def enrich(client,m,s,now,old):
    oi,mins=oi_change(old,m.oi,m.oi_ts,max(now,m.oi_ts or now))
    p=s.pressure; label=s.pressure_label
    try:
        if m.source=='OKX' and m.multiplier>0:
            rr=client.get('/api/v5/market/trades',instId=m.symbol,limit=500)
            trades=[(int(x['ts'])/1000,x['side'],float(x['sz'])*float(x['px'])*m.multiplier) for x in rr]
            p=sample_pressure(trades,s.ts,s.volume)
        elif m.source=='GATE':
            rr=client.get('/futures/usdt/contract_stats',contract=m.symbol,interval='5m',limit=5)
            rr=sorted(rr,key=lambda x:int(x['time'])); target=s.ts+300
            # Stats are point-in-time; label the reported taker ratio separately.
            match=[x for x in rr if int(x['time'])==target]
            if match:
                x=match[-1]; prev=next((y for y in rr if int(y['time'])==target-300),None)
                a=num(x.get('open_interest')); b=num(prev.get('open_interest')) if prev else None
                if a is not None and b and b>0: oi,mins=100*(a/b-1),5
                # Different Gate docs describe ratio inconsistently; do not infer notional share.
            # Calculate trade notional from actual contract multiplier and signed executed size.
            meta=client.get('/futures/usdt/contracts/'+urllib.parse.quote(m.symbol,safe=''))
            mult=num(meta.get('quanto_multiplier'))
            if mult and mult>0 and meta.get('type')=='direct':
                rr=client.get('/futures/usdt/trades',contract=m.symbol,limit=1000)
                trades=[(float(x['create_time']), 'buy' if float(x['size'])>0 else 'sell',abs(float(x['size']))*float(x['price'])*mult) for x in rr]
                p=sample_pressure(trades,s.ts,s.volume)
        elif m.source=='KUCOIN':
            rr=client.get('/api/v1/trade/history',symbol=m.symbol)
            trades=[(int(x['ts'])/1e9,x['side'],abs(float(x['size']))*float(x['price'])*m.multiplier) for x in rr]
            p=sample_pressure(trades,s.ts,s.volume)
        if m.source!='BINANCE_SPOT' and p is not None: label='taker notional (sample ≥90% candle)'
    except (APIError,KeyError,ValueError,TypeError,ArithmeticError) as e:
        print(f'{m.source} {m.symbol} enrichment partial: {type(e).__name__}',flush=True)
    return replace(s,pressure=p,pressure_label=label,oi=oi,oi_minutes=mins)

def classify(s,cfg):
    move=abs(s.move); p=s.pressure; oi=s.oi
    direction='BUY' if p is not None and p>=55 else 'SELL' if p is not None and p<=45 else 'BALANCED' if p is not None else ('UP (price)' if s.move>0 else 'DOWN (price)' if s.move<0 else 'UNKNOWN')
    kinds=[]
    if s.rvol>=cfg['awakening_rvol'] and move<=cfg['awakening_max_move']: kinds.append('AWAKENING')
    if s.rvol>=cfg['momentum_rvol'] and move>=cfg['momentum_min_move']: kinds.append('MOMENTUM')
    if s.source not in ('DEX','BINANCE_SPOT') and s.rvol>=cfg['early_buy_rvol'] and 0<=s.move<=cfg['early_buy_max_move'] and p is not None and p>=cfg['early_buy_min_pressure'] and oi is not None and oi>=cfg['early_buy_min_oi']: kinds.append('EARLY BUY WATCH')
    result=[]
    for kind in kinds:
        # 0–10, separate from confidence or price prediction.
        score=2+ (3 if s.rvol>=10 else 2 if s.rvol>=5 else 1)
        score+=2 if s.volume>=500000 else 1 if s.volume>=200000 else 0
        score+=1 if p is not None and abs(p-50)>=5 else 0
        score+=1 if oi is not None and oi>=1 else 0
        score+=1 if (move<=1 if kind!='MOMENTUM' else move>=3) else 0
        tier='HOT' if s.volume>=500000 and score>=7 else 'STRONG' if s.volume>=200000 and score>=5 else 'NORMAL'
        result.append(replace(s,kind=kind,direction=direction,score=min(10,score),tier=tier))
    return result

def scan_cex(src,cfg,state,now):
    client=Client(src,cfg['request_rates'][src],cfg['source_budget_seconds'])
    rows=markets(client,now); excluded=set(cfg['excluded_assets'])
    eligible=sorted((m for m in rows if m.asset and m.asset not in excluded and (m.daily is None or m.daily>=cfg['min_daily_usd'])),key=lambda m:m.symbol)
    # Start after last attempted index if budget/cap was reached; avoid starving small coins.
    prev=state.get('cursor',{}).get(src,0); start=int(prev)%max(1,len(eligible))
    ordered=eligible[start:]+eligible[:start]; cap=cfg['max_pairs_per_source']
    if cap>0: ordered=ordered[:cap]
    snapshots={m.symbol:[m.oi_ts,m.oi] for m in rows if m.oi is not None and m.oi>0 and m.oi_ts is not None and 0<=time.time()-m.oi_ts<=180}
    old=state.get('oi',{}).get(src,{})
    output=[]; errors=0; attempted=0; analyzed=0
    def one(m):
        bars=candles(client,m,now,cfg['baseline_candles'])
        if len(bars)<cfg['baseline_candles']+1 or bars[-1].ts!=int(now//300)*300-300 or any(b.ts-a.ts!=300 for a,b in zip(bars[-cfg['baseline_candles']-1:],bars[-cfg['baseline_candles']:])): raise APIError('missing/latest candle unavailable')
        s=candle_signal(m,bars,now,cfg)
        return s
    # Bounded batches: finish full candle scan before any optional enrichment.
    pre=[]
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for i in range(0,len(ordered),16):
            if client.blocked or time.monotonic()>=client.deadline-18: break
            batch=ordered[i:i+16]; futures=[pool.submit(one,m) for m in batch]
            for m,f in zip(batch,futures):
                attempted+=1
                try:
                    signal=f.result(); analyzed+=1
                    if signal and (abs(signal.move)<=cfg['awakening_max_move'] or abs(signal.move)>=cfg['momentum_min_move']): pre.append((m,signal))
                except Exception as e:
                    errors+=1
                    if errors<=5: print(f'{src} {m.symbol}: {type(e).__name__} {str(e)[:120]}',flush=True)
    for m,s in sorted(pre,key=lambda z:(z[1].volume,z[1].rvol),reverse=True):
        if src=='KUCOIN' and m.oi is None and not client.blocked and time.monotonic()<client.deadline-15:
            try:
                detail=client.get('/api/v1/contracts/'+urllib.parse.quote(m.symbol,safe=''))
                m.oi=num(detail.get('openInterest')); m.oi_ts=time.time()
                if m.oi is not None and m.oi>0: snapshots[m.symbol]=[m.oi_ts,m.oi]
            except Exception as e: print(f'KUCOIN {m.symbol} OI detail: {type(e).__name__}',flush=True)
        if src=='BINANCE_SPOT': enriched=s
        elif client.blocked or time.monotonic()>=client.deadline-10:
            oi,mins=oi_change(old.get(m.symbol),m.oi,m.oi_ts,max(now,m.oi_ts or now)); enriched=replace(s,oi=oi,oi_minutes=mins)
        else: enriched=enrich(client,m,s,now,old.get(m.symbol))
        output.extend(classify(enriched,cfg))
    print(f'{src}: listed={len(rows)} eligible={len(eligible)} attempted={attempted} analyzed={analyzed} errors={errors} unattempted={len(eligible)-attempted} candidates={len(output)} OI={len(snapshots)}',flush=True)
    return output,snapshots,(start+attempted)%max(1,len(eligible)) if attempted<len(eligible) else 0,analyzed

def dex_signal(x,now,cfg):
    chain=x.get('chainId',''); address=x.get('pairAddress',''); base=x.get('baseToken') or {}; token=base.get('address',''); asset=base.get('symbol','')
    volume=x.get('volume') or {}; changes=x.get('priceChange') or {}; txn=(x.get('txns') or {}).get('m5') or {}
    v=num(volume.get('m5')); hour=num(volume.get('h1')); daily=num(volume.get('h24')); liq=num((x.get('liquidity') or {}).get('usd')); p=num(x.get('priceUsd')); move=num(changes.get('m5')); created=num(x.get('pairCreatedAt'))
    buy=num(txn.get('buys')); sell=num(txn.get('sells'))
    if not all((chain,address,token,asset,p,created)) or asset in cfg['excluded_assets'] or chain not in cfg['dex_chains']: return None
    if None in (v,hour,daily,liq,move,buy,sell) or v<cfg['min_5m_usd'] or daily<cfg['min_daily_usd'] or liq<cfg['dex_min_liquidity'] or buy<0 or sell<0 or buy+sell<cfg['dex_min_5m_trades'] or now-created/1000<cfg['dex_min_age_minutes']*60: return None
    # h1 includes m5; compare m5 to mean of preceding eleven 5m-equivalents.
    avg=(hour-v)/11
    if avg<=0: return None
    ratio=v/avg
    pressure=100*buy/(buy+sell)
    # Names are untrusted; URL is constructed from actual chain/pair IDs.
    url='https://dexscreener.com/'+urllib.parse.quote(chain,safe='')+'/'+urllib.parse.quote(address,safe='')
    return Signal('DEX',chain+':'+address,asset,int(now//300)*300,p,move,v,ratio,'previous 55m (rolling)',pressure,'transaction COUNT, not notional',liquidity=liq,link=url)

def scan_dex(cfg,state,now):
    c=Client('DEX',cfg['request_rates']['DEX'],cfg['source_budget_seconds']); tokens={}
    for path in ('/token-profiles/latest/v1','/token-boosts/latest/v1','/token-boosts/top/v1'):
        try:
            rows=c.get(path); rows=rows if isinstance(rows,list) else [rows]
            for x in rows:
                chain=x.get('chainId'); addr=x.get('tokenAddress')
                if chain in cfg['dex_chains'] and addr: tokens[(chain,addr)]=now
        except Exception as e: print(f'DEX discovery: {type(e).__name__} {str(e)[:100]}',flush=True)
    if cfg.get('dex_gecko_discovery',True):
        g=Client('GECKO',1,cfg['source_budget_seconds'])
        mapping={'eth':'ethereum','bsc':'bsc','solana':'solana','base':'base','arbitrum':'arbitrum','polygon_pos':'polygon','avax':'avalanche'}
        try:
            result=g.get('/networks/trending_pools',duration='5m')
            for pool in result.get('data',[]):
                ident=pool.get('relationships',{}).get('base_token',{}).get('data',{}).get('id','')
                network,_,addr=ident.partition('_'); chain=mapping.get(network)
                if chain in cfg['dex_chains'] and addr: tokens[(chain,addr)]=now+0.001
        except Exception as e: print(f'Gecko discovery: {type(e).__name__} {str(e)[:100]}',flush=True)
    # Revisit recently discovered addresses even after they leave the discovery feed.
    for key,t in state.get('dex_tokens',{}).items():
        if ':' in key and now-86400<=t<=now+1:
            chain,addr=key.split(':',1)
            if chain in cfg['dex_chains']: tokens.setdefault((chain,addr),t)
    chosen=sorted(tokens,key=lambda k:(-tokens[k],k))[:cfg['dex_max_tokens']]
    grouped={}
    for chain,addr in chosen: grouped.setdefault(chain,[]).append(addr)
    best={}; errors=0; successful=0
    for chain,addresses in grouped.items():
        for i in range(0,len(addresses),30):
            if c.blocked or time.monotonic()>=c.deadline-10: break
            chunk=addresses[i:i+30]
            try:
                pairs=seq(c.get('/tokens/v1/'+urllib.parse.quote(chain,safe='')+'/'+','.join(urllib.parse.quote(a,safe='') for a in chunk))); successful+=1
                for x in pairs:
                    token=(x.get('baseToken') or {}).get('address',''); key=(chain,token if chain=='solana' else token.lower())
                    allowed=chunk if chain=='solana' else [a.lower() for a in chunk]
                    if x.get('chainId')!=chain or key[1] not in allowed: continue
                    liq=num((x.get('liquidity') or {}).get('usd'),0)
                    if key not in best or liq>num((best[key].get('liquidity') or {}).get('usd'),0): best[key]=x
            except Exception as e: errors+=1; print(f'DEX pairs: {type(e).__name__} {str(e)[:100]}',flush=True)
    out=[]
    for x in best.values():
        s=dex_signal(x,time.time(),cfg)
        if s: out.extend(classify(s,cfg))
    print(f'DEX: tokens={len(chosen)} pools={len(best)} batches_ok={successful} errors={errors} candidates={len(out)}',flush=True)
    return out,{f'{chain}:{addr}':tokens[(chain,addr)] for chain,addr in chosen},successful

def combine(items):
    # CEX ticker match is a heuristic; require same price direction and similar quote
    # price. Never merge DEX token addresses with CEX tickers.
    groups={}
    for s in items:
        asset_key=s.symbol if s.source=='DEX' else s.asset
        sign='UP' if s.move>0 else 'DOWN' if s.move<0 else 'FLAT'
        key=(('DEX' if s.source=='DEX' else 'CEX'),asset_key,s.kind,sign,s.ts)
        clusters=groups.setdefault(key,[])
        target=next((part for part in clusters if all(abs(s.price/x.price-1)<=.05 and not ({s.direction,x.direction}=={'BUY','SELL'}) for x in part)),None)
        if target is None: clusters.append([s])
        else: target.append(s)
    out=[]
    for clusters in groups.values():
        for parts in clusters:
            best=max(parts,key=lambda x:(x.score,x.volume,x.rvol))
            venues=tuple(sorted({x.source for x in parts}))
            score=min(10,best.score+(1 if len(venues)>1 else 0))
            tier='HOT' if best.volume>=500000 and score>=7 else 'STRONG' if best.volume>=200000 and score>=5 else 'NORMAL'
            out.append(replace(best,confirms=venues,score=score,tier=tier))
    return sorted(out,key=lambda x:(x.score,x.volume,x.rvol),reverse=True)

def alert_key(s):
    return '|'.join(('DEX' if s.source=='DEX' else 'CEX',s.symbol if s.source=='DEX' else s.asset,s.kind,'UP' if s.move>0 else 'DOWN' if s.move<0 else 'FLAT',s.direction))

def eligible_alert(s,history,now,cfg):
    old=history.get(alert_key(s),{})
    return old.get('candle')!=s.ts and now-old.get('sent',0)>=cfg['cooldown_minutes'][s.kind]*60

def money(v): return f'${v/1e6:.2f}M' if v>=1e6 else f'${v/1000:.1f}K'
def message(s):
    icon={'AWAKENING':'🦅','MOMENTUM':'⚡','EARLY BUY WATCH':'🎯'}[s.kind]
    tier={'NORMAL':'🟡','STRONG':'🟢','HOT':'🔥'}[s.tier]
    oi='N/A' if s.oi is None else f'{s.oi:+.2f}% ({s.oi_minutes:.1f}m)'
    pressure='N/A' if s.pressure is None else f'Buy {s.pressure:.0f}% / Sell {100-s.pressure:.0f}%\n({s.pressure_label})'
    stamp=dt.datetime.fromtimestamp(s.ts+(0 if s.source=='DEX' else 300),dt.timezone.utc).strftime('%H:%M UTC')
    t=f'ALTCOIN MARKET WATCHER V5\n{icon} {s.kind} | {tier} {s.tier}\n\n#{s.asset[:35]}/USDT' if s.source!='DEX' else f'ALTCOIN MARKET WATCHER V5\n{icon} DEX {s.kind} | {tier} {s.tier}\n\n{s.asset[:35]} | {s.symbol.split(":",1)[0]}'
    t+=f'\nMarket: {s.source}\nDirection: {s.direction}\n──────────────────\n💰 Price: {s.price:.9g} (5m: {s.move:+.2f}%)\n📊 Vol 5m: {money(s.volume)} ({s.rvol:.1f}x)\nBaseline: {s.baseline}\n⚖️ {pressure}\n🧲 OI contracts: {oi}\n⭐ Activity: {s.score}/10 {"🟩"*s.score}{"⬜"*(10-s.score)}\n'
    t+=f'Extra volume vs baseline: {money(s.volume-s.volume/s.rvol)}\n'
    if s.source!='DEX': t+=f'High-volume candles (last 3, ≥2x baseline): {s.persistent}/3\n'
    if s.liquidity is not None: t+=f'Liquidity: {money(s.liquidity)}\n'
    if len(s.confirms)>1: t+='Same ticker / price direction: '+', '.join(s.confirms)+'\n'
    t+=f'Data: {stamp}\n'
    return t+(s.link or f'https://www.tradingview.com/chart/?symbol={urllib.parse.quote({"OKX":"OKX:","GATE":"GATEIO:","KUCOIN":"KUCOIN:","BINANCE_SPOT":"BINANCE:"}[s.source]+s.symbol.replace("-USDT-SWAP","USDT.P").replace("_USDT","USDT.P").replace("USDTM","USDT.P"),safe="")}')

def telegram(text):
    token=os.environ.get('TELEGRAM_BOT_TOKEN'); chat=os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat: raise RuntimeError('Telegram secrets missing')
    data=urllib.parse.urlencode({'chat_id':chat,'text':text,'disable_web_page_preview':'true'}).encode()
    try:
        req=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',data=data)
        with urllib.request.urlopen(req,timeout=18) as r: response=json.load(r)
        if response.get('ok') is not True: raise RuntimeError('Telegram rejected message')
    except urllib.error.HTTPError as e: raise RuntimeError(f'Telegram HTTP {e.code}') from None
    except (OSError,ValueError): raise RuntimeError('Telegram network or response error') from None

def save_state(state):
    tmp=ROOT/'state.tmp'; tmp.write_text(json.dumps(state,ensure_ascii=False,separators=(',',':'))+'\n',encoding='utf-8'); tmp.replace(ROOT/'state.json')

def main():
    cfg=json.loads((ROOT/'config.json').read_text()); mode=os.getenv('TEST_MODE','false').lower()
    if mode=='true': telegram('✅ Altcoin Market Watcher V5: Telegram test successful.'); return
    now=time.time(); state=json.loads((ROOT/'state.json').read_text()); state=state if state.get('version')==5 else {'version':5}
    all_signals=[]; stats={}; snapshots=state.setdefault('oi',{}); cursors=state.setdefault('cursor',{}); sources=[s for s in URL if s!='GECKO' and cfg['sources'].get(s)]
    with cf.ThreadPoolExecutor(max_workers=5) as pool:
        tasks={pool.submit(scan_dex,cfg,state,now) if src=='DEX' else pool.submit(scan_cex,src,cfg,state,now):src for src in sources}
        for f in cf.as_completed(tasks):
            src=tasks[f]
            try:
                if src=='DEX': signals,tokens,count=f.result(); state['dex_tokens']=tokens
                else:
                    signals,oi,cursor,count=f.result(); cursors[src]=cursor
                    if oi:
                        previous=snapshots.get(src,{})
                        snapshots[src]={**{k:v for k,v in previous.items() if now-v[0]<1200},**oi}
                all_signals.extend(signals); stats[src]=count
            except Exception as e:
                stats[src]=0; print(f'{src} FAILED: {type(e).__name__} {str(e)[:160]}',flush=True)
    if not any(stats.values()): raise RuntimeError('No enabled source produced usable data')
    history=state.setdefault('alerts',{}); state['alerts']=history={k:v for k,v in history.items() if now-v.get('sent',0)<86400*2}
    candidates=combine(all_signals); sent=0
    if mode!='dry': save_state(state)
    shown=0
    for s in candidates:
        if sent>=cfg['max_alerts']: break
        if not eligible_alert(s,history,now,cfg): continue
        if mode=='dry':
            print(message(s),flush=True); shown+=1
            if shown>=cfg['max_alerts']: break
            continue
        telegram(message(s)); history[alert_key(s)]={'sent':time.time(),'candle':s.ts}; save_state(state); sent+=1
    print(f'V5 SUMMARY sources={stats} raw={len(all_signals)} combined={len(candidates)} sent={sent}',flush=True)

if __name__=='__main__': main()
