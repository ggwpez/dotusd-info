import unittest
import tempfile
import json
from pathlib import Path
import import_quote_batches as importer
from unittest.mock import Mock, patch
import backfill_quotes as quotes

DOT = {'parents':1,'interior':'Here'}
USD = {'parents':0,'interior':{'X2':[{'PalletInstance':50},{'GeneralIndex':7873}]}}


def event(path, kind='SwapExecuted'):
    return {'event':dict(module_id='AssetConversion',event_id=kind,attributes={'path':path})}


class QuoteReconstructionTests(unittest.TestCase):
    def test_trades_match_dex_orientation_dust_filter_and_event_order(self):
        events = [event([(DOT,10*10**10),(USD,11_200_000)]),
                  event([(USD,5_400_000),(DOT,5*10**10)],'SwapCreditExecuted'),
                  event([(DOT,10_000),(USD,9_999)])]
        self.assertEqual([row['quote'] for row in quotes.qualifying_trades(events)], ['1.12','1.08'])
        self.assertEqual(quotes.qualifying_trades(events)[-1]['event'],1)
        self.assertEqual(quotes.qualifying_trades([event([(DOT,10**10),(USD,10**6)],'LiquidityAdded')]),[])

    def test_timestamp_search_uses_last_block_at_or_before_reference(self):
        worker = object.__new__(quotes.Reconstruction)
        worker.head = 20
        worker.stamps = {n:n*2000 for n in range(10,21)}
        worker.load_stamps = Mock()
        with patch.object(quotes,'START',10):
            self.assertEqual(worker.locate([20000,21000,39999,40000]),[10,10,19,20])

    def test_latest_qualifying_trade_and_no_trade_reserve_fallback(self):
        worker = object.__new__(quotes.Reconstruction)
        worker.reserve_quote = Mock(return_value=('1.2',None,None))
        worker.read_blocks = Mock(return_value=[
            dict(number=20,timestamp=40000,trades={'7873':[]}),
            dict(number=19,timestamp=38000,trades={'7873':[dict(quote='1.1'),dict(quote='1.15')]})])
        with patch.object(quotes,'START',10):
            self.assertEqual(worker.quote(20,40000),('1.15',19,38000))
            worker.read_blocks.return_value = [dict(number=20,timestamp=40000-86400001,trades={'7873':[dict(quote='1')]})]
            self.assertEqual(worker.quote(20,40000),('1.2',None,None))
        worker.reserve_quote.assert_called_once_with(20,7873)

    def test_both_legs_are_independent_and_use_actual_chain_time(self):
        worker = Mock()
        worker.stamps = {20: 40000}
        records = {
            20: dict(number=20,timestamp=40000,trades={'7873':[], '1984':[]}),
            21: dict(number=21,timestamp=42000,trades={'7873':[dict(quote='2.5')], '1984':[]}),
            22: dict(number=22,timestamp=44000,trades={'7873':[], '1984':[dict(quote='3')]})}
        worker.read_blocks.side_effect = lambda numbers: [records[n] for n in numbers]
        worker.quote.side_effect = lambda n,t,a: ('2',19,38000)
        with tempfile.TemporaryDirectory() as directory:
            quotes.reconstruct_targets(worker, [[39,9,20],[41,9,21],[43,9,22]], Path(directory))
            payload = json.loads(next(Path(directory).glob('*.json')).read_text())
        self.assertEqual(payload['samples'], [[39,1.,None],[41,.8,None],[43,1.2,None]])
        self.assertEqual(payload['evidence'][1]['chainTimestamp'],42000)
        self.assertEqual(payload['evidence'][1]['usdtTradeTimestamp'],38000)
        self.assertEqual(importer.validate(payload),3)
        worker.reserve_quote.assert_not_called()
        payload['samples'] *= 400
        with self.assertRaisesRegex(ValueError, 'Invalid import batch'):
            importer.validate(payload)

    def test_usdt_multi_hop_trade_has_correct_orientation(self):
        usdt = {'parents':0,'interior':{'X2':[{'PalletInstance':50},{'GeneralIndex':1984}]}}
        events = [event([(USD, 2_000_000),(DOT,10**10),(usdt,2_010_000)])]
        self.assertEqual(quotes.qualifying_trades(events,7873)[0]['quote'],'2')
        self.assertEqual(quotes.qualifying_trades(events,1984)[0]['quote'],'2.01')
