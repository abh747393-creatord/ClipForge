"use strict";
/* ClipForge UI — talks to the API exactly as specified in app/server.py. */

const $ = (id) => document.getElementById(id);
const state = {
  layouts: [], styles: [], anims: [],
  layout: "stack", style: "bounce",
  presetFacecam: null,
  currentJobId: null, currentJob: null,
  regions: { facecam: null, game: null },
  liveId: null,
  jobTimer: null, liveTimer: null,
  uploadFile: null,
};

const VIEW_TITLES = {
  new: ["New Job", "Paste a link or drop a video — get captioned clips."],
  job: ["Job", "Clips, regions and re-renders for this video."],
  library: ["Library", "Every job you have made."],
  live: ["Live", "Clip moments while the stream is still on."],
  settings: ["Settings", "AI endpoint and speech model."],
};

/* ---------------- API helpers ---------------- */
async function api(path, opts) {
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || ("request failed (" + res.status + ")"));
  return data;
}
function postJSON(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}
function toast(msg, ok) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (ok ? " ok" : "");
  t.hidden = false;
  clearTimeout(t._hide);
  t._hide = setTimeout(() => { t.hidden = true; }, 4500);
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function baseName(p) { return String(p || "").split("/").pop().split("\\").pop(); }
function fmtTime(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + ":" + String(s).padStart(2, "0");
}
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

/* ---------------- layout regions ----------------
   Mirrors layouts.geometry(): these layouts draw a separate camera layer
   and/or gameplay layer, so the region editor applies to them. */
const CAM_LAYOUTS = new Set(["auto", "stack", "bottom", "side", "pip_tl", "pip_tr", "pip_bl", "pip_br", "circle"]);
const GAME_LAYOUTS = new Set(["auto", "stack", "bottom", "side", "game", "pip_tl", "pip_tr", "pip_bl", "pip_br", "circle"]);
function needsCam(id) { return CAM_LAYOUTS.has(id || ""); }
function needsGame(id) { return GAME_LAYOUTS.has(id || ""); }
function layoutPreview(id) {
  id = id || "";
  if (/^pip_|circle/.test(id)) return '<div class="pv"><i class="game" style="inset:3%"></i><i class="cam" style="right:5%;top:5%;width:34%;height:24%"></i></div>';
  if (id === "side") return '<div class="pv"><i class="cam" style="left:3%;top:3%;width:32%;height:94%"></i><i class="game" style="right:3%;top:3%;width:60%;height:94%"></i></div>';
  if (id === "split") return '<div class="pv"><i class="full" style="left:3%;top:3%;width:94%;height:45%"></i><i class="full" style="left:3%;bottom:3%;width:94%;height:45%"></i></div>';
  if (id === "stack") return '<div class="pv"><i class="cam" style="left:3%;top:3%;width:94%;height:32%"></i><i class="game" style="left:3%;bottom:3%;width:94%;height:60%"></i></div>';
  if (id === "bottom") return '<div class="pv"><i class="game" style="left:3%;top:3%;width:94%;height:60%"></i><i class="cam" style="left:3%;bottom:3%;width:94%;height:32%"></i></div>';
  if (id === "game") return '<div class="pv"><i class="game" style="inset:3%"></i></div>';
  if (id === "auto") return '<div class="pv"><i class="cam" style="left:3%;top:3%;width:94%;height:32%"></i><i class="game" style="left:3%;bottom:3%;width:94%;height:60%"></i><span class="pv-badge">AUTO</span></div>';
  if (id === "follow") return '<div class="pv"><i class="full" style="inset:3%"></i><i class="cam" style="left:33%;top:18%;width:34%;height:30%"></i></div>';
  return '<div class="pv"><i class="full" style="inset:3%"></i></div>';
}
const PRESETS = {
  tl: { x: 0.02, y: 0.02, w: 0.28, h: 0.28 },
  tr: { x: 0.70, y: 0.02, w: 0.28, h: 0.28 },
  bl: { x: 0.02, y: 0.70, w: 0.28, h: 0.28 },
  br: { x: 0.70, y: 0.70, w: 0.28, h: 0.28 },
};

