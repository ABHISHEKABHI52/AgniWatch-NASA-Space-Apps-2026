const MAP_BOUNDS = [[21.14, 72.80], [21.21, 72.88]];
const DEFAULT_START = "";
const DEFAULT_END = "";
const ROUTES = {
  dashboard: ["Overview", "Fire intelligence dashboard"],
  livefire: ["Monitor", "Live fire map"],
  historical: ["Analyze", "Historical activity"],
  sensorcomparison: ["Analyze", "Sensor comparison"],
  analytics: ["Analyze", "Analytics"],
  datasources: ["Understand", "Data sources"],
  methodology: ["Understand", "Methodology"],
  settings: ["Workspace", "Settings"],
  login: ["Access", "Sign in"],
};
const state = {
  route: "dashboard", records: [], selected: null, sensors: new Set(["MODIS", "VIIRS"]),
  metadata: null, methodology: null, summary: null, trends: [], calendar: {}, map: null, layer: null, user: null, health: null,
};
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const formatDate = (value) => value ? String(value).slice(0, 10) : "—";
const formatValue = (value) => value == null || value === "" ? "—" : value;

function queryString() {
  const query = new URLSearchParams({ area: $("area")?.value || "all" });
  const start = $("start")?.value || DEFAULT_START;
  const end = $("end")?.value || DEFAULT_END;
  if (start) query.set("start", start);
  if (end) query.set("end", end);
  [...state.sensors].forEach((sensor) => query.append("sensors", sensor));
  if (!state.sensors.size) query.append("sensors", "__NONE__");
  return query.toString();
}
function notify(message, type = "error") {
  const notice = $("notice");
  notice.textContent = message;
  notice.className = "notice " + type;
  window.clearTimeout(notify.timer);
  notify.timer = window.setTimeout(() => { notice.className = "notice"; }, 5000);
}
function setHeader(route) {
  const labels = ROUTES[route] || ROUTES.dashboard;
  $("page-kicker").textContent = labels[0];
  $("page-title").textContent = labels[1];
  document.querySelectorAll("[data-route]").forEach((link) => link.classList.toggle("active", link.dataset.route === route));
}
function filterMarkup() {
  return `<section class="filter-panel"><div class="filter-item"><label for="area">Area of interest</label><select id="area"><option value="all">All available observations</option><option value="surat">Surat pilot region · Gujarat, India</option></select></div><div class="filter-item"><label for="start">From</label><input id="start" type="date" value="${DEFAULT_START}"></div><div class="filter-item"><label for="end">To</label><input id="end" type="date" value="${DEFAULT_END}"></div><div class="sensor-filter"><label>Sensors</label><div class="sensor-buttons"><button class="sensor ${state.sensors.has("MODIS") ? "active" : ""}" data-sensor="MODIS" aria-pressed="${state.sensors.has("MODIS")}"><i class="sensor-shape circle"></i>MODIS</button><button class="sensor ${state.sensors.has("VIIRS") ? "active" : ""}" data-sensor="VIIRS" aria-pressed="${state.sensors.has("VIIRS")}"><i class="sensor-shape square"></i>VIIRS</button></div></div><button class="button primary" id="apply">Apply filters</button><button class="button subtle" id="reset">Reset</button></section>`;
}
function shell(title, subtitle, content, options = {}) {
  return `<div class="page"><div class="page-heading"><div><p class="eyebrow">${options.eyebrow || "Satellite intelligence"}</p><h1>${title}</h1><p>${subtitle}</p></div>${options.action || ""}</div>${content}</div>`;
}
function metric(label, value, note, accent = "") {
  return `<article class="metric-card ${accent}"><div class="metric-top"><span>${label}</span><i>${accent === "modis" ? "○" : accent === "viirs" ? "□" : "◆"}</i></div><strong>${value}</strong><small>${note}</small></article>`;
}
function mapMarkup(id = "map") {
  return `<div class="map-wrap"><div id="${id}" class="map" aria-label="Observation map"></div><div class="map-legend map-sensor-legend"><span><i class="sensor-shape circle"></i>MODIS</span><span><i class="sensor-shape square"></i>VIIRS</span></div><div class="map-legend intensity-legend" aria-label="Fire intensity legend"><span><i class="intensity-dot low"></i>Low</span><span><i class="intensity-dot moderate"></i>Moderate</span><span><i class="intensity-dot high"></i>High</span><span><i class="intensity-dot critical"></i>Critical · blinking</span></div></div>`;
}
function cardsMarkup(summary) {
  const by = summary?.by_sensor || {};
  return `<div class="metrics">${metric("Total observations", summary?.total_records ?? "—", "Returned records in selection")}${metric("MODIS observations", by.MODIS ?? 0, "Terra / Aqua · separate count", "modis")}${metric("VIIRS observations", by.VIIRS ?? 0, "Suomi-NPP / NOAA-20 · separate count", "viirs")}${metric("Geographic coverage", summary?.coverage?.start ? `${formatDate(summary.coverage.start)} — ${formatDate(summary.coverage.end)}` : "No data", "UTC observation coverage")}</div>`;
}
function renderDashboard() {
  $("app").innerHTML = shell("Fire intelligence dashboard", "Satellite-based active fire observations and historical activity.", filterMarkup() + `<div id="dashboard-data">${loadingMarkup()}</div>`, { eyebrow: "Overview" });
}
function loadingMarkup() { return `<div class="loading-state"><span class="spinner"></span>Loading observation intelligence…</div>`; }
function renderDashboardData() {
  const summary = state.summary;
  const demoBanner = state.metadata?.status === "PUBLIC_DEMO" ? `<article class="panel notice-panel public-demo-banner"><strong>PUBLIC DEMO — CURATED SAMPLE OF NASA DATA</strong><p>This evaluator build works without a live backend and contains a small genuine NASA sample, not the complete indexed database.</p></article>` : "";
  $("dashboard-data").innerHTML = demoBanner + cardsMarkup(summary) + `<div class="dashboard-grid"><article class="panel map-panel large"><div class="panel-head"><div><p class="eyebrow">Observation map</p><h2>Geographic activity</h2></div><button class="button subtle" id="focus-map">Reset view</button></div>${mapMarkup()}<p class="caption">Marker shape identifies the sensor. Select a marker or table row to inspect the source record.</p></article><aside class="stack"><article class="panel detail-card"><div class="panel-head"><div><p class="eyebrow">Selected record</p><h2 id="detail-title">No record selected</h2></div></div><div id="detail" class="empty">Select a marker or observation below.</div></article><article class="panel"><div class="panel-head"><div><p class="eyebrow">Daily activity</p><h2>Sensor comparison</h2></div></div><div id="chart" class="chart"></div></article></aside></div><div class="two-col"><article class="panel"><div class="panel-head"><div><p class="eyebrow">UTC activity</p><h2>Activity calendar</h2></div></div><div id="calendar" class="calendar"></div></article><article class="panel"><div class="panel-head"><div><p class="eyebrow">Recent observations</p><h2>Returned records</h2></div><a href="#livefire" class="text-link">Open map →</a></div><div id="observation-list" class="obs-list"></div></article></div><div class="data-footer">${statusMarkup()}</div>`;
  bindFilters(); renderVisuals(); $("focus-map").onclick = resetMap;
}
const FRP_INTENSITY_LEVELS = [
  { key: "low", label: "Low", max: 10, color: "#22C55E" },
  { key: "moderate", label: "Moderate", max: 20, color: "#F97316" },
  { key: "high", label: "High", max: 35, color: "#F43F5E" },
  { key: "critical", label: "Critical", max: Infinity, color: "#991B1B" },
];
function classifyIntensity(record) {
  const frp = Number(record.frp);
  if (!Number.isFinite(frp) || frp < 0) {
    return { key: "unknown", label: "Unknown", color: "#64748B" };
  }
  return FRP_INTENSITY_LEVELS.find((level) => frp <= level.max);
}
function popupMarkup(record, intensity) {
  return `<strong>${esc(record.source_sensor)} · ${esc(record.record_id)}</strong><br>` +
    `<span class="popup-intensity ${intensity.key}">Fire intensity: ${intensity.label}</span><br>` +
    `${esc(record.source_platform || "Platform unavailable")}<br>` +
    `<small>${esc(record.acq_datetime_utc)} UTC · ${esc(record.product)}<br>` +
    `FRP: ${esc(formatValue(record.frp))}</small>`;
}
function statusMarkup() {
  const metadata = state.metadata || {};
  const loaded = metadata.records_loaded == null ? "" : ` · ${metadata.records_loaded.toLocaleString()} loaded`;
  const partialNote = metadata.status === "PARTIAL" ? " NASA data — partially indexed." : metadata.status === "PUBLIC_DEMO" ? " PUBLIC DEMO — CURATED SAMPLE OF NASA DATA." : "";
  return `<p><strong>Data state</strong> <span>${metadata.status || "ERROR"} · ${metadata.source || "Data unavailable"}${loaded}</span></p><p><strong>Retrieved</strong> <span>${metadata.retrieved_at ? new Date(metadata.retrieved_at).toLocaleString() : ["LOCAL", "PARTIAL"].includes(metadata.status) ? "Local files" : "Unavailable"}</span></p><p class="muted">${(metadata.warnings || []).join(" ") || "Counts are observation records, not confirmed individual fires."}${partialNote}</p>`;
}
function bindFilters() {
  document.querySelectorAll(".sensor").forEach((button) => button.onclick = () => {
    const sensor = button.dataset.sensor;
    state.sensors.has(sensor) ? state.sensors.delete(sensor) : state.sensors.add(sensor);
    button.classList.toggle("active", state.sensors.has(sensor));
    button.setAttribute("aria-pressed", String(state.sensors.has(sensor)));
  });
  $("apply").onclick = loadData;
  $("reset").onclick = () => { state.sensors = new Set(["MODIS", "VIIRS"]); route(); };
}
function markerIcon(record, selected) {
  const modis = record.source_sensor === "MODIS";
  const intensity = classifyIntensity(record);
  const size = selected ? 20 : 14;
  return L.divIcon({ className: `map-marker ${modis ? "modis" : "viirs"} intensity-${intensity.key} ${selected ? "selected" : ""}`, html: "<span></span>", iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
}
function initMap(id = "map") {
  if (!window.L || !$(id)) return;
  state.map?.remove();
  state.map = L.map(id, { zoomControl: true }).fitBounds(MAP_BOUNDS);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "© OpenStreetMap contributors", maxZoom: 19 }).addTo(state.map);
  state.layer = L.markerClusterGroup({ showCoverageOnHover: false, spiderfyOnMaxZoom: false });
  state.map.addLayer(state.layer);
  renderMapMarkers();
}
function renderMapMarkers() {
  if (!state.layer) return;
  state.layer.clearLayers();
  state.records.filter((record) => Number.isFinite(Number(record.latitude)) && Number.isFinite(Number(record.longitude))).forEach((record) => {
    const intensity = classifyIntensity(record);
    const marker = L.marker([record.latitude, record.longitude], { icon: markerIcon(record, state.selected?.record_id === record.record_id), riseOnHover: true });
    marker.bindPopup(popupMarkup(record, intensity));
    marker.on("click", () => selectRecord(record));
    state.layer.addLayer(marker);
  });
}
function resetMap() { if (state.map) state.map.fitBounds(MAP_BOUNDS); }
function selectRecord(record) {
  state.selected = record;
  const detail = $("detail");
  if (detail) detail.innerHTML = record ? `<div class="record-badge ${record.source_sensor.toLowerCase()}">${esc(record.source_sensor)}</div><dl class="detail-grid"><dt>Record</dt><dd>${esc(record.record_id)}</dd><dt>Platform</dt><dd>${esc(record.source_platform)}</dd><dt>Acquired UTC</dt><dd>${esc(record.acq_datetime_utc)}</dd><dt>Coordinates</dt><dd>${Number(record.latitude).toFixed(4)}, ${Number(record.longitude).toFixed(4)}</dd><dt>Confidence</dt><dd>${formatValue(record.confidence)}</dd><dt>FRP</dt><dd>${formatValue(record.frp)}</dd><dt>Quality</dt><dd>${formatValue(record.quality_flag)}</dd></dl>` : "Select a marker or observation below.";
  if ($("detail-title")) $("detail-title").textContent = record ? `${record.source_sensor} · ${record.record_id}` : "No record selected";
  renderObservationList();
  renderMapMarkers();
}
function renderObservationList() {
  const container = $("observation-list");
  if (!container) return;
  if (!state.records.length) { container.innerHTML = `<div class="empty">No observations returned for this selection.</div>`; return; }
  container.innerHTML = `<div class="table-scroll"><table><thead><tr><th>Sensor</th><th>Acquisition UTC</th><th>Coordinates</th><th>Confidence</th></tr></thead><tbody>${state.records.slice(0, 80).map((record) => `<tr class="${state.selected?.record_id === record.record_id ? "selected" : ""}" data-id="${esc(record.record_id)}"><td><span class="sensor-tag ${record.source_sensor.toLowerCase()}">${esc(record.source_sensor)}</span></td><td>${esc(formatDate(record.acq_datetime_utc))}</td><td>${Number(record.latitude).toFixed(3)}, ${Number(record.longitude).toFixed(3)}</td><td>${esc(formatValue(record.confidence))}</td></tr>`).join("")}</tbody></table></div>`;
  container.querySelectorAll("tr[data-id]").forEach((row) => row.onclick = () => {
    const record = state.records.find((item) => item.record_id === row.dataset.id);
    selectRecord(record);
    if (state.map) state.map.setView([record.latitude, record.longitude], 12);
  });
}
function renderChart() {
  const chart = $("chart");
  if (!chart) return;
  const points = state.trends || [];
  if (!points.length) { chart.innerHTML = `<div class="empty">No daily records in this range.</div>`; return; }
  const max = Math.max(1, ...points.flatMap((point) => [point.MODIS, point.VIIRS]));
  chart.innerHTML = points.map((point) => `<button class="bar-group" data-date="${point.date}" title="${point.date}: MODIS ${point.MODIS}, VIIRS ${point.VIIRS}"><span class="bar modis" style="height:${point.MODIS / max * 100}%"></span><span class="bar viirs" style="height:${point.VIIRS / max * 100}%"></span><small>${point.date.slice(5)}</small></button>`).join("");
  chart.querySelectorAll(".bar-group").forEach((button) => button.onclick = () => { $("start").value = button.dataset.date; $("end").value = button.dataset.date; loadData(); });
}
function renderCalendar() {
  const calendar = $("calendar");
  if (!calendar) return;
  const days = Object.entries(state.calendar || {});
  calendar.innerHTML = days.length ? days.map(([date, counts]) => `<button class="day ${(counts.MODIS + counts.VIIRS) > 1 ? "busy" : ""}" data-date="${date}"><b>${date.slice(5)}</b><small>${counts.MODIS + counts.VIIRS} records</small></button>`).join("") : `<div class="empty">No activity for the selected range.</div>`;
  calendar.querySelectorAll(".day").forEach((day) => day.onclick = () => { $("start").value = day.dataset.date; $("end").value = day.dataset.date; loadData(); });
}
function renderVisuals() { initMap(); renderCalendar(); renderChart(); renderObservationList(); }
async function loadData() {
  const apply = $("apply");
  if (apply) { apply.disabled = true; apply.textContent = "Loading…"; }
  try {
    const query = queryString();
    const [health, metadata, methodology, summary, observations, calendar, trends] = await Promise.all([
      AgniWatchApi.health(),
      AgniWatchApi.metadata(),
      AgniWatchApi.get("/methodology"),
      AgniWatchApi.get("/summary?" + query),
      AgniWatchApi.get("/observations?" + query),
      AgniWatchApi.get("/activity-calendar?" + query),
      AgniWatchApi.get("/trends?" + query),
    ]);
    state.health = health;
    state.metadata = metadata; state.methodology = methodology; state.summary = summary; state.records = observations.records || []; state.calendar = calendar.days || {}; state.trends = trends.points || [];
    updateStatus(metadata);
    if (state.route === "dashboard") renderDashboardData();
    else if (["datasources", "methodology", "settings"].includes(state.route)) renderInfo(state.route);
    else renderRouteData();
  } catch (error) {
    updateStatus({ status: "ERROR" });
    notify("Data error: " + error.message + ". Check that the backend is running. Retry from the refresh button.");
  } finally { if (apply) { apply.disabled = false; apply.textContent = "Apply filters"; } }
}
function updateStatus(metadata) {
  const status = $("data-status");
  if (!status) return;
  status.className = "data-status " + String(metadata.status || "ERROR").toLowerCase();
  const label = metadata.status === "REAL" ? (metadata.freshness === "live" ? "LIVE DATA" : "CACHED DATA") : metadata.status === "LOCAL" ? "NASA DATA" : metadata.status === "PARTIAL" ? "NASA DATA — PARTIALLY INDEXED" : metadata.status === "PUBLIC_DEMO" ? "PUBLIC DEMO" : metadata.status === "DEMO" ? "DEMO DATA" : "DATA ERROR";
  status.innerHTML = `<i></i><b>${label}</b><small>${metadata.source || "Data request failed"}</small>`;
}
function renderRouteData() {
  const route = state.route;
  if (route === "livefire") {
    $("route-data").innerHTML = `<div class="live-layout"><article class="panel map-panel live-map">${mapMarkup("live-map")}<p class="caption">NASA dataset observations. Local files are not a continuously updating live stream.</p></article><aside class="stack"><article class="panel detail-card"><div class="panel-head"><div><p class="eyebrow">Selected record</p><h2 id="detail-title">No record selected</h2></div></div><div id="detail" class="empty">Select a marker or row.</div></article><article class="panel"><div class="panel-head"><div><p class="eyebrow">Observation feed</p><h2>Latest records</h2></div></div><div id="observation-list" class="obs-list"></div></article></aside></div>`;
    initMap("live-map"); renderObservationList(); return;
  }
  if (route === "historical") {
    $("route-data").innerHTML = `<div class="two-col"><article class="panel"><div class="panel-head"><div><p class="eyebrow">Historical distribution</p><h2>Observation map</h2></div></div>${mapMarkup("history-map")}</article><article class="panel"><div class="panel-head"><div><p class="eyebrow">Daily activity</p><h2>Calendar and trend</h2></div></div><div id="calendar" class="calendar"></div><div id="chart" class="chart chart-tall"></div></article></div><article class="panel"><div class="panel-head"><div><p class="eyebrow">Exportable records</p><h2>Historical observations</h2></div><button class="button primary" id="export">Export CSV</button></div><div id="observation-list" class="obs-list"></div></article>`;
    initMap("history-map"); renderCalendar(); renderChart(); renderObservationList(); bindExport(); return;
  }
  if (route === "sensorcomparison" || route === "analytics") {
    const by = state.summary?.by_sensor || {};
    $("route-data").innerHTML = `${cardsMarkup(state.summary)}<div class="two-col"><article class="panel"><div class="panel-head"><div><p class="eyebrow">${route === "analytics" ? "Derived activity" : "Sensor harmonization"}</p><h2>MODIS vs VIIRS daily records</h2></div></div><div id="chart" class="chart chart-tall"></div></article><article class="panel"><div class="panel-head"><div><p class="eyebrow">Spatial distribution</p><h2>Observation footprint</h2></div></div>${mapMarkup("comparison-map")}</article></div><article class="panel notice-panel"><strong>Interpretation guardrail</strong><p>MODIS and VIIRS differ in spatial resolution, overpass timing, algorithms, and detection behavior. Raw record counts are not equivalent to fire intensity, burned area, or confirmed individual fires.</p></article><article class="panel"><div class="panel-head"><h2>${route === "analytics" ? "Analytics records" : "Sensor-specific records"}</h2><span class="muted">${by.MODIS || 0} MODIS · ${by.VIIRS || 0} VIIRS</span></div><div id="observation-list" class="obs-list"></div></article>`;
    initMap("comparison-map"); renderChart(); renderObservationList(); return;
  }
}
function bindExport() {
  if (!$("export")) return;
  $("export").onclick = async () => {
    try {
      const csv = await AgniWatchApi.get("/export?" + queryString());
      const link = document.createElement("a");
      link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
      link.download = "agniwatch-observations.csv";
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (error) {
      notify("Export failed: " + error.message);
    }
  };
}
function renderInfo(route) {
  const metadata = state.metadata || {};
  if (route === "datasources") {
    $("app").innerHTML = shell("Data sources", "Know where every observation comes from and how fresh it is.", `<div class="source-grid"><article class="panel source-hero"><span class="source-icon">◎</span><p class="eyebrow">Current data mode</p><h2>${metadata.status || "ERROR"} <span class="status-pill">${metadata.freshness || "unavailable"}</span></h2><p>${metadata.source || "Data unavailable"}</p><div class="source-facts"><span><b>Products</b>${(metadata.products || ["MODIS", "VIIRS"]).join(" · ")}</span><span><b>Spatial standard</b>${metadata.spatial_grid || "Source coordinates"}</span><span><b>Temporal standard</b>${metadata.time_bin || "UTC acquisition timestamp"}</span></div></article><article class="panel"><p class="eyebrow">Sensor reference</p><h2>Two complementary views</h2><div class="sensor-reference"><div><i class="sensor-shape circle"></i><strong>MODIS</strong><p>Terra / Aqua products with a longer observation history and distinct detection characteristics.</p></div><div><i class="sensor-shape square"></i><strong>VIIRS</strong><p>Suomi-NPP / NOAA-20 products with different spatial and temporal sampling.</p></div></div></article></div><article class="panel data-disclaimer"><strong>Transparency note</strong><p>${metadata.note || (metadata.warnings || []).join(" ") || "Counts are observation records, not confirmed individual fires."}</p></article>`);
  } else if (route === "methodology") {
    const methodology = state.methodology || {};
    const sections = [
      ["Challenge overview", "AgniWatch explores harmonized context for MODIS and VIIRS hot-spot observations without collapsing their differences."],
      ["Data standardization", methodology.standardization || "Source fields are validated, timestamps are normalized to UTC, and sensor identity is retained."],
      ["Temporal and spatial aggregation", methodology.aggregation || "Records use UTC calendar-day bins and a 0.1-degree display grid; the shared view does not make sensors physically equivalent."],
      ["Missing data handling", methodology.missing_values || "Missing source values remain null and are never silently converted to zero."],
      ["Comparison limitations", methodology.comparison || "MODIS and VIIRS counts are displayed separately and are not equivalent measures of intensity, area, or severity."],
      ["Interpretation guidelines", methodology.limitations || "Counts describe returned observation records, not fire intensity, burned area, or confirmed fires."],
    ];
    $("app").innerHTML = shell("Methodology", "A transparent path from source records to comparable context.", `<div class="method-grid">${sections.map(([title, body], index) => `<article class="panel method-card"><span class="step">${String(index + 1).padStart(2, "0")}</span><h2>${esc(title)}</h2><p>${esc(body)}</p></article>`).join("")}</div>`);
  } else if (route === "settings") {
    const user = state.user || {};
    $("app").innerHTML = shell("Settings", "Application preferences and session information.", `<div class="settings-grid"><article class="panel settings-card"><p class="eyebrow">Profile</p><h2>${esc(user.name || "Not signed in")}</h2><p class="muted">${esc(user.role || "Guest")} · ${esc(user.auth_mode || "No active session")}</p><dl class="settings-list"><dt>Username</dt><dd>${esc(user.username || "—")}</dd><dt>Auth mode</dt><dd>${esc(user.auth_mode || "—")}</dd><dt>API status</dt><dd>${state.health?.status === "ok" ? "Connected" : "Unavailable"}</dd></dl><button class="button subtle" id="settings-login">${user.username ? "Sign in as another user" : "Open sign-in screen"}</button></article><article class="panel settings-card"><p class="eyebrow">Preferences</p><h2>Interface</h2><label class="preference"><span>Compact navigation</span><input id="compact-nav" type="checkbox"></label><label class="preference"><span>Remember selected sensors</span><input type="checkbox" checked></label><p class="muted small">API keys and backend credentials are never exposed in this interface.</p></article></div>`);
    $("settings-login").onclick = () => { window.location.hash = "login"; };
  }
}
function renderLogin() {
  $("app").innerHTML = `<div class="login-view"><div class="login-card"><div class="brand login-brand"><span class="brand-mark">⌘</span><span><strong>AgniWatch</strong><small>Satellite fire intelligence</small></span></div><p class="eyebrow">Secure workspace</p><h1>Welcome back</h1><p class="muted">Sign in to the AgniWatch demonstration workspace.</p><form id="login-form"><label>Username<input name="username" autocomplete="username" value="demo" required></label><label>Password<div class="password-field"><input name="password" type="password" autocomplete="current-password" value="demo" required><button type="button" id="show-password">Show</button></div></label><button class="button primary wide" type="submit">Sign in</button><p class="login-hint">Demo credentials: demo / demo · authentication is simulated, not production-secure.</p><p id="login-error" class="form-error"></p></form></div></div>`;
  $("show-password").onclick = () => { const input = document.querySelector('[name="password"]'); input.type = input.type === "password" ? "text" : "password"; $("show-password").textContent = input.type === "password" ? "Show" : "Hide"; };
  $("login-form").onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.target); const button = event.target.querySelector("button[type=submit]"); button.disabled = true; button.textContent = "Signing in…"; try {   const result = await AgniWatchApi.login({ username: form.get("username"), password: form.get("password") }); localStorage.setItem("agniwatch_token", result.access_token); window.location.hash = "dashboard"; } catch (error) { $("login-error").textContent = error.message + ". Use demo / demo."; button.disabled = false; button.textContent = "Sign in"; } };
}
async function route() {
  state.route = (window.location.hash.slice(1) || "dashboard").toLowerCase();
  if (!ROUTES[state.route]) state.route = "dashboard";
  setHeader(state.route);
  if (state.route === "login") { renderLogin(); return; }
  if (["datasources", "methodology", "settings"].includes(state.route)) {
    renderInfo(state.route);
    const token = localStorage.getItem("agniwatch_token");
    if (token) state.user = await AgniWatchApi.me(token).catch(() => null);
    await loadData();
    return;
  }
  if (state.route === "dashboard") renderDashboard(); else $("app").innerHTML = shell(ROUTES[state.route][1], "Explore sensor-aware satellite fire observations with transparent context.", filterMarkup() + `<div id="route-data">${loadingMarkup()}</div>`);
  bindFilters();
  await loadData();
}
$("refresh").onclick = loadData;
$("logout").onclick = async () => {
  const token = localStorage.getItem("agniwatch_token");
  try { if (token) await AgniWatchApi.logout(token); } catch (error) { notify("Sign out request failed: " + error.message); }
  localStorage.removeItem("agniwatch_token");
  state.user = null;
  window.location.hash = "login";
};
$("sidebar-toggle").onclick = () => $("sidebar").classList.toggle("collapsed");
window.addEventListener("hashchange", route);
window.addEventListener("resize", () => state.map?.invalidateSize());
route();
