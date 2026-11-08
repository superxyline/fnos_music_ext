"""前端请求路径契约：所有 API 请求必须带 APP_BASE 前缀。

issue 回归：歌单导入的 piApi / 启动导入用了站点根路径 `/api/match/...`，
从飞牛桌面（/app/fnmusic-ext 前缀）打开时请求打到网关根路径，返回 HTML 404，
前端 `resp.json()` 抛 `SyntaxError: Unexpected token '<'`。

同时锁定歌单导入走真实后端路由 `/api/import/*`（网关的导入接口不在
/api/match/* 下，旧路径直连 8774 会得到 JSON 404）。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "webui-service" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8")

# fetch( 后面直接跟以 / 开头的字面量 = 漏了 APP_BASE
_BARE_FETCH = re.compile(r"""fetch\(\s*["'`]/""")


def test_no_bare_root_fetch():
    hits = [m.group(0) for m in _BARE_FETCH.finditer(JS)]
    assert not hits, f"这些 fetch 缺少 APP_BASE 前缀（桌面入口会 404 并返回 HTML）: {hits}"


def test_import_uses_api_import_prefix():
    assert "APP_BASE + `/api/import/${path}`" in JS or "/api/import/" in JS, \
        "piApi 应走 /api/import/ 前缀"
    assert '/api/match/${path}' not in JS, "歌单导入不应再拼 /api/match/ 路径"
    assert '"/api/match/import/run"' not in JS, "启动导入应改为 APP_BASE + /api/import/run"


def test_import_calls_do_not_double_prefix():
    """piApi 自己补 /api/import/，调用点不应再带 import/ 段。"""
    assert 'piApi(`import/' not in JS
    assert 'piApi("import/' not in JS
