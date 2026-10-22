/* static/app.js 的 Node 行为测试 —— 零 npm 依赖，node 原生 assert。
 *
 * 背景：musicbox CLI 的 auth 接口返回 {ok, data:{...}} 信封结构（与
 * netease_login.sh / proxy/tests/test_musicbox_service.py 的契约一致），
 * 前端 pollQr/checkQrStatus 必须从 data.code 取扫码状态码。
 * 本文件用最小 DOM/fetch 桩直接加载 app.js 驱动真实函数，防止信封解析回归。
 */
"use strict";

const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* ------------------------------------------------ 最小 DOM/浏览器桩 -------- */
function stubEl() {
  const classes = new Set();
  const listeners = new Map();
  return {
    hidden: false, textContent: "", innerHTML: "", className: "", value: "",
    checked: false, disabled: false, src: "", dataset: {}, _t: null,
    classList: {
      add(...c) { c.forEach((x) => classes.add(x)); },
      remove(...c) { c.forEach((x) => classes.delete(x)); },
      toggle(c, force) {
        if (force === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); }
        else if (force) { classes.add(c); }
        else { classes.delete(c); }
      },
      contains(c) { return classes.has(c); },
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    dispatch(type, event = {}) {
      for (const fn of listeners.get(type) || []) fn(event);
    },
    querySelectorAll() { return []; },
  };
}

const els = new Map();

global.document = {
  querySelector(sel) {
    if (!els.has(sel)) els.set(sel, stubEl());
    return els.get(sel);
  },
  querySelectorAll() { return []; },
};
global.window = { addEventListener() {} };

// 定时器：unref 避免挡住进程退出；记录句柄开关状态供停表断言
let openIntervals = 0;
const realSetInterval = global.setInterval;
global.setInterval = (fn, ms, ...a) => {
  const h = realSetInterval(fn, ms, ...a);
  h.unref && h.unref();
  h._open = true;
  openIntervals += 1;
  return h;
};
const realClearInterval = global.clearInterval;
global.clearInterval = (h) => {
  if (h && h._open) { h._open = false; openIntervals -= 1; }
  return realClearInterval(h);
};
const realSetTimeout = global.setTimeout;
global.setTimeout = (fn, ms, ...a) => {
  const h = realSetTimeout(fn, ms, ...a);
  h.unref && h.unref();
  return h;
};

/* ------------------------------------------------ fetch 桩（可编排队列）---- */
const defaults = new Map([
  ["/app/fnmusic-ext/api/config", { values: {} }],
  ["/app/fnmusic-ext/api/status", { version: "t", current_provider: "none", processes: {}, services: {}, lx_source: null }],
  ["/app/fnmusic-ext/api/platforms", { ok: true, enabled: [], registered: [] }],
]);
let routes = new Map(); // path -> bodies 队列（Error 表示 HTTP 非 2xx）
let lastFetch = null;   // 最近一次 fetch 的 {url, path, body}（POST body 已解析）
let fetchLog = [];       // 全部 fetch 记录（分批/多次请求断言用）

function enqueue(p, ...bodies) {
  if (!routes.has(p)) routes.set(p, []);
  routes.get(p).push(...bodies);
}

global.fetch = async (url, options) => {
  const p = String(url).split("?")[0];
  lastFetch = {
    url: String(url),
    path: p,
    body: options && options.body ? JSON.parse(options.body) : null,
  };
  fetchLog.push(lastFetch);
  if (routes.has(p) && routes.get(p).length) {
    const body = routes.get(p).shift();
    if (body instanceof Error) {
      return { ok: false, status: 502, json: async () => ({ detail: body.message }) };
    }
    return { ok: true, status: 200, json: async () => body };
  }
  return { ok: true, status: 200, json: async () => (defaults.get(p) || { ok: true }) };
};

function reset() {
  els.clear();
  routes = new Map();
  lastFetch = null;
  fetchLog = [];
}

const tick = (ms) => new Promise((r) => realSetTimeout(r, ms));

