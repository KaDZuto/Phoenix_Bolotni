/**
 * Phoenix_Bolotni Collector Mini App - WaveSurfer & Audio Annotation
 */

document.addEventListener("DOMContentLoaded", async () => {
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }

  // Parse launch params
  let hashStr = window.location.hash.startsWith("#") ? window.location.hash.slice(1) : "";
  let searchParams = new URLSearchParams(hashStr || window.location.search);
  const subId = searchParams.get("sub_id") || "1";
  const audioUrl = searchParams.get("audio") || `/api/audio/${subId}?sig=${searchParams.get("sig") || ""}`;

  // DOM elements
  const lblStart = document.getElementById("lblStart");
  const lblEnd = document.getElementById("lblEnd");
  const lblDuration = document.getElementById("lblDuration");
  const btnPlayPause = document.getElementById("btnPlayPause");
  const btnLoopRegion = document.getElementById("btnLoopRegion");
  const btnSpeed = document.getElementById("btnSpeed");
  const btnZoomIn = document.getElementById("btnZoomIn");
  const btnZoomOut = document.getElementById("btnZoomOut");
  const btnSave = document.getElementById("btnSave");
  const speciesListEl = document.getElementById("speciesList");
  const speciesSearchInput = document.getElementById("speciesSearch");
  const customSpeciesBox = document.getElementById("customSpeciesBox");
  const customSpeciesInput = document.getElementById("customSpeciesInput");
  const habitatTabs = document.querySelectorAll(".tab-btn");

  // State
  let activeRegion = null;
  let isLooping = false;
  let currentZoom = 0;
  let currentSpeed = 1.0;
  let allSpecies = [];
  let currentHabitat = "all";
  let selectedSpeciesSlug = null;
  let customSpeciesName = null;

  // Initialize WaveSurfer
  let ws = null;
  let wsRegions = null;

  try {
    if (window.WaveSurfer) {
      wsRegions = window.WaveSurfer.Regions ? window.WaveSurfer.Regions.create() : null;
      ws = window.WaveSurfer.create({
        container: "#waveform",
        waveColor: "#93c5fd",
        progressColor: "#2563eb",
        cursorColor: "#1d4ed8",
        height: 100,
        normalize: true,
        plugins: wsRegions ? [wsRegions] : []
      });

      if (audioUrl) {
        ws.load(audioUrl);
      }

      ws.on("ready", () => {
        const totalDuration = ws.getDuration();
        const initialStart = 0;
        const initialEnd = Math.min(3.0, totalDuration);

        if (wsRegions) {
          activeRegion = wsRegions.addRegion({
            start: initialStart,
            end: initialEnd,
            color: "rgba(16, 185, 129, 0.35)",
            drag: true,
            resize: true
          });
          updateRegionLabels(initialStart, initialEnd);
        }
      });

      if (wsRegions) {
        wsRegions.on("region-updated", (reg) => {
          activeRegion = reg;
          updateRegionLabels(reg.start, reg.end);
        });
      }

      ws.on("timeupdate", (currentTime) => {
        if (isLooping && activeRegion) {
          if (currentTime >= activeRegion.end || currentTime < activeRegion.start) {
            ws.setTime(activeRegion.start);
          }
        }
      });

      ws.on("finish", () => {
        btnPlayPause.textContent = "▶️ Слушать";
      });
    }
  } catch (err) {
    console.error("Failed to initialize WaveSurfer:", err);
  }

  function updateRegionLabels(start, end) {
    const s = Math.max(0, start);
    const e = Math.max(s, end);
    const d = e - s;
    lblStart.textContent = `${s.toFixed(2)}с`;
    lblEnd.textContent = `${e.toFixed(2)}с`;
    lblDuration.textContent = `${d.toFixed(2)}с`;
    validateSaveState();
  }

  // Audio controls
  btnPlayPause.addEventListener("click", () => {
    if (!ws) return;
    if (ws.isPlaying()) {
      ws.pause();
      btnPlayPause.textContent = "▶️ Слушать";
    } else {
      if (isLooping && activeRegion) {
        ws.setTime(activeRegion.start);
      }
      ws.play();
      btnPlayPause.textContent = "⏸️ Пауза";
    }
  });

  btnLoopRegion.addEventListener("click", () => {
    isLooping = !isLooping;
    btnLoopRegion.classList.toggle("active", isLooping);
    if (isLooping && ws && activeRegion) {
      ws.setTime(activeRegion.start);
      if (!ws.isPlaying()) {
        ws.play();
        btnPlayPause.textContent = "⏸️ Пауза";
      }
    }
  });

  btnSpeed.addEventListener("click", () => {
    if (!ws) return;
    if (currentSpeed === 1.0) currentSpeed = 0.5;
    else if (currentSpeed === 0.5) currentSpeed = 0.75;
    else currentSpeed = 1.0;

    ws.setPlaybackRate(currentSpeed);
    btnSpeed.textContent = `⚡ ${currentSpeed}x`;
  });

  btnZoomIn.addEventListener("click", () => {
    if (!ws) return;
    currentZoom = Math.min(100, currentZoom + 20);
    ws.zoom(currentZoom);
  });

  btnZoomOut.addEventListener("click", () => {
    if (!ws) return;
    currentZoom = Math.max(0, currentZoom - 20);
    ws.zoom(currentZoom);
  });

  // Load Species List
  try {
    const resp = await fetch("/api/species");
    if (resp.ok) {
      allSpecies = await resp.json();
    } else {
      throw new Error("HTTP error " + resp.status);
    }
  } catch (e) {
    console.warn("Could not fetch /api/species, using fallback:", e);
    allSpecies = [
      { slug: "anas_platyrhynchos", ru: "Кряква", habitat: "wetland" },
      { slug: "ardea_cinerea", ru: "Серая цапля", habitat: "wetland" },
      { slug: "fringilla_coelebs", ru: "Зяблик", habitat: "forest" },
      { slug: "parus_major", ru: "Большая синица", habitat: "forest" },
      { slug: "coloeus_monedula", ru: "Галка", habitat: "urban" }
    ];
  }

  function renderSpeciesList() {
    speciesListEl.innerHTML = "";
    const filterText = speciesSearchInput.value.trim().toLowerCase();

    // Special items
    const specialItems = [
      { slug: "_noise", ru: "🔇 Шум / нет птицы", habitat: "special" },
      { slug: "_uncertain", ru: "❓ Не уверен(а)", habitat: "special" },
      { slug: "_custom", ru: "➕ Другой вид (нет в списке)", habitat: "special" }
    ];

    const filtered = allSpecies.filter((sp) => {
      const matchHab = (currentHabitat === "all" || sp.habitat === currentHabitat);
      const matchText = !filterText || sp.ru.toLowerCase().includes(filterText) || sp.slug.toLowerCase().includes(filterText);
      return matchHab && matchText;
    });

    const combined = [...specialItems, ...filtered];

    combined.forEach((item) => {
      const div = document.createElement("div");
      div.className = "species-item";
      if (selectedSpeciesSlug === item.slug) {
        div.classList.add("selected");
      }

      const habLabel = item.habitat === "wetland" ? "болотный" :
                       item.habitat === "forest" ? "лесной" :
                       item.habitat === "urban" ? "городской" :
                       item.habitat === "field_steppe" ? "степной" : "";

      div.innerHTML = `
        <span class="name">${item.ru}</span>
        ${habLabel ? `<span class="habitat-tag">${habLabel}</span>` : ""}
      `;

      div.addEventListener("click", () => {
        selectedSpeciesSlug = item.slug;
        if (item.slug === "_custom") {
          customSpeciesBox.classList.add("visible");
          customSpeciesInput.focus();
        } else {
          customSpeciesBox.classList.remove("visible");
        }
        renderSpeciesList();
        validateSaveState();
      });

      speciesListEl.appendChild(div);
    });
  }

  // Habitat tab click
  habitatTabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      habitatTabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      currentHabitat = tab.getAttribute("data-habitat");
      renderSpeciesList();
    });
  });

  // Search input
  speciesSearchInput.addEventListener("input", () => {
    renderSpeciesList();
  });

  // Custom species input
  customSpeciesInput.addEventListener("input", () => {
    customSpeciesName = customSpeciesInput.value.trim();
    validateSaveState();
  });

  // Validation
  function validateSaveState() {
    const hasRegion = activeRegion && (activeRegion.end - activeRegion.start) >= 0.1;
    let hasSpecies = false;
    if (selectedSpeciesSlug === "_custom") {
      hasSpecies = !!customSpeciesInput.value.trim();
    } else {
      hasSpecies = !!selectedSpeciesSlug;
    }

    const isValid = hasRegion && hasSpecies;
    btnSave.disabled = !isValid;

    if (activeRegion) {
      const dur = (activeRegion.end - activeRegion.start).toFixed(2);
      btnSave.textContent = `✅ Сохранить фрагмент (${dur}с)`;
    } else {
      btnSave.textContent = "✅ Сохранить фрагмент";
    }
  }

  // Save button action
  btnSave.addEventListener("click", () => {
    if (!activeRegion) return;

    const startSec = Math.round(activeRegion.start * 100) / 100;
    const endSec = Math.round(activeRegion.end * 100) / 100;
    const payload = {
      submission_id: parseInt(subId, 10),
      species_slug: selectedSpeciesSlug === "_custom" ? "_proposed" : selectedSpeciesSlug,
      proposed_name: selectedSpeciesSlug === "_custom" ? customSpeciesInput.value.trim() : null,
      start_sec: startSec,
      end_sec: endSec
    };

    const jsonStr = JSON.stringify(payload);

    if (window.Telegram?.WebApp && window.Telegram.WebApp.sendData) {
      window.Telegram.WebApp.sendData(jsonStr);
      window.Telegram.WebApp.close();
    } else {
      alert("Данные для отправки боту (эмуляция вне Telegram):\n" + jsonStr);
    }
  });

  // Initial render
  renderSpeciesList();
});
