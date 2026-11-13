import unittest
from watcher import volume_metrics, oi_change, trade_notional, sample_pressure, choose_alerts

class WatcherTest(unittest.TestCase):
    def test_candles(self):
        a=[]
        for i in range(34):
            a.append([str(i*300000),'1','2','0.9',str(1.0 + i*.001),'1','1',str(1000 if i<33 else 5000),'1'])
        r=volume_metrics(list(reversed(a)))
        self.assertAlmostEqual(r['rvol'], 5.0)
        self.assertAlmostEqual(r['volume'],5000)
    def test_unclosed_ignored(self):
        a=[[str(i*300000),'1','2','0.9','1','1','1','1000','1'] for i in range(35)]
        a[-1][7]='999999';a[-1][8]='0'
        self.assertAlmostEqual(volume_metrics(list(reversed(a)))['rvol'],1)
    def test_oi_window(self):
        self.assertAlmostEqual(oi_change({'oi':100,'observed_at':1000},110,301000)['percent'],10)
        self.assertIsNone(oi_change({'oi':100,'observed_at':1000},110,10000000))
    def test_contract_notional(self):
        i={'instId':'BTC-USDT-SWAP','ctValCcy':'BTC','ctType':'linear','ctVal':'0.01'}
        t={'px':'70000','sz':'2','side':'buy'}
        self.assertEqual(trade_notional(t,i),1400)
        self.assertEqual(sample_pressure([t,{'px':'70000','sz':'2','side':'sell'}],i),50)
    def test_invalid_quote(self):
        self.assertIsNone(trade_notional({'px':'100','sz':'2'}, {'instId':'BTC-USDT-SWAP','ctValCcy':'USD','ctType':'linear','ctVal':'1'}))
    def test_alerts(self):
        cfg={'awakening':{'enabled':True,'min_rvol':5,'max_abs_move':2.5,'min_oi_percent':0.5}, 'momentum':{'enabled':True,'min_rvol':3,'min_abs_move':2},'early_buy':{'enabled':True,'min_rvol':4,'max_move':2,'min_buy_pressure':57}}
        row={'change':0.8,'rvol':7,'pressure':62}
        kinds=[x[0] for x in choose_alerts(row,{'percent':2,'minutes':5},cfg)]
        self.assertEqual(kinds,['AWAKENING','EARLY BUY WATCH'])

if __name__=='__main__':unittest.main()
