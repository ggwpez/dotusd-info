import {RangeSeries, BrowserHistory, pixelRows, readPriceStream} from './ranges.js?v=__APP_VERSION__';
const $ = id => document.getElementById(id);
// Replaced by the server from the same release snapshot as the served assets.
const APP_VERSION = '__APP_VERSION__';
let reloading = false;
function acceptVersion(version) {
  if (reloading) return false;
  if (!version || version === APP_VERSION) return true;
  reloading = true;
  controller?.abort(); generation++;
  location.reload();
  return false;
}
const resolutions = new Map([['15m', 900], ['1h', 3600], ['6h', 21600], ['24h', 86400], ['7d', 604800]]);
function linkedView() {
  const params = new URLSearchParams(location.search);
  const range = resolutions.get(params.get('resolution')) ?? 3600;
  const start = Number(params.get('start')), end = Number(params.get('end'));
  const valid = params.has('start') && params.has('end') &&
    params.get('start').trim() !== '' && params.get('end').trim() !== '' &&
    Number.isFinite(start) && Number.isFinite(end) && start >= 0 &&
    end - start >= 1 && end - start <= 604800 && end <= Date.now()/1000 + 60;
  return {range, zoom: valid ? [start, end] : null};
}
const initialView = linkedView();
const canvas = $('chart'), ctx = canvas.getContext('2d');
const state = {rows: new RangeSeries(), range: initialView.range, start: 0, end: 0, live: !initialView.zoom, zoom: initialView.zoom, hover: null, drag: null, error: null, fetchedAt: null, historyLoading: false, serverWarming: false, extremes: null};
let controller, generation = 0, geometry, loading = false;
const backing = document.createElement('canvas');
let chartDirty = true;
const signed = v => `${v > 0 ? '+' : ''}${v.toFixed(2)}`;
const ratingColors = ['#73bf69', '#63c8ae', '#dfc464', '#f29b58', '#f2495c'];
const deviationColor = bps => ratingColors[ratingForBps(bps)] ?? '#b7c0cf';
const clock = t => new Date(t * 1000).toISOString().slice(11, 19);
const date = t => new Date(t * 1000).toISOString().replace('T', ' ').slice(0, 19);
function status(text, kind) { $('status').textContent = text; $('status').dataset.state = kind; }
const ratingNames = ['Excellent', 'Good', 'Fair', 'Poor', 'Horrible'];
const ratingLimits = [5, 20, 50, 100];
const ratingBands = ['≤ 5 bps', '> 5–20 bps', '> 20–50 bps', '> 50–100 bps', '> 100 bps'];
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
  const last = state.rows.at(-1);
  const bps = last ? (last[1] - 1) * 10000 : NaN;
  const current = ratingForBps(bps);
  const preview = hoveredRating ?? focusedRating;
  const displayed = preview ?? current;
  document.querySelectorAll('[data-rating]').forEach(segment => {
    const active = Number(segment.dataset.rating) === current;
    segment.classList.toggle('is-current', active);
    segment.classList.toggle('is-preview', Number(segment.dataset.rating) === preview);
    if (active) segment.setAttribute('aria-current', 'true');
    else segment.removeAttribute('aria-current');
  });
  $('rating-value').textContent = displayed < 0 ? '—' : ratingNames[displayed];
  $('rating-caption').textContent = displayed < 0 ? 'Waiting for prices' : ratingBands[displayed];
}
function summary() {
  const rows = state.rows, last = rows.at(-1);
  $('latest').textContent = last ? signed((last[1] - 1) * 10000) : '—';
  $('price').textContent = last ? last[1].toFixed(6) : '—';
  const low = state.extremes ? (state.extremes.low[1]-1)*10000 : (rows.low ? (rows.low[1]-1)*10000 : 0);
  const high = state.extremes ? (state.extremes.high[1]-1)*10000 : (rows.high ? (rows.high[1]-1)*10000 : 0);
  $('extremes').textContent = last ? `${signed(low)} / ${signed(high)}` : '—';
  $('window').textContent = `${date(state.start)} — ${date(state.end)} · ${rows.length.toLocaleString()} samples${state.historyLoading ? ` · loading history…${state.serverWarming ? ' · server loading stored data' : ''}` : ''}`;
  $('message').textContent = state.error || (last ? '' : state.historyLoading ? 'Loading samples…' : 'No price samples in this window');
  $('message').hidden = !state.error && !!last;
  updateFreshness();
}
function updateFreshness() {
  updateRating();
  const last = state.rows.at(-1);
  const now = Date.now()/1000;
  const age = last ? Math.max(0, Math.round(now - last[0])) : null;
  const sampleAge = last ? ` · last finalized ~${age}s ago` : '';
  if (!state.live || state.zoom) status('Historical view', 'paused');
  else if (navigator.onLine === false) status(`Offline${sampleAge}`, 'error');
  else if (state.error) status(`Connection error${sampleAge}`, 'error');
  else if (!last) status('Waiting for finalized prices', 'stale');
  else if (!Number.isFinite(state.fetchedAt) || now - state.fetchedAt > 30)
    status(`Updates delayed${sampleAge}`, 'stale');
  else if (age > 180) status(`Data delayed${sampleAge}`, 'stale');
  else status(`● Live · finalized ~${age}s ago`, 'live');
}

