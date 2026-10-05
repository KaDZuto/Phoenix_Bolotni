/*
 * Дашборд карты активности (Задача 5, td3.md раздел 5).
 *
 * Логика простая и сознательно без фреймворков: карта — это несколько слоёв Leaflet,
 * которые перезапрашиваются у `/api/map/*` при смене фильтров. Статику отдаёт
 * FastAPI, состояние между перезагрузками не нужно.
 *
 * Важное правило отображения: пустая ячейка тепловой карты означает «птиц не
 * зафиксировано» только внутри зоны покрытия. Поэтому слой покрытия включён по
 * умолчанию, а ячейки вне покрытия рисуются иначе (см. STYLE_OUTSIDE).
 */

const STYLE_DEVICE = {
  online: { color: "#3fb950", fillColor: "#3fb950" },
  silent: { color: "#d29922", fillColor: "#d29922" },
  offline: { color: "#f85149", fillColor: "#f85149" },
  never_seen: { color: "#8b949e", fillColor: "#8b949e" },
};

const STYLE_DEVICES = {
  point: (feature) => {
    const s = STYLE_DEVICE[feature.properties.status] || STYLE_DEVICE.never_seen;
    return {
      radius: 7,
      color: "#0d1117",
      weight: 2,
      fillColor: s.fillColor,
      fillOpacity: 0.95,
    };
  },
};

const STYLE_COVERAGE = {
  color: "#58a6ff",
  weight: 1,
  fillColor: "#58a6ff",
  fillOpacity: 0.06,
  dashArray: "4 4",
};

const STYLE_GRID = (feature) => {
  const p = feature.properties || {};
  return {
    color: "#00000000",
    weight: 0,
    fillColor: p.covered === false ? "#7a7f8a" : heatColor(p.heat ?? 0),
    fillOpacity: p.covered === false ? 0.08 : 0.45 + 0.45 * (p.heat ?? 0),
  };
};

const STYLE_KDE = {
  color: "#ff7b72",
  weight: 2,
  fillColor: "#ff7b72",
  fillOpacity: 0.12,
};

const STYLE_POINTS = {
  radius: 3,
  color: "#ffffff",
  weight: 0.5,
  fillColor: "#ffa657",
  fillOpacity: 0.8,
};

const STYLE_DIFF = (feature) => {
  const kind = feature.properties && feature.properties.kind;
  if (kind === "appeared") {
    return { color: "#3fb950", weight: 2, fillColor: "#3fb950", fillOpacity: 0.35 };
  }
  if (kind === "disappeared") {
    return { color: "#f85149", weight: 2, fillColor: "#f85149", fillOpacity: 0.35 };
  }
  return { color: "#8b949e", weight: 1, fillColor: "#8b949e", fillOpacity: 0.12 };
};

/** Градиент «мало → много». Достаточно для чтения карты глазами, без d3. */
function heatColor(t) {
  const stops = [
    [0.0, [13, 43, 69]],
    [0.25, [31, 111, 139]],
    [0.5, [53, 183, 121]],
    [0.75, [246, 215, 70]],
    [1.0, [240, 62, 62]],
  ];
  const value = Math.max(0, Math.min(1, t));
  for (let i = 1; i < stops.length; i += 1) {
    if (value <= stops[i][0]) {
      const [p0, c0] = stops[i - 1];
      const [p1, c1] = stops[i];
      const k = (value - p0) / (p1 - p0 || 1);
      const mix = c0.map((c, idx) => Math.round(c + (c1[idx] - c) * k));
      return `rgb(${mix.join(",")})`;
    }
  }
  return "rgb(240,62,62)";
}

const state = {
  map: null,
  layers: {},
  species: [],
  summary: null,
  busy: false,
};

// --------------------------------------------------------------- утилиты

function el(id) {
  return document.getElementById(id);
}

function setStatus(text, kind) {
  const node = el("status");
  node.textContent = text;
  node.className = kind || "";
}

function num(value) {
  return (value ?? 0).toLocaleString("ru-RU");
}

function fmtDate(value) {
  if (!value) return "—";
  return String(value).slice(0, 10);
}

function dateOrNull(id) {
  const raw = el(id).value;
  return raw ? raw : null;
}

