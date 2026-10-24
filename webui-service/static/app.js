/* fnmusic-ext WebUI — 原生 JS，无框架无外部资产。 */
"use strict";

// 飞牛桌面用 HTTPS 打开管理窗，页面必须挂在同源路径 /app/fnmusic-ext 下。
// WebUI 自己也会剥掉这个前缀。管理接口只认飞牛网关注入的管理员头。
const APP_BASE = "/app/fnmusic-ext";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const PROVIDER_LABEL = { musicdl: "musicdl 聚合音源", musicbox: "网易云音乐盒子", lxmusic: "洛雪自定义源", none: "未配置" };
const PROC_LABEL = { musicdl: "musicdl", musicbox: "musicbox", lxmusic: "lxmusic", webui: "WebUI", xiaomusic: "小爱音箱" };

let configValues = {};   // GET /api/config 的 values
let platforms = { enabled: [], registered: [] };
let dirty = false;
let qrTimer = null;
let lxVerifiedUrl = null; // 已通过测试的 lx URL（保存时免二次校验提示用）

async function api(path, options) {
  const resp = await fetch(APP_BASE + path, options);
  let body = {};
  try { body = await resp.json(); } catch (_) { /* 非 JSON */ }
  if (!resp.ok) throw new Error(body.error || body.detail || `HTTP ${resp.status}`);
  return body;
}

function toast(message, kind) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast " + (kind || "");
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 3800);
}

function markDirty(note) {
  dirty = true;
  const bar = $("#save-bar");
  if (bar) bar.classList.add("show");
  const noteEl = $("#save-note");
  if (noteEl) noteEl.textContent = note || "有未保存的修改";
}

function clearDirty() {
  dirty = false;
  const bar = $("#save-bar");
  if (bar) bar.classList.remove("show");
  const noteEl = $("#save-note");
  if (noteEl) noteEl.textContent = "";
}

/* -------------------------------------------------------------- 导航 */
function switchPage(page) {
  $$(".page").forEach((el) => el.classList.toggle("active", el.id === "page-" + page));
  $$("[data-page]").forEach((el) => el.classList.toggle("active", el.dataset.page === page));
  if (page === "about") loadAbout();
  if (page === "xiaomusic") loadXmPage();
  else stopXmPolling();
}
$$("[data-page]").forEach((btn) => btn.addEventListener("click", () => switchPage(btn.dataset.page)));

/* -------------------------------------------------------------- 主题 / 外观
   默认「跟随系统」：飞牛 Web 宿主里以宿主主题为准（getPlatformConfig 取初值，
   $on('os/theme') 监听后续切换，见 fnOS「页面交互」文档）；宿主不可用或独立浏览器
   退回 prefers-color-scheme。用户选择只存当前浏览器，不进 .env，也不参与保存条。
   首屏由 theme-boot.js（样式表之前的同步脚本）引导，避免深色用户先闪白底。 */
const THEME_KEY = "fnmusic-theme";
const HOST_THEME_KEY = "fnmusic-host-theme";
const themeMedia = (typeof window !== "undefined" && typeof window.matchMedia === "function")
  ? window.matchMedia("(prefers-color-scheme: dark)")
  : null;
let themePref = "auto";  // auto | light | dark —— 「设置 → 外观」里的选择
let hostTheme = null;    // 飞牛宿主报告的主题：light | dark（无宿主时为 null）

function normalizeTheme(value) {
  return value === "dark" || value === "light" ? value : null;
}

