#!/usr/bin/env python3
"""OKX public-USDT-swap watcher. Python 3.12 standard library only."""
import json, math, os, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

BASE = 'https://www.okx.com'
STATE_FILE = Path('state.json')
CONFIG_FILE = Path('config.json')


def number(value, default=0.0):
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError):
        return default


def api(path, params=None):
    url = BASE + path + ('?' + urllib.parse.urlencode(params) if params else '')
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'AltcoinWatcherOKX/4.0', 'Accept':'application/json'})
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.load(response)
            if str(result.get('code')) != '0':
                raise RuntimeError(f'OKX API {path} code={result.get("code")} msg={result.get("msg")}')
            return result.get('data', [])
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            if attempt == 2:
                raise RuntimeError(f'Network failed {path}: {e}') from e
            time.sleep(attempt + 1)


def telegram(msg):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in Actions Secrets')
    body = urllib.parse.urlencode({'chat_id':chat, 'text':msg, 'disable_web_page_preview':'true'}).encode('utf-8')
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', data=body)
    try:
        with urllib.request.urlopen(req, timeout=20) as result:
            answer = json.load(result)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Telegram HTTP {e.code} (check chat ID / bot permissions)') from None
    if not answer.get('ok'):
        raise RuntimeError('Telegram returned not ok')


