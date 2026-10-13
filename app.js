import {RangeSeries, BrowserHistory, pixelRows, readPriceStream} from './ranges.js?v=__APP_VERSION__';
const $ = id => document.getElementById(id);
const coingecko = document.body.dataset.priceSource === 'coingecko';
const source = coingecko ? {
  endpoint: '/api/coingecko/prices', currency: 'USDT', pollSeconds: 6,
  staleFetch: 30, stalePrice: 30, sampleLabel: 'finalized Hub sample',
  empty: 'No finalized Hub samples available in this window.',
} : {
  endpoint: '/api/prices', currency: 'USDT', pollSeconds: 6,
  staleFetch: 30, stalePrice: 180, sampleLabel: 'finalized',
  empty: 'No price samples in this window',
};
// Aggregated prices have timestamps, not block numbers. Break the line when
// more than one expected interval is missing; retain block-gap rules for pools.
const connectSamples = coingecko ? (a, b) => a && b && b[0] > a[0] &&
  b[0] - a[0] <= 900 : undefined;
$('title-logo').src = document.querySelector('link[rel="icon"]').href;
// Replaced by the server from the same release snapshot as the served assets.
const APP_VERSION = '__APP_VERSION__';
let reloading = false;
function acceptVersion(version) {
  if (reloading) return false;
  if (!version || version === APP_VERSION) return true;
  reloading = true;
  controller?.abort();
  generation++;
  location.reload();
  return false;
}
const resolutions = new Map([['15m', 900], ['1h', 3600], ['6h', 21600], ['24h', 86400], ['7d', 604800]]);
function linkedView() {
  const params = new URLSearchParams(location.search);
  const range = resolutions.get(params.get('resolution')) ?? 86400;
  const start = Number(params.get('start')), end = Number(params.get('end'));
  const valid = params.has('start') && params.has('end') &&
    params.get('start').trim() !== '' && params.get('end').trim() !== '' &&
    Number.isFinite(start) && Number.isFinite(end) && start >= 0 &&
    end - start >= 1 && end - start <= 604800 && end <= Date.now()/1000 + 60;
  return {range, zoom: valid ? [start, end] : null};
}
const initialView = linkedView();
const canvas = $('chart'), ctx = canvas.getContext('2d');
const state = {
  rows: new RangeSeries(),
  range: initialView.range,
  start: 0,
  end: 0,
  live: !initialView.zoom,
  zoom: initialView.zoom,
  hover: null,
  drag: null,
  error: null,
  fetchedAt: null,
  historyLoading: false,
  serverWarming: false,
  extremes: null,
};
let controller, generation = 0, geometry, loading = false;
let summaryValues = null;
const backing = document.createElement('canvas');
let chartDirty = true, drawFrame = null;
function scheduleDraw() {
  if (drawFrame === null) drawFrame = requestAnimationFrame(draw);
}
const signed = v => `${v > 0 ? '+' : ''}${v.toFixed(2)}`;
// Display units never alter stored prices, rating thresholds, or subscriptions.
let displayScale = 'dollars';
try {
  const saved = localStorage.getItem('dotusd-display-scale');
  if (['bps', 'percent', 'dollars'].includes(saved)) displayScale = saved;
} catch {}
function formatScale(bps, unit = false, axis = false) {
  if (displayScale === 'dollars') return '$' + (1 + bps / 10000).toFixed(axis ? 4 : 6);
  if (displayScale === 'percent') {
    const value = bps / 100;
    return (axis ? value.toFixed(2) : (value > 0 ? '+' : '') + value.toFixed(4)) + (unit ? '%' : '');
  }
  return (axis ? String(bps) : signed(bps)) + (unit ? ' bps' : '');
}
function ratingBand(index) {
  const threshold = value => displayScale === 'bps' ? value + ' bps' :
    displayScale === 'percent' ? (value / 100).toFixed(2) + '%' :
      '$' + (value / 10000).toFixed(4);
  const band = index === 0 ? '≤ ' + threshold(5) :
    index === 4 ? '> ' + threshold(100) :
      '> ' + threshold(ratingLimits[index - 1]) + '–' + threshold(ratingLimits[index]);
  return band + (displayScale === 'dollars' ? ' from $1' : '');
}
function applyScale() {
  $('display-scale').value = displayScale;
  document.body.dataset.displayScale = displayScale;
  $('metric-label').textContent = displayScale === 'dollars' ? 'dotUSD price' : 'Price deviation';
  $('metric-unit').textContent = displayScale === 'bps' ? 'bps' : displayScale === 'percent' ? '%' : '';
  $('range-unit').textContent = displayScale === 'bps' ? 'Basis points' : displayScale === 'percent' ? 'Percent' : 'Dollar price (USDT)';
  $('chart-unit').textContent = $('range-unit').textContent.toLowerCase();
  $('chart-title').textContent = displayScale === 'dollars' ? 'dotUSD price' : 'Peg deviation';
  canvas.setAttribute('aria-label', 'dotUSD chart in ' + $('chart-unit').textContent +
    '. Drag to zoom; double-click to toggle fullscreen. Select a time range to reset zoom; Escape exits fullscreen. Arrow keys inspect samples.');
  document.querySelectorAll('[data-rating]').forEach(segment => {
    const index = Number(segment.dataset.rating);
    const label = ratingNames[index] + ': ' + ratingBand(index);
    segment.title = label;
    segment.setAttribute('aria-label', label);
  });
  chartDirty = true;
  summary();
  scheduleDraw();
}
const ratingColors = ['#73bf69', '#63c8ae', '#dfc464', '#f29b58', '#f2495c'];
const deviationColor = bps => ratingColors[ratingForBps(bps)] ?? '#b7c0cf';
const clock = t => new Date(t * 1000).toISOString().slice(11, 19);
const date = t => new Date(t * 1000).toISOString().replace('T', ' ').slice(0, 19);
function status(text, kind) {
  $('status').textContent = text;
  $('status').dataset.state = kind;
}
const ratingNames = ['Excellent', 'Good', 'Fair', 'Poor', 'Horrible'];
const ratingLimits = [5, 20, 50, 100];
let hoveredRating = null, focusedRating = null;
function ratingForBps(bps) {
  if (!Number.isFinite(bps)) return -1;
  const magnitude = Math.round(Math.abs(bps) * 1e6) / 1e6;
  const index = ratingLimits.findIndex(limit => magnitude <= limit);
  return index === -1 ? 4 : index;
}
function ratingGradient(context, top, bottom, extent, alpha = '') {
  const gradient = context.createLinearGradient(0, top, 0, bottom);
  const levels = [extent, ...[...ratingLimits].reverse(), ...ratingLimits.map(v => -v), -extent]
    .filter((v, i, levels) => i === 0 || i === levels.length - 1 || Math.abs(v) < extent);
  for (let i = 0; i < levels.length - 1; i++) {
    const color = deviationColor((levels[i] + levels[i + 1]) / 2) + alpha;
    gradient.addColorStop((extent - levels[i]) / (2 * extent), color);
    gradient.addColorStop((extent - levels[i + 1]) / (2 * extent), color);
  }
  return gradient;
}
function updateRating() {
  const last = summaryValues?.last;
  const bps = last ? (last[1] - 1) * 10000 : NaN;
  const current = ratingForBps(bps);
  const preview = last ? hoveredRating ?? focusedRating : null;
  const displayed = preview ?? current;
  document.querySelectorAll('[data-rating]').forEach(segment => {
    const active = Number(segment.dataset.rating) === current;
    segment.classList.toggle('is-current', active);
    segment.classList.toggle('is-preview', Number(segment.dataset.rating) === preview);
    if (active) segment.setAttribute('aria-current', 'true');
    else segment.removeAttribute('aria-current');
  });
  $('rating-value').textContent = displayed < 0 ? '-' : ratingNames[displayed];
  $('rating-caption').textContent = displayed < 0 ? 'Waiting for prices' : ratingBand(displayed);
}
function summary() {
  const rows = state.rows, last = rows.at(-1);
  const ready = summaryValues?.last;
  $('latest').textContent = ready ? formatScale((ready[1] - 1) * 10000) : '-';
  $('price').textContent = ready ? ready[1].toFixed(6) : '-';
  const low = ready ? (summaryValues.low[1]-1)*10000 : 0;
  const high = ready ? (summaryValues.high[1]-1)*10000 : 0;
  $('extremes').textContent = ready ? `${formatScale(low)} / ${formatScale(high)}` : '-';
  $('window').textContent = `${date(state.start)} — ${date(state.end)} · ${rows.length.toLocaleString()} samples${state.historyLoading ? ` · loading history…${state.serverWarming ? ' · server loading stored data' : ''}` : ''}`;
  $('message').textContent = state.error || (last ? '' : state.historyLoading ? 'Loading samples…' : source.empty);
  $('message').hidden = !state.error && !!last;
  updateFreshness();
}
function updateFreshness() {
  updateRating();
  const last = state.rows.at(-1);
  const now = Date.now()/1000;
  const age = last ? Math.max(0, Math.round(now - last[0])) : null;
  const sampleAge = last ? ` · last ${source.sampleLabel} ~${age}s ago` : '';
  if (!state.live || state.zoom) status('Historical view', 'paused');
  else if (navigator.onLine === false) status(`Offline${sampleAge}`, 'error');
  else if (state.error) status(`Connection error${sampleAge}`, 'error');
  else if (!last) status(coingecko ? 'Waiting for finalized Hub prices' : 'Waiting for finalized prices', 'stale');
  else if (!Number.isFinite(state.fetchedAt) || now - state.fetchedAt > source.staleFetch)
    status(`Updates delayed${sampleAge}`, 'stale');
  else if (age > source.stalePrice) status(`Data delayed${sampleAge}`, 'stale');
  else status(`● Live · ${source.sampleLabel} ~${age}s ago`, 'live');
}

