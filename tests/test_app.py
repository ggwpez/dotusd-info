"""Check chart rendering and input behavior with lightweight DOM/canvas stand-ins."""
from pathlib import Path
import unittest

from js_runtime import evaluate

ROOT = Path(__file__).resolve().parents[1]


class AppTests(unittest.TestCase):
    def test_pool_history_refresh_bypasses_live_cursor_for_repairs(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += r"""
        function require(value, message) { if (!value) throw new Error(message); }
        class AbortController { constructor() { this.signal = {}; } abort() {} }
        const setTimeout = () => 1;
        const clearTimeout = () => {};
        const requests = [];
        const fetch = url => { requests.push(url); return new Promise(() => {}); };
        reloading = false;
        const now = Date.now()/1000;
        state.start = now - 3600; state.end = now;
        state.range = 3600; state.live = true; state.zoom = null;
        state.serverWarming = false;
        historyCache.merge(RangeSeries.fromWire([[now - 20, 10, 1], [0, now - 10, 15, 1]]),
                           now - 3600, now, now, true);
        nextHistoryRefresh = Date.now() + 60000;
        refresh();
        require(requests.length === 1 && requests[0].includes('afterBlock=15'), 'normal live cursor');
        nextHistoryRefresh = 0;
        refresh();
        require(requests.length === 2 && !requests[1].includes('afterBlock'), 'repair refresh includes old blocks');
        state.live = false;
        require(needsRefresh(), 'fixed views also discover repairs');
        nextHistoryRefresh = Date.now() + 60000;
        state.historyLoading = false;
        require(!needsRefresh(), 'fixed views rest between refreshes');
        JSON.stringify(true);
        """
        self.assertTrue(evaluate(source))

    def test_drawing_hover_keyboard_and_drag_zoom(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        # The server also combines the range module with the app, removing its import.
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += '\ncheckApp(); JSON.stringify(true);'
        self.assertTrue(evaluate(source))

    def test_coingecko_uses_hub_timestamps_and_shared_rendering(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += "\ndocument.body.dataset.priceSource = 'coingecko'; location.search = '?resolution=7d';"
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += r"""
        checkApp();
        function require(value, message) { if (!value) throw new Error(message); }
        require(source.endpoint === '/api/coingecko/prices', 'isolated API');
        require(source.pollSeconds === 6, 'six second refresh');
        require(elements.get('compare-source').href === '/?resolution=7d', 'comparison preserves range');
        reloading = false;
        const now = Date.now()/1000;
        state.start = now - 3600; state.end = now; state.live = true; state.zoom = null;
        state.rows = RangeSeries.fromWire([[now - 4, null, .999], [0, now - 2, null, 1.001]]);
        state.extremes = null; state.hover = 1; state.fetchedAt = now;
        chartDirty = true; drawing.length = 0; draw();
        require(elements.get('tooltip').children.some(child => child.textContent === '1.00100000 USDT'), 'USDT tooltip');
        const points = [...pixelRows(state.rows, t => t, connectSamples)];
        require(points.length === 2 && points[0].move && !points[1].move, 'connect timestamp samples');
        require(!connectSamples([now - 2000, 1, null], [now, 1, null]), 'break missing recent data');
        require(!connectSamples([now - 100000, 1, null], [now - 96400, 1, null]), 'older history also preserves observation gaps');
        require(!connectSamples([now - 100000, 1, null], [now - 92800, 1, null]), 'break missing hourly data');
        updateFreshness();
        require(elements.get('status').dataset.state === 'live', '2 second sample is fresh');
        state.rows = RangeSeries.fromWire([[now - 60, null, 1.001]]); updateFreshness();
        require(elements.get('status').dataset.state === 'live', 'normal finality lag is fresh');
        state.rows = RangeSeries.fromWire([[now - 181, null, 1.001]]); updateFreshness();
        require(elements.get('status').textContent.startsWith('Data delayed'), 'stalled chain is delayed');
        state.fetchedAt = now - 1000; updateFreshness();
        require(elements.get('status').textContent.startsWith('Updates delayed'), 'failed source refresh becomes stale');
        state.rows = new RangeSeries(); state.error = null; state.historyLoading = false;
        summaryValues = null; summary();
        require(elements.get('message').textContent.includes('No finalized Hub samples available'), 'honest empty state');
        require(elements.get('price').textContent === '-', 'empty state has no fabricated price');
        JSON.stringify(true);
        """
        self.assertTrue(evaluate(source))

    def test_graph_colors_blend_symmetrically_without_zoom_dependent_cutoffs(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += r"""
        function require(value, message) { if (!value) throw new Error(message); }
        require(deviationColor(0) === ratingColors[0], 'peg uses logo green');
        require(!ratingColors.includes(deviationColor(10)), 'intermediate deviation blends colors');
        require(deviationColor(1000) === ratingColors[4], 'extreme deviation stays red');
        for (const boundary of ratingLimits) {
          require(deviationColor(boundary - .00001) === deviationColor(boundary + .00001),
            'color is continuous across rating boundaries');
        }
        require(ratingForBps(5) === 0 && ratingForBps(5.01) === 1, 'rating thresholds unchanged');
        for (const extent of [5, 10, 20, 37, 100, 150, 200]) {
          const stops = [];
          const context = {createLinearGradient: () => ({addColorStop: (at, color) => stops.push([at, color])})};
          ratingGradient(context, 0, 300, extent, '20');
          require(stops[0][0] === 0 && stops.at(-1)[0] === 1, 'gradient covers full viewport');
          require(stops.every(([at, color], i) => !i || at > stops[i - 1][0]), 'no duplicate hard stops');
          require(stops[0][1] === deviationColor(extent) + '20', 'clipped edge uses interpolated color');
          require(stops.every(([at, color], i) => color === stops[stops.length - 1 - i][1]),
            'positive and negative deviations match');
        }
        JSON.stringify(true);
        """
        self.assertTrue(evaluate(source))

    def test_display_scales_preserve_prices_and_do_not_refetch(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += r"""
        function require(value, message) { if (!value) throw new Error(message); }
        require(displayScale === 'dollars', 'dollar price is the default');
        let requests = 0;
        refresh = () => requests++;
        state.start = 100; state.end = 200;
        state.rows = RangeSeries.fromWire([[110, 1, .999]]);
        summaryValues = {last: state.rows.at(-1), low: state.rows.low, high: state.rows.high};
        state.hover = 0;
        for (const [scale, expected, tip] of [
          ['percent', '-0.1000', '-0.1000%'],
          ['dollars', '$0.999000', '$0.999000'],
          ['bps', '-10.00', '-10.00 bps'],
        ]) {
          elements.get('display-scale').events.change({target: {value: scale}});
          draw();
          require(elements.get('latest').textContent === expected, scale + ' summary');
          require(elements.get('tooltip').children[1].textContent === tip, scale + ' tooltip');
          require(elements.get('rating-value').textContent === 'Good', 'rating unchanged');
          require(state.rows.at(-1)[1] === .999, 'source price unchanged');
        }
        require(requests === 0, 'display changes never refetch history');
        JSON.stringify(true);
        """
        self.assertTrue(evaluate(source))
