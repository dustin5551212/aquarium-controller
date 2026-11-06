'use strict';

const META = {
  temp_c:      { label: 'Temperature',  unit: '°C',      dp: 2, color: '#35c2e8' },
  ph:          { label: 'pH',           unit: '',        dp: 2, color: '#7bd88f' },
  salinity_ppt:{ label: 'Salinity',     unit: 'ppt',     dp: 2, color: '#b28dff' },
  ec_us:       { label: 'Conductivity', unit: 'µS/cm',   dp: 0, color: '#6fb1ff' },
  tds_ppm:     { label: 'TDS',          unit: 'ppm',     dp: 0, color: '#6fb1ff' },
  orp_mv:      { label: 'ORP',          unit: 'mV',      dp: 0, color: '#f0b429' },
  do_mg_l:     { label: 'Dissolved O₂', unit: 'mg/L',    dp: 2, color: '#4fd1c5' },
  level_pct:   { label: 'Sump level',   unit: '%',       dp: 1, color: '#8ad4a8' },
  turbidity_ntu:{label: 'Turbidity',    unit: 'NTU',     dp: 0, color: '#c0a080' },
};

const ORDER = ['temp_c', 'ph', 'salinity_ppt', 'do_mg_l', 'orp_mv', 'level_pct', 'ec_us', 'tds_ppm', 'turbidity_ntu'];
const KIND_LABEL = {
  heater: 'Heater', chiller: 'Chiller', ato: 'Top-off pump', doser: 'Dosing pump',
  co2: 'CO₂ solenoid', light: 'Light', pump: 'Pump', fan: 'Fan',
};

const st = {
  token: localStorage.getItem('aq-token') || new URLSearchParams(location.search).get('token') || '',
  series: {},
  minutes: 360,
  cards: new Map(),
  lastPoint: {},
  connected: false,
  askedToken: false,
};

const $ = (sel) => document.querySelector(sel);

function api(path) {
  if (!st.token) return path;
  return path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(st.token);
}

async function getJSON(path) {
  const resp = await fetch(api(path), { cache: 'no-store' });
  if (resp.status === 401) {
    await askToken();
    return getJSON(path);
  }
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return resp.json();
}

async function askToken() {
  if (st.askedToken) throw new Error('unauthorised');
  st.askedToken = true;
  const value = prompt('This controller requires an API token:');
  if (!value) throw new Error('unauthorised');
  st.token = value.trim();
  localStorage.setItem('aq-token', st.token);
  st.askedToken = false;
}

function fmt(value, dp) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number(value).toFixed(dp);
}

function toast(message, bad = false) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('bad', bad);
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), 2600);
}

/* ---------------------------------------------------------------- rendering */

function buildCard(metric) {
  const meta = META[metric] || { label: metric, unit: '', dp: 2, color: '#35c2e8' };
  const el = document.createElement('article');
  el.className = 'card';
  el.innerHTML = `
    <div class="card-head"><span class="label"></span><span class="flag"></span></div>
    <div class="value"><span class="num">—</span><span class="unit"></span></div>
    <div class="sub"><span class="delta"></span><span class="state"></span></div>
    <canvas></canvas>`;
  el.querySelector('.label').textContent = meta.label;
  el.querySelector('.unit').textContent = meta.unit;
  $('#cards').appendChild(el);
  st.cards.set(metric, el);
  return el;
}

function ensureCards(metrics) {
  const present = new Set(metrics);
  const water = st.snap?.tank?.water_type;
  if (water === 'reef') present.delete('tds_ppm');
  if (water === 'fresh') present.delete('salinity_ppt');
  let previous = null;
  for (const metric of ORDER) {
    if (!present.has(metric)) continue;
    const el = st.cards.get(metric) || buildCard(metric);
    if (previous) previous.after(el); else $('#cards').prepend(el);
    previous = el;
  }
}