const historyCache = new BrowserHistory();
let nextComparisonHistoryRefresh = 0;
async function refresh(force = false) {
  if (reloading) return;
  controller?.abort();
  controller = new AbortController();
  const ticket = ++generation;
  loading = true;
  const requestController = controller;
  const end = state.zoom?.[1] ?? (state.live || !state.end ? Date.now()/1000 : state.end);
  const start = state.zoom?.[0] ?? end - state.range;
  historyCache.expire(Date.now()/1000);
  const sameWindow = Math.abs((state.end - state.start) - (end - start)) < .01;
  state.start = start;
  state.end = end;
  state.error = null;
  state.extremes = null;
  const missing = historyCache.missing(start, end);
  const live = state.live && !state.zoom;
  const last = historyCache.rows.window(start, end).at(-1);
  // Advance only from a completed history snapshot; warming/backfills need the full window.
  const refreshHistory = coingecko && Date.now() >= nextComparisonHistoryRefresh;
  const incremental = live && !force && !state.serverWarming &&
    (coingecko ? !!last && !refreshHistory : Number.isSafeInteger(last?.[2])) &&
    !missing.some(([a]) => a < last[0]);
  const left = incremental ? Math.max(start, Math.min(last[0], end - 1)) : start;
  // Keep completed numbers visible during ordinary live polls.
  if (!(sameWindow && (live ? incremental : !missing.length))) summaryValues = null;
  const publish = rows => {
    state.rows = rows.window(start, end);
    state.hover = null;
    chartDirty = true;
    summary();
    draw();
  };
  state.historyLoading = missing.length > 0;
  state.fetchedAt = historyCache.fetchedAt;
  publish(historyCache.rows);
  let timeout;
  try {
    if (!force && !live && !missing.length && !refreshHistory) return;
    timeout = setTimeout(() => requestController.abort(), 15000);
    const response = await fetch(`${source.endpoint}?start=${coingecko ? left : start}&end=${end}&windowStart=${start}&windowEnd=${end}&format=stream${incremental && !coingecko ? `&afterBlock=${last[2]}` : ''}`, {signal: requestController.signal});
    if (!acceptVersion(response.headers.get('X-App-Version'))) return;
    if (!response.ok) {
      const result = await response.json();
      throw new Error(result.error || 'Unable to load prices');
    }
    const runs = [];
    let lastDraw = -Infinity;
    for await (const message of readPriceStream(response)) {
      if (ticket !== generation) return;
      if (message.encoding) {
        state.extremes = message.extremes;
        state.serverWarming = message.historyReady === false;
        state.fetchedAt = message.fetchedAt;
      } else if (message.ranges) {
        runs.push(...RangeSeries.fromWire(message.ranges).runs);
        if (performance.now() - lastDraw >= 32) {
          // Partial data is drawable, but only a finished stream marks cache coverage complete.
          publish(new RangeSeries([...historyCache.rows.window(-Infinity, left).runs, ...runs,
            ...historyCache.rows.window(end, Infinity).runs]));
          lastDraw = performance.now();
        }
      }
    }
    if (coingecko && !incremental) nextComparisonHistoryRefresh = Date.now() + 60000;
    historyCache.merge(new RangeSeries(runs), left, end, state.fetchedAt, !state.serverWarming);
    state.historyLoading = historyCache.missing(start, end).length > 0;
    publish(historyCache.rows);
  } catch (error) {
    if (ticket !== generation) return;
    state.error = error.name === 'AbortError' ? 'Request timed out. Retrying…' : error.message;
  } finally {
    clearTimeout(timeout);
    if (ticket === generation) {
      loading = false;
      state.historyLoading = historyCache.missing(start, end).length > 0;
      // A complete response can contain live prices while older history is still
      // loading or unavailable. Archive readiness must not hide the live metric.
      if (!state.error) {
        summaryValues = {last: state.rows.at(-1),
          low: state.extremes?.low ?? state.rows.low,
          high: state.extremes?.high ?? state.rows.high};
      }
      summary();
    }
  }
}
function chartScale(peak) {
  const padded = Math.max(5, peak + Math.max(peak * .12, .5));
  const magnitude = 10 ** Math.floor(Math.log10(padded));
  const extent = [1, 2, 5, 10].map(n => n * magnitude).find(n => n >= padded);
  const step = extent / (extent / magnitude === 2 ? 4 : 5);
  const divisions = Math.round(extent / step);
  return {extent, ticks: Array.from({length: divisions * 2 + 1}, (_, i) => (i - divisions) * step)};
}
function drawGrid({left, right, top, bottom, x, y}, width, yTicks) {
  ctx.font = '10px ui-monospace, monospace';
  ctx.lineWidth = 1;
  for (const value of yTicks) {
    const py = y(value);
    const boundaryColor = ratingLimits.includes(Math.abs(value)) ? deviationColor(value) : null;
    ctx.strokeStyle = boundaryColor ? boundaryColor + '50' : '#272d37';
    ctx.beginPath();
    ctx.moveTo(left, py);
    ctx.lineTo(right, py);
    ctx.stroke();
    ctx.fillStyle = boundaryColor ?? '#8792a3';
    ctx.textAlign = 'right';
    ctx.fillText(formatScale(value, false, true), left - 12, py + 3);
  }

  const ticks = width < 600 ? 3 : 6;
  for (let i = 0; i <= ticks; i++) {
    const time = state.start + (state.end - state.start) * i / ticks;
    const px = x(time);
    ctx.strokeStyle = '#202630';
    ctx.beginPath();
    ctx.moveTo(px, top);
    ctx.lineTo(px, bottom);
    ctx.stroke();
    ctx.fillStyle = '#8792a3';
    ctx.textAlign = i === 0 ? 'left' : i === ticks ? 'right' : 'center';
    const label = state.end - state.start > 86400 ? date(time).slice(5, 16) : clock(time).slice(0, 5);
    ctx.fillText(label, px, bottom + 23);
  }

  ctx.setLineDash([]);
  ctx.strokeStyle = '#ffffff';
  ctx.lineWidth = 0.6;
  ctx.beginPath();
  ctx.moveTo(left, y(0));
  ctx.lineTo(right, y(0));
  ctx.stroke();
}

