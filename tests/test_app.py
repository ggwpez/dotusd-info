"""Check chart rendering and input behavior with lightweight DOM/canvas stand-ins."""
from pathlib import Path
import unittest

from js_runtime import evaluate

ROOT = Path(__file__).resolve().parents[1]


class AppTests(unittest.TestCase):
    def test_current_metrics_only_in_live_view(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += r"""
        function require(value, message) { if (!value) throw new Error(message); }
        state.rows = RangeSeries.fromWire([[100, 1, 1.0001]]);
        summaryValues = {last: state.rows.at(-1), low: state.rows.low, high: state.rows.high};
        displayScale = 'dollars';
        for (const [live, zoom] of [[true, null], [false, [90, 110]], [true, [90, 110]], [true, null]]) {
            state.live = live; state.zoom = zoom;
            hoveredRating = 4;
            summary();
            const historical = !live || !!zoom;
            require($('latest').textContent === (historical ? '-' : '$1.000100'), 'price follows view mode');
            require($('price').textContent === (historical ? '-' : '1.000100'), 'spot follows view mode');
            require($('rating-value').textContent === (historical ? '-' : 'Horrible'), 'history suppresses rating previews');
            require($('extremes').textContent === '$1.000100 / $1.000100', 'historical extrema remain visible');
        }
        true;
        """
        self.assertTrue(evaluate(source))

    def test_stream_completion_and_cancelled_responses(self):
        source = (ROOT / 'tests/app_test.js').read_text()
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
            require(!requests[0].includes('windowStart='), 'incremental polls request no full-window statistics');
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
            historyCache.merge(RangeSeries.fromWire([[now - 3598.5, 8, .5], [0, now - 3598, 9, 1.5]]),
                               now - 3600, now - 3590, now, true);
            nextHistoryRefresh = Date.now() + 60000;
            const delta = refresh();
            require(!requests.at(-1).includes('windowStart='), 'delta still skips server extrema');
            respond({ok: true, headers: {get: key => key === 'X-History-End' ? String(now + 3) : APP_VERSION}, messages: [
                {encoding: 'ranges-stream-v1', fetchedAt: now, historyReady: true, extremes: null},
                {ranges: [[now + 1, 11, .9], [0, now + 2, 12, 1.1]]}, {done: true},
            ]});
            await delta;
            require(summaryValues.low[1] === .9 && summaryValues.high[1] === 1.1,
                    'local extrema include new samples and drop expired extrema');
            state.live = false; state.zoom = [now - 100, now - 50];
            const fixed = refresh(true);
            require(requests.at(-1).includes('&end=' + (now - 50)), 'fixed history retains explicit end');
            require(requests.at(-1).includes('windowStart=') && requests.at(-1).includes('windowEnd='),
                    'history loads retain early extrema for streaming');
            respond({ok: true, headers: {get: () => APP_VERSION}, messages: [
                {encoding: 'ranges-stream-v1', fetchedAt: now, historyReady: true}, {done: true},
            ]});
            await fixed;
            return true;
        })();
        """
        self.assertTrue(evaluate(source, asynchronous=True))

    def test_return_to_live_waits_for_current_snapshot(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += r"""
        function require(value, message) { if (!value) throw new Error(message); }
        class AbortController { constructor() { this.signal = {}; } abort() {} }
        const setTimeout = () => 1, clearTimeout = () => {};
        const performance = {now: () => 100};
        const now = Date.now()/1000;
        Date.now = () => now * 1000;
        let respond, pending;
        const requests = [];
        const fetch = url => {
            requests.push(url);
            return new Promise(resolve => { respond = resolve; });
        };
        readPriceStream = async function* (response) {
            for (const message of response.messages) yield message;
        };
        const originalRefresh = refresh;
        refresh = (...args) => pending = originalRefresh(...args);
        writeViewUrl = () => {};
        (async () => {
            reloading = false;
            for (const outcome of ['fresh', 'stale', 'error']) {
                historyCache.coverage = [];
                historyCache.rows = new RangeSeries();
                const oldEnd = now - 13373;
                historyCache.merge(RangeSeries.fromWire([[oldEnd - 2, 10, 1]]),
                    oldEnd - 86400, oldEnd, now, true);
                state.range = 86400;
                state.live = false;
                state.zoom = [oldEnd - 86400, oldEnd];
                state.rows = historyCache.rows;
                const previousRequests = requests.length;
                reset();
                require(requests.length === previousRequests + 1 && loading, 'return to live fetches immediately');
                require(state.end === now, 'live window does not use the historical endpoint');
                require(elements.get('status').textContent === 'Loading live prices…',
                    'historical samples are not reported as delayed live data');
                if (outcome === 'error') {
                    respond({ok: false, headers: {get: () => APP_VERSION},
                        json: async () => ({error: 'RPC unavailable'})});
                } else {
                    respond({ok: true, headers: {get: key =>
                        key === 'X-History-End' ? String(now) : APP_VERSION}, messages: [
                        {encoding: 'ranges-stream-v1', fetchedAt: now, historyReady: true},
                        {ranges: [[outcome === 'fresh' ? now - 2 : oldEnd, 11, 1]]},
                        {done: true},
                    ]});
                }
                await pending;
                require(!state.liveLoading, 'completion clears live loading');
                const statusText = elements.get('status').textContent;
                require(outcome === 'fresh' ? statusText.startsWith('Live ~2s') :
                    outcome === 'stale' ? statusText.startsWith('Data delayed') :
                    statusText.startsWith('Connection error'), 'completed response reports its real freshness');
            }
            return true;
        })();
        """
        self.assertTrue(evaluate(source, asynchronous=True))

    def test_cached_range_changes_wait_for_poll(self):
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

    def test_large_sparse_issuance_history(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += r"""
        function require(value, message) { if (!value) throw new Error(message); }
        showIssuance = true;
        // A full retention window must not be expanded into function arguments.
        issuanceRows = Array.from({length: 300000}, (_, i) => [i * 2, 1000000 + i, i * 2]);
        state.start = 0; state.end = 604800;
        let points = 0, connections = 0;
        // Count drawing work without retaining a canvas trace for every sample.
        ctx.fillText = ctx.beginPath = ctx.moveTo = ctx.stroke = ctx.fill = () => {};
        ctx.lineTo = () => connections++;
        ctx.arc = (x, y) => {
            require(Number.isFinite(x) && Number.isFinite(y), 'finite historical sample position');
            points++;
        };
        const bounds = {right: 700, top: 20, bottom: 300, x: time => time / 1000};
        drawIssuance(bounds);
        require(points === 0, 'historical issuance has no individual sample markers');
        require(connections === 0, 'sparse history never draws across missing blocks');
        issuanceRows = Array.from({length: 15}, (_, i) => [100 + i * 2, 1000000, i]);
        state.start = 0; state.end = 200;
        points = connections = 0;
        drawIssuance(bounds);
        require(points === 0 && connections === 14, 'consecutive preview samples render as a line without markers');
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
        let requests = 0;
        refresh = () => requests++;
        writeViewUrl = () => {};
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
