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
  moveTo(...args) { drawing.push(['path', 'moveTo', ...args]); }
  lineTo(...args) { drawing.push(['path', 'lineTo', ...args]); }
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
  function checkPeg(ranges, visible, message) {
    state.rows = RangeSeries.fromWire(ranges);
    state.extremes = null;
    chartDirty = true;
    drawing.length = 0;
    draw();
    assert(drawing.some(call => call[1] === 'strokeStyle' && call[2] === '#ffffff') === visible, message);
  }
  checkPeg([[110, 1, 1], [1, 20, 1, 1]], false, 'flat peg prices hide the white line');
  checkPeg([[110, 1, 1.000001]], false, 'subpixel proximity hides the white line');
  checkPeg([[110, 1, .9999], [1, 20, 1, 1.0001]], false, 'connected crossing hides the white line');
  checkPeg([[110, 1, 1.0001], [1, 20, 1, 1.0002]], true, 'separated prices retain the white line');
  if (!coingecko) {
    checkPeg([[110, 1, .9999], [0, 130, 5, 1.0001]], true, 'a block gap is not a crossing');
  }
  checkPeg([], true, 'empty chart retains the peg reference');
  // Returning traces also supports comparisons against the pre-refactor renderer.
  return {initialDraw, hoverDraw, zoom: state.zoom};
}
