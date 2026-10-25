function assert(value, message) { if (!value) throw new Error(message); }
function equal(actual, expected, message) {
  assert(JSON.stringify(actual) === JSON.stringify(expected), message);
}
const expand = series => Array.from({length: series.length}, (_, i) => series.at(i));
function canonical(points) {
  const out = [];
  for (const point of points) {
    const a = out.at(-2), b = out.at(-1);
    if (a && !point.move && !b.move && a.row[1] === b.row[1] && b.row[1] === point.row[1]) out.pop();
    out.push(point);
  }
  return out;
}
function checkFixture(rows, wire) {
  const series = RangeSeries.fromWire(wire);
  equal(expand(series), rows, 'lossless wire decode and random access');
  equal(series.at(-1), rows.at(-1), 'last sample');
  const expected = rows.map((row, i) => ({row, move: !connected(rows[i-1], row)}));
  equal(canonical([...series.points()]), canonical(expected), 'same line geometry and gaps');
  if (!rows.length) { assert(series.nearest(10) === null, 'empty hover'); return; }
  const low = rows.reduce((a,b) => b[1] < a[1] ? b : a);
  const high = rows.reduce((a,b) => b[1] > a[1] ? b : a);
  equal(series.low, low, 'low sample'); equal(series.high, high, 'high sample');
  for (let i = 0; i < rows.length; i += Math.max(1, Math.floor(rows.length / 100))) {
    for (const delta of [0, -.1, .1, 1]) {
      const time = rows[i][0] + delta;
      let nearest = 0;
      for (let j = 1; j < rows.length; j++) {
        if (Math.abs(rows[j][0] - time) <= Math.abs(rows[nearest][0] - time)) nearest = j;
      }
      equal(series.at(series.nearest(time)), rows[nearest], 'nearest sample and tie handling');
    }
    const start = rows[i][0], end = rows[Math.min(rows.length-1, i+37)][0];
    equal(expand(series.window(start, end)), rows.filter(r => r[0] > start && r[0] <= end), 'exclusive start/inclusive end clipping');
  }
  const points = [...pixelRows(series, t => Math.floor(t / 100))];
  assert(points.some(p => p.row[1] === low[1]) && points.some(p => p.row[1] === high[1]), 'pixel extrema');
  equal(points.filter(p => p.move).length, expected.filter(p => p.move).length, 'pixel gaps');
  equal(expand(series.tail(37)), rows.slice(-37), 'sample cap within a run');
}
function checkCache() {
  const live = new BrowserHistory();
  const initial = RangeSeries.fromWire([[100, 50, 1], [1, 2, 1, 1.01]]);
  live.merge(initial, 0, 110, 110);
  // Empty cursor responses must retain the last sample for subsequent polls.
  live.merge(new RangeSeries(), live.rows.at(-1)[0], 116, 116);
  equal(expand(live.rows), expand(initial), 'empty live poll retains samples and block cursor');
  equal(live.missing(0, 116), [], 'empty live poll advances completed coverage');
  const catchup = RangeSeries.fromWire([[104, 52, .99], [1, 396, 1, 1]]);
  live.merge(catchup, live.rows.at(-1)[0], 410, 410);
  equal(expand(live.rows), [...expand(initial), ...expand(catchup)], 'reconnect appends delayed samples without duplicates');
  equal(live.missing(0, 410), [], 'reconnect completes coverage');
  const old = new RangeSeries([{t: 10, price: 1, block: 100, count: 100, dt: 2, db: 1}]);
  const history = new BrowserHistory();
  history.merge(old.window(90, 210), 90, 210, 1);
  history.merge(old.window(0, 90), 0, 90, 2);
  equal(expand(history.rows), expand(old), 'reverse-order adjacent chunks');
  assert(history.rows.runs.length === 1, 'coalesce chunks');
  const replacement = new RangeSeries([{t: 100, price: 2, block: 145, count: 5, dt: 2, db: 1}]);
  history.merge(replacement, 99, 109, 3);
  const expected = expand(old).filter(r => r[0] <= 99 || r[0] > 109).concat(expand(replacement)).sort((a,b) => a[0]-b[0]);
  equal(expand(history.rows), expected, 'overlap replaces only covered samples');
  history.merge(replacement, 99, 109, 4);
  equal(expand(history.rows), expected, 'idempotent live overlap');
  history.merge(new RangeSeries(), 99, 109, 5);
  equal(expand(history.rows), expand(old).filter(r => r[0] <= 99 || r[0] > 109), 'authoritative empty interval');
  equal(history.missing(0, 210), [], 'coverage merge');
  history.expire(604900);
  assert(history.rows.at(0)[0] > 100, 'expiry clips inside range');
  equal(history.missing(0, 210), [[0, 100]], 'only expired history loses coverage');
  history.expire(604960);
  equal(history.missing(160, 216), [[210, 216]], 'live polls retain history after a minute');
  const partial = new BrowserHistory();
  partial.merge(old, 0, 210, 1, false);
  equal(partial.missing(0, 210), [[0, 210]], 'warming response is retried');
  partial.merge(old, 0, 210, 2, true);
  partial.merge(new RangeSeries(), 90, 110, 3, false);
  equal(partial.missing(0, 210), [[90, 110]], 'incomplete replacement invalidates old coverage only within its bounds');
  partial.merge(old.window(90, 110), 90, 110, 4, true);
  equal(partial.missing(0, 210), [], 'completed retry restores coverage');
  const large = new RangeSeries([{t: 10, price: 1, block: 1, count: 400000, dt: 2, db: 1}]);
  partial.merge(large, 0, 800010, 2);
  assert(partial.rows.length === 350000 && partial.rows.runs.length === 1, 'bounded cache without unrolling');
  equal(partial.missing(0, 800010), [[0, 100008]], 'evicted samples lose coverage');
  assert([...large.points()].length === 2, '400000 flat samples draw as two points');
  const issuance = [[100, 1000, 10], [110, 1200, 12], [110, 1300, 13]];
  const repaired = mergeIssuanceRows(issuance, {reset: false, rows: [
    [105, 1100, 11], [109, 1250, 12], [120, 1400, 14],
  ]}, 604800);
  equal(repaired, [[100, 1000, 10], [105, 1100, 11], [109, 1250, 12],
    [110, 1300, 13], [120, 1400, 14]], 'TI repairs older blocks and replaces by block, not timestamp');
  equal(issuance, [[100, 1000, 10], [110, 1200, 12], [110, 1300, 13]],
    'TI merge leaves the prior completed snapshot intact');
  equal(mergeIssuanceRows(repaired, {reset: false, rows: [[110, 1300, 13]]}, 604910),
    [[120, 1400, 14]], 'TI retention prunes old blocks even on an otherwise unchanged delta');
  equal(mergeIssuanceRows(repaired, {reset: true, rows: [[115, 1500, 15]]}, 604800),
    [[115, 1500, 15]], 'TI reset replaces the entire prior selected-window cache');
  equal(mergeIssuanceRows(repaired, {reset: true, rows: []}, 604800),
    [], 'an empty TI reset clears the cache');
}