function alertSeverityFor(metric, alerts) {
  let level = 0;
  for (const a of alerts || []) {
    if (a.metric !== metric) continue;
    level = Math.max(level, a.severity === 'critical' ? 2 : a.severity === 'warning' ? 1 : 1);
  }
  return level;
}

function deltaFor(metric, now, value) {
  const series = st.series[metric];
  if (!series || series.t.length < 2) return '';
  const target = now - 3600;
  let best = null;
  for (let i = 0; i < series.t.length; i++) {
    if (series.t[i] <= target) best = series.v[i];
    else break;
  }
  if (best === null) best = series.v[0];
  const diff = value - best;
  if (Math.abs(diff) < Math.pow(10, -(META[metric]?.dp ?? 2))) return 'steady last hour';
  return `${diff > 0 ? '+' : ''}${diff.toFixed(META[metric]?.dp ?? 2)} in 1h`;
}

function loopStateFor(metric, snap) {
  const loops = snap.loops || {};
  const info = (snap.readings || {})[metric] || {};
  if (metric === 'temp_c' && loops.temp) {
    const t = loops.temp;
    return `target ${t.target} · ${t.state}`;
  }
  if (metric === 'ph' && loops.co2) return `CO₂ ${loops.co2.state}`;
  if (metric === 'level_pct' && loops.ato) return `top-off ${loops.ato.state}`;
  if (info.source === 'derived') return 'derived from EC';
  if (metric === 'salinity_ppt') return 'reef target 35.0';
  if (metric === 'orp_mv') return 'target 300–400';
  if (metric === 'do_mg_l') return 'target > 6.0';
  return '';
}

