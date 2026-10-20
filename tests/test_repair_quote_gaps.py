import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from repair_quote_gaps import missing_intervals, repair


class RepairQuoteGapsTests(unittest.TestCase):
    def test_only_missing_intervals_and_valid_ordered_history(self):
        self.assertEqual(missing_intervals([[1, 1, None], [121, 1, None],
                                            [242, 1, None]]), [(121, 242)])
        self.assertEqual(missing_intervals([]), [])
        for rows in ([[[1, 1, None], [1, 1, None]], [[2, 1, None], [1, 1, None]],
                      [[1, 1, 42]], [[float('nan'), 1, None]]]):
            with self.assertRaises(ValueError):
                missing_intervals(rows)

    def test_reconstructs_actual_block_times_without_overwriting_boundaries(self):
        worker = Mock()
        worker.locate.return_value = [10, 14]
        worker.stamps = {11: 104000}
        records = {n: dict(number=n, timestamp=t, trades={'7873': [], '1984': []})
                   for n, t in [(11, 104000), (12, 109000), (13, 109000), (14, 115000)]}
        records[12]['trades']['7873'] = [{'quote': '2.5'}]
        worker.read_blocks.side_effect = lambda ns: [records[n] for n in ns]
        worker.quote.return_value = ('2', 9, 99000)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.assertEqual(repair(worker, [(101, 115)], output), 2)
            payload = json.loads(next(output.glob('*.json')).read_text())
        self.assertEqual(payload['samples'], [[104, 1, 11], [109, .8, 13]])
        self.assertEqual([e['chainTimestamp'] for e in payload['evidence']], [104000, 109000])
        self.assertEqual([e['block'] for e in payload['evidence']], [11, 13])
        worker.locate.assert_called_once_with([101000, 115000])
        worker.reserve_quote.assert_not_called()
