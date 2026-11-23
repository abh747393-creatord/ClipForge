"use strict";
/* ClipForge UI — talks to the API exactly as specified in app/server.py. */

const $ = (id) => document.getElementById(id);
const state = {
  layouts: [], styles: [], anims: [],
  layout: "stack", style: "bounce",
  presetFacecam: null,
  currentJobId: null, currentJob: null,
  regions: { facecam: null, game: null },
  newRegions: { facecam: null, game: null },
  liveRegions: { facecam: null, game: null },
  editor: { jobId: null, idx: -1, clip: null, regions: { facecam: null, game: null }, words: [], previewTimer: null, previewSeq: 0, previewUrl: null },
  logoPath: null, musicPath: null,
  liveId: null, liveSig: null,
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
function captionCustom() {
  if (!$("capCustomOn").checked) return null;
  return {
    font: $("capFont").value,
    size: parseInt($("capSize").value, 10) || 120,
    base: $("capBase").value,
    active: $("capActive").value,
    strong: $("capStrong").value,
    outline: parseInt($("capOutlineW").value, 10) || 7,
    per: parseInt($("capPer").value, 10) || 3,
    upper: $("capUpper").checked,
  };
}
function camFxString() {
  const c = parseFloat($("fxContrast").value), b = parseFloat($("fxBrightness").value),
    s = parseFloat($("fxSaturation").value), cl = parseFloat($("fxClarity").value);
  if (c === 1 && b === 0 && s === 1 && cl === 0.8) return null;  // defaults = no FX
  return "eq=contrast=" + c + ":brightness=" + b + ":saturation=" + s + ",unsharp=5:5:" + cl + ":5:5:0";
}
function collectOptions() {
  const lang = $("optLanguage").value;
  const stars = $("optStars").value;
  const rangeStart = parseFloat($("optRangeStart").value);
  const rangeEnd = parseFloat($("optRangeEnd").value);
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
    facecam: state.newRegions.facecam || state.presetFacecam,
    game: state.newRegions.game,
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
    profile: $("optProfile").value || "balanced",
    exclude: $("optExclude").value.trim(),
    range_start: isNaN(rangeStart) ? null : rangeStart,
    range_end: isNaN(rangeEnd) ? null : rangeEnd,
    review_first: $("optReview").checked,
    skip_static: $("optSkipStatic").checked,
    hook_text: $("optHookText").value.trim(),
    description_text: $("optDescText").value.trim(),
    caption_custom: captionCustom(),
    hook_style: $("optHookStyle").value || "box",
    cam_fx: camFxString(),
    fade: $("optFade").checked,
    outro_text: $("optOutro").value.trim(),
    music_riser: $("optMusicRiser").checked,
    music: state.musicPath ? { path: state.musicPath, volume: parseFloat($("optMusicVol").value) || 0.15 } : null,
    watermark: {
      text: $("optWmText").value.trim(),
      image: state.logoPath,
      position: $("optWmPos").value,
      opacity: parseFloat($("optWmOpacity").value) || 0.85,
      size: parseFloat($("optWmSize").value) || 0.18,
    },
  };
}
function liveOptions() {
  const o = collectOptions();
  o.layout = $("liveLayout").value || o.layout;
  o.style = $("liveStyle").value || o.style;
  if (state.liveRegions.facecam) o.facecam = state.liveRegions.facecam;
  if (state.liveRegions.game) o.game = state.liveRegions.game;
  o.ai_every_min = parseInt($("liveEvery").value, 10) || 3;
  o.live_judge = $("liveJudge").checked;
  o.live_min_score = parseFloat($("liveMinScore").value) || 6;
  o.live_decide = $("liveDecide").value || "either";
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
  if (name === "live") { loadCreators().catch(() => {}); restoreLive().catch(() => {}); }
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
  refreshCapPreview();
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
  scheduleCapPreview();
}
function selectStyle(id) {
  state.style = id;
  document.querySelectorAll(".style-chip").forEach((c) => c.classList.toggle("on", c.dataset.style === id));
  scheduleCapPreview();
}

/* ---------------- caption preview ----------------
   One still frame from POST /api/caption-preview, rendered server-side
   through the same caption engine as a real clip. */
let capPreviewTimer = null;
let capPreviewSeq = 0;
let capPreviewUrl = null;
const ASPECT_RATIOS = { vertical: "9/16", square: "1/1", portrait: "4/5", landscape: "16/9" };
function scheduleCapPreview() {
  clearTimeout(capPreviewTimer);
  capPreviewTimer = setTimeout(refreshCapPreview, 350);
}
async function fetchPreviewBlob(body) {
  const res = await fetch("/api/caption-preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = "preview failed (" + res.status + ")";
    try { const d = await res.json(); if (d && d.error) msg = d.error; } catch (e) { /* keep default */ }
    throw new Error(msg);
  }
  return URL.createObjectURL(await res.blob());
}
async function refreshCapPreview() {
  const img = $("capPreviewImg");
  const loading = $("capPreviewLoading");
  if (!img) return;
  const seq = ++capPreviewSeq;
  const aspect = $("optAspect").value;
  const frame = img.parentElement;
  if (frame) frame.style.aspectRatio = ASPECT_RATIOS[aspect] || "9/16";
  loading.hidden = false;
  loading.textContent = "rendering preview…";
  try {
    const url = await fetchPreviewBlob({
      style: state.style,
      anim: $("optAnim").value || "highlight",
      layout: state.layout,
      aspect: aspect,
      text: $("capPreviewText").value,
      hook: $("capPreviewHook").value,
      captions: $("optCaptions").checked,
      hook_enabled: $("optHook").checked,
      caption_custom: captionCustom(),
      hook_style: $("optHookStyle").value || "box",
    });
    if (seq !== capPreviewSeq) { URL.revokeObjectURL(url); return; }  // a newer preview won
    if (capPreviewUrl) URL.revokeObjectURL(capPreviewUrl);
    capPreviewUrl = url;
    img.src = url;
    loading.hidden = true;
  } catch (e) {
    if (seq === capPreviewSeq) loading.textContent = "preview failed — " + e.message;
  }
}
function updatePresetRow() {
  $("presetRow").hidden = !needsCam(state.layout);
}

/* ---------------- new job ---------------- */
function setUploadFile(f) {
  state.uploadFile = f || null;
  $("dzFile").textContent = f ? f.name : "";
  if (f) grabUploadFrame(f);
}

/* The real frame loads after the metadata (it is a stream fetch and can
   be slow) and swaps in over the thumbnail when it arrives. */
async function loadPreviewFrame(url, img, infoEl) {
  try {
    const r = await postJSON("/api/preview-frame", { url: url });
    if (r.frame) { img.src = r.frame; img.hidden = false; }
  } catch (e) {
    /* thumbnail stays, but SAY why the real frame failed */
    if (infoEl) infoEl.textContent += " · frame failed: " + e.message;
  }
}

/* Link preview: title/duration + a REAL frame from the video (the
   thumbnail is only a fallback) so the facecam & gameplay sections can
   be drag-selected on the actual layout BEFORE making clips. */
