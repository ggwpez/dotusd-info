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
const state = {rows: [], range: initialView.range, start: 0, end: 0, live: !initialView.zoom, zoom: initialView.zoom, hover: null, drag: null, error: null, fetchedAt: null};
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
const ratingBands = ['≤ 5 bps', '> 5–20 bps', '> 20–50 bps', '> 50–100 bps', '> 100 bps'];
let hoveredRating = null, focusedRating = null;
function ratingForBps(bps) {
  if (!Number.isFinite(bps)) return -1;
  const magnitude = Math.round(Math.abs(bps) * 1e6) / 1e6;
  const index = [5, 20, 50, 100].findIndex(limit => magnitude <= limit);
  return index === -1 ? 4 : index;
}
function ratingGradient(context, top, bottom, extent, alpha = '') {
  const gradient = context.createLinearGradient(0, top, 0, bottom);
  const levels = [extent, ...[100, 50, 20, 5, -5, -20, -50, -100].filter(v => v < extent && v > -extent), -extent];
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
  const low = values.reduce((a,b) => Math.min(a,b), Infinity), high = values.reduce((a,b) => Math.max(a,b), -Infinity);
  $('extremes').textContent = last ? `${signed(low)} / ${signed(high)}` : '—';
  $('window').textContent = `${date(state.start)} — ${date(state.end)} · ${rows.length.toLocaleString()} samples`;
  $('message').textContent = state.error || (last ? '' : 'No price samples in this window');
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

async function refresh() {
  controller?.abort(); controller = new AbortController(); const ticket = ++generation; loading = true;
  const requestController = controller;
  const timeout = setTimeout(() => requestController.abort(), 15000);
  const end = state.zoom?.[1] ?? (state.live || !state.end ? Date.now()/1000 : state.end);
  const start = state.zoom?.[0] ?? end - state.range;
  try {
    const response = await fetch(`/api/prices?start=${start}&end=${end}`, {signal: requestController.signal});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Unable to load prices');
    if (ticket !== generation) return;
    state.fetchedAt = result.fetchedAt;
    state.rows = result.samples; state.start = start; state.end = end; state.error = null; state.hover = null;
  } catch (error) {
    if (ticket !== generation) return;
    state.error = error.name === 'AbortError' ? 'Request timed out. Retrying…' : error.message;
    if (!state.start) {state.start = start; state.end = end;}
  } finally {
    clearTimeout(timeout);
  }
  loading = false; chartDirty = true; summary(); draw();
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
  let high, low;
  for (const row of state.rows) {
    peak = Math.max(peak, Math.abs((row[1]-1)*10000));
    if (!high || row[1] > high[1]) high = row;
    if (!low || row[1] < low[1]) low = row;
  }
  const extent = peak + Math.max(peak*.12, .5);
  const min = -extent, max = extent;
  const x = t => left+(t-state.start)/(state.end-state.start || 1)*(right-left);
  const y = v => bottom-(v-min)/(max-min)*(bottom-top);
  geometry = {left,right,top,bottom,x,y};
  ctx.font = '10px ui-monospace, monospace'; ctx.lineWidth = 1;
  for (let i=0;i<=6;i++) {
    const v = extent*(i-3)/3, py = y(v);
    ctx.strokeStyle = '#272d37';ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(right,py);ctx.stroke();
    ctx.fillStyle = '#8792a3';ctx.textAlign='right';ctx.fillText(v.toFixed(2),left-12,py+3);
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
  const areaColor = ratingGradient(ctx, top, bottom, extent, '20');
  const area = new Path2D();
  ctx.strokeStyle=lineColor;ctx.lineWidth=1.1;ctx.lineJoin='round';ctx.lineCap='round';ctx.beginPath();let previous;
  // Consecutive blocks remain connected regardless of estimated timestamp spacing.
  for(const row of state.rows) {
    const px=x(row[0]),py=y((row[1]-1)*10000);
    if (!previous || row[2] == null || previous[2] == null || row[2] !== previous[2] + 1) {
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
  controller?.abort(); generation++; loading = false;
  state.live = false; state.zoom = [state.start, state.end];
  writeViewUrl(); updateViewControls(); summary();
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
$('refresh').onclick=refresh;
new ResizeObserver(draw).observe($('plot'));
setInterval(()=>{if(state.live&&!state.zoom&&!document.hidden&&!loading)refresh();},6000);
// Age the last received timestamps independently of network requests or chart redraws.
setInterval(updateFreshness, 1000);
window.addEventListener('offline', updateFreshness);
window.addEventListener('online', () => {
  updateFreshness();
  if (state.live && !state.zoom) refresh();
});
document.addEventListener('visibilitychange', () => {
  updateFreshness();
  if (!document.hidden && state.live && !state.zoom) refresh();
});
refresh();