function readThemePref() {
  try {
    const saved = window.localStorage.getItem(THEME_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch (_) { /* 隐私模式/直连没有 localStorage：按跟随系统处理 */ }
  return "auto";
}

function updateHostTheme(value) {
  hostTheme = normalizeTheme(value);
  try {
    if (hostTheme) window.localStorage.setItem(HOST_THEME_KEY, hostTheme);
    else window.localStorage.removeItem(HOST_THEME_KEY);
  } catch (_) { /* 宿主主题仍可在本次页面生效 */ }
}

function effectiveTheme() {
  if (themePref !== "auto") return themePref;
  if (hostTheme) return hostTheme;
  return themeMedia && themeMedia.matches ? "dark" : "light";
}

function applyTheme() {
  const theme = effectiveTheme();
  // 与飞牛文档示例一致：主题写到 <html data-theme>，CSS 按此覆盖配色变量
  if (typeof document !== "undefined" && document.documentElement && document.documentElement.dataset) {
    document.documentElement.dataset.theme = theme;
  }
  $$("input[name=theme]").forEach((el) => { el.checked = el.value === themePref; });
}

function setThemePref(pref) {
  themePref = normalizeTheme(pref) || "auto";
  try {
    if (themePref === "auto") window.localStorage.removeItem(THEME_KEY);
    else window.localStorage.setItem(THEME_KEY, themePref);
  } catch (_) { /* 存不了就只对本次会话生效 */ }
  applyTheme();
}

async function initTheme() {
  themePref = readThemePref();
  const pathname = (typeof window !== "undefined" && window.location && window.location.pathname) || "";
  const embedded = pathname.startsWith("/app/");
  hostTheme = null;
  if (embedded) {
    try { hostTheme = normalizeTheme(window.localStorage.getItem(HOST_THEME_KEY)); } catch (_) {}
  }
  applyTheme();
  $$("input[name=theme]").forEach((el) => el.addEventListener("change", () => {
    if (el.checked) setThemePref(el.value);
  }));
  // 独立浏览器 / 设备级深浅色变化（宿主主题优先，变化由下面的 $on 刷新）
  if (themeMedia && typeof themeMedia.addEventListener === "function") {
    themeMedia.addEventListener("change", () => { if (themePref === "auto" && !hostTheme) applyTheme(); });
  }
  if (!embedded) return;  // 直连 8774：没有飞牛宿主，不碰 SDK
  const sdk = await lxLoadTrimSdk();  // 复用 NAS 文件选择的 SDK 单例（同语义）
  if (!sdk) {
    updateHostTheme(null);
    applyTheme();
    return;
  }
  try {
    // 移动端 App 内嵌页也能取到主题；独立浏览器没有宿主桥，抛错后走媒体查询
    const config = await sdk.getPlatformConfig();
    updateHostTheme(config && (config.theme || (config.os && config.os.theme)));
  } catch (_) {
    updateHostTheme(null);
  }
  applyTheme();
  if (!sdk.isWeb || sdk.isStandaloneWeb) return;  // 只有 Web 宿主支持 $on 主题事件
  try {
    if (typeof sdk.$on === "function") {
      // 仅 Web 宿主支持；移动端 App 内嵌页与独立浏览器没有该事件
      await sdk.$on("os/theme", (theme) => {
        updateHostTheme(theme);
        applyTheme();
      });
    }
  } catch (_) { /* 宿主不支持事件推送时退回媒体查询 */ }
}

/* -------------------------------------------------------------- 概览 */
async function loadStatus() {
  try {
    const st = await api("/api/status");
    $("#brand-version").textContent = `v${st.version}`;
    $("#sidebar-foot").textContent = `${PROVIDER_LABEL[st.current_provider] || st.current_provider}`;
    $("#ov-provider-body").innerHTML =
      `<span class="state-line"><span class="dot ok"></span>${PROVIDER_LABEL[st.current_provider] || st.current_provider}</span>`;
    $("#ov-processes").innerHTML = Object.entries(st.processes).map(([name, p]) => {
      const ok = p.state === "RUNNING";
      const cls = ok ? "ok" : p.state === "FATAL" ? "err" : "";
      return `<span class="chip"><span class="dot ${cls}"></span>${PROC_LABEL[name] || name} · ${p.state}</span>`;
    }).join("");
    $("#ov-services").innerHTML = Object.entries(st.services).map(([name, s]) => {
      if (!s.reachable && s.note) return `<span class="state-line"><span class="dot"></span>${PROC_LABEL[name]}：${s.note}</span>`;
      return `<span class="state-line"><span class="dot ${s.reachable ? "ok" : "err"}"></span>${PROC_LABEL[name]}：${s.reachable ? "正常" : "不可达"}</span>`;
    }).join("");
    // 小爱音箱登录失效 / 轮询降频等异常状态：进入小爱音箱页后由完成页健康检查展示
    const lxCard = $("#ov-lx-card");
    if (st.current_provider === "lxmusic" && st.lx_source) {
      lxCard.hidden = false;
      const s = st.lx_source.source;
      const rows = [];
      rows.push(`<div class="kv"><b>状态</b>${st.lx_source.initialized ? "已加载" : "未加载"}</div>`);
      if (s) {
        rows.push(`<div class="kv"><b>源名称</b>${s.name || "-"} ${s.version ? "v" + s.version : ""}</div>`);
        rows.push(`<div class="kv"><b>平台</b>${Object.keys(s.platforms || {}).join("、") || "-"}</div>`);
        rows.push(`<div class="kv"><b>运行</b>${s.running ? "是" : "否"}</div>`);
      }
      if (st.lx_source.last_error) rows.push(`<div class="kv"><b>错误</b>${st.lx_source.last_error}</div>`);
      $("#ov-lx").innerHTML = rows.join("");
    } else {
      lxCard.hidden = true;
    }
  } catch (exc) {
    $("#ov-provider-body").textContent = "状态加载失败：" + exc.message;
  }
}

/* -------------------------------------------------------------- 配置读写 */
async function loadConfig() {
  const cfg = await api("/api/config");
  configValues = cfg.values;
  applyConfigToForm();
  clearDirty();
}

function applyConfigToForm() {
  const v = configValues;
  const provider = v.FNMUSIC_NETEASE_ENABLED === "true" ? "musicbox"
    : v.FNMUSIC_MUSICDL_ENABLED === "true" ? "musicdl"
    : v.FNMUSIC_LX_ENABLED === "true" ? "lxmusic" : "";
  $$("input[name=provider]").forEach((el) => { el.checked = el.value === provider; });
  syncProviderPanels(provider);
  syncNeteaseAccount(); // 扫码卡片常显：任意音源下都同步网易登录态
  const quality = v.FNMUSIC_QUALITY_MODE || "high";
  $$("input[name=quality]").forEach((el) => { el.checked = el.value === quality; });
  $("#recommend-hot").checked = v.FNMUSIC_RECOMMEND_HOT === "true";
  $("#recommend-daily").checked = v.FNMUSIC_RECOMMEND_DAILY === "true";
  $("#history-playlist").checked = v.FNMUSIC_HISTORY_PLAYLIST_ENABLED !== "false";
  // 公共歌单：选择串只存 {provider,source,kind,id}，_name/_cover_url 为展示用临时字段
  $("#public-playlists-enabled").checked = v.FNMUSIC_PUBLIC_PLAYLISTS_ENABLED === "true";
  $("#xm-enabled").checked = v.FNMUSIC_XIAOMUSIC_ENABLED === "true";
  try {
    const rows = JSON.parse(v.FNMUSIC_PUBLIC_PLAYLISTS_SELECTION || "[]");
    publicSelection = Array.isArray(rows)
      ? rows.filter((it) => it && it.provider === "musicbox" && it.id).map((it) => ({
          provider: "musicbox", source: it.source || "netease",
          kind: it.kind === "chart" ? "chart" : "playlist", id: String(it.id),
        }))
      : [];
  } catch (_) { publicSelection = []; }
  publicSelectionTouched = false;
  $("#public-playlists-browser").hidden = !$("#public-playlists-enabled").checked;
  renderPublicSelected();
  $("#tee-enabled").checked = v.FNMUSIC_TEE_SAVE_ENABLED === "true";
  $("#fav-download-enabled").checked = v.FNMUSIC_FAVORITE_DOWNLOAD_ENABLED !== "false";
  $("#fav-download-quality").value = ["any", "320", "lossless"].includes(v.FNMUSIC_DOWNLOAD_MIN_QUALITY) ? v.FNMUSIC_DOWNLOAD_MIN_QUALITY : "any";
  $("#direct-token").value = v.FNMUSIC_DIRECT_TOKEN || "";
  $("#auto-cover").checked = v.FNMUSIC_AUTO_COVER !== "false";
  $("#lyric-auto-dl").checked = v.FNMUSIC_LYRIC_AUTO_DL === "true";
  $("#tee-dir").value = v.FNMUSIC_TEE_SAVE_DIR || "";
  $("#tee-max").value = v.FNMUSIC_TEE_CACHE_MAX || "2";
  $("#bind-timeout").value = v.FNMUSIC_OFFICIAL_BIND_TIMEOUT_S || "120";
  $("#handoff-max").value = v.FNMUSIC_TEE_HANDOFF_MAX != null ? v.FNMUSIC_TEE_HANDOFF_MAX : "3";
  $("#scan-path").value = v.FNMUSIC_LIBRARY_SCAN_PATH || "";
  updateTeeCountLabel();
  updateBindTimeoutLabel();
  $("#llm-base").value = v.FNMUSIC_LLM_BASE_URL || "";
  $("#llm-key").value = v.FNMUSIC_LLM_API_KEY || "";
  $("#llm-model").value = v.FNMUSIC_LLM_MODEL || "";
  $("#search-timeout").value = v.FNMUSIC_SEARCH_TIMEOUT || "15";
  $("#search-probe").checked = v.FNMUSIC_SEARCH_PROBE === "true";
  $("#app-v2-search").checked = v.FNMUSIC_APP_V2_SEARCH !== "false";
  $("#search-log-query").checked = v.FNMUSIC_SEARCH_LOG_QUERY !== "false";
  $("#netease-my-playlists").checked = v.FNMUSIC_NETEASE_MY_PLAYLISTS === "true";
  $("#netease-subscribed").checked = v.FNMUSIC_NETEASE_SUBSCRIBED !== "false";
  // 三态常驻开关：未配置（老配置）时按「是否有需求」回填——账号歌单或公共歌单都算需求
  const residentRaw = (v.FNMUSIC_NETEASE_PLAYLIST_RESIDENT || "").trim();
  const residentBox = $("#netease-resident");
  const residentWanted = $("#netease-my-playlists").checked
    || ($("#public-playlists-enabled").checked && publicMusicboxSelected());
  residentBox.checked = residentRaw === "true" || (residentRaw === "" && residentWanted);
  updateResidentAvailability();
  $("#lx-url").value = v.LX_SOURCE_URL || "";
  lxVerifiedUrl = v.LX_SOURCE_URL || null;
  renderPlatformChips();
}

function collectConfig() {
  const provider = ($$("input[name=provider]").find((el) => el.checked) || {}).value || "";
  const values = {
    FNMUSIC_MUSICDL_ENABLED: provider === "musicdl",
    FNMUSIC_NETEASE_ENABLED: provider === "musicbox",
    FNMUSIC_LX_ENABLED: provider === "lxmusic",
    FNMUSIC_QUALITY_MODE: ($$("input[name=quality]").find((el) => el.checked) || {}).value || "high",
    FNMUSIC_RECOMMEND_HOT: $("#recommend-hot").checked,
    FNMUSIC_RECOMMEND_DAILY: $("#recommend-daily").checked,
    FNMUSIC_HISTORY_PLAYLIST_ENABLED: $("#history-playlist").checked,
    FNMUSIC_TEE_SAVE_ENABLED: $("#tee-enabled").checked,
    FNMUSIC_FAVORITE_DOWNLOAD_ENABLED: $("#fav-download-enabled").checked,
    FNMUSIC_DOWNLOAD_MIN_QUALITY: $("#fav-download-quality").value || "any",
    FNMUSIC_DIRECT_TOKEN: $("#direct-token").value.trim(),
    FNMUSIC_AUTO_COVER: $("#auto-cover").checked,
    FNMUSIC_LYRIC_AUTO_DL: $("#lyric-auto-dl").checked,
    FNMUSIC_TEE_SAVE_DIR: $("#tee-dir").value.trim(),
    FNMUSIC_TEE_CACHE_MAX: parseInt($("#tee-max").value || "2", 10),
    FNMUSIC_OFFICIAL_BIND_TIMEOUT_S: parseInt($("#bind-timeout").value || "120", 10) || 120,
    FNMUSIC_TEE_HANDOFF_MAX: parseInt($("#handoff-max").value || "3", 10) || 0,
    FNMUSIC_LIBRARY_SCAN_PATH: $("#scan-path").value.trim(),
    FNMUSIC_LLM_BASE_URL: $("#llm-base").value.trim(),
    FNMUSIC_LLM_API_KEY: $("#llm-key").value.trim(),
    FNMUSIC_LLM_MODEL: $("#llm-model").value.trim(),
    FNMUSIC_SEARCH_TIMEOUT: parseInt($("#search-timeout").value || "15", 10) || 15,
    FNMUSIC_SEARCH_PROBE: $("#search-probe").checked,
    FNMUSIC_APP_V2_SEARCH: $("#app-v2-search").checked,
    FNMUSIC_SEARCH_LOG_QUERY: $("#search-log-query").checked,
    FNMUSIC_NETEASE_MY_PLAYLISTS: $("#netease-my-playlists").checked,
    FNMUSIC_NETEASE_SUBSCRIBED: $("#netease-subscribed").checked,
    FNMUSIC_PUBLIC_PLAYLISTS_ENABLED: $("#public-playlists-enabled").checked,
    FNMUSIC_XIAOMUSIC_ENABLED: $("#xm-enabled").checked,
    // 常驻三态：未配置且勾选状态未变时不写键，避免把老配置钉成显式值
    ...(function residentValue() {
      const raw = (configValues.FNMUSIC_NETEASE_PLAYLIST_RESIDENT || "").trim();
      const checked = $("#netease-resident").checked;
      const effective = ($("#netease-my-playlists").checked
        || ($("#public-playlists-enabled").checked && publicMusicboxSelected()))
        && (raw === "" || raw === "true");
      if (raw === "" && checked === effective) return {};
      return { FNMUSIC_NETEASE_PLAYLIST_RESIDENT: checked };
    })(),
  };
  // 选择串只在「确有内容 / 本次动过 / 开关打开」时写：避免把没碰过的老配置写成 "[]"
  if ($("#public-playlists-enabled").checked || publicSelection.length || publicSelectionTouched) {
    values.FNMUSIC_PUBLIC_PLAYLISTS_SELECTION = JSON.stringify(
      publicSelection.map(({ provider, source, kind, id }) => ({ provider, source, kind, id }))
    );
  }
  if (provider === "musicdl") {
    values.FNMUSIC_ONLINE_SOURCES = platforms.enabled.join(",");
    values.MUSICDL_SOURCES = platforms.enabled.join(",");
  }
  if (provider === "lxmusic") {
    const url = $("#lx-url").value.trim();
    if (!url) throw new Error("洛雪源需要填写脚本 URL");
    values.LX_SOURCE_URL = url;
  }
  return values;
}

async function saveConfig() {
  let values;
  try { values = collectConfig(); } catch (exc) { toast(exc.message, "fail"); return; }
  const btn = $("#save-btn");
  btn.disabled = true;
  btn.textContent = "保存中…";
  try {
    const result = await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const failed = (result.actions || []).filter((a) => !a.ok);
    const parts = [];
    if (result.changed && result.changed.length) parts.push(`已保存 ${result.changed.length} 项`);
    (result.actions || []).forEach((a) => {
      if (a.kind === "process") parts.push(`进程 ${a.program} ${a.op} ${a.ok ? "成功" : "失败"}`);
      if (a.kind === "lx_activate") parts.push(`洛雪源激活${a.ok ? "成功" : "失败"}`);
    });
    if (failed.length) {
      toast((parts.join("；") || "") + ` —— ${failed.map((f) => f.error).join("；")}`, "fail");
    } else {
      toast(parts.join("；") || "配置无变化", "ok");
    }
    clearDirty();
    await loadConfig();
    await loadStatus();
  } catch (exc) {
    toast("保存失败：" + exc.message, "fail");
  } finally {
    btn.disabled = false;
    btn.textContent = "保存并生效";
  }
}
$("#save-btn").addEventListener("click", saveConfig);

/* -------------------------------------------------------------- 音源选择 */
function savedProvider() {
  const v = configValues;
  return v.FNMUSIC_NETEASE_ENABLED === "true" ? "musicbox"
    : v.FNMUSIC_MUSICDL_ENABLED === "true" ? "musicdl"
    : v.FNMUSIC_LX_ENABLED === "true" ? "lxmusic" : "";
}

// 点选未启用的音源：临时拉起其进程供预览（不写配置；5 分钟内未保存自动停止）
async function startPreview(provider) {
  try {
    const r = await api("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider }),
    });
    if (r.preview) toast(`已临时启动 ${PROVIDER_LABEL[provider]}（预览）：5 分钟内未保存将自动停止`);
    return true;
  } catch (exc) {
    toast(`临时启动 ${PROVIDER_LABEL[provider]} 失败：${exc.message}`, "fail");
    return false;
  }
}

function syncProviderPanels(provider) {
  $$(".provider-card").forEach((el) => el.classList.toggle("selected", el.dataset.provider === provider));
  $("#panel-musicdl").hidden = provider !== "musicdl";
  $("#panel-lxmusic").hidden = provider !== "lxmusic";
}
$$("input[name=provider]").forEach((el) =>
  el.addEventListener("change", async () => {
    syncProviderPanels(el.value);
    if (el.value === "musicbox") syncNeteaseAccount();
    markDirty("音源切换需保存后生效");
    if (el.value && el.value !== savedProvider() && await startPreview(el.value)) {
      if (el.value === "musicdl") await loadPlatforms(false);
    }
  }));

/* -------------------------------------------------------------- musicdl 平台 */
function setPlatformFallback() {
  platforms = { enabled: (configValues.FNMUSIC_ONLINE_SOURCES || "").split(",").filter(Boolean), registered: [] };
  $("#platform-note").textContent = "musicdl 进程未运行，暂无法获取平台列表（点选 musicdl 音源可临时启动预览）";
  renderPlatformChips();
}

async function fetchPlatforms() {
  const data = await api("/api/platforms");
  platforms = { enabled: data.enabled || [], registered: data.registered || [] };
  $("#platform-note").textContent = `共 ${platforms.registered.length} 个注册平台，已启用 ${platforms.enabled.length} 个`;
  renderPlatformChips();
}

async function loadPlatforms(autoPreview = true) {
  try {
    await fetchPlatforms();
  } catch (_) {
    // 进程未运行：autoPreview（用户点选/刷新触发）时临时拉起后重试；页面加载不自动拉起
    if (!autoPreview) { setPlatformFallback(); return; }
    try {
      if (await startPreview("musicdl")) await fetchPlatforms();
      else setPlatformFallback();
    } catch (_) {
      setPlatformFallback();
    }
  }
}

function renderPlatformChips() {
  const keyword = $("#platform-search").value.trim().toLowerCase();
  const container = $("#platform-list");
  const enabledSet = new Set(platforms.enabled);
  const items = platforms.registered.filter((p) => !keyword || p.toLowerCase().includes(keyword));
  container.innerHTML = items.length
    ? items.map((p) => `<span class="chip ${enabledSet.has(p) ? "on" : ""}" data-platform="${p}">${p}</span>`).join("")
    : `<span class="muted">无匹配平台</span>`;
  container.querySelectorAll(".chip[data-platform]").forEach((chip) =>
    chip.addEventListener("click", () => {
      const p = chip.dataset.platform;
      const set = new Set(platforms.enabled);
      if (set.has(p)) { set.delete(p); chip.classList.remove("on"); }
      else { set.add(p); chip.classList.add("on"); }
      platforms.enabled = Array.from(set);
      markDirty("musicdl 平台已修改");
    }));
}
$("#platform-search").addEventListener("input", renderPlatformChips);
$("#platform-reload").addEventListener("click", loadPlatforms);

/* -- musicdl 音源测试搜索：输入歌名逐平台验证是否出歌 -- */
// 测试范围（2026-10-22 用户需求）：enabled=只测已勾选平台（默认，历史行为）；
// all=一次测全部注册平台（新装/换源时快速看哪些平台能出歌）
let musicdlTestScope = "enabled";

function renderMusicdlTestScope() {
  document.querySelectorAll("#musicdl-test-scope .chip").forEach((chip) =>
    chip.classList.toggle("on", chip.dataset.scope === musicdlTestScope));
}

function setMusicdlTestScope(scope) {
  musicdlTestScope = scope === "all" ? "all" : "enabled";
  renderMusicdlTestScope();
}
document.querySelectorAll("#musicdl-test-scope .chip").forEach((chip) =>
  chip.addEventListener("click", () => setMusicdlTestScope(chip.dataset.scope)));

async function musicdlTest() {
  const q = $("#musicdl-test-q").value.trim();
  const box = $("#musicdl-test-result");
  if (!q) return toast("请输入歌名", "fail");
  const scopeAll = musicdlTestScope === "all";
  const sources = scopeAll ? (platforms.registered || []) : platforms.enabled;
  if (!sources.length) {
    return toast(scopeAll ? "平台列表还没加载，请先点刷新列表" : "没有勾选任何平台", "fail");
  }
  // 全部平台是一次重活（几十个源 × 每个都要解析直链）：慢网下要 2-4 分钟，
  // 期间共享探活池被占满，App 里点歌/收藏会跟着变慢。平台多时先确认。
  if (scopeAll && sources.length > 20 && typeof window.confirm === "function") {
    const mins = Math.max(1, Math.round((sources.length / 8) * 25 / 60));
    if (!window.confirm(
      `测全部 ${sources.length} 个平台约需 ${mins} 分钟，期间点歌/收藏可能变慢，` +
      `部分源还会被熔断（约 2 分钟自愈）。\n\n只想确认常用源能否出歌，用「已勾选」更快。继续吗？`
    )) return;
  }
  box.hidden = false;
  box.className = "report";
  const scopeLabel = scopeAll ? "全部平台" : "已勾选";
  // 全部模式分批（2026-10-22）：一次打几十个源 = 数百条外网连接，
  // 会把出口/路由会话表压垮（实测导致所有音源搜索集体变慢/超时）。8 个一批串行。
  const BATCH = 8;
  const chunks = [];
  if (scopeAll) {
    for (let i = 0; i < sources.length; i += BATCH) chunks.push(sources.slice(i, i + BATCH));
  } else {
    chunks.push(sources);
  }
  box.textContent = `测试中…（${scopeLabel} ${sources.length} 个${chunks.length > 1 ? `，分 ${chunks.length} 批` : ""}，部分慢平台可能要等十几秒）`;
  const btn = $("#musicdl-test-run");
  btn.disabled = true;
  const tested = [];   // 分批累积（catch 里也要能读到已测结果）
  let tookMs = 0;
  try {
    for (let i = 0; i < chunks.length; i++) {
      if (chunks.length > 1) {
        box.textContent = `测试中…（第 ${i + 1}/${chunks.length} 批，已完成 ${tested.length}/${sources.length} 个平台）`;
      }
      const body = await api("/api/musicdl/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keyword: q, sources: chunks[i], scope: musicdlTestScope }),
      });
      tested.push(...(body.platforms || []));
      tookMs += body.tookMs || 0;
      // 批间喘口气：上一批的连接/探活任务还没释放完，紧接着打下一批会互相抢
      // 共享探活池（8 worker），让每批都更容易撞超时。
      if (i < chunks.length - 1) await new Promise((r) => setTimeout(r, 300));
    }
    const rows = tested.map((p) => {
      const ok = p.count > 0;
      const head = ok ? `✓ ${p.count} 条：${(p.samples || []).join(" / ")}` : `✗ ${p.error || "无结果"}`;
      return `<div class="kv" style="color:${ok ? "var(--ok)" : "var(--err)"}"><b>${escapeHtml(p.source)}</b> ${escapeHtml(head)}</div>`;
    });
    const hit = tested.filter((p) => p.count > 0).length;
    const secs = tookMs ? `，耗时 ${(tookMs / 1000).toFixed(1)}s` : "";
    // 测完有源超时/熔断时提示自愈时间：这些源在接下来 2 分钟内点歌会被跳过，
    // 不说清楚用户会以为「测了一下结果点歌坏了」。
    const sick = tested.filter((p) => !p.count && /超时|timeout|busy|熔断|circuit/i.test(String(p.error || ""))).length;
    const summary =
      `<div class="kv muted">范围：${scopeLabel} ${sources.length} 个 · 出歌 ${hit} 个${secs}</div>` +
      (sick
        ? `<div class="kv muted">其中 ${sick} 个源超时/熔断，约 2 分钟后自动恢复；期间点歌若搜不到歌，稍等再试。</div>`
        : "");
    box.innerHTML = summary + (rows.join("") || '<div class="kv muted">没有可测的平台</div>');
    // 判定：已勾选模式全部出歌才算 ok（历史行为）；全部模式只要有平台出歌即 ok
    // （57 个平台里个别没有某首歌是正常的，不能把整页标红）
    const bad = !tested.length || (scopeAll ? hit === 0 : tested.some((p) => !p.count));
    box.className = "report " + (bad ? "fail" : "ok");
  } catch (e) {
    box.className = "report fail";
    box.textContent = `测试失败：${e.message}${testedSafe(tested)}`;
  } finally {
    btn.disabled = false;
  }
}

