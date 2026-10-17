/* fnmusic-ext WebUI — 原生 JS，无框架无外部资产。 */
"use strict";

// 飞牛桌面用 HTTPS 打开管理窗，页面必须挂在同源路径 /app/fnmusic-ext 下。
// WebUI 自己也会剥掉这个前缀。管理接口只认飞牛网关注入的管理员头。
const APP_BASE = "/app/fnmusic-ext";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const PROVIDER_LABEL = { musicdl: "musicdl 聚合音源", musicbox: "网易云音乐盒子", lxmusic: "洛雪自定义源", none: "未配置" };
const PROC_LABEL = { musicdl: "musicdl", musicbox: "musicbox", lxmusic: "lxmusic", webui: "WebUI" };

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
}
$$("[data-page]").forEach((btn) => btn.addEventListener("click", () => switchPage(btn.dataset.page)));

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
  // 公共歌单：选择串只存 {provider,source,kind,id}，_name/_cover_url 为展示用临时字段
  $("#public-playlists-enabled").checked = v.FNMUSIC_PUBLIC_PLAYLISTS_ENABLED === "true";
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
    FNMUSIC_TEE_SAVE_ENABLED: $("#tee-enabled").checked,
    FNMUSIC_FAVORITE_DOWNLOAD_ENABLED: $("#fav-download-enabled").checked,
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
async function musicdlTest() {
  const q = $("#musicdl-test-q").value.trim();
  const box = $("#musicdl-test-result");
  if (!q) return toast("请输入歌名", "fail");
  box.hidden = false;
  box.className = "report";
  box.textContent = "测试中…（部分慢平台可能要等十几秒）";
  const btn = $("#musicdl-test-run");
  btn.disabled = true;
  try {
    const body = await api("/api/musicdl/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keyword: q, sources: platforms.enabled }),
    });
    const rows = (body.platforms || []).map((p) => {
      const ok = p.count > 0;
      const head = ok ? `✓ ${p.count} 条：${(p.samples || []).join(" / ")}` : `✗ ${p.error || "无结果"}`;
      return `<div class="kv" style="color:${ok ? "var(--ok)" : "var(--err)"}"><b>${escapeHtml(p.source)}</b> ${escapeHtml(head)}</div>`;
    });
    box.innerHTML = rows.join("") || '<div class="kv muted">没有勾选任何平台</div>';
    const bad = !(body.platforms || []).length || (body.platforms || []).some((p) => !p.count);
    box.className = "report " + (bad ? "fail" : "ok");
  } catch (e) {
    box.className = "report fail";
    box.textContent = `测试失败：${e.message}`;
  } finally {
    btn.disabled = false;
  }
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
["#recommend-hot", "#recommend-daily", "#public-playlists-enabled", "#search-probe", "#app-v2-search", "#search-log-query", "#tee-enabled", "#fav-download-enabled", "#auto-cover", "#lyric-auto-dl", "#netease-my-playlists", "#netease-subscribed", "#netease-resident"].forEach((sel) =>
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
  ["#about-dot", "#about-dot-m"].forEach((sel) => {
    const dot = $(sel);
    if (dot) dot.hidden = !on;
  });
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
  const go = $("#update-go");
  go.href = latest.html_url || "#";
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
      ["项目仓库", about.repo_url],
      ["问题反馈", about.issues_url],
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

/* -------------------------------------------------------------- 启动 */
(async function boot() {
  await loadConfig();
  await loadStatus();
  await loadPlatforms(false);  // 页面加载不自动拉起预览进程，等用户点选音源
  setInterval(loadStatus, 15000);
  loadUpdateCheck();           // 进管理页查一次（后端 6h 缓存，不轮询、不烧限额）
})();