/* ------------------------------------------------ 加载 app.js -------------- */
const src = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");
vm.runInThisContext(src, { filename: "app.js" });

for (const fn of ["pollQr", "checkQrStatus", "startQrLogin", "syncNeteaseAccount", "stopQrPolling"]) {
  assert.strictEqual(typeof global[fn], "function", `app.js 应在全局暴露 ${fn}`);
}

/* ------------------------------------------------ 用例 --------------------- */
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("checkQrStatus：信封 data.code 各状态分支文案", async () => {
  const cases = [
    [801, "等待扫码…"],
    [802, "已扫码，请在手机上确认"],
    [800, "二维码已过期，请重新生成"],
  ];
  for (const [code, text] of cases) {
    reset();
    enqueue("/app/fnmusic-ext/api/netease/auth/login/check", { ok: true, data: { code, message: "x" } });
    await global.checkQrStatus("K1");
    assert.strictEqual(els.get("#qr-status").textContent, text);
  }
});

test("checkQrStatus：803 信封 → 登录成功并同步账号昵称", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/netease/auth/login/check", { ok: true, data: { code: 803, cookie: "c" } });
  enqueue("/app/fnmusic-ext/api/netease/auth/status", { ok: true, data: { logged_in: true, nickname: "小明", user_id: 1 } });
  await global.checkQrStatus("K1");
  await tick(5); // 等 syncNeteaseAccount 的异步 fetch 落定
  assert.ok(els.get("#qr-status").textContent.includes("登录成功"));
  assert.strictEqual(els.get("#qr-check").textContent, "当前登录：小明");
});

test("checkQrStatus：803 扁平 {code} 结构兼容", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/netease/auth/login/check", { code: 803 });
  enqueue("/app/fnmusic-ext/api/netease/auth/status", { ok: true, data: { logged_in: true, user_id: 42 } });
  await global.checkQrStatus("K1");
  await tick(5);
  assert.ok(els.get("#qr-status").textContent.includes("登录成功"));
  assert.strictEqual(els.get("#qr-check").textContent, "当前登录：42"); // 无昵称回退 user_id
});

test("checkQrStatus：803 停止轮询定时器", async () => {
  reset();
  const before = openIntervals;
  global.pollQr("K1"); // 默认 2000ms，测试期内不会自然触发
  assert.strictEqual(openIntervals, before + 1);
  enqueue("/app/fnmusic-ext/api/netease/auth/login/check", { ok: true, data: { code: 803 } });
  enqueue("/app/fnmusic-ext/api/netease/auth/status", { ok: true, data: { logged_in: false } });
  await global.checkQrStatus("K1");
  await tick(5);
  assert.strictEqual(openIntervals, before); // 803 后停表
});

test("startQrLogin：信封 data.unikey 出码并启动轮询", async () => {
  reset();
  const before = openIntervals;
  enqueue("/app/fnmusic-ext/api/netease/auth/status", { ok: true, data: { logged_in: false } });
  enqueue("/app/fnmusic-ext/api/netease/auth/login", { ok: true, data: { unikey: "KEY9", qr_url: "u" } });
  await global.startQrLogin();
  assert.strictEqual(els.get("#qr-img").src, "/app/fnmusic-ext/api/netease/qr?unikey=KEY9");
  assert.strictEqual(els.get("#qr-img").hidden, false);
  assert.strictEqual(els.get("#qr-status").textContent, "请用手机网易云音乐 App 扫码");
  assert.strictEqual(openIntervals, before + 1); // 轮询已启动
  global.stopQrPolling();
});

test("startQrLogin：上游不可达 → 生成失败文案", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/netease/auth/login", new Error("musicbox 服务不可达"), new Error("musicbox 服务不可达"));
  await global.startQrLogin();
  assert.ok(els.get("#qr-status").textContent.startsWith("生成失败："));
  assert.strictEqual(els.get("#qr-img").hidden, true);
});