/* ---------------- options ---------------- */
function collectOptions() {
  const lang = $("optLanguage").value;
  const stars = $("optStars").value;
  return {
    count: parseInt($("optCount").value, 10) || 4,
    min_len: parseInt($("optMinLen").value, 10) || 15,
    max_len: parseInt($("optMaxLen").value, 10) || 60,
    layout: state.layout,
    style: state.style,
    anim: $("optAnim").value || "highlight",
    aspect: $("optAspect").value,
    quality: parseInt($("optQuality").value, 10) || 1080,
    language: lang || null,
    hinglish: $("optHinglish").checked || lang === "hinglish",
    use_llm: $("optLlm").checked,
    captions: $("optCaptions").checked,
    hook_enabled: $("optHook").checked,
    facecam: state.presetFacecam,
    game: null,
    game_events: $("optGameEvents").checked,
    focus: $("optFocus").value.trim(),
    brain_mode: $("optBrainMode").value,
    min_score: parseInt($("optMinScore").value, 10) || 0,
    preset: $("optPreset").value || null,
    title_bar: $("optTitleBar").checked,
    killcam: $("optKillcam").checked,
    gta_overlay: {
      stars: stars === "" ? null : (stars === "auto" ? "auto" : parseInt(stars, 10)),
      mission_card: $("optMissionCard").checked,
    },
    campaign: {
      handle: $("optCampaignHandle").value.trim(),
      tag: $("optCampaignTag").value.trim() || "#ad",
    },
  };
}
function liveOptions() {
  const o = collectOptions();
  o.layout = $("liveLayout").value || o.layout;
  o.style = $("liveStyle").value || o.style;
  o.ai_every_min = parseInt($("liveEvery").value, 10) || 3;
  return o;
}

/* ---------------- view switching ---------------- */
function showView(name) {
  for (const v of ["new", "job", "library", "live", "settings"]) {
    $("view-" + v).hidden = v !== name;
  }
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("on", b.dataset.view === name));
  const t = VIEW_TITLES[name] || VIEW_TITLES.new;
  $("viewTitle").textContent = t[0];
  $("viewSub").textContent = t[1];
  if (name !== "job") stopJobPolling();
  if (name === "library") loadLibrary().catch((e) => toast(e.message));
  if (name === "settings") loadSettings().catch((e) => toast(e.message));
}

/* ---------------- status ---------------- */
async function loadStatus() {
  try {
    const s = await api("/api/status");
    const dot = $("statusDot");
    dot.className = "status-dot " + (s.ok && s.ffmpeg ? "ok" : "bad");
    dot.title = "ClipForge " + s.version + " · ffmpeg " + (s.ffmpeg ? "ready" : "MISSING") + " · AI " + (s.llm_available ? "on" : "off");
    $("sideStatus").textContent = "v" + s.version + " · ASR " + s.asr_model + " · AI " + (s.llm_available ? "on" : "off");
    $("asrModel").textContent = s.asr_model;
  } catch (e) {
    $("statusDot").className = "status-dot bad";
    $("sideStatus").textContent = "server unreachable";
  }
}