// 分批中途失败时把已测结果也带出来，别让用户白等
function testedSafe(tested) {
  if (!tested || !tested.length) return "";
  const hit = tested.filter((p) => p.count > 0).length;
  return `（已测 ${tested.length} 个平台，其中 ${hit} 个出歌）`;
}
$("#musicdl-test-run").addEventListener("click", musicdlTest);
$("#musicdl-test-q").addEventListener("keydown", (e) => { if (e.key === "Enter") musicdlTest(); });

/* -------------------------------------------------------------- 公共歌单 */
// 发现池浏览 + 选择（issue #55）。选择结果写入 FNMUSIC_PUBLIC_PLAYLISTS_SELECTION，
// 音乐页展示由 proxy 侧完成；这里只负责「挑歌单」这一件事。
const PUBLIC_SOURCE_LABEL = { netease: "网易", wy: "网易", kw: "酷我", kg: "酷狗", tx: "QQ", mg: "咪咕" };
const PUBLIC_CHANNELS = [
  { id: "charts", name: "排行榜" },
  { id: "recommend", name: "推荐歌单" },
  { id: "playlist", name: "分类歌单" },
  { id: "highquality", name: "精品歌单" },
  { id: "account_recommend", name: "账号推荐（需登录）", needLogin: true },
];
let publicSelection = [];        // [{provider, source, kind, id, _name, _cover_url, _track_count}]
let publicSelectionTouched = false;
let publicCapabilities = null;
let publicState = {
  channel: "charts", category: "", sort: "hot", page: 1,
  items: [], hasMore: false, total: 0, cursor: "", cursors: { 1: "" },
};
// 空选择（从未配置过）时给一份开箱可用的默认榜单，与 proxy 侧口径一致
const PUBLIC_DEFAULT_SELECTION = [
  { provider: "musicbox", source: "netease", kind: "chart", id: "3778678", _name: "热歌榜" },
  { provider: "musicbox", source: "netease", kind: "chart", id: "3779629", _name: "新歌榜" },
  { provider: "musicbox", source: "netease", kind: "chart", id: "19723756", _name: "飙升榜" },
  { provider: "musicbox", source: "netease", kind: "chart", id: "2884035", _name: "原创榜" },
];

function publicEntryKey(item) {
  // 网易榜单与同名歌单是同一份内容：去重键统一按 playlist（与 proxy 侧一致）
  return [item.provider, item.source, "playlist", String(item.id)].join("|");
}

function publicSelected(item) {
  return publicSelection.find((it) => publicEntryKey(it) === publicEntryKey(item));
}

function publicMusicboxSelected() {
  return publicSelection.some((it) => it.provider === "musicbox");
}

function setPublicSelection(item, selected) {
  if (selected) {
    if (publicSelected(item)) return true;
    if (publicSelection.length >= 50) {
      toast("公共歌单最多选择 50 份", "fail");
      return false;
    }
    publicSelection.push({
      provider: item.provider || "musicbox",
      source: item.source || "netease",
      kind: item.kind || "playlist",
      id: String(item.id),
      _name: item.name || "",
      _cover_url: item.cover_url || "",
      _track_count: item.track_count || 0,
    });
  } else {
    publicSelection = publicSelection.filter((it) => publicEntryKey(it) !== publicEntryKey(item));
  }
  publicSelectionTouched = true;
  renderPublicSelected();
  updateResidentAvailability();
  renderPublicList();
  markDirty("公共歌单选择已修改");
  return true;
}

function movePublicSelection(index, delta) {
  const next = index + delta;
  if (next < 0 || next >= publicSelection.length) return;
  const moved = publicSelection.splice(index, 1);
  publicSelection.splice(next, 0, moved[0]);
  publicSelectionTouched = true;
  renderPublicSelected();
  markDirty("公共歌单顺序已修改");
}

function renderPublicSelected() {
  const box = $("#public-selected");
  if (!box) return;
  box.innerHTML = publicSelection.length ? publicSelection.map((item, index) => `
    <div class="public-selected">
      <img src="${escapeHtml(item._cover_url || "")}" alt="" ${item._cover_url ? "" : "hidden"}>
      <div class="min">
        <b>${escapeHtml(item._name || item.id)}</b>
        <span>${escapeHtml(PUBLIC_SOURCE_LABEL[item.source] || item.source)} · ${item.kind === "chart" ? "榜单" : "歌单"} · ${escapeHtml(item.id)}</span>
        ${Number(item._track_count || 0) > 1000 ? "<small>超过 1,000 首，仅载入前 1,000 首。</small>" : ""}
      </div>
      <div class="btn-row">
        <button class="btn small" data-public-up="${index}" ${index === 0 ? "disabled" : ""}>↑</button>
        <button class="btn small" data-public-down="${index}" ${index === publicSelection.length - 1 ? "disabled" : ""}>↓</button>
        <button class="btn small danger" data-public-remove="${index}">移除</button>
      </div>
    </div>
  `).join("") : '<p class="hint">尚未选择歌单。</p>';
  box.querySelectorAll("[data-public-up]").forEach((btn) =>
    btn.addEventListener("click", () => movePublicSelection(Number(btn.dataset.publicUp), -1)));
  box.querySelectorAll("[data-public-down]").forEach((btn) =>
    btn.addEventListener("click", () => movePublicSelection(Number(btn.dataset.publicDown), 1)));
  box.querySelectorAll("[data-public-remove]").forEach((btn) =>
    btn.addEventListener("click", () => {
      publicSelection.splice(Number(btn.dataset.publicRemove), 1);
      publicSelectionTouched = true;
      renderPublicSelected();
      updateResidentAvailability();
      renderPublicList();
      markDirty("公共歌单选择已修改");
    }));
}

function renderPublicList() {
  const box = $("#public-list");
  if (!box) return;
  box.innerHTML = publicState.items.length ? publicState.items.map((item) => {
    const checked = !!publicSelected(item);
    return `
      <label class="public-item">
        <input type="checkbox" data-public-id="${escapeHtml(String(item.id))}" ${checked ? "checked" : ""}>
        <img src="${escapeHtml(item.cover_url || "")}" alt="" ${item.cover_url ? "" : "hidden"}>
        <span class="min">
          <b>${escapeHtml(item.name || String(item.id))}</b>
          <small>${escapeHtml(item.author || PUBLIC_SOURCE_LABEL[item.source] || item.source || "")} · ${Number(item.track_count || 0)} 首</small>
        </span>
      </label>`;
  }).join("") : '<p class="hint">当前入口没有返回歌单。</p>';
  box.querySelectorAll("input[data-public-id]").forEach((input) => {
    input.addEventListener("change", () => {
      const item = publicState.items.find((it) => String(it.id) === input.dataset.publicId);
      if (item) setPublicSelection(item, input.checked);
    });
  });
}

function renderPublicPage() {
  $("#public-page").textContent = `${publicState.page}${publicState.hasMore ? "（还有更多）" : ""}`;
  $("#public-prev").disabled = publicState.page <= 1;
  $("#public-next").disabled = !publicState.hasMore;
}

