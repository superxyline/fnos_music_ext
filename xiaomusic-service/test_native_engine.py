"""native 引擎结构性回归测试（默认不依赖 xiaomusic 导入）。

双引擎架构（xiaomusic | native）的关键防护：
- native 引擎复用上游登录/设备/轮询/播放组件，但必须钉死三个开关
  （enable_pull_ask=True / get_ask_by_mina=False / web_music_proxy=False），
  否则复现历史静默故障（口令全不到达 / mina 空壳 KeyError / 音箱多绕一跳）；
- 引擎开关默认 xiaomusic（灰度安全），切换入口与 health 暴露齐全；
- 口令默认值单一来源（native/keywords.py），防止双引擎漂移。

带 xiaomusic 环境的集成用例自动跳过（pytest.importorskip）。
"""
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_PY = (HERE / "app.py").read_text(encoding="utf-8")
NATIVE = HERE / "native"

CORE = (NATIVE / "core.py").read_text(encoding="utf-8")
DISPATCH = (NATIVE / "dispatch.py").read_text(encoding="utf-8")
CONFIG = (NATIVE / "config.py").read_text(encoding="utf-8")
KEYWORDS = (NATIVE / "keywords.py").read_text(encoding="utf-8")


def test_native_package_files_exist():
    for name in ("__init__.py", "core.py", "dispatch.py", "config.py",
                 "qrlogin.py", "keywords.py"):
        assert (NATIVE / name).is_file(), f"native 包缺少 {name}"


def test_native_config_pins_critical_flags():
    """三个开关任何一个被改回默认值都会复现历史故障，必须钉死。"""
    assert "enable_pull_ask=True" in CONFIG, "必须显式开启口令轮询（默认 false 会空转）"
    assert "get_ask_by_mina=False" in CONFIG, "mina ubus 在本机固件是空壳，必须关"
    assert "web_music_proxy=False" in CONFIG, "native 应让音箱直连桥接 /play 取流"


def test_native_core_reuses_upstream_components():
    """登录/设备/轮询/歌单/播放必须复用上游类（自研只做编排与分发）。"""
    assert "from xiaomusic.auth import AuthManager" in CORE
    assert "from xiaomusic.conversation import ConversationPoller" in CORE
    assert "from xiaomusic.device_manager import DeviceManager" in CORE
    assert "from xiaomusic.music_library import MusicLibrary" in CORE
    # 装配顺序：device_manager 先于 auth_manager（AuthManager 依赖它）
    assert CORE.index("DeviceManager(") < CORE.index("AuthManager(")


def test_native_core_has_shim_surface():
    """XiaoMusicDevice/DevicePlayer 会回调这些方法，缺一个运行即崩。"""
    for sig in ("async def do_tts(", "async def handle_fatal_error(",
                "async def add_singer_song(", "async def play_music_list(",
                "async def play(", "async def do_check_cmd(",
                "async def reset_timer_when_answer(", "async def run_forever("):
        assert sig in CORE, f"NativeCore 缺少 {sig}"
    # 关闭自动搜歌入歌单（DevicePlayer 判 None 直接 return）
    assert "self.js_plugin_manager = None" in CORE


# 口令表能产出的全部动作（默认关键词 + 桥接推送的关键词）。
# 缺任何一个的后果不是没反应，而是半截行为（见 core.py 注释与 2026-10-19 停止播放事故）。
NATIVE_COMMAND_SURFACE = [
    "play",                  # user_key_word_dict「播放」→ 点歌
    "play_music_list",       # keywords_playlist → 播收藏歌单
    "play_music_list_index", # 「播放列表第N个」
    "playlocal",             # keywords_playlocal → 播本地下载
    "stop",                  # keywords_stop → 停止（必须取消下一曲定时器）
    "play_next",             # 下一首
    "play_prev",             # 上一首
    "set_play_type_one",     # 单曲循环
    "set_play_type_all",     # 全部循环
    "set_play_type_rnd",     # 随机播放
    "set_play_type_sin",     # 单曲播放
    "set_play_type_seq",     # 顺序播放
    "stop_after_minute",     # 分钟后关机
    "gen_music_list",        # 刷新列表
    "select_index",          # 多匹配待选态「第几个」
]