function markExtreme(row, {x, y}) {
  const value = (row[1] - 1) * 10000;
  ctx.beginPath();
  ctx.arc(x(row[0]), y(value), 4, 0, Math.PI * 2);
  ctx.fillStyle = deviationColor(value);
  ctx.fill();
  ctx.strokeStyle = '#14181f';
  ctx.lineWidth = 2;
  ctx.stroke();
}

function drawPrices({left, right, top, bottom, x, y}, extent, high, low) {
  ctx.save();
  ctx.beginPath();
  ctx.rect(left, top, right - left, bottom - top);
  ctx.clip();
  // Symmetric hard stops color both sides by absolute peg deviation.
  const lineColor = ratingGradient(ctx, top, bottom, extent);
  // Fade the fill at wider time scales, independently of history loading progress.
  const secondsPerPixel = (state.end - state.start) / Math.max(1, right - left);
  const fillAlpha = Math.max(8, Math.round(32 / Math.sqrt(Math.max(1, secondsPerPixel / 6))));
  const areaColor = ratingGradient(ctx, top, bottom, extent, fillAlpha.toString(16).padStart(2, '0'));
  const area = new Path2D();
  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 1.1;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.beginPath();
  let previous;
  // Draw range endpoints, preserving pixel extrema and every real block gap.
  for (const {row, move} of pixelRows(state.rows, x, connectSamples)) {
    const px = x(row[0]), py = y((row[1] - 1) * 10000);
    if (move) {
      if (previous) {
        area.lineTo(x(previous[0]), y(0));
        area.closePath();
      }
      area.moveTo(px, y(0));
      ctx.moveTo(px, py);
    } else {
      ctx.lineTo(px, py);
    }
    area.lineTo(px, py);
    previous = row;
  }
  if (previous) {
    area.lineTo(x(previous[0]), y(0));
    area.closePath();
  }
  ctx.fillStyle = areaColor;
  ctx.fill(area);
  ctx.stroke();
  // New observation histories start with one point; make it visible immediately.
  const latest = coingecko ? state.rows.at(-1) : null;
  if (latest) {
    ctx.beginPath();
    ctx.fillStyle = deviationColor((latest[1] - 1) * 10000);
    ctx.arc(x(latest[0]), y((latest[1] - 1) * 10000), 3, 0, Math.PI * 2);
    ctx.fill();
  }
  if (high && low) {
    const highBps = Number(((high[1] - 1) * 10000).toFixed(2));
    const lowBps = Number(((low[1] - 1) * 10000).toFixed(2));
    if (highBps !== lowBps) {
      markExtreme(high, {x, y});
      markExtreme(low, {x, y});
    }
  }
  ctx.restore();
}

