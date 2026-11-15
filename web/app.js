/* SuperFoto UNO Q - UI sin dependencias externas. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const IP_STORAGE_KEY = "superfoto.ipUrl";
  const state = {
    polling: false,
    status: null,
    sessions: [],
    currentSession: null,
    img: null,
    zoom: 1,
    fitZoom: 1,
    offsetX: 0,
    offsetY: 0,
    dragging: false,
    dragStart: null,
    logLines: [],
    lastState: null,
    seenSession: null,
    sceneWarned: false,
    adviceWarned: "",
  };

  // ---------------------------------------------------------------- utils
  const fmtBytes = (n) => {
    if (!n && n !== 0) return "—";
    if (n > 1e9) return (n / 1e9).toFixed(2) + " GB";
    if (n > 1e6) return (n / 1e6).toFixed(1) + " MB";
    if (n > 1e3) return (n / 1e3).toFixed(0) + " kB";
    return n + " B";
  };
  const fmtTime = (s) => {
    if (s == null) return "—";
    if (s < 60) return s.toFixed(1) + " s";
    const m = Math.floor(s / 60);
    return m + " min " + Math.round(s - m * 60) + " s";
  };
  const log = (msg) => {
    const t = new Date().toLocaleTimeString("es-AR", { hour12: false });
    state.logLines.push(`[${t}] ${msg}`);
    if (state.logLines.length > 200) state.logLines.shift();
    $("log").textContent = state.logLines.join("\n");
    $("log").scrollTop = $("log").scrollHeight;
  };

  async function api(path, options) {
    const resp = await fetch(path, options);
    const text = await resp.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { error: text }; }
    if (!resp.ok) {
      const err = new Error(data.error || `HTTP ${resp.status}`);
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------- IP Webcam (URL)
  function saveIpUrl(value) {
    const text = (value !== undefined ? value : $("url").value || "").trim();
    try {
      if (text) localStorage.setItem(IP_STORAGE_KEY, text);
      else localStorage.removeItem(IP_STORAGE_KEY);
    } catch (err) { /* modo privado: no es critico */ }
  }

  function restoreIpUrl() {
    try {
      const saved = localStorage.getItem(IP_STORAGE_KEY);
      if (saved && !$("url").value) $("url").value = saved;
    } catch (err) { /* sin localStorage */ }
  }

  function setProbeInfo(text, kind) {
    const node = $("probeInfo");
    node.textContent = text || "";
    node.className = "small " + (kind === "ok" ? "ok" : kind === "warn" ? "warn" : "muted");
  }

  async function probe() {
    const value = ($("url").value || "").trim();
    if (!value) {
      setProbeInfo("Escribí la IP del teléfono", "warn");
      $("url").focus();
      return;
    }
    saveIpUrl(value);
    const button = $("probeBtn");
    button.disabled = true;
    setProbeInfo("probando...", "muted");
    try {
      const data = await api("/api/probe?url=" + encodeURIComponent(value));
      const res = Array.isArray(data.resolution) ? data.resolution : [0, 0];
      $("url").value = data.url;  // forma canonica normalizada
      saveIpUrl(data.url);
      setProbeInfo(`Conectado: ${res[1]}x${res[0]}`, "ok");
      log(`Camara OK: ${data.url} (${res[1]}x${res[0]}, ${data.content_type || "jpeg"})`);
    } catch (err) {
      setProbeInfo("No se pudo conectar: " + (err.message || "error"), "warn");
      log("Probar conexion fallo: " + err.message);
    } finally {
      button.disabled = false;
    }
  }

  // ---------------------------------------------------------------- forms
  function syncVisibility() {
    const src = $("source").value;
    $("urlField").classList.toggle("hidden", !(src === "ip" || src === "auto"));
    $("autoHint").classList.toggle("hidden", src !== "auto");
    $("probeBtn").title = src === "auto" ? "Probar la IP escrita (opcional)" : "";
    $("pathField").classList.toggle("hidden", !(src === "file" || src === "folder"));
    $("indexField").classList.toggle("hidden", src !== "webcam");
    $("path").placeholder = src === "folder"
      ? "/home/arduino/imagenes"
      : "/home/arduino/videos/hoja.mp4";
  }

  function payload() {
    const src = $("source").value;
    const args = {};
    if (src === "ip" || src === "auto") {
      if ($("url").value.trim()) args.url = $("url").value.trim();
    }
    if (src === "file" || src === "folder") {
      if ($("path").value.trim()) args.path = $("path").value.trim();
    }
    if (src === "webcam") args.index = Number($("index").value || 0);
    if (src === "synthetic") {
      args.width = 640;
      args.height = 480;
      args.frames = 0;
      args.noise_sigma = 3.0;
      args.jpeg_quality = 88;
      args.optical_sigma = 0.5;
    }
    return {
      source: src,
      args,
      scale: Number($("scale").value),
      quality_percentile: Number($("quality").value),
      psf_sigma: Number($("psf").value),
      rl_iterations: Number($("rl").value),
      target_frames: Number($("frames").value || 0),
      duration_s: Number($("duration").value || 0),
      label: src,
    };
  }

  function updateSliderLabels() {
    $("scaleVal").textContent = Number($("scale").value).toFixed(1);
    $("qualityVal").textContent = $("quality").value;
    $("psfVal").textContent = Number($("psf").value).toFixed(1);
    $("rlVal").textContent = $("rl").value;
  }

  // ---------------------------------------------------------------- start/stop
  async function start() {
    $("actionMsg").textContent = "";
    $("actionMsg").className = "small";
    const src = $("source").value;
    const ipValue = ($("url").value || "").trim();
    if (src === "ip" && !ipValue) {
      $("actionMsg").textContent = "Escribí la IP del teléfono o usá Buscar cámara";
      $("actionMsg").className = "small warn";
      $("url").focus();
      $("url").classList.add("fieldError");
      return;
    }
    $("url").classList.remove("fieldError");
    if (ipValue) saveIpUrl(ipValue);
    try {
      const data = await api("/api/session/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload()),
      });
      log(`Sesion iniciada: ${data.session_id} (escala ${data.scale})`);
      $("startBtn").disabled = true;
      $("stopBtn").disabled = false;
      state.seenSession = data.session_id;
    } catch (err) {
      const msg = err.message || "error desconocido";
      $("actionMsg").textContent = msg;
      $("actionMsg").className = "small";
      log("ERROR al iniciar: " + msg);
      if (err.status === 400) {
        log("Sugerencia: usa 'Demo sintetica' o 'Archivo de video' si la camara esta apagada.");
      }
      $("startBtn").disabled = false;
      $("stopBtn").disabled = true;
    }
  }

  async function stop() {
    try {
      const data = await api("/api/session/stop", { method: "POST" });
      log("Detencion solicitada: " + (data.status || "?"));
      $("stopBtn").disabled = true;
    } catch (err) {
      log("ERROR al detener: " + err.message);
    }
  }

  // ---------------------------------------------------------------- status
  async function poll() {
    if (state.polling) return;
    state.polling = true;
    try {
      const s = await api("/api/status");
      state.status = s;
      renderStatus(s);
      if (s.session_id && s.session_id !== state.currentSession) {
        state.currentSession = s.session_id;
      }
      if (s.status === "finished" && s.session_id && state.seenSession !== s.session_id) {
        state.seenSession = s.session_id;
        log("Superfoto guardada: " + s.session_id);
        await loadSessions();
        openResult(s.session_id);
      }
    } catch (err) {
      $("serviceInfo").textContent = "sin conexion";
    } finally {
      state.polling = false;
    }
  }

  function renderStatus(s) {
    const pill = $("statePill");
    const map = {
      idle: ["idle", "inactivo"],
      starting: ["processing", "iniciando"],
      capturing: ["capturing", "capturando"],
      processing: ["processing", "procesando"],
      finished: ["finished", "listo"],
      error: ["error", "error"],
      stopped: ["idle", "detenido"],
    };
    const [cls, text] = map[s.status] || ["idle", s.status || "?"];
    pill.className = "pill " + cls;
    pill.textContent = text;

    const svc = s.service || {};
    $("serviceInfo").textContent = svc.disk
      ? `libre ${Math.round(svc.disk.free_mb)} MB · ${svc.sessions_count} sesiones`
      : "";
    if (svc.disk) $("footDisk").textContent = `Disco: ${Math.round(svc.disk.free_mb)} MB libres de ${Math.round(svc.disk.total_mb)} MB`;

    const active = ["starting", "capturing", "processing"].includes(s.status);
    $("startBtn").disabled = active;
    $("stopBtn").disabled = !active || s.status === "processing";

    const pct = Math.round((s.progress || 0) * 100);
    $("progressBar").style.width = pct + "%";
    $("progressPct").textContent = pct + "%";
    $("progressLabel").textContent = `${s.status || "idle"} ${s.message ? "· " + s.message : ""}`;

    $("statSeen").textContent = s.seen ?? 0;
    $("statAccepted").textContent = s.accepted ?? 0;
    $("statRejected").textContent = s.rejected ?? 0;
    $("statAlign").textContent = s.align_invalid ?? 0;
    $("statElapsed").textContent = fmtTime(s.elapsed_seconds);
    $("statOutput").textContent = s.output_size_estimate || "—";
    $("statThreshold").textContent = s.quality && s.quality.threshold != null
      ? Number(s.quality.threshold).toFixed(1) : "—";
    $("statCoverage").textContent = s.coverage && s.coverage.coverage_4 != null
      ? Math.round(s.coverage.coverage_4 * 100) + "%" : "—";

    if (s.error) {
      $("actionMsg").textContent = s.error;
      $("actionMsg").className = "small warn";
    } else if (s.advice) {
      // El aviso puede ser de escena en movimiento o de fuente desenfocada.
      $("actionMsg").textContent = s.advice;
      $("actionMsg").className = "small warn";
      if (state.adviceWarned !== s.advice) {
        state.adviceWarned = s.advice;
        log("AVISO: " + s.advice);
      }
    } else if ($("actionMsg").classList.contains("warn")) {
      $("actionMsg").className = "small";
      state.adviceWarned = "";
    }
    if (s.status === "capturing" && !s.advice) state.adviceWarned = "";
    if (s.status === "idle" || s.status === "starting") state.adviceWarned = "";

    if (s.status === "processing" && state.lastState !== "processing") {
      log("Postproceso en curso (IBP + deconvolucion). Puede tardar.");
    }
    if (s.status === "capturing" && state.lastState !== "capturing") {
      log("Capturando frames...");
    }
    state.lastState = s.status;

    if (s.has_preview && (s.status === "capturing" || s.status === "processing")) {
      const img = $("previewImg");
      const stamp = Date.now();
      img.src = `/api/preview?t=${stamp}`;
    }
  }

  // ---------------------------------------------------------------- visor
  function stageSize() {
    const stage = $("viewerStage");
    const rect = stage.getBoundingClientRect();
    return { w: Math.max(100, rect.width), h: Math.max(100, rect.height) };
  }

  function resizeCanvas() {
    const canvas = $("viewer");
    const { w, h } = stageSize();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    draw();
  }

  function fitZoomValue() {
    if (!state.img) return 1;
    const { w, h } = stageSize();
    return Math.min(w / state.img.width, h / state.img.height);
  }

  function draw() {
    const canvas = $("viewer");
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!state.img) return;
    const z = state.zoom * dpr;
    const cx = canvas.width / 2 + state.offsetX * dpr;
    const cy = canvas.height / 2 + state.offsetY * dpr;
    const dw = state.img.width * z;
    const dh = state.img.height * z;
    ctx.imageSmoothingEnabled = state.zoom < 2;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(state.img, cx - dw / 2, cy - dh / 2, dw, dh);
    $("zoomLabel").textContent = Math.round(state.zoom * 100) + "%";
    if (state.img) {
      const visibleW = Math.min(state.img.width, stageSize().w / state.zoom);
      $("viewerInfo").textContent =
        `${state.img.width}×${state.img.height} px · visible ${Math.round(visibleW)} px`;
    }
  }

  function setZoom(z, keepCenter = true) {
    if (!state.img) return;
    const min = state.fitZoom * 0.5;
    state.zoom = Math.max(min, Math.min(8, z));
    if (state.zoom <= state.fitZoom * 1.02) {
      state.zoom = state.fitZoom;
      if (!keepCenter) { state.offsetX = 0; state.offsetY = 0; }
    }
    draw();
  }

  async function openResult(sessionId, key = "jpg") {
    const url = `/api/result/${sessionId}/${key}`;
    $("downloadLink").href = `/api/result/${sessionId}/png`;
    $("downloadLink").classList.remove("hidden");
    const img = new Image();
    img.onload = () => {
      state.img = img;
      state.fitZoom = fitZoomValue();
      state.zoom = state.fitZoom;
      state.offsetX = 0;
      state.offsetY = 0;
      $("viewerEmpty").classList.add("hidden");
      resizeCanvas();
      highlightHistory(sessionId);
    };
    img.onerror = () => log("No se pudo cargar la imagen de la sesion " + sessionId);
    img.src = url + "?t=" + Date.now();
  }

  function cropVisible() {
    if (!state.img) return null;
    const canvas = $("viewer");
    const dpr = window.devicePixelRatio || 1;
    const z = state.zoom * dpr;
    const cx = canvas.width / 2 + state.offsetX * dpr;
    const cy = canvas.height / 2 + state.offsetY * dpr;
    const dw = state.img.width * z;
    const dh = state.img.height * z;
    const left = Math.max(0, cx - dw / 2);
    const top = Math.max(0, cy - dh / 2);
    const visW = Math.min(canvas.width, cx + dw / 2) - left;
    const visH = Math.min(canvas.height, cy + dh / 2) - top;
    const sx = (left - (cx - dw / 2)) / z;
    const sy = (top - (cy - dh / 2)) / z;
    const sw = visW / z;
    const sh = visH / z;
    return {
      sx: Math.max(0, sx), sy: Math.max(0, sy),
      sw: Math.min(state.img.width - sx, sw),
      sh: Math.min(state.img.height - sy, sh),
    };
  }

  function downloadCanvas(canvas, name) {
    canvas.toBlob((blob) => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    }, "image/png");
  }

  function downloadCrop(mode) {
    if (!state.img) { log("No hay imagen para recortar."); return; }
    const rect = cropVisible();
    if (!rect || rect.sw < 2 || rect.sh < 2) return;
    const sid = state.currentSession || "superfoto";
    if (mode === "1to1") {
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(rect.sw);
      canvas.height = Math.round(rect.sh);
      const ctx = canvas.getContext("2d");
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(state.img, rect.sx, rect.sy, rect.sw, rect.sh, 0, 0, canvas.width, canvas.height);
      downloadCanvas(canvas, `superfoto_${sid}_recorte_1a1.png`);
      log(`Recorte 1:1 descargado (${canvas.width}×${canvas.height}).`);
    } else {
      // "Microscopio": cuarto central del area visible, ampliado 4x sin interpolar.
      const sw = Math.max(8, rect.sw / 4);
      const sh = Math.max(8, rect.sh / 4);
      const sx = rect.sx + (rect.sw - sw) / 2;
      const sy = rect.sy + (rect.sh - sh) / 2;
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(sw * 4);
      canvas.height = Math.round(sh * 4);
      const ctx = canvas.getContext("2d");
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(state.img, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
      downloadCanvas(canvas, `superfoto_${sid}_central_4x.png`);
      log(`Recorte central 4x (digital, sin interpolar): ${Math.round(sw)}×${Math.round(sh)} px de origen.`);
    }
  }

  // ---------------------------------------------------------------- historial
  async function loadSessions() {
    try {
      const data = await api("/api/sessions");
      state.sessions = data.sessions || [];
      renderHistory();
    } catch (err) {
      log("No se pudo leer el historial: " + err.message);
    }
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function renderHistory() {
    // Solo createElement + textContent: nunca innerHTML con datos del servidor.
    const box = $("history");
    box.replaceChildren();
    if (!state.sessions.length) {
      box.appendChild(el("p", "small muted", "Sin sesiones guardadas."));
      return;
    }
    for (const s of state.sessions) {
      const item = el("div", "histItem");
      item.dataset.id = s.id;

      const head = el("div", "histHead");
      head.appendChild(el("b", "", s.id));
      const scale = Number(s.scale || 0);
      head.appendChild(el("span", "small muted", scale.toFixed(2) + "x"));
      item.appendChild(head);

      const m = s.metrics || {};
      const gt = m.ground_truth && m.ground_truth.gain_db != null
        ? ` · GT ${Number(m.ground_truth.gain_db) >= 0 ? "+" : ""}${m.ground_truth.gain_db} dB`
        : "";
      const shape = Array.isArray(s.output_shape) && s.output_shape.length === 2
        ? `${s.output_shape[1]}×${s.output_shape[0]}`
        : "?";
      const frames = s.frames_accepted != null ? s.frames_accepted : "?";
      const meta = `${shape} · ${frames} frames · ${fmtTime(s.elapsed_seconds)} · ` +
        `${fmtBytes(s.bytes_total)}${gt}`;
      item.appendChild(el("div", "histMeta", meta));

      const links = el("div", "histLinks");
      const view = el("button", "", "Ver");
      view.type = "button";
      view.addEventListener("click", () => openResult(s.id));
      links.appendChild(view);
      for (const [key, label] of [["png", "PNG"], ["jpg", "JPG"], ["compare", "Comparar"], ["json", "JSON"]]) {
        if (!s.files || !s.files[key]) continue;
        const a = el("a", "", label);
        a.href = `/api/result/${encodeURIComponent(s.id)}/${encodeURIComponent(key)}`;
        a.download = "";
        links.appendChild(a);
      }
      item.appendChild(links);
      box.appendChild(item);
    }
  }

  function highlightHistory(id) {
    document.querySelectorAll(".histItem").forEach((el) => {
      el.classList.toggle("active", el.dataset.id === id);
    });
  }

  // ---------------------------------------------------------------- descubrir
  async function discover() {
    const button = $("discoverBtn");
    button.disabled = true;
    setProbeInfo("buscando en la LAN...", "muted");
    try {
      const data = await api("/api/discover");
      const cams = data.cameras || [];
      if (!cams.length) {
        setProbeInfo("ninguna cámara encontrada en " + (data.subnet || "la LAN"), "warn");
        log("Descubrimiento: sin camaras IP Webcam en " + (data.subnet || "la LAN"));
        return;
      }
      $("url").value = cams[0].url;
      $("source").value = "ip";
      syncVisibility();
      saveIpUrl(cams[0].url);
      setProbeInfo(`${cams.length} encontrada(s): ${cams[0].url}`, "ok");
      log("Camaras: " + cams.map((c) => `${c.url} (${c.resolution || "?"})`).join(", "));
      await probe();
    } catch (err) {
      setProbeInfo("error al buscar: " + err.message, "warn");
      log("Descubrimiento fallo: " + err.message);
    } finally {
      button.disabled = false;
    }
  }

  // ---------------------------------------------------------------- eventos
  function bind() {
    $("source").addEventListener("change", syncVisibility);
    ["scale", "quality", "psf", "rl"].forEach((id) => $(id).addEventListener("input", updateSliderLabels));
    $("startBtn").addEventListener("click", start);
    $("stopBtn").addEventListener("click", stop);
    $("probeBtn").addEventListener("click", probe);
    $("discoverBtn").addEventListener("click", discover);
    $("url").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); probe(); }
    });
    $("url").addEventListener("change", () => saveIpUrl());
    $("presetReal").addEventListener("click", () => {
      $("source").value = "ip";
      $("duration").value = 300;
      $("frames").value = 0;
      $("scale").value = 1.5;
      $("quality").value = 55;
      syncVisibility(); updateSliderLabels();
      if (!$("url").value.trim()) {
        $("url").focus();
        setProbeInfo("Escribí la IP del teléfono o usá Buscar cámara", "warn");
      }
      log("Preset 'Prueba real': 300 s, todos los frames, 1.5x. Enfoca la hoja y pulsa Iniciar.");
    });
    $("presetDemo").addEventListener("click", () => {
      $("source").value = "synthetic";
      $("duration").value = 30;
      $("frames").value = 60;
      $("scale").value = 2;
      $("quality").value = 40;
      syncVisibility(); updateSliderLabels();
      log("Preset 'Demo rapida': fuente sintetica, 60 frames, 2x.");
    });

    document.querySelectorAll(".tab").forEach((tab) => {
      if (!tab.dataset.tab) return;
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        $("tab-result").classList.toggle("hidden", tab.dataset.tab !== "result");
        $("tab-preview").classList.toggle("hidden", tab.dataset.tab !== "preview");
        if (tab.dataset.tab === "result") resizeCanvas();
      });
    });

    $("zoomIn").addEventListener("click", () => setZoom(state.zoom * 1.4));
    $("zoomOut").addEventListener("click", () => setZoom(state.zoom / 1.4));
    $("zoomFit").addEventListener("click", () => { state.zoom = state.fitZoom; state.offsetX = 0; state.offsetY = 0; draw(); });
    $("zoom100").addEventListener("click", () => { setZoom(1, false); state.offsetX = 0; state.offsetY = 0; draw(); });
    $("cropBtn").addEventListener("click", () => downloadCrop("1to1"));
    $("crop4Btn").addEventListener("click", () => downloadCrop("4x"));

    const stage = $("viewerStage");
    stage.addEventListener("wheel", (ev) => {
      if (!state.img) return;
      ev.preventDefault();
      const factor = ev.deltaY < 0 ? 1.18 : 1 / 1.18;
      const canvas = $("viewer");
      const rect = canvas.getBoundingClientRect();
      const mx = ev.clientX - rect.left - rect.width / 2;
      const my = ev.clientY - rect.top - rect.height / 2;
      const before = state.zoom;
      setZoom(state.zoom * factor);
      const k = state.zoom / before;
      state.offsetX = (state.offsetX - mx) * k + mx;
      state.offsetY = (state.offsetY - my) * k + my;
      draw();
    }, { passive: false });

    stage.addEventListener("mousedown", (ev) => {
      if (!state.img) return;
      state.dragging = true;
      state.dragStart = { x: ev.clientX - state.offsetX, y: ev.clientY - state.offsetY };
      $("viewer").classList.add("dragging");
    });
    window.addEventListener("mousemove", (ev) => {
      if (!state.dragging) return;
      state.offsetX = ev.clientX - state.dragStart.x;
      state.offsetY = ev.clientY - state.dragStart.y;
      draw();
    });
    window.addEventListener("mouseup", () => {
      state.dragging = false;
      $("viewer").classList.remove("dragging");
    });
    window.addEventListener("resize", () => {
      state.fitZoom = fitZoomValue();
      resizeCanvas();
    });
  }

  async function init() {
    bind();
    restoreIpUrl();
    syncVisibility();
    updateSliderLabels();
    log("UI lista. Servicio superfoto-unoq.");
    try {
      const cfg = await api("/api/sources");
      const d = cfg.defaults || {};
      if (d.scale) $("scale").value = d.scale;
      if (d.quality_percentile != null) $("quality").value = Math.round(d.quality_percentile);
      if (d.psf_sigma) $("psf").value = d.psf_sigma;
      if (d.rl_iterations != null) $("rl").value = d.rl_iterations;
      if (d.source_url) $("url").value = d.source_url;
      updateSliderLabels();
      if (d.source && d.source !== "auto") { $("source").value = d.source; syncVisibility(); }
    } catch (err) { /* defaults del HTML */ }
    await loadSessions();
    await poll();
    setInterval(poll, 700);
    setInterval(loadSessions, 6000);
    if (state.sessions.length) openResult(state.sessions[0].id);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