test("syncNeteaseAccount：未登录清空 / 请求失败清空", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/netease/auth/status", { ok: true, data: { logged_in: false } });
  await global.syncNeteaseAccount();
  assert.strictEqual(els.get("#qr-check").textContent, "");
  reset();
  enqueue("/app/fnmusic-ext/api/netease/auth/status", new Error("down"));
  await global.syncNeteaseAccount();
  assert.strictEqual(els.get("#qr-check").textContent, "");
});

test("markDirty / clearDirty：save-bar 的 show 类显隐与文案同步", () => {
  reset();
  global.markDirty("已修改配置");
  assert.strictEqual(els.get("#save-note").textContent, "已修改配置");
  assert.strictEqual(els.get("#save-bar").classList.contains("show"), true);
  global.clearDirty();
  assert.strictEqual(els.get("#save-note").textContent, "");
  assert.strictEqual(els.get("#save-bar").classList.contains("show"), false);
});

test("lxUploadScript→lxAfterUpload：上传成功后 file:// URL 填入输入框并标脏", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/lx/upload", {
    ok: true,
    data: { path: "/data/lxmusic/uploads/mine.js", url: "file:///data/lxmusic/uploads/mine.js", meta: { name: "上传源" } },
  });
  const r = await global.lxUploadScript("mine.js", "/*stub*/");
  assert.strictEqual(r.data.url, "file:///data/lxmusic/uploads/mine.js");
  await global.lxAfterUpload(r);
  assert.strictEqual(els.get("#lx-url").value, "file:///data/lxmusic/uploads/mine.js");
  assert.strictEqual(els.get("#lx-upload-note").textContent, "已上传：上传源");
  assert.strictEqual(els.get("#save-bar").classList.contains("show"), true);
});

test("lxUploadScript：lxmusic 未运行时先拉预览再重试", async () => {
  reset();
  // 第一次 upload 502（进程未起），预览成功后重试成功
  enqueue("/app/fnmusic-ext/api/lx/upload", new Error("lxmusic 服务不可达"));
  enqueue("/app/fnmusic-ext/api/preview", { ok: true, preview: true });
  enqueue("/app/fnmusic-ext/api/lx/upload", {
    ok: true,
    data: { path: "/p", url: "file:///p", meta: { name: "n" } },
  });
  const r = await global.lxUploadScript("a.js", "/*s*/");
  assert.strictEqual(r.ok, true);
});

test("网易账号歌单开关：loadConfig 回填 + collectConfig 收集", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: { FNMUSIC_NETEASE_MY_PLAYLISTS: "true" } });
  await global.loadConfig();
  assert.strictEqual(els.get("#netease-my-playlists").checked, true);
  els.get("#netease-my-playlists").checked = false;
  assert.strictEqual(global.collectConfig().FNMUSIC_NETEASE_MY_PLAYLISTS, false);
  // 回填 false：缺省/关闭都表现为未勾选
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: { FNMUSIC_NETEASE_MY_PLAYLISTS: "false" } });
  await global.loadConfig();
  assert.strictEqual(els.get("#netease-my-playlists").checked, false);
});

test("自动下载歌词开关：loadConfig 回填 + collectConfig 收集", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: { FNMUSIC_LYRIC_AUTO_DL: "true" } });
  await global.loadConfig();
  assert.strictEqual(els.get("#lyric-auto-dl").checked, true);
  els.get("#lyric-auto-dl").checked = false;
  assert.strictEqual(global.collectConfig().FNMUSIC_LYRIC_AUTO_DL, false);
  // 缺省/关闭都表现为未勾选（默认关）
  reset();
  enqueue("/app/fnmusic-ext/api/config", { values: {} });
  await global.loadConfig();
  assert.strictEqual(els.get("#lyric-auto-dl").checked, false);
});

/* ------------------------------------------------ 主题 / 设置 -------------- */
const themeBootSrc = fs.readFileSync(path.join(__dirname, "static", "theme-boot.js"), "utf8");