async function publicApi(target, params = {}, retry = true) {
  const query = new URLSearchParams({ provider: "musicbox", ...params });
  try {
    const data = await api(`/api/public-playlists/${target}?${query.toString()}`);
    if (data.ok === false) throw new Error(data.error || "歌单请求失败");
    return data;
  } catch (exc) {
    if (!retry) throw exc;
    // 盒子没起来时先临时拉起预览再重试一次（与点选音源卡片的预览机制一致）
    if (!await startPreview("musicbox")) throw exc;
    return await publicApi(target, params, false);
  }
}

function publicChannels() {
  const logged = !!(((publicCapabilities || {}).sources || []).find((it) => it.id === "netease") || {}).logged_in;
  return PUBLIC_CHANNELS.filter((it) => !it.needLogin || logged);
}

async function loadPublicCapabilities() {
  try {
    publicCapabilities = await publicApi("capabilities");
  } catch (exc) {
    publicCapabilities = null;
    $("#public-note").textContent = "网易盒子不可达：" + exc.message;
  }
  renderPublicChannels();
  return publicCapabilities;
}

function renderPublicChannels() {
  const select = $("#public-channel");
  const channels = publicChannels();
  select.innerHTML = channels.map((it) => `<option value="${it.id}">${it.name}</option>`).join("");
  if (!channels.some((it) => it.id === publicState.channel)) publicState.channel = "charts";
  select.value = publicState.channel;
}

function fillPublicCategories(data) {
  publicState.cursor = "";
  publicState.cursors = { 1: "" };
  const select = $("#public-category");
  const groups = Array.isArray(data.categories) ? data.categories : [];
  const opts = ['<option value="">全部分类</option>'];
  for (const group of groups) {
    const list = Array.isArray(group.list) ? group.list : [group];
    // 平铺的单层分类（含 id 为空的「全部」）直接当选项，多分类才用 optgroup
    if (list.length === 1 && !Array.isArray(group.list)) {
      if (group.id) opts.push(`<option value="${escapeHtml(String(group.id))}">${escapeHtml(group.name || group.id)}</option>`);
      continue;
    }
    opts.push(`<optgroup label="${escapeHtml(group.name || "分类")}">${list.map((tag) =>
      `<option value="${escapeHtml(String(tag.id ?? tag.name ?? ""))}">${escapeHtml(tag.name || tag.id)}</option>`).join("")}</optgroup>`);
  }
  select.innerHTML = opts.join("");
  publicState.category = "";
  // 分类分页列表才有排序语义（精选/推荐/榜单按各自规则排序）
  $("#public-sort").hidden = publicState.channel !== "playlist";
  publicState.sort = $("#public-sort").value || "hot";
}

async function loadPublicCategories() {
  if (publicState.channel === "recommend" || publicState.channel === "account_recommend") {
    fillPublicCategories({ categories: [] });
    return;
  }
  try {
    const data = await publicApi("categories", { kind: publicState.channel === "highquality" ? "highquality" : "playlist" });
    fillPublicCategories(publicState.channel === "highquality"
      ? { categories: data.highquality_tags || data.tags || [] }
      : data);
  } catch (exc) {
    fillPublicCategories({ categories: [] });
    $("#public-note").textContent = "分类加载失败：" + exc.message;
  }
}

async function loadPublicList() {
  $("#public-note").textContent = "正在加载歌单…";
  try {
    const params = {
      channel: publicState.channel, category: publicState.category,
      sort: publicState.sort, page: publicState.page,
    };
    if (publicState.channel === "highquality") params.cursor = publicState.cursors[publicState.page] || "";
    const data = await publicApi("", params);
    publicState.items = (data.items || []).map((item) => ({
      ...item,
      provider: "musicbox",
      source: item.source || "netease",
      kind: publicState.channel === "charts" ? "chart" : (item.kind || "playlist"),
    }));
    publicState.hasMore = !!data.has_more;
    publicState.total = Number(data.total || 0);
    if (publicState.channel === "highquality") publicState.cursors[publicState.page + 1] = data.next_cursor || "";
    const suffix = publicState.total ? `，共约 ${publicState.total} 份` : "";
    $("#public-note").textContent = `返回 ${publicState.items.length} 份${suffix}；勾选后保存生效。`;
  } catch (exc) {
    publicState.items = [];
    publicState.hasMore = false;
    $("#public-note").textContent = "歌单加载失败：" + exc.message;
  }
  renderPublicList();
  renderPublicPage();
}

async function initPublicDefaults() {
  publicSelection = PUBLIC_DEFAULT_SELECTION.map((it) => ({ ...it }));
  publicSelectionTouched = true;
  renderPublicSelected();
  updateResidentAvailability();
  markDirty("已填入默认网易榜单（热歌/新歌/飙升/原创）");
}

async function showPublicBrowser() {
  $("#public-playlists-browser").hidden = false;
  await loadPublicCapabilities();
  await loadPublicCategories();
  // 补齐已选条目的名称/封面（选择串只存了 id）
  for (const item of publicSelection) {
    if (item._name) continue;
    try {
      const resp = await publicApi(encodeURIComponent(item.id), { kind: item.kind });
      item._name = resp.data.name;
      item._cover_url = resp.data.cover_url;
      item._track_count = resp.data.track_count;
    } catch (_) { /* 拿不到名字就按 id 展示，不阻塞 */ }
  }
  renderPublicSelected();
  await loadPublicList();
}

$("#public-playlists-enabled").addEventListener("change", async () => {
  const enabled = $("#public-playlists-enabled").checked;
  updateResidentAvailability();
  markDirty(enabled ? "公共歌单已开启" : "公共歌单已关闭");
  if (!enabled) { $("#public-playlists-browser").hidden = true; return; }
  if (!publicSelection.length && !publicSelectionTouched) await initPublicDefaults();
  await showPublicBrowser();
});
$("#public-channel").addEventListener("change", async () => {
  publicState.channel = $("#public-channel").value;
  publicState.page = 1;
  markDirty();
  await loadPublicCategories();
  await loadPublicList();
});
$("#public-category").addEventListener("change", async () => {
  publicState.category = $("#public-category").value;
  publicState.page = 1;
  publicState.cursors = { 1: "" };
  markDirty();
  await loadPublicList();
});
$("#public-sort").addEventListener("change", async () => {
  publicState.sort = $("#public-sort").value;
  publicState.page = 1;
  markDirty();
  await loadPublicList();
});
$("#public-refresh").addEventListener("click", async () => {
  await loadPublicCapabilities();
  await loadPublicCategories();
  await loadPublicList();
});
$("#public-prev").addEventListener("click", async () => {
  if (publicState.page > 1) { publicState.page--; await loadPublicList(); }
});
$("#public-next").addEventListener("click", async () => {
  if (publicState.hasMore) { publicState.page++; await loadPublicList(); }
});

/* -------------------------------------------------------------- 网易扫码 */
async function syncNeteaseAccount(retries = 0) {
  // 把网易账号态同步到 #qr-check（已登录显示昵称；未登录/失败清空）
  for (let i = 0; ; i++) {
    try {
      const st = await api("/api/netease/auth/status");
      const d = st.data || st;
      if (d.logged_in) {
        $("#qr-check").textContent = `当前登录：${d.nickname || d.user_id || "已登录用户"}`;
        return;
      }
    } catch (_) { /* status 不可达视为未登录 */ }
    if (i >= retries) { $("#qr-check").textContent = ""; return; }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

async function startQrLogin() {
  stopQrPolling();
  $("#qr-status").textContent = "正在生成二维码…";
  $("#qr-img").hidden = true;
  syncNeteaseAccount(); // 生成前同步当前账号态（非致命，不阻塞出码）
  const callLogin = () => api("/api/netease/auth/login", { method: "POST" });
  try {
    let data;
    try {
      data = await callLogin();
    } catch (exc) {
      // musicbox 进程未运行（未点选/预览过期）：临时拉起后重试一次
      if (!await startPreview("musicbox")) throw exc;
      data = await callLogin();
    }
    const unikey = data.unikey || data.codekey || (data.data && (data.data.unikey || data.data.codekey)) || "";
    if (!unikey) throw new Error("未获取到 unikey");
    $("#qr-img").src = `${APP_BASE}/api/netease/qr?unikey=${encodeURIComponent(unikey)}`;
    $("#qr-img").hidden = false;
    $("#qr-status").textContent = "请用手机网易云音乐 App 扫码";
    pollQr(unikey);
  } catch (exc) {
    $("#qr-status").textContent = "生成失败：" + exc.message;
  }
}

async function checkQrStatus(unikey) {
  const st = await api(`/api/netease/auth/login/check?unikey=${encodeURIComponent(unikey)}`);
  // musicbox CLI 返回 {ok, data:{code}} 信封结构；兼容扁平 {code}
  const code = st?.data?.code ?? st?.code;
  if (code === 803) {
    stopQrPolling();
    $("#qr-status").textContent = "登录成功 ✓";
    syncNeteaseAccount(2); // 803 后 cookie 落盘需要一点时间，带重试取昵称
    toast("网易账号登录成功", "ok");
  } else if (code === 802) {
    $("#qr-status").textContent = "已扫码，请在手机上确认";
  } else if (code === 800) {
    stopQrPolling();
    $("#qr-status").textContent = "二维码已过期，请重新生成";
  } else {
    $("#qr-status").textContent = "等待扫码…";
  }
}

function pollQr(unikey, intervalMs = 2000) {
  stopQrPolling();
  qrTimer = setInterval(() => { checkQrStatus(unikey).catch(() => {}); }, intervalMs);
}

function stopQrPolling() {
  if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
}
$("#qr-btn").addEventListener("click", startQrLogin);

/* -------------------------------------------------------------- lx 源测试 */
$("#lx-test").addEventListener("click", async () => {
  const url = $("#lx-url").value.trim();
  const box = $("#lx-report");
  if (!url) { toast("请先填写源 URL", "fail"); return; }
  box.hidden = false;
  box.className = "report";
  box.textContent = "测试中（下载脚本 → 沙箱初始化 → 多首抽样搜索/解析/探活）…";
  const callVerify = () => api("/api/lx/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values: { url } }),
  });
  try {
    let r;
    try {
      r = await callVerify();
    } catch (exc) {
      // lxmusic 进程未运行（未点选/预览过期）：临时拉起后重试一次
      if (!await startPreview("lxmusic")) throw exc;
      r = await callVerify();
    }
    if (r.ok) {
      const d = r.data || {};
      const meta = d.meta || {};
      const probe = d.probe || {};
      lxVerifiedUrl = url;
      box.className = "report ok";
      box.innerHTML =
        `<div class="kv"><b>源名称</b>${meta.name || "-"} ${meta.version ? "v" + meta.version : ""}（${meta.author || "未知作者"}）</div>` +
        `<div class="kv"><b>可用平台</b>${(d.platforms || []).join("、")}</div>` +
        (probe.title ? `<div class="kv"><b>实测</b>${probe.title} - ${probe.artist} [${probe.platform}/${probe.quality}] ${probe.content_type || ""}</div>` : "") +
        `<div class="kv"><b>结论</b>可用 ✓（保存后激活）</div>`;
    } else {
      const d = r.data || {};
      box.className = "report fail";
      box.innerHTML = `<div class="kv"><b>不可用</b>${d.message || r.error || "校验失败"}</div>`;
    }
  } catch (exc) {
    box.className = "report fail";
    box.textContent = "测试失败：" + exc.message;
  }
});

/* -------------------------------------------------- lx 源：文件上传 / NAS 选择 */
async function lxUploadScript(filename, script) {
  // 先确保 lxmusic 进程可用（预览拉起），再转发落盘
  const callUpload = () => api("/api/lx/upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, script }),
  });
  try {
    return await callUpload();
  } catch (exc) {
    if (!await startPreview("lxmusic")) throw exc;
    return await callUpload();
  }
}

