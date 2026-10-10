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
  equal(history.missing(0, 210), [[0, 210]], 'periodic revalidation');
  const partial = new BrowserHistory();
  partial.merge(old, 0, 210, 1, false);
  equal(partial.missing(0, 210), [[0, 210]], 'warming response is retried');
  const large = new RangeSeries([{t: 10, price: 1, block: 1, count: 400000, dt: 2, db: 1}]);
  partial.merge(large, 0, 800010, 2);
  assert(partial.rows.length === 350000 && partial.rows.runs.length === 1, 'bounded cache without unrolling');
  assert([...large.points()].length === 2, '400000 flat samples draw as two points');
}