/* ---------------- catalogs (layouts/styles) ---------------- */
async function loadCatalogs() {
  const [layouts, styles, presets] = await Promise.all([
    api("/api/layouts"), api("/api/styles"), api("/api/presets").catch(() => []),
  ]);
  fillSelect($("optPreset"), [["", "None"]].concat((presets || []).map((p) => [p.id, p.label || p.id])), "");
  state.layouts = layouts || [];
  state.styles = (styles && styles.styles) || [];
  state.anims = (styles && styles.anims) || [];
  if (state.layouts.length && !state.layouts.some((l) => l.id === state.layout)) state.layout = state.layouts[0].id;
  if (state.styles.length && !state.styles.some((s) => s.id === state.style)) state.style = state.styles[0].id;

  const grid = $("layoutGrid");
  grid.innerHTML = "";
  for (const l of state.layouts) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "layout-card" + (l.id === state.layout ? " on" : "");
    b.dataset.layout = l.id;
    b.innerHTML = layoutPreview(l.id) + '<span class="lc-label">' + esc(l.label || l.id) + '</span><span class="lc-desc">' + esc(l.desc || "") + "</span>";
    b.addEventListener("click", () => selectLayout(l.id));
    grid.appendChild(b);
  }
  const chips = $("styleChips");
  chips.innerHTML = "";
  for (const s of state.styles) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "style-chip" + (s.id === state.style ? " on" : "");
    b.dataset.style = s.id;
    const aaColor = s.active || "#fff";
    const dotColor = s.base || "#888";
    b.innerHTML = '<span class="aa" style="color:' + esc(aaColor) + '">Aa</span><span class="dot" style="background:' + esc(dotColor) + '"></span>' + esc(s.label || s.id);
    b.title = s.font || "";
    b.addEventListener("click", () => selectStyle(s.id));
    chips.appendChild(b);
  }
  const animSel = $("optAnim");
  animSel.innerHTML = "";
  for (const a of state.anims) {
    const o = document.createElement("option");
    o.value = a; o.textContent = a;
    animSel.appendChild(o);
  }
  if (state.anims.includes("highlight")) animSel.value = "highlight";

  fillSelect($("liveLayout"), state.layouts.map((l) => [l.id, l.label || l.id]), state.layout);
  fillSelect($("liveStyle"), state.styles.map((s) => [s.id, s.label || s.id]), state.style);
  updatePresetRow();
}
function fillSelect(sel, pairs, value) {
  sel.innerHTML = "";
  for (const [v, label] of pairs) {
    const o = document.createElement("option");
    o.value = v; o.textContent = label;
    sel.appendChild(o);
  }
  if (value) sel.value = value;
}
function selectLayout(id) {
  state.layout = id;
  document.querySelectorAll(".layout-card").forEach((c) => c.classList.toggle("on", c.dataset.layout === id));
  updatePresetRow();
}
function selectStyle(id) {
  state.style = id;
  document.querySelectorAll(".style-chip").forEach((c) => c.classList.toggle("on", c.dataset.style === id));
}
function updatePresetRow() {
  $("presetRow").hidden = !needsCam(state.layout);
}