def test_native_implements_full_command_surface():
    """NativeCore 必须实现口令表能产出的每个动作。

    2026-10-19 事故：stop 未实现时，分发器执行前的 group_force_stop_xiaoai
    暂停了音箱（用户以为停了），但 is_playing 仍为 True、下一曲定时器未取消，
    poller 的 reset_timer_when_answer 把定时器重新 armed，58 秒后自动续播。
    """
    for action in NATIVE_COMMAND_SURFACE:
        assert f"async def {action}(" in CORE, f"NativeCore 缺少口令动作 {action}"


def test_native_command_surface_is_async(tmp_path):
    """集成：口令动作必须全是协程（dispatcher 用 await func(...) 调用）。"""
    pytest.importorskip("xiaomusic", reason="需要 xiaomusic 环境（容器内/开发 venv）")
    import asyncio
    import inspect
    import logging

    from native.config import build_config
    from native.core import NativeCore

    conf = tmp_path / "conf"
    music = tmp_path / "music"
    conf.mkdir(parents=True)
    music.mkdir(parents=True)

    async def _build():
        return NativeCore(
            build_config(conf_dir=conf, music_dir=music,
                         nas_ip="192.168.x.x", public_port=8775),
            logging.getLogger("test"))

    core = asyncio.run(_build())
    for action in NATIVE_COMMAND_SURFACE:
        func = getattr(core, action, None)
        assert func is not None, action
        assert inspect.iscoroutinefunction(func), action


def test_native_dispatch_handles_pending_selection():
    """多匹配待选态：「第几个」必须走 select_index（移植自 command_handler）。"""
    assert "_pending_selection" in DISPATCH
    assert '"select_index", query' in DISPATCH


def test_native_dispatch_matches_like_upstream():
    """匹配逻辑必须与上游 command_handler 同构（完全匹配+模糊+激活过滤+停小爱）。"""
    assert "def check_full_match_cmd(" in DISPATCH
    assert "def match_cmd(" in DISPATCH
    assert "KEY_WORD_ARG_BEFORE_DICT" in DISPATCH
    assert "get_active_cmd_arr()" in DISPATCH
    assert "group_force_stop_xiaoai()" in DISPATCH
    # 未匹配不能执行任何动作
    assert "return None, None" in DISPATCH


def test_app_engine_switch_wired():
    """引擎开关：默认 xiaomusic、lifespan 分支、切换端点、health 暴露。"""
    assert '"engine": "xiaomusic"' in APP_PY, "引擎默认值必须是 xiaomusic（灰度安全）"
    assert "def create_native():" in APP_PY
    start = APP_PY.index("async def lifespan(")
    end = APP_PY.index("app.router.lifespan_context", start)
    lifespan = APP_PY[start:end]
    assert 'CFG.get("engine") == "native"' in lifespan
    assert "create_native()" in lifespan
    assert "_app.mount(\"/xm\", XM_APP)" in lifespan, "xiaomusic 引擎仍须挂载 api 子应用"
    # 切换端点与 health
    assert '@app.post("/api/engine")' in APP_PY
    assert '"engine": CFG.get("engine", "xiaomusic")' in APP_PY


def test_native_playlist_channel_is_music_list_json():
    """歌单必须走 music_list_json 通道。

    MusicLibrary._append_music_list 只消费 config.music_list_json；
    music_list_url 仅是 xiaomusic 引擎 refresh_web_music_list 的备用拉取路径。
    曾漏掉此通道导致 native 引擎歌单为空——三处（读设置/推送/落盘）都要在。
    """
    assert 'music_list_json=settings.get("music_list_json", "")' in CONFIG
    assert '"music_list_json": music_list_json' in CORE
    assert '"music_list_json"' in APP_PY


def test_native_analytics_stub_is_async():
    """device_player.py:612 会 `await self.xiaomusic.analytics.send_play_event(...)`。

    同步桩会在播放前一刻崩掉（TypeError: object NoneType can't be used in
    'await'），口令有反应、音箱无声——2026-10-19 实测踩坑。
    """
    assert "async def send_play_event" in CORE