def read_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def persist(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def volume_metrics(raw, previous=30):
    # OKX: [ts,open,high,low,close,vol,volCcy,volCcyQuote,confirm]; newest first
    # Only completed candles, sort chronologically.
    completed = sorted((r for r in raw if len(r) >= 9 and r[8] == '1'), key=lambda x:int(x[0]))
    if len(completed) < previous + 2:
        return None
    current = completed[-1]
    baseline = [number(x[7]) for x in completed[-1-previous:-1]]
    avg = sum(baseline) / len(baseline)
    if avg <= 0:
        return None
    close = number(current[4])
    earlier = number(completed[-2][4])
    if earlier <= 0 or close <= 0:
        return None
    change = (close / earlier - 1) * 100
    high = max(number(c[2]) for c in completed[-21:-1])
    return {'price':close, 'change':change, 'volume':number(current[7]),
            'rvol':number(current[7])/avg, 'breakout':close>high,
            'candle_ts':int(current[0]), 'candle_confirmed':True}


def oi_change(prior, oi, now_ms):
    # OI snapshot comparisons are over actual elapsed time, not precisely 5m.
    if not isinstance(prior, dict):
        return None
    before = number(prior.get('oi'))
    delta_mins = (now_ms - int(prior.get('observed_at', 0)))/60000
    if before <= 0 or not (3 <= delta_mins <= 13):
        return None
    return {'percent':(oi / before-1)*100, 'minutes':delta_mins}


def trade_notional(trade, instrument):
    # For linear USDT swaps with ctValCcy equal to base, size means contracts.
    # E.g. BTC-USDT-SWAP ctVal=0.01 BTC; 2 contracts at $70k -> $1,400.
    base = instrument['instId'].split('-')[0]
    if instrument.get('ctValCcy') != base or instrument.get('ctType') != 'linear':
        return None
    val = number(instrument.get('ctVal'))
    amount = number(trade.get('sz'))
    price = number(trade.get('px'))
    if min(val, amount, price) <= 0:
        return None
    return val * amount * price


def sample_pressure(trades, instrument):
    bought = sold = 0.0
    for t in trades:
        size = trade_notional(t, instrument)
        if size is None:
            return None
        if t.get('side') == 'buy': bought += size
        elif t.get('side') == 'sell': sold += size
    if bought + sold <= 0:
        return None
    return round(100*bought/(bought+sold), 1)


def choose_alerts(row, oi, cfg):
    change, rvol = row['change'], row['rvol']
    pressure = row.get('pressure')
    result = []
    direction = 'BUY' if change >= 0 else 'SELL'
    oi_up = oi is not None and oi['percent'] >= cfg['awakening']['min_oi_percent']
    if cfg['awakening']['enabled'] and rvol >= cfg['awakening']['min_rvol'] and abs(change) <= cfg['awakening']['max_abs_move'] and oi_up:
        score = min(10, 6 + int(rvol>=10) + int(rvol>=20) + int(oi['percent']>=3) + int(pressure is not None and (pressure >= 60 or pressure <= 40)))
        result.append(('AWAKENING', direction, score))
    if cfg['momentum']['enabled'] and rvol >= cfg['momentum']['min_rvol'] and abs(change) >= cfg['momentum']['min_abs_move']:
        score = min(10, 6 + int(rvol>=6) + int(rvol>=12) + int(abs(change)>=4) + int(pressure is not None and ((direction=='BUY' and pressure>=60) or (direction=='SELL' and pressure<=40))))
        result.append(('MOMENTUM', direction, score))
    if (cfg['early_buy']['enabled'] and rvol >= cfg['early_buy']['min_rvol'] and
        0 < change <= cfg['early_buy']['max_move'] and pressure is not None and
        pressure >= cfg['early_buy']['min_buy_pressure'] and oi_up):
        score = min(10, 7 + int(rvol>=8) + int(pressure>=65) + int(oi['percent']>=3))
        result.append(('EARLY BUY WATCH', 'BUY', score))
    return result


def format_alert(kind, direction, score, inst, row, oi):
    buy = row.get('pressure')
    pressure = f'Buy {buy:.1f}% / Sell {100-buy:.1f}% (recent trades sample)' if buy is not None else 'N/A (insufficient sample)'
    oistr = 'N/A (need two separate runs)' if oi is None else f'{oi["percent"]:+.2f}% over {oi["minutes"]:.1f} min'
    icon = {'AWAKENING':'🦅', 'MOMENTUM':'⚡', 'EARLY BUY WATCH':'🎯'}[kind]
    return (f'OKX ALTCOIN WATCHER V4\n{icon} {kind}\n#{inst}\n'
            f'Direction: {"🟢 BUY" if direction=="BUY" else "🔴 SELL"}\n'
            f'Price: {row["price"]:.8g} | 5m move: {row["change"]:+.2f}%\n'
            f'5m volume: ${row["volume"]:,.0f} | RVOL: {row["rvol"]:.1f}x (30 earlier 5m candles)\n'
            f'Buy/Sell pressure: {pressure}\nOI contracts: {oistr}\n'
            f'20-candle breakout: {"YES" if row["breakout"] else "NO"}\n'
            f'Activity score: {score}/10 (rule-based)\n'
            f'Candle UTC: {time.strftime("%Y-%m-%d %H:%M", time.gmtime(row["candle_ts"]/1000))}\n'
            f'https://www.okx.com/trade-swap/{inst.lower()}')


def format_whale(inst, side, usd, price, tid):
    return (f'OKX ALTCOIN WATCHER V4\n🐋 LARGE EXECUTED TRADE (sample)\n#{inst}\n'
            f'Side: {"🟢 BUY" if side=="buy" else "🔴 SELL"}\n'
            f'Price: ${price:,.8g}\nTrade notional: ${usd:,.0f}\nTrade ID: {tid}\n'
            f'Observation: recent OKX trades only; not the complete market/order book.\n'
            f'https://www.okx.com/trade-swap/{inst.lower()}')


def main():
    if os.getenv('TEST_MODE', 'false').strip().lower() == 'true':
        telegram('✅ OKX Altcoin Watcher V4: Telegram test successful.')
        print('Telegram test sent')
        return
    cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    state = read_state()
    now = int(time.time()*1000)
    # A single fatal response fails loudly; no silent success if market is unreachable.
    instrument_data = api('/api/v5/public/instruments', {'instType':'SWAP'})
    instruments = {x['instId']:x for x in instrument_data
                   if x.get('instId','').endswith('-USDT-SWAP')
                   and x.get('state') == 'live' and x.get('ctType') == 'linear'}
    tickers = api('/api/v5/market/tickers', {'instType':'SWAP'})
    eligible=[]
    for t in tickers:
        iid=t.get('instId')
        if iid not in instruments: continue
        # USD-ish 24h quote turnover from last price * base volume, if volCcy24h is base coin.
        last = number(t.get('last'))
        turnover = number(t.get('volCcy24h'))*last
        if turnover >= cfg['min_24h_turnover_usdt']:
            eligible.append((iid,turnover))
    eligible.sort(key=lambda x:x[1], reverse=True)
    selected=eligible[:cfg['max_pairs']]
    if not selected:
        raise RuntimeError('No eligible USDT swaps - review ticker fields/API response')
    # Bulk OI request: stores snapshots across runs, not a fictional historical 5m value.
    oi_data = api('/api/v5/public/open-interest', {'instType':'SWAP'})
    oi_map={x['instId']:number(x.get('oi')) for x in oi_data if x.get('instId')}
    print(f'OKX eligible={len(eligible)} scanning={len(selected)} OI_items={len(oi_map)}')
    prev_oi=state.get('oi', {})
    sent_log=state.get('sent', {})
    seen_trade=state.get('seen_trades', {})
    pending=[]
    scan_ok=0
    scan_errors=0
    for iid, turnover in selected:
        try:
            candles=api('/api/v5/market/candles', {'instId':iid,'bar':'5m','limit':'45'})
            row=volume_metrics(candles, previous=30)
            if row is None: continue
            scan_ok+=1
            current_oi=oi_map.get(iid, 0)
            delta=oi_change(prev_oi.get(iid),current_oi,now) if current_oi>0 else None
            # Query latest trades only when near a volume opportunity, reducing API calls.
            trades=[]
            if row['rvol'] >= cfg['trade_sampling_min_rvol'] or iid in [i for i,_ in selected[:cfg['whale']['scan_top_pairs']]]:
                trades=api('/api/v5/market/trades', {'instId':iid,'limit':'100'})
                # Pressure describes the recent trade SAMPLE, not all 5m trades.
                row['pressure']=sample_pressure(trades, instruments[iid])
            else:
                row['pressure']=None
            for kind, direction, score in choose_alerts(row,delta,cfg):
                minscore=cfg['min_score']
                if score < minscore: continue
                key=f'{iid}|{kind}|{direction}'
                last_sent=int(sent_log.get(key,0))
                cooldown=cfg['cooldown_minutes'][kind]*60*1000
                if now-last_sent>=cooldown:
                    pending.append({'priority':score, 'id':key, 'message':format_alert(kind,direction,score,iid,row,delta),'candle':row['candle_ts']})
            if cfg['whale']['enabled'] and trades and iid in [i for i,_ in selected[:cfg['whale']['scan_top_pairs']]]:
                for t in trades:
                    ts=int(t.get('ts',0))
                    tid=str(t.get('tradeId',''))
                    if not tid or not (0 <= now-ts <= cfg['whale']['max_trade_age_seconds']*1000): continue
                    key=f'{iid}|{tid}'
                    if key in seen_trade: continue
                    usd=trade_notional(t,instruments[iid])
                    if usd is None or usd < cfg['whale']['min_trade_usdt']: continue
                    pending.append({'priority':11, 'id':'WHALE|'+key,
                                    'message':format_whale(iid,t.get('side',''),usd,number(t.get('px')),tid),
                                    'trade_key':key,'candle':row['candle_ts']})
            time.sleep(.06)
        except Exception as exc:
            scan_errors+=1
            print(f'Skipped {iid}: {type(exc).__name__}: {str(exc)[:90]}')
    if scan_ok < max(3, int(len(selected)*.5)):
        raise RuntimeError(f'Only {scan_ok}/{len(selected)} pairs scanned successfully; refusing to overwrite state')
    # Send highest priority first, dedupe exact alert ID within same run.
    pending.sort(key=lambda x:(x['priority'],x['candle']),reverse=True)
    sent_count=0
    sent_in_run=set()
    for item in pending:
        if sent_count >= cfg['max_alerts_per_run']:break
        if item['id'] in sent_in_run:continue
        telegram(item['message'])
        sent_in_run.add(item['id'])
        if 'trade_key' in item:
            seen_trade[item['trade_key']]=now
        else:
            sent_log[item['id']]=now
        sent_count+=1
        # Persist after each message: prevents duplicates if a later send fails.
        state['sent']=sent_log
        state['seen_trades']=seen_trade
        persist(state)
        time.sleep(.15)
    # snapshots updated after all analysis; compare to next successful run.
    state['oi']={iid:{'oi':v,'observed_at':now} for iid,v in oi_map.items() if v>0}
    state['sent']={k:v for k,v in sent_log.items() if now-int(v)<7*86400*1000}
    state['seen_trades']={k:v for k,v in seen_trade.items() if now-int(v)<86400*1000}
    persist(state)
    print(f'Analyzed={scan_ok} errors={scan_errors} candidates={len(pending)} sent={sent_count} OI_snapshots={len(state["oi"])}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'FATAL: {type(e).__name__}: {e}', file=sys.stderr)
        sys.exit(1)