// 每例完整加载生产 app.js 到独立页面，避免主题偏好、媒体/宿主监听器污染既有用例。
// 仅以同名函数桩替换 SDK 加载边界（我方为 lxLoadTrimSdk），不改主题实现。
async function themeHarness(options = {}) {
  const elements = new Map();
  const get = (sel) => {
    if (!elements.has(sel)) elements.set(sel, stubEl());
    return elements.get(sel);
  };
  const appliedThemes = [];
  const root = { dataset: new Proxy({}, { set(target, key, value) {
    if (key === "theme") appliedThemes.push(value);
    target[key] = value;
    return true;
  } }) };
  const radios = ["auto", "light", "dark"].map((value) => Object.assign(stubEl(), { value }));
  const pages = ["overview", "settings", "llm", "tee"].map((page) =>
    Object.assign(stubEl(), { id: "page-" + page }));
  const nav = ["settings", "settings", "llm", "tee"].map((page) =>
    Object.assign(stubEl(), { dataset: { page } }));
  const media = Object.assign(stubEl(), { matches: !!options.dark });
  const saved = new Map(options.saved == null ? [] : [["fnmusic-theme", options.saved]]);
  if (options.hostSaved != null) saved.set("fnmusic-host-theme", options.hostSaved);
  const storage = {
    getItem(key) { if (options.blockStorage) throw new Error("storage denied"); return saved.get(key) ?? null; },
    setItem(key, value) { if (options.blockStorage) throw new Error("storage denied"); saved.set(key, value); },
    removeItem(key) { if (options.blockStorage) throw new Error("storage denied"); saved.delete(key); },
  };
  const win = Object.assign(stubEl(), {
    location: { pathname: options.pathname || "/" }, localStorage: storage,
    matchMedia: options.noMedia ? undefined : () => media,
  });
  const calls = [];
  const context = vm.createContext({
    window: win,
    document: {
      documentElement: root,
      querySelector: get,
      querySelectorAll(sel) {
        return ({ "input[name=theme]": radios, ".page": pages, "[data-page]": nav })[sel] || [];
      },
    },
    fetch: async (url, request = {}) => {
      calls.push({ url, ...request });
      return { ok: true, json: async () => defaults.get(String(url).split("?")[0]) || { ok: true } };
    },
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    __sdk: options.sdk || false, __sdkCalls: 0,
  });
  vm.runInContext(themeBootSrc, context, { filename: "theme-boot.js" });
  const initialTheme = root.dataset.theme;
  vm.runInContext(src + '\nasync function lxLoadTrimSdk() { __sdkCalls++; return __sdk; }', context, { filename: "app.js" });
  await tick(0);
  return { context, root, initialTheme, appliedThemes, radios, media, saved, win, calls, elements, pages, nav };
}

function themeSelected(h) {
  return h.radios.filter((el) => el.checked).map((el) => el.value);
}

function changeTheme(h, value) {
  h.radios.forEach((el) => { el.checked = el.value === value; });
  h.radios.find((el) => el.checked).dispatch("change");
}

test("首屏主题：偏好优先，非法/缺省/存储禁用回退系统，无媒体查询默认浅色", async () => {
  for (const [options, expected, selected] of [
    [{ dark: true }, "dark", "auto"],
    [{ dark: false }, "light", "auto"],
    [{ saved: "light", dark: true }, "light", "light"],
    [{ saved: "dark", dark: false }, "dark", "dark"],
    [{ saved: "invalid", dark: true }, "dark", "auto"],
    [{ saved: "auto", dark: true }, "dark", "auto"],
    [{ blockStorage: true, dark: true }, "dark", "auto"],
    [{ noMedia: true }, "light", "auto"],
  ]) {
    const h = await themeHarness(options);
    assert.strictEqual(h.initialTheme, expected, "样式表前已写入正确主题");
    assert.strictEqual(h.root.dataset.theme, expected, "app 初始化与首屏引导一致");
    assert.deepStrictEqual(themeSelected(h), [selected]);
    assert.strictEqual(h.context.__sdkCalls, 0, "直连不加载宿主 SDK");
  }
});

