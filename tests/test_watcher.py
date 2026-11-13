import copy
import json
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from watcher import calc_metrics, classify, get_whale_events, market_side, score_for

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())


def candles(last_volume=500_000, last_price=102, buys=300_000, n=40):
    result=[]
    for i in range(n):
        close=100.0
        op=100.0
        volume=50_000
        buy=25_000
        if i == n-1:
            close=last_price; volume=last_volume; buy=buys
        result.append([i*300000, str(op), '110', '90', str(close), '1', (i+1)*300000-1,
                       str(volume), 10, '1', str(buy), '0'])
    return result


class WatcherTests(unittest.TestCase):
    def test_metrics(self):
        m=calc_metrics(candles(),30,'TESTUSDT',10_000_000)
        self.assertAlmostEqual(m['rvol'],10)
        self.assertAlmostEqual(m['move'],2)
        self.assertAlmostEqual(m['buy_pct'],60)
    def test_early_buy(self):
        m=calc_metrics(candles(last_price=101),30,'TESTUSDT',10_000_000)
        m['oi_pct']=2
        kinds=[e[0] for e in classify(m,CONFIG)]
        self.assertIn('EARLY_BUY_WATCH',kinds)
        self.assertIn('AWAKENING',kinds)
    def test_momentum(self):
        m=calc_metrics(candles(last_price=103),30,'TESTUSDT',10_000_000)
        m['oi_pct']=0
        self.assertIn('MOMENTUM',[e[0] for e in classify(m,CONFIG)])
    def test_no_oi_no_awakening(self):
        m=calc_metrics(candles(last_price=101),30,'TESTUSDT',10_000_000)
        self.assertNotIn('AWAKENING',[e[0] for e in classify(m,CONFIG)])
    def test_side(self):
        self.assertIn('BUY',market_side({'buy_pct':70}))
        self.assertIn('SELL',market_side({'buy_pct':20}))
    def test_whale_agg_side_and_threshold(self):
        from unittest.mock import patch
        sample=[{'a':10,'T':1_000_000,'p':'10','q':'120000','m':False},
                {'a':11,'T':1_000_000,'p':'10','q':'100','m':True}]
        with patch('watcher.fetch',return_value=sample):
            trades=get_whale_events('TESTUSDT',CONFIG,now_ms=1_050_000)
        self.assertEqual(len(trades),1)
        self.assertEqual(trades[0]['side'],'BUY')

if __name__ == '__main__': unittest.main()
