import unittest
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
            dict(number=20,timestamp=40000,trades=[]),
            dict(number=19,timestamp=38000,trades=[dict(quote='1.1'),dict(quote='1.15')])])
        with patch.object(quotes,'START',10):
            self.assertEqual(worker.quote(20,40000),('1.15',19,38000))
            worker.read_blocks.return_value = [dict(number=20,timestamp=40000-86400001,trades=[dict(quote='1')])]
            self.assertEqual(worker.quote(20,40000),('1.2',None,None))
        worker.reserve_quote.assert_called_once_with(20)