function drawOverlay({left, right, top, bottom, x, y}, width, height) {
  ctx.save();
  ctx.beginPath();
  ctx.rect(left, top, right - left, bottom - top);
  ctx.clip();
  if (state.drag) {
    ctx.fillStyle = '#ec83b122';
    ctx.fillRect(Math.min(...state.drag), top, Math.abs(state.drag[1] - state.drag[0]), bottom - top);
  }
  const tip = $('tooltip');
  tip.hidden = true;
  const row = state.hover === null ? null : state.rows.at(state.hover);
  if (row) {
    const bps = (row[1] - 1) * 10000;
    const px = x(row[0]), py = y(bps);
    ctx.strokeStyle = '#7e899b';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(px, top);
    ctx.lineTo(px, bottom);
    ctx.moveTo(left, py);
    ctx.lineTo(right, py);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = deviationColor(bps);
    ctx.beginPath();
    ctx.arc(px, py, 3, 0, Math.PI * 2);
    ctx.fill();

    tip.hidden = false;
    tip.replaceChildren();
    const stamp = document.createElement('div');
    stamp.textContent = `${date(row[0])} UTC`;
    const value = document.createElement('strong');
    value.textContent = formatScale(bps, true);
    value.style.color = deviationColor(bps);
    const price = document.createElement('div');
    price.textContent = `${row[1].toFixed(8)} ${source.currency}`;
    tip.append(stamp, value, price);
    tip.style.left = `${Math.max(0, Math.min(px + 16, width - 230))}px`;
    tip.style.top = `${Math.max(0, Math.min(py + 16, height - 100))}px`;
  }
  ctx.restore();
}