/* ---------------- new job ---------------- */
function setUploadFile(f) {
  state.uploadFile = f || null;
  $("dzFile").textContent = f ? f.name : "";
}
async function makeClips() {
  const btn = $("makeBtn");
  btn.disabled = true;
  try {
    let job;
    if (state.uploadFile) {
      const fd = new FormData();
      fd.append("file", state.uploadFile);
      fd.append("options", JSON.stringify(collectOptions()));
      job = await api("/api/upload", { method: "POST", body: fd });
    } else {
      const url = $("urlInput").value.trim();
      if (!url) { toast("Paste a video link or choose a file first."); return; }
      job = await postJSON("/api/jobs", { url: url, options: collectOptions() });
    }
    openJob(job.id);
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- job view ---------------- */
function openJob(id) {
  state.currentJobId = id;
  showView("job");
  refreshJob().catch((e) => toast(e.message));
  startJobPolling();
}
function startJobPolling() {
  stopJobPolling();
  state.jobTimer = setInterval(() => {
    refreshJob().catch(() => { /* keep polling through blips */ });
  }, 2000);
}
function stopJobPolling() {
  if (state.jobTimer) { clearInterval(state.jobTimer); state.jobTimer = null; }
}
async function refreshJob() {
  if (!state.currentJobId) return;
  const job = await api("/api/jobs/" + state.currentJobId);
  state.currentJob = job;
  renderJob(job);
  if (job.status === "done" || job.status === "error" || job.status === "waiting_ai") stopJobPolling();
}
function renderJob(job) {
  $("jobTitle").textContent = job.title || (job.source && job.source.name) || "Job";
  const meta = [];
  if (job.uploader) meta.push(job.uploader);
  if (job.duration) meta.push(fmtTime(job.duration));
  if (job.width) meta.push(job.width + "×" + job.height);
  if (job.transcript_from && job.transcript_from !== "none") meta.push("transcript: " + job.transcript_from);
  $("jobMeta").textContent = meta.join(" · ");
  const st = $("jobStatus");
  st.textContent = job.status;
  st.className = "badge " + job.status;
  $("jobStage").textContent = job.stage || "";
  $("jobProgress").style.width = Math.round((job.progress || 0) * 100) + "%";
  const err = $("jobError");
  err.hidden = !job.error;
  err.textContent = job.error || "";
  $("jobNotes").innerHTML = (job.notes || []).map((n) => "<li>" + esc(n) + "</li>").join("");
  $("exportBtn").href = "/api/jobs/" + job.id + "/export.zip";
  $("exportBtn").style.pointerEvents = (job.clips || []).some((c) => c.status === "done") ? "" : "none";
  $("exportBtn").style.opacity = (job.clips || []).some((c) => c.status === "done") ? "" : ".45";
  $("postingBtn").href = "/media/" + job.id + "/POSTING.md";
  const ge = $("jobGameEvents");
  if ((job.game_events || []).length) {
    ge.hidden = false;
    ge.textContent = "🎮 Game events: " + job.game_events.map((e) => e.type + " @" + fmtTime(e.start)).join(" · ");
  } else {
    ge.hidden = true;
  }
  // manual AI panel
  const waiting = job.status === "waiting_ai";
  $("aiPanel").hidden = !waiting;
  if (waiting) {
    const wrap = $("promptBtns");
    wrap.innerHTML = "";
    (job.prompts || []).forEach((p) => {
      const b = document.createElement("button");
      b.className = "btn sm";
      b.textContent = "📋 Copy prompt " + (p.part + 1) + " (" + fmtTime(p.start) + "–" + fmtTime(p.end) + ")";
      b.addEventListener("click", async () => {
        try {
          const res = await fetch("/api/jobs/" + job.id + "/prompts/" + p.part);
          const text = await res.text();
          await navigator.clipboard.writeText(text);
          toast("Prompt copied — paste it into your AI.", true);
        } catch (e) { toast("Could not copy: " + e.message); }
      });
      wrap.appendChild(b);
    });
  }

  // region editor: show once the preview frame exists and the layout uses regions
  const layout = (job.options && job.options.layout) || state.layout;
  const showRegions = needsCam(layout) || needsGame(layout);
  if (showRegions) {
    const img = $("frameImg");
    const src = "/media/" + job.id + "/frame.jpg";
    if (img.dataset.src !== src) {
      img.dataset.src = src;
      img.onload = () => { $("regionCard").hidden = false; syncRegionBoxes(job); };
      img.onerror = () => { $("regionCard").hidden = true; };
      img.src = src;
    } else if (img.complete && img.naturalWidth) {
      $("regionCard").hidden = false;
      syncRegionBoxes(job);
    }
  } else {
    $("regionCard").hidden = true;
  }

  const grid = $("clipGrid");
  grid.innerHTML = "";
  (job.clips || []).forEach((clip, i) => grid.appendChild(clipCard(job, clip, i)));
}
function syncRegionBoxes(job) {
  const opts = job.options || {};
  if (!state.regions.facecam && opts.facecam) state.regions.facecam = Object.assign({}, opts.facecam);
  if (!state.regions.game && opts.game) state.regions.game = Object.assign({}, opts.game);
  const layout = opts.layout || state.layout;
  placeBox($("boxFacecam"), needsCam(layout) ? state.regions.facecam : null);
  placeBox($("boxGame"), needsGame(layout) ? state.regions.game : null);
  if (opts.top) { $("topSlider").value = opts.top; $("topVal").textContent = Number(opts.top).toFixed(2); }
}
function placeBox(el, rect) {
  if (!rect) { el.hidden = true; return; }
  el.hidden = false;
  el.style.left = rect.x * 100 + "%";
  el.style.top = rect.y * 100 + "%";
  el.style.width = rect.w * 100 + "%";
  el.style.height = rect.h * 100 + "%";
}
function clipCard(job, clip, i) {
  const card = document.createElement("div");
  card.className = "clip-card";
  const media = (name) => "/media/" + job.id + "/" + encodeURIComponent(baseName(name));
  let html = "";
  if (clip.status === "done" && clip.file) {
    html += '<video controls preload="metadata" src="' + media(clip.file) + '"' + (clip.cover ? ' poster="' + media(clip.cover) + '"' : "") + "></video>";
  } else if (clip.status === "error") {
    html += '<div class="error-banner">Clip failed: ' + esc(clip.error || "unknown error") + "</div>";
  } else {
    html += '<div class="hint">Rendering… (' + esc(clip.status || "pending") + ")</div>";
  }
  html += '<div class="clip-top"><div class="clip-title">' + esc(clip.title || ("Clip " + (i + 1))) + '</div>';
  const vs = clip.viral_score != null ? clip.viral_score : clip.score;
  if (vs != null) html += '<span class="score" title="viral score">⚡' + esc(Math.round(Number(vs))) + "</span>";
  html += "</div>";
  if (clip.hook) html += '<div class="clip-hook">' + esc(clip.hook) + "</div>";
  const chips = [];
  if (clip.kind) chips.push(clip.kind);
  if (clip.game_event) chips.push("🎮 " + clip.game_event);
  if (clip.look) chips.push("look: " + clip.look);
  if (chips.length) html += '<div class="clip-reason">' + chips.map(esc).join(" · ") + "</div>";
  if (clip.why) html += '<div class="clip-reason">💡 ' + esc(clip.why) + "</div>";
  if (clip.reason) html += '<div class="clip-reason">' + esc(clip.reason) + "</div>";
  if (clip.check) {
    html += '<a href="' + media(clip.check) + '" target="_blank" rel="noopener" title="QC check sheet (3 frames)">'
      + '<img class="check-sheet" src="' + media(clip.check) + '" alt="check sheet"></a>';
  }
  const feats = clip.features || {};
  const featKeys = Object.keys(feats).filter((k) => typeof feats[k] === "number");
  if (featKeys.length) {
    html += '<div class="feats">' + featKeys.map((k) => {
      const v = feats[k];
      const pct = Math.round(Math.max(0, Math.min(1, v <= 1 ? v : v / 100)) * 100);
      return '<div class="feat"><span>' + esc(k) + '</span><span class="feat-bar"><span class="feat-fill" style="display:block;width:' + pct + '%"></span></span></div>';
    }).join("") + "</div>";
  }
  const dls = [["MP4", clip.file], ["SRT", clip.srt], ["VTT", clip.vtt], ["ASS", clip.ass], ["Cover", clip.cover], ["Check", clip.check], ["JSON", clip.meta]];
  html += '<div class="dl-row">' + dls.filter((d) => d[1]).map((d) => '<a class="btn" href="' + media(d[1]) + '" download>' + d[0] + "</a>").join("") + "</div>";
  html += '<div class="clip-ctl"><select class="clip-layout"></select><select class="clip-style"></select><button class="btn sm clip-rerender">Re-render</button></div>';
  card.innerHTML = html;

  const layoutSel = card.querySelector(".clip-layout");
  const styleSel = card.querySelector(".clip-style");
  fillSelect(layoutSel, state.layouts.map((l) => [l.id, l.label || l.id]), clip.layout || (job.options && job.options.layout) || state.layout);
  fillSelect(styleSel, state.styles.map((s) => [s.id, s.label || s.id]), (job.options && job.options.style) || state.style);
  card.querySelector(".clip-rerender").addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    try {
      await postJSON("/api/jobs/" + job.id + "/clips/" + i + "/rerender", {
        options: {
          layout: layoutSel.value, style: styleSel.value,
          facecam: state.regions.facecam, game: state.regions.game,
          top: parseFloat($("topSlider").value),
        },
      });
      toast("Clip re-rendered.", true);
      await refreshJob();
    } catch (e) { toast(e.message); }
    ev.target.disabled = false;
  });
  return card;
}

