// Internal runs retain an absolute start, so clipping and cache merges never
// depend on another response. Individual samples are generated only on demand.
const sample = (run, i) => [run.t + i * run.dt, run.price, run.block === null ? null : run.block + i * run.db];
const lastTime = run => run.t + (run.count - 1) * run.dt;
const connected = (a, b) => a && b && a[2] !== null && b[2] !== null && b[2] === a[2] + 1;
function upperTime(run, time) {
  let lo = 0, hi = run.count;
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (run.t + mid * run.dt <= time) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}
function part(run, first, end) {
  const [t, , block] = sample(run, first);
  return {...run, t, block, count: end - first};
}

export class RangeSeries {
  constructor(runs = []) {
    this.runs = [];
    this.length = 0;
    this.low = null; this.high = null;
    for (const input of runs) {
      if (!input.count) continue;
      const run = {...input};
      const prev = this.runs.at(-1);
      // Coalesce exact integer-step runs at chunk boundaries and live overlaps.
      // Fractional timestamps keep their original anchors to avoid rounding drift.
      const dt = prev?.count === 1 ? run.t - prev.t : prev?.dt;
      const db = prev && run.block !== null && prev.block !== null ?
        (prev.count === 1 ? run.block - prev.block : prev.db) : null;
      if (prev && prev.price === run.price && db !== null &&
          [prev.t, run.t, dt, run.dt].every(Number.isSafeInteger) &&
          run.t === prev.t + prev.count * dt && run.block === prev.block + prev.count * db &&
          (run.count === 1 || (run.dt === dt && run.db === db))) {
        prev.dt = dt; prev.db = db; prev.count += run.count;
      } else {
        run.offset = this.length;
        this.runs.push(run);
      }
      this.length += run.count;
      const row = sample(run, 0);
      if (!this.low || row[1] < this.low[1]) this.low = row;
      if (!this.high || row[1] > this.high[1]) this.high = row;
    }
  }
  static fromWire(records) {
    if (!records.length) return new RangeSeries();
    let [t, block, price] = records[0];
    const runs = [{t, block, price, count: 1, dt: 0, db: 0}];
    for (const record of records.slice(1)) {
      const [count, dt, db, value] = record;
      if (count === 0) {
        t = dt; block = db; price = value;
        runs.push({t, block, price, count: 1, dt: 0, db: 0});
      } else {
        if (record.length === 4) price = value;
        const run = {t: t + dt, block: block + db, price, count, dt, db};
        runs.push(run);
        [t, , block] = sample(run, count - 1);
      }
    }
    return new RangeSeries(runs);
  }
  at(index) {
    if (index < 0) index += this.length;
    if (index < 0 || index >= this.length) return undefined;
    let lo = 0, hi = this.runs.length;
    while (lo + 1 < hi) {
      const mid = (lo + hi) >> 1;
      if (this.runs[mid].offset <= index) lo = mid;
      else hi = mid;
    }
    const run = this.runs[lo];
    return sample(run, index - run.offset);
  }
  window(start, end) {
    const runs = [];
    for (const run of this.runs) {
      if (lastTime(run) <= start) continue;
      if (run.t > end) break;
      const first = upperTime(run, start), stop = upperTime(run, end);
      if (first < stop) runs.push(part(run, first, stop));
    }
    return new RangeSeries(runs);
  }
  tail(count) {
    const first = Math.max(0, this.length - count);
    return new RangeSeries(this.runs.filter(run => run.offset + run.count > first)
      .map(run => part(run, Math.max(0, first - run.offset), run.count)));
  }
  nearest(time) {
    if (!this.length) return null;
    let lo = 0, hi = this.runs.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (lastTime(this.runs[mid]) < time) lo = mid + 1;
      else hi = mid;
    }
    if (lo === this.runs.length) return this.length - 1;
    const run = this.runs[lo];
    const next = Math.min(this.length - 1, run.offset + upperTime(run, time));
    const before = this.at(next - 1), after = this.at(next);
    return next > 0 && time - before[0] < after[0] - time ? next - 1 : next;
  }
  *points() {
    let previous;
    for (const run of this.runs) {
      const first = sample(run, 0);
      yield {row: first, move: !connected(previous, first)};
      if (run.count > 1 && run.db === 1 && run.block !== null) {
        yield {row: sample(run, run.count - 1), move: false};
      } else {
        for (let i = 1; i < run.count; i++) yield {row: sample(run, i), move: true};
      }
      previous = sample(run, run.count - 1);
    }
  }
}

export class BrowserHistory {
  constructor() { this.rows = new RangeSeries(); this.coverage = []; this.fetchedAt = null; this.checkedAt = 0; }
  expire(now) {
    const cutoff = now - 604800;
    this.rows = this.rows.window(cutoff, Infinity);
    this.coverage = this.coverage.filter(([, end]) => end > cutoff).map(([start, end]) => [Math.max(start, cutoff), end]);
    if (now - this.checkedAt >= 60) { this.coverage = []; this.checkedAt = now; }
  }
  merge(rows, start, end, fetchedAt, complete = true) {
    // Each response is authoritative for (start, end], including empty windows.
    this.rows = new RangeSeries([
      ...this.rows.window(-Infinity, start).runs,
      ...rows.runs,
      ...this.rows.window(end, Infinity).runs,
    ]).tail(350000);
    const intervals = [...this.coverage, ...(complete ? [[start, end]] : [])].sort((a, b) => a[0] - b[0]);
    this.coverage = [];
    for (const interval of intervals) {
      const previous = this.coverage.at(-1);
      if (previous && interval[0] <= previous[1]) previous[1] = Math.max(previous[1], interval[1]);
      else this.coverage.push([...interval]);
    }
    this.fetchedAt = fetchedAt;
  }
  missing(start, end) {
    const gaps = []; let cursor = start;
    for (const [left, right] of this.coverage) {
      if (right <= cursor || left >= end) continue;
      if (left > cursor) gaps.push([cursor, Math.min(left, end)]);
      cursor = Math.max(cursor, right);
      if (cursor >= end) break;
    }
    if (cursor < end) gaps.push([cursor, end]);
    return gaps;
  }
}

export function* pixelRows(rows, x) {
  let bucket = [], pixel;
  function* flush() {
    if (!bucket.length) return;
    let low = 0, high = 0;
    for (let i = 1; i < bucket.length; i++) {
      if (bucket[i].row[1] < bucket[low].row[1]) low = i;
      if (bucket[i].row[1] > bucket[high].row[1]) high = i;
    }
    for (const i of [...new Set([0, low, high, bucket.length - 1])].sort((a, b) => a - b)) yield bucket[i];
  }
  for (const point of rows.points()) {
    const nextPixel = Math.floor(x(point.row[0]));
    if (point.move || nextPixel !== pixel) {
      yield* flush(); bucket = [];
    }
    bucket.push(point); pixel = nextPixel;
  }
  yield* flush();
}
