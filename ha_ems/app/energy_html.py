"""Energy dashboard HTML — served at /energy."""

ENERGY_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Energy — HA EMS</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#111827;--card:#1f2937;--border:#374151;
    --text:#f9fafb;--muted:#9ca3af;
    --solar:#f59e0b;--bat:#10b981;--bat-charge:#e91e8c;
    --grid:#7c4dff;--home:#f59e0b;--lowcarbon:#10b981;
    --accent:#10b981;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;padding:1rem;padding-bottom:3rem}
  nav{display:flex;gap:.4rem;margin-bottom:1rem;border-bottom:1px solid var(--border);padding-bottom:.6rem;flex-wrap:wrap}
  .nav-btn{padding:.35rem .85rem;border-radius:.5rem;border:1px solid transparent;background:none;color:var(--muted);cursor:pointer;font-size:.85rem;text-decoration:none}
  .nav-btn.active,.nav-btn:hover{background:var(--card);border-color:var(--border);color:var(--text)}
  h1{font-size:1.2rem;font-weight:600;margin-bottom:1rem;display:flex;align-items:center;gap:.5rem}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--accent);animation:pulse 2s infinite;display:inline-block}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

  /* Layout */
  .layout{display:grid;grid-template-columns:1fr 300px;gap:1rem}
  @media(max-width:900px){.layout{grid-template-columns:1fr}}
  @media(max-width:600px){.layout{gap:.6rem}}
  .left{display:flex;flex-direction:column;gap:1rem}
  .right{display:flex;flex-direction:column;gap:1rem}

  /* Cards */
  .card{background:var(--card);border:1px solid var(--border);border-radius:.75rem;padding:1rem}
  @media(max-width:480px){.card{padding:.75rem .65rem;border-radius:.5rem}}
  .card-title{font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:.75rem;font-weight:600}
  .card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:.75rem}
  .card-value-big{font-size:1rem;font-weight:700;color:var(--solar)}

  /* Chart containers */
  .chart-wrap{position:relative;height:180px}
  @media(max-width:600px){.chart-wrap{height:140px}}

  /* EPEX stat pills */
  .epex-pills{display:grid;grid-template-columns:repeat(4,1fr);gap:.5rem;margin-bottom:.75rem}
  @media(max-width:600px){.epex-pills{grid-template-columns:repeat(2,1fr)}}
  @media(max-width:600px){.pill-val{font-size:.85rem}}
  .pill{background:#0f172a;border:1px solid var(--border);border-radius:.5rem;padding:.5rem .75rem;text-align:center}
  .pill-label{font-size:.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
  .pill-val{font-size:1rem;font-weight:700;margin-top:.15rem}
  .pill-val.green{color:var(--bat)}
  .pill-val.red{color:#ef4444}
  .pill-val.yellow{color:var(--solar)}
  .pill-val.purple{color:var(--grid)}

  /* Flow diagram */
  #flow-svg{width:100%;overflow:visible}

  /* Gauge grid */
  .gauge-grid{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}
  @media(max-width:480px){.gauge-grid{gap:.4rem}}
  .gauge-wrap{display:flex;flex-direction:column;align-items:center;padding:.5rem}
  .gauge-canvas-wrap{position:relative;width:100px;height:60px;overflow:hidden}
  .gauge-label{position:absolute;bottom:0;left:50%;transform:translateX(-50%);font-size:14px;font-weight:700;white-space:nowrap}
  .gauge-sub{font-size:.65rem;color:var(--muted);text-align:center;margin-top:.3rem;line-height:1.3;max-width:100px}

  /* Price table */
  .price-table{width:100%;border-collapse:collapse;font-size:.78rem}
  .price-table th{color:var(--muted);font-size:.65rem;text-transform:uppercase;padding:.3rem .5rem;border-bottom:1px solid var(--border)}
  .price-table td{padding:.3rem .5rem;border-bottom:1px solid #1f2937}
  .price-table tr.current td{background:#0f2318;color:var(--bat);font-weight:700}
  .price-bar{height:6px;border-radius:3px;margin-top:2px}

  /* Day toggle */
  .day-toggle{display:flex;gap:.4rem;margin-bottom:.6rem}
  .day-btn{padding:.25rem .7rem;border-radius:.4rem;border:1px solid var(--border);background:none;color:var(--muted);cursor:pointer;font-size:.78rem}
  .day-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}

  .updated{font-size:.65rem;color:var(--border);text-align:right;margin-top:.4rem}
  .no-epex{font-size:.82rem;color:var(--muted);text-align:center;padding:2rem 0}

  /* Schedule plan table */
  .plan-table{width:100%;border-collapse:collapse;font-size:.75rem}
  .plan-table th{color:var(--muted);font-size:.63rem;text-transform:uppercase;padding:.3rem .4rem;border-bottom:1px solid var(--border)}
  .plan-table td{padding:.3rem .4rem;border-bottom:1px solid #1a2233}
  .plan-table tr.plan-now td{background:#0f2318;font-weight:700}
  @media(max-width:480px){.plan-table .col-solar,.plan-table .col-load{display:none}}
  @media(max-width:480px){.plan-table td,.plan-table th{padding:.2rem .3rem;font-size:.7rem}}
  .plan-badge{display:inline-block;padding:.1rem .45rem;border-radius:.3rem;font-size:.68rem;font-weight:600}
  .plan-badge.charge{background:#1a3a2a;color:#34d399}
  .plan-badge.discharge{background:#3a1a1a;color:#f87171}
  .plan-badge.idle{background:#1f2937;color:var(--muted)}
  .no-plan{font-size:.82rem;color:var(--muted);text-align:center;padding:1.5rem 0}
</style>
</head>
<body>
<h1><span class="dot"></span> HA EMS</h1>
<nav>
  <a class="nav-btn" href="/">Dashboard</a>
  <a class="nav-btn active" href="/energy">Energy</a>
  <a class="nav-btn" href="/" onclick="localStorage.setItem('tab','settings');return true;">Settings</a>
</nav>

<div class="layout">

  <!-- LEFT -->
  <div class="left">

    <!-- EPEX price chart -->
    <div class="card" id="epex-card">
      <div class="card-header">
        <span class="card-title">EPEX SPOT Prices</span>
        <div class="day-toggle">
          <button class="day-btn active" onclick="showDay('today',this)">Today</button>
          <button class="day-btn" id="tmrw-btn" onclick="showDay('tomorrow',this)" style="display:none">Tomorrow</button>
        </div>
      </div>
      <!-- Stat pills -->
      <div class="epex-pills">
        <div class="pill"><div class="pill-label">Now</div><div class="pill-val yellow" id="p-now">--</div></div>
        <div class="pill"><div class="pill-label">Next slot</div><div class="pill-val" id="p-next">--</div></div>
        <div class="pill"><div class="pill-label">Min today</div><div class="pill-val green" id="p-min">--</div></div>
        <div class="pill"><div class="pill-label">Max today</div><div class="pill-val red" id="p-max">--</div></div>
      </div>
      <div class="chart-wrap"><canvas id="epexChart"></canvas></div>
      <div class="no-epex" id="no-epex" style="display:none">
        No EPEX data — add your ENTSO-E token in the add-on Configuration tab.
      </div>
      <div class="updated" id="epex-updated"></div>
    </div>

    <!-- Energy flow -->
    <div class="card">
      <div class="card-title">Live Energy Flow</div>
      <svg id="flow-svg" viewBox="0 0 320 200" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <filter id="glow"><feGaussianBlur stdDeviation="2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
          <filter id="shadow"><feDropShadow dx="0" dy="1" stdDeviation="2" flood-color="#0003"/></filter>
        </defs>
        <g id="flow-paths"></g>
        <g id="flow-particles"></g>
        <g id="flow-nodes"></g>
      </svg>
    </div>

  </div>

  <!-- RIGHT -->
  <div class="right">

    <!-- Today's price schedule -->
    <div class="card" style="max-height:420px;overflow-y:auto">
      <div class="card-title" id="schedule-title">Price schedule — Today</div>
      <table class="price-table">
        <thead><tr><th>Time</th><th>€/kWh</th><th>Bar</th></tr></thead>
        <tbody id="schedule-body"></tbody>
      </table>
    </div>


    <!-- 24h Optimization Plan -->
    <div class="card" style="max-height:420px;overflow-y:auto" id="plan-card">
      <div class="card-title">24h Optimization Plan</div>
      <div class="no-plan" id="no-plan">No schedule — configure panel or EPEX prices</div>
      <table class="plan-table" id="plan-table" style="display:none">
        <thead><tr><th>Time</th><th class="col-solar">Solar</th><th class="col-load">Load</th><th>Price</th><th>Battery</th></tr></thead>
        <tbody id="plan-body"></tbody>
      </table>
      <div class="updated" id="plan-updated"></div>
    </div>

    <!-- Gauges -->
    <div class="card">
      <div class="card-title">Stats</div>
      <div class="gauge-grid">
        <div class="gauge-wrap">
          <div class="gauge-canvas-wrap">
            <canvas id="g-bat" width="100" height="60"></canvas>
            <div class="gauge-label" id="g-bat-lbl" style="color:var(--bat)">--%</div>
          </div>
          <div class="gauge-sub">Battery SOC</div>
        </div>
        <div class="gauge-wrap">
          <div class="gauge-canvas-wrap">
            <canvas id="g-self" width="100" height="60"></canvas>
            <div class="gauge-label" id="g-self-lbl" style="color:var(--solar)">--%</div>
          </div>
          <div class="gauge-sub">Solar surplus %</div>
        </div>
        <div class="gauge-wrap">
          <div class="gauge-canvas-wrap">
            <canvas id="g-min" width="100" height="60"></canvas>
            <div class="gauge-label" id="g-min-lbl" style="color:var(--bat)">-- ct</div>
          </div>
          <div class="gauge-sub">Min price today</div>
        </div>
        <div class="gauge-wrap">
          <div class="gauge-canvas-wrap">
            <canvas id="g-max" width="100" height="60"></canvas>
            <div class="gauge-label" id="g-max-lbl" style="color:#ef4444">-- ct</div>
          </div>
          <div class="gauge-sub">Max price today</div>
        </div>
      </div>
    </div>

  </div>
</div>

<script>
const BASE = window.location.pathname.replace(/\/energy\/?$/, '');
let _epex = null, _state = null, _epexChart = null, _currentDay = 'today';

// ── Fetch ──
async function loadAll() {
  try { _state = await fetch(BASE+'/api/state').then(r=>r.json()); } catch(e){}
  try { _epex  = await fetch(BASE+'/api/epex').then(r=>r.json());  } catch(e){}

  try { _forecast = await fetch(BASE+'/api/forecast').then(r=>r.json()); } catch(e){}
  render();
}

function fmt(v, digits=4) { return v != null ? (v*100).toFixed(2)+' ct' : '--'; }
function fmtEur(v) { return v != null ? v.toFixed(4)+' €' : '--'; }

// ── Render ──
function render() {
  renderEpex();
  renderFlow();
  renderGauges();
  renderPlan();
}

// ── EPEX ──
let _epexChartInst = null;

function renderEpex() {
  if (!_epex || _epex.error || !_epex.prices_today) {
    document.getElementById('no-epex').style.display = 'block';
    document.getElementById('epexChart').style.display = 'none';
    document.querySelectorAll('.pill-val').forEach(el => el.textContent = '--');
    return;
  }
  document.getElementById('no-epex').style.display = 'none';
  document.getElementById('epexChart').style.display = 'block';

  document.getElementById('p-now').textContent  = fmt(_epex.current_price);
  document.getElementById('p-next').textContent = fmt(_epex.next_slot_price);
  document.getElementById('p-min').textContent  = fmt(_epex.today_min);
  document.getElementById('p-max').textContent  = fmt(_epex.today_max);
  document.getElementById('epex-updated').textContent = 'Zone: '+(_epex.zone||'--')+' · '+(_epex.slot_minutes||60)+' min slots';

  // Tomorrow button
  if (_epex.prices_tomorrow && _epex.prices_tomorrow.length > 0) {
    document.getElementById('tmrw-btn').style.display = '';
  }

  drawEpexChart(_currentDay === 'today' ? _epex.prices_today : _epex.prices_tomorrow);
  renderSchedule(_currentDay === 'today' ? _epex.prices_today : _epex.prices_tomorrow, _currentDay);
}

function showDay(day, btn) {
  _currentDay = day;
  document.querySelectorAll('.day-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (!_epex) return;
  const slots = day === 'today' ? _epex.prices_today : _epex.prices_tomorrow;
  drawEpexChart(slots);
  renderSchedule(slots, day);
}

function drawEpexChart(slots) {
  if (!slots || !slots.length) return;
  const now = new Date();
  const labels = slots.map(s => new Date(s.start).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}));
  const vals   = slots.map(s => +(s.price_eur_kwh * 100).toFixed(4));
  const colors = slots.map(s => {
    const isCurrent = new Date(s.start) <= now && now < new Date(s.end);
    if (isCurrent) return 'rgba(245,158,11,0.95)';
    const v = s.price_eur_kwh;
    const mn = Math.min(...slots.map(x=>x.price_eur_kwh));
    const mx = Math.max(...slots.map(x=>x.price_eur_kwh));
    const ratio = mx > mn ? (v - mn)/(mx - mn) : 0.5;
    const r = Math.round(16 + ratio*239), g = Math.round(185 - ratio*151), b = Math.round(129 - ratio*100);
    return `rgba(${r},${g},${b},0.85)`;
  });

  const ctx = document.getElementById('epexChart').getContext('2d');
  if (_epexChartInst) _epexChartInst.destroy();
  _epexChartInst = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets: [{ data: vals, backgroundColor: colors, borderRadius: 2 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 600 },
      plugins: { legend: { display:false }, tooltip: { callbacks: { label: ctx => ` ${ctx.parsed.y.toFixed(2)} ct/kWh` }}},
      scales: {
        x: { ticks: { color:'#6b7280', maxTicksLimit:12, font:{size:10} }, grid:{ display:false } },
        y: { ticks: { color:'#6b7280', font:{size:10}, callback: v=>v+' ct' }, grid:{ color:'#1f2937' } }
      }
    }
  });
}

function renderSchedule(slots, day) {
  const tbody = document.getElementById('schedule-body');
  const title = document.getElementById('schedule-title');
  title.textContent = 'Price schedule — ' + (day==='today'?'Today':'Tomorrow');
  if (!slots || !slots.length) { tbody.innerHTML = '<tr><td colspan="3" style="text-align:center;color:var(--muted);padding:1rem">No data</td></tr>'; return; }
  const now = new Date();
  const mn = Math.min(...slots.map(s=>s.price_eur_kwh));
  const mx = Math.max(...slots.map(s=>s.price_eur_kwh));
  tbody.innerHTML = slots.map(s => {
    const isCur = new Date(s.start) <= now && now < new Date(s.end);
    const pct = mx > mn ? Math.round((s.price_eur_kwh - mn)/(mx-mn)*100) : 50;
    const col = pct < 33 ? 'var(--bat)' : pct > 66 ? '#ef4444' : 'var(--solar)';
    const t = new Date(s.start).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
    return `<tr class="${isCur?'current':''}">
      <td>${t}</td>
      <td>${(s.price_eur_kwh*100).toFixed(2)} ct</td>
      <td><div class="price-bar" style="width:${Math.max(4,pct)}%;background:${col}"></div></td>
    </tr>`;
  }).join('');
  if (day==='today') {
    const curRow = tbody.querySelector('tr.current');
    if (curRow) curRow.scrollIntoView({block:'center',behavior:'smooth'});
  }
}

// ── Flow diagram ──
const SVG_NS = 'http://www.w3.org/2000/svg';
let _particles = [], _animFrame = null;

function buildFlow() {
  const solar_w = _state?.solar_w ?? 0;
  const grid_w  = _state?.grid_w  ?? 0;
  const bat_soc = _state?.battery_soc ?? 0;
  const bat_w   = _state?.battery_w ?? 0;  // negative = charging, positive = discharging

  const importing   = grid_w  >  50;
  const exporting   = grid_w  < -50;
  const charging    = bat_w   < -50;   // battery absorbing power
  const discharging = bat_w   >  50;   // battery delivering power
  const bat_abs     = Math.abs(bat_w);

  const nodes = [
    { id:'solar',   x:160, y:25,  r:28, color:'#f59e0b', icon:'☀️', label:'Solar',   sub: solar_w+'W' },
    { id:'grid',    x:55,  y:115, r:26, color:'#7c4dff', icon:'⚡',  label:'Grid',    sub: Math.abs(grid_w)+'W' },
    { id:'battery', x:160, y:175, r:28, color:'#10b981', icon:'🔋', label:'Battery', sub: bat_soc+'%' },
    { id:'home',    x:265, y:115, r:28, color:'#f59e0b', icon:'🏠', label:'Home',    sub: (_state?.net_power_w ?? 0)+'W' },
  ];

  const edges = [
    solar_w > 50  && { from:'solar',   to:'home',    color:'#f59e0b' },
    solar_w > 50  && charging && { from:'solar',   to:'battery', color:'#e91e8c' },
    importing     && charging && { from:'grid',    to:'battery', color:'#7c4dff' },
    importing     && { from:'grid',    to:'home',    color:'#7c4dff' },
    discharging   && { from:'battery', to:'home',    color:'#10b981' },
    exporting     && { from:'solar',   to:'grid',    color:'#10b981' },
    exporting     && discharging && { from:'battery', to:'grid',  color:'#10b981' },
  ].filter(Boolean);

  const pathsG    = document.getElementById('flow-paths');
  const particlesG= document.getElementById('flow-particles');
  const nodesG    = document.getElementById('flow-nodes');
  pathsG.innerHTML = ''; particlesG.innerHTML = ''; nodesG.innerHTML = '';
  _particles = [];
  if (_animFrame) cancelAnimationFrame(_animFrame);

  const byId = id => nodes.find(n=>n.id===id);

  // Draw edges
  const pathEls = [];
  edges.forEach(e => {
    const a=byId(e.from), b=byId(e.to);
    const cx = a.x + (b.x-a.x)*0.5;
    const p = document.createElementNS(SVG_NS,'path');
    p.setAttribute('d',`M${a.x},${a.y} C${cx},${a.y} ${cx},${b.y} ${b.x},${b.y}`);
    p.setAttribute('fill','none'); p.setAttribute('stroke',e.color);
    p.setAttribute('stroke-width','2.5'); p.setAttribute('opacity','0.4');
    p.setAttribute('stroke-linecap','round');
    pathsG.appendChild(p);
    pathEls.push({el:p, color:e.color});
  });

  // Draw nodes
  nodes.forEach(n => {
    const g = document.createElementNS(SVG_NS,'g');
    const glow = document.createElementNS(SVG_NS,'circle');
    glow.setAttribute('cx',n.x); glow.setAttribute('cy',n.y); glow.setAttribute('r',n.r+6);
    glow.setAttribute('fill',n.color); glow.setAttribute('opacity','0.1');
    g.appendChild(glow);
    const c = document.createElementNS(SVG_NS,'circle');
    c.setAttribute('cx',n.x); c.setAttribute('cy',n.y); c.setAttribute('r',n.r);
    c.setAttribute('fill','#1f2937'); c.setAttribute('stroke',n.color); c.setAttribute('stroke-width','2');
    g.appendChild(c);
    const icon = document.createElementNS(SVG_NS,'text');
    icon.setAttribute('x',n.x); icon.setAttribute('y',n.y+1);
    icon.setAttribute('text-anchor','middle'); icon.setAttribute('dominant-baseline','middle');
    icon.setAttribute('font-size',n.r*0.75); icon.textContent = n.icon;
    g.appendChild(icon);
    const lbl = document.createElementNS(SVG_NS,'text');
    lbl.setAttribute('x',n.x); lbl.setAttribute('y',n.y+n.r+11);
    lbl.setAttribute('text-anchor','middle'); lbl.setAttribute('font-size','9');
    lbl.setAttribute('fill','#9ca3af'); lbl.setAttribute('font-weight','600');
    lbl.textContent = n.label; g.appendChild(lbl);
    const sub = document.createElementNS(SVG_NS,'text');
    sub.setAttribute('x',n.x); sub.setAttribute('y',n.y+n.r+21);
    sub.setAttribute('text-anchor','middle'); sub.setAttribute('font-size','9');
    sub.setAttribute('fill',n.color); sub.setAttribute('font-weight','700');
    sub.textContent = n.sub; g.appendChild(sub);
    nodesG.appendChild(g);
  });

  // Particles
  pathEls.forEach(({el, color}) => {
    const len = el.getTotalLength();
    for (let i=0; i<3; i++) {
      const c = document.createElementNS(SVG_NS,'circle');
      c.setAttribute('r','3'); c.setAttribute('fill',color);
      particlesG.appendChild(c);
      _particles.push({el:c, path:el, len, progress: i/3, speed:0.003+Math.random()*0.001});
    }
  });
  animParticles();
}

function animParticles() {
  _particles.forEach(p => {
    p.progress += p.speed;
    if (p.progress > 1) p.progress -= 1;
    const pt = p.path.getPointAtLength(p.progress * p.len);
    p.el.setAttribute('cx', pt.x); p.el.setAttribute('cy', pt.y);
    p.el.setAttribute('opacity', (0.4 + Math.sin(p.progress*Math.PI)*0.6).toFixed(2));
  });
  _animFrame = requestAnimationFrame(animParticles);
}

function renderFlow() { buildFlow(); }

// ── Gauges ──
const _gauges = {};
function drawGauge(id, value, max, color, suffix) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const cx=50, cy=58, r=40;
  let cur = _gauges[id] ?? 0;
  const target = Math.min(value/max, 1);
  function frame() {
    cur += (target - cur) * 0.06;
    ctx.clearRect(0,0,100,65);
    ctx.beginPath(); ctx.arc(cx,cy,r,Math.PI,2*Math.PI);
    ctx.strokeStyle='#374151'; ctx.lineWidth=10; ctx.lineCap='round'; ctx.stroke();
    if (cur > 0.01) {
      ctx.beginPath(); ctx.arc(cx,cy,r,Math.PI,Math.PI+cur*Math.PI);
      ctx.strokeStyle=color; ctx.lineWidth=10; ctx.lineCap='round'; ctx.stroke();
    }
    if (Math.abs(cur-target) > 0.003) requestAnimationFrame(frame);
    else _gauges[id] = cur;
  }
  frame();
}

function renderGauges() {
  const bat = _state?.battery_soc ?? 0;
  const solar = _state?.solar_w ?? 0;
  const grid  = _state?.grid_w  ?? 0;
  const selfPct = solar > 0 ? Math.min(100, Math.max(0, Math.round((solar - Math.max(0,grid))/solar*100))) : 0;
  drawGauge('g-bat',  bat,    100, 'var(--bat)',  '%');
  drawGauge('g-self', selfPct,100, 'var(--solar)','%');
  document.getElementById('g-bat-lbl').textContent  = bat+'%';
  document.getElementById('g-self-lbl').textContent = selfPct+'%';

  if (_epex && !_epex.error) {
    const mn = _epex.today_min, mx = _epex.today_max;
    const spread = mx && mn ? mx - mn : 0.1;
    drawGauge('g-min', mn ?? 0, 0.3, 'var(--bat)', 'ct');
    drawGauge('g-max', mx ?? 0, 0.3, '#ef4444',    'ct');
    document.getElementById('g-min-lbl').textContent = mn != null ? (mn*100).toFixed(1)+' ct' : '--';
    document.getElementById('g-max-lbl').textContent = mx != null ? (mx*100).toFixed(1)+' ct' : '--';
  }
}


// ── 24h Plan ──
let _forecast = null;

function renderPlan() {
  if (!_forecast || !_forecast.schedule || !_forecast.schedule.length) {
    document.getElementById('no-plan').style.display = '';
    document.getElementById('plan-table').style.display = 'none';
    return;
  }
  document.getElementById('no-plan').style.display = 'none';
  document.getElementById('plan-table').style.display = '';

  const nowH = new Date().getHours();
  const tbody = document.getElementById('plan-body');
  tbody.innerHTML = '';
  const fmtW = w => w >= 1000 ? (w/1000).toFixed(1)+'k' : Math.round(w)+'';
  const fmtP = p => p != null ? (p*100).toFixed(1) : '--';

  for (const slot of _forecast.schedule) {
    const h = new Date(slot.hour).getHours();
    const isCurrent = h === nowH;
    const tr = document.createElement('tr');
    if (isCurrent) tr.className = 'plan-now';
    const badge = `<span class="plan-badge ${slot.battery_action}">${slot.battery_action}</span>`;
    tr.innerHTML = `
      <td>${slot.hour_label}${isCurrent ? ' ◀' : ''}</td>
      <td class="col-solar">${fmtW(slot.solar_w)} W</td>
      <td class="col-load">${fmtW(slot.consumption_w)} W</td>
      <td>${fmtP(slot.buy_price)} ct</td>
      <td>${badge}</td>`;
    tbody.appendChild(tr);
    if (isCurrent) tr.scrollIntoView({block:'nearest'});
  }
  const builtEl = document.getElementById('plan-updated');
  if (builtEl && _forecast.built_at) {
    const d = new Date(_forecast.built_at);
    builtEl.textContent = 'Built ' + d.getHours() + ':' + String(d.getMinutes()).padStart(2,'0');
  }
}

// ── Init ──
loadAll();
setInterval(loadAll, 30000);
</script>
</body>
</html>
"""