async function previewLink() {
  const url = $("urlInput").value.trim();
  if (!url) { toast("Paste a video link first."); return; }
  const btn = $("previewBtn");
  btn.disabled = true;
  $("linkInfo").textContent = "fetching preview…";
  $("linkPreviewCard").hidden = false;
  try {
    const p = await postJSON("/api/preview", { url: url });
    const bits = [];
    if (p.title) bits.push(p.title);
    if (p.uploader) bits.push(p.uploader);
    if (p.duration) bits.push(fmtTime(p.duration));
    if (p.is_live) bits.push("🔴 LIVE");
    $("linkInfo").textContent = bits.join(" · ") || "preview ready";
    const img = $("linkThumb");
    const src = p.frame || p.thumbnail;
    if (src) { img.src = src; img.hidden = false; }
    else { img.hidden = true; }
    loadPreviewFrame(url, img, $("linkInfo"));
  } catch (e) {
    $("linkInfo").textContent = "preview failed — " + e.message + " (you can still make clips)";
  }
  btn.disabled = false;
}

/* Live preview: same real-frame preview + facecam/gameplay boxes as the
   New Job tab, so live clips use the sections picked here. */
function syncLiveBoxes() {
  const layout = $("liveLayout").value || state.layout;
  placeBox($("boxLiveFacecam"), needsCam(layout) ? state.liveRegions.facecam : null);
  placeBox($("boxLiveGame"), needsGame(layout) ? state.liveRegions.game : null);
}
async function previewLive() {
  const url = $("liveUrl").value.trim();
  if (!url) { toast("Paste a live stream link first."); return; }
  const btn = $("livePreviewBtn");
  btn.disabled = true;
  $("liveInfo").textContent = "fetching preview…";
  $("livePreviewCard").hidden = false;
  try {
    const p = await postJSON("/api/preview", { url: url });
    const bits = [];
    if (p.title) bits.push(p.title);
    if (p.uploader) bits.push(p.uploader);
    if (p.is_live) bits.push("🔴 LIVE");
    else if (p.duration) bits.push(fmtTime(p.duration));
    $("liveInfo").textContent = bits.join(" · ") || "preview ready";
    const img = $("liveThumb");
    const src = p.frame || p.thumbnail;
    if (src) { img.src = src; img.hidden = false; }
    else { img.hidden = true; }
    syncLiveBoxes();
    loadPreviewFrame(url, img, $("liveInfo"));
  } catch (e) {
    $("liveInfo").textContent = "preview failed — " + e.message + " (you can still start live clipping)";
  }
  btn.disabled = false;
}
function grabUploadFrame(file) {
  // Show a frame from the uploaded file so regions can be picked pre-job.
  const url = URL.createObjectURL(file);
  const v = document.createElement("video");
  v.muted = true;
  v.preload = "metadata";
  v.src = url;
  v.addEventListener("loadedmetadata", () => {
    v.currentTime = Math.min(5, (v.duration || 10) * 0.1);
  });
  v.addEventListener("seeked", () => {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = v.videoWidth || 960;
      canvas.height = v.videoHeight || 540;
      canvas.getContext("2d").drawImage(v, 0, 0);
      const img = $("linkThumb");
      img.src = canvas.toDataURL("image/jpeg", 0.8);
      img.hidden = false;
      $("linkPreviewCard").hidden = false;
      $("linkInfo").textContent = file.name + " · " + fmtTime(v.duration || 0) + " — drag the sections if you want";
    } catch (e) { /* frame grab is best-effort */ }
    URL.revokeObjectURL(url);
  });
  v.addEventListener("error", () => URL.revokeObjectURL(url));
}
async function uploadAsset(file, hintId, kind) {
  const hint = $(hintId);
  hint.textContent = "uploading…";
  try {
    const fd = new FormData();
    fd.append("file", file);
    const r = await api("/api/upload-asset", { method: "POST", body: fd });
    hint.textContent = "✓ " + file.name;
    if (kind === "logo") state.logoPath = r.path; else state.musicPath = r.path;
    toast((kind === "logo" ? "Logo" : "Music") + " uploaded.", true);
  } catch (e) {
    hint.textContent = "upload failed";
    toast(e.message);
  }
}

