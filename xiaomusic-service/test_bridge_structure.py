"""xiaomusic 桥接的结构性回归测试（不做网络/依赖 xiaomusic 的导入）。

只校验关键防护逻辑还在，防止后续改动把已修的坑改回去：
  - 口令轮询看门狗：登录失效时必须不再请求小米接口（历史上每秒撞击，
    10 分钟 6396 次 400，加重风控）；
  - health 暴露 poll_degraded 供完成页健康检查展示；
  - 管理台菜单不再有红点（用户明确要求，2026-10-21）。
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_PY = (HERE / "app.py").read_text(encoding="utf-8")
WEBUI = HERE.parent / "webui-service" / "static"


def test_poll_watchdog_is_wired_in_lifespan():
    assert "def patch_poll_watchdog(" in APP_PY
    # 必须在 lifespan 内被调用，否则只有定义没有效果
    start = APP_PY.index("async def lifespan(")
    end = APP_PY.index("app.router.lifespan_context", start)
    assert "patch_poll_watchdog()" in APP_PY[start:end]


def test_poll_watchdog_skips_requests_when_logged_out():
    body = APP_PY[APP_PY.index("def patch_poll_watchdog("):]
    body = body[:body.index("\ndef poll_degraded(")]
    # 判据：mina_service 为空即视为登录不可用
    assert "mina_service" in body
    # 失效时必须提前 return，不能继续调用 original（否则就是每秒撞接口）
    assert "return None" in body
    assert "await original(" in body
    # 降频到 60 秒，恢复回 1 秒
    assert "pull_ask_sec = 60" in body
    assert "pull_ask_sec = 1" in body
    # return None 必须发生在调用 original 之前
    assert body.index("return None") < body.index("await original(")


def test_poll_guard_loop_registered():
    """登录失效时设备列表为空、看门狗包装层不会被调用，需要独立守卫维护间隔。"""
    assert "async def poll_guard_loop()" in APP_PY
    start = APP_PY.index("async def lifespan(")
    end = APP_PY.index("app.router.lifespan_context", start)
    assert "poll_guard_loop()" in APP_PY[start:end]
    body = APP_PY[APP_PY.index("async def poll_guard_loop():"):]
    assert "pull_ask_sec = want" in body
    assert "want = 1 if healthy else 60" in body


def test_health_exposes_poll_degraded():
    assert "def poll_degraded(" in APP_PY
    health = APP_PY[APP_PY.index('@app.get("/health")'):]
    health = health[:health.index("@app.get(\"/\"")]
    assert '"poll_degraded"' in health
    assert "poll_degraded()" in health
    # 实际轮询间隔也要暴露，便于确认已降频到 60s
    assert '"pull_ask_sec"' in health


def test_done_page_prefers_degraded_message():
    """登录失效时，状态条应显示「轮询已暂停」而不是「正常」。"""
    js = (WEBUI / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function xmRefreshDone"):]
    body = body[:body.index("\n}")]
    assert "poll_degraded" in body
    # poll_degraded 分支必须排在 pull_ask_on 正常分支之前
    assert body.index("poll_degraded") < body.index("口令轮询：正常")


def test_wizard_steps_all_done_on_completion_page():
    # 完成页（step=4）时"完成"步骤也必须显示绿色对勾，不能停留在蓝色 active
    js = (WEBUI / "app.js").read_text(encoding="utf-8")
    assert "step >= XM_STEPS.length" in js, "管理台步骤条缺少完成页全点亮逻辑"

    wiz = (HERE / "static" / "wizard.html").read_text(encoding="utf-8")
    assert "step >= labels.length" in wiz, "旁路页步骤条缺少完成页全点亮逻辑"


def test_webui_nav_has_no_alert_dot():
    # 用户明确要求：管理台「关于 / 小爱音箱」菜单不显示红点（2026-10-21）
    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    assert 'id="xm-dot"' not in html, "桌面侧栏不应再有红点"
    assert 'id="xm-dot-m"' not in html, "移动底栏不应再有红点"
    assert 'id="about-dot"' not in html and 'id="about-dot-m"' not in html, "关于菜单不应再有红点"

    js = (WEBUI / "app.js").read_text(encoding="utf-8")
    assert "function xmUpdateNavDot(" not in js, "红点逻辑应已移除"
    # 关于页内 NEW 徽标（非菜单红点）保留：有新版本时仍提示
    assert 'id="about-new"' in html
    assert 'if (badge) badge.hidden = !on;' in js