/* ---------------- region editor interactions ---------------- */
function makeDraggable(box, key) {
  let drag = null;
  box.addEventListener("pointerdown", (ev) => {
    const wrap = $("regionWrap").getBoundingClientRect();
    const rect = state.regions[key];
    if (!rect) return;
    const resizing = ev.target.classList.contains("handle");
    drag = { resizing: resizing, startX: ev.clientX, startY: ev.clientY, rect: Object.assign({}, rect), wrap: wrap };
    box.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  });
  box.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const dx = (ev.clientX - drag.startX) / drag.wrap.width;
    const dy = (ev.clientY - drag.startY) / drag.wrap.height;
    const r = Object.assign({}, drag.rect);
    if (drag.resizing) {
      r.w = Math.max(0.03, Math.min(1 - r.x, r.w + dx));
      r.h = Math.max(0.03, Math.min(1 - r.y, r.h + dy));
    } else {
      r.x = Math.max(0, Math.min(1 - r.w, r.x + dx));
      r.y = Math.max(0, Math.min(1 - r.h, r.y + dy));
    }
    state.regions[key] = r;
    placeBox(box, r);
  });
  box.addEventListener("pointerup", () => { drag = null; });
  box.addEventListener("pointercancel", () => { drag = null; });
}
async function rerenderAll() {
  const job = state.currentJob;
  if (!job || !(job.clips || []).length) { toast("No clips to re-render yet."); return; }
  const btn = $("rerenderAllBtn");
  btn.disabled = true;
  try {
    for (let i = 0; i < job.clips.length; i++) {
      await postJSON("/api/jobs/" + job.id + "/clips/" + i + "/rerender", {
        options: { facecam: state.regions.facecam, game: state.regions.game, top: parseFloat($("topSlider").value) },
      });
    }
    toast("All clips re-rendered with the new regions.", true);
    await refreshJob();
  } catch (e) { toast(e.message); }
  btn.disabled = false;
}