def test_native_playlist_entries_use_public_url():
    """native 引擎 web_music_proxy=False，条目 URL 原样发给音箱，必须对外可达。

    127.0.0.1:8005 是容器内环地址，音箱拿到只会连它自己（播放无声）。
    xiaomusic 引擎保持 SELF_URL（代理服务端拉取，既有行为）。
    """
    assert "def _play_entry_base()" in APP_PY
    base = APP_PY[APP_PY.index("def _play_entry_base()"):]
    base = base[:base.index("\ndef ")]
    assert 'CFG.get("engine") == "native"' in base
    assert "PUBLIC_PORT" in base
    # build_playlist 必须用 _play_entry_base 而不是硬编码 SELF_URL
    body = APP_PY[APP_PY.index("def build_playlist("):]
    body = body[:body.index("\n\n")]
    assert "_play_entry_base()" in body
    assert 'f"{SELF_URL}/play?guid=' not in body


def test_app_native_branches_present():
    """所有原本走 /xm/* HTTP 的路径都必须有 native 直调分支。"""
    for marker in (
        "XM.push_config(",              # push_playlist（歌单+口令）
        "XM.refresh_music_list()",      # 点歌下载后刷新
        "return await XM.fetch_devices(",  # _fetch_devices
        "_native_save_setting(",        # mi_login / wizard_devices / wizard_reset
        "XM.qrlogin.get_qrcode()",      # wizard_qrcode
    ):
        assert marker in APP_PY, f"app.py 缺少 native 分支: {marker}"


def test_keywords_single_source():
    """口令默认值只准有一处定义（native/keywords.py），app.py 只准 import。"""
    assert "KEYWORDS_PLAYLIST_DEFAULT" in KEYWORDS
    assert "from native.keywords import" in APP_PY
    assert "KEYWORDS_PLAYLIST_DEFAULT" in APP_PY
    # push_playlist 里不允许再出现内联的口令清单（双份必然漂移）
    push = APP_PY[APP_PY.index("async def push_playlist("):]
    push = push[:push.index("\n\n")]
    assert "播放列表" not in push, "push_playlist 又内联了口令清单，应改用共享常量"


# ---------------- 集成用例（需 xiaomusic 环境，开发机无 xiaomusic 时自动跳过） ----------------


def test_native_engine_smoke(tmp_path):
    """构造 NativeCore + 口令匹配（不联网：不触发登录与轮询）。"""
    pytest.importorskip("xiaomusic", reason="需要 xiaomusic 环境（容器内/开发 venv）")
    import asyncio

    from native.config import build_config
    from native.core import NativeCore

    conf = tmp_path / "conf"
    music = tmp_path / "music"
    conf.mkdir(parents=True)
    music.mkdir(parents=True)

    cfg = build_config(
        conf_dir=conf, music_dir=music,
        nas_ip="192.168.x.x", public_port=8775,
        settings={"mi_did": "936484158"},
    )
    assert cfg.enable_pull_ask is True
    assert cfg.get_ask_by_mina is False
    assert cfg.web_music_proxy is False
    assert cfg.key_word_dict.get("播放列表") == "play_music_list"
    assert cfg.key_word_dict.get("播放") == "play"

    async def _build():
        return NativeCore(cfg, __import__("logging").getLogger("test"))

    core = asyncio.run(_build())
    assert core.device_manager.xiaomusic is core
    assert core.mi_logged_in() is False

    class _Dev:
        is_playing = False

    dev = _Dev()
    assert core.dispatcher.match_cmd(dev, "播放列表", False) == ("play_music_list", "")
    assert core.dispatcher.match_cmd(dev, "播放本地收藏歌曲", False) == ("play_music_list", "")
    assert core.dispatcher.match_cmd(dev, "播放晴天", False) == ("play", "晴天")
    assert core.dispatcher.match_cmd(dev, "播放本地歌曲", False) == ("play_music_list", "")
    assert core.dispatcher.match_cmd(dev, "今天天气怎么样", False) == (None, None)

    core.push_config(
        music_list_json="[]",
        keywords_playlist="播放列表,播放歌单",
        keywords_playlocal="播放下载歌曲",
        user_key_word_dict={"播放": "play"},
        music_list_url="http://192.168.x.x:8775/playlist.json",
    )
    assert "播放歌单" in core.config.key_word_dict
    assert core.config.music_list_url.endswith("/playlist.json")


