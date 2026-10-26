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
            require(requests[0].startsWith('/api/historic?series=price&'), 'one historic price subscription');
            require(requests[0].includes('startpoint=10') && !requests[0].includes('&start='), 'block cursor replaces timestamp start');
            require(!requests[0].includes('&end=') && !requests[0].includes('windowEnd='), 'live requests are open-ended');
            require(!requests[0].includes('windowStart='), 'incremental polls request no full-window statistics');
            respond({ok: true, headers: {get: key => key === 'X-History-End' ? String(now + 1) : APP_VERSION}, messages: [
                {encoding: 'ranges-stream-v2', fetchedAt: now, historyReady: true},
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
                {encoding: 'ranges-stream-v2', fetchedAt: now, historyReady: true, extremes: null},
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
                {encoding: 'ranges-stream-v2', fetchedAt: now, historyReady: true}, {done: true},
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
                        {encoding: 'ranges-stream-v2', fetchedAt: now, historyReady: true},
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
        const setTimeout = () => 1, clearTimeout = () => {};
        const performance = {now: () => 100};
        const requests = [];
        const now = Date.now()/1000;
        const fetch = async url => {
            requests.push(url);
            return {ok: true, headers: {get: key => key === 'X-History-End' ? String(now) : APP_VERSION}, messages: [
                {encoding: 'ranges-stream-v2', fetchedAt: now, historyReady: true},
                {ranges: url.includes('startpoint=') ? [] : [[now - 20, 10, 1], [0, now - 10, 15, 1]]},
                {done: true},
            ]};
        };
        readPriceStream = async function* (response) {
            for (const message of response.messages) yield message;
        };
        (async () => {
            reloading = false;
            state.start = now - 3600; state.end = now;
            state.range = 3600; state.live = true; state.zoom = null;
            state.serverWarming = false;
            historyCache.merge(RangeSeries.fromWire([[now - 20, 10, 1], [0, now - 10, 15, 1]]),
                               now - 3600, now, now, true);
            nextHistoryRefresh = Date.now() + 60000;
            await refresh();
            require(requests.length === 1 && requests[0].includes('startpoint=15'), 'normal live cursor');
            nextHistoryRefresh = 0;
            await refresh();
            require(requests.length === 2 && !requests[1].includes('startpoint'), 'repair refresh includes old blocks');
            state.live = false;
            nextHistoryRefresh = 0;
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
            await refresh();
            require(requests.length === 2, 'complete fixed history makes no request');
            state.live = true;
            await refresh();
            require(requests.length === 3 && requests[2].includes('startpoint=15'),
                    'complete live history keeps its cursor after the deadline');
            require(!hasHistoryGaps(RangeSeries.fromWire([[now - 1000, 1, 1], [0, now, 2, 1]])),
                    'timestamp pauses alone are not missing pool blocks');
            require(hasHistoryGaps(RangeSeries.fromWire([[now - 20, 1, 1], [4, 2, 2]])),
                    'detect missing blocks inside compressed runs');
            return true;
        })();
        """
        self.assertTrue(evaluate(source, asynchronous=True))

    def test_price_only_subscription_ignores_state_series_links(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += r"""
        function require(value, message) { if (!value) throw new Error(message); }
        class AbortController {
            constructor() { this.signal = {aborted: false}; }
            abort() { this.signal.aborted = true; }
        }
        class TextDecoder { decode(value) { return value ?? ''; } }
        const setTimeout = () => 1, clearTimeout = () => {};
        const performance = {now: () => 100};
        const now = Date.now()/1000;
        Date.now = () => now * 1000;
        const requests = [];
        const fetch = (url, {signal}) => {
            const result = deferred();
            requests.push({url, signal, respond: result.resolve});
            return result.promise;
        };
        writeViewUrl = () => {};
        (async () => {
            reloading = false;
            state.range = 86400; state.live = true; state.zoom = null;
            const price = refresh();
            require(requests.length === 1 && requests[0].url.startsWith('/api/historic?series=price&'),
                'one price-only historic subscription');
            window.events.online();
            document.events.visibilitychange();
            intervals[0].callback();
            await refresh();
            require(requests.length === 1 && !requests[0].signal.aborted,
                'online, visibility, and poll triggers share the in-flight request');

            require(elements.get('issuance-axis').disabled && !elements.get('issuance-axis').checked,
                'the unavailable state-series setting is disabled and unchecked');
            location.search = '?ti=1';
            URLSearchParams.prototype.get = name => name === 'ti' ? '1' : null;
            window.events.popstate();
            require(!showIssuance && requests.length === 1,
                'a state-series link cannot enable the display or restart the subscription');
            const priceOnly = historicStream(undefined, now);
            requests[0].respond(priceOnly.response);
            await priceOnly.started;
            priceOnly.finish();
            await price;
            require(requests.every(request => request.url.startsWith('/api/historic?series=price&') &&
                !request.url.includes('&ti') && !request.url.includes('&treasury')),
                'only price data is requested, without state windows or cursors');
            return true;
        })();
        """
        self.assertTrue(evaluate(source, asynchronous=True))

    def test_ti_repairs_commit_only_after_a_complete_stream(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += r"""
        function require(value, message) { if (!value) throw new Error(message); }
        class AbortController { constructor() { this.signal = {}; } abort() {} }
        class TextDecoder { decode(value) { return value ?? ''; } }
        const setTimeout = () => 1, clearTimeout = () => {};
        const performance = {now: () => 100};
        const now = Date.now()/1000;
        Date.now = () => now * 1000;
        const requests = [];
        const fetch = url => {
            const result = deferred();
            requests.push({url, respond: result.resolve});
            return result.promise;
        };
        (async () => {
            reloading = false; showIssuance = true;
            state.range = 3600; state.live = true; state.zoom = null;
            const initialRows = [[now - 120, 1000, 10], [now - 90, 1200, 12]];
            const firstCursor = 'revision:1 /+';
            let pending = refresh();
            const first = historicStream({rows: initialRows, reset: true, cursor: firstCursor, error: null},
                now, [[now - 90, 12, 1]]);
            requests.at(-1).respond(first.response);
            await first.started;
            require(issuanceCursor === null && !issuanceRows.length, 'reset remains staged until completion');
            first.finish();
            await pending;
            require(issuanceCursor === firstCursor && issuanceRows.length === 2, 'completed reset is committed');
            const initialSnapshot = issuanceRows;
            const update = {rows: [[now - 100, 1100, 11], [now - 95, 1250, 12], [now - 80, 1300, 13]],
                reset: false, cursor: 'revision:2', error: null};
            pending = refresh();
            require(requests.at(-1).url.includes('startpoint=12') &&
                requests.at(-1).url.includes('&tiStart=' + (now - 3600)) &&
                requests.at(-1).url.includes('&tiCursor=' + encodeURIComponent(firstCursor)),
                'price cursor and opaque TI revision use independent bounds in one URL');
            const interrupted = historicStream(update, now);
            requests.at(-1).respond(interrupted.response);
            await interrupted.started;
            interrupted.finish([]);
            await pending;
            require(state.error && issuanceRows === initialSnapshot && issuanceCursor === firstCursor,
                'truncated response cannot publish TI or consume its revision');

            pending = refresh();
            require(requests.at(-1).url.includes('&tiCursor=' + encodeURIComponent(firstCursor)),
                'retry resumes from the last completed revision');
            const repaired = historicStream(update, now);
            requests.at(-1).respond(repaired.response);
            await repaired.started;
            require(issuanceRows === initialSnapshot && issuanceCursor === firstCursor,
                'repair metadata stays staged during the price stream');
            repaired.finish();
            await pending;
            require(!state.error && issuanceCursor === 'revision:2' &&
                JSON.stringify(issuanceRows) === JSON.stringify([
                    initialRows[0], update.rows[0], update.rows[1], update.rows[2],
                ]), 'delta merges older repairs, replaces the exact block, and retains untouched samples');
            const repairedSnapshot = issuanceRows;
            for (const ti of [
                undefined,
                {error: 'TI unavailable'},
            ]) {
                pending = refresh();
                const unavailable = historicStream(ti, now);
                requests.at(-1).respond(unavailable.response);
                await unavailable.started;
                unavailable.finish();
                await pending;
                require(issuanceCursor === 'revision:2' && issuanceRows === repairedSnapshot,
                    'omitted or incomplete TI metadata preserves the last completed cursor and cache');
            }
            pending = refresh();
            const trailing = historicStream({rows: [], reset: true, cursor: 'trailing', error: null}, now);
            requests.at(-1).respond(trailing.response);
            await trailing.started;
            trailing.finish([{done: true}, {ranges: []}]);
            await pending;
            require(state.error && issuanceCursor === 'revision:2' && issuanceRows === repairedSnapshot,
                'even a done marker cannot commit TI when trailing stream data is invalid');
            issuanceRows = []; issuanceCursor = null; issuanceCoverage = null;
            pending = refresh();
            const retained = historicStream({rows: [[now - 70, 1400, 14]], reset: true,
                cursor: 'retained', error: 'Live RPC unavailable'}, now);
            requests.at(-1).respond(retained.response);
            await retained.started;
            retained.finish();
            await pending;
            require(!state.error && issuanceCursor === 'retained' && issuanceRows[0][2] === 14,
                'cold clients accept retained TI samples despite a live RPC error status');
            pending = refresh();
            const backfill = historicStream({rows: [[now - 80, 1300, 13]], reset: false,
                cursor: 'backfilled', error: 'Live RPC unavailable'}, now);
            requests.at(-1).respond(backfill.response);
            await backfill.started;
            backfill.finish();
            await pending;
            require(issuanceCursor === 'backfilled' && issuanceRows.length === 2 && issuanceRows[0][2] === 13,
                'completed backfill revisions remain usable while live RPC status reports an error');
            pending = refresh();
            const replacement = historicStream({rows: [], reset: true, cursor: 'replacement', error: null}, now);
            requests.at(-1).respond(replacement.response);
            await replacement.started;
            replacement.finish();
            await pending;
            require(!state.error && issuanceCursor === 'replacement' && !issuanceRows.length,
                'a completed empty reset removes the previous cache');
            return true;
        })();
        """
        self.assertTrue(evaluate(source, asynchronous=True))

    def test_ti_viewport_coverage_and_fixed_history_polling(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += r"""
        function require(value, message) { if (!value) throw new Error(message); }
        class AbortController { constructor() { this.signal = {}; } abort() {} }
        class TextDecoder { decode(value) { return value ?? ''; } }
        const setTimeout = () => 1, clearTimeout = () => {};
        const performance = {now: () => 100};
        let now = Date.now()/1000;
        Date.now = () => now * 1000;
        const requests = [];
        const fetch = url => {
            const result = deferred();
            requests.push({url, respond: result.resolve});
            return result.promise;
        };
        async function finishRequest(pending, cursor, reset = false, rows = []) {
            const stream = historicStream({rows, cursor, reset, error: null}, now, [],
                {rows, cursor: 'treasury-' + cursor, reset, error: null});
            requests.at(-1).respond(stream.response);
            await stream.started;
            stream.finish();
            await pending;
        }
        (async () => {
            reloading = false; showIssuance = true;
            state.range = 3600; state.live = true; state.zoom = null;
            await finishRequest(refresh(), 'live-1', true);
            now += 6;
            let pending = refresh();
            require(requests.at(-1).url.includes('&tiCursor=live-1') &&
                requests.at(-1).url.includes('&treasuryCursor=treasury-live-1') &&
                requests.at(-1).url.includes('&tiStart=' + (now - 3600)) &&
                requests.at(-1).url.includes('&treasuryStart=' + (now - 3600)) &&
                !requests.at(-1).url.includes('&tiEnd=') && !requests.at(-1).url.includes('&treasuryEnd='),
                'forward live motion keeps both cursors and moves both viewport lower bounds');
            await finishRequest(pending, 'live-2');
            state.live = false;
            state.zoom = [now - 100, now - 50];
            const beforeNavigation = requests.length;
            await refresh(false, true);
            require(requests.length === beforeNavigation && issuanceCursor === 'live-2' &&
                treasuryCursor === 'treasury-live-2',
                'a covered narrower viewport can render locally until the shared poll');
            require(needsRefresh(), 'TI keeps complete fixed-history views subscribed for repairs');
            pending = refresh();
            require(requests.at(-1).url.includes('&tiStart=' + (now - 100)) &&
                requests.at(-1).url.includes('&tiEnd=' + (now - 50)) &&
                requests.at(-1).url.includes('&tiCursor=live-2') &&
                requests.at(-1).url.includes('&treasuryStart=' + (now - 100)) &&
                requests.at(-1).url.includes('&treasuryEnd=' + (now - 50)) &&
                requests.at(-1).url.includes('&treasuryCursor=treasury-live-2'),
                'fixed history requests independent explicit bounds on the same subscription');
            await finishRequest(pending, 'fixed-1', false, [[now - 80, 1000, 10]]);
            pending = refresh();
            require(requests.length === beforeNavigation + 2 &&
                requests.at(-1).url.includes('&tiCursor=fixed-1'),
                'every fixed-history TI poll runs despite complete price coverage');
            await finishRequest(pending, 'fixed-2', false, [[now - 75, 1100, 11]]);
            require(issuanceRows.length === 2, 'fixed-history polling discovers repaired TI blocks');

            state.zoom = [now - 200, now - 50];
            pending = refresh(false, true);
            require(!requests.at(-1).url.includes('tiCursor=') && issuanceCursor === null &&
                !requests.at(-1).url.includes('treasuryCursor=') && treasuryCursor === null,
                'extending left resets both cursors even with cached prices');
            await finishRequest(pending, 'wider', true, [[now - 150, 900, 9]]);
            require(issuanceRows.length === 1 && issuanceRows[0][2] === 9,
                'a wider-window reset replaces the narrow cache');
            state.zoom = [now - 150, now - 10];
            pending = refresh(false, true);
            require(!requests.at(-1).url.includes('tiCursor=') && !requests.at(-1).url.includes('treasuryCursor='),
                'extending the fixed upper bound also resets both cursors');
            await finishRequest(pending, 'later', true);
            state.live = true; state.zoom = null; state.range = 7200;
            pending = refresh(false, true);
            require(!requests.at(-1).url.includes('tiCursor=') && !requests.at(-1).url.includes('tiEnd=') &&
                requests.at(-1).url.includes('&tiStart=' + (now - 7200)) &&
                !requests.at(-1).url.includes('treasuryCursor=') && !requests.at(-1).url.includes('treasuryEnd=') &&
                requests.at(-1).url.includes('&treasuryStart=' + (now - 7200)),
                'returning to a wider live window resets both series independently of the price cache');
            await finishRequest(pending, 'wide-live', true);
            return true;
        })();
        """
        self.assertTrue(evaluate(source, asynchronous=True))

    def test_treasury_revisions_failures_and_independent_coverage(self):
        source = (ROOT / 'tests/app_test.js').read_text()
        source += '\n' + (ROOT / 'ranges.js').read_text().replace('export ', '')
        source += '\n' + (ROOT / 'app.js').read_text().split('\n', 1)[1]
        source += r"""
        function require(value, message) { if (!value) throw new Error(message); }
        class AbortController { constructor() { this.signal = {}; } abort() {} }
        class TextDecoder { decode(value) { return value ?? ''; } }
        const setTimeout = () => 1, clearTimeout = () => {};
        const performance = {now: () => 100};
        const now = Date.now()/1000;
        Date.now = () => now * 1000;
        const requests = [];
        const fetch = url => {
            const result = deferred();
            requests.push({url, respond: result.resolve});
            return result.promise;
        };
        const snapshot = (rows, cursor, reset = false) => ({rows, cursor, reset, error: null});
        async function complete(pending, ti, treasury, ranges = []) {
            const stream = historicStream(ti, now, ranges, treasury);
            requests.at(-1).respond(stream.response);
            await stream.started;
            stream.finish();
            await pending;
        }
        (async () => {
            reloading = false; showIssuance = true;
            state.range = 3600; state.live = true; state.zoom = null;
            const initial = [[now - 120, 0, 10], [now - 90, 120, 12]];
            const cursor = 'treasury:1 /+';
            let pending = refresh();
            await complete(pending, snapshot([[now - 90, 1000, 12]], 'ti:1', true),
                snapshot(initial, cursor, true), [[now - 90, 12, 1]]);
            require(treasuryCursor === cursor && treasuryRows.length === 2 && treasuryRows[0][1] === 0,
                'completed Treasury reset retains zero balances');
            const retained = treasuryRows, retainedCoverage = treasuryCoverage;
            const retainedTI = issuanceRows, retainedTICoverage = issuanceCoverage;
            const update = snapshot([[now - 100, 100, 11], [now - 95, 125, 12], [now - 80, 130, 13]],
                'treasury:2');
            for (const endings of [[], [{done: true}, {ranges: []}]]) {
                pending = refresh();
                require(requests.at(-1).url.includes('startpoint=12') &&
                    requests.at(-1).url.includes('&tiCursor=ti%3A1') &&
                    requests.at(-1).url.includes('&treasuryCursor=' + encodeURIComponent(cursor)),
                    'price, TI, and opaque Treasury cursors are independent');
                const interrupted = historicStream(snapshot([], 'ti:lost', true), now, [], update);
                requests.at(-1).respond(interrupted.response);
                await interrupted.started;
                require(treasuryRows === retained && treasuryCursor === cursor &&
                    treasuryCoverage === retainedCoverage && issuanceRows === retainedTI &&
                    issuanceCoverage === retainedTICoverage,
                    'headers cannot publish either cursor, coverage, or sample cache');
                interrupted.finish(endings);
                await pending;
                require(state.error && treasuryRows === retained && treasuryCursor === cursor &&
                    treasuryCoverage === retainedCoverage && issuanceRows === retainedTI &&
                    issuanceCursor === 'ti:1' && issuanceCoverage === retainedTICoverage,
                    'truncated or trailing-invalid streams retain both completed snapshots');
            }
            pending = refresh();
            requests.at(-1).respond({ok: false, headers: {get: () => APP_VERSION},
                json: async () => ({error: 'Archive unavailable'})});
            await pending;
            require(state.error && treasuryRows === retained && treasuryCursor === cursor,
                'HTTP failures preserve successful Treasury data and revision');
            for (const treasury of [undefined, {error: 'Treasury unavailable'}]) {
                pending = refresh();
                await complete(pending, snapshot([], 'ti:2'), treasury);
                require(treasuryRows === retained && treasuryCursor === cursor &&
                    treasuryCoverage === retainedCoverage && issuanceCursor === 'ti:2',
                    'missing Treasury metadata cannot consume its cursor or block a successful TI update');
            }
            pending = refresh();
            await complete(pending, undefined, update);
            require(!state.error && treasuryCursor === 'treasury:2' && issuanceCursor === 'ti:2' &&
                JSON.stringify(treasuryRows) === JSON.stringify([initial[0], ...update.rows]),
                'Treasury repairs insert old blocks, replace exact blocks, and preserve untouched zero rows');

            state.live = false; state.zoom = [now - 100, now - 50];
            pending = refresh();
            await complete(pending, undefined, snapshot([], 'treasury:narrow'));
            require(treasuryCoverage.start === now - 100 && treasuryCoverage.end === now - 50 &&
                !treasuryCoverage.live && issuanceCoverage.live,
                'each completed series tracks its own subscribed coverage');
            state.zoom = [now - 200, now - 50];
            pending = refresh(false, true);
            require(requests.at(-1).url.includes('&tiCursor=ti%3A2') &&
                !requests.at(-1).url.includes('treasuryCursor=') &&
                requests.at(-1).url.includes('&treasuryStart=' + (now - 200)) &&
                requests.at(-1).url.includes('&treasuryEnd=' + (now - 50)),
                'expansion resets only Treasury when TI still covers the requested viewport');
            const narrowCoverage = treasuryCoverage, repairedRows = treasuryRows;
            const widened = historicStream(undefined, now, [], snapshot([], 'treasury:wide', true));
            requests.at(-1).respond(widened.response);
            await widened.started;
            require(treasuryCoverage === narrowCoverage && treasuryRows === repairedRows,
                'expanded coverage and empty reset remain staged until stream completion');
            widened.finish();
            await pending;
            require(!treasuryRows.length && treasuryCursor === 'treasury:wide' &&
                treasuryCoverage.start === now - 200 && issuanceCursor === 'ti:2',
                'completed empty reset replaces only Treasury and commits expanded coverage');
            return true;
        })();
        """
        self.assertTrue(evaluate(source, asynchronous=True))

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
        require(connections === 299999, 'densifying coarse history remains connected');
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

    def test_changelog_dot_marks_unseen_releases_until_opened(self):
        app = (ROOT / 'ranges.js').read_text().replace('export ', '') + '\n' + \
            (ROOT / 'app.js').read_text().split('\n', 1)[1]
        for seen, unseen in ((None, True), ('1.12.6', True), ('1.9.9', True), ('garbage', True),
                             ('1.13.0', False), ('1.14.0', False)):
            with self.subTest(seen=seen):
                source = (ROOT / 'tests/app_test.js').read_text() + r"""
                const stored = new Map(%s);
                const localStorage = {getItem: key => stored.get(key) ?? null,
                                      setItem: (key, value) => stored.set(key, value)};
                const changelogElement = document.getElementById('changelog');
                changelogElement.dataset.latest = '1.13.0';
                changelogElement.querySelector = () => document.getElementById('changelog-summary');
                changelogElement.contains = target => target === changelogElement;
                const settingsElement = document.getElementById('settings');
                settingsElement.contains = target => target === settingsElement;
                document.querySelector = selector => selector === '.settings' ? settingsElement :
                    selector.includes('icon') ? {href: 'icon.svg'} : {content: 'old-version'};
                """ % ('[]' if seen is None else f"[['dotusd-changelog-seen', '{seen}']]") + '\n' + app + r"""
                function require(value, message) { if (!value) throw new Error(message); }
                const dot = elements.get('changelog-dot');
                require(dot.hidden === %s, 'initial unseen marker');
                settingsElement.open = true;
                changelogElement.open = true;
                changelogElement.events.toggle();
                require(dot.hidden && stored.get('dotusd-changelog-seen') === '1.13.0',
                        'opening marks the latest release seen');
                require(!settingsElement.open, 'opening the changelog closes settings');
                document.events.click({target: {}});
                require(!changelogElement.open, 'outside clicks close the changelog');
                JSON.stringify(true);
                """ % ('false' if unseen else 'true')
                self.assertTrue(evaluate(source))