function draw() {
  if (drawFrame !== null) {
    cancelAnimationFrame(drawFrame);
    drawFrame = null;
  }
  // Render at least two pixels per CSS pixel for smoother thin lines.
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.max(2, window.devicePixelRatio || 1);
  const width = Math.round(rect.width * dpr), height = Math.round(rect.height * dpr);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
    chartDirty = true;
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (chartDirty) {
    ctx.lineCap = 'butt';
    ctx.lineJoin = 'miter';
    ctx.setLineDash([]);
    let peak = 0;
    let high = state.extremes?.high, low = state.extremes?.low;
    if (high && low) peak = Math.max(Math.abs((high[1] - 1) * 10000), Math.abs((low[1] - 1) * 10000));
    for (const row of [state.rows.low, state.rows.high].filter(Boolean)) {
      peak = Math.max(peak, Math.abs((row[1] - 1) * 10000));
      if (!high || row[1] > high[1]) high = row;
      if (!low || row[1] < low[1]) low = row;
    }
    const {extent, ticks} = chartScale(peak);
    const left = displayScale === 'dollars' ? 82 : 65, right = rect.width - 20, top = 18, bottom = rect.height - 35;
    const x = time => left + (time - state.start) / (state.end - state.start || 1) * (right - left);
    const y = value => bottom - (value + extent) / (2 * extent) * (bottom - top);
    geometry = {left, right, top, bottom, x, y};
    drawGrid(geometry, rect.width, ticks);
    drawPrices(geometry, extent, high, low);

    if (backing.width !== width || backing.height !== height) {
      backing.width = width;
      backing.height = height;
    }
    const backingContext = backing.getContext('2d');
    backingContext.clearRect(0, 0, width, height);
    backingContext.drawImage(canvas, 0, 0);
    chartDirty = false;
  } else {
    ctx.drawImage(backing, 0, 0, rect.width, rect.height);
  }
  drawOverlay(geometry, rect.width, rect.height);
}