/* ---------------- library ---------------- */
async function loadLibrary() {
  const jobs = await api("/api/jobs");
  const list = $("libraryList");
  list.innerHTML = "";
  $("libraryEmpty").hidden = jobs.length > 0;
  for (const j of jobs) {
    const row = document.createElement("div");
    row.className = "lib-row";
    row.innerHTML = '<div class="lib-main"><div class="lib-title">' + esc(j.title || j.id) + '</div>' +
      '<div class="lib-sub">' + esc(fmtDate(j.created)) + " · " + (j.clips || 0) + " clips</div></div>" +
      '<span class="badge ' + esc(j.status) + '">' + esc(j.status) + "</span>" +
      '<button class="btn sm danger lib-del">Delete</button>';
    row.addEventListener("click", () => openJob(j.id));
    row.querySelector(".lib-del").addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!confirm("Delete this job and its clips?")) return;
      try { await api("/api/jobs/" + j.id, { method: "DELETE" }); loadLibrary(); }
      catch (e) { toast(e.message); }
    });
    list.appendChild(row);
  }
}

/* ---------------- live ---------------- */
async function startLive() {
  const url = $("liveUrl").value.trim();
  if (!url) { toast("Paste a live stream link first."); return; }
  try {
    const s = await postJSON("/api/live/start", { url: url, options: liveOptions() });
    state.liveId = s.id;
    $("liveStatusCard").hidden = false;
    $("liveStopBtn").disabled = false;
    $("clipNowBtn").disabled = false;
    $("liveStartBtn").disabled = true;
    renderLive(s);
    stopLivePolling();
    state.liveTimer = setInterval(() => {
      api("/api/live/" + state.liveId).then(renderLive).catch(() => { /* keep polling */ });
    }, 2000);
  } catch (e) { toast(e.message); }
}
function stopLivePolling() {
  if (state.liveTimer) { clearInterval(state.liveTimer); state.liveTimer = null; }
}
function renderLive(s) {
  const st = $("liveStatus");
  st.textContent = s.status;
  st.className = "badge " + s.status;
  $("liveElapsed").textContent = fmtTime(s.elapsed);
  $("hypeFill").style.width = Math.max(0, Math.min(100, s.hype || 0)) + "%";
  $("liveTitle").textContent = s.title || s.url || "";
  const err = $("liveError");
  err.hidden = !s.error;
  err.textContent = s.error || "";
  if (s.status === "stopped" || s.status === "error") {
    stopLivePolling();
    $("liveStopBtn").disabled = true;
    $("clipNowBtn").disabled = true;
    $("liveStartBtn").disabled = false;
  }
  const grid = $("liveClipGrid");
  grid.innerHTML = "";
  (s.clips || []).forEach((clip, i) => {
    const card = document.createElement("div");
    card.className = "clip-card";
    const media = (name) => "/media/live/" + s.id + "/" + encodeURIComponent(baseName(name));
    let html = "";
    if (clip.status === "done" && clip.file) {
      html += '<video controls preload="metadata" src="' + media(clip.file) + '"' + (clip.cover ? ' poster="' + media(clip.cover) + '"' : "") + "></video>";
    } else if (clip.status === "error") {
      html += '<div class="error-banner">Clip failed: ' + esc(clip.error || "") + "</div>";
    } else {
      html += '<div class="hint">Rendering…</div>';
    }
    html += '<div class="clip-top"><div class="clip-title">' + esc(clip.title || ("Live clip " + (i + 1))) + "</div></div>";
    if (clip.hook) html += '<div class="clip-hook">' + esc(clip.hook) + "</div>";
    if (clip.kind) html += '<div class="clip-reason">trigger: ' + esc(clip.kind) + "</div>";
    const dls = [["MP4", clip.file], ["SRT", clip.srt], ["VTT", clip.vtt], ["ASS", clip.ass], ["Cover", clip.cover], ["JSON", clip.meta]];
    html += '<div class="dl-row">' + dls.filter((d) => d[1]).map((d) => '<a class="btn" href="' + media(d[1]) + '" download>' + d[0] + "</a>").join("") + "</div>";
    card.innerHTML = html;
    grid.appendChild(card);
  });
}