test("设置主题：立即生效、持久化/恢复 auto，不标脏、不写业务配置", async () => {
  const h = await themeHarness();
  const configBefore = JSON.stringify(h.context.collectConfig());
  for (const value of ["dark", "light"]) {
    changeTheme(h, value);
    assert.strictEqual(h.root.dataset.theme, value);
    assert.strictEqual(h.saved.get("fnmusic-theme"), value);
    assert.deepStrictEqual(themeSelected(h), [value]);
    const reloaded = await themeHarness({ saved: h.saved.get("fnmusic-theme"), dark: value !== "dark" });
    assert.strictEqual(reloaded.initialTheme, value);
  }
  changeTheme(h, "auto");
  assert.strictEqual(h.saved.has("fnmusic-theme"), false);
  h.media.matches = true;
  h.media.dispatch("change");
  assert.strictEqual(h.root.dataset.theme, "dark");
  assert.deepStrictEqual(themeSelected(h), ["auto"]);
  assert.strictEqual(JSON.stringify(h.context.collectConfig()), configBefore);
  assert.strictEqual(h.elements.get("#save-bar").classList.contains("show"), false);
  h.win.dispatch("beforeunload", { preventDefault() { assert.fail("主题不应产生未保存警告"); } });
  assert.ok(h.calls.every((call) => !call.method || call.method === "GET"));
  h.context.markDirty("已编辑业务配置");
  changeTheme(h, "dark");
  assert.strictEqual(h.elements.get("#save-bar").classList.contains("show"), true, "不清除既有脏标记");
});

test("主题：存储拒绝写入仍在本次页面生效，手动偏好不受系统切换影响", async () => {
  const h = await themeHarness({ blockStorage: true });
  changeTheme(h, "dark");
  h.media.dispatch("change");
  assert.strictEqual(h.root.dataset.theme, "dark");
  changeTheme(h, "light");
  h.media.matches = true;
  h.media.dispatch("change");
  assert.strictEqual(h.root.dataset.theme, "light");
  changeTheme(h, "auto");
  assert.strictEqual(h.root.dataset.theme, "dark");
});

test("设置导航：桌面和移动入口切换同一页面，不破坏 LLM 导航", async () => {
  const h = await themeHarness();
  for (const button of h.nav.slice(0, 2)) {
    h.context.switchPage("tee");
    button.dispatch("click");
    assert.deepStrictEqual(h.pages.filter((el) => el.classList.contains("active")).map((el) => el.id), ["page-settings"]);
    assert.ok(h.nav.slice(0, 2).every((el) => el.classList.contains("active")));
    h.context.switchPage("llm");
    assert.strictEqual(h.pages.find((el) => el.id === "page-llm").classList.contains("active"), true);
  }
});

test("宿主主题：初值、os/theme 动态切换优先系统，手动偏好优先宿主", async () => {
  let notify;
  const h = await themeHarness({
    pathname: "/app/fnmusic-ext/", dark: false,
    sdk: {
      isWeb: true, isStandaloneWeb: false,
      getPlatformConfig: async () => ({ theme: "dark" }),
      $on: async (name, fn) => { assert.strictEqual(name, "os/theme"); notify = fn; },
    },
  });
  assert.strictEqual(h.initialTheme, "light");
  assert.strictEqual(h.root.dataset.theme, "dark");
  assert.deepStrictEqual(themeSelected(h), ["auto"]);
  h.media.dispatch("change");
  assert.strictEqual(h.root.dataset.theme, "dark");
  notify("light");
  assert.strictEqual(h.root.dataset.theme, "light");
  changeTheme(h, "dark");
  notify("light");
  assert.strictEqual(h.root.dataset.theme, "dark");
  changeTheme(h, "auto");
  assert.strictEqual(h.root.dataset.theme, "light");
  h.media.matches = true;
  notify("invalid");
  assert.strictEqual(h.root.dataset.theme, "dark", "未知宿主主题回退媒体查询");
});