const historyCache = new BrowserHistory();
async function refresh(force = false) {
  if (reloading) return;
  controller?.abort(); controller = new AbortController(); const ticket = ++generation; loading = true;
  const requestController = controller;
  const end = state.zoom?.[1] ?? (state.live || !state.end ? Date.now()/1000 : state.end);
  const start = state.zoom?.[0] ?? end - state.range;
  historyCache.expire(Date.now()/1000);
  if (force) historyCache.coverage = [];
  state.start = start; state.end = end; state.error = null;
  state.extremes = null;
  const missing = historyCache.missing(start, end);
  const live = state.live && !state.zoom;
  // One full-window request for history; ordinary live polls retain a five-minute overlap.
  const recentStart = Math.max(start, end - 300);
  const left = live && historyCache.rows.length && !missing.some(([a]) => a < recentStart) ? recentStart : start;
  const publish = rows => {
    state.rows = rows.window(start, end);
    state.hover = null; chartDirty = true; summary(); draw();
  };
  state.historyLoading = missing.length > 0;
  state.fetchedAt = historyCache.fetchedAt;
  publish(historyCache.rows);
  let timeout;
  try {
    if (!live && !missing.length) return;
    timeout = setTimeout(() => requestController.abort(), 15000);
    const response = await fetch(`/api/prices?start=${left}&end=${end}&windowStart=${start}&windowEnd=${end}&format=stream`, {signal: requestController.signal});
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
    historyCache.merge(new RangeSeries(runs), left, end, state.fetchedAt, !state.serverWarming);
    state.historyLoading = historyCache.missing(start, end).length > 0;
    publish(historyCache.rows);
  } catch (error) {
    if (ticket !== generation) return;
    state.error = error.name === 'AbortError' ? 'Request timed out. Retrying…' : error.message;
  } finally {
    clearTimeout(timeout);
    if (ticket === generation) {
      loading = false; state.historyLoading = historyCache.missing(start, end).length > 0; summary();
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
function draw() {
  // Render at least two pixels per CSS pixel for smoother thin lines.
  const rect = canvas.getBoundingClientRect(), dpr = Math.max(2, window.devicePixelRatio || 1);
  const width = Math.round(rect.width*dpr), height = Math.round(rect.height*dpr);
  if (canvas.width !== width || canvas.height !== height) chartDirty = true;
  canvas.width = width; canvas.height = height;
  ctx.setTransform(dpr,0,0,dpr,0,0);
  const w = rect.width, h = rect.height, left = 65, right = w-20, top = 18, bottom = h-35;
  if (chartDirty) {
  let peak = 0;
  let high = state.extremes?.high, low = state.extremes?.low;
  if (high && low) peak = Math.max(Math.abs((high[1]-1)*10000), Math.abs((low[1]-1)*10000));
  for (const row of [state.rows.low, state.rows.high].filter(Boolean)) {
    peak = Math.max(peak, Math.abs((row[1]-1)*10000));
    if (!high || row[1] > high[1]) high = row;
    if (!low || row[1] < low[1]) low = row;
  }
  const {extent, ticks: yTicks} = chartScale(peak);
  const min = -extent, max = extent;
  const x = t => left+(t-state.start)/(state.end-state.start || 1)*(right-left);
  const y = v => bottom-(v-min)/(max-min)*(bottom-top);
  geometry = {left,right,top,bottom,x,y};
  ctx.font = '10px ui-monospace, monospace'; ctx.lineWidth = 1;
  for (const v of yTicks) {
    const py = y(v);
    const boundaryColor = ratingLimits.includes(Math.abs(v)) ? deviationColor(v) : null;
    ctx.strokeStyle = boundaryColor ? boundaryColor + '50' : '#272d37';ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(right,py);ctx.stroke();
    ctx.fillStyle = boundaryColor ?? '#8792a3';ctx.textAlign='right';ctx.fillText(String(v),left-12,py+3);
  }
  const ticks = w < 600 ? 3 : 6;
  for (let i=0;i<=ticks;i++) {
    const t = state.start+(state.end-state.start)*i/ticks, px=x(t);
    ctx.strokeStyle='#202630';ctx.beginPath();ctx.moveTo(px,top);ctx.lineTo(px,bottom);ctx.stroke();
    ctx.fillStyle='#8792a3';ctx.textAlign=i===0?'left':i===ticks?'right':'center';
    ctx.fillText(state.end-state.start>86400 ? date(t).slice(5,16) : clock(t).slice(0,5),px,bottom+23);
  }
  ctx.setLineDash([]);ctx.strokeStyle='#ffffff';ctx.lineWidth=.6;
  ctx.beginPath();ctx.moveTo(left,y(0));ctx.lineTo(right,y(0));ctx.stroke();
  ctx.save();ctx.beginPath();ctx.rect(left,top,right-left,bottom-top);ctx.clip();
  // Symmetric hard stops color both sides by absolute peg deviation.
  const lineColor = ratingGradient(ctx, top, bottom, extent);
  // Fade the fill at wider time scales, independently of history loading progress.
  const secondsPerPixel = (state.end-state.start) / Math.max(1, right-left);
  const fillAlpha = Math.max(8, Math.round(32 / Math.sqrt(Math.max(1, secondsPerPixel / 6))));
  const areaColor = ratingGradient(ctx, top, bottom, extent, fillAlpha.toString(16).padStart(2, '0'));
  const area = new Path2D();
  ctx.strokeStyle=lineColor;ctx.lineWidth=1.1;ctx.lineJoin='round';ctx.lineCap='round';ctx.beginPath();let previous;
  // Draw range endpoints, preserving pixel extrema and every real block gap.
  for(const {row, move} of pixelRows(state.rows, x)) {
    const px=x(row[0]),py=y((row[1]-1)*10000);
    if (move) {
      if (previous) {area.lineTo(x(previous[0]),y(0));area.closePath();}
      area.moveTo(px,y(0));
      ctx.moveTo(px,py);
    } else ctx.lineTo(px,py);
    area.lineTo(px,py);
    previous=row;
  }
  if (previous) {area.lineTo(x(previous[0]),y(0));area.closePath();}
  ctx.fillStyle=areaColor;ctx.fill(area);
  ctx.stroke();
  function markExtreme(row) {
    const value = (row[1]-1)*10000, px = x(row[0]), py = y(value);
    const color = deviationColor(value);
    ctx.beginPath();ctx.arc(px,py,4,0,Math.PI*2);ctx.fillStyle = color;ctx.fill();
    ctx.strokeStyle = '#14181f';ctx.lineWidth = 2;ctx.stroke();
  }
  if (high && low) {
    const highBps = Number(((high[1]-1)*10000).toFixed(2));
    const lowBps = Number(((low[1]-1)*10000).toFixed(2));
    if (highBps !== lowBps) {
      markExtreme(high);markExtreme(low);
    }
  }
  ctx.restore();
  backing.width = canvas.width; backing.height = canvas.height;
  backing.getContext('2d').drawImage(canvas, 0, 0);
  chartDirty = false;
  } else {
    ctx.drawImage(backing, 0, 0, w, h);
  }
  const {x,y} = geometry;
  ctx.save();ctx.beginPath();ctx.rect(left,top,right-left,bottom-top);ctx.clip();
  if(state.drag) {ctx.fillStyle='#ec83b122';ctx.fillRect(Math.min(...state.drag),top,Math.abs(state.drag[1]-state.drag[0]),bottom-top);}
  $('tooltip').hidden=true;
  if(state.hover!==null && state.rows.at(state.hover)) {
    const row=state.rows.at(state.hover),px=x(row[0]),py=y((row[1]-1)*10000);
    ctx.strokeStyle='#7e899b';ctx.lineWidth=1;ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(px,top);ctx.lineTo(px,bottom);ctx.moveTo(left,py);ctx.lineTo(right,py);ctx.stroke();ctx.setLineDash([]);
    ctx.fillStyle=deviationColor((row[1]-1)*10000);ctx.beginPath();ctx.arc(px,py,3,0,Math.PI*2);ctx.fill();
    const tip=$('tooltip');tip.hidden=false;tip.replaceChildren();
    const stamp=document.createElement('div');stamp.textContent=`${date(row[0])} UTC`;
    const value=document.createElement('strong');value.textContent=`${signed((row[1]-1)*10000)} bps`;
    value.style.color=deviationColor((row[1]-1)*10000);
    const price=document.createElement('div');price.textContent=`${row[1].toFixed(8)} USDT`;
    tip.append(stamp,value,price);tip.style.left=`${Math.max(0,Math.min(px+16,w-230))}px`;tip.style.top=`${Math.max(0,Math.min(py+16,h-100))}px`;
  }
  ctx.restore();
}
function position(event) {return Math.max(geometry.left,Math.min(geometry.right,event.clientX-canvas.getBoundingClientRect().left));}
canvas.addEventListener('pointermove', event => {
  if (!geometry || !state.rows.length) return;
  const px=position(event);
  if(state.drag) state.drag[1]=px;
  const t=state.start+(px-geometry.left)/(geometry.right-geometry.left)*(state.end-state.start);
  state.hover = state.rows.nearest(t);
  draw();
});
canvas.addEventListener('pointerleave',()=>{if(!state.drag){state.hover=null;draw();}});
canvas.addEventListener('pointerdown',event=>{if(!geometry || event.button!==0)return;state.drag=[position(event),position(event)];canvas.setPointerCapture(event.pointerId);});
canvas.addEventListener('pointerup',event=>{
  if(!state.drag)return;
  const [a,b]=state.drag;state.drag=null;
  if(Math.abs(a-b)>8){const time=px=>state.start+(px-geometry.left)/(geometry.right-geometry.left)*(state.end-state.start);const window=[time(Math.min(a,b)),time(Math.max(a,b))];if(window[1]-window[0]>=1){state.zoom=window;state.live=false;writeViewUrl();updateViewControls();refresh();}}
  canvas.releasePointerCapture(event.pointerId);draw();
});
canvas.addEventListener('pointercancel',()=>{state.drag=null;draw();});
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
  $('reset').hidden = !state.zoom;
  $('live').setAttribute('aria-pressed', String(state.live));
  $('live').lastChild.textContent = state.live ? ' Live · 6s' : ' Paused';
}
function reset() {
  state.zoom = null; state.live = true; state.start = 0; state.end = 0;
  writeViewUrl(); updateViewControls(); refresh();
}
const chartPanel = $('chart-panel');
let chartFullscreen = false;
function updateFullscreen() {
  const active = chartFullscreen;
  chartPanel.classList.toggle('is-fullscreen', active);
  document.body.classList.toggle('chart-fullscreen', active);
  $('fullscreen').setAttribute('aria-pressed', String(active));
  $('fullscreen').textContent = active ? 'Exit fullscreen' : 'Fullscreen';
  state.drag = null; state.hover = null;
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
$('reset').onclick = reset;
canvas.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();state.hover=Math.max(0,Math.min(state.rows.length-1,(state.hover??state.rows.length-1)+(event.key==='ArrowRight'?1:-1)));draw();}});
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
  state.range = view.range; state.zoom = view.zoom; state.live = !view.zoom;
  state.start = 0; state.end = 0;
  updateRangeButtons(); updateViewControls(); refresh();
});
updateRangeButtons();
updateViewControls();
$('live').onclick = () => {
  if (!state.live || state.zoom) { reset(); return; }
  if (state.end - state.start < 1) return;
  controller?.abort(); generation++; loading = false; state.historyLoading = false;
  state.live = false; state.zoom = [state.start, state.end];
  writeViewUrl(); updateViewControls(); refresh();
};
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
setInterval(()=>{if(((state.live&&!state.zoom)||state.historyLoading)&&!document.hidden&&!loading)refresh();},6000);
// Age the last received timestamps independently of network requests or chart redraws.
setInterval(updateFreshness, 1000);
window.addEventListener('offline', updateFreshness);
window.addEventListener('online', () => {
  updateFreshness();
  if ((state.live && !state.zoom) || state.historyLoading) refresh();
});
document.addEventListener('visibilitychange', () => {
  updateFreshness();
  if (!document.hidden && ((state.live && !state.zoom) || state.historyLoading)) refresh();
});
if (acceptVersion(document.querySelector('meta[name="app-version"]')?.content)) {
  // Every price response checks the version, including when live updates resume.
  refresh();
}