/* ---------------- discover ---------------- */
async function discoverStreams() {
  const topic = $("discoverTopic").value.trim();
  if (!topic) { toast("Type a topic first (e.g. GTA 6)."); return; }
  const btn = $("discoverBtn");
  btn.disabled = true;
  const box = $("discoverResults");
  box.innerHTML = '<div class="hint">Searching…</div>';
  try {
    const q = "?topic=" + encodeURIComponent(topic) + "&when=" + encodeURIComponent($("discoverWhen").value) + "&count=8";
    const results = await api("/api/discover" + q);
    box.innerHTML = "";
    if (!results.length) {
      box.innerHTML = '<div class="hint">No matching long streams found. Try a wider time window.</div>';
      return;
    }
    for (const r of results) {
      const row = document.createElement("div");
      row.className = "lib-row";
      row.innerHTML = '<div class="lib-main"><div class="lib-title">' + esc(r.title || r.url) + "</div>" +
        '<div class="lib-sub">' + esc(r.channel || "") + " · " + (r.views ? Number(r.views).toLocaleString() + " views" : "") +
        (r.duration ? " · " + fmtTime(r.duration) : "") + (r.live ? " · 🔴 LIVE" : "") + " · " + esc(r.from || "") + "</div></div>" +
        '<button class="btn sm primary">Use this</button>';
      row.querySelector("button").addEventListener("click", () => {
        $("urlInput").value = r.url;
        setUploadFile(null);
        toast("Link filled in — hit Make clips.", true);
        $("urlInput").scrollIntoView({ behavior: "smooth", block: "center" });
      });
      box.appendChild(row);
    }
  } catch (e) {
    box.innerHTML = "";
    toast(e.message);
  }
  btn.disabled = false;
}

/* ---------------- manual AI answers ---------------- */
async function submitAiAnswers() {
  if (!state.currentJobId) return;
  const text = $("aiAnswers").value.trim();
  if (!text) { toast("Paste the AI's JSON answer first."); return; }
  const btn = $("aiSubmitBtn");
  btn.disabled = true;
  try {
    await postJSON("/api/jobs/" + state.currentJobId + "/ai-answers", { text: text });
    toast("Picks accepted — making clips…", true);
    startJobPolling();
    await refreshJob();
  } catch (e) { toast(e.message); }
  btn.disabled = false;
}