test("宿主主题：刷新沿用缓存直到异步校正，直连和手动选择不受缓存影响", async () => {
  let notify;
  const sdk = {
    isWeb: true,
    getPlatformConfig: async () => ({ theme: "dark" }),
    $on: async (_, fn) => { notify = fn; },
  };
  const first = await themeHarness({ pathname: "/app/fnmusic-ext/", sdk });
  assert.strictEqual(first.saved.get("fnmusic-host-theme"), "dark");
  let resolveConfig;
  const reloaded = await themeHarness({
    pathname: "/app/fnmusic-ext/", hostSaved: first.saved.get("fnmusic-host-theme"),
    sdk: { getPlatformConfig: () => new Promise((resolve) => { resolveConfig = resolve; }) },
  });
  assert.strictEqual(reloaded.initialTheme, "dark");
  assert.ok(reloaded.appliedThemes.every((value) => value === "dark"), "SDK 返回前不能切回设备浅色");
  resolveConfig({ theme: "light" });
  await tick(0);
  assert.strictEqual(reloaded.root.dataset.theme, "light");
  assert.strictEqual(reloaded.saved.get("fnmusic-host-theme"), "light");
  notify("light");
  assert.strictEqual(first.saved.get("fnmusic-host-theme"), "light");
  notify("invalid");
  assert.strictEqual(first.saved.has("fnmusic-host-theme"), false);
  for (const options of [
    { hostSaved: "dark" },
    { pathname: "/app/fnmusic-ext/", hostSaved: "dark", saved: "light", sdk },
    { pathname: "/app/fnmusic-ext/", hostSaved: "invalid" },
  ]) {
    const h = await themeHarness(options);
    assert.strictEqual(h.initialTheme, "light");
    assert.strictEqual(h.root.dataset.theme, "light");
  }
});

test("宿主主题：缓存失效或宿主不可用时清缓存并回退系统，存储禁用不阻断", async () => {
  for (const sdk of [false, {},
    { getPlatformConfig: async () => { throw new Error("no host"); } },
    { getPlatformConfig: async () => ({ theme: "invalid" }) },
  ]) {
    const h = await themeHarness({ pathname: "/app/fnmusic-ext/", hostSaved: "dark", sdk });
    assert.strictEqual(h.initialTheme, "dark");
    assert.strictEqual(h.root.dataset.theme, "light", "缓存失效后回退系统");
    assert.strictEqual(h.saved.has("fnmusic-host-theme"), false);
    h.media.matches = true;
    h.media.dispatch("change");
    assert.strictEqual(h.root.dataset.theme, "dark");
  }
  const blocked = await themeHarness({
    pathname: "/app/fnmusic-ext/", blockStorage: true,
    sdk: { getPlatformConfig: async () => ({ theme: "dark" }) },
  });
  assert.strictEqual(blocked.root.dataset.theme, "dark", "存储禁用不阻断宿主主题");
  const standalone = await themeHarness({
    pathname: "/app/fnmusic-ext/",
    sdk: { isStandaloneWeb: true, getPlatformConfig: async () => ({ theme: "dark" }) },
  });
  assert.strictEqual(standalone.root.dataset.theme, "dark");
});

/* 升级弹窗：GitHub / Gitee 双下载源（2026-10-22 用户需求） */
test("升级弹窗：mirrors 双源渲染，Gitee 按钮默认隐藏、有源时展示", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/update-check", {
    ok: true,
    has_update: true,
    current_version: "3.5.0",
    source: "github",
    latest: {
      version: "3.5.1",
      name: "v3.5.1",
      published_at: "2026-10-22T00:00:00Z",
      changelog: "- 顺序播放本地歌曲",
      html_url: "https://github.com/superxyline/fnos_music_ext/releases/tag/v3.5.1",
      mirrors: [
        { name: "GitHub", url: "https://github.com/superxyline/fnos_music_ext/releases/tag/v3.5.1" },
        { name: "Gitee", url: "https://gitee.com/superxyline/fnos_music_ext/releases/tag/v3.5.1" },
      ],
    },
  });
  await global.loadUpdateCheck();
  global.openUpdateModal();
  assert.strictEqual(
    els.get("#update-go").href,
    "https://github.com/superxyline/fnos_music_ext/releases/tag/v3.5.1");
  assert.strictEqual(els.get("#update-go").textContent, "GitHub 下载");
  assert.strictEqual(els.get("#update-go-gitee").hidden, false, "有 Gitee 源必须展示");
  assert.strictEqual(
    els.get("#update-go-gitee").href,
    "https://gitee.com/superxyline/fnos_music_ext/releases/tag/v3.5.1");
  assert.ok(els.get("#update-log").textContent.includes("顺序播放本地歌曲"));
});

