import json
from pathlib import Path
import random
import unittest

from js_runtime import evaluate
from test_server import page

ROOT = Path(__file__).resolve().parents[1]


class RangeTests(unittest.TestCase):
    def test_packed_samples_cache_and_drawing(self):
        rng = random.Random(42)
        rows, t, block, price = [], 1700000000, 100, 1.0
        for i in range(3000):
            t += rng.choice([2] * 30 + [4, 26])
            block += rng.choice([1] * 30 + [3])
            if rng.random() < .05:
                price += rng.choice([1e-12, -.001, .002])
            rows.append([t, price, block if rng.random() > .01 else None])
        fractional = [[1700000000 + i * .1, 1, i] for i in range(100)]
        fixtures = [[], [[100, 1, 4]], [[100, 1, None]], rows, fractional,
                    [[100 + i * 2, 1, 20 + i] for i in range(10000)]]
        source = (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += (ROOT / 'tests/ranges_test.js').read_text()
        for fixture in fixtures:
            source += f'\ncheckFixture({json.dumps(fixture)}, {json.dumps(list(page.pack_ranges(fixture)))});'
        source += '\ncheckCache(); JSON.stringify(true);'
        self.assertTrue(evaluate(source))