def _fresh_bridge(tmp_path, monkeypatch):
    """按当前 tmp_path 重新加载 app 模块（模块级路径在 import 时绑定 env，
    单例缓存会导致测试间串味；pytest-randomly 下必须显式 reload）。"""
    monkeypatch.setenv("XIAOMUSIC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NAS_IP", "192.168.x.x")
    import importlib

    import app as bridge
    importlib.reload(bridge)
    return bridge


def test_native_shim_awaits_like_device_player(tmp_path):
    """按 device_player 的真实调用方式 await 桩方法（复刻 612 行崩溃点）。"""
    pytest.importorskip("xiaomusic", reason="需要 xiaomusic 环境（容器内/开发 venv）")
    import asyncio
    import inspect
    import logging

    from native.config import build_config
    from native.core import NativeCore

    conf = tmp_path / "conf"
    music = tmp_path / "music"
    conf.mkdir(parents=True)
    music.mkdir(parents=True)
    cfg = build_config(conf_dir=conf, music_dir=music,
                       nas_ip="192.168.x.x", public_port=8775)

    async def _build():
        return NativeCore(cfg, logging.getLogger("test"))

    core = asyncio.run(_build())
    # device_player.py:612 —— 这一行崩过，必须可 await
    assert inspect.iscoroutinefunction(core.analytics.send_play_event)
    asyncio.run(core.analytics.send_play_event("晴天", 239.0, "LX06"))
    # 其余桩方法同样必须可 await
    for m in (core.do_tts, core.handle_fatal_error, core.add_singer_song):
        assert inspect.iscoroutinefunction(m), m


def test_native_playlist_entry_urls_are_speaker_reachable(tmp_path, monkeypatch):
    """native 引擎下 build_playlist 生成的条目 URL 必须是音箱可达地址。"""
    pytest.importorskip("xiaomusic", reason="需要 xiaomusic 环境（容器内/开发 venv）")
    bridge = _fresh_bridge(tmp_path, monkeypatch)

    bridge.CFG["engine"] = "native"
    bridge.CFG["nas_ip"] = "192.168.x.x"
    playlist = bridge.build_playlist([
        {"guid": "abc123", "title": "晴天",
         "artists": [{"name": "周杰伦"}], "album": {"name": "叶惠美"}},
    ])
    url = playlist[0]["musics"][0]["url"]
    assert url == "http://192.168.x.x:8775/play?guid=abc123"
    # xiaomusic 引擎保持内环地址（代理服务端拉取）
    bridge.CFG["engine"] = "xiaomusic"
    url2 = bridge.build_playlist([
        {"guid": "abc123", "title": "晴天", "artists": [], "album": {}},
    ])[0]["musics"][0]["url"]
    assert url2.startswith("http://127.0.0.1:")


def test_app_engine_switch_endpoint(tmp_path, monkeypatch):
    """引擎切换端点：持久化 + 校验 + health 反映当前引擎（不起服务）。"""
    pytest.importorskip("xiaomusic", reason="需要 xiaomusic 环境（容器内/开发 venv）")
    bridge = _fresh_bridge(tmp_path, monkeypatch)

    class _Req:
        def __init__(self, data):
            self._data = data

        async def json(self):
            return self._data

    import asyncio
    import json

    # 默认 xiaomusic
    assert bridge.CFG.get("engine") == "xiaomusic"
    # 非法值拒绝
    r = asyncio.run(bridge.set_engine(_Req({"engine": "bogus"})))
    assert r.status_code == 400
    # 切 native：落盘 + 内存生效
    r = asyncio.run(bridge.set_engine(_Req({"engine": "native"})))
    assert r["ok"] and r["engine"] == "native"
    saved = json.loads((tmp_path / "bridge" / "config.json").read_text(encoding="utf-8"))
    assert saved["engine"] == "native"
    assert bridge.CFG["engine"] == "native"