async function lxAfterUpload(r) {
  const d = r.data || {};
  $("#lx-url").value = d.url || "";
  lxVerifiedUrl = null; // 上传地址仍需走一次"测试"
  $("#lx-upload-note").textContent = d.meta && d.meta.name ? `已上传：${d.meta.name}` : "已上传";
  markDirty("洛雪源已更新为上传脚本，测试后保存生效");
  toast("脚本已上传，请点「测试」验证后保存", "ok");
}

$("#lx-upload").addEventListener("click", () => $("#lx-file").click());
$("#lx-file").addEventListener("change", async () => {
  const file = $("#lx-file").files && $("#lx-file").files[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".js")) { toast("只支持 .js 后缀文件", "fail"); return; }
  if (file.size > 9_000_000) { toast("脚本超过 9MB 上限", "fail"); return; }
  $("#lx-upload-note").textContent = "读取并上传中…";
  try {
    const script = await file.text();
    const r = await lxUploadScript(file.name, script);
    await lxAfterUpload(r);
  } catch (exc) {
    $("#lx-upload-note").textContent = "";
    toast("上传失败：" + exc.message, "fail");
  } finally {
    $("#lx-file").value = ""; // 允许重复选择同一文件
  }
});

/* NAS 文件选择：仅桌面环境（统一网关 /app/fnmusic-ext 内）可用。
   选中的主机路径经 /api/host-file 代读（webui_gateway 本地处理），再走上传落盘。 */
let lxTrimSdk = null;
async function lxLoadTrimSdk() {
  if (lxTrimSdk !== null) return lxTrimSdk;
  try {
    const mod = await import("/app/fnmusic-ext/static/vendor/trim-web-app.js");
    lxTrimSdk = new mod.TrimApp();
  } catch (_) {
    lxTrimSdk = false;
  }
  return lxTrimSdk;
}

async function lxPickFromNas() {
  const sdk = await lxLoadTrimSdk();
  if (!sdk) { toast("当前环境不支持 NAS 文件选择（直连 8774 时请用上传或 URL）", "fail"); return; }
  try {
    const result = await sdk.pickUserFile({
      directory: false,
      accept: [".js"],
      title: "选择洛雪源脚本",
      okText: "选择",
      sidebarGroup: ["myFiles", "otherShare", "favorites"],
    });
    const paths = (result && result.data) || [];
    if (!paths.length) return;
    const hostPath = paths[0];
    $("#lx-upload-note").textContent = "读取 NAS 文件中…";
    const resp = await fetch(APP_BASE + "/api/host-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: hostPath }),
    });
    let body = {};
    try { body = await resp.json(); } catch (_) { /* 非 JSON */ }
    if (!resp.ok) throw new Error(body.error || body.detail || `HTTP ${resp.status}`);
    const filename = hostPath.split("/").pop() || "source.js";
    const r = await lxUploadScript(filename, body.script || "");
    await lxAfterUpload(r);
  } catch (exc) {
    $("#lx-upload-note").textContent = "";
    toast("NAS 选择失败：" + exc.message, "fail");
  }
}
$("#lx-pick").addEventListener("click", lxPickFromNas);

(async function detectNasPicker() {
  // 桌面网关路径下才尝试加载 SDK；探测失败（直连 8774）保持隐藏
  const pathname = (typeof window !== "undefined" && window.location && window.location.pathname) || "";
  if (pathname.startsWith("/app/")) {
    const sdk = await lxLoadTrimSdk();
    if (sdk) $("#lx-pick").hidden = false;
  }
})();

/* -------------------------------------------------------------- 歌曲匹配 */
const matchState = { page: 1, size: 50, total: 0, items: [], taskId: null, timer: null, loaded: false };

function matchApi(path, options) {
  return api(path, options).then((body) => {
    if (body && typeof body.code === "number" && body.code !== 0) {
      throw new Error(body.msg || `错误 ${body.code}`);
    }
    return body;
  });
}

function matchWants() {
  const w = [];
  if ($("#match-want-lyric").checked) w.push("lyric");
  if ($("#match-want-cover").checked) w.push("cover");
  return w;
}

function matchRender() {
  const rows = $("#match-rows");
  const sel = new Set([...rows.querySelectorAll("input[data-guid]:checked")].map((c) => c.dataset.guid));
  rows.innerHTML = "";
  if (!matchState.items.length) {
    rows.innerHTML = `<tr><td colspan="7" class="muted">没有匹配的歌曲</td></tr>`;
  }
  for (const it of matchState.items) {
    const tr = document.createElement("tr");
    tr.dataset.guid = it.guid;
    const checked = sel.has(it.guid) ? "checked" : "";
    tr.innerHTML = `
      <td class="mt-check"><input type="checkbox" data-guid="${it.guid}" ${checked}></td>
      <td class="mt-title" title="${(it.path || "").replace(/"/g, "&quot;")}">${escapeHtml(it.title || "（无标题）")}</td>
      <td>${escapeHtml(it.artist || "")}</td>
      <td>${escapeHtml(it.album || "")}</td>
      <td class="mt-flag">${it.hasLyric ? "✓" : "—"}</td>
      <td class="mt-flag">${it.hasCover ? "✓" : "—"}</td>
      <td class="mt-status" data-status-for="${it.guid}"></td>`;
    rows.appendChild(tr);
  }
  $("#match-count").textContent = `共 ${matchState.total} 首，本页 ${matchState.items.length} 首`;
  $("#match-pageinfo").textContent = `第 ${matchState.page} 页 / ${Math.max(1, Math.ceil(matchState.total / matchState.size))} 页`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function matchLoad() {
  try {
    const body = await matchApi(`/api/match/tracks?page=${matchState.page}&size=${matchState.size}` +
      `&q=${encodeURIComponent($("#match-q").value.trim())}&filter=${$("#match-filter").value}`);
    matchState.total = body.data.total;
    matchState.items = body.data.items;
    matchRender();
    matchState.loaded = true;
  } catch (e) {
    $("#match-rows").innerHTML = `<tr><td colspan="7" style="color:var(--err)">${escapeHtml(e.message)}</td></tr>`;
  }
}

function matchSelected() {
  return [...$("#match-rows").querySelectorAll("input[data-guid]:checked")].map((c) => c.dataset.guid);
}

async function matchRun(guids) {
  const wants = matchWants();
  if (!wants.length) return toast("至少勾选歌词或封面之一", "fail");
  if (!guids.length) return toast("请先勾选歌曲", "fail");
  if (matchState.timer) return toast("已有匹配任务进行中", "fail");
  try {
    const body = await matchApi("/api/match/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guids, wants }),
    });
    matchState.taskId = body.data.taskId;
    const box = $("#match-progress");
    box.hidden = false;
    box.className = "report";
    box.textContent = `任务 ${matchState.taskId}：0 / ${guids.length}`;
    matchState.timer = setInterval(matchPoll, 1200);
  } catch (e) {
    toast(e.message, "fail");
  }
}

async function matchPoll() {
  if (!matchState.taskId) return;
  try {
    const body = await matchApi(`/api/match/task/${matchState.taskId}`);
    const t = body.data;
    const box = $("#match-progress");
    // 即时把单曲结果标到表格状态列
    for (const r of t.results || []) {
      const cell = $(`[data-status-for="${r.guid}"]`);
      if (cell) {
        cell.textContent = r.ok
          ? (r.source ? `✓ 已写入（${r.source}）` : "✓ 已写入")
          : (r.error || "失败");
        cell.style.color = r.ok ? "var(--ok)" : "var(--err)";
      }
    }
    box.textContent = `任务 ${t.id}：${t.done} / ${t.total}` +
      (t.current ? `（处理中 ${t.current.slice(0, 8)}…）` : "") +
      `，成功 ${t.okCount}`;
    if (!t.running) {
      clearInterval(matchState.timer);
      matchState.timer = null;
      matchState.taskId = null;
      const bad = (t.results || []).filter((r) => !r.ok);
      box.className = "report " + (bad.length ? "fail" : "ok");
      box.textContent = `完成：成功 ${t.okCount} / ${t.total}` +
        (bad.length ? `，失败 ${bad.length}（如：${bad[0].error || "?"}）` : "");
      toast(`匹配完成：成功 ${t.okCount}/${t.total}`, bad.length ? "fail" : "ok");
      matchLoad();
    }
  } catch (e) {
    clearInterval(matchState.timer);
    matchState.timer = null;
    toast(`进度查询失败：${e.message}`, "fail");
  }
}

$$('[data-page="match"]').forEach((btn) => btn.addEventListener("click", () => {
  if (!matchState.loaded) matchLoad();
}));
$("#match-refresh").addEventListener("click", () => { matchState.page = 1; matchLoad(); });
$("#match-q").addEventListener("keydown", (e) => { if (e.key === "Enter") { matchState.page = 1; matchLoad(); } });
$("#match-filter").addEventListener("change", () => { matchState.page = 1; matchLoad(); });
// 直链示例 URL：进页面时按当前地址与令牌实时生成
$$('[data-page="direct"]').forEach((btn) => btn.addEventListener("click", () => {
  const base = `${location.protocol}//${location.host}`;
  const token = encodeURIComponent($("#direct-token").value.trim() || "<你的令牌>");
  const play = $("#direct-url-play");
  const search = $("#direct-url-search");
  if (play) play.textContent = `${base}/direct?id=kuwo:123&token=${token}`;
  if (search) search.textContent = `${base}/direct/search?keyword=晴天&token=${token}`;
}));
$("#match-prev").addEventListener("click", () => { if (matchState.page > 1) { matchState.page--; matchLoad(); } });
$("#match-next").addEventListener("click", () => {
  if (matchState.page * matchState.size < matchState.total) { matchState.page++; matchLoad(); }
});
$("#match-check-all").addEventListener("change", (e) => {
  $("#match-rows").querySelectorAll("input[data-guid]").forEach((c) => { c.checked = e.target.checked; });
});
$("#match-run").addEventListener("click", () => matchRun(matchSelected()));
$("#match-missing").addEventListener("click", () => {
  const wants = matchWants();
  const guids = matchState.items
    .filter((it) => (wants.includes("lyric") && !it.hasLyric) || (wants.includes("cover") && !it.hasCover))
    .map((it) => it.guid);
  matchRun(guids);
});

/* -------------------------------------------------------------- 重复清理 */
const dedupState = { groups: [] };

function dedupFormatBitrate(it) {
  const br = it.bitrate ? Math.round(it.bitrate / 1000) + " kbps" : "";
  const loss = it.lossless ? "无损" : "";
  return [it.format, loss, br].filter(Boolean).join(" · ");
}
function dedupSize(n) {
  return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.round(n / 1024) + " KB";
}

function dedupSelection() {
  const out = [];
  $("#dedup-report").querySelectorAll(".dedup-check:checked").forEach((c) => {
    const g = dedupState.groups[+c.dataset.gi];
    if (g) out.push({ removeTrackId: g.remove[+c.dataset.ri].trackId, keepTrackId: g.keep.trackId });
  });
  return out;
}

function updateDedupCount() {
  $("#dedup-run").disabled = dedupSelection().length === 0;
}

function dedupRender() {
  const rep = $("#dedup-report");
  rep.innerHTML = "";
  if (!dedupState.groups.length) {
    rep.innerHTML = '<p class="muted">未发现重复歌曲</p>';
    updateDedupCount();
    return;
  }
  dedupState.groups.forEach((g, gi) => {
    const box = document.createElement("div");
    box.className = "dedup-group";
    let rows = `<div class="dedup-title">${escapeHtml(g.title)}<span class="muted"> · ${g.remove.length + 1} 个副本</span></div>` +
      `<div class="dedup-row keep">✓ 保留 · ${dedupFormatBitrate(g.keep)} · ${dedupSize(g.keep.size)}` +
      ` <span class="muted">${escapeHtml(g.keep.path)}</span></div>`;
    g.remove.forEach((r, ri) => {
      const refs = [];
      if (r.favCount) refs.push(`收藏×${r.favCount}`);
      if (r.plCount) refs.push(`歌单×${r.plCount}`);
      rows += `<label class="dedup-row"><input type="checkbox" class="dedup-check" data-gi="${gi}" data-ri="${ri}" checked>` +
        ` 删除 · ${dedupFormatBitrate(r)} · ${dedupSize(r.size)}` +
        (refs.length ? ` <b class="dedup-ref">♥ ${refs.join(" / ")}（将迁移）</b>` : "") +
        ` <span class="muted">${escapeHtml(r.path)}</span></label>`;
    });
    box.innerHTML = rows;
    rep.appendChild(box);
  });
  updateDedupCount();
}

