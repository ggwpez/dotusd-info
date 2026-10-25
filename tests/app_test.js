// Small DOM/canvas stand-ins let us exercise drawing and input without a browser.
const drawing = [];
function context(name) {
  return new Proxy({}, {
    get(target, key) {
      if (key === 'measureText') return text => ({width: text.length * 6});
      if (key === 'createLinearGradient') {
        return (...args) => {
          drawing.push([name, key, ...args]);
          return {addColorStop: (...stop) => drawing.push(['gradient', ...stop])};
        };
      }
      return target[key] ?? ((...args) => drawing.push([name, key, ...args]));
    },
    set(target, key, value) {
      target[key] = value;
      drawing.push([name, key, value]);
      return true;
    },
  });
}
let elementCount = 0;
function element(name) {
  const drawingContext = context(name);
  return {
    name, width: 0, height: 0, hidden: false, dataset: {}, style: {}, children: [], events: {},
    classList: {toggle() {}},
    getContext: () => drawingContext,
    getBoundingClientRect: () => ({left: 0, width: 800, height: 400}),
    addEventListener(type, handler) { this.events[type] = handler; },
    setAttribute() {}, removeAttribute() {}, setPointerCapture() {}, releasePointerCapture() {},
    replaceChildren() { this.children = []; },
    append(...children) { this.children.push(...children); },
  };
}
const elements = new Map();
const document = {
  events: {},
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  },
  createElement: tag => element(`${tag}-${elementCount++}`),
  querySelector: selector => selector.includes('icon') ? {href: 'icon.svg'} : {content: 'old-version'},
  querySelectorAll: () => [],
  addEventListener(type, handler) { this.events[type] = handler; },
  body: element('body'),
};
const window = {
  devicePixelRatio: 1, events: {},
  addEventListener(type, handler) { this.events[type] = handler; },
};
const navigator = {onLine: true};
const location = {search: '', reload() {}};
class URLSearchParams {
  get() { return null; }
  has() { return false; }
}
class ResizeObserver { observe() {} }
class Path2D {
  constructor() { this.segments = []; }
  moveTo(...args) { this.segments.push(['moveTo', ...args]); drawing.push(['path', 'moveTo', ...args]); }
  lineTo(...args) { this.segments.push(['lineTo', ...args]); drawing.push(['path', 'lineTo', ...args]); }
  closePath() { drawing.push(['path', 'closePath']); }
}
const requestAnimationFrame = () => 1;
const cancelAnimationFrame = () => {};
const intervals = [];
const setInterval = (callback, delay) => intervals.push({callback, delay});

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

function historicStream(ti, end, ranges = [], treasury) {
  const started = deferred(), finished = deferred();
  let first = true;
  const reader = {
    async read() {
      if (first) {
        first = false;
        return {done: false, value: JSON.stringify({
          encoding: 'ranges-stream-v1', fetchedAt: end, historyReady: true, extremes: null, ti, treasury,
        }) + '\n' + JSON.stringify({ranges}) + '\n'};
      }
      started.resolve();
      return finished.promise;
    },
    async cancel() {},
    releaseLock() {},
  };
  return {
    started: started.promise,
    response: {ok: true, headers: {get: key => key === 'X-History-End' ? String(end) : APP_VERSION},
      body: {getReader: () => reader}},
    finish(messages = [{done: true}]) {
      finished.resolve({done: true, value: messages.map(message => JSON.stringify(message) + '\n').join('')});
    },
  };
}