/* ---------------- settings ---------------- */
async function loadSettings() {
  const s = await api("/api/settings");
  const llm = (s && s.llm) || {};
  $("llmBase").value = llm.base_url || "";
  $("llmModel").value = llm.model || "";
  $("llmKey").value = "";
  $("llmKey").placeholder = llm.has_key ? "•••• (saved)" : "API key";
  try {
    const w = await api("/api/watchlist");
    $("watchlistText").value = (w && w.text) || "";
  } catch (e) { /* watchlist is optional */ }
}
async function saveWatchlist() {
  try {
    await api("/api/watchlist", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: $("watchlistText").value }),
    });
    toast("Watchlist saved.", true);
  } catch (e) { toast(e.message); }
}
async function saveSettings() {
  try {
    await api("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ llm_base_url: $("llmBase").value.trim(), llm_model: $("llmModel").value.trim(), llm_key: $("llmKey").value }),
    });
    toast("Settings saved.", true);
    loadSettings().catch(() => {});
    loadStatus().catch(() => {});
  } catch (e) { toast(e.message); }
}

/* ---------------- wiring ---------------- */
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  $("makeBtn").addEventListener("click", makeClips);
  $("urlInput").addEventListener("keydown", (e) => { if (e.key === "Enter") makeClips(); });

  const dz = $("dropzone"), fi = $("fileInput");
  dz.addEventListener("click", () => fi.click());
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fi.click(); } });
  fi.addEventListener("change", () => setUploadFile(fi.files[0] || null));
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("over");
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) setUploadFile(f);
  });

  document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
    const p = b.dataset.preset;
    state.presetFacecam = p === "none" ? null : Object.assign({}, PRESETS[p]);
    $("presetHint").textContent = p === "none" ? "auto-detect on first frame" : "preset: " + p.toUpperCase();
  }));
  document.querySelectorAll("[data-rpreset]").forEach((b) => b.addEventListener("click", () => {
    const p = b.dataset.rpreset;
    state.regions.facecam = p === "none" ? null : Object.assign({}, PRESETS[p]);
    placeBox($("boxFacecam"), state.regions.facecam);
  }));
  makeDraggable($("boxFacecam"), "facecam");
  makeDraggable($("boxGame"), "game");
  $("topSlider").addEventListener("input", () => { $("topVal").textContent = Number($("topSlider").value).toFixed(2); });
  $("rerenderAllBtn").addEventListener("click", rerenderAll);
  $("detectBtn").addEventListener("click", async () => {
    if (!state.currentJobId) return;
    try {
      const r = await postJSON("/api/jobs/" + state.currentJobId + "/facecam/detect", {});
      if (r.facecam) {
        state.regions.facecam = r.facecam;
        placeBox($("boxFacecam"), r.facecam);
        toast("Facecam found.", true);
      } else {
        toast("No facecam found on this frame.");
      }
    } catch (e) { toast(e.message); }
  });
  $("deleteJobBtn").addEventListener("click", async () => {
    if (!state.currentJobId || !confirm("Delete this job and its clips?")) return;
    try {
      await api("/api/jobs/" + state.currentJobId, { method: "DELETE" });
      state.currentJobId = null;
      showView("library");
    } catch (e) { toast(e.message); }
  });

  $("liveStartBtn").addEventListener("click", startLive);
  $("liveStopBtn").addEventListener("click", async () => {
    if (!state.liveId) return;
    try { renderLive(await postJSON("/api/live/" + state.liveId + "/stop", {})); }
    catch (e) { toast(e.message); }
  });
  $("clipNowBtn").addEventListener("click", async () => {
    if (!state.liveId) return;
    try {
      renderLive(await postJSON("/api/live/" + state.liveId + "/clip-now", { seconds: 30 }));
      toast("Clipping the last 30 seconds…", true);
    } catch (e) { toast(e.message); }
  });
  $("saveSettingsBtn").addEventListener("click", saveSettings);
  $("saveWatchlistBtn").addEventListener("click", saveWatchlist);
  $("discoverBtn").addEventListener("click", discoverStreams);
  $("discoverTopic").addEventListener("keydown", (e) => { if (e.key === "Enter") discoverStreams(); });
  $("aiSubmitBtn").addEventListener("click", submitAiAnswers);
  $("optMinScore").addEventListener("input", () => {
    const v = parseInt($("optMinScore").value, 10) || 0;
    $("minScoreVal").textContent = v === 0 ? "0 (keep all)" : String(v);
  });

  loadStatus().catch(() => {});
  loadCatalogs().catch((e) => toast("Could not load layouts/styles: " + e.message));
});