/** Значения текущих фильтров периода и вида в виде query-параметров. */
function commonParams() {
  const params = new URLSearchParams();
  const species = selectedSpecies();
  if (species.length) params.set("species", species.join(","));
  const from = dateOrNull("date-from");
  const to = dateOrNull("date-to");
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  return params;
}

function selectedSpecies() {
  const box = el("species-select");
  return Array.from(box.selectedOptions).map((o) => o.value);
}

async function getJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch (err) {
      /* ответ без JSON — оставляем statusText */
    }
    throw new Error(detail);
  }
  return response.json();
}

function addLayer(name, geojson, style, popup) {
  const layer = L.geoJSON(geojson, {
    style,
    pointToLayer: (feature, latlng) => {
      const s = typeof style === "function" ? style(feature) : style;
      return L.circleMarker(latlng, s);
    },
    onEachFeature: (feature, lyr) => {
      if (popup) {
        lyr.bindPopup(popup(feature.properties || {}));
      }
    },
  });
  layer.addTo(state.map);
  state.layers[name] = layer;
  return layer;
}

function dropLayer(name) {
  if (state.layers[name]) {
    state.map.removeLayer(state.layers[name]);
    delete state.layers[name];
  }
}

function toggle(name, visible) {
  if (visible) {
    if (state.layers[name] && !state.map.hasLayer(state.layers[name])) {
      state.layers[name].addTo(state.map);
    }
  } else {
    dropLayer(name);
  }
}

// --------------------------------------------------------------- слои

async function loadSummary() {
  const params = new URLSearchParams();
  const from = dateOrNull("date-from");
  const to = dateOrNull("date-to");
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  const summary = await getJSON(`/api/map/summary?${params.toString()}`);
  state.summary = summary;

  const devices = summary.devices || {};
  const detections = summary.detections || {};
  const gaps = summary.gaps || {};
  const chips = [
    `<span class="chip">устройств <b>${num(devices.active)}/${num(devices.total)}</b></span>`,
    `<span class="chip">наблюдений <b>${num(detections.total)}</b></span>`,
    `<span class="chip">видов <b>${num(detections.species_observed)}</b></span>`,
  ];
  if (devices.states && devices.states.offline) {
    chips.push(`<span class="chip bad">не на связи <b>${num(devices.states.offline)}</b></span>`);
  }
  if (devices.states && devices.states.silent) {
    chips.push(`<span class="chip warn">молчат <b>${num(devices.states.silent)}</b></span>`);
  }
  if (gaps.stale_devices) {
    chips.push(`<span class="chip warn">разрывы <b>${num(gaps.stale_devices)}</b></span>`);
  }
  const queue = summary.queue || {};
  if (queue.pending !== undefined) {
    chips.push(`<span class="chip">в очереди <b>${num(queue.pending)}</b></span>`);
  }
  el("summary").innerHTML = chips.join("");

  if (detections.top_species && detections.top_species.length) {
    const top = detections.top_species[0];
    el("legend-meta").textContent =
      `Топ-вид: ${top.species_ru} (${num(top.detections)})`;
  }
}

async function loadSpecies() {
  const data = await getJSON("/api/map/species");
  state.species = data.species || [];
  const groups = data.habitats || {};
  const box = el("species-select");
  box.innerHTML = "";
  for (const [habitat, items] of Object.entries(groups)) {
    const optgroup = document.createElement("optgroup");
    optgroup.label = `${habitat} (${items.length})`;
    for (const item of items) {
      const option = document.createElement("option");
      option.value = item.slug;
      option.textContent = item.ru;
      option.dataset.count = item.detections ?? "";
      optgroup.appendChild(option);
    }
    box.appendChild(optgroup);
  }
}

async function loadDevices() {
  const geojson = await getJSON("/api/map/devices");
  addLayer(
    "devices",
    geojson,
    STYLE_DEVICES,
    (p) => {
      if (p.located === false) {
        return `<div class="popup-title">${p.name}</div><div class="popup-row">нет фиксированной точки</div>`;
      }
      return [
        `<div class="popup-title">${p.name}</div>`,
        `<div class="popup-row">статус: <b>${p.status}</b></div>`,
        `<div class="popup-row">последние данные: ${fmtDate(p.last_seen)}</div>`,
        `<div class="popup-row">молчит: ${p.seconds_since_last_seen ?? "—"} с</div>`,
        p.place_note ? `<div class="popup-row">${p.place_note}</div>` : "",
      ].join("");
    }
  );
  fitToData();
}