$("#dedup-scan").addEventListener("click", async () => {
  const btn = $("#dedup-scan");
  btn.disabled = true;
  btn.textContent = "扫描中…";
  try {
    const body = await matchApi("/api/match/duplicates");
    dedupState.groups = body.data.groups || [];
    $("#dedup-summary").textContent = `发现 ${body.data.totalGroups} 组重复，共 ${body.data.totalExtra} 个多余副本`;
    dedupRender();
  } catch (e) {
    toast(e.message, "fail");
  }
  btn.disabled = false;
  btn.textContent = "扫描重复";
});
$("#dedup-report").addEventListener("change", (e) => {
  if (e.target.classList && e.target.classList.contains("dedup-check")) updateDedupCount();
});
$("#dedup-run").addEventListener("click", async () => {
  const items = dedupSelection();
  if (!items.length) return;
  if (!confirm(`确认清理 ${items.length} 个重复文件？\n将移入回收站（可手动找回），收藏/歌单引用自动迁移到保留副本。`)) return;
  const btn = $("#dedup-run");
  btn.disabled = true;
  btn.textContent = "清理中…";
  try {
    const body = await matchApi("/api/match/dedup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    const d = body.data;
    const bad = (d.results || []).filter((r) => !r.ok);
    toast(`清理完成：成功 ${d.ok}/${d.total}` + (bad.length ? `（${bad[0].error || "错误"}）` : ""),
      d.ok === d.total ? "ok" : "fail");
    $("#dedup-scan").click();
  } catch (e) {
    toast(e.message, "fail");
    btn.disabled = false;
    btn.textContent = "清理选中（移回收站）";
  }
});

/* -------------------------------------------------------------- 表单脏标记 */
["#tee-dir", "#tee-max", "#llm-base", "#llm-key", "#llm-model", "#lx-url", "#search-timeout", "#bind-timeout", "#handoff-max", "#scan-path"].forEach((sel) =>
  $(sel).addEventListener("input", () => markDirty()));
$$("input[name=quality]").forEach((el) => el.addEventListener("change", () => markDirty("音质偏好需保存后生效")));
["#recommend-hot", "#recommend-daily", "#history-playlist", "#public-playlists-enabled", "#search-probe", "#app-v2-search", "#search-log-query", "#tee-enabled", "#fav-download-enabled", "#auto-cover", "#lyric-auto-dl", "#netease-my-playlists", "#netease-subscribed", "#netease-resident"].forEach((sel) =>
  $(sel).addEventListener("change", () => markDirty()));

// 常驻开关可用态：账号歌单，或「公共歌单已开且选了网易歌单」任一成立即可勾选；
// 两者都无需求时置灰（已勾选状态保留，重开即恢复）
function updateResidentAvailability() {
  const box = $("#netease-resident");
  if (!box) return;
  const needed = $("#netease-my-playlists").checked
    || ($("#public-playlists-enabled").checked && publicMusicboxSelected());
  box.disabled = !needed;
  if (!needed) box.checked = false;
}
$("#netease-my-playlists").addEventListener("change", updateResidentAvailability);

function updateTeeCountLabel() {
  const n = $("#tee-max").value || configValues.FNMUSIC_TEE_CACHE_MAX || "2";
  $("#tee-count-label").textContent = `（缓存数 ${n} 首）`;
}
$("#tee-max").addEventListener("input", updateTeeCountLabel);

function updateBindTimeoutLabel() {
  const n = $("#bind-timeout").value || configValues.FNMUSIC_OFFICIAL_BIND_TIMEOUT_S || "120";
  $("#bind-timeout-label").textContent = `（${n} 秒）`;
}
$("#bind-timeout").addEventListener("input", updateBindTimeoutLabel);

window.addEventListener("beforeunload", (ev) => {
  if (dirty) ev.preventDefault();
});

/* -------------------------------------------------------------- 关于 / 检查更新 */
let updateState = null;   // /api/update-check 的最近结果（有更新时非空）
let aboutLoaded = false;

function setUpdateBadge(on) {
  const badge = $("#about-new");
  if (badge) badge.hidden = !on;
}

function openUpdateModal() {
  if (!updateState || !updateState.latest) return;
  const latest = updateState.latest;
  $("#update-title").textContent = `发现新版本 ${latest.version || ""}`.trim();
  const meta = [];
  if (latest.published_at) meta.push(`发布于 ${String(latest.published_at).slice(0, 10)}`);
  meta.push(`当前版本 ${updateState.current_version || "—"}`);
  meta.push(updateState.source === "gitee" ? "检查源 Gitee" : "检查源 GitHub");
  $("#update-meta").textContent = meta.join(" · ");
  // changelog 由后端清洗成纯文本，这里用 textContent 注入，天然防 XSS
  $("#update-log").textContent = latest.changelog || "（该发行版未填写说明）";
  // 下载源自选（2026-10-22 用户需求）：GitHub / Gitee 两个 Release 页，
  // tag 路径两平台一致；mirrors 缺失时退回主源 html_url 单链接
  const mirrors = Array.isArray(latest.mirrors) && latest.mirrors.length
    ? latest.mirrors : [{ name: "GitHub", url: latest.html_url || "#" }];
  const go = $("#update-go");
  const goGitee = $("#update-go-gitee");
  const gh = mirrors.find((m) => /github/i.test(m.name || "")) || mirrors[0];
  const gitee = mirrors.find((m) => /gitee/i.test(m.name || ""));
  go.href = gh.url || "#";
  go.textContent = `${gh.name || "GitHub"} 下载`;
  if (gitee && gitee.url) {
    goGitee.href = gitee.url;
    goGitee.textContent = `${gitee.name || "Gitee"} 下载`;
    goGitee.hidden = false;
  } else {
    goGitee.hidden = true;
  }
  $("#update-modal").hidden = false;
}

function closeUpdateModal() {
  $("#update-modal").hidden = true;
}

async function loadUpdateCheck() {
  try {
    const res = await api("/api/update-check");
    updateState = res && res.has_update ? res : null;
    // 红点只跟随结果：查到新版本常亮，检查失败静默（不打扰用户）
    setUpdateBadge(Boolean(updateState));
    const note = $("#about-check-note");
    if (note) {
      note.textContent = res && res.error
        ? `检查更新失败（已静默）：${res.error}`
        : (res && res.has_update ? `最新版本 ${res.latest.version}，点击版本号旁的 NEW 查看详情` : "已是最新版本");
    }
  } catch (_) {
    /* 检查更新只是锦上添花：任何失败都静默降级 */
  }
}

async function loadAbout() {
  try {
    const about = await api("/api/about");
    $("#about-version").textContent = `v${about.version}`;
    $("#about-deploy").textContent = `部署形态：${about.deploy_mode === "native" ? "原生（systemd）" : "Docker"}`;
    const links = [
      ["项目仓库 · GitHub", about.repo_url],
      ["项目仓库 · Gitee", about.repo_url_gitee],
      ["问题反馈 · GitHub", about.issues_url],
      ["问题反馈 · Gitee", about.issues_url_gitee],
      ["作者", about.author_url],
    ].filter(([, url]) => url);
    $("#about-links").innerHTML = links.map(([label, url]) =>
      `<a class="state-line" href="${url}" target="_blank" rel="noopener noreferrer">${label} · ${url}</a>`
    ).join("") + `<span class="state-line muted">${about.author || ""}</span>`;
    aboutLoaded = true;
  } catch (err) {
    toast(`关于信息加载失败：${err.message || err}`, "err");
  }
  await loadUpdateCheck();
}

$("#about-new").addEventListener("click", openUpdateModal);
["#update-close", "#update-later"].forEach((sel) => $(sel).addEventListener("click", closeUpdateModal));
$("#update-modal").addEventListener("click", (ev) => {
  if (ev.target === $("#update-modal")) closeUpdateModal();
});

/* -------------------------------------------------------------- 小爱音箱（xiaomusic 桥接） */
/* 三步向导 + 状态页：全部经 WebUI 的 /api/xiaomusic/* 代理到同容器桥接（:8005）。 */
let xmState = null;
let xmQrTimer = null;
let xmHealthTimer = null;
let xmLoadSeq = 0;
let xmGateway = "";  // 容器网关（health.gateway）：音箱不可达，禁止填成它


function xmNasPrefill() {
  const v = xmState.nas_ip || "";
  if (v && xmGateway && v === xmGateway) return "";
  return v;
}
const XM_STEPS = ["小米账号", "选择音箱", "飞牛音乐", "完成"];

function stopXmPolling() {
  if (xmQrTimer) { clearInterval(xmQrTimer); xmQrTimer = null; }
  if (xmHealthTimer) { clearInterval(xmHealthTimer); xmHealthTimer = null; }
}

async function loadXmPage() {
  stopXmPolling();
  const body = $("#xm-body");
  if (configValues.FNMUSIC_XIAOMUSIC_ENABLED !== "true") {
    body.innerHTML = `<div class="card"><div class="muted">尚未开启。请勾选上方「接管小爱音箱音乐播放」并保存，桥接进程会自动拉起。</div></div>`;
    return;
  }
  body.innerHTML = `<div class="card"><div class="muted">正在连接桥接服务…</div></div>`;
  const seq = ++xmLoadSeq;
  // 进程刚拉起时 xiaomusic 初始化要点时间：最多重试约 15s
  for (let i = 0; i < 10; i++) {
    let st = null;
    try { st = await api("/api/xiaomusic/status"); } catch (_) { st = null; }
    if (seq !== xmLoadSeq) return;  // 已切走页面
    if (st && st.ok) { xmRender(st); return; }
    await new Promise((r) => setTimeout(r, 1500));
  }
  body.innerHTML = `<div class="card">
      <div class="muted">桥接服务暂时不可达（可能仍在启动或已停止）。可重试，或到「概览 → 进程状态」查看 xiaomusic 进程。</div>
      <div class="btn-row"><button class="btn" id="xm-retry">重新加载</button></div>
    </div>`;
  $("#xm-retry").addEventListener("click", loadXmPage);
}

function xmStepsHtml(step) {
  return `<div class="xm-steps">` + XM_STEPS.map((lb, i) => {
    const n = i + 1;
    // step=4 即完成页：全部步骤（含"完成"）都点亮为绿色对勾
    const done = n < step || step >= XM_STEPS.length;
    const cls = done ? "done" : (n === step ? "active" : "");
    return `<div class="step ${cls}"><span class="num">${done ? "✓" : n}</span>${lb}</div>`
      + (i < XM_STEPS.length - 1 ? `<div class="bar"></div>` : "");
  }).join("") + `</div>`;
}

function xmRender(st) {
  xmState = st.wizard || {};
  xmGateway = st.gateway || "";
  const step = xmState.step || 1;
  let html = `<div class="card"><div class="card-title">配置向导</div>${xmStepsHtml(step)}</div>`;
  if (step === 1) html += xmStep1Html();
  else if (step === 2) html += xmStep2Html();
  else if (step === 3) html += xmStep3Html();
  else html += xmDoneHtml(st);
  $("#xm-body").innerHTML = html;
  xmBind(step, st);
}

/* ---------- Step1 小米登录 ---------- */
function xmStep1Html() {
  return `<div class="card">
    <div class="card-title">登录小米账号</div>
    <p class="hint">桥接通过小米账号接管小爱音箱（口令轮询与播放控制都经小米云）。支持账号密码或米家 App 扫码。</p>
    <div class="xm-tabs">
      <div class="tab active" id="xm-tab-pwd">账号密码</div>
      <div class="tab" id="xm-tab-qr">扫码登录</div>
    </div>
    <div id="xm-pane-pwd">
      <label class="field"><span>小米账号（手机号/邮箱）</span>
        <input class="input" id="xm-mi-account" placeholder="请输入小米账号" autocomplete="username"></label>
      <label class="field"><span>密码</span>
        <input class="input" id="xm-mi-password" type="password" placeholder="请输入密码" autocomplete="current-password"></label>
      <div class="btn-row"><button class="btn primary" id="xm-mi-login">登录</button></div>
    </div>
    <div id="xm-pane-qr" hidden>
      <div class="qr-area">
        <img id="xm-qr-img" alt="登录二维码" hidden>
        <div id="xm-qr-status" class="muted">正在获取二维码…</div>
      </div>
    </div>
    <div class="xm-msg" id="xm-step1-msg" hidden></div>
  </div>`;
}

/* ---------- Step2 设备勾选 ---------- */
function xmStep2Html() {
  return `<div class="card">
    <div class="card-title">选择小爱音箱</div>
    <p class="hint">勾选要接管的小爱音箱（可多选），口令对勾选的音箱生效。</p>
    <div id="xm-device-list"><div class="muted">正在加载设备列表…</div></div>
    <div class="btn-row">
      <button class="btn" id="xm-device-refresh">刷新设备</button>
      <button class="btn primary" id="xm-device-save">下一步</button>
    </div>
    <div class="xm-msg" id="xm-step2-msg" hidden></div>
  </div>`;
}

function xmDeviceListHtml(devices, selected) {
  if (!devices || !devices.length) {
    return `<div class="muted">未获取到设备。请确认小米账号登录成功，或点「刷新设备」重试。</div>`;
  }
  return devices.map((d) => {
    // did 必须是纯数字；取不到 did 的设备不可勾选（mi_did 存 UUID 会失配）
    const did = String(d.did || "");
    if (!did) {
      return `<div class="xm-device" style="opacity:.55;cursor:not-allowed">
      <input type="checkbox" disabled>
      <div class="min">
        <div class="name">${escapeHtml(d.name || d.deviceID || "未知设备")}</div>
        <div class="hw">标识获取失败，请点「刷新设备」重试</div>
      </div>
    </div>`;
    }
    const on = selected.includes(did) ? " selected" : "";
    return `<div class="xm-device${on}" data-did="${escapeHtml(did)}">
      <input type="checkbox" ${on ? "checked" : ""}>
      <div class="min">
        <div class="name">${escapeHtml(d.name || d.deviceID || "未知设备")}</div>
        <div class="hw">型号 ${escapeHtml(d.hardware || "未知")}</div>
      </div>
    </div>`;
  }).join("");
}

/* ---------- Step3 飞牛音乐 ---------- */
function xmStep3Html() {
  const fn = (xmState.fnmusic) || {};
  return `<div class="card">
    <div class="card-title">连接飞牛音乐</div>
    <p class="hint">自动扫描 NAS 上的飞牛音乐服务，然后填写登录账号（与飞牛音乐 App 同一账号）。</p>
    <div id="xm-scan-result"><div class="muted">正在扫描…</div></div>
    <label class="field"><span>飞牛音乐地址</span>
      <input class="input" id="xm-fn-url" placeholder="如 http://192.168.x.x:5666（扫描自动填写）" value="${escapeHtml(fn.fnmusic_base_url || "")}"></label>
    <label class="field"><span>音箱访问本机的地址（NAS 局域网 IP，不含端口）</span>
      <input class="input" id="xm-nas-ip" placeholder="如 192.168.x.x" value="${escapeHtml(xmNasPrefill())}"></label>
    <p class="hint">决定小爱音箱拉流地址：必须是 NAS 的局域网 IP（与音箱同一网段）${xmGateway ? `，不能是容器网关 ${escapeHtml(xmGateway)}` : ""}。</p>
    <label class="field"><span>飞牛音乐账号</span>
      <input class="input" id="xm-fn-username" placeholder="飞牛音乐登录账号" value="${escapeHtml(fn.fnmusic_username || "")}"></label>
    <label class="field"><span>密码</span>
      <input class="input" id="xm-fn-password" type="password" placeholder="飞牛音乐密码"></label>
    <div class="btn-row"><button class="btn primary" id="xm-fn-save">完成，开始使用</button></div>
    <div class="xm-msg" id="xm-step3-msg" hidden></div>
  </div>`;
}

/* ---------- 完成页 ---------- */
function xmDoneHtml(st) {
  return `<div class="card">
    <div class="card-title">配置完成</div>
    <div class="xm-health">
      <div class="xm-health-item" id="xm-h-poll">
        <span class="dot"></span><span class="txt">口令轮询：检测中…</span>
      </div>
      <div class="xm-health-item" id="xm-h-mi">
        <span class="dot"></span><span class="txt">小米账号：检测中…</span>
      </div>
    </div>
    <p class="hint">现在可以对小爱音箱说「播放收藏」随机播放飞牛音乐收藏，或者说「播放晴天」直接点歌。收藏有增减时约 5 分钟自动同步。</p>
    <div class="xm-status-grid">
      <div class="xm-status-cell"><div class="v" id="xm-d-fav">-</div><div class="k">收藏歌曲</div></div>
      <div class="xm-status-cell"><div class="v" id="xm-d-pl">-</div><div class="k">歌单歌曲（乱序）</div></div>
      <div class="xm-status-cell"><div class="v" id="xm-d-push">-</div><div class="k">歌单推送</div></div>
      <div class="xm-status-cell"><div class="v" id="xm-d-err">-</div><div class="k">最近错误</div></div>
    </div>
    <table class="xm-cmd-table">
      <tr><td>播放收藏 / 播放本地</td><td>随机播放收藏（每次重新打乱）</td></tr>
      <tr><td>播放本地收藏歌曲 / 播放本地歌曲 / 播放本地音乐</td><td>同上</td></tr>
      <tr><td>放几首本地歌 / 放首本地歌 / 随便放几首歌 / 来几首本地歌</td><td>同上（口语说法都支持）</td></tr>
      <tr><td>播放晴天 / 播放周杰伦的晴天</td><td><b>点歌</b>：收藏里有直接播；没有则本地曲库直接播 / 在线源<b>下载</b>到本地音乐库（<b>都不进收藏</b>）后播放</td></tr>
      <tr><td>下一首 / 上一首 / 暂停 / 停止播放</td><td>播放控制</td></tr>
      <tr><td>停下来吧 / 别放了 / 别播了 / 安静 / 停一下 / 闭嘴</td><td>停止播放（口语说法也行；停止后不会自动续播）</td></tr>
      <tr><td>随机播放 / 全部循环 / 单曲循环 / 顺序播放</td><td>切换播放模式</td></tr>
      <tr><td>刷新列表</td><td>重新加载歌单</td></tr>
    </table>
    <p class="hint">💡 点播的歌自然播完且没有后续指令时，会自动接播收藏随机歌单；期间说了任何新指令（下一首 / 停止 / 新点歌）则不会接管。</p>
    <div class="btn-row">
      <button class="btn" id="xm-sync">立即同步</button>
      <button class="btn" id="xm-reset">重新配置</button>
    </div>
    <div class="xm-msg" id="xm-done-msg" hidden></div>
  </div>`;
}

/* ---------- 绑定与动作 ---------- */
function xmMsg(id, kind, text) {
  const el = $(id);
  if (!el) return;
  el.hidden = false;
  el.className = "xm-msg " + kind;
  el.textContent = text;
}

function xmBind(step, st) {
  if (step === 1) {
    $("#xm-tab-pwd").addEventListener("click", () => xmSwitchTab("pwd"));
    $("#xm-tab-qr").addEventListener("click", () => xmSwitchTab("qr"));
    $("#xm-mi-login").addEventListener("click", xmMiLogin);
  } else if (step === 2) {
    $("#xm-device-refresh").addEventListener("click", () => xmLoadDevices());
    $("#xm-device-save").addEventListener("click", xmSaveDevices);
    $$("#xm-device-list .xm-device").forEach((el) =>
      el.addEventListener("click", () => el.classList.toggle("selected")));
    xmLoadDevices();
  } else if (step === 3) {
    $("#xm-fn-save").addEventListener("click", xmSaveFnmusic);
    xmScan();
  } else {
    $("#xm-sync").addEventListener("click", xmSyncNow);
    $("#xm-reset").addEventListener("click", xmResetWizard);
    xmRefreshDone(st);
    xmHealthTimer = setInterval(async () => {
      try {
        const fresh = await api("/api/xiaomusic/status");
        if (fresh && fresh.ok) { xmRefreshDone(fresh); }
      } catch (_) { /* 忽略单次失败 */ }
    }, 5000);
  }
}

function xmSwitchTab(t) {
  $("#xm-tab-pwd").classList.toggle("active", t === "pwd");
  $("#xm-tab-qr").classList.toggle("active", t === "qr");
  $("#xm-pane-pwd").hidden = t !== "pwd";
  $("#xm-pane-qr").hidden = t !== "qr";
  if (t === "qr") xmStartQr();
  else stopXmQr();
}

function stopXmQr() {
  if (xmQrTimer) { clearInterval(xmQrTimer); xmQrTimer = null; }
}

async function xmMiLogin() {
  const msg = "#xm-step1-msg";
  const account = $("#xm-mi-account").value.trim();
  const password = $("#xm-mi-password").value;
  if (!account || !password) { xmMsg(msg, "err", "账号和密码不能为空"); return; }
  const btn = $("#xm-mi-login");
  btn.disabled = true; btn.textContent = "登录中…";
  try {
    const r = await api("/api/xiaomusic/wizard/mi_login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account, password }),
    });
    xmMsg(msg, "ok", "登录成功，正在加载设备列表…");
    setTimeout(() => xmGotoStep2(r.devices || []), 600);
  } catch (exc) {
    xmMsg(msg, "err", "登录失败：" + (exc.message || exc));
    btn.disabled = false; btn.textContent = "登录";
  }
}

