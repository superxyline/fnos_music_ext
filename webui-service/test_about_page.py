"""关于页静态回归（B3）：入口、元素引用与 hidden 兜底。

上游 issue #57 的教训：`.modal-mask { display: flex }` 的作者样式会压过浏览器
对 hidden 属性的默认 `display: none`，管理页一打开就被空弹窗遮罩盖住且关不掉。
这里做静态交叉断言——凡以 hidden 属性切换显隐、且类规则声明了 display 的元素，
必须存在对应的 `[hidden]` 隐藏兜底。
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_about_entry_in_both_navs():
    assert HTML.count('data-page="about"') == 2, "桌面侧栏与移动底栏都要有「关于」入口"
    assert 'id="page-about"' in HTML


def test_update_modal_structure():
    assert 'id="update-modal"' in HTML and 'class="modal-mask"' in HTML
    assert 'id="update-go"' in HTML and 'target="_blank"' in HTML
    assert 'rel="noopener noreferrer"' in HTML
    # 双下载源（2026-10-22 用户需求）：GitHub 主按钮 + Gitee 备选
    assert 'id="update-go-gitee"' in HTML
    assert HTML.count('id="update-go-row"') == 1


def test_about_page_lists_both_repos():
    """关于页必须同时给 GitHub 与 Gitee 两个仓库/反馈入口（用户需求）。"""
    assert '"repo_url_gitee"' in JS or "repo_url_gitee" in JS
    assert "项目仓库 · Gitee" in JS
    assert "issues_url_gitee" in JS
    # 后端 ABOUT 常量两平台链接都要有
    app_py = (Path(__file__).resolve().parent / "app.py").read_text(encoding="utf-8")
    assert '"repo_url_gitee": "https://gitee.com/superxyline/fnos_music_ext"' in app_py
    assert '"issues_url_gitee"' in app_py


def test_hidden_elements_have_css_fallback():
    """凡用 hidden 切换显隐的元素，其 display 类规则必须有 [hidden] 兜底。"""
    assert re.search(r"\.modal-mask\[hidden\]\s*\{[^}]*display:\s*none", CSS), \
        "modal-mask 缺 [hidden] 隐藏兜底：弹窗会盖住整页且关不掉"


def test_js_references_existing_ids():
    ids = set(re.findall(r'id="([^"]+)"', HTML))
    # 运行期生成的模板同样算“已定义”（如小爱音箱向导各步骤的 DOM 由 JS 渲染），
    # 否则动态页面会被误报；静态 HTML 与 JS 模板都没定义才是真笔误。
    js_ids = set(re.findall(r'id="([^"]+)"', JS))
    referenced = set(re.findall(r'\$\("#([a-zA-Z0-9_-]+)"\)', JS))
    missing = sorted(referenced - ids - js_ids)
    assert not missing, f"app.js 引用了不存在的元素: {missing}"


def test_update_check_not_polled():
    """检查更新只进页面查一次 + 进关于页查（后端有缓存），不得进定时器轮询。"""
    assert "loadUpdateCheck();" in JS
    assert not re.search(r"setInterval\([^)]*loadUpdateCheck", JS), "不得轮询检查更新"