async function loadCoverage() {
  const geojson = await getJSON("/api/map/coverage?radius_m=300");
  addLayer("coverage", geojson, STYLE_COVERAGE, (p) =>
    `<div class="popup-title">Покрытие</div><div class="popup-row">${p.devices ?? ""} активн. микрофонов, ${p.cells_covered ?? 0} ячеек</div>`
  );
}

async function loadDetections() {
  const params = commonParams();
  params.set("limit", "20000");
  const data = await getJSON(`/api/map/detections?${params.toString()}`);
  addLayer(
    "points",
    data,
    STYLE_POINTS,
    (p) =>
      [
        `<div class="popup-title">${p.species_ru}</div>`,
        `<div class="popup-row">уверенность: ${(p.confidence * 100).toFixed(0)}%</div>`,
        `<div class="popup-row">${fmtDate(p.window_start_ts)} ${p.window_start_ts.slice(11, 16)}</div>`,
        `<div class="popup-row">устройство: ${p.device_id}</div>`,
      ].join("")
  );
  const props = data.properties || {};
  if (props.truncated) {
    setStatus(
      `Показаны первые ${num(props.count)} из ${num(props.total_matched)} детекций — увеличьте период фильтра или выгружайте CSV`,
      "busy"
    );
  }
}

/** Зоны активности — только для одного вида: «зона» без вида бессмысленна. */
async function loadZones() {
  const species = selectedSpecies();
  if (species.length !== 1) {
    dropLayer("grid");
    dropLayer("kde");
    el("legend-meta").textContent = "Для зон активности выберите ровно один вид";
    return;
  }
  const params = commonParams();
  params.set("species", species[0]);
  const wantGrid = el("layer-grid").checked;
  const wantKde = el("layer-kde").checked;
  params.set("level", wantKde ? "both" : "grid");

  const data = await getJSON(`/api/map/activity_zones?${params.toString()}`);
  const layers = data.layers || {};

  dropLayer("grid");
  dropLayer("kde");

  if (wantGrid && layers.grid) {
    // `heat` (0..1) считает сервер — тепловая карта одинакова в браузере и в ГИС.
    addLayer("grid", layers.grid, STYLE_GRID, (p) => {
      const covered = p.covered === false ? "<br>вне зоны покрытия" : "";
      return [
        `<div class="popup-title">${data.species_ru}</div>`,
        `<div class="popup-row">ячейка ${p.cell}</div>`,
        `<div class="popup-row">наблюдений: ${p.count}${covered}</div>`,
        `<div class="popup-row">ср. уверенность: ${((p.mean_confidence ?? 0) * 100).toFixed(0)}%</div>`,
      ].join("");
    });
  }
  if (wantKde && layers.kde) {
    addLayer("kde", layers.kde, STYLE_KDE, (p) =>
      [
        `<div class="popup-title">Граница зоны активности</div>`,
        `<div class="popup-row">слой: ${p.layer}</div>`,
        p.level ? `<div class="popup-row">уровень плотности: ${p.level}</div>` : "",
      ].join("")
    );
  }

  const meta = (layers.grid_meta && layers.grid_meta.cells) || 0;
  el("legend-meta").textContent = meta ? `Ячеек с наблюдениями: ${num(meta)}` : "Нет наблюдений за период";
}

async function loadDiff() {
  const species = selectedSpecies();
  const bounds = ["a-from", "a-to", "b-from", "b-to"].map(dateOrNull);
  if (species.length !== 1 || bounds.some((b) => !b)) {
    dropLayer("diffs");
    return;
  }
  const params = new URLSearchParams();
  params.set("species", species[0]);
  params.set("a_from", bounds[0]);
  params.set("a_to", bounds[1]);
  params.set("b_from", bounds[2]);
  params.set("b_to", bounds[3]);
  params.set("normalize_by_devices", el("diff-normalize").checked ? "true" : "false");

  const data = await getJSON(`/api/map/period_diff?${params.toString()}`);
  dropLayer("diffs");
  addLayer("diffs", data, STYLE_DIFF, (p) =>
    [
      `<div class="popup-title">${p.species_slug}</div>`,
      `<div class="popup-row">${p.kind}</div>`,
      `<div class="popup-row">A: ${p.count_a} → B: ${p.count_b}</div>`,
    ].join("")
  );

  const summary = (data.properties && data.properties.summary) || {};
  const parts = Object.entries(summary)
    .filter(([, value]) => value)
    .map(([key, value]) => `${key}: ${value}`);
  if (parts.length) {
    setStatus(`Изменения за два периода — ${parts.join(", ")}`);
  }
}

