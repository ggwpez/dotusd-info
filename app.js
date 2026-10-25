import {RangeSeries, BrowserHistory, pixelRows, readPriceStream, mergeIssuanceRows} from './ranges.js?v=__APP_VERSION__';
const $ = id => document.getElementById(id);
const source = {
  endpoint: '/api/historic', currency: 'USDT', pollSeconds: 6,
  staleFetch: 30, stalePrice: 180, sampleLabel: 'finalized',
  empty: 'No price samples in this window',
};
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
  const scale = params.get('scale'), axis = params.get('axis');
  return {range, zoom: valid ? [start, end] : null,
    scale: ['bps', 'percent', 'dollars'].includes(scale) ? scale : null,
    axis: ['linear', 'log'].includes(axis) ? axis : null,
    issuance: params.get('ti') === '1' ? true : params.get('ti') === '0' ? false : null};
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
  liveLoading: false,
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
let axisScale = 'linear';
try {
  if (localStorage.getItem('dotusd-axis-scale') === 'log') axisScale = 'log';
} catch {}
let showIssuance = false;
try { showIssuance = localStorage.getItem('dotusd-issuance-axis') === 'true'; } catch {}
const preferredIssuance = showIssuance;
showIssuance = initialView.issuance ?? preferredIssuance;
let issuanceRows = [], issuanceCursor = null, issuanceCoverage = null;
const issuanceColor = '#82b7ff';
const issuanceFormat = new Intl.NumberFormat('en-US', {notation: 'compact', maximumSignificantDigits: 3});
const issuanceNumber = value => issuanceFormat.format(value);
const preferredScale = displayScale, preferredAxis = axisScale;
displayScale = initialView.scale ?? preferredScale;
axisScale = initialView.axis ?? preferredAxis;
function axisPosition(value, extent) {
  // Signed log1p keeps the peg and negative deviations finite (1 bps soft knee).
  if (axisScale === 'log') {
    return (1 - Math.sign(value) * Math.log1p(Math.abs(value)) / Math.log1p(extent)) / 2;
  }
  return (1 - value / extent) / 2;
}
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
  $('axis-scale').checked = axisScale === 'log';
  document.body.dataset.displayScale = displayScale;
  $('metric-label').textContent = displayScale === 'dollars' ? 'dotUSD price' : 'Price deviation';
  $('metric-unit').textContent = displayScale === 'bps' ? 'bps' : displayScale === 'percent' ? '%' : '';
  $('range-unit').textContent = displayScale === 'bps' ? 'Basis points' : displayScale === 'percent' ? 'Percent' : 'Dollar price (USDT)';
  const chartUnit = $('range-unit').textContent.toLowerCase() +
    (axisScale === 'log' ? ' · symmetric log axis' : '');
  $('chart-title').textContent = displayScale === 'dollars' ? 'dotUSD price' : 'Peg deviation';
  canvas.setAttribute('aria-label', 'dotUSD chart in ' + chartUnit +
    (showIssuance ? '. Total dotUSD issuance on the right linear axis' : '') +
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
const ratingColors = ['#19ac4f', '#63c8ae', '#dfc464', '#f29b58', '#f2495c'];
// Keep the peg green, then blend toward each successive color as deviation grows.
// The open-ended red band reaches its full color at 150 bps.
const colorLevels = [5, 20, 50, 100, 150];
function deviationColor(bps) {
  if (!Number.isFinite(bps)) return '#b7c0cf';
  const magnitude = Math.abs(bps);
  const upper = colorLevels.findIndex(level => magnitude <= level);
  if (upper === -1) return ratingColors.at(-1);
  if (upper === 0) return ratingColors[0];
  const fraction = (magnitude - colorLevels[upper - 1]) /
    (colorLevels[upper] - colorLevels[upper - 1]);
  const from = ratingColors[upper - 1], to = ratingColors[upper];
  return '#' + [1, 3, 5].map(offset => {
    const start = parseInt(from.slice(offset, offset + 2), 16);
    const end = parseInt(to.slice(offset, offset + 2), 16);
    return Math.round(start + (end - start) * fraction).toString(16).padStart(2, '0');
  }).join('');
}
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
  const levels = [extent, ...[...colorLevels].reverse(), 0, ...colorLevels.map(v => -v), -extent]
    .filter((v, i, levels) => i === 0 || i === levels.length - 1 || Math.abs(v) < extent);
  for (const level of levels) {
    gradient.addColorStop(axisPosition(level, extent), deviationColor(level) + alpha);
  }
  return gradient;
}
function updateRating() {
  const last = state.live && !state.zoom ? summaryValues?.last : null;
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
  $('rating-caption').textContent = displayed < 0 ? (state.live && !state.zoom ? 'Waiting for prices' : '-') : ratingBand(displayed);
}
function summary() {
  const rows = state.rows, last = rows.at(-1);
  const ready = summaryValues?.last;
  const livePrice = state.live && !state.zoom ? ready : null;
  $('latest').textContent = livePrice ? formatScale((livePrice[1] - 1) * 10000) : '-';
  $('price').textContent = livePrice ? livePrice[1].toFixed(6) : '-';
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
  else if (state.liveLoading) status('Loading live prices…', 'loading');
  else if (!last) status('Waiting for finalized prices', 'stale');
  else if (!Number.isFinite(state.fetchedAt) || now - state.fetchedAt > source.staleFetch)
    status(`Updates delayed${sampleAge}`, 'stale');
  else if (age > source.stalePrice) status(`Data delayed${sampleAge}`, 'stale');
  else status(`Live ~${age}s ago`, 'live');
}