function checkApp() {
  displayScale = 'bps';
  applyScale();
  function assert(value, message) {
    if (!value) throw new Error(message);
  }
  let refreshes = 0, urlUpdates = 0;
  refresh = () => refreshes++;
  writeViewUrl = () => urlUpdates++;
  reloading = false;
  state.start = 100;
  state.end = 200;
  state.rows = RangeSeries.fromWire([[110, 1, 1], [1, 20, 1, 1.001], [0, 180, 5, 0.999]]);
  state.extremes = {high: [130, 1.001, 2], low: [180, 0.999, 5]};
  drawing.length = 0;
  draw();
  assert(canvas.width === 1600 && canvas.height === 800, 'high-resolution canvas');
  assert(drawing.some(call => call[1] === 'lineTo'), 'price paths are drawn');
  const initialDraw = drawing.slice();

  drawing.length = 0;
  state.hover = 1;
  draw();
  assert(drawing.some(call => call[1] === 'drawImage'), 'hover reuses the cached chart');
  assert(!drawing.some(call => call[1] === 'createLinearGradient'), 'hover does not rebuild price paths');
  const tip = elements.get('tooltip');
  assert(!tip.hidden && tip.children[1].textContent === '+10.00 bps', 'hover shows sample deviation');
  const hoverDraw = drawing.slice();

  showIssuance = true;
  issuanceRows = [[110, 1000000, 1], [110, 1307938.731358, 2], [180, 2000000, 5]];
  treasuryRows = [[110, 100, 1], [110, 0, 2], [180, 200, 5]];
  draw();
  assert(tip.children[3].textContent === 'TI: 1.31M dotUSD',
    'hover matches issuance by block despite estimated or duplicate timestamps');
  assert(tip.children[4].textContent === 'Treasury: 0 dotUSD',
    'Treasury matches the exact block despite timestamps and displays zero');
  assert(tip.children[4].style.color === treasuryColor && treasuryColor !== issuanceColor,
    'Treasury tooltip identifies its distinct line color');
  issuanceRows = [[110, 1000000, 1], [180, 2000000, 5]];
  draw();
  assert(tip.children[3].textContent === 'TI: unavailable', 'hover does not bridge issuance gaps');
  treasuryRows = [[110, 100, 1], [130, 200, 3]];
  draw();
  assert(tip.children[4].textContent === 'Treasury: unavailable',
    'Treasury never substitutes nearest timestamp or block');
  treasuryRows = [];
  issuanceRows = [];
  draw();
  assert(tip.children[3].textContent === 'TI: unavailable', 'hover handles unloaded issuance');
  assert(tip.children[4].textContent === 'Treasury: unavailable', 'hover handles unloaded Treasury');
  showIssuance = false;
  draw();
  assert(tip.children.length === 3, 'disabled issuance leaves the price-only tooltip');

  canvas.events.keydown({key: 'ArrowRight', preventDefault() {}});
  assert(state.hover === 2, 'keyboard moves to next sample');
  canvas.events.keydown({key: 'ArrowRight', preventDefault() {}});
  assert(state.hover === 2, 'keyboard stops at the final sample');
  canvas.events.pointerleave();
  assert(state.hover === null, 'leaving clears hover');

  canvas.events.pointerdown({button: 0, clientX: 200, pointerId: 1});
  canvas.events.pointermove({clientX: 204});
  canvas.events.pointerup({pointerId: 1});
  assert(state.zoom === null && refreshes === 0, 'small drags do not zoom');
  canvas.events.pointerdown({button: 0, clientX: 600, pointerId: 1});
  canvas.events.pointermove({clientX: 200});
  canvas.events.pointerup({pointerId: 1});
  assert(state.zoom[0] < state.zoom[1] && !state.live, 'reverse drags produce a historical window');
  assert(refreshes === 1 && urlUpdates === 1 && state.drag === null, 'zoom updates URL and refreshes once');

  state.rows = new RangeSeries();
  state.extremes = null;
  state.hover = null;
  chartDirty = true;
  draw();
  assert(tip.hidden, 'empty chart hides the tooltip');
  function checkPeg(ranges, checks, message) {
    state.rows = RangeSeries.fromWire(ranges);
    state.extremes = null;
    chartDirty = true;
    drawing.length = 0;
    draw();
    const peg = drawing.find(call => call[1] === 'stroke' && call[2] instanceof Path2D)[2];
    for (const [time, visible] of checks) {
      const px = geometry.x(time);
      const drawn = peg.segments.some((segment, i) =>
        segment[0] === 'lineTo' && peg.segments[i - 1][1] <= px && px <= segment[1]);
      assert(drawn === visible, `${message} at ${time}`);
    }
  }
  checkPeg([[110, 1, 1], [1, 20, 1, 1]], [[105, true], [120, false], [150, true]], 'flat stroke masks only its own span');
  checkPeg([[110, 1, 1.000001]], [[105, true], [110, false], [150, true]], 'near-peg point masks locally');
  checkPeg([[110, 1, .9999], [1, 20, 1, 1.0001]], [[112, true], [120, false], [128, true]], 'crossing leaves reference visible on both sides');
  checkPeg([[110, 1, 1.0001], [1, 20, 1, 1.0002]], [[105, true], [120, true], [150, true]], 'fill does not mask reference');
  checkPeg([[110, 1, .9999], [0, 130, 5, 1.0001]], [[120, true]], 'a block gap is not a crossing');
  checkPeg([], [[105, true], [150, true], [195, true]], 'empty chart retains reference');
  elements.get('axis-scale').events.change({target: {checked: true}});
  assert(axisPosition(0, 10000) === .5, 'log axis keeps the peg finite and centered');
  assert(axisPosition(10, 10000) < .4, 'log axis exposes small deviations beside outliers');
  assert(Math.abs(axisPosition(-10, 10000) + axisPosition(10, 10000) - 1) < 1e-12, 'log axis treats both sides symmetrically');
  for (const peak of [0, .1, 5, 100, 10000]) {
    const {extent, ticks} = chartScale(peak);
    assert(ticks.includes(0), 'log ticks retain the peg');
    assert(ticks.every((tick, i) => Number.isFinite(axisPosition(tick, extent)) &&
      (i === 0 || tick > ticks[i - 1])), 'log ticks stay finite and ordered');
  }
  checkPeg([[110, 1, .99], [1, 20, 1, 1.01]], [[112, true], [120, false], [128, true]], 'log stroke intersections preserve reference');
  const prices = state.rows;
  const beforeScaleRefreshes = refreshes;
  elements.get('axis-scale').events.change({target: {checked: false}});
  assert(state.rows === prices && refreshes === beforeScaleRefreshes, 'axis changes preserve prices without fetching');
  showIssuance = true;
  assert(issuanceNumber(1760000000) === '1.76B' && issuanceNumber(1307938.731358) === '1.31M',
    'issuance uses three significant digits with compact suffixes');
  issuanceRows = [[110, 1000000, 1], [120, 1000000, 1801], [180, 1000000, 3602]];
  state.start = 100;
  state.end = 200;
  chartDirty = true;
  drawing.length = 0;
  draw();
  assert(geometry.right < 780, 'issuance reserves a separate right axis');
  const blueStart = drawing.findIndex(call => call[1] === 'strokeStyle' && call[2] === issuanceColor);
  const supplyPath = drawing.slice(blueStart).filter(call => ['moveTo', 'lineTo'].includes(call[1]));
  assert(supplyPath.length === 3 && supplyPath[0][1] === 'moveTo' &&
    supplyPath[1][1] === 'lineTo' && supplyPath[2][1] === 'moveTo',
    'issuance connects 1800-block samples but not wider gaps');
  assert(supplyPath.every(call => Number.isFinite(call[3]) && call[3] === supplyPath[0][3]),
    'constant issuance has a finite horizontal line');
  treasuryRows = [[110, 0, 1], [120, 1000000, 1801], [180, 2000000, 3602]];
  drawing.length = 0;
  drawIssuance(geometry);
  const purpleStart = drawing.findIndex(call => call[1] === 'strokeStyle' && call[2] === treasuryColor);
  const treasuryPath = drawing.slice(purpleStart).filter(call => ['moveTo', 'lineTo'].includes(call[1]));
  assert(treasuryPath.length === 3 && treasuryPath[0][1] === 'moveTo' &&
    treasuryPath[1][1] === 'lineTo' && treasuryPath[2][1] === 'moveTo',
    'Treasury connects exactly 1800-block gaps but not wider gaps');
  assert(treasuryPath.every(call => Number.isFinite(call[3]) &&
    call[3] >= geometry.top && call[3] <= geometry.bottom),
    'shared linear axis includes Treasury zero and values above TI');
  const sharedSupply = drawing.slice(0, purpleStart).filter(call => ['moveTo', 'lineTo'].includes(call[1]));
  assert(sharedSupply.every(call => call[3] === treasuryPath[1][3]) &&
    treasuryPath[0][3] > treasuryPath[1][3] && treasuryPath[1][3] > treasuryPath[2][3],
    'identical TI and Treasury balances share exactly the same linear dotUSD coordinate');
  issuanceRows = [];
  treasuryRows = [[110, 0, 1], [120, 0, 2]];
  drawing.length = 0;
  drawIssuance(geometry);
  const zeros = drawing.filter(call => ['moveTo', 'lineTo'].includes(call[1]));
  assert(zeros.length === 2 && zeros.every(call => Number.isFinite(call[3]) && call[3] === geometry.bottom),
    'Treasury-only all-zero history is finite at the axis baseline');
  state.start = 300;
  state.end = 400;
  drawing.length = 0;
  drawIssuance(geometry);
  assert(!drawing.some(call => ['moveTo', 'lineTo', 'arc'].includes(call[1])),
    'neither series is extrapolated into missing history');
  showIssuance = false;
  // Returning traces also supports comparisons against the pre-refactor renderer.
  return {initialDraw, hoverDraw, zoom: state.zoom};
}