async function xmStartQr() {
  stopXmQr();
  $("#xm-qr-img").hidden = true;
  $("#xm-qr-status").textContent = "正在获取二维码…";
  try {
    const r = await api("/api/xiaomusic/wizard/qrcode");
    if (r.already_logged_in) {
      $("#xm-qr-status").textContent = "已登录，正在加载设备…";
      setTimeout(() => xmGotoStep2(r.devices || []), 400);
      return;
    }
    if (r.ok && r.qrcode_url) {
      $("#xm-qr-img").src = r.qrcode_url;
      $("#xm-qr-img").hidden = false;
      $("#xm-qr-status").textContent = "请使用「米家 App」扫码登录";
      xmPollQr(r.expire_seconds || 120);
    } else {
      $("#xm-qr-status").textContent = "二维码获取失败：" + (r.message || "未知错误");
    }
  } catch (exc) {
    $("#xm-qr-status").textContent = "二维码获取失败：" + (exc.message || exc);
  }
}

function xmPollQr(expire) {
  stopXmQr();
  let elapsed = 0;
  xmQrTimer = setInterval(async () => {
    elapsed += 2;
    if (elapsed > expire + 10) { xmStartQr(); return; }
    try {
      const r = await api("/api/xiaomusic/wizard/qrcode_status");
      if (r.status === "success") {
        stopXmQr();
        $("#xm-qr-status").textContent = "扫码成功！正在加载设备列表…";
        setTimeout(() => xmGotoStep2(r.devices || []), 600);
      }
    } catch (_) { /* 继续轮询 */ }
  }, 2000);
}

async function xmGotoStep2(devices) {
  // 直接渲染第二步（不等整页状态刷新，扫码/登录响应里已带设备）
  xmState = Object.assign({}, xmState, { step: 2 });
  const html = `<div class="card"><div class="card-title">配置向导</div>${xmStepsHtml(2)}</div>` + xmStep2Html();
  $("#xm-body").innerHTML = html;
  const box = $("#xm-device-list");
  box.innerHTML = xmDeviceListHtml(devices, []);
  $("#xm-device-refresh").addEventListener("click", () => xmLoadDevices());
  $("#xm-device-save").addEventListener("click", xmSaveDevices);
  $$("#xm-device-list .xm-device").forEach((el) =>
    el.addEventListener("click", () => el.classList.toggle("selected")));
}

