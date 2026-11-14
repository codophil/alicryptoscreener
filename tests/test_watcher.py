import unittest,json,time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import watcher as w
C=json.loads((Path(__file__).parents[1]/'config.json').read_text())
NOW=1800000000
class Fake:
    def __init__(self,src,rows): self.source=src; self.rows=rows; self.calls=[]
    def get(self,path,**kw): self.calls.append((path,kw)); return self.rows[path] if isinstance(self.rows,dict) else self.rows
class Tests(unittest.TestCase):
    def m(self,src='OKX'): return w.Market(src,'ABC-USDT-SWAP','ABC',1e6,1)
    def bars(self): return [w.Candle(NOW-2100+i*300,100,100,10000) for i in range(6)]+[w.Candle(NOW-300,100,101,90000,54000)]
    def s(self): return w.candle_signal(self.m(),self.bars(),NOW,C)
    def test_volume_and_buy(self):
        s=self.s(); self.assertEqual(s.rvol,9); self.assertEqual(s.pressure,60); self.assertAlmostEqual(s.move,1)
    def test_gap(self): self.assertIsNone(w.candle_signal(self.m(),self.bars()[1:],NOW,C))
    def test_zero_baseline(self): self.assertIsNone(w.candle_signal(self.m(),[replace(x,volume=0) for x in self.bars()[:-1]]+self.bars()[-1:],NOW,C))
    def test_low_absolute_volume(self): self.assertIsNone(w.candle_signal(self.m(),[replace(x,volume=x.volume/10,buy=None) for x in self.bars()],NOW,C))
    def test_stale(self): self.assertIsNone(w.candle_signal(self.m(),self.bars(),NOW+300,C))
    def test_awakening_missing_oi(self): self.assertEqual([x.kind for x in w.classify(self.s(),C)],['AWAKENING'])
    def test_early_buy_requires_oi(self): self.assertIn('EARLY BUY WATCH',[x.kind for x in w.classify(replace(self.s(),oi=2,oi_minutes=5),C)])
    def test_down_momentum(self): self.assertEqual(w.classify(replace(self.s(),move=-4,pressure=30),C)[0].direction,'SELL')
    def test_oi(self): self.assertAlmostEqual(w.oi_change([NOW-300,100],104,NOW,NOW)[0],4); self.assertEqual(w.oi_change([NOW-3600,100],104,NOW,NOW),(None,None))
    def test_sample_coverage(self): self.assertIsNone(w.sample_pressure([(NOW-10,'buy',100)],NOW-300,1000)); self.assertEqual(w.sample_pressure([(NOW-10,'buy',600),(NOW-20,'sell',400),(NOW,'buy',1000)],NOW-300,1000),60)
    def test_okx_parser(self):
        f=Fake('OKX',[[str((NOW-300)*1000),'1','2','1','1.5','999','999','30000','1'],[str(NOW*1000),'1','2','1','1.5','999','999','99999','0']]); self.assertEqual(w.candles(f,self.m(),NOW,6)[0].volume,30000)
    def test_gate_parser(self):
        f=Fake('GATE',[{'t':NOW-300,'o':'1','c':'2','v':'999','sum':'12345'},{'t':NOW,'o':'1','c':'2','v':'9','sum':'4'}]); self.assertEqual(w.candles(f,self.m('GATE'),NOW,6)[0].volume,12345)
    def test_kucoin_parser(self):
        f=Fake('KUCOIN',[[int((NOW-300)*1000),1,2,1,2,300,1500]]); self.assertEqual(w.candles(f,self.m('KUCOIN'),NOW,6)[0].volume,1500); self.assertEqual(f.calls[0][1]['granularity'],300)
    def test_binance_parser(self):
        f=Fake('BINANCE_SPOT',[[int((NOW-300)*1000),1,2,1,2,100,0,1000,50,60,600,0]]); self.assertEqual(w.candles(f,self.m('BINANCE_SPOT'),NOW,6)[0].buy,600)
    def test_dex(self):
        row={'chainId':'solana','pairAddress':'pair','baseToken':{'address':'token','symbol':'ABC'},'priceUsd':'1','pairCreatedAt':(NOW-7200)*1000,'volume':{'m5':100000,'h1':210000,'h24':500000},'priceChange':{'m5':1},'txns':{'m5':{'buys':60,'sells':40}},'liquidity':{'usd':100000}}
        s=w.dex_signal(row,NOW,C); self.assertEqual(s.rvol,10); self.assertEqual(s.pressure,60); self.assertIsNone(s.oi); self.assertEqual(w.classify(s,C)[0].kind,'AWAKENING')
        row['liquidity']['usd']=1; self.assertIsNone(w.dex_signal(row,NOW,C))
    def test_merge_and_dex_separation(self):
        a=w.classify(self.s(),C)[0]; b=replace(a,source='GATE'); d=replace(a,source='DEX',symbol='solana:pair'); out=w.combine([a,b,d]); self.assertEqual(len(out),2); self.assertEqual(len(next(s for s in out if s.source!='DEX').confirms),2)
    def test_conflicting_pressure_not_merged(self):
        a=w.classify(self.s(),C)[0]; self.assertEqual(len(w.combine([a,replace(a,source='GATE',direction='SELL')])),2)
    def test_cooldown_independent(self):
        a=w.classify(self.s(),C)[0]; hist={w.alert_key(a):{'sent':NOW,'candle':a.ts}}; self.assertFalse(w.eligible_alert(a,hist,NOW+60,C)); self.assertTrue(w.eligible_alert(replace(a,kind='MOMENTUM'),hist,NOW+60,C))
    def test_message(self): self.assertIn('OI contracts: N/A',w.message(w.classify(self.s(),C)[0]))
    def test_daily_unknown(self): self.assertIsNone(w.candle_signal(replace(self.m(),daily=None),self.bars(),NOW,C))
    def test_mock_cex_scan(self):
        m=self.m('BINANCE_SPOT'); m.symbol='ABCUSDT'
        with patch('watcher.markets',return_value=[m]),patch('watcher.candles',return_value=self.bars()):
            result,oi,cursor,count=w.scan_cex('BINANCE_SPOT',C,{},NOW)
        self.assertEqual(count,1); self.assertEqual(result[0].kind,'AWAKENING')
    def test_mock_dex_scan(self):
        d={'chainId':'solana','tokenAddress':'addr'}
        with patch.object(w.Client,'get',side_effect=[[d],[d],[d],{'data':[]},[]]):
            result,tokens,count=w.scan_dex(C,{},NOW)
        self.assertEqual(count,1); self.assertIn('solana:addr',tokens); self.assertEqual(result,[])
if __name__=='__main__': unittest.main()