function position(event) {
  const px = event.clientX - canvas.getBoundingClientRect().left;
  return Math.max(geometry.left, Math.min(geometry.right, px));
}

function timeAtPixel(px) {
  return state.start + (px - geometry.left) / (geometry.right - geometry.left) * (state.end - state.start);
}

function zoomToSelection(startPixel, endPixel) {
  if (Math.abs(startPixel - endPixel) <= 8) return;
  const start = timeAtPixel(Math.min(startPixel, endPixel));
  const end = timeAtPixel(Math.max(startPixel, endPixel));
  if (end - start < 1) return;
  state.zoom = [start, end];
  state.live = false;
  writeViewUrl();
  updateViewControls();
  refresh();
}

canvas.addEventListener('pointermove', event => {
  if (!geometry || !state.rows.length) return;
  const px = position(event);
  if (state.drag) state.drag[1] = px;
  state.hover = state.rows.nearest(timeAtPixel(px));
  scheduleDraw();
});
canvas.addEventListener('pointerleave', () => {
  if (!state.drag) {
    state.hover = null;
    scheduleDraw();
  }
});
canvas.addEventListener('pointerdown', event => {
  if (!geometry || event.button !== 0) return;
  state.drag = [position(event), position(event)];
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener('pointerup', event => {
  if (!state.drag) return;
  const [start, end] = state.drag;
  state.drag = null;
  zoomToSelection(start, end);
  canvas.releasePointerCapture(event.pointerId);
  draw();
});
canvas.addEventListener('pointercancel', () => {
  state.drag = null;
  draw();
});

function writeViewUrl() {
  const url = new URL(location.pathname, location.origin);
  const resolution = [...resolutions].find(([, seconds]) => seconds === state.range)[0];
  url.searchParams.set('resolution', resolution);
  if (state.zoom) {
    url.searchParams.set('start', String(state.zoom[0]));
    url.searchParams.set('end', String(state.zoom[1]));
  }
  if (url.href !== location.href) history.pushState(null, '', url);
}
function updateViewControls() {
  $('compare-source').href = (coingecko ? '/' : '/coingecko') + location.search;
  $('live').hidden = !state.live || !!state.zoom;
  $('live').dataset.live = String(state.live);
  $('live-label').textContent = state.live ? `${source.pollSeconds}s` : 'Refresh';
  $('live').setAttribute('aria-label', state.live ? `Refresh prices; automatic updates every ${source.pollSeconds} seconds` : 'Refresh prices');
  $('live').title = 'Refresh prices';
}
function reset() {
  state.zoom = null;
  state.live = true;
  state.start = 0;
  state.end = 0;
  writeViewUrl();
  updateViewControls();
  refresh();
}
const chartPanel = $('chart-panel');
let chartFullscreen = false;
function updateFullscreen() {
  const active = chartFullscreen;
  chartPanel.classList.toggle('is-fullscreen', active);
  document.body.classList.toggle('chart-fullscreen', active);
  $('fullscreen').setAttribute('aria-pressed', String(active));
  $('fullscreen').textContent = active ? 'Exit fullscreen' : 'Fullscreen';
  state.drag = null;
  state.hover = null;
  chartDirty = true;
  requestAnimationFrame(draw);
}
function toggleFullscreen() {
  chartFullscreen = !chartFullscreen;
  updateFullscreen();
}
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && chartFullscreen) {
    chartFullscreen = false;
    updateFullscreen();
  }
});
canvas.addEventListener('dblclick', toggleFullscreen);
$('fullscreen').onclick = toggleFullscreen;
canvas.addEventListener('keydown', event => {
  if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
  event.preventDefault();
  const current = state.hover ?? state.rows.length - 1;
  const step = event.key === 'ArrowRight' ? 1 : -1;
  state.hover = Math.max(0, Math.min(state.rows.length - 1, current + step));
  draw();
});
function updateRangeButtons() {
  document.querySelectorAll('[data-range]').forEach(button =>
    button.setAttribute('aria-pressed', String(Number(button.dataset.range) === state.range)));
}
document.querySelectorAll('[data-range]').forEach(button => button.onclick = () => {
  state.range = Number(button.dataset.range);
  updateRangeButtons();
  reset();
});
window.addEventListener('popstate', () => {
  const view = linkedView();
  state.range = view.range;
  state.zoom = view.zoom;
  state.live = !view.zoom;
  state.start = 0;
  state.end = 0;
  updateRangeButtons();
  updateViewControls();
  refresh();
});
$('display-scale').addEventListener('change', event => {
  displayScale = event.target.value;
  try { localStorage.setItem('dotusd-display-scale', displayScale); } catch {}
  applyScale();
});
applyScale();
updateRangeButtons();
updateViewControls();
$('live').onclick = () => refresh(true);
document.querySelectorAll('[data-rating]').forEach(segment => {
  const index = Number(segment.dataset.rating);
  segment.addEventListener('pointerenter', () => { hoveredRating = index; updateRating(); });
  segment.addEventListener('pointerleave', () => { hoveredRating = null; updateRating(); });
  segment.addEventListener('focus', () => { focusedRating = index; updateRating(); });
  segment.addEventListener('blur', () => { focusedRating = null; updateRating(); });
  segment.addEventListener('keydown', event => {
    if (event.key === 'Escape') { hoveredRating = null; segment.blur(); updateRating(); }
  });
});
new ResizeObserver(draw).observe($('plot'));
function needsRefresh() {
  return (state.live && !state.zoom) || state.historyLoading ||
    (coingecko && Date.now() >= nextComparisonHistoryRefresh);
}
setInterval(() => {
  if (needsRefresh() && !document.hidden && !loading) refresh();
}, source.pollSeconds * 1000);
// Age the last received timestamps independently of network requests or chart redraws.
setInterval(updateFreshness, 1000);
window.addEventListener('offline', updateFreshness);
window.addEventListener('online', () => {
  updateFreshness();
  if (needsRefresh()) refresh();
});
document.addEventListener('visibilitychange', () => {
  updateFreshness();
  if (!document.hidden && needsRefresh()) refresh();
});
if (acceptVersion(document.querySelector('meta[name="app-version"]')?.content)) {
  // Every price response checks the version, including when live updates resume.
  refresh();
}