/* ---------------- profiles & caption looks ---------------- */
async function loadProfiles() {
  let profiles = [];
  try { profiles = await api("/api/profiles"); } catch (e) { return; }
  const jobs = profiles.filter((p) => !p.name.startsWith("look: "));
  const looks = profiles.filter((p) => p.name.startsWith("look: "));
  fillSelect($("jobProfile"), [["", "— none —"]].concat(jobs.map((p) => [p.name, p.name])), "");
  fillSelect($("capLooks"), [["", "— none —"]].concat(looks.map((p) => [p.name, p.name.slice(6)])), "");
  const list = $("profilesList");
  if (list) {
    list.innerHTML = "";
    for (const p of profiles) {
      const row = document.createElement("div");
      row.className = "lib-row";
      row.innerHTML = '<div class="lib-main"><div class="lib-title">' + esc(p.name) + '</div><div class="lib-sub">' + esc(fmtDate(p.created)) + "</div></div>" +
        '<button class="btn sm danger">Delete</button>';
      row.querySelector("button").addEventListener("click", async () => {
        try { await api("/api/profiles/" + encodeURIComponent(p.name), { method: "DELETE" }); loadProfiles(); }
        catch (e) { toast(e.message); }
      });
      list.appendChild(row);
    }
    if (!profiles.length) list.innerHTML = '<div class="hint">No saved profiles yet.</div>';
  }
  return profiles;
}
function applyCaptionCustom(cc) {
  $("capCustomOn").checked = !!cc;
  $("capCustomBox").hidden = !cc;
  if (!cc) return;
  if (cc.font) $("capFont").value = cc.font;
  if (cc.size) { $("capSize").value = cc.size; $("capSizeVal").textContent = cc.size; }
  if (cc.base) $("capBase").value = cc.base;
  if (cc.active) $("capActive").value = cc.active;
  if (cc.strong) $("capStrong").value = cc.strong;
  if (cc.outline != null) { $("capOutlineW").value = cc.outline; $("capOutlineVal").textContent = cc.outline; }
  if (cc.per) $("capPer").value = cc.per;
  $("capUpper").checked = cc.upper !== false;
}
function applyProfileOptions(opts) {
  if (!opts) return;
  if (opts.layout) selectLayout(opts.layout);
  if (opts.style) selectStyle(opts.style);
  if (opts.anim) $("optAnim").value = opts.anim;
  if (opts.aspect) $("optAspect").value = opts.aspect;
  if (opts.quality) $("optQuality").value = opts.quality;
  if (opts.count) $("optCount").value = opts.count;
  if (opts.min_len) $("optMinLen").value = opts.min_len;
  if (opts.max_len) $("optMaxLen").value = opts.max_len;
  $("optCaptions").checked = opts.captions !== false;
  $("optHook").checked = opts.hook_enabled !== false;
  $("optLlm").checked = opts.use_llm !== false;
  $("optHinglish").checked = !!opts.hinglish;
  $("optGameEvents").checked = opts.game_events !== false;
  $("optReview").checked = !!opts.review_first;
  $("optSkipStatic").checked = !!opts.skip_static;
  $("optFade").checked = !!opts.fade;
  $("optMusicRiser").checked = !!opts.music_riser;
  $("optTitleBar").checked = !!opts.title_bar;
  $("optKillcam").checked = !!opts.killcam;
  if (opts.focus) $("optFocus").value = opts.focus;
  if (opts.exclude) $("optExclude").value = opts.exclude;
  if (opts.hook_text) $("optHookText").value = opts.hook_text;
  if (opts.description_text) $("optDescText").value = opts.description_text;
  if (opts.outro_text) $("optOutro").value = opts.outro_text;
  if (opts.hook_style) $("optHookStyle").value = opts.hook_style;
  if (opts.profile) $("optProfile").value = opts.profile;
  if (opts.facecam) { state.newRegions.facecam = opts.facecam; placeBox($("boxNewFacecam"), opts.facecam); }
  if (opts.game) { state.newRegions.game = opts.game; placeBox($("boxNewGame"), opts.game); }
  applyCaptionCustom(opts.caption_custom || null);
  scheduleCapPreview();
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
  try { localStorage.setItem("cf_jobId", id); } catch (e) { /* private mode */ }
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
  if (["done", "error", "waiting_ai", "waiting_review", "cancelled"].includes(job.status)) stopJobPolling();
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

  // cancel button only while the job is still running
  $("cancelJobBtn").hidden = !["queued", "running", "waiting_review", "waiting_ai"].includes(job.status);

  // review-first: candidate moments to tick before rendering
  const review = job.status === "waiting_review";
  $("reviewPanel").hidden = !review;
  if (review) {
    const list = $("reviewList");
    list.innerHTML = "";
    (job.candidates || []).forEach((c, i) => {
      const row = document.createElement("label");
      row.className = "review-row";
      row.innerHTML = '<input type="checkbox" class="review-check" value="' + i + '" checked>' +
        '<span class="review-main"><b>' + esc(c.title || ("Moment " + (i + 1))) + "</b> " +
        '<span class="hint">' + fmtTime(c.start) + "–" + fmtTime(c.end) +
        (c.score != null ? " · ⚡" + Math.round(c.score) : "") + "</span>" +
        (c.hook ? '<div class="clip-hook">' + esc(c.hook) + "</div>" : "") +
        (c.why ? '<div class="hint">' + esc(c.why) + "</div>" : "") + "</span>";
      list.appendChild(row);
    });
  }

  // transcript + manual clip maker once a transcript exists
  $("transcriptCard").hidden = !(job.transcript_from && job.transcript_from !== "none" && job.status !== "running");
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
    html += '<div class="rendering-box"><span class="spin"></span>Rendering… (' + esc(clip.status || "pending") + ")</div>";
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
  html += '<div class="clip-ctl"><select class="clip-layout"></select><select class="clip-style"></select>'
    + '<span class="clip-btns"><button class="btn sm clip-preview-btn" title="Preview captions on this job\'s frame">👁 Preview</button>'
    + '<button class="btn sm clip-edit-btn">✎ Edit</button>'
    + '<button class="btn sm clip-rerender">Re-render</button></span></div>';
  html += '<div class="stars" title="Rate this clip — ClipForge learns what you like">' +
    [1, 2, 3, 4, 5].map((n) => '<button type="button" class="star' + ((clip.rating || 0) >= n ? " on" : "") + '" data-star="' + n + '">★</button>').join("") + "</div>";
  html += '<img class="clip-preview" alt="caption preview" hidden>';
  card.innerHTML = html;

  const layoutSel = card.querySelector(".clip-layout");
  const styleSel = card.querySelector(".clip-style");
  fillSelect(layoutSel, state.layouts.map((l) => [l.id, l.label || l.id]), clip.layout || (job.options && job.options.layout) || state.layout);
  fillSelect(styleSel, state.styles.map((s) => [s.id, s.label || s.id]), (job.options && job.options.style) || state.style);
  card.querySelector(".clip-edit-btn").addEventListener("click", () => openEditor(job, clip, i));
  card.querySelectorAll(".star").forEach((b) => b.addEventListener("click", async () => {
    const n = parseInt(b.dataset.star, 10);
    try {
      await postJSON("/api/jobs/" + job.id + "/clips/" + i + "/rating", { rating: n });
      clip.rating = n;
      card.querySelectorAll(".star").forEach((s) => s.classList.toggle("on", parseInt(s.dataset.star, 10) <= n));
      toast("Rated " + n + "★ — thanks, ClipForge learns from this.", true);
    } catch (e) { toast(e.message); }
  }));
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
  card.querySelector(".clip-preview-btn").addEventListener("click", async (ev) => {
    const img = card.querySelector(".clip-preview");
    if (!img.hidden) { img.hidden = true; return; }  // second click hides it
    ev.target.disabled = true;
    try {
      const url = await fetchPreviewBlob({
        job_id: job.id,
        style: styleSel.value,
        anim: (job.options && job.options.anim) || "highlight",
        layout: layoutSel.value,
        aspect: (job.options && job.options.aspect) || "vertical",
        text: $("capPreviewText").value,
        hook: clip.hook || $("capPreviewHook").value,
        captions: true,
        hook_enabled: !!(job.options && job.options.hook_enabled !== false),
        facecam: state.regions.facecam, game: state.regions.game,
        top: parseFloat($("topSlider").value),
      });
      if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
      img.dataset.url = url;
      img.src = url;
      img.hidden = false;
    } catch (e) { toast(e.message); }
    ev.target.disabled = false;
  });
  return card;
}

