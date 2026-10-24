// Small DOM/canvas stand-ins let us exercise drawing and input without a browser.
const drawing = [];
function context(name) {
  return new Proxy({}, {
    get(target, key) {
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
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  },
  createElement: tag => element(`${tag}-${elementCount++}`),
  querySelector: selector => selector.includes('icon') ? {href: 'icon.svg'} : {content: 'old-version'},
  querySelectorAll: () => [],
  addEventListener() {},
  body: element('body'),
};
const window = {devicePixelRatio: 1, addEventListener() {}};
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
const setInterval = () => {};

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
  if (!coingecko) {
    checkPeg([[110, 1, .9999], [0, 130, 5, 1.0001]], [[120, true]], 'a block gap is not a crossing');
  }
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
  // Returning traces also supports comparisons against the pre-refactor renderer.
  return {initialDraw, hoverDraw, zoom: state.zoom};
}
