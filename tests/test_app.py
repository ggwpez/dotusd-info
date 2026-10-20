"""Check chart rendering and input behavior with lightweight DOM/canvas stand-ins."""
from pathlib import Path
import unittest

from js_runtime import evaluate

ROOT = Path(__file__).resolve().parents[1]


class AppTests(unittest.TestCase):
    def test_stream_completion_and_cancelled_responses(self):
        for price_source in ('pool', 'coingecko'):
            with self.subTest(price_source=price_source):
                source = (ROOT / 'tests/app_test.js').read_text()
                source += f'\ndocument.body.dataset.priceSource = "{price_source}";'
                source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
                source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
                source += r"""
                function require(value, message) { if (!value) throw new Error(message); }
                class AbortController { constructor() { this.signal = {}; } abort() {} }
                const setTimeout = () => 1;
                const clearTimeout = () => {};
                const performance = {now: () => 100};
                const now = Date.now()/1000;
                Date.now = () => now * 1000;
                const requests = [];
                let respond;
                const fetch = url => {
                    requests.push(url);
                    return new Promise(resolve => { respond = resolve; });
                };
                readPriceStream = async function* (response) {
                    for (const message of response.messages) yield message;
                };
                (async () => {
                    reloading = false;
                    state.range = 3600; state.live = true; state.zoom = null;
                    const stamp = now - .1;
                    historyCache.merge(RangeSeries.fromWire([[stamp, 10, 1]]),
                                       now - 3600, now - .05, now, true);
                    const pending = refresh();
                    require(requests[0].includes('startpoint=10') && !requests[0].includes('?start='), 'block cursor replaces timestamp start');
                    require(!requests[0].includes('&end=') && !requests[0].includes('windowEnd='), 'live requests are open-ended');
                    respond({ok: true, headers: {get: key => key === 'X-History-End' ? String(now + 1) : APP_VERSION}, messages: [
                        {encoding: 'ranges-stream-v1', fetchedAt: now, historyReady: true},
                        {ranges: []}, {done: true},
                    ]});
                    await pending;
                    require(!state.error && !loading, 'stream completed');
                    require(historyCache.rows.length === 1 && historyCache.rows.at(-1)[0] === stamp,
                            'sub-second delta retains the last sample exactly once');
                    require(!historyCache.missing(now - 3600, now + 1).length, 'delta advances coverage to the server boundary');
                    require(state.end === now + 1, 'chart uses server boundary');
                    const stale = refresh();
                    const finishStale = respond;
                    await refresh(false, true);
                    finishStale({headers: {get: () => 'obsolete-release'}});
                    await stale;
                    require(!reloading, 'cancelled response cannot reload the page');
                    require(historyCache.rows.length === 1, 'cancelled response cannot alter cache');
                    state.live = false; state.zoom = [now - 100, now - 50];
                    const fixed = refresh(true);
                    require(requests.at(-1).includes('&end=' + (now - 50)), 'fixed history retains explicit end');
                    respond({ok: true, headers: {get: () => APP_VERSION}, messages: [
                        {encoding: 'ranges-stream-v1', fetchedAt: now, historyReady: true}, {done: true},
                    ]});
                    await fixed;
                    return true;
                })();
                """
                self.assertTrue(evaluate(source, asynchronous=True))

    def test_cached_range_changes_wait_for_poll(self):
        for price_source in ('pool', 'coingecko'):
            with self.subTest(price_source=price_source):
                source = (ROOT / 'tests/app_test.js').read_text()
                source += f'\ndocument.body.dataset.priceSource = "{price_source}";'
                source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
                source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
                source += r"""
                function require(value, message) { if (!value) throw new Error(message); }
                class AbortController { constructor() { this.signal = {}; } abort() {} }
                const setTimeout = () => 1;
                const clearTimeout = () => {};
                const requests = [];
                const fetch = url => { requests.push(url); return new Promise(() => {}); };
                writeViewUrl = () => {};
                reloading = false;
                const end = Date.now()/1000 - 3;
                historyCache.merge(RangeSeries.fromWire([[end - 90000, 10, .99],
                    [0, end - 10, 15, 1]]), end - 604800, end, end, true);
                // Even an overdue repair refresh can wait for the scheduled poll.
                nextHistoryRefresh = 0;
                for (const range of [86400, 3600, 21600, 604800]) {
                    state.range = range;
                    reset();
                    require(requests.length === 0, 'covered range never fetches');
                    require(state.end === end && state.start === end - range, 'snapshot bounds');
                    require(state.rows.length === (range === 604800 ? 2 : 1), 'local rows');
                    require(summaryValues.low[1] === (range === 604800 ? .99 : 1), 'local extrema');
                    require(!loading && !state.historyLoading && needsRefresh(), 'ready for next poll');
                }
                refresh();
                require(requests.length === 1, 'next poll still fetches');
                refresh(true, true);
                require(requests.length === 2, 'manual refresh still fetches');
                historyCache.coverage = [[end - 3600, end]];
                state.range = 86400;
                reset();
                require(requests.length === 3, 'uncached history fetches immediately');
                JSON.stringify(true);
                """
                self.assertTrue(evaluate(source))

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
        require(requests.length === 1 && requests[0].includes('startpoint=15'), 'normal live cursor');
        nextHistoryRefresh = 0;
        refresh();
        require(requests.length === 2 && !requests[1].includes('startpoint'), 'repair refresh includes old blocks');
        state.live = false;
        require(needsRefresh(), 'fixed views also discover repairs');
        nextHistoryRefresh = Date.now() + 60000;
        state.historyLoading = false;
        require(!needsRefresh(), 'fixed views rest between refreshes');
        // Once repairs close the gap, an elapsed deadline must not refetch history.
        historyCache.merge(RangeSeries.fromWire([[now - 20, 10, 1], [5, 2, 1]]),
                           now - 3600, now, now, true);
        state.start = now - 3600; state.end = now;
        state.rows = historyCache.rows;
        nextHistoryRefresh = 0;
        require(!needsRefresh(), 'complete fixed history needs no periodic refresh');
        refresh();
        require(requests.length === 2, 'complete fixed history makes no request');
        state.live = true;
        refresh();
        require(requests.length === 3 && requests[2].includes('startpoint=15'),
                'complete live history keeps its cursor after the deadline');
        require(!hasHistoryGaps(RangeSeries.fromWire([[now - 1000, 1, 1], [0, now, 2, 1]])),
                'timestamp pauses alone are not missing pool blocks');
        require(hasHistoryGaps(RangeSeries.fromWire([[now - 20, 1, 1], [4, 2, 2]])),
                'detect missing blocks inside compressed runs');
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
        require(!hasHistoryGaps(RangeSeries.fromWire([[100, null, 1], [0, 200, null, 1]])),
                'normal quote intervals need no history refresh');
        require(hasHistoryGaps(RangeSeries.fromWire([[100, null, 1], [0, 1100, null, 1]])),
                'missing quote intervals allow history retries');
        state.live = false; state.zoom = null; state.historyLoading = false;
        state.rows = RangeSeries.fromWire([[100, null, 1], [0, 200, null, 1]]);
        nextHistoryRefresh = 0;
        require(!needsRefresh(), 'complete quote history rests after the deadline');
        state.rows = RangeSeries.fromWire([[100, null, 1], [0, 1100, null, 1]]);
        require(needsRefresh(), 'gapped quote history retries after the deadline');
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
        const observed = RangeSeries.fromWire([[now - 20, 100, 1], [3, 6, 3]]);
        const observedPoints = [...observed.points(connectSamples)];
        require(observedPoints.length <= 3 && observedPoints.slice(1).every(point => !point.move), 'quote blocks do not create false gaps between observations');
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