test("升级弹窗：mirrors 缺失时退回主源单链接，Gitee 按钮保持隐藏", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/update-check", {
    ok: true,
    has_update: true,
    current_version: "3.4.2",
    source: "gitee",
    latest: {
      version: "3.5.1",
      changelog: "",
      html_url: "https://gitee.com/superxyline/fnos_music_ext/releases/tag/v3.5.1",
    },
  });
  await global.loadUpdateCheck();
  global.openUpdateModal();
  assert.strictEqual(
    els.get("#update-go").href,
    "https://gitee.com/superxyline/fnos_music_ext/releases/tag/v3.5.1",
    "退回主源 html_url");
  assert.strictEqual(els.get("#update-go-gitee").hidden, true);
});

/* 关于页：项目仓库双平台链接 */
test("关于页：项目仓库/问题反馈同时列 GitHub 与 Gitee", async () => {
  reset();
  enqueue("/app/fnmusic-ext/api/about", {
    ok: true,
    version: "3.5.1",
    deploy_mode: "docker",
    author: "superxyline",
    author_url: "https://github.com/superxyline",
    repo_url: "https://github.com/superxyline/fnos_music_ext",
    repo_url_gitee: "https://gitee.com/superxyline/fnos_music_ext",
    issues_url: "https://github.com/superxyline/fnos_music_ext/issues",
    issues_url_gitee: "https://gitee.com/superxyline/fnos_music_ext/issues",
  });
  await global.loadAbout();
  const html = els.get("#about-links").innerHTML;
  assert.ok(html.includes("项目仓库 · GitHub"), "GitHub 仓库入口");
  assert.ok(html.includes("项目仓库 · Gitee"), "Gitee 仓库入口");
  assert.ok(html.includes("问题反馈 · Gitee"), "Gitee 反馈入口");
  assert.ok(html.includes("https://gitee.com/superxyline/fnos_music_ext"), "Gitee 链接地址");
  assert.strictEqual(els.get("#about-version").textContent, "v3.5.1");
});

/* 音源测试：范围切换（已勾选 / 全部平台）——2026-10-22 用户需求 */
test("音源测试：默认只测已勾选平台，可切换全部注册平台", async () => {
  reset();
  global.setMusicdlTestScope("enabled"); // 范围是页面级状态：显式初始化，不依赖用例顺序
  enqueue("/app/fnmusic-ext/api/platforms", {
    ok: true,
    enabled: ["kw", "mg"],
    registered: ["kw", "mg", "kg", "tx", "wy"],
  });
  await global.loadPlatforms(false);
  document.querySelector("#musicdl-test-q").value = "晴天";

  // 默认范围：已勾选
  enqueue("/app/fnmusic-ext/api/musicdl/test", {
    ok: true,
    keyword: "晴天",
    tookMs: 3200,
    platforms: [{ source: "kw", count: 3, samples: ["晴天 - 周杰伦"] }],
  });
  await global.musicdlTest();
  assert.strictEqual(lastFetch.body.scope, "enabled");
  assert.deepStrictEqual(lastFetch.body.sources, ["kw", "mg"]);
  let html = els.get("#musicdl-test-result").innerHTML;
  assert.ok(html.includes("范围：已勾选 2 个"), "汇总行显示范围与平台数");
  assert.ok(html.includes("出歌 1 个"), "汇总行显示出歌数");
  assert.ok(html.includes("耗时 3.2s"), "汇总行显示耗时");

  // 切到全部平台：registered 全量 + scope=all
  global.setMusicdlTestScope("all");
  enqueue("/app/fnmusic-ext/api/musicdl/test", {
    ok: true,
    keyword: "晴天",
    tookMs: 8123,
    platforms: [
      { source: "kw", count: 2, samples: [] },
      { source: "kg", count: 0, error: "无结果" },
    ],
  });
  await global.musicdlTest();
  assert.strictEqual(lastFetch.body.scope, "all");
  assert.deepStrictEqual(lastFetch.body.sources, ["kw", "mg", "kg", "tx", "wy"]);
  html = els.get("#musicdl-test-result").innerHTML;
  assert.ok(html.includes("范围：全部平台 5 个"));
  assert.ok(html.includes("耗时 8.1s"));
  assert.strictEqual(els.get("#musicdl-test-result").className, "report ok",
    "全部模式只要有平台出歌即 ok（个别平台没有某首歌是正常的）");
});

