const $ = id => document.getElementById(id);
const resolutions = new Map([['15m', 900], ['1h', 3600], ['6h', 21600], ['24h', 86400], ['7d', 604800]]);
const linkedRange = () => resolutions.get(new URLSearchParams(location.search).get('resolution')) ?? 3600;
const canvas = $('chart'), ctx = canvas.getContext('2d');
const state = {rows: [], range: linkedRange(), start: 0, end: 0, live: true, zoom: null, hover: null, drag: null, error: null, fetchedAt: null};
let controller, generation = 0, geometry, loading = false;
const backing = document.createElement('canvas');
let chartDirty = true;
const signed = v => `${v > 0 ? '+' : ''}${v.toFixed(2)}`;
const positiveColor = '#73bf69', negativeColor = '#f2495c';
const deviationColor = v => v > 0 ? positiveColor : v < 0 ? negativeColor : '#b7c0cf';
const clock = t => new Date(t * 1000).toISOString().slice(11, 19);
const date = t => new Date(t * 1000).toISOString().replace('T', ' ').slice(0, 19);
function status(text, kind) { $('status').textContent = text; $('status').dataset.state = kind; }
function summary() {
  const rows = state.rows, last = rows.at(-1);
  $('latest').textContent = last ? signed((last[1] - 1) * 10000) : '—';
  $('latest').style.color = deviationColor(last ? last[1]-1 : 0);
  $('price').textContent = last ? last[1].toFixed(6) : '—';
  const values = rows.map(r => (r[1] - 1) * 10000);
  const low = values.reduce((a,b) => Math.min(a,b), Infinity), high = values.reduce((a,b) => Math.max(a,b), -Infinity);
  $('extremes').textContent = last ? `${signed(low)} / ${signed(high)}` : '—';
  $('window').textContent = `${date(state.start)} — ${date(state.end)} · ${rows.length.toLocaleString()} samples`;
  $('message').textContent = state.error || (last ? '' : 'No price samples in this window');
  $('message').hidden = !state.error && !!last;
  if (state.error) status('Connection error', 'error');
  else if (!state.live || state.zoom) status('Historical view', 'paused');
  else if (!last) status('Waiting for finalized prices', 'stale');
  else {
    const now = Date.now()/1000;
    const age = Math.max(0, Math.round(now - last[0]));
    // Finality normally trails wall time. Cache freshness is a separate signal.
    if (!Number.isFinite(state.fetchedAt) || now - state.fetchedAt > 30)
      status('Updates delayed · showing cached prices', 'stale');
    else if (age > 180)
      status(`Data delayed · last finalized sample ~${age}s ago`, 'stale');
    else status(`● Live · finalized ~${age}s ago`, 'live');
  }
}
async function refresh() {
  controller?.abort(); controller = new AbortController(); const ticket = ++generation; loading = true;
  const end = state.zoom?.[1] ?? (state.live || !state.end ? Date.now()/1000 : state.end);
  const start = state.zoom?.[0] ?? end - state.range;
  try {
    const response = await fetch(`/api/prices?start=${start}&end=${end}`, {signal: controller.signal});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Unable to load prices');
    if (ticket !== generation) return;
    state.fetchedAt = result.fetchedAt;
    state.rows = result.samples; state.start = start; state.end = end; state.error = null; state.hover = null;
  } catch (error) {
    if (error.name === 'AbortError' || ticket !== generation) return;
    state.error = error.message;
    if (!state.start) {state.start = start; state.end = end;}
  }
  loading = false; chartDirty = true; summary(); draw();
}
function draw() {
  const rect = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
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
  ctx.save();ctx.beginPath();ctx.rect(left,top,right-left,bottom-top);ctx.clip();
  // A hard color stop splits every segment precisely at the zero baseline.
  const lineColor = ctx.createLinearGradient(0,top,0,bottom);
  const zeroStop = (y(0)-top)/(bottom-top);
  lineColor.addColorStop(0,positiveColor);lineColor.addColorStop(zeroStop,positiveColor);
  lineColor.addColorStop(zeroStop,negativeColor);lineColor.addColorStop(1,negativeColor);
  const areaColor = ctx.createLinearGradient(0,top,0,bottom);
  areaColor.addColorStop(0,positiveColor+'38');areaColor.addColorStop(zeroStop,positiveColor+'10');
  areaColor.addColorStop(zeroStop,negativeColor+'10');areaColor.addColorStop(1,negativeColor+'38');
  const area = new Path2D();
  ctx.strokeStyle=lineColor;ctx.lineWidth=1.5;ctx.beginPath();let previous;
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
  ctx.setLineDash([]);ctx.strokeStyle='#ffffff';ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(left,y(0));ctx.lineTo(right,y(0));ctx.stroke();
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
    ctx.fillStyle=deviationColor(row[1]-1);ctx.beginPath();ctx.arc(px,py,3,0,Math.PI*2);ctx.fill();
    const tip=$('tooltip');tip.hidden=false;tip.replaceChildren();
    const stamp=document.createElement('div');stamp.textContent=`${date(row[0])} UTC`;
    const value=document.createElement('strong');value.textContent=`${signed((row[1]-1)*10000)} bps`;
    value.style.color=deviationColor(row[1]-1);
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
  if(Math.abs(a-b)>8){const time=px=>state.start+(px-geometry.left)/(geometry.right-geometry.left)*(state.end-state.start);const window=[time(Math.min(a,b)),time(Math.max(a,b))];if(window[1]-window[0]>=1){state.zoom=window;$('reset').hidden=false;refresh();}}
  canvas.releasePointerCapture(event.pointerId);draw();
});
canvas.addEventListener('pointercancel',()=>{state.drag=null;draw();});
function reset(){state.zoom=null;$('reset').hidden=true;refresh();}
canvas.addEventListener('dblclick',reset);$('reset').onclick=reset;
canvas.addEventListener('keydown',event=>{if(event.key==='Escape')reset();if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();state.hover=Math.max(0,Math.min(state.rows.length-1,(state.hover??state.rows.length-1)+(event.key==='ArrowRight'?1:-1)));draw();}});
function updateRangeButtons() {
  document.querySelectorAll('[data-range]').forEach(button =>
    button.setAttribute('aria-pressed', String(Number(button.dataset.range) === state.range)));
}
document.querySelectorAll('[data-range]').forEach(button => button.onclick = () => {
  state.range = Number(button.dataset.range);
  const resolution = [...resolutions].find(([, seconds]) => seconds === state.range)[0];
  // Share only the rolling window size, never absolute timestamps or a zoom.
  const url = new URL(location.pathname, location.origin);
  url.searchParams.set('resolution', resolution);
  if (url.href !== location.href) history.pushState(null, '', url);
  updateRangeButtons();
  reset();
});
window.addEventListener('popstate', () => {
  state.range = linkedRange();
  updateRangeButtons();
  reset();
});
updateRangeButtons();
$('live').onclick=()=>{state.live=!state.live;$('live').setAttribute('aria-pressed',String(state.live));$('live').lastChild.textContent=state.live?' Live · 6s':' Paused';if(state.live)reset();else summary();};
$('refresh').onclick=refresh;
new ResizeObserver(draw).observe($('plot'));
setInterval(()=>{if(state.live&&!state.zoom&&!document.hidden&&!loading)refresh();},6000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.live&&!state.zoom)refresh();});
refresh();