const historyCache = new BrowserHistory();
let nextHistoryRefresh = 0;
function hasHistoryGaps(rows) {
  let previous;
  for (const run of rows.runs) {
    if ((previous && previous[2] !== null && run.block !== null && run.block > previous[2] + 1) ||
        (run.count > 1 && run.block !== null && run.db > 1)) return true;
    previous = rows.at(run.offset + run.count - 1);
  }
  return false;
}
async function refresh(force = false, preferCache = false) {
  if (reloading || (loading && !force && !preferCache)) return;
  controller?.abort();
  controller = new AbortController();
  const ticket = ++generation;
  loading = true;
  const requestController = controller;
  // Keep the completed snapshot intact during local navigation. Polls advance
  // both its endpoint and its retention boundary together.
  if (!preferCache) historyCache.expire(Date.now()/1000);
  // Only recent snapshots can satisfy live navigation without a new request.
  const latestCoverage = historyCache.coverage.at(-1)?.[1];
  const recentSnapshot = Number.isFinite(latestCoverage) &&
    Date.now()/1000 - latestCoverage <= source.staleFetch;
  state.liveLoading = state.live && !state.zoom && !recentSnapshot;
  const cachedEnd = preferCache && state.live && recentSnapshot ? latestCoverage : null;
  let end = state.zoom?.[1] ?? cachedEnd ?? (state.live || !state.end ? Date.now()/1000 : state.end);
  let start = state.zoom?.[0] ?? end - state.range;
  const sameWindow = Math.abs((state.end - state.start) - (end - start)) < .01;
  state.start = start;
  state.end = end;
  state.error = null;
  state.extremes = null;
  const missing = historyCache.missing(start, end);
  const live = state.live && !state.zoom;
  const includeIssuance = showIssuance;
  const tiStart = start, tiEnd = live ? null : end;
  if (includeIssuance && (!issuanceCoverage || start < issuanceCoverage.start ||
      (end > issuanceCoverage.end && !(live && issuanceCoverage.live)))) {
    issuanceCursor = null;
  }
  const cachedRows = historyCache.rows.window(start, end);
  const last = cachedRows.at(-1);
  // Advance only from a completed history snapshot; warming/backfills need the full window.
  const refreshHistory = Date.now() >= nextHistoryRefresh && hasHistoryGaps(cachedRows);
  const incremental = live && !force && !state.serverWarming && !refreshHistory &&
    Number.isSafeInteger(last?.[2]) &&
    !missing.some(([a]) => a < last[0]);
  const left = incremental ? Math.max(start, last[0]) : start;
  const blockCursor = incremental && Number.isSafeInteger(last?.[2]) ? last[2] : null;
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
    if (preferCache && !force && !missing.length && (!includeIssuance || issuanceCursor !== null)) return;
    if (!force && !live && !missing.length && !refreshHistory && !includeIssuance) return;
    timeout = setTimeout(() => requestController.abort(), 15000);
    const lowerBound = blockCursor === null ? `start=${start}` : `startpoint=${blockCursor}`;
    const upperBound = live ? '' : `&end=${end}`;
    // History streams need full-window extrema before all chunks arrive.
    // Incremental polls already have that history locally.
    const statisticsWindow = incremental ? '' : `&windowStart=${start}${live ? '' : `&windowEnd=${end}`}`;
    const selection = includeIssuance ? 'price,ti' : 'price';
    const issuanceWindow = includeIssuance ? `&tiStart=${tiStart}${tiEnd === null ? '' : `&tiEnd=${tiEnd}`}` +
      (issuanceCursor === null ? '' : `&tiCursor=${encodeURIComponent(issuanceCursor)}`) : '';
    const response = await fetch(`${source.endpoint}?series=${selection}&${lowerBound}${upperBound}${statisticsWindow}${issuanceWindow}&format=stream`, {signal: requestController.signal});
    if (ticket !== generation) return;
    if (!acceptVersion(response.headers.get('X-App-Version'))) return;
    if (!response.ok) {
      const result = await response.json();
      throw new Error(result.error || 'Unable to load prices');
    }
    const responseEnd = Number(response.headers.get('X-History-End'));
    if (live && Number.isFinite(responseEnd) && responseEnd > 0) {
      end = responseEnd;
      start = end - state.range;
      state.start = start;
      state.end = end;
    }
    const runs = [];
    let lastDraw = -Infinity, pendingIssuance;
    for await (const message of readPriceStream(response)) {
      if (ticket !== generation) return;
      if (message.encoding) {
        state.extremes = message.extremes;
        state.serverWarming = message.historyReady === false;
        state.fetchedAt = message.fetchedAt;
        if (includeIssuance) pendingIssuance = message.ti;
      } else if (message.ranges) {
        const batch = RangeSeries.fromWire(message.ranges);
        runs.push(...(blockCursor === null ? batch.window(left, end) : batch).runs);
        if (performance.now() - lastDraw >= 32) {
          // Partial data is drawable, but only a finished stream marks cache coverage complete.
          publish(new RangeSeries([...historyCache.rows.window(-Infinity, left).runs, ...runs,
            ...historyCache.rows.window(end, Infinity).runs]));
          lastDraw = performance.now();
        }
      }
    }
    if (ticket !== generation) return;
    if (!incremental) nextHistoryRefresh = Date.now() + 60000;
    historyCache.merge(new RangeSeries(runs), left, end, state.fetchedAt, !state.serverWarming);
    if (Array.isArray(pendingIssuance?.rows) && typeof pendingIssuance.cursor === 'string' &&
        typeof pendingIssuance.reset === 'boolean') {
      // TI error reports live RPC status; retained samples and repairs remain usable.
      issuanceRows = mergeIssuanceRows(issuanceRows, pendingIssuance, Date.now()/1000);
      issuanceCursor = pendingIssuance.cursor;
      // A narrowed subscription no longer observes repairs outside its window.
      issuanceCoverage = {start: tiStart, end: tiEnd ?? end, live};
    }
    state.historyLoading = historyCache.missing(start, end).length > 0;
    publish(historyCache.rows);
  } catch (error) {
    if (ticket !== generation) return;
    state.error = error.name === 'AbortError' ? 'Request timed out. Retrying…' : error.message;
  } finally {
    clearTimeout(timeout);
    if (ticket === generation) {
      loading = false;
      controller = null;
      state.liveLoading = false;
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
  if (axisScale === 'log') {
    const positive = [];
    for (let tick = 1; tick <= extent; tick *= 10) positive.push(tick);
    return {extent, ticks: [...positive.map(v => -v).reverse(), 0, ...positive]};
  }
  const step = extent / (extent / magnitude === 2 ? 4 : 5);
  const divisions = Math.round(extent / step);
  return {extent, ticks: Array.from({length: divisions * 2 + 1}, (_, i) => (i - divisions) * step)};
}
function drawGrid({left, right, top, bottom, x, y}, width, yTicks) {
  ctx.font = '10px ui-monospace, monospace';
  ctx.lineWidth = 1;
  for (const value of yTicks) {
    const py = y(value);
    if (axisScale === 'log' && value !== 0 && Math.abs(py - y(0)) < 20) continue;
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
    const duration = state.end - state.start;
    const label = duration > 86400 ? date(time).slice(5, 16) :
      duration < 120 ? clock(time) : clock(time).slice(0, 5);
    ctx.fillText(label, px, bottom + 23);
  }

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
  // A symmetric gradient blends colors by absolute peg deviation at every zoom level.
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
  const peg = new Path2D(), pegY = y(0);
  let pegCursor = left, previousOffset, previousX;
  function excludePeg(start, end) {
    start = Math.max(left, start - 2);
    end = Math.min(right, end + 2);
    if (start > pegCursor) {
      peg.moveTo(pegCursor, pegY);
      peg.lineTo(start, pegY);
    }
    pegCursor = Math.max(pegCursor, end);
  }
  // Draw range endpoints, preserving pixel extrema and every real block gap.
  for (const {row, move} of pixelRows(state.rows, x)) {
    const px = x(row[0]), py = y((row[1] - 1) * 10000);
    const offset = py - pegY;
    // Cut only where the price stroke enters a two-pixel band around the peg.
    // The shaded area never participates in this intersection calculation.
    if (move) {
      if (Math.abs(offset) <= 2) excludePeg(px, px);
    } else if (offset === previousOffset) {
      if (Math.abs(offset) <= 2) excludePeg(previousX, px);
    } else {
      const a = (-2 - previousOffset) / (offset - previousOffset);
      const b = (2 - previousOffset) / (offset - previousOffset);
      const start = Math.max(0, Math.min(a, b)), end = Math.min(1, Math.max(a, b));
      if (start <= end) excludePeg(previousX + (px - previousX) * start, previousX + (px - previousX) * end);
    }
    previousOffset = offset;
    previousX = px;
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
  if (high && low) {
    const highBps = Number(((high[1] - 1) * 10000).toFixed(2));
    const lowBps = Number(((low[1] - 1) * 10000).toFixed(2));
    if (highBps !== lowBps) {
      markExtreme(high, {x, y});
      markExtreme(low, {x, y});
    }
  }
  ctx.restore();
  if (pegCursor < right) {
    peg.moveTo(pegCursor, pegY);
    peg.lineTo(right, pegY);
  }
  return peg;
}

function drawIssuance({right, top, bottom, x}) {
  if (!showIssuance) return;
  const visible = issuanceRows.filter(row => row[0] >= state.start && row[0] <= state.end);
  ctx.fillStyle = issuanceColor;
  ctx.font = '10px ui-monospace, monospace';
  ctx.textAlign = 'left';
  ctx.fillText('dotUSD', right + 10, top - 6);
  if (!visible.length) return;
  let low = Infinity, high = -Infinity;
  for (const row of visible) {
    low = Math.min(low, row[1]);
    high = Math.max(high, row[1]);
  }
  // Leave enough separation for three-significant-digit compact tick labels.
  const padding = Math.max((high - low) * .1, high * .02, .000001);
  const min = Math.max(0, low - padding), max = high + padding;
  const y = value => bottom - (value - min) / (max - min) * (bottom - top);
  for (let i = 0; i <= 4; i++) {
    const value = min + (max - min) * i / 4;
    ctx.fillText(issuanceNumber(value), right + 10, y(value) + 3);
  }
  ctx.strokeStyle = issuanceColor;
  ctx.lineWidth = 2;
  ctx.beginPath();
  let previous;
  for (const row of visible) {
    // Show the coarse backfill, but do not bridge an unfilled coarse interval.
    if (!previous || row[2] <= previous[2] || row[2] - previous[2] > 1800) ctx.moveTo(x(row[0]), y(row[1]));
    else ctx.lineTo(x(row[0]), y(row[1]));
    previous = row;
  }
  ctx.stroke();
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
    if (showIssuance) {
      // Price timestamps are estimated; match actual chain samples by block.
      let low = 0, high = issuanceRows.length;
      while (low < high) {
        const mid = Math.floor((low + high) / 2);
        if (issuanceRows[mid][2] < row[2]) low = mid + 1;
        else high = mid;
      }
      const sample = issuanceRows[low];
      const issuance = document.createElement('div');
      issuance.textContent = sample && row[2] !== null && sample[2] === row[2]
        ? `TI: ${issuanceNumber(sample[1])} dotUSD` : 'TI: unavailable';
      issuance.style.color = issuanceColor;
      tip.append(issuance);
    }
    tip.style.left = `${Math.max(0, Math.min(px + 16, width - 230))}px`;
    tip.style.top = `${Math.max(0, Math.min(py + 16, height - (showIssuance ? 125 : 100)))}px`;
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
    const left = displayScale === 'dollars' ? 82 : 65;
    ctx.font = '10px ui-monospace, monospace';
    let issuanceWidth = showIssuance ? 90 : 20;
    if (showIssuance) {
      for (const row of issuanceRows) {
        issuanceWidth = Math.max(issuanceWidth, ctx.measureText(issuanceNumber(row[1] * 1.00001)).width + 25);
      }
    }
    const right = rect.width - issuanceWidth, top = 18, bottom = rect.height - 35;
    const x = time => left + (time - state.start) / (state.end - state.start || 1) * (right - left);
    const y = value => top + axisPosition(value, extent) * (bottom - top);
    geometry = {left, right, top, bottom, x, y};
    drawGrid(geometry, rect.width, ticks);
    const peg = drawPrices(geometry, extent, high, low);
    ctx.setLineDash([]);
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 0.6;
    ctx.stroke(peg);
    drawIssuance(geometry);

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
  refresh(false, true);
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
  url.searchParams.set('scale', displayScale);
  url.searchParams.set('axis', axisScale);
  url.searchParams.set('ti', showIssuance ? '1' : '0');
  if (state.zoom) {
    url.searchParams.set('start', String(state.zoom[0]));
    url.searchParams.set('end', String(state.zoom[1]));
  }
  if (url.href !== location.href) history.pushState(null, '', url);
}
function updateViewControls() {
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
  refresh(false, true);
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
  const rangeChanged = state.range !== view.range ||
    state.zoom?.[0] !== view.zoom?.[0] || state.zoom?.[1] !== view.zoom?.[1];
  displayScale = view.scale ?? preferredScale;
  axisScale = view.axis ?? preferredAxis;
  const issuance = view.issuance ?? preferredIssuance;
  const issuanceChanged = showIssuance !== issuance;
  showIssuance = issuance;
  $('issuance-axis').checked = showIssuance;
  if (issuanceChanged && showIssuance) {
    issuanceCursor = null;
    issuanceCoverage = null;
  }
  applyScale();
  if (!rangeChanged) {
    updateViewControls();
    if (issuanceChanged) refresh(true);
    return;
  }
  state.range = view.range;
  state.zoom = view.zoom;
  state.live = !view.zoom;
  state.start = 0;
  state.end = 0;
  updateRangeButtons();
  updateViewControls();
  refresh(issuanceChanged, true);
});
$('display-scale').addEventListener('change', event => {
  displayScale = event.target.value;
  try { localStorage.setItem('dotusd-display-scale', displayScale); } catch {}
  applyScale();
  writeViewUrl();
  updateViewControls();
});
$('axis-scale').addEventListener('change', event => {
  axisScale = event.target.checked ? 'log' : 'linear';
  try { localStorage.setItem('dotusd-axis-scale', axisScale); } catch {}
  applyScale();
  writeViewUrl();
  updateViewControls();
});
$('issuance-axis').checked = showIssuance;
$('issuance-axis').addEventListener('change', event => {
  showIssuance = event.target.checked;
  if (showIssuance) {
    issuanceCursor = null;
    issuanceCoverage = null;
  }
  try { localStorage.setItem('dotusd-issuance-axis', String(showIssuance)); } catch {}
  applyScale();
  writeViewUrl();
  refresh(true);
});
const settings = document.querySelector('.settings');
document.addEventListener('click', event => {
  if (settings.open && !settings.contains(event.target)) settings.open = false;
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
  return showIssuance || (state.live && !state.zoom) || state.historyLoading ||
    (Date.now() >= nextHistoryRefresh && hasHistoryGaps(state.rows));
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
  // Every historic response checks the version, including when updates resume.
  refresh();
}