/* ---------------- region editor interactions ---------------- */
function makeDraggable(box, key, wrapId, store, onChange) {
  wrapId = wrapId || "regionWrap";
  store = store || state.regions;
  let drag = null;
  box.addEventListener("pointerdown", (ev) => {
    const wrapEl = $(wrapId);
    if (!wrapEl) return;
    const wrap = wrapEl.getBoundingClientRect();
    const rect = store[key];
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
    store[key] = r;
    placeBox(box, r);
    if (onChange) onChange();
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

/* ---------------- post-generation clip editor ---------------- */
function openEditor(job, clip, idx) {
  const ed = state.editor;
  ed.jobId = job.id; ed.idx = idx; ed.clip = clip;
  const eo = clip.edit_options || {};
  const opts = Object.assign({}, job.options || {}, eo);
  ed.regions = {
    facecam: clip.facecam ? Object.assign({}, clip.facecam) : (opts.facecam ? Object.assign({}, opts.facecam) : null),
    game: clip.game ? Object.assign({}, clip.game) : (opts.game ? Object.assign({}, opts.game) : null),
  };
  $("editorModal").hidden = false;
  $("edTitle").textContent = "Edit clip " + (idx + 1) + " — " + (clip.title || "");
  $("edStatus").textContent = "";
  // source frame: the clip's own moment frame (created by the preview call),
  // fall back to the job frame
  const img = $("edFrameImg");
  const name = (clip.file || "").replace(/\.[^.]+$/, "");
  img.src = "/media/" + job.id + "/" + encodeURIComponent(name + "_edit.jpg");
  img.onerror = () => { img.onerror = null; img.src = "/media/" + job.id + "/frame.jpg"; };
  img.onload = () => { placeBox($("edBoxFacecam"), ed.regions.facecam); placeBox($("edBoxGame"), ed.regions.game); };
  syncEditorRectInputs();
  fillSelect($("edLayout"), state.layouts.map((l) => [l.id, l.label || l.id]), clip.layout || opts.layout || state.layout);
  fillSelect($("edStyle"), state.styles.map((s) => [s.id, s.label || s.id]), opts.style || state.style);
  fillSelect($("edAnim"), state.anims.map((a) => [a, a]), opts.anim || "highlight");
  $("edHookStyle").value = opts.hook_style || "box";
  $("edTop").value = opts.top || 0.32;
  $("edTopVal").textContent = Number($("edTop").value).toFixed(2);
  $("edCapY").value = opts.caption_y || 1450;
  $("edCapYVal").textContent = $("edCapY").value;
  $("edClipTitle").value = clip.title || "";
  $("edHook").value = clip.hook || "";
  $("edDesc").value = clip.description || "";
  $("edTags").value = (clip.hashtags || []).join(", ");
  $("edHighlight").value = (opts.highlight_words || []).join(", ");
  $("edStart").value = clip.start != null ? Number(clip.start).toFixed(1) : "";
  $("edEnd").value = clip.end != null ? Number(clip.end).toFixed(1) : "";
  const v = $("edVideo");
  v.src = clip.file ? "/media/" + job.id + "/" + encodeURIComponent(baseName(clip.file)) : "";
  loadEditorWords();
  editorPreview();
}
function closeEditor() {
  $("editorModal").hidden = true;
  const v = $("edVideo");
  v.pause(); v.removeAttribute("src"); v.load();
}
function syncEditorRectInputs() {
  const set = (prefix, r) => {
    $(prefix + "X").value = r ? Math.round(r.x * 100) : "";
    $(prefix + "Y").value = r ? Math.round(r.y * 100) : "";
    $(prefix + "W").value = r ? Math.round(r.w * 100) : "";
    $(prefix + "H").value = r ? Math.round(r.h * 100) : "";
  };
  set("edFc", state.editor.regions.facecam);
  set("edGm", state.editor.regions.game);
}
function readEditorRectInputs() {
  const get = (prefix) => {
    const x = parseFloat($(prefix + "X").value), y = parseFloat($(prefix + "Y").value),
      w = parseFloat($(prefix + "W").value), h = parseFloat($(prefix + "H").value);
    if ([x, y, w, h].some((n) => isNaN(n))) return null;
    return { x: x / 100, y: y / 100, w: w / 100, h: h / 100 };
  };
  state.editor.regions.facecam = get("edFc");
  state.editor.regions.game = get("edGm");
  placeBox($("edBoxFacecam"), state.editor.regions.facecam);
  placeBox($("edBoxGame"), state.editor.regions.game);
}
function scheduleEditorPreview() {
  clearTimeout(state.editor.previewTimer);
  state.editor.previewTimer = setTimeout(editorPreview, 350);
}
async function editorPreview() {
  const ed = state.editor;
  if (!ed.jobId || $("editorModal").hidden) return;
  const seq = ++ed.previewSeq;
  const loading = $("edPreviewLoading");
  loading.hidden = false;
  loading.textContent = "rendering preview…";
  const job = state.currentJob || {};
  try {
    const url = await fetchPreviewBlob({
      job_id: ed.jobId,
      clip_idx: ed.idx,
      style: $("edStyle").value,
      anim: $("edAnim").value || "highlight",
      layout: $("edLayout").value,
      aspect: (job.options && job.options.aspect) || "vertical",
      text: $("capPreviewText").value,
      hook: $("edHook").value,
      captions: true,
      hook_enabled: true,
      facecam: ed.regions.facecam,
      game: ed.regions.game,
      top: parseFloat($("edTop").value),
      caption_y: parseInt($("edCapY").value, 10),
      caption_custom: (ed.clip && ed.clip.edit_options && ed.clip.edit_options.caption_custom) ||
        (job.options && job.options.caption_custom) || captionCustom(),
      hook_style: $("edHookStyle").value,
    });
    if (seq !== ed.previewSeq) { URL.revokeObjectURL(url); return; }
    if (ed.previewUrl) URL.revokeObjectURL(ed.previewUrl);
    ed.previewUrl = url;
    $("edPreviewImg").src = url;
    loading.hidden = true;
    // the preview call also wrote the clip's source frame — show it for dragging
    const name = ((ed.clip && ed.clip.file) || "").replace(/\.[^.]+$/, "");
    const img = $("edFrameImg");
    const src = "/media/" + ed.jobId + "/" + encodeURIComponent(name + "_edit.jpg") + "?v=" + seq;
    img.onerror = () => { img.onerror = null; };
    img.onload = () => { placeBox($("edBoxFacecam"), ed.regions.facecam); placeBox($("edBoxGame"), ed.regions.game); };
    img.src = src;
  } catch (e) {
    if (seq === ed.previewSeq) loading.textContent = "preview failed — " + e.message;
  }
}
async function loadEditorWords() {
  const ed = state.editor;
  const box = $("edWords");
  box.innerHTML = '<div class="hint">loading words…</div>';
  try {
    const r = await api("/api/jobs/" + ed.jobId + "/clips/" + ed.idx + "/words");
    ed.words = r.words || [];
    box.innerHTML = "";
    ed.words.forEach((w, i) => {
      const row = document.createElement("span");
      row.className = "ed-word";
      row.innerHTML = '<input type="text" value="' + esc(w.word) + '" data-i="' + i + '"><small>' + fmtTime(w.start) + "</small>";
      box.appendChild(row);
    });
    if (!ed.words.length) box.innerHTML = '<div class="hint">No caption words for this clip.</div>';
    box.querySelectorAll("input").forEach((inp) => inp.addEventListener("input", () => {
      ed.words[parseInt(inp.dataset.i, 10)].word = inp.value;
    }));
  } catch (e) {
    box.innerHTML = '<div class="hint">words unavailable: ' + esc(e.message) + "</div>";
  }
}
async function editorRerender() {
  const ed = state.editor;
  const btn = $("edRerender");
  btn.disabled = true;
  $("edStatus").textContent = "re-rendering…";
  try {
    const options = {
      _per_clip: true,
      layout: $("edLayout").value,
      style: $("edStyle").value,
      anim: $("edAnim").value,
      hook_style: $("edHookStyle").value,
      top: parseFloat($("edTop").value),
      caption_y: parseInt($("edCapY").value, 10),
      facecam: ed.regions.facecam,
      game: ed.regions.game,
      caption_custom: (ed.clip && ed.clip.edit_options && ed.clip.edit_options.caption_custom) ||
        (state.currentJob && state.currentJob.options && state.currentJob.options.caption_custom) || captionCustom(),
      highlight_words: $("edHighlight").value.split(",").map((s) => s.trim()).filter(Boolean),
      clip_fields: {
        title: $("edClipTitle").value,
        hook: $("edHook").value,
        description: $("edDesc").value,
        hashtags: $("edTags").value.split(",").map((s) => s.trim()).filter(Boolean),
      },
    };
    const start = parseFloat($("edStart").value), end = parseFloat($("edEnd").value);
    if (!isNaN(start)) options.start = start;
    if (!isNaN(end)) options.end = end;
    await postJSON("/api/jobs/" + ed.jobId + "/clips/" + ed.idx + "/rerender", { options: options });
    $("edStatus").textContent = "re-rendered ✓";
    toast("Clip re-rendered with your edits.", true);
    await refreshJob();
    const job = state.currentJob;
    if (job && job.clips && job.clips[ed.idx]) {
      ed.clip = job.clips[ed.idx];
      const v = $("edVideo");
      v.src = "/media/" + ed.jobId + "/" + encodeURIComponent(baseName(ed.clip.file)) + "?v=" + Date.now();
    }
    editorPreview();
  } catch (e) {
    $("edStatus").textContent = "failed: " + e.message;
    toast(e.message);
  }
  btn.disabled = false;
}

/* ---------------- transcript & manual clips ---------------- */
async function loadTranscript() {
  if (!state.currentJobId) return;
  const q = $("transcriptSearch").value.trim();
  const box = $("transcriptLines");
  box.innerHTML = '<div class="hint">loading…</div>';
  try {
    const r = await api("/api/jobs/" + state.currentJobId + "/transcript" + (q ? "?q=" + encodeURIComponent(q) : ""));
    box.innerHTML = "";
    (r.lines || []).forEach((ln) => {
      const div = document.createElement("div");
      div.className = "transcript-line";
      div.innerHTML = '<span class="tl-time">' + fmtTime(ln.start) + "</span> " + esc(ln.text);
      div.addEventListener("click", (ev) => {
        if (ev.shiftKey) { $("manualEnd").value = Number(ln.end).toFixed(1); }
        else { $("manualStart").value = Number(ln.start).toFixed(1); if (!$("manualEnd").value) $("manualEnd").value = Number(ln.end + 15).toFixed(1); }
        box.querySelectorAll(".transcript-line").forEach((d) => d.classList.remove("on"));
        div.classList.add("on");
      });
      box.appendChild(div);
    });
    if (!(r.lines || []).length) box.innerHTML = '<div class="hint">No lines match.</div>';
  } catch (e) { box.innerHTML = '<div class="hint">' + esc(e.message) + "</div>"; }
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
function attachLive(s) {
  // Show a session (just started, auto-started by the creator watcher, or
  // restored after a reload) and keep it updated.
  state.liveId = s.id;
  state.liveSig = null;
  try { localStorage.setItem("cf_liveId", s.id); } catch (e) { /* private mode */ }
  $("liveStatusCard").hidden = false;
  const active = ["starting", "recording"].includes(s.status);
  $("liveStopBtn").disabled = !active;
  $("clipNowBtn").disabled = !active;
  $("liveStartBtn").disabled = active;
  renderLive(s);
  stopLivePolling();
  if (active) {
    state.liveTimer = setInterval(() => {
      api("/api/live/" + state.liveId).then(renderLive).catch(() => { /* keep polling */ });
    }, 2000);
  }
}
async function startLive() {
  const url = $("liveUrl").value.trim();
  if (!url) { toast("Paste a live stream link first."); return; }
  try {
    const s = await postJSON("/api/live/start", { url: url, options: liveOptions() });
    attachLive(s);
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
  $("liveTitle").textContent = (s.title || s.url || "") + (s.rec_height ? " · recording " + s.rec_height + "p" : "");
  const err = $("liveError");
  err.hidden = !s.error;
  err.textContent = s.error || "";
  if (["stopped", "error", "ended"].includes(s.status)) {
    stopLivePolling();
    $("liveStopBtn").disabled = true;
    $("clipNowBtn").disabled = true;
    $("liveStartBtn").disabled = false;
  }
  // Rebuilding the grid on every poll recreates the <video> elements and
  // restarts playback from 0 — only rebuild when the clips actually change.
  const sig = (s.clips || []).map((c) => [c.file || "", c.status || "", c.title || ""].join("|")).join(";");
  if (sig === state.liveSig) return;
  state.liveSig = sig;
  // Preserve any playing clip across the rebuild.
  const playing = {};
  document.querySelectorAll("#liveClipGrid video").forEach((v) => {
    if (!v.paused && !v.ended) playing[v.getAttribute("src")] = v.currentTime;
  });
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
      html += '<div class="rendering-box"><span class="spin"></span>Rendering…</div>';
    }
    html += '<div class="clip-top"><div class="clip-title">' + esc(clip.title || ("Live clip " + (i + 1))) + "</div></div>";
    if (clip.hook) html += '<div class="clip-hook">' + esc(clip.hook) + "</div>";
    if (clip.kind) html += '<div class="clip-reason">trigger: ' + esc(clip.kind) + "</div>";
    if (clip.verdict) html += '<div class="clip-reason">judge: ' + esc(clip.verdict) + (clip.judge_score != null ? " (" + esc(clip.judge_score) + "/10)" : "") + "</div>";
    const dls = [["MP4", clip.file], ["SRT", clip.srt], ["VTT", clip.vtt], ["ASS", clip.ass], ["Cover", clip.cover], ["JSON", clip.meta]];
    html += '<div class="dl-row">' + dls.filter((d) => d[1]).map((d) => '<a class="btn" href="' + media(d[1]) + '" download>' + d[0] + "</a>").join("") + "</div>";
    card.innerHTML = html;
    grid.appendChild(card);
  });
  document.querySelectorAll("#liveClipGrid video").forEach((v) => {
    const t = playing[v.getAttribute("src")];
    if (t != null) { v.currentTime = t; v.play().catch(() => {}); }
  });
}

/* ---------------- creators (auto-record) ---------------- */
async function loadCreators() {
  const creators = await api("/api/creators");
  const list = $("creatorList");
  list.innerHTML = "";
  $("creatorEmpty").hidden = creators.length > 0;
  for (const c of creators) {
    const row = document.createElement("div");
    row.className = "lib-row";
    const live = c.is_live && c.session_id;
    const status = live ? "🔴 LIVE — recording" : (c.is_live ? "🔴 LIVE" : "⚪ offline");
    row.innerHTML = '<div class="lib-main"><div class="lib-title">' + esc(c.name || c.handle) + '</div>' +
      '<div class="lib-sub">' + esc(c.platform) + " · " + esc(c.handle) + " · " + status +
      (c.last_checked ? " · checked " + esc(fmtDate(c.last_checked)) : "") + "</div></div>" +
      (live ? '<button class="btn sm cr-watch">Watch</button>' : '<button class="btn sm cr-check">Check now</button>') +
      '<button class="btn sm danger cr-del">Remove</button>';
    const watch = row.querySelector(".cr-watch");
    if (watch) watch.addEventListener("click", async () => {
      try { attachLive(await api("/api/live/" + c.session_id)); }
      catch (e) { toast(e.message); }
    });
    const check = row.querySelector(".cr-check");
    if (check) check.addEventListener("click", async () => {
      check.disabled = true; check.textContent = "checking…";
      try {
        const r = await postJSON("/api/creators/" + c.id + "/check", {});
        if (r.is_live && r.session_id) { attachLive(await api("/api/live/" + r.session_id)); toast("They are live — recording started.", true); }
        else toast("Not live right now.");
      } catch (e) { toast(e.message); }
      loadCreators().catch(() => {});
    });
    row.querySelector(".cr-del").addEventListener("click", async () => {
      if (!confirm("Remove " + (c.name || c.handle) + " from creators?")) return;
      try { await api("/api/creators/" + c.id, { method: "DELETE" }); loadCreators(); }
      catch (e) { toast(e.message); }
    });
    list.appendChild(row);
  }
}
async function addCreator() {
  const handle = $("creatorHandle").value.trim();
  if (!handle) { toast("Type a creator handle or channel link first."); return; }
  try {
    const c = await postJSON("/api/creators", { platform: $("creatorPlatform").value, handle: handle });
    $("creatorHandle").value = "";
    toast("Creator saved: " + (c.name || c.handle), true);
    loadCreators().catch(() => {});
    // check right away so a live creator starts recording immediately
    const r = await postJSON("/api/creators/" + c.id + "/check", {}).catch(() => null);
    if (r && r.is_live && r.session_id) { attachLive(await api("/api/live/" + r.session_id)); toast("They are live — recording started.", true); }
    loadCreators().catch(() => {});
  } catch (e) { toast(e.message); }
}

/* ---------------- restore after reload ---------------- */
async function restoreLive() {
  const sessions = await api("/api/live");
  const active = sessions.find((s) => ["starting", "recording"].includes(s.status));
  if (active) {
    if (state.liveId !== active.id || !state.liveTimer) attachLive(active);
  } else if (!state.liveId) {
    let stored = "";
    try { stored = localStorage.getItem("cf_liveId") || ""; } catch (e) { /* private mode */ }
    const past = sessions.find((s) => s.id === stored);
    if (past) attachLive(past);
  }
  renderPastLive(sessions.filter((s) => !["starting", "recording"].includes(s.status)));
}
function renderPastLive(past) {
  const card = $("pastLiveCard");
  const list = $("pastLiveList");
  card.hidden = past.length === 0;
  list.innerHTML = "";
  for (const s of past.slice(0, 10)) {
    const row = document.createElement("div");
    row.className = "lib-row";
    row.innerHTML = '<div class="lib-main"><div class="lib-title">' + esc(s.title || s.url || s.id) + '</div>' +
      '<div class="lib-sub">' + esc(fmtDate(s.started_at)) + " · " + (s.clips || []).length + " clips</div></div>" +
      '<span class="badge ' + esc(s.status) + '">' + esc(s.status) + "</span>";
    row.addEventListener("click", () => attachLive(s));
    list.appendChild(row);
  }
}
async function restoreSession() {
  // Wherever you left off, it resumes from there: live session + open job.
  await restoreLive().catch(() => {});
  let jobId = "";
  try { jobId = localStorage.getItem("cf_jobId") || ""; } catch (e) { /* private mode */ }
  if (jobId) openJob(jobId);
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
  $("discordWebhook").value = s.discord_webhook || "";
  $("telegramToken").value = "";
  $("telegramToken").placeholder = s.telegram_token_set ? "•••• (saved)" : "bot token";
  $("telegramChat").value = s.telegram_chat_id || "";
  $("phonePin").value = "";
  $("phonePin").placeholder = s.phone_pin_set ? "•••• (saved — blank keeps it)" : "4–8 digits";
  $("autoRecord").checked = !!s.auto_record;
  try {
    const providers = await api("/api/llm/providers");
    fillSelect($("llmProvider"), (providers || []).map((p) => [p.id, p.label]), "");
    const match = (providers || []).find((p) => p.base_url && llm.base_url && llm.base_url.startsWith(p.base_url));
    if (match) $("llmProvider").value = match.id;
    state.providers = providers;
  } catch (e) { /* providers are optional */ }
  try {
    const w = await api("/api/watchlist");
    $("watchlistText").value = (w && w.text) || "";
  } catch (e) { /* watchlist is optional */ }
  loadReadiness().catch(() => {});
  loadProfiles().catch(() => {});
}
async function loadReadiness() {
  const r = await api("/api/readiness");
  const list = $("readinessList");
  list.innerHTML = (r.checks || []).map((c) =>
    '<div class="readiness-row"><span class="dot ' + (c.ok ? "ok" : "bad") + '"></span><b>' + esc(c.name) + "</b><span>" + esc(c.detail || "") + "</span></div>"
  ).join("");
  const st = await api("/api/storage");
  $("storageInfo").textContent = "Storage: " + (st.human || "") + " used by " + (st.jobs || 0) + " jobs · " + (st.disk_free_human || "") + " free on disk";
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
async function saveAlerts() {
  try {
    const body = {
      discord_webhook: $("discordWebhook").value.trim(),
      telegram_chat_id: $("telegramChat").value.trim(),
      auto_record: $("autoRecord").checked,
    };
    if ($("telegramToken").value) body.telegram_token = $("telegramToken").value;
    if ($("phonePin").value) body.phone_pin = $("phonePin").value.trim();
    await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast("Alerts saved.", true);
    loadSettings().catch(() => {});
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
  $("creatorAddBtn").addEventListener("click", addCreator);
  $("creatorHandle").addEventListener("keydown", (e) => { if (e.key === "Enter") addCreator(); });
  $("liveStopBtn").addEventListener("click", async () => {
    if (!state.liveId) return;
    try { renderLive(await postJSON("/api/live/" + state.liveId + "/stop", {})); }
    catch (e) { toast(e.message); }
  });
  $("clipNowBtn").addEventListener("click", async () => {
    if (!state.liveId) return;
    const secs = parseInt($("clipNowSec").value, 10) || 30;
    try {
      renderLive(await postJSON("/api/live/" + state.liveId + "/clip-now", { seconds: secs }));
      toast("Clipping the last " + secs + " seconds…", true);
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
  // Caption preview follows every knob that changes how captions look.
  $("optAnim").addEventListener("change", scheduleCapPreview);
  $("optAspect").addEventListener("change", scheduleCapPreview);
  $("optCaptions").addEventListener("change", scheduleCapPreview);
  $("optHook").addEventListener("change", scheduleCapPreview);
  $("capPreviewText").addEventListener("input", scheduleCapPreview);
  $("capPreviewHook").addEventListener("input", scheduleCapPreview);

  /* ---- new: link preview + pre-job regions ---- */
  $("previewBtn").addEventListener("click", previewLink);
  makeDraggable($("boxNewFacecam"), "facecam", "newRegionWrap", state.newRegions);
  makeDraggable($("boxNewGame"), "game", "newRegionWrap", state.newRegions);
  document.querySelectorAll("[data-npreset]").forEach((b) => b.addEventListener("click", () => {
    const p = b.dataset.npreset;
    state.newRegions.facecam = p === "none" ? null : Object.assign({}, PRESETS[p]);
    placeBox($("boxNewFacecam"), state.newRegions.facecam);
  }));
  $("newGameFullBtn").addEventListener("click", () => {
    state.newRegions.game = { x: 0, y: 0, w: 1, h: 1 };
    placeBox($("boxNewGame"), state.newRegions.game);
  });

  /* ---- live: preview + regions ---- */
  $("livePreviewBtn").addEventListener("click", previewLive);
  makeDraggable($("boxLiveFacecam"), "facecam", "liveRegionWrap", state.liveRegions);
  makeDraggable($("boxLiveGame"), "game", "liveRegionWrap", state.liveRegions);
  document.querySelectorAll("[data-lpreset]").forEach((b) => b.addEventListener("click", () => {
    const p = b.dataset.lpreset;
    state.liveRegions.facecam = p === "none" ? null : Object.assign({}, PRESETS[p]);
    syncLiveBoxes();
  }));
  $("liveGameFullBtn").addEventListener("click", () => {
    state.liveRegions.game = { x: 0, y: 0, w: 1, h: 1 };
    syncLiveBoxes();
  });
  $("liveLayout").addEventListener("change", syncLiveBoxes);

  /* ---- new: custom caption look ---- */
  $("capCustomOn").addEventListener("change", () => {
    $("capCustomBox").hidden = !$("capCustomOn").checked;
    scheduleCapPreview();
  });
  ["capFont", "capPer", "capUpper"].forEach((id) => $(id).addEventListener("change", scheduleCapPreview));
  ["capSize", "capBase", "capActive", "capStrong", "capOutlineW"].forEach((id) => $(id).addEventListener("input", scheduleCapPreview));
  $("capSize").addEventListener("input", () => { $("capSizeVal").textContent = $("capSize").value; });
  $("capOutlineW").addEventListener("input", () => { $("capOutlineVal").textContent = $("capOutlineW").value; });
  $("optHookStyle").addEventListener("change", scheduleCapPreview);
  $("capSaveLook").addEventListener("click", async () => {
    const name = prompt("Name this caption look:", "My look");
    if (!name) return;
    try {
      await postJSON("/api/profiles", {
        name: "look: " + name,
        options: { style: state.style, anim: $("optAnim").value, caption_custom: captionCustom(), hook_style: $("optHookStyle").value },
      });
      toast("Caption look saved.", true);
      loadProfiles();
    } catch (e) { toast(e.message); }
  });
  $("capLooks").addEventListener("change", async () => {
    const name = $("capLooks").value;
    if (!name) return;
    try {
      const profiles = await api("/api/profiles");
      const p = (profiles || []).find((x) => x.name === name);
      if (p) {
        if (p.options.style) selectStyle(p.options.style);
        if (p.options.anim) $("optAnim").value = p.options.anim;
        if (p.options.hook_style) $("optHookStyle").value = p.options.hook_style;
        applyCaptionCustom(p.options.caption_custom || null);
        scheduleCapPreview();
      }
    } catch (e) { toast(e.message); }
  });

  /* ---- new: assets, profiles ---- */
  $("optLogoFile").addEventListener("change", () => { const f = $("optLogoFile").files[0]; if (f) uploadAsset(f, "logoHint", "logo"); });
  $("optMusicFile").addEventListener("change", () => { const f = $("optMusicFile").files[0]; if (f) uploadAsset(f, "musicHint", "music"); });
  $("optWmOpacity").addEventListener("input", () => { $("wmOpacityVal").textContent = Number($("optWmOpacity").value).toFixed(2); });
  $("optWmSize").addEventListener("input", () => { $("wmSizeVal").textContent = Math.round(parseFloat($("optWmSize").value) * 100) + "%"; });
  $("saveProfileBtn").addEventListener("click", async () => {
    const name = prompt("Profile name:", "My profile");
    if (!name) return;
    try {
      await postJSON("/api/profiles", { name: name, options: collectOptions() });
      toast("Profile saved.", true);
      loadProfiles();
    } catch (e) { toast(e.message); }
  });
  $("jobProfile").addEventListener("change", async () => {
    const name = $("jobProfile").value;
    if (!name) return;
    try {
      const profiles = await api("/api/profiles");
      const p = (profiles || []).find((x) => x.name === name);
      if (p) { applyProfileOptions(p.options); toast("Profile applied.", true); }
    } catch (e) { toast(e.message); }
  });

  /* ---- new: job actions (cancel, review, transcript, manual clip) ---- */
  $("cancelJobBtn").addEventListener("click", async () => {
    if (!state.currentJobId || !confirm("Cancel this job?")) return;
    try { await postJSON("/api/jobs/" + state.currentJobId + "/cancel", {}); toast("Job cancelled."); refreshJob(); }
    catch (e) { toast(e.message); }
  });
  $("reviewRenderBtn").addEventListener("click", async () => {
    const picked = Array.from(document.querySelectorAll(".review-check:checked")).map((c) => parseInt(c.value, 10));
    if (!picked.length) { toast("Tick at least one moment."); return; }
    try {
      await postJSON("/api/jobs/" + state.currentJobId + "/review-select", { indices: picked });
      toast("Rendering your picks…", true);
      startJobPolling();
      refreshJob();
    } catch (e) { toast(e.message); }
  });
  $("transcriptLoadBtn").addEventListener("click", loadTranscript);
  $("transcriptSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") loadTranscript(); });
  $("manualClipBtn").addEventListener("click", async () => {
    const start = parseFloat($("manualStart").value), end = parseFloat($("manualEnd").value);
    if (isNaN(start) || isNaN(end) || end <= start) { toast("Set a valid start and end first."); return; }
    try {
      await postJSON("/api/jobs/" + state.currentJobId + "/manual-clip", { start: start, end: end, title: $("manualTitle").value.trim() });
      toast("Manual clip rendered.", true);
      refreshJob();
    } catch (e) { toast(e.message); }
  });

  /* ---- new: clip editor ---- */
  makeDraggable($("edBoxFacecam"), "facecam", "edRegionWrap", state.editor.regions, () => { syncEditorRectInputs(); scheduleEditorPreview(); });
  makeDraggable($("edBoxGame"), "game", "edRegionWrap", state.editor.regions, () => { syncEditorRectInputs(); scheduleEditorPreview(); });
  document.querySelectorAll("[data-epreset]").forEach((b) => b.addEventListener("click", () => {
    const p = b.dataset.epreset;
    state.editor.regions.facecam = p === "none" ? null : Object.assign({}, PRESETS[p]);
    placeBox($("edBoxFacecam"), state.editor.regions.facecam);
    syncEditorRectInputs(); scheduleEditorPreview();
  }));
  $("edGameFullBtn").addEventListener("click", () => {
    state.editor.regions.game = { x: 0, y: 0, w: 1, h: 1 };
    placeBox($("edBoxGame"), state.editor.regions.game);
    syncEditorRectInputs(); scheduleEditorPreview();
  });
  ["edFcX", "edFcY", "edFcW", "edFcH", "edGmX", "edGmY", "edGmW", "edGmH"].forEach((id) =>
    $(id).addEventListener("change", () => { readEditorRectInputs(); scheduleEditorPreview(); }));
  ["edLayout", "edStyle", "edAnim", "edHookStyle"].forEach((id) => $(id).addEventListener("change", scheduleEditorPreview));
  $("edTop").addEventListener("input", () => { $("edTopVal").textContent = Number($("edTop").value).toFixed(2); scheduleEditorPreview(); });
  $("edCapY").addEventListener("input", () => { $("edCapYVal").textContent = $("edCapY").value; scheduleEditorPreview(); });
  $("edHook").addEventListener("input", scheduleEditorPreview);
  $("edSafeToggle").addEventListener("change", () => { $("edSafeZone").hidden = !$("edSafeToggle").checked; });
  $("edClose").addEventListener("click", closeEditor);
  $("editorModal").addEventListener("click", (ev) => { if (ev.target === $("editorModal")) closeEditor(); });
  $("edRerender").addEventListener("click", editorRerender);
  $("edSetStart").addEventListener("click", () => {
    const ed = state.editor, v = $("edVideo");
    if (ed.clip && ed.clip.start != null) $("edStart").value = (Number(ed.clip.start) + v.currentTime).toFixed(1);
  });
  $("edSetEnd").addEventListener("click", () => {
    const ed = state.editor, v = $("edVideo");
    if (ed.clip && ed.clip.start != null) $("edEnd").value = (Number(ed.clip.start) + v.currentTime).toFixed(1);
  });
  $("edSaveText").addEventListener("click", async () => {
    const ed = state.editor;
    try {
      await api("/api/jobs/" + ed.jobId + "/clips/" + ed.idx + "/text", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: $("edClipTitle").value, hook: $("edHook").value, description: $("edDesc").value, hashtags: $("edTags").value.split(",").map((s) => s.trim()).filter(Boolean) }),
      });
      toast("Text saved.", true);
      refreshJob();
    } catch (e) { toast(e.message); }
  });
  $("edWrite").addEventListener("click", async () => {
    const ed = state.editor;
    try {
      const r = await postJSON("/api/jobs/" + ed.jobId + "/clips/" + ed.idx + "/write", {});
      if (r.title) $("edClipTitle").value = r.title;
      if (r.hook) $("edHook").value = r.hook;
      if (r.description) $("edDesc").value = r.description;
      if (r.hashtags) $("edTags").value = (r.hashtags || []).join(", ");
      toast("AI text ready — check it, then Save text or Re-render.", true);
      scheduleEditorPreview();
    } catch (e) { toast(e.message); }
  });
  $("edSaveWords").addEventListener("click", async () => {
    const ed = state.editor;
    try {
      await postJSON("/api/jobs/" + ed.jobId + "/clips/" + ed.idx + "/words", { words: ed.words });
      toast("Words saved — Re-render to burn them in.", true);
    } catch (e) { toast(e.message); }
  });
  $("edResetWords").addEventListener("click", async () => {
    const ed = state.editor;
    try {
      await postJSON("/api/jobs/" + ed.jobId + "/clips/" + ed.idx + "/words", { words: [] });
      toast("Words reset to the transcript.", true);
      loadEditorWords();
    } catch (e) { toast(e.message); }
  });
  $("edImportSubs").addEventListener("click", async () => {
    const ed = state.editor;
    const text = $("edSubs").value.trim();
    if (!text) { toast("Paste subtitle text first."); return; }
    try {
      const r = await postJSON("/api/jobs/" + ed.jobId + "/clips/" + ed.idx + "/subtitles", { text: text });
      toast("Imported " + r.words + " words — Re-render to burn them in.", true);
      loadEditorWords();
    } catch (e) { toast(e.message); }
  });

  /* ---- new: settings extras ---- */
  $("saveAlertsBtn").addEventListener("click", saveAlerts);
  $("llmProvider").addEventListener("change", () => {
    const p = (state.providers || []).find((x) => x.id === $("llmProvider").value);
    if (p && p.base_url) $("llmBase").value = p.base_url;
    if (p && p.model_hint && !$("llmModel").value) $("llmModel").value = p.model_hint;
  });
  $("llmModelsBtn").addEventListener("click", async () => {
    $("llmTestOut").textContent = "asking the endpoint for models…";
    try {
      const r = await postJSON("/api/llm/models", { base_url: $("llmBase").value.trim(), key: $("llmKey").value });
      if (r.ok && r.models && r.models.length) {
        const sel = document.createElement("select");
        sel.id = "llmModelSel";
        r.models.forEach((m) => { const o = document.createElement("option"); o.value = m; o.textContent = m; sel.appendChild(o); });
        sel.value = $("llmModel").value || r.models[0];
        $("llmModel").value = sel.value;
        sel.addEventListener("change", () => { $("llmModel").value = sel.value; });
        const old = $("llmModelSel"); if (old) old.replaceWith(sel); else $("llmTestOut").after(sel);
        $("llmTestOut").textContent = r.models.length + " model(s) found — pick one, then Save settings.";
      } else {
        $("llmTestOut").textContent = "no models: " + (r.error || "endpoint returned none");
      }
    } catch (e) { $("llmTestOut").textContent = e.message; }
  });
  $("llmTestBtn").addEventListener("click", async () => {
    $("llmTestOut").textContent = "testing…";
    try {
      const r = await postJSON("/api/llm/test", { base_url: $("llmBase").value.trim(), model: $("llmModel").value.trim(), key: $("llmKey").value });
      $("llmTestOut").textContent = r.ok ? "✓ works — replied: " + (r.reply || "").slice(0, 80) : "✗ " + (r.error || "failed");
    } catch (e) { $("llmTestOut").textContent = e.message; }
  });
  $("readinessBtn").addEventListener("click", () => loadReadiness().catch((e) => toast(e.message)));
  $("updateBtn").addEventListener("click", async () => {
    $("updateOut").textContent = "checking…";
    try {
      const r = await api("/api/update-check");
      $("updateOut").textContent = r.update_available ? "update available: v" + r.latest + " (you have v" + r.local + ")" :
        (r.error ? "couldn't check (" + r.error + ")" : "up to date (v" + r.local + ")");
    } catch (e) { $("updateOut").textContent = e.message; }
  });

  loadStatus().catch(() => {});
  loadCatalogs().catch((e) => toast("Could not load layouts/styles: " + e.message));
  loadProfiles().catch(() => {});
  restoreSession().catch(() => {});
  // Keep creator live-status fresh while the app is open.
  setInterval(() => { if (!$("view-live").hidden) loadCreators().catch(() => {}); }, 20000);
});
