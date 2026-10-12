"""Check chart rendering and input behavior with lightweight DOM/canvas stand-ins."""
from pathlib import Path
import unittest

from js_runtime import evaluate

ROOT = Path(__file__).resolve().parents[1]


class AppTests(unittest.TestCase):
    def test_drawing_hover_keyboard_and_drag_zoom(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        # The server also combines the range module with the app, removing its import.
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += '\ncheckApp(); JSON.stringify(true);'
        self.assertTrue(evaluate(source))

    def test_coingecko_uses_usd_timestamps_and_shared_rendering(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += "\ndocument.body.dataset.priceSource = 'coingecko'; location.search = '?resolution=7d';"
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += r"""
        checkApp();
        function require(value, message) { if (!value) throw new Error(message); }
        require(source.endpoint === '/api/coingecko/prices', 'isolated API');
        require(elements.get('compare-source').href === '/?resolution=7d', 'comparison preserves range');
        reloading = false;
        const now = Date.now()/1000;
        state.start = now - 3600; state.end = now; state.live = true; state.zoom = null;
        state.rows = RangeSeries.fromWire([[now - 600, null, .999], [0, now - 300, null, 1.001]]);
        state.extremes = null; state.hover = 1; state.fetchedAt = now;
        chartDirty = true; drawing.length = 0; draw();
        require(elements.get('tooltip').children.some(child => child.textContent === '1.00100000 USD'), 'USD tooltip');
        const points = [...pixelRows(state.rows, t => t, connectSamples)];
        require(points.length === 2 && points[0].move && !points[1].move, 'connect timestamp samples');
        require(!connectSamples([now - 2000, 1, null], [now, 1, null]), 'break missing recent data');
        require(connectSamples([now - 100000, 1, null], [now - 96400, 1, null]), 'connect hourly history');
        require(!connectSamples([now - 100000, 1, null], [now - 92800, 1, null]), 'break missing hourly data');
        updateFreshness();
        require(elements.get('status').dataset.state === 'live', '5 minute sample is fresh');
        state.fetchedAt = now - 1000; updateFreshness();
        require(elements.get('status').textContent.startsWith('Updates delayed'), 'failed source refresh becomes stale');
        state.rows = new RangeSeries(); state.error = null; state.historyLoading = false;
        summaryValues = null; summary();
        require(elements.get('message').textContent.includes('CoinGecko has no'), 'honest empty state');
        require(elements.get('price').textContent === '-', 'empty state has no fabricated price');
        JSON.stringify(true);
        """
        self.assertTrue(evaluate(source))