function drawLine(canvas, times, values, color) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 200;
  const h = canvas.clientHeight || 42;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!times || times.length < 2) return;

  const t0 = times[0];
  const t1 = times[times.length - 1] || t0 + 1;
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (max - min < 1e-9) { max += 1; min -= 1; }
  const pad = (max - min) * 0.18;
  min -= pad; max += pad;

  const X = (t) => ((t - t0) / Math.max(1, t1 - t0)) * (w - 2) + 1;
  const Y = (v) => h - ((v - min) / (max - min)) * (h - 4) - 2;

  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + '38');
  grad.addColorStop(1, color + '00');
  ctx.beginPath();
  ctx.moveTo(X(times[0]), h);
  for (let i = 0; i < times.length; i++) ctx.lineTo(X(times[i]), Y(values[i]));
  ctx.lineTo(X(times[times.length - 1]), h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.beginPath();
  for (let i = 0; i < times.length; i++) {
    const x = X(times[i]), y = Y(values[i]);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.stroke();

  const lx = X(times[times.length - 1]), ly = Y(values[values.length - 1]);
  ctx.beginPath();
  ctx.arc(lx, ly, 2.1, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
}

function render(snap) {
  st.snap = snap;
  const metrics = Object.keys(snap.readings || {});
  ensureCards(metrics);

  const water = snap.tank?.water_type;
  for (const metric of metrics) {
    if (water === 'reef' && metric === 'tds_ppm') continue;
    if (water === 'fresh' && metric === 'salinity_ppt') continue;
    const el = st.cards.get(metric);
    const info = snap.readings[metric];
    if (!el || !info) continue;
    const meta = META[metric] || { dp: 2 };
    el.querySelector('.num').textContent = fmt(info.value, meta.dp);

    const level = alertSeverityFor(metric, snap.alerts);
    el.classList.toggle('warn', level === 1);
    el.classList.toggle('crit', level >= 2);

    el.querySelector('.delta').textContent = deltaFor(metric, snap.now, info.value);
    el.querySelector('.state').textContent = loopStateFor(metric, snap);

    const series = st.series[metric];
    if (series) drawLine(el.querySelector('canvas'), series.t, series.v,
                        (META[metric] || {}).color || '#35c2e8');
  }

  $('#tank').textContent = snap.tank?.name || 'Aquarium';
  const mode = $('#mode');
  mode.textContent = snap.mode === 'simulation' ? 'simulation' : 'hardware';
  mode.className = 'chip ' + (snap.mode === 'simulation' ? 'sim' : 'hw');

  renderAlerts(snap.alerts || []);
  renderEquipment(snap);
  renderDosing(snap.dosing || {});

  const hours = (snap.uptime_s / 3600).toFixed(1);
  $('#footer-info').textContent =
    `${snap.tank?.volume_l ?? '—'} L · ${snap.samples ?? 0} samples stored · up ${hours} h · ${snap.mode}`;

  pushLivePoints(snap);
}

function renderAlerts(alerts) {
  const box = $('#alerts');
  if (!alerts.length) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  box.classList.remove('hidden');
  box.innerHTML = alerts.map((a) => `
    <div class="alert ${a.severity}">
      <span class="sev">${a.severity}</span>
      <span>${escapeHTML(a.message)}</span>
      <span class="age">${Math.round(a.age_s)}s</span>
      <button class="dismiss" data-rule="${escapeHTML(a.rule)}" title="Acknowledge">×</button>
    </div>`).join('');
  box.querySelectorAll('.dismiss').forEach((btn) => {
    btn.addEventListener('click', () => clearAlert(btn.dataset.rule));
  });
}

function renderEquipment(snap) {
  const box = $('#equipment');
  box.innerHTML = snap.actuators.map((a) => {
    const label = a.label || KIND_LABEL[a.kind] || a.id;
    const manual = a.mode === 'manual';
    return `
      <div class="equip" data-id="${a.id}">
        <div>
          <span class="equip-name">${escapeHTML(label)}</span>
          <span class="equip-reason">${escapeHTML(a.reason || 'idle')}</span>
        </div>
        <span class="pill ${a.on ? 'on' : ''} ${manual ? 'manual' : ''}">${manual ? 'manual' : (a.on ? 'on' : 'off')}</span>
        <div style="display:flex;gap:6px">
          <button class="btn" data-act="toggle" data-on="${a.on ? '0' : '1'}">${a.on ? 'Turn off' : 'Turn on'}</button>
          ${manual ? '<button class="btn ghost" data-act="auto">Auto</button>' : ''}
        </div>
      </div>`;
  }).join('');
  box.querySelectorAll('.equip').forEach((row) => {
    const id = row.dataset.id;
    row.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (btn.dataset.act === 'toggle') setActuator(id, btn.dataset.on === '1');
        else clearOverride(id);
      });
    });
  });
}

function renderDosing(dosing) {
  const box = $('#dosing');
  const pumps = Object.keys(dosing);
  if (!pumps.length) {
    box.innerHTML = '<div class="dose-note">No dosing schedule configured.</div>';
    return;
  }
  const labels = {};
  for (const a of (st.snap?.actuators || [])) labels[a.id] = a.label || a.id;
  box.innerHTML = pumps.map((pump) => {
    const d = dosing[pump];
    const pct = Math.min(100, (d.today_ml / Math.max(1, d.cap_ml)) * 100);
    return `
      <div class="dose-row">
        <span class="name">${escapeHTML(labels[pump] || pump)}</span>
        <span class="amt">${Number(d.today_ml).toFixed(1)} / ${Number(d.cap_ml).toFixed(0)} ml</span>
        <div class="dose-bar"><i style="width:${pct.toFixed(1)}%"></i></div>
        <span class="dose-note">next ${d.next ? escapeHTML(d.next) : '—'} · ${pct.toFixed(0)}% of daily cap</span>
      </div>`;
  }).join('');
}

function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

/* ------------------------------------------------------------------ actions */

async function setActuator(id, on, minutes = 15) {
  try {
    const resp = await fetch(api(`/api/actuators/${id}`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on, minutes }),
    });
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.error || resp.statusText);
    toast(`${id} ${on ? 'on' : 'off'} for ${minutes} min`);
  } catch (err) {
    toast(String(err.message || err), true);
  }
}