async function xmLoadDevices() {
  const box = $("#xm-device-list");
  if (!box) return;
  box.innerHTML = `<div class="muted">正在加载设备列表…</div>`;
  try {
    const r = await api("/api/xiaomusic/wizard/devices");
    const selected = $$("#xm-device-list .xm-device.selected").map((el) => el.dataset.did);
    box.innerHTML = xmDeviceListHtml(r.devices || [], selected);
    $$("#xm-device-list .xm-device").forEach((el) =>
      el.addEventListener("click", () => el.classList.toggle("selected")));
  } catch (exc) {
    box.innerHTML = `<div class="muted">设备列表加载失败：${escapeHtml(exc.message || exc)}</div>`;
  }
}

async function xmSaveDevices() {
  const msg = "#xm-step2-msg";
  const dids = $$("#xm-device-list .xm-device.selected").map((el) => el.dataset.did);
  if (!dids.length) { xmMsg(msg, "err", "请至少勾选一台音箱"); return; }
  const btn = $("#xm-device-save");
  btn.disabled = true; btn.textContent = "保存中…";
  try {
    await api("/api/xiaomusic/wizard/devices", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dids }),
    });
    loadXmPage();  // 回到 step3
  } catch (exc) {
    xmMsg(msg, "err", "保存失败：" + (exc.message || exc));
    btn.disabled = false; btn.textContent = "下一步";
  }
}

async function xmScan() {
  const box = $("#xm-scan-result");
  try {
    const r = await api("/api/xiaomusic/wizard/scan_fnmusic");
    const found = r.found || [];
    if (found.length) {
      box.innerHTML = found.map((u, i) =>
        `<div class="xm-scan${i === 0 ? " selected" : ""}" data-url="${escapeHtml(u)}">
           <span class="dot ok"></span>${escapeHtml(u)}</div>`).join("");
      $$("#xm-scan-result .xm-scan").forEach((el) =>
        el.addEventListener("click", () => {
          $$("#xm-scan-result .xm-scan").forEach((x) => x.classList.remove("selected"));
          el.classList.add("selected");
          $("#xm-fn-url").value = el.dataset.url;
        }));
      $("#xm-fn-url").value = found[0];
    } else {
      box.innerHTML = `<div class="muted">未自动发现飞牛音乐（ scanned: ${escapeHtml((r.scanned || []).join("、"))} ）。请确认飞牛音乐正在运行，或手动填写地址。</div>`;
    }
  } catch (exc) {
    box.innerHTML = `<div class="muted">扫描失败：${escapeHtml(exc.message || exc)}，请手动填写。</div>`;
  }
}

async function xmSaveFnmusic() {
  const msg = "#xm-step3-msg";
  const base_url = $("#xm-fn-url").value.trim().replace(/\/+$/, "");
  const username = $("#xm-fn-username").value.trim();
  const password = $("#xm-fn-password").value;
  const nas_ip = $("#xm-nas-ip").value.trim();
  if (!base_url || !username || !password) { xmMsg(msg, "err", "请填写完整的地址、账号和密码"); return; }
  if (!nas_ip) { xmMsg(msg, "err", "请填写音箱访问本机的地址（NAS 局域网 IP）"); return; }
  if (xmGateway && nas_ip === xmGateway) {
    xmMsg(msg, "err", `不能填写容器网关 ${nas_ip}：小爱音箱无法访问该地址，请填 NAS 的局域网 IP（如 192.168.x.x）`);
    return;
  }
  const btn = $("#xm-fn-save");
  btn.disabled = true; btn.textContent = "验证并保存…";
  try {
    const r = await api("/api/xiaomusic/wizard/fnmusic", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url, username, password, nas_ip }),
    });
    let note = `配置成功！已获取 ${r.favorites} 首收藏，歌单生成中…`;
    if (r.restarted) note += "（音箱访问地址已变更，桥接已自动重启）";
    xmMsg(msg, "ok", note);
    setTimeout(loadXmPage, 1200);
  } catch (exc) {
    xmMsg(msg, "err", "配置失败：" + (exc.message || exc));
    btn.disabled = false; btn.textContent = "完成，开始使用";
  }
}

function xmSetHealth(id, ok, text) {
  const el = $(id);
  if (!el) return;
  el.className = "xm-health-item " + (ok ? "ok" : "err");
  const t = el.querySelector(".txt");
  if (t) t.textContent = text;
}

function xmRefreshDone(st) {
  const set = (id, v, cls) => {
    const el = $(id);
    if (!el) return;
    el.textContent = v;
    if (cls) el.className = "v " + cls;
  };
  set("#xm-d-fav", st.favorites != null ? st.favorites : "-");
  set("#xm-d-pl", st.playlist_songs != null ? st.playlist_songs : "-");
  set("#xm-d-push", st.push_ok ? "正常" : "等待", st.push_ok ? "ok" : "err");
  const err = st.last_error || "";
  set("#xm-d-err", err ? (err.length > 12 ? err.slice(0, 12) + "…" : err) : "无", err ? "err" : "ok");

  // ① 口令轮询（桥接 enable_pull_ask）：false 时 poller 空转，语音指令永远到不了桥接
  const pollOn = st.pull_ask_on;
  const chan = st.ask_channel === "mina" ? "mina 通道" : "小爱对话记录通道";
  if (st.poll_degraded === true) {
    xmSetHealth("#xm-h-poll", false,
      "口令轮询：已暂停（小米登录失效）— 为免频繁请求被风控，暂不拉取对话记录；重新登录后自动恢复");
  } else if (pollOn === true) {
    xmSetHealth("#xm-h-poll", true, `口令轮询：正常（${chan}）`);
  } else if (pollOn === false) {
    xmSetHealth("#xm-h-poll", false,
      "口令轮询：未开启 — 请点「重新配置」重走向导，或重启小爱音箱进程");
  } else {
    xmSetHealth("#xm-h-poll", false, "口令轮询：状态未知（桥接未返回 pull_ask_on，可能未启动）");
  }

  // ② 小米账号登录态：失效时口令无法经小米云下发到音箱
  if (st.mi_logged === true) {
    xmSetHealth("#xm-h-mi", true, "小米账号：登录有效，口令可正常下发到音箱");
  } else if (st.mi_logged === false) {
    xmSetHealth("#xm-h-mi", false,
      "小米账号：登录已失效 — 口令无法下发，请点下方「重新配置」重新扫码登录");
  } else {
    xmSetHealth("#xm-h-mi", false, "小米账号：状态未知（桥接未返回 mi_logged）");
  }
}

async function xmSyncNow() {
  const msg = "#xm-done-msg";
  try {
    await api("/api/xiaomusic/sync", { method: "POST" });
    xmMsg(msg, "ok", "已触发同步");
    const fresh = await api("/api/xiaomusic/status");
    if (fresh && fresh.ok) xmRefreshDone(fresh);
  } catch (exc) {
    xmMsg(msg, "err", exc.message || exc);
  }
}

async function xmResetWizard() {
  if (!confirm("确定要完全重新配置吗？将退出小米账号登录，需要重新登录并重新勾选设备。")) return;
  try {
    const r = await api("/api/xiaomusic/wizard/reset", { method: "POST" });
    if (r.restarted) toast("已完全重置，桥接正在重启，稍后从第一步重新配置", "ok");
    else toast("已重置" + (r.restart_error ? "（桥接重启失败：" + r.restart_error + "）" : ""), r.restart_error ? "fail" : "ok");
    // 进程重启需要几秒：loadXmPage 自带重试，等它就绪后再拉状态
    setTimeout(loadXmPage, 3000);
  } catch (exc) {
    toast("重置失败：" + (exc.message || exc), "fail");
  }
}

/* -------------------------------------------------------------- 启动 */
(async function boot() {
  await loadConfig();
  await loadStatus();
  initTheme().catch(() => {});  // 外观先应用（内部等宿主 SDK，不阻塞配置加载）
  await loadPlatforms(false);  // 页面加载不自动拉起预览进程，等用户点选音源
  setInterval(loadStatus, 15000);
  loadUpdateCheck();           // 进管理页查一次（后端 6h 缓存，不轮询、不烧限额）
})();

// ---------------- 歌单导入（网易云/QQ → 本地曲库 + 官方歌单） ----------------
const piState = { data: null, taskId: "", timer: null };

async function piApi(path, opts) {
  const resp = await fetch(`/api/match/${path}`, {
    headers: { "Content-Type": "application/json" }, ...opts,
  });
  return resp.json();
}

function piRenderProgress(t) {
  if (!t) return;
  const pct = t.total ? Math.round((t.done / t.total) * 100) : 0;
  $("#pi-progress").textContent = `[${t.state}] ${t.done}/${t.total} 成功 ${t.ok} · 跳过 ${t.skipped} · 失败 ${(t.failed || []).length} ${t.current ? "· " + t.current : ""} (${pct}%)`;
  $("#pi-errors").textContent = (t.failed || []).slice(0, 8)
    .map((f) => `${f.title} - ${f.artist}: ${f.reason}`).join("；") || "";
  $("#pi-refresh").disabled = t.state !== "running";
  if (t.state === "running") {
    piState.timer = setTimeout(() => piPoll(), 2000);
  } else {
    piState.timer = null;
  }
}

async function piPoll() {
  if (!piState.taskId) return;
  try {
    const data = await piApi(`import/task/${piState.taskId}`);
    if (data.code === 0) piRenderProgress(data.data);
  } catch (_) { /* 网关短暂不可达，等下一轮 */ }
}

$("#pi-preview").addEventListener("click", async () => {
  const input = $("#pi-input").value.trim();
  if (!input) { $("#pi-summary").textContent = "请先输入歌单链接或 ID"; return; }
  $("#pi-summary").textContent = "解析中…";
  const platform = $("#pi-platform").value;
  try {
    const data = await piApi(`import/preview?input=${encodeURIComponent(input)}&platform=${platform}`);
    if (data.code !== 0) { $("#pi-summary").textContent = data.msg || "解析失败"; return; }
    piState.data = data.data;
    $("#pi-summary").textContent = `歌单「${data.data.name}」（${data.data.platform}）共 ${data.data.tracks.length} 首：` +
      data.data.tracks.slice(0, 5).map((t) => `${t.title} - ${t.artist}`).join("；") + "…";
    $("#pi-run").disabled = false;
  } catch (e) {
    $("#pi-summary").textContent = `解析失败: ${e}`;
  }
});

async function piLoadTargets() {
  const data = await piApi("import/playlists");
  const sel = $("#pi-target");
  sel.innerHTML = '<option value="">— 选择目标歌单 —</option>';
  if (data.code !== 0) {
    const opt = document.createElement("option");
    opt.textContent = data.msg || "拉取失败";
    sel.appendChild(opt);
    return;
  }
  for (const p of data.data || []) {
    const opt = document.createElement("option");
    opt.value = p.guid;
    opt.textContent = `${p.name} (${p.trackCount}首)`;
    sel.appendChild(opt);
  }
}

$("#pi-reload-target").addEventListener("click", piLoadTargets);
$$('[data-page="pimport"]').forEach((btn) => btn.addEventListener("click", piLoadTargets));

$("#pi-run").addEventListener("click", async () => {
  const target = $("#pi-target").value;
  if (!piState.data) { $("#pi-progress").textContent = "请先预览歌单"; return; }
  if (!target && !($("#pi-new-name").value || "").trim()) { $("#pi-progress").textContent = "请选择目标歌单或填写新建歌单名称"; return; }
  $("#pi-run").disabled = true;
  $("#pi-progress").textContent = "启动导入…";
  const platform = $("#pi-platform").value;
  const resp = await fetch("/api/match/import/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input: $("#pi-input").value.trim(), platform,
    targetGuid: target, targetNew: ($("#pi-new-name").value || "").trim() }),
  });
  const data = await resp.json();
  if (data.code !== 0) { $("#pi-progress").textContent = data.msg || "启动失败"; $("#pi-run").disabled = false; return; }
  piState.taskId = data.data.taskId;
  $("#pi-refresh").disabled = false;
  piPoll();
});

$("#pi-refresh").addEventListener("click", () => { if (piState.timer) return; piPoll(); });