test("音源测试：全部模式一个平台都没出歌才标红；空范围有提示", async () => {
  reset();
  global.setMusicdlTestScope("enabled"); // 范围是页面级状态：显式初始化，不依赖用例顺序
  enqueue("/app/fnmusic-ext/api/platforms", { ok: true, enabled: [], registered: ["kw"] });
  await global.loadPlatforms(false);
  document.querySelector("#musicdl-test-q").value = "晴天";
  // 没勾选任何平台：提示且不发测试请求
  const before = lastFetch && lastFetch.path;
  await global.musicdlTest();
  assert.strictEqual(lastFetch && lastFetch.path, before, "无勾选平台时不得发测试请求");
  assert.strictEqual(els.get("#toast").hidden, false);

  // 切全部：全都不出歌 → 标红
  global.setMusicdlTestScope("all");
  enqueue("/app/fnmusic-ext/api/musicdl/test", {
    ok: true, keyword: "晴天", tookMs: 500,
    platforms: [{ source: "kw", count: 0, error: "无结果" }],
  });
  await global.musicdlTest();
  assert.strictEqual(els.get("#musicdl-test-result").className, "report fail");
});


test("音源测试：全部模式分批限流，>8 个平台拆成多批请求", async () => {
  reset();
  global.setMusicdlTestScope("all");
  const registered = Array.from({ length: 10 }, (_, i) => `P${i}MusicClient`);
  enqueue("/app/fnmusic-ext/api/platforms", { ok: true, enabled: [], registered });
  await global.loadPlatforms(false);
  document.querySelector("#musicdl-test-q").value = "晴天";
  enqueue("/app/fnmusic-ext/api/musicdl/test", {
    ok: true, tookMs: 1000,
    platforms: registered.slice(0, 8).map((s) => ({ source: s, count: 0, error: "无结果" })),
  });
  enqueue("/app/fnmusic-ext/api/musicdl/test", {
    ok: true, tookMs: 800,
    platforms: registered.slice(8).map((s) => ({ source: s, count: 1, samples: [] })),
  });
  await global.musicdlTest();

  const calls = fetchLog.filter((e) => e.path.endsWith("/api/musicdl/test"));
  assert.strictEqual(calls.length, 2, "10 个平台必须拆成 2 批（8 + 2）");
  assert.strictEqual(calls[0].body.sources.length, 8);
  assert.strictEqual(calls[1].body.sources.length, 2);
  assert.strictEqual(calls[0].body.scope, "all");
  const html = els.get("#musicdl-test-result").innerHTML;
  assert.ok(html.includes("出歌 2 个"), "两批结果合并统计");
  assert.ok(html.includes("P9MusicClient"), "第二批平台也在报告里");
});

/* ------------------------------------------------ 运行 --------------------- */
(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      reset();
      await fn();
      console.log(`ok - ${name}`);
    } catch (exc) {
      failed += 1;
      console.error(`not ok - ${name}\n  ${exc && exc.stack ? exc.stack : exc}`);
    }
  }
  console.log(`${tests.length - failed}/${tests.length} passed, ${failed} failed`);
  process.exitCode = failed ? 1 : 0;
})();
