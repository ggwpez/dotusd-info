const $ = id => document.getElementById(id);
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
const state = {rows: [], range: initialView.range, start: 0, end: 0, live: !initialView.zoom, zoom: initialView.zoom, hover: null, drag: null, error: null, fetchedAt: null, historyLoading: false, historyProgress: null, serverWarming: false, extremes: null};
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
  const historical = !state.live || state.zoom;
  const stale = state.error || navigator.onLine === false || !Number.isFinite(state.fetchedAt) ||
    Date.now()/1000 - state.fetchedAt > 30 || (last && Date.now()/1000 - last[0] > 180);
  const context = historical ? 'historical sample' : stale ? 'last available sample' : 'latest sample';
  $('rating-caption').textContent = displayed < 0 ? 'Waiting for prices' : `${ratingBands[displayed]} · ${preview === null ? context : 'rating band'}`;
}
function summary() {
  const rows = state.rows, last = rows.at(-1);
  $('latest').textContent = last ? signed((last[1] - 1) * 10000) : '—';
  $('price').textContent = last ? last[1].toFixed(6) : '—';
  const values = rows.map(r => (r[1] - 1) * 10000);
  const low = state.extremes ? (state.extremes.low[1]-1)*10000 : values.reduce((a,b) => Math.min(a,b), Infinity);
  const high = state.extremes ? (state.extremes.high[1]-1)*10000 : values.reduce((a,b) => Math.max(a,b), -Infinity);
  $('extremes').textContent = last ? `${signed(low)} / ${signed(high)}` : '—';
  $('window').textContent = `${date(state.start)} — ${date(state.end)} · ${rows.length.toLocaleString()} samples${state.historyLoading ? ` · loading history${state.historyProgress ? ` · ${state.historyProgress[0]}/${state.historyProgress[1]}` : '…'}${state.serverWarming ? ' · server loading stored data' : ''}` : ''}`;
  $('message').textContent = state.error || (last ? '' : state.historyLoading ? 'Loading recent samples…' : 'No price samples in this window');
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

class BrowserHistory {
  constructor() { this.rows = []; this.coverage = []; this.fetchedAt = null; this.checkedAt = 0; }
  expire(now) {
    const cutoff = now - 604800;
    this.rows = this.rows.filter(row => row[0] > cutoff);
    this.coverage = this.coverage.filter(([, end]) => end > cutoff).map(([start, end]) => [Math.max(start, cutoff), end]);
    // Recheck old history periodically so late backfill is not cached as an empty gap.
    if (now - this.checkedAt >= 60) { this.coverage = []; this.checkedAt = now; }
  }
  merge(rows, start, end, fetchedAt, complete = true) {
    const byTime = new Map(this.rows.map(row => [row[0], row]));
    for (const row of rows) byTime.set(row[0], row);
    this.rows = [...byTime.values()].sort((a, b) => a[0] - b[0]).slice(-350000);
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
function historyChunks(gaps) {
  const chunks = [];
  for (const [start, end] of [...gaps].reverse()) {
    for (let right = end; right > start;) {
      const left = Math.min(right - 1, Math.max(start, right - 21600));
      chunks.push([left, right]); right = left;
    }
  }
  return chunks;
}
const historyCache = new BrowserHistory();
async function refresh(force = false) {
  controller?.abort(); controller = new AbortController(); const ticket = ++generation; loading = true;
  const requestController = controller;
  const end = state.zoom?.[1] ?? (state.live || !state.end ? Date.now()/1000 : state.end);
  const start = state.zoom?.[0] ?? end - state.range;
  historyCache.expire(Date.now()/1000);
  if (force) historyCache.coverage = [];
  state.start = start; state.end = end; state.error = null; state.historyProgress = null;
  let awaitingExtremes = historyCache.missing(start, end).length > 0 || (state.live && !state.zoom);
  state.extremes = null;
  const publish = () => {
    state.rows = awaitingExtremes ? [] : historyCache.rows.filter(row => row[0] > start && row[0] <= end);
    state.fetchedAt = historyCache.fetchedAt; state.hover = null;
    chartDirty = true; summary(); draw();
  };
  const fetchChunk = async (left, right, includeExtremes = false) => {
    const timeout = setTimeout(() => requestController.abort(), 15000);
    try {
      const metadata = includeExtremes ? `&windowStart=${start}&windowEnd=${end}` : '';
      const response = await fetch(`/api/prices?start=${left}&end=${right}${metadata}`, {signal: requestController.signal});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Unable to load prices');
      if (ticket !== generation) return;
      if (includeExtremes) { state.extremes = result.extremes; awaitingExtremes = false; }
      state.serverWarming = result.historyReady === false;
      historyCache.merge(result.samples, left, right, result.fetchedAt, !state.serverWarming);
    } finally { clearTimeout(timeout); }
  };
  try {
    state.historyLoading = historyCache.missing(start, end).length > 0;
    if (!awaitingExtremes) publish(); // Fully cached windows already have their bounds.
    if ((state.live && !state.zoom) || state.historyLoading) {
      // Recent data first; live polls re-read five minutes to catch late samples.
      await fetchChunk(Math.max(start, end - (historyCache.rows.length ? 300 : 3600)), end, true);
      if (ticket !== generation) return;
      state.historyLoading = historyCache.missing(start, end).length > 0;
      publish();
    }
    const chunks = historyChunks(historyCache.missing(start, end));
    state.historyProgress = [0, chunks.length];
    for (let i = 0; i < chunks.length; i++) {
      await fetchChunk(...chunks[i]);
      if (ticket !== generation) return;
      state.historyProgress = [i + 1, chunks.length];
      state.historyLoading = i < chunks.length - 1 || state.serverWarming;
      publish();
    }
  } catch (error) {
    if (ticket !== generation) return;
    state.error = error.name === 'AbortError' ? 'Request timed out. Retrying…' : error.message;
  } finally {
    if (ticket === generation) {
      loading = false; state.historyLoading = historyCache.missing(start, end).length > 0; summary();
    }
  }
}
function* pixelRows(rows, x) {
  const connected = (a, b) => a && b && a[2] != null && b[2] != null && b[2] === a[2] + 1;
  for (let first = 0; first < rows.length;) {
    const pixel = Math.floor(x(rows[first][0]));
    let last = first, low = first, high = first;
    // Bucket each continuous run separately so real missing blocks remain gaps.
    while (last + 1 < rows.length && connected(rows[last], rows[last + 1]) &&
      Math.floor(x(rows[last + 1][0])) === pixel) {
      last++;
      if (rows[last][1] < rows[low][1]) low = last;
      if (rows[last][1] > rows[high][1]) high = last;
    }
    // Preserve extrema and endpoints in their original time order, without averaging.
    const indices = [...new Set([first, low, high, last])].sort((a, b) => a - b);
    for (const index of indices) {
      yield {row: rows[index], move: index === first && !connected(rows[first - 1], rows[first])};
    }
    first = last + 1;
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
  for (const row of state.rows) {
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
    ctx.strokeStyle = '#272d37';ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(right,py);ctx.stroke();
    ctx.fillStyle = '#8792a3';ctx.textAlign='right';ctx.fillText(String(v),left-12,py+3);
  }
  const ticks = w < 600 ? 3 : 6;
  for (let i=0;i<=ticks;i++) {
    const t = state.start+(state.end-state.start)*i/ticks, px=x(t);
    ctx.strokeStyle='#202630';ctx.beginPath();ctx.moveTo(px,top);ctx.lineTo(px,bottom);ctx.stroke();
    ctx.fillStyle='#8792a3';ctx.textAlign=i===0?'left':i===ticks?'right':'center';
    ctx.fillText(state.end-state.start>86400 ? date(t).slice(5,16) : clock(t).slice(0,5),px,bottom+23);
  }
  // Rating boundaries are accents over the evenly spaced, linear grid.
  ctx.setLineDash([4, 5]);ctx.lineWidth=.8;
  for (const limit of ratingLimits) {
    if (limit > extent) continue;
    ctx.strokeStyle = deviationColor(limit) + '70';
    for (const v of [-limit, limit]) {
      ctx.beginPath();ctx.moveTo(left,y(v));ctx.lineTo(right,y(v));ctx.stroke();
    }
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
  // Reduce overdraw per CSS pixel; raw samples still drive hover, bounds and zoom.
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
  function markExtreme(row, label, above) {
    const value = (row[1]-1)*10000, px = x(row[0]), py = y(value);
    const color = deviationColor(value);
    const text = `${label} ${signed(value)} bps`;
    ctx.font = '10px ui-monospace, monospace';
    const labelWidth = ctx.measureText(text).width+14, labelHeight = 22;
    const lx = Math.max(left+2, Math.min(px-labelWidth/2, right-labelWidth-2));
    const ly = Math.max(top+2, Math.min(above ? py-labelHeight-10 : py+10, bottom-labelHeight-2));
    ctx.strokeStyle = color;ctx.lineWidth = 1;
    ctx.beginPath();ctx.moveTo(px,py);ctx.lineTo(Math.max(lx,Math.min(px,lx+labelWidth)),above ? ly+labelHeight : ly);ctx.stroke();
    ctx.fillStyle = '#14181f';ctx.fillRect(lx,ly,labelWidth,labelHeight);
    ctx.strokeStyle = color+'80';ctx.strokeRect(lx,ly,labelWidth,labelHeight);
    ctx.fillStyle = color;ctx.textAlign = 'left';ctx.textBaseline = 'middle';
    ctx.fillText(text,lx+7,ly+labelHeight/2);
    ctx.textBaseline = 'alphabetic';
    ctx.beginPath();ctx.arc(px,py,4,0,Math.PI*2);ctx.fillStyle = color;ctx.fill();
    ctx.strokeStyle = '#14181f';ctx.lineWidth = 2;ctx.stroke();
  }
  if (high && low) {
    const highBps = Number(((high[1]-1)*10000).toFixed(2));
    const lowBps = Number(((low[1]-1)*10000).toFixed(2));
    if (highBps !== lowBps) {
      markExtreme(high, 'High', true);markExtreme(low, 'Low', false);
    } else if (highBps > 0) {
      markExtreme(high, 'High', true);
    } else if (lowBps < 0) {
      markExtreme(low, 'Low', false);
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
  if(state.hover!==null && state.rows[state.hover]) {
    const row=state.rows[state.hover],px=x(row[0]),py=y((row[1]-1)*10000);
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
  let lo=0,hi=state.rows.length-1;
  while(lo<hi) {const mid=(lo+hi)>>1;if(state.rows[mid][0]<t)lo=mid+1;else hi=mid;}
  state.hover=lo>0 && t-state.rows[lo-1][0]<state.rows[lo][0]-t ? lo-1 : lo;
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
canvas.addEventListener('dblclick',reset);$('reset').onclick=reset;
canvas.addEventListener('keydown',event=>{if(event.key==='Escape')reset();if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();state.hover=Math.max(0,Math.min(state.rows.length-1,(state.hover??state.rows.length-1)+(event.key==='ArrowRight'?1:-1)));draw();}});
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
$('refresh').onclick=()=>refresh(true);
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
refresh();