function fitToData() {
  const layers = [state.layers.devices, state.layers.coverage, state.layers.points];
  for (const layer of layers) {
    if (layer && layer.getLayers().length) {
      state.map.fitBounds(layer.getBounds(), { padding: [24, 24] });
      return;
    }
  }
}

// --------------------------------------------------------------- сценарии

async function refreshAll() {
  if (state.busy) return;
  state.busy = true;
  setStatus("Загрузка…", "busy");
  try {
    await loadSummary();
    await loadDevices();
    await loadCoverage();
    await loadZones();
    if (el("layer-points").checked) await loadDetections();
    else dropLayer("points");
    if (el("layer-diffs").checked) await loadDiff();
    else dropLayer("diffs");
    setStatus(`Обновлено ${new Date().toLocaleTimeString("ru-RU")}`);
  } catch (err) {
    setStatus(`Ошибка: ${err.message}`, "error");
  } finally {
    state.busy = false;
  }
}

function applyLayerVisibility() {
  toggle("devices", el("layer-devices").checked);
  toggle("coverage", el("layer-coverage").checked);
  if (!el("layer-devices").checked && !el("layer-coverage").checked) {
    state.map.setView([55.75, 37.6], 9);
  }
}

function initDefaults() {
  const to = new Date();
  const from = new Date(to.getTime() - 30 * 24 * 3600 * 1000);
  el("date-from").value = toISO(from);
  el("date-to").value = toISO(to);

  // Периоды A/B по умолчанию: две половины выбранного интервала.
  el("a-from").value = toISO(from);
  const middle = new Date(from.getTime() + (to.getTime() - from.getTime()) / 2);
  el("a-to").value = toISO(middle);
  el("b-from").value = toISO(middle);
  el("b-to").value = toISO(to);
}

function toISO(date) {
  return date.toISOString().slice(0, 10);
}

function bindEvents() {
  el("btn-refresh").addEventListener("click", refreshAll);
  el("btn-period").addEventListener("click", refreshAll);
  el("btn-diff").addEventListener("click", () => {
    loadDiff().catch((err) => setStatus(`Ошибка сравнения: ${err.message}`, "error"));
  });
  el("btn-clear").addEventListener("click", () => {
    el("date-from").value = "";
    el("date-to").value = "";
    refreshAll();
  });

  for (const id of ["layer-devices", "layer-coverage"]) {
    el(id).addEventListener("change", applyLayerVisibility);
  }
  for (const id of ["layer-grid", "layer-kde", "layer-points", "layer-diffs"]) {
    el(id).addEventListener("change", refreshAll);
  }
  el("species-select").addEventListener("change", () => {
    loadZones().catch((err) => setStatus(err.message, "error"));
  });

  // Ссылки на выгрузку всегда соответствуют текущим фильтрам.
  const updateExports = () => {
    el("link-export").href = `/api/map/export.geojson?${commonParams().toString()}`;
    el("link-export-csv").href = `/api/map/export.csv?${commonParams().toString()}`;
  };
  el("species-select").addEventListener("change", updateExports);
  el("date-from").addEventListener("change", updateExports);
  el("date-to").addEventListener("change", updateExports);
  updateExports();
}

async function init() {
  state.map = L.map("map").setView([55.75, 37.6], 9);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "© OpenStreetMap",
  }).addTo(state.map);

  initDefaults();
  bindEvents();
  try {
    await loadSpecies();
    await refreshAll();
  } catch (err) {
    setStatus(`Ошибка загрузки: ${err.message}`, "error");
  }
}

document.addEventListener("DOMContentLoaded", init);