"""深色主题 / 外观设置 / 内容列布局契约（移植自上游 v2.10.0e test_frontend_theme.py，
按我方选择器裁剪：无 .btn.danger/.badge.new/.about-donate-img/.about-wrap，
NEW 徽标类名为 .new-badge）。动态主题行为（applyTheme/initTheme）由本地 node 用例覆盖。"""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "webui-service" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = re.sub(r"/\*.*?\*/", "", (STATIC / "style.css").read_text(encoding="utf-8"), flags=re.S)


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def elements():
    parsed = Elements()
    parsed.feed(HTML)
    return parsed.tags


def asset_path(attrs):
    """静态资源路径去掉 ?v= 缓存参数，便于 endswith 匹配。"""
    return attrs.get("src", attrs.get("href", "")).split("?")[0]


def blocks(selector):
    return re.findall(r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}", CSS)


def test_theme_boot_is_synchronous_before_stylesheet():
    tags = elements()
    boot = next(i for i, (tag, attrs) in enumerate(tags)
                if tag == "script" and asset_path(attrs).endswith("/theme-boot.js"))
    style = next(i for i, (tag, attrs) in enumerate(tags)
                 if tag == "link" and attrs.get("rel") == "stylesheet")
    app = next(i for i, (tag, attrs) in enumerate(tags)
               if tag == "script" and asset_path(attrs).endswith("/app.js"))
    assert boot < style < app
    assert not {"async", "defer", "type"}.intersection(tags[boot][1])
    for _, attrs in tags:
        asset = attrs.get("src", attrs.get("href", ""))
        if asset.startswith("/app/fnmusic-ext/static/"):
            assert (STATIC / asset.rsplit("/", 1)[1].split("?")[0]).is_file(), asset


def test_settings_has_desktop_mobile_navigation_and_three_local_preferences():
    tags = elements()
    assert len([a for tag, a in tags if tag == "button" and a.get("data-page") == "settings"]) == 2
    assert len([a for tag, a in tags if tag == "section" and a.get("id") == "page-settings"]) == 1
    radios = [a for tag, a in tags if tag == "input" and a.get("name") == "theme"]
    assert [a["value"] for a in radios] == ["auto", "light", "dark"]
    assert all(a.get("type") == "radio" for a in radios)


def test_theme_tokens_cover_surfaces_and_keep_code_images_white():
    light = blocks(":root")[0]
    dark = blocks(':root[data-theme="dark"]')[0]
    tokens = lambda body: set(re.findall(r"(--[\w-]+)\s*:", body))
    assert "color-scheme: light" in light
    assert "color-scheme: dark" in dark
    colors = {"--bg", "--card", "--line", "--text", "--muted", "--accent", "--accent-soft",
              "--input-bg", "--chip-bg", "--surface-2", "--badge-dim-bg", "--ok-soft",
              "--dot-idle", "--overlay", "--bar-bg", "--nav-bg", "--toast-bg", "--toast-text",
              "--focus-ring", "--shadow", "--shadow-up", "--ok", "--warn", "--err"}
    assert colors <= tokens(light) & tokens(dark)
    # CSS 里引用的变量都必须在浅色块有定义（深色块只覆盖部分）
    assert set(re.findall(r"var\((--[\w-]+)\)", CSS)) <= tokens(light)
    # 二维码必须白底：深色模式也不反色，否则手机扫不出来
    assert "background: #fff" in blocks("#qr-img")[0], "二维码不能随主题反色"
    for selector in [".input", ".chip", ".report", ".modal-mask", ".toast", ".bottom-nav"]:
        body = " ".join(blocks(selector))
        assert "background: var(--" in body, selector


@pytest.mark.parametrize("selector", [":root", ':root[data-theme="dark"]'])
def test_solid_controls_and_status_toasts_have_readable_contrast(selector):
    values = dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", blocks(selector)[0]))

    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
                  for v in channels]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    for control, background in [(".nav-item.active", "--accent"),
                                (".btn.primary", "--accent"),
                                (".new-badge", "--err"),
                                (".toast.ok", "--ok"), (".toast.fail", "--err")]:
        body = blocks(control)[0]
        assert f"background: var({background})" in body
        assert "color: var(--on-accent)" in body
        low, high = sorted((luminance(values[background]), luminance(values["--on-accent"])))
        assert (high + 0.05) / (low + 0.05) >= 4.5, (selector, control)
    # 悬停反馈不能降低已验证的文字对比度（上游 749b61c/98a5a37 的教训）
    for control in (".btn.primary:hover", ".new-badge:hover"):
        body = blocks(control)[0]
        assert not re.search(r"(?:opacity|filter|background|color)\s*:", body.replace(
            "color: var(--on-accent);", "")), "悬停反馈不能降低已验证的文字对比度"


def test_pages_share_centered_content_width_and_save_bar_alignment():
    main = blocks(".main")
    assert len(main) == 2  # 桌面主规则 + 手机内边距覆盖，共用同一个宽度上限
    assert "max-width: var(--content-max)" in main[0]
    assert "margin: 0 auto" in main[0]
    assert "width: 100%" in blocks(".page")[0]
    save_bar = blocks(".save-bar")[0]
    assert "--content-max" in save_bar and "--sidebar-w" in save_bar
    assert "padding-left: max(" in save_bar and "padding-right: max(" in save_bar
    mobile = CSS.split("@media (max-width: 760px)", 1)[1]
    assert re.search(r"\.main\s*\{[^}]*14px", mobile)
    assert re.search(r"\.save-bar\s*\{[^}]*padding: 10px 14px", mobile)
    assert "safe-area-inset-bottom" in mobile


@pytest.mark.parametrize("filename", ["theme-boot.js", "app.js"])
def test_theme_preference_key_matches_boot_and_app(filename):
    assert '"fnmusic-theme"' in (STATIC / filename).read_text(encoding="utf-8")