async function clearOverride(id) {
  try {
    const resp = await fetch(api(`/api/actuators/${id}`), { method: 'DELETE' });
    if (!resp.ok) throw new Error((await resp.json()).error || resp.statusText);
    toast(`${id} back to automatic`);
  } catch (err) {
    toast(String(err.message || err), true);
  }
}

async function clearAlert(rule) {
  try {
    await fetch(api('/api/alerts/clear'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rule }),
    });
    toast('alert acknowledged');
  } catch (err) {
    toast('could not acknowledge alert', true);
  }
}

/* --------------------------------------------------------------- data feeds */

function pushLivePoints(snap) {
  for (const [metric, info] of Object.entries(snap.readings || {})) {
    let series = st.series[metric];
    if (!series) {
      series = st.series[metric] = { t: [], v: [] };
    }
    const last = series.t[series.t.length - 1] || 0;
    if (info.ts - last >= 4) {
      series.t.push(info.ts);
      series.v.push(info.value);
      const cutoff = snap.now - st.minutes * 60;
      while (series.t.length > 2 && series.t[0] < cutoff) {
        series.t.shift();
        series.v.shift();
      }
      if (series.t.length > 900) { series.t.shift(); series.v.shift(); }
    }
  }
}

async function loadHistory() {
  try {
    const data = await getJSON(`/api/history?minutes=${st.minutes}&points=300`);
    st.series = {};
    for (const [metric, series] of Object.entries(data.series || {})) {
      st.series[metric] = { t: series.t, v: series.v };
    }
    if (st.snap) render(st.snap);
  } catch (err) {
    console.warn('history failed', err);
  }
}

async function loadEvents() {
  try {
    const data = await getJSON('/api/events?limit=24');
    const box = $('#events');
    if (!data.events.length) {
      box.innerHTML = '<div class="event"><span class="t">—</span><span class="what">no events yet</span><span class="st"></span></div>';
      return;
    }
    box.innerHTML = data.events.map((e) => {
      const when = new Date(e.ts * 1000);
      const hh = String(when.getHours()).padStart(2, '0');
      const mm = String(when.getMinutes()).padStart(2, '0');
      return `<div class="event">
        <span class="t">${hh}:${mm}</span>
        <span class="what">${escapeHTML(e.actuator)} <em>${escapeHTML(e.reason || '')}</em></span>
        <span class="st ${e.on ? 'on' : ''}">${e.on ? 'on' : 'off'}</span>
      </div>`;
    }).join('');
  } catch (err) {
    console.warn('events failed', err);
  }
}

function setConnected(on) {
  st.connected = on;
  $('#live-dot').parentElement.classList.toggle('on', on);
  $('#live-dot').parentElement.classList.toggle('off', !on);
  $('#live-text').textContent = on ? 'live' : 'reconnecting';
}

function connectStream() {
  if (!window.EventSource) return;
  const source = new EventSource(api('/api/stream'));
  source.onopen = () => setConnected(true);
  source.onmessage = (event) => {
    setConnected(true);
    try { render(JSON.parse(event.data)); } catch (err) { console.warn(err); }
  };
  source.onerror = () => {
    setConnected(false);
    source.close();
    setTimeout(connectStream, 3000);
  };
}

function wireRangeButtons() {
  document.querySelectorAll('.range').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.range').forEach((b) => b.classList.toggle('is-on', b === btn));
      st.minutes = Number(btn.dataset.min);
      loadHistory();
    });
  });
}

async function boot() {
  wireRangeButtons();
  try {
    const snap = await getJSON('/api/state');
    render(snap);
  } catch (err) {
    console.error(err);
    toast('cannot reach the controller', true);
  }
  connectStream();
  await loadHistory();
  await loadEvents();
  setInterval(loadHistory, 60000);
  setInterval(loadEvents, 15000);
  window.addEventListener('resize', () => { if (st.snap) render(st.snap); });
}

boot();